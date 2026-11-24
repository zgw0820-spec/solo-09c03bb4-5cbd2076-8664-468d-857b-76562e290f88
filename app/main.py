"""Git bundle 批量导入服务。

把多份（至多 6 份）乱序的 git bundle 导入本地裸仓库：

* 按各包声明的前置提交（prerequisite）自动排出可用的验包顺序；
* 缺包时指出缺失的提交；
* 只更新分支（refs/heads/*），标签等其它引用一律忽略；
* 仓库与各包中的同名分支必须能排成同一条快进链，生效后指向链尾；
* 验包在带 alternates 的隔离仓库里进行：照常读取现行分支，不复制已有对象库；
* 全部包通过校验后才一起更新涉及的分支；验包期间分支已变或任一包校验失败，
  整批退回，仓库里已生效的分支保持原状。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

REPO_PATH = os.environ.get("REPO_PATH", "/srv/repo.git")
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "")
SAMPLES_BAD_DIR = os.environ.get("SAMPLES_BAD_DIR", "")
MAX_BUNDLES = 6
ZERO_SHA = "0" * 40
HEADS_PREFIX = "refs/heads/"
TMP_REFS_PREFIX = "refs/import/"  # 对象迁入正式库时使用的临时引用空间

app = FastAPI(title="Git bundle 批量导入")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["short"] = lambda s: (s or "")[:12]
import_lock = threading.Lock()


# --------------------------------------------------------------------------
# git 基础操作
# --------------------------------------------------------------------------

class GitError(Exception):
    def __init__(self, args, stderr):
        super().__init__(f"git {' '.join(args)}: {stderr}")
        self.stderr = stderr


def git(repo, *args, input_text=None, check=True):
    cmd = ["git"]
    if repo:
        cmd += ["-C", str(repo)]
    cmd += [str(a) for a in args]
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and proc.returncode != 0:
        raise GitError(cmd, proc.stderr.strip())
    return proc


def list_refs(repo, prefix=""):
    """返回 {完整引用名: sha}。prefix 如 'refs/heads/'。"""
    out = git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)",
              prefix, check=False).stdout
    refs = {}
    for line in out.splitlines():
        if "\x00" in line:
            ref, sha = line.split("\x00", 1)
            refs[ref] = sha
    return refs


def list_branches(repo):
    return list_refs(repo, HEADS_PREFIX)


def is_ancestor(repo, old, new):
    """old 是否为 new 的祖先（对象不存在时视为否）。"""
    return git(repo, "merge-base", "--is-ancestor", old, new,
               check=False).returncode == 0


# --------------------------------------------------------------------------
# bundle 解析与校验
# --------------------------------------------------------------------------

HEX_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass
class Bundle:
    name: str                                  # 原始上传文件名
    path: str                                  # 落盘路径
    prereqs: list = field(default_factory=list)  # [(sha, 注释)]
    heads: dict = field(default_factory=dict)    # refs/heads/* -> sha
    ignored: dict = field(default_factory=dict)  # 其它引用（标签等，不导入）


def parse_bundle(path, name):
    """直接解析 bundle 文件头，取出前置提交与引用列表。"""
    prereqs, heads, ignored = [], {}, {}
    with open(path, "rb") as fh:
        first = fh.readline()
        if not first.startswith(b"# v") or b"git bundle" not in first:
            raise ValueError("不是有效的 git bundle 文件")
        for raw in fh:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line or line.startswith("PACK"):
                break  # 头部结束，后面是 pack 数据
            if line.startswith("@"):  # v3 能力行，如 @object-format=sha256
                continue
            if line.startswith("-"):
                parts = line[1:].split(" ", 1)
                prereqs.append((parts[0], parts[1] if len(parts) > 1 else ""))
                continue
            parts = line.split(" ")
            if len(parts) == 2 and HEX_RE.match(parts[0]):
                sha, ref = parts
                if ref.startswith(HEADS_PREFIX):
                    heads[ref] = sha
                else:
                    ignored[ref] = sha
            else:
                break
    return Bundle(name=name, path=path, prereqs=prereqs, heads=heads,
                  ignored=ignored)


def missing_prereqs(repo, bundle):
    """前置提交中未能从 repo 任一引用到达的列表（[(sha, 注释)]）。"""
    tips = list(list_refs(repo).values())
    missing = []
    for sha, comment in bundle.prereqs:
        if not any(is_ancestor(repo, sha, tip) for tip in tips):
            missing.append((sha, comment))
    return missing


def describe(bundle):
    return {
        "file": bundle.name,
        "prereqs": bundle.prereqs,
        "refs": {ref[len(HEADS_PREFIX):]: sha for ref, sha in bundle.heads.items()},
        "ignored": bundle.ignored,
    }


# --------------------------------------------------------------------------
# 导入主流程
# --------------------------------------------------------------------------

def import_bundles(repo, files):
    """把 files=[(文件名, 内容)] 的一批 bundle 导入 repo，返回结果字典。"""
    result = {"ok": False, "order": [], "updates": [], "unchanged": [],
              "reason": None, "missing": None}
    if not files:
        result["reason"] = "未选择任何包。"
        return result
    if len(files) > MAX_BUNDLES:
        result["reason"] = (f"一次最多导入 {MAX_BUNDLES} 份包，本次收到 "
                            f"{len(files)} 份，整批退回。")
        return result

    with import_lock:
        work = tempfile.mkdtemp(prefix="bundle-import-")
        tmp_refs_created = False
        try:
            # 1) 落盘并解析包头
            bundles = []
            bundle_dir = os.path.join(work, "bundles")
            os.makedirs(bundle_dir)
            for i, (fname, data) in enumerate(files):
                safe = re.sub(r"[^\w.()-]", "_", os.path.basename(fname or ""))
                safe = safe or f"bundle-{i}"
                path = os.path.join(bundle_dir, f"{i:02d}-{safe}")
                with open(path, "wb") as fh:
                    fh.write(data)
                try:
                    bundles.append(parse_bundle(path, fname or safe))
                except ValueError as exc:
                    result["reason"] = f"包 {fname} 无法解析：{exc}。整批退回。"
                    return result
            for b in bundles:
                if not b.heads:
                    result["reason"] = (f"包 {b.name} 不含任何分支"
                                        f"（{HEADS_PREFIX}*），只更新分支，整批退回。")
                    return result

            # 2) 记录现行分支，并搭建隔离仓库：
            #    alternates 指向现行对象库（照常读取、无需复制），镜像现行引用。
            snapshot0 = list_branches(repo)
            q = os.path.join(work, "q.git")
            git(None, "init", "-q", "--bare", q)
            info_dir = os.path.join(q, "objects", "info")
            os.makedirs(info_dir, exist_ok=True)
            with open(os.path.join(info_dir, "alternates"), "w") as fh:
                fh.write(os.path.join(os.path.abspath(repo), "objects") + "\n")
            for ref, sha in list_refs(repo).items():
                git(q, "update-ref", ref, sha, check=False)

            # 3) 依前置提交确定验包顺序：能验则验，验过即取入隔离库，循环推进
            pending = list(range(len(bundles)))
            order = []
            verify_err = {}
            while pending:
                progressed = False
                for idx in list(pending):
                    b = bundles[idx]
                    proc = git(q, "bundle", "verify", b.path, check=False)
                    if proc.returncode == 0:
                        git(q, "fetch", "--no-tags", "--", b.path,
                            f"+{HEADS_PREFIX}*:refs/q/{idx}/heads/*")
                        pending.remove(idx)
                        order.append(idx)
                        progressed = True
                    else:
                        verify_err[idx] = (proc.stderr or proc.stdout).strip()
                if not progressed:
                    break
            result["order"] = [describe(bundles[i]) for i in order]
            if pending:
                missing = {}
                details = []
                for idx in pending:
                    b = bundles[idx]
                    miss = missing_prereqs(q, b)
                    if miss:
                        missing[b.name] = miss
                    else:
                        details.append(f"{b.name}: {verify_err.get(idx, '校验失败')}")
                result["missing"] = missing or None
                parts = []
                if missing:
                    parts.append("缺少前置提交")
                if details:
                    parts.append("；".join(details))
                result["reason"] = "，".join(parts) + "。整批退回，仓库分支保持原状。"
                return result

            # 4) 同名分支（仓库 + 各包）须能排成同一条快进链，生效后指向链尾
            candidates = {}
            for idx in order:
                for ref, sha in bundles[idx].heads.items():
                    candidates.setdefault(ref[len(HEADS_PREFIX):], set()).add(sha)
            for branch in list(candidates):
                old = snapshot0.get(HEADS_PREFIX + branch)
                if old:
                    candidates[branch].add(old)

            updates = []
            unchanged = []
            for branch in sorted(candidates):
                shas = sorted(candidates[branch])
                pair = None
                for a in range(len(shas)):
                    for b_ in range(a + 1, len(shas)):
                        x, y = shas[a], shas[b_]
                        if not is_ancestor(q, x, y) and not is_ancestor(q, y, x):
                            pair = (x, y)
                            break
                    if pair:
                        break
                if pair:
                    result["reason"] = (
                        f"分支 {branch} 在仓库与各包中的提交无法排成同一条快进链："
                        f"{pair[0][:12]} 与 {pair[1][:12]} 互不包含。"
                        "整批退回，仓库分支保持原状。")
                    return result
                tip = next(s for s in shas
                           if all(is_ancestor(q, t, s) for t in shas))
                old = snapshot0.get(HEADS_PREFIX + branch)
                if old == tip:
                    unchanged.append(branch)
                else:
                    updates.append({"branch": branch, "old": old, "new": tip})

            # 5) 验包期间分支已变 → 整批退回
            snapshot1 = list_branches(repo)
            if snapshot1 != snapshot0:
                changed = sorted(
                    ref[len(HEADS_PREFIX):]
                    for ref in set(snapshot0) | set(snapshot1)
                    if snapshot0.get(ref) != snapshot1.get(ref))
                result["reason"] = ("验包期间仓库分支已变化（"
                                    + "、".join(changed)
                                    + "），整批退回，仓库分支保持原状。")
                return result

            if not updates:
                result["ok"] = True
                result["unchanged"] = unchanged
                return result

            # 6) 全部通过：对象迁入正式库（临时引用空间），再一次性更新分支
            batch = uuid.uuid4().hex[:12]
            git(repo, "fetch", "--no-tags", "--", q,
                f"+refs/q/*:{TMP_REFS_PREFIX}{batch}/*")
            tmp_refs_created = True
            lines = ["start"]
            for u in updates:
                lines.append(f"update {HEADS_PREFIX}{u['branch']} "
                             f"{u['new']} {u['old'] or ZERO_SHA}")
            lines.append("commit")
            proc = git(repo, "update-ref", "--stdin",
                       input_text="\n".join(lines) + "\n", check=False)
            if proc.returncode != 0:
                result["reason"] = (
                    "更新分支失败（分支可能在验包期间被并发修改）："
                    f"{proc.stderr.strip()}。整批退回，仓库分支保持原状。")
                return result

            result["ok"] = True
            result["updates"] = updates
            result["unchanged"] = unchanged
            return result
        except GitError as exc:
            result["reason"] = f"内部 git 操作失败：{exc}。整批退回。"
            return result
        except Exception as exc:  # 兜底：任何意外错误都不应影响正式库
            result["reason"] = f"内部错误：{exc!r}。整批退回。"
            return result
        finally:
            if tmp_refs_created:
                stale = git(repo, "for-each-ref", "--format=%(refname)",
                            TMP_REFS_PREFIX, check=False).stdout.split()
                if stale:
                    git(repo, "update-ref", "--stdin",
                        input_text="".join(f"delete {r}\n" for r in stale),
                        check=False)
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 网页
# --------------------------------------------------------------------------

def page_context(result=None):
    try:
        branches = list_branches(REPO_PATH)
        repo_error = None
    except Exception as exc:  # 仓库尚未初始化等
        branches, repo_error = {}, str(exc)
    return {
        "repo": REPO_PATH,
        "branches": branches,
        "repo_error": repo_error,
        "samples": list_sample_files(SAMPLES_DIR),
        "bad_samples": list_sample_files(SAMPLES_BAD_DIR),
        "max_bundles": MAX_BUNDLES,
        "result": result,
    }


def list_sample_files(directory):
    if directory and os.path.isdir(directory):
        return sorted(f for f in os.listdir(directory) if f.endswith(".bundle"))
    return []


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", page_context())


@app.post("/import", response_class=HTMLResponse)
async def import_upload(request: Request):
    form = await request.form()
    files = []
    for item in form.getlist("files"):
        if isinstance(item, UploadFile) and item.filename:
            files.append((item.filename, await item.read()))
    result = await run_in_threadpool(import_bundles, REPO_PATH, files)
    return templates.TemplateResponse(request, "index.html", page_context(result))


@app.post("/import-samples", response_class=HTMLResponse)
async def import_samples(request: Request):
    form = await request.form()
    files = []
    for name in form.getlist("sample"):
        base = os.path.basename(name)
        for directory in (SAMPLES_DIR, SAMPLES_BAD_DIR):
            path = os.path.join(directory, base) if directory else ""
            if path and os.path.isfile(path):
                with open(path, "rb") as fh:
                    files.append((base, fh.read()))
                break
    result = await run_in_threadpool(import_bundles, REPO_PATH, files)
    return templates.TemplateResponse(request, "index.html", page_context(result))

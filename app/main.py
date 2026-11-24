"""Git bundle 批量导入服务。

把多份（至多 6 份）乱序的 git bundle 导入本地裸仓库：

* 按各包声明的前置提交（prerequisite）自动排出可用的验包顺序；
* 缺包时指出缺失的提交；
* 只更新分支（refs/heads/*），标签等其它引用一律忽略；
* 仓库与各包中的同名分支必须能排成同一条快进链，生效后指向链尾；
* 验包在带 alternates 的隔离仓库里进行：照常读取现行分支，不复制已有对象库；
  隔离目录由 TMPDIR 指定（默认系统临时目录）；
* 全部包通过校验后才一起更新涉及的分支；验包期间分支已变或任一包校验失败，
  整批退回，仓库里已生效的分支保持原状。

对象迁入（不重新打包，复用验包生成的对象文件）：

* 验包时 ``git fetch`` 进隔离库产生的 pack/松散对象文件，就是迁入正式库的来源，
  不再对 bundle 或隔离库二次打包；
* 隔离目录与正式库在同一文件系统时，用硬链接把对象文件装进正式库，磁盘上只有
  一份数据；跨文件系统（``EXDEV``）时回退为复制对象文件；
* 正式库中已存在的对象文件一律跳过，绝不覆盖或删除；
* 更新分支前，正式库必须能脱离隔离库自行读全本批分支可达的全部对象
  （提交、树、文件 blob）；迁入出错、自检缺对象或分支更新事务被拒，本批不发布，
  撤回本批新增的全部正式对象文件。

前提：本服务单进程写库，不与外部写库或对象回收（gc）并行。
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import tempfile
import threading
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
# 迁入正式库时认可的对象文件后缀（objects/pack/ 内）
PACK_SUFFIXES = (".pack", ".idx", ".rev", ".keep")

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
# 对象迁入：复用隔离库验包生成的对象文件，不重新打包
# --------------------------------------------------------------------------

def reachable_object_shas(repo, tips):
    """一组尖端提交可达的全部对象 sha（提交、树、标签、文件 blob）。"""
    proc = git(repo, "rev-list", "--objects", *tips)
    return {line.split(" ", 1)[0]
            for line in proc.stdout.splitlines() if line}


def missing_objects_locally(repo, tips):
    """正式库不借助任何 alternate 时，读不到的对象 sha 列表。

    本服务从不给正式库配置 alternates；若发现正式库自带 alternates，
    直接报错，避免把“借隔离库读到对象”误判成正式库已具备该对象。
    """
    alt_file = os.path.join(repo, "objects", "info", "alternates")
    if os.path.exists(alt_file):
        raise GitError(["cat-file"],
                       f"正式库存在 alternates（{alt_file}），"
                       "拒绝在借用外部对象库的情况下发布。")
    shas = sorted(reachable_object_shas(repo, tips))
    if not shas:
        return []
    env = {**os.environ, "GIT_ALTERNATE_OBJECT_DIRECTORIES": ""}
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch-check"],
        input="\n".join(shas) + "\n", capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise GitError(["cat-file", "--batch-check"], proc.stderr.strip())
    missing = []
    for line in proc.stdout.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) == 2 and parts[1] == "missing":
            missing.append(parts[0])
    return missing


def _link_or_copy(src, dst, installed, skipped):
    """把单个对象文件装进正式库：已存在则跳过；优先硬链接，跨文件系统则复制。"""
    if dst.exists():
        skipped.append(str(dst))
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 先登记：即便落盘动作本身抛异常（如复制中途 EIO），回滚也能找到半截文件。
    # 失败时就地清理本文件并撤销本条登记；此前已成功的登记必须保留给回滚。
    installed.append(str(dst))
    try:
        os.link(src, dst)  # 同一文件系统：只加目录项，不写第二份数据
    except FileExistsError:
        installed.pop()
        skipped.append(str(dst))
        return
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            dst.unlink(missing_ok=True)
            installed.pop()
            raise
        # 跨文件系统（EXDEV）或硬链接不可用：回退为复制对象文件
        try:
            shutil.copyfile(src, dst)
        except BaseException:
            dst.unlink(missing_ok=True)  # 不留半截文件，避免后续被误判为已存在
            installed.pop()
            raise


def install_quarantine_objects(q_repo, repo, installed=None):
    """把隔离库验包产生的对象文件装入正式库，返回 (本批新增路径, 已存在跳过路径)。

    只搬运 git 对象文件：``objects/xx/<38hex>`` 松散对象与
    ``objects/pack/`` 下的 pack/idx（及 rev/keep）；``objects/info`` 不动。
    正式库已有同名文件一律跳过，不覆盖、不删除。

    ``installed`` 由调用方传入并持有（累加器）：安装中途即便抛异常，调用方
    仍能凭该清单逐个撤回；返回值里的新增清单与它是同一个列表。
    """
    q_obj = Path(q_repo) / "objects"
    r_obj = Path(repo) / "objects"
    if installed is None:
        installed = []
    skipped = []

    for shard in sorted(q_obj.iterdir()):
        # 仅认两位十六进制的松散对象分片目录；pack/、info/ 另行处理
        if not (shard.is_dir() and len(shard.name) == 2
                and re.fullmatch(r"[0-9a-f]{2}", shard.name)):
            continue
        target_dir = r_obj / shard.name
        for src in sorted(shard.iterdir()):
            if src.is_file():
                _link_or_copy(src, target_dir / src.name, installed, skipped)

    q_pack = q_obj / "pack"
    if q_pack.is_dir():
        r_pack = r_obj / "pack"
        for src in sorted(q_pack.iterdir()):
            if src.is_file() and src.suffix in PACK_SUFFIXES:
                _link_or_copy(src, r_pack / src.name, installed, skipped)

    return installed, skipped


def rollback_installed(installed):
    """撤回本批新增的正式对象文件，并清理因此变空的对象分片目录。

    只删本批记录在案的新文件；正式库原有文件从未被覆盖或删除，此处也碰不到。
    删除顺序很重要：先删全部 ``.idx`` 再删 ``.pack``，这样即使回滚被信号打断，
    仓库里也不会留下“pack 与 idx 成对完整”的包被 git 自动发现而复活。
    """
    def _unlink(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    idx_files = [p for p in installed if p.endswith(".idx")]
    other_files = [p for p in installed if not p.endswith(".idx")]
    for path in idx_files:
        _unlink(path)
    for path in other_files:
        _unlink(path)
    for d in sorted({os.path.dirname(p) for p in installed}, reverse=True):
        try:
            os.rmdir(d)  # 仅当目录已空才删除，pack/ 等非空目录原样保留
        except OSError:
            pass


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
        installed = []  # 本批在正式库新增的对象文件（失败时逐个撤回）
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

            # 6) 全部通过：把验包生成的对象文件装入正式库（同文件系统硬链接、
            #    跨文件系统复制，均不重新打包），自检正式库脱离隔离库可读全对象，
            #    最后用 update-ref 事务（带原值校验）一次性更新分支。
            final_tips = {u["new"] for u in updates}
            final_tips.update(
                sha for sha in (snapshot0.get(HEADS_PREFIX + b)
                                for b in unchanged) if sha)
            try:
                # installed 是外层持有的累加器：安装中途异常也不丢清单，
                # except/finally 都能凭它撤回本批已装入的对象文件。
                install_quarantine_objects(q, repo, installed)
                missing_local = missing_objects_locally(repo, sorted(final_tips))
                if missing_local:
                    raise GitError(
                        ["cat-file"],
                        "正式库仍缺少 " + str(len(missing_local))
                        + " 个本批分支所需对象："
                        + "、".join(missing_local[:5])
                        + ("…" if len(missing_local) > 5 else ""))
            except Exception as exc:
                rollback_installed(installed)
                installed = []
                result["reason"] = (
                    f"对象迁入正式库失败，本批不发布，已撤回本批新增对象：{exc}。"
                    "整批退回，仓库分支保持原状。")
                return result

            lines = ["start"]
            for u in updates:
                lines.append(f"update {HEADS_PREFIX}{u['branch']} "
                             f"{u['new']} {u['old'] or ZERO_SHA}")
            lines.append("commit")
            proc = git(repo, "update-ref", "--stdin",
                       input_text="\n".join(lines) + "\n", check=False)
            if proc.returncode != 0:
                rollback_installed(installed)
                installed = []
                result["reason"] = (
                    "更新分支失败（分支可能在验包期间被并发修改）："
                    f"{proc.stderr.strip()}。本批不发布，已撤回本批新增对象，"
                    "仓库分支保持原状。")
                return result

            result["ok"] = True
            result["updates"] = updates
            result["unchanged"] = unchanged
            installed = []  # 发布成功，对象保留，finally 不再回滚
            return result
        except GitError as exc:
            result["reason"] = f"内部 git 操作失败：{exc}。整批退回。"
            return result
        except Exception as exc:  # 兜底：任何意外错误都不应影响正式库
            result["reason"] = f"内部错误：{exc!r}。整批退回。"
            return result
        finally:
            if installed:  # 走到异常兜底：尽力撤回本批新增的正式对象
                rollback_installed(installed)
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

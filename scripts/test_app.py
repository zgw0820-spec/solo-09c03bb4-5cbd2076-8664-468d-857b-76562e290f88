#!/usr/bin/env python3
"""关键操作的端到端测试：直接驱动 app.main.import_bundles。

覆盖：乱序自动排序导入、重复导入（无需更新）、缺前置提交、同名分支分叉、
验包期间分支被改、超量上传、非法文件、标签被忽略。
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
FAILED = []


def fresh_repo():
    d = tempfile.mkdtemp(prefix="test-repo-")
    repo = os.path.join(d, "repo.git")
    subprocess.run(["git", "clone", "-q", "--bare",
                    os.path.join(SAMPLES, "repo.git"), repo], check=True)
    return d, repo


def load(*names):
    files = []
    for n in names:
        with open(os.path.join(SAMPLES, n), "rb") as fh:
            files.append((os.path.basename(n), fh.read()))
    return files


def head(repo, branch):
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q",
                        f"refs/heads/{branch}"], capture_output=True, text=True)
    return p.stdout.strip() or None


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(name)


def bundle_tip(name, ref):
    b = m.parse_bundle(os.path.join(SAMPLES, name), name)
    return b.heads[f"refs/heads/{ref}"]


GOOD1, GOOD2, GOOD3 = ("bundles/01-main-c4-feature-f1.bundle",
                       "bundles/02-main-c5.bundle",
                       "bundles/03-feature-f2.bundle")
BAD_MISS = "bad/missing-prereq-main-c7.bundle"
BAD_DIV = "bad/diverged-main.bundle"

# --- 1. 乱序上传 03/02/01，应自动按 01→02→03 验包并更新 --------------------
d, repo = fresh_repo()
try:
    main0, stable0 = head(repo, "main"), head(repo, "stable")
    c5, f2 = bundle_tip(GOOD2, "main"), bundle_tip(GOOD3, "feature")
    r = m.import_bundles(repo, load(GOOD3, GOOD2, GOOD1))  # 故意乱序
    check("乱序导入成功", r["ok"], r.get("reason") or "")
    seq = [os.path.basename(x["file"]) for x in r["order"]]
    check("验包顺序满足前置依赖（01 在 02、03 之前）",
          seq.index(os.path.basename(GOOD1)) < seq.index(os.path.basename(GOOD2))
          and seq.index(os.path.basename(GOOD1)) < seq.index(os.path.basename(GOOD3))
          and len(seq) == 3, str(seq))
    check("main 指向链尾 C5", head(repo, "main") == c5)
    check("feature 新建并指向 F2", head(repo, "feature") == f2)
    check("stable 未受影响", head(repo, "stable") == stable0)
    check("标签未被导入", subprocess.run(
        ["git", "-C", repo, "for-each-ref", "refs/tags/"],
        capture_output=True, text=True).stdout.strip() == "")
    upd = {u["branch"]: (u["old"], u["new"]) for u in r["updates"]}
    check("更新清单正确", upd.get("main") == (main0, c5) and
          upd.get("feature") == (None, f2), str(upd))

    # --- 2. 重复导入：全部已是最新 --------------------------------------
    r = m.import_bundles(repo, load(GOOD1, GOOD2, GOOD3))
    check("重复导入成功且无需更新", r["ok"] and not r["updates"]
          and sorted(r["unchanged"]) == ["feature", "main"], str(r))
finally:
    shutil.rmtree(d, ignore_errors=True)

# --- 3. 缺前置提交：指出缺失的提交，仓库不变 -------------------------------
d, repo = fresh_repo()
try:
    main0 = head(repo, "main")
    r = m.import_bundles(repo, load(GOOD1, BAD_MISS))
    c6 = m.parse_bundle(os.path.join(SAMPLES, BAD_MISS), "x").prereqs[0][0]
    check("缺包被退回", not r["ok"], r.get("reason") or "")
    check("指出缺失提交 C6", r["missing"] and
          any(sha == c6 for miss in r["missing"].values() for sha, _ in miss),
          str(r["missing"]))
    check("缺包时分支保持原状", head(repo, "main") == main0
          and head(repo, "feature") is None)
finally:
    shutil.rmtree(d, ignore_errors=True)

# --- 4. 同名分支分叉：整批退回 ---------------------------------------------
d, repo = fresh_repo()
try:
    r = m.import_bundles(repo, load(GOOD1, GOOD2, BAD_DIV))
    check("分叉被退回", not r["ok"] and "快进链" in (r["reason"] or ""),
          r.get("reason") or "")
    check("分叉时 main 未被更新", head(repo, "main") != bundle_tip(GOOD2, "main")
          and head(repo, "feature") is None)
finally:
    shutil.rmtree(d, ignore_errors=True)

# --- 5. 验包期间分支已变：整批退回 -----------------------------------------
d, repo = fresh_repo()
try:
    orig = m.list_branches
    calls = {"n": 0}

    def fake_list_branches(r):
        calls["n"] += 1
        snap = orig(r)
        if calls["n"] == 2:  # 第二次快照（验包后）模拟并发修改
            snap = dict(snap)
            snap["refs/heads/stable"] = "0" * 40
        return snap

    m.list_branches = fake_list_branches
    try:
        r = m.import_bundles(repo, load(GOOD1, GOOD2, GOOD3))
    finally:
        m.list_branches = orig
    check("验包期间分支已变被退回", not r["ok"] and "已变化" in (r["reason"] or ""),
          r.get("reason") or "")
    check("并发变化时分支保持原状", head(repo, "feature") is None)
finally:
    shutil.rmtree(d, ignore_errors=True)

# --- 6. 超过 6 份：退回 -----------------------------------------------------
d, repo = fresh_repo()
try:
    r = m.import_bundles(repo, load(GOOD1) * 7)
    check("超过 6 份被退回", not r["ok"] and "最多" in (r["reason"] or ""),
          r.get("reason") or "")
finally:
    shutil.rmtree(d, ignore_errors=True)

# --- 7. 非法文件：退回 ------------------------------------------------------
d, repo = fresh_repo()
try:
    r = m.import_bundles(repo, [("junk.bundle", b"not a bundle")])
    check("非法文件被退回", not r["ok"] and "无法解析" in (r["reason"] or ""),
          r.get("reason") or "")
finally:
    shutil.rmtree(d, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部测试通过")

#!/usr/bin/env python3
"""对象迁入（复用验包对象文件）专项测试。

覆盖需求：
* 同一文件系统迁入用硬链接，不写第二份数据，且不重新打包；
* 跨文件系统（模拟 EXDEV）回退为复制对象文件；
* 正式库已有对象文件不覆盖、不删除；
* 发布前正式库脱离隔离库读全本批分支所需对象；
* 迁入出错、分支更新被拒 → 本批不发布，撤回本批新增对象；
* 正式库出现 alternates 时拒绝发布；
* TMPDIR 指定隔离目录。
"""
import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
FAILED = []
GOOD1, GOOD2, GOOD3 = ("bundles/01-main-c4-feature-f1.bundle",
                       "bundles/02-main-c5.bundle",
                       "bundles/03-feature-f2.bundle")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(name)


def fresh_repo():
    d = tempfile.mkdtemp(prefix="test-mig-")
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


def snapshot_files(repo):
    """{路径: (大小, mtime_ns)}，覆盖 objects/ 下全部文件。"""
    snap = {}
    for base, _, names in os.walk(os.path.join(repo, "objects")):
        for n in names:
            p = os.path.join(base, n)
            st = os.stat(p)
            snap[p] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return snap


def list_packs(repo):
    pdir = os.path.join(repo, "objects", "pack")
    return sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []


def run_import_good(repo):
    return m.import_bundles(repo, load(GOOD1, GOOD2, GOOD3))


# --- 1. 同文件系统：硬链接迁入，只有一份数据，不重新打包 --------------------
d, repo = fresh_repo()
try:
    before = snapshot_files(repo)
    # 在隔离目录被清理前截胡一次，以便观察硬链接的真实链接数
    observed = {}
    real_rmtree = shutil.rmtree

    def spy_rmtree(path, *a, **k):
        if not observed and os.path.basename(str(path)).startswith("bundle-import-"):
            q_pack = os.path.join(str(path), "q.git", "objects", "pack")
            for f in os.listdir(q_pack):
                p = os.path.join(q_pack, f)
                rp = os.path.join(repo, "objects", "pack", f)
                if os.path.exists(rp):
                    observed[f] = (os.stat(p).st_ino, os.stat(rp).st_ino,
                                   os.stat(rp).st_nlink)
        return real_rmtree(path, *a, **k)

    shutil.rmtree = spy_rmtree
    try:
        r = run_import_good(repo)
    finally:
        shutil.rmtree = real_rmtree
    check("正常发布成功", r["ok"], r.get("reason") or "")
    after = snapshot_files(repo)
    new_files = sorted(set(after) - set(before))
    check("迁入产生了对象文件", bool(new_files), f"{len(new_files)} 个新文件")
    check("新文件全是 pack/idx（未产生额外松散对象）",
          all("/objects/pack/" in p for p in new_files),
          str([os.path.relpath(p, repo) for p in new_files]))
    check("正式库新增 pack 与隔离库命名一致（复用同一 pack，未重新打包）",
          {os.path.basename(p) for p in new_files}
          <= {f for f in list_packs(repo)})
    check("同一文件系统上全部硬链接（与隔离库同 inode、nlink>=2）",
          observed and all(v[0] == v[1] and v[2] >= 2
                           for v in observed.values()),
          str(observed))
    # 正式库独立完整性：脱离任何 alternates 做 fsck 与对象读取
    fsck = subprocess.run(["git", "-C", repo, "fsck", "--full"],
                          capture_output=True, text=True)
    check("发布后正式库 fsck 干净", fsck.returncode == 0, fsck.stderr.strip())
    main_tip = head(repo, "main")
    feat_tip = head(repo, "feature")
    miss = m.missing_objects_locally(repo, [main_tip, feat_tip])
    check("正式库不借 alternate 读全 main/feature 全部对象", miss == [], str(miss[:3]))
    # 历史内容可独立读出（提交、树、blob 全链路）
    log = subprocess.run(["git", "-C", repo, "log", "--oneline", main_tip],
                         capture_output=True, text=True)
    check("正式库可独立读取完整提交历史", log.returncode == 0
          and len(log.stdout.splitlines()) == 5, str(len(log.stdout.splitlines())))
    blob = subprocess.run(["git", "-C", repo, "show", f"{main_tip}:a.txt"],
                          capture_output=True, text=True)
    check("正式库可独立读取文件 blob", blob.returncode == 0
          and blob.stdout.splitlines() == ["one", "two", "three",
                                           "four", "five"], blob.stdout.strip())
    check("正式库未配置 alternates",
          not os.path.exists(os.path.join(repo, "objects", "info", "alternates")))
    check("无 refs/import 临时引用残留",
          m.list_refs(repo, "refs/import/") == {})
    # 隔离目录已清理（mkdtemp 前缀）
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("bundle-import-")]
    check("隔离临时目录已清理", leftovers == [], str(leftovers))
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 2. 重复导入：无更新、无新增文件 ---------------------------------------
d, repo = fresh_repo()
try:
    run_import_good(repo)
    before = snapshot_files(repo)
    r = run_import_good(repo)
    check("重复导入成功且无更新", r["ok"] and not r["updates"])
    check("无更新时不新增对象文件", snapshot_files(repo) == before)
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 3. 已有对象文件不覆盖、不删除 -----------------------------------------
d, repo = fresh_repo()
try:
    # 先成功发布一次，再以“第二次发布”的方式跑同样的包：目标 pack 已存在
    run_import_good(repo)
    before = snapshot_files(repo)
    r = run_import_good(repo)
    check("再次导入不报错", r["ok"])
    check("已有对象文件的 inode/大小/mtime 均未变化（未覆盖）",
          snapshot_files(repo) == before)
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 4. 跨文件系统：模拟 os.link 抛 EXDEV，回退复制 ------------------------
d, repo = fresh_repo()
try:
    real_link = os.link
    calls = {"n": 0}

    def fake_link(src, dst, *a, **k):
        calls["n"] += 1
        raise OSError(errno.EXDEV, "simulated cross-device link")

    os.link = fake_link
    try:
        r = run_import_good(repo)
    finally:
        os.link = real_link
    check("跨文件系统时发布仍成功（回退复制）", r["ok"], r.get("reason") or "")
    check("确实尝试过硬链接并收到 EXDEV", calls["n"] > 0, str(calls["n"]))
    new_packs = [os.path.join(repo, "objects", "pack", f)
                 for f in list_packs(repo)]
    check("复制得到的对象文件 nlink==1（独立第二份数据）",
          all(os.stat(p).st_nlink == 1 for p in new_packs),
          str([os.stat(p).st_nlink for p in new_packs]))
    miss = m.missing_objects_locally(repo, [head(repo, "main"),
                                            head(repo, "feature")])
    check("复制迁入后正式库同样读全对象", miss == [], str(miss[:3]))
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 5. 迁入后自检失败：撤回本批全部新增对象，分支不动 ---------------------
d, repo = fresh_repo()
try:
    before = snapshot_files(repo)
    main0 = head(repo, "main")
    real_check = m.missing_objects_locally

    def fail_check(r, tips):
        raise OSError(errno.EIO, "simulated post-install io error")

    m.missing_objects_locally = fail_check
    try:
        r = run_import_good(repo)
    finally:
        m.missing_objects_locally = real_check
    check("自检失败时整批退回", not r["ok"] and "迁入" in (r["reason"] or ""),
          r.get("reason") or "")
    check("分支保持原状", head(repo, "main") == main0
          and head(repo, "feature") is None)
    check("本批新增对象已全部撤回（含 idx 与 pack）",
          snapshot_files(repo) == before,
          str(set(snapshot_files(repo)) ^ set(before)))
    fsck = subprocess.run(["git", "-C", repo, "fsck", "--full"],
                          capture_output=True, text=True)
    check("撤回后正式库 fsck 干净", fsck.returncode == 0, fsck.stderr.strip())
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 5b. 安装中途失败：已装入的部分同样被撤回，且无完整 pack 残留 ----------
d, repo = fresh_repo()
try:
    before = snapshot_files(repo)
    main0 = head(repo, "main")
    real_link = os.link
    state = {"n": 0}

    def flaky_link(src, dst, *a, **k):
        state["n"] += 1
        if state["n"] <= 2:  # 先装入第一个包的 .idx/.pack，之后失败
            return real_link(src, dst)
        raise OSError(errno.EIO, "simulated mid-install io error")

    os.link = flaky_link
    try:
        r = run_import_good(repo)
    finally:
        os.link = real_link
    check("安装中途出错整批退回", not r["ok"], r.get("reason") or "")
    check("分支保持原状", head(repo, "main") == main0
          and head(repo, "feature") is None)
    check("已装入的部分对象被撤回", snapshot_files(repo) == before,
          str(set(snapshot_files(repo)) ^ set(before)))
    # 关键：残留的孤立 pack（无 idx）不会被 git 当作可用包
    orphans = [f for f in list_packs(repo) if f.endswith(".pack")
               and not os.path.exists(os.path.join(
                   repo, "objects", "pack", f[:-5] + ".idx"))]
    check("不存在 pack+idx 成对残留", orphans == [], str(orphans))
    fsck = subprocess.run(["git", "-C", repo, "fsck", "--full"],
                          capture_output=True, text=True)
    check("中途失败撤回后 fsck 干净", fsck.returncode == 0, fsck.stderr.strip())
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 6. 分支更新事务被拒：撤回本批新增对象，分支不动 -----------------------
d, repo = fresh_repo()
try:
    before = snapshot_files(repo)
    main0 = head(repo, "main")
    # 预置 main 的 ref 锁，使 update-ref --stdin 事务被拒
    lock = os.path.join(repo, "refs", "heads", "main.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w") as fh:
        fh.write("0" * 40)
    try:
        r = run_import_good(repo)
    finally:
        os.unlink(lock)
    check("分支更新被拒时整批退回", not r["ok"]
          and "更新分支失败" in (r["reason"] or ""), r.get("reason") or "")
    check("被拒时分支保持原状", head(repo, "main") == main0
          and head(repo, "feature") is None)
    check("被拒后本批新增对象已全部撤回", snapshot_files(repo) == before,
          str(set(snapshot_files(repo)) ^ set(before)))
    fsck = subprocess.run(["git", "-C", repo, "fsck", "--full"],
                          capture_output=True, text=True)
    check("被拒撤回后正式库 fsck 干净", fsck.returncode == 0, fsck.stderr.strip())
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 7. 正式库自带 alternates：拒绝发布（不得借隔离库） --------------------
d, repo = fresh_repo()
try:
    alt_dir = os.path.join(repo, "objects", "info")
    os.makedirs(alt_dir, exist_ok=True)
    with open(os.path.join(alt_dir, "alternates"), "w") as fh:
        fh.write("/nonexistent/alternate/objects\n")
    r = run_import_good(repo)
    check("正式库有 alternates 时拒绝发布", not r["ok"]
          and "alternates" in (r["reason"] or ""), r.get("reason") or "")
    check("拒绝发布时分支不动", head(repo, "feature") is None)
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 8. TMPDIR 指定隔离目录位置 --------------------------------------------
d, repo = fresh_repo()
custom_tmp = os.path.join(d, "my-tmp")
os.makedirs(custom_tmp)
old_tmpdir = os.environ.get("TMPDIR")
os.environ["TMPDIR"] = custom_tmp
try:
    try:
        r = run_import_good(repo)
        check("TMPDIR 下发布成功", r["ok"], r.get("reason") or "")
        leftovers = os.listdir(custom_tmp)
        check("隔离目录建在 TMPDIR 且收尾已清理", leftovers == [], str(leftovers))
    finally:
        if old_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmpdir
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 9. 单元级：install/rollback 对松散对象与既有文件的处理 ----------------
d = tempfile.mkdtemp(prefix="test-unit-")
try:
    q = os.path.join(d, "q.git")
    target = os.path.join(d, "repo.git")
    subprocess.run(["git", "init", "-q", "--bare", q], check=True)
    subprocess.run(["git", "init", "-q", "--bare", target], check=True)
    # 手工构造一个松散对象：用 git hash-object 写入隔离库
    blob_file = os.path.join(d, "blob.bin")
    with open(blob_file, "wb") as fh:
        fh.write(b"hello-migration\n")
    sha = subprocess.run(["git", "-C", q, "hash-object", "-w", blob_file],
                         capture_output=True, text=True, check=True).stdout.strip()
    src_loose = os.path.join(q, "objects", sha[:2], sha[2:])
    dst_loose = os.path.join(target, "objects", sha[:2], sha[2:])
    check("隔离库松散对象已就位", os.path.isfile(src_loose))
    installed, skipped = m.install_quarantine_objects(q, target)
    check("松散对象硬链接安装成功", os.path.isfile(dst_loose)
          and os.stat(dst_loose).st_nlink == 2)
    check("安装清单记录了新文件", installed == [dst_loose], str(installed))
    # 再装一次：目标已存在，必须跳过且不覆盖
    st_before = os.stat(dst_loose)
    installed2, skipped2 = m.install_quarantine_objects(q, target)
    st_after = os.stat(dst_loose)
    check("已存在的对象文件被跳过（不覆盖）", installed2 == []
          and skipped2 == [dst_loose]
          and (st_after.st_ino, st_after.st_size) == (st_before.st_ino,
                                                      st_before.st_size))
    # 回滚只删本批新增；模拟“正式库原有文件”不应被动到
    other = os.path.join(target, "objects", "aa", "b" * 38)
    os.makedirs(os.path.dirname(other), exist_ok=True)
    with open(other, "wb") as fh:
        fh.write(b"preexisting")
    m.rollback_installed(installed)
    check("回滚删除本批新增松散对象", not os.path.exists(dst_loose))
    check("回滚不碰正式库原有文件", os.path.isfile(other))
    check("回滚清理变空的分片目录",
          not os.path.isdir(os.path.dirname(dst_loose)))
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 10. 单元级：跨设备回退复制 + 回滚清理 --------------------------------
d = tempfile.mkdtemp(prefix="test-unit2-")
try:
    q = os.path.join(d, "q.git")
    target = os.path.join(d, "repo.git")
    subprocess.run(["git", "init", "-q", "--bare", q], check=True)
    subprocess.run(["git", "init", "-q", "--bare", target], check=True)
    blob_file = os.path.join(d, "blob.bin")
    with open(blob_file, "wb") as fh:
        fh.write(b"cross-device-content\n")
    sha = subprocess.run(["git", "-C", q, "hash-object", "-w", blob_file],
                         capture_output=True, text=True, check=True).stdout.strip()
    real_link = os.link

    def exdev(src, dst, *a, **k):
        raise OSError(errno.EXDEV, "cross-device")

    os.link = exdev
    try:
        installed, skipped = m.install_quarantine_objects(q, target)
    finally:
        os.link = real_link
    dst_loose = os.path.join(target, "objects", sha[:2], sha[2:])
    check("EXDEV 时复制出独立文件", os.path.isfile(dst_loose)
          and os.stat(dst_loose).st_nlink == 1)
    m.rollback_installed(installed)
    check("复制文件回滚同样被撤回", not os.path.exists(dst_loose))
finally:
    shutil.rmtree(d, ignore_errors=True)


# --- 11. 隔离库 alternates 指向的正式库对象不会被重复搬运 ------------------
d, repo = fresh_repo()
try:
    # 正式库原有松散对象数量基线
    before = snapshot_files(repo)
    r = run_import_good(repo)
    check("发布成功", r["ok"], r.get("reason") or "")
    after = snapshot_files(repo)
    # 原有文件一个都不能少、不能变
    check("正式库原有对象文件全部保留且未变",
          all(p in after and after[p] == before[p] for p in before))
    # 新增的只有 pack/idx
    check("新增项仅为 pack 文件",
          all("/objects/pack/" in p for p in set(after) - set(before)))
finally:
    shutil.rmtree(d, ignore_errors=True)


print()
if FAILED:
    print(f"{len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部迁入测试通过")

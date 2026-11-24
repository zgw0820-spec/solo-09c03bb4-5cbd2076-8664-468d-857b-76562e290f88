# Git bundle 批量导入服务

隔离机房场景：收到几份乱序的 Git 增量包（git bundle），需要更新供部署使用的本地裸仓库。
本服务提供导入网页：一次上传至多 6 份 bundle，自动按前置提交排出验包顺序，
全部校验通过后一次性更新涉及的分支；任一环节失败则整批退回，仓库保持原状。

## 功能

- **批量上传**：网页一次最多上传 6 份 `.bundle`，可乱序。
- **自动排序**：解析各包头部声明的前置提交（prerequisite），循环验包——
  前置提交已能从（隔离环境中的）任一引用到达即验过并取入，据此排出可用的验包顺序。
- **缺包提示**：无法推进时，指出每份未过验的包缺失哪些前置提交（完整 SHA 及注释）。
- **只更新分支**：仅处理 `refs/heads/*`，包内标签等其它引用一律忽略（页面会列出被忽略的引用）。
- **快进链检查**：同名分支在仓库和各包中的提交必须能排成同一条快进链（两两可比），
  生效后分支指向链尾；分叉（互不包含）则整批退回。
- **隔离验包**：在临时隔离仓库中验包，`objects/info/alternates` 指向现行对象库——
  照常读取现行分支与对象，无需复制已有对象库；验包期间正式库完全不被触碰。
  隔离目录由环境变量 `TMPDIR` 指定（默认系统临时目录）。
- **对象迁入不重新打包**：验包时 `git fetch` 进隔离库生成的 pack/松散对象文件，
  就是迁入正式库的唯一来源，不再对 bundle 或隔离库二次 `fetch`/打包。
  - 同一文件系统：用硬链接（`os.link`）把对象文件装进正式库，磁盘上只有一份数据，
    不写第二份 pack；
  - 跨文件系统（硬链接报 `EXDEV` 等）：回退为复制对象文件；
  - 正式库已存在的同名对象文件一律跳过，**绝不覆盖或删除**。
- **发布前独立可读自检**：更新分支前，正式库必须脱离隔离库（清空
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`，并拒绝自带 alternates 的正式库）自行读全
  本批分支可达的全部提交、树、文件 blob；读不全则不发布。
- **失败整批撤回**：迁入出错、自检缺对象或分支更新事务被拒，本批一律不发布，
  撤回本批新增的全部正式对象文件（先删 `.idx` 再删 `.pack`，避免半截包被 git
  自动发现；并清理变空的对象分片目录）。正式库原有文件不受影响。
- **整批原子生效**：对象就位且自检通过后，用 `update-ref --stdin` 事务
  （带原值校验）一次性更新涉及的分支。
- **并发保护**：验包前后各取一次分支快照，验包期间分支已变 → 整批退回；
  最终更新事务的原值校验兜底；服务级互斥锁串行化导入。交付前提为单进程写库，
  不与外部写库或对象回收（gc）并行。
- **结果展示**：页面显示验包顺序（含各包前置提交与分支）、实际更新的分支（原提交 → 新提交）、
  未变化的分支，或整批退回的原因（缺失提交 / 分叉 / 分支已变 / 超量 / 非法文件）。

## 运行（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 首次启动自动把 `samples/repo.git` 种到数据卷 `/srv/repo.git`（已存在则跳过）。
- 入口脚本把 `TMPDIR` 设为 `/srv/tmp` 并自动建目录：隔离验包目录与正式库同在
  `/srv` 数据卷（同一文件系统），迁入对象走硬链接复用，不写第二份 pack；
  若把 `TMPDIR` 改指到其它挂载点（跨文件系统），服务自动回退为复制对象文件。
- 页面内置样例包勾选项：正常样例（`samples/bundles/`，相互依赖）与
  反例样例（`samples/bad/`，缺前置 / 分叉），勾选后一键导入，便于演示。
- 用自己的仓库：把卷 `repo-data` 换成指向你的裸仓库的绑定挂载，
  或设置 `REPO_PATH` 环境变量。

## 样例内容

提交图（`scripts/make_samples.sh` 生成，可重复执行重建）：

```
C1 ── C2 ── C3 ── C4 ── C5            main 方向
 │     └──── F1 ── F2                 feature 方向
 └── stable（指向 C1，演示无关分支不受影响）
```

| 文件 | 内容 | 前置提交 |
| --- | --- | --- |
| `samples/repo.git` | 裸仓库：`main=C2`、`stable=C1` | — |
| `samples/bundles/01-main-c4-feature-f1.bundle` | `main→C4`、`feature→F1`、标签 `v1.0`（被忽略） | `C2`（仓库已有） |
| `samples/bundles/02-main-c5.bundle` | `main→C5` | `C4`（由 01 提供） |
| `samples/bundles/03-feature-f2.bundle` | `feature→F2` | `F1`（由 01 提供） |
| `samples/bad/missing-prereq-main-c7.bundle` | `main→C7` | `C6`（无人提供 → 指出缺失） |
| `samples/bad/diverged-main.bundle` | `main→ALT`（与 C4/C5 分叉） | `C2` |

正常顺序为 01 → 02 → 03，可故意乱序上传验证自动排序。

## 本地开发与测试（不依赖 Docker）

```bash
pip install -r requirements.txt
scripts/make_samples.sh                      # 重建样例
python3 scripts/test_app.py                  # 端到端逻辑测试（16 项）
python3 scripts/test_migration.py            # 对象迁入/硬链接/回滚专项测试（49 项断言）
REPO_PATH=/tmp/repo.git SAMPLES_DIR=samples/bundles SAMPLES_BAD_DIR=samples/bad \
  TMPDIR=/tmp/bundle-tmp uvicorn app.main:app --port 8000   # 本地起服务
```

## 实现要点

- 验包顺序：对未决包循环执行 `git bundle verify`，验过即 `git fetch` 进隔离仓库
  命名空间 `refs/q/<序号>/`，使后续包的前置提交可达；一轮无推进则判定缺包。
- 缺失提交：对包头的每个前置提交，用 `merge-base --is-ancestor` 检查能否从
  隔离环境任一引用到达，列出不达者。
- 快进链：同名分支的候选提交（仓库现行值 + 各包尖端）两两做祖先检查，
  全部可比则取链尾为生效值；仓库现行值即链尾时记为"未变化"。
- 对象迁入（`install_quarantine_objects`）：遍历隔离库 `objects/` 下的两位十六
  进制松散对象分片与 `objects/pack/` 中的 `.pack/.idx`（含 `.rev/.keep`），
  逐个装入正式库；`objects/info`（含 alternates）不搬运。
  - 目标已存在 → 跳过，不覆盖；目标不存在 → 先登记进调用方持有的累加清单，
    再 `os.link`；`EXDEV/EPERM/EMLINK` 时回退 `shutil.copyfile`，复制失败即
    就地删除半截文件并撤销登记。
  - 由调用方持有累加清单是关键：安装中途异常时，函数内部局部清单无法通过返回值
    传出，外层仍能凭传入的清单完整回滚。
- 独立可读自检（`missing_objects_locally`）：对本批最终分支尖端
  `git rev-list --objects` 取全部可达对象，再用清空了
  `GIT_ALTERNATE_OBJECT_DIRECTORIES` 的 `git cat-file --batch-check` 批量核验；
  正式库若自带 alternates 直接拒绝发布，确保"不借用隔离库"。
- 回滚（`rollback_installed`）：只删本批登记的新文件，先删全部 `.idx` 再删
  `.pack`，保证回滚被信号打断时也不会留下 pack+idx 成对的"可复活"包；
  随后 `rmdir` 清理因此变空的分片目录，非空目录（如 `pack/`）原样保留。
- 原子更新：自检通过后，`update-ref --stdin` 的 `start`/`commit` 事务带原值
  一次提交；事务被拒则同样回滚本批对象，分支保持原状。

## 验证结果

- `scripts/test_app.py` 16 项全部通过（既有规则无回归）：乱序自动排序、链尾生效、
  新分支创建、无关分支不动、标签忽略、重复导入无操作、缺前置指出 C6、分叉退回、
  验包期间分支被改退回、超量退回、非法文件退回。
- `scripts/test_migration.py` 49 项断言全部通过，覆盖：
  - 同文件系统迁入为硬链接（与隔离库同 inode、nlink≥2），pack 命名一致、未重新打包；
  - 发布后正式库 `git fsck --full` 干净，脱离 alternates 可独立读全提交/树/blob；
  - 重复导入无新增文件；已有对象文件 inode/大小/mtime 不变（不覆盖）；
  - 模拟 `EXDEV` 回退复制，得到 nlink=1 的独立副本且对象仍读全；
  - 安装中途 EIO、自检失败、`update-ref` 事务被拒三种失败均整批退回、分支不动、
    本批对象全部撤回、无 pack+idx 成对残留、fsck 干净；
  - 正式库自带 alternates 时拒绝发布；`TMPDIR` 指定隔离目录且收尾清理；
  - 松散对象的硬链接/复制、跳过、回滚与空目录清理等单元行为。
- 网页实测（uvicorn + curl，从网页相互依赖的样例包导入）：
  - 同文件系统：乱序勾选 03/01/02 发布成功，main→C5、feature→F2、stable 不动；
  - 跨文件系统（`TMPDIR` 指向 tmpfs）：自动回退复制，发布成功、隔离目录清理、
    正式库独立读全对象；
  - 迁入失败：预置 `main.lock` 使分支更新事务被拒，页面提示整批退回，分支保持
    原状、本批 pack 全部撤回、fsck 干净；
  - 反例样例：缺前置包列出缺失的 C6 完整 SHA，分叉包退回，仓库分支不变。

## 待完成事项

- 本环境无 Docker 守护进程，`docker compose up` 未实际跑过；Dockerfile /
  compose / entrypoint 已做静态校验（`py_compile`、`sh -n`、YAML 解析），
  且等价的本地步骤（venv 安装、种子仓库克隆、带 `TMPDIR` 的 uvicorn 启动、
  网页导入与跨文件系统回退）均已实测，请在有 Docker 的机器上复验卷内硬链接。
- 未做上传大小限制与身份认证，内网隔离环境按需再加（反向代理或 FastAPI 中间件）。
- 回滚为尽力而为（best-effort）：若在回滚过程中再次发生 I/O 错误，可能残留孤立
  文件；先删 idx 后删 pack 的顺序保证其不会被 git 当作完整包复活，重启后可人工
  清理。单进程写库、无外部 gc 的交付前提下不会影响一致性。
- 并发场景以快照比对 + 事务原值校验兜底，未做跨进程文件锁（单进程部署足够）。

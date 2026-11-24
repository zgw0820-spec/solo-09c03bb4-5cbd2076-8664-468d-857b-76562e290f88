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
- **整批原子生效**：全部包通过校验后，先把对象迁入正式库临时引用空间，
  再用 `update-ref --stdin` 事务（带原值校验）一次性更新涉及的分支。
- **并发保护**：验包前后各取一次分支快照，验包期间分支已变 → 整批退回；
  最终更新事务的原值校验兜底；服务级互斥锁串行化导入。
- **结果展示**：页面显示验包顺序（含各包前置提交与分支）、实际更新的分支（原提交 → 新提交）、
  未变化的分支，或整批退回的原因（缺失提交 / 分叉 / 分支已变 / 超量 / 非法文件）。

## 运行（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 首次启动自动把 `samples/repo.git` 种到数据卷 `/srv/repo.git`（已存在则跳过）。
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
REPO_PATH=/tmp/repo.git SAMPLES_DIR=samples/bundles SAMPLES_BAD_DIR=samples/bad \
  uvicorn app.main:app --port 8000           # 本地起服务
```

## 实现要点

- 验包顺序：对未决包循环执行 `git bundle verify`，验过即 `git fetch` 进隔离仓库
  命名空间 `refs/q/<序号>/`，使后续包的前置提交可达；一轮无推进则判定缺包。
- 缺失提交：对包头的每个前置提交，用 `merge-base --is-ancestor` 检查能否从
  隔离环境任一引用到达，列出不达者。
- 快进链：同名分支的候选提交（仓库现行值 + 各包尖端）两两做祖先检查，
  全部可比则取链尾为生效值；仓库现行值即链尾时记为"未变化"。
- 原子更新：`git fetch` 把对象迁入正式库 `refs/import/<批次>/` 临时空间，
  `update-ref --stdin` 的 `start`/`commit` 事务带原值一次提交，事后清理临时引用。

## 验证结果

- `scripts/test_app.py` 16 项全部通过：乱序自动排序、链尾生效、新分支创建、
  无关分支不动、标签忽略、重复导入无操作、缺前置指出 C6、分叉退回、
  验包期间分支被改退回、超量退回、非法文件退回。
- 网页实测（uvicorn + curl）：乱序上传 3 包成功并按 01→02→03 展示；
  重复导入提示无需更新；混入缺前置包退回并列出缺失提交完整 SHA；
  混入分叉包退回并指出互不包含的两个提交；7 份上传退回。

## 待完成事项

- 本环境无 Docker 守护进程，`docker compose up` 未实际跑过；
  Dockerfile / compose / entrypoint 已做静态校验，且等价的本地步骤
  （pip 安装、种子仓库克隆、uvicorn 启动）均已实测，请在有 Docker 的机器上复验。
- 未做上传大小限制与身份认证，内网隔离环境按需再加（反向代理或 FastAPI 中间件）。
- 并发场景以快照比对 + 事务原值校验兜底，未做跨进程文件锁（单进程部署足够）。

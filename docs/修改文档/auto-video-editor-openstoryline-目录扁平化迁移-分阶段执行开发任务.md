# auto-video-editor：OpenStoryline 目录扁平化迁移——分阶段执行开发任务

> 文档类型：执行任务分解 / 进度跟踪
> 上游设计：《auto-video-editor：OpenStoryline 目录扁平化迁移执行设计》（`docs/修改文档/auto-video-editor-openstoryline-目录扁平化迁移执行设计.md`，下称"设计文档"）
> 创建日期：2026-11-01
> 执行环境：Windows / pwsh + Git Bash / 仓库根 `E:\Documents\kuaishou\auto-video-editor`
> 状态：**未开始**

---

## 1. 使用说明

- 本文是设计文档 §6「执行步骤」的任务化分解，按阶段组织；每个阶段给出前置条件、任务清单和阶段门（通过标准）。
- 命令细节以设计文档对应章节为准，本文只保留关键判定动作，避免两份文档漂移。
- 逐阶段推进；任何阶段门不通过，立即停止并按设计文档 §8「回滚方案」处理。
- 任务状态用复选框跟踪：`[ ]` 未开始 / `[x]` 完成；执行结果记入 §8「执行记录」。

## 2. 阶段地图与提交拆分

| 阶段 | 名称 | 对应设计文档 | 主要产出 | 状态 |
|---|---|---|---|---|
| 阶段 0 | 前置整备 | §6 阶段 0 | 干净工作区 + 迁移分支 | 未开始 |
| 阶段 1 | 停服与备份 | §6 阶段 1 | 仓库外全量备份目录 | 未开始 |
| 阶段 2 | 目标路径冲突检查 | §6 阶段 2 | 冲突清单 = 0 | 未开始 |
| 阶段 3 | 目录与文件搬迁 | §6 阶段 3 | 根目录扁平化文件就位 | 未开始 |
| 阶段 4 | 虚拟环境重建 | §6 阶段 4 | `.venv-openstoryline/` 可用 | 未开始 |
| 阶段 5 | 代码与配置改造 | §5 + §6 阶段 5 | 14 项路径适配完成 | 未开始 |
| 阶段 6 | 静态验证 | §6 阶段 6 | 文件 / 配置 / 忽略规则检查通过 | 未开始 |
| 阶段 7 | 测试验证 | §6 阶段 7 | 四套测试通过 | 未开始 |
| 阶段 8 | 启动与集成验收 | §6 阶段 8 | A01–A15 全过 | 未开始 |
| 阶段 9 | 旧目录清理与收尾 | §6 阶段 9 | 活动树中无 `openstoryline/` | 未开始 |

提交拆分（对应设计文档 §10）：

| 提交 | 内容 | 汇入阶段 |
|---|---|---|
| Commit 1 | 目录搬迁 + 路径适配（代码 / 配置 / 脚本 / `.gitignore` / 文档） | 阶段 3、5、6 |
| Commit 2 | 运行环境与输出隔离（依赖清单改名、安装脚本、`.gitignore` 补充） | 阶段 4 |
| Commit 3 | 测试与验收记录、旧目录清理 | 阶段 7–9 |

> 说明：`.venv-openstoryline/` 与 `outputs/` 内容本身不入库，Commit 2 主要落地脚本与依赖清单改名；若执行时提交边界不便切分，允许将 Commit 1/2 合并为一次提交，但**不得把旧目录删除混进前两个提交**。

## 3. 全局约束与红线

1. **双环境隔离**：主 `.venv/` 一律不动；OpenStoryline 使用新建的 `.venv-openstoryline/`，禁止 `Move-Item` 合并旧 `.venv`，禁止把 `requirements-openstoryline.txt` 装进主环境。
2. **输出隔离**：OpenStoryline 会话产物只进 `outputs/openstoryline/<session_id>/`，禁止写入根 `outputs/` 其他层级，禁止覆盖主工作流 `outputs/job-*`。
3. **数据红线**：`.storyline/models/`（约 118MB 模型权重）、`resource/`、`outputs/openstoryline/`、两个 venv 均**不得**进入任何自动清理清单；清理只针对 `.storyline/.server_cache/`（且走回收站）。
4. **密钥红线**：`config.toml.local` 不入库；入库的 `config.toml` 只保留占位密钥。
5. **旧目录保留**：`openstoryline/` 只能在阶段 8 全部通过后于阶段 9 移出；采取"先复制、后验证、最后清理"策略，任何时刻不删除唯一副本。
6. **脚本解析**：ps1 的解析与校验一律使用 pwsh，不使用 Windows PowerShell 5.1。
7. **Postgres 依赖**：主项目 restore 系测试依赖 Postgres 5432 存活；`stop_production.ps1` 会停掉 `edb-postgres16` 容器，阶段 7 前确认其已恢复。
8. **代理**：git fetch / push 需走 `git -c http.proxy=socks5://127.0.0.1:3067 ...`。
9. **包名保护**：`open_storyline` 是 Python 包名，不属于旧目录引用，全文检索时不得误改。

## 4. 基线快照（2026-11-01，迁移前实测）

以下为本机实测状态（设计文档撰写后补充核实），执行时如与实况不符，以实况为准并更新本节：

- **未提交改动**（清理缓存回收站改造，已完成开发但未 commit）：
  `config.py`、`nodes/node_01_clean_cache.py`、`openstoryline/agent_fastapi.py`、`tests/integration/test_e2e_tc01_to_tc06.py`、`tests/integration/test_no_autoclean_cache.py`、`tests/unit/test_node_01_clean_cache.py`
  → 其中 `config.py` 与 `openstoryline/agent_fastapi.py` 正是迁移要搬 / 要改的文件，**必须先单独提交**，避免与迁移改动纠缠。
- **未跟踪文档**：设计文档与本文（随 Commit 1 一并入库）。
- **`openstoryline/` 顶层实有内容**：`.gitignore`、`.storyline`、`.venv`、`LICENSE`、`README.md`、`agent_fastapi.py`、`config.toml`、`config.toml.local`、`outputs`、`prompts`、`resource`、`scripts`、`src`、`tests`、`web`。
- **`.storyline/` 当前仅含 `models/`**：`resource.zip` 安装包残留已被 `download_resources.ps1` 解压后自删，不存在的搬运负担；`.server_cache/` 运行时生成，当前不存在。
- **根目录目标位占用**：仅 `outputs/` 与 `scripts/` 已存在；`src/`、`prompts/`、`web/`、`resource/`、`.storyline/`、`agent_fastapi.py`、`config.toml` 在根目录**均不存在**，属于整体上移而非合并。
- **唯一真合并点** = `scripts/`（根目录现有 17 个文件，与 OpenStoryline 3 个脚本 `install.ps1` / `download_resources.ps1` / `download_models.ps1` 暂无同名冲突，执行时仍须跑扫描确认）；**唯一隔离点** = `outputs/`。
- **根目录已有 `openstoryline-config/`**（`.gitignore` 第 48 行独立规则），与本次迁移无关，保留不动。
- `pyproject.toml` 的 `testpaths = ["tests"]`：迁移后 `tests_openstoryline/` 不会被主套件默认收集，保持独立入口。
- 主测试套件基线：仓库根 0 失败；OpenStoryline 3 个 config 泄漏失败仅在其 tests 定向作用域复现（已知怪癖，迁移后位置变为 `tests_openstoryline/`）。
- Karing 系统代理可能影响 pwsh 对 localhost 的探活，阶段 8 健康检查如遇假阴性先排查代理。

## 5. 分阶段执行开发任务

### 阶段 0：前置整备

前置条件：无。

- [ ] 0.1 将"清理缓存回收站改造"的 6 个文件**单独提交**（feat(fix) 独立提交，不与迁移混合）
- [ ] 0.2 将设计文档与本文档入库（docs 提交，可并入 0.1 或单独提交）
- [ ] 0.3 创建迁移分支：`git switch -c refactor/flatten-openstoryline`（若已存在则先确认其状态，勿重复创建）
- [ ] 0.4 `git status --short` 确认工作区干净

**阶段门**：工作区干净、迁移分支就绪、`openstoryline/agent_fastapi.py` 无未提交差异。

### 阶段 1：停服与备份

前置条件：阶段 0 门通过。

- [ ] 1.1 `.\stop_production.ps1` 停生产服务（会一并停 `edb-postgres16` 容器；阶段 7 前恢复）
- [ ] 1.2 `Get-NetTCPConnection -LocalPort 7860 -State Listen` 确认无监听；仍有监听则按 PID / 命令行确认归属，不盲杀未知进程
- [ ] 1.3 robocopy 全量备份 `openstoryline/`（含 `.venv`、`.storyline`、`outputs`、`resource`、`config.toml.local`）到仓库外备份目录，命名带时间戳
- [ ] 1.4 若根目录已存在 `.storyline/`、`resource/`、`src/`、`prompts/`、`web/` 等目标位，一并备份（当前实测根目录仅 `outputs/`、`scripts/` 被占用）

**阶段门**：robocopy 退出码 < 8；备份目录包含全部运行时内容（`.venv`、`.storyline\models`、`config.toml.local` 抽查在位）。

### 阶段 2：目标路径冲突检查

前置条件：阶段 1 门通过。

- [ ] 2.1 执行设计文档 §6 阶段 2 的 MergePairs 递归同名扫描（8 对目录，含本机未跟踪文件）
- [ ] 2.2 文件级检查 `agent_fastapi.py` / `config.toml` / `config.toml.local`（当前实测根目录均无同名，仍须复核）
- [ ] 2.3 如有冲突项，逐个人工比较裁决，禁止直接覆盖

**阶段门**：`$Collisions.Count -eq 0`。

### 阶段 3：目录与文件搬迁

前置条件：阶段 2 门通过。

- [ ] 3.1 新建 `tests_openstoryline/`、`outputs/openstoryline/`
- [ ] 3.2 robocopy 整体上移 `src/`、`prompts/`、`web/`、`resource/`、`.storyline/` → 根目录（当前实测均为干净上移，无合并）
- [ ] 3.3 `openstoryline/outputs/` → `outputs/openstoryline/`（隔离，不触碰根 `outputs/` 其他内容）
- [ ] 3.4 `openstoryline/scripts/` 3 个脚本合并进根 `scripts/`
- [ ] 3.5 根文件搬迁：`agent_fastapi.py`、`config.toml`、`config.toml.local` 直迁；`README.md → README-OpenStoryline.md`、`LICENSE → LICENSE-OpenStoryline`、`requirements.txt → requirements-openstoryline.txt`
- [ ] 3.6 `openstoryline/tests/` → `tests_openstoryline/`
- [ ] 3.7 `openstoryline/.gitignore` 不搬迁，其规则并入根 `.gitignore`（阶段 5 处理）

**阶段门**：设计文档 §6 阶段 6 的 RequiredPaths 核心项（`agent_fastapi.py`、`src\open_storyline\__init__.py`、`prompts\tasks`、`web\index.html`、`tests_openstoryline\conftest.py`、`.storyline\models`、`resource`）抽查在位；源目录 `openstoryline/` 暂不删除。

### 阶段 4：虚拟环境重建

前置条件：阶段 3 门通过。

- [ ] 4.1 读旧环境 `openstoryline/.venv/pyvenv.cfg` + `python --version`，确认 Python 主 / 次版本
- [ ] 4.2 以相同版本创建根目录环境：`py -3.x -m venv .venv-openstoryline`
- [ ] 4.3 `.\.venv-openstoryline\Scripts\python.exe -m pip install -r .\requirements-openstoryline.txt`
- [ ] 4.4 `pip freeze` 比对新旧环境关键包（uvicorn / fastapi / torch / torchaudio 等）版本一致

**阶段门**：`import uvicorn, fastapi` 通过；关键包版本与旧环境一致。禁止任何写入根 `.venv` 的操作。

### 阶段 5：代码与配置改造（14 项）

前置条件：阶段 4 门通过。

| 编号 | 文件 | 改动要点 | 设计文档 |
|---|---|---|---|
| 5.1 | `config.py` | `VENDORED_OPENSTORYLINE_ROOT = <repo>`；venv 指向 `.venv-openstoryline`；`CACHE_PATHS_TO_CLEAN` 指向根 `.storyline/.server_cache`，不含 models / resource / outputs / venv；缓存根由 `Path(__file__).resolve().parent` 推导 | §5.1 |
| 5.2 | `nodes/node_02_launch_openstoryline.py` | `OPENSTORYLINE_DIR = REPO_ROOT`、`OPENSTORYLINE_AGENT`、`OPENSTORYLINE_VENV_PY` 同步改根目录；节点仍只做健康检查 | §5.2 |
| 5.3 | `nodes/node_04b_get_storyboard_plan.py` | `OPENSTORYLINE_OUTPUTS_ROOT = <repo>/outputs/openstoryline`，读取逻辑不变 | §5.3 |
| 5.4 | `agent_fastapi.py`（根目录版） | 删除 `REPO_ROOT_DIR = dirname(ROOT_DIR)` 假设；`ROOT_DIR` 即仓库根；`sys.path` 初始化保证主项目 `config.py`/`db`/`nodes` 与 `open_storyline` 均可导入；`SERVER_CACHE_DIR` 指向根 `.storyline/.server_cache`；`/api/system/clean-cache` 与 `/api/system/import-video` 两处 `AUTO_VIDEO_EDITOR_DIR` 默认值改为 `ROOT_DIR`（不再硬编码盘符路径）；注释中输出路径改 `outputs/openstoryline/<sid>/...` | §5.4 |
| 5.5 | 根 `config.toml` | `media_dir = ./outputs/openstoryline/media`、`outputs_dir = ./outputs/openstoryline`、`bgm_dir = ./resource/bgms`；只保留占位密钥 | §5.5 |
| 5.6 | `src/open_storyline/utils/prompts.py` | `PROMPTS_DIR` 改为模块位置推导的绝对路径（`parents[3]` / `prompts/tasks`），摆脱对进程 CWD 的依赖 | §5.6 |
| 5.7 | `start_production.ps1` | `$OsDir = $RepoRoot`；`$OsVenvPy` / `$OsAgent` / `$OsSrc` / `$OsResource` / `$OsModels` 按根目录重算；Web 启动加 `-WorkingDirectory $RepoRoot`；脚本提示改根目录路径 | §5.7 |
| 5.8 | `scripts/install.ps1` | `$RepoRoot` 由 `$PSScriptRoot` 推导；创建 `.venv-openstoryline`；引用 `requirements-openstoryline.txt`；不触碰主 `.venv` | §5.8 |
| 5.9 | `scripts/download_resources.ps1`、`scripts/download_models.ps1` | `$PSScriptRoot` 推导仓库根并切换 CWD；资源解压到 `resource/`、模型解压到 `.storyline/models/`，不再落到旧路径 | §5.8 |
| 5.10 | `scripts/check_openstoryline_llm.py` | `OPENSTORYLINE_DIR` 默认值改为 `REPO_ROOT`，保留环境变量覆盖；说明改用 `.venv-openstoryline` | §5.9 |
| 5.11 | `tests_openstoryline/conftest.py` | `TESTS_DIR / REPO_ROOT / SRC_DIR` 按新层级重算；保留 `OPENSTORYLINE_DIR = REPO_ROOT` 兼容别名；测试仍用 `.venv-openstoryline` 跑 | §5.10 |
| 5.12 | 根 `.gitignore` | 删除 `openstoryline/.venv/` 等 5 条旧规则（第 51–55 行）；新增 `.venv-openstoryline/`、`.storyline/`、`resource/`、`config.toml.local`；`openstoryline-config/` 规则保留 | §5.11 |
| 5.13 | 根 `README` | OpenStoryline 安装、资源下载、目录路径说明改为扁平化后路径 | §6 阶段 5 |
| 5.14 | 全文检索 | `git grep -n -I -E 'openstoryline[/\\]|openstoryline\.venv|openstoryline\\outputs|OPENSTORYLINE_DIR' -- '*.py' '*.ps1' '*.toml' '*.md'`，逐项分类：运行时必改 / 历史文档保留（标记历史状态）/ `open_storyline` 包名不改 | §6 阶段 5 |

**阶段门**：5.14 检索结果中，运行时代码与启动脚本无旧路径残留；`tests/integration`、`agent_gate` 等主项目代码无本迁移引发的路径引用。

### 阶段 6：静态验证

前置条件：阶段 5 门通过。

- [ ] 6.1 设计文档 §6 阶段 6 的 RequiredPaths 15 项存在性检查，无 Warning
- [ ] 6.2 `config.toml` 路径值核对：`outputs_dir` / `media_dir` / `bgm_dir` / 模型路径（`.storyline/models/`）/ 缓存路径（`.storyline/.server_cache/`）
- [ ] 6.3 `git check-ignore config.toml.local` 生效；`git status` 不出现该文件；入库 `config.toml` 无真实密钥
- [ ] 6.4 用 pwsh 对 4 个 ps1（`start_production` / `install` / `download_resources` / `download_models`）做解析校验（不执行）

**阶段门**：全部通过，无缺失项、无路径解析到旧位置。

### 阶段 7：测试验证

前置条件：阶段 6 门通过；`edb-postgres16` 运行中、5432 可连。

- [ ] 7.1 环境准备：`$env:PYTHONUTF8='1'`、`$env:PYTHONIOENCODING='utf-8'`；确认 Postgres 存活（阶段 1 若停过则 `docker start edb-postgres16`）
- [ ] 7.2 OpenStoryline 测试：`.\.venv-openstoryline\Scripts\python.exe -m pytest .\tests_openstoryline -q --junitxml="$env:TEMP\openstoryline.xml"`
- [ ] 7.3 主项目测试：`.\.venv\Scripts\python.exe -m pytest .\tests -q --junitxml="$env:TEMP\main.xml"`（基线 0 失败）
- [ ] 7.4 agent_gate 测试：`.\.venv\Scripts\python.exe -m pytest .\agent_gate\tests -q -p no:cacheprovider --junitxml="$env:TEMP\agent-gate.xml"`
- [ ] 7.5 前端测试：`node --test .\web\tests\*.test.mjs`

**阶段门**：四套测试通过；OpenStoryline 已知的 3 个 config 泄漏怪癖若在 `tests_openstoryline` 定向作用域复现，按既有怪癖记录，不判为迁移回归；主套件出现任何新失败即触发回滚评估。

### 阶段 8：启动与集成验收

前置条件：阶段 7 门通过。

- [ ] 8.1 手工启动：仓库根目录 `.\.venv-openstoryline\Scripts\python.exe -m uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`，验收：首页 200、静态资源加载、提示词不报 `Template not found`、`config.toml`+`config.toml.local` 正常合并、上传视频落 `outputs/openstoryline/<sid>/media/`、计划落 `outputs/openstoryline/<sid>/plan_timeline_pro/`、`.storyline/.server_cache/` 可写
- [ ] 8.2 生产 human 模式：`.\start_production.ps1 -StorylineMode human`，验收：脚本从根目录找到 `.venv-openstoryline`、PID 文件 `os_dir` = 仓库根、`node_02` 健康检查通过、`node_04b` 从 `outputs/openstoryline/` 读到计划
- [ ] 8.3 生产 auto 模式：`.\start_production.ps1 -StorylineMode auto`，验收：原有行为不回归，模型 / ASR 资源经根目录路径可访问
- [ ] 8.4 清缓存安全：触发 Web 清理缓存或对应单测，验收：只清 `.storyline/.server_cache`（走回收站），`.storyline/models`、`resource/`、`outputs/openstoryline/`、两个 venv 全部原样

**阶段门**：设计文档 §7 验收标准 **A01–A15 全部通过**（对照见本文 §7）；生产 Web 需重启加载新代码，健康检查假阴性先排查 Karing 代理。

### 阶段 9：旧目录清理与收尾

前置条件：阶段 8 门通过（A01–A15 全过）。

- [ ] 9.1 `Move-Item` 将 `openstoryline/` 整体移入备份区（`openstoryline-before-cleanup`），不直接删除
- [ ] 9.2 确认活动树中已无 `openstoryline/` 目录
- [ ] 9.3 Commit 3 落地：测试与验收记录、目录清理；推送 GitHub（走 socks5 代理）
- [ ] 9.4 更新设计文档与本文状态为"已完成"；备份目录进入观察期，稳定运行一段时间后手动清理

**阶段门**：`git status` 干净、远端已推送、活动树无 `openstoryline/`。

## 6. 回滚触发与流程

阶段 4–8 中出现任一情况立即停止迁移、不执行阶段 9：

- 新 venv 依赖安装失败；OpenStoryline Web 无法启动；`open_storyline` 或主项目模块导入失败；提示词 / 资源路径失效；`node_04b` 读不到计划；主项目测试出现迁移相关回归。

回滚步骤（设计文档 §8）：停新 Web → Git 回退本次提交 → 从备份目录恢复旧 `openstoryline/` → 恢复迁移前的根目录目标文件（如迁移前已存在）→ 用旧 `openstoryline/.venv` 启动服务 → 核对 `start_production.ps1` / `node_02` / `node_04b` 路径引用一致。

> 注意：`git restore` 只能恢复跟踪文件，`.venv`、`.storyline`、`resource`、`outputs`、`config.toml.local` 只能靠阶段 1 的备份恢复——这是备份门不可跳过的原因。

## 7. 验收标准与验证阶段对照

| 编号 | 验收项 | 验证位置 |
|---|---|---|
| A01 | 活动目录无 `openstoryline/` | 阶段 9 |
| A02 | 主 `.venv` 原样、主测试通过 | 阶段 7.3 |
| A03 | `.venv-openstoryline` 独立启动 Web 与测试 | 阶段 4 / 7.2 / 8.1 |
| A04 | `open_storyline`、`db`、`nodes` 可导入 | 阶段 5.4 / 7 |
| A05 | `prompts/tasks` 正常加载 | 阶段 8.1 |
| A06 | `http://127.0.0.1:7860/` 200、静态资源正常 | 阶段 8.1 |
| A07 | `.server_cache` 可写、模型权重在位 | 阶段 8.1 / 8.4 |
| A08 | `resource/` 资源完整 | 阶段 3 / 8 |
| A09 | 输出只进 `outputs/openstoryline/` | 阶段 8.1 |
| A10 | `node_04b` 读到新目录计划 | 阶段 8.2 |
| A11 | human 模式启动成功 | 阶段 8.2 |
| A12 | auto 模式不回归 | 阶段 8.3 |
| A13 | 两套测试可分别运行 | 阶段 7.2 / 7.3 |
| A14 | `config.toml.local` 被忽略、无密钥入库 | 阶段 6.3 |
| A15 | 清理不删模型 / 资源 / 输出 / venv | 阶段 8.4 |

## 8. 执行记录

| 日期 | 阶段 | 结果 | 提交 / 分支 | 备注 |
|---|---|---|---|---|
| | | | | |

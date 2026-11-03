# 阶段 0 基线与保护记录（冗余模块合并 2026-11-03）

> 依据：`docs/修改文档/auto-video-editor_main_冗余模块合并设计与执行计划.md` §6.1
> 基准提交：`a27298a11deb5b597474ac6b317edf7db6a12c0f`（本地 `main` 与其一致）
> 执行分支：`refactor/dedupe-openstoryline-mcp-20261103`（基点为 docs 入库提交 `f2e9465`）
> 执行时间：2026-11-03 07:20–07:55（本地）
> 环境：Windows 10.0.26300 / Git Bash；主项目 `.venv`（Python 3.13.11、pytest 9.1.1）；node v24.19.0；`.venv-openstoryline` 在位
> 远端说明：GitHub 不可达（离线环境），`git fetch` 失败；以本地 `main` HEAD 为基准，未做任何 reset

---

## 0.1 分支与文档入库

- 未跟踪的 5 个 `docs/修改文档/` 文件（4 份设计/审查文档 + 交付脚本）已作为独立 docs 提交 `f2e9465` 入库。
- 重构分支自 `a27298a`（= 本地 `main`）创建。

## 0.2–0.3 交付脚本与 dry-run 预检

- 脚本已复制：`scripts/refactor_dedupe_openstoryline_mcp.py`（与 docs 交付副本逐字一致）。
- dry-run 预检 **通过**：6 组 MCP 新旧路径逐字节一致；两套提示词目录各 48 份 `.md`、路径集合与字节一致；目标测试文件不存在。输出要点：`将执行以下变更：6 个旧 MCP 源码路径替换为兼容模块别名…`、`DRY RUN：预检通过`。

## 0.4 基线测试记录

pytest 计数来自 junit XML（`baseline-pytest-*.xml`），通过/失败/跳过分列：

| 套件 | 总数 | 通过 | 失败 | 错误 | 跳过 | 退出码 | 明细文件 |
|---|---:|---:|---:|---:|---:|---|---|
| `tests/unit` | 1231 | 1189 | 18 | 0 | 24 | 1 | `baseline-pytest-unit.xml` / `-run2.txt`（失败 ID 清单见 `baseline-pytest-unit.txt`） |
| `tests/integration` | 230 | 199 | 0 | 23 | 8 | 1 | `baseline-pytest-integration.xml` / `-run2.txt` |
| `tests_openstoryline` | 319 | 244 | 66 | 0 | 9 | 1 | `baseline-pytest-openstoryline.xml` / `-run2.txt` |
| `node --test`（前端 monitor + chat indicator） | 37 | 37 | 0 | — | 0 | 0 | `baseline-node-frontend.txt` |

存量失败说明（均非本计划引入，作为对照基线）：

- `tests/unit` 18 个失败集中在 `test_session_store_repository.py` 等；与 `a27298a` 提交说明记录的"21 个存量失败"数量存在环境差异，以**本机本次记录**为对照基线。
- `tests/integration` 23 个 ERROR 全部为 Postgres 不可达类（本机无运行中的 PostgreSQL），文件 `test_sessions_api_postgres.py` 等。
- `tests_openstoryline` 66 个失败为环境性存量失败（mcode CLI / 实时服务 / 本地资源数据依赖），重构未应用前即如此。

## 0.5 Schema 快照（`build_graph_schema()`，四种配置组合）

快照脚本：`scripts/capture_graph_schema_snapshots.py`（子进程按组合设 env，`InMemorySaver` 建图）。
输出：`schema-snapshots/{default_baseline,assembly_qc_off,human_fusion_on,ai_transition_on}.json`。

| 组合 | 注册节点 | 可达节点 | 边数 | 文件 SHA-256（前 12 位） |
|---|---:|---:|---:|---|
| 默认基线（QC=on, fusion=off, ai=off） | 55 | 52 | 69 | `c733b53b5d05` |
| QC 关闭 | 55 | 46 | 62 | `64c9049037ea` |
| Human Fusion 开启 | 55 | 46→54* | 61 | `72c2f312bf5d` |
| AI 转场开启 | 55 | 54 | 75 | `8f7843083a08` |

*注册节点数四组合恒为 55：QC/fusion 关闭时节点仍注册但**不接边**（可达数与边数体现差异），与 `graph.py` 注释一致。四份快照两两不同，可作重构后逐字节对比。

## 0.6 重复文件审计

脚本：`scripts/audit_duplicate_files.py`（只报告，不删除）。输出：`duplicate-files-audit-20261103_073245.{json,md}`。

- 扫描范围：仓库全部普通文件；排除所有点目录（`.git/.venv*/.video_agent/.storyline/.worktrees/.zcode` 等）及 `__pycache__/node_modules/outputs/out/logs/inputs/drafts/vendor`；单文件 >64MB 跳过。
- 结果：**57 组重复**（`production_source=54`：48 组提示词 + 6 组 MCP；`mixed=3`）。
  - mixed 组 1：`agent_gate/fixtures/storyline_capabilities/vlm_client.py` ↔ 生产 `storyline_capabilities/vlm_client.py`（fixture，保留）。
  - mixed 组 2：`docs/修改文档/refactor_dedupe_openstoryline_mcp.py` ↔ `scripts/` 同名（交付脚本双副本，保留 docs 归档）。
  - mixed 组 3：10 个空文件（`__init__.py` 等，保留）。
- 与审查报告"59 组"的口径差异：本审计排除点目录与运行期数据目录（报告基于 GitHub 树含全部 926 条目），差异不构成矛盾。

## 附：改造前引用扫描与 golden

- 旧 MCP 路径引用（`open_storyline.(hooks|register_tools|sampling_handler|sampling_requester|server)`）：生产/测试代码 **0 处**，仅交付脚本自身字符串（含将生成的测试代码）。见会话记录。
- 规范路径 `open_storyline.mcp.*` 引用 10 处：`mcp-canonical-import-scan-before.txt`。
- 提示词 golden 基线：`tests/unit/golden/prompt_render_golden.json`（48 份模板 × 双副本 SHA + 双 Loader 加载/渲染 SHA + 变量映射；采集脚本 `scripts/capture_prompt_golden.py`）。采集时两份物理副本逐字一致（校验内置于脚本）。
- 双 Loader 语义回归测试 `tests/unit/test_dual_prompt_loader_semantics.py` 已在**重构前**预跑：17/17 通过。

## 附：openstoryline 独立环境冒烟（1.4 前置观察）

- `PYTHONPATH=src .venv-openstoryline/Scripts/python.exe -c "import open_storyline.mcp.server"`：**通过**（节点注册表扫描完成）。
- `create_server()` 冒烟在 `resource/script_templates/meta.json` 缺失处失败 —— 本机缺少该运行期数据文件（目录为空），属**既有环境缺口**，与重构无关；`install.ps1`/运行期生成流程应负责创建。1.4 的入口验证以此为上限：导入级 + 模块身份 + shim AST 校验，不做真实端口绑定。

## 交付物清单

- 分支 `refactor/dedupe-openstoryline-mcp-20261103`
- `scripts/refactor_dedupe_openstoryline_mcp.py`（交付脚本）、`scripts/audit_duplicate_files.py`、`scripts/capture_graph_schema_snapshots.py`、`scripts/capture_prompt_golden.py`
- `docs/refactor-baseline-20261103/`：本文件 + 4 份 Schema JSON + 审计 JSON/MD + 4 份 junit XML 与测试输出 + 引用扫描记录
- `tests/unit/golden/prompt_render_golden.json` + `tests/unit/test_dual_prompt_loader_semantics.py`（重构前预跑通过）

---

## 重构后复核（阶段 1 / 2A / 2B 完成后，2026-11-03 08:08）

各阶段提交：阶段 1 `refactor(mcp)`、阶段 2A `refactor(prompts)` 资源统一、阶段 2B `refactor(prompts)` 共享 Loader（见 `git log refactor/dedupe-openstoryline-mcp-20261103`）。

### 全量回归对照（junit XML：`post-pytest-*.xml` vs `baseline-pytest-*.xml`）

| 套件 | 基线 通过/总数(败/错/跳) | 重构后 通过/总数(败/错/跳) | 新增失败 |
|---|---|---|---|
| `tests/unit` | 1189/1231 (18/0/24) | 1228/1270 (18/0/24) | **0**（+39 为本次新增的语义/策略/golden 测试，全过） |
| `tests/integration` | 199/230 (0/23/8) | 199/230 (0/23/8) | **0**（23 个 Postgres 存量 ERROR 不变） |
| `tests_openstoryline` | 244/319 (66/0/9) | 250/325 (66/0/9) | **0**（+6 为 MCP 兼容身份测试，全过；66 个存量失败集合逐项一致） |
| `node --test` 前端 | 37/37 | 37/37 | 0 |

### 静态验收

- Schema 快照：四种配置组合重构后重采（`schema-snapshots-post/`），schema 与基线**逐字节一致**。
- `graph.py` / `db/` / `pyproject.toml` 零改动；节点统计 53/44/10 不变。
- 删除清单仅含 `storyline_capabilities/prompts/` 的 48 份 `.md` 副本；docs 快照、fixture、`__init__.py` 无一删除。
- 重复文件审计：57 组 → 3 组（-48 提示词、-6 MCP），`duplicate-files-audit-20261103_075452.json`。
- 旧路径引用扫描：生产代码 0 处引用旧 MCP 路径（仅兼容测试字符串）。
- openstoryline 独立环境（`.venv-openstoryline`，cwd=仓库根）：`open_storyline.mcp.server` 导入 + 节点注册表扫描通过；`open_storyline.server`/`open_storyline.hooks.*` 旧路径与规范模块对象身份一致；重构后 Facade 经 `jy_common.prompt_core` 加载模板正常（`instruction.system` 2174 字符）。`create_server()` 完整冒烟仍受既有环境缺口限制（`resource/script_templates/meta.json` 缺失，与本次重构无关）。

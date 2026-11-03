# 阶段0 · 基线冻结与依赖盘点(remove-chat-loop-20261104)

> 日期:2026-11-04
> 执行计划:《auto-video-editor_单流水线架构设计与分阶段执行计划_评审修订版_20261104.md》阶段 0
> 基准 SHA:`9ab3df57614fc2acda61134e0ae5af6e1bf62a93`(已确认 `main` == `HEAD` == 该 SHA)
> 分支:`refactor/remove-chat-loop-20261104`(自该 SHA 切出)
> 基线测试所在提交:`ab7982b`(基准 SHA + 计划文档入库 + .gitignore 补规则,**代码与基准 SHA 零差异**)
> 环境:Windows / Git Bash / Python 3.13.11(主 `.venv`)/ node v24.19.0

---

## 1. Git 基线

- 工作区:除两份计划文档(已入库)外 clean;`git status --short` 仅 `?? docs/修改文档/*.md` ×2。
- `git rev-parse HEAD` == `git rev-parse main` == `9ab3df57614fc2acda61134e0ae5af6e1bf62a93`,与计划基准一致,无需更新基准 SHA。
- 回滚锚点:阶段回滚 revert 对应阶段 commit;全量回滚回到 `9ab3df5`。

## 2. 测试基线(JUnit XML 于本目录)

执行命令(全部在主 `.venv` / 仓库根):

| 套件 | JUnit | 结果 |
|---|---|---|
| `pytest tests/unit -q` | `baseline-unit.xml` | **18 failed**(全部 `test_session_store_repository.py`),其余通过 |
| `pytest tests/integration -q` | `baseline-integration.xml` | **23 error**(全部 `test_sessions_api_postgres.py`)+ **3 failed**(明细见 §2.1) |
| `pytest agent_gate/tests -q` | `baseline-agent-gate.xml` | **1 failed**:`test_scaffold.py::test_init_writes_expected_files` |
| `pytest tests_openstoryline -q` | `baseline-openstoryline.xml` | **66 failed**(与 `9ab3df5` 提交记录完全一致) |
| `node --test web/tests/*.test.mjs` | (文本日志 `baseline-run.log`) | **77/77 全绿** |

> 注:`node --test web/tests`(目录参数)在本机 node v24.19.0 报 `MODULE_NOT_FOUND`(目录参数解析怪癖,非代码问题);等效 glob 形式 77/77 全绿。后续回归一律用 glob 形式。

### 2.1 存量失败明细(阶段 2 差分对照表)

**tests/unit FAILED ×18**(同一文件,均为聊天会话存储旧链路,阶段 4 计划删除对象):
`tests/unit/test_session_store_repository.py` 全部 18 条(save_state_atomic ×4、load_from_state ×4、store ×6、rate_limit ×3、`test_store_get_or_404_maps_db_error_to_503` 等)。

**tests/integration ERROR ×23**:`tests/integration/test_sessions_api_postgres.py` 全部 23 条,异常统一为 `ModuleNotFoundError: No module named 'openstoryline'` —— 2026-11-01 扁平化遗留死路径(无下划线旧包名),与 `9ab3df5` 记录的"23 条错误"数量一致(当时记录归因 PG 不可达,实际主因为死路径)。

**tests/integration FAILED ×3**(环境/配置性):
| 测试 ID | 现象 |
|---|---|
| `test_assembly_qc_graph.py::test_assembly_chain_end_to_end_produces_all_artifacts` | 缺失产物文件(media/transcript/video_ingest/timeline/preview.mp4 等),ffmpeg/inputs 环境性 |
| `test_human_fusion_graph.py::test_artifacts_are_isolated_and_published_under_new_keys` | `job_id 'fusion-on' != 'fusion-off'`,融合 flag 本机配置差异 |
| `test_reorder_topology.py::test_tail_chain_applies_to_both_drafts` | 产物存在性断言失败,同上环境性 |

**agent_gate FAILED ×1**:`test_scaffold.py::test_init_writes_expected_files` —— 仓库路径含中文「开发代码」,`json.dumps` 产出 `\u5f00...` 转义含反斜杠,断言 `"\\" not in json.dumps(...)` 必然失败;traceback 显示旧盘符路径 `E:\Documents\kuaishou\auto-video-editor` 为断言重写缓存随目录搬迁遗留,与本仓库当前代码无关。纯环境性。

**tests_openstoryline FAILED ×66**:`test_mcode_streaming` 19、`test_mcode_runner` 13、`test_mcode_ws_events` 9、`test_mcode_chat_model` 9、`test_mcode_process_env` 8、`test_mcode_ws_e2e` 4、`test_mcode_source_isolation` 4(合计 66,与提交记录一致)。

> 验收原则(按计划):阶段 2 差分按**测试 ID + 异常类型**逐条对照本表;新增失败一律先修复;上表存量不计。

## 3. 生产代码 import 清单(删除审计)

全仓 grep(排除 `.venv*`/`node_modules`/`__pycache__`/`docs`/`.git`/`.worktrees`),结论:

1. **保留侧生产代码对 `open_storyline` 的 Python import 为零**(agent_gate/、graph.py、nodes/、monitoring/、db/、scripts/、storyline_capabilities/、assembly_capabilities/、web/api_workflow.py、根 config.py/state.py)。
2. 删除侧 `open_storyline` import 全部位于:`agent_fastapi.py`(15 条)与 `src/open_storyline/` 包内部互引、`tests_openstoryline/`。三个测试文件有残留(`tests/integration/test_sessions_api_postgres.py:181`、`tests/unit/test_dual_prompt_loader_semantics.py:44`、`tests/unit/test_prompt_core_strategies.py:147`),阶段 2 处理。
3. **非 import 的运行时引用仅两处**:`storyline_capabilities/asr_runner.py:144-146` 子进程 `-m open_storyline.runway`(目标模块在全仓不存在,今天已是死链,有 stub 降级);`config.py:276` `OPENSTORYLINE_CMD` 字符串(零消费者)。
4. **勘误**:原附件所列"根级转发壳 server.py/register_tools.py/sampling_handler.py/sampling_requester.py"实际位于 `src/open_storyline/` 包根(另有 hooks/ 两壳),不在仓库根;随整包删除,唯一牵连是 `tests_openstoryline/test_mcp_legacy_compat.py`(随目录删)。仓库根仅有 `agent_fastapi.py`。
5. `src/` 下只有 `open_storyline` 一个包;`jy_common/` 在仓库根且不 import open_storyline(依赖方向相反),**不随删**。

### scripts/ 判定(逐个)

| 判定 | 脚本 | 依据 |
|---|---|---|
| **删** | `check_openstoryline_llm.py` | import open_storyline.agent/config,旧运行时体检,零引用 |
| **删** | `drill_disconnect.py` | 旧 ChatSession/SessionStore 断连演练;`openstoryline.agent_fastapi` import 已死 |
| **删** | `refactor_dedupe_openstoryline_mcp.py` | 一次性重构脚本(转发壳作者),壳删除后无意义 |
| **删** | `import_session_states.py` | 一次性迁移工具,主依赖根 `agent_fastapi.ChatSession`(将删) |
| **改** | `capture_prompt_golden.py` | 按计划改单 Loader 后保留(当前 import `open_storyline.utils.prompts`) |
| 留 | run_workflow / migrate_db / align_postgres / verify_* / setup_postgres_schema / build_resource_library / audit_duplicate_files / capture_graph_schema_snapshots / _smoke_auto_storyline / video_edit_phase56_e2e / assembly/* | 全保留侧 |

### config.py 常量三档

| 档 | 常量 | 处置 |
|---|---|---|
| 现已死,阶段 2 删 | `OPENSTORYLINE_CMD`、`openstoryline_mcp_url()` | 零消费者 |
| node_02 消费,**本次保留**(阶段 3 删节点时才删) | `OPENSTORYLINE_MCP_PORT`、`OPENSTORYLINE_WEB_PORT`、`OPENSTORYLINE_HEALTH_TIMEOUT_S`、`openstoryline_web_url()` | `nodes/node_02_launch_openstoryline.py:36-39,100-127` |
| 保留侧消费者,永不可删 | `IMPORT_VIDEO_*`(node_04a)、`VENDORED_OPENSTORYLINE_*`(asr_runner)、`STORYLINE_MODE`、`STORYLINE_HUMAN_FUSION`、`STORYLINE_FUSION_DISABLE` | graph/monitoring/tests 在用 |

## 4. tests_openstoryline 分类(28 测试文件 + conftest + fixtures)

| 类别 | 文件 | 处置 |
|---|---|---|
| 迁移→tests/ | `test_workflow_monitor_frontend.py` | 阶段 1 迁(路径改仓库根推导) |
| 迁移(拆分)→tests/ | `test_mcode_status_constants.py` 前 3 测(`agent_gate.protocol` 直测) | 阶段 2 迁,新文件 |
| 随聊天 UI 死 | `test_mcode_frontend_policy.py`、`test_media_upload_frontend.py`、`test_workflow_chat_indicator_frontend.py` | 被测 .mjs 仅聊天页(app.js)消费 |
| 随目录删 | 其余 22 个旧运行时专属 + `conftest.py` + `fixtures/mcode_stream/` | 迁出文件不依赖 conftest fixture(已核实) |
| live 默认 skip | `test_glm_live.py`、`test_zcode_live.py` | 环境门控,不产失败 |

- **workflow_chat_indicator.mjs 评估结论**(阶段 1 任务 3):核心依赖 chat_session_id 关联查询(`GET /chat-sessions/{id}/run`),唯一消费方为聊天页 app.js;无独立于 node-map/monitor.html 的监控价值 → **判定删除,随聊天 UI 在阶段 2 一起删**(阶段 1 先不动,避免中间态聊天页 JS 404 破坏回滚检查)。
- 与 `tests/` 现有 137 个测试文件**零文件名冲突**。
- `tests_openstoryline/` 无 DB 依赖测试;覆盖保留模块(storyline_capabilities/prompt_core/ASR/monitoring/db/graph)的测试**已全部在 `tests/`**,无需额外迁移。

## 5. config.toml.local 备份

- 已复制为 `config.toml.local.bak-20261104`,`cmp` 校验逐字节一致;**内容未输出**(含 API key)。
- `.gitignore` 追加 `/config.toml.local.bak-*`(`*.bak` 规则覆盖不到带日期后缀的文件名),`git check-ignore` 验证通过。

## 6. PostgreSQL 盘点(只读,未做任何写操作)

- 实例:`config.POSTGRES_URI` → `localhost:5432/video_workflow`,**可连**。
| 项 | 值 |
|---|---|
| workflow_runs | 1 条:`video-wiring-001`(running,updated 2026-10-29)—— 6 天无新事件,历史联调遗留,**非活跃任务**;不再恢复、不清理 |
| workflow_run_events | 296 行 |
| chat_sessions | 16 行(存量保留,阶段 4 起 app 层不再读写) |
| workflow_chat_links | 0 行 |
| checkpoints / blobs / writes | 2214 / 8270 / 29679 |
| app_schema_migrations | 1-5 全部已应用 |
- 处置结论:**无运行中任务需等待**;全部存量表与数据不 DROP、不清理(与计划 §4.6 一致);已发布迁移定义不改写。

## 7. 依赖现状(阶段 1 输入)

- 主 `.venv` 已装有 `fastapi==0.162.0`、`uvicorn==0.54.0`(未在任何依赖清单声明);阶段 1 补声明到 `requirements.txt` + `pyproject.toml`,无需联网安装。
- `requirements-openstoryline.txt`(fastapi 0.128 / uvicorn 0.40 / langchain 系 / torch 系等)为旧运行时专属,阶段 2 处理;`.venv-openstoryline/` 本地目录按计划留待部署清理步骤,本次不动。

## 8. 产物清单

- `baseline-unit.xml` / `baseline-integration.xml` / `baseline-agent-gate.xml` / `baseline-openstoryline.xml`
- `baseline-run.log`(五套件完整输出)
- 本文件 `baseline.md`

# 阶段2 · 迁移保留测试并删除旧聊天运行时 —— 审计与回归报告

> 日期:2026-11-04
> 执行计划:《单流水线架构设计与分阶段执行计划_评审修订版_20261104》阶段 2
> 前置:阶段 0(commit `d2eb1d8`)、阶段 1(commit `6530837`)已完成

---

## 1. 删除清单(git rm,共 122 个跟踪文件)

| 范围 | 内容 |
|---|---|
| 旧聊天宿主 | `agent_fastapi.py`(根,~5.6k 行:ChatSession / WS 聊天循环 / import-video / clean-cache / workflow router 挂载) |
| ①运行时整包 | `src/open_storyline/`(68+ py:agent、mcode_*、zcode_*、mcp/、storage/、nodes/ vendored 副本、utils/prompts.py、4 个转发壳 + hooks/ 2 个壳);随后空目录 `src/` 一并移除 |
| 旧测试 | `tests_openstoryline/` 整目录(28 测试文件 + conftest + fixtures;保留测试已先迁出) |
| 聊天 UI | `web/index.html`、`web/static/app.js`(5.4k 行)、`mcode_panel.mjs`、`media_upload.mjs`、`workflow_chat_indicator.mjs`、`style.css`、7 张仅聊天页引用的图片;`web/tests/{mcode_panel,media_upload,workflow_chat_indicator}.test.mjs` |
| 旧依赖清单 | `requirements-openstoryline.txt`(整文件皆为旧 runtime 专属依赖;主依赖已在 requirements.txt;`.venv-openstoryline/` 目录不动,留部署清理) |
| 旧脚本 ×4 | `check_openstoryline_llm.py`、`drill_disconnect.py`(import 已死)、`refactor_dedupe_openstoryline_mcp.py`(一次性)、`import_session_states.py`(依赖被删 ChatSession) |

**保留**:web/static/workflow_monitor.mjs、web/tests/workflow_monitor.test.mjs、web/node_map/、web/api_workflow.py、monitor_fastapi.py、jy_common/(在仓库根,不在 src/)、prompts/tasks/(48 模板唯一物理目录)、storyline_capabilities/、assembly_capabilities/。

## 2. 先行改造(删除前落地)

1. **`scripts/capture_prompt_golden.py` 单 Loader 化**(计划任务 1):不再 import `open_storyline.utils.prompts`;只记录 `storyline_capabilities.prompts` 的 load/render SHA + 模板 SHA;**golden 已重录**(48 份,`tests/unit/golden/prompt_render_golden.json` 新 schema)。
2. **`tests/unit/test_dual_prompt_loader_semantics.py` → `test_prompt_loader_semantics.py`**(git mv + 重写):golden 比对改单口径(sha_template/sha_load_prompt/sha_render_prompt),保留 Loader 语义矩阵(缺失文件→空串、三语法、空白容忍、缺变量原样保留、无缓存直读)全部钉住;osl 侧 7 个语义用例随模块删除(git 历史可查)。
3. **`tests/unit/test_prompt_core_strategies.py`**:删除 osl Facade 等价性抽查(raw_double/raise),core 策略直测保留。
4. **protocol 测试迁移**:`test_mcode_status_constants.py` 中 3 个仅依赖 `agent_gate.protocol`/`drive.py` 的测试 → 新文件 `tests/unit/test_agent_gate_protocol_status.py`;6 个扫描旧 mcode 源文件的用例随被测文件删除。
5. **监控前端测试**已在阶段 1 迁出(`tests/test_workflow_monitor_frontend.py`)。

## 3. asr_runner vendored 死分支清理(R-14)

- 事实:``VendoredASRClient`` spawn `-m open_storyline.runway`,该模块**删除前就不存在**(全仓零命中),链路今天已是死链;`STORYLINE_ASR_MODE` 无任何生产入口设为 vendored。
- 处置:删除 `VendoredASRClient`、vendored 分支、`ASR_VENDORED_TIMEOUT_S`;`STORYLINE_ASR_MODE` 只接受 `stub`(其他值 import 即报错,注明 vendored 已删);stub 默认路径不变(保留节点 `node_local_asr` 消费的 `ASR_MODE`/`transcribe_media` 接口不变)。
- 配套:`config.py` 删除 `VENDORED_OPENSTORYLINE_ROOT` / `VENDORED_OPENSTORYLINE_VENV_PY`(唯一消费方即上述死分支)。

## 4. config.py 死常量(证据驱动)

| 常量 | 处置 | 依据 |
|---|---|---|
| `OPENSTORYLINE_CMD` | **删** | 零消费者(本来就死) |
| `openstoryline_mcp_url()` | **删** | 零调用者 |
| `VENDORED_OPENSTORYLINE_ROOT/_VENV_PY` | **删** | 唯一消费方 asr_runner vendored 死分支(本次删除) |
| `OPENSTORYLINE_MCP_PORT/WEB_PORT/HEALTH_TIMEOUT_S`、`openstoryline_web_url()` | **保留** | `nodes/node_02`(human 节点,阶段 3 删)在用 |
| `IMPORT_VIDEO_*`、`STORYLINE_MODE/HUMAN_FUSION/FUSION_DISABLE` | **保留** | 保留侧消费中 |
| `nodes/node_02` 内 `OPENSTORYLINE_DIR/AGENT/VENV_PY` 三死常量 | **删** | 定义后零使用;AGENT 指向已删文件 |

## 5. import 审计(删除后)

- 全仓 grep `open_storyline|agent_fastapi|mcode_chat_model|zcode_agent_adapter`(生产目录 + tests/):**零功能性引用**;剩余命中全部为注释/docstring(storyline/__init__、storyline_capabilities 各文件头、config.py:269、node_04a 文档字符串等,属历史说明,阶段 5 文档收口统一处理)。
- 导入冒烟:`import graph; import agent_gate.mcp_server; import monitor_fastapi; import nodes.node_02_launch_openstoryline; import storyline_capabilities.asr_runner` 全部通过(主 `.venv`)。
- `start_production.ps1`/`stop_production.ps1` 仍引用 `agent_fastapi:app` 与旧清理段 —— 属阶段 5 范围,本阶段不动(脚本与代码删除的中间态按计划接受)。

## 6. 回归对照(对阶段 0 基线,JUnit 逐条差分)

| 套件 | 基线 | 阶段2 | 差分 |
|---|---|---|---|
| `tests/unit` | 18 failed | 15 error + 3 failed(**同 18 个 ID,NEW=0/GONE=0**) | 15 个同 ID 用例 failure→error:`test_session_store_repository.py:59` 惰性 import 已删除的 `open_storyline`(被测对象=旧聊天会话存储,阶段 4 计划删除物);其余 3 个 rate_limit 用例与基线同因失败 |
| `tests/integration` | 23 error + 3 failed | **完全一致**(23+3,类型也一致) | 零变化 |
| `agent_gate/tests` | 1 failed | 1 failed(同 ID `test_scaffold.py::test_init_writes_expected_files`) | 零变化 |
| `node --test web/tests/*.test.mjs` | 77/77 | **77/77 全绿**(NODE_EXIT=0) | 零变化 |
| `tests_openstoryline` | 66 failed | 目录已删(不再收集) | 按计划移除 |

**结论:零新增失败 ID。** 唯一类型变化(15× failure→error)是被删运行时的直接后果,逐条列于 `stage2-run.log` 与 JUnit XML;保留流水线(agent_gate / auto 图 / 监控 / 前端)无任何变化。

## 7. 产物

- `stage2-unit.xml` / `stage2-integration.xml` / `stage2-agent-gate.xml` / `stage2-run.log`
- 本文件

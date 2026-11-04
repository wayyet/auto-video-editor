# 阶段3 中断交接文档(2026-11-04,程序重启前保存)

> **2026-11-05 更新:本清单已执行完毕。** 6 个"新增失败"确认为纯环境性(当日 PG 容器未启动,
> docker start 后 25/25 全绿);WIP 收尾已审计合格并 amend 进阶段3 commit;
> 门禁结果:unit 18=18 同 ID 零新增 / integration 零新增 / 前端 14/14 / 导入冒烟过 /
> 残留扫描仅"已删除"说明性注释。本文档保留为中断-续作过程记录。

> 执行计划:《auto-video-editor_单流水线架构设计与分阶段执行计划_评审修订版_20261104.md》§6 阶段3-6
> 本文档由执行会话在收到"停止执行,重启程序"指令时写盘,供重启后的会话精确续作。
> 分支:`refactor/remove-chat-loop-20261104`(未推送,离线惯例)

---

## 1. 状态快照(2026-11-04 16:2x)

| 项 | 状态 |
|---|---|
| HEAD | `4cc7d29` refactor(graph): 阶段3 图拓扑收敛 auto-only(子代理已提交) |
| 前置 commit | `8af762d` scripts/resume_workflow.py 直接CLI resume(✅ 门禁过:unit 18=18 同ID) |
| 阶段0-2 | `ab7982b`/`d2eb1d8`/`6530837`/`7c70d30` 全部完成(sess_ddb715fe 交付) |
| 未提交 WIP | **36 文件,+407/-1198**(子代理被取消前的第二波收尾,内容见 §3) |
| 最近一次 unit(WIP 状态跑的,stage3-unit.xml) | 1220 条:18 坏(test_session_store_repository,**基线存量**) + **6 坏(test_workflow_chat_link_store,新增失败未修复)** |
| 未跟踪 | `docs/remove-chat-loop-20261104/schema-snapshots/`(子代理已生成快照)、`stage3-unit.xml`、**`docs/第二期开发/自动剪辑工作流重复冗余模块分析与合并设计执行计划.md`(95KB,来源不明,非本次任务授权产物——重启后由用户甄别,严禁混入阶段3 commit)** |

`4cc7d29` 自述已知遗留(与 §3 清单一致):tests/ 13 用例 collection error、monitoring/workflow_graph.py 仍读 STORYLINE_HUMAN_FUSION —— 即任务 9/10/11 未完,正是未提交 WIP 的内容。

## 2. 阶段3 剩余工作核对清单(重启后逐项核对 WIP 完成度,缺啥补啥)

先 `git diff` 逐文件核对下面各项是否已在 WIP 中完成,再补缺:

- [ ] **A. state.py**:删 `openstoryline_pid/openstoryline_mcp_endpoint/openstoryline_web_url/openstoryline_ready/preview_opened/cache_cleaned/cache_cleaned_paths/import_video_status/import_video_media_path/import_video_media_paths/import_video_web_session_id/import_video_filename`。**保留**:`shot_plan`(node_05 兜底+_route_after_storyline_join 判断)、`chat_session_id`(阶段4删)、`storyline_search_media_artifact/storyline_timeline_plan_ai/storyline_auto_plan`(auto 链在用;注释头"human 模式融合专用键"应改为"2026-10 调序新增的独立键")。
- [ ] **B. monitoring/workflow_graph.py**:删 FUSION_FLAG/fusion 分支/FUSION_NODE_NAMES import/_edge_label 的 fusion 参数;`_current_mode` 改为直接返回 config.STORYLINE_MODE(删对已不存在的 graph._STORYLINE_MODE_OVERRIDE 的 import);删 _HUMAN_ONLY_NODES 与 _NODE_META 6 条目(clean_cache/launch_openstoryline/open_preview/checkpoint0_storyline_plan/import_video/get_storyboard_plan);_EDGE_LABELS 删 6 条已不存在边的标签;CHECKPOINT_MARKS 改 ("①","②","③");输出 JSON 保留 `mode`(恒 auto)+`flags`(仅 QC)字段以兼容 web/static/workflow_monitor.mjs;SCHEMA_VERSION 保持 1;storyline_load_media/generate_draft 的 description 去 human 措辞。
- [ ] **C. monitoring/workflow_runner.py**:STATE_SUMMARY_KEYS 删 import_video_*/openstoryline_ready(保留 draft_encryption_status/video_input_paths)。chat_link 相关**不动**(阶段4)。
- [ ] **D. scripts/run_workflow.py**:_check_final_state 简化(删 openstoryline_ready 判定→打印 error_log 恒返 0);`--chat-session-id` 不动。
- [ ] **E. scripts/resume_workflow.py**:删 EXIT_OPENSTORYLINE_NOT_READY;_check_final_state 同 D;docstring 退出码/关卡标识更新(①②③)。
- [ ] **F. scripts/_smoke_auto_storyline.py**:删 _STORYLINE_MODE_OVERRIDE import 与 set/reset;docstring "START 直连 storyline_load_media"。
- [ ] **G. scripts/capture_graph_schema_snapshots.py**:fusion 引用清理;快照已生成到 docs/remove-chat-loop-20261104/schema-snapshots/(未跟踪,需纳入 commit);确认内容与编译图一致后提交。
- [ ] **H. 注释修正**:nodes/_checkpoint.py(⓪/①/② → ①/②,step 注释删 ⓪=4)、nodes/storyline/qa_gate.py:15、nodes/storyline/_common.py:135(写入者改指 storyline_load_media/入口层)、mcp_clients/__init__.py:5(node_02 引用改写;**mcp_clients 包保留**)。
- [ ] **I. tests/conftest.py**:删 _skip_checkpoint0_for_pre_migration_tests/_no_wait_for_manual_import_button/_fusion_off_by_default 三 autouse fixture + stub_proc/_StubProc(零消费者已核实)。
- [ ] **J. 测试处置**(WIP 中已有部分 D 状态与 M 状态,核对补齐):
  - 已 git rm(WIP 中):tests/integration/test_e2e_tc01_to_tc06.py、test_human_fusion_graph.py、test_no_auto_import_video.py、test_no_autoclean_cache.py;tests/unit/test_import_video_request_result.py、test_node_01_clean_cache.py、test_node_02_launch_openstoryline.py、test_node_03_open_preview.py、test_node_04a_import_video.py、test_node_04b_get_storyboard_plan.py、test_node_checkpoint0_storyline_plan.py、test_storyline_fusion.py。
  - 需更新(核对 WIP 是否已改完):test_workflow_graph_schema.py(auto-only 重写,删 FUSION_SPECS)、test_unreachable_nodes_wiring.py(删 clean_cache 豁免与 override,首任务断言改 storyline_load_media)、test_draft_path_degradation.py、test_nodes_checkpoint_helper.py、test_auto_mode_end_to_end.py、test_interrupt_resume.py(⓪段删,保留①②③)、test_state_field_compatability.py、test_week4_graph.py、test_week5_resilience.py、test_postgres_resume.py、test_reorder_topology.py、test_sequential_integration.py、test_video_input_paths_wiring.py、test_assembly_qc_graph.py、test_graph_compiles.py、test_workflow_stream_events.py、tests/unit/monitoring/test_workflow_events.py、test_flag_defaults_aligned.py、test_shared_node_helpers_single_source.py、test_workflow_runner_chat_link.py、web/tests/workflow_monitor.test.mjs、tests/unit/test_workflow_monitor_frontend.py。
- [ ] **兜底残留扫描**:`grep -rn "node_01_clean_cache\|node_02_launch\|node_03_open_preview\|node_04a\|node_04b\|node_checkpoint0\|storyline.fusion\|STORYLINE_HUMAN_FUSION\|STORYLINE_FUSION_DISABLE\|IMPORT_VIDEO_\|OPENSTORYLINE_\|openstoryline_web_url\|EDGE_BROWSER\|CACHE_PATHS\|CACHE_GLOB\|resolved_cache_paths\|resolved_trash\|CACHE_TRASH\|openstoryline_ready\|openstoryline_pid\|preview_opened\|import_video_\|cache_cleaned\|_STORYLINE_MODE_OVERRIDE\|_route_mode\|_route_after_launch\|_route_after_import" tests/ scripts/ monitoring/ nodes/ storyline/ web/ mcp_clients/ db/ assembly_capabilities/ storyline_capabilities/ *.py`,docs/ 与"已删除"说明性注释外零命中。

## 3. 未提交 WIP 的性质与处置建议

WIP = 上述清单 B-I/J 的实施(子代理做得比原清单更宽:还改了 nodes/node_05_generate_draft.py、storyline/__init__.py、tests/unit/monitoring/test_workflow_events.py、test_flag_defaults_aligned.py、test_shared_node_helpers_single_source.py、test_workflow_runner_chat_link.py、web/tests/workflow_monitor.test.mjs、tests/integration/test_graph_compiles.py、test_workflow_stream_events.py —— 重启后 `git diff` 逐个核对是否改得对)。

**处置建议**:修完 §4 门禁后 `git add`(⚠️ 只 add 代码+测试+docs/remove-chat-loop-20261104/,**绝不 add docs/第二期开发/**)→ `git commit --amend`(把收尾并入 `4cc7d29`,保"每阶段一 commit"纪律;分支未推送,amend 安全。若用户不愿 amend 则二次 commit,由用户定)。

## 4. 阶段3 提交门禁(逐条过完才允许 commit)

1. `./.venv/Scripts/python.exe -m pytest tests/unit -q --junitxml=docs/remove-chat-loop-20261104/stage3-unit.xml` —— 与 baseline-unit.xml 按**坏用例 ID 集合**差分,**零新增**(基线 18 全在 test_session_store_repository,阶段4删除对象)。⚠️ 当前已知 6 个新增坏 ID(test_workflow_chat_link_store ×6):先查根因——该 store 是阶段4删除对象,但纪律要求阶段3 commit 不得带新增失败;若是 WIP 改动误伤则修,若根因属阶段4删除范围且修复即阶段4内容,向用户报告后按用户决定处理。
2. `pytest tests/integration -q --junitxml=.../stage3-integration.xml` —— 基线 23 error(test_sessions_api_postgres,openstoryline 死路径)+3 failure(test_assembly_qc_graph 环境性、test_human_fusion_graph[已删,ID消失不算新增]、test_reorder_topology 环境性);零新增坏 ID。
3. `node --test web/tests/*.test.mjs`(**必须 glob 形式**,目录形式在本机 node v24 报 MODULE_NOT_FOUND)77/77。
4. 导入冒烟:`./.venv/Scripts/python.exe -X utf8 -c "import graph, config, monitoring.workflow_graph, monitoring.workflow_runner"`。
5. §2 兜底残留扫描。

commit message 首行含"阶段3",正文列:config auto-only fail-fast / R-03 图入口重接 / R-10 clean_cache+缓存配置块 / R-11 node_02/03 依赖 / R-12 conftest fixture / fusion 删除 / state 字段清理 / workflow_graph 元数据 / schema 快照重录 / 测试迁移清单 / 回归差分结果。

## 5. 之后:阶段4 → 阶段5 → 阶段6(按计划文档 §6)

- **阶段4** chat_link 收口:删 monitoring/workflow_chat_link_store.py、workflow_runner 的 _default_link_store/_chat_link_upsert_running/_chat_link_update/_CHAT_LINK_STATUS_BY_RUN_EVENT/link_store 参数、run_workflow.py --chat-session-id(_chat_session_id_arg/_apply_chat_session_id)、WorkflowState.chat_session_id、web/api_workflow.py chat-session 查询、tests/unit/test_workflow_chat_link_store.py 等 chat_link/session 测试(test_session_repository/test_session_store_repository/test_sessions_api_postgres/test_session_repository_postgres);**db/migrations.py 迁移1/5 定义保留加 legacy 注释;存量表不 DROP**;fresh/existing DB 迁移测试。commit 含"阶段4"。
- **阶段5** 启停脚本/README/ADR:start_production.ps1 删 .venv-openstoryline/agent_fastapi/Phase 0.6/0.7/human 参数+增 monitor_fastapi 启动项(主 .venv,127.0.0.1:7860,PID/日志);stop_production.ps1 只停本项目 PID 的 monitor,禁止按端口盲杀;README 重写(drive 示例必含 --thread-id/--agent/--project-dir,--video 在 AGENT_GATE_MEDIA_ROOTS 白名单);ADR 追加;旧文档加失效头注;.venv-openstoryline 不删。commit 含"阶段5"。
- **阶段6** 全量回归:JUnit 差分(同环境)、图拓扑断言(START→auto 链可达、①②③ 可达)、E2E agent_gate start --backend stub(唯一 thread_id,--video 白名单)停人工关卡→status;resume 验证可用 scripts/resume_workflow.py --dry-run/--checkpoint 或对隔离 thread 用 --force-no-tty 并记录(真机 TTY resume 留用户);monitor 冒烟(SSE/Last-Event-ID/node-map/PG 不可达 503);残留扫描;git diff --check/secret 扫描;docs/remove-chat-loop-20261104/regression.md。commit 含"阶段6"。

## 6. 纪律与已知陷阱

- 仓库路径含中文;所有编辑 UTF-8,严禁 PowerShell 重写配置/py。
- node --test 用 glob 形式。
- PG localhost:5432/video_workflow 可达;存量表(chat_sessions 16 行/workflow_chat_links 0 行/checkpoint blob 等)只读不 DROP;迁移1-5 定义不改写。
- 不动 agent_gate/ 行为、E:\Documents\kuaishou\video-ops、.venv*。
- 基线 JUnit 在 docs/remove-chat-loop-20261104/(baseline-unit/integration/agent-gate/openstoryline.xml)。
- `docs/第二期开发/` 目录来历不明,**不 add、不删、不改**,待用户甄别。
- 禁止 push。

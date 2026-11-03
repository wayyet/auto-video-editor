# auto-video-editor · 删除①对话回合循环,收敛为②流水线循环 · 执行计划

> 日期:2026-11-04
> 状态:计划已批准;**执行以用户明确确认"开始执行"为启动信号**
> 来源会话:sess_c8a2a0cc-66f4-455e-8fd2-d07d0e7e30cd(「两套循环」架构讨论)+ 本次规划会话
> 计划分支:`refactor/remove-chat-loop-20261104`(自 `refactor/dedupe-openstoryline-mcp-20261103` @ `9ab3df5` 切出)
> 执行纪律:沿用阶段0-6 分阶段提交模式,每阶段一次 commit、测试零新增失败

---

## 1. 背景与动机

本项目当前存在**两套独立循环**:

- **① 对话回合循环**:Web 聊天 UI(127.0.0.1:7860,`agent_fastapi.py`)+ mcode/zcode 档位子进程。每条消息 spawn 一次 `mcode exec`(900s 硬预算,`mcode_chat_model.py:653`),回合结束即回到等待输入。
- **② 流水线循环**:LangGraph 后台 worker,由 video-agent MCP `run_start` 启动,关卡制自动运行(LLM 任务走信箱,人工关卡 interrupt),PostgreSQL checkpoint,可恢复。

2026-11-03 的故障排查(素材路径不存在)暴露了①②接缝治理的成本(两层配置、路径双份、回合冷启动、终止语义模糊);架构讨论后用户决定:**彻底删除①,把项目收敛为纯②流水线自动运行**。

## 2. 探索结论(关键事实)

三路并行代码探索 + 专项核实,结论如下:

1. **②对 `src/open_storyline`(①运行时,68 个 py 文件)零 import 依赖**。`storyline_capabilities/` 是 core_nodes 的 vendored 副本,与①互不引用。全仓 grep `^(from|import) open_storyline`:src 外仅 `agent_fastapi.py` 与 `scripts/capture_prompt_golden.py` 两处命中。
2. 耦合仅在**五个接缝**:Web 宿主、web 静态资源混居、human 模式节点、`workflow_chat_links` 表、提示词双 Loader。
3. `POST /api/system/clean-cache`(`agent_fastapi.py:3941`)**仅被聊天前端按钮调用**(`web/static/app.js:2866`),②图节点不经 HTTP 调它,删除①后该端点无存在必要。
4. `STORYLINE_MODE` 默认值为 `human`(根 `config.py:366`);`start_production.ps1` 在 auto 模式本就跳过 Web 生命周期阶段(555-557)。
5. LLM 答题后端现状(`agent_gate/factory.py:18`):`stub`(默认,假答案)/ `gate`(信箱,等外部答题者)/ `claude-cli` / `mcode-cli`,**没有直连 HTTP API 后端**;MCP 路径的 worker 由 `run_start` 固定以 `--backend gate` 拉起,答题者 = 与 MCP 对话的 agent(目前是聊天里的 mcode)。
6. `web/api_workflow.py`(610 行,②监控 API)宿主在①的 FastAPI 进程(`agent_fastapi.py:4571-4573` 挂载),其 `POST /runs` 用主 venv 子进程跑 `scripts/run_workflow.py`,事件落共享 PG 表,SSE 只读补发——与聊天无逻辑耦合,可整体搬迁。
7. 现成无人值守入口:`agent_gate drive`(`drive.py:35-119`)自动 spawn worker + 反复拉起 claude/mcode 会话答题 + 自动崩溃重启,停在人工关卡(退出码 10);`--backend stub` 可零依赖全自动过 LLM 关卡。**没有任何入口能"全自动闯过人工 interrupt 关卡"**(刻意防火墙:CLI resume 要 TTY、human_resume 要真人标记)。

## 3. 已拍板决策(2026-11-04)

| # | 决策点 | 结论 |
|---|---|---|
| 1 | ②监控 Web UI(工作流监控/SSE/node-map) | **瘦身保留**:独立轻量入口 `monitor_fastapi.py`,只挂 `/api/workflow/*` + node-map + 独立监控页;fastapi/uvicorn 装进主 `.venv`;`.venv-openstoryline` 整体退役 |
| 2 | 删①后 13 类 LLM 关卡任务谁来答 | **drive 链路**:`python -m agent_gate drive --agent mcode --project-dir video-ops`,无人值守拉 mcode 会话答题,零新代码;人工关卡到点停(退出码 10),`agent_gate resume` 放行 |
| 3 | human 模式链路(STORYLINE_MODE=human) | **彻底删除**:graph 只留 auto 拓扑,默认 auto,传 human 直接报错 |

## 4. 目标终态

```
唯一循环 = ②流水线循环,两条等价触发路径:
  A. 直连:scripts/run_workflow.py --production(进程内,VIDEO_LLM_BACKEND 选答题后端)
  B. Agent:video-agent MCP run_start → 后台 worker;无人值守 = agent_gate drive
监控 = monitor_fastapi.py(127.0.0.1:7860,主 .venv,只挂 /api/workflow/* + node-map + monitor.html)
删除 = 聊天 Web、mcode+zcode 档位、src/open_storyline、human 模式、chat_link、.venv-openstoryline
```

## 5. 不动清单

- `agent_gate/` 包全部(MCP 服务、runner、信箱、drive、lease、safety、tests)
- `graph.py` auto 19 节点链 + assembly QC + 人工 interrupt 关卡①②③(保留,这是②的内建暂停点)
- `monitoring/`(除 chat_link)、`db/connection.py`、`db/postgres_config.py`
- `.agents/mcp.json`(②消费入口注册)
- `E:\Documents\kuaishou\video-ops` 操作员目录(②的 drive 工作区 + MCP 注册,含 `.mcp.json`/AGENTS.md/mcp_cli.py)
- `inputs/`、`outputs/` 存量;**PostgreSQL 存量表一律不 DROP**(与既有"存量不处理"决定一致,只删代码不删数据)
- 主 `.venv`

## 6. 分阶段实施

### 阶段0 · 基线盘点
- 建分支 `refactor/remove-chat-loop-20261104`。
- 记录现有测试基线(unit / integration / agent_gate / web tests)。
- **`config.toml.local` 备份为 `config.toml.local.bak-20261104`**(gitignore 文件,删段不可逆)。

### 阶段1 · 监控 Web 瘦身(最先做,给②监控找新宿主)
- 新建仓库根 `monitor_fastapi.py`:挂 `web/api_workflow.py` router + `/node-map` 静态 + 新写 `web/monitor.html` 独立监控页(以 `workflow_monitor.mjs` SSE 为核心;`workflow_chat_indicator.mjs` 节点状态渲染尽量并入,代价过大则丢弃该组件)。端口保持 7860,`Origin` 校验保留。
- `web/api_workflow.py`:删 `/chat-sessions/{id}/run` 端点及 chat_link 引用;`POST /runs` 子进程机制原样保留。
- `requirements.txt` 加 `fastapi`、`uvicorn[standard]`(监控进主 .venv)。
- 迁移②前端测试:`tests_openstoryline/test_workflow_monitor_frontend.py`、`test_workflow_chat_indicator_frontend.py` → `tests/`;`web/tests/` 保留 monitor/indicator 两个 `.mjs`。
- 删:`web/index.html` 聊天壳、`web/static/app.js`(5374 行)、`mcode_panel.mjs`、`media_upload.mjs`、`web/tests/mcode_panel|media_upload.test.mjs`。

### 阶段2 · 删①运行时
- 整删 `src/open_storyline/`(先确认 `jy_common` 不在其中,保留)+ 根级转发壳 `server.py`、`register_tools.py`、`sampling_handler.py`、`sampling_requester.py` + `tests_openstoryline/` 整目录(②测试已在阶段1迁出)。
- 删 `requirements-openstoryline.txt` + `.venv-openstoryline/`。
- `config.toml` / `config.toml.local`:删 `[mcode]/[zcode]/[glm]/[llm]/[vlm]/[local_mcp_server]/[skills]/[developer]` 及仅①消费的 storyline 参数段(**UTF-8 编辑,防 GBK 污染**)。
- 根 `config.py`:删 `OPENSTORYLINE_CMD / OPENSTORYLINE_MCP_PORT / OPENSTORYLINE_WEB_PORT / OPENSTORYLINE_HEALTH_TIMEOUT_S / openstoryline_web_url / IMPORT_VIDEO_* / VENDORED_OPENSTORYLINE_*` 常量;`storyline_capabilities/asr_runner.py` 的死引用(`VendoredASRClient` spawn 不存在的 `open_storyline.runway`)清掉,stub 降级保留。
- 提示词收敛单 Loader:删 `src/open_storyline/utils/prompts.py`,保留 `jy_common/prompt_core` + `storyline_capabilities/prompts` + `prompts/tasks/`(48 模板唯一物理目录);`scripts/capture_prompt_golden.py` 与 `test_dual_prompt_loader_semantics.py` 改单口径并重录 golden。
- 删①专属脚本:`check_openstoryline_llm.py`、`drill_disconnect.py` 等(按 import 图逐一判定)。

### 阶段3 · 删 human 模式
- `graph.py`:删 human 分支(`_route_mode` 319-331、`node_checkpoint0_storyline_plan`、`node_04a_import_video`、`node_04b_get_storyboard_plan`、`node_02_launch_openstoryline`、`node_03_open_preview`),图只留 auto 19 节点链 + assembly QC + 人工 interrupt 关卡①②③(**interrupt 保留,这是②的内建暂停点**);`nodes/storyline/fusion.py` 的 `STORYLINE_HUMAN_FUSION` 清理。
- 根 `config.py`:`STORYLINE_MODE` 默认改 `auto`,传 `human` 在 import 时报错;`monitoring/workflow_graph.py` 拓扑同步。
- `scripts/run_workflow.py` 的 `_check_final_state`(`openstoryline_ready` 判定)简化;schema 快照重录;`test_node_02/03/04a/04b`、checkpoint0 相关测试删除;`test_auto_mode_end_to_end`、`test_interrupt_resume` 更新基线。

### 阶段4 · DB / chat_link 收口(存量数据不 DROP,只删代码)
- 删 `monitoring/workflow_chat_link_store.py`;`monitoring/workflow_runner.py:449` 的 chat_link 写入与 `run_with_events` 的 chat_session_id 参数;`scripts/run_workflow.py` 的 `--chat-session-id`(64-93、239-249);`WorkflowState.chat_session_id` 字段。
- `db/migrations.py`:移除 `chat_sessions`(98-119)、`workflow_chat_links`(280-282)迁移代码,保留 `workflow_runs / workflow_run_events / app_schema_migrations`。
- 删测试:`test_session_repository.py`、`test_session_store_repository.py`、`test_sessions_api_postgres.py`、`test_session_repository_postgres.py`、4 个 chat_link 测试(unit × 3 + integration × 1)。

### 阶段5 · 脚本与文档
- `start_production.ps1`:删 Phase 0.6/0.7(Web 生命周期,543-791)与 `.venv-openstoryline` 引用,保留 Phase 0/0.5/0.55/0.58/2/3;`stop_production.ps1` 删 Web / `open_storyline.mcp.server` 清理段(173-282)。
- README 写入 drive 用法:

  ```powershell
  python -m agent_gate drive --agent mcode --project-dir E:\Documents\kuaishou\video-ops --video <素材路径>
  # 自动拉起 worker + mcode 会话答题,自动崩溃重启;人工关卡退出码 10 → python -m agent_gate resume
  ```

- `README.md`(§1 目录树 / §2.6 / 退出码表 / §10 架构图)重写为②-only;`README-OpenStoryline.md` 及 docs/ 下 mcode/zcode 专属文档加「历史文档,2026-11-04 架构收敛后失效」头注;`docs/integration/architecture_decision_record.md` 追加 ADR(双循环收敛决议与依据)。

### 阶段6 · 全量回归与验收
- 新基线记录落 `docs/remove-chat-loop-20261104/regression.md`。

## 7. 验证

1. `pytest tests/unit tests/integration` 对照阶段0基线**零新增失败**(存量 PG 不可达 ERROR 不计);`pytest agent_gate/tests` 全绿;`node --test web/tests` 全绿。
2. 导入冒烟:`.venv/Scripts/python.exe -X utf8 -c "import graph, agent_gate.mcp_server"`。
3. 端到端冒烟:`python -m agent_gate start --backend stub --video tests/fixtures/30s.mp4` → 应停在人工关卡,`status.json` 显示 `ask_human`。
4. 监控页冒烟:起 `monitor_fastapi` → 浏览器确认 SSE 事件流 + node-map 可用。
5. 残留检查:全仓 grep `open_storyline | agent_fastapi | mcode_chat_model | zcode_agent_adapter` 生产代码零命中(文档历史头注除外)。
6. 真机(可选,用户操作):`drive --agent mcode` 跑一条真实素材,人工关卡用 `agent_gate resume` 放行,监控页观察全程。

## 8. 风险与注意

| 风险 | 对策 |
|---|---|
| 仓库路径含中文「开发代码」 | 所有生成/编辑文件保持 UTF-8,勿经 PowerShell 重写(既有教训) |
| `config.toml.local` 含 API key 且不入库 | 阶段0先备份;删段前 diff 确认 |
| workflow_chat_indicator 并入监控页改造量不确定 | 实施时评估,最坏丢弃该组件(监控页以 workflow_monitor + node_map 为准) |
| 测试迁移顺序 | 阶段1先迁②前端测试再删 tests_openstoryline,避免中间态丢测试 |
| `db/migrations.py` 迁移号被引用 | 只删迁移代码不回滚 DB;`app_schema_migrations` 已记录项保持 |

## 附A · ①专属文件清单(删除范围)

- `agent_fastapi.py`(5588 行:app 定义 3448、WS 聊天循环 4617-5588、ChatSession 1983-3316、import-video 4006-4180、clean-cache 3941、workflow router 挂载 4571-4573)
- `src/open_storyline/` 全包:agent.py(build_agent@406 / _build_mcode_agent@228 / _build_zcode_agent@344)、config.py(MCodeConfig 281-321 / ZCodeConfig 324-374)、mcode_chat_model.py(MCodeChatModel 621-1335)、mcode_runner / mcode_events / mcode_event_adapter / mcode_stream_parser、zcode_agent_adapter(ZCodeChatModel@448)/ zcode_event_adapter、mcp/(8001,已停用)、skills/skills_io.py、storage/、nodes/(vendored 副本)、utils/(含 prompts.py)、6 个兼容 shim(ADR-015)
- 根级转发壳:server.py、register_tools.py、sampling_handler.py、sampling_requester.py
- web/:index.html(525 行聊天壳)、static/app.js(5374 行)、mcode_panel.mjs、media_upload.mjs
- tests_openstoryline/ 整目录(325 条,存量 66 失败为环境性;②前端两测试先迁出)
- config 段:[mcode](config.toml:200-219)/[zcode]/[glm]/[llm]/[vlm]/[local_mcp_server]/[skills]/[developer]
- requirements-openstoryline.txt + .venv-openstoryline/
- human 模式节点 ×5(node_02/03/04a/04b/checkpoint0)
- chat_link 全链(store、runner 参数、API 端点、4 个测试)

## 附B · ②核心保留清单

- `agent_gate/`:mcp_server.py(run_start@72 / run_status@129 / gate_next_tasks@139 / gate_get_task@156 / gate_submit_result@168 / gate_submit_batch@177 / human_resume@191)、runner.py(spawn_worker@733 / run_worker@540 / make_checkpointer@183)、gate_client.py(信箱写任务@57-177)、mailbox.py、service.py、cli.py(start/drive/resume/worker)、drive.py(35-119)+ launcher.py + agent_bin.py、factory.py、lease.py、safety.py、validate.py、protocol.py、media.py、scaffold.py、tests/(16 文件)
- `graph.py`(build_graph@540)+ `state.py` + `nodes/`(auto 链 + assembly)+ `storyline_capabilities/` + `assembly_capabilities/`
- `monitoring/`(workflow_event_store / workflow_runner / workflow_graph,除 chat_link)+ `db/`(postgres_config@115-161 DSN 解析 / connection / migrations 保留部分)
- `scripts/run_workflow.py`、`scripts/preflight.ps1`、`start_production.ps1`(瘦身后)
- web/api_workflow.py + workflow_monitor.mjs + node_map + monitor.html(新)
- 仓库外:`E:\Documents\kuaishou\video-ops`(.mcp.json / AGENTS.md / mcp_cli.py)

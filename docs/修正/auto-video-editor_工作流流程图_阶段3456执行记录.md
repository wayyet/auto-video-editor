# auto-video-editor 工作流流程图 — 阶段3/4/5/6 执行记录

> 对应设计:[《工作流流程图与实时节点追踪》§9.6–§9.9](../修改文档/auto-video-editor-工作流流程图与实时节点追踪.md)
> 记录日期:2026-11-01
> 前序:阶段0/1/2 见 `auto-video-editor_工作流流程图_阶段0基线记录.md` / `阶段012执行记录.md`
> 执行范围:用户指定仅执行阶段3(统一 Runner 与双入口接线)、阶段4(SSE API 与重连补发)、阶段5(节点图前端实时化)、阶段6(回归验收与文档);阶段0–2 此前已完成。

## 1. 提交清单(每阶段独立一笔,逐文件 add)

| 阶段 | 提交内容 | 文件 |
|---|---|---|
| 3 | feat(monitoring): 统一 WorkflowRunner 与双入口接线 | 新增 `monitoring/workflow_runner.py`、`tests/integration/test_workflow_stream_events.py`;调整 `scripts/run_workflow.py`、`agent_gate/runner.py`、`nodes/node_04a_import_video.py`、`tests/unit/test_node_04a_import_video.py`、`tests/integration/test_preflight_windows.py` |
| 4 | feat(web): /api/workflow 监控 API 与 SSE 断线补发 | 新增 `openstoryline/web/api_workflow.py`;调整 `openstoryline/agent_fastapi.py`(挂载 router) |
| 5 | feat(web): 节点图前端实时化 | 新增 `openstoryline/web/static/workflow_monitor.mjs`、`openstoryline/web/tests/workflow_monitor.test.mjs`、`openstoryline/tests/test_workflow_monitor_frontend.py`;调整 `openstoryline/web/node_map/node_map.html` |
| 6 | docs: 验收记录与 README | 本文档、根 `README.md` §10 |

## 2. 阶段3:统一 Runner 与双入口接线

### 2.1 实现

- **`monitoring/workflow_runner.py`**(新增):`run_with_events(graph, initial_state, config, *, run_id, on_event, store, stream_mode)` 封装 `astream(stream_mode=["tasks","updates","custom"], version="v2")` 消费循环。每个事件经 `WorkflowStreamAdapter` 转为 §7.1 标准事件,**先 `append_event` 落库拿全局 id,再回调 on_event**。异常兜底:`run.failed` 后原样 re-raise(CLI 退出码语义不变);返回值 = `aget_state(config).values`(与 `ainvoke` 等价)。模块顶层不 import langgraph(`Command` 判定延迟到函数内)。
- **CLI 接线**:`scripts/run_workflow.py` 的 `ainvoke`(:259)替换为 runner 调用;pre-flight 顺序、`--skip-preflight`、退出码 0/2/3/130 全部不变;事件以 `[wf] #<id> <event_type> <node>` 行打 **stderr**(不污染 stdout 消费者)。
- **worker 接线(底线)**:`agent_gate/runner.py::run_worker` 在既有 status 挂点旁补发运行级事件——resume 用 `run.resumed`,首跑/崩溃续跑用 `run.started(reason=start/continue_after_crash)`;挂起 `run.interrupted`、完成 `run.completed`、失败 `run.failed`。保持同步 `invoke` 语义零变化(§11.9);事件写入失败仅记 debug,不影响 worker 状态机。**写入经单一后台线程 FIFO 落库**(阶段6 回归发现同步 PG 写会放大 `agent_gate/tests/test_images.py` 既有的取任务时序竞态窗口,见 §5.2/§6.2;`drain_run_events` 在 worker 退出前最多等 5s 排空队列,防进程退出丢尾事件)。
- **import_video 等待事件**:`nodes/node_04a_import_video.py` 轮询前经 `get_stream_writer()` 发 custom 事件(`monitor_event=waiting_input, waiting_kind=web_file_handshake`);轮询/超时/回滚逻辑零改动。**跨 venv 硬约束守护测试改为 AST 只查模块顶层 import**(函数级延迟 import 允许)——原全文子串匹配会误伤 `langgraph.config` 的延迟 import,新写法反而更精确地表达"顶层轻依赖"的真实意图。

### 2.2 关键实现决策(设计未预见,实测驱动)

1. **跨调用 resume 判定**:阶段0 契约说"resume 复跑同一 task id → node.resumed",但 resume 是**新一次** runner 调用,适配器的 task 状态不跨调用。改为:恢复运行前先 `aget_state` 取快照,把带 interrupt 的任务名传给适配器作 `resumed_nodes`;attempt 历史从事件表 `latest_snapshot().node_attempts` 续接。
2. **interrupt 双现去重**:以 tasks.end.interrupts 为准(§4.6 契约 3),updates 的 `__interrupt__` 忽略;挂起任务**不发** node.succeeded。
3. **branch.selected 时序推断**(§7.3 规则 8):`join:a+b:x` 触发器直接给出 from 集合(免费信号);条件边用"最近完成节点 ∩ builder.branches 条件入边"推断,`inferred=true`;普通静态边不发(降噪)。
4. **存储降级不伪造**:append 失败(返回 None)时事件照发进程内订阅者但**不带 id**,SSE 帧省略 id 行;运行本身不受影响。
5. **既有测试适配两处**(均为接线必然后果,语义未变):`test_preflight_windows.py` 的 mock 从 `ainvoke` 改为 `astream` 空 async 生成器 + `aget_state` 空终态 + patch `workflow_event_store.append_event`(保持该用例"不依赖活的 Postgres、不写生产库"的初衷);`test_node_04a_import_video.py` 守护测试如上。

### 2.3 测试

- 新增 `tests/integration/test_workflow_stream_events.py`(10 用例,isolated PG 测试库):起止序列+并行超步+fan-in join / interrupt→resume 游标跨运行单调 / 重试 attempt 递增+自环 branch.selected / custom 等待事件 / 异常脱敏+只发一次 node.failed / state 白名单摘要(提示词不外泄、绝对路径降级) / 存储降级显式降级 / None 输入 run.resumed / thread_id 校验。
- 全量回归:`1384 → 1394 passed, 0 failed`(失败集合=基线空;增量即本阶段新用例)。

## 3. 阶段4:SSE API 与重连补发

### 3.1 实现落点与关键决策

- 新增 `openstoryline/web/api_workflow.py`(独立 APIRouter,prefix=/api/workflow),`agent_fastapi.py` 在 `app.include_router(api)` 旁挂载;不进 FastMCP(§11.8)。
- **五端点**:graph(拓扑,模块级缓存+锁,`?refresh=1` 重建)/ POST runs(启动)/ GET runs 快照 / GET events(SSE)/ POST resume(挂起探测+指引)。
- **启动与订阅分离**:`GET .../events` 只读事件表,绝不触发图执行(§6.2-E 要点 1);重复启动由"事件表里该线程存在未终态运行"判定返回 409。
- **POST /runs 用主 venv CLI 子进程**:Web 进程(openstoryline venv)langgraph 是 1.1.10,生产运行时是 1.2.9 + editable checkpoint 4.1.1——Web 进程内跑图/执行 `Command(resume)` 有 checkpoint 兼容风险。子进程跑 `scripts/run_workflow.py`(经统一 runner,事件结构与 CLI 完全一致,§9.7 任务 5);resume 端点按设计 §6.2-E 明确允许的"**仅查询**外部恢复后的事件"形态实现,返回挂起探测与外部恢复通道指引。
- **SSE 行为**:连接时先显式查 `latest_snapshot().available`,不可用→503(§6.2-D 不得静默);有 `Last-Event-ID`(头或 `?last_event_id=`)从 `id > 游标` 补发,无则先发 `snapshot` 摘要帧再全量补发;之后 0.8s 短轮询共享事件表跟进(§11.4:跨进程事件只能这样到达),15s `: ping` 保活,每 20s 复查存储健康(坏→`store_error` 帧后关流);`run.completed/run.failed` 发出后关流。
- **访问控制**:有 `Origin` 头必须是本机来源(127.0.0.1/localhost/::1),跨源 403;thread_id 白名单正则(与 workflow_runs CHECK 同规则,防路径注入)。

### 3.2 跨 venv 实测(设计风险 §11.8 的实证)

- openstoryline venv **可导入主仓 graph**(冷 ~13s,含全部 nodes 重依赖),构图+拓扑提取 2.3s;`build_graph_schema` 输出与主 venv 基线完全一致(55 节点/61 边/13 条件边)——`GET /graph` 因此可在 Web 进程内直接服务。
- 限流中间件为纯 ASGI 透传(不缓冲响应),SSE 流式兼容;`/api/*` 通用桶 burst=120/rpm=2400 对 SSE 重连足够,未加专用桶。

### 3.3 curl 验收(§9.7,临时 uvicorn 实例 :7861,生产 :7860 未动)

| 项 | 结果 |
|---|---|
| `GET /api/workflow/graph` | ✅ 200;55 节点/61 边/13 条件边,flags/mode 与基线一致 |
| `POST /api/workflow/runs` | ✅ 202 + spawn;子进程经统一 runner 跑真实图(launch→open_preview→checkpoint0 interrupt),事件落生产库 |
| `GET /runs/{tid}` | ✅ count=10 / last_id=12 / state=waiting_human / node_attempts 正确 |
| `GET .../events` SSE 帧 | ✅ snapshot 帧 + id 单调帧,格式符合 §7.2(payload 平铺) |
| 断线续传 `Last-Event-ID: 5` | ✅ 首帧 id=6,补到 12,无重复无遗漏 |
| 刷新/重复 GET | ✅ 只重放,事件表无新 run |
| 跨源 Origin | ✅ 403;本机 Origin 200 |
| 挂起探测 resume | ✅ 200 + 指引(waiting_human);非挂起态 409 |
| 非法 thread_id | ✅ 400;路径穿越变体被路由/正则双重挡下 |

## 4. 阶段5:节点图前端实时化

### 4.1 实现

- **`static/workflow_monitor.mjs`(纯逻辑层,node --test 可测)**:§7.1 事件归约状态机(七态)、`Last-Event-ID` 补发去重(cursor 幂等,重放不重复应用)、并行多活动节点、attempt 历史不覆盖、布局 key(`wf_layout_<id>_<mode>_v<n>`,旧图沿用 `node_layout_<id>`)、已走边投影、主图数据归一(edges→dependencies/next_nodes)。
- **`node_map.html`**:保留深色主题/卡片/SVG 连线/拖拽/折叠/Expand-Collapse-Reset/localStorage 布局;新增双视图切换、运行状态区(thread 订阅、状态 chip、计数、七态图例、跟随开关)、节点七态样式+标题状态点+attempt 角标、展开节点 RUN STATUS 详情(状态/attempt/耗时历史/等待类别与关卡/脱敏错误)。`computeDepths` 从迭代松弛改为 **DFS+回边忽略**——QC/qa 重试回环与 node_07 自环会让松弛不收敛,把列布局炸穿(实测前就修掉的结构缺陷)。

### 4.2 投影语义(设计 §4.1"未命中分支不预点亮"的落实)

- 已走边:源/目标都真实执行过才点亮;`START→x` 目标启动即亮;`x→END` 需要 **x 已完成且运行正常完成,且 x 的其它出边都没被走过**(无歧义)——实测发现首轮实现会把未命中的 `launch_openstoryline→END` 条件门边点亮,已修正(浏览器对照:4 条→3 条)。
- "未参与"(skipped)只在**运行终态后**对启用/可达但未执行的节点给出;另一模式路径、QC 关闭的 6 节点、clean_cache 以"未连通/离线"虚线呈现,不算跳过。
- 跟随开关:活动节点在视口外才滚动;拖动节点/滚轮暂停。

### 4.3 测试与浏览器验收

- `node --test`:14 用例全绿(状态机/并行/去重/run 状态机/finalizeSkipped/END 边规则/布局 key/deriveRunState/归一);pytest 包装 `openstoryline/tests/test_workflow_monitor_frontend.py` 与 media_upload 同惯例。
- 浏览器实测(in-app browser,:7861 临时实例):
  - 主图 55 节点渲染(中文名/分组/关卡/modes;clean_cache 离线虚线);
  - 订阅挂起线程 `video-webacc-001`:事件重放正确着色(launch/open_preview 绿、checkpoint0 琥珀、3 条已走边绿、chip=等待人工操作·#12、计数 2/0/1/0);
  - **实时**新运行 `video-weblive-001`(页面内 POST /runs):launch_openstoryline 蓝色执行中 → 绿色完成 → checkpoint0 琥珀等待,SSE 实时推进;
  - 刷新页面重新订阅:状态从重放恢复,不产生第二次 run;
  - 旧 14 节点子流程视图:14 节点/19 边、交互与布局数据不受影响;
  - 展开节点 RUN STATUS 正确显示等待详情(事件先到、后展开的补渲染已修)。

## 5. 阶段6:回归验收与文档

### 5.1 AC-01..AC-14 对照

| 编号 | 结论 | 证据 |
|---|---|---|
| AC-01 页面展示编译图全部节点/边 | ✅ | 阶段1 schema 测试 + curl graph 与主 venv 基线逐点一致 + 浏览器 55 节点 |
| AC-02 启动→执行中、完成→已完成 | ✅ | 集成测试起止序列 + 浏览器实时观察(launch 蓝→绿) |
| AC-03 并行活动节点同时高亮 | ✅(测试图级) | 集成测试 b1/b2 同时 started + 前端 activeNodeIds;真实 node_15/17 链路未实跑(见 §6 已知限制) |
| AC-04 条件路由/重试反映真实路径 | ✅(测试图级) | 重试图 attempt 1..3 + branch.selected(inferred)+ 前端重试态;assembly QC/node_07 真实回环未实跑 |
| AC-05 interrupt=等待非失败,恢复沿用 thread_id | ✅ | 集成测试 waiting_input+run.interrupted+resume;真实 CLI 挂起在关卡⓪;浏览器琥珀态 |
| AC-06 import_video 独立等待态 | ✅(测试图级) | custom→web_file_handshake 用例 + node_04a 发射点;600s 真实网页握手未实跑 |
| AC-07 刷新/断线恢复且不重复启动 | ✅ | curl Last-Event-ID 补发无重复无遗漏 + 浏览器刷新重放;events 只读 |
| AC-08 三入口事件一致 | ✅(worker 运行级未实跑) | POST /runs 即 CLI 子进程(同一 runner);worker 挂点代码旁路既有 status 写入,有 try/except 兜底 |
| AC-09 QC 关闭→禁用/未参与 | ✅ | 阶段1 schema 两态测试 + 前端 st-offline |
| AC-10 fusion 两态正确、另一模式路径不显示为跳过 | ✅ | 阶段1 测试 + finalizeSkipped 排除 reachable=false 节点 + 离线样式 |
| AC-11 旧 14 节点图不受影响 | ✅ | 浏览器回归(14/19)+ 布局 key 隔离 + 旧 key 不变 |
| AC-12 不泄漏完整 state/密钥/提示词 | ✅ | 白名单单测 + 泄漏用例(真实运行事件的 summary 只含 keys 名单+白名单值) |
| AC-13 既有执行/checkpoint/resume 测试不回归 | ✅ | 全量 1394 passed 0 failed(失败集合=基线空);openstoryline 域失败=已知 3 条 config 泄漏基线 |
| AC-14 运行结束后可回放 | ✅ | fetch_events_after 全量回放测试 + latest_snapshot node_attempts + 页面订阅即回放 |

### 5.2 最终回归(阶段6 复核)

- 仓库根全量:`python -m pytest -m "not slow and not admin"` → **1394 passed, 2 skipped, 3 xfailed, 4 xpassed, 0 failed**(基线=空失败集合,保持)。
- `agent_gate/tests`(run_worker 改动覆盖域):**初跑 1 例失败**
  (`test_images.py::test_real_size_frames_reach_mcode`,任务序号失序)。取证:`test_images` 域外既有 flaky——任务 id 是 `sha256("t_seed:k")` 确定性哈希,失败时实际返回 ids[1] 打头,即取任务顺序竞态;同代码连跑时好时坏(7 次中 2 败),worktree 在改动前提交单独跑通过、当前代码单独跑也时败时过。根因:worker 内**同步** PG 事件写(每次 run 2-4 个连接往返)加进了执行路径,放大了竞态窗口。**修复**:run.* 事件改为单一后台线程 FIFO 落库 + `drain_run_events` 退出前排空(fix 提交);修复后该用例连跑 4/4 通过,`agent_gate/tests` 全域 **0 fail**(1 skip)。
- `openstoryline/tests`(agent_fastapi 挂载 + 前端包装)→ 3 失败,全部为已知 `config.toml.local` 泄漏基线(test_glm_preset / test_web_mcode_wiring / test_zcode_config,见 lastfailed 陈旧集合),与本改动无关;新增前端包装用例通过。
- Postgres 5432 全程存活(edb-postgres16)。

### 5.3 文档

- 根 `README.md` 新增 §10:页面入口与能力、事件来源(双入口)、五端点清单、排错(事件表不可用/CLI stderr 事件行/`workflow_run_events` 查询)。

## 6. 已知限制与后续建议

1. **真实主链路级验收未实跑**(AC-03/04/06 标注"测试图级"):assembly QC 回环、node_07 护栏自环、node_15/17 列表 fan-in、600s import_video 握手均为重资源或需人工网页操作的链路;其行为由最小测试图集成测试 + 前端投影单测覆盖,投影逻辑与链路无关。需要时按 §9.9 用 human/auto 各一次完整真实运行补验。
2. **agent_gate worker 运行级事件**:worker 事件已落库,但"真实 worker 进程事件序列"未做端到端核对(挂点全部 try/except + 后台线程兜底);后续跑一次真实 worker 即可从事件表核对 run.started/completed。另注:同步事件写会放大 `test_images.py` 既有取任务竞态(§5.2),已改后台落库;该测试本身仍偶发(域外 flaky,与本功能无关)。
3. **Web 进程不执行 Command(resume)**(跨 langgraph 版本风险,设计允许的查询形态);真实恢复仍走 agent_gate MCP `human_resume` / 人工脚本。
4. **事件存储降级即历史缺口**:降级期间的事件只有进程内订阅者可见,不落库、不可回放(store 本身语义如此,/events 显式报错)。
5. `POST /runs` 的重复启动防护依赖事件表已有记录;子进程导入期(~15s)窗口内连点不会被 409 挡(观测信号尚未落库)。
6. 验收期间通过 `POST /runs` 在生产库留下两个真实线程:`video-webacc-001`(挂起于关卡⓪,可直接 resume 或忽略)、`video-weblive-001`(同);`outputs/web_runs/` 下有对应 CLI 日志(不入库)。
7. 验收用临时 uvicorn 实例跑在 :7861,已停止;生产 :7860 全程未动,**需重启生产 Web 服务**才能加载新 router 与新页面(下次 start_production 受控重启即可)。

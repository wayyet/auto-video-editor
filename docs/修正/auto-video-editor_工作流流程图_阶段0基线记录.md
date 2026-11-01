# auto-video-editor 工作流流程图 — 阶段0 基线记录

> 对应设计：[《工作流流程图与实时节点追踪》§9.3 阶段0](../修改文档/auto-video-editor-工作流流程图与实时节点追踪.md)
> 记录日期：2026-11-01
> 性质：只读取证 + 临时探针，**不含任何生产代码改动**。回滚方式：删除本记录与探针脚本即可。

## 1. 环境基线

| 项 | 值（实测） | 备注 |
|---|---|---|
| HEAD SHA | `400332a9a17f7289f5f14ec378ab4dca2d28fb4d`（`400332a` docs(修改文档) 设计稿入库） | 设计稿撰写时为 `352a468`，其后新增 `63cb0b0`（fusion 转正默认开）/ `cb1b4fa` / `400332a` |
| 工作区 | `git status` clean | — |
| Python | **3.13.11**（venv：`.venv\Scripts\python.exe`；系统解释器 `C:\Program Files\Python313\python.exe` 同版本） | — |
| langgraph | **1.2.9**（`.venv\Lib\site-packages`） | 与设计稿预期一致 |
| langgraph-checkpoint | **4.1.1，editable** → `E:\Documents\kuaishou\langgraph-main\libs\checkpoint` | 与设计稿 §11.10 记录一致；升级会改变行为面，事件契约测试（阶段3）充当护栏 |
| Postgres | 容器 `edb-postgres16` Up (healthy)，`0.0.0.0:5432->5432` | restore 系测试依赖其存活 |
| 关键开关默认值 | `STORYLINE_MODE=human`；`STORYLINE_HUMAN_FUSION=1`（**默认开**，63cb0b0 转正）；`ASSEMBLY_QC_GATE_ENABLED=true` | 比设计稿"fusion 默认关"更新 |
| pytest | `pyproject.toml [tool.pytest.ini_options]`：`-q --strict-markers`，markers `admin/slow/postgres`，`asyncio_mode=auto` | 全量命令 `python -m pytest -m "not slow and not admin"` |

## 2. 全量测试基线失败集合

> Postgres 存活前提下运行 `python -m pytest -m "not slow and not admin"`。

命令：`.venv/Scripts/python.exe -m pytest -m "not slow and not admin" --tb=no -q`
（注意：pyproject `addopts` 已含 `-q`，命令里重复给 `-q` 会变成 `-qq`，**吞掉最终总结行**；后续阶段只写一次 `-q` 或干脆不写。）

结果（2026-11-01 实测）：

```
全量进度 19 行全部为 . / s(2) / x / X，无任何 F / E；
无 "short test summary info" 段；
.pytest_cache/v/cache/lastfailed 中无 tests/ 范围内条目。
⇒ 基线失败集合 = 空（0 failed）
```

对照与说明：

- **设计稿 §9.1 不变量 2 所述"本机固定 3 个基线失败（`config.toml.local` 的 `[zcode].cwd` 合并泄漏）已不复现"** —— 2026-11-01 实测为 0 失败。该 3 个失败是更早期（约 2026-10）的状态，后续提交已使其消失；阶段1/2 的对照基准即"**失败集合 = 空**"。
- `lastfailed` 里现存 7 条均为历史范围外陈旧条目：4 条 `openstoryline/tests/*`（不在 `testpaths=["tests"]` 内，属另一次定向运行的遗留）、3 条 `tests/integration/_diag_tmp.py`（已删除的临时诊断文件），与本次全量无关，不进基线。
- restore 系测试需要 Postgres 5432 存活；本次运行前已确认容器 healthy。
- 佐证计数：`.pytest_cache/v/cache/nodeids` 共 1949 条（含被 `-m` 剔除的 slow/admin/postgres 用例）。

## 3. 执行入口清单（与设计稿 §2.1 核对）

| 入口 | 位置 | 执行方式 | 接入阶段3的事件要求 |
|---|---|---|---|
| CLI | `scripts/run_workflow.py` | pre-flight(:102) → `apply_migrations_to_db`(:249) → **唯一路径** `ainvoke`(:259)，Postgres `AsyncPostgresSaver` | async runner 接管（`_invoke_async` 内替换） |
| agent_gate worker | `agent_gate/runner.py::run_worker` | **独立进程**，同步 `invoke`(:513 首跑/:515 崩溃续跑)；恢复走 `resume_pending_sync` → `Command(resume=resume_map)`(:400)；状态挂点 `w.set(STATE_WAITING_HUMAN/COMPLETED/FAILED)`(:519-527) | 运行级事件挂在 w.set 旁（底线）；节点级为可选增强 |
| worker 拉起方 | `agent_gate/cli.py:213`（进程内 run_worker）、`:307/:348`（spawn_worker）；`agent_gate/drive.py:77/:90`（spawn_worker）；`agent_gate/mcp_server.py:124/:202`（MCP 工具 spawn_worker） | 都是同一 `run_worker` | 覆盖 run_worker 即全覆盖 |
| smoke | `scripts/_smoke_auto_storyline.py:95` | `g.stream(state, config, stream_mode="updates")`（已有流式消费者，v1 单模式） | 测试脚本，不强制接事件 |
| drill | `scripts/drill_disconnect.py` | `build_graph`（断连演练） | 测试脚本，不强制接事件 |
| assembly e2e | `scripts/assembly/phase5_e2e.py` | 节点直调（注释明示不用 `build_graph().invoke()` 跑全图） | 不接 |
| Web 服务 | `openstoryline/agent_fastapi.py` | **不执行图**（无 invoke/ainvoke 调用）；只托管页面与 `/api/*` | 阶段4 的 SSE/API 宿主 |

结论与设计稿 §2.1 一致；补充确认：MCP `server.py`（FastMCP）不执行图、不托管静态页面。

## 4. 流事件契约样例（langgraph 1.2.9，`version="v2"`）

探针脚本：`scripts/probe_stream_events.py`（临时，**不入库**；运行后可删）。
运行：`.venv/Scripts/python.exe scripts/probe_stream_events.py > stream_events_samples.jsonl`（完整 49 行 JSONL 为进程临时产物，不在库内；本节摘录全部关键形态）。

测试图：`START→a`，`a` 两条静态出边到 `b1`/`b2`（同超步并行），`["b1","b2"]→gate`（列表 fan-in），`gate` 条件边二选一（`cp` 或 END），`cp` 内 `interrupt()`；另有一张单节点 `boom`（抛 ValueError）的错误专用图。checkpointer=InMemorySaver。

### 4.1 事件封套（最重要的契约事实）

```json
{"type": "tasks"|"updates"|"custom", "ns": [], "data": {...}}
```

- `stream_mode=["tasks","updates","custom"]` + `version="v2"` 下，每个 chunk 是**统一封套 dict**：`type` ∈ 流模式名；`ns` 为子图命名空间（主图恒 `[]`）；`data` 为负载。
- 不带任何时间戳 —— `occurred_at` 必须由适配器落事件时补。

### 4.2 普通节点起止（tasks 用同一 id 关联 start/end）

task start（含 `triggers`，可反推来源边）：

```json
{"type": "tasks", "ns": [], "data": {"id": "211573ef-…", "name": "a", "input": {"seen": [], "go_interrupt": false}, "triggers": ["branch:to:a"]}}
```

task end（`error: null` 表示正常；`result` 是节点返回的 state 增量；本例同时可见 custom 事件）：

```json
{"type": "custom",  "ns": [], "data": {"probe": "custom-from-a", "detail": {"x": 1}}}
{"type": "updates", "ns": [], "data": {"a": {"value": 1, "seen": ["a"]}}}
{"type": "tasks",   "ns": [], "data": {"id": "211573ef-…", "name": "a", "error": null, "result": {"value": 1, "seen": ["a"]}, "interrupts": []}}
```

### 4.3 并行超步（两个 task start 连续出现，再各自 updates/end）

```json
{"type": "tasks", "data": {"id": "18df8628-…", "name": "b1", "input": {…}, "triggers": ["branch:to:b1"]}}
{"type": "tasks", "data": {"id": "4575b579-…", "name": "b2", "input": {…}, "triggers": ["branch:to:b2"]}}
{"type": "updates", "data": {"b1": {"seen": ["b1"]}}}
{"type": "tasks",   "data": {"id": "18df8628-…", "name": "b1", "error": null, "result": {…}, "interrupts": []}}
{"type": "updates", "data": {"b2": {"seen": ["b2"]}}}
{"type": "tasks",   "data": {"id": "4575b579-…", "name": "b2", "error": null, "result": {…}, "interrupts": []}}
```

**fan-in 标记**：汇合点 task start 的 `triggers` 里出现 `join:b1+b2:gate`（barrier 渠道名），普通静态/条件边是 `branch:to:<node>` —— 这是运行期区分"列表 fan-in 汇合"与"普通触发"的免费信号（设计稿 §7.3 规则 8 的 `branch.selected` 推断可直接用 `triggers`）。

```json
{"type": "tasks", "data": {"id": "6692af99-…", "name": "gate", "input": {…}, "triggers": ["branch:to:gate", "join:b1+b2:gate"]}}
```

### 4.4 interrupt 挂起与 resume

挂起（`updates` 出现 `__interrupt__`；task end 的 `interrupts` 非空且 `result` 为空 `{}`；`get_state().next` 停在 `["cp"]`）：

```json
{"type": "updates", "data": {"__interrupt__": [{"id": "8120e1cd…", "value": {"checkpoint": "probe-cp", "instructions": "probe interrupt"}}]}}
{"type": "tasks",   "data": {"id": "7f20ad2d-…", "name": "cp", "error": null, "result": {}, "interrupts": [{"value": {"checkpoint": "probe-cp", …}, "id": "8120e1cd…"}]}}
```

resume（`Command(resume={"answer":"ok"})` 后：**同一 task id** 重新出现 task start（`triggers` 不变），随后正常 updates/end —— "resumed" 判据 = 同一 task id 二次 start（此前 end 带 interrupts）：

```json
{"type": "tasks",   "data": {"id": "7f20ad2d-…", "name": "cp", "input": {…}, "triggers": ["branch:to:cp"]}}
{"type": "updates", "data": {"cp": {"value": 102, "seen": ["cp:{'answer': 'ok'}"]}}}
{"type": "tasks",   "data": {"id": "7f20ad2d-…", "name": "cp", "error": null, "result": {…}, "interrupts": []}}
```

### 4.5 节点异常

task end 的 `error` 字段携带异常对象；随后异常**同样从 astream 冒出**（第 3 行为捕获到的 raise）——runner 必须 try/except 兜底，且 task-end `error` 与外层异常是同一来源，去重处理避免双发 `node.failed`：

```json
{"type": "tasks", "data": {"id": "83a61087-…", "name": "boom", "error": {"ValueError": "probe-error-xyz 故意失败"}, "result": {}, "interrupts": []}}
{"__raised__": "ValueError", "message": "probe-error-xyz 故意失败"}
```

### 4.6 契约要点清单（直接约束阶段3的适配器实现）

1. start/end 靠相同 `data.id` 关联；`data.name` 是节点名。
2. `data.error` 非空 + 外层异常同时存在 → 只发一次 `node.failed`。
3. `data.interrupts` 非空 → `node.waiting_input` + `run.interrupted`，且**不发** `node.succeeded`（`result` 为空）。
4. resume 复跑同一 task id → 以"同一 task id 二次 started 且曾带 interrupts"发 `node.resumed`。
5. `triggers` 含 `join:…` → fan-in 汇合触发；`branch:to:<node>` → 普通触发。可用作 `branch.selected(inferred=true)`。
6. `custom` 流 `data` 原样透传节点 `get_stream_writer()` 写入的对象（import_video 等待事件走这里）。
7. `updates` 的 `data` 键是节点名、值是该节点返回的 state 增量 —— 白名单摘要从这里取；`__interrupt__` 是特殊键。

## 5. 页面访问入口与 /api 前缀（阶段4 落点确认）

- 页面：`http://127.0.0.1:7860/node-map`（`agent_fastapi.py:3670-3678`）；静态目录挂载 `/node_map`（`:3476`）。
- API 前缀：`api = APIRouter(prefix="/api")`（`:3478`），`app.include_router(api)`（`:4567`）；现有 `/api/system/import-video` 等都挂在它上面。阶段4 新 router 的挂载方式：新增独立 `APIRouter` 模块并在 `:4567` 附近 `app.include_router(...)`，保持与 `api` 同层。
- uvicorn 绑定 `127.0.0.1:7860`（`start_production.ps1:678`），本机限定。

## 6. `get_graph()` 形态（阶段1 拓扑服务的直接输入）

默认构建（human + fusion on + QC on）实测：

- 节点 **55**（53 业务节点 + `__start__` + `__end__`）；边 **81**；条件边 **13**。
- 边对象为 `langgraph.graph.graph.Edge`（NamedTuple）：`source` / `target` / `data`(分支键，多数 None，仅当分支键≠目标名时非空，如 `node_07_speed_fit→bridge_snapshot2` 的 `data="node_08_add_subtitles"`) / `conditional`(bool)。
- 条件边清单（13）：`launch_openstoryline→{open_preview,__end__}`、`open_preview→{checkpoint0_storyline_plan, storyline_load_media}`、`get_storyboard_plan→{generate_draft,__end__}`、`assembly_validate_render_qc→{assembly_repair_loop, assembly_write_report}`、`node_07_speed_fit→{bridge_snapshot2, node_07_speed_fit(自环), escalate_guardrail_failure}`、`node_10b_inject_text_fx_en→{node_checkpoint3_layout_review, node_17_inject_english_tts}`。
- fusion 默认开 ⇒ 21 条侧支边在图中（如 `assembly_asr_and_visual_observe→storyline_speech_rough_cut`），**auto 链内部边不在**（`_build_state_graph` 在 fusion 分支提前 return，graph.py:871-873）；`storyline_qa_gate` 的条件边也不在（qa_gate 无出边，仅作侧支）。
- `clean_cache` 注册但 0 边（如实保留）。

## 7. 阶段0 交付物核对

- [x] 基线记录（本文档：环境、失败集合、入口清单、事件样例、get_graph 形态、URL/前缀）
- [x] 执行入口清单（§3）
- [x] LangGraph 事件样例（§4，完整 JSONL 由探针生成）
- [x] node_map 访问 URL 与 /api 前缀（§5）
- 探针脚本 `scripts/probe_stream_events.py` 按计划"可不入库"：本笔提交**不含**它（提交后保留在工作区，供阶段3 编写契约测试时参考，阶段3 收尾时删除）。

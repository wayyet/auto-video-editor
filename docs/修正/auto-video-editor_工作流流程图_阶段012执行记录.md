# auto-video-editor 工作流流程图 — 阶段0/1/2 执行记录

> 对应设计:[《工作流流程图与实时节点追踪》§9](../修改文档/auto-video-editor-工作流流程图与实时节点追踪.md)
> 执行日期:2026-11-01
> 范围:**仅阶段0(基线固定与流事件契约验证)、阶段1(拓扑 Schema 服务与元数据注册表)、阶段2(事件协议、脱敏与事件存储)** —— 阶段3–6 未执行(用户指定)。
> 基线记录:[阶段0基线记录](./auto-video-editor_工作流流程图_阶段0基线记录.md)(提交 `221cc12`)

## 提交清单

| 阶段 | 提交 | 内容 |
|---|---|---|
| 0 | `221cc12` docs(修正) | 基线记录(环境/零失败基线/入口清单/事件契约样例/get_graph 形态/URL 前缀) |
| 1 | `3c51388` feat(monitoring) | `monitoring/workflow_graph.py` + `tests/integration/test_workflow_graph_schema.py`(17 用例) |
| 2 | 见 git log | `monitoring/workflow_events.py` + `monitoring/workflow_event_store.py` + `db/migrations.py`(004)+ `tests/unit/monitoring/test_workflow_events.py`(32 用例) |

探针脚本 `scripts/probe_stream_events.py` 按设计"可不入库"保留在工作区未提交,供阶段3 编写契约测试参考;阶段3 收尾时删除。

## 阶段0:基线与契约(结论速览)

详细数据见阶段0基线记录,关键结论:

1. **环境**:HEAD `400332a`;Python 3.13.11(venv);langgraph 1.2.9;langgraph-checkpoint 4.1.1 **editable → `E:\Documents\kuaishou\langgraph-main\libs\checkpoint`**(与设计稿一致);Postgres `edb-postgres16` healthy。
2. **基线失败集合 = 空(0 failed)** —— 设计稿 §9.1 所述"本机固定 3 个失败(config.toml.local 泄漏)"**已不复现**,后续阶段对照基准为"失败集合 = 空"。注意:`pyproject` 的 `addopts` 已含 `-q`,命令行再写 `-q` 会变 `-qq` 吞掉总结行。
3. **执行入口清单**与设计稿 §2.1 一致,另确认:`agent_gate` 三个拉起方(cli/drive/mcp_server)都收敛到同一 `run_worker`;smoke/drill/e2e 是测试脚本不强制接事件;Web 宿主 `agent_fastapi.py` 不执行图。
4. **流事件契约(langgraph 1.2.9,`version="v2"`)固化**为四类样例(普通起止/并行超步/interrupt 挂起与 resume/节点异常),完整形态见基线记录 §4。要点:统一封套 `{type, ns, data}`;task start/end 同 `id` 关联;`triggers` 含 `branch:to:x` 与 **`join:a+b:x`**(fan-in 免费信号);interrupt 在 `tasks.end.interrupts` + `updates.__interrupt__` 双处可见,且 resume 复跑**同一 task id**;节点异常同时出现在 `tasks.end.error` 与 astream 外层 raise(需去重)。
5. **页面落点确认**:`http://127.0.0.1:7860/node-map`;`api = APIRouter(prefix="/api")`(:3478)+ `app.include_router(api)`(:4567)是阶段4 的挂载方式。

## 阶段1:拓扑 Schema 服务(纯新增)

### 交付

- `monitoring/workflow_graph.py`:`build_graph_schema(compiled_graph, config=None) -> dict`,§4.3 结构 + `modes`/`waiting_kind` 两个扩展字段;53 节点中文元数据注册表(名称/分组/描述/关卡⓪①②③)+ 条件边标签注册表;`flags` 实时读 `ASSEMBLY_QC_GATE_ENABLED` / `STORYLINE_HUMAN_FUSION`;`mode` 与构图同源(优先 `_STORYLINE_MODE_OVERRIDE` contextvar)。
- `tests/integration/test_workflow_graph_schema.py`:17 用例,覆盖 (a) 与编译图逐点一致、(b) QC/fusion 两态(恰好多 21 条侧支边)、(c) 元数据完备,外加 auto 模式构建与 langgraph 画图偏差护栏。

### ⚠️ 结构性发现与设计偏差:边真源从 `get_graph()` 改为 `compiled_graph.builder`

实测(langgraph 1.2.9,`langgraph/pregel/_draw.py`)发现 **`compiled_graph.get_graph()` 不是真实拓扑**,而是静态模拟 + 画图启发式:

1. 它给所有"只入不出"的 termini 节点**合成** `→__end__` 边 —— 默认构建(human + fusion on)下 21 个 storyline 侧支/死胡同节点各多出一条伪边;
2. 它会**漏掉**"有出边节点"的普通直连 `→__end__` 边 —— fusion 开启时 `join_before_delivery→END`(真实 `add_edge`)在画图里不存在,因为融合侧支使该节点不再是 terminus;
3. 数字:真实 61 条边(46 plain + 2 fan-in + 13 条件端点)在画图里变成 81 条。

处置(设计 §9.10.5"中途发现结构性缺陷回到对应阶段修"):

- `build_graph_schema` 的边真源改为 `compiled_graph.builder` 的三件套:`edges`(plain,含 `__start__`/`__end__`)+ `waiting_edges`(列表 fan-in 逐源展开)+ `branches[*][*].ends`(条件映射端点)。这仍是**编译产物**,不引入第二份手工定义,不违背设计 §11.6;
- `get_graph()` 的偏差规则(termini 合成集 − 被漏真边集)固化在护栏测试 `test_schema_matches_drawn_graph_with_documented_artifacts` —— langgraph 升级改变画图行为时该测试先红,兼作升级护栏(呼应设计 §11.10);
- 设计稿 §6.2-A"用 `compiled_graph.get_graph()` 取节点和边"一句按本记录理解执行,设计稿文本未改动。

### 回归

- `tests/integration/test_workflow_graph_schema.py`:17 passed。
- 全量 `python -m pytest -m "not slow and not admin"`:**1352 passed, 2 skipped, 5 deselected, 3 xfailed, 4 xpassed,0 failed = 基线**(348s)。

## 阶段2:事件协议、脱敏与事件存储(纯新增)

### 交付

- `monitoring/workflow_events.py`:§7.1 全部 13 类事件常量;`make_event`(字段校验:node.* 必带 node_id、run.* 禁带、attempt ≥ 1;UTC ISO+Z 时间戳)+ `validate_event`;**拒绝式白名单** `ALLOWED_PAYLOAD_KEYS`(白名单外顶层键直接 `ValueError`,呼应 §9.1 不变量 4)+ 嵌套敏感键丢弃(`SENSITIVE_KEY_NAMES`);`sanitize_text`(密钥 KV / `sk-` / Bearer / DSN 密码打码,**绝对路径降级为文件名**(§11.7 + §4.2 允许文件名),超长截断挡 ASR 全文/提示词)+ `sanitize_value` + `sanitize_exception`;`to_sse_frame`(payload 平铺进 data,对齐 §7.2 示例;`id` 行 = 事件表 BIGSERIAL)。
- `monitoring/workflow_event_store.py`:`append_event`(`RETURNING id`,先落库后推送的落库侧)/ `fetch_events_after(thread_id, last_id, limit)`(`WHERE thread_id=$1 AND id > $2 ORDER BY id ASC` 游标补发)/ `latest_snapshot`(available/event_count/last_event/node_attempts);URI 走 `db.postgres_config.resolve_postgres_target()` 统一解析;连接复用 `db.connection`(UTF-8 client_encoding);JSONB 以 `%s::jsonb` + `ensure_ascii=False` 自包含写入,不依赖进程级 `set_json_dumps`;降级语义沿用 runs_table 风格(ERROR + `degraded_count()`,返回 `None`/`[]`/`available=False` —— 上层 /events 必须显式报错,不得静默)。
- `db/migrations.py`:**迁移 004 `create_workflow_run_events`**,DDL 按设计 §6.2-D,外加一处强化:`attempt INT CHECK (attempt >= 1)`(设计稿 DDL 无此 CHECK,为防御性补充,其余逐字一致);索引 `idx_wre_thread(thread_id, id)` 与 `idx_wre_run(run_id)`。
- `tests/unit/monitoring/test_workflow_events.py`:32 用例(事件常量分区、字段校验、attempt 递增、异常夹路径/密钥脱敏、ASR 截断、白名单拒绝、SSE 帧两态、fake 连接的成功/降级路径、坏事件不触库)。

### 验收

- `python -m pytest tests/unit/monitoring/test_workflow_events.py tests/unit/test_db_migrations.py`:44 passed。
- 真实 PG(5432 healthy):`apply_migrations_to_db()` 首次应用 `[4]`,重复执行 `[]`(幂等)✓;端到端回环 `append_event → fetch_events_after → latest_snapshot` 正常,中文 payload 经 JSONB 回读无 `\uXXXX` 转义问题 ✓(验收数据已清理)。
- 全量回归结果:**1384 passed, 2 skipped, 5 deselected, 3 xfailed, 4 xpassed, 0 failed = 基线**(413s;通过数 1352 → 1384,恰好多出的 32 条即本阶段新增单测)。

## 贯穿不变量核对(§9.1)

1. 现有工作流行为零变化 ✓ —— 阶段1/2 全部为纯新增文件 + `db/migrations.py` 追加一条迁移,未触碰 `graph.py`/`run_workflow.py`/`agent_gate/runner.py`/任何节点函数;checkpoint/resume 语义不变。
2. 全量失败集合 = 基线(空)✓ —— 阶段1 实测 1352 passed 0 failed;阶段2 见上。
3. 事件只读 ✓ —— 未新增任何端点;`build_graph_schema` 为纯函数,不触发执行。
4. 白名单与脱敏单测先于 runner 接线 ✓ —— 阶段2 完成并全绿,阶段3(`runner`)尚未实施。
5. `git add` 逐文件 ✓ —— 每笔提交均按文件清单添加,未用 `git add -A`。

## 已知限制与后续阶段注意

- `get_graph()` 伪边规则是从 langgraph 1.2.9 的 `_draw.py` 实现反推的;`test_schema_matches_drawn_graph_with_documented_artifacts` 在 langgraph 升级时可能先红 —— 届时按新行为更新该护栏,而 builder 真源提取(`_real_topology`)大概率无需变化。
- `fetch_events_after` 降级返回 `[]` 与"没有更多事件"在返回值上不可区分 —— 阶段4 的 /events 端点必须结合 `latest_snapshot().available` 区分(设计 §6.2-E 要点 5)。
- `to_sse_frame` 的 `id` 行依赖事件已入库(有 `id`);阶段3 runner 必须先 `append_event` 拿 id 再组帧。
- 阶段3 接线时的协议要点(来自阶段0契约):task end `interrupts` 非空 → `node.waiting_input`+`run.interrupted` 且不发 `node.succeeded`;resume = 同 task id 二次 start;异常在 `tasks.end.error` 与外层 raise 双现,去重发一次 `node.failed`;`triggers` 的 `join:a+b:x` 可直接做 `branch.selected(inferred=true)` 依据。

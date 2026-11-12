// /web/tests/workflow_monitor.test.mjs
//
// workflow_monitor.mjs(前端纯逻辑层)的 Node 单测:事件归约状态机、
// Last-Event-ID 补发去重、并行活动节点、重试 attempt 历史、布局 key 隔离、
// 已走边投影、主图数据归一。不碰 DOM、不发 fetch —— 与 media_upload.test.mjs
// 同一惯例(pytest 包装见 openstoryline/tests/test_workflow_monitor_frontend.py)。
import test from "node:test";
import assert from "node:assert/strict";

import {
  API_PATHS,
  NODE_STATUS,
  RUN_STATE,
  activeNodeIds,
  applyEvent,
  computeCounts,
  computeNodeOrder,
  createMonitorState,
  deriveRunState,
  edgeStates,
  finalizeSkipped,
  legacyLayoutKey,
  mainLayoutKey,
  normalizeMainGraph,
} from "../static/workflow_monitor.mjs";
import { getLayoutCacheKey } from "../static/workflow_layout.mjs";

function ev(type, { id = null, node_id = null, attempt = 1, payload = {}, occurred_at = "2026-11-01T07:20:30.000Z", run_id = null } = {}) {
  return { id, event_type: type, node_id, attempt, payload, occurred_at, run_id };
}

// ---------------------------------------------------------------------------
// 1. 节点事件归约(§4.1 七态状态机)
// ---------------------------------------------------------------------------
test("started/succeeded/failed/waiting 状态机与耗时历史", () => {
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "a" }));
  assert.equal(s.nodes.a.status, NODE_STATUS.RUNNING);
  assert.equal(s.nodes.a.attempt, 1);

  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "a", payload: { duration_ms: 1250 } }));
  assert.equal(s.nodes.a.status, NODE_STATUS.DONE);
  assert.equal(s.nodes.a.durationMs, 1250);
  assert.deepEqual(s.nodes.a.durations, [1250]);

  applyEvent(s, ev("node.started", { id: 3, node_id: "b" }));
  applyEvent(s, ev("node.failed", { id: 4, node_id: "b", payload: { error: { type: "ValueError", message: "x" } } }));
  assert.equal(s.nodes.b.status, NODE_STATUS.FAILED);
  assert.equal(s.nodes.b.lastError.type, "ValueError");

  applyEvent(s, ev("node.started", { id: 5, node_id: "c" }));
  applyEvent(s, ev("node.waiting_input", { id: 6, node_id: "c", payload: { waiting_kind: "interrupt", detail: { checkpoints: ["①"] } } }));
  assert.equal(s.nodes.c.status, NODE_STATUS.WAITING);
  assert.equal(s.nodes.c.waitingKind, "interrupt");
  // interrupt 挂起不是失败,也不是完成
  assert.notEqual(s.nodes.c.status, NODE_STATUS.FAILED);

  // web 文件握手是另一种等待(§11.3)
  applyEvent(s, ev("node.waiting_input", { id: 7, node_id: "d", payload: { waiting_kind: "web_file_handshake" } }));
  assert.equal(s.nodes.d.waitingKind, "web_file_handshake");
});

test("retrying 保留历史不覆盖(§11.1)", () => {
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "r", attempt: 1 }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "r", payload: { duration_ms: 100 } }));
  applyEvent(s, ev("node.retrying", { id: 3, node_id: "r", attempt: 2 }));
  assert.equal(s.nodes.r.status, NODE_STATUS.RETRYING);
  assert.equal(s.nodes.r.attempt, 2);
  applyEvent(s, ev("node.started", { id: 4, node_id: "r", attempt: 2 }));
  applyEvent(s, ev("node.succeeded", { id: 5, node_id: "r", payload: { duration_ms: 80 } }));
  assert.deepEqual(s.nodes.r.durations, [100, 80]);
  assert.equal(s.nodes.r.status, NODE_STATUS.DONE);
});

test("resumed 从等待转回执行中", () => {
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "cp" }));
  applyEvent(s, ev("node.waiting_input", { id: 2, node_id: "cp", payload: { waiting_kind: "interrupt" } }));
  applyEvent(s, ev("node.resumed", { id: 3, node_id: "cp", attempt: 1 }));
  assert.equal(s.nodes.cp.status, NODE_STATUS.RUNNING);
  assert.equal(s.nodes.cp.resumed, true);
  assert.equal(s.nodes.cp.waitingKind, null);
});

// ---------------------------------------------------------------------------
// 2. 并行:多节点同时 running(§11.2)
// ---------------------------------------------------------------------------
test("并行超步:两个节点同时活动", () => {
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "b1" }));
  applyEvent(s, ev("node.started", { id: 2, node_id: "b2" }));
  assert.deepEqual(activeNodeIds(s).sort(), ["b1", "b2"]);
  const counts = computeCounts(s);
  assert.equal(counts.running, 2);
  applyEvent(s, ev("node.succeeded", { id: 3, node_id: "b1", payload: {} }));
  assert.deepEqual(activeNodeIds(s), ["b2"]);
});

// ---------------------------------------------------------------------------
// 3. Last-Event-ID 补发去重 + 游标
// ---------------------------------------------------------------------------
test("重放去重:id <= cursor 不重复应用;null id 照常应用不推进游标", () => {
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "a" }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "a", payload: { duration_ms: 5 } }));
  const r = applyEvent(s, ev("node.started", { id: 2, node_id: "a" })); // 重连补发重叠
  assert.equal(r.applied, false);
  assert.equal(s.nodes.a.status, NODE_STATUS.DONE, "重复事件不得把 done 打回 running");
  assert.equal(s.cursor, 2);

  const r2 = applyEvent(s, ev("node.started", { id: null, node_id: "degraded" }));
  assert.equal(r2.applied, true, "存储降级(无 id)事件照常应用");
  assert.equal(s.cursor, 2, "无 id 不推进游标");
});

// ---------------------------------------------------------------------------
// 4. run.* 与 branch.selected
// ---------------------------------------------------------------------------
test("run 状态机 + branch.selected 记录", () => {
  const s = createMonitorState();
  applyEvent(s, ev("run.started", { id: 1, run_id: "run-x" }));
  assert.equal(s.run.state, RUN_STATE.RUNNING);
  assert.equal(s.runId, "run-x");
  applyEvent(s, ev("branch.selected", { id: 2, node_id: "gate", payload: { label: "join:b1+b2:gate", inferred: true, detail: { from: ["b1", "b2"], to: "gate" } } }));
  assert.equal(s.branches.length, 1);
  assert.deepEqual(s.branches[0].from, ["b1", "b2"]);
  applyEvent(s, ev("run.interrupted", { id: 3 }));
  assert.equal(s.run.state, RUN_STATE.WAITING_HUMAN);
  applyEvent(s, ev("run.resumed", { id: 4 }));
  assert.equal(s.run.state, RUN_STATE.RUNNING);
  applyEvent(s, ev("run.completed", { id: 5 }));
  assert.equal(s.run.state, RUN_STATE.COMPLETED);

  const s2 = createMonitorState();
  applyEvent(s2, ev("run.failed", { id: 1 }));
  assert.equal(s2.run.state, RUN_STATE.FAILED);
});

// ---------------------------------------------------------------------------
// 5. finalizeSkipped:只在终态后给"未执行"节点
// ---------------------------------------------------------------------------
test("finalizeSkipped 仅终态生效,且不动禁用/不可达节点", () => {
  const schemaNodes = [
    { id: "a", kind: "task", enabled: true, reachable: true },
    { id: "off", kind: "task", enabled: false, reachable: false }, // QC 关闭
    { id: "term", kind: "terminal", enabled: true, reachable: true },
  ];
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "a" }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "a", payload: {} }));
  assert.equal(finalizeSkipped(s, schemaNodes), 0, "非终态不标记");
  applyEvent(s, ev("run.completed", { id: 3 }));
  const marked = finalizeSkipped(s, schemaNodes);
  assert.equal(marked, 0, "a 已执行、off 禁用、term 是 terminal —— 没有可标的");
  assert.equal(s.nodes.off, undefined, "禁用节点不标 skipped");
  assert.equal(s.nodes.term, undefined, "terminal 不标 skipped");
});

test("finalizeSkipped 终态后标记未执行节点", () => {
  const schemaNodes = [
    { id: "a", kind: "task", enabled: true, reachable: true },
    { id: "z", kind: "task", enabled: true, reachable: true },
  ];
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "a" }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "a", payload: {} }));
  applyEvent(s, ev("run.completed", { id: 3 }));
  assert.equal(finalizeSkipped(s, schemaNodes), 1);
  assert.equal(s.nodes.z.status, NODE_STATUS.SKIPPED);
  assert.equal(s.nodes.a.status, NODE_STATUS.DONE, "已执行节点不受影响");
});

// ---------------------------------------------------------------------------
// 6. 已走边投影
// ---------------------------------------------------------------------------
test("edgeStates:双端执行过才点亮;END 边单侧判定;running 目标边 active", () => {
  const edges = [
    { id: "START->a", from: "START", to: "a" },
    { id: "a->b1", from: "a", to: "b1" },
    { id: "a->b2", from: "a", to: "b2" },
    { id: "b1->END", from: "b1", to: "END" },
  ];
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "a" }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "a", payload: {} }));
  applyEvent(s, ev("node.started", { id: 3, node_id: "b1" }));

  const { traversed, active } = edgeStates(edges, s);
  assert.ok(traversed.has("START->a"), "START 边单侧判定");
  assert.ok(traversed.has("a->b1"));
  assert.ok(!traversed.has("a->b2"), "未命中的并行分支不点亮(不预判跳过)");
  assert.ok(active.has("a->b1"), "目标节点 running → active");
  assert.equal(traversed.has("b1->END"), false, "END 边在对侧未完成前不点亮(仅 started)");
});

test("edgeStates:END 门边未命中不点亮;单出边源完成后点亮", () => {
  // gate 有两条出边:gate->other(走了)与 gate->END(条件门未命中)
  const edges = [
    { id: "gate->other", from: "gate", to: "other" },
    { id: "gate->END", from: "gate", to: "END" },
    { id: "last->END", from: "last", to: "END" },
  ];
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "gate" }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "gate", payload: {} }));
  applyEvent(s, ev("node.started", { id: 3, node_id: "other" }));
  applyEvent(s, ev("node.started", { id: 4, node_id: "last" }));
  applyEvent(s, ev("node.succeeded", { id: 5, node_id: "last", payload: {} }));
  // 运行未完成:END 边一律不点亮(即使源已 done)
  let { traversed } = edgeStates(edges, s);
  assert.equal(traversed.has("gate->END"), false, "运行未完成不点亮 END 门边");
  assert.equal(traversed.has("last->END"), false, "运行未完成不点亮 END 边");

  applyEvent(s, ev("run.completed", { id: 6 }));
  ({ traversed } = edgeStates(edges, s));
  assert.equal(traversed.has("gate->END"), false, "gate 的其它出边已走 → END 门边未命中,不点亮");
  assert.ok(traversed.has("gate->other"), "实际走出的边点亮");
  assert.ok(traversed.has("last->END"), "单出边源完成 + 运行完成 → END 边点亮");
});

test("edgeStates:挂起(等待人工)时 END 边不点亮", () => {
  const edges = [{ id: "cp->END", from: "cp", to: "END" }];
  const s = createMonitorState();
  applyEvent(s, ev("node.started", { id: 1, node_id: "cp" }));
  applyEvent(s, ev("node.succeeded", { id: 2, node_id: "cp", payload: {} }));
  applyEvent(s, ev("run.interrupted", { id: 3 }));
  const { traversed } = edgeStates(edges, s);
  assert.equal(traversed.has("cp->END"), false, "挂起 ≠ 走到终点");
});

// ---------------------------------------------------------------------------
// 7. 布局 key 隔离(§6.2-F 第 7 条)
// ---------------------------------------------------------------------------
test("布局 key:主图带 mode+schema_version;旧子流程 key 不变", () => {
  assert.equal(mainLayoutKey("auto_video_editor_main", "human", 1), "wf_layout_auto_video_editor_main_human_v1");
  assert.equal(mainLayoutKey("auto_video_editor_main", "auto", 1), "wf_layout_auto_video_editor_main_auto_v1");
  assert.notEqual(mainLayoutKey("wf", "human", 1), mainLayoutKey("wf", "auto", 1));
  assert.notEqual(mainLayoutKey("wf", "human", 1), mainLayoutKey("wf", "human", 2));
  assert.equal(legacyLayoutKey("video_editing_pipeline"), "node_layout_video_editing_pipeline");
});

// 第5期阶段1:TB 带算法版本后缀,LR/缺省保持历史格式(零迁移);双函数同规则。
test("布局 key:TB/LR 方向隔离,LR 缓存不被 TB 覆盖", () => {
  assert.equal(
    mainLayoutKey("auto_video_editor_main", "auto", 6, "tb"),
    "wf_layout_auto_video_editor_main_auto_v6_tb_v1"
  );
  assert.equal(mainLayoutKey("auto_video_editor_main", "auto", 6, "lr"), "wf_layout_auto_video_editor_main_auto_v6");
  assert.equal(mainLayoutKey("auto_video_editor_main", "auto", 6), "wf_layout_auto_video_editor_main_auto_v6");
  assert.notEqual(
    mainLayoutKey("auto_video_editor_main", "auto", 6, "tb"),
    mainLayoutKey("auto_video_editor_main", "auto", 6, "lr")
  );
  // 与 workflow_layout.mjs::getLayoutCacheKey 同规则(单一来源常量)
  assert.equal(
    mainLayoutKey("auto_video_editor_main", "auto", 6, "tb"),
    getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "tb" })
  );
  assert.equal(
    mainLayoutKey("auto_video_editor_main", "auto", 6, "lr"),
    getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "lr" })
  );
});

// ---------------------------------------------------------------------------
// 8. deriveRunState / API 路径
// ---------------------------------------------------------------------------
test("deriveRunState 与后端快照同规则", () => {
  assert.equal(deriveRunState("run.completed"), "completed");
  assert.equal(deriveRunState("run.interrupted"), "waiting_human");
  assert.equal(deriveRunState("node.started"), "running");
  assert.equal(deriveRunState("node.succeeded"), "idle");
  assert.equal(deriveRunState(undefined), "no_events");
  assert.equal(API_PATHS.runEvents("video 001"), "/api/workflow/runs/video%20001/events");
});

// ---------------------------------------------------------------------------
// 9. normalizeMainGraph:edges → dependencies/next_nodes
// ---------------------------------------------------------------------------
test("normalizeMainGraph 推导依赖与后继,保留元数据", () => {
  const schema = {
    workflow_id: "auto_video_editor_main",
    schema_version: 1,
    mode: "human",
    flags: { ASSEMBLY_QC_GATE_ENABLED: true },
    nodes: [
      { id: "START", name: "开始", kind: "terminal" },
      { id: "a", name: "A", kind: "task" },
      { id: "b", name: "B", kind: "task" },
      { id: "END", name: "结束", kind: "terminal" },
    ],
    edges: [
      { id: "START->a", from: "START", to: "a", kind: "normal" },
      { id: "a->b", from: "a", to: "b", kind: "normal" },
      { id: "b->END", from: "b", to: "END", kind: "normal" },
    ],
  };
  const g = normalizeMainGraph(schema);
  const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
  assert.deepEqual(byId.a.dependencies, ["START"]);
  assert.deepEqual(byId.a.next_nodes, ["b"]);
  assert.deepEqual(byId.b.dependencies, ["a"]);
  assert.equal(g.workflow_meta.id, "auto_video_editor_main");
  assert.equal(g.edges.length, 3);
});

// ---------------------------------------------------------------------------
// 10. computeNodeOrder:节点状态面板的执行顺序序号(与节点图视图同规则)
// ---------------------------------------------------------------------------
// 注意:涉及"同深度内排序"的用例用 ASCII 名(Alpha/Beta/Gamma)——
// localeCompare 对 CJK 的排序依赖运行时 ICU,不在单测里钉死;
// 中文排序行为与 node_map.html 在同一浏览器运行时下天然一致。
test("computeNodeOrder 线性链:只编 task 节点,1 起,START/END 不入映射", () => {
  const schema = {
    workflow_id: "auto_video_editor_main",
    schema_version: 2,
    mode: "auto",
    nodes: [
      { id: "START", name: "开始", kind: "terminal" },
      { id: "a", name: "载入媒体", kind: "task" },
      { id: "b", name: "分镜切分", kind: "task" },
      { id: "END", name: "结束", kind: "terminal" },
    ],
    edges: [
      { id: "START->a", from: "START", to: "a", kind: "normal" },
      { id: "a->b", from: "a", to: "b", kind: "normal" },
      { id: "b->END", from: "b", to: "END", kind: "normal" },
    ],
  };
  const order = computeNodeOrder(schema);
  assert.equal(order.size, 2);
  assert.equal(order.get("a"), 1);
  assert.equal(order.get("b"), 2);
  assert.equal(order.has("START"), false, "terminal 不编号");
  assert.equal(order.has("END"), false, "terminal 不编号");
});

test("computeNodeOrder 并行 fan-out:深度取最长链,同深度按中文名 localeCompare 排序", () => {
  // id 故意与名字序交错(p1=Beta/p2=Gamma/p3=Alpha):若实现误按 id 排序会立刻暴露
  const schema = {
    workflow_id: "auto_video_editor_main",
    schema_version: 2,
    mode: "auto",
    nodes: [
      { id: "START", name: "开始", kind: "terminal" },
      { id: "a", name: "生成文案", kind: "task" },
      { id: "p1", name: "Beta", kind: "task" },
      { id: "p2", name: "Gamma", kind: "task" },
      { id: "p3", name: "Alpha", kind: "task" },
      { id: "z", name: "时间线规划", kind: "task" },
      { id: "END", name: "结束", kind: "terminal" },
    ],
    edges: [
      { id: "START->a", from: "START", to: "a", kind: "normal" },
      { id: "a->p1", from: "a", to: "p1", kind: "normal" },
      { id: "a->p2", from: "a", to: "p2", kind: "normal" },
      { id: "a->p3", from: "a", to: "p3", kind: "normal" },
      { id: "p1->z", from: "p1", to: "z", kind: "normal" },
      { id: "p2->z", from: "p2", to: "z", kind: "normal" },
      { id: "p3->z", from: "p3", to: "z", kind: "normal" },
      { id: "z->END", from: "z", to: "END", kind: "normal" },
    ],
  };
  const order = computeNodeOrder(schema);
  assert.equal(order.get("a"), 1);
  assert.equal(order.get("p3"), 2); // Alpha(同深度按名序)
  assert.equal(order.get("p1"), 3); // Beta
  assert.equal(order.get("p2"), 4); // Gamma
  assert.equal(order.get("z"), 5);  // 汇聚节点深度 = 并行列深度 + 1
});

test("computeNodeOrder 回环/自环不炸:回边按 0 贡献跳过,不发散(与 computeDepths 同构)", () => {
  // QA 重试回环(q->g)+ node_07 式自环(r->r):迭代松弛会不收敛,DFS 必须跳过。
  // 本用例从环内 g 进入(q 尚在访问中,回边贡献 0):g 经 q 绕回计为深度 3,
  // s/q 沿 DAG 链取真实深度 —— 与 node_map.html::computeDepths 同算法同序输入,
  // 结果必然一致;深度并列的 g/r 用 ASCII 名钉死列内名序。
  const schema = {
    workflow_id: "auto_video_editor_main",
    schema_version: 2,
    mode: "auto",
    nodes: [
      { id: "START", name: "开始", kind: "terminal" },
      { id: "g", name: "Group", kind: "task" },
      { id: "s", name: "Script", kind: "task" },
      { id: "q", name: "QA", kind: "task" },
      { id: "r", name: "Render", kind: "task" },
      { id: "END", name: "结束", kind: "terminal" },
    ],
    edges: [
      { id: "START->g", from: "START", to: "g", kind: "normal" },
      { id: "g->s", from: "g", to: "s", kind: "normal" },
      { id: "s->q", from: "s", to: "q", kind: "normal" },
      { id: "q->g", from: "q", to: "g", kind: "conditional" }, // QA 未过,重试:回边
      { id: "q->r", from: "q", to: "r", kind: "conditional" },
      { id: "r->r", from: "r", to: "r", kind: "normal" },      // 自环
      { id: "r->END", from: "r", to: "END", kind: "normal" },
    ],
  };
  const order = computeNodeOrder(schema);
  assert.equal(order.get("s"), 1); // 深度 1(s 不受回边影响)
  assert.equal(order.get("q"), 2); // 深度 2
  assert.deepEqual([order.get("g"), order.get("r")], [3, 4], "深度并列 3,按名序 Group<Render;回边/自环不得把深度顶上天");
});

test("computeNodeOrder 异常入参:返回空 Map(面板回退字母序)", () => {
  assert.equal(computeNodeOrder(null).size, 0);
  assert.equal(computeNodeOrder(undefined).size, 0);
  assert.equal(computeNodeOrder({ nodes: "nope" }).size, 0);
  assert.equal(computeNodeOrder({ nodes: [], edges: [] }).size, 0);
});

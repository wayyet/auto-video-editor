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
  createMonitorState,
  deriveRunState,
  edgeStates,
  finalizeSkipped,
  legacyLayoutKey,
  mainLayoutKey,
  normalizeMainGraph,
} from "../static/workflow_monitor.mjs";

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
  applyEvent(s, ev("node.waiting_input", { id: 6, node_id: "c", payload: { waiting_kind: "interrupt", detail: { checkpoints: ["⓪"] } } }));
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

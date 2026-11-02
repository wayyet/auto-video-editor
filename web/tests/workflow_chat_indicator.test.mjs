// /web/tests/workflow_chat_indicator.test.mjs
//
// 「对话框显示 LangGraph 当前运行节点」阶段 4(执行设计 §10.4 任务 4.7):
// workflow_chat_indicator.mjs 的 Node 单测 ——
//   纯逻辑:node_id -> name 映射、Schema 缓存 key 隔离(human/auto)、§6.5
//           显示规则(单节点/并行/重试/等待/终态/暂不可用/node_id 降级);
//   生命周期:会话关联建立与 SSE 渲染、Last-Event-ID 补发去重、store_error
//           切换、关联 503 重试、无关联隐藏(TC-13)、会话切换关旧流(TC-10)、
//           新工作流替换旧 thread、run.completed 终态(TC-08)、turn 定位、
//           clearAll 后重挂。
// DOM / fetch / EventSource / timer 全部注入假件 —— 与 workflow_monitor.test.mjs
// 同一惯例(pytest 包装见 tests_openstoryline/test_workflow_chat_indicator_frontend.py)。
import test from "node:test";
import assert from "node:assert/strict";

import {
  INDICATOR_STATE,
  WorkflowChatIndicator,
  deriveIndicatorView,
  graphNodeNameMap,
  graphSchemaKey,
} from "../static/workflow_chat_indicator.mjs";
import { applyEvent, createMonitorState } from "../static/workflow_monitor.mjs";

const SID_A = "a".repeat(32);
const SID_B = "b".repeat(32);
const THREAD_1 = "video-web-001";
const THREAD_2 = "video-web-002";

const SCHEMA = {
  workflow_id: "auto_video_editor_main",
  mode: "human",
  schema_version: 3,
  nodes: [
    { id: "launch_openstoryline", name: "探测 OpenStoryline", kind: "task" },
    { id: "storyline_generate_script", name: "生成文案", kind: "task" },
    { id: "storyline_generate_voiceover", name: "生成配音", kind: "task" },
    { id: "storyline_recommend_text", name: "推荐文本", kind: "task" },
    { id: "node_06_human_reorder", name: "人工调整镜头顺序①", kind: "task" },
  ],
};

function sseEvent(type, { id = null, node_id = null, attempt = 1, run_id = "run-1", data = {} } = {}) {
  // to_sse_frame:data 平铺 payload;此处直接给 data 字段
  return {
    id,
    event_type: type,
    node_id,
    attempt,
    run_id,
    occurred_at: "2026-11-03T02:30:00Z",
    ...data,
  };
}

function esFrame(type, payloadObj, lastEventId = "") {
  return { type, lastEventId, data: JSON.stringify(payloadObj) };
}

function reducerEvent(type, { id = null, node_id = null, attempt = 1 } = {}) {
  return { id, event_type: type, node_id, attempt, payload: {}, occurred_at: null, run_id: "run-1" };
}

// ---------------------------------------------------------------------------
// 假件:DOM / EventSource / fetch / timer
// ---------------------------------------------------------------------------

class FakeEl {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.dataset = {};
    this.hidden = false;
    this.textContent = "";
    this.attrs = {};
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c);
    else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  replaceChildren(...cs) {
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    for (const c of cs) this.appendChild(c);
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.children.indexOf(this);
    return i >= 0 ? this.parentNode.children[i + 1] || null : null;
  }
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    this.closed = false;
    FakeEventSource.created.push(this);
  }
  addEventListener(t, fn) {
    if (!this.listeners.has(t)) this.listeners.set(t, []);
    this.listeners.get(t).push(fn);
  }
  emit(frame) {
    for (const fn of this.listeners.get(frame.type) || []) fn(frame);
  }
  close() { this.closed = true; }
}
FakeEventSource.created = [];

class FakeResponse {
  constructor(status, body) {
    this.status = status;
    this.ok = status >= 200 && status < 300;
    this._body = body;
  }
  async json() { return this._body; }
}

function makeHarness({ linkResponse, graphResponse, anchor = null } = {}) {
  const chat = new FakeEl("main");
  const fakeDoc = { createElement: (t) => new FakeEl(t) };
  const pendingTimers = [];
  const fetchCalls = [];
  FakeEventSource.created.length = 0; // 类级数组,每个 harness 独立清零

  const fetchImpl = async (url) => {
    fetchCalls.push(String(url));
    if (url.includes("/chat-sessions/")) {
      if (typeof linkResponse === "function") return linkResponse(url);
      return linkResponse || new FakeResponse(200, { available: true, run: null });
    }
    if (url.includes("/api/workflow/graph")) {
      return graphResponse || new FakeResponse(200, SCHEMA);
    }
    return new FakeResponse(404, {});
  };

  const indicator = new WorkflowChatIndicator({
    chatEl: chat,
    getTurnAnchor: typeof anchor === "function" ? anchor : () => null,
    doc: fakeDoc,
    fetchImpl,
    EventSourceImpl: FakeEventSource,
    scheduleTimer: (fn) => { pendingTimers.push(fn); return pendingTimers.length; },
    cancelTimer: (t) => { pendingTimers[Number(t) - 1] = null; },
    log: { warn: () => {}, error: () => {}, log: () => {} },
    linkRetryDelayMs: 1,
    linkThrottleMs: 0,
  });

  const flush = async () => {
    for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0));
  };
  const flushTimers = async () => {
    const fns = pendingTimers.splice(0, pendingTimers.length).filter(Boolean);
    for (const fn of fns) fn();
    await flush();
  };

  return { chat, indicator, fetchCalls, pendingTimers, flush, flushTimers, FakeEventSource };
}

function indicatorRow(chat) {
  return chat.children.find((c) => String(c.className || "").includes("workflow-node-msg")) || null;
}

function renderedNames(chat) {
  const row = indicatorRow(chat);
  if (!row) return null;
  const indicator = row.children[0];
  const label = indicator.children[0];
  const names = indicator.children[1];
  return {
    state: indicator.dataset.state,
    label: label.hidden ? "" : label.textContent,
    names: names.children.map((c) => c.textContent),
  };
}

// ---------------------------------------------------------------------------
// 1. 纯逻辑:Schema key / 名称映射
// ---------------------------------------------------------------------------

test("graphSchemaKey:workflow_id + mode + schema_version 隔离(TC-16 前提)", () => {
  const base = graphSchemaKey(SCHEMA);
  assert.match(base, /auto_video_editor_main_human_v3/);
  assert.notEqual(base, graphSchemaKey({ ...SCHEMA, mode: "auto" }));
  assert.notEqual(base, graphSchemaKey({ ...SCHEMA, schema_version: 4 }));
  assert.notEqual(base, graphSchemaKey({ ...SCHEMA, workflow_id: "other" }));
  assert.equal(graphSchemaKey(null), "wf_graph__default_v1");
});

test("graphNodeNameMap:name 缺失回退 node_id;非 task 节点也进映射", () => {
  const map = graphNodeNameMap(SCHEMA);
  assert.equal(map.get("launch_openstoryline"), "探测 OpenStoryline");
  assert.equal(map.size, SCHEMA.nodes.length);
  const map2 = graphNodeNameMap({ nodes: [{ id: "x" }, { name: "无 id" }, null] });
  assert.equal(map2.get("x"), "x");
  assert.equal(map2.size, 1);
  assert.equal(graphNodeNameMap(null).size, 0);
});

// ---------------------------------------------------------------------------
// 2. 纯逻辑:§6.5 显示规则
// ---------------------------------------------------------------------------

test("deriveIndicatorView:单 running 节点 -> 中文大字(TC-02)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "launch_openstoryline" }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.visible, true);
  assert.equal(v.state, INDICATOR_STATE.RUNNING);
  assert.equal(v.label, "当前运行节点");
  assert.deepEqual(v.names, ["探测 OpenStoryline"]);
  assert.equal(v.parallel, false);
});

test("deriveIndicatorView:并行多节点 -> 全部展示、标记(并行),不互相覆盖(TC-04)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "storyline_generate_script" }));
  applyEvent(m, reducerEvent("node.started", { id: 2, node_id: "storyline_generate_voiceover" }));
  applyEvent(m, reducerEvent("node.started", { id: 3, node_id: "storyline_recommend_text" }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.parallel, true);
  assert.equal(v.label, "当前运行节点（并行）");
  assert.deepEqual(v.names, ["生成文案", "生成配音", "推荐文本"]);
});

test("deriveIndicatorView:retrying 保留名称并切状态(TC-05)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "storyline_render_video" }));
  applyEvent(m, reducerEvent("node.retrying", { id: 2, node_id: "storyline_render_video", attempt: 2 }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.state, INDICATOR_STATE.RETRYING);
  assert.deepEqual(v.names, ["storyline_render_video"]);
});

test("deriveIndicatorView:node.waiting_input -> 等待人工操作区(TC-06)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "node_06_human_reorder" }));
  applyEvent(m, reducerEvent("node.waiting_input", { id: 2, node_id: "node_06_human_reorder", payload: { waiting_kind: "interrupt" } }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.state, INDICATOR_STATE.WAITING);
  assert.equal(v.label, "等待人工操作");
  assert.deepEqual(v.names, ["人工调整镜头顺序①"]);
});

test("deriveIndicatorView:run.interrupted 无 waiting 节点 -> 仅等待文本;resumed 回到运行中(TC-07)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "node_06_human_reorder" }));
  applyEvent(m, reducerEvent("node.waiting_input", { id: 2, node_id: "node_06_human_reorder" }));
  applyEvent(m, reducerEvent("run.interrupted", { id: 3 }));
  let v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.state, INDICATOR_STATE.WAITING);

  applyEvent(m, reducerEvent("node.resumed", { id: 4, node_id: "node_06_human_reorder" }));
  v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.state, INDICATOR_STATE.RUNNING);
  assert.deepEqual(v.names, ["人工调整镜头顺序①"]);
});

test("deriveIndicatorView:node.succeeded 移除已完成,其余活动节点保留", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "storyline_generate_script" }));
  applyEvent(m, reducerEvent("node.started", { id: 2, node_id: "storyline_generate_voiceover" }));
  applyEvent(m, reducerEvent("node.succeeded", { id: 3, node_id: "storyline_generate_script" }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.deepEqual(v.names, ["生成配音"]);
});

test("deriveIndicatorView:run.completed / run.failed 终态(TC-08/TC-09)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "storyline_render_video" }));
  applyEvent(m, reducerEvent("run.completed", { id: 2 }));
  let v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.state, INDICATOR_STATE.DONE);
  assert.deepEqual(v.names, ["工作流已完成"]);
  assert.equal(v.label, "");

  applyEvent(m, reducerEvent("run.started", { id: 3 }));
  applyEvent(m, reducerEvent("node.started", { id: 4, node_id: "storyline_render_video" }));
  applyEvent(m, reducerEvent("run.failed", { id: 5 }));
  v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.equal(v.state, INDICATOR_STATE.FAILED);
  assert.deepEqual(v.names, ["工作流失败"]);
});

test("deriveIndicatorView:store_error -> 暂不可用,不冒充节点(TC-15)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "launch_openstoryline" }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA), { storeError: true });
  assert.equal(v.state, INDICATOR_STATE.UNAVAILABLE);
  assert.deepEqual(v.names, ["工作流状态暂不可用"]);
});

test("deriveIndicatorView:Schema 缺失节点 -> 降级 node_id 并记录 unmapped(§5.3)", () => {
  const m = createMonitorState();
  applyEvent(m, reducerEvent("node.started", { id: 1, node_id: "mystery_node" }));
  const v = deriveIndicatorView(m, graphNodeNameMap(SCHEMA));
  assert.deepEqual(v.names, ["mystery_node"]);
  assert.deepEqual(v.unmapped, ["mystery_node"]);
});

test("deriveIndicatorView:零事件时用关联状态兜底;running 关联显示空运行视图", () => {
  const v1 = deriveIndicatorView(createMonitorState(), graphNodeNameMap(SCHEMA), { linkStatus: "completed" });
  assert.equal(v1.state, INDICATOR_STATE.DONE);
  const v2 = deriveIndicatorView(createMonitorState(), graphNodeNameMap(SCHEMA), { linkStatus: "waiting_human" });
  assert.equal(v2.state, INDICATOR_STATE.WAITING);
  const v3 = deriveIndicatorView(createMonitorState(), graphNodeNameMap(SCHEMA), { linkStatus: "running" });
  assert.equal(v3.state, INDICATOR_STATE.RUNNING);
  assert.deepEqual(v3.names, []);
});

// ---------------------------------------------------------------------------
// 3. 生命周期:关联查询 + SSE + DOM
// ---------------------------------------------------------------------------

test("会话关联建立:订阅 runEvents、DOM 结构、中文节点渲染(TC-01/TC-02)", async () => {
  const h = makeHarness({
    linkResponse: new FakeResponse(200, {
      available: true, chat_session_id: SID_A, thread_id: THREAD_1, run_id: "run-1",
      status: "running", updated_at: "2026-11-03T02:30:00Z",
    }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();

  const es = h.FakeEventSource.created.at(-1);
  assert.ok(es, "应创建 EventSource");
  assert.equal(es.url, `/api/workflow/runs/${THREAD_1}/events`);

  const row = indicatorRow(h.chat);
  assert.ok(row, "组件行应挂载");
  assert.equal(row.getAttribute("role"), "status");
  assert.equal(row.getAttribute("aria-live"), "polite");
  assert.ok(String(row.className).includes("msg assistant workflow-node-msg"));

  es.emit(esFrame("snapshot", { thread_id: THREAD_1, state: "running", event_count: 0 }));
  es.emit(esFrame("node.started", sseEvent("node.started", { id: 7, node_id: "launch_openstoryline" }), "7"));
  await h.flush();

  const view = renderedNames(h.chat);
  assert.equal(view.state, "running");
  assert.equal(view.label, "当前运行节点");
  assert.deepEqual(view.names, ["探测 OpenStoryline"]);
});

test("Last-Event-ID 补发重叠:同 id/旧 id 重复投递不重复更新、不倒退(TC-12)", async () => {
  const h = makeHarness({
    linkResponse: new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  const es = h.FakeEventSource.created.at(-1);

  es.emit(esFrame("node.started", sseEvent("node.started", { id: 10, node_id: "launch_openstoryline" }), "10"));
  await h.flush();
  assert.deepEqual(renderedNames(h.chat).names, ["探测 OpenStoryline"]);

  // 同一事件重发(SSE 重连补发重叠)
  es.emit(esFrame("node.started", sseEvent("node.started", { id: 10, node_id: "launch_openstoryline" }), "10"));
  // 更旧的历史事件(乱序补发)
  es.emit(esFrame("run.started", sseEvent("run.started", { id: 3 }), "3"));
  // 前置节点完成 + 新节点照常推进
  es.emit(esFrame("node.succeeded", sseEvent("node.succeeded", { id: 12, node_id: "launch_openstoryline", data: { duration_ms: 120 } }), "12"));
  es.emit(esFrame("node.started", sseEvent("node.started", { id: 13, node_id: "storyline_generate_script" }), "13"));
  await h.flush();
  const view = renderedNames(h.chat);
  assert.deepEqual(view.names, ["生成文案"]); // 旧节点已完成切换,没有被重复插入
  assert.equal(view.label, "当前运行节点");
});

test("store_error 帧 -> 暂不可用;后续事件恢复(TC-15)", async () => {
  const h = makeHarness({
    linkResponse: new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  const es = h.FakeEventSource.created.at(-1);

  es.emit(esFrame("node.started", sseEvent("node.started", { id: 1, node_id: "launch_openstoryline" }), "1"));
  await h.flush();
  assert.deepEqual(renderedNames(h.chat).names, ["探测 OpenStoryline"]);

  es.emit(esFrame("store_error", { detail: "event store unavailable" }));
  await h.flush();
  let view = renderedNames(h.chat);
  assert.equal(view.state, "unavailable");
  assert.deepEqual(view.names, ["工作流状态暂不可用"]);

  es.emit(esFrame("node.succeeded", sseEvent("node.succeeded", { id: 2, node_id: "launch_openstoryline", data: { duration_ms: 120 } }), "2"));
  es.emit(esFrame("node.started", sseEvent("node.started", { id: 3, node_id: "storyline_generate_script" }), "3"));
  await h.flush();
  view = renderedNames(h.chat);
  assert.equal(view.state, "running");
  assert.deepEqual(view.names, ["生成文案"]);
});

test("关联查询 503 -> 暂不可用 + 有限重试,恢复后正常订阅(TC-15/§6.6)", async () => {
  let fail = true;
  const h = makeHarness({
    linkResponse: (url) => fail
      ? new FakeResponse(503, { detail: "workflow chat link unavailable" })
      : new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();

  let view = renderedNames(h.chat);
  assert.equal(view.state, "unavailable");
  assert.deepEqual(view.names, ["工作流状态暂不可用"]);
  assert.equal(h.FakeEventSource.created.length, 0, "503 期间不得订阅");

  // turn 开始:本轮 Agent 执行期间允许有限间隔重试(§6.6)
  h.indicator.onAssistantTurnStart();
  await h.flush();
  assert.ok(h.pendingTimers.length > 0, "应安排重试");

  fail = false;
  await h.flushTimers();
  await h.flush();

  assert.equal(h.FakeEventSource.created.length, 1, "恢复后应订阅");
  const view2 = renderedNames(h.chat);
  assert.equal(view2.state, "running");
});

test("无关联(run:null) -> 隐藏组件、不订阅(TC-13)", async () => {
  const h = makeHarness({ linkResponse: new FakeResponse(200, { available: true, run: null }) });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();

  assert.equal(h.FakeEventSource.created.length, 0);
  assert.equal(indicatorRow(h.chat), null);
});

test("会话切换:旧 EventSource 关闭、状态清空,新会话独立订阅(TC-10)", async () => {
  const responses = new Map([
    [SID_A, new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" })],
    [SID_B, new FakeResponse(200, { available: true, thread_id: THREAD_2, status: "running" })],
  ]);
  const h = makeHarness({
    linkResponse: (url) => (url.includes(SID_A) ? responses.get(SID_A) : responses.get(SID_B)),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  const esA = h.FakeEventSource.created.at(-1);
  esA.emit(esFrame("node.started", sseEvent("node.started", { id: 1, node_id: "launch_openstoryline" }), "1"));
  await h.flush();
  assert.deepEqual(renderedNames(h.chat).names, ["探测 OpenStoryline"]);

  h.indicator.onSessionChanged(SID_B);
  await h.flush();

  assert.equal(esA.closed, true, "旧 EventSource 必须先关闭");
  const esB = h.FakeEventSource.created.at(-1);
  assert.notEqual(esB, esA);
  assert.equal(esB.url, `/api/workflow/runs/${THREAD_2}/events`);
  assert.deepEqual(renderedNames(h.chat).names, [], "旧会话节点名称必须清空");

  esB.emit(esFrame("node.started", sseEvent("node.started", { id: 50, node_id: "storyline_generate_voiceover" }), "50"));
  await h.flush();
  assert.deepEqual(renderedNames(h.chat).names, ["生成配音"]);
});

test("新工作流替换旧 thread:关旧流、重置 reducer(§6.6)", async () => {
  let body = { available: true, thread_id: THREAD_1, status: "running" };
  const h = makeHarness({ linkResponse: () => new FakeResponse(200, body) });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  const es1 = h.FakeEventSource.created.at(-1);
  es1.emit(esFrame("node.started", sseEvent("node.started", { id: 1, node_id: "launch_openstoryline" }), "1"));
  await h.flush();
  assert.deepEqual(renderedNames(h.chat).names, ["探测 OpenStoryline"]);

  body = { available: true, thread_id: THREAD_2, status: "running" };
  h.indicator.onAssistantTurnStart(); // 新 turn 刷新关联 -> 发现新 thread
  await h.flush();

  assert.equal(es1.closed, true);
  const es2 = h.FakeEventSource.created.at(-1);
  assert.equal(es2.url, `/api/workflow/runs/${THREAD_2}/events`);
  assert.deepEqual(renderedNames(h.chat).names, [], "旧 reducer 状态必须重置");
});

test("run.completed -> 显示工作流已完成(TC-08)", async () => {
  const h = makeHarness({
    linkResponse: new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  const es = h.FakeEventSource.created.at(-1);
  es.emit(esFrame("node.started", sseEvent("node.started", { id: 1, node_id: "storyline_render_video" }), "1"));
  es.emit(esFrame("run.completed", sseEvent("run.completed", { id: 2 }), "2"));
  await h.flush();
  const view = renderedNames(h.chat);
  assert.equal(view.state, "done");
  assert.deepEqual(view.names, ["工作流已完成"]);
});

test("turn 开始定位:组件行插到当前轮气泡前;新 turn 移动(阶段 5 §6.1)", async () => {
  // 第一轮:anchor 代表当前轮正文气泡 wrap(MCode 面板在其之前已 append)
  const wrap1 = new FakeEl("div");
  const h = makeHarness({
    linkResponse: new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
    anchor: () => wrap1,
  });
  h.chat.appendChild(wrap1);
  h.indicator.onAssistantTurnStart();
  h.indicator.onSessionChanged(SID_A);
  await h.flush();

  assert.ok(indicatorRow(h.chat), "组件行应挂载");
  assert.equal(h.chat.children.indexOf(indicatorRow(h.chat)), 0);
  assert.equal(h.chat.children[1], wrap1, "组件行应在正文气泡前(锚点插入)");

  // 第二轮:新 anchor -> 组件行移动
  const wrap2 = new FakeEl("div");
  h.chat.appendChild(wrap2);
  h.indicator._getTurnAnchor = () => wrap2;
  h.indicator.onAssistantTurnStart();
  await h.flush();

  assert.equal(h.chat.children.indexOf(indicatorRow(h.chat)), 1);
  assert.equal(h.chat.children[2], wrap2);
});

test("clearAll 清掉组件行后 onChatCleared 重新挂载(TC-11 前提)", async () => {
  const h = makeHarness({
    linkResponse: new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  assert.ok(indicatorRow(h.chat));

  // 模拟 ui.clearAll() / 历史重放:聊天区清空
  h.chat.replaceChildren();
  assert.equal(indicatorRow(h.chat), null);

  h.indicator.onChatCleared();
  assert.ok(indicatorRow(h.chat), "应按当前关联重新挂载");
});

test("同会话幂等:onSessionChanged 重复调用不重复订阅", async () => {
  const h = makeHarness({
    linkResponse: new FakeResponse(200, { available: true, thread_id: THREAD_1, status: "running" }),
  });
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  h.indicator.onSessionChanged(SID_A);
  h.indicator.onSessionChanged(SID_A);
  await h.flush();
  assert.equal(h.FakeEventSource.created.length, 1);
});

// /static/workflow_monitor.mjs
//
// 工作流流程图实时化的**纯逻辑层**(设计《工作流流程图与实时节点追踪》
// §6.2-F,阶段5)。不碰 DOM,因此在 Node 里可直接单测
// (web/tests/workflow_monitor.test.mjs,与 media_upload.mjs 同一惯例)。
//
// 职责:
//   1. §7.1 标准事件 → 节点/运行状态机的归约(applyEvent / createMonitorState);
//   2. Last-Event-ID 补发去重(cursor 单调,重放不重复应用);
//   3. 并行:多个节点可同时处于 running(绝不强制单线程流水线);
//   4. 重试:同一 node_id 多次执行按 attempt 保留历史,不覆盖(设计 §11.1);
//   5. 布局缓存 key 纳入 workflow_id + mode + schema_version(§6.2-F 第 7 条),
//      主图与 14 节点子流程、human 与 auto 互不污染;
//   6. API 路径与 SSE URL 构造;运行状态派生(与后端 _derive_state 同规则)。
//
// 边界:不 fetch、不操作 DOM、不依赖浏览器全局 —— 全部由调用方(node_map.html
// 的模块脚本)注入。

// 布局 key 的 TB 算法版本常量单一来源(第5期 阶段1);纯常量依赖,不引布局逻辑。
import { LAYOUT_ALGORITHM_VERSION } from "./workflow_layout.mjs";

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

/** 节点七态(§4.1)。skipped 仅在运行终态后对"未执行"节点给出。 */
export const NODE_STATUS = Object.freeze({
  IDLE: "idle",
  RUNNING: "running",
  DONE: "done",
  WAITING: "waiting",
  RETRYING: "retrying",
  FAILED: "failed",
  SKIPPED: "skipped",
});

/** 运行状态(与后端 api_workflow._derive_state 同一套映射)。 */
export const RUN_STATE = Object.freeze({
  NO_EVENTS: "no_events",
  IDLE: "idle",
  RUNNING: "running",
  WAITING_HUMAN: "waiting_human",
  COMPLETED: "completed",
  FAILED: "failed",
});

const LAST_EVENT_RUN_STATE = Object.freeze({
  "run.completed": "completed",
  "run.failed": "failed",
  "run.interrupted": "waiting_human",
  "node.waiting_input": "waiting_human",
  "node.started": "running",
  "node.resumed": "running",
  "node.retrying": "running",
  "node.progress": "running",
});

/** API 路径(只读监控 API;工作流启动走 POST /api/console/runs —— 订阅绝不触发执行)。 */
export const API_PATHS = Object.freeze({
  graph: "/api/workflow/graph",
  nodeNames: "/api/workflow/node-names",
  runs: "/api/workflow/runs",
  runSnapshot: (threadId) => `/api/workflow/runs/${encodeURIComponent(threadId)}`,
  runEvents: (threadId) => `/api/workflow/runs/${encodeURIComponent(threadId)}/events`,
});

// ---------------------------------------------------------------------------
// 布局缓存 key
// ---------------------------------------------------------------------------

/**
 * 主图布局 key:纳入 workflow_id + mode + schema_version(§6.2-F 第 7 条)。
 * human 与 auto、schema 演进互不污染。
 * 第5期阶段1:可选 direction —— "tb" 追加纵向布局算法版本后缀(与
 * workflow_layout.mjs::getLayoutCacheKey 同规则);其余/缺省保持历史格式,
 * 用户已保存的横向(LR)拖拽位置零迁移兼容。
 */
export function mainLayoutKey(workflowId, mode, schemaVersion, direction) {
  const base = `wf_layout_${String(workflowId || "workflow")}_${String(mode || "default")}_v${Number(schemaVersion) || 1}`;
  if (String(direction).toLowerCase() === "tb") return `${base}_tb_${LAYOUT_ALGORITHM_VERSION}`;
  return base;
}

/** 旧 14 节点子流程沿用历史 key(node_map.html 既有规则),布局数据不受影响。 */
export function legacyLayoutKey(workflowId) {
  return `node_layout_${String(workflowId || "workflow")}`;
}

// ---------------------------------------------------------------------------
// 状态归约
// ---------------------------------------------------------------------------

/** 新建监控状态。nodes 按 node_id 索引;branches 记录 branch.selected 序列。 */
export function createMonitorState() {
  return {
    cursor: 0,          // 已应用的最大事件 id(Last-Event-ID 去重游标)
    runId: null,
    run: { state: RUN_STATE.NO_EVENTS, startedAt: null, lastEvent: null },
    nodes: {},          // id -> nodeState
    branches: [],       // {from:[], to, label, inferred}
  };
}

function ensureNode(state, id) {
  if (!state.nodes[id]) {
    state.nodes[id] = {
      id,
      status: "idle",
      attempt: 0,
      startedAt: null,
      durationMs: null,
      durations: [],      // 每次 attempt 的耗时(重试历史,§11.1)
      lastError: null,
      waitingKind: null,  // "interrupt" | "web_file_handshake"
      waitingDetail: null,
      resumed: false,
      lastProgress: null,
    };
  }
  return state.nodes[id];
}

/**
 * 应用一条事件;返回 {applied, state}。
 *
 * - **去重**:`event.id` 非 null 且 <= cursor → 不应用(SSE 重连补发/重放
 *   与已应用事件重叠时,保证幂等)。
 * - id 为 null(存储降级时未落库的事件)照常应用,不推进 cursor。
 */
export function applyEvent(state, event) {
  if (!event || typeof event !== "object" || !event.event_type) {
    return { applied: false, state };
  }
  const id = event.id;
  if (id !== null && id !== undefined && Number(id) <= state.cursor) {
    return { applied: false, state };
  }
  if (id !== null && id !== undefined) {
    state.cursor = Number(id);
  }

  const type = event.event_type;
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  const at = event.occurred_at || null;

  if (type.startsWith("run.")) {
    state.runId = event.run_id || state.runId;
    state.run.lastEvent = event;
    if (type === "run.started") {
      state.run.state = RUN_STATE.RUNNING;
      state.run.startedAt = at;
    } else if (type === "run.resumed") {
      state.run.state = RUN_STATE.RUNNING;
    } else if (type === "run.interrupted") {
      state.run.state = RUN_STATE.WAITING_HUMAN;
    } else if (type === "run.completed") {
      state.run.state = RUN_STATE.COMPLETED;
    } else if (type === "run.failed") {
      state.run.state = RUN_STATE.FAILED;
    }
    return { applied: true, state };
  }

  if (type === "branch.selected") {
    state.branches.push({
      to: event.node_id || null,
      from: Array.isArray(payload.detail?.from) ? payload.detail.from.slice() : [],
      label: payload.label || null,
      inferred: !!payload.inferred,
    });
    return { applied: true, state };
  }

  if (type.startsWith("node.") && event.node_id) {
    const n = ensureNode(state, event.node_id);
    state.run.lastEvent = event;
    switch (type) {
      case "node.started":
        n.status = "running";
        n.attempt = Number(event.attempt) || n.attempt || 1;
        n.startedAt = at;
        n.waitingKind = null;
        n.waitingDetail = null;
        n.resumed = false;
        n.lastError = null;
        break;
      case "node.retrying":
        n.status = "retrying";
        n.attempt = Number(event.attempt) || n.attempt + 1;
        break;
      case "node.resumed":
        n.status = "running";
        n.resumed = true;
        n.startedAt = at;
        n.waitingKind = null;
        n.waitingDetail = null;
        break;
      case "node.progress":
        n.lastProgress = payload.summary || null;
        break;
      case "node.succeeded":
        n.status = "done";
        n.durationMs = Number.isFinite(payload.duration_ms) ? payload.duration_ms : n.durationMs;
        if (Number.isFinite(payload.duration_ms)) n.durations.push(payload.duration_ms);
        break;
      case "node.failed":
        n.status = "failed";
        n.lastError = payload.error || null;
        if (Number.isFinite(payload.duration_ms)) n.durations.push(payload.duration_ms);
        break;
      case "node.waiting_input":
        n.status = "waiting";
        n.waitingKind = payload.waiting_kind || "interrupt";
        n.waitingDetail = payload.detail || null;
        break;
      default:
        break; // 未知 node.*:忽略但不破坏状态
    }
    return { applied: true, state };
  }

  return { applied: true, state };
}

/** 运行终态后,把"启用了但从未执行"的节点标记 skipped(§4.1:执行状态确定后才弱化)。 */
export function finalizeSkipped(state, schemaNodes) {
  const terminal = state.run.state === RUN_STATE.COMPLETED || state.run.state === RUN_STATE.FAILED;
  if (!terminal) return 0;
  let marked = 0;
  for (const n of schemaNodes || []) {
    const st = state.nodes[n.id];
    if (!st && n.enabled !== false && n.reachable !== false && n.kind === "task") {
      state.nodes[n.id] = {
        id: n.id, status: "skipped", attempt: 0, startedAt: null, durationMs: null,
        durations: [], lastError: null, waitingKind: null, waitingDetail: null,
        resumed: false, lastProgress: null,
      };
      marked += 1;
    }
  }
  return marked;
}

// ---------------------------------------------------------------------------
// 投影:计数 / 活动节点 / 已走边
// ---------------------------------------------------------------------------

/** 进度概览(§4:不得用百分比;按状态计数)。 */
export function computeCounts(state) {
  const counts = { running: 0, done: 0, waiting: 0, retrying: 0, failed: 0, skipped: 0, idle: 0, touched: 0 };
  for (const n of Object.values(state.nodes)) {
    if (counts[n.status] !== undefined) counts[n.status] += 1;
    if (n.status !== "idle" && n.status !== "skipped") counts.touched += 1;
  }
  return counts;
}

/** 当前活动节点(可能多个 —— 并行超步,§11.2)。 */
export function activeNodeIds(state) {
  return Object.values(state.nodes)
    .filter((n) => n.status === "running" || n.status === "retrying")
    .map((n) => n.id);
}

/**
 * 已走边 keys(源/目标都真实执行过才点亮;未命中条件分支不预判为跳过)。
 * 返回 {traversed: Set<edgeKey>, active: Set<edgeKey>};edgeKey 用 edge.id,
 * 缺省回退 "from->to"。
 *
 * 终端边规则:
 * - ``START->x``:目标启动即点亮;
 * - ``x->END``:需要 x **已完成** 且运行**正常完成** —— 挂起/失败都没走到
 *   终点;且 x 的其它出边已被点亮(条件门走了另一条)时抑制(§4.1:未命中
 *   分支不点亮;单出边源无歧义,x 完成即视为走到 END)。
 */
export function edgeStates(schemaEdges, state) {
  const statusOf = (id) => (state.nodes[id] ? state.nodes[id].status : "idle");
  const touched = (id) => statusOf(id) !== "idle" && statusOf(id) !== "skipped";
  const isActive = (id) => statusOf(id) === "running" || statusOf(id) === "retrying";
  const runCompleted = !!(state.run && state.run.state === RUN_STATE.COMPLETED);
  const traversed = new Set();
  const active = new Set();
  const terminalEdges = [];
  for (const e of schemaEdges || []) {
    const key = e.id || `${e.from}->${e.to}`;
    if (e.from === "START" || e.to === "END") {
      terminalEdges.push([e, key]);
      continue;
    }
    if (touched(e.from) && touched(e.to)) {
      traversed.add(key);
      if (isActive(e.to)) active.add(key);
    }
  }
  for (const [e, key] of terminalEdges) {
    if (e.from === "START") {
      if (touched(e.to)) traversed.add(key);
      continue;
    }
    // x->END:源完成 + 运行正常完成 + 源的其它出边都没被走过(无歧义)
    if (
      runCompleted &&
      statusOf(e.from) === "done" &&
      !traversedHasFrom(schemaEdges, traversed, e.from)
    ) {
      traversed.add(key);
    }
  }
  return { traversed, active };
}

function traversedHasFrom(schemaEdges, traversed, from) {
  for (const e of schemaEdges || []) {
    if (e.from === from && traversed.has(e.id || `${e.from}->${e.to}`)) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// 杂项
// ---------------------------------------------------------------------------

/** 运行状态派生(与后端快照同规则;快照帧/冷启动用)。 */
export function deriveRunState(lastEventType) {
  if (!lastEventType) return "no_events";
  return LAST_EVENT_RUN_STATE[lastEventType] || "idle";
}

/**
 * 主图 API 数据 → 渲染器数据:由 edges 推导 dependencies / next_nodes
 * (渲染器两套都兼容;edges 存在时连线以 edges 为准)。
 */
export function normalizeMainGraph(schema) {
  if (!schema || !Array.isArray(schema.nodes)) return schema;
  const edges = Array.isArray(schema.edges) ? schema.edges : [];
  const deps = new Map(schema.nodes.map((n) => [n.id, new Set()]));
  const nexts = new Map(schema.nodes.map((n) => [n.id, new Set()]));
  for (const e of edges) {
    if (!deps.has(e.from) || !deps.has(e.to)) continue;
    nexts.get(e.from).add(e.to);
    deps.get(e.to).add(e.from);
  }
  return {
    workflow_meta: {
      id: schema.workflow_id || "auto_video_editor_main",
      name: `完整 LangGraph 工作流(mode=${schema.mode || "?"})`,
      description: `schema v${schema.schema_version} · flags: ${
        Object.entries(schema.flags || {}).map(([k, v]) => `${k}=${v}`).join(", ") || "-"
      }`,
    },
    schema_version: schema.schema_version,
    mode: schema.mode,
    flags: schema.flags,
    edges,
    nodes: schema.nodes.map((n) => ({
      ...n,
      dependencies: Array.from(deps.get(n.id) || []),
      next_nodes: Array.from(nexts.get(n.id) || []),
    })),
  };
}

/**
 * 主图 schema → 节点执行顺序序号(监控页"节点状态"面板用,2026-11-11)。
 *
 * 与节点图视图(node_map.html,main 视图)的展示顺序严格一致:
 *   1. normalizeMainGraph 由 edges 推 dependencies;
 *   2. 最长路径分层(DFS + 回边/自环按 0 贡献跳过)—— 同构于
 *      node_map.html::computeDepths(QC/qa 重试回环与 node_07 自环在迭代
 *      松弛下不收敛,DFS 遇 visiting 态依赖直接跳过,DAG 部分取最长链);
 *   3. 排序 = 深度升序,同深度按(中文名 || id)localeCompare 升序 ——
 *      即节点图视图"按列从左到右、同列自上而下"的阅读顺序
 *      (同列内排序规则同 autoLayout)。
 *
 * 只给 kind === "task" 的节点编号:START"开始"/END"结束"是 terminal,
 * 不会出现在监控面板的事件里;这样首个任务节点(载入媒体)从 1 起。
 * 同深度节点是并行超步,列内序号只是图页同列展示序,不对应真实调度时刻。
 *
 * 注意:node_map.html::computeDepths/autoLayout 是本算法的渲染层同构实现
 * (与 normalizeMainGraph / normalizeMainGraphInline 同一惯例),
 * 任何一侧调整必须两处同步。
 *
 * @param {object} schema /api/workflow/graph 原始 JSON
 * @returns {Map<string, number>} node_id -> 1-based 序号;schema 异常时为空 Map
 */
export function computeNodeOrder(schema) {
  if (!schema || !Array.isArray(schema.nodes)) return new Map();
  const norm = normalizeMainGraph(schema);
  const byId = new Map(norm.nodes.map((n) => [n.id, n]));
  const depsOf = new Map(norm.nodes.map((n) => [n.id,
    (Array.isArray(n.dependencies) ? n.dependencies : []).filter((d) => byId.has(d) && d !== n.id)
  ]));
  const depth = new Map(norm.nodes.map((n) => [n.id, 0]));
  const mark = new Map(); // 0=未访问 1=访问中 2=完成
  function visit(id) {
    const st = mark.get(id) || 0;
    if (st === 2) return depth.get(id) || 0;
    if (st === 1) return 0; // 回边/环:忽略,防不收敛(同 computeDepths)
    mark.set(id, 1);
    let d = 0;
    for (const dep of depsOf.get(id) || []) {
      d = Math.max(d, visit(dep) + 1);
    }
    mark.set(id, 2);
    depth.set(id, d);
    return d;
  }
  for (const n of norm.nodes) visit(n.id);
  const ordered = norm.nodes
    .filter((n) => n.kind === "task")
    .sort((a, b) =>
      (depth.get(a.id) - depth.get(b.id)) ||
      ((a.name || a.id).localeCompare(b.name || b.id)));
  return new Map(ordered.map((n, i) => [n.id, i + 1]));
}

// ---------------------------------------------------------------------------
// SSE 帧 → applyEvent 期望的事件形状(第四期阶段 1,G1 前端契约修复)
// ---------------------------------------------------------------------------
// 后端 to_sse_frame(monitoring/workflow_events.py)把 payload 的字段平铺进 data:
//   data = {thread_id, run_id, node_id, attempt, occurred_at, ...payload}
// 而 applyEvent 与创作控制台的交付状态卡读取的是 event.payload.xxx。
// 这里把平铺的 data 还原:信封字段留在顶层,其余字段(summary / detail / error /
// duration_ms / waiting_kind / trigger / label / inferred / reason,即后端的
// ALLOWED_PAYLOAD_KEYS)收回 payload。payload 白名单里没有信封字段名,拆分不会串键。
const SSE_ENVELOPE_KEYS = new Set(["thread_id", "run_id", "node_id", "attempt", "occurred_at"]);

/**
 * @param {string|number|null|undefined} lastEventId  EventSource 的 lastEventId(事件表 id)
 * @param {string} eventType                          EventSource 的 type,如 "node.progress"
 * @param {object} data                               JSON.parse(esEvent.data) 的结果
 */
export function eventFromSse(lastEventId, eventType, data) {
  const envelope = {};
  const payload = {};
  for (const [key, value] of Object.entries(data && typeof data === "object" ? data : {})) {
    if (SSE_ENVELOPE_KEYS.has(key)) envelope[key] = value;
    else payload[key] = value;
  }
  return {
    id: lastEventId ? Number(lastEventId) : null,
    event_type: eventType,
    ...envelope,
    payload,
  };
}

// /static/workflow_chat_indicator.mjs
//
// 「对话框显示 LangGraph 当前运行节点」阶段 4(执行设计 §6.3–§6.6)。
// 对话页的会话—工作流节点状态组件:
//   1. 以当前 chat_session_id 查询关联接口(§5.1 GET /api/workflow/chat-sessions/{id}/run);
//   2. GET /api/workflow/graph 建立 node_id -> 中文名映射(Schema 缓存 key 含
//      workflow_id + mode + schema_version,对齐 workflow_monitor.mainLayoutKey 口径);
//   3. 订阅现有 SSE(API_PATHS.runEvents),事件交给 workflow_monitor.mjs 的
//      状态归约(createMonitorState / applyEvent / activeNodeIds)—— **不重建状态机**;
//   4. 按归约结果生成显示数据(单节点 / 并行 / 重试 / 等待 / 终态 / 暂不可用,§6.5);
//   5. 管理 EventSource 生命周期:会话切换先关旧连接、thread 切换重置 reducer(§6.6)。
//
// 分层:纯逻辑(graphSchemaKey / graphNodeNameMap / deriveIndicatorView)不碰 DOM、
// 不发请求,Node 里直接单测(web/tests/workflow_chat_indicator.test.mjs);
// WorkflowChatIndicator 只做副作用,document / fetch / EventSource / timer 全部
// 可注入。页面刷新、切换会话、订阅 SSE 均为 GET —— 绝不 POST 启动工作流(§5.2)。

import {
  API_PATHS,
  activeNodeIds,
  applyEvent,
  createMonitorState,
} from "./workflow_monitor.mjs";

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

/** SSE 订阅事件类型(§6.4;与 node_map.html 同一份清单)。 */
export const INDICATOR_EVENT_TYPES = Object.freeze([
  "snapshot", "store_error",
  "run.started", "run.resumed", "run.interrupted", "run.completed", "run.failed",
  "node.started", "node.succeeded", "node.failed", "node.waiting_input",
  "node.resumed", "node.retrying", "node.progress", "branch.selected",
]);

/** 组件显示态(写进 data-state,样式按它切换;§6.2)。 */
export const INDICATOR_STATE = Object.freeze({
  RUNNING: "running",
  RETRYING: "retrying",
  WAITING: "waiting",
  DONE: "done",
  FAILED: "failed",
  UNAVAILABLE: "unavailable",
});

const LABEL_RUNNING = "当前运行节点";
const LABEL_PARALLEL_SUFFIX = "（并行）";
const LABEL_WAITING = "等待人工操作";
const TEXT_DONE = "工作流已完成";
const TEXT_FAILED = "工作流失败";
const TEXT_UNAVAILABLE = "工作流状态暂不可用";

// ---------------------------------------------------------------------------
// 纯逻辑(可单测)
// ---------------------------------------------------------------------------

/**
 * 图 Schema 缓存 key:workflow_id + mode + schema_version(§5.3,对齐
 * workflow_monitor.mainLayoutKey)。human / auto 模式、schema 演进互不污染。
 */
export function graphSchemaKey(schema) {
  if (!schema || typeof schema !== "object") return "wf_graph__default_v1";
  const wid = String(schema.workflow_id || "workflow");
  const mode = String(schema.mode || "default");
  const ver = Number(schema.schema_version) || 1;
  return `wf_graph_${wid}_${mode}_v${ver}`;
}

/** 图 Schema -> node_id -> 中文名映射(name 缺失回退 node_id;§5.3)。 */
export function graphNodeNameMap(schema) {
  const map = new Map();
  const nodes = (schema && Array.isArray(schema.nodes)) ? schema.nodes : [];
  for (const n of nodes) {
    if (!n || !n.id) continue;
    map.set(String(n.id), String(n.name || n.id));
  }
  return map;
}

/**
 * 归约状态 -> 组件显示数据(§6.5 规则表)。
 *
 * 返回 {visible, state, label, names, parallel, unmapped}:
 * - names 大字号逐行展示;label 是小标题(空串则隐藏 label 元素);
 * - unmapped 是 Schema 里找不到的 node_id(降级显示 node_id 并告警,§5.3);
 * - 并行:多个 running/retrying 节点同时展示,绝不互相覆盖(§10.3 红线 3);
 * - store_error / 503:显示"暂不可用",不沿用旧节点名称冒充(§8/§12.9)。
 */
export function deriveIndicatorView(monitor, nameById, { storeError = false, linkStatus = null } = {}) {
  if (storeError) {
    return { visible: true, state: INDICATOR_STATE.UNAVAILABLE, label: "", names: [TEXT_UNAVAILABLE], parallel: false, unmapped: [] };
  }
  const runState = monitor && monitor.run ? monitor.run.state : "no_events";
  if (runState === "completed") {
    return { visible: true, state: INDICATOR_STATE.DONE, label: "", names: [TEXT_DONE], parallel: false, unmapped: [] };
  }
  if (runState === "failed") {
    return { visible: true, state: INDICATOR_STATE.FAILED, label: "", names: [TEXT_FAILED], parallel: false, unmapped: [] };
  }

  const nodes = monitor && monitor.nodes ? Object.values(monitor.nodes) : [];
  const actives = nodes.filter((n) => n && (n.status === "running" || n.status === "retrying"));
  const waitings = nodes.filter((n) => n && n.status === "waiting");

  if (actives.length > 0) {
    const anyRetrying = actives.some((n) => n.status === "retrying");
    const mapped = actives.map((n) => mapName(nameById, n.id));
    return {
      visible: true,
      state: anyRetrying ? INDICATOR_STATE.RETRYING : INDICATOR_STATE.RUNNING,
      label: LABEL_RUNNING + (mapped.length > 1 ? LABEL_PARALLEL_SUFFIX : ""),
      names: mapped.map((m) => m.name),
      parallel: mapped.length > 1,
      unmapped: mapped.filter((m) => !m.mapped).map((m) => m.id),
    };
  }
  if (waitings.length > 0) {
    const mapped = waitings.map((n) => mapName(nameById, n.id));
    return {
      visible: true,
      state: INDICATOR_STATE.WAITING,
      label: LABEL_WAITING,
      names: mapped.map((m) => m.name),
      parallel: mapped.length > 1,
      unmapped: mapped.filter((m) => !m.mapped).map((m) => m.id),
    };
  }
  if (runState === "waiting_human") {
    // run.interrupted 但没有 waiting 节点事件:只显示等待状态文本
    return { visible: true, state: INDICATOR_STATE.WAITING, label: "", names: [LABEL_WAITING], parallel: false, unmapped: [] };
  }
  // 无任何事件(刚订阅、首个事件未到):用关联表状态兜底,避免闪"运行中"空行
  if (runState === "no_events" && linkStatus) {
    if (linkStatus === "completed") {
      return { visible: true, state: INDICATOR_STATE.DONE, label: "", names: [TEXT_DONE], parallel: false, unmapped: [] };
    }
    if (linkStatus === "failed") {
      return { visible: true, state: INDICATOR_STATE.FAILED, label: "", names: [TEXT_FAILED], parallel: false, unmapped: [] };
    }
    if (linkStatus === "waiting_human") {
      return { visible: true, state: INDICATOR_STATE.WAITING, label: "", names: [LABEL_WAITING], parallel: false, unmapped: [] };
    }
  }
  return { visible: true, state: INDICATOR_STATE.RUNNING, label: LABEL_RUNNING, names: [], parallel: false, unmapped: [] };
}

function mapName(nameById, nodeId) {
  if (nameById && nameById.has(String(nodeId))) {
    return { id: String(nodeId), name: String(nameById.get(String(nodeId))), mapped: true };
  }
  return { id: String(nodeId), name: String(nodeId), mapped: false }; // §5.3 降级
}

// ---------------------------------------------------------------------------
// 副作用层:关联查询 + SSE 生命周期 + DOM 渲染
// ---------------------------------------------------------------------------

export class WorkflowChatIndicator {
  /**
   * @param {object} opts
   * @param {Element} opts.chatEl            #chat 容器(消息行的挂载点)
   * @param {Function} [opts.getTurnAnchor]  () => 当前轮 Assistant 气泡 wrap(定位用)
   * @param {Document} [opts.doc]            可注入(单测)
   * @param {Function} [opts.fetchImpl]      可注入(单测)
   * @param {Function} [opts.EventSourceImpl] 可注入(单测)
   * @param {Function} [opts.scheduleTimer]  可注入(单测)
   * @param {Function} [opts.cancelTimer]    可注入(单测)
   * @param {object}   [opts.log]            console 形状
   * @param {number}   [opts.linkRetryDelayMs] 关联重试间隔(§6.6 有限间隔)
   * @param {number}   [opts.linkRetryMax]   关联重试次数上限
   * @param {number}   [opts.linkThrottleMs] 关联查询节流窗口(onTurnActivity 高频触发防抖)
   */
  constructor(opts = {}) {
    this._chatEl = opts.chatEl || null;
    this._getTurnAnchor = typeof opts.getTurnAnchor === "function" ? opts.getTurnAnchor : () => null;
    this._doc = opts.doc || (typeof document !== "undefined" ? document : null);
    this._fetchImpl = opts.fetchImpl || ((...a) => fetch(...a));
    this._EventSourceImpl = opts.EventSourceImpl
      || (typeof EventSource !== "undefined" ? EventSource : null);
    this._scheduleTimer = opts.scheduleTimer || ((fn, ms) => setTimeout(fn, ms));
    this._cancelTimer = opts.cancelTimer || ((t) => clearTimeout(t));
    this._log = opts.log || console;
    this._linkRetryDelayMs = Number.isFinite(opts.linkRetryDelayMs) ? opts.linkRetryDelayMs : 2000;
    this._linkRetryMax = Number.isFinite(opts.linkRetryMax) ? opts.linkRetryMax : 30;
    this._linkThrottleMs = Number.isFinite(opts.linkThrottleMs) ? opts.linkThrottleMs : 1000;

    this._sessionId = null;
    this._threadId = null;
    this._linkStatus = null;      // 关联表 status 原样(running/waiting_human/completed/failed)
    this._storeError = false;     // 关联查询 503 / SSE store_error -> "暂不可用"
    this._monitor = createMonitorState();
    this._source = null;
    this._schemaKey = null;
    this._schemaMap = null;

    this._row = null;
    this._indicatorEl = null;
    this._labelEl = null;
    this._namesEl = null;

    this._retryTimer = null;
    this._retryAttempts = 0;
    this._retryAllowed = false;
    this._linkInFlight = false;
    this._lastLinkQueryAt = 0;
    this._lastUnmappedWarnAt = 0;
    this._generation = 0; // 会话/thread 切换代际:作废在途 Schema 拉取
    this._disposed = false;
  }

  // ---- 生命周期入口(app.js 挂接点;§6.6) -------------------------------

  /**
   * 当前聊天会话变化(页面加载 / 切换会话 / 新会话)。同会话幂等;
   * 换会话先关旧 EventSource、取消旧查询任务、清空旧节点状态(§6.6/红线 8)。
   */
  onSessionChanged(sessionId) {
    const sid = String(sessionId || "").trim();
    if (this._disposed || !sid) return;
    if (sid === this._sessionId) return; // 快照反复到达:幂等,不重查
    this._teardown();
    this._sessionId = sid;
    this._refreshLink({ allowRetry: false });
  }

  /** 一轮 Assistant 回复开始(assistant.start):组件就位 + 刷新关联(§6.6)。 */
  onAssistantTurnStart() {
    if (this._disposed) return;
    this._retryAttempts = 0;
    this._retryAllowed = true;
    this._placeRow({ force: true });
    this._refreshLink({ allowRetry: true });
  }

  /** 一轮 Assistant 回复结束(assistant.end):停止本轮关联重试。 */
  onAssistantTurnEnd() {
    if (this._disposed) return;
    this._retryAllowed = false;
    this._cancelRetryTimer();
  }

  /**
   * 快照同步出 turn 运行状态(恢复轮询/页面刷新场景):turn 在跑而关联尚未
   * 建立时,本轮期间有限间隔重试(§6.6);已订阅到 thread 时不再轮询。
   */
  onTurnActivity(turnRunning) {
    if (this._disposed) return;
    if (turnRunning) {
      this._retryAllowed = true;
      if (!this._threadId) this._refreshLink({ allowRetry: true });
    } else {
      this._retryAllowed = false;
      this._cancelRetryTimer();
    }
  }

  /** 快照(含历史重放)应用完毕:clearAll 可能清掉了组件行,按需重新挂载。 */
  onSnapshotApplied() {
    if (this._disposed) return;
    this._render();
  }

  /** chat.cleared:同 onSnapshotApplied(行被 ui.clearAll() 清掉后重挂)。 */
  onChatCleared() {
    this.onSnapshotApplied();
  }

  /** 彻底销毁(页面卸载/控制器销毁时)。 */
  destroy() {
    this._disposed = true;
    this._retryAllowed = false;
    this._closeSource();
    this._cancelRetryTimer();
    this._detachRow();
  }

  // ---- 内部:会话/thread 状态重置 ----------------------------------------

  _teardown() {
    this._generation += 1;
    this._closeSource();
    this._cancelRetryTimer();
    this._threadId = null;
    this._monitor = createMonitorState();
    this._linkStatus = null;
    this._storeError = false;
    this._schemaKey = null;
    this._schemaMap = null;
    this._retryAttempts = 0;
    this._retryAllowed = false;
    this._detachRow();
  }

  // ---- 关联查询(§5.1) --------------------------------------------------

  async _refreshLink({ allowRetry = false } = {}) {
    if (this._disposed || !this._sessionId || this._linkInFlight) {
      // 在途合流:保一条重试活口,避免 turn 开始撞上在途查询后重试链断掉
      if (allowRetry && !this._disposed) this._scheduleRetry();
      return;
    }
    // 节流:onTurnActivity 随恢复轮询高频触发,窗口内不重复发查询
    const now = Date.now();
    if (now - this._lastLinkQueryAt < this._linkThrottleMs) {
      if (allowRetry) this._scheduleRetry();
      return;
    }
    this._lastLinkQueryAt = now;
    this._linkInFlight = true;
    const sid = this._sessionId;
    let outcome = null;
    try {
      const url = `/api/workflow/chat-sessions/${encodeURIComponent(sid)}/run`;
      const res = await this._fetchImpl(url, { headers: { Accept: "application/json" } });
      if (this._disposed || sid !== this._sessionId) return;

      if (res.status === 400) {
        // 会话 ID 非法:不订阅(§5.1);正常页面不会走到这里
        this._log.warn("[wf-indicator] chat link 400(会话 ID 非法),隐藏组件");
        this._dropThread();
        outcome = "none";
      } else if (res.status === 503 || !res.ok) {
        // 关联存储不可用:显式"暂不可用",不得当成"无关联"(§5.1/§12.9)
        this._log.warn(`[wf-indicator] chat link HTTP ${res.status},显示暂不可用`);
        this._storeError = true;
        this._render();
        outcome = "unavailable";
      } else {
        const body = await res.json().catch(() => null);
        if (this._disposed || sid !== this._sessionId) return;
        const run = body && body.available && body.thread_id ? body : null;
        if (run) {
          this._storeError = false;
          this._linkStatus = String(run.status || "") || null;
          this._adoptThread(String(run.thread_id));
          outcome = "found";
        } else {
          // 尚无关联:隐藏组件(§5.1),不是错误(TC-13)
          this._dropThread();
          outcome = "none";
        }
      }
    } catch (err) {
      if (this._disposed || sid !== this._sessionId) return;
      this._log.warn("[wf-indicator] chat link query failed:", err);
      this._storeError = true;
      this._render();
      outcome = "unavailable";
    } finally {
      this._linkInFlight = false;
    }
    if (allowRetry && outcome !== "found") this._scheduleRetry();
  }

  _scheduleRetry() {
    if (this._disposed || this._retryTimer || !this._retryAllowed) return;
    if (this._retryAttempts >= this._linkRetryMax) {
      this._log.warn(
        `[wf-indicator] 关联查询重试 ${this._retryAttempts} 次未果,停止(会话 ${this._sessionId})`,
      );
      return;
    }
    this._retryAttempts += 1;
    this._retryTimer = this._scheduleTimer(() => {
      this._retryTimer = null;
      if (this._disposed || !this._retryAllowed) return;
      this._refreshLink({ allowRetry: true });
    }, this._linkRetryDelayMs);
  }

  _cancelRetryTimer() {
    if (this._retryTimer) {
      this._cancelTimer(this._retryTimer);
      this._retryTimer = null;
    }
  }

  // ---- thread 采纳 / 丢弃 / 订阅 ----------------------------------------

  _adoptThread(threadId) {
    if (this._threadId === threadId && this._source) {
      this._render(); // 同线程:reducer 状态保留(游标去重),只刷新显示
      return;
    }
    // 新 thread:先关旧 EventSource、重置 reducer、Schema 重拉(§6.6/红线 8;
    // human/auto 模式、schema 演进的映射隔离,§5.3)
    this._closeSource();
    this._generation += 1;
    const generation = this._generation;
    this._threadId = threadId;
    this._monitor = createMonitorState();
    this._schemaKey = null;
    this._schemaMap = null;
    void this._ensureSchema().then(() => {
      if (!this._disposed && this._generation === generation) this._render();
    });
    this._render();
    this._subscribe(threadId);
  }

  _dropThread() {
    this._closeSource();
    this._threadId = null;
    this._monitor = createMonitorState();
    this._linkStatus = null;
    this._storeError = false;
    this._schemaKey = null;
    this._schemaMap = null;
    this._render(); // _view() 判定无关联 -> 隐藏整个组件(TC-13)
  }

  _subscribe(threadId) {
    if (!this._EventSourceImpl) {
      this._log.warn("[wf-indicator] EventSource 不可用,节点实时层未启用");
      return;
    }
    const url = API_PATHS.runEvents(threadId);
    let source;
    try {
      source = new this._EventSourceImpl(url);
    } catch (err) {
      this._log.warn("[wf-indicator] EventSource 创建失败:", err);
      return;
    }
    this._source = source;
    for (const t of INDICATOR_EVENT_TYPES) {
      source.addEventListener(t, (esEvent) => this._handleSseEvent(esEvent));
    }
    // onopen/onerror 不改显示:EventSource 原生重连(自动带 Last-Event-ID),
    // 补发重叠由 reducer 游标去重(§8);store_error 帧负责"暂不可用"切换。
  }

  _closeSource() {
    if (this._source) {
      try { this._source.close(); } catch { /* 已关闭 */ }
      this._source = null;
    }
  }

  _handleSseEvent(esEvent) {
    if (this._disposed || !this._monitor) return;
    const type = String((esEvent && esEvent.type) || "");
    let data = {};
    try { data = JSON.parse(esEvent.data || "{}"); } catch { data = {}; }

    if (type === "snapshot") {
      // 快照摘要帧:冷启动基线(随后的历史补发覆盖细节);连接确认 -> 清"暂不可用"
      this._storeError = false;
      this._render();
      return;
    }
    if (type === "store_error") {
      // 事件存储不可用:显式提示,不沿用旧节点名称冒充(§8/§12.9)
      this._storeError = true;
      this._render();
      return;
    }

    // 与 node_map.html 同一组装方式:id(lastEventId 游标)+ event 行 + data 平铺
    const event = {
      id: esEvent.lastEventId ? Number(esEvent.lastEventId) : null,
      event_type: type,
      ...data,
    };
    const { applied } = applyEvent(this._monitor, event);
    if (!applied) return; // Last-Event-ID 补发重叠:去重,不重复更新 UI(§6.4)
    this._storeError = false;
    this._render();
  }

  // ---- Schema(§5.3) -----------------------------------------------------

  async _ensureSchema() {
    if (this._schemaMap) return this._schemaMap;
    const generation = this._generation;
    try {
      const res = await this._fetchImpl(API_PATHS.graph, { headers: { Accept: "application/json" } });
      if (this._disposed || generation !== this._generation) return this._schemaMap;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const schema = await res.json();
      if (this._disposed || generation !== this._generation) return this._schemaMap;
      const key = graphSchemaKey(schema);
      if (this._schemaKey === key && this._schemaMap) return this._schemaMap;
      this._schemaKey = key;
      this._schemaMap = graphNodeNameMap(schema);
    } catch (err) {
      // §5.3:Schema 拉取失败不崩 —— 降级显示原始 node_id,下次采纳 thread 再试
      this._log.warn("[wf-indicator] graph schema 拉取失败,节点名降级为 node_id:", err);
    }
    return this._schemaMap;
  }

  // ---- 显示(§6.5)与 DOM(§6.1) -----------------------------------------

  _view() {
    const view = deriveIndicatorView(this._monitor, this._schemaMap, {
      storeError: this._storeError,
      linkStatus: this._linkStatus,
    });
    // 会话尚无关联工作流(且存储未报不可用)-> 隐藏整个组件(§6.5 末行/TC-13)
    if (!this._threadId && !this._storeError) return { ...view, visible: false };
    return view;
  }

  _render() {
    if (this._disposed || !this._doc || !this._chatEl) return;
    const view = this._view();
    if (!view.visible) {
      this._detachRow();
      return;
    }
    if (view.unmapped.length && this._schemaMap && this._schemaMap.size > 0) {
      const now = Date.now();
      if (now - this._lastUnmappedWarnAt > 5000) {
        this._lastUnmappedWarnAt = now;
        this._log.warn("[wf-indicator] Schema 缺节点名,降级显示 node_id:", view.unmapped);
      }
    }
    this._ensureRow();
    this._indicatorEl.dataset.state = view.state;
    if (view.label) {
      this._labelEl.textContent = view.label;
      this._labelEl.hidden = false;
    } else {
      this._labelEl.textContent = "";
      this._labelEl.hidden = true;
    }
    this._namesEl.replaceChildren();
    for (const name of view.names) {
      const el = this._doc.createElement("div");
      el.className = "workflow-node-indicator__name";
      el.textContent = name;
      this._namesEl.appendChild(el);
    }
    this._placeRow();
  }

  _ensureRow() {
    if (this._row) return;
    const doc = this._doc;
    this._row = doc.createElement("div");
    this._row.className = "msg assistant workflow-node-msg";
    this._row.setAttribute("role", "status");
    this._row.setAttribute("aria-live", "polite");
    this._indicatorEl = doc.createElement("div");
    this._indicatorEl.className = "workflow-node-indicator";
    this._labelEl = doc.createElement("div");
    this._labelEl.className = "workflow-node-indicator__label";
    this._namesEl = doc.createElement("div");
    this._namesEl.className = "workflow-node-indicator__names";
    this._indicatorEl.appendChild(this._labelEl);
    this._indicatorEl.appendChild(this._namesEl);
    this._row.appendChild(this._indicatorEl);
  }

  /**
   * 组件行就位。force=true(turn 开始)时移动到当前轮锚点(MCode 面板后、
   * 正文气泡前,§6.1/§6.7);否则只在行脱离 DOM(clearAll/历史重放)时重新挂载,
   * 避免轮内正文分段时组件行来回跳动(§12.6)。
   */
  _placeRow({ force = false } = {}) {
    if (!this._row || this._disposed || !this._chatEl) return;
    const anchor = this._getTurnAnchor();
    if (force && anchor && anchor.parentNode === this._chatEl) {
      if (this._row.nextSibling !== anchor) this._chatEl.insertBefore(this._row, anchor);
      return;
    }
    if (this._row.parentNode !== this._chatEl) {
      if (anchor && anchor.parentNode === this._chatEl) this._chatEl.insertBefore(this._row, anchor);
      else this._chatEl.appendChild(this._row);
    }
  }

  _detachRow() {
    if (this._row && this._row.parentNode) {
      this._row.parentNode.removeChild(this._row);
    }
  }
}

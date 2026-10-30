// /static/mcode_panel.mjs
//
// 阶段 E —— MCode「执行过程」面板的**纯策略层**。
//
// 这里刻意不碰 DOM:所有「哪些事件进面板」「哪些事件进正文」
// 「怎么去重排序」「面板长什么样(数据层面)」都写成一个纯函数 / 纯 reducer,
// 于是可以:
//
//   1. 在 Node 里直接跑单测(不需要浏览器、不需要 playwright);
//   2. 让「实时流」和「刷新后恢复」走**同一条**代码路径 ——
//      实时是 applyEventToPanelModel(逐条喂事件),
//      恢复是 buildPanelModel(拿 trace 一次性重建);
//
// 两条路径共用同一个模型 ⇒ 恢复出来的面板和实时看到的面板**长得一样**。
//
// 协议依据:设计文档 §7(WebSocket 事件协议)/ §8(前端展示设计)。

// -- 常量 ----------------------------------------------------------------

/** MCode 来源标识。与后端 `TURN_SOURCE_MCODE` 对齐。 */
export const MCODE_SOURCE = "mcode";

/** 普通模型来源标识。与后端 `TURN_SOURCE_LLM` 对齐。 */
export const LLM_SOURCE = "llm";

/** 面板标题上显示的来源名(设计文档 §8.1)。 */
export const MCODE_DISPLAY_NAME = "MCode Agent";

/** `status` 枚举(设计文档 §7.2)。 */
export const STATUS_VALUES = Object.freeze([
  "queued",
  "running",
  "waiting_tool",
  "completed",
  "failed",
  "cancelled",
  "timed_out",
]);

/** `phase` 枚举(设计文档 §7.2)。 */
export const PHASE_VALUES = Object.freeze([
  "initializing",
  "analyzing",
  "tool_execution",
  "synthesizing",
  "completed",
]);

/** 终态:到了这些状态就必须关掉 loading。 */
export const TERMINAL_STATUSES = Object.freeze(new Set([
  "completed",
  "failed",
  "cancelled",
  "timed_out",
]));

/** 工具卡片的三个状态(与 `upsertToolCard` 的 state 取值一致)。 */
export const TOOL_STATE_RUNNING = "running";
export const TOOL_STATE_SUCCESS = "success";
export const TOOL_STATE_ERROR = "error";

// -- 来源校验 ------------------------------------------------------------

/** 归一化来源字符串;不是 mcode 就是空串。 */
export function normalizeSource(value) {
  const s = String(value ?? "").trim().toLowerCase();
  if (s === MCODE_SOURCE) return MCODE_SOURCE;
  if (!s) return "";
  return s;
}

/** 这条事件是不是 MCode 来源。 */
export function isMcodeSource(value) {
  return normalizeSource(value) === MCODE_SOURCE;
}

/**
 * 当前 turn 能不能收下这条事件(设计文档 §8.2 第 2 条)。
 *
 * - MCode turn:**只**收 `source=mcode` 的事件 —— 文本与过程都只能来自 MCode 通道;
 * - 普通 turn:凡是自称 mcode 的都拒 —— 普通模型不许出现「MCode Agent」面板。
 *
 * 普通 turn 下没有 source 字段的老事件照样放行(零回归)。
 */
export function shouldAcceptEvent({ turnSource, eventSource } = {}) {
  const t = normalizeSource(turnSource) || LLM_SOURCE;
  const e = normalizeSource(eventSource);
  if (t === MCODE_SOURCE) return e === MCODE_SOURCE;
  return e !== MCODE_SOURCE;
}

// -- run_id + seq 去重 / 排序 --------------------------------------------

/**
 * 一轮 turn 的事件闸门:按 `run_id + seq` 去重,并挡住乱序 / 换 run 的事件。
 *
 * 没有 seq 的事件(``assistant.start`` / ``assistant.end`` / ``error`` 这类
 * 收尾事件)直接放行 —— 它们不参与过程事件的排序。
 */
export class TurnEventGate {
  constructor() {
    this.reset();
  }

  /** 换一轮就重置。 */
  reset() {
    this._seen = new Set();
    this._lastSeq = -1;
    this._runId = "";
  }

  /**
   * 判断一条事件要不要处理。
   * @returns {{ok: boolean, reason: string}}
   */
  accept(data) {
    if (!data || typeof data !== "object") {
      return { ok: false, reason: "malformed" };
    }

    const runId = String(data.run_id ?? "");
    if (runId) {
      if (this._runId && runId !== this._runId) {
        return { ok: false, reason: "run_id_mismatch" };
      }
      if (!this._runId) this._runId = runId;
    }

    const rawSeq = data.seq;
    const seq = Number.isFinite(rawSeq) ? Number(rawSeq) : null;
    if (seq === null) {
      return { ok: true, reason: "no_seq" };
    }

    const key = `${this._runId}|${runId}|${seq}`;
    if (this._seen.has(key)) {
      return { ok: false, reason: "duplicate" };
    }
    if (seq <= this._lastSeq) {
      return { ok: false, reason: "out_of_order" };
    }

    this._seen.add(key);
    this._lastSeq = seq;
    return { ok: true, reason: "accepted" };
  }
}

// -- 文案 key ------------------------------------------------------------

/** 状态 -> i18n key。未知状态退回 running。 */
export function statusI18nKey(status) {
  const s = String(status ?? "").trim().toLowerCase();
  return STATUS_VALUES.includes(s) ? `mcode.status.${s}` : "mcode.status.running";
}

/** 阶段 -> i18n key。没有阶段就是空串(不显示)。 */
export function phaseI18nKey(phase) {
  const p = String(phase ?? "").trim().toLowerCase();
  return PHASE_VALUES.includes(p) ? `mcode.phase.${p}` : "";
}

/** 状态是不是终态(该关 loading 了)。 */
export function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(String(status ?? "").trim().toLowerCase());
}

/** 步骤 / 工具的图标。 */
export function statusIcon(status) {
  const s = String(status ?? "").trim().toLowerCase();
  if (s === "completed" || s === "success") return "✓";
  if (s === "failed" || s === "error") return "✕";
  if (s === "cancelled") return "⊘";
  if (s === "timed_out") return "⏱";
  if (s === "waiting_tool") return "◌";
  if (s === "queued") return "○";
  return "●";
}

// -- 事件路由 ------------------------------------------------------------

//: 路由目标:助手答复气泡。
export const ROUTE_BUBBLE = "bubble";
//: 路由目标:MCode 执行过程面板。
export const ROUTE_PANEL = "panel";
//: 路由目标:聊天正文(普通模型的工具卡片留在这里)。
export const ROUTE_BODY = "body";

/**
 * 一条 WebSocket 事件该去哪儿(设计文档 §8.2 第 1/4/5 条)。
 *
 * 这是**唯一**一份路由规则 —— app.js 按它分发,单测也按它断言,
 * 所以「测试通过」= 「页面行为正确」,不会出现测的是一份、跑的是另一份。
 *
 * @returns {string[]} 路由目标列表(可能为空 = 忽略这条事件)。
 */
export function routeEvent({ type, turnSource, eventSource } = {}) {
  const t = normalizeSource(turnSource) || LLM_SOURCE;
  const e = normalizeSource(eventSource);
  const mcode = t === MCODE_SOURCE;
  const sourceOk = shouldAcceptEvent({ turnSource: t, eventSource: e });
  const routes = [];

  switch (String(type || "")) {
    case "assistant.flush":
      routes.push(ROUTE_BUBBLE);
      break;

    case "assistant.delta":
      // 文本**只**进助手气泡;来源对不上直接丢。
      if (sourceOk) routes.push(ROUTE_BUBBLE);
      break;

    case "assistant.end":
      routes.push(ROUTE_BUBBLE);
      if (mcode) routes.push(ROUTE_PANEL);
      break;

    case "agent.status":
    case "agent.step":
      // 过程事件只进面板;普通 turn 一律忽略(SRC-03)。
      if (mcode && sourceOk) routes.push(ROUTE_PANEL);
      break;

    case "tool.start":
    case "tool.progress":
    case "tool.end":
      if (mcode) {
        // MCode 的工具卡片在面板里。
        if (sourceOk) routes.push(ROUTE_PANEL);
      } else {
        // 普通模型的工具卡片留在聊天正文(零回归)。
        routes.push(ROUTE_BODY);
      }
      break;

    case "error":
      routes.push(ROUTE_BUBBLE);
      if (mcode) routes.push(ROUTE_PANEL);
      break;

    default:
      break;
  }

  return routes;
}

// -- 面板模型 ------------------------------------------------------------

/** 空面板模型。 */
export function createPanelModel() {
  return {
    source: "",
    turnId: "",
    status: "",
    phase: "",
    message: "",
    steps: [],
    tools: [],
  };
}

function _cloneModel(model) {
  return {
    source: model.source,
    turnId: model.turnId,
    status: model.status,
    phase: model.phase,
    message: model.message,
    steps: model.steps.map((s) => ({ ...s })),
    tools: model.tools.map((t) => ({ ...t })),
  };
}

function _findStep(model, id) {
  return model.steps.find((s) => s.id === id) || null;
}

function _upsertStep(model, step) {
  const existing = _findStep(model, step.id);
  if (existing) {
    Object.assign(existing, step);
    return existing;
  }
  model.steps.push(step);
  return step;
}

function _upsertTool(model, patch) {
  const id = String(patch.tool_call_id ?? "");
  let tool = model.tools.find((t) => t.tool_call_id === id);
  if (!tool) {
    tool = {
      tool_call_id: id,
      server: "",
      name: "",
      state: TOOL_STATE_RUNNING,
      args: undefined,
      summary: null,
      message: "",
      progress: 0,
    };
    model.tools.push(tool);
  }
  for (const key of ["server", "name", "state", "args", "summary", "message", "progress"]) {
    if (patch[key] !== undefined) tool[key] = patch[key];
  }
  return tool;
}

/**
 * 把一条 WebSocket 事件应用到面板模型上(纯 reducer)。
 *
 * 返回**新的**模型对象(不原地改),调用方自己决定要不要重新渲染。
 *
 * @param {object} model 当前模型(createPanelModel() 起步)
 * @param {string} type  WebSocket 事件类型
 * @param {object} data  事件负载
 */
export function applyEventToPanelModel(model, type, data) {
  const next = _cloneModel(model);
  const d = data && typeof data === "object" ? data : {};

  switch (String(type || "")) {
    case "assistant.start": {
      next.turnId = String(d.turn_id ?? next.turnId);
      next.source = normalizeSource(d.source) || next.source;
      return next;
    }

    case "agent.status": {
      const status = String(d.status ?? "").trim().toLowerCase();
      const phase = String(d.phase ?? "").trim().toLowerCase();
      const message = String(d.message ?? "");
      if (status) next.status = status;
      if (phase) next.phase = phase;
      if (message) next.message = message;
      if (d.source) next.source = normalizeSource(d.source) || next.source;
      // 状态事件**合并去重**:同一个 phase 只留一条,phase 变了才新增一行。
      const stepId = `phase:${phase || next.phase || "default"}`;
      const existing = _findStep(next, stepId);
      if (existing) {
        existing.status = status || existing.status;
        if (message) existing.text = message;
      } else {
        _upsertStep(next, {
          id: stepId,
          kind: "status",
          text: message,
          status: status || "running",
          phase,
        });
      }
      return next;
    }

    case "agent.step": {
      const stepId = String(d.step_id ?? "");
      const id = stepId ? `step:${stepId}` : `step:${next.steps.length}`;
      _upsertStep(next, {
        id,
        kind: "step",
        text: String(d.title ?? d.message ?? ""),
        status: String(d.status ?? "running").trim().toLowerCase(),
        phase: String(d.phase ?? ""),
      });
      if (d.source) next.source = normalizeSource(d.source) || next.source;
      return next;
    }

    case "tool.start": {
      const tool = _upsertTool(next, {
        tool_call_id: d.tool_call_id,
        server: d.server,
        name: d.name,
        state: TOOL_STATE_RUNNING,
        args: d.args,
        message: "",
        progress: 0,
      });
      _upsertStep(next, {
        id: `tool:${tool.tool_call_id}`,
        kind: "tool",
        text: String(d.name || ""),
        status: "running",
        phase: "tool_execution",
      });
      if (d.source) next.source = normalizeSource(d.source) || next.source;
      return next;
    }

    case "tool.progress": {
      const tool = _upsertTool(next, {
        tool_call_id: d.tool_call_id,
        server: d.server,
        name: d.name,
        message: String(d.message ?? ""),
        progress: Number.isFinite(d.progress) ? d.progress : 0,
      });
      _upsertStep(next, {
        id: `tool:${tool.tool_call_id}`,
        kind: "tool",
        text: String(d.name || ""),
        status: "running",
        phase: "tool_execution",
      });
      if (d.source) next.source = normalizeSource(d.source) || next.source;
      return next;
    }

    case "tool.end": {
      const isError = !!d.is_error;
      const tool = _upsertTool(next, {
        tool_call_id: d.tool_call_id,
        server: d.server,
        name: d.name,
        state: isError ? TOOL_STATE_ERROR : TOOL_STATE_SUCCESS,
        summary: d.summary,
        progress: 1,
      });
      _upsertStep(next, {
        id: `tool:${tool.tool_call_id}`,
        kind: "tool",
        text: String(d.name || ""),
        status: isError ? "error" : "completed",
        phase: "tool_execution",
      });
      if (d.source) next.source = normalizeSource(d.source) || next.source;
      return next;
    }

    case "error": {
      next.status = "failed";
      next.message = String(d.message ?? next.message);
      _upsertStep(next, {
        id: `error:${next.steps.length}`,
        kind: "error",
        text: String(d.message ?? ""),
        status: "failed",
        phase: next.phase,
      });
      return next;
    }

    case "assistant.end": {
      if (d.status) {
        next.status = String(d.status).trim().toLowerCase();
      } else if (next.status && !isTerminalStatus(next.status)) {
        next.status = "completed";
      }
      if (d.source) next.source = normalizeSource(d.source) || next.source;
      return next;
    }

    default:
      return next;
  }
}

/**
 * 从 trace 记录重建面板模型(刷新 / 重连后用)。
 *
 * trace 是**过程事件**的集合 —— 不含 assistant 正文,
 * 所以恢复出来的面板**不会**往聊天正文里插一句话(设计文档 §8.2 第 8 条 / WS-07)。
 */
export function buildPanelModel(trace, opts = {}) {
  let model = createPanelModel();
  const rows = Array.isArray(trace) ? trace : [];
  const turnId = String(opts.turnId ?? "");

  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    if (turnId && String(row.turn_id ?? "") !== turnId) continue;

    const type = String(row.type ?? "");
    switch (type) {
      case "agent.status":
        model = applyEventToPanelModel(model, "agent.status", row);
        break;
      case "agent.step":
        model = applyEventToPanelModel(model, "agent.step", row);
        break;
      case "tool.start":
        model = applyEventToPanelModel(model, "tool.start", row);
        break;
      case "tool.progress":
        model = applyEventToPanelModel(model, "tool.progress", row);
        break;
      case "tool.end":
        model = applyEventToPanelModel(model, "tool.end", row);
        break;
      default:
        break;
    }
  }

  if (model.status && !isTerminalStatus(model.status)) {
    // trace 是快照,不是实时流 —— 恢复出来的面板不再「运行中」。
    model.status = "completed";
  }
  return model;
}

/** trace 里最近一轮的 turn_id(没有就空串)。 */
export function latestTraceTurnId(trace) {
  const rows = Array.isArray(trace) ? trace : [];
  let last = "";
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const tid = String(row.turn_id ?? "");
    if (tid) last = tid;
  }
  return last;
}

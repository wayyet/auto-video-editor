// /static/workflow_console.mjs
//
// 工作流创建台(/console)的**纯逻辑层**(第二期阶段 3,无后缀版计划 §11 阶段 3)。
// 与 /static/workflow_monitor.mjs 同一惯例:不碰 DOM、不 fetch、不依赖浏览器全局,
// Node 里可直接单测(web/tests/workflow_console.test.mjs);DOM/XHR 接线全部在
// web/console.html 的模块脚本里完成。
//
// 职责:
//   1. 表单文件列表的客户端预校验(扩展名/数量/大小)与增删;
//   2. 提交参数校验 + multipart FormData 构建(字段名与 POST /api/console/runs 契约一致);
//   3. 提交状态机(idle → uploading → live / 失败回 idle,可重试);
//   4. 错误码 → 稳定可读提示(不显示堆栈与敏感路径,设计 §8.1);
//   5. 复用监控 reducer:re-export workflow_monitor.mjs 的事件归约与 API 路径,
//      创建成功后页面用同一套 applyEvent 消费既有 SSE。
//
// 边界:SSE 订阅/进度条/时长探测(video 元素)属浏览器行为,由页面脚本注入;
// 运行状态(running/waiting_human/completed/failed)一律来自事件归约,本模块不猜测。

import {
  API_PATHS, applyEvent, createMonitorState, computeCounts, deriveRunState,
  NODE_STATUS, RUN_STATE, eventFromSse,
} from "./workflow_monitor.mjs";

export {
  API_PATHS, applyEvent, createMonitorState, computeCounts, deriveRunState,
  NODE_STATUS, RUN_STATE, eventFromSse,
};

// ---------------------------------------------------------------------------
// Console API 路径(与 web/api_console.py 路由一一对应)
// ---------------------------------------------------------------------------

export const CONSOLE_API_PATHS = Object.freeze({
  config: "/api/console/config",
  csrf: "/api/console/csrf",
  createRun: "/api/console/runs",
  // 阶段 5:历史 / 详情 / 恢复 / 取消
  listRuns: "/api/console/runs",
  runDetail: (threadId) => `/api/console/runs/${encodeURIComponent(threadId)}`,
  resumeRun: (threadId) => `/api/console/runs/${encodeURIComponent(threadId)}/resume`,
  cancelRun: (threadId) => `/api/console/runs/${encodeURIComponent(threadId)}/cancel`,
  // 2026-11-07:历史任务行内删除(终态/未启动;任务目录+PG事件,不可恢复)
  deleteRun: (threadId) => `/api/console/runs/${encodeURIComponent(threadId)}/delete`,
  // 2026-11-06:清除可再生缓存(维护入口)
  clearCache: "/api/console/cache/clear",
  // 只读探针:人工关卡恢复指引
  resumeStatus: (threadId) => `/api/workflow/runs/${encodeURIComponent(threadId)}/resume-status`,
  // 2026-11-06 阶段 2(人工关卡与剪映草稿导入):真实 payload 展示 + 打开草稿目录
  gates: (threadId) => `/api/console/runs/${encodeURIComponent(threadId)}/gates`,
  openDraftDir: (threadId) => `/api/console/runs/${encodeURIComponent(threadId)}/open-draft-dir`,
  // 监控页跳转(创建成功后"查看完整监控")
  monitorPage: (threadId) => `/?thread_id=${encodeURIComponent(threadId)}`,
});

// ---------------------------------------------------------------------------
// Agent 智能体白名单(与后端 web/console_service.py CONSOLE_FORM_BACKENDS
// 冻结契约一致;stub 永不可由浏览器指定)
// ---------------------------------------------------------------------------

export const CONSOLE_BACKENDS = Object.freeze(["claude-cli", "mcode-cli"]);

// ---------------------------------------------------------------------------
// 客户端限制默认值(config 端点不可用时的兜底;与服务端 config.py 默认一致)
// ---------------------------------------------------------------------------

export const DEFAULT_LIMITS = Object.freeze({
  max_videos: 10,
  max_video_bytes: 2 * 1024 ** 3,
  prompt_max_chars: 4000,
  target_duration_ms_default: 35000,
  target_duration_ms_max: 600000,
  speed_multiplier_default: 1,
  speed_multiplier_max: 4,
  allowed_video_exts: [".mp4", ".mov"],
  ratios: ["9:16", "16:9", "4:3"],
  langs: ["zh", "en"],
});

// ---------------------------------------------------------------------------
// 文件列表
// ---------------------------------------------------------------------------

/** 文件条目:{ name, size, ext, durationMs:null, file }。duration 由页面探测后回填。 */
export function makeFileEntry(file) {
  const name = String(file && file.name ? file.name : "");
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  return {
    name,
    size: Number(file && file.size) || 0,
    ext,
    durationMs: null,
    file,
  };
}

function normalizeExt(ext) {
  const e = String(ext || "").trim().toLowerCase();
  if (!e) return "";
  return e.startsWith(".") ? e : `.${e}`;
}

/**
 * 新增文件的客户端预校验(服务端仍会全量校验,这里只做即时反馈)。
 * 返回 { accepted: FileEntry[], errors: string[] }。
 */
export function validateNewFiles(fileList, currentCount, limits) {
  const lim = { ...DEFAULT_LIMITS, ...(limits || {}) };
  const allowed = new Set((lim.allowed_video_exts || []).map(normalizeExt));
  const accepted = [];
  const errors = [];
  const files = Array.from(fileList || []);
  for (const f of files) {
    const entry = makeFileEntry(f);
    if (currentCount + accepted.length >= lim.max_videos) {
      errors.push(`视频数量超过上限(${lim.max_videos}个):已忽略 ${f.name}`);
      continue;
    }
    if (!allowed.has(entry.ext)) {
      errors.push(`格式不支持(${entry.ext || "无扩展名"}):${f.name}`);
      continue;
    }
    if (entry.size <= 0) {
      errors.push(`空文件:${f.name}`);
      continue;
    }
    if (entry.size > lim.max_video_bytes) {
      errors.push(`超过单文件大小上限(${formatBytes(lim.max_video_bytes)}):${f.name}`);
      continue;
    }
    accepted.push(entry);
  }
  return { accepted, errors };
}

/** 按 name+size+lastModified 去重后追加(拖放重复投放同一文件时幂等)。 */
export function addFiles(state, entries) {
  const seen = new Set(state.files.map((f) => `${f.name}::${f.size}::${f.file && f.file.lastModified}`));
  let added = 0;
  for (const entry of entries || []) {
    const key = `${entry.name}::${entry.size}::${entry.file && entry.file.lastModified}`;
    if (seen.has(key)) continue;
    seen.add(key);
    state.files.push(entry);
    added += 1;
  }
  return added;
}

export function removeFile(state, index) {
  if (index < 0 || index >= state.files.length) return false;
  state.files.splice(index, 1);
  return true;
}

// ---------------------------------------------------------------------------
// 参数与提交校验
// ---------------------------------------------------------------------------

/**
 * 校验提交参数;返回错误对象 { prompt, duration, ratio, lang, speed, backend },全空表示通过。
 * params: { prompt, target_duration_ms, ratio, lang, speed_multiplier?, llm_backend? }
 * llm_backend 为空 = 跟随服务端默认(合法);非空必须 ∈ CONSOLE_BACKENDS。
 * speed_multiplier 缺省按 1(自动)处理;非法(非有限数/越界)报 speed 错误。
 */
export function validateParams(params, limits) {
  const lim = { ...DEFAULT_LIMITS, ...(limits || {}) };
  const errors = { prompt: "", duration: "", ratio: "", lang: "", speed: "", backend: "" };
  const prompt = String((params && params.prompt) || "").trim();
  if (!prompt) errors.prompt = "请输入剪辑提示词";
  else if (prompt.length > lim.prompt_max_chars) {
    errors.prompt = `提示词过长(>${lim.prompt_max_chars} 字符)`;
  }
  const dur = Number(params && params.target_duration_ms);
  if (!Number.isInteger(dur) || dur <= 0 || dur > lim.target_duration_ms_max) {
    errors.duration = `目标时长须为 0 < 值 ≤ ${Math.round(lim.target_duration_ms_max / 1000)} 秒`;
  }
  if (!(lim.ratios || []).includes(params && params.ratio)) errors.ratio = "画幅比例非法";
  if (!(lim.langs || []).includes(params && params.lang)) errors.lang = "输出语言非法";
  const rawSpeed = params && params.speed_multiplier;
  const speed =
    rawSpeed === undefined || rawSpeed === null || rawSpeed === ""
      ? lim.speed_multiplier_default
      : Number(rawSpeed);
  if (!Number.isFinite(speed) || speed < lim.speed_multiplier_default || speed > lim.speed_multiplier_max) {
    errors.speed = `快进速度须为 ${lim.speed_multiplier_default} ≤ 值 ≤ ${lim.speed_multiplier_max}`;
  }
  const backend = String((params && params.llm_backend) || "").trim();
  if (backend && !CONSOLE_BACKENDS.includes(backend)) errors.backend = "Agent 智能体非法";
  return errors;
}

export function hasErrors(errors) {
  return Object.values(errors || {}).some(Boolean);
}

/** 是否可提交:空闲态 + 至少一个视频 + 参数全部合法。 */
export function canSubmit(state, params, limits) {
  if (!state || state.phase !== "idle") return false;
  if (!state.files.length) return false;
  return !hasErrors(validateParams(params, limits));
}

// ---------------------------------------------------------------------------
// 提交状态机
// ---------------------------------------------------------------------------

/**
 * phase 取值(设计 §3.3 前四态;running/waiting_human/... 由 SSE 事件归约给出,
 * 存于监控 state.run.state,不进入本状态机):
 *   idle       等待输入(可提交)
 *   uploading  正在上传(进度条活跃,禁止重复提交)
 *   live       已创建(thread_id 就绪,SSE 订阅中)
 */
export function createConsoleState() {
  return {
    phase: "idle",
    files: [],
    progress: { loaded: 0, total: 0 },
    threadId: null,
    error: null,
  };
}

export function beginUpload(state, totalBytes) {
  state.phase = "uploading";
  state.error = null;
  state.progress = { loaded: 0, total: Number(totalBytes) || 0 };
  return state;
}

export function updateProgress(state, loaded, total) {
  if (state.phase !== "uploading") return state;
  state.progress = {
    loaded: Number(loaded) || 0,
    total: Number(total) || state.progress.total,
  };
  return state;
}

/** 202 响应体 {ok, thread_id, state, monitor_url, snapshot_url, events_url}。 */
export function acceptCreated(state, resp) {
  state.phase = "live";
  state.error = null;
  state.threadId = (resp && resp.thread_id) || null;
  return state;
}

/** 提交失败:回到 idle 允许修改重试;error 为 friendlyError 结果。 */
export function failSubmission(state, err) {
  state.phase = "idle";
  state.error = err || { code: 0, message: "创建失败", detail: "" };
  return state;
}

// ---------------------------------------------------------------------------
// FormData 构建(字段名与 POST /api/console/runs 冻结契约一致)
// ---------------------------------------------------------------------------

// 必需功能复选框(阶段 6):Form 字段名与 POST /runs 契约一致,顺序即展示顺序
export const REQUIRED_FEATURE_FIELDS = Object.freeze([
  ["require_subtitles_zh", "subtitles_zh"],
  ["require_subtitles_en", "subtitles_en"],
  ["require_voiceover_zh", "voiceover_zh"],
  ["require_voiceover_en", "voiceover_en"],
  ["require_bgm", "bgm"],
  ["require_transitions", "transitions"],
  ["require_text_animation", "text_animation"],
  ["require_stickers", "stickers"],
  ["require_volume_fades", "volume_fades"],
]);

export function buildCreateFormData(state, params) {
  const fd = new FormData();
  fd.append("prompt", String((params && params.prompt) || "").trim());
  for (const entry of state.files) {
    fd.append("videos", entry.file, entry.name);
  }
  fd.append("target_duration_ms", String(Math.round(Number(params && params.target_duration_ms))));
  fd.append("ratio", String((params && params.ratio) || ""));
  fd.append("lang", String((params && params.lang) || ""));
  // 快进速度(倍)(2026-11-14):恒下发,缺省 1 = 自动;服务端二次校验范围
  fd.append("speed_multiplier", String(Number(params && params.speed_multiplier) || 1));
  // Agent 智能体选择(2026-11-07):非空才下发,保持旧契约向后兼容(缺省 = 服务端默认)
  const backend = String((params && params.llm_backend) || "").trim();
  if (backend) fd.append("llm_backend", backend);
  // 必需功能(阶段 6):全量下发勾选态,服务端写进 required_features
  const required = (params && params.required_features) || {};
  for (const [formKey, featureKey] of REQUIRED_FEATURE_FIELDS) {
    fd.append(formKey, required[featureKey] ? "true" : "false");
  }
  return fd;
}

// ---------------------------------------------------------------------------
// 错误提示与杂项
// ---------------------------------------------------------------------------

const ERROR_HINTS = Object.freeze({
  400: "参数不合法,请检查提示词、视频与参数",
  403: "来源或 CSRF 校验失败,请从本机 /console 页面重新提交",
  409: "并发或重复请求冲突,请稍后再试",
  413: "视频文件超过大小限制",
  415: "视频格式或内容不支持(需真实可解码的 MP4/MOV)",
  503: "PostgreSQL 或真实模型后端未就绪,请先完成服务端配置",
  500: "服务端内部错误,请查看服务日志",
});

/** HTTP 状态 → {code, message, detail};detail 截断,不透出堆栈/敏感路径全文。 */
export function friendlyError(status, detail) {
  const code = Number(status) || 0;
  const hint = ERROR_HINTS[code] || "创建失败,请稍后重试";
  const d = typeof detail === "string" ? detail.slice(0, 200) : "";
  return { code, message: d ? `${hint}:${d}` : hint, detail: d };
}

export function formatBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return "—";
  if (v < 1024) return `${v} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let u = -1;
  let x = v;
  do {
    x /= 1024;
    u += 1;
  } while (x >= 1024 && u < units.length - 1);
  return `${x >= 100 ? x.toFixed(0) : x.toFixed(1)} ${units[u]}`;
}

/** 毫秒时长 → "12.3s";未知返回 "—"。 */
export function formatDuration(ms) {
  const v = Number(ms);
  if (!Number.isFinite(v) || v <= 0) return "—";
  return `${(v / 1000).toFixed(1)}s`;
}

// ---------------------------------------------------------------------------
// 阶段 5:历史列表 / 恢复 / 取消(纯逻辑)
// ---------------------------------------------------------------------------

/** 调和状态 → 中文展示词(waiting_human 属正常态,不译成失败)。 */
export const HISTORY_STATE_LABEL = Object.freeze({
  prepared: "已就绪未启动",
  starting: "启动中",
  running: "运行中",
  waiting_human: "等待人工关卡",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "未知(进程已退出且无事件)",
});

/** 恢复动作只对等待人工关卡的运行开放。 */
export function canResume(state) {
  return state === "waiting_human";
}

/** 取消只对"服务端还能管到的活跃运行"开放(unknown/终态一律不可)。 */
export function canCancel(state) {
  return state === "running" || state === "starting";
}

/**
 * 删除只对终态 + 已就绪未启动(prepared,从未有进程)开放;与后端
 * web/console_service.py DELETABLE_STATES 冻结契约一致 —— running/starting/
 * waiting_human/unknown 一律 409 拒删,这里只做即时反馈,服务端是权威。
 */
export function canDelete(state) {
  return state === "completed" || state === "failed"
    || state === "cancelled" || state === "prepared";
}

/**
 * 历史列表客户端过滤(任务搜索,阶段 5):query 按 thread_id / 状态 /
 * 状态中文词 / 创建时间 做大小写不敏感的子串匹配。
 */
export function filterHistoryRows(rows, query) {
  const q = String(query || "").trim().toLowerCase();
  const list = Array.isArray(rows) ? rows : [];
  if (!q) return list.slice();
  return list.filter((r) => {
    const haystack = [
      r && r.thread_id,
      r && r.state,
      HISTORY_STATE_LABEL[r && r.state],
      r && r.created_at,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(q);
  });
}

/**
 * 恢复值解析(与 scripts/resume_workflow.parse_resume_value 同语义):
 * 优先 JSON;失败回退 true/false 字面量;再回退原始字符串。
 * 返回 {ok, value, error} —— 不抛异常,交由调用方展示。
 */
export function parseResumeValue(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return { ok: true, value: true, error: "" }; // 空 = 默认 true
  try {
    return { ok: true, value: JSON.parse(text), error: "" };
  } catch (e) {
    const lowered = text.toLowerCase();
    if (lowered === "true") return { ok: true, value: true, error: "" };
    if (lowered === "false") return { ok: true, value: false, error: "" };
    return { ok: false, value: null, error: "恢复值须为合法 JSON(如 true 或 {\"answer\":\"ok\"})" };
  }
}

/**
 * 单行历史 → 操作提示(哪个动作可用;服务端仍是权威,这里只做即时反馈)。
 * 返回 { resume: bool, cancel: bool, delete: bool }。
 */
export function historyActions(row) {
  const state = row && row.state;
  return { resume: canResume(state), cancel: canCancel(state), delete: canDelete(state) };
}

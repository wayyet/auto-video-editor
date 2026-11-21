// ---------------------------------------------------------------------------
// AI 内容明细页纯逻辑层(2026-11-19 第八期审查修订版)。
// 边界:零 DOM / 零 fetch —— 页面内嵌脚本负责取数与调渲染(与
// workflow_console.mjs 同款纪律);本模块只做归一化、格式化、状态文案。
// ---------------------------------------------------------------------------

// 区块状态统一枚举(web/api_console.py content-details 契约;审查文档 §5.4)
export const AI_CONTENT_STATUS_LABELS = Object.freeze({
  ok: "已完成",
  cache_hit: "已完成(缓存)",
  no_results: "检索完成,未找到结果",
  skipped: "已跳过",
  pending: "等待生成",
  missing: "产物缺失",
  failed: "执行失败",
  unavailable: "暂时无法读取",
  invalid: "产物格式异常",
  no_match: "检索完成,无匹配模板",
});

// 英文字幕翻译状态徽章(real/stub/skipped;stub 不得显示为翻译成功)
export const TRANSLATION_STATUS_LABELS = Object.freeze({
  real: "真实翻译",
  stub: "占位翻译(非真实翻译)",
  skipped: "未执行翻译",
});

export function statusLabel(status) {
  if (status == null || status === "") return "未知状态";
  return AI_CONTENT_STATUS_LABELS[status] || String(status);
}

export function translationLabel(translationStatus) {
  if (translationStatus == null || translationStatus === "") return null;
  return TRANSLATION_STATUS_LABELS[translationStatus] || String(translationStatus);
}

// 状态 → 徽章配色类(页面 .st.* 与 console.html 四态色一致)
export function statusClass(status) {
  switch (status) {
    case "ok":
    case "cache_hit":
      return "done";
    case "pending":
      return "run";
    case "skipped":
    case "no_results":
    case "no_match":
      return "waiting";
    case "failed":
    case "unavailable":
    case "invalid":
      return "failed";
    default:
      return "muted"; // missing / 未知
  }
}

// 毫秒 → "mm:ss.s";≥1h → "h:mm:ss.s";非法输入返回 "—"(原始 ms 保留在数据里)
export function formatMs(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "—";
  const totalSec = Math.floor(ms / 1000);
  const tenth = Math.floor((ms % 1000) / 100);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const ss = `${String(s).padStart(2, "0")}.${tenth}`;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${String(m).padStart(2, "0")}:${ss}`;
}

// 外链协议白名单:仅 http/https 可点击;其余(含 javascript:/data:/相对路径/
// 解析失败)返回 null → 页面渲染纯文本(审查文档 §6.3)
export function safeExternalUrl(url) {
  if (typeof url !== "string") return null;
  const raw = url.trim();
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return raw;
  } catch {
    return null;
  }
}

// 从 URL 提取域名(仅 http/https);失败返回 ""(后端已给 domain 时优先用后端的)
export function domainOf(url) {
  if (typeof url !== "string") return "";
  try {
    const u = new URL(url.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return u.hostname;
  } catch {
    return "";
  }
}

// 后端 payload → 视图模型:字段缺失/null/数组异常一律归一为固定形状,
// 渲染层不再自行判空(审查文档 §6.2:不能仅凭空数组推断失败)
export function normalizeContentDetails(payload) {
  const p = payload && typeof payload === "object" ? payload : {};
  const sec = (s) => (s && typeof s === "object" ? s : {});
  const arr = (v) => (Array.isArray(v) ? v : []);
  const wt = sec(p.web_topic);
  const wte = sec(p.web_topic_en);
  const tpl = sec(p.template);
  const sc = sec(p.script);
  const zh = sec(p.subtitles_zh);
  const en = sec(p.subtitles_en);
  const count = (s) => (typeof s.count === "number" ? s.count : arr(s.cues).length);
  return {
    threadId: p.thread_id || "",
    taskStatus: p.task_status || "unknown",
    generatedAt: p.generated_at || "",
    webTopic: {
      status: wt.status || "missing",
      reason: wt.reason || null,
      // 引擎展示名完全由后端产物推导(老产物无 engine → 未知引擎,不得伪造,
      // 审查修订版 §6.3);后端 console_service 恒给字符串,这里只兜渲染缺省。
      engine: wt.engine || "未知引擎",
      query: wt.query ?? null,
      searchedAt: wt.searched_at ?? null,
      entries: arr(wt.entries),
      // 2026-11-20 审查阶段5:MC 回退 Bing 的状态与原因透传(后端已脱敏限长)
      fallback:
        wt.fallback && typeof wt.fallback === "object"
          ? {
              toProvider: wt.fallback.to_provider ?? null,
              primaryErrorCode: wt.fallback.primary_error_code ?? null,
              reason: wt.fallback.reason ?? null,
            }
          : null,
      // 2026-11-21 审查修订版 §5.5:平台回退(xhs→dy)成功时的切换过程透传
      platformFallback:
        wt.platform_fallback && typeof wt.platform_fallback === "object"
          ? {
              fromPlatform: wt.platform_fallback.from_platform ?? null,
              toPlatform: wt.platform_fallback.to_platform ?? null,
              reason: wt.platform_fallback.reason ?? null,
            }
          : null,
    },
    // 2026-11-20 第九期:英文线独立视图模型(不与中文线共享字段;审查 §8.3)
    webTopicEn: {
      status: wte.status || "missing",
      reason: wte.reason || null,
      engine: wte.engine || "未知引擎",
      query: wte.query ?? null,
      searchedAt: wte.searched_at ?? null,
      entries: arr(wte.entries),
      // 逐源状态摘要(部分源空/错的可观测诊断;后端已脱敏限长)
      sourceSummary: wte.source_summary ?? null,
    },
    template: {
      status: tpl.status || "missing",
      reason: tpl.reason || null,
      templateId: tpl.template_id ?? null,
      templateVersion: tpl.template_version ?? null,
      selectedTemplate:
        tpl.selected_template && typeof tpl.selected_template === "object"
          ? tpl.selected_template
          : null,
      candidates: arr(tpl.candidates),
      retrievalMethod: tpl.retrieval_method ?? null,
      selectionMethod: tpl.selection_method ?? null,
      selectedTemplateContent: tpl.selected_template_content ?? null,
      contentTruncated: Boolean(tpl.content_truncated),
    },
    script: {
      status: sc.status || "missing",
      reason: sc.reason || null,
      lang: sc.lang ?? null,
      voiceoverStyle: sc.voiceover_style ?? null,
      groups: arr(sc.groups),
      references: arr(sc.references),
    },
    subtitlesZh: {
      status: zh.status || "missing",
      reason: zh.reason || null,
      count: count(zh),
      cues: arr(zh.cues),
      cuesTruncated: Boolean(zh.cues_truncated),
    },
    subtitlesEn: {
      status: en.status || "missing",
      reason: en.reason || null,
      count: count(en),
      translationStatus: en.translation_status ?? null,
      cues: arr(en.cues),
      cuesTruncated: Boolean(en.cues_truncated),
    },
  };
}

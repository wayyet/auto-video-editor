// /static/delivery_summary.mjs
//
// 创建台「交付状态卡」的**纯逻辑层**(第四期阶段 1 G1 补强,2026-11-13)。
// 与 workflow_monitor.mjs / workflow_console.mjs 同一惯例:不碰 DOM、不 fetch、
// 不依赖浏览器全局,Node 里可直接单测(web/tests/delivery_summary.test.mjs);
// DOM 渲染(renderDeliveryCard)仍留在 web/console.html 的模块脚本里。
//
// 背景:SSE 帧契约失配(后端平铺 payload、前端读 event.payload.xxx)让交付卡
// 恒「待发生」存活至今,而交付卡的归约逻辑内联在 console.html 里没有任何单测
// —— 本次把「事件 → 交付卡状态归并」抽为纯函数并纳入测试,防止同类缺陷再存活。
//
// 职责:
//   1. progressSummaryOf:node.progress 事件 → 交付卡入口判定(payload.summary.event);
//   2. reduceDeliveryCard:业务事件(draft.* / delivery.qa_* / export.*)→
//      交付卡六行状态的不可变归并(纯函数,不改入参);
//   3. shaNote / deliveryEffectsRow:QA 摘要与效果落盘行的子构造器。
//
// 状态字典形状(与 console.html DELIVERY_ROWS 一致):
//   { artifacts, draft_generated, draft_published, effects, delivery_qa, final_export }
//   每行 = { state: "pending"|"ok"|"warn"|"fail"|"skip", note: string, issues: any[] }

/**
 * 从 node.progress 事件里取交付卡入口 summary(事件已由 eventFromSse 还原)。
 * 返回 summary 对象(必须带 .event)或 null —— 与 console.html 旧判定逐字同语义:
 *   event.event_type === "node.progress" && payload?.summary && payload.summary.event
 */
export function progressSummaryOf(event) {
  if (!event || event.event_type !== "node.progress") return null;
  const summary = event.payload && typeof event.payload === "object" ? event.payload.summary : null;
  return summary && summary.event ? summary : null;
}

/** sha256 回读摘要 → note 后缀(`[SHA zh:xxxx… / en:xxxx…]`);无值返回空串。 */
export function shaNote(sha) {
  if (!sha || typeof sha !== "object") return "";
  const parts = [];
  for (const role of ["zh", "en"]) {
    const h = sha[role] && sha[role]["draft_content.json"];
    if (h) parts.push(`${role}:${String(h).slice(0, 12)}…`);
  }
  return parts.length ? ` [SHA ${parts.join(" / ")}]` : "";
}

/** QA 类别结果 → 效果落盘行(通过/降级/失败/不适用 聚合;计划 §8 阶段 6 任务 5)。 */
export function deliveryEffectsRow(categories) {
  let ok = 0, degraded = 0, failed = 0, na = 0;
  for (const k of Object.keys(categories || {})) {
    const st = categories[k] && categories[k].status;
    if (st === "passed") ok += 1;
    else if (st === "degraded") degraded += 1;
    else if (st === "failed") failed += 1;
    else na += 1;
  }
  const total = ok + degraded + failed + na;
  return {
    state: failed ? "fail" : degraded ? "warn" : (ok ? "ok" : "skip"),
    note: total ? `通过 ${ok} / 降级 ${degraded} / 失败 ${failed} / 不适用 ${na}` : "",
    issues: [],
  };
}

/**
 * 归并一条交付业务事件到交付卡状态;返回**新的**状态字典(纯函数,不改入参),
 * 事件不认识时返回 null(调用方保持原状态不重渲染)。
 *
 * @param {object} delivery 当前状态字典(行 → {state, note, issues})
 * @param {object} summary  node.progress 的 payload.summary(必须带 .event)
 */
export function reduceDeliveryCard(delivery, summary) {
  if (!delivery || typeof delivery !== "object") return null;
  if (!summary || typeof summary !== "object" || !summary.event) return null;
  const next = { ...delivery };
  const ev = String(summary.event);
  if (ev === "draft.generated") {
    next.draft_generated = { state: "ok", note: summary.draft_dir || "", issues: [] };
  } else if (ev === "draft.published") {
    next.draft_published = { state: "ok", note: summary.target || "", issues: [] };
  } else if (ev === "draft.publish_failed") {
    next.draft_published = {
      state: "warn",
      note: `发布失败,降级用任务目录草稿:${String(summary.reason || "")}`.slice(0, 300),
      issues: [],
    };
  } else if (ev === "delivery.qa_passed") {
    next.delivery_qa = {
      state: summary.skipped ? "skip" : "ok",
      note: (summary.skipped ? "未执行(英文分支未生成,上游降级)" : "QA 通过") + shaNote(summary.sha256),
      issues: [],
    };
    if (summary.categories) next.effects = deliveryEffectsRow(summary.categories);
  } else if (ev === "delivery.qa_issues") {
    const gateFailed = !!summary.gate_failed;
    next.delivery_qa = {
      state: gateFailed ? "fail" : "warn",
      note: (gateFailed
        ? `必需功能缺失,阻断交付(发现 ${summary.issue_count ?? "?"} 个问题)`
        : `发现 ${summary.issue_count ?? "?"} 个问题(非必需,不阻断;全量见监控事件)`) +
        shaNote(summary.sha256),
      issues: Array.isArray(summary.issues) ? summary.issues : [],
    };
    if (summary.categories) next.effects = deliveryEffectsRow(summary.categories);
  } else if (ev === "delivery.qa_repair") {
    next.delivery_qa = {
      state: "warn",
      note: `定向修复中:${(summary.retried || []).join(", ") || "无可修复类别"}(修复后复检)`,
      issues: [],
    };
  } else if (ev === "delivery.qa_failed") {
    next.delivery_qa = {
      state: "fail",
      note: `必需功能复检仍失败,运行已终止(run.failed):${(summary.blocked || []).join(", ")}`,
      issues: [],
    };
  } else if (ev === "export.succeeded") {
    next.final_export = { state: "ok", note: summary.output_path || "", issues: [] };
  } else if (ev === "export.failed") {
    next.final_export = {
      state: "fail",
      note: String(summary.reason || "").slice(0, 300),
      issues: [],
    };
  } else if (ev === "export.skipped") {
    next.final_export = { state: "skip", note: summary.reason || "未启用", issues: [] };
  } else {
    return null;
  }
  return next;
}

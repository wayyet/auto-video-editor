// /web/tests/delivery_summary.test.mjs
//
// 交付状态卡纯逻辑层单测(第四期阶段 1 G1 补强):progressSummaryOf 入口判定 +
// reduceDeliveryCard 状态归并。与 workflow_console.test.mjs 同一惯例,node --test 直跑。
import test from "node:test";
import assert from "node:assert/strict";

import {
  deliveryEffectsRow,
  progressSummaryOf,
  reduceDeliveryCard,
  shaNote,
} from "../static/delivery_summary.mjs";

function cardState() {
  const rows = ["artifacts", "draft_generated", "draft_published", "effects", "delivery_qa", "final_export"];
  const state = {};
  for (const key of rows) state[key] = { state: "pending", note: "", issues: [] };
  return state;
}

test("progressSummaryOf:仅 node.progress 且 payload.summary.event 存在时返回 summary", () => {
  const ok = progressSummaryOf({
    event_type: "node.progress",
    payload: { summary: { monitor_event: "progress", event: "draft.generated" } },
  });
  assert.equal(ok && ok.event, "draft.generated");

  // 非 node.progress / 无 payload / summary 缺 event → null
  assert.equal(progressSummaryOf({ event_type: "node.succeeded", payload: { summary: { event: "x" } } }), null);
  assert.equal(progressSummaryOf({ event_type: "node.progress", payload: {} }), null);
  assert.equal(progressSummaryOf({ event_type: "node.progress", payload: { summary: {} } }), null);
  assert.equal(progressSummaryOf(null), null);
});

test("progressSummaryOf:平铺旧形状(无 payload 层)→ null(修复前交付卡恒待发生的根因形状)", () => {
  assert.equal(
    progressSummaryOf({ event_type: "node.progress", summary: { event: "draft.generated" } }),
    null,
  );
});

test("reduceDeliveryCard:draft 生成/发布/发布失败 三态", () => {
  const s = cardState();
  let next = reduceDeliveryCard(s, { event: "draft.generated", draft_dir: "AutoVideo_x" });
  assert.equal(next.draft_generated.state, "ok");
  assert.equal(next.draft_generated.note, "AutoVideo_x");
  next = reduceDeliveryCard(next, { event: "draft.published", target: "剪映草稿库" });
  assert.equal(next.draft_published.state, "ok");
  next = reduceDeliveryCard(next, { event: "draft.publish_failed", reason: "注册失败" });
  assert.equal(next.draft_published.state, "warn");
  assert.ok(next.draft_published.note.includes("发布失败"));
});

test("reduceDeliveryCard:QA 三态 + sha 摘要 + 效果落盘行", () => {
  const s = cardState();
  const categories = {
    subtitles_zh: { status: "passed" },
    transitions: { status: "failed" },
    stickers: { status: "degraded" },
    bgm: { status: "not_applicable" },
  };
  let next = reduceDeliveryCard(s, {
    event: "delivery.qa_passed", skipped: false,
    sha256: { zh: { "draft_content.json": "a".repeat(64) } },
    categories,
  });
  assert.equal(next.delivery_qa.state, "ok");
  assert.ok(next.delivery_qa.note.includes("[SHA zh:"));
  // 效果落盘行 = 类别聚合:1 通过 1 降级 1 失败 1 不适用
  assert.equal(next.effects.state, "fail");
  assert.ok(next.effects.note.includes("通过 1 / 降级 1 / 失败 1 / 不适用 1"));

  next = reduceDeliveryCard(next, { event: "delivery.qa_passed", skipped: true });
  assert.equal(next.delivery_qa.state, "skip");
  next = reduceDeliveryCard(next, { event: "delivery.qa_issues", gate_failed: true, issue_count: 2, issues: [{}] });
  assert.equal(next.delivery_qa.state, "fail");
  assert.ok(next.delivery_qa.note.includes("阻断交付"));
  assert.equal(next.delivery_qa.issues.length, 1);
  next = reduceDeliveryCard(next, { event: "delivery.qa_issues", gate_failed: false, issue_count: 1 });
  assert.equal(next.delivery_qa.state, "warn");
  next = reduceDeliveryCard(next, { event: "delivery.qa_repair", retried: ["transitions"] });
  assert.ok(next.delivery_qa.note.includes("transitions"));
  next = reduceDeliveryCard(next, { event: "delivery.qa_failed", blocked: ["voiceover_zh"] });
  assert.equal(next.delivery_qa.state, "fail");
});

test("reduceDeliveryCard:导出三态", () => {
  const s = cardState();
  let next = reduceDeliveryCard(s, { event: "export.succeeded", output_path: "out.mp4" });
  assert.equal(next.final_export.state, "ok");
  next = reduceDeliveryCard(next, { event: "export.failed", reason: "编码失败" });
  assert.equal(next.final_export.state, "fail");
  next = reduceDeliveryCard(next, { event: "export.skipped", reason: "" });
  assert.equal(next.final_export.state, "skip");
  assert.equal(next.final_export.note, "未启用");
});

test("reduceDeliveryCard:未知事件返回 null;纯函数不改入参", () => {
  const s = cardState();
  const snapshot = JSON.stringify(s);
  assert.equal(reduceDeliveryCard(s, { event: "node.progress" }), null);
  assert.equal(reduceDeliveryCard(s, null), null);
  assert.equal(reduceDeliveryCard(null, { event: "draft.generated" }), null);
  const next = reduceDeliveryCard(s, { event: "draft.generated", draft_dir: "x" });
  assert.equal(JSON.stringify(s), snapshot, "入参不得被修改");
  assert.notEqual(next, s, "必须返回新对象");
});

test("deliveryEffectsRow 与 shaNote 子构造器", () => {
  const row = deliveryEffectsRow({
    a: { status: "passed" }, b: { status: "passed" }, c: { status: "degraded" },
  });
  assert.equal(row.state, "warn");
  assert.equal(row.note, "通过 2 / 降级 1 / 失败 0 / 不适用 0");
  assert.deepEqual(deliveryEffectsRow({}).note, "");
  assert.equal(shaNote(null), "");
  assert.equal(
    shaNote({ zh: { "draft_content.json": "ab".repeat(32) }, en: { "draft_content.json": "cd".repeat(32) } }),
    ` [SHA zh:${"ab".repeat(6)}… / en:${"cd".repeat(6)}…]`,
  );
});

// AI 内容明细页纯逻辑层单测(2026-11-19 第八期审查修订版)。
// 被测:web/static/ai_content.mjs(零 DOM/零 fetch,同 workflow_console.mjs 纪律)。
// pytest 显式包装:tests/test_workflow_console_frontend.py(不能依赖 glob 发现)。
import test from "node:test";
import assert from "node:assert/strict";

import {
  domainOf,
  formatMs,
  normalizeContentDetails,
  safeExternalUrl,
  statusClass,
  statusLabel,
  translationLabel,
} from "../static/ai_content.mjs";

// ---------------------------------------------------------------------------
// formatMs(毫秒 → mm:ss.s;≥1h → h:mm:ss.s)
// ---------------------------------------------------------------------------

test("formatMs:边界与非法输入", () => {
  assert.equal(formatMs(0), "00:00.0");
  assert.equal(formatMs(2399), "00:02.3");
  assert.equal(formatMs(61000), "01:01.0");
  assert.equal(formatMs(3661500), "1:01:01.5");
  assert.equal(formatMs(-1), "—");
  assert.equal(formatMs(null), "—");
  assert.equal(formatMs(undefined), "—");
  assert.equal(formatMs("2400"), "—");
  assert.equal(formatMs(Number.NaN), "—");
  assert.equal(formatMs(Number.POSITIVE_INFINITY), "—");
});

// ---------------------------------------------------------------------------
// safeExternalUrl / domainOf(协议白名单;审查文档 §6.3)
// ---------------------------------------------------------------------------

test("safeExternalUrl:仅 http/https 放行", () => {
  assert.equal(safeExternalUrl("https://travel.example.com/mosque"), "https://travel.example.com/mosque");
  assert.equal(safeExternalUrl("http://travel.example.com/mosque"), "http://travel.example.com/mosque");
  assert.equal(safeExternalUrl("  https://a.example.com  "), "https://a.example.com");
});

test("safeExternalUrl:javascript:/data:/相对路径/垃圾输入一律拒绝", () => {
  assert.equal(safeExternalUrl("javascript:alert(1)"), null);
  assert.equal(safeExternalUrl("data:text/html,<script>"), null);
  assert.equal(safeExternalUrl("ftp://x.example.com/a"), null);
  assert.equal(safeExternalUrl("relative/path/only"), null);
  assert.equal(safeExternalUrl("/console"), null);
  assert.equal(safeExternalUrl(""), null);
  assert.equal(safeExternalUrl("   "), null);
  assert.equal(safeExternalUrl(null), null);
  assert.equal(safeExternalUrl(undefined), null);
  assert.equal(safeExternalUrl(123), null);
});

test("domainOf:提取域名,非 http/https 返回空串", () => {
  assert.equal(domainOf("https://travel.example.com/mosque"), "travel.example.com");
  assert.equal(domainOf("http://a.b.example.com/x?y=1"), "a.b.example.com");
  assert.equal(domainOf("javascript:alert(1)"), "");
  assert.equal(domainOf("not a url"), "");
  assert.equal(domainOf(null), "");
  assert.equal(domainOf(42), "");
});

// ---------------------------------------------------------------------------
// 状态文案(含 stub 不得显示为成功、skipped 不是失败)
// ---------------------------------------------------------------------------

test("statusLabel:全枚举中文文案,未知状态透传", () => {
  assert.equal(statusLabel("ok"), "已完成");
  assert.equal(statusLabel("cache_hit"), "已完成(缓存)");
  assert.equal(statusLabel("no_results"), "检索完成,未找到结果");
  assert.equal(statusLabel("no_match"), "检索完成,无匹配模板");
  assert.equal(statusLabel("skipped"), "已跳过");
  assert.equal(statusLabel("pending"), "等待生成");
  assert.equal(statusLabel("missing"), "产物缺失");
  assert.equal(statusLabel("failed"), "执行失败");
  assert.equal(statusLabel("unavailable"), "暂时无法读取");
  assert.equal(statusLabel("invalid"), "产物格式异常");
  assert.equal(statusLabel("mystery_status"), "mystery_status");
  assert.equal(statusLabel(null), "未知状态");
  assert.equal(statusLabel(""), "未知状态");
});

test("translationLabel:stub 必须明示占位,不得写成翻译成功", () => {
  assert.equal(translationLabel("real"), "真实翻译");
  const stubLabel = translationLabel("stub");
  assert.match(stubLabel, /占位翻译/);
  assert.notEqual(stubLabel, "真实翻译", "stub 不得直接标成「真实翻译」");
  assert.ok(!stubLabel.startsWith("真实翻译"), "stub 文案不得以「真实翻译」开头");
  assert.equal(translationLabel("skipped"), "未执行翻译");
  assert.equal(translationLabel(null), null);
  assert.equal(translationLabel("custom"), "custom");
});

test("statusClass:状态 → 徽章配色类", () => {
  assert.equal(statusClass("ok"), "done");
  assert.equal(statusClass("cache_hit"), "done");
  assert.equal(statusClass("pending"), "run");
  assert.equal(statusClass("skipped"), "waiting");
  assert.equal(statusClass("no_results"), "waiting");
  assert.equal(statusClass("no_match"), "waiting");
  assert.equal(statusClass("failed"), "failed");
  assert.equal(statusClass("unavailable"), "failed");
  assert.equal(statusClass("invalid"), "failed");
  assert.equal(statusClass("missing"), "muted");
  assert.equal(statusClass("whatever"), "muted");
});

// ---------------------------------------------------------------------------
// normalizeContentDetails(payload → 视图模型)
// ---------------------------------------------------------------------------

test("normalizeContentDetails:完整 payload 归一化", () => {
  const vm = normalizeContentDetails({
    thread_id: "console-ab12cd34ef56",
    task_status: "completed",
    generated_at: "2026-11-19T00:00:00+00:00",
    web_topic: {
      status: "cache_hit", engine: "DuckDuckGo", query: "q", searched_at: "t",
      entries: [{ title: "T", url: "https://a.example.com/x", domain: "a.example.com", snippet: "s" }],
    },
    template: {
      status: "ok", template_id: "t1", template_version: "1.0",
      selected_template: { template_id: "t1", description: "d", score: 0.9 },
      candidates: [{ template_id: "t1" }],
      retrieval_method: "keyword_bm25", selection_method: "deterministic_top1",
      selected_template_content: "body", content_truncated: 1,
    },
    script: {
      status: "ok", lang: "zh", voiceover_style: "明快",
      groups: [{ group_id: "g1", summary: "s", narration: "n" }],
      references: [{ title: "r", url: "https://b.example.com/y", domain: "b.example.com" }],
    },
    subtitles_zh: { status: "ok", count: 2, cues: [{ index: 1, start_ms: 0, end_ms: 1, text: "a" }] },
    subtitles_en: {
      status: "ok", count: 2, translation_status: "stub",
      cues: [{ index: 1, start_ms: 0, end_ms: 1, text: "b" }], cues_truncated: true,
    },
  });
  assert.equal(vm.threadId, "console-ab12cd34ef56");
  assert.equal(vm.taskStatus, "completed");
  assert.equal(vm.webTopic.status, "cache_hit");
  assert.deepEqual(vm.webTopic.entries[0], { title: "T", url: "https://a.example.com/x", domain: "a.example.com", snippet: "s" });
  assert.equal(vm.template.selectedTemplate.description, "d");
  assert.equal(vm.template.contentTruncated, true);
  assert.equal(vm.script.groups[0].narration, "n");
  assert.equal(vm.subtitlesZh.count, 2);
  assert.equal(vm.subtitlesEn.translationStatus, "stub");
  assert.equal(vm.subtitlesEn.cuesTruncated, true);
});

test("normalizeContentDetails:空对象/null/数组异常全部容错", () => {
  const vm = normalizeContentDetails({});
  assert.equal(vm.threadId, "");
  assert.equal(vm.taskStatus, "unknown");
  for (const sec of [vm.webTopic, vm.template, vm.script, vm.subtitlesZh, vm.subtitlesEn]) {
    assert.equal(sec.status, "missing");
    assert.equal(sec.reason, null);
  }
  assert.equal(vm.webTopic.entries.length, 0);
  assert.equal(vm.template.candidates.length, 0);
  assert.equal(vm.template.selectedTemplate, null);
  assert.equal(vm.script.groups.length, 0);
  assert.equal(vm.subtitlesZh.cues.length, 0);
  assert.equal(vm.subtitlesEn.translationStatus, null);
  assert.equal(vm.subtitlesEn.cuesTruncated, false);

  const vm2 = normalizeContentDetails(null);
  assert.equal(vm2.taskStatus, "unknown");
  assert.equal(vm2.webTopic.status, "missing");

  const vm3 = normalizeContentDetails({ subtitles_zh: { cues: [{ index: 1 }], status: "ok" } });
  assert.equal(vm3.subtitlesZh.status, "ok");
  assert.equal(vm3.subtitlesZh.count, 1, "count 缺失时回退 cues.length");
  assert.equal(vm3.webTopic.status, "missing", "缺区块不串数据");
});

// 2026-11-20 审查阶段5:MC 回退 Bing 的 fallback 透传(后端已脱敏限长)
test("normalizeContentDetails:webTopic fallback 状态映射与缺省", () => {
  const vm = normalizeContentDetails({
    web_topic: {
      status: "ok",
      engine: "Bing(MediaCrawler 回退)",
      fallback: {
        to_provider: "bing_html",
        primary_error_code: "NETWORK_ERROR",
        reason: "MediaCrawler(小红书) NETWORK_ERROR: down; 已回退 Bing",
      },
    },
  });
  assert.equal(vm.webTopic.fallback.toProvider, "bing_html");
  assert.equal(vm.webTopic.fallback.primaryErrorCode, "NETWORK_ERROR");
  assert.ok(vm.webTopic.fallback.reason.includes("已回退 Bing"));

  const vmNoFallback = normalizeContentDetails({
    web_topic: { status: "ok", engine: "MediaCrawler(小红书)" },
  });
  assert.equal(vmNoFallback.webTopic.fallback, null, "无 fallback 时为 null");
});

// 2026-11-20 审查修订版阶段4:英文检索线独立区块(web_topic_en)
test("normalizeContentDetails:webTopicEn 双线独立归一化", () => {
  const vm = normalizeContentDetails({
    web_topic: { status: "ok", engine: "MediaCrawler(小红书)", query: "夏季防晒" },
    web_topic_en: {
      status: "ok",
      engine: "Xpoz(海外社媒)",
      query: "summer sun protection travel",
      platforms: ["tiktok", "twitter", "reddit", "instagram"],
      entries: [
        {
          url: "https://www.tiktok.com/@creator/video/1",
          title: "summer tips",
          domain: "www.tiktok.com",
          snippet: "beach guide",
          platform: "tiktok",
          language_status: "en",
          content_kind: "video",
          has_video: true,
        },
        {
          url: "https://x.com/a/status/1",
          title: "thread",
          platform: "twitter",
          language_status: "unknown",
          content_kind: "post",
        },
      ],
    },
  });
  // 双线互不掺杂:两区块 query 独立(审查 §2.1)
  assert.equal(vm.webTopic.query, "夏季防晒");
  assert.equal(vm.webTopicEn.query, "summer sun protection travel");
  assert.equal(vm.webTopicEn.engine, "Xpoz(海外社媒)");
  assert.deepEqual(vm.webTopicEn.platforms, ["tiktok", "twitter", "reddit", "instagram"]);
  assert.equal(vm.webTopicEn.entries.length, 2);
  assert.equal(vm.webTopicEn.entries[0].language_status, "en");
  assert.equal(vm.webTopicEn.entries[0].content_kind, "video");
  // unknown 语言如实透传,不冒充英文(审查阶段4任务5)
  assert.equal(vm.webTopicEn.entries[1].language_status, "unknown");
});

test("normalizeContentDetails:webTopicEn 缺区块/异常输入容错", () => {
  const vm = normalizeContentDetails({});
  assert.equal(vm.webTopicEn.status, "missing");
  assert.deepEqual(vm.webTopicEn.platforms, []);
  assert.deepEqual(vm.webTopicEn.entries, []);

  const vm2 = normalizeContentDetails({
    web_topic_en: { status: "skipped", platforms: "not-a-list", entries: 42 },
  });
  assert.equal(vm2.webTopicEn.status, "skipped");
  assert.deepEqual(vm2.webTopicEn.platforms, [], "非数组 platforms 归一为空");
  assert.deepEqual(vm2.webTopicEn.entries, [], "非数组 entries 归一为空");
});

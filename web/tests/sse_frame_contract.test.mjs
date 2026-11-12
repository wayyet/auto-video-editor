// /web/tests/sse_frame_contract.test.mjs
//
// SSE 帧契约(前端一侧):后端 to_sse_frame 产出的真实帧(fixtures/sse_frames.txt,
// 由 tests/unit/monitoring/test_sse_frame_contract.py 生成并校验),
// 经 eventFromSse 还原后,必须能被 applyEvent 正确归约。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  NODE_STATUS,
  applyEvent,
  createMonitorState,
  eventFromSse,
} from "../static/workflow_monitor.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

/** 按 SSE 规范把文本拆成 EventSource 风格的对象:{lastEventId, type, data} */
function parseFrames(text) {
  return text.replace(/\r\n/g, "\n").split("\n\n").filter(Boolean).map((block) => {
    const frame = { lastEventId: "", type: "message", data: "" };
    for (const line of block.split("\n")) {
      if (line.startsWith("id: ")) frame.lastEventId = line.slice(4);
      else if (line.startsWith("event: ")) frame.type = line.slice(7);
      else if (line.startsWith("data: ")) frame.data += line.slice(6);
    }
    return frame;
  });
}

function replay() {
  const frames = parseFrames(readFileSync(join(HERE, "fixtures", "sse_frames.txt"), "utf-8"));
  const state = createMonitorState();
  const events = frames.map((f) => {
    const event = eventFromSse(f.lastEventId, f.type, JSON.parse(f.data));
    applyEvent(state, event);
    return event;
  });
  return { state, events };
}

test("eventFromSse:信封字段留在顶层,业务字段收进 payload", () => {
  const { events } = replay();
  const progress = events[0];
  assert.equal(progress.id, 2001);
  assert.equal(progress.event_type, "node.progress");
  assert.equal(progress.node_id, "generate_draft");
  assert.equal(progress.payload.summary.event, "draft.generated");
  assert.equal(progress.summary, undefined, "summary 不应再留在顶层");
});

test("真实帧经 eventFromSse + applyEvent:耗时、失败原因、等待类型都读得到", () => {
  const { state } = replay();
  assert.equal(state.nodes.generate_draft.durationMs, 1250);
  assert.equal(state.nodes.node_13_adjust_volume.status, NODE_STATUS.FAILED);
  assert.equal(state.nodes.node_13_adjust_volume.lastError.message, "boom");
  assert.equal(state.nodes.node_12_human_add_bgm.waitingKind, "web_file_handshake");
  assert.deepEqual(state.nodes.node_12_human_add_bgm.waitingDetail, { checkpoints: ["②"] });
});

test("交付状态卡的入口条件:node.progress 的 payload.summary.event 读得到", () => {
  const { events } = replay();
  const seen = events
    .filter((e) => e.event_type === "node.progress" && e.payload.summary && e.payload.summary.event)
    .map((e) => e.payload.summary.event);
  assert.deepEqual(seen, ["draft.generated", "delivery.qa_issues"]);
});

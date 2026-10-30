// 阶段 E:前端执行过程面板的策略单测。
//
// 跑法:node --test openstoryline/web/tests/mcode_panel.test.mjs
// (pytest 里也有一层包装,见 openstoryline/tests/test_mcode_frontend_policy.py)
//
// 被测的是 /static/mcode_panel.mjs —— 纯函数,不碰 DOM。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import {
  LLM_SOURCE,
  MCODE_SOURCE,
  ROUTE_BODY,
  ROUTE_BUBBLE,
  ROUTE_PANEL,
  TurnEventGate,
  applyEventToPanelModel,
  buildPanelModel,
  createPanelModel,
  isTerminalStatus,
  latestTraceTurnId,
  normalizeSource,
  phaseI18nKey,
  routeEvent,
  shouldAcceptEvent,
  statusI18nKey,
  statusIcon,
} from "../static/mcode_panel.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TIMELINE_PATH = path.join(
  HERE, "..", "..", "tests", "fixtures", "mcode_stream", "ws_timeline_mcode.jsonl",
);

// ---------------------------------------------------------------------------
// 来源校验
// ---------------------------------------------------------------------------

test("MCode turn 只收 source=mcode 的事件", () => {
  assert.equal(shouldAcceptEvent({ turnSource: MCODE_SOURCE, eventSource: MCODE_SOURCE }), true);
  assert.equal(shouldAcceptEvent({ turnSource: MCODE_SOURCE, eventSource: "llm" }), false);
  assert.equal(shouldAcceptEvent({ turnSource: MCODE_SOURCE, eventSource: undefined }), false);
  assert.equal(shouldAcceptEvent({ turnSource: MCODE_SOURCE, eventSource: "" }), false);
});

test("普通 turn 拒绝自称 mcode 的事件", () => {
  assert.equal(shouldAcceptEvent({ turnSource: LLM_SOURCE, eventSource: MCODE_SOURCE }), false);
  assert.equal(shouldAcceptEvent({ turnSource: LLM_SOURCE, eventSource: LLM_SOURCE }), true);
  // 老事件没有 source 字段 —— 放行(零回归)
  assert.equal(shouldAcceptEvent({ turnSource: LLM_SOURCE, eventSource: undefined }), true);
});

test("来源归一化", () => {
  assert.equal(normalizeSource("MCODE"), MCODE_SOURCE);
  assert.equal(normalizeSource("  mcode "), MCODE_SOURCE);
  assert.equal(normalizeSource("llm"), "llm");
  assert.equal(normalizeSource(null), "");
});

// ---------------------------------------------------------------------------
// run_id + seq 去重 / 排序
// ---------------------------------------------------------------------------

test("同一个 run_id + seq 只放行一次", () => {
  const gate = new TurnEventGate();
  const evt = { run_id: "r1", seq: 3 };
  assert.equal(gate.accept(evt).ok, true);
  assert.equal(gate.accept(evt).ok, false);
  assert.equal(gate.accept(evt).reason, "duplicate");
});

test("乱序的 seq 被丢弃", () => {
  const gate = new TurnEventGate();
  assert.equal(gate.accept({ run_id: "r1", seq: 5 }).ok, true);
  const out = gate.accept({ run_id: "r1", seq: 2 });
  assert.equal(out.ok, false);
  assert.equal(out.reason, "out_of_order");
});

test("换 run_id 的事件被丢弃", () => {
  const gate = new TurnEventGate();
  assert.equal(gate.accept({ run_id: "r1", seq: 1 }).ok, true);
  const out = gate.accept({ run_id: "r2", seq: 2 });
  assert.equal(out.ok, false);
  assert.equal(out.reason, "run_id_mismatch");
});

test("没有 seq 的收尾事件直接放行", () => {
  const gate = new TurnEventGate();
  const out = gate.accept({ type: "assistant.end" });
  assert.equal(out.ok, true);
  assert.equal(out.reason, "no_seq");
});

// ---------------------------------------------------------------------------
// 路由:文本进气泡,过程进面板
// ---------------------------------------------------------------------------

test("MCode turn:文本进气泡,状态/步骤/工具进面板", () => {
  assert.deepEqual(
    routeEvent({ type: "assistant.delta", turnSource: MCODE_SOURCE, eventSource: MCODE_SOURCE }),
    [ROUTE_BUBBLE],
  );
  assert.deepEqual(
    routeEvent({ type: "agent.status", turnSource: MCODE_SOURCE, eventSource: MCODE_SOURCE }),
    [ROUTE_PANEL],
  );
  assert.deepEqual(
    routeEvent({ type: "agent.step", turnSource: MCODE_SOURCE, eventSource: MCODE_SOURCE }),
    [ROUTE_PANEL],
  );
  assert.deepEqual(
    routeEvent({ type: "tool.start", turnSource: MCODE_SOURCE, eventSource: MCODE_SOURCE }),
    [ROUTE_PANEL],
  );
});

test("普通 turn:过程事件一律忽略,工具卡片留在聊天正文", () => {
  assert.deepEqual(
    routeEvent({ type: "agent.status", turnSource: LLM_SOURCE, eventSource: LLM_SOURCE }),
    [],
  );
  assert.deepEqual(
    routeEvent({ type: "agent.step", turnSource: LLM_SOURCE, eventSource: LLM_SOURCE }),
    [],
  );
  assert.deepEqual(
    routeEvent({ type: "tool.start", turnSource: LLM_SOURCE, eventSource: LLM_SOURCE }),
    [ROUTE_BODY],
  );
  assert.deepEqual(
    routeEvent({ type: "tool.end", turnSource: LLM_SOURCE, eventSource: LLM_SOURCE }),
    [ROUTE_BODY],
  );
  // 文本照旧进气泡(零回归)
  assert.deepEqual(
    routeEvent({ type: "assistant.delta", turnSource: LLM_SOURCE, eventSource: LLM_SOURCE }),
    [ROUTE_BUBBLE],
  );
});

test("来源对不上的事件不进任何通道", () => {
  assert.deepEqual(
    routeEvent({ type: "assistant.delta", turnSource: MCODE_SOURCE, eventSource: LLM_SOURCE }),
    [],
  );
  assert.deepEqual(
    routeEvent({ type: "tool.start", turnSource: MCODE_SOURCE, eventSource: LLM_SOURCE }),
    [],
  );
});

// ---------------------------------------------------------------------------
// 面板模型
// ---------------------------------------------------------------------------

test("状态事件合并成按阶段的步骤", () => {
  let model = createPanelModel();
  model = applyEventToPanelModel(model, "agent.status", {
    status: "running", phase: "initializing", message: "MCode 进程已启动", seq: 1,
  });
  model = applyEventToPanelModel(model, "agent.status", {
    status: "running", phase: "initializing", message: "MCode 进程已启动(重复)", seq: 2,
  });
  model = applyEventToPanelModel(model, "agent.status", {
    status: "running", phase: "analyzing", message: "正在分析任务", seq: 3,
  });

  assert.equal(model.status, "running");
  assert.equal(model.phase, "analyzing");
  assert.equal(model.steps.length, 2, "同一个 phase 只留一条步骤");
  assert.equal(model.steps[0].text, "MCode 进程已启动(重复)", "同 phase 的步骤就地更新");
  assert.equal(model.steps[1].text, "正在分析任务");
});

test("工具生命周期进面板,不出现在正文", () => {
  let model = createPanelModel();
  model = applyEventToPanelModel(model, "tool.start", {
    tool_call_id: "call_1", server: "MCode Agent", name: "read_notes",
    args: { path: "a.md" }, seq: 1,
  });
  model = applyEventToPanelModel(model, "tool.progress", {
    tool_call_id: "call_1", server: "MCode Agent", name: "read_notes",
    message: "正在执行", progress: 0.5, seq: 2,
  });
  model = applyEventToPanelModel(model, "tool.end", {
    tool_call_id: "call_1", server: "MCode Agent", name: "read_notes",
    is_error: false, summary: "已读取 5 个素材文件", seq: 3,
  });

  assert.equal(model.tools.length, 1);
  assert.equal(model.tools[0].state, "success");
  assert.equal(model.tools[0].summary, "已读取 5 个素材文件");
  const toolSteps = model.steps.filter((s) => s.kind === "tool");
  assert.equal(toolSteps.length, 1);
  assert.equal(toolSteps[0].status, "completed");
});

test("文本增量不进面板", () => {
  const model = createPanelModel();
  const next = applyEventToPanelModel(model, "assistant.delta", {
    delta: "这段是答复正文", source: MCODE_SOURCE, seq: 1,
  });
  assert.equal(next.steps.length, 0);
  assert.equal(next.tools.length, 0);
  assert.equal(JSON.stringify(next).includes("这段是答复正文"), false);
});

test("错误把面板置为 failed,已完成的步骤保留", () => {
  let model = createPanelModel();
  model = applyEventToPanelModel(model, "agent.status", {
    status: "running", phase: "analyzing", message: "正在分析任务", seq: 1,
  });
  model = applyEventToPanelModel(model, "error", { message: "CLI 挂了" });

  assert.equal(model.status, "failed");
  assert.equal(isTerminalStatus(model.status), true);
  assert.equal(model.steps.length, 2);
  assert.equal(model.steps[0].text, "正在分析任务");
});

test("状态 / 阶段的 i18n key", () => {
  assert.equal(statusI18nKey("running"), "mcode.status.running");
  assert.equal(statusI18nKey("waiting_tool"), "mcode.status.waiting_tool");
  assert.equal(statusI18nKey("瞎写的"), "mcode.status.running");
  assert.equal(phaseI18nKey("tool_execution"), "mcode.phase.tool_execution");
  assert.equal(phaseI18nKey(""), "");
});

// ---------------------------------------------------------------------------
// WS-07:从 trace 恢复面板,不重复插入答复文本
// ---------------------------------------------------------------------------

test("buildPanelModel 从 trace 重建面板", () => {
  const trace = [
    { type: "agent.status", source: "mcode", turn_id: "t1", seq: 1,
      status: "running", phase: "initializing", message: "MCode 进程已启动" },
    { type: "tool.start", source: "mcode", turn_id: "t1", seq: 2,
      tool_call_id: "call_1", server: "MCode Agent", name: "read_notes" },
    { type: "tool.end", source: "mcode", turn_id: "t1", seq: 3,
      tool_call_id: "call_1", server: "MCode Agent", name: "read_notes",
      is_error: false, summary: "ok" },
    { type: "agent.status", source: "mcode", turn_id: "t1", seq: 4,
      status: "completed", phase: "completed", message: "本轮已完成" },
  ];

  const model = buildPanelModel(trace, { turnId: "t1" });
  assert.equal(model.status, "completed");
  assert.equal(model.tools.length, 1);
  assert.equal(model.steps.length, 3);
  assert.equal(latestTraceTurnId(trace), "t1");
});

test("trace 里没有答复正文 —— 恢复面板不会往正文插话", () => {
  const trace = [
    { type: "agent.status", source: "mcode", turn_id: "t1", seq: 1,
      status: "running", phase: "analyzing", message: "正在分析任务" },
  ];
  const model = buildPanelModel(trace, { turnId: "t1" });
  const blob = JSON.stringify(model);
  assert.equal(blob.includes("content"), false);
  assert.equal(blob.includes("assistant"), false);
});

// ---------------------------------------------------------------------------
// 用真实的事件时间线跑一遍(阶段 E 的四件套证据之一)
// ---------------------------------------------------------------------------

test("真实事件时间线:文本只进气泡,过程只进面板", () => {
  const raw = readFileSync(TIMELINE_PATH, "utf-8");
  const events = raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));

  assert.ok(events.length > 0, "事件时间线是空的");

  const turnSource = MCODE_SOURCE;
  let bubble = "";
  let model = createPanelModel();
  const gate = new TurnEventGate();
  const panelTypes = new Set();

  for (const evt of events) {
    const type = evt.type;
    const data = evt.data || {};
    const routes = routeEvent({ type, turnSource, eventSource: data.source });

    if (routes.includes(ROUTE_PANEL)) {
      const verdict = gate.accept(data);
      if (!verdict.ok) continue;
      model = applyEventToPanelModel(model, type, data);
      panelTypes.add(type);
    }
    if (routes.includes(ROUTE_BUBBLE) && type === "assistant.delta") {
      const verdict = gate.accept(data);
      if (!verdict.ok) continue;
      bubble += data.delta || "";
    }
  }

  assert.ok(bubble.length > 0, "时间线里应该有可见文本");
  assert.ok(panelTypes.has("agent.status"), "面板应该收到状态事件");
  assert.ok(model.steps.length > 0, "面板应该有步骤");

  // 状态文案不能混进答复正文
  for (const step of model.steps) {
    assert.equal(bubble.includes(step.text), false, `状态文案混进了正文:${step.text}`);
  }
  // 面板里不能出现答复正文
  assert.equal(JSON.stringify(model).includes(bubble), false, "答复正文混进了面板");
});

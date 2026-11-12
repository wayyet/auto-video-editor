// /web/tests/workflow_layout.test.mjs
//
// workflow_layout.mjs(纵向有向图布局纯模块)的 Node 单测,第5期阶段1。
// 不碰 DOM —— measurements 直接传普通对象。运行方式:node --test 直跑
// (pytest 包装见 tests/test_workflow_monitor_frontend.py)。
//
// 用例对照计划文档 §7 阶段1「重点单测」:单链 / 一对多 / 多对一 / 三路汇合 /
// 条件边 / 自环 / 多节点环 / 组合回路 / 不可达节点 / 异构宽高 / 输出稳定 / 缓存 key。
import test from "node:test";
import assert from "node:assert/strict";

import {
  LAYOUT_ALGORITHM_VERSION,
  assignVerticalRanks,
  computeLayeredGraphPositions,
  detectFeedbackEdges,
  edgeKeyOf,
  getLayoutCacheKey,
  orderGraphLayers,
} from "../static/workflow_layout.mjs";
import { mainLayoutKey } from "../static/workflow_monitor.mjs";

/** 便捷构图:ids 用 | 分隔;edges 用 "src->dst[:id]" 逗号分隔(id 缺省= src->dst)。 */
function g(spec, edgeSpec) {
  const nodes = spec.split("|").map((id) => ({ id }));
  const edges = (edgeSpec || "").split(",").filter(Boolean).map((s) => {
    const [pair, id] = s.split(":");
    const [from, to] = pair.split("->");
    return { from, to, id: id || `${from}->${to}` };
  });
  return { nodes, edges };
}

function rects(result, measurements) {
  const out = new Map();
  for (const [id, p] of result.positions) {
    const m = (measurements || {})[id] || {};
    out.set(id, { x: p.x, y: p.y, w: m.width ?? 340, h: m.height ?? 90 });
  }
  return out;
}

// ---------------------------------------------------------------------------
// 1. 单链:rank 逐层向下增加
// ---------------------------------------------------------------------------
test("单链 START→A→B→END:rank 0..3,Y 严格递增", () => {
  const { nodes, edges } = g("START|A|B|END", "START->A,A->B,B->END");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.deepEqual([...r.rankByNode.entries()].sort(), [["A", 1], ["B", 2], ["END", 3], ["START", 0]]);
  const [yStart, yA, yB, yEnd] = ["START", "A", "B", "END"].map((id) => r.positions.get(id).y);
  assert.ok(yStart < yA && yA < yB && yB < yEnd, "纵向布局 Y 逐层递增");
  assert.equal(r.feedbackEdgeIds.size, 0);
  assert.equal(r.unreachableNodeIds.size, 0);
  assert.equal(r.unreachableBandTopY, null);
});

// ---------------------------------------------------------------------------
// 2. 一对多:同层水平展开
// ---------------------------------------------------------------------------
test("一对多 A→B/C:B、C 同 rank 同 Y,水平相邻不重叠", () => {
  const { nodes, edges } = g("A|B|C", "A->B,A->C");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.equal(r.rankByNode.get("B"), 1);
  assert.equal(r.rankByNode.get("C"), 1);
  const pb = r.positions.get("B");
  const pc = r.positions.get("C");
  assert.equal(pb.y, pc.y, "并行分支同层同 Y");
  assert.notEqual(pb.x, pc.x);
});

// ---------------------------------------------------------------------------
// 3. 多对一汇合:目标唯一、rank 晚于全部上游
// ---------------------------------------------------------------------------
test("多对一 B/C→D:D 只出现一次且 rank 晚于 B、C", () => {
  const { nodes, edges } = g("A|B|C|D", "A->B,A->C,B->D,C->D");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.equal(r.rankByNode.get("D"), 2);
  const layer2 = r.orderByRank.get(2);
  assert.equal(layer2.length, 1, "汇合节点在层内只占一个位置");
  assert.equal(layer2[0], "D");
  assert.equal([...r.positions.keys()].filter((k) => k === "D").length, 1);
});

// ---------------------------------------------------------------------------
// 4. 三路并行后汇合:全部入边保留
// ---------------------------------------------------------------------------
test("三路并行汇合:5 条边全在排版边集,目标节点唯一", () => {
  const { nodes, edges } = g("A|B|C|D|E", "A->B,A->C,A->D,B->E,C->E,D->E");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.equal(r.forwardEdgeIds.size, 6);
  assert.equal(r.feedbackEdgeIds.size, 0);
  assert.equal(r.rankByNode.get("E"), 2);
});

// ---------------------------------------------------------------------------
// 5. 条件边:保留 kind/label/id,全部可能目标参与分层
// ---------------------------------------------------------------------------
test("条件边:按 id 进排版边集,可能目标各占一层位(不等于已执行)", () => {
  const nodes = [{ id: "A" }, { id: "B" }, { id: "C" }];
  const edges = [
    { id: "a->b", from: "A", to: "B", kind: "conditional", label: "检索条件命中" },
    { id: "a->c", from: "A", to: "C", kind: "normal", label: null },
  ];
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.ok(r.forwardEdgeIds.has("a->b"), "条件边保留在排版边集");
  assert.ok(r.forwardEdgeIds.has("a->c"));
  assert.equal(r.rankByNode.get("B"), 1);
  assert.equal(r.rankByNode.get("C"), 1);
  assert.equal(r.feedbackEdgeIds.has("a->b"), false);
  // 模块不改写后端边语义:输入对象未被触碰
  assert.equal(edges[0].kind, "conditional");
  assert.equal(edges[0].label, "检索条件命中");
});

// ---------------------------------------------------------------------------
// 6. 自环:不影响拓扑排序,保留在反馈边集合
// ---------------------------------------------------------------------------
test("自环 A→A:进反馈边集合,rank 计算不挂起", () => {
  const { nodes, edges } = g("START|A|END", "START->A,A->A:A->A,A->END");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.deepEqual([...r.feedbackEdgeIds], ["A->A"]);
  assert.equal(r.rankByNode.get("A"), 1);
  assert.equal(r.rankByNode.get("END"), 2);
});

// ---------------------------------------------------------------------------
// 7. 多节点环:识别反馈边,剩余排版边无环
// ---------------------------------------------------------------------------
test("多节点环 B→C→A:恰好识别一条反馈边,分层不抛错", () => {
  const { nodes, edges } = g("START|A|B|C|D|END", "START->A,A->B,B->C,C->A,C->D,D->END");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.deepEqual([...r.feedbackEdgeIds], ["C->A"], "DFS 按稳定顺序恰好切断 C→A");
  assert.equal(r.rankByNode.get("D"), 4);
  assert.equal(r.rankByNode.get("END"), 5);
  // 反馈边也在完整边集合中(渲染层负责画):forward + feedback == 全部边
  const all = new Set(edges.map((e) => e.id));
  for (const id of all) {
    assert.ok(r.forwardEdgeIds.has(id) || r.feedbackEdgeIds.has(id), `边 ${id} 未丢失`);
  }
});

// ---------------------------------------------------------------------------
// 8. 组合回路:QA 重试 + QC 修复 + 速度自环 + 交付 QA 修复
// ---------------------------------------------------------------------------
test("组合回路:四类反馈边各就各位,主链 rank 单调,算法不挂起", () => {
  const { nodes, edges } = g(
    "START|GC|QA|JOIN|DRAFT|QC4|QCR|REPORT|SPEED|JD|EXPORT|REPAIR|FAIL|END",
    [
      "START->GC",
      "GC->QA", "QA->GC", "QA->JOIN",       // QA 重试回路
      "JOIN->DRAFT", "DRAFT->QC4",
      "QC4->QCR", "QCR->QC4", "QC4->REPORT", // QC 修复回路
      "REPORT->SPEED", "SPEED->SPEED",       // 速度护栏自环
      "SPEED->JD",
      "JD->EXPORT", "JD->REPAIR", "REPAIR->JD", "JD->FAIL", // 交付 QA 修复回路
      "EXPORT->END", "FAIL->END",
    ].join(",")
  );
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.deepEqual(
    [...r.feedbackEdgeIds].sort(),
    ["QA->GC", "QCR->QC4", "REPAIR->JD", "SPEED->SPEED"].sort(),
    "恰好识别四条反馈边"
  );
  for (const [u, v] of [["GC", "QA"], ["QA", "JOIN"], ["JOIN", "DRAFT"], ["DRAFT", "QC4"],
    ["QC4", "REPORT"], ["REPORT", "SPEED"], ["SPEED", "JD"], ["JD", "EXPORT"]]) {
    assert.ok(r.rankByNode.get(v) > r.rankByNode.get(u), `rank(${v}) > rank(${u})`);
  }
  // 同输入再算一次:全部输出稳定
  const r2 = computeLayeredGraphPositions(nodes, edges, {});
  assert.deepEqual(
    [...r.positions.entries()].map(([k, p]) => [k, p.x, p.y]).sort(),
    [...r2.positions.entries()].map(([k, p]) => [k, p.x, p.y]).sort()
  );
});

// ---------------------------------------------------------------------------
// 9. 不可达节点:独立区带,不伪造真实拓扑边
// ---------------------------------------------------------------------------
test("不可达节点:进独立区带(QC 关闭的 assembly 场景),主流程不受干扰", () => {
  // 主链 + 悬空 2 节点链(X→Y)+ 完全孤立 Z(QC off:assembly 零边)
  const { nodes, edges } = g("START|A|END|X|Y|Z", "START->A,A->END,X->Y");
  const r = computeLayeredGraphPositions(nodes, edges, {});
  assert.deepEqual([...r.unreachableNodeIds].sort(), ["X", "Y", "Z"]);
  // 悬空链按局部 rank 分层:X=0、Y=1,孤立 Z 自成分量
  assert.equal(r.rankByNode.get("X"), 0);
  assert.equal(r.rankByNode.get("Y"), 1);
  assert.ok(r.unreachableBandTopY > 0, "区带顶 Y 已给出");
  const mainBottom = Math.max(...["START", "A", "END"].map((id) => r.positions.get(id).y));
  assert.ok(r.unreachableBandTopY >= mainBottom, "区带在主流程之下");
  for (const id of ["X", "Y", "Z"]) {
    assert.ok(r.positions.get(id).y >= r.unreachableBandTopY, `${id} 落在区带内`);
  }
  assert.equal(r.forwardEdgeIds.has("X->Y"), true, "悬空链真实边保留(不伪造也不删除)");
});

// ---------------------------------------------------------------------------
// 10. 不同节点宽高:卡片坐标无重叠
// ---------------------------------------------------------------------------
test("异构宽高:任意两张卡片矩形不交叠", () => {
  const { nodes, edges } = g(
    "START|A|B|C|D|E|END",
    "START->A,A->B,A->C,A->D,B->E,C->E,D->E,E->END"
  );
  const measurements = {
    START: { width: 340, height: 64 },
    A: { width: 420, height: 220 },
    B: { width: 300, height: 90 },
    C: { width: 360, height: 340 },
    D: { width: 280, height: 110 },
    E: { width: 380, height: 180 },
    END: { width: 340, height: 64 },
  };
  const r = computeLayeredGraphPositions(nodes, edges, measurements);
  const rs = [...rects(r, measurements).entries()];
  for (let i = 0; i < rs.length; i++) {
    for (let j = i + 1; j < rs.length; j++) {
      const [, a] = rs[i];
      const [, b] = rs[j];
      const overlapX = a.x < b.x + b.w && b.x < a.x + a.w;
      const overlapY = a.y < b.y + b.h && b.y < a.y + a.h;
      assert.ok(!(overlapX && overlapY), `卡片矩形重叠:${rs[i][0]} × ${rs[j][0]}`);
    }
  }
});

// ---------------------------------------------------------------------------
// 11. 稳定性:同输入多次计算输出一致;边数组顺序打乱不影响结果
// ---------------------------------------------------------------------------
test("稳定性:重复计算与边序打乱均输出一致(确定性)", () => {
  const { nodes, edges } = g(
    "START|A|B|C|D|END",
    "START->A,A->B,A->C,B->D,C->D,D->END,B->C"
  );
  const norm = (r) => JSON.stringify({
    positions: [...r.positions.entries()].sort().map(([k, p]) => [k, p.x, p.y]),
    ranks: [...r.rankByNode.entries()].sort(),
    order: [...r.orderByRank.entries()].sort(),
    fb: [...r.feedbackEdgeIds].sort(),
    fwd: [...r.forwardEdgeIds].sort(),
    lanes: [...r.feedbackLanes.entries()].sort(),
  });
  const r1 = computeLayeredGraphPositions(nodes, edges, {});
  const r2 = computeLayeredGraphPositions(nodes, edges, {});
  assert.equal(norm(r1), norm(r2), "同输入两次计算输出一致");

  const shuffled = [...edges].reverse();
  const r3 = computeLayeredGraphPositions(nodes, shuffled, {});
  assert.deepEqual([...r1.feedbackEdgeIds].sort(), [...r3.feedbackEdgeIds].sort(), "边数组顺序不影响反馈边识别");
  assert.deepEqual([...r1.rankByNode.entries()].sort(), [...r3.rankByNode.entries()].sort(), "边数组顺序不影响 rank");
});

// ---------------------------------------------------------------------------
// 12. 缓存 key:TB/LR 隔离(与 workflow_monitor.mjs::mainLayoutKey 同规则)
// ---------------------------------------------------------------------------
test("缓存 key:TB 带算法版本,LR 沿用历史格式,双函数同规则", () => {
  const base = "wf_layout_auto_video_editor_main_auto_v6";
  // 计划 §6.3 字面量:..._tb_v1(算法版本升级时才变 v2)
  assert.equal(getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "tb" }), `${base}_tb_${LAYOUT_ALGORITHM_VERSION}`);
  assert.equal(getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "tb" }), `${base}_tb_v1`);
  assert.equal(getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "lr" }), base);
  assert.equal(getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6 }), base);
  assert.equal(getLayoutCacheKey({}), "wf_layout_workflow_default_v1");
  assert.equal(mainLayoutKey("auto_video_editor_main", "auto", 6, "tb"), getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "tb" }));
  assert.equal(mainLayoutKey("auto_video_editor_main", "auto", 6), getLayoutCacheKey({ workflowId: "auto_video_editor_main", mode: "auto", schemaVersion: 6, direction: "lr" }));
});

// ---------------------------------------------------------------------------
// 13. 辅助:异常边忽略并告警;feedbackLanes 稳定分配;LR 拒绝
// ---------------------------------------------------------------------------
test("异常边忽略+告警;lane 分配稳定;LR 方向明确拒绝", () => {
  const warnings = [];
  const { nodes, edges } = g("A|B", "A->B,A->ghost,ghost->B");
  const r = computeLayeredGraphPositions(nodes, edges, {}, { onWarning: (m) => warnings.push(m) });
  assert.equal(r.forwardEdgeIds.size, 1);
  assert.equal(warnings.length, 2, "两条引用未知节点的边各告警一次");

  // 三条反馈边:key 序稳定 → left/0、right/0、left/1
  const { nodes: cn, edges: ce } = g("START|A|B|C|END", "START->A,A->B,B->A,B->C,C->B,C->END");
  const fb = detectFeedbackEdges(cn, ce);
  assert.equal(fb.feedbackEdgeIds.size >= 2, true);
  const lanes = new Map();
  [...fb.feedbackEdgeIds].sort().forEach((k, i) => {
    lanes.set(k, { side: i % 2 === 0 ? "left" : "right", index: Math.floor(i / 2) });
  });
  const r2 = computeLayeredGraphPositions(cn, ce, {});
  for (const [k, lane] of lanes) assert.deepEqual(r2.feedbackLanes.get(k), lane);

  assert.throws(() => computeLayeredGraphPositions([{ id: "A" }], [], {}, { direction: "LR" }),
    /LR 布局由页面/);
});

// ---------------------------------------------------------------------------
// 14. 分层原语直测:assignVerticalRanks 环残留抛错(计划 §5.4 第 7 条)
// ---------------------------------------------------------------------------
test("排版边集残留环:assignVerticalRanks 显式抛错,不静默乱摆", () => {
  const { nodes, edges } = g("A|B", "A->B,B->A");
  assert.throws(() => assignVerticalRanks(nodes, edges, new Set()), /仍存在环/);
  // 正确识别反馈边后不抛
  const fb = detectFeedbackEdges(nodes, edges);
  const ranks = assignVerticalRanks(nodes, edges, fb.feedbackEdgeIds);
  assert.equal(ranks.rankByNode.get("A"), 0);
  assert.equal(ranks.rankByNode.get("B"), 1);
  assert.deepEqual(ranks.mainNodeIds, [], "无 START 时主流程为空");
  assert.equal(ranks.unreachableComponents.length, 1, "A、B 连通,自成一组独立分层");
});

// ---------------------------------------------------------------------------
// 15. orderGraphLayers:交叉减少(接受准则)
// ---------------------------------------------------------------------------
test("层内排序:barycenter 减少交叉且保持稳定", () => {
  // 初始序层1=[A,B]、层2=[C,D]:A→D、B→C 构成交叉;barycenter 下扫后消除
  const { nodes, edges } = g("A|B|C|D", "A->D,B->C");
  const ranks = new Map([["A", 0], ["B", 0], ["C", 1], ["D", 1]]);
  const order = orderGraphLayers(nodes, edges, ranks, {});
  assert.deepEqual(order.get(0), ["A", "B"]);
  assert.deepEqual(order.get(1), ["D", "C"], "层2 重排为 D(左)C(右)消除交叉");
});

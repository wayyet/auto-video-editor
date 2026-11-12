/**
 * 工作流节点地图——纵向有向图布局纯模块(第5期阶段1)。
 *
 * 职责边界(计划 §6.1):
 * - 只计算布局数据:不创建 DOM、不发 HTTP、不订阅 SSE、不改节点/边状态;
 * - 只给边增加前端临时布局角色(forward/feedback),不改写后端 kind/label/id;
 * - 输入输出均为普通 JS 数据结构,node --test 可直接覆盖。
 *
 * 算法(Sugiyama 简化版,计划 §5):
 * 1. 三色标记确定性 DFS 识别反馈边 —— 反馈边只从「排版边集」中排除,
 *    真实边一个不少(最终渲染仍用完整 data.edges,计划 §1.2 关键原则);
 * 2. 排版边集上 Kahn 拓扑分层(rank):rank(v) = max(rank(u)+1),
 *    多前驱汇合不需要唯一父节点;不可达节点按连通分量独立分层,
 *    不插入主流程 rank 空间(计划 §5.4);
 * 3. 层内 barycenter 上下交替排序减交叉,初始序/平局 = 节点注册序(计划 §5.5);
 * 4. 按实测 DOM 尺寸分配坐标:每层高度取该层最大卡片高,层间 NODE_GAP_Y,
 *    每层整体水平居中;不可达节点放主流程下方「未启用/未接入」区带,
 *    不伪造连线(计划 §5.6)。
 */

/** TB 布局算法版本(缓存 key 后缀用;改变 rank/层内排序规则时升级为 v2,计划 §6.3)。 */
export const LAYOUT_ALGORITHM_VERSION = "v1";

export const LAYOUT_DEFAULTS = Object.freeze({
  direction: "TB",
  nodeGapX: 48,       // 同层相邻卡片水平间距(计划 §5.6)
  nodeGapY: 64,       // 相邻 rank 层垂直间距
  paddingX: 48,       // 画布左右留白
  paddingY: 40,       // 画布顶部留白
  laneBase: 36,       // 反馈边第一道 lane 距节点边缘的基准偏移
  laneGap: 26,        // 相邻 lane 间距(按 edge id 稳定分配,计划 §5.7)
  bandGap: 96,        // 主流程底部到「未启用/未接入」区带顶部(含分隔标签空间)
  maxSweepRounds: 8,  // barycenter 上下交替轮数上限(计划 §5.5:4~8)
  startNodeId: "START",
  defaultWidth: 340,  // 与 .node 卡片默认宽度一致
  defaultHeight: 90,
});

/** 边的稳定 key:后端有 id 用 id;无 id(防御)按 下标:from->to,避免同对边互撞。 */
export function edgeKeyOf(edge, index = 0) {
  if (edge && edge.id != null && edge.id !== "") return String(edge.id);
  return `#${index}:${edge?.from}->${edge?.to}`;
}

function normalizeNodes(nodes) {
  const ids = [];
  const byId = new Map();
  for (const n of Array.isArray(nodes) ? nodes : []) {
    if (!n || n.id == null || byId.has(n.id)) continue;
    byId.set(n.id, n);
    ids.push(n.id);
  }
  return { ids, byId };
}

/**
 * 出边邻接:按节点注册序;每节点出边按 (key, 原始下标) 稳定排序 ——
 * 相同 schema 多次布局输出稳定(计划 §5.2 第 5 条)。
 * 引用未知节点的异常边被忽略并告警,不进任何边集。
 */
function buildAdjacency(ids, byId, edges, onWarning) {
  const outgoing = new Map(ids.map((id) => [id, []]));
  const edgeKeys = new Map(); // edge 对象 -> key
  const known = [];           // 端点合法的边(输入顺序)
  (Array.isArray(edges) ? edges : []).forEach((e, i) => {
    const key = edgeKeyOf(e, i);
    edgeKeys.set(e, key);
    if (!e || !byId.has(e.from) || !byId.has(e.to)) {
      if (onWarning) onWarning(`workflow_layout: 忽略引用未知节点的边 ${key}`);
      return;
    }
    known.push(e);
  });
  for (const e of known) outgoing.get(e.from).push(e);
  for (const list of outgoing.values()) {
    list.sort((a, b) => {
      const ka = edgeKeys.get(a);
      const kb = edgeKeys.get(b);
      return ka < kb ? -1 : ka > kb ? 1 : 0;
    });
  }
  return { outgoing, known, edgeKeys };
}

function resolveStartId(byId, options) {
  const startId = options.startNodeId !== undefined ? options.startNodeId : LAYOUT_DEFAULTS.startNodeId;
  return startId != null && byId.has(startId) ? startId : null;
}

/**
 * 识别反馈边(计划 §5.3):三色标记确定性 DFS。
 * v 为灰色(在当前 DFS 路径上)的边 u→v 即反馈边,自环同理;
 * 其余(树边/前向边/跨树边)全部留在排版边集 —— 删除反馈边后必无环
 * (DFS 完成时间序即剩余 DAG 的拓扑序)。
 *
 * Returns { forwardEdgeIds: Set<string>, feedbackEdgeIds: Set<string> }
 */
export function detectFeedbackEdges(nodes, edges, options = {}) {
  const { ids, byId } = normalizeNodes(nodes);
  const { outgoing, known, edgeKeys } = buildAdjacency(ids, byId, edges, options.onWarning);
  const feedback = new Set();
  const GRAY = 1;
  const BLACK = 2;
  const color = new Map();
  const startId = resolveStartId(byId, options);
  const roots = [];
  if (startId != null) roots.push(startId);
  for (const id of ids) if (id !== startId) roots.push(id);

  for (const root of roots) {
    if ((color.get(root) ?? 0) !== 0) continue;
    color.set(root, GRAY);
    const stack = [{ id: root, list: outgoing.get(root) || [], i: 0 }];
    while (stack.length) {
      const frame = stack[stack.length - 1];
      if (frame.i < frame.list.length) {
        const e = frame.list[frame.i++];
        const cv = color.get(e.to) ?? 0;
        if (cv === GRAY) {
          feedback.add(edgeKeys.get(e)); // 回边/自环:仅排版时排除
        } else if (cv === 0) {
          color.set(e.to, GRAY);
          stack.push({ id: e.to, list: outgoing.get(e.to) || [], i: 0 });
        }
        // BLACK:前向/跨树边,排版边集保留
      } else {
        color.set(frame.id, BLACK);
        stack.pop();
      }
    }
  }

  const forwardEdgeIds = new Set();
  for (const e of known) {
    const key = edgeKeys.get(e);
    if (!feedback.has(key)) forwardEdgeIds.add(key);
  }
  return { forwardEdgeIds, feedbackEdgeIds: feedback };
}

/**
 * 纵向分层(计划 §5.4):排版边集上按组做 Kahn 最长路径。
 * 主流程 = START 沿排版边正向可达;不可达节点按无向连通分量独立分层,
 * 跨组边不参与 rank(不可达不与主流程竞争 rank 空间,计划 §5.4 第 6 条)。
 * 剔除反馈边后仍出现环 ⇒ 反馈边识别或输入图有缺陷,直接抛错(计划 §5.4 第 7 条)。
 *
 * Returns { rankByNode: Map, mainNodeIds: string[], unreachableComponents: string[][] }
 */
export function assignVerticalRanks(nodes, edges, feedbackEdgeIds, options = {}) {
  const { ids, byId } = normalizeNodes(nodes);
  const { known, edgeKeys } = buildAdjacency(ids, byId, edges, options.onWarning);
  const fb = feedbackEdgeIds instanceof Set ? feedbackEdgeIds : new Set(feedbackEdgeIds || []);
  const startId = resolveStartId(byId, options);

  const rankEdges = [];
  const fwdOut = new Map(ids.map((id) => [id, []]));
  for (const e of known) {
    if (fb.has(edgeKeys.get(e))) continue;
    rankEdges.push(e);
    fwdOut.get(e.from).push(e.to);
  }

  // 主流程 = START 正向可达
  const mainNodeIds = [];
  if (startId != null) {
    const seen = new Set([startId]);
    const queue = [startId];
    while (queue.length) {
      const u = queue.shift();
      mainNodeIds.push(u);
      for (const v of fwdOut.get(u) || []) {
        if (!seen.has(v)) {
          seen.add(v);
          queue.push(v);
        }
      }
    }
  }
  const mainSet = new Set(mainNodeIds);

  // 不可达节点按排版边的无向连通性分组(孤立节点自成一组)
  const unreachable = ids.filter((id) => !mainSet.has(id));
  const undOut = new Map(unreachable.map((id) => [id, []]));
  for (const e of rankEdges) {
    if (mainSet.has(e.from) || mainSet.has(e.to)) continue;
    undOut.get(e.from).push(e.to);
    undOut.get(e.to).push(e.from);
  }
  const idIndex = new Map(ids.map((id, i) => [id, i]));
  const compOf = new Map();
  const unreachableComponents = [];
  for (const id of unreachable) {
    if (compOf.has(id)) continue;
    const compIdx = unreachableComponents.length;
    const comp = [];
    const stack = [id];
    compOf.set(id, compIdx);
    while (stack.length) {
      const u = stack.pop();
      comp.push(u);
      for (const v of undOut.get(u) || []) {
        if (!compOf.has(v)) {
          compOf.set(v, compIdx);
          stack.push(v);
        }
      }
    }
    comp.sort((a, b) => idIndex.get(a) - idIndex.get(b));
    unreachableComponents.push(comp);
  }

  // 分层:同组内 Kahn 最长路径;组内入度为零的节点 rank=0(计划 §5.4 公式)
  const rankByNode = new Map(ids.map((id) => [id, 0]));
  const groups = [mainNodeIds, ...unreachableComponents];
  for (const group of groups) {
    const gset = new Set(group);
    const indeg = new Map(group.map((id) => [id, 0]));
    const out = new Map(group.map((id) => [id, []]));
    for (const e of rankEdges) {
      if (!gset.has(e.from) || !gset.has(e.to) || e.from === e.to) continue;
      out.get(e.from).push(e.to);
      indeg.set(e.to, indeg.get(e.to) + 1);
    }
    const queue = group.filter((id) => indeg.get(id) === 0); // 注册序入队,稳定
    let processed = 0;
    while (queue.length) {
      const u = queue.shift();
      processed++;
      for (const v of out.get(u)) {
        rankByNode.set(v, Math.max(rankByNode.get(v), rankByNode.get(u) + 1));
        const d = indeg.get(v) - 1;
        indeg.set(v, d);
        if (d === 0) queue.push(v);
      }
    }
    if (processed !== group.length) {
      throw new Error(
        `workflow_layout: 排版边集仍存在环(反馈边识别缺陷或输入图非法),` +
        `组内 ${group.length - processed} 个节点无法分层`
      );
    }
  }

  return { rankByNode, mainNodeIds, unreachableComponents };
}

/**
 * 层内排序(计划 §5.5):barycenter 上下交替,仅交叉数下降才接受新序。
 * 参与排序的边 = 排版边中相邻 rank(Δrank=1)的边;长跨边与反馈边只渲染不排序。
 * 无相邻邻居的节点以自身当前位置为重心 ⇒ 保持原位;平局按层内原序(首轮即注册序)。
 *
 * Returns orderByRank: Map<rank, nodeId[]>
 */
export function orderGraphLayers(nodes, edges, rankByNode, options = {}) {
  const { ids } = normalizeNodes(nodes);
  const { known, edgeKeys } = buildAdjacency(ids, new Map(ids.map((id) => [id, { id }])), edges, options.onWarning);
  const fb = options.feedbackEdgeIds instanceof Set
    ? options.feedbackEdgeIds
    : new Set(options.feedbackEdgeIds || []);
  const ranks = rankByNode instanceof Map ? rankByNode : new Map(Object.entries(rankByNode || {}));

  const layers = new Map(); // rank -> [nodeId],初始序 = 注册序
  for (const id of ids) {
    const r = ranks.get(id) ?? 0;
    if (!layers.has(r)) layers.set(r, []);
    layers.get(r).push(id);
  }
  const sortedRanks = Array.from(layers.keys()).sort((a, b) => a - b);

  // 相邻 rank 排序边,按上层 rank 归档
  const esByRank = new Map();
  for (const e of known) {
    if (fb.has(edgeKeys.get(e))) continue;
    const rf = ranks.get(e.from);
    const rt = ranks.get(e.to);
    if (rf == null || rt !== rf + 1) continue;
    if (!esByRank.has(rf)) esByRank.set(rf, []);
    esByRank.get(rf).push(e);
  }

  const inNbrs = new Map(ids.map((id) => [id, []]));
  const outNbrs = new Map(ids.map((id) => [id, []]));
  for (const [r, es] of esByRank) {
    for (const e of es) {
      outNbrs.get(e.from).push(e.to);
      inNbrs.get(e.to).push(e.from);
    }
  }

  const posInLayer = new Map();
  const refreshPos = () => {
    for (const arr of layers.values()) arr.forEach((id, i) => posInLayer.set(id, i));
  };
  refreshPos();

  const crossings = () => {
    let total = 0;
    for (const es of esByRank.values()) {
      for (let i = 0; i < es.length; i++) {
        for (let j = i + 1; j < es.length; j++) {
          const a = es[i];
          const b = es[j];
          if (a.from === b.from || a.to === b.to) continue;
          if ((posInLayer.get(a.from) - posInLayer.get(b.from)) *
              (posInLayer.get(a.to) - posInLayer.get(b.to)) < 0) total++;
        }
      }
    }
    return total;
  };

  const barycenter = (id, nbrs) => {
    if (!nbrs.length) return posInLayer.get(id);
    let s = 0;
    for (const v of nbrs) s += posInLayer.get(v);
    return s / nbrs.length;
  };

  const reorder = (rank, nbrsOf) => {
    const arr = layers.get(rank).slice().sort((a, b) => {
      const ba = barycenter(a, nbrsOf(a));
      const bb = barycenter(b, nbrsOf(b));
      return ba - bb || posInLayer.get(a) - posInLayer.get(b);
    });
    layers.set(rank, arr);
    arr.forEach((id, i) => posInLayer.set(id, i));
  };
  const sweepDown = () => {
    for (let k = 1; k < sortedRanks.length; k++) reorder(sortedRanks[k], (id) => inNbrs.get(id));
  };
  const sweepUp = () => {
    for (let k = sortedRanks.length - 2; k >= 0; k--) reorder(sortedRanks[k], (id) => outNbrs.get(id));
  };

  const snapshot = () => new Map(Array.from(layers, ([r, arr]) => [r, arr.slice()]));
  const restore = (snap) => {
    for (const [r, arr] of snap) {
      layers.set(r, arr.slice());
      arr.forEach((id, i) => posInLayer.set(id, i));
    }
  };

  let bestCross = crossings();
  let bestSnap = snapshot();
  const maxRounds = Math.max(0, Number(options.maxSweepRounds ?? LAYOUT_DEFAULTS.maxSweepRounds));
  for (let round = 0; round < maxRounds; round++) {
    sweepDown();
    sweepUp();
    const c = crossings();
    if (c < bestCross) {
      bestCross = c;
      bestSnap = snapshot();
    }
  }
  restore(bestSnap);

  return new Map(Array.from(layers, ([r, arr]) => [r, arr.slice()]));
}

/**
 * 编排入口(计划 §5.1):反馈边识别 → 分层 → 层内排序 → 坐标分配。
 *
 * Args:
 *   nodes: 后端 nodes[];edges: 后端 edges[](id/from/to/kind/label);
 *   measurements: nodeId -> {width, height}(DOM 实测,Map 或普通对象);
 *   options: LAYOUT_DEFAULTS 覆盖项。
 *
 * Returns {
 *   positions: Map<nodeId, {x, y}>,
 *   rankByNode: Map, orderByRank: Map,
 *   forwardEdgeIds: Set, feedbackEdgeIds: Set,
 *   unreachableNodeIds: Set,
 *   feedbackLanes: Map<edgeKey, {side: "left"|"right", index: number}>,
 *   unreachableBandTopY: number|null,
 * }
 */
export function computeLayeredGraphPositions(nodes, edges, measurements, options = {}) {
  const opts = { ...LAYOUT_DEFAULTS, ...options };
  if (String(opts.direction).toUpperCase() === "LR") {
    throw new Error("workflow_layout: LR 布局由页面 computeHorizontalPositions 承担,本模块仅支持 TB");
  }

  const { ids, byId } = normalizeNodes(nodes);
  // 三个阶段共享同一输入边集:告警去重,避免同一异常边重复上报
  const seenWarn = new Set();
  const warnOnce = (msg) => {
    if (seenWarn.has(msg)) return;
    seenWarn.add(msg);
    if (opts.onWarning) opts.onWarning(msg);
  };
  const { forwardEdgeIds, feedbackEdgeIds } = detectFeedbackEdges(nodes, edges, { ...opts, onWarning: warnOnce });
  const { rankByNode, mainNodeIds, unreachableComponents } =
    assignVerticalRanks(nodes, edges, feedbackEdgeIds, { ...opts, onWarning: warnOnce });
  const orderByRank = orderGraphLayers(nodes, edges, rankByNode, { ...opts, feedbackEdgeIds, onWarning: warnOnce });

  const measOf = (id) => {
    const m = measurements instanceof Map ? measurements.get(id) : (measurements || {})[id];
    return {
      width: Math.max(1, Number(m && m.width) || opts.defaultWidth),
      height: Math.max(1, Number(m && m.height) || opts.defaultHeight),
    };
  };

  const positions = new Map();
  const mainSet = new Set(mainNodeIds);
  const mainRanks = Array.from(new Set(mainNodeIds.map((id) => rankByNode.get(id)))).sort((a, b) => a - b);

  // 主流程:每层宽度/最大高度 → 层 Y 递增(计划 §5.6 公式),每层水平居中
  const layerWidth = new Map();
  const layerMaxH = new Map();
  for (const r of mainRanks) {
    const arr = (orderByRank.get(r) || []).filter((id) => mainSet.has(id));
    let w = 0;
    let h = 0;
    arr.forEach((id, i) => {
      const m = measOf(id);
      w += m.width + (i ? opts.nodeGapX : 0);
      h = Math.max(h, m.height);
    });
    layerWidth.set(r, w);
    layerMaxH.set(r, h);
  }
  const maxLayerWidth = mainRanks.reduce((acc, r) => Math.max(acc, layerWidth.get(r) || 0), 0);
  const centerX = opts.paddingX + maxLayerWidth / 2;
  const layerY = new Map();
  let cursorY = opts.paddingY;
  for (const r of mainRanks) {
    layerY.set(r, cursorY);
    cursorY += (layerMaxH.get(r) || 0) + opts.nodeGapY;
  }
  for (const r of mainRanks) {
    const arr = (orderByRank.get(r) || []).filter((id) => mainSet.has(id));
    let x = centerX - (layerWidth.get(r) || 0) / 2;
    for (const id of arr) {
      positions.set(id, { x, y: layerY.get(r) });
      x += measOf(id).width + opts.nodeGapX;
    }
  }
  const lastRank = mainRanks[mainRanks.length - 1];
  const mainBottom = lastRank == null
    ? opts.paddingY
    : layerY.get(lastRank) + (layerMaxH.get(lastRank) || 0);

  // 不可达区带:各连通分量在带内从左到右排,分量内部按局部 rank 纵向分层
  const unreachableNodeIds = new Set(ids.filter((id) => !mainSet.has(id)));
  const idIndex = new Map(ids.map((id, i) => [id, i]));
  let bandTop = null;
  if (unreachableComponents.length) {
    bandTop = mainBottom + opts.bandGap;
    let cursorX = opts.paddingX;
    for (const comp of unreachableComponents) {
      const compRanks = Array.from(new Set(comp.map((id) => rankByNode.get(id)))).sort((a, b) => a - b);
      const compLayers = compRanks.map((r) =>
        comp.filter((id) => rankByNode.get(id) === r).sort((a, b) => idIndex.get(a) - idIndex.get(b))
      );
      const lw = [];
      const lh = [];
      let compW = 0;
      for (const arr of compLayers) {
        let w = 0;
        let h = 0;
        arr.forEach((id, i) => {
          const m = measOf(id);
          w += m.width + (i ? opts.nodeGapX : 0);
          h = Math.max(h, m.height);
        });
        lw.push(w);
        lh.push(h);
        compW = Math.max(compW, w);
      }
      const compCenter = cursorX + compW / 2;
      let cy = bandTop;
      compLayers.forEach((arr, k) => {
        let px = compCenter - lw[k] / 2;
        for (const id of arr) {
          positions.set(id, { x: px, y: cy });
          px += measOf(id).width + opts.nodeGapX;
        }
        cy += lh[k] + opts.nodeGapY;
      });
      cursorX += compW + opts.nodeGapX * 2;
    }
  }

  // 反馈边 lane:按 edge key 稳定排序,左右交替 + 序号偏移(计划 §5.7/§10)
  const feedbackLanes = new Map();
  Array.from(feedbackEdgeIds).sort().forEach((key, i) => {
    feedbackLanes.set(key, { side: i % 2 === 0 ? "left" : "right", index: Math.floor(i / 2) });
  });

  return {
    positions,
    rankByNode,
    orderByRank,
    forwardEdgeIds,
    feedbackEdgeIds,
    unreachableNodeIds,
    feedbackLanes,
    unreachableBandTopY: bandTop,
  };
}

/**
 * 布局缓存 key(计划 §6.3):TB 带算法版本后缀;LR 沿用历史格式(零迁移,
 * 用户已保存的横向拖拽位置不受影响)。与 workflow_monitor.mjs::mainLayoutKey 同规则。
 */
export function getLayoutCacheKey({ workflowId, mode, schemaVersion, direction } = {}) {
  const base = `wf_layout_${String(workflowId || "workflow")}_${String(mode || "default")}_v${Number(schemaVersion) || 1}`;
  if (String(direction).toLowerCase() === "tb") return `${base}_tb_${LAYOUT_ALGORITHM_VERSION}`;
  return base;
}

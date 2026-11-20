"""QA 重试轮陈旧读取检测入库(第九期边核验 L3 / P4-d):真实编译图 + 确定性记录器桩。

12 场景矩阵 = 检索产物 有/无 × AI 转场 开/关 × QA 重试 0/1/3。每组断言:
- 每轮每个回路节点恰好触发 1 次(出现 2 次 = 屏障被拆成独立边 / 重复边);
- 一次性上游链(LM/SS/UC/FC)与 JOIN 整次运行恰好 1 次;
- 回路内消费者读到的循环体产物 stamp == 当前轮次(0 陈旧读 / 0 意外缺失读);
  检索关闭时 ``storyline_web_topic_artifact`` 是合法的软读取缺失(节点内软跳过),
  不计入缺失 —— 与测试文档 §6.4 的口径一致;
- 图停在 ``assembly_discover_and_probe`` 之前(interrupt 截停,JOIN 之后的第一站)。

桩机制:替换节点函数的模块属性后重新建图(add_node 在建图时取当前属性值);
GC 桩维护轮次计数,每个桩把 ``@r<轮次>`` 戳写进自己的产物键,消费桩记录
读到的 stamp。机制源自文档附录 D 的沙箱脚本 dyn_experiments_e29dd26.py(不入库)。
"""
from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import config
import graph as graph_module
import nodes.storyline.join_storyline as _join_mod
import nodes.storyline.qa_gate as _qa_mod

_LM = "storyline_load_media"
_SS = "storyline_split_shots"
_UC = "storyline_understand_clips"
_FC = "storyline_filter_clips"
_GC = "storyline_group_clips"
_STR = "storyline_script_template_recommendation"
_SW = "storyline_search_web_topic"
_SW_EN = "storyline_search_web_topic_en"
_GS = "storyline_generate_script"
_GV = "storyline_generate_voiceover"
_TXT = "storyline_recommend_text"
_SB = "storyline_select_bgm"
_RTR = "storyline_recommend_transition"
_GAT = "storyline_generate_ai_transition"
_PTP = "storyline_plan_timeline_pro"
_PTAT = "storyline_plan_timeline_ai_transition"
_QA = "storyline_qa_gate"
_JOIN = "storyline_join"

_STOP_NODE = "assembly_discover_and_probe"  # QC 开时 JOIN 之后的第一站

#: 注册名 -> (桩要替换的模块对象, 属性名)。QA/JOIN 经模块属性注册(graph.py:584-585),
#: 其余节点是 import 进 graph 命名空间的函数引用。
_TARGETS: dict[str, tuple[object, str]] = {
    _LM: (graph_module, "storyline_load_media_node"),
    _SS: (graph_module, "storyline_split_shots_node"),
    _UC: (graph_module, "storyline_understand_clips_node"),
    _FC: (graph_module, "storyline_filter_clips_node"),
    _GC: (graph_module, "storyline_group_clips_node"),
    _STR: (graph_module, "storyline_script_template_recommendation_node"),
    _SW: (graph_module, "storyline_search_web_topic_node"),
    _SW_EN: (graph_module, "storyline_search_web_topic_en_node"),
    _GS: (graph_module, "storyline_generate_script_node"),
    _GV: (graph_module, "storyline_generate_voiceover_node"),
    _TXT: (graph_module, "storyline_recommend_text_node"),
    _SB: (graph_module, "storyline_select_bgm_node"),
    _RTR: (graph_module, "storyline_recommend_transition_node"),
    _TXT: (graph_module, "storyline_recommend_text_node"),
    _PTP: (graph_module, "storyline_plan_timeline_pro_node"),
    _GAT: (graph_module, "storyline_generate_ai_transition_node"),
    _PTAT: (graph_module, "storyline_plan_timeline_ai_transition_node"),
    _QA: (_qa_mod, "storyline_qa_gate_node"),
    _JOIN: (_join_mod, "storyline_join_node"),
}

#: 桩节点产出的 state 键(带 @r<轮次> 戳)
_PRODUCES = {
    _LM: "storyline_media_artifact",
    _SS: "storyline_shots_artifact",
    _UC: "storyline_understanding_artifact",
    _FC: "storyline_filtered_clips",
    _GC: "storyline_groups_artifact",
    _STR: "storyline_script_template_artifact",
    _SW: "storyline_web_topic_artifact",
    _SW_EN: "storyline_web_topic_en_artifact",
    _GS: "storyline_script_artifact",
    _GV: "storyline_voiceover_artifact",
    _SB: "storyline_bgm_selection",
    _RTR: "storyline_transition_plan",
    _TXT: "storyline_text_style_plan",
    _PTP: "storyline_timeline_plan",
    _GAT: "storyline_ai_transition_artifact",
    _PTAT: "storyline_timeline_plan",
}
_DICT_KEYS = {"storyline_bgm_selection", "storyline_transition_plan", "storyline_text_style_plan"}
_WEB_KEY = "storyline_web_topic_artifact"

#: 消费者 -> 它读取的"循环体内上游产物键"(LM/SS/UC 产物不带轮次戳,不在此列)
_CONSUMES = {
    _STR: ["storyline_groups_artifact"],
    _SW: ["storyline_groups_artifact"],
    _GS: ["storyline_groups_artifact", "storyline_script_template_artifact", _WEB_KEY],
    _GV: ["storyline_script_artifact"],
    _SB: ["storyline_script_artifact", "storyline_voiceover_artifact"],
    _RTR: ["storyline_groups_artifact", "storyline_bgm_selection"],
    _TXT: ["storyline_groups_artifact", "storyline_script_artifact"],
    _PTP: [
        "storyline_groups_artifact",
        "storyline_script_artifact",
        "storyline_voiceover_artifact",
        "storyline_bgm_selection",
        "storyline_transition_plan",
        "storyline_text_style_plan",
    ],
    _GAT: ["storyline_groups_artifact", "storyline_transition_plan"],
    _PTAT: ["storyline_timeline_plan", "storyline_ai_transition_artifact"],
    _QA: ["storyline_timeline_plan"],
}

#: 每轮必跑的回路节点(SW_EN 由 GC 并行 fan-out,每轮必跑)
_LOOP_BASE = [_GC, _STR, _SW, _SW_EN, _GS, _GV, _TXT, _SB, _RTR, _PTP, _QA]
_ONCE = [_LM, _SS, _UC, _FC]


def _stamp_of(val):
    """从桩写入的值里取 @r<轮次>;缺失返回 None。"""
    if isinstance(val, dict):
        val = val.get("stamp")
    if isinstance(val, str):
        m = re.search(r"@r(\d+)$", val)
        if m:
            return int(m.group(1))
    return None


class _Harness:
    def __init__(self, *, web: bool, retries: int):
        self.web = web
        self.retries = retries
        self.round = 0
        self.qa_retried = 0
        self.reads: dict[str, list[tuple[int, dict[str, int | None]]]] = defaultdict(list)

    def _expected_missing(self, key: str, rnd: int) -> bool:
        """该轮该键"本来就不该有产物"→ 读到 None 不算缺失。"""
        return key == _WEB_KEY and not self.web

    def make_stub(self, name: str):
        key = _PRODUCES.get(name)

        def stub(state):
            if name == _GC:
                self.round += 1
            if name in _CONSUMES:
                self.reads[name].append(
                    (self.round, {k: _stamp_of(state.get(k)) for k in _CONSUMES[name]})
                )
            if name == _QA:
                if self.qa_retried < self.retries:
                    self.qa_retried += 1
                    return {"storyline_qa_route_signal": "storyline_qa_retry"}
                return {"storyline_qa_route_signal": "storyline_qa_done"}
            if name == _JOIN:
                return {"storyline_plan": {"clips": [1]}}
            if name == _SW and not self.web:  # 软跳过:不写产物,只照常返回
                return {}
            assert key is not None, f"桩 {name} 没有产出键"
            stamp = f"{key}@r{self.round}"
            return {key: {"stamp": stamp} if key in _DICT_KEYS else stamp}

        stub.__name__ = f"stub_{name}"
        return stub


def _run_scenario(monkeypatch: pytest.MonkeyPatch, *, web: bool, ai: bool, retries: int):
    h = _Harness(web=web, retries=retries)
    for name, (obj, attr) in _TARGETS.items():
        monkeypatch.setattr(obj, attr, h.make_stub(name))
    monkeypatch.setattr(graph_module, "is_ai_transition_enabled", lambda: ai)
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)

    g = graph_module._build_state_graph()
    compiled = g.compile(
        checkpointer=InMemorySaver(),
        interrupt_before=[_STOP_NODE, "generate_draft"],
    )
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 500}
    events: list[tuple[int, str]] = []
    err = None
    try:
        for ev in compiled.stream({"session_id": "s"}, cfg, stream_mode="debug"):
            if ev["type"] == "task":
                events.append((ev["step"], ev["payload"]["name"]))
    except Exception as e:  # noqa: BLE001
        err = repr(e)[:200]
    snap = compiled.get_state(cfg)
    return h, events, tuple(snap.next), err


@pytest.mark.parametrize("retries", [0, 1, 3])
@pytest.mark.parametrize("ai", [True, False], ids=["ai_on", "ai_off"])
@pytest.mark.parametrize("web", [True, False], ids=["web_on", "web_off"])
def test_retry_rounds_never_read_stale_artifacts(
    monkeypatch: pytest.MonkeyPatch, web: bool, ai: bool, retries: int
) -> None:
    h, events, nxt, err = _run_scenario(monkeypatch, web=web, ai=ai, retries=retries)

    assert err is None, f"图执行异常:{err}"
    assert nxt == (_STOP_NODE,), f"应停在 {web=} {ai=} {retries=}:next={nxt}"

    # 1) 每轮触发次数:回路节点每轮恰好 1 次
    per_round: dict[int, Counter] = defaultdict(Counter)
    rnd = 0
    for _step, name in events:
        if name == _GC:
            rnd += 1
        per_round[rnd][name] += 1
    assert rnd == retries + 1, f"轮次数 {rnd} != {retries + 1}"

    loop = set(_LOOP_BASE) | ({_GAT, _PTAT} if ai else set())
    for r in range(1, rnd + 1):
        for node in loop:
            assert per_round[r][node] == 1, (
                f"{web=} {ai=} {retries=}: 第 {r} 轮 {node} 触发 {per_round[r][node]} 次(应恰 1 次)"
            )
        extra = set(per_round[r]) - loop - {_JOIN}
        assert not extra, f"第 {r} 轮出现预期外的触发:{extra}"

    # 2) 一次性上游链与 JOIN:整次运行恰好 1 次
    total = Counter(name for _s, name in events)
    for node in _ONCE:
        assert total[node] == 1, f"{node} 应整次运行只执行 1 次,实际 {total[node]}"
    assert total[_JOIN] == 1
    assert total[_QA] == rnd

    # 3) 新鲜度:0 陈旧读 / 0 意外缺失读
    # 防空转:消费记录必须真的发生(每轮每消费者 1 条),否则下面的断言形同虚设
    expected_consumers = set(_CONSUMES) - (set() if ai else {_GAT, _PTAT})
    for node in expected_consumers:
        assert len(h.reads[node]) == rnd, (
            f"{node} 的新鲜度记录 {len(h.reads[node])} 条 != 轮数 {rnd}(桩读取记录失效)"
        )

    stale: list[str] = []
    for node, lst in h.reads.items():
        for r, stamps in lst:
            for key, stamp in stamps.items():
                if stamp is None:
                    if not h._expected_missing(key, r):
                        stale.append(f"{node} 第 {r} 轮读到缺失的 {key}")
                elif stamp != r:
                    stale.append(f"{node} 第 {r} 轮读到第 {stamp} 轮的旧 {key}")
    assert not stale, "发现陈旧/缺失读取:\n" + "\n".join(stale)

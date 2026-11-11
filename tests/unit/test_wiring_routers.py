"""单测:路由函数(纯函数,不编译图)。

- ``_storyline_route_after_template_rec`` 检索条件路由(初始挂 ``group_clips``
  之后,2026-11-12 noop 治理阶段1 改挂模板推荐出口):检索开关 AND ``build_web_topic_query`` 非空 →
  先检索再文案;否则直达文案。路由与检索节点共用同一 builder(设计方案 §4.1)。
- qa_gate 之后的路由不再被包装(AI 转场两节点在 qa_gate 之前,由建图时的屏障接线决定)

(原 ``_asr_branch_enabled`` / ``_storyline_route_after_split_shots`` 已随
2026-11-08 B1 ASR 移除删除。原 ``_web_topic_branch_enabled`` /
``_storyline_route_after_load_media`` 已随 2026-11-10 前置化阶段3 删除:
``load_media`` 改静态直连 ``split_shots``,检索条件路由移到 ``group_clips`` 之后(阶段1 再改挂模板推荐出口),
检索词改由 ``build_web_topic_query`` 自动构造 —— 其矩阵用例见
``tests/unit/test_web_topic_reference.py`` 的 builder 用例与 wiring 集成用例。)
"""
from __future__ import annotations

import pytest

import config
import graph as g
import nodes.storyline.node_search_web_topic as node_search_mod
from nodes.storyline import qa_gate as qa_gate_mod
from nodes.storyline import _common as common_mod


# ---------------------------------------------------------------------------
# _storyline_route_after_template_rec(前置化阶段3,设计方案 §4.3)
# ---------------------------------------------------------------------------
def test_route_after_group_clips_flag_off_goes_straight_to_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭 → 条件路由直达文案,即使检索词可构造。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", False)
    monkeypatch.setattr(g, "build_web_topic_query", lambda _state: "夏季防晒")
    assert g._storyline_route_after_template_rec({}) == "storyline_generate_script"


def test_route_after_group_clips_empty_builder_skips_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关开启但 builder 构造为空(query/overall/themes 全空)→ 直达文案。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    monkeypatch.setattr(g, "build_web_topic_query", lambda _state: "")
    assert g._storyline_route_after_template_rec({}) == "storyline_generate_script"


def test_route_after_group_clips_routes_to_search_when_builder_non_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关开启且能构造出检索词 → 先检索再文案。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    monkeypatch.setattr(g, "build_web_topic_query", lambda _state: "夏季防晒、整体概述")
    assert g._storyline_route_after_template_rec({}) == "storyline_search_web_topic"


def test_route_and_node_share_the_same_query_builder() -> None:
    """§4.1 一致性约束:路由判定与检索节点必须共用同一个 build_web_topic_query。"""
    assert g.build_web_topic_query is common_mod.build_web_topic_query
    assert node_search_mod.build_web_topic_query is common_mod.build_web_topic_query


# ---------------------------------------------------------------------------
# qa_gate 之后的路由:AI 转场两节点已挪到 qa_gate 之前(与 plan_timeline_pro 并排),
# 所以 qa 之后的路由**不再被包装**,仍是原函数(重试 → group_clips,否则 → join;
# 边治理阶段5:render_video 预览旁支已整体删除)。
# 2026-11-12 阶段1:权威依据改为 storyline_qa_route_signal 信号字段(qa_gate 每次
# 运行必写,不受 status_log 的 _append_unique 去重影响);旧 checkpoint 无字段时回退旧判据。
# ---------------------------------------------------------------------------
def test_qa_route_is_the_original_function_and_no_wrapper_left() -> None:
    assert not hasattr(g, "_route_after_storyline_qa")
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["x", "storyline_qa_retry"]}) == (
        "storyline_group_clips"
    )
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["storyline_qa_passed"]}) == (
        "storyline_join"
    )


def test_qa_route_follows_signal_field() -> None:
    """信号字段是权威依据:即使 status_log[-1] 被其他节点标签抢占也按信号路由。"""
    # retry 信号 → 回 group_clips;哪怕 log[-1] 是 PTP 的逐轮 warning
    assert qa_gate_mod.route_after_storyline_qa(
        {
            "storyline_qa_route_signal": "storyline_qa_retry",
            "status_log": ["storyline_qa_retry", "storyline_timeline_warning: x"],
        }
    ) == "storyline_group_clips"
    # done / failed / empty 信号 → join;同值重复写也能持续生效
    for signal in ("storyline_qa_done", "storyline_qa_failed", "storyline_qa_done_empty"):
        assert qa_gate_mod.route_after_storyline_qa(
            {"storyline_qa_route_signal": signal, "status_log": ["storyline_qa_retry"]}
        ) == "storyline_join", signal


def test_qa_route_falls_back_to_log_when_signal_missing() -> None:
    """旧 checkpoint 无信号字段 → 回退 status_log[-1] 旧判据(兼容中途恢复)。"""
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["storyline_qa_retry"]}) == (
        "storyline_group_clips"
    )
    assert qa_gate_mod.route_after_storyline_qa({}) == "storyline_join"

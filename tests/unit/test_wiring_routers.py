"""单测:2026-10 接入"不可达节点"新增 / 改动的路由函数(纯函数,不编译图)。

- ``_web_topic_branch_enabled``         网页话题分支:开关 AND query 非空
- ``_storyline_route_after_load_media`` 返回 str 或 list[str](并行 fan-out)
- qa_gate 之后的路由不再被包装(AI 转场两节点在 qa_gate 之前,由建图时的屏障接线决定)

(原 ``_asr_branch_enabled`` / ``_storyline_route_after_split_shots`` 已随
2026-11-08 B1 ASR 移除删除:``split_shots`` 改为静态直连 ``understand_clips``。
原 ``_storyline_route_after_load_media`` 内的 ``search_media`` Pexels 旁路已随
2026-11-10 检索素材移除删除:主干恒 ``split_shots``,不再读任何 env。)
"""
from __future__ import annotations

import pytest

import config
import graph as g
from nodes.storyline import qa_gate as qa_gate_mod


# ---------------------------------------------------------------------------
# _web_topic_branch_enabled
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "flag,targets,expected",
    [
        (True, {"query": "夏季防晒"}, True),
        (True, {"query": "  夏季防晒  "}, True),
        (True, {"query": "   "}, False),
        (True, {"query": ""}, False),
        (True, {"query": None}, False),
        (True, {"query": 123}, False),
        (True, {}, False),
        (True, None, False),
        (False, {"query": "夏季防晒"}, False),
    ],
)
def test_web_topic_branch_needs_flag_and_non_empty_query(
    monkeypatch: pytest.MonkeyPatch, flag: bool, targets, expected: bool
) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", flag)
    state = {} if targets is None else {"storyline_targets": targets}
    assert g._web_topic_branch_enabled(state) is expected


# ---------------------------------------------------------------------------
# _storyline_route_after_load_media
# ---------------------------------------------------------------------------
def test_after_load_media_default_is_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    # 没有 query → 没有网页分支,返回值仍是单个 str(与接入前完全一致)
    assert g._storyline_route_after_load_media({}) == "storyline_split_shots"


def test_after_load_media_adds_web_topic_as_parallel_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    state = {"storyline_targets": {"query": "夏季防晒"}}
    assert g._storyline_route_after_load_media(state) == [
        "storyline_split_shots",
        "storyline_search_web_topic",
    ]


def test_after_load_media_flag_off_equals_pre_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", False)
    state = {"storyline_targets": {"query": "夏季防晒"}}
    assert g._storyline_route_after_load_media(state) == "storyline_split_shots"


# ---------------------------------------------------------------------------
# qa_gate 之后的路由:AI 转场两节点已挪到 qa_gate 之前(与 plan_timeline_pro 并排),
# 所以 qa 之后的路由**不再被包装**,仍是原函数(重试 → group_clips,否则 → render_video)。
# ---------------------------------------------------------------------------
def test_qa_route_is_the_original_function_and_no_wrapper_left() -> None:
    assert not hasattr(g, "_route_after_storyline_qa")
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["x", "storyline_qa_retry"]}) == (
        "storyline_group_clips"
    )
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["storyline_qa_passed"]}) == (
        "storyline_render_video"
    )

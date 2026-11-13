"""单测:路由函数(纯函数,不编译图)。

- 2026-11-14 依赖约简阶段5:``_storyline_route_after_template_rec`` 检索条件路由
  已删除 —— web_topic 节点改为每轮必跑,「检索开关 AND 检索词非空」的判定移入
  节点内部(关闭/为空 → skipped 标签,不发网络请求、不写产物);原三态路由矩阵
  改写为节点级 skipped/执行矩阵(见下方 web topic 节点用例)。
- qa_gate 之后的路由不再被包装(AI 转场两节点在 qa_gate 之前,由建图时的屏障接线决定)

(原 ``_asr_branch_enabled`` / ``_storyline_route_after_split_shots`` 已随
2026-11-08 B1 ASR 移除删除。原 ``_web_topic_branch_enabled`` /
``_storyline_route_after_load_media`` 已随 2026-11-10 前置化阶段3 删除;
``_storyline_route_after_template_rec`` 已随 2026-11-14 依赖约简阶段5 删除 ——
检索词改由 ``build_web_topic_query`` 自动构造,矩阵用例见
``tests/unit/test_web_topic_reference.py`` 的 builder 用例、下方节点用例与
wiring 集成用例。)
"""
from __future__ import annotations

import pytest

import config
import graph as g
import nodes.storyline.node_search_web_topic as node_search_mod
from nodes.storyline import qa_gate as qa_gate_mod
from nodes.storyline import _common as common_mod


# ---------------------------------------------------------------------------
# web topic 节点内部判定(2026-11-14 依赖约简阶段5:每轮必跑 + 内部软跳过)
# ---------------------------------------------------------------------------
def test_web_topic_node_skips_without_network_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭 → 节点正常返回(不抛、不发网络请求、不写产物),只落 skipped 标签。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", False)
    calls: list[str] = []
    monkeypatch.setattr(node_search_mod, "cap_topic", lambda **kw: calls.append(kw) or {})
    out = node_search_mod.storyline_search_web_topic_node({})
    assert calls == [], "开关关闭不得发起网络请求"
    assert "storyline_web_topic_artifact" not in out, "skipped 不写产物(GS 软降级)"
    assert node_search_mod.SKIPPED_STATUS_TAG in (out.get("status_log") or [])


def test_web_topic_node_skips_when_query_builder_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关开启但 builder 构造为空(query/overall/themes 全空)→ 同款软跳过。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    monkeypatch.setattr(node_search_mod, "build_web_topic_query", lambda _state: "")
    calls: list[str] = []
    monkeypatch.setattr(node_search_mod, "cap_topic", lambda **kw: calls.append(kw) or {})
    out = node_search_mod.storyline_search_web_topic_node({})
    assert calls == [], "无检索词不得发起网络请求"
    assert "storyline_web_topic_artifact" not in out
    assert node_search_mod.SKIPPED_STATUS_TAG in (out.get("status_log") or [])


def test_web_topic_node_runs_when_flag_on_and_query_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """开关开启且能构造出检索词 → 正常检索并写产物(沿用既有能力层链)。"""
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    monkeypatch.setattr(node_search_mod, "build_web_topic_query", lambda _state: "夏季防晒")
    monkeypatch.setattr(
        node_search_mod, "cap_topic",
        lambda **kw: {"search_web_topic": [], "status": "no_results",
                      "error_code": "NO_RESULTS", "query_hash": kw.get("query", "")},
    )
    state = {"storyline_outputs_root": str(tmp_path)}
    out = node_search_mod.storyline_search_web_topic_node(state)
    assert "storyline_web_topic_artifact" in out, "执行轮应写检索产物"
    assert "storyline_search_web_topic_no_results" in (out.get("status_log") or [])


def test_node_uses_the_same_query_builder_as_capabilities() -> None:
    """一致性约束(§4.1 承接):检索节点必须共用同一个 build_web_topic_query。"""
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

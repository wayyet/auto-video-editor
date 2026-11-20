"""recommend_transition / recommend_text 失败必须刷新产物键(第九期边核验 R3b)。

背景:两个节点的失败分支只写 ``error_log`` 与 ``_failed`` 标签、不写产物键。
state 里这两个键是 ``_last_wins`` 字段,不写就保留上一轮的值 ——
QA 重试轮里推荐失败时,``plan_timeline_pro`` 会把上一轮的旧转场 / 旧文字样式
并进新时间线(与 R3 的配音同型问题;影响限于转场/文字样式,不涉及音频)。

清空必须写 ``{}`` 而不是 ``None``(dict 键):``_last_wins`` 对 ``None`` 的语义是
"保持原值"(state.py:82-90),写 None 等于没清。下游对空 dict 容缺省
(``plan_timeline_pro`` 用 ``state.get(...) or {}``;GAT 的提示解析把非 dict 当"无提示")。
"""
from __future__ import annotations

import pytest

import nodes.storyline.node_recommend_text as txtn
import nodes.storyline.node_recommend_transition as rtrn


def _rtr_fails(monkeypatch):
    def boom(**kw):
        raise RuntimeError("推荐服务不可用(模拟 QA 重试轮失败)")

    monkeypatch.setattr(rtrn, "recommend_transition", boom)


def _txt_fails(monkeypatch):
    def boom(**kw):
        raise RuntimeError("推荐服务不可用(模拟 QA 重试轮失败)")

    monkeypatch.setattr(txtn, "recommend_text", boom)


def test_transition_failure_clears_the_plan_key(tmp_path, monkeypatch):
    """节点级:RTR 失败分支必须带 storyline_transition_plan == {}(不是缺键,也不是 None)。"""
    _rtr_fails(monkeypatch)
    state = {
        "session_id": "s",
        "storyline_outputs_root": str(tmp_path),
        "storyline_transition_plan": {"stale": "ROUND1"},
        "error_log": [],
        "status_log": [],
    }
    delta = rtrn.storyline_recommend_transition_node(state)
    assert "storyline_recommend_transition_failed" in delta["status_log"]
    assert delta.get("storyline_transition_plan") == {}


def test_text_style_failure_clears_the_plan_key(tmp_path, monkeypatch):
    """节点级:TXT 失败分支必须带 storyline_text_style_plan == {}(不是缺键,也不是 None)。"""
    _txt_fails(monkeypatch)
    state = {
        "session_id": "s",
        "storyline_outputs_root": str(tmp_path),
        "storyline_text_style_plan": {"stale": "ROUND1"},
        "error_log": [],
        "status_log": [],
    }
    delta = txtn.storyline_recommend_text_node(state)
    assert "storyline_recommend_text_failed" in delta["status_log"]
    assert delta.get("storyline_text_style_plan") == {}

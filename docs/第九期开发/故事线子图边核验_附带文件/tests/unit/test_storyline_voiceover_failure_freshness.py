"""generate_voiceover 失败必须刷新产物键(R3):QA 重试轮里 TTS 失败时,下游不得读到上一轮的旧配音。

背景:失败分支只写 ``error_log`` 与 ``_failed`` 标签、不写 ``storyline_voiceover_artifact``。
state 里该键是 ``_last_wins`` 字段(state.py:288),不写就保留上一轮的值 ——
``select_bgm`` / ``plan_timeline_pro`` 读到旧配音,外层 ``node_08`` 还会把它写进最终草稿。

清空必须用 ``""`` 而不是 ``None``:``_last_wins`` 对 ``new is None`` 的语义是"保持原值"
(state.py:82-90),写 None 等于没清。
"""
from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

import nodes.storyline.node_generate_voiceover as gvn
import nodes.storyline.node_select_bgm as nsb
from nodes.storyline._common import _read_json_artifact
from state import WorkflowState


def _write_script(root):
    script = root / "storyline" / "script.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        json.dumps({"group_scripts": [{"group_id": "g1", "narration": "你好"}]}),
        encoding="utf-8",
    )
    return script


def _tts_fails_on_second_call(monkeypatch):
    calls = {"n": 0}

    def fake_generate_voiceover(*, group_scripts, output_dir, user_request):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("TTS 服务不可用(模拟 QA 重试轮失败)")
        return {"voiceover": [{"duration": 3000, "marker": "ROUND1"}], "stub_count": 0, "errors": []}

    monkeypatch.setattr(gvn, "install_tts_client_from_env", lambda: None)
    monkeypatch.setattr(gvn, "generate_voiceover", fake_generate_voiceover)


def test_failure_branch_clears_the_artifact_key(tmp_path, monkeypatch):
    """节点级:失败分支必须带上 storyline_voiceover_artifact == ""(不是缺键,也不是 None)。"""
    _tts_fails_on_second_call(monkeypatch)
    script = _write_script(tmp_path)
    state = {
        "session_id": "s",
        "storyline_outputs_root": str(tmp_path),
        "storyline_script_artifact": str(script),
        "error_log": [],
        "status_log": [],
    }
    first = gvn.storyline_generate_voiceover_node(state)  # 第 1 轮成功
    assert first["storyline_voiceover_artifact"].endswith("voiceover.json")

    state["storyline_voiceover_artifact"] = first["storyline_voiceover_artifact"]
    second = gvn.storyline_generate_voiceover_node(state)  # 第 2 轮失败
    assert "storyline_generate_voiceover_failed" in second["status_log"]
    assert second.get("storyline_voiceover_artifact") == ""


def test_two_rounds_through_langgraph_downstream_reads_no_stale_voiceover(tmp_path, monkeypatch):
    """图级(真实 reducer):第 2 轮失败后,select_bgm / plan_timeline_pro 读到的都是'没有配音'。"""
    _tts_fails_on_second_call(monkeypatch)
    script = _write_script(tmp_path)

    g = StateGraph(WorkflowState)
    g.add_node("gv", gvn.storyline_generate_voiceover_node)
    g.add_edge(START, "gv")
    g.add_edge("gv", END)
    graph = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "gv-freshness"}}

    graph.invoke(
        {
            "session_id": "s",
            "storyline_outputs_root": str(tmp_path),
            "storyline_script_artifact": str(script),
        },
        cfg,
    )
    round1 = dict(graph.get_state(cfg).values)
    assert nsb._read_voiceover_total_ms(round1) == 3000  # 第 1 轮:下游读到真配音

    graph.invoke({"session_id": "s"}, cfg)  # 第 2 轮:TTS 失败
    round2 = dict(graph.get_state(cfg).values)
    assert nsb._read_voiceover_total_ms(round2) == 0, "SB 读到了上一轮遗留的配音时长"
    assert _read_json_artifact(round2, "storyline_voiceover_artifact") is None, (
        "PTP 读到了上一轮遗留的配音文档"
    )

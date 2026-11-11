"""边治理阶段7:qa_gate 路由决策观测事件单测(不执行图)。

``storyline_qa_gate_node`` 的每个返回点都会经 LangGraph custom stream 上报
``qa_route`` 事件(signal + actual/target_ms/重试轮次),经
``monitoring/workflow_runner.py`` 的 ``handle_custom`` 通用通道进创建台事件详情
—— 后端/前端零改动。本文件用假 ``get_stream_writer`` 验证 payload 形状,以及
"观测异常绝不影响主流程"的纪律(与原 node_render_video 的 preview.* 事件同款)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import langgraph.config

from nodes.storyline import qa_gate as qa_gate_mod


def _capture_writer(
    monkeypatch: pytest.MonkeyPatch, *, raise_on_call: Exception | None = None
) -> list[dict]:
    """替换 ``langgraph.config.get_stream_writer``,捕获事件 payload。"""
    events: list[dict] = []

    def fake_get_stream_writer():
        if raise_on_call is not None:
            raise raise_on_call

        def writer(payload: dict) -> None:
            events.append(payload)

        return writer

    monkeypatch.setattr(langgraph.config, "get_stream_writer", fake_get_stream_writer)
    return events


def _make_state(**overrides: Any) -> dict:
    base: dict[str, Any] = {
        "storyline_targets": {"target_duration_ms": 35000},
        "storyline_timeline_plan": "",
        "storyline_qa_retry_count": 0,
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def _write_timeline(tmp_path: Path, *, timeline_out_ms: int) -> str:
    timeline = {
        "schema_version": "1.0",
        "job_id": "test",
        "created_at_ms": 0,
        "source_media": [
            {
                "media_id": "m1",
                "file_uri": "file:///x",
                "duration_ms": timeline_out_ms,
                "media_type": "video",
            }
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": timeline_out_ms,
                "timeline_in_ms": 0,
                "timeline_out_ms": timeline_out_ms,
            }
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": None, "en": None},
        "options": {
            "enable_ai_transition": False,
            "enable_voiceover": False,
            "max_duration_ms": timeline_out_ms,
        },
    }
    tp = tmp_path / "timeline_plan.json"
    tp.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    return str(tp)


def _assert_event_shape(payload: dict, signal: str) -> dict:
    assert payload["monitor_event"] == "progress"
    assert payload["node_id"] == "storyline_qa_gate"
    detail = payload["detail"]
    assert detail["event"] == "qa_route"
    assert detail["signal"] == signal
    return detail


def test_empty_plan_emits_done_empty_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _capture_writer(monkeypatch)
    delta = qa_gate_mod.storyline_qa_gate_node(_make_state())

    assert delta["storyline_qa_route_signal"] == "storyline_qa_done_empty"
    assert len(events) == 1
    detail = _assert_event_shape(events[0], "storyline_qa_done_empty")
    assert detail["reason"] == "空 plan / 时间线缺失"


def test_retry_event_carries_ms_and_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """总时长超限 → retry 事件带 actual/target_ms 与下一轮重试序号。"""
    events = _capture_writer(monkeypatch)
    plan = _write_timeline(tmp_path, timeline_out_ms=5000)
    delta = qa_gate_mod.storyline_qa_gate_node(
        _make_state(
            storyline_timeline_plan=plan,
            storyline_targets={"target_duration_ms": 1000},
        )
    )

    assert delta["storyline_qa_route_signal"] == "storyline_qa_retry"
    assert delta["storyline_qa_retry_count"] == 1
    assert len(events) == 1
    detail = _assert_event_shape(events[0], "storyline_qa_retry")
    assert detail["actual_ms"] == 5000
    assert detail["target_ms"] == 1000
    assert detail["retry_count"] == 1


def test_done_event_carries_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _capture_writer(monkeypatch)
    plan = _write_timeline(tmp_path, timeline_out_ms=5000)
    delta = qa_gate_mod.storyline_qa_gate_node(_make_state(storyline_timeline_plan=plan))

    assert delta["storyline_qa_route_signal"] == "storyline_qa_done"
    assert len(events) == 1
    detail = _assert_event_shape(events[0], "storyline_qa_done")
    assert detail["actual_ms"] == 5000
    assert detail["target_ms"] == 35000


def test_writer_failure_never_breaks_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图运行时外(或观测通道异常)→ 事件静默放弃,节点照常返回路由信号。"""
    _capture_writer(monkeypatch, raise_on_call=RuntimeError("outside graph runtime"))
    delta = qa_gate_mod.storyline_qa_gate_node(_make_state())

    assert delta["storyline_qa_route_signal"] == "storyline_qa_done_empty"
    assert any("storyline_qa_done_empty" in t for t in delta["status_log"])


def test_contract_invalid_emits_failed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON 合法但 Pydantic 校验失败 → failed 事件 + error_log 结构化留痕。

    (注意:JSON 语法损坏走 ``json.JSONDecodeError → None → done_empty`` 分支,
    ``ContractInvalid`` 只由"能解析但不符合 CanonicalTimeline 契约"触发。)
    """
    events = _capture_writer(monkeypatch)
    bad = tmp_path / "bad_contract.json"
    bad.write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")
    delta = qa_gate_mod.storyline_qa_gate_node(_make_state(storyline_timeline_plan=str(bad)))

    assert delta["storyline_qa_route_signal"] == "storyline_qa_failed"
    assert len(events) == 1
    _assert_event_shape(events[0], "storyline_qa_failed")
    assert any("CONTRACT_INVALID" in line for line in delta["error_log"])

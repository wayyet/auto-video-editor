"""resume_workflow.py 纯函数单测(阶段3前置:直接 CLI checkpoint 恢复)。

只测可脱离 Postgres / langgraph 的纯逻辑:``parse_resume_value`` /
``describe_interrupts`` / ``match_interrupt``。脚本经 importlib 按路径加载
(scripts/ 不是包),加载本身不得触发 graph / db import。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "resume_workflow.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("resume_workflow_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rw():
    return _load_script()


# ---------------------------------------------------------------------------
# parse_resume_value
# ---------------------------------------------------------------------------
class TestParseResumeValue:
    def test_json_literals(self, rw):
        assert rw.parse_resume_value("true") is True
        assert rw.parse_resume_value("false") is False
        assert rw.parse_resume_value("null") is None
        assert rw.parse_resume_value("1") == 1

    def test_json_object(self, rw):
        assert rw.parse_resume_value('{"a": 1}') == {"a": 1}

    def test_non_json_falls_back_to_bool_then_string(self, rw):
        assert rw.parse_resume_value("TRUE") is True
        assert rw.parse_resume_value("False") is False
        assert rw.parse_resume_value("abc") == "abc"


# ---------------------------------------------------------------------------
# describe_interrupts
# ---------------------------------------------------------------------------
def _snapshot(interrupts):
    return SimpleNamespace(interrupts=interrupts)


class TestDescribeInterrupts:
    def test_dict_payload_fields(self, rw):
        snap = _snapshot(
            [
                SimpleNamespace(
                    id="abc123",
                    value={
                        "checkpoint": "①",
                        "legacy_id": "checkpoint1_reorder",
                        "step": 6,
                        "instructions": "在剪映手动调整",
                    },
                )
            ]
        )
        out = rw.describe_interrupts(snap)
        assert len(out) == 1
        assert out[0]["id"] == "abc123"
        assert out[0]["checkpoint"] == "①"
        assert out[0]["legacy_id"] == "checkpoint1_reorder"
        assert out[0]["step"] == 6

    def test_non_dict_payload_wrapped(self, rw):
        snap = _snapshot([SimpleNamespace(id="x", value="plain")])
        out = rw.describe_interrupts(snap)
        assert out[0]["checkpoint"] is None
        assert out[0]["payload"] == {"value": "plain"}

    def test_no_interrupts(self, rw):
        assert rw.describe_interrupts(_snapshot([])) == []
        assert rw.describe_interrupts(_snapshot(None)) == []
        assert rw.describe_interrupts(SimpleNamespace()) == []


# ---------------------------------------------------------------------------
# match_interrupt
# ---------------------------------------------------------------------------
def _make_interrupts():
    return [
        {
            "id": "i-1",
            "checkpoint": "①",
            "legacy_id": "checkpoint1_reorder",
            "step": 6,
            "instructions": "reorder",
            "payload": {},
        },
        {
            "id": "i-2",
            "checkpoint": "②",
            "legacy_id": "checkpoint2_add_bgm",
            "step": 12,
            "instructions": "bgm",
            "payload": {},
        },
    ]


class TestMatchInterrupt:
    def test_match_by_checkpoint_marker(self, rw):
        assert rw.match_interrupt(_make_interrupts(), "②")["id"] == "i-2"

    def test_match_by_legacy_id(self, rw):
        assert rw.match_interrupt(_make_interrupts(), "checkpoint1_reorder")["id"] == "i-1"

    def test_match_by_interrupt_id(self, rw):
        assert rw.match_interrupt(_make_interrupts(), "i-2")["id"] == "i-2"

    def test_no_match_returns_none(self, rw):
        assert rw.match_interrupt(_make_interrupts(), "⑨") is None

    def test_omitted_with_single_interrupt(self, rw):
        sole = [_make_interrupts()[1]]
        assert rw.match_interrupt(sole, None)["id"] == "i-2"
        assert rw.match_interrupt(sole, "  ")["id"] == "i-2"

    def test_omitted_with_multiple_is_ambiguous(self, rw):
        assert rw.match_interrupt(_make_interrupts(), None) is None

    def test_empty_interrupts(self, rw):
        assert rw.match_interrupt([], "①") is None
        assert rw.match_interrupt([], None) is None

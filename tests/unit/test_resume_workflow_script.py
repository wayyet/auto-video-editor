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


# ---------------------------------------------------------------------------
# --continue 续跑(2026-11-17 创建台失败重试:无挂起关卡从最后 checkpoint 续跑)
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402


def _continue_args(**over):
    ns = SimpleNamespace(
        thread_id="prod-001", dry_run=False, allow_jianying_running=False,
    )
    return SimpleNamespace(**{**vars(ns), **over})


class TestContinueAfterFailure:
    def test_parse_args_continue_flag(self, rw, monkeypatch):
        monkeypatch.setattr("sys.argv", ["resume_workflow", "--thread-id", "t", "--continue"])
        args = rw._parse_args()
        assert args.continue_run is True

    def test_continue_and_checkpoint_mutually_exclusive(self, rw):
        """--continue 与 --checkpoint 同给 → 退出码 2(参数非法),不做任何环境准备。"""
        args = SimpleNamespace(
            production=False, thread_id="t", checkpoint="①", continue_run=True,
        )
        rc = asyncio.run(rw._main_async(args))
        assert rc == rw.EXIT_PREFLIGHT == 2

    def test_no_checkpoint_returns_7_fail_closed(self, rw):
        """Postgres 确无 checkpoint → 退出码 7,绝不盲目续跑。"""

        class FakeSaver:
            async def aget_tuple(self, config):
                return None

        rc = asyncio.run(rw._continue_after_failure(
            object(), FakeSaver(), {"configurable": {"thread_id": "t"}},
            _continue_args(), SimpleNamespace(values={}),
        ))
        assert rc == rw.EXIT_NO_CHECKPOINT == 7

    def test_db_query_failure_returns_7_fail_closed(self, rw):
        """查询失败(数据库不可达)≠ 无 checkpoint:同样拒绝,语义准确。"""

        class BoomSaver:
            async def aget_tuple(self, config):
                raise RuntimeError("pg down")

        rc = asyncio.run(rw._continue_after_failure(
            object(), BoomSaver(), {"configurable": {"thread_id": "t"}},
            _continue_args(), SimpleNamespace(values={}),
        ))
        assert rc == rw.EXIT_NO_CHECKPOINT == 7

    def test_dry_run_with_checkpoint_returns_0(self, rw):
        """--continue --dry-run:确认 checkpoint 存在即返回,不执行续跑。"""

        class FakeSaver:
            async def aget_tuple(self, config):
                return SimpleNamespace(checkpoint={"ts": "2026-11-17T00:00:00Z"})

        rc = asyncio.run(rw._continue_after_failure(
            object(), FakeSaver(), {"configurable": {"thread_id": "t"}},
            _continue_args(dry_run=True), SimpleNamespace(values={}),
        ))
        assert rc == rw.EXIT_OK

    def test_guard_fail_returns_6(self, rw, monkeypatch):
        """state 有 draft_path 时照常执行文件门禁(剪映未关 → 退出码 6)。"""

        class FakeSaver:
            async def aget_tuple(self, config):
                return SimpleNamespace(checkpoint={"ts": "t"})

        monkeypatch.setattr(
            "draft_ops.draft_lock.assert_resume_ready",
            lambda draft_file, **k: {
                "ok": False, "problems": ["检测到剪映客户端正在运行"],
                "hashes": {}, "jianying_running": True,
            },
        )
        snap = SimpleNamespace(values={"draft_path": "X:/d/draft_content.json"})
        rc = asyncio.run(rw._continue_after_failure(
            object(), FakeSaver(), {"configurable": {"thread_id": "t"}},
            _continue_args(), snap,
        ))
        assert rc == rw.EXIT_RESUME_GUARD

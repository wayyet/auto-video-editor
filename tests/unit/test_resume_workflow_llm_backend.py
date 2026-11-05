"""scripts/resume_workflow.py --llm-backend 注入单元测试(第二期阶段 5)。

背景:此前 resume 入口没有 LLM 注入通道,生产环境恢复的运行会静默回落
懒加载 Stub —— 阶段 5 与 run_workflow --llm-backend 对齐补齐。
覆盖:默认不注入(旧 CLI 行为不变)/ 已知后端解析 / 注入发生在图执行前。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import storyline_capabilities.vlm_client as vlm  # noqa: E402
from scripts import resume_workflow as rw  # noqa: E402


class TestParseArgs:
    def test_default_is_none_keeps_legacy_behavior(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["resume_workflow", "--thread-id", "t"])
        assert rw._parse_args().llm_backend is None

    def test_accepts_known_backends(self, monkeypatch):
        for backend in ("stub", "claude-cli", "mcode-cli"):
            monkeypatch.setattr(
                sys, "argv",
                ["resume_workflow", "--thread-id", "t", "--llm-backend", backend],
            )
            assert rw._parse_args().llm_backend == backend

    def test_rejects_unknown_backend(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["resume_workflow", "--thread-id", "t", "--llm-backend", "gpt-4"],
        )
        with pytest.raises(SystemExit):
            rw._parse_args()


class TestApplyLlmBackend:
    def test_none_injects_nothing(self, monkeypatch):
        built, injected = [], []
        monkeypatch.setattr(
            "agent_gate.factory.build_client", lambda b: built.append(b)
        )
        monkeypatch.setattr(
            vlm, "set_default_client", lambda c: injected.append(c)
        )
        rw._apply_llm_backend(None)
        assert built == [] and injected == []

    def test_stub_injects_built_client(self, monkeypatch):
        sentinel = object()
        built, injected = [], []
        monkeypatch.setattr(
            "agent_gate.factory.build_client",
            lambda b: (built.append(b), sentinel)[1],
        )
        monkeypatch.setattr(
            vlm, "set_default_client", lambda c: injected.append(c)
        )
        rw._apply_llm_backend("stub")
        assert built == ["stub"]
        assert injected == [sentinel]

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setattr(
            "agent_gate.factory.build_client",
            lambda b: (_ for _ in ()).throw(ValueError(f"未知后端:{b!r}")),
        )
        with pytest.raises(ValueError):
            rw._apply_llm_backend("nonexistent")


class TestResumeAsyncOrdering:
    def test_backend_applied_before_graph_work(self, monkeypatch):
        """注入必须发生在任何图/checkpointer 工作之前。"""
        calls: list = []
        monkeypatch.setattr(
            rw, "_apply_llm_backend", lambda b: calls.append(("llm", b))
        )

        async def fake_resume(args):
            calls.append("resume")
            return 0

        monkeypatch.setattr(rw, "_resume_async_impl", fake_resume)
        args = argparse.Namespace(thread_id="t", llm_backend="mcode-cli")
        assert asyncio.run(rw._resume_async(args)) == 0
        assert calls == [("llm", "mcode-cli"), "resume"]

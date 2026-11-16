"""scripts/run_workflow.py --llm-backend 注入单元测试(第二期阶段 2)。

覆盖计划 §11 阶段 2 任务行:增加 --llm-backend / 接入 build_client() /
注入 set_default_client() / 保留 CLI 旧接口兼容(默认不注入)。

2026-11-17 P1:注入改走公共装配 ``agent_gate.bootstrap.install_llm_backend`` ——
同一 client 实例同时注入 VLM 与英文翻译;stub 保持翻译侧 Mock。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import run_workflow as rw  # noqa: E402
import storyline_capabilities.vlm_client as vlm  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_global_clients():
    """注入测试会改写模块级默认 client(VLM+翻译),用例后必须还原。"""
    from jy_common.translate_client import MockTranslateClient, set_default_client

    yield
    vlm.set_default_client(None)
    set_default_client(MockTranslateClient())


class TestParseArgs:
    def test_default_is_none_keeps_legacy_behavior(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_workflow"])
        args = rw._parse_args()
        assert args.llm_backend is None

    def test_accepts_known_backends(self, monkeypatch):
        for backend in ("stub", "claude-cli", "mcode-cli"):
            monkeypatch.setattr(
                sys, "argv", ["run_workflow", "--llm-backend", backend]
            )
            assert rw._parse_args().llm_backend == backend

    def test_rejects_unknown_backend(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["run_workflow", "--llm-backend", "gpt-4"]
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

    def test_mcode_backend_reaches_factory(self, monkeypatch):
        built = []
        monkeypatch.setattr(
            "agent_gate.factory.build_client", lambda b: built.append(b)
        )
        monkeypatch.setattr(vlm, "set_default_client", lambda c: None)
        rw._apply_llm_backend("mcode-cli")
        assert built == ["mcode-cli"]

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setattr(
            "agent_gate.factory.build_client",
            lambda b: (_ for _ in ()).throw(ValueError(f"未知后端:{b!r}")),
        )
        with pytest.raises(ValueError):
            rw._apply_llm_backend("nonexistent")


class TestTranslationAdapterAssembly:
    """2026-11-17 P1:同一 client 实例同时装配 VLM 与翻译(修订计划 §5.3)。"""

    def test_real_backend_shares_client_instance(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(
            "agent_gate.factory.build_client", lambda b: sentinel
        )
        rw._apply_llm_backend("mcode-cli")

        assert vlm.get_default_client() is sentinel
        from jy_common.translate_client import (
            LLMTranslateClient,
            get_default_client,
        )

        translator = get_default_client()
        assert isinstance(translator, LLMTranslateClient)
        assert translator._llm is sentinel  # 同一实例,非二次构建
        assert translator.translator_id == "mcode-cli"

    def test_stub_backend_keeps_mock_translator(self, monkeypatch):
        from jy_common.translate_client import (
            MockTranslateClient,
            get_default_client,
        )

        monkeypatch.setattr("agent_gate.factory.build_client", lambda b: None)
        rw._apply_llm_backend("stub")
        assert isinstance(get_default_client(), MockTranslateClient)


class TestMainAsyncOrdering:
    def test_backend_applied_before_invoke(self, monkeypatch):
        """注入必须发生在执行图之前(首节点就要用上真实模型)。"""
        calls: list = []
        monkeypatch.setattr(rw, "_maybe_preflight", lambda args: 0)
        monkeypatch.setattr(
            rw, "_apply_llm_backend", lambda b: calls.append(("llm", b))
        )

        async def fake_invoke(args):
            calls.append("invoke")
            return 0

        monkeypatch.setattr(rw, "_invoke_async", fake_invoke)
        args = argparse.Namespace(production=False, llm_backend="mcode-cli")
        assert asyncio.run(rw._main_async(args)) == 0
        assert calls == [("llm", "mcode-cli"), "invoke"]

    def test_no_backend_flag_skips_injection(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(rw, "_maybe_preflight", lambda args: 0)
        monkeypatch.setattr(
            rw, "_apply_llm_backend", lambda b: calls.append(("llm", b))
        )

        async def fake_invoke(args):
            calls.append("invoke")
            return 0

        monkeypatch.setattr(rw, "_invoke_async", fake_invoke)
        args = argparse.Namespace(production=False, llm_backend=None)
        assert asyncio.run(rw._main_async(args)) == 0
        assert calls == [("llm", None), "invoke"]  # 传 None = 旧行为,不注入

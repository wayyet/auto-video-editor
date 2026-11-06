"""入口 A 的 LLM 后端提示(附件评审 20261105 阶段3G / 决策 D3-A)单测。

覆盖:未设 VIDEO_LLM_BACKEND / 设为 stub → 只打印提示返回 0;
设为非 stub(claude-cli)→ 退出码 2;显式指定 --llm-backend → 短路不打扰。
"""

from __future__ import annotations

import pytest

from scripts._entry_a_llm import VIDEO_LLM_BACKEND_ENV, llm_backend_notice


def test_no_env_prints_stub_notice_and_returns_0(monkeypatch, capsys):
    """未设 VIDEO_LLM_BACKEND:只打印 stub 假答案提示,返回 0 继续。"""
    monkeypatch.delenv(VIDEO_LLM_BACKEND_ENV, raising=False)
    rc = llm_backend_notice(None)
    captured = capsys.readouterr()
    assert rc == 0
    assert "stub 假答案" in captured.err
    assert "--llm-backend" in captured.err


def test_env_stub_prints_notice_and_returns_0(monkeypatch, capsys):
    """VIDEO_LLM_BACKEND=stub:提示照打(仍是假答案口径),返回 0 继续。"""
    monkeypatch.setenv(VIDEO_LLM_BACKEND_ENV, "stub")
    rc = llm_backend_notice(None)
    captured = capsys.readouterr()
    assert rc == 0
    assert "stub 假答案" in captured.err


def test_env_non_stub_rejected_with_exit_2(monkeypatch, capsys):
    """VIDEO_LLM_BACKEND=claude-cli 且未显式指定 --llm-backend:退出码 2。"""
    monkeypatch.setenv(VIDEO_LLM_BACKEND_ENV, "claude-cli")
    rc = llm_backend_notice(None)
    captured = capsys.readouterr()
    assert rc == 2
    assert "claude-cli" in captured.err
    assert "入口 A 不读取该变量" in captured.err


def test_explicit_backend_short_circuits(monkeypatch, capsys):
    """显式指定 --llm-backend 时短路:不打印任何提示,尊重调用方。"""
    monkeypatch.setenv(VIDEO_LLM_BACKEND_ENV, "claude-cli")
    rc = llm_backend_notice("claude-cli")
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""

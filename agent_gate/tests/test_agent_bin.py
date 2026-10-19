"""Agent 可执行文件解析(Windows PATHEXT 坑)。

背景:``subprocess.Popen`` 在 Windows 上走 ``CreateProcess``,**不做 PATHEXT 补全**。
裸名 ``mcode`` 一定 ``WinError 2``,即使 ``mcode.CMD`` 就在 PATH 里
(本机 ``C:\\Users\\wayyet\\.minimax-code\\mcode.CMD`` 就是这种只有 ``.CMD``
没有 ``.exe`` 的情况)。``shutil.which`` 会按 PATHEXT 找到真正的文件。

回归这条,避免 drive 在 Windows 上又出现
「找不到可执行文件 'mcode'」→ 连续会话没进展 → 退出码 4。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from agent_gate.launcher import (
    ClaudeLauncher,
    McodeLauncher,
    resolve_agent_bin,
)


def test_resolve_agent_bin_keeps_explicit_path(tmp_path: Path) -> None:
    p = tmp_path / "agent.exe"
    p.write_bytes(b"x")
    assert resolve_agent_bin(str(p)) == str(p)


def test_resolve_agent_bin_finds_pathext_variant(tmp_path: Path, monkeypatch) -> None:
    """PATH 里只有 ``.CMD`` 时,必须解析到那个 ``.CMD``。"""
    d = tmp_path / "bin"
    d.mkdir()
    cmd = d / "mytool.CMD"
    cmd.write_text("@echo off\r\n", encoding="ascii")
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    assert resolve_agent_bin("mytool").lower() == str(cmd).lower()


def test_resolve_agent_bin_returns_input_when_not_found() -> None:
    assert resolve_agent_bin("definitely-not-a-real-binary-xyz") == (
        "definitely-not-a-real-binary-xyz"
    )


def test_resolve_agent_bin_empty() -> None:
    assert resolve_agent_bin("") == ""


def test_mcode_launcher_resolves_bare_name(tmp_path: Path, monkeypatch) -> None:
    """McodeLauncher 拿到裸名时,executable 必须是能直接 CreateProcess 的路径。"""
    d = tmp_path / "bin"
    d.mkdir()
    cmd = d / "mcode.CMD"
    cmd.write_text("@echo off\r\n", encoding="ascii")
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("AGENT_GATE_MCODE_BIN", raising=False)

    launcher = McodeLauncher(project_dir=str(tmp_path))
    # Windows 的 shutil.which 返回 PATHEXT 大写形式(.EXE/.CMD),比对要忽略大小写
    assert launcher.executable.lower() == str(cmd).lower()
    assert launcher.command()[0].lower() == str(cmd).lower()


def test_claude_launcher_resolves_bare_name(tmp_path: Path, monkeypatch) -> None:
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "claude.exe"
    exe.write_bytes(b"x")
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("AGENT_GATE_CLAUDE_BIN", raising=False)

    launcher = ClaudeLauncher(project_dir=str(tmp_path))
    assert launcher.executable.lower() == str(exe).lower()


def test_env_var_override_still_wins(tmp_path: Path, monkeypatch) -> None:
    """显式给的 AGENT_GATE_MCODE_BIN 优先于 PATH 查找。"""
    explicit = tmp_path / "custom-mcode.exe"
    explicit.write_bytes(b"x")
    monkeypatch.setenv("AGENT_GATE_MCODE_BIN", str(explicit))
    launcher = McodeLauncher(project_dir=str(tmp_path))
    assert launcher.executable == str(explicit)

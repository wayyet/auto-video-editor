"""Agent 可执行文件解析(Windows PATHEXT 坑 + PATH 缺失兜底)。

背景:``subprocess.Popen`` 在 Windows 上走 ``CreateProcess``,**不做 PATHEXT 补全**。
裸名 ``mcode`` 一定 ``WinError 2``,即使 ``mcode.CMD`` 就在 PATH 里
(本机 ``C:\\Users\\wayyet\\.minimax-code\\mcode.CMD`` 就是这种只有 ``.CMD``
没有 ``.exe`` 的情况)。``shutil.which`` 会按 PATHEXT 找到真正的文件。

第二层背景:本机 ``C:\\Users\\wayyet\\.minimax-code`` **默认常常不在 PATH**。
那时 ``shutil.which("mcode")`` 返回 None,后果是两个方向的隐蔽失败:

- kit 测试里 ``@needs_mcode`` 的用例被**静默 skip**(汇总数字分不出 skip 和 pass);
- ``drive`` 拉起 mcode 报 ``WinError 2`` → 连续会话没进展 → 退出码 4(stalled)。

所以解析要能**在 PATH 之外**找到安装目录,并且测试脚手架和 launcher
必须用**同一个**解析入口 —— 否则又会退化成「一处能找到、另一处找不到」。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from agent_gate.agent_bin import (
    augment_path,
    candidate_dirs,
    resolve_agent_bin,
    resolve_executable,
)
from agent_gate.launcher import (
    ClaudeLauncher,
    McodeLauncher,
)


def _fake_exe(directory: Path, stem: str) -> Path:
    """在 directory 里放一个假的可执行文件(Windows 用 .CMD,POSIX 不用后缀)。"""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / (f"{stem}.CMD" if os.name == "nt" else stem)
    exe.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="ascii")
    return exe


def _fake_home(tmp_path: Path, monkeypatch) -> Path:
    """把用户目录指到临时目录,并把 PATH 清空(模拟「安装目录不在 PATH」)。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    empty = tmp_path / "empty-bin"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("AGENT_GATE_BIN_DIRS", "")
    monkeypatch.delenv("AGENT_GATE_MCODE_BIN", raising=False)
    monkeypatch.delenv("AGENT_GATE_CLAUDE_BIN", raising=False)
    return home


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


# ---------------------------------------------------------------- PATH 缺失兜底
def test_resolve_executable_finds_mcode_outside_path(tmp_path: Path, monkeypatch) -> None:
    """PATH 里没有 mcode,但 ``~/.minimax-code`` 里有 → 必须能找到。"""
    home = _fake_home(tmp_path, monkeypatch)
    exe = _fake_exe(home / ".minimax-code", "mcode")
    assert shutil.which("mcode") is None, "测试前提失效:PATH 里居然能找到 mcode"

    found = resolve_executable("mcode")
    assert found is not None, "PATH 里没有就该去安装目录找,不能直接放弃"
    assert Path(found).name.lower() == exe.name.lower()


def test_resolve_agent_bin_finds_mcode_outside_path(tmp_path: Path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    exe = _fake_exe(home / ".minimax-code", "mcode")
    resolved = resolve_agent_bin("mcode")
    assert Path(resolved).exists(), f"解析到不存在的文件:{resolved}"
    assert Path(resolved).name.lower() == exe.name.lower()


def test_mcode_launcher_resolves_without_path(tmp_path: Path, monkeypatch) -> None:
    """launcher 在 PATH 缺失时也要解析到 mcode —— 这正是 drive 退出码 4 的根因。"""
    home = _fake_home(tmp_path, monkeypatch)
    exe = _fake_exe(home / ".minimax-code", "mcode")
    launcher = McodeLauncher(project_dir=str(tmp_path))
    assert Path(launcher.executable).exists(), (
        f"executable 指向不存在的文件:{launcher.executable}"
    )
    assert Path(launcher.executable).name.lower() == exe.name.lower()
    assert Path(launcher.command()[0]).exists()


def test_candidate_dirs_include_home_fallback(tmp_path: Path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    dirs = [str(d) for d in candidate_dirs("mcode")]
    assert any(d.endswith(".minimax-code") for d in dirs), dirs
    assert str(home) in dirs[0] or any(str(home) in d for d in dirs)


def test_env_var_is_returned_verbatim_even_if_missing(tmp_path: Path, monkeypatch) -> None:
    """环境变量写错了要**响亮报错**,不能变成静默跳过。"""
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_GATE_MCODE_BIN", str(tmp_path / "nope" / "mcode.cmd"))
    found = resolve_executable("mcode")
    assert found == str(tmp_path / "nope" / "mcode.cmd")


# ------------------------------------------------- 静默 skip 的护栏(最重要的一条)
def test_support_never_silently_skips_mcode() -> None:
    """装了 mcode 就必须被测试脚手架认出来。

    这是「静默 skip」的护栏:``support.py`` 一旦退回用 ``shutil.which``,
    本机(mcode 不在 PATH)就会把 10 条 ``@needs_mcode`` 用例静默 skip 掉 ——
    汇总数字(17 skip / 0 failure)完全正常,回归会被当成没发生。
    这条测试让它**响亮地失败**。
    """
    from support import MCODE_BIN

    resolved = resolve_executable("mcode")
    if resolved is None:
        pytest.skip("本机确实没有 mcode(PATH / AGENT_GATE_MCODE_BIN / ~/.minimax-code 都没有)")
    assert MCODE_BIN, "support.MCODE_BIN 为空 → @needs_mcode 用例会被静默 skip"
    assert Path(MCODE_BIN).exists(), f"support.MCODE_BIN 指向不存在的文件:{MCODE_BIN}"


def test_support_and_launcher_use_the_same_resolver() -> None:
    """脚手架和 launcher 必须用同一个解析入口,否则又会「一处能找到、一处找不到」。"""
    from support import MCODE_BIN

    from agent_gate.launcher import resolve_agent_bin as launcher_resolve

    assert bool(MCODE_BIN) == bool(launcher_resolve("mcode"))


# ---------------------------------------------------------------- ffmpeg 兜底
def test_media_honors_ffmpeg_env_override(tmp_path: Path, monkeypatch) -> None:
    """media 抽帧用的 ffmpeg 也走统一解析(找不到就静默出不了图,必须能配)。"""
    from agent_gate import media

    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("AGENT_GATE_FFMPEG_BIN", str(fake))
    assert media._tool("ffmpeg") == str(fake)


def test_augment_path_prepends_tool_dir(tmp_path: Path, monkeypatch) -> None:
    """子进程 PATH 要补上工具目录 —— 否则 worker 里抽帧静默失败(0 张图,不报错)。"""
    d = tmp_path / "bin"
    exe = _fake_exe(d, "ffmpeg")
    monkeypatch.setenv("AGENT_GATE_FFMPEG_BIN", str(exe))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    out = augment_path(os.environ.get("PATH", ""), ("ffmpeg",))
    assert out.split(os.pathsep)[0] == str(d)


def test_augment_path_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    d = tmp_path / "bin"
    exe = _fake_exe(d, "ffmpeg")
    monkeypatch.setenv("AGENT_GATE_FFMPEG_BIN", str(exe))
    once = augment_path(str(d), ("ffmpeg",))
    twice = augment_path(once, ("ffmpeg",))
    assert once == twice

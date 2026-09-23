"""精简版 RunContext 的纯逻辑测试(无外部依赖,无 ffmpeg)。"""
from __future__ import annotations

import pytest

from assembly_capabilities.run_context import (
    RunContext,
    reject_input_output_collision,
)


def test_session_kind_must_be_pipeline():
    with pytest.raises(ValueError, match="pipeline"):
        RunContext(session_kind="mcp")
    with pytest.raises(ValueError, match="pipeline"):
        RunContext(session_kind="cli")
    ctx = RunContext(session_kind="pipeline")
    assert ctx.session_kind == "pipeline"


def test_project_dir_is_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = RunContext()
    assert ctx.project_dir == tmp_path.resolve()
    assert ctx.output_dir == tmp_path.resolve() / "out"
    assert ctx.work_dir == tmp_path.resolve() / ".video_agent"
    # out / .video_agent 已自动创建
    assert ctx.output_dir.is_dir()
    assert ctx.work_dir.is_dir()


def test_resolve_absolute_path(tmp_path):
    ctx = RunContext()
    target = tmp_path / "file.mp4"
    resolved = ctx.resolve(target)
    assert resolved == target.resolve()


def test_resolve_relative_anchored_to_project_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = RunContext()
    resolved = ctx.resolve("foo/bar.mp4")
    assert resolved == (tmp_path / "foo" / "bar.mp4").resolve()


def test_virtualize_inside_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = RunContext()
    p = tmp_path / "inside.mp4"
    assert ctx.virtualize(p) == "inside.mp4"


def test_virtualize_outside_project(tmp_path):
    ctx = RunContext()
    p = tmp_path / "outside.mp4"
    # 路径不在 project_dir 内时,直接返回 absolute 字符串
    assert ctx.virtualize(p) == str(p.resolve())


def test_file_fingerprint_for_existing_file(tmp_path):
    p = tmp_path / "a.mp4"
    p.write_bytes(b"hello")
    fp = RunContext.file_fingerprint(p)
    assert fp is not None
    assert str(p.resolve()) in fp
    assert f"|{p.stat().st_size}|" in fp


def test_file_fingerprint_for_missing_file(tmp_path):
    p = tmp_path / "missing.mp4"
    assert RunContext.file_fingerprint(p) is None


def test_file_fingerprint_for_none():
    assert RunContext.file_fingerprint(None) is None


def test_reject_input_output_collision_raises():
    from pathlib import Path
    p = Path("/tmp/in.mp4")
    with pytest.raises(ValueError, match="refusing to write output over the input file"):
        reject_input_output_collision(p, p)


def test_reject_input_output_collision_passes_for_distinct_paths():
    from pathlib import Path
    reject_input_output_collision(Path("/tmp/in.mp4"), Path("/tmp/out.mp4"))
    reject_input_output_collision(None, Path("/tmp/out.mp4"))
    reject_input_output_collision(Path("/tmp/in.mp4"), None)

"""节点 1 单测(附件 1.2 节要点)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from nodes.node_01_clean_cache import clean_cache_paths


@pytest.fixture
def state_base() -> dict:
    return {"session_id": "test", "error_log": []}


def test_nonexistent_path_does_not_error(tmp_path: Path, state_base: dict) -> None:
    """目标目录不存在 → 正常跳过,不影响整体。"""
    missing = tmp_path / "does_not_exist"
    out = clean_cache_paths([missing], state_base)
    assert out["cache_cleaned"] is True
    assert out["cache_cleaned_paths"] == []  # 不存在的路径不算成功


def test_file_lock_exception_writes_error_log(tmp_path: Path, state_base: dict, monkeypatch) -> None:
    """rmtree 抛 PermissionError → 异常被捕获,写入 error_log,不中断。"""
    target = tmp_path / "locked_dir"
    target.mkdir()
    (target / "file.txt").write_text("data", encoding="utf-8")

    def fail_rmtree(path, *args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("shutil.rmtree", fail_rmtree)
    out = clean_cache_paths([target], state_base)
    assert out["cache_cleaned"] is True
    assert out["cache_cleaned_paths"] == []  # 失败不算成功
    assert any("清理失败" in e for e in out["error_log"])


def test_only_successful_paths_returned(tmp_path: Path, state_base: dict, monkeypatch) -> None:
    """混合成功/失败时,cache_cleaned_paths 只含成功项。"""
    success = tmp_path / "ok_dir"
    success.mkdir()
    fail = tmp_path / "fail_dir"
    fail.mkdir()

    real_rmtree = __import__("shutil").rmtree

    def selective_rmtree(path, *args, **kwargs):
        if str(path).endswith("fail_dir"):
            raise PermissionError("nope")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("shutil.rmtree", selective_rmtree)
    out = clean_cache_paths([success, fail], state_base)
    assert out["cache_cleaned_paths"] == [str(success)]
    assert any("清理失败" in e for e in out["error_log"])

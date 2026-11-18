"""jy_common/cache_manifest.py 单测(2026-11-19 审查阶段 0 / P2-03)。"""

from __future__ import annotations

import json
from pathlib import Path

from jy_common.cache_manifest import build_cache_manifest


def test_manifest_missing_dir_returns_none(tmp_path: Path) -> None:
    assert build_cache_manifest(tmp_path, "1644278", "d3e2") is None


def test_manifest_empty_identity_returns_none(tmp_path: Path) -> None:
    assert build_cache_manifest(tmp_path, "", "M") is None
    assert build_cache_manifest(tmp_path, "E", "") is None


def test_manifest_lists_files_with_sha256(tmp_path: Path) -> None:
    root = tmp_path / "1644278" / "d3e2"
    (root / "sub").mkdir(parents=True)
    (root / "config.json").write_text('{"version": "6.0.0"}', encoding="utf-8")
    (root / "anim.prefab").write_bytes(b"\x00\x01\x02")
    (root / "sub" / "part.lua").write_text("-- lua", encoding="utf-8")

    manifest = build_cache_manifest(tmp_path, "1644278", "d3e2")

    assert manifest is not None
    assert manifest["file_count"] == 3
    assert manifest["config_version"] == "6.0.0"
    paths = [f["path"] for f in manifest["files"]]
    assert paths == ["anim.prefab", "config.json", "sub/part.lua"], "相对路径 + 排序稳定"
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert manifest["total_size"] == sum(f["size"] for f in manifest["files"])
    assert len(manifest["manifest_sha256"]) == 64


def test_manifest_sha256_stable_and_content_sensitive(tmp_path: Path) -> None:
    root = tmp_path / "E" / "M"
    root.mkdir(parents=True)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    first = build_cache_manifest(tmp_path, "E", "M")["manifest_sha256"]

    second = build_cache_manifest(tmp_path, "E", "M")["manifest_sha256"]
    assert first == second, "同内容清单摘要稳定"

    (root / "b.txt").write_text("world", encoding="utf-8")
    changed = build_cache_manifest(tmp_path, "E", "M")
    assert changed["manifest_sha256"] != first, "内容变化 → 摘要变化(stale 判据基础)"
    assert json.dumps(changed["files"], ensure_ascii=False)

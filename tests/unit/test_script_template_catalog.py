"""TemplateCatalog 单测(2026-11-12 noop 治理阶段1,设计文档 §1.1)。

覆盖:正常加载、meta 缺失/损坏降级、路径穿越拒绝、文件缺失/hash 不匹配/
编码错误丢弃、disabled 排除、fingerprint 稳定性。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from storyline_capabilities.script_template_rec import load_template_catalog


def _write_lib(root: Path, templates: list[dict], *, files: dict[str, str] | None = None) -> Path:
    tdir = root / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else {}
    for name, text in files.items():
        (tdir / name).write_text(text, encoding="utf-8")
    (root / "meta.json").write_text(
        json.dumps({"schema_version": "1.0", "templates": templates}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _entry(name: str, *, tags: list[str] | None = None, sha: str | None = None,
           enabled: bool = True, path: str | None = None, **extra) -> dict:
    e = {
        "template_id": f"{name}_v1",
        "path": path or f"templates/{name}.txt",
        "language": "zh",
        "description": f"{name} 描述",
        "tags": tags or ["Life"],
        "content_sha256": sha or "",
        "version": 1,
        "enabled": enabled,
    }
    e.update(extra)
    return e


def test_normal_load_reads_content_and_hash(tmp_path: Path) -> None:
    root = _write_lib(
        tmp_path,
        files={"a.txt": "模板甲的内容"},
        templates=[_entry("a", sha=hashlib.sha256("模板甲的内容".encode()).hexdigest())],
    )
    cat = load_template_catalog(root)
    assert cat.available and len(cat.entries) == 1
    e = cat.entries[0]
    assert e.template_id == "a_v1" and e.content == "模板甲的内容"
    assert e.content_sha256 == hashlib.sha256("模板甲的内容".encode()).hexdigest()
    assert not cat.warnings


def test_missing_meta_is_unavailable(tmp_path: Path) -> None:
    cat = load_template_catalog(tmp_path)
    assert not cat.available and not cat.entries
    assert cat.warnings and "不存在" in cat.warnings[0]


def test_corrupt_meta_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
    cat = load_template_catalog(tmp_path)
    assert not cat.available and cat.warnings


def test_meta_without_templates_array_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")
    cat = load_template_catalog(tmp_path)
    assert not cat.available


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    """`../` 路径穿越必须被拒绝,候选丢弃并留 warning(设计文档 §1.1 任务4)。"""
    secret = tmp_path / "outside.txt"
    secret.write_text("机密", encoding="utf-8")
    root = _write_lib(
        tmp_path,
        templates=[_entry("a", path="../outside.txt", sha=hashlib.sha256("机密".encode()).hexdigest())],
        files={},
    )
    cat = load_template_catalog(root)
    assert not cat.available
    assert any("越出模板根目录" in w for w in cat.warnings)


def test_missing_file_dropped_with_warning(tmp_path: Path) -> None:
    root = _write_lib(tmp_path, templates=[_entry("ghost")], files={})
    cat = load_template_catalog(root)
    assert not cat.available
    assert any("缺失" in w for w in cat.warnings)


def test_hash_mismatch_dropped_not_forged(tmp_path: Path) -> None:
    """hash 不匹配的候选必须丢弃,绝不伪造(设计文档 §1.1)。"""
    root = _write_lib(
        tmp_path,
        files={"a.txt": "真实内容"},
        templates=[_entry("a", sha="0" * 64)],
    )
    cat = load_template_catalog(root)
    assert not cat.available
    assert any("content_sha256 不匹配" in w for w in cat.warnings)


def test_bad_encoding_dropped(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir(parents=True)
    (tmp_path / "templates" / "bad.txt").write_bytes(b"\xff\xfe\xfa")
    root = _write_lib(
        tmp_path,
        templates=[_entry("bad", sha=hashlib.sha256(b"\xff\xfe\xfa").hexdigest())],
        files={},
    )
    cat = load_template_catalog(root)
    assert not cat.available
    assert any("解码失败" in w for w in cat.warnings)


def test_disabled_entry_excluded_silently(tmp_path: Path) -> None:
    root = _write_lib(
        tmp_path,
        files={"a.txt": "内容A"},
        templates=[_entry("a", enabled=False, sha=hashlib.sha256("内容A".encode()).hexdigest())],
    )
    cat = load_template_catalog(root)
    assert not cat.available  # 只有一条且停用 → 库视为空,不算 warning
    assert cat.warnings == []


def test_fingerprint_stable_and_content_sensitive(tmp_path: Path) -> None:
    sha_a = hashlib.sha256("内容A".encode()).hexdigest()
    root = _write_lib(tmp_path, files={"a.txt": "内容A"}, templates=[_entry("a", sha=sha_a)])
    cat1 = load_template_catalog(root)
    cat2 = load_template_catalog(root)
    assert cat1.fingerprint == cat2.fingerprint

    # 内容变化 → hash 失配被丢 → fingerprint 变化(库版本推进)
    (tmp_path / "templates" / "a.txt").write_text("内容B", encoding="utf-8")
    cat3 = load_template_catalog(root)
    assert not cat3.available and cat3.fingerprint != cat1.fingerprint

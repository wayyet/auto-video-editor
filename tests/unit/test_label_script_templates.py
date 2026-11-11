"""scripts/label_script_templates.py 单测(2026-11-12 noop 治理阶段1)。

覆盖:增量复用(内容未变不调 LLM)、dry-run 零写入、apply 写回、
Stub 后端遇到需要标注的新模板时拒绝并指路真实后端。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.label_script_templates import main


def _make_lib(root: Path, files: dict[str, str], *, with_meta: bool = True) -> Path:
    tdir = root / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    templates = []
    for name, text in files.items():
        (tdir / name).write_text(text, encoding="utf-8")
        stem = name[:-4]
        templates.append(
            {
                "template_id": f"{stem}_v1",
                "path": f"templates/{name}",
                "language": "zh",
                "description": f"{stem} 的旧描述",
                "tags": ["Life"],
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "version": 1,
                "enabled": True,
            }
        )
    if with_meta:
        (root / "meta.json").write_text(
            json.dumps({"schema_version": "1.0", "templates": templates}, ensure_ascii=False),
            encoding="utf-8",
        )
    return root


def _run(monkeypatch: pytest.MonkeyPatch, root: Path, *extra: str) -> int:
    argv = ["label_script_templates.py", "--root", str(root), *extra]
    monkeypatch.setattr(sys, "argv", argv)
    return main()


def test_dry_run_reuse_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    text = "模板正文内容甲"
    root = _make_lib(tmp_path, {"demo_zh.txt": text})
    before = (root / "meta.json").read_text(encoding="utf-8")

    rc = _run(monkeypatch, root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[复用]" in out and "dry-run" in out
    assert (root / "meta.json").read_text(encoding="utf-8") == before  # dry-run 零写入


def test_apply_reuse_rewrites_equivalent_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "模板正文内容甲"
    root = _make_lib(tmp_path, {"demo_zh.txt": text})
    before = json.loads((root / "meta.json").read_text(encoding="utf-8"))

    rc = _run(monkeypatch, root, "--apply")
    assert rc == 0
    after = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    assert after["schema_version"] == "1.0"
    assert len(after["templates"]) == 1
    e = after["templates"][0]
    # 增量复用:template_id/description/tags 原样保留,hash 重算一致
    assert e["template_id"] == before["templates"][0]["template_id"]
    assert e["description"] == before["templates"][0]["description"]
    assert e["content_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_new_template_with_stub_backend_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """需要(重新)标注但当前是 Stub → 退出码 1,并指路 --llm-backend(不伪造标注)。"""
    root = _make_lib(tmp_path, {"demo_zh.txt": "已有模板"})
    (root / "templates" / "fresh_zh.txt").write_text("全新模板正文", encoding="utf-8")

    rc = _run(monkeypatch, root, "--apply")
    assert rc == 1
    err = capsys.readouterr().err
    assert "--llm-backend" in err


def test_missing_templates_dir_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rc = _run(monkeypatch, tmp_path)
    assert rc == 1
    assert "不存在" in capsys.readouterr().err


def test_stale_meta_entry_removed_on_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """meta 里有、目录里没有的条目,apply 时移除并提示。"""
    text = "正文"
    root = _make_lib(tmp_path, {"demo_zh.txt": text}, with_meta=False)
    (root / "meta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "templates": [
                    {
                        "template_id": "ghost_v1",
                        "path": "templates/ghost_zh.txt",
                        "language": "zh",
                        "description": "幽灵条目",
                        "tags": ["Life"],
                        "content_sha256": "0" * 64,
                        "version": 1,
                        "enabled": True,
                    },
                    {
                        "template_id": "demo_zh_v1",
                        "path": "templates/demo_zh.txt",
                        "language": "zh",
                        "description": "已有描述",
                        "tags": ["Life"],
                        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "version": 1,
                        "enabled": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc = _run(monkeypatch, root, "--apply")
    assert rc == 0
    after = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    assert [e["template_id"] for e in after["templates"]] == ["demo_zh_v1"]

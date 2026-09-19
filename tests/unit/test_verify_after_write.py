"""draft_ops.verify_after_write 单测(Week 5 新增;对齐计划 §2.1 / §4.1)。"""

from __future__ import annotations

import json
from pathlib import Path


from draft_ops.verify_after_write import verify_draft_loadable


def test_verify_draft_loadable_ok(tmp_path: Path) -> None:
    """双文件均存在 + 内容一致 + 都是合法 JSON → (True, 'ok')。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    payload = {"a": 1, "tracks": []}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    (draft_dir / "draft_content.json").write_text(text, encoding="utf-8")
    (draft_dir / "draft_info.json").write_text(text, encoding="utf-8")

    ok, reason = verify_draft_loadable(draft_dir)
    assert ok is True
    assert reason == "ok"


def test_verify_draft_loadable_mismatch(tmp_path: Path) -> None:
    """双文件内容不一致 → (False, 'draft_content_info_mismatch')。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text('{"a": 1}', encoding="utf-8")
    (draft_dir / "draft_info.json").write_text('{"a": 2}', encoding="utf-8")

    ok, reason = verify_draft_loadable(draft_dir)
    assert ok is False
    assert reason == "draft_content_info_mismatch"


def test_verify_draft_loadable_missing_content(tmp_path: Path) -> None:
    """缺 draft_content.json → (False, 'missing_draft_content.json')。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_info.json").write_text("{}", encoding="utf-8")

    ok, reason = verify_draft_loadable(draft_dir)
    assert ok is False
    assert reason == "missing_draft_content.json"


def test_verify_draft_loadable_missing_info(tmp_path: Path) -> None:
    """缺 draft_info.json → (False, 'missing_draft_info.json')。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")

    ok, reason = verify_draft_loadable(draft_dir)
    assert ok is False
    assert reason == "missing_draft_info.json"


def test_verify_draft_loadable_content_not_json(tmp_path: Path) -> None:
    """draft_content.json 不是合法 JSON → (False, 'draft_content_not_json:...')。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{not json", encoding="utf-8")
    (draft_dir / "draft_info.json").write_text("{}", encoding="utf-8")

    ok, reason = verify_draft_loadable(draft_dir)
    assert ok is False
    assert reason.startswith("draft_content_not_json:")


def test_verify_draft_loadable_info_not_json(tmp_path: Path) -> None:
    """draft_info.json 不是合法 JSON → (False, 'draft_info_not_json:...')。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")
    (draft_dir / "draft_info.json").write_text("{also not json", encoding="utf-8")

    ok, reason = verify_draft_loadable(draft_dir)
    assert ok is False
    assert reason.startswith("draft_info_not_json:")


def test_verify_draft_loadable_with_injected_reader(tmp_path: Path) -> None:
    """注入 reader → 校验逻辑走注入路径(便于 CI 用第三方解析器)。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text('{"a": 1}', encoding="utf-8")
    (draft_dir / "draft_info.json").write_text('{"a": 1}', encoding="utf-8")

    calls = {"n": 0}

    def reader(text: str) -> dict:
        calls["n"] += 1
        return json.loads(text)

    ok, reason = verify_draft_loadable(draft_dir, reader=reader)
    assert ok is True
    assert calls["n"] == 2  # content + info 各一次

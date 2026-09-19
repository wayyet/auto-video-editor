"""draft_ops.duration_index 单测(Week 5 新增;对齐计划 §2.2 / §4.1)。"""

from __future__ import annotations

import json
from pathlib import Path


from draft_ops.duration_index import update_duration_index


def _write_json(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def test_update_duration_index_creates_draft_meta_if_missing(tmp_path: Path) -> None:
    """draft_meta_info.json 不存在 → 创建最小可用版,设置 tm_duration。"""
    draft_dir = tmp_path / "drafts" / "MyDraft"
    draft_dir.mkdir(parents=True)

    result = update_duration_index(draft_dir, 35_000_000)

    meta_path = draft_dir / "draft_meta_info.json"
    assert meta_path.exists()
    assert json.loads(meta_path.read_text(encoding="utf-8")) == {
        "tm_duration": 35_000_000
    }
    assert result["draft_meta"] is True
    # 草稿根目录没有 root_meta_info.json → 不更新
    assert result["root_meta"] is False


def test_update_duration_index_updates_existing_draft_meta(tmp_path: Path) -> None:
    """draft_meta_info.json 已存在 → 保留其它字段,只更新 tm_duration。"""
    drafts_root = tmp_path / "drafts"
    draft_dir = drafts_root / "MyDraft"
    draft_dir.mkdir(parents=True)
    meta_path = draft_dir / "draft_meta_info.json"
    _write_json(
        meta_path,
        {"tm_duration": 1_000_000, "draft_id": "abc", "extra": {"x": 1}},
    )

    update_duration_index(draft_dir, 50_000_000)

    after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert after["tm_duration"] == 50_000_000
    assert after["draft_id"] == "abc"
    assert after["extra"] == {"x": 1}


def test_update_duration_index_updates_root_meta_entry(tmp_path: Path) -> None:
    """root_meta_info.json 存在 + 有匹配条目 → 更新其 tm_duration。"""
    drafts_root = tmp_path / "drafts"
    draft_dir = drafts_root / "MyDraft"
    draft_dir.mkdir(parents=True)
    # draft_meta_info.json
    _write_json(draft_dir / "draft_meta_info.json", {"tm_duration": 0})
    # root_meta_info.json(含一个匹配的 + 一个不匹配的条目)
    root_meta_path = drafts_root / "root_meta_info.json"
    _write_json(
        root_meta_path,
        {
            "all_draft_store": [
                {"draft_name": "OtherDraft", "tm_duration": 1, "draft_id": "OtherDraft"},
                {"draft_name": "MyDraft", "tm_duration": 1, "draft_id": "MyDraft"},
            ]
        },
    )

    log: list[str] = []
    result = update_duration_index(draft_dir, 42_000_000, log=log)

    assert result == {"draft_meta": True, "root_meta": True}
    after = json.loads(root_meta_path.read_text(encoding="utf-8"))
    # MyDraft 更新;OtherDraft 保持原值
    my_entry = next(e for e in after["all_draft_store"] if e["draft_name"] == "MyDraft")
    other_entry = next(e for e in after["all_draft_store"] if e["draft_name"] == "OtherDraft")
    assert my_entry["tm_duration"] == 42_000_000
    assert other_entry["tm_duration"] == 1
    # log 应记录两条
    assert any("draft_meta_info.json" in s for s in log)
    assert any("root_meta_info.json" in s for s in log)


def test_update_duration_index_root_meta_no_match_warns(tmp_path: Path) -> None:
    """root_meta_info.json 存在但 all_draft_store 没匹配 draft_name → 仅告警,不写。"""
    drafts_root = tmp_path / "drafts"
    draft_dir = drafts_root / "NoMatchDraft"
    draft_dir.mkdir(parents=True)
    _write_json(draft_dir / "draft_meta_info.json", {})
    root_meta_path = drafts_root / "root_meta_info.json"
    _write_json(
        root_meta_path,
        {"all_draft_store": [{"draft_name": "SomeOther", "tm_duration": 1}]},
    )

    log: list[str] = []
    result = update_duration_index(draft_dir, 9_000_000, log=log)

    # draft_meta 更新;root_meta 未更新
    assert result["draft_meta"] is True
    assert result["root_meta"] is False
    # root_meta 文件不变
    after = json.loads(root_meta_path.read_text(encoding="utf-8"))
    assert after["all_draft_store"][0]["tm_duration"] == 1
    # log 含未匹配提示
    assert any("未匹配" in s or "未匹配" in s for s in log)


def test_update_duration_index_root_meta_malformed_warns(tmp_path: Path) -> None:
    """root_meta_info.json 存在但 all_draft_store 不是 list → 仅告警。"""
    drafts_root = tmp_path / "drafts"
    draft_dir = drafts_root / "BadRootDraft"
    draft_dir.mkdir(parents=True)
    _write_json(draft_dir / "draft_meta_info.json", {})
    root_meta_path = drafts_root / "root_meta_info.json"
    _write_json(root_meta_path, {"all_draft_store": "not-a-list"})

    log: list[str] = []
    result = update_duration_index(draft_dir, 7_000_000, log=log)

    assert result == {"draft_meta": True, "root_meta": False}
    assert any("不是 list" in s or "not-a-list" in s or "all_draft_store" in s for s in log)


def test_update_duration_index_root_meta_missing_is_ok(tmp_path: Path) -> None:
    """root_meta_info.json 不存在 → 仅更新 draft_meta_info.json,不报错。"""
    drafts_root = tmp_path / "drafts"
    draft_dir = drafts_root / "NoRootDraft"
    draft_dir.mkdir(parents=True)
    _write_json(draft_dir / "draft_meta_info.json", {})

    log: list[str] = []
    result = update_duration_index(draft_dir, 8_000_000, log=log)

    assert result == {"draft_meta": True, "root_meta": False}
    assert any("root_meta_info.json 缺失" in s for s in log)

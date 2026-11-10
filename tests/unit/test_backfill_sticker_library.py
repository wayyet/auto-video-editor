"""scripts/backfill_sticker_library.py 单测 — 最小草稿/模板夹具,不碰真实剪映数据。

覆盖:dry-run 零写入;apply 的映射校验(缺"默认"拒绝/19 位校验/名称不存在/
关键词唯一)、pending 翻转与留痕、原子写后模板仍为合法 JSON。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.backfill_sticker_library import (
    FALLBACK_KEYWORD,
    _parse_map_args,
    _validate_mapping,
    main,
)

RID_A = "1234567890123456789"
RID_B = "2345678901234567890"
RID_C = "3456789012345678901"


def _make_draft(tmp_path: Path, stickers: list[dict]) -> Path:
    """造一份最小样本草稿目录(内含 draft_content.json)。"""
    draft_dir = tmp_path / "sticker_sample"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text(
        json.dumps({"materials": {"stickers": stickers}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return draft_dir


def _make_template(tmp_path: Path) -> Path:
    """造一份 Week 3 占位模板夹具。"""
    tpl = tmp_path / "sticker_template.json"
    tpl.write_text(
        json.dumps(
            {
                "_reverse_engineering_pending": True,
                "_week3_status": "placeholder",
                "sticker_by_keyword": {"默认": "PLACEHOLDER_STICKER_DEFAULT"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tpl


def _stickers_real() -> list[dict]:
    return [
        {"id": "S1", "type": "sticker", "name": "点赞爱心", "resource_id": RID_A},
        {"id": "S2", "type": "sticker", "name": "加油鸭", "resource_id": RID_B},
        {"id": "S3", "type": "sticker", "name": "星星闪闪", "resource_id": RID_C},
    ]


def _run_main(argv: list[str]) -> int:
    monkeypatch_argv = argv
    old = sys.argv
    sys.argv = monkeypatch_argv
    try:
        return main()
    finally:
        sys.argv = old


# ---------- dry-run ----------


class TestDryRun:
    def test_lists_entries_and_writes_nothing(self, tmp_path, capsys):
        draft_dir = _make_draft(tmp_path, _stickers_real())
        template = _make_template(tmp_path)
        before = template.read_text(encoding="utf-8")

        rc = _run_main(
            ["prog", "--draft-dir", str(draft_dir), "--template", str(template)]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "点赞爱心" in out and RID_A in out
        assert "19位合法=True" in out
        # 模板逐字节未动(dry-run 不写任何文件)
        assert template.read_text(encoding="utf-8") == before

    def test_missing_draft_dir_exits_1(self, tmp_path):
        with pytest.raises(SystemExit) as ei:
            _run_main(
                ["prog", "--draft-dir", str(tmp_path / "nope"),
                 "--template", str(_make_template(tmp_path))]
            )
        assert ei.value.code == 1

    def test_empty_stickers_exits_1(self, tmp_path):
        draft_dir = _make_draft(tmp_path, [])
        with pytest.raises(SystemExit) as ei:
            _run_main(
                ["prog", "--draft-dir", str(draft_dir),
                 "--template", str(_make_template(tmp_path))]
            )
        assert ei.value.code == 1


# ---------- 映射解析与校验 ----------


class TestMappingValidation:
    def test_parse_map_args_ok(self):
        m = _parse_map_args(["点赞爱心=赞", "星星闪闪=默认"])
        assert m == {"点赞爱心": ("赞", "点赞爱心=赞"), "星星闪闪": ("默认", "星星闪闪=默认")}

    def test_parse_map_args_no_equals_exits_1(self):
        with pytest.raises(SystemExit):
            _parse_map_args(["没有等号"])

    def test_validate_missing_fallback_keyword_exits_1(self):
        stickers = _stickers_real()
        mapping = _parse_map_args(["点赞爱心=赞"])
        with pytest.raises(SystemExit):
            _validate_mapping(stickers, mapping)

    def test_validate_bad_resource_id_exits_1(self):
        stickers = [
            {"id": "S1", "type": "sticker", "name": "占位贴纸",
             "resource_id": "PLACEHOLDER_STICKER_DEFAULT"},
        ]
        mapping = _parse_map_args([f"占位贴纸={FALLBACK_KEYWORD}"])
        with pytest.raises(SystemExit):
            _validate_mapping(stickers, mapping)

    def test_validate_unknown_name_exits_1(self):
        mapping = _parse_map_args([f"不存在的贴纸={FALLBACK_KEYWORD}"])
        with pytest.raises(SystemExit):
            _validate_mapping(_stickers_real(), mapping)

    def test_validate_duplicate_keyword_exits_1(self):
        stickers = [
            {"id": "S1", "type": "sticker", "name": "甲", "resource_id": RID_A},
            {"id": "S2", "type": "sticker", "name": "乙", "resource_id": RID_B},
        ]
        mapping = _parse_map_args([f"甲={FALLBACK_KEYWORD}", f"乙={FALLBACK_KEYWORD}"])
        with pytest.raises(SystemExit):
            _validate_mapping(stickers, mapping)


# ---------- apply ----------


class TestApply:
    def test_apply_backfills_template(self, tmp_path):
        draft_dir = _make_draft(tmp_path, _stickers_real())
        template = _make_template(tmp_path)

        rc = _run_main(
            ["prog", "--draft-dir", str(draft_dir), "--template", str(template),
             "--map", "点赞爱心=赞", "--map", "加油鸭=加油", "--map", "星星闪闪=默认",
             "--apply"]
        )

        assert rc == 0
        data = json.loads(template.read_text(encoding="utf-8"))
        assert data["_reverse_engineering_pending"] is False
        assert data["_week3_status"] == "real_backfilled"
        assert data["sticker_by_keyword"] == {"赞": RID_A, "加油": RID_B, "默认": RID_C}
        assert data["_backfilled_from"] == "sticker_sample"
        assert data["_backfilled_at"]

    def test_apply_missing_fallback_template_untouched(self, tmp_path):
        draft_dir = _make_draft(tmp_path, _stickers_real())
        template = _make_template(tmp_path)
        before = template.read_text(encoding="utf-8")

        with pytest.raises(SystemExit) as ei:
            _run_main(
                ["prog", "--draft-dir", str(draft_dir), "--template", str(template),
                 "--map", "点赞爱心=赞", "--apply"]
            )
        assert ei.value.code == 1
        assert template.read_text(encoding="utf-8") == before

    def test_apply_bad_id_template_untouched(self, tmp_path):
        """整库 fail-closed:任一映射的 resource_id 非 19 位,一个都不写。"""
        draft_dir = _make_draft(
            tmp_path,
            [
                {"id": "S1", "type": "sticker", "name": "好贴纸", "resource_id": RID_A},
                {"id": "S2", "type": "sticker", "name": "坏贴纸", "resource_id": ""},
            ],
        )
        template = _make_template(tmp_path)
        before = template.read_text(encoding="utf-8")

        with pytest.raises(SystemExit):
            _run_main(
                ["prog", "--draft-dir", str(draft_dir), "--template", str(template),
                 "--map", f"好贴纸=赞", "--map", f"坏贴纸=默认", "--apply"]
            )
        assert template.read_text(encoding="utf-8") == before

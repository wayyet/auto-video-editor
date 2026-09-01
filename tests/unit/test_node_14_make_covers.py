"""node_14_make_covers 单测 — 三比例命名、text_bbox、atomic_write_file 无 .tmp 残留。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from nodes.node_14_make_covers import (
    COVER_SPECS,
    _draw_title,
    _get_title,
    _render_cover_pillow,
    _resize_and_crop,
    _node_14_make_covers_async,
)


def _seed_draft(draft_dir: Path, *, with_texts: bool = True) -> Path:
    """预置最小草稿 + 可选 cover_source_frame.jpg。"""
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {
            "videos": [{"id": "v1"}],
            "texts": [{"content": "测试标题"}] if with_texts else [],
        },
        "tracks": [],
    }
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    # 一张简单的 source frame
    src = Image.new("RGB", (1280, 720), color=(80, 100, 120))
    src.save(draft_dir / "cover_source_frame.jpg", format="JPEG")
    return draft_file


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_resize_and_crop_16x9(tmp_path: Path) -> None:
    """16:9 resize + crop 后输出尺寸正确。"""
    src = Image.new("RGB", (1280, 720))
    out = _resize_and_crop(src, 1920, 1080)
    assert out.size == (1920, 1080)


def test_resize_and_crop_4x3(tmp_path: Path) -> None:
    """4:3 resize + crop 后输出尺寸正确。"""
    src = Image.new("RGB", (800, 600))
    out = _resize_and_crop(src, 1440, 1080)
    assert out.size == (1440, 1080)


def test_resize_and_crop_9x16(tmp_path: Path) -> None:
    """9:16 resize + crop 后输出尺寸正确。"""
    src = Image.new("RGB", (720, 1280))
    out = _resize_and_crop(src, 1080, 1920)
    assert out.size == (1080, 1920)


def test_draw_title_returns_bbox(tmp_path: Path) -> None:
    """_draw_title 应返回完整 text_bbox dict。"""
    img = Image.new("RGB", (1920, 1080))
    bbox = _draw_title(img, "测试标题", "16:9")
    for key in ("x", "y", "w", "h", "font_path", "font_size"):
        assert key in bbox
    assert bbox["w"] > 0 and bbox["h"] > 0


def test_get_title_from_draft(tmp_path: Path) -> None:
    """_get_title 从 materials.texts[0].content 读标题。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    assert _get_title(draft_dir) == "测试标题"


def test_get_title_default_when_empty(tmp_path: Path) -> None:
    """texts 为空时使用默认标题。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir, with_texts=False)
    title = _get_title(draft_dir)
    assert title  # 非空
    assert title != ""


# ---------------------------------------------------------------------------
# 渲染子任务测试
# ---------------------------------------------------------------------------
def test_render_pillow_16x9(tmp_path: Path) -> None:
    """_render_cover_pillow 16:9 应生成 cover_16x9_zh.png。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    asset = _render_cover_pillow(draft_dir, "16:9")
    assert asset["ratio"] == "16:9"
    out = Path(asset["zh_path"])
    assert out.exists()
    assert out.name == "cover_16x9_zh.png"
    # 校验像素尺寸
    img = Image.open(out)
    assert img.size == (1920, 1080)
    # text_bbox 字段完整
    for k in ("x", "y", "w", "h", "font_path", "font_size"):
        assert k in asset["text_bbox"]


def test_render_pillow_4x3(tmp_path: Path) -> None:
    """_render_cover_pillow 4:3 应生成 cover_4x3_zh.png,尺寸 1440x1080。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    asset = _render_cover_pillow(draft_dir, "4:3")
    assert asset["ratio"] == "4:3"
    out = Path(asset["zh_path"])
    assert out.exists()
    assert out.name == "cover_4x3_zh.png"
    img = Image.open(out)
    assert img.size == (1440, 1080)


def test_render_cover_idempotent_no_tmp_leftover(tmp_path: Path) -> None:
    """重复调用 2 次:第二次成功,无 .tmp_ 残留。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    _render_cover_pillow(draft_dir, "16:9")
    _render_cover_pillow(draft_dir, "16:9")  # 第二次

    covers_dir = draft_dir / "covers"
    leftovers = [p for p in covers_dir.iterdir() if p.name.startswith(".tmp_")]
    assert leftovers == []
    # 仍只有 1 张 16:9 图
    pngs = [p for p in covers_dir.iterdir() if p.name.endswith(".png")]
    assert len(pngs) == 1


# ---------------------------------------------------------------------------
# 节点函数测试
# ---------------------------------------------------------------------------
def test_node_14_produces_three_ratios(tmp_path: Path) -> None:
    """node_14 应同时产出 9:16 / 16:9 / 4:3 三张图。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    state = {
        "draft_path": str(draft_dir / "draft_content.json"),
        "status_log": [],
        "error_log": [],
    }

    out = asyncio.run(_node_14_make_covers_async(state))

    covers = out["covers"]
    assert len(covers) == 3
    ratios = sorted([c["ratio"] for c in covers])
    assert ratios == ["16:9", "4:3", "9:16"]
    for c in covers:
        assert Path(c["zh_path"]).exists()
        assert c["text_bbox"]["w"] > 0
    assert "node_14_make_covers_done" in out["status_log"]


def test_node_14_no_draft_path_skips(tmp_path: Path) -> None:
    """draft_path 缺失 → 跳过,error_log 有说明,covers 为空。"""
    state = {"status_log": [], "error_log": []}
    out = asyncio.run(_node_14_make_covers_async(state))
    assert out["covers"] == []
    assert any("draft_path 缺失" in e for e in out["error_log"])
    assert "node_14_make_covers_skipped" in out["status_log"]


def test_node_14_file_naming_compliance(tmp_path: Path) -> None:
    """封面文件命名应严格对齐计划 §8 规范。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    state = {
        "draft_path": str(draft_dir / "draft_content.json"),
        "status_log": [],
        "error_log": [],
    }
    out = asyncio.run(_node_14_make_covers_async(state))

    covers_dir = draft_dir / "covers"
    expected = {"cover_9x16_zh.png", "cover_16x9_zh.png", "cover_4x3_zh.png"}
    actual = {p.name for p in covers_dir.iterdir() if p.suffix == ".png"}
    assert actual == expected

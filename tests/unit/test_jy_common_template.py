"""jy_common.template_library 单测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jy_common.template_library import TemplateLibrary, load_template_library


def test_template_library_from_missing_file(tmp_path: Path) -> None:
    """模板文件不存在 → 标记 _missing=True,调用方可降级。"""
    lib = TemplateLibrary.from_json_file(tmp_path / "nope.json")
    assert lib.is_empty is True


def test_template_library_loads_real_template(tmp_path: Path) -> None:
    """加载真实模板文件:pick_transition / default_text_style 返回正确数据。"""
    template = {
        "transitions": [{"name": "淡入淡出", "resource_id": "tx-001", "duration_us": 500000}],
        "video_effects": [{"name": "轻微放大", "resource_id": "vfx-001", "intensity": 0.3}],
        "default_style": {"outline": True, "shadow": True, "entrance_animation": None},
    }
    f = tmp_path / "tpl.json"
    f.write_text(__import__("json").dumps(template), encoding="utf-8")

    lib = TemplateLibrary.from_json_file(f)
    assert lib.is_empty is False

    tx = lib.pick_transition("default")
    assert tx["resource_id"] == "tx-001"
    assert tx["name"] == "淡入淡出"

    vfx = lib.pick_video_effect("default")
    assert vfx["resource_id"] == "vfx-001"

    style = lib.default_text_style()
    assert style["outline"] is True
    assert style["entrance_animation"] is None


def test_pick_transition_empty_list(tmp_path: Path) -> None:
    """transitions 为空列表 → pick_transition 返回空 dict。"""
    f = tmp_path / "empty.json"
    f.write_text('{"transitions": []}', encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)
    assert lib.pick_transition("anything") == {}


def test_default_text_style_fallback(tmp_path: Path) -> None:
    """default_style 缺失 → 返回空 dict(由调用方兜底)。"""
    f = tmp_path / "no_default.json"
    f.write_text('{}', encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)
    assert lib.default_text_style() == {}


def test_load_template_library_convenience(tmp_path: Path) -> None:
    """便捷构造器等价于 TemplateLibrary.from_json_file。"""
    f = tmp_path / "x.json"
    f.write_text('{"transitions": []}', encoding="utf-8")
    lib = load_template_library(f)
    assert isinstance(lib, TemplateLibrary)
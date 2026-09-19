"""jy_common.template_library 单测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jy_common.template_library import (
    TemplateLibrary,
    load_resource_libraries,
    load_template_library,
)


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def test_template_library_from_missing_file(tmp_path: Path) -> None:
    """模板文件不存在 → 标记 _missing=True,调用方可降级。"""
    lib = TemplateLibrary.from_json_file(tmp_path / "nope.json")
    assert lib.is_empty is True


def test_template_library_loads_real_template(tmp_path: Path) -> None:
    """加载真实模板文件:pick_transition / default_text_style 返回正确数据。"""
    template = {
        "transitions": [{"name": "渐变模糊", "resource_id": "tx-001", "duration_us": 500000}],
        "video_effects": [{"name": "胶片式黑白", "resource_id": "vfx-001", "intensity": 0.3}],
        "default_style": {"outline": True, "shadow": True, "entrance_animation": None},
    }
    f = tmp_path / "tpl.json"
    f.write_text(__import__("json").dumps(template), encoding="utf-8")

    lib = TemplateLibrary.from_json_file(f)
    assert lib.is_empty is False

    tx = lib.pick_transition("default")
    assert tx["resource_id"] == "tx-001"
    assert tx["name"] == "渐变模糊"

    vfx = lib.pick_video_effect("default")
    assert vfx["resource_id"] == "vfx-001"

    style = lib.default_text_style()
    assert style["outline"] is True
    assert style["entrance_animation"] is None


# ---------- Week 5 升级:pick_transition/pick_video_effect 按 style_tag 匹配 ----------


def test_pick_transition_style_tag_exact_match(tmp_path: Path) -> None:
    """transitions 行有 style_tag 字段 → pick_transition 按 style_tag 精确匹配。"""
    template = {
        "transitions": [
            {"name": "渐变模糊", "style_tag": "calm", "resource_id": "tx-c"},
            {"name": "动感快切", "style_tag": "energetic", "resource_id": "tx-e"},
            {"name": "魔法放大", "style_tag": "energetic", "resource_id": "tx-m"},
        ],
        "video_effects": [],
    }
    f = tmp_path / "tpl.json"
    f.write_text(__import__("json").dumps(template), encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)

    # 精确匹配 calm → 唯一命中
    calm = lib.pick_transition("calm")
    assert calm["name"] == "渐变模糊"
    assert calm["resource_id"] == "tx-c"

    # 精确匹配 energetic → 第一条(确定性,顺序)
    energetic = lib.pick_transition("energetic")
    assert energetic["style_tag"] == "energetic"
    assert energetic["name"] in {"动感快切", "魔法放大"}


def test_pick_transition_no_match_falls_back_to_legacy_hash(tmp_path: Path) -> None:
    """无 style_tag 匹配时回退到 hash-modulo 全表(行为不变)。"""
    template = {
        "transitions": [
            {"name": "A", "style_tag": "calm"},
            {"name": "B", "style_tag": "energetic"},
            {"name": "C", "style_tag": "default"},
        ],
    }
    f = tmp_path / "tpl.json"
    f.write_text(__import__("json").dumps(template), encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)

    # "unknown" 不匹配任何 style_tag → hash 兜底
    picked = lib.pick_transition("unknown")
    assert picked in template["transitions"]
    # hash 兜底对同一 style_tag 应稳定(同 hash → 同 idx)
    assert lib.pick_transition("unknown") == picked


def test_pick_transition_rows_without_style_tag_use_legacy(tmp_path: Path) -> None:
    """行无 style_tag 字段 → 全表都走 hash 兜底(语义等价旧行为)。"""
    template = {
        "transitions": [
            {"name": "A"},
            {"name": "B"},
            {"name": "C"},
        ],
    }
    f = tmp_path / "tpl.json"
    f.write_text(__import__("json").dumps(template), encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)

    picked = lib.pick_transition("default")
    assert picked in template["transitions"]
    # 旧 hash 行为:abs(hash("default")) % 3 → 稳定 idx
    expected_idx = abs(hash("default")) % 3
    assert picked["name"] == template["transitions"][expected_idx]["name"]


def test_pick_video_effect_style_tag_exact_match(tmp_path: Path) -> None:
    """video_effects 按 style_tag 精确匹配 + 无匹配时回退 hash。"""
    template = {
        "transitions": [],
        "video_effects": [
            {"name": "胶片式黑白", "style_tag": "vintage", "resource_id": "vfx-v"},
            {"name": "动感抖动", "style_tag": "energetic", "resource_id": "vfx-e"},
        ],
    }
    f = tmp_path / "tpl.json"
    f.write_text(__import__("json").dumps(template), encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)

    vintage = lib.pick_video_effect("vintage")
    assert vintage["name"] == "胶片式黑白"

    # 无匹配 → 兜底到 hash-modulo 全表
    unknown = lib.pick_video_effect("unknown")
    assert unknown in template["video_effects"]


def test_pick_video_effect_empty_list(tmp_path: Path) -> None:
    """video_effects 为空 → 返回空 dict。"""
    f = tmp_path / "empty.json"
    f.write_text('{"video_effects": []}', encoding="utf-8")
    lib = TemplateLibrary.from_json_file(f)
    assert lib.pick_video_effect("anything") == {}


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


# ---------- 新 API 测试(资源库) ----------


def _fixture_lib() -> TemplateLibrary:
    """加载仓库内 fixture,得到一份同时持有 _data + _fx_lib + _text_lib 的 lib。"""
    return TemplateLibrary.from_json_files(
        fx_template=FIXTURE_DIR / "fx_template_stub.json",
        fx_resource=FIXTURE_DIR / "fx_resource_library_fixture.json",
        text_resource=FIXTURE_DIR / "text_resource_library_fixture.json",
    )


def test_from_json_files_loads_real_fixture() -> None:
    """三个 fixture 都存在 → 三块数据都进来。"""
    lib = _fixture_lib()
    assert lib.is_empty is False
    assert lib.has_fx_lib is True
    assert lib.has_text_lib is True


def test_pick_transition_by_name_vip() -> None:
    """按 name 精确查转场 → 返回完整字段(resource_id 19 位数字)。"""
    lib = _fixture_lib()
    t = lib.pick_transition_by_name("渐变模糊")
    assert t is not None
    assert t["resource_id"] == "7123135366504124936"
    assert t["effect_id"] == "FX_TX_001"
    assert t["is_vip"] is True


def test_pick_video_effect_by_name_vip() -> None:
    """按 name 精确查视频特效。"""
    lib = _fixture_lib()
    v = lib.pick_video_effect_by_name("胶片式黑白")
    assert v is not None
    assert v["resource_id"] == "7447351620641231369"


def test_pick_text_animation_intro_vip() -> None:
    """按 kind + name 查文字入场动画。"""
    lib = _fixture_lib()
    a = lib.pick_text_animation("intro", "居中打字机")
    assert a is not None
    assert a["resource_id"] == "7265222187286532667"
    assert a["duration_s"] == 0.5


def test_pick_unknown_returns_none() -> None:
    """未命中的 name 返回 None;非法 kind 也返回 None。"""
    lib = _fixture_lib()
    assert lib.pick_transition_by_name("不存在的转场") is None
    assert lib.pick_video_effect_by_name("不存在的特效") is None
    assert lib.pick_text_animation("intro", "不存在的动画") is None
    assert lib.pick_text_animation("foo", "x") is None  # 非法 kind


def test_list_methods() -> None:
    """list_transitions / list_text_animations 返回 name 列表。"""
    lib = _fixture_lib()
    tx_names = lib.list_transitions()
    assert set(tx_names) == {"渐变模糊", "魔法放大", "黑白抖动"}
    intro_names = lib.list_text_animations("intro")
    assert "居中打字机" in intro_names
    assert "渐次出现" in intro_names


def test_resource_libraries_missing_fallback(tmp_path: Path) -> None:
    """fx_resource / text_resource 不存在时,by_name 查询返回 None,但 is_empty 不变。"""
    fx_template = tmp_path / "tpl.json"
    fx_template.write_text('{"transitions": [{"name": "x"}]}', encoding="utf-8")
    lib = TemplateLibrary.from_json_files(
        fx_template=fx_template,
        fx_resource=tmp_path / "missing_fx.json",
        text_resource=tmp_path / "missing_text.json",
    )
    assert lib.is_empty is False  # 模板还在
    assert lib.has_fx_lib is False  # 但 fx_lib 没数据
    assert lib.has_text_lib is False
    assert lib.pick_transition_by_name("x") is None


def test_load_resource_libraries_convenience() -> None:
    """便捷函数 load_resource_libraries 等价于 TemplateLibrary.from_json_files。"""
    lib = load_resource_libraries(
        fx_template=FIXTURE_DIR / "fx_template_stub.json",
        fx_resource=FIXTURE_DIR / "fx_resource_library_fixture.json",
        text_resource=FIXTURE_DIR / "text_resource_library_fixture.json",
    )
    assert isinstance(lib, TemplateLibrary)
    assert lib.pick_transition_by_name("魔法放大") is not None

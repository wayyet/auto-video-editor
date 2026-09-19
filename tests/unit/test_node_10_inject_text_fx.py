"""节点 10 花字样式追加单测。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nodes.node_10_inject_text_fx import inject_text_fx


_VIP_ID_PATTERN = re.compile(r"^\d{19}$")


@pytest.fixture
def tmp_draft_with_texts(tmp_path: Path) -> Path:
    """构造含 2 条字幕的草稿。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                {
                    "id": "t1",
                    "content": "你好",
                    "target_timerange": {"start": 0, "duration": 1_500_000},
                    "style": {"size": 5.0},
                },
                {
                    "id": "t2",
                    "content": "世界",
                    "target_timerange": {"start": 1_500_000, "duration": 1_500_000},
                    "style": {"size": 5.0},
                },
            ],
        },
        "tracks": [],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_inject_text_fx_appends_outline_shadow(tmp_path: Path, tmp_draft_with_texts: Path) -> None:
    """每条字幕的 style 应被追加 outline/shadow/entrance_animation 字段。

    Week 4 升级:entrance_animation 是含真实 resource_id 的 dict(而非 None)。
    """
    state = _state(tmp_draft_with_texts)
    out = inject_text_fx(state)

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    for text in draft["materials"]["texts"]:
        style = text["style"]
        assert style.get("outline") is True
        assert style.get("shadow") is True
        assert "entrance_animation" in style
        # Week 4:entrance_animation 是 dict 且 resource_id 是 19 位数字
        anim = style["entrance_animation"]
        assert isinstance(anim, dict), f"entrance_animation 应为 dict,实际 {type(anim).__name__}"
        assert "resource_id" in anim, f"entrance_animation 缺少 resource_id:{anim}"
        assert _VIP_ID_PATTERN.match(anim["resource_id"]), (
            f"resource_id 不是 19 位数字:{anim['resource_id']!r}"
        )
        # 原有 size 字段不应被覆盖
        assert style["size"] == 5.0

    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_works_on_empty_texts(tmp_path: Path) -> None:
    """materials.texts 为空时:节点应直接通过,无副作用。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {},
        "tracks": [],
    }
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(draft), encoding="utf-8")

    state = {"draft_path": str(p), "status_log": [], "error_log": []}
    out = inject_text_fx(state)
    assert "node_10_inject_text_fx_done" in out["status_log"]
    # 草稿未被改写
    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert "texts" not in draft_after["materials"]


def test_inject_text_fx_fallback_when_text_resource_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """text_resource_library.json 缺失时:entrance_animation 降级为 None(不抛异常)。"""
    from jy_common import template_library as tl_mod

    def fake_loader(**_kwargs):
        return tl_mod.TemplateLibrary(
            {"default_style": {"outline": True, "shadow": True, "entrance_animation": None}},
            fx_lib={},
            text_lib={},
        )

    monkeypatch.setattr("nodes.node_10_inject_text_fx.load_resource_libraries", fake_loader)

    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [{"id": "t1", "style": {}}]},
        "tracks": [],
    }
    # Week 5:safe_write_draft 写到 draft_dir/draft_content.json(目录级别);
    # 用 ``tmp_path / draft / draft_content.json`` 模拟真实剪映草稿目录布局。
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    p = draft_dir / "draft_content.json"
    p.write_text(json.dumps(draft), encoding="utf-8")
    state = _state(p)
    out = inject_text_fx(p) if False else inject_text_fx({"draft_path": str(p), "status_log": [], "error_log": []})

    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert draft_after["materials"]["texts"][0]["style"]["entrance_animation"] is None
    assert "node_10_inject_text_fx_done" in out["status_log"]

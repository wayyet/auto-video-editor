"""节点 10 字幕入场动画单测(阶段 4:原生 material_animations 通道)。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nodes.node_10_inject_text_fx import inject_text_fx, inject_text_fx_en


_VIP_ID_PATTERN = re.compile(r"^\d{19}$")
_ANIM_REF_PREFIX = "text-anim-"


def _text_material(text_id: str, text: str, start: int, duration: int) -> dict:
    """node_08 产出的我方字幕素材形态(原生 content JSON + 私有键)。"""
    return {
        "id": text_id,
        "type": "text",
        "content": json.dumps({"styles": [{"range": [0, len(text)]}], "text": text}, ensure_ascii=False),
        "target_timerange": {"start": start, "duration": duration},
        "style": {"size": 5.0},
        "track_name": "Subtitles_zh",
    }


@pytest.fixture
def tmp_draft_with_texts(tmp_path: Path) -> Path:
    """2 条我方字幕(text 轨段齐全)+ 1 条人工文本素材。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                _text_material("text-zh-1", "你好", 0, 1_500_000),
                _text_material("text-zh-2", "世界", 1_500_000, 1_500_000),
                {
                    "id": "4F1A2B3C-HUMAN-0001",
                    "type": "text",
                    "content": json.dumps({"text": "人工文本"}),
                    "target_timerange": {"start": 3_000_000, "duration": 1_000_000},
                },
            ],
        },
        "tracks": [{
            "type": "text",
            "id": "track-text-1",
            "segments": [
                {"id": "text-seg-1", "material_id": "text-zh-1",
                 "target_timerange": {"start": 0, "duration": 1_500_000}},
                {"id": "text-seg-2", "material_id": "text-zh-2",
                 "target_timerange": {"start": 1_500_000, "duration": 1_500_000}},
            ],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_inject_text_fx_writes_native_animations(tmp_draft_with_texts: Path) -> None:
    """我方字幕 → material_animations 条目 + text 轨段 refs 绑定;人工文本不动。"""
    out = inject_text_fx(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    anims = draft["materials"]["material_animations"]
    assert len(anims) == 2
    for anim in anims:
        assert anim["id"].startswith(_ANIM_REF_PREFIX)
        assert anim["type"] == "sticker_animation"
        (entry,) = anim["animations"]
        assert entry["type"] == "in"
        assert _VIP_ID_PATTERN.match(entry["resource_id"]), entry["resource_id"]
        assert int(entry["duration"]) > 0

    segments = draft["tracks"][0]["segments"]
    assert any(
        a["id"] in segments[0]["extra_material_refs"] for a in anims
    ), "text-zh-1 的轨段应绑定动画"
    assert any(
        a["id"] in segments[1]["extra_material_refs"] for a in anims
    ), "text-zh-2 的轨段应绑定动画"

    # 人工文本:无动画、无私有 style 改动
    human = next(t for t in draft["materials"]["texts"] if t["id"].startswith("4F1A"))
    assert not any(
        a["animations"][0]["name"] == f"{_ANIM_REF_PREFIX}{human['id']}" for a in anims
    )
    assert "entrance_animation" not in (human.get("style") or {})

    # 我方字幕的私有 style 也不再写 entrance_animation(原生通道替代)
    ours = [t for t in draft["materials"]["texts"] if t["id"].startswith("text-zh-")]
    assert all("entrance_animation" not in (t.get("style") or {}) for t in ours)

    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_default_animation_is_new_year_typewriter(
    tmp_draft_with_texts: Path,
) -> None:
    """默认动画名是库内实证存在的"新年打字机"(旧默认四个名字全不在库内)。"""
    inject_text_fx(_state(tmp_draft_with_texts))
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    assert names == {"新年打字机"}


def test_inject_text_fx_dual_write_content_and_info(tmp_draft_with_texts: Path) -> None:
    """A6:写回后 ``draft_content.json`` 与 ``draft_info.json`` 都在,且内容一致。"""
    out = inject_text_fx(_state(tmp_draft_with_texts))

    content_file = tmp_draft_with_texts
    info_file = tmp_draft_with_texts.parent / "draft_info.json"
    assert content_file.exists(), "draft_content.json 不应被写没"
    assert info_file.exists(), (
        "draft_info.json 缺失 —— safe_write_draft 很可能收到了文件路径而不是目录"
    )
    assert info_file.read_text(encoding="utf-8") == content_file.read_text(encoding="utf-8")
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_no_nested_draft_directory(tmp_draft_with_texts: Path) -> None:
    """A6 反向断言:不该出现"以 draft_content.json 命名的子目录"这种退化写入。"""
    inject_text_fx(_state(tmp_draft_with_texts))

    nested = tmp_draft_with_texts / "draft_content.json"
    assert not nested.is_dir()


def test_inject_text_fx_works_on_empty_texts(tmp_path: Path) -> None:
    """materials.texts 为空时:节点通过,material_animations 保持空。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {},
        "tracks": [],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft), encoding="utf-8")

    out = inject_text_fx(_state(p))
    assert "node_10_inject_text_fx_done" in out["status_log"]
    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("material_animations")


def test_inject_text_fx_fallback_when_text_resource_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """text_resource_library.json 缺失时:动画降级不写入(degraded 计数),不抛异常。"""
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
        "materials": {"texts": [_text_material("text-zh-1", "你好", 0, 1_000_000)]},
        "tracks": [],
    }
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    p = draft_dir / "draft_content.json"
    p.write_text(json.dumps(draft), encoding="utf-8")
    out = inject_text_fx({"draft_path": str(p), "status_log": [], "error_log": []})

    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("material_animations")
    assert any("动画降级不写入" in t for t in out["status_log"])
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_replay_is_byte_identical(tmp_draft_with_texts: Path) -> None:
    """重放两次:整份草稿字节级一致(确定性 id + 清理不引入漂移)。"""
    inject_text_fx(_state(tmp_draft_with_texts))
    first = tmp_draft_with_texts.read_bytes()
    inject_text_fx(_state(tmp_draft_with_texts))
    second = tmp_draft_with_texts.read_bytes()
    assert first == second


def test_inject_text_fx_cleans_stale_animations(tmp_draft_with_texts: Path) -> None:
    """字幕被删后重放:旧动画素材与 refs 被清理,不留悬空。"""
    inject_text_fx(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["texts"] = draft["materials"]["texts"][:1]  # 只留 text-zh-1
    draft["tracks"][0]["segments"] = draft["tracks"][0]["segments"][:1]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_text_fx(_state(tmp_draft_with_texts))
    draft2 = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    anims = draft2["materials"]["material_animations"]
    assert len(anims) == 1
    assert anims[0]["id"] == f"{_ANIM_REF_PREFIX}text-zh-1"
    seg_refs = draft2["tracks"][0]["segments"][0]["extra_material_refs"]
    assert anims[0]["id"] in seg_refs
    assert f"{_ANIM_REF_PREFIX}text-zh-2" not in seg_refs


def test_inject_text_fx_en_returns_delta_with_count_tag(tmp_path: Path) -> None:
    """英文分支:只返回 delta,status_log 带写入数标签(同超步无键冲突)。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [_text_material("text-en-1", "hello", 0, 1_000_000)]},
        "tracks": [{
            "type": "text",
            "id": "track-text-1",
            "segments": [{"id": "text-seg-1", "material_id": "text-en-1",
                          "target_timerange": {"start": 0, "duration": 1_000_000}}],
        }],
    }
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    (en_dir / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")

    out = inject_text_fx_en({"draft_dir_en_branch": str(en_dir)})
    en_draft = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert len(en_draft["materials"]["material_animations"]) == 1
    assert "node_10b_text_animations_written:1" in out["status_log"]
    assert "node_10b_inject_text_fx_en_done" in out["status_log"]

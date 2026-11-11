"""节点 9 转场 + 视频特效单测 — 语义消费、原生绑定、幂等清理、降级。

2026-11-09 阶段 3 重写后的口径:
- 转场/特效是原生 material + segment ``extra_material_refs`` 绑定(不再是孤立数组记录);
- 边界 = max(视频段数 - 1, 0),不依赖 shot_plan;
- 语义(fade/cut)经 ``fx_template.json`` 的 ``transition_semantic_map`` 解析;
- 1 段 → not_applicable;资源未解析 → degraded 不写入;
- 重放幂等(字节级)+ 我方旧产物清理,人工转场不受影响。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from draft_ops.draft_integrity import check_track_references
from nodes.node_09_inject_fx import (
    _boundary_semantics_plan,
    inject_fx,
)


_VIP_ID_PATTERN = re.compile(r"^\d{19}$")


def _seg(seg_id: str, start: int, duration: int = 10_000_000) -> dict:
    return {"id": seg_id, "target_timerange": {"start": start, "duration": duration}}


def _draft(segments: list[dict]) -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 30_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": segments}],
    }


def _write_draft(path: Path, draft: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """3 段视频的草稿(s1/s2/s3 各 10s)。"""
    return _write_draft(
        tmp_path / "draft" / "draft_content.json",
        _draft([_seg("s1", 0), _seg("s2", 10_000_000), _seg("s3", 20_000_000)]),
    )


def _state(**overrides) -> dict:
    base = {
        "draft_path": "",
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def test_inject_fx_binds_transitions_to_boundaries(tmp_draft: Path) -> None:
    """3 段 → 2 个转场,原生形态并挂到后一段的 extra_material_refs;s1 无转场 ref。"""
    state = _state(draft_path=str(tmp_draft))
    out = inject_fx(state)

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    transitions = draft["materials"]["transitions"]
    segments = draft["tracks"][0]["segments"]

    assert len(transitions) == 2
    for t in transitions:
        assert t["type"] == "transition"
        assert _VIP_ID_PATTERN.match(t["resource_id"])
        assert int(t["duration"]) > 0
    # 绑定:边界 i 的转场挂在 segments[i+1]
    assert "transition-1" in segments[1]["extra_material_refs"]
    assert "transition-2" in segments[2]["extra_material_refs"]
    assert not any(
        str(r).startswith("transition-") for r in segments[0].get("extra_material_refs", [])
    )
    # 引用完整性:无悬空
    assert check_track_references(draft, "zh") == []
    # 计数(fx_summary 已随阶段5批2 移除,以草稿原生条目为准):2 边界全写入
    assert len(transitions) == 2
    # 2026-11-11:fx_template video_effects 已清空("抖动模糊"全片挂载致成片模糊)
    # → 降级不写入,每段一条 degraded 记录,error_log 留痕
    assert not draft["materials"].get("video_effects")
    assert any("视频特效资源未解析" in e for e in out["error_log"])
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_ignores_shot_plan(tmp_draft: Path) -> None:
    """阶段3任务4:不再依赖 shot_plan —— 不传它,注入行为不变。"""
    state = _state(draft_path=str(tmp_draft))
    assert "shot_plan" not in state
    out = inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft["materials"]["transitions"]) == 2
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_consumes_transition_semantics_fade(tmp_draft: Path) -> None:
    """storyline_plan clips 的 transition_out='fade_in' → 语义映射到免费转场"叠化"。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "fade_in"},
                {"clip_id": "c2", "transition_out": "fade_in"},
                {"clip_id": "c3"},
            ],
        },
    )
    inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    transitions = draft["materials"]["transitions"]
    assert len(transitions) == 2
    assert all(t["name"] == "叠化" for t in transitions)
    # 免费资源 19 位 ID(vendor transition_meta 实值)
    assert transitions[0]["resource_id"] == "6724845717472416269"


def test_inject_fx_semantic_cut_writes_nothing(tmp_draft: Path) -> None:
    """语义 = cut → 该边界硬切,不写转场素材,也不留悬空 ref。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "cut"},
                {"clip_id": "c2", "transition_out": "cut"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"]["transitions"] == []
    assert all(
        not any(str(r).startswith("transition-") for r in s.get("extra_material_refs", []))
        for s in draft["tracks"][0]["segments"]
    )
    # 2 边界均为语义硬切(fx_summary 已移除,以"零转场素材"验证)
    assert len(draft["materials"]["transitions"]) == 0


def test_boundary_semantics_plan_prefers_out_then_in() -> None:
    """边界语义取前一 clip 的 transition_out,缺省回落下一 clip 的 transition_in;无语义占位 None(按边界对齐)。"""
    state = {
        "storyline_plan": {
            "clips": [
                {"clip_id": "c1", "transition_out": "fade_in", "transition_in": "cut"},
                {"clip_id": "c2", "transition_in": "flash"},
                {"clip_id": "c3"},
            ],
        },
    }
    # 边界0(c1→c2):c1.out=fade_in 优先,c2.in=flash 不重复创建;
    # 边界1(c2→c3):c2.out 缺、c3.in 缺 → None 占位。
    assert _boundary_semantics_plan(state) == ["fade_in", None]


def test_boundary_semantics_plan_falls_back_to_transition_plan() -> None:
    """storyline_plan 无 clips 时回落 storyline_transition_plan items(组级:两组之间 1 个边界)。"""
    state = {
        "storyline_transition_plan": {
            "items": [
                {"group_id": "g1", "transition_out": "fade_out"},
                {"group_id": "g2", "transition_out": "fade_out"},
            ],
        },
    }
    assert _boundary_semantics_plan(state) == ["fade_out"]


def test_inject_fx_not_applicable_single_segment(tmp_path: Path) -> None:
    """1 段 → 0 边界:not_applicable,不写转场,不伪装成功。"""
    draft_path = _write_draft(tmp_path / "draft" / "draft_content.json", _draft([_seg("s1", 0)]))
    state = _state(draft_path=str(draft_path))
    out = inject_fx(state)
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["materials"]["transitions"] == []
    assert "node_09_inject_fx_zh_not_applicable" in out["status_log"]


def test_inject_fx_handles_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模板库缺失时,降级通过 — status_log 标记 + error_log 警告,不动草稿。"""
    from jy_common import template_library as tl_mod

    empty_lib = tl_mod.TemplateLibrary({"_missing": True, "_path": "/nope.json"})

    def fake_loader(**_kwargs):
        return empty_lib

    monkeypatch.setattr("nodes.node_09_inject_fx.load_resource_libraries", fake_loader)

    draft_path = _write_draft(tmp_path / "draft" / "draft_content.json", _draft([]))
    state = _state(draft_path=str(draft_path))
    out = inject_fx(state)

    assert "node_09_inject_fx_no_template" in out["status_log"]
    assert any("模板库缺失" in e for e in out["error_log"])
    draft_after = json.loads(draft_path.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("transitions")


def test_inject_fx_handles_missing_fx_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fx_resource_library 缺失(只有风格索引):转场/特效 degraded 不写入,不抛异常。"""
    from jy_common import template_library as tl_mod

    def fake_loader(**_kwargs):
        # 仅返回 data,fx_lib / text_lib 都空 → by_name 查不到 → degraded
        return tl_mod.TemplateLibrary(
            {
                "transitions": [{"name": "PLACEHOLDER_NAME", "style_tag": "default"}],
                "video_effects": [{"name": "PLACEHOLDER_VFX", "style_tag": "default"}],
            },
            fx_lib={},
            text_lib={},
        )

    monkeypatch.setattr("nodes.node_09_inject_fx.load_resource_libraries", fake_loader)

    draft_path = _write_draft(
        tmp_path / "draft" / "draft_content.json",
        _draft([_seg("s1", 0), _seg("s2", 10_000_000)]),
    )
    state = _state(draft_path=str(draft_path))
    out = inject_fx(state)
    assert "node_09_inject_fx_done" in out["status_log"]
    draft_after = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft_after["materials"]["transitions"] == []
    assert any("degraded" in e for e in out["error_log"])


def test_inject_fx_replay_is_byte_identical(tmp_draft: Path) -> None:
    """重放两次:materials 条数不变,整份草稿字节级一致(幂等三件套)。"""
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)
    first = tmp_draft.read_bytes()

    inject_fx(state)
    second = tmp_draft.read_bytes()

    assert first == second
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft["materials"]["transitions"]) == 2
    assert len(draft["materials"]["video_effects"]) == 0


def test_inject_fx_cleans_stale_after_boundary_shrink(tmp_draft: Path) -> None:
    """3 段注入后裁成 2 段再重放:旧转场素材/引用被清掉,不留悬空。"""
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    draft["tracks"][0]["segments"] = draft["tracks"][0]["segments"][:2]
    tmp_draft.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_fx(state)
    draft2 = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft2["materials"]["transitions"]) == 1
    assert len(draft2["materials"]["video_effects"]) == 0
    assert check_track_references(draft2, "zh") == []
    assert "transition-2" not in str(draft2["tracks"][0]["segments"][1].get("extra_material_refs"))


def test_inject_fx_preserves_human_transitions(tmp_draft: Path) -> None:
    """人工在剪映加的转场(uuid id)不落我方模式 → 素材与引用都保留。"""
    human_transition = {
        "id": "4F1A2B3C-0000-1111-2222-333344445555",
        "type": "transition",
        "name": "人工转场",
        "resource_id": "8888888888888888888",
    }
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    draft["materials"]["transitions"] = [human_transition]
    draft["tracks"][0]["segments"][1]["extra_material_refs"] = [human_transition["id"]]
    tmp_draft.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_fx(_state(draft_path=str(tmp_draft)))

    draft2 = json.loads(tmp_draft.read_text(encoding="utf-8"))
    ids = [t["id"] for t in draft2["materials"]["transitions"]]
    assert human_transition["id"] in ids
    assert human_transition["id"] in draft2["tracks"][0]["segments"][1]["extra_material_refs"]


def test_inject_fx_applies_to_en_draft_when_present(tmp_draft: Path, tmp_path: Path) -> None:
    """K2=B:英文分支草稿存在时,转场/特效同样注入,且两份条数与绑定一致。"""
    zh = tmp_draft
    en_file = tmp_path / "en_branch" / "draft_content.json"
    en_file.parent.mkdir(parents=True, exist_ok=True)
    en_file.write_text(zh.read_text(encoding="utf-8"), encoding="utf-8")

    state = _state(
        draft_path=str(zh),
        draft_dir_en_branch=str(en_file.parent),
    )
    out = inject_fx(state)

    zh_draft = json.loads(zh.read_text(encoding="utf-8"))
    en_draft = json.loads(en_file.read_text(encoding="utf-8"))

    assert len(zh_draft["materials"]["transitions"]) > 0
    assert len(en_draft["materials"]["transitions"]) == len(zh_draft["materials"]["transitions"])
    assert len(en_draft["materials"]["video_effects"]) == 0
    assert "node_09_inject_fx_done" in out["status_log"]
    assert "node_09_inject_fx_en_done" in out["status_log"]


def test_inject_fx_degrades_when_en_draft_missing(tmp_draft: Path, tmp_path: Path) -> None:
    """K2=B 降级:英文草稿目录指向不存在的路径 → 只处理中文,不抛异常。"""
    zh = tmp_draft
    state = _state(draft_path=str(zh), draft_dir_en_branch=str(tmp_path / "nope"))
    out = inject_fx(state)

    assert "node_09_inject_fx_done" in out["status_log"]
    assert "node_09_inject_fx_en_done" not in out["status_log"]
    assert any("英文草稿不存在" in e for e in out["error_log"])
    assert len(json.loads(zh.read_text(encoding="utf-8"))["materials"]["transitions"]) > 0

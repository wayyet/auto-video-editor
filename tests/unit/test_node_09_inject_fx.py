"""节点 9 转场 + 视频特效单测 — 池轮换、克制点缀、幂等清理、降级。

2026-11-17 池化重写后的口径(核验报告 §6.4-§6.7 / §8.3):
- 语义 → transition_semantic_map 池 key → transition_pools 池内 sha256 确定性
  轮换(禁 python hash()、不含 zh/en tag,跨 PYTHONHASHSEED 稳定,双语一致);
- 未收录语义回退 pool_gentle;all-cut + required → 边界 0 保底 fade;
- 视频特效克制点缀:0-based 位点 {0, n-1} ∪ {i∈[1,n-2] 且 i%3==0},按挂载
  序号轮换白名单,intensity 钳制 [0,1],空白名单 fail-closed 不写;
- 边界 = max(视频段数 - 1, 0);1 段 → not_applicable;资源未解析 → degraded;
- 重放幂等(字节级)+ 我方旧产物清理,人工转场不受影响。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from draft_ops.draft_integrity import check_track_references
from jy_common.template_library import TemplateLibrary
from nodes.node_09_inject_fx import (
    _boundary_semantics_plan,
    _effect_segment_positions,
    _stable_pool_seed,
    inject_fx,
)

REPO = Path(__file__).resolve().parents[2]

_VIP_ID_PATTERN = re.compile(r"^\d{19}$")

# 真实 fx_template.json 的池(与 templates/fx_template.json 同步维护)
POOL_GENTLE = ("叠化", "水墨", "相片切换", "前后对比", "融化")
POOL_ZOOM = ("推近 II", "推远 II", "中心切开")
EFFECT_WHITELIST = ("柔和辉光", "金色辉光", "梦幻辉光", "精致辉光", "失焦光斑", "幻动光斑")


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


def _stub_lib(
    pools: dict[str, list[str]] | None = None,
    effects: list[dict] | None = None,
    sem_map: dict | None = None,
) -> TemplateLibrary:
    """可控小库:池成员/特效候选的 19 位 ID 全部内联(fx_lib 分段精确命中)。"""
    pools = pools if pools is not None else {"pool_gentle": ["甲转场", "乙转场", "丙转场"]}
    effects = effects if effects is not None else []
    sem_map = sem_map if sem_map is not None else {"fade": "pool_gentle", "cut": None}
    data = {
        "transition_semantic_map": sem_map,
        "transition_pools": pools,
        "video_effects": effects,
    }
    fx_lib = {
        "transitions": [
            {
                "name": name,
                "is_vip": False,
                "resource_id": f"70000000000000000{idx:02d}",
                "effect_id": f"E{idx}",
                "md5": "m",
                "default_duration_s": 0.5,
                "is_overlap": False,
            }
            for idx, name in enumerate(
                [n for members in pools.values() for n in members]
            )
        ],
        "video_effects": [
            {
                "name": row["name"],
                "is_vip": False,
                "resource_id": f"71000000000000000{idx:02d}",
                "effect_id": f"V{idx}",
                "md5": "m",
            }
            for idx, row in enumerate(effects)
        ],
    }
    return TemplateLibrary(data, fx_lib=fx_lib, text_lib={})


def _use_stub_lib(monkeypatch: pytest.MonkeyPatch, lib: TemplateLibrary) -> None:
    monkeypatch.setattr(
        "nodes.node_09_inject_fx.load_resource_libraries", lambda **_kw: lib
    )


# ---------------------------------------------------------------- 转场池轮换


def test_inject_fx_binds_transitions_to_boundaries(tmp_draft: Path) -> None:
    """3 段无计划 → 2 个转场(pool_gentle 轮换),挂后一段;特效挂首/尾段。"""
    state = _state(draft_path=str(tmp_draft))
    out = inject_fx(state)

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    transitions = draft["materials"]["transitions"]
    segments = draft["tracks"][0]["segments"]
    video_effects = draft["materials"]["video_effects"]

    assert len(transitions) == 2
    for t in transitions:
        assert t["type"] == "transition"
        assert _VIP_ID_PATTERN.match(t["resource_id"])
        assert t["name"] in POOL_GENTLE
        assert int(t["duration"]) > 0
    # 绑定:边界 i 的转场挂在 segments[i+1]
    assert "transition-1" in segments[1]["extra_material_refs"]
    assert "transition-2" in segments[2]["extra_material_refs"]
    assert not any(
        str(r).startswith("transition-") for r in segments[0].get("extra_material_refs", [])
    )
    assert check_track_references(draft, "zh") == []
    # 克制点缀:3 段 → 位点 {0, 2},按挂载序号轮换白名单前两名
    assert [e["name"] for e in video_effects] == ["柔和辉光", "金色辉光"]
    assert "vfx-1" in segments[0]["extra_material_refs"]
    assert "vfx-3" in segments[2]["extra_material_refs"]
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_ignores_shot_plan(tmp_draft: Path) -> None:
    """阶段3任务4:不再依赖 shot_plan —— 不传它,注入行为不变。"""
    state = _state(draft_path=str(tmp_draft))
    out = inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft["materials"]["transitions"]) == 2
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_consumes_transition_semantics_fade(tmp_draft: Path) -> None:
    """transition_out='fade_in' → pool_gentle 池内轮换(全部真实 19 位资源)。"""
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
    assert all(t["name"] in POOL_GENTLE for t in transitions)
    assert all(_VIP_ID_PATTERN.match(t["resource_id"]) for t in transitions)


def test_inject_fx_semantic_zoom_maps_pool_zoom(tmp_draft: Path) -> None:
    """zoom 语义 → pool_zoom 池(推近 II / 推远 II / 中心切开)。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "zoom"},
                {"clip_id": "c2", "transition_out": "zoom"},
                {"clip_id": "c3"},
            ],
        },
    )
    inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert all(t["name"] in POOL_ZOOM for t in draft["materials"]["transitions"])


def test_inject_fx_unknown_semantic_falls_back_to_gentle(tmp_draft: Path) -> None:
    """未收录语义(flash)→ 统一回退 pool_gentle,不再落强视觉默认值(§6.5)。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "flash"},
                {"clip_id": "c2", "transition_out": "flash"},
                {"clip_id": "c3"},
            ],
        },
    )
    inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert all(t["name"] in POOL_GENTLE for t in draft["materials"]["transitions"])


def test_inject_fx_pool_rotation_locks_formula(tmp_draft: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """池轮换公式锁定:index = (boundary_index + sha256(job|boundary|pool)) % len(pool)。"""
    pool = ["甲转场", "乙转场", "丙转场"]
    _use_stub_lib(monkeypatch, _stub_lib(pools={"pool_gentle": pool}))
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    names = [t["name"] for t in draft["materials"]["transitions"]]
    seed0 = _stable_pool_seed("draft", 0, "pool_gentle")
    seed1 = _stable_pool_seed("draft", 1, "pool_gentle")
    assert names[0] == pool[(0 + seed0) % len(pool)]
    assert names[1] == pool[(1 + seed1) % len(pool)]


def test_inject_fx_deterministic_across_pythonhashseed(tmp_draft: Path) -> None:
    """同输入在不同 PYTHONHASHSEED 子进程下输出一致(核验报告 §8.3)。"""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from nodes.node_09_inject_fx import inject_fx\n"
        f"state = {{'draft_path': {str(tmp_draft)!r}, 'status_log': [], 'error_log': []}}\n"
        "inject_fx(state)\n"
        f"d = json.load(open({str(tmp_draft)!r}, encoding='utf-8'))\n"
        "names = [t['name'] for t in d['materials']['transitions']]"
        " + [e['name'] for e in d['materials']['video_effects']]\n"
        "print(json.dumps(names, ensure_ascii=False))\n"
    )
    outputs = []
    for seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout.strip())
    assert outputs[0] == outputs[1]
    assert outputs[0]  # 非空:确有产出


# ---------------------------------------------------------------- 硬切与 all-cut 保底


def test_inject_fx_all_cut_with_required_falls_back(tmp_draft: Path) -> None:
    """全边界硬切 + 默认 required(transitions=True)→ 边界 0 保底 fade(§6.7)。"""
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
    transitions = draft["materials"]["transitions"]
    # 边界 0 保底产出一个 pool_gentle 转场;边界 1 仍是硬切
    assert len(transitions) == 1
    assert transitions[0]["name"] in POOL_GENTLE
    assert "transition-1" in draft["tracks"][0]["segments"][1]["extra_material_refs"]
    assert not any(
        str(r).startswith("transition-")
        for r in draft["tracks"][0]["segments"][2].get("extra_material_refs", [])
    )
    assert "node_09_inject_fx_zh_all_cut_fallback" in out["status_log"]


def test_inject_fx_all_cut_without_required_keeps_hard_cuts(tmp_draft: Path) -> None:
    """用户显式不要求转场 → 允许全硬切,零转场素材,无保底(§6.7)。"""
    state = _state(
        draft_path=str(tmp_draft),
        required_features={"transitions": False},
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "hard_cut"},
                {"clip_id": "c2", "transition_out": "none"},
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
    assert "node_09_inject_fx_zh_all_cut_fallback" not in out["status_log"]


def test_inject_fx_mixed_cut_writes_only_transition_boundaries(tmp_draft: Path) -> None:
    """部分硬切部分转场:硬切边界不写素材,转场边界正常;不触发保底(§6.7)。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "cut"},
                {"clip_id": "c2", "transition_out": "fade_in"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    transitions = draft["materials"]["transitions"]
    assert len(transitions) == 1
    assert transitions[0]["name"] in POOL_GENTLE
    assert "transition-2" in draft["tracks"][0]["segments"][2]["extra_material_refs"]
    assert not any(
        str(r).startswith("transition-")
        for r in draft["tracks"][0]["segments"][1].get("extra_material_refs", [])
    )
    assert "node_09_inject_fx_zh_all_cut_fallback" not in out["status_log"]


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


# ---------------------------------------------------------------- 视频特效克制点缀


def test_effect_segment_positions_formula() -> None:
    """位点公式(0-based):{0, n-1} ∪ {i∈[1,n-2] 且 i%3==0}(核验报告 §4.10)。"""
    assert _effect_segment_positions(0) == set()
    assert _effect_segment_positions(1) == {0}
    assert _effect_segment_positions(2) == {0, 1}
    assert _effect_segment_positions(3) == {0, 2}
    assert _effect_segment_positions(4) == {0, 3}
    assert _effect_segment_positions(7) == {0, 3, 6}


def test_inject_fx_not_applicable_single_segment(tmp_path: Path) -> None:
    """1 段 → 0 边界:not_applicable;特效仍挂首段(唯一段即首尾)。"""
    draft_path = _write_draft(tmp_path / "draft" / "draft_content.json", _draft([_seg("s1", 0)]))
    state = _state(draft_path=str(draft_path))
    out = inject_fx(state)
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["materials"]["transitions"] == []
    assert [e["name"] for e in draft["materials"]["video_effects"]] == ["柔和辉光"]
    assert "node_09_inject_fx_zh_not_applicable" in out["status_log"]


def test_inject_fx_effect_rotation_by_mount_sequence(
    tmp_draft: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7 段 → 位点 {0,3,6},特效按挂载序号轮换白名单(非段索引取模)。"""
    effects = [
        {"name": f"特效{c}", "style_tag": "default", "intensity": 0.5}
        for c in ("甲", "乙", "丙", "丁")
    ]
    _use_stub_lib(monkeypatch, _stub_lib(effects=effects))
    draft_path = _write_draft(
        tmp_path / "draft7" / "draft_content.json",
        _draft([_seg(f"s{i}", i * 10_000_000) for i in range(7)]),
    )
    inject_fx(_state(draft_path=str(draft_path)))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    effects_written = {m["id"]: m["name"] for m in draft["materials"]["video_effects"]}
    assert effects_written == {"vfx-1": "特效甲", "vfx-4": "特效乙", "vfx-7": "特效丙"}


def test_inject_fx_effect_intensity_clamped(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intensity 钳制 [0,1](核验报告 §6.6)。"""
    effects = [
        {"name": "特效高", "style_tag": "default", "intensity": 1.5},
        {"name": "特效负", "style_tag": "default", "intensity": -0.2},
    ]
    _use_stub_lib(monkeypatch, _stub_lib(effects=effects))
    inject_fx(_state(draft_path=str(tmp_draft)))
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    values = {m["id"]: m["value"] for m in draft["materials"]["video_effects"]}
    assert values["vfx-1"] == 1.0
    assert values["vfx-3"] == 0.0  # 3 段位点 {0,2} → 第 2 次挂载在 vfx-3(特效负)


def test_inject_fx_empty_whitelist_fail_closed(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空白名单 → fail-closed 不注入特效,只记 status 说明,不刷 error_log(§6.6)。"""
    _use_stub_lib(monkeypatch, _stub_lib(effects=[]))
    out = inject_fx(_state(draft_path=str(tmp_draft)))
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"]["video_effects"] == []
    assert not any("视频特效" in e for e in out["error_log"])
    assert "node_09_inject_fx_zh_vfx_whitelist_empty" in out["status_log"]
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_effect_names_not_resolved_degrades(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单非空但资源库解析失败 → degraded 不写入,error_log 留痕。"""
    lib = TemplateLibrary(
        {"video_effects": [{"name": "幽灵特效", "style_tag": "default", "intensity": 0.5}]},
        fx_lib={"transitions": [], "video_effects": []},
        text_lib={},
    )
    _use_stub_lib(monkeypatch, lib)
    out = inject_fx(_state(draft_path=str(tmp_draft)))
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"]["video_effects"] == []
    assert any("degraded" in e for e in out["error_log"])


# ---------------------------------------------------------------- 幂等与守卫


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
    """fx_resource_library 缺失(只有白名单):转场/特效 degraded 不写入,不抛异常。"""
    from jy_common import template_library as tl_mod

    def fake_loader(**_kwargs):
        # 仅返回 data,fx_lib / text_lib 都空 → by_name 查不到 → degraded
        return tl_mod.TemplateLibrary(
            {
                "transition_pools": {"pool_gentle": ["PLACEHOLDER_NAME"]},
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
    assert len(draft["materials"]["video_effects"]) == 2


def test_inject_fx_cleans_stale_after_boundary_shrink(tmp_draft: Path) -> None:
    """3 段注入后裁成 2 段再重放:旧转场/特效素材与引用被清掉,不留悬空。"""
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    draft["tracks"][0]["segments"] = draft["tracks"][0]["segments"][:2]
    tmp_draft.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_fx(state)
    draft2 = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft2["materials"]["transitions"]) == 1
    # 2 段 → 位点 {0,1}:vfx-1/vfx-2 续用,旧 vfx-3 被清理
    assert {m["id"] for m in draft2["materials"]["video_effects"]} == {"vfx-1", "vfx-2"}
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


# ---------------------------------------------------------------- 双语草稿


def test_inject_fx_applies_to_en_draft_when_present(tmp_draft: Path, tmp_path: Path) -> None:
    """K2=B:英文分支草稿同步注入,且逐边界转场名/特效名与中文一致(§4.9 seed 不含 tag)。"""
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

    zh_names = [t["name"] for t in zh_draft["materials"]["transitions"]]
    en_names = [t["name"] for t in en_draft["materials"]["transitions"]]
    assert zh_names and zh_names == en_names
    zh_vfx = [e["name"] for e in zh_draft["materials"]["video_effects"]]
    en_vfx = [e["name"] for e in en_draft["materials"]["video_effects"]]
    assert zh_vfx == en_vfx
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

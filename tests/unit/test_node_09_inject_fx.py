"""节点 9 转场 + 视频特效单测 — 池轮换、克制点缀、构建计划发射(SDK 链转正版)。

2026-11-22 SDK 链转正:node_09 不再改写草稿,观察点从草稿 materials 移到
DraftBuildSpec(transitions/video_effects 段);转场挂载语义为**前段**
(SDK 约定,boundary_index + from/to clip 表达)。池轮换/克制点缀/降级语义
保持 2026-11-17 池化重写口径(核验报告 §6.4-§6.7 / §8.3):
- 语义 → transition_semantic_map 池 key → transition_pools 池内 sha256 确定性
  轮换(禁 python hash()、不含 zh/en tag,跨 PYTHONHASHSEED 稳定,双语一致);
- 未收录语义回退 pool_gentle;all-cut + required → 边界 0 保底 fade;
- 视频特效克制点缀:0-based 位点 {0, n-1} ∪ {i∈[1,n-2] 且 i%3==0},按挂载
  序号轮换白名单,intensity 钳制 [0,1](spec 侧 0-100),空白名单 fail-closed;
- 边界 = max(视频段数 - 1, 0);1 段 → not_applicable;资源未解析 → degraded;
- 模板库缺失 → 降级通过 + 响亮报错(交付 QA 闸门兜底)。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from jy_common.template_library import TemplateLibrary
from nodes.node_09_inject_fx import (
    _boundary_semantics_plan,
    _effect_segment_positions,
    _stable_pool_seed,
    inject_fx,
)

REPO = Path(__file__).resolve().parents[2]

_VIP_ID_PATTERN = re.compile(r"^\d{19}$")

# 真实 fx_template.json 的池/白名单(2026-11-18 全库分池后成员变大,改为运行时
# 读取单一事实源,免维护数百名元组;成员归属断言语义不变)
with open(REPO / "templates" / "fx_template.json", "r", encoding="utf-8") as _f:
    _FX_TEMPLATE = json.load(_f)
POOL_GENTLE: tuple[str, ...] = tuple(_FX_TEMPLATE["transition_pools"]["pool_gentle"])
POOL_ZOOM: tuple[str, ...] = tuple(_FX_TEMPLATE["transition_pools"]["pool_zoom"])
POOL_WHIP: tuple[str, ...] = tuple(_FX_TEMPLATE["transition_pools"]["pool_whip"])
EFFECT_WHITELIST: tuple[str, ...] = tuple(row["name"] for row in _FX_TEMPLATE["video_effects"])


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


def _spec_of(draft_file: Path):
    """读取 node_09 发射的构建计划(spec sidecar;缺失即失败)。"""
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(draft_file.parent)
    assert spec is not None, "spec sidecar 应已发射"
    return spec


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


def test_inject_fx_plans_transitions_for_boundaries(tmp_draft: Path) -> None:
    """3 段无计划 → 2 个转场(pool_gentle 轮换,边界 0/1);特效挂首/尾段;草稿零写入。"""
    state = _state(draft_path=str(tmp_draft))
    out = inject_fx(state)

    spec = _spec_of(tmp_draft)
    assert len(spec.transitions) == 2
    for i, t in enumerate(spec.transitions):
        assert t.boundary_index == i
        assert t.from_clip_id == f"s{i + 1}" and t.to_clip_id == f"s{i + 2}"
        assert _VIP_ID_PATTERN.match(t.resource_id)
        assert t.name in POOL_GENTLE
        assert t.duration_us > 0
    # 克制点缀:3 段 → 位点 {0, 2},按挂载序号轮换白名单前两名
    assert [v.name for v in spec.video_effects] == ["柔和辉光", "金色辉光"]
    assert {v.clip_id for v in spec.video_effects} == {"s1", "s3"}
    # 草稿零写入(SDK 链:node_11 编译才落草稿)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"].get("transitions", []) == []
    assert draft["materials"].get("video_effects", []) == []
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_ignores_shot_plan(tmp_draft: Path) -> None:
    """阶段3任务4:不再依赖 shot_plan —— 不传它,计划行为不变。"""
    state = _state(draft_path=str(tmp_draft))
    out = inject_fx(state)
    assert len(_spec_of(tmp_draft).transitions) == 2
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
    spec = _spec_of(tmp_draft)
    assert len(spec.transitions) == 2
    assert all(t.name in POOL_GENTLE for t in spec.transitions)
    assert all(_VIP_ID_PATTERN.match(t.resource_id) for t in spec.transitions)


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
    assert all(t.name in POOL_ZOOM for t in _spec_of(tmp_draft).transitions)


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
    assert all(t.name in POOL_GENTLE for t in _spec_of(tmp_draft).transitions)


def test_inject_fx_pool_rotation_locks_formula(tmp_draft: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """池轮换公式锁定:index = (boundary_index + sha256(job|boundary|pool)) % len(pool)。"""
    pool = ["甲转场", "乙转场", "丙转场"]
    _use_stub_lib(monkeypatch, _stub_lib(pools={"pool_gentle": pool}))
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)
    names = [t.name for t in _spec_of(tmp_draft).transitions]
    seed0 = _stable_pool_seed("draft", 0, "pool_gentle")
    seed1 = _stable_pool_seed("draft", 1, "pool_gentle")
    assert names[0] == pool[(0 + seed0) % len(pool)]
    assert names[1] == pool[(1 + seed1) % len(pool)]


def test_inject_fx_deterministic_across_pythonhashseed(tmp_draft: Path) -> None:
    """同输入在不同 PYTHONHASHSEED 子进程下 spec 计划一致(核验报告 §8.3)。"""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from nodes.node_09_inject_fx import inject_fx\n"
        f"state = {{'draft_path': {str(tmp_draft)!r}, 'status_log': [], 'error_log': []}}\n"
        "inject_fx(state)\n"
        "spec = json.load(open(r"
        + repr(str(tmp_draft.parent / '.autovideo_draft_build_spec.json'))
        + ", encoding='utf-8'))\n"
        "names = [t['name'] for t in spec['transitions']]"
        " + [v['name'] for v in spec['video_effects']]\n"
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


# ------------------------------------------------ job_id 链路修复(2026-11-18,审查阶段 2)


def test_inject_fx_dual_draft_same_seed_via_config(tmp_path: Path) -> None:
    """2.10 一次 inject_fx 处理 zh+en 双草稿:config.thread_id 作 seed,两稿逐边界同名。

    zh 目录名故意与 thread_id 不同 —— 若 resolver 误用目录名兜底,公式断言即失败。
    """
    zh_draft = _write_draft(
        tmp_path / "AutoVideo_folder-not-tid" / "draft_content.json",
        _draft([_seg("s1", 0), _seg("s2", 10_000_000), _seg("s3", 20_000_000)]),
    )
    en_draft = _write_draft(
        tmp_path / "en_branch" / "draft_content.json",
        _draft([_seg("e1", 0), _seg("e2", 10_000_000), _seg("e3", 20_000_000)]),
    )
    tid = "console-dual-n09"
    state = _state(draft_path=str(zh_draft), draft_dir_en_branch=str(en_draft.parent))
    out = inject_fx(state, {"configurable": {"thread_id": tid}})

    zh_names = [t.name for t in _spec_of(zh_draft).transitions]
    en_names = [t.name for t in _spec_of(en_draft).transitions]
    assert len(zh_names) == len(en_names) == 2
    assert zh_names == en_names
    # seed 来自 resolver 解析出的 thread_id(公式镜像,boundary 0-based)
    for i, name in enumerate(zh_names):
        seed = _stable_pool_seed(tid, i, "pool_gentle")
        assert name == POOL_GENTLE[(i + seed) % len(POOL_GENTLE)]
    assert "node_09_inject_fx_done" in out["status_log"]
    assert "node_09_inject_fx_en_done" in out["status_log"]


def test_inject_fx_session_id_fallback_matches_formula(tmp_path: Path) -> None:
    """旧 checkpoint 兼容:无 config 只有 state.session_id → seed = session_id(非目录名)。"""
    zh_draft = _write_draft(
        tmp_path / "AutoVideo_folder-not-sid" / "draft_content.json",
        _draft([_seg("s1", 0), _seg("s2", 10_000_000), _seg("s3", 20_000_000)]),
    )
    sid = "console-via-session"
    state = _state(draft_path=str(zh_draft), session_id=sid)
    inject_fx(state)  # 不传 config
    for i, t in enumerate(_spec_of(zh_draft).transitions):
        seed = _stable_pool_seed(sid, i, "pool_gentle")
        assert t.name == POOL_GENTLE[(i + seed) % len(POOL_GENTLE)]


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
    transitions = _spec_of(tmp_draft).transitions
    # 边界 0 保底产出一个 pool_gentle 转场;边界 1 仍是硬切
    assert len(transitions) == 1
    assert transitions[0].boundary_index == 0
    assert transitions[0].name in POOL_GENTLE
    assert "node_09_inject_fx_zh_all_cut_fallback" in out["status_log"]


def test_inject_fx_all_cut_without_required_keeps_hard_cuts(tmp_draft: Path) -> None:
    """用户显式不要求转场 → 允许全硬切,零转场计划,无保底(§6.7)。"""
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
    assert _spec_of(tmp_draft).transitions == []
    assert "node_09_inject_fx_zh_all_cut_fallback" not in out["status_log"]


def test_inject_fx_mixed_cut_writes_only_transition_boundaries(tmp_draft: Path) -> None:
    """部分硬切部分转场:硬切边界不进计划,转场边界正常;不触发保底(§6.7)。"""
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
    transitions = _spec_of(tmp_draft).transitions
    assert len(transitions) == 1
    assert transitions[0].boundary_index == 1
    assert transitions[0].from_clip_id == "s2" and transitions[0].to_clip_id == "s3"
    assert transitions[0].name in POOL_GENTLE
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
    spec = _spec_of(draft_path)
    assert spec.transitions == []
    assert [v.name for v in spec.video_effects] == ["柔和辉光"]
    assert "node_09_inject_fx_zh_not_applicable" in out["status_log"]


def test_inject_fx_effect_rotation_by_mount_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    effects_planned = {v.clip_id: v.name for v in _spec_of(draft_path).video_effects}
    assert effects_planned == {"s0": "特效甲", "s3": "特效乙", "s6": "特效丙"}


def test_inject_fx_effect_intensity_clamped(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intensity 钳制 [0,1] → spec 换算 0-100(核验报告 §6.6)。"""
    effects = [
        {"name": "特效高", "style_tag": "default", "intensity": 1.5},
        {"name": "特效负", "style_tag": "default", "intensity": -0.2},
    ]
    _use_stub_lib(monkeypatch, _stub_lib(effects=effects))
    inject_fx(_state(draft_path=str(tmp_draft)))
    values = {v.clip_id: v.intensity for v in _spec_of(tmp_draft).video_effects}
    assert values["s1"] == 100.0
    assert values["s3"] == 0.0  # 3 段位点 {0,2} → 第 2 次挂载在 s3(特效负)


def test_inject_fx_empty_whitelist_fail_closed(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空白名单 → fail-closed 零特效计划,只记 status 说明,不刷 error_log(§6.6)。"""
    _use_stub_lib(monkeypatch, _stub_lib(effects=[]))
    out = inject_fx(_state(draft_path=str(tmp_draft)))
    assert _spec_of(tmp_draft).video_effects == []
    assert not any("视频特效" in e for e in out["error_log"])
    assert "node_09_inject_fx_zh_vfx_whitelist_empty" in out["status_log"]
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_effect_names_not_resolved_degrades(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单非空但资源库解析失败 → degraded 零计划,error_log 留痕。"""
    lib = TemplateLibrary(
        {"video_effects": [{"name": "幽灵特效", "style_tag": "default", "intensity": 0.5}]},
        fx_lib={"transitions": [], "video_effects": []},
        text_lib={},
    )
    _use_stub_lib(monkeypatch, lib)
    out = inject_fx(_state(draft_path=str(tmp_draft)))
    assert _spec_of(tmp_draft).video_effects == []
    assert any("degraded" in e for e in out["error_log"])


# ---------------------------------------------------------------- 幂等与守卫


def test_inject_fx_handles_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模板库缺失 → 降级通过:error_log 响亮留痕 + 零计划(交付 QA 闸门兜底)。"""
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
    # spec 不发射(node_09 早退),草稿不动
    assert not (draft_path.parent / ".autovideo_draft_build_spec.json").exists()


def test_inject_fx_handles_missing_fx_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fx_resource_library 缺失(只有白名单):转场/特效 degraded 零计划,不抛异常。"""
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
    spec = _spec_of(draft_path)
    assert spec.transitions == []
    assert spec.video_effects == []
    assert any("degraded" in e for e in out["error_log"])


def test_inject_fx_replay_is_byte_identical(tmp_draft: Path) -> None:
    """重放两次:草稿字节级一致(零写入)+ spec 计划字节级一致(确定性轮换)。"""
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)
    first_draft = tmp_draft.read_bytes()
    spec_file = tmp_draft.parent / ".autovideo_draft_build_spec.json"
    first_spec = spec_file.read_bytes()

    inject_fx(state)
    second = tmp_draft.read_bytes()

    assert tmp_draft.read_bytes() == first_draft, "草稿零写入"
    assert spec_file.read_bytes() == first_spec, "spec 计划重放稳定"
    assert len(_spec_of(tmp_draft).transitions) == 2
    assert len(_spec_of(tmp_draft).video_effects) == 2


def test_inject_fx_cleans_stale_after_boundary_shrink(tmp_draft: Path) -> None:
    """3 段发射后裁成 2 段再重放:spec 计划随边界收缩(段整体替换=幂等清理)。"""
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)
    assert len(_spec_of(tmp_draft).transitions) == 2

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    draft["tracks"][0]["segments"] = draft["tracks"][0]["segments"][:2]
    tmp_draft.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_fx(state)
    spec = _spec_of(tmp_draft)
    assert len(spec.transitions) == 1
    # 2 段 → 位点 {0,1}:首尾两段续用,旧 s3 计划消失
    assert {v.clip_id for v in spec.video_effects} == {"s1", "s2"}
    assert all(t.boundary_index == 0 for t in spec.transitions)


def test_inject_fx_preserves_human_transitions(tmp_draft: Path) -> None:
    """人工在剪映加的转场(uuid id):node_09 零写入,草稿原样保留;计划只含我方条目。"""
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
    assert ids == [human_transition["id"]], "人工素材不被触碰"
    assert human_transition["id"] in draft2["tracks"][0]["segments"][1]["extra_material_refs"]
    spec = _spec_of(tmp_draft)
    assert len(spec.transitions) == 2, "spec 只记录计划边界(人工 uuid 不进计划)"


# ---------------------------------------------------------------- 双语草稿


def test_inject_fx_applies_to_en_draft_when_present(tmp_draft: Path, tmp_path: Path) -> None:
    """K2=B:英文分支草稿同步发射,且逐边界转场名/特效名与中文一致(§4.9 seed 不含 tag)。"""
    zh = tmp_draft
    en_file = tmp_path / "en_branch" / "draft_content.json"
    en_file.parent.mkdir(parents=True, exist_ok=True)
    en_file.write_text(zh.read_text(encoding="utf-8"), encoding="utf-8")

    state = _state(
        draft_path=str(zh),
        draft_dir_en_branch=str(en_file.parent),
    )
    out = inject_fx(state)

    zh_names = [t.name for t in _spec_of(zh).transitions]
    en_names = [t.name for t in _spec_of(en_file).transitions]
    assert zh_names and zh_names == en_names
    zh_vfx = [v.name for v in _spec_of(zh).video_effects]
    en_vfx = [v.name for v in _spec_of(en_file).video_effects]
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
    assert len(_spec_of(zh).transitions) > 0


# ---------------------------------------------------------------- 开场转场加强(第八期阶段3,设计文档 §6.3)


def _boost_request() -> dict:
    return {"user_request": "【开场】开头使用最炸的转场，配大字标题"}


def test_inject_fx_opening_boost_real_lib_boundary0_whip(tmp_draft: Path) -> None:
    """真实库:【开场】正向 → 边界 0 语义 whip → pool_whip;后续边界不受影响。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_targets=_boost_request(),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "fade_in"},
                {"clip_id": "c2", "transition_out": "fade_in"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)
    transitions = _spec_of(tmp_draft).transitions

    assert len(transitions) == 2
    assert transitions[0].name in POOL_WHIP  # 边界 0 = 强动势池
    assert transitions[1].name in POOL_GENTLE  # 边界 1 = 原 fade 语义
    assert "node_09_inject_fx_opening_boost_requested" in out["status_log"]
    assert "node_09_inject_fx_zh_opening_boost_applied" in out["status_log"]
    assert "node_09_inject_fx_zh_all_cut_fallback" not in out["status_log"]


def test_inject_fx_opening_boost_empty_semantics_still_applies(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-03 核心:语义计划为空但草稿有多段 → 仍覆盖边界 0,不静默漏执行。"""
    lib = _stub_lib(
        pools={
            "pool_gentle": ["柔切一", "柔切二"],
            "pool_whip": ["甩镜一", "甩镜二"],
        },
        sem_map={"whip": "pool_whip", "fade": "pool_gentle", "cut": None},
    )
    _use_stub_lib(monkeypatch, lib)
    state = _state(
        draft_path=str(tmp_draft),
        storyline_targets={"user_request": "【开场】开场用甩镜转场"},
    )
    out = inject_fx(state)

    transitions = _spec_of(tmp_draft).transitions
    assert len(transitions) == 2
    assert transitions[0].name in ("甩镜一", "甩镜二")  # 边界 0 = whip
    assert transitions[1].name in ("柔切一", "柔切二")  # 边界 1 缺省语义 → gentle 回退
    assert "node_09_inject_fx_zh_opening_boost_applied" in out["status_log"]


def test_inject_fx_opening_boost_not_applicable_single_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单镜头无边界 → not_applicable,不得记 applied(P1-03/§6.3)。"""
    single = _write_draft(
        tmp_path / "draft" / "draft_content.json", _draft([_seg("s1", 0)])
    )
    lib = _stub_lib(
        pools={"pool_gentle": ["柔切一"], "pool_whip": ["甩镜一"]},
        sem_map={"whip": "pool_whip", "fade": "pool_gentle", "cut": None},
    )
    _use_stub_lib(monkeypatch, lib)
    state = _state(draft_path=str(single), storyline_targets=_boost_request())
    out = inject_fx(state)

    assert _spec_of(single).transitions == []
    assert "node_09_inject_fx_opening_boost_requested" in out["status_log"]
    assert "node_09_inject_fx_zh_opening_boost_not_applicable" in out["status_log"]
    assert "node_09_inject_fx_zh_opening_boost_applied" not in out["status_log"]


def test_inject_fx_opening_boost_negation_does_not_trigger(tmp_draft: Path) -> None:
    """否定表述 → 不触发;转场全按原语义(§8.1)。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_targets={"user_request": "【开场】开头不要使用强转场"},
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "fade_in"},
                {"clip_id": "c2", "transition_out": "fade_in"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)
    transitions = _spec_of(tmp_draft).transitions
    assert len(transitions) == 2
    assert all(t.name in POOL_GENTLE for t in transitions)
    assert "node_09_inject_fx_opening_boost_requested" not in out["status_log"]
    assert "node_09_inject_fx_zh_opening_boost_applied" not in out["status_log"]


def test_inject_fx_no_markers_keeps_legacy_behavior(tmp_draft: Path) -> None:
    """P1-06:无任何【】标记(含「开头/最炸/转场」自由文本)→ 行为与旧版一致。"""
    raw = (
        "#1、附件上传的这些视频是关于：马来西亚海上清真寺的旅游素材。"
        "#2、要求是制作一个「开头 3 秒用最炸的转场 + 大字标题」的视频。"
    )
    state = _state(
        draft_path=str(tmp_draft),
        storyline_targets={"user_request": raw},
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "fade_in"},
                {"clip_id": "c2", "transition_out": "fade_in"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)
    transitions = _spec_of(tmp_draft).transitions
    assert len(transitions) == 2
    assert all(t.name in POOL_GENTLE for t in transitions)
    assert "node_09_inject_fx_opening_boost_requested" not in out["status_log"]


def test_inject_fx_opening_boost_overrides_all_cut_plan(tmp_draft: Path) -> None:
    """全硬切计划 + 开场加强:边界 0 变 whip,其余边界仍硬切;不触发 all-cut 保底。"""
    state = _state(
        draft_path=str(tmp_draft),
        storyline_targets=_boost_request(),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "cut"},
                {"clip_id": "c2", "transition_out": "cut"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)
    transitions = _spec_of(tmp_draft).transitions

    assert len(transitions) == 1
    assert transitions[0].boundary_index == 0
    assert transitions[0].name in POOL_WHIP
    assert "node_09_inject_fx_zh_all_cut_fallback" not in out["status_log"]
    assert "node_09_inject_fx_zh_opening_boost_applied" in out["status_log"]


def test_inject_fx_opening_boost_degraded_when_whip_pool_missing(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pool_whip 缺失 → 边界 0 degraded 不进计划;不得记 applied(§6.3 不假成功)。"""
    lib = _stub_lib(
        pools={"pool_gentle": ["柔切一", "柔切二"]},
        sem_map={"whip": "pool_whip", "fade": "pool_gentle", "cut": None},
    )
    _use_stub_lib(monkeypatch, lib)
    state = _state(
        draft_path=str(tmp_draft),
        storyline_targets=_boost_request(),
        storyline_plan={
            "clips": [
                {"clip_id": "c1", "transition_out": "fade_in"},
                {"clip_id": "c2", "transition_out": "fade_in"},
                {"clip_id": "c3"},
            ],
        },
    )
    out = inject_fx(state)

    transitions = _spec_of(tmp_draft).transitions
    # 边界 0 degraded 不进;边界 1 fade 语义正常进
    assert len(transitions) == 1
    assert transitions[0].boundary_index == 1
    assert transitions[0].name in ("柔切一", "柔切二")
    assert "node_09_inject_fx_zh_opening_boost_applied" not in out["status_log"]
    assert "node_09_inject_fx_zh_opening_boost_degraded" in out["status_log"]
    assert any("opening boost" in w for w in out["error_log"])


def test_inject_fx_opening_boost_zh_en_same_boundary0_name(
    tmp_draft: Path, tmp_path: Path
) -> None:
    """zh/en 共享同一 opening_boost 与 job_id → 双稿边界 0 转场名一致(§4.9)。"""
    zh = tmp_draft
    en_file = tmp_path / "en_branch" / "draft_content.json"
    en_file.parent.mkdir(parents=True, exist_ok=True)
    en_file.write_text(zh.read_text(encoding="utf-8"), encoding="utf-8")

    state = _state(
        draft_path=str(zh),
        draft_dir_en_branch=str(en_file.parent),
        storyline_targets=_boost_request(),
    )
    out = inject_fx(state)

    zh_name = _spec_of(zh).transitions[0].name
    en_name = _spec_of(en_file).transitions[0].name
    assert zh_name == en_name
    assert zh_name in POOL_WHIP
    assert "node_09_inject_fx_zh_opening_boost_applied" in out["status_log"]
    assert "node_09_inject_fx_en_opening_boost_applied" in out["status_log"]


def test_inject_fx_opening_boost_replay_byte_identical(
    tmp_draft: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重放幂等:同一任务二次发射,spec 字节级一致(确定性轮换不被 boost 破坏)。"""
    lib = _stub_lib(
        pools={"pool_gentle": ["柔切一", "柔切二"], "pool_whip": ["甩镜一", "甩镜二"]},
        sem_map={"whip": "pool_whip", "fade": "pool_gentle", "cut": None},
    )
    _use_stub_lib(monkeypatch, lib)
    state = _state(draft_path=str(tmp_draft), storyline_targets=_boost_request())

    inject_fx(state)
    first = (tmp_draft.parent / ".autovideo_draft_build_spec.json").read_bytes()
    inject_fx({**state, "status_log": [], "error_log": []})
    assert (tmp_draft.parent / ".autovideo_draft_build_spec.json").read_bytes() == first

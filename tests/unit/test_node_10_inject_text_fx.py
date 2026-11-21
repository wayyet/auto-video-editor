"""节点 10 单测(2026-11-22 SDK 链转正裁剪版)。

保留意图:delta 返回契约(2026-11-20 回归锁)、动画选取确定性(auto/fixed/
池回退/审计标签)、zh/en 一致、seed 源(config.thread_id → session_id)、
spec 装饰发射、草稿零写入、无字幕/资源缺失降级。
删除:legacy 原生 material_animations 写入/refs 绑定/旧产物清理断言(随六段
legacy 链退役;"非破坏降级保留既有装饰"由
test_jianying_sdk_phase4::test_apply_text_fx_degradation_keeps_spec 锁定)。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from nodes.node_10_inject_text_fx import inject_text_fx, inject_text_fx_en

_VIP_ID_PATTERN = re.compile(r"^\d{19}$")


def _tmp_spec_dir(tmp_path: Path) -> Path:
    """草稿目录:只含最小草稿文件(node_10 不改写它,仅作存在性锚点)。"""
    (tmp_path / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return tmp_path


def _seed_spec_subtitles(tmp_path: Path, ids: tuple[str, ...] = ("text-zh-1", "text-zh-2"), language: str = "zh") -> None:
    from jianying_sdk.spec import SubtitleSpec
    from nodes._draft_build_spec import replace_spec_sections

    replace_spec_sections(tmp_path, subtitles=[
        SubtitleSpec(
            subtitle_id=tid, text=f"字幕{i + 1}",
            start_us=i * 1_500_000, end_us=(i + 1) * 1_500_000,
            language=language,  # type: ignore[arg-type]
        )
        for i, tid in enumerate(ids)
    ])


def _state(draft_dir: Path) -> dict:
    return {
        "draft_path": str(draft_dir / "draft_content.json"),
        "status_log": [],
        "error_log": [],
    }


def _emitted_animations(draft_dir: Path) -> list:
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(draft_dir)
    out = []
    for deco in (spec.text_decorations if spec else []):
        out.extend(deco.animations)
    return out


def _usable_intro_rows(cache_root: str | Path) -> list[dict]:
    """真实池 JSON 里「verified 且缓存目录存在」的 intro 行(文件序即池序)。"""
    lib_path = Path(__file__).resolve().parents[2] / "templates" / "text_resource_library.json"
    with open(lib_path, "r", encoding="utf-8") as f:
        intro = list(json.load(f).get("intro", []))
    root = Path(cache_root)
    return [
        row
        for row in intro
        if row.get("verified")
        and (root / str(row["effect_id"]) / str(row["md5"])).is_dir()
    ]


def _expected_pool_member(job_id: str) -> dict:
    """镜像 node_10 池选公式(锁定契约,2026-11-19 守卫版)。"""
    from nodes.node_10_inject_text_fx import JIANYING_CACHE_ROOT

    lib_path = Path(__file__).resolve().parents[2] / "templates" / "text_resource_library.json"
    with open(lib_path, "r", encoding="utf-8") as f:
        intro = list(json.load(f).get("intro", []))
    usable = _usable_intro_rows(JIANYING_CACHE_ROOT)
    assert usable, "conftest 封闭化 fixture 应伪造至少一条 verified 条目缓存"
    seed = int(hashlib.sha256(f"{job_id}|text_intro_pool".encode("utf-8")).hexdigest()[:8], 16)
    start = seed % len(intro)
    for offset in range(len(intro)):
        candidate = intro[(start + offset) % len(intro)]
        if candidate in usable:
            return candidate
    raise AssertionError("unreachable: usable 子集非空必有命中")


# ---------------------------------------------------------------------------
# 返回契约与发射
# ---------------------------------------------------------------------------
def test_inject_text_fx_returns_delta_not_full_state(tmp_path: Path) -> None:
    """返回增量 patch,不得 ``{**state}`` 全量回传(2026-11-20 回归锁)。

    本节点与 fork_draft_for_english_branch 同一超步并发(fork 唯一写
    ``draft_dir_en_branch``);线程复用旧 checkpoint 时该键在通道里残留旧值,
    全量回传会把它原样重发 → 同超步双写 → LangGraph ``InvalidUpdateError``。
    """
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    state = dict(_state(d))
    state["draft_dir_en_branch"] = "C:/stale/en_branch"  # 旧 checkpoint 残留值
    state["storyline_outputs_root"] = "C:/stale/outputs"
    out = inject_text_fx(state)
    assert "draft_dir_en_branch" not in out, "不得全量回传无关 state 键"
    assert "storyline_outputs_root" not in out
    assert "node_10_inject_text_fx_done" in (out.get("status_log") or [])


def test_inject_text_fx_emits_animation_spec(tmp_path: Path) -> None:
    """我方字幕 → spec 装饰(kind=in、19 位 resource_id);草稿不被改写。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    draft_file = d / "draft_content.json"
    draft_bytes = draft_file.read_text(encoding="utf-8")

    out = inject_text_fx(_state(d))
    entries = _emitted_animations(d)
    assert len(entries) == 2, "spec 全部字幕各得一条入场动画"
    for entry in entries:
        assert entry.kind == "in"
        assert _VIP_ID_PATTERN.match(entry.resource_id), entry.resource_id
        assert entry.duration_us and entry.duration_us > 0
    assert "node_10_inject_text_fx_done" in out["status_log"]
    assert any("文本装饰计划已入 spec" in tag for tag in out["status_log"])
    assert draft_file.read_text(encoding="utf-8") == draft_bytes, "草稿零写入"


def test_inject_text_fx_no_spec_subtitles(tmp_path: Path) -> None:
    """无 spec 字幕(空产物)→ 零装饰、done 标签,不崩。"""
    d = _tmp_spec_dir(tmp_path)
    out = inject_text_fx(_state(d))
    assert _emitted_animations(d) == []
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_fixed_mode_uses_config_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fixed 模式:config 固定名"新年打字机"未 verified(守卫)→ 回退可用子集池序第一个。"""
    from nodes.node_10_inject_text_fx import JIANYING_CACHE_ROOT

    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.TEXT_FX_INTRO_POOL_MODE", "fixed"
    )
    inject_text_fx(_state(d))
    names = {e.name for e in _emitted_animations(d)}
    (first_usable,) = (_r["name"] for _r in _usable_intro_rows(JIANYING_CACHE_ROOT)[:1])
    assert names == {first_usable}


def test_inject_text_fx_missing_text_resource_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """资源库缺失 → 动画降级(不进 spec)、done 标签,不崩不伪装成功。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.load_resource_libraries",
        lambda **_kw: __import__("jy_common.template_library", fromlist=["TemplateLibrary"]).TemplateLibrary(
            {"default_style": {}}, fx_lib={}, text_lib={}, flower_lib={},
        ),
    )
    out = inject_text_fx(_state(d))
    assert _emitted_animations(d) == []
    assert any("无可用入场动画资源" in tag for tag in out["status_log"])
    assert "node_10_inject_text_fx_done" in out["status_log"]


# ---------------------------------------------------------------------------
# job_id 链路(2026-11-18 修复的回归锁)
# ---------------------------------------------------------------------------
def test_inject_text_fx_uses_config_thread_id_as_seed(tmp_path: Path) -> None:
    """2.6 真实节点入口:config.thread_id 为权威 seed → 动画名 == 池公式预期条目。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    tid = "console-pooltest1"
    out = inject_text_fx(_state(d), {"configurable": {"thread_id": tid}})
    names = {e.name for e in _emitted_animations(d)}
    assert names == {_expected_pool_member(tid)["name"]}
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_falls_back_to_session_id(tmp_path: Path) -> None:
    """旧 checkpoint 兼容:state 无 thread_id 只有 session_id → seed = session_id。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    sid = "console-pooltest2"
    state = {**_state(d), "session_id": sid}
    inject_text_fx(state)
    names = {e.name for e in _emitted_animations(d)}
    assert names == {_expected_pool_member(sid)["name"]}


# ---------------------------------------------------------------------------
# zh/en 一致
# ---------------------------------------------------------------------------
def test_inject_text_fx_en_returns_delta_with_count_tag(tmp_path: Path) -> None:
    """英文分支:只返回 delta,status_log 带选取数标签(同超步无键冲突)。"""
    d = tmp_path / "en_branch"
    d.mkdir()
    (d / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _seed_spec_subtitles(d, ids=("text-en-1",), language="en")

    # 2026-11-18 适配:job_id 链路修复后 en 入口经公共 resolver 解析任务 ID,
    # 直接调用必须显式携带(生产环境 session_id 恒由入口写入;config 由图注入)
    out = inject_text_fx_en({"draft_dir_en_branch": str(d), "session_id": "console-en-delta"})
    entries = _emitted_animations(d)
    assert len(entries) == 1
    assert "node_10b_text_animations_selected:1" in out["status_log"]
    assert "node_10b_inject_text_fx_en_done" in out["status_log"]


def test_inject_text_fx_en_node_matches_zh_with_same_config(
    tmp_path: Path,
) -> None:
    """2.7 zh(node_10)与 en(node_10b)真实入口同 config → 动画逐字段一致。"""
    tid = "console-dualtest"
    config = {"configurable": {"thread_id": tid}}

    zh_dir = tmp_path / "zh"
    zh_dir.mkdir()
    (zh_dir / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _seed_spec_subtitles(zh_dir)
    inject_text_fx(_state(zh_dir), config)

    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    (en_dir / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _seed_spec_subtitles(en_dir, ids=("text-en-1", "text-en-2"), language="en")
    inject_text_fx_en(
        {"draft_dir_en_branch": str(en_dir), "status_log": [], "error_log": []}, config
    )

    zh_entries = _emitted_animations(zh_dir)
    en_entries = _emitted_animations(en_dir)
    assert len(zh_entries) == len(en_entries) == 2
    zh_fields = [(e.name, e.resource_id, e.effect_id, e.duration_us) for e in zh_entries]
    en_fields = [(e.name, e.resource_id, e.effect_id, e.duration_us) for e in en_entries]
    assert zh_fields == en_fields
    expected = _expected_pool_member(tid)
    assert zh_fields[0][0] == expected["name"]
    assert zh_fields[0][1] == expected["resource_id"]


def test_inject_text_fx_en_without_any_job_id_degrades(tmp_path: Path) -> None:
    """2.5 en 无任何任务 ID(config/state 全空)→ 显式降级:不发射 + 告警 + skipped 标签。"""
    d = tmp_path / "en_branch"
    d.mkdir()
    (d / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    _seed_spec_subtitles(d, ids=("text-en-1",), language="en")

    out = inject_text_fx_en({"draft_dir_en_branch": str(d), "status_log": [], "error_log": []})

    assert _emitted_animations(d) == []
    assert any("无法解析当前任务 ID" in m for m in out.get("error_log", []))
    assert "node_10b_inject_text_fx_en_skipped" in out.get("status_log", [])
    assert "node_10b_inject_text_fx_en_done" not in out.get("status_log", [])


# ---------------------------------------------------------------------------
# 池回退与审计(2026-11-19 守卫;纯选取直驱)
# ---------------------------------------------------------------------------
def _fake_lib_with_intro(rows: list[dict]):
    from jy_common.template_library import TemplateLibrary

    return TemplateLibrary({"default_style": {}}, fx_lib={}, text_lib={"intro": rows}, flower_lib={})


def _seed_position(job_id: str, pool_len: int) -> int:
    seed = int(hashlib.sha256(f"{job_id}|text_intro_pool".encode("utf-8")).hexdigest()[:8], 16)
    return seed % pool_len


def test_auto_pool_walks_past_unverified_seed_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫:seed 命中未 verified 条目 → 按池序回退到第一个可用条目。"""
    from nodes.node_10_inject_text_fx import _pick_intro_animation

    cache_root = tmp_path / "cache"
    (cache_root / "E-ok" / "M-ok").mkdir(parents=True)
    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(cache_root))
    rows = [
        {"name": "未实证", "is_vip": True, "effect_id": "E-bad", "md5": "M-bad", "resource_id": "1", "verified": False, "duration_s": 1.0},
        {"name": "已实证", "is_vip": True, "effect_id": "E-ok", "md5": "M-ok", "resource_id": "2", "verified": True, "duration_s": 1.0},
    ]
    lib = _fake_lib_with_intro(rows)
    job_id = "walk-test"
    assert _seed_position(job_id, 2) == 0, "前置:seed 落在未实证条目(否则换 job_id)"
    anim, audit = _pick_intro_animation(lib, job_id)
    assert anim is not None and anim["name"] == "已实证"
    assert audit["fallback_offset"] == 1


def test_verified_but_cache_missing_walks_to_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫:verified 但缓存目录缺失 → 跳过(防「动画丢失」)。"""
    from nodes.node_10_inject_text_fx import _pick_intro_animation

    cache_root = tmp_path / "cache"
    (cache_root / "E-ok" / "M-ok").mkdir(parents=True)
    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(cache_root))
    rows = [
        {"name": "缓存缺失", "is_vip": True, "effect_id": "E-no", "md5": "M-no", "resource_id": "1", "verified": True, "duration_s": 1.0},
        {"name": "缓存齐全", "is_vip": True, "effect_id": "E-ok", "md5": "M-ok", "resource_id": "2", "verified": True, "duration_s": 1.0},
    ]
    lib = _fake_lib_with_intro(rows)
    job_id = "walk-cache-test"
    assert _seed_position(job_id, 2) == 0, "前置:seed 落在缓存缺失条目(否则换 job_id)"
    anim, audit = _pick_intro_animation(lib, job_id)
    assert anim is not None and anim["name"] == "缓存齐全"
    assert audit["fallback_offset"] == 1


def test_no_usable_animation_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全池不可用 → (None, audit),调用方降级不进 spec。"""
    from nodes.node_10_inject_text_fx import _pick_intro_animation

    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(tmp_path / "empty"))
    lib = _fake_lib_with_intro([
        {"name": "未实证", "is_vip": True, "effect_id": "E-bad", "md5": "M-bad", "resource_id": "1", "verified": False, "duration_s": 1.0},
    ])
    anim, audit = _pick_intro_animation(lib, "no-usable")
    assert anim is None
    assert audit["usable_count"] == 0


def test_selection_audit_recomputable_from_tag(tmp_path: Path) -> None:
    """P2-01:status_log 选取标签含池大小/可用数/seed/选中 index,可离线复算。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d)
    tid = "console-audit-tag"
    out = inject_text_fx(_state(d), {"configurable": {"thread_id": tid}})
    tags = [t for t in out["status_log"] if t.startswith("node_10_text_anim_selected:")]
    assert len(tags) == 1
    expected = _expected_pool_member(tid)
    assert expected["name"] in tags[0]
    assert "pool=" in tags[0] and "usable=" in tags[0] and "mode=auto" in tags[0]

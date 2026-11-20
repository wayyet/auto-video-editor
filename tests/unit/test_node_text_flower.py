"""原生花字绑定器单测(2026-11-21,审查修订版阶段 3/4)。

覆盖 TF-04~06(缓存/验证状态机)、TF-08/09(范围)、TF-10(zh/en 一致)、
TF-12(幂等)、TF-14(no_text_segment 不产孤儿)、TF-15(人工素材不动)、
TF-16(降级不误删)、TF-17(关闭只停新写)、选取审计可复算、node_10 接线。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jy_common.cache_manifest import build_cache_manifest
from jy_common.template_library import TemplateLibrary
from nodes._text_flower import (
    apply_text_flower_effects,
    flower_entry_status,
    our_flower_material_id,
)

_EFFECT_ID = "7407340226189757733"
_MD5 = "c093f229a5b9529beab4d27c00bc0c4f"


def _text_material(text_id: str, text: str, start: int, duration: int) -> dict:
    return {
        "id": text_id,
        "type": "text",
        "content": json.dumps({"styles": [{"range": [0, len(text)]}], "text": text}, ensure_ascii=False),
        "target_timerange": {"start": start, "duration": duration},
        "style": {"size": 5.0},
        "track_name": "Subtitles_zh",
    }


@pytest.fixture
def flower_cache_root(tmp_path: Path) -> Path:
    """伪造 artistEffect 缓存根,返回它(目录内容 → manifest sha 由用例现算)。"""
    root = tmp_path / "artistEffect"
    d = root / _EFFECT_ID / _MD5
    d.mkdir(parents=True)
    (d / "config.json").write_text('{"version":"v1.0"}', encoding="utf-8")
    (d / "effectStyle.json").write_text("{}", encoding="utf-8")
    return root


def _verified_row(cache_root: Path, *, name: str = "互动立体", enabled: bool = True) -> dict:
    manifest = build_cache_manifest(cache_root, _EFFECT_ID, _MD5)
    return {
        "name": name,
        "category_id": "panel-text-flower",
        "effect_id": _EFFECT_ID,
        "resource_id": _EFFECT_ID,
        "md5": _MD5,
        "is_vip": True,
        "paid_type": "subscribe",
        "enabled": enabled,
        "verified": True,
        "verification": {
            "status": "verified",
            "verified_at": "2026-11-18",
            "jianying_version": "5.9.0.11632",
            "cache_manifest_sha256": str(manifest["manifest_sha256"]),
        },
    }


def _lib(row: dict | None) -> TemplateLibrary:
    return TemplateLibrary(
        {"default_style": {}},
        fx_lib={},
        text_lib={},
        flower_lib={"flowers": [row]} if row is not None else {},
    )


@pytest.fixture
def draft_with_texts(tmp_path: Path) -> Path:
    """2 条我方字幕 + 1 条人工 UUID 文本 + 1 条开场标题;轨段齐全。"""
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
                _text_material("hook-title-1", "标题", 0, 2_000_000),
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
                {"id": "text-seg-3", "material_id": "hook-title-1",
                 "target_timerange": {"start": 0, "duration": 2_000_000}},
            ],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------ 状态机(TF-03~06)


def test_entry_status_matrix(flower_cache_root: Path) -> None:
    """可用性状态机:只有 ready 入候选;四种非可用状态互相可区分(R-05)。"""
    row = _verified_row(flower_cache_root)
    assert flower_entry_status(row, flower_cache_root) == "ready"

    # enabled=false(资源级禁用)
    assert flower_entry_status({**row, "enabled": False}, flower_cache_root) == "disabled"
    # 未实证(元数据扫描不携带 verified)
    assert flower_entry_status({**row, "verified": False}, flower_cache_root) == "unverified"
    # 缓存目录不存在
    assert flower_entry_status(row, flower_cache_root / "empty") == "cache_missing"
    # 验证记录未固化 manifest 摘要 → cache_unverified(不静默当作完整可用)
    no_sha = _verified_row(flower_cache_root)
    del no_sha["verification"]["cache_manifest_sha256"]
    assert flower_entry_status(no_sha, flower_cache_root) == "cache_unverified"
    # 缓存内容被替换 → verification_stale
    stale_row = _verified_row(flower_cache_root)
    stale_row["verification"]["cache_manifest_sha256"] = "0" * 64
    assert flower_entry_status(stale_row, flower_cache_root) == "verification_stale"


def test_our_flower_material_id_deterministic_and_unique() -> None:
    """UUIDv5 派生:同字幕重放同 id(upsert 幂等),不同字幕不同 id。"""
    a1 = our_flower_material_id("text-zh-1")
    assert a1 == our_flower_material_id("text-zh-1")
    assert a1 != our_flower_material_id("text-zh-2")
    assert a1 != our_flower_material_id("text-en-1")
    assert len(a1) == 36 and a1.count("-") == 4 and a1 == a1.upper()


# ------------------------------------------------ 写入与范围(TF-08/09/15)


def test_apply_writes_flower_for_our_body_texts_only(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """我方正文字幕挂花字;开场标题(hook-title-*)与人工 UUID 文本不处理。"""
    draft = _load(draft_with_texts)
    warnings, summary = apply_text_flower_effects(
        draft,
        _lib(_verified_row(flower_cache_root)),
        job_id="job-a",
        enabled=True,
        tag="node_10",
        cache_root=flower_cache_root,
    )
    assert warnings == []
    assert summary["status"] == "written"
    assert summary["flowers_written"] == 2
    assert summary["texts_scanned"] == 2, "扫描计数只含我方正文字幕(hook-title/人工文本排除)"

    flowers = [e for e in draft["materials"]["effects"] if e.get("type") == "text_effect"]
    assert len(flowers) == 2
    ids = {e["id"] for e in flowers}
    assert ids == {our_flower_material_id("text-zh-1"), our_flower_material_id("text-zh-2")}
    for e in flowers:
        assert e["name"] == "互动立体"
        assert e["effect_id"] == _EFFECT_ID
        assert e["category_id"] == "panel-text-flower"
    segments = {s["material_id"]: s for s in draft["tracks"][0]["segments"]}
    for tid in ("text-zh-1", "text-zh-2"):
        assert our_flower_material_id(tid) in segments[tid]["extra_material_refs"]
    # 标题与人工文本段不得被绑定
    assert our_flower_material_id("hook-title-1") not in segments["hook-title-1"].get("extra_material_refs", [])
    human_id = our_flower_material_id("4F1A2B3C-HUMAN-0001")
    assert human_id not in {e["id"] for e in draft["materials"]["effects"]}


def test_apply_no_text_segment_writes_no_orphan(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """TF-14/R-09:字幕无轨段 → 记 no_text_segment,不写 effect material。"""
    draft = _load(draft_with_texts)
    draft["tracks"][0]["segments"] = [
        s for s in draft["tracks"][0]["segments"] if s["material_id"] == "text-zh-1"
    ]
    warnings, summary = apply_text_flower_effects(
        draft,
        _lib(_verified_row(flower_cache_root)),
        job_id="job-a",
        enabled=True,
        tag="node_10",
        cache_root=flower_cache_root,
    )
    assert summary["status"] == "written"
    assert summary["no_text_segment"] == 1
    assert summary["flowers_written"] == 1
    effects = [e for e in draft["materials"]["effects"] if e.get("type") == "text_effect"]
    assert [e["id"] for e in effects] == [our_flower_material_id("text-zh-1")], "无轨段字幕不得产生孤儿素材"


def test_apply_replay_byte_identical(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """TF-12:同一节点连续运行两次,material 与 refs 不重复(重放字节级稳定)。"""
    draft = _load(draft_with_texts)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    once = json.dumps(draft, ensure_ascii=False, sort_keys=True)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    twice = json.dumps(draft, ensure_ascii=False, sort_keys=True)
    assert once == twice
    flowers = [e for e in draft["materials"]["effects"] if e.get("type") == "text_effect"]
    assert len(flowers) == 2, "upsert 语义不得叠出重复条目"


def test_disabled_keeps_existing_no_cleanup(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """TF-17/R-10:开关关闭 = 不写新、不清理、不发告警;已有花字原样保留。"""
    draft = _load(draft_with_texts)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    before = json.dumps(draft["materials"]["effects"], ensure_ascii=False)
    warnings, summary = apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=False, tag="node_10", cache_root=flower_cache_root,
    )
    assert warnings == []
    assert summary["status"] == "disabled"
    assert json.dumps(draft["materials"]["effects"], ensure_ascii=False) == before


def test_degraded_no_resource_keeps_existing(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """TF-16:池无 ready 条目 → 降级告警,不写新素材,**不误删**已有花字。"""
    draft = _load(draft_with_texts)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    before_effects = json.dumps(draft["materials"]["effects"], ensure_ascii=False)
    before_refs = json.dumps(draft["tracks"][0]["segments"], ensure_ascii=False)
    # 缓存整体消失(配置错误/缓存被清)→ 全池 cache_missing
    warnings, summary = apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10",
        cache_root=flower_cache_root / "gone",
    )
    assert summary["status"] == "degraded_no_resource"
    assert any("无可用花字资源" in w for w in warnings)
    assert json.dumps(draft["materials"]["effects"], ensure_ascii=False) == before_effects
    assert json.dumps(draft["tracks"][0]["segments"], ensure_ascii=False) == before_refs


def test_empty_pool_degrades_not_fails(draft_with_texts: Path) -> None:
    """TF-18:池为空(花字库缺失)→ 降级不伪装成功,不影响其他写入。"""
    warnings, summary = apply_text_flower_effects(
        _load(draft_with_texts), _lib(None),
        job_id="job-a", enabled=True, tag="node_10", cache_root=None,
    )
    assert summary["status"] == "degraded_no_resource"
    assert summary["selection_audit"]["pool_size"] == 0
    assert any("无可用花字资源" in w for w in warnings)


# ------------------------------------------------ 清理(TF-15 非破坏)


def test_cleanup_removes_only_our_stale_flowers(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """清理只删带我方标记且未续用的条目;人工 UUID 花字(无标记)不删。"""
    draft = _load(draft_with_texts)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    human_flower = {
        "id": "E1D5E66A-HUMAN-FLOWER", "type": "text_effect",
        "category_id": "panel-text-flower", "name": "人工花字",
    }
    draft["materials"]["effects"].append(human_flower)
    # 模拟上一轮遗留:字幕 text-zh-2 被删除(轨段与素材一并消失)
    draft["materials"]["texts"] = [
        t for t in draft["materials"]["texts"] if t["id"] != "text-zh-2"
    ]
    draft["tracks"][0]["segments"] = [
        s for s in draft["tracks"][0]["segments"] if s["material_id"] != "text-zh-2"
    ]
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    effect_ids = {e["id"] for e in draft["materials"]["effects"]}
    assert our_flower_material_id("text-zh-2") not in effect_ids, "孤儿我方花字应被清理"
    assert "E1D5E66A-HUMAN-FLOWER" in effect_ids, "人工花字不得被清理波及"
    seg_refs = [s for s in draft["tracks"][0]["segments"] if s["material_id"] == "text-zh-1"][0]["extra_material_refs"]
    assert our_flower_material_id("text-zh-1") in seg_refs


def test_cleanup_disabled_when_degraded(
    draft_with_texts: Path, flower_cache_root: Path
) -> None:
    """R-10:本轮未写入(降级)时绝不触发清理 —— 草稿已有我方标记旧条目保留。"""
    draft = _load(draft_with_texts)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    stale_orphan = dict(
        draft["materials"]["effects"][0], id=our_flower_material_id("text-zh-9")
    )
    draft["materials"]["effects"].append(stale_orphan)
    apply_text_flower_effects(
        draft, _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10",
        cache_root=flower_cache_root / "gone",
    )
    effect_ids = {e["id"] for e in draft["materials"]["effects"]}
    assert our_flower_material_id("text-zh-9") in effect_ids, "降级轮不得清理(非破坏)"


# ------------------------------------------------ zh/en 一致与审计(TF-10)


def test_same_job_same_resource_zh_en(flower_cache_root: Path) -> None:
    """TF-10:同 job_id 的 zh/en 两次调用选出同一资源(name/effect_id/md5 一致)。"""

    def _resource_fields(draft: dict) -> set[tuple[str, str, str]]:
        return {
            (e["name"], e["effect_id"], e["resource_id"])
            for e in draft["materials"]["effects"]
            if e.get("type") == "text_effect"
        }

    zh = {
        "materials": {"texts": [_text_material("text-zh-1", "你好", 0, 1_000_000)]},
        "tracks": [{"type": "text", "segments": [
            {"id": "s1", "material_id": "text-zh-1",
             "target_timerange": {"start": 0, "duration": 1_000_000}}]}],
    }
    en = {
        "materials": {"texts": [_text_material("text-en-1", "hello", 0, 1_000_000)]},
        "tracks": [{"type": "text", "segments": [
            {"id": "s1", "material_id": "text-en-1",
             "target_timerange": {"start": 0, "duration": 1_000_000}}]}],
    }
    lib = _lib(_verified_row(flower_cache_root))
    apply_text_flower_effects(zh, lib, job_id="job-x", enabled=True, tag="n", cache_root=flower_cache_root)
    apply_text_flower_effects(en, lib, job_id="job-x", enabled=True, tag="n", cache_root=flower_cache_root)
    assert _resource_fields(zh) == _resource_fields(en) == {("互动立体", _EFFECT_ID, _EFFECT_ID)}
    # material id 按字幕 material id 派生(zh/en 不同,资源字段相同)
    assert our_flower_material_id("text-zh-1") in {e["id"] for e in zh["materials"]["effects"]}
    assert our_flower_material_id("text-en-1") in {e["id"] for e in en["materials"]["effects"]}


def test_selection_audit_seed_formula(flower_cache_root: Path) -> None:
    """选取审计可复算:seed = sha256(job_id|text_flower_pool)[:8] % pool_size。"""
    rows = [
        {"name": "甲", "effect_id": "E1", "resource_id": "R1", "md5": "M1", "verified": False},
        _verified_row(flower_cache_root),
    ]
    lib = TemplateLibrary({}, fx_lib={}, text_lib={}, flower_lib={"flowers": rows})
    audit_seed = None
    for i in range(1000):
        job_id = f"seed-search-{i}"
        seed = int(hashlib.sha256(f"{job_id}|text_flower_pool".encode("utf-8")).hexdigest()[:8], 16)
        if seed % 2 == 0:  # 起点落在甲(未实证)→ 应回退到乙
            audit_seed = (job_id, seed % 2)
            break
    assert audit_seed is not None
    job_id, start = audit_seed
    _warnings, summary = apply_text_flower_effects(
        {
            "materials": {"texts": [_text_material("text-zh-1", "你好", 0, 1_000_000)]},
            "tracks": [{"type": "text", "segments": [
                {"id": "s1", "material_id": "text-zh-1",
                 "target_timerange": {"start": 0, "duration": 1_000_000}}]}],
        },
        lib, job_id=job_id, enabled=True, tag="n", cache_root=flower_cache_root,
    )
    audit = summary["selection_audit"]
    assert audit["pool_size"] == 2
    assert audit["status_counts"]["unverified"] == 1
    assert audit["seed_start_index"] == start
    assert audit["selected_index"] == (start + 1) % 2, "seed 落未实证条目应按池序回退"
    assert audit["fallback_offset"] == 1
    assert summary["selected"]["name"] == "互动立体"


# ------------------------------------------------ node_10 接线(apply_text_fx)


def test_apply_text_fx_wires_flower_and_status_label(
    draft_with_texts: Path,
    flower_cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线:动画之后同轮写花字;status_log 出现选中标签;摘要带 text_flower 子键。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    row = _verified_row(flower_cache_root)
    monkeypatch.setattr(
        "nodes._text_flower.JIANYING_ARTIST_EFFECT_CACHE_ROOT", str(flower_cache_root)
    )
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.load_resource_libraries",
        lambda **_kw: TemplateLibrary(
            {"default_style": {}}, fx_lib={}, text_lib={}, flower_lib={"flowers": [row]}
        ),
    )
    log, summary = apply_text_fx(
        draft_with_texts, [], node_tag="node_10", done_tag="done_tag", job_id="job-wire"
    )
    draft = _load(draft_with_texts)
    flowers = [e for e in draft["materials"]["effects"] if e.get("type") == "text_effect"]
    assert len(flowers) == 2
    assert summary["text_flower"]["status"] == "written"
    tags = [t for t in log if t.startswith("node_10_text_flower_selected:")]
    assert len(tags) == 1
    assert "互动立体@7407340226189757733" in tags[0]
    assert "usable=1" in tags[0] and "mode=auto" in tags[0]
    assert "done_tag" in log


def test_apply_text_fx_disabled_flower_leaves_animations_intact(
    draft_with_texts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TF-28:花字功能关闭时,入场动画链路行为不变(不回归)。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    monkeypatch.setattr("nodes.node_10_inject_text_fx.TEXT_FLOWER_ENABLED", False)
    log, summary = apply_text_fx(
        draft_with_texts, [], node_tag="node_10", done_tag="done_tag", job_id="job-off"
    )
    draft = _load(draft_with_texts)
    assert not draft["materials"].get("effects")
    assert summary["text_flower"]["status"] == "disabled"
    assert draft["materials"]["material_animations"], "动画不受花字开关影响"
    assert not any("text_flower_selected" in t for t in log)
    assert "done_tag" in log

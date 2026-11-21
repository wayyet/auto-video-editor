"""花字选取器单测(2026-11-21 审查修订版;2026-11-22 SDK 链转正裁剪)。

2026-11-22 SDK 链转正后 _text_flower 只负责**选取**(不触碰草稿;花字经
TextDecorationSpec → node_11 SDK 编译落 materials.effects 6 键形态)。保留:
TF-04~06 缓存/验证状态机、TF-10(zh/en 一致)、选取审计可复算、node_10 接线。
删除:legacy 原生写入/绑定/清理类测试(TF-12/14/15/16 清理语义随 legacy 链退役,
"不触碰草稿"由节点无写入天然保证)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jy_common.cache_manifest import build_cache_manifest
from jy_common.template_library import TemplateLibrary
from nodes._text_flower import (
    flower_entry_status,
    select_text_flower,
)

_EFFECT_ID = "7407340226189757733"
_MD5 = "c093f229a5b9529beab4d27c00bc0c4f"


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


# ------------------------------------------------ 选取(TF-08/17)


def test_select_returns_ready_resource(flower_cache_root: Path) -> None:
    """ready 条目 → status=selected;selected 携带双 ID(编译器三重门禁复检)。"""
    warnings, summary = select_text_flower(
        _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10", cache_root=flower_cache_root,
    )
    assert warnings == []
    assert summary["status"] == "selected"
    assert summary["selected"] == {
        "name": "互动立体", "effect_id": _EFFECT_ID, "resource_id": _EFFECT_ID,
    }


def test_disabled_selects_nothing(flower_cache_root: Path) -> None:
    """TF-17/R-10:开关关闭 = 不选取、不发告警。"""
    warnings, summary = select_text_flower(
        _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=False, tag="node_10", cache_root=flower_cache_root,
    )
    assert warnings == []
    assert summary["status"] == "disabled"
    assert summary["selected"] is None


def test_degraded_no_resource(flower_cache_root: Path) -> None:
    """TF-16:池无 ready 条目 → 降级告警(缓存整体消失场景)。"""
    warnings, summary = select_text_flower(
        _lib(_verified_row(flower_cache_root)),
        job_id="job-a", enabled=True, tag="node_10",
        cache_root=flower_cache_root / "gone",
    )
    assert summary["status"] == "degraded_no_resource"
    assert any("无可用花字资源" in w for w in warnings)
    assert summary["selected"] is None


def test_empty_pool_degrades_not_fails() -> None:
    """TF-18:池为空(花字库缺失)→ 降级不伪装成功。"""
    warnings, summary = select_text_flower(
        _lib(None), job_id="job-a", enabled=True, tag="node_10", cache_root=None,
    )
    assert summary["status"] == "degraded_no_resource"
    assert summary["selection_audit"]["pool_size"] == 0
    assert any("无可用花字资源" in w for w in warnings)


# ------------------------------------------------ zh/en 一致与审计(TF-10)


def test_same_job_same_resource_zh_en(flower_cache_root: Path) -> None:
    """TF-10:同 job_id 的 zh/en 两次选取得到同一资源(双稿一致)。"""
    lib = _lib(_verified_row(flower_cache_root))
    zh = select_text_flower(lib, job_id="job-x", enabled=True, tag="n", cache_root=flower_cache_root)[1]
    en = select_text_flower(lib, job_id="job-x", enabled=True, tag="n", cache_root=flower_cache_root)[1]
    assert zh["selected"] == en["selected"] == {
        "name": "互动立体", "effect_id": _EFFECT_ID, "resource_id": _EFFECT_ID,
    }


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
    _warnings, summary = select_text_flower(
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


def _seed_spec_subtitles(tmp_path: Path) -> None:
    from jianying_sdk.spec import SubtitleSpec
    from nodes._draft_build_spec import replace_spec_sections

    replace_spec_sections(tmp_path, subtitles=[
        SubtitleSpec(subtitle_id="text-zh-1", text="你好", start_us=0, end_us=1_500_000, language="zh"),
        SubtitleSpec(subtitle_id="text-zh-2", text="世界", start_us=1_500_000, end_us=3_000_000, language="zh"),
    ])


def test_apply_text_fx_wires_flower_and_status_label(
    tmp_path: Path,
    flower_cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线:spec 装饰带花字;status_log 出现选中标签;摘要带 text_flower 子键;草稿不动。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    _seed_spec_subtitles(tmp_path)
    draft_path = tmp_path / "draft_content.json"
    draft_bytes = json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False)
    draft_path.write_text(draft_bytes, encoding="utf-8")

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
        tmp_path, [], node_tag="node_10", done_tag="done_tag", job_id="job-wire"
    )
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert len(spec.text_decorations) == 2, "spec 全部字幕各得一条装饰"
    for deco in spec.text_decorations:
        assert deco.text_effect is not None
        assert deco.text_effect.effect_id == _EFFECT_ID
    assert summary["text_flower"]["status"] == "selected"
    tags = [t for t in log if t.startswith("node_10_text_flower_selected:")]
    assert len(tags) == 1
    assert "互动立体@7407340226189757733" in tags[0]
    assert "usable=1" in tags[0] and "mode=auto" in tags[0]
    assert "done_tag" in log
    # 草稿不被 node_10 改写
    assert draft_path.read_text(encoding="utf-8") == draft_bytes


def test_apply_text_fx_disabled_flower_keeps_animations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TF-28:花字功能关闭时,入场动画链路行为不变(不回归)。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    _seed_spec_subtitles(tmp_path)
    (tmp_path / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("nodes.node_10_inject_text_fx.TEXT_FLOWER_ENABLED", False)
    log, summary = apply_text_fx(
        tmp_path, [], node_tag="node_10", done_tag="done_tag", job_id="job-off"
    )
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert all(d.text_effect is None for d in spec.text_decorations)
    assert summary["text_flower"]["status"] == "disabled"
    assert all(d.animations for d in spec.text_decorations), "动画不受花字开关影响"
    assert not any("text_flower_selected" in t for t in log)
    assert "done_tag" in log

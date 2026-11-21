"""花字双语草稿集成测试(2026-11-21 审查修订版阶段 6;2026-11-22 SDK 链转正改版)。

不依赖 PostgreSQL / 剪映进程:走 node_10 真实 apply_text_fx 发射链(装饰计划 →
DraftBuildSpec.text_decorations),验证同 job_id 的 zh/en 双稿花字选取一致、
重放幂等、审计标签齐全。legacy 的草稿 effects 桶写入与 mutations 审计随六段
legacy 链退役 —— 草稿落轨由 node_11 SDK 编译统一承担(test_jianying_sdk_phase6
编译用例锁定 materials.effects 落桶形态)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.cache_manifest import build_cache_manifest
from jy_common.template_library import TemplateLibrary
from nodes._draft_build_spec import load_build_spec, replace_spec_sections
from nodes.node_10_inject_text_fx import apply_text_fx

_EFFECT_ID = "7407340226189757733"
_MD5 = "c093f229a5b9529beab4d27c00bc0c4f"


@pytest.fixture
def flower_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """伪造花字缓存根 + 池行(verified + manifest 一致),封闭 artistEffect 路径。"""
    cache_root = tmp_path / "artistEffect"
    d = cache_root / _EFFECT_ID / _MD5
    d.mkdir(parents=True)
    (d / "config.json").write_text('{"version":"v1.0"}', encoding="utf-8")
    (d / "effectStyle.json").write_text("{}", encoding="utf-8")
    manifest_sha = str(build_cache_manifest(cache_root, _EFFECT_ID, _MD5)["manifest_sha256"])
    row = {
        "name": "互动立体",
        "category_id": "panel-text-flower",
        "effect_id": _EFFECT_ID,
        "resource_id": _EFFECT_ID,
        "md5": _MD5,
        "is_vip": True,
        "paid_type": "subscribe",
        "enabled": True,
        "verified": True,
        "verification": {"cache_manifest_sha256": manifest_sha},
    }
    monkeypatch.setattr(
        "nodes._text_flower.JIANYING_ARTIST_EFFECT_CACHE_ROOT", str(cache_root)
    )
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.load_resource_libraries",
        lambda **_kw: TemplateLibrary(
            {"default_style": {}}, fx_lib={}, text_lib={}, flower_lib={"flowers": [row]}
        ),
    )
    return cache_root


def _seed_spec(draft_dir: Path, ids: list[str]) -> None:
    from jianying_sdk.spec import SubtitleSpec

    replace_spec_sections(draft_dir, subtitles=[
        SubtitleSpec(
            subtitle_id=tid, text=f"text-{i}",
            start_us=i * 1_500_000, end_us=(i + 1) * 1_500_000,
            language="zh" if tid.startswith("text-zh-") else "en",  # type: ignore[arg-type]
        )
        for i, tid in enumerate(ids)
    ])


def _tmp_draft_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "draft_content.json").write_text(
        json.dumps({"canvas_config": {"width": 1080, "height": 1920}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _flower_decos(draft_dir: Path) -> list:
    spec = load_build_spec(draft_dir)
    return [d for d in (spec.text_decorations if spec else []) if d.text_effect is not None]


def test_dual_draft_flower_consistent_and_audited(tmp_path: Path, flower_env: Path) -> None:
    """同 job_id:zh/en 双稿花字选取一致、幂等重放稳定、审计标签齐全、草稿零写入。"""
    zh_dir = _tmp_draft_dir(tmp_path / "AutoVideo_console-zh")
    zh_dir.parent.mkdir(parents=True, exist_ok=True)
    _seed_spec(zh_dir, ["text-zh-1", "text-zh-2"])
    en_dir = _tmp_draft_dir(tmp_path / "en_branch")
    _seed_spec(en_dir, ["text-en-1", "text-en-2"])

    job_id = "console-flower-e2e"
    log1, _ = apply_text_fx(zh_dir, [], node_tag="node_10", done_tag="zh_done", job_id=job_id)
    log2, _ = apply_text_fx(en_dir, [], node_tag="node_10b", done_tag="en_done", job_id=job_id)

    zh_decos, en_decos = _flower_decos(zh_dir), _flower_decos(en_dir)
    assert len(zh_decos) == len(en_decos) == 2, "spec 全部字幕各得一条花字装饰"
    for deco in (*zh_decos, *en_decos):
        assert (deco.text_effect.name, deco.text_effect.effect_id) == ("互动立体", _EFFECT_ID)
        assert deco.text_effect.verification == "cache_verified"
    # status_log 标签:双稿各自带花字选中审计
    assert any("node_10_text_flower_selected:" in t for t in log1)
    assert any("node_10b_text_flower_selected:" in t for t in log2)
    assert "zh_done" in log1 and "en_done" in log2

    # 幂等重放:草稿与 spec 字节级稳定,装饰不翻倍
    first_draft = (zh_dir / "draft_content.json").read_bytes()
    first_spec = (zh_dir / ".autovideo_draft_build_spec.json").read_bytes()
    apply_text_fx(zh_dir, [], node_tag="node_10", done_tag="zh_done", job_id=job_id)
    assert (zh_dir / "draft_content.json").read_bytes() == first_draft, "草稿零写入"
    assert (zh_dir / ".autovideo_draft_build_spec.json").read_bytes() == first_spec
    assert len(_flower_decos(zh_dir)) == 2

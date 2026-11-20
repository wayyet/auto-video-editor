"""原生花字双语草稿集成测试(2026-11-21,审查修订版阶段 6 / TF-10/TF-12/TF-13)。

不依赖 PostgreSQL / 剪映进程:走 node_10 真实 apply_text_fx 链(动画 + 花字 +
原子写回 + mutations 审计),验证同 job_id 的 zh/en 双草稿花字一致、重放幂等、
审计记录带 effects 桶计数与花字指纹。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.cache_manifest import build_cache_manifest
from nodes._draft_io import read_draft_mutations
from nodes._text_flower import our_flower_material_id
from nodes.node_10_inject_text_fx import apply_text_fx
from jy_common.template_library import TemplateLibrary

_EFFECT_ID = "7407340226189757733"
_MD5 = "c093f229a5b9529beab4d27c00bc0c4f"


def _text_material(text_id: str, text: str) -> dict:
    return {
        "id": text_id,
        "type": "text",
        "content": json.dumps({"styles": [{"range": [0, len(text)]}], "text": text}, ensure_ascii=False),
        "target_timerange": {"start": 0, "duration": 1_500_000},
        "style": {"size": 5.0},
    }


def _draft(texts: list[tuple[str, str]]) -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [_text_material(tid, t) for tid, t in texts]},
        "tracks": [{
            "type": "text",
            "id": "track-text-1",
            "segments": [
                {"id": f"seg-{tid}", "material_id": tid,
                 "target_timerange": {"start": 0, "duration": 1_500_000}}
                for tid, _ in texts
            ],
        }],
    }


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


def test_dual_draft_flower_consistent_and_audited(tmp_path: Path, flower_env: Path) -> None:
    """同 job_id:zh/en 双草稿花字资源一致、幂等重放稳定、审计带指纹与桶计数。"""
    zh_file = tmp_path / "AutoVideo_console-zh" / "draft_content.json"
    zh_file.parent.mkdir(parents=True)
    zh_file.write_text(json.dumps(_draft([("text-zh-1", "你好"), ("text-zh-2", "世界")]), ensure_ascii=False), encoding="utf-8")
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    en_file = en_dir / "draft_content.json"
    en_file.write_text(json.dumps(_draft([("text-en-1", "hello"), ("text-en-2", "world")])), encoding="utf-8")

    job_id = "console-flower-e2e"
    log1, _ = apply_text_fx(zh_file, [], node_tag="node_10", done_tag="zh_done", job_id=job_id)
    log2, _ = apply_text_fx(en_file, [], node_tag="node_10b", done_tag="en_done", job_id=job_id)

    zh = json.loads(zh_file.read_text(encoding="utf-8"))
    en = json.loads(en_file.read_text(encoding="utf-8"))

    # 双稿花字:资源字段一致,material id 按各语种字幕派生
    def flowers(draft: dict) -> list[dict]:
        return [e for e in draft["materials"]["effects"] if e.get("type") == "text_effect"]

    zh_flowers, en_flowers = flowers(zh), flowers(en)
    assert len(zh_flowers) == len(en_flowers) == 2
    assert {(e["name"], e["effect_id"]) for e in zh_flowers} == {
        (e["name"], e["effect_id"]) for e in en_flowers
    } == {("互动立体", _EFFECT_ID)}
    assert {e["id"] for e in zh_flowers} == {
        our_flower_material_id("text-zh-1"), our_flower_material_id("text-zh-2")
    }
    assert {e["id"] for e in en_flowers} == {
        our_flower_material_id("text-en-1"), our_flower_material_id("text-en-2")
    }
    # refs 绑定齐全
    for draft, tids in ((zh, ("text-zh-1", "text-zh-2")), (en, ("text-en-1", "text-en-2"))):
        segments = {s["material_id"]: s for s in draft["tracks"][0]["segments"]}
        for tid in tids:
            assert our_flower_material_id(tid) in segments[tid]["extra_material_refs"]
    # status_log 标签:双稿各自带花字选中审计
    assert any("node_10_text_flower_selected:" in t for t in log1)
    assert any("node_10b_text_flower_selected:" in t for t in log2)

    # 幂等重放(TF-12/13):字节级稳定,effects 桶计数不翻倍
    first = zh_file.read_bytes()
    apply_text_fx(zh_file, [], node_tag="node_10", done_tag="zh_done", job_id=job_id)
    assert zh_file.read_bytes() == first
    assert len(flowers(json.loads(zh_file.read_text(encoding="utf-8")))) == 2

    # mutations 审计:effects 桶计数 + 花字指纹(R-08)
    zh_records = read_draft_mutations(zh_file.parent)
    assert records_have_effects_signal(zh_records)


def records_have_effects_signal(records: list[dict]) -> bool:
    """node_10 写入记录应出现 effects 桶计数变化或花字指纹。"""
    for rec in records:
        if rec.get("node_id") != "node_10":
            continue
        fp = rec.get("flower_fingerprint") or {}
        after_effects = (fp.get("after") or {}).get("effects_sha")
        if rec.get("changed_counts", {}).get("materials.effects") or after_effects:
            return True
    return False

"""花字资源池单测(2026-11-21 原生花字通道,审查修订版阶段 2)。

覆盖 TF-01(证据导入去重)/ TF-02(缺字段不入候选)/ 池文件真实形状 /
TemplateLibrary 花字 API 与三参调用兼容(R-12)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.cache_manifest import build_cache_manifest
from jy_common.template_library import TemplateLibrary, load_resource_libraries
from scripts.import_text_flower_pool import EVIDENCE_PATH, POOL_PATH, build_pool


def _fabricate_flower_cache(root: Path, effect_id: str, md5: str) -> str:
    """伪造花字资源包缓存目录,返回其 manifest_sha256(完整性守卫基准)。"""
    d = root / effect_id / md5
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text('{"version":"v1.0"}', encoding="utf-8")
    (d / "effectStyle.json").write_text("{}", encoding="utf-8")
    manifest = build_cache_manifest(root, effect_id, md5)
    assert manifest is not None
    return str(manifest["manifest_sha256"])


def _fake_evidence(records: list[dict]) -> dict:
    return {"authoritative_own_item_records": records}


def _evidence_record(rowid: int, effect_id: str, title: str, md5: str, is_vip: bool) -> dict:
    return {
        "rowid": rowid,
        "url_head": f"/artist/v1/effect/get_resources_by_category_id_X_flower_jianyingpro_0",
        "cache_timestamp": "2026-11-18 00:00:23",
        "common_attr": {
            "id": effect_id,
            "effect_id": effect_id,
            "title": title,
            "md5": md5,
            "business_info": {
                "json_str": json.dumps(
                    {"is_vip": is_vip, "paid_type": "subscribe"}
                    if is_vip
                    else {"is_vip": False, "paid_type": "free"}
                )
            },
        },
    }


# ------------------------------------------------ TF-01:证据导入按资源身份去重


def test_build_pool_dedupes_same_resource_records(tmp_path: Path) -> None:
    """R-02:2 条重复记录(同 effect_id/resource_id/md5)归一为 1 个唯一资源。"""
    rec = _evidence_record(3449, "7407", "互动立体", "c093", is_vip=True)
    dup = _evidence_record(3458, "7407", "互动立体", "c093", is_vip=True)
    import scripts.import_text_flower_pool as mod

    manifest_sha = _fabricate_flower_cache(tmp_path, "7407", "c093")
    orig_evidence = mod.EVIDENCE_PATH
    try:
        mod.EVIDENCE_PATH = _write_evidence(tmp_path, _fake_evidence([rec, dup]))
        pool = build_pool(tmp_path)
    finally:
        mod.EVIDENCE_PATH = orig_evidence

    assert pool["_counts"]["records_raw"] == 2
    assert pool["_counts"]["flowers_unique"] == 1
    (row,) = pool["flowers"]
    assert row["name"] == "互动立体"
    assert row["effect_id"] == "7407"
    assert row["resource_id"] == "7407"
    assert row["md5"] == "c093"
    assert row["is_vip"] is True
    assert row["paid_type"] == "subscribe"
    assert row["category_id"] == "panel-text-flower"
    assert row["enabled"] is True
    assert row["verified"] is True, "有 GUI 实证证据的唯一资源应标 verified"
    assert row["verification"]["cache_manifest_sha256"] == manifest_sha


def test_build_pool_unverified_resource_has_no_verification(tmp_path: Path) -> None:
    """未实测资源 verified=false 且无 verification 记录(绝不自行伪造 verified)。"""
    rec = _evidence_record(1, "9999", "某未实测花字", "abc", is_vip=True)
    import scripts.import_text_flower_pool as mod

    orig_evidence = mod.EVIDENCE_PATH
    try:
        mod.EVIDENCE_PATH = _write_evidence(tmp_path, _fake_evidence([rec]))
        pool = build_pool(tmp_path)
    finally:
        mod.EVIDENCE_PATH = orig_evidence

    (row,) = pool["flowers"]
    assert row["verified"] is False
    assert "verification" not in row


def _write_evidence(tmp_path: Path, evidence: dict) -> Path:
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    return p


# ------------------------------------------------ 真实池文件形状(已入库产物)


def test_real_pool_file_shape() -> None:
    """入库池文件:1 个唯一资源(互动立体),verified + manifest 摘要齐备。"""
    pool = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    assert pool["_counts"]["records_raw"] == 2
    assert pool["_counts"]["flowers_unique"] == 1
    (row,) = pool["flowers"]
    assert row["name"] == "互动立体"
    assert row["effect_id"] == "7407340226189757733"
    assert row["md5"] == "c093f229a5b9529beab4d27c00bc0c4f"
    assert row["category_id"] == "panel-text-flower"
    assert row["verified"] is True
    assert row["verification"]["cache_manifest_sha256"], "manifest 摘要必须已固化"
    assert row["verification"]["evidence_path"].startswith("docs/")


def test_real_evidence_file_is_sanitized() -> None:
    """R-01:证据源不含用户名与请求签名(公开仓库脱敏边界锁定)。"""
    raw = EVIDENCE_PATH.read_text(encoding="utf-8")
    assert "wayyet" not in raw
    assert "Cz6dRIFcs62WYn9sZEgyS5EupAevbZZ" not in raw


# ------------------------------------------------ TemplateLibrary 花字 API


def _lib_with_flowers(rows: list[dict] | None) -> TemplateLibrary:
    return TemplateLibrary(
        {"default_style": {}},
        fx_lib={},
        text_lib={},
        flower_lib={"flowers": rows} if rows is not None else {},
    )


def test_template_library_flower_apis() -> None:
    rows = [
        {"name": "互动立体", "effect_id": "7407", "is_vip": True, "enabled": True},
        {"name": "禁用花字", "effect_id": "0001", "enabled": False},
    ]
    lib = _lib_with_flowers(rows)
    assert lib.has_flower_lib is True
    assert [r["name"] for r in lib.list_flower_rows()] == ["互动立体", "禁用花字"]
    picked = lib.pick_flower_by_name("互动立体")
    assert picked is not None and picked["effect_id"] == "7407"
    assert lib.pick_flower_by_name("不存在") is None


def test_template_library_without_flower_lib_degrades() -> None:
    """花字库缺失/未传 = 空库,has_flower_lib=False,查询返回 None(R-12)。"""
    empty = TemplateLibrary({}, fx_lib={}, text_lib={})
    assert empty.has_flower_lib is False
    assert empty.list_flower_rows() == []
    assert empty.pick_flower_by_name("互动立体") is None


def test_load_resource_libraries_three_arg_call_still_works(tmp_path: Path) -> None:
    """R-12:既有三参数调用不变(第四参可选,缺省无花字库)。"""
    fx_template = tmp_path / "fx_template.json"
    fx_template.write_text(json.dumps({"transitions": []}), encoding="utf-8")
    fx_resource = tmp_path / "fx_resource_library.json"
    fx_resource.write_text(json.dumps({"transitions": []}), encoding="utf-8")
    text_resource = tmp_path / "text_resource_library.json"
    text_resource.write_text(json.dumps({"intro": []}), encoding="utf-8")

    lib = load_resource_libraries(fx_template, fx_resource, text_resource)
    assert lib.has_flower_lib is False
    assert lib.has_text_lib is False, "intro 空列表不算有库(既有语义)"
    assert lib.list_text_animation_rows("intro") == []
    assert lib.list_flower_rows() == []

    flower_file = tmp_path / "text_flower_resource_library.json"
    flower_file.write_text(
        json.dumps({"flowers": [{"name": "互动立体"}]}, ensure_ascii=False), encoding="utf-8"
    )
    lib4 = load_resource_libraries(fx_template, fx_resource, text_resource, flower_file)
    assert lib4.has_flower_lib is True


def test_load_resource_libraries_missing_flower_file_is_empty(tmp_path: Path) -> None:
    fx_template = tmp_path / "fx_template.json"
    fx_template.write_text("{}", encoding="utf-8")
    lib = load_resource_libraries(
        fx_template, tmp_path / "no-fx.json", tmp_path / "no-text.json",
        tmp_path / "no-flower.json",
    )
    assert lib.has_flower_lib is False


def test_pool_rows_missing_identity_never_ready(tmp_path: Path) -> None:
    """TF-02:缺 effect_id/resource_id/md5 的条目不可用(cache_missing 兜底)。"""
    from nodes._text_flower import flower_entry_status

    broken_rows = [
        {"name": "缺md5", "effect_id": "E", "resource_id": "R", "verified": True},
        {"name": "缺effect", "resource_id": "R", "md5": "M", "verified": True},
    ]
    for row in broken_rows:
        assert flower_entry_status(row, tmp_path) == "cache_missing"

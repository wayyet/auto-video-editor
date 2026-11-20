"""原生花字构造器单测(2026-11-21,审查修订版阶段 3)。

字段形状以 Golden Sample(docs/第七期开发/花字审计证据_20261118/
zh_draft_flower_extract.json,GUI 实证)为准逐字段锁定;ID 策略 = UUIDv5
(R-03);fail-closed 边界(TF-02/03)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jy_common.native_fx import (
    OUR_TEXT_FLOWER_PRIVATE_KEY,
    OUR_TEXT_FLOWER_ROLE,
    build_native_text_effect_material,
    flower_effect_cache_available,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_EXTRACT = (
    _REPO_ROOT / "docs" / "第七期开发" / "花字审计证据_20261118" / "zh_draft_flower_extract.json"
)

_FLOWER_META = {
    "name": "互动立体",
    "effect_id": "7407340226189757733",
    "resource_id": "7407340226189757733",
    "md5": "c093f229a5b9529beab4d27c00bc0c4f",
}


@pytest.fixture
def flower_cache(tmp_path: Path) -> Path:
    """伪造花字资源包缓存(Cache/artistEffect/<effect_id>/<md5>/ 5 文件形态)。"""
    d = tmp_path / "artistEffect" / _FLOWER_META["effect_id"] / _FLOWER_META["md5"]
    d.mkdir(parents=True)
    for name in ("6beec301", "986143b7", "config.json", "cover_icon.png", "effectStyle.json"):
        (d / name).write_bytes(b"x")
    return tmp_path / "artistEffect"


def test_builder_fields_match_golden_sample(flower_cache: Path) -> None:
    """构造器输出与 Golden Sample 条目逐字段同形(除实例性字段 id/path/request_id)。"""
    material = build_native_text_effect_material(
        material_id="1D5E66AA-119A-416A-A828-3157E60EDDD3",
        flower_meta=_FLOWER_META,
        artist_effect_cache_root=flower_cache,
    )
    golden = json.loads(_GOLDEN_EXTRACT.read_text(encoding="utf-8"))["text_effect_entries"][0]
    instance_fields = {"id", "path", "request_id"}
    for key, golden_value in golden.items():
        if key in instance_fields:
            continue
        assert key in material, f"Golden Sample 字段 {key} 缺失"
        assert material[key] == golden_value, f"字段 {key} 与 Golden Sample 不一致"
    assert set(golden) - instance_fields <= set(material)
    # path 由当前运行环境缓存根动态构造(Golden Sample 同款 POSIX 正斜杠形态)
    assert material["path"] == (flower_cache / _FLOWER_META["effect_id"] / _FLOWER_META["md5"]).as_posix()


def test_builder_carries_our_private_marker(flower_cache: Path) -> None:
    """我方私有标记键存在(清理逻辑识别我方素材;人工 UUID 花字无此键)。"""
    material = build_native_text_effect_material(
        material_id="X",
        flower_meta=_FLOWER_META,
        artist_effect_cache_root=flower_cache,
    )
    assert material[OUR_TEXT_FLOWER_PRIVATE_KEY] == OUR_TEXT_FLOWER_ROLE


def test_builder_id_is_caller_supplied_uuid_shape(flower_cache: Path) -> None:
    """material id 由调用方传入(UUIDv5 大写形态,Glyph 与 Golden Sample 同形)。"""
    material = build_native_text_effect_material(
        material_id="B3B4E112-3E71-5CD3-9ACB-2A2D6F7A18B4",
        flower_meta=_FLOWER_META,
        artist_effect_cache_root=flower_cache,
    )
    assert material["id"] == "B3B4E112-3E71-5CD3-9ACB-2A2D6F7A18B4"
    assert re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", material["id"])


def test_builder_fail_closed_on_incomplete_meta(flower_cache: Path) -> None:
    """TF-02:元数据缺 effect_id/resource_id/name → ValueError,不产占位素材。"""
    for key in ("effect_id", "resource_id", "name"):
        broken = {k: v for k, v in _FLOWER_META.items() if k != key}
        with pytest.raises(ValueError):
            build_native_text_effect_material(
                material_id="X", flower_meta=broken, artist_effect_cache_root=flower_cache
            )


def test_builder_fail_closed_on_missing_cache(tmp_path: Path) -> None:
    """TF-03/TF-04:缓存目录不存在 → ValueError(不写悬空 path)。"""
    missing_root = tmp_path / "no-such-artist-effect"
    with pytest.raises(ValueError):
        build_native_text_effect_material(
            material_id="X", flower_meta=_FLOWER_META, artist_effect_cache_root=missing_root
        )


def test_flower_cache_available_dir_check(tmp_path: Path) -> None:
    """TF-03/TF-04:artistEffect 目录存在性判断(effect_id+md5 缺一不可)。"""
    assert flower_effect_cache_available(_FLOWER_META, tmp_path) is False
    d = tmp_path / _FLOWER_META["effect_id"] / _FLOWER_META["md5"]
    d.mkdir(parents=True)
    assert flower_effect_cache_available(_FLOWER_META, tmp_path) is True
    assert flower_effect_cache_available({"effect_id": "", "md5": "m"}, tmp_path) is False
    assert flower_effect_cache_available(_FLOWER_META, None) is False

"""fx_template 转场池 / 视频特效白名单 × fx_resource_library 完整性扫描。

2026-11-17 核验报告 §6.4/§8.2:池成员必须**分段**精确命中
``transitions`` 数组且有 19 位 resource_id;特效候选必须精确命中
``video_effects`` 数组 —— 同名资源可能存在于另一类型数组(金色光斑/金沙
是转场、推近推远是特效),只验"名字在文件里"不够。

本文件直接扫真实 templates/*.json(配置耦合测试,golden 先例:
test_native_fx / prompt golden)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jy_common.template_library import TemplateLibrary

REPO = Path(__file__).resolve().parents[2]
FX_TEMPLATE_PATH = REPO / "templates" / "fx_template.json"
FX_LIBRARY_PATH = REPO / "templates" / "fx_resource_library.json"

_RESOURCE_ID_RE = re.compile(r"^\d{19}$")

_EXPECTED_POOLS = (
    "pool_gentle",
    "pool_zoom",
    "pool_slide",
    "pool_whip",
    "pool_creative",
)

# 核验报告 §5.2/§5.3 资源类型纠错 —— 防止错配回归进配置
_MUST_NOT_IN_ANY_POOL = ("推近推远",)  # 它是 video_effect
_MUST_NOT_IN_EFFECT_WHITELIST = ("金色光斑", "金沙")  # 它们是转场


@pytest.fixture(scope="module")
def real_template() -> TemplateLibrary:
    with open(FX_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return TemplateLibrary(data)


@pytest.fixture(scope="module")
def library_sections() -> tuple[dict[str, dict], dict[str, dict]]:
    """返回 (transitions[name→row], video_effects[name→row]) 分段名索引。"""
    with open(FX_LIBRARY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    tr = {row["name"]: row for row in data.get("transitions", [])}
    fx = {row["name"]: row for row in data.get("video_effects", [])}
    return tr, fx


def _pool_members(real_template: TemplateLibrary) -> list[tuple[str, str]]:
    members: list[tuple[str, str]] = []
    for key in _EXPECTED_POOLS:
        for name in real_template.transition_pool(key):
            members.append((key, name))
    return members


class TestTransitionPoolsIntegrity:
    def test_all_five_pools_present_and_non_empty(self, real_template):
        for key in _EXPECTED_POOLS:
            pool = real_template.transition_pool(key)
            assert pool, f"{key} 缺失或为空"

    def test_pool_members_unique_within_pool(self, real_template):
        for key in _EXPECTED_POOLS:
            pool = real_template.transition_pool(key)
            assert len(pool) == len(set(pool)), f"{key} 存在重复成员"

    def test_every_pool_member_is_transition_with_19_digit_id(
        self, real_template, library_sections
    ):
        tr_rows, _fx_rows = library_sections
        for key, name in _pool_members(real_template):
            row = tr_rows.get(name)
            assert row is not None, (
                f"{key} 成员 {name!r} 不在 fx_resource_library.transitions 段"
                "(可能误配成了另一类型资源)"
            )
            assert _RESOURCE_ID_RE.match(str(row.get("resource_id") or "")), (
                f"{key} 成员 {name!r} resource_id 非 19 位数字"
            )

    def test_zoom_pool_uses_fixed_members(self, real_template):
        """核验报告 §5.2:推近推远(实为特效)不得出现在 pool_zoom。"""
        pool = real_template.transition_pool("pool_zoom")
        assert "推近 II" in pool
        assert "推远 II" in pool
        assert "中心切开" in pool
        for banned in _MUST_NOT_IN_ANY_POOL:
            for key in _EXPECTED_POOLS:
                assert banned not in real_template.transition_pool(key), (
                    f"{banned!r} 不得出现在 {key}"
                )

    def test_missing_pool_returns_empty_not_cross_pool(self, tmp_path):
        """池缺失/为空 → [] 调用方 degraded,不得静默换池(核验报告 §6.4)。"""
        lib = TemplateLibrary({"transition_pools": {"pool_gentle": []}})
        assert lib.transition_pool("pool_gentle") == []
        assert lib.transition_pool("pool_missing") == []
        assert lib.transition_pool("pool_bad") == []  # 非列表值


class TestVideoEffectWhitelistIntegrity:
    def test_whitelist_non_empty_and_rows_wellformed(self, real_template):
        rows = real_template.video_effect_candidates()
        assert len(rows) >= 4, "特效白名单过少(核验报告 §6.6 建议 4-6 个)"
        for row in rows:
            assert row.get("name"), "白名单行缺少 name"
            intensity = row.get("intensity")
            assert intensity is not None and 0.0 <= float(intensity) <= 1.0

    def test_whitelist_names_unique(self, real_template):
        names = [r["name"] for r in real_template.video_effect_candidates()]
        assert len(names) == len(set(names)), "特效白名单存在重复名"

    def test_every_candidate_is_video_effect_with_19_digit_id(
        self, real_template, library_sections
    ):
        _tr_rows, fx_rows = library_sections
        for row in real_template.video_effect_candidates():
            name = row["name"]
            lib_row = fx_rows.get(name)
            assert lib_row is not None, (
                f"特效候选 {name!r} 不在 fx_resource_library.video_effects 段"
                "(可能误配成了转场资源)"
            )
            assert _RESOURCE_ID_RE.match(str(lib_row.get("resource_id") or "")), (
                f"特效候选 {name!r} resource_id 非 19 位数字"
            )

    def test_transition_names_not_in_effect_whitelist(self, real_template):
        """核验报告 §5.3:金色光斑/金沙是转场,不得混入特效白名单。"""
        names = {r["name"] for r in real_template.video_effect_candidates()}
        for banned in _MUST_NOT_IN_EFFECT_WHITELIST:
            assert banned not in names, f"{banned!r} 是转场,不得进入特效白名单"

    def test_whitelist_no_full_frame_distortion_candidates(self, real_template):
        """禁令:模糊/抖动/频闪/故障/变焦类不得进入白名单(20261111 事故)。"""
        banned_marks = ("模糊", "抖动", "频闪", "故障", "变焦", "震动")
        for row in real_template.video_effect_candidates():
            for mark in banned_marks:
                assert mark not in row["name"], (
                    f"特效候选 {row['name']!r} 含禁用类别标记 {mark!r}"
                )


class TestSemanticMapPoolsWiring:
    def test_semantic_map_values_are_pool_keys_or_null(self, real_template):
        pool_keys = set(_EXPECTED_POOLS)
        sem_map = real_template.transition_semantic_map()
        assert sem_map, "transition_semantic_map 缺失"
        for semantic, target in sem_map.items():
            assert target is None or target in pool_keys, (
                f"语义 {semantic!r} → {target!r} 既非池 key 也非硬切(null)"
            )

    def test_new_vocab_covered(self, real_template):
        """recommend_transition 受控词表的每个值都有映射。"""
        sem_map = real_template.transition_semantic_map()
        for semantic in ("fade", "zoom", "slide", "whip", "creative", "cut"):
            assert semantic in sem_map, f"受控词表 {semantic!r} 未映射"

"""jy_common/hook_title.py 单测 — 开场标题三来源优先级与切句规则。

验收基准(第四期计划 §3.2 / 原计划 §7.10 验收 4):
第 1 条字幕 ``马六甲海上清真寺,日落时分海面金光闪烁。``(半角逗号)应切出
``马六甲海上清真寺``;全角逗号同样要认;上限 12 字;无标题回落 None。
"""

from __future__ import annotations

import pytest

from jy_common.hook_title import (
    HOOK_ANIMATION_CANDIDATES,
    HOOK_EFFECT_CANDIDATES,
    HOOK_TITLE_MAX_LEN,
    pick_hook_title,
)


class TestPickHookTitleFromState:
    def test_explicit_state_title_wins_over_cutting(self):
        state = {"hook_title": " 环球旅行日记 "}
        assert pick_hook_title(state, "马六甲海上清真寺,日落。") == "环球旅行日记"

    def test_explicit_title_not_truncated(self):
        long_title = "一个明显超过十二个字的显式开场标题示例"
        assert len(long_title) > HOOK_TITLE_MAX_LEN
        assert pick_hook_title({"hook_title": long_title}, "字幕") == long_title

    def test_blank_state_title_falls_through_to_first_text(self):
        state = {"hook_title": "   "}
        assert (
            pick_hook_title(state, "马六甲海上清真寺,日落。") == "马六甲海上清真寺"
        )

    def test_state_without_key_uses_first_text(self):
        assert pick_hook_title({}, "标题。正文") == "标题"


class TestPickHookTitleFromFirstText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            # 验收基准句:半角逗号(原计划 §7.10 明确要求两种逗号都认)
            ("马六甲海上清真寺,日落时分海面金光闪烁。", "马六甲海上清真寺"),
            ("马六甲海上清真寺,日落时分海面金光闪烁。", "马六甲海上清真寺"),
            ("马六甲风光、美食与人文。", "马六甲风光"),
            ("日落时分。海面金光。", "日落时分"),
            ("旅行日记;第一站马六甲", "旅行日记"),
            ("太美了!这座清真寺", "太美了"),
            ("你知道吗?这是马六甲", "你知道吗"),
        ],
    )
    def test_cut_at_first_punctuation(self, text, expected):
        assert pick_hook_title({}, text) == expected

    def test_no_punctuation_within_limit_kept_whole(self):
        text = "马六甲海上清真寺日落"  # 10 字,无标点
        assert pick_hook_title({}, text) == text

    def test_no_punctuation_truncated_to_twelve(self):
        text = "一二三四五六七八九十甲乙丙丁戊己庚辛"
        assert len(text) > HOOK_TITLE_MAX_LEN
        assert pick_hook_title({}, text) == text[:HOOK_TITLE_MAX_LEN]

    def test_leading_punctuation_yields_none(self):
        # 文本以标点开头:切完为空 → 无可用标题
        assert pick_hook_title({}, ",开头就是逗号") is None

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_empty_first_text_returns_none(self, bad):
        assert pick_hook_title({}, bad) is None


class TestFreeResourceCandidates:
    """内置免费资源行:来源 vendor metadata,必须保持 is_vip=False 与 19 位 ID。"""

    def test_effect_candidates_are_free_with_valid_ids(self):
        assert HOOK_EFFECT_CANDIDATES[0]["name"] == "CCD闪光"
        assert HOOK_EFFECT_CANDIDATES[1]["name"] == "闪白"
        for row in HOOK_EFFECT_CANDIDATES:
            assert row["is_vip"] is False
            assert len(row["resource_id"]) == 19 and row["resource_id"].isdigit()

    def test_animation_candidates_are_free_with_valid_ids(self):
        assert [r["name"] for r in HOOK_ANIMATION_CANDIDATES] == ["冲屏位移", "放大", "渐显"]
        for row in HOOK_ANIMATION_CANDIDATES:
            assert row["is_vip"] is False
            assert len(row["resource_id"]) == 19 and row["resource_id"].isdigit()
            assert row["duration_s"] > 0

"""jy_common/user_directives 单测(第八期·输入剪辑要求逐条生效,阶段1)。

用例矩阵对齐设计文档 §8.1(指令解析器)+ §6.1 解析规则:
行首标记、多行值、空值未提供、重复字段 fail-closed、未知标记不解释、
无标记不触发任何新增动作(修订版 P1-06)、否定词优先、标题规范化截断留痕。
"""
from __future__ import annotations

import jy_common.user_directives as ud

TEMPLATE_FULL = (
    "【主题】马来西亚吉隆坡武吉马六甲海上清真寺旅行Vlog\n"
    "【开场】开头使用强动势甩镜风格转场，配大字标题\n"
    "【检索】马六甲海上清真寺旅游攻略、开放时间和拍摄打卡信息\n"
    "【标题】马六甲海上清真寺之旅"
)


# ---------- parse_user_directives ----------


def test_parse_all_fields() -> None:
    d = ud.parse_user_directives(TEMPLATE_FULL)
    assert d["theme"] == "马来西亚吉隆坡武吉马六甲海上清真寺旅行Vlog"
    assert d["opening"] == "开头使用强动势甩镜风格转场，配大字标题"
    assert d["search"] == "马六甲海上清真寺旅游攻略、开放时间和拍摄打卡信息"
    assert d["title"] == "马六甲海上清真寺之旅"
    assert d["warnings"] == []


def test_parse_partial_only_search() -> None:
    d = ud.parse_user_directives("【检索】清真寺打卡机位")
    assert d["search"] == "清真寺打卡机位"
    assert d["theme"] is None and d["opening"] is None and d["title"] is None


def test_parse_empty_field_is_not_provided() -> None:
    # 标记存在但值为空 = 未提供(回退链不受空串污染)
    d = ud.parse_user_directives("【标题】\n【检索】攻略")
    assert d["title"] is None
    assert d["search"] == "攻略"
    # 值全是空白同样视为未提供
    d2 = ud.parse_user_directives("【标题】   ")
    assert d2["title"] is None


def test_parse_multiline_value() -> None:
    d = ud.parse_user_directives("【检索】海上清真寺攻略\n开放时间\n日落时段")
    assert d["search"] == "海上清真寺攻略\n开放时间\n日落时段"


def test_parse_duplicate_field_fails_closed() -> None:
    d = ud.parse_user_directives("【标题】第一个标题\n【检索】攻略\n【标题】第二个标题")
    assert d["title"] is None  # 不静默取第一条/最后一条
    assert d["search"] == "攻略"  # 无冲突字段不受影响
    assert d["warnings"] == ["duplicate_directive:title"]


def test_parse_duplicate_warning_recorded_once() -> None:
    d = ud.parse_user_directives("【标题】A\n【标题】B\n【标题】C")
    assert d["warnings"] == ["duplicate_directive:title"]
    assert d["title"] is None


def test_parse_no_markers_returns_empty() -> None:
    # 修订版 P1-06:无标记 = 解析结果为空,不做自由文本兜底
    raw = "#1、附件上传的这些视频是关于：马来西亚海上清真寺的旅游素材。#2、开头 3 秒用最炸的转场 + 大字标题。"
    d = ud.parse_user_directives(raw)
    assert d["theme"] is None and d["opening"] is None
    assert d["search"] is None and d["title"] is None
    assert d["warnings"] == []
    assert ud.opening_boost_requested(d) is False


def test_parse_unknown_marker_stays_plain_text() -> None:
    # 未知【xx】不是标记:不终止当前字段、不解释为新功能
    d = ud.parse_user_directives("【检索】海上清真寺攻略\n【备注】周五闭馆\n记得看日落")
    assert d["search"] == "海上清真寺攻略\n【备注】周五闭馆\n记得看日落"
    assert d["warnings"] == []


def test_parse_marker_must_be_line_start() -> None:
    # 行中出现的【开场】不是标记(P2-01:只认行首)
    d = ud.parse_user_directives("【主题】城市漫步【开场】开头用炸的转场")
    assert d["theme"] == "城市漫步【开场】开头用炸的转场"
    assert d["opening"] is None
    assert ud.opening_boost_requested(d) is False


def test_parse_marker_with_leading_whitespace() -> None:
    d = ud.parse_user_directives("  【标题】马六甲之旅")
    assert d["title"] == "马六甲之旅"


def test_parse_empty_and_non_string_input() -> None:
    assert ud.parse_user_directives("")["theme"] is None
    assert ud.parse_user_directives("   \n  ")["warnings"] == []
    assert ud.parse_user_directives(None)["title"] is None  # type: ignore[arg-type]


def test_parse_is_pure_and_deterministic() -> None:
    first = ud.parse_user_directives(TEMPLATE_FULL)
    second = ud.parse_user_directives(TEMPLATE_FULL)
    assert first == second


# ---------- opening_boost_requested ----------


def test_boost_positive_marker() -> None:
    d = ud.parse_user_directives("【开场】开头使用最炸的转场，配大字标题")
    assert ud.opening_boost_requested(d) is True


def test_boost_positive_whip_wording() -> None:
    d = ud.parse_user_directives("【开场】开头使用强动势甩镜风格转场")
    assert ud.opening_boost_requested(d) is True


def test_boost_negation_wins() -> None:
    # §8.1:否定词优先于正向词
    d = ud.parse_user_directives("【开场】开头不要使用强转场")
    assert ud.opening_boost_requested(d) is False


def test_boost_requires_scope_transition_and_intensity() -> None:
    cases = {
        "无范围词": "转场要炸",
        "无转场词": "开头配大字标题，视觉要炸",
        "无强度词": "开头加一个转场",
        "范围+转场但强度不足": "开头转场柔和一点",
    }
    for _name, opening in cases.items():
        assert ud.opening_boost_requested({"opening": opening}) is False


def test_boost_ignores_other_fields() -> None:
    # 【主题】/【标题】里的「转场」「炸」不得误触开场加强
    d = ud.parse_user_directives("【主题】研究转场特效的爆炸感\n【标题】转场旅行记")
    assert ud.opening_boost_requested(d) is False


def test_boost_empty_or_missing_directives() -> None:
    assert ud.opening_boost_requested({}) is False
    assert ud.opening_boost_requested(None) is False  # type: ignore[arg-type]
    assert ud.opening_boost_requested({"opening": None}) is False
    assert ud.opening_boost_requested({"opening": ""}) is False


# ---------- normalize_hook_title / resolve_template_title ----------


def test_resolve_template_title_basic() -> None:
    d = ud.parse_user_directives("【标题】马六甲海上清真寺之旅")
    assert ud.resolve_template_title(d) == "马六甲海上清真寺之旅"


def test_resolve_template_title_truncates_to_12_chars() -> None:
    d = ud.parse_user_directives("【标题】马六甲海峡的海上清真寺日落航行记录")
    title = ud.resolve_template_title(d)
    assert title is not None
    assert len(title) == 12


def test_resolve_template_title_none_when_missing_or_empty() -> None:
    assert ud.resolve_template_title({}) is None
    assert ud.resolve_template_title({"title": None}) is None
    assert ud.resolve_template_title({"title": "  "}) is None
    assert ud.resolve_template_title(ud.parse_user_directives("【检索】攻略")) is None


def test_normalize_hook_title_collapses_whitespace() -> None:
    title, truncated = ud.normalize_hook_title("马六甲\n 海上\t清真寺之旅")
    assert title == "马六甲 海上 清真寺之旅"
    assert truncated is False


def test_normalize_hook_title_truncation_flag() -> None:
    title, truncated = ud.normalize_hook_title("一二三四五六七八九十一二三四五六")  # 16 字
    assert len(title) == 12
    assert truncated is True
    short, flag = ud.normalize_hook_title("正好十二字标题哦")
    assert short == "正好十二字标题哦" and flag is False


def test_normalize_hook_title_empty_and_non_string() -> None:
    assert ud.normalize_hook_title("") == ("", False)
    assert ud.normalize_hook_title("   ") == ("", False)
    assert ud.normalize_hook_title(None) == ("", False)  # type: ignore[arg-type]
    assert ud.normalize_hook_title(123) == ("", False)  # type: ignore[arg-type]

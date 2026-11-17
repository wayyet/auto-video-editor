"""字幕折行单测(2026-11-18):单位标定 + 容量钉住 + 断行规则 + 幂等。

标定口径与规则说明见 ``jy_common/subtitle_wrap.py`` docstring;本文件把
"size → 每行单位数"与真机两口径(zh 6.25 → 21 / en 5.0 → 26)钉死,防止
无意改动让字幕重新超宽。小预算用例用 size=60 造 budget=2,便于手算断点。

2026-11-18 第二轮补充(设计审查 P2-01/P2-02/P2-04):断点空白保留、
布局质量告警、标定样本表(估算值钉值,实测值 TODO 待真机)。
"""

from __future__ import annotations

import pytest

from jy_common.subtitle_wrap import (
    LINE_WIDTH_RATIO,
    PX_PER_SIZE_UNIT,
    char_units,
    estimate_line_px,
    layout_warnings,
    line_units,
    max_line_units,
    wrap_subtitle_text,
)

# 真机 text-zh-3 原文(2026-11-18 截图单行超宽被两端裁剪的那条,39 字符,
# 其中半角逗号 2 个/全角顿号句号 3 个 → 38.1 全角单位)
ZH39 = "我沿着栈桥往里走,白色拱廊、灰色立柱、绿色花纹地毯和金蓝穹顶,一样样收进眼里。"

# 真机 text-en-4 原文(157 字符,最长英文 cue)
EN157 = (
    "The stained-glass windows break the sunlight into pieces, and the wall "
    "clock, second-floor railing, and black-and-gold wooden pulpit are all "
    "clearly visible."
)


class TestUnitsAndCapacity:
    def test_constants_pinned(self) -> None:
        assert PX_PER_SIZE_UNIT == 6.4, "真机截图反推标定(size 6.25 → 40px/全角字)"
        assert LINE_WIDTH_RATIO == 0.78, "低于剪映 line_max_width 0.82 的安全余量"

    def test_char_units(self) -> None:
        assert char_units("我") == 1.0
        assert char_units("，") == 1.0
        assert char_units("、") == 1.0
        assert char_units("…") == 1.0, "低于 CJK 区但按全角渲染的标点"
        assert char_units("A") == 0.67, "大写拉丁单独抬高"
        assert char_units("a") == 0.55
        assert char_units(" ") == 0.55

    def test_line_units_mixed(self) -> None:
        # 真机 zh cue1「别眨眼!」:3 全角 + 半角 !
        assert line_units("别眨眼!") == 3.0 + 0.55

    def test_capacity_pinned_real_sizes(self) -> None:
        assert max_line_units(6.25, 1080) == 21, "zh 真机口径"
        assert max_line_units(5.0, 1080) == 26, "en/默认样式口径"

    def test_capacity_zero_or_negative_size(self) -> None:
        assert max_line_units(0, 1080) == 0
        assert max_line_units(-1, 1080) == 0

    def test_estimate_line_px(self) -> None:
        assert estimate_line_px("我我", 6.25) == 2 * 6.4 * 6.25


class TestWrapRules:
    def test_short_unchanged(self) -> None:
        assert wrap_subtitle_text("别眨眼!", size=6.25, canvas_width=1080) == "别眨眼!"

    def test_empty_unchanged(self) -> None:
        assert wrap_subtitle_text("", size=6.25, canvas_width=1080) == ""

    def test_invalid_size_unchanged(self) -> None:
        assert wrap_subtitle_text(ZH39, size=0, canvas_width=1080) == ZH39

    def test_zh39_real_cue_two_lines(self) -> None:
        out = wrap_subtitle_text(ZH39, size=6.25, canvas_width=1080)
        lines = out.split("\n")
        assert len(lines) == 2, f"真机 39 字长句应折 2 行,实得 {len(lines)} 行"
        assert all(line_units(l) <= 21 for l in lines)
        assert out.replace("\n", "") == ZH39, "折行不丢字符"

    def test_prefer_break_after_punct(self) -> None:
        # 真机 zh cue2:最大填充会把「直接」腰斩;标点优先 → 断在逗号之后
        out = wrap_subtitle_text(
            "海面、邮轮、礁石一闪,马六甲的海上清真寺直接撞进镜头!",
            size=6.25, canvas_width=1080,
        )
        assert out.split("\n")[0].endswith("一闪,"), "优先断在标点之后(语义边界)"
        assert out.replace("\n", "") == (
            "海面、邮轮、礁石一闪,马六甲的海上清真寺直接撞进镜头!"
        )

    def test_zh_real_cue_idempotent(self) -> None:
        once = wrap_subtitle_text(ZH39, size=6.25, canvas_width=1080)
        assert wrap_subtitle_text(once, size=6.25, canvas_width=1080) == once

    def test_en_real_cue_word_wrap(self) -> None:
        out = wrap_subtitle_text(EN157, size=5.0, canvas_width=1080)
        lines = out.split("\n")
        assert len(lines) == 4, (
            f"纯英文按空格最大填充(不因逗号优先断行而碎行),应 4 行,实得 {len(lines)} 行"
        )
        for line in lines:
            assert line_units(line) <= 26
            assert not line.startswith(" "), "无行首空格"
            assert not line.endswith(" "), "无行尾空格"
        assert " ".join(lines) == EN157, "词边界折行可经空格重组无损还原"

    def test_en_real_cue_idempotent(self) -> None:
        once = wrap_subtitle_text(EN157, size=5.0, canvas_width=1080)
        assert wrap_subtitle_text(once, size=5.0, canvas_width=1080) == once

    def test_no_line_head_closing_punct(self) -> None:
        # budget=2(size 60):naive 切点正落在全角逗号上 → 回溯让逗号并入上一行
        out = wrap_subtitle_text("我你，他", size=60, canvas_width=1080)
        assert out == "我\n你，\n他"
        for line in out.split("\n"):
            assert line[0] not in "，。、；：！？", "行首禁闭合标点"

    def test_no_line_tail_open_punct(self) -> None:
        # 行尾禁则:开口括号不得悬在行尾 → 断点甩到其前
        out = wrap_subtitle_text("我（你他", size=60, canvas_width=1080)
        assert out == "我\n（你\n他"
        for line in out.split("\n"):
            assert line[-1] not in "（【《「『〈“‘([{", "行尾禁开口标点"

    def test_hard_cut_long_word(self) -> None:
        # 无空格无标点的超长串:无合法断点 → 硬切,每行仍不超预算
        out = wrap_subtitle_text("a" * 10, size=60, canvas_width=1080)
        lines = out.split("\n")
        assert all(line_units(l) <= 2 for l in lines)
        assert out.replace("\n", "") == "a" * 10, "硬切不丢字符"

    def test_respects_existing_newlines_and_never_merges(self) -> None:
        # 已有 \n 逐行独立处理,人工换行永不合并
        out = wrap_subtitle_text("我\n我你他", size=60, canvas_width=1080)
        assert out == "我\n我你\n他"

    def test_trailing_newline_preserved(self) -> None:
        assert wrap_subtitle_text("我你\n", size=60, canvas_width=1080) == "我你\n"


class TestWhitespacePreservation:
    """断点空白保留(20261118 审查 P2-01):恰好吸收一个空格,其余字符不丢。"""

    def test_consecutive_spaces_keep_extra(self) -> None:
        # budget=2(size 60):"A␣␣B" 断在两个空格之间 → 行尾吸收一个,另一个保留
        out = wrap_subtitle_text("A  B", size=60, canvas_width=1080)
        assert out == "A \nB", "连续空格只消费一个,剩余保留在行尾"
        assert out.replace("\n", "") == "A B", "原文 4 字符 - 1 分隔空格 = 3 字符"

    def test_tab_never_consumed(self) -> None:
        # Tab 不是断行分隔符:断点落在 Tab 之后(前一字符是字母数字不可断 →
        # 回溯到 Tab 后),Tab 原样保留在行尾
        out = wrap_subtitle_text("A\tB\tC\tD\tE", size=60, canvas_width=1080)
        assert out.replace("\n", "") == "A\tB\tC\tD\tE", "Tab 一律不得丢失"
        for line in out.split("\n"):
            assert line_units(line) <= 2

    def test_break_space_leading_side(self) -> None:
        # 断点右侧有空格时从行首吸收:"我␣␣你" → 行1"我",行2"␣你"(保留一个行首空格)
        out = wrap_subtitle_text("我  你", size=60, canvas_width=1080)
        assert out == "我\n 你"
        assert out.replace("\n", "") == "我 你"

    def test_no_space_hard_cut_loses_nothing(self) -> None:
        # 硬切(两侧无空格)不吞任何字符
        out = wrap_subtitle_text("abcde", size=60, canvas_width=1080)
        assert out.replace("\n", "") == "abcde"

    def test_space_preservation_idempotent(self) -> None:
        once = wrap_subtitle_text("A  B", size=60, canvas_width=1080)
        assert wrap_subtitle_text(once, size=60, canvas_width=1080) == once
        once_en = wrap_subtitle_text(EN157 + "  tail", size=5.0, canvas_width=1080)
        assert wrap_subtitle_text(once_en, size=5.0, canvas_width=1080) == once_en

    def test_double_spaces_two_breaks_exact(self) -> None:
        # 两组双空格、两个断点(budget=2):每个断点恰好消费一个空格,
        # 双空格的另一个分别保留在行首/行尾 —— 手算钉死,防回归
        out = wrap_subtitle_text("ab  cd  ef", size=60, canvas_width=1080)
        assert out == "ab\n cd\n ef", "每断点吸收一个空格;剩余空格各归其位"
        assert out.replace("\n", "") == "ab cd ef", "10 字符 - 2 断点空格 = 8 字符,无丢失"


class TestLayoutWarnings:
    """布局质量告警(20261118 审查 P2-04):长文本明确告警,普通字幕零误报。"""

    def test_en157_wrapped_warns_line_count(self) -> None:
        out = wrap_subtitle_text(EN157, size=5.0, canvas_width=1080)
        warns = layout_warnings(out, size=5.0, canvas_width=1080)
        assert any(f"行数 {len(out.splitlines())}" in w for w in warns), "4 行英文 cue 必须告警"

    def test_cps_warning_when_too_fast(self) -> None:
        # 39 字 / 3 秒 = 13 字/秒 > 12 → 告警
        warns = layout_warnings(ZH39, size=6.25, canvas_width=1080, duration_us=3_000_000)
        assert any("阅读速度" in w for w in warns)

    def test_no_warning_for_normal_cue(self) -> None:
        warns = layout_warnings(
            "别眨眼!", size=6.25, canvas_width=1080, duration_us=2_000_000
        )
        assert warns == [], "普通短字幕零误报"

    def test_no_warning_without_duration(self) -> None:
        # 折行后的文本(2 行、每行 ≤ 预算)→ 零告警;duration 缺省则无 CPS 检查
        wrapped = wrap_subtitle_text(ZH39, size=6.25, canvas_width=1080)
        assert layout_warnings(wrapped, size=6.25, canvas_width=1080) == []

    def test_vertical_occupancy_warning(self) -> None:
        # 5 行 × size 12:块高 5×6.4×12×1.3 ≈ 499px > 1920×0.25 → 告警
        text = "\n".join(["我"] * 5)
        warns = layout_warnings(text, size=12.0, canvas_width=1080, canvas_height=1920)
        assert any("垂直占位" in w for w in warns)

    def test_overwide_line_defensive_check(self) -> None:
        # 防御性:未折行的超宽行(如外来素材)给出行超预算告警
        warns = layout_warnings("超" * 60, size=5.0, canvas_width=1080)
        assert any("行超预算" in w for w in warns)


class TestCalibrationSamples:
    """标定样本表(20261118 审查 P2-02):估算值钉值。

    估算模型:px = line_units(text) × PX_PER_SIZE_UNIT × size(±15% 不确定度,
    单一真机样本 size 6.25 → 40px/全角字 反推)。**剪映实测值列为 TODO**,
    真机验收(阶段 P3)时逐样本回填;若实测低估误差 > 10%,收紧
    PX_PER_SIZE_UNIT 或 LINE_WIDTH_RATIO 并重跑本表。
    """

    def test_estimated_values_pinned(self) -> None:
        # 每组样本的估算 px(手算钉值);模型常量一变,这里立刻红
        assert estimate_line_px(ZH39, 6.25) == pytest.approx(38.1 * 6.4 * 6.25)  # 1524.0
        assert estimate_line_px("THE QUICK BROWN FOX", 5.0) == pytest.approx(12.37 * 6.4 * 5.0)
        assert estimate_line_px("2026-11-18 10:30:00", 5.0) == pytest.approx(10.45 * 6.4 * 5.0)
        assert estimate_line_px("马六甲 Malacca 海峡", 6.25) == pytest.approx(10.07 * 6.4 * 6.25)
        assert estimate_line_px("Stained-glass", 5.0) == pytest.approx(7.27 * 6.4 * 5.0)

    def test_emoji_counted_fullwidth(self) -> None:
        # emoji(≥U+2E80)按 1.0 全角单位计 —— 真机实际渲染可能更宽,
        # 属已知低估方向(保守折行靠 LINE_WIDTH_RATIO 三重余量兜住)
        assert char_units("👀") == 1.0
        assert estimate_line_px("别眨眼👀", 6.25) == pytest.approx(4 * 6.4 * 6.25)

    def test_uppercase_uses_wider_unit(self) -> None:
        # 全大写行不低估:每个大写 0.67 > 平均 0.55
        assert char_units("T") == 0.67
        assert line_units("THE") == pytest.approx(3 * 0.67)

    # TODO(真机 P3):下表为剪映实测列,验收时逐行回填:
    # | 样本                        | size  | 估算 px | 剪映实测 px | 误差  |
    # |-----------------------------|-------|---------|-------------|-------|
    # | ZH39(全角句)              | 6.25  | 1524    | TODO        | TODO  |
    # | THE QUICK BROWN FOX(全大写)| 5.0   | 396     | TODO        | TODO  |
    # | 2026-11-18 10:30:00(数字)  | 5.0   | 334     | TODO        | TODO  |
    # | 马六甲 Malacca 海峡(混排)  | 6.25  | 403     | TODO        | TODO  |
    # | Stained-glass(连字符词)    | 5.0   | 233     | TODO        | TODO  |
    # | 别眨眼👀(emoji)            | 6.25  | 256     | TODO        | TODO  |

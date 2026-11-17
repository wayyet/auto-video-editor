"""字幕折行单测(2026-11-18):单位标定 + 容量钉住 + 断行规则 + 幂等。

标定口径与规则说明见 ``jy_common/subtitle_wrap.py`` docstring;本文件把
"size → 每行单位数"与真机两口径(zh 6.25 → 21 / en 5.0 → 26)钉死,防止
无意改动让字幕重新超宽。小预算用例用 size=60 造 budget=2,便于手算断点。
"""

from __future__ import annotations

from jy_common.subtitle_wrap import (
    LINE_WIDTH_RATIO,
    PX_PER_SIZE_UNIT,
    char_units,
    estimate_line_px,
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

"""字幕文本折行 —— 按画布宽与字号确定性内嵌 ``\\n``,防止单行超宽被画布裁剪。

2026-11-18 修复背景(AutoVideo_console-31a8a8e9edd2 真机截图实证):竖屏
1080×1920 画布上,39 字长句单行排布 ≈1560px 被两端裁剪;英文分支最长 cue
157 字符 ≈2760px(2.6 倍画布宽)。根因有三层:cue 无字数上限(node_08 只按
句末标点切)/ 写入内容无 ``\\n`` / 素材 ``line_max_width=0.82`` 键存在但
``force_apply_line_max_width=False`` —— 剪映不强制折行,超宽直接裁。本模块
从源头消掉第二层:字幕构造唯一咽喉点(``nodes/_subtitle_texts.
make_text_material``)把渲染文本折成多行(剪映渲染 ``\\n`` 为换行,
``alignment=1`` 逐行居中)。

标定口径:剪映 ``size`` 与像素之间无公开换算公式,本模块用真机截图反推的
线性近似 —— size 6.25 的全角字实测 ≈40px → ``PX_PER_SIZE_UNIT = 6.4``。
系数有 ±15% 不确定度,``LINE_WIDTH_RATIO``(0.78 < 剪映自身 line_max_width
的 0.82)已留余量;真机若仍见超宽,收紧这一个常量即可。

只负责"一段文本 → 折行后的文本",不碰 I/O、不碰 state、不改样式:
颜色/描边/字号/时间轴全部不动,短文本原样返回(幂等)。
"""

from __future__ import annotations

#: 全角字渲染宽度(px)= size × 该系数。标定:真机截图 size 6.25 → 40px/字。
PX_PER_SIZE_UNIT = 6.4

#: 可用行宽占画布宽的比例。0.78×1080 ≈ 842px,低于剪映自身 line_max_width
#: 的 0.82(885.6px)与画布 1080,三重余量抵住标定误差与加粗/描边加宽。
LINE_WIDTH_RATIO = 0.78

#: 半角字符(latin/数字/半角标点/空格)的平均渲染宽度(全角=1.0)
_HALF_WIDTH = 0.55
#: 大写拉丁略宽于平均,单独抬高(全大写行不至于低估)
_UPPER_WIDTH = 0.67

#: 低于 CJK 区(U+2E80)但按全角渲染的常见标点(省略号/破折号/弯引号)
_EXTRA_FULLWIDTH = set("…—―‘’“”")

#: 行首禁则:闭合类标点不得出现在行首(回溯断点时并入上一行)
_NO_LINE_HEAD_CHARS = set("，。、；：！？…·）】》」』〉”’%,.!?;:>)]}")

#: 行尾禁则:开口类标点不得悬在行尾(回溯断点时甩给下一行)
_NO_LINE_TAIL_CHARS = set("（【《「『〈“‘([{")

# ---------------------------------------------------------------------------
# 布局质量告警阈值(设计审查 20261118 P2-04:只告警,不自动改时间轴)
# ---------------------------------------------------------------------------

#: 单条字幕折行后超过该行数 → 告警(阅读吃力/易遮挡画面)
MAX_LINES_WARN = 3
#: 字幕块垂直占用超过画布高的该比例 → 告警(底部字幕遮挡防护)
SAFE_HEIGHT_RATIO = 0.25
#: 平均阅读速度上限(字符/秒,超出 = 观众大概率读不完)
MAX_CPS_WARN = 12.0

__all__ = [
    "LINE_WIDTH_RATIO",
    "MAX_CPS_WARN",
    "MAX_LINES_WARN",
    "PX_PER_SIZE_UNIT",
    "SAFE_HEIGHT_RATIO",
    "char_units",
    "estimate_line_px",
    "layout_warnings",
    "line_units",
    "max_line_units",
    "wrap_subtitle_text",
]


def char_units(ch: str) -> float:
    """单字符渲染宽度(全角字 = 1.0 单位)。"""
    if ord(ch) >= 0x2E80 or ch in _EXTRA_FULLWIDTH:
        return 1.0
    if ch.isupper():
        return _UPPER_WIDTH
    return _HALF_WIDTH


def line_units(text: str) -> float:
    """一行文本的总宽度(全角单位)。"""
    return sum(char_units(ch) for ch in text)


def max_line_units(size: float, canvas_width: int = 1080) -> int:
    """给定字号与画布宽,单行能放下的全角单位数(向下取整)。

    size 6.25(真机 zh)→ 21 单位;size 5.0(默认/en)→ 26 单位(≈47 个
    小写英文字符)。size ≤ 0 或算出 0 → 返回 0(调用方视为"不折行")。
    """
    per_unit = PX_PER_SIZE_UNIT * float(size)
    if per_unit <= 0:
        return 0
    return int(float(canvas_width) * LINE_WIDTH_RATIO / per_unit)


def estimate_line_px(text: str, size: float) -> float:
    """估算一行文本的渲染宽度(px),修复脚本与自校验共用。"""
    return line_units(text) * PX_PER_SIZE_UNIT * float(size)


def _can_break_before(s: str, j: int) -> bool:
    """位置 j 之前是否允许断行(行首/行尾禁则 + 词完整性)。"""
    if j <= 0 or j >= len(s):
        return False
    if s[j] in _NO_LINE_HEAD_CHARS:
        return False  # 闭合标点不能落到行首
    prev = s[j - 1]
    if prev in _NO_LINE_TAIL_CHARS:
        return False  # 开口标点不能悬在行尾
    if char_units(prev) >= 1.0:
        return True  # 全角(CJK/全角标点)之后任意断
    if prev == "-" or prev == "–":
        return False  # 连字符词(stained-glass)不拆,词内硬切兜底
    if prev.isalnum():
        return False  # 半角字母/数字之间不拆词(保词完整)
    return True  # 空格/半角标点之后可断


def _find_cut(s: str, budget: float, prefer_punct: bool) -> int:
    """找第一个超预算位置的最佳断点,优先级:标点之后 > 最近合法断点 > 硬切。

    断在标点后(，、,;)是语义边界,读起来最自然 —— 比如宁可「…礁石一闪, /
    马六甲的…」也不要「…清真寺直 / 接撞进镜头!」这种词组腰斩。仅当
    ``prefer_punct``(文本含全角字符,即中文)时启用;英文按空格最大填充,
    否则逗号断点会把长句切碎。标点断点不存在时退回最大填充的合法断点;
    连合法断点都没有(超长无空格串)→ 硬切。
    """
    acc = 0.0
    for i, ch in enumerate(s):
        if acc + char_units(ch) > budget and i > 0:
            fallback = 0
            for j in range(i, 0, -1):
                if not _can_break_before(s, j):
                    continue
                if not s[:j].strip():
                    continue  # 断点左侧全是空白 → 吞空格却不产生新行(幻影断行),跳过
                if prefer_punct and s[j - 1] in _NO_LINE_HEAD_CHARS:
                    return j  # 标点后断行(语义边界,最优先)
                if fallback == 0:
                    fallback = j  # 最近合法断点(次优)
            return fallback if fallback else i  # 无合法断点 → 硬切
        acc += char_units(ch)
    return len(s)


def _split_at_break(s: str, cut: int) -> tuple[str, str]:
    """在断点处切分,恰好吸收**一个**半角空格作为换行分隔符(20261118 P2-01)。

    其余空格(连续空格的剩余部分)与 Tab 一律保留,不得丢失:
    - 断点左侧以空格结尾 → 行尾吸收一个,其余行尾空格保留在行尾;
    - 否则断点右侧以空格开头 → 行首吸收一个,其余行首空格保留在行首;
    - 两侧都无空格(硬切 / 标点断点)→ 原样切分,不吞任何字符。
    """
    seg, nxt = s[:cut], s[cut:]
    if seg.endswith(" "):
        seg = seg[:-1]
    elif nxt.startswith(" "):
        nxt = nxt[1:]
    return seg, nxt


def _wrap_line(line: str, budget: float) -> list[str]:
    """把一行(不含 \\n)按 budget 贪心折成多行;不超宽原样返回单行。"""
    if line_units(line) <= budget:
        return [line]
    # 标点优先只对含全角字符(CJK/全角标点)的文本启用:中文断在标点后最自然;
    # 纯半角(英文)按空格最大填充 —— 英文字幕断在逗号后会把长句切碎。
    prefer_punct = any(char_units(ch) >= 1.0 for ch in line)
    out: list[str] = []
    rest = line
    while line_units(rest) > budget:
        cut = _find_cut(rest, budget, prefer_punct)
        if cut <= 0:
            break  # 防御:断点无法推进(理论不可达),避免死循环
        seg, rest = _split_at_break(rest, cut)
        if seg:
            out.append(seg)  # 防御:吸收分隔空格后可能为空串,不产生空行
    if rest:
        out.append(rest)
    return out


def wrap_subtitle_text(text: str, *, size: float, canvas_width: int = 1080) -> str:
    """字幕渲染文本 → 按画布宽×字号折行后的文本(超宽处内嵌 ``\\n``)。

    规则:
    - 尊重已有 ``\\n``:逐行处理,**永不合并**(人工换行/已折行文本幂等);
    - 贪心累积超预算时回溯最近合法断点(latin 在空格/半角标点后断,CJK
      任意字间);含全角字符的文本优先断在标点之后(语义边界),纯半角
      (英文)按空格最大填充;行首禁闭合标点、行尾禁开口标点;
      无断点可回溯则硬切;
    - 每行 ≤ ``max_line_units(size, canvas_width)`` 单位;
    - 短文本/size ≤ 0/空文本 → 原样返回。

    只折**渲染口径**;工作流私有键(text_zh/text_en)由调用方传原文,
    16a 翻译指纹与 SRT 语义不受影响。
    """
    if not text:
        return text
    budget = max_line_units(size, canvas_width)
    if budget <= 0:
        return text
    lines: list[str] = []
    for seg in str(text).split("\n"):
        lines.extend(_wrap_line(seg, budget))
    return "\n".join(lines)


def layout_warnings(
    text: str,
    *,
    size: float,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    duration_us: int | None = None,
) -> list[str]:
    """折行后字幕的布局质量告警(只读、只告警,不修改内容/时间轴;P2-04)。

    检查项(设计审查 20261118 §6 P2-04):
    - 行数:折行后行数 > ``MAX_LINES_WARN``(长文本折成 4+ 行,阅读吃力/遮挡);
    - 垂直占位:行数 × 行高估算 超出画布高 ``SAFE_HEIGHT_RATIO``;
    - 每行估宽:超出行预算(防御性检查 —— wrap 后理论上不会发生);
    - 阅读速度:``duration_us`` 给出时按 字符/秒(CPS)检查,> ``MAX_CPS_WARN``
      告警(折行不重排时间轴,超长 cue 仍可能读不完,提示人工关注)。

    Returns:
        可直接打印的告警文案列表;无告警返回空列表(普通字幕零误报)。
    """
    warnings: list[str] = []
    if not text or not isinstance(text, str) or size <= 0:
        return warnings
    lines = text.split("\n")
    if len(lines) > MAX_LINES_WARN:
        warnings.append(
            f"行数 {len(lines)} > {MAX_LINES_WARN}:阅读吃力/易遮挡画面"
        )
    # 行高粗估:字号×PX_PER_SIZE_UNIT×1.3(含行距与行间呼吸空间)
    block_px = len(lines) * PX_PER_SIZE_UNIT * size * 1.3
    if block_px > canvas_height * SAFE_HEIGHT_RATIO:
        warnings.append(
            f"垂直占位 {block_px:.0f}px ≈ 画布高 {block_px / canvas_height:.0%}"
            f"(阈值 {SAFE_HEIGHT_RATIO:.0%})"
        )
    budget = max_line_units(size, canvas_width)
    if budget > 0:
        for line in lines:
            if line_units(line) > budget:
                warnings.append(
                    f"行超预算 {line_units(line):.1f} > {budget} 单位: {line[:20]}…"
                )
                break
    if duration_us is not None and duration_us > 0:
        seconds = duration_us / 1_000_000
        cps = len(text.replace("\n", "")) / seconds
        if cps > MAX_CPS_WARN:
            warnings.append(
                f"阅读速度 {cps:.1f} 字/秒 > {MAX_CPS_WARN}(时长 {seconds:.1f}s)"
            )
    return warnings

"""节点 8 字幕注入单测 — ASR Mock + 样式字段完整性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.asr_client import set_default_client
from nodes.node_08_add_subtitles import add_subtitles
from video_edit_capabilities import subtitle_style as sty


class _FixedASRClient:
    """返回固定字幕段的 ASR mock。"""

    def __init__(self, segments: list[dict]) -> None:
        self._segments = segments

    def transcribe(self, video_path: str) -> list[dict]:
        return list(self._segments)


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """最小可用草稿 — 含 video track,无 texts。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{"type": "video", "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": 10_000_000}}]}],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path, video_path: str = "C:/v.mp4") -> dict:
    return {
        "draft_path": str(draft_path),
        "video_input_path": video_path,
        "status_log": [],
        "error_log": [],
    }


def test_add_subtitles_basic(tmp_draft: Path) -> None:
    """ASR 返回 3 段 → materials.texts 应含 3 条,样式字段完整。"""
    set_default_client(_FixedASRClient([
        {"text": "你好", "start_s": 0.0, "end_s": 1.5},
        {"text": "世界", "start_s": 1.5, "end_s": 3.0},
        {"text": "再见", "start_s": 3.0, "end_s": 4.5},
    ]))

    out = add_subtitles(_state(tmp_draft))

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    assert len(texts) == 3
    # 第一条 target_timerange 应是 [0, 1.5s]
    assert texts[0]["target_timerange"]["start"] == 0
    assert texts[0]["target_timerange"]["duration"] == 1_500_000
    # 样式字段完整性(借鉴 jianying-add-subtitles 惯例)
    style = texts[0]["style"]
    assert style["size"] == 5.0
    assert style["bold"] is True
    assert style["align"] == 1
    assert style["border"]["width"] == 40.0
    assert style["transform_y"] == -0.8
    assert texts[0]["track_name"] == "Subtitles"
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_empty_asr(tmp_draft: Path) -> None:
    """ASR 返回空 → materials.texts 应为空列表。"""
    set_default_client(_FixedASRClient([]))
    out = add_subtitles(_state(tmp_draft))

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"]["texts"] == []
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_uses_snapshot2(tmp_path: Path, tmp_draft: Path) -> None:
    """若 state.snapshot2_path 存在,优先从 snapshot2 读取。"""
    # 构造 snapshot2 文件
    snap2_dir = tmp_path / "snapshots" / "snapshot2"
    snap2_dir.mkdir(parents=True)
    snap2_path = snap2_dir / "draft_content.json"
    snap2_draft = {"canvas_config": {"width": 1080, "height": 1920}, "materials": {}, "tracks": []}
    snap2_path.write_text(json.dumps(snap2_draft), encoding="utf-8")

    set_default_client(_FixedASRClient([{"text": "A", "start_s": 0, "end_s": 1}]))
    state = _state(tmp_draft)
    state["snapshot2_path"] = str(snap2_path)
    out = add_subtitles(state)

    # 写入的是 draft_path(应保留原有内容);读取的是 snapshot2
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert len(draft["materials"]["texts"]) == 1


# ---------------------------------------------------------------------------
# 9 工具迁移 §6.4 step 1:node_08 接入 subtitle_scout
# ---------------------------------------------------------------------------
def test_add_subtitles_scout_failure_falls_back_to_default_style(
    tmp_draft: Path,
) -> None:
    """``subtitle_scout`` 失败 → 不写 ``subtitle_scout_report``,沿用 JianYing 默认样式。"""
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    set_default_client(_FixedASRClient([{"text": "你好", "start_s": 0.0, "end_s": 1.5}]))

    # 让 subtitle_scout 返回 [ERROR](模拟 ffmpeg 缺失 / scenedetect 未装)
    err = ToolResult(text="[ERROR] could not probe video size: missing ffmpeg")

    with patch("nodes.node_08_add_subtitles.subtitle_scout", return_value=err):
        out = add_subtitles(_state(tmp_draft))

    # 失败:不写 subtitle_scout_report
    assert "subtitle_scout_report" not in out
    # status_log 标注 scout 失败 + 沿用默认
    log_text = " | ".join(out["status_log"])
    assert "scout 失败" in log_text
    # 草稿里的样式仍是默认
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    style = draft["materials"]["texts"][0]["style"]
    assert style["size"] == 5.0
    assert style["bold"] is True
    assert style["border"]["width"] == 40.0


def test_add_subtitles_scout_exception_falls_back_to_default_style(
    tmp_draft: Path,
) -> None:
    """``subtitle_scout`` 抛异常 → 同样兜底为默认样式,不污染主链。"""
    from unittest.mock import patch

    set_default_client(_FixedASRClient([{"text": "你好", "start_s": 0.0, "end_s": 1.5}]))

    def boom(*args, **kwargs):
        raise RuntimeError("scenedetect import error")

    with patch("nodes.node_08_add_subtitles.subtitle_scout", side_effect=boom):
        out = add_subtitles(_state(tmp_draft))

    assert "subtitle_scout_report" not in out
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    style = draft["materials"]["texts"][0]["style"]
    assert style["size"] == 5.0
    assert style["border"]["width"] == 40.0


def test_add_subtitles_scout_success_writes_report_to_state(
    tmp_draft: Path,
    scout_style,
) -> None:
    """``subtitle_scout`` 成功 → 报告落到 state,且两条建议都真的改进草稿样式。

    9 工具迁移 §7.1 原始片段写的是 ``{**default, **result.data["style"]}``,但 scout
    报告根本没有顶层 ``style`` 键——建议挂在 ``contrast_recommendation.style`` /
    ``font_recommendation.style``,而剪映 ``style`` 与 libass 键名、单位两套,所以
    必须经 ``STYLE_KEY_MAP`` 换算。这就是原先"只记录不生效"的缺口。
    """
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    set_default_client(_FixedASRClient([{"text": "你好", "start_s": 0.0, "end_s": 1.5}]))

    scout_data = {
        "preset": "shortform_zh",
        # 竖屏 1080x1920 下 shortform_zh baseline:font_size 63px / outline 5.38px
        "video": {"width": 1080, "height": 1920, "duration": 30.0},
        "caption_band": {"top": 1500, "bottom": 1700, "left": 60, "right": 1020},
        "shot_cut_count": 2,
        "contrast_recommendation": {
            "level": "medium",
            "style": {"readability": "heavy_outline"},
        },
        "font_recommendation": {
            "level": "too_small",
            "style": {"font_size": 96},
        },
    }
    ok = ToolResult(text="Scouted ok", data=scout_data)

    with patch("nodes.node_08_add_subtitles.subtitle_scout", return_value=ok):
        out = add_subtitles(_state(tmp_draft))

    # 报告落到 state
    assert out.get("subtitle_scout_report") == scout_data
    log_text = " | ".join(out["status_log"])
    assert "scout 成功" in log_text

    # 建议真正落进草稿:字号 96/63 ≈ 1.5238 倍 → 5.0*1.5238;描边 heavy_outline ≈1.6 倍
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    style = draft["materials"]["texts"][0]["style"]
    assert style["size"] == 7.62
    assert style["border"]["width"] == 64.01
    # 未被建议触及的字段保持调好的默认值
    assert style["bold"] is True
    assert style["align"] == 1
    assert style["transform_y"] == -0.8
    assert style["color"] == [1.0, 1.0, 1.0]
    # 变更说明进 status_log,跑批时不用翻 draft 就知道 scout 改了什么
    assert "scout 样式已生效" in log_text
    assert "字号 5.0 → 7.62" in log_text
    assert "描边宽度 40.0 → 64.01" in log_text


def test_add_subtitles_no_video_path_skips_scout(tmp_draft: Path) -> None:
    """``video_input_path`` 缺失 → 不调 scout(避免 [ERROR] 噪声),沿用默认。"""
    from unittest.mock import patch

    set_default_client(_FixedASRClient([{"text": "hi", "start_s": 0, "end_s": 1}]))
    state = {"draft_path": str(tmp_draft), "status_log": [], "error_log": []}

    with patch("nodes.node_08_add_subtitles.subtitle_scout") as mock_scout:
        out = add_subtitles(state)
        # 没传 video_path → _run_subtitle_scout 内部直接 return None,不会调 subtitle_scout
        mock_scout.assert_not_called()

    assert "subtitle_scout_report" not in out
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    style = draft["materials"]["texts"][0]["style"]
    assert style["size"] == 5.0


# ---------------------------------------------------------------------------
# 9 工具迁移 §7.1:STYLE_KEY_MAP —— scout 建议 → 剪映草稿样式换算
# ---------------------------------------------------------------------------
# 这一组直接测换算函数本身,不再绕 add_subtitles。``sty.resolve_style`` 依赖本机
# 装了可用的 CJK 字体,没有就抛 ValueError——那会让断言随机器变。所以这里用
# ``_fake_resolve_style`` 复刻上游真实的解析算术(只覆盖本模块读到的 font_size /
# outline 两个键),把"映射逻辑对不对"和"这台机器字体装没装"两件事拆开。
def _fake_resolve_style(
    preset_name: str | None, overrides: dict | None, *, video_width: int, video_height: int
) -> dict:
    """复刻 ``subtitle_style.resolve_style`` 中本模块关心的部分(shortform_zh preset)。"""
    overrides = overrides or {}
    height = int(video_height)
    base_font = max(12, round(height * 0.0330))   # font_size_ratio
    base_outline = round(height * 0.0028, 2)      # outline_ratio
    style = {"font_size": base_font, "outline": base_outline}
    if "font_size" in overrides:
        style["font_size"] = int(overrides["font_size"])
    if "outline" in overrides:
        style["outline"] = float(overrides["outline"])
    readability = overrides.get("readability")
    if readability == "heavy_outline":
        style["outline"] = round(base_outline * 1.6, 2)
    elif readability in ("box", "opaque_box"):
        # 上游 box 的描边是"盒子内边距",比 baseline 细得多——这正是不能直接拿它
        # 当描边用的原因,所以本模块对 box 走独立常量。
        style["outline"] = max(1.5, round(style["font_size"] * 0.05, 2))
    return style


@pytest.fixture
def scout_style(monkeypatch: pytest.MonkeyPatch):
    """把 resolve_style 换成确定性假实现,返回 ``_style_from_scout``。"""
    from nodes.node_08_add_subtitles import _style_from_scout

    monkeypatch.setattr(
        "nodes.node_08_add_subtitles.sty.resolve_style", _fake_resolve_style
    )
    return _style_from_scout


def _report(**sections) -> dict:
    """竖屏 1080x1920 + shortform_zh 的 scout 报告骨架(baseline:63px / 5.38px)。"""
    report = {"preset": "shortform_zh", "video": {"width": 1080, "height": 1920, "duration": 30.0}}
    report.update(sections)
    return report


def test_style_key_map_covers_only_calibrated_fields() -> None:
    """对照表就是本链路真校准过的旋钮,不多不少。"""
    from nodes.node_08_add_subtitles import STYLE_KEY_MAP

    assert STYLE_KEY_MAP == {"size": "font_size", "border.width": "outline"}


def test_scout_font_recommendation_scales_size(scout_style) -> None:
    """字号建议 96px / baseline 63px ≈ 1.5238 倍 → 5.0 → 7.62,只动 size。"""
    style, notes = scout_style(
        _report(font_recommendation={"level": "too_small", "style": {"font_size": 96}})
    )
    assert style["size"] == 7.62
    assert style["border"]["width"] == 40.0          # 对比度没建议 → 描边不动
    assert len(notes) == 1 and "字号 5.0 → 7.62" in notes[0]


def test_scout_contrast_recommendation_scales_border(scout_style) -> None:
    """heavy_outline 把描边加到 1.6 倍 → 40.0 → 64.01,只动 border.width。"""
    style, notes = scout_style(
        _report(contrast_recommendation={"level": "medium", "style": {"readability": "heavy_outline"}})
    )
    assert style["border"]["width"] == 64.01
    assert style["size"] == 5.0
    assert len(notes) == 1 and "描边宽度 40.0 → 64.01" in notes[0]


def test_scout_box_treatment_degrades_and_says_so(scout_style) -> None:
    """ASS 的 box 底色带在剪映 schema 里没有对应字段 → 降级为描边加粗,并明说不等价。"""
    style, notes = scout_style(
        _report(contrast_recommendation={"level": "high", "style": {"readability": "box"}})
    )
    assert style["border"]["width"] == 100.0         # 40.0 * 2.5
    joined = " | ".join(notes)
    assert "降级" in joined
    assert "描边 ≠ ASS 的 box" in joined


def test_scout_both_recommendations_apply_together(scout_style) -> None:
    """字号与对比度两条建议同时成立 → 两个字段各自缩放,互不干扰。"""
    style, notes = scout_style(_report(
        font_recommendation={"level": "too_small", "style": {"font_size": 96}},
        contrast_recommendation={"level": "high", "style": {"readability": "heavy_outline"}},
    ))
    assert style["size"] == 7.62
    assert style["border"]["width"] == 64.01
    assert len(notes) == 2


def test_scout_recommends_nothing_leaves_style_untouched(scout_style) -> None:
    """contrast level=low + font level=ok → 两处 style 都是空 dict,草稿一个字段都不动。"""
    style, notes = scout_style(_report(
        font_recommendation={"level": "ok", "style": {}},
        contrast_recommendation={"level": "low", "style": {}},
    ))
    assert style == {
        "size": 5.0, "bold": True, "color": [1.0, 1.0, 1.0],
        "align": 1, "border": {"color": [0, 0, 0], "width": 40.0}, "transform_y": -0.8,
    }
    assert notes == []


@pytest.mark.parametrize(
    ("report", "why"),
    [
        (None, "scout 没跑成"),
        ({}, "空报告"),
        ({"preset": "shortform_zh"}, "缺 video 段"),
        ({"video": "not-a-dict"}, "video 不是 dict"),
        ({"video": {}}, "video 缺 width/height"),
        ({"video": {"width": "abc", "height": 1920}}, "width 不是数字"),
        ({"video": {"width": 0, "height": 1920}}, "width 为 0"),
        ({"video": {"width": 1080, "height": -1}}, "height 为负"),
    ],
)
def test_scout_malformed_report_falls_back(scout_style, report, why) -> None:
    """报告结构坏了就原样退回默认样式,绝不让 scout 的问题冒到主链(why 仅作可读性标注)。"""
    style, notes = scout_style(report)
    assert style["size"] == 5.0
    assert style["border"]["width"] == 40.0
    assert notes == [], why


def test_scout_resolve_style_failure_falls_back(monkeypatch: pytest.MonkeyPatch, scout_style) -> None:
    """未知 preset / 无可用 CJK 字体 → resolve_style 抛 ValueError,吞掉退回默认。"""
    def boom(*args, **kwargs):
        raise ValueError("no font file found for family 'Noto Sans SC'")

    monkeypatch.setattr("nodes.node_08_add_subtitles.sty.resolve_style", boom)
    style, notes = scout_style(
        _report(font_recommendation={"level": "too_small", "style": {"font_size": 96}})
    )
    assert style["size"] == 5.0
    assert notes == []


@pytest.mark.parametrize(("px", "expected"), [(9999, 10.0), (1, 3.0)])
def test_scout_extreme_font_size_is_clamped(scout_style, px, expected) -> None:
    """0.6x ~ 2.0x 是夹取区间——一句离谱建议不该把字幕推到不可用的极端。"""
    style, _ = scout_style(_report(font_recommendation={"level": "too_small", "style": {"font_size": px}}))
    assert style["size"] == expected


def test_scout_style_switch_off_falls_back(scout_style) -> None:
    """``SUBTITLE_SCOUT_STYLE_ENABLED=false`` → 退回只记录不改样式的旧行为。"""
    import config

    style, notes = scout_style(_report(font_recommendation={"level": "too_small", "style": {"font_size": 96}}))
    assert style["size"] == 7.62

    original = config.SUBTITLE_SCOUT_STYLE_ENABLED
    config.SUBTITLE_SCOUT_STYLE_ENABLED = False
    try:
        style, notes = scout_style(
            _report(font_recommendation={"level": "too_small", "style": {"font_size": 96}})
        )
    finally:
        config.SUBTITLE_SCOUT_STYLE_ENABLED = original

    assert style["size"] == 5.0
    assert notes == []


def test_scout_style_never_mutates_default_constant(scout_style) -> None:
    """换算结果必须是独立副本,不能共享 ``border`` 嵌套字典。"""
    import copy

    from nodes.node_08_add_subtitles import _JIANYING_DEFAULT_STYLE

    snapshot = copy.deepcopy(_JIANYING_DEFAULT_STYLE)
    style, _ = scout_style(
        _report(contrast_recommendation={"level": "high", "style": {"readability": "box"}})
    )
    style["border"]["width"] = 1.0        # 事后就地改,模拟下游误用
    assert _JIANYING_DEFAULT_STYLE == snapshot


def test_add_subtitles_each_text_gets_own_style_dict(tmp_draft: Path, scout_style) -> None:
    """3 条字幕的 style 互相独立:改一条不影响另外两条(嵌套 border 也要分开)。"""
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    set_default_client(_FixedASRClient([
        {"text": "一", "start_s": 0.0, "end_s": 1.0},
        {"text": "二", "start_s": 1.0, "end_s": 2.0},
        {"text": "三", "start_s": 2.0, "end_s": 3.0},
    ]))
    ok = ToolResult(text="ok", data=_report(
        font_recommendation={"level": "too_small", "style": {"font_size": 96}}
    ))
    with patch("nodes.node_08_add_subtitles.subtitle_scout", return_value=ok):
        add_subtitles(_state(tmp_draft))

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    assert len(texts) == 3
    assert all(t["style"]["size"] == 7.62 for t in texts)
    assert texts[0]["style"] is not texts[1]["style"]
    assert texts[0]["style"]["border"] is not texts[1]["style"]["border"]


def test_scout_style_with_real_resolve_style() -> None:
    """冒烟:真 ``subtitle_style.resolve_style`` 在本机能解析 → 换算走通不回落。

    上面的用例都用假 ``resolve_style`` 把"映射逻辑"和"字体装没装"拆开;这条负责
    确认两者接得上。机器上没有可用 CJK 字体时 resolve_style 会抛 ValueError,
    那正是 ``_style_from_scout`` 要吞掉的降级路径,所以这里 skip 而不是 fail。
    """
    from nodes.node_08_add_subtitles import _style_from_scout

    try:
        sty.resolve_style("shortform_zh", None, video_width=1080, video_height=1920)
    except Exception as exc:  # noqa: BLE001 — 缺字体是环境问题,不是本模块的 bug
        pytest.skip(f"本机无可用 CJK 字体,resolve_style 不可用: {exc}")

    style, notes = _style_from_scout({
        "preset": "shortform_zh",
        "video": {"width": 1080, "height": 1920, "duration": 30.0},
        "font_recommendation": {"level": "too_small", "style": {"font_size": 96}},
        "contrast_recommendation": {"level": "high", "style": {"readability": "box"}},
    })
    # baseline 63px → 建议 96px 约 1.52 倍;box 走 2.5 倍常量
    assert style["size"] == 7.62
    assert style["border"]["width"] == 100.0
    assert len(notes) == 3
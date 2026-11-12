"""节点 8 字幕主干单测 — B1 ASR 移除后的无 ASR 语义 + scout 样式字段完整性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_08_add_subtitles import add_subtitles
from video_edit_capabilities import subtitle_style as sty


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


def test_add_subtitles_no_asr_omits_asr_keys(tmp_draft: Path) -> None:
    """B1 ASR 移除:节点不做语音转写 —— 不写 asr_segments_zh(计划 §3.3 取"缺省")。

    新任务 state 无此键 → node_16a 走"无源字幕"正常跳过;不注入任何字幕。
    """
    out = add_subtitles(_state(tmp_draft))

    assert "asr_segments_zh" not in out
    assert "asr_segments_zh_written" not in out
    assert out["error_log"] == []
    assert "node_08_add_subtitles_done" in out["status_log"]
    # 草稿不被注入文本素材(ASR 字幕通道已整体移除)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"].get("texts") is None


def test_add_subtitles_passes_through_legacy_asr_segments_unchanged(tmp_draft: Path) -> None:
    """旧线程兼容:state 已有的 asr_segments_zh 经 {**state} 原样透传,node_08 不改写。

    节点既不重新生产也不清空 —— 16a 对旧线程残留段按兼容语义消费。
    """
    state = _state(tmp_draft)
    legacy = [{"index": 0, "start_ms": 0, "end_ms": 1_000, "text_zh": "旧段"}]
    state["asr_segments_zh"] = legacy
    state["asr_segments_zh_written"] = True

    out = add_subtitles(state)

    assert out["asr_segments_zh"] == legacy
    assert out["asr_segments_zh_written"] is True
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_preserves_existing_script_subtitles(tmp_path: Path) -> None:
    """已有脚本字幕(mapper 写入)→ node_08 不得清空/覆盖/替换原字幕轨。

    回归锁:历史上 ``sync_text_track(draft, [])`` 会把已有 text 轨整轨替换为空。
    """
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "videos": [{"id": "v1"}],
            "texts": [{
                "id": "subtitle-zh-1",
                "type": "text",
                "content": "脚本字幕",
                "target_timerange": {"start": 0, "duration": 10_000_000},
            }],
        },
        "tracks": [
            {"type": "video", "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": 10_000_000}}]},
            {"type": "text", "segments": [{"id": "t1", "material_id": "subtitle-zh-1", "target_timerange": {"start": 0, "duration": 10_000_000}}]},
        ],
    }
    draft_file = tmp_path / "draft" / "draft_content.json"
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    out = add_subtitles(_state(draft_file))

    # 整份草稿逐字段不变:无 ASR 段时绝不做整轨同步
    after = json.loads(draft_file.read_text(encoding="utf-8"))
    assert after == draft
    assert len(after["materials"]["texts"]) == 1
    assert after["materials"]["texts"][0]["content"] == "脚本字幕"
    assert any(t["type"] == "text" and len(t["segments"]) == 1 for t in after["tracks"])
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_uses_snapshot2(tmp_path: Path, tmp_draft: Path) -> None:
    """若 state.snapshot2_path 存在,优先从 snapshot2 读取(内容原样落回 draft_path)。"""
    # 构造 snapshot2 文件(带可识别标记:materials 为空,与 draft_path 的 v1 区分)
    snap2_dir = tmp_path / "snapshots" / "snapshot2"
    snap2_dir.mkdir(parents=True)
    snap2_path = snap2_dir / "draft_content.json"
    snap2_draft = {"canvas_config": {"width": 1080, "height": 1920}, "materials": {}, "tracks": []}
    snap2_path.write_text(json.dumps(snap2_draft), encoding="utf-8")

    state = _state(tmp_draft)
    state["snapshot2_path"] = str(snap2_path)
    out = add_subtitles(state)

    # 读取的是 snapshot2:其内容被原样写回 draft_path(v1 视频素材消失即为证据)
    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    assert draft["materials"] == {}
    assert "node_08_add_subtitles_done" in out["status_log"]


# ---------------------------------------------------------------------------
# 9 工具迁移 §6.4 step 1:node_08 接入 subtitle_scout
# ---------------------------------------------------------------------------
def test_add_subtitles_scout_failure_falls_back_to_default_style(
    tmp_draft: Path,
) -> None:
    """``subtitle_scout`` 失败 → 不写 ``subtitle_scout_report``,沿用 JianYing 默认样式。"""
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

    # 让 subtitle_scout 返回 [ERROR](模拟 ffmpeg 缺失 / scenedetect 未装)
    err = ToolResult(text="[ERROR] could not probe video size: missing ffmpeg")

    with patch("nodes.node_08_add_subtitles.subtitle_scout", return_value=err):
        out = add_subtitles(_state(tmp_draft))

    # 失败:不写 subtitle_scout_report
    assert "subtitle_scout_report" not in out
    # status_log 标注 scout 失败 + 沿用默认
    log_text = " | ".join(out["status_log"])
    assert "scout 失败" in log_text
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_scout_exception_falls_back_to_default_style(
    tmp_draft: Path,
) -> None:
    """``subtitle_scout`` 抛异常 → 同样兜底为默认样式,不污染主链。"""
    from unittest.mock import patch

    def boom(*args, **kwargs):
        raise RuntimeError("scenedetect import error")

    with patch("nodes.node_08_add_subtitles.subtitle_scout", side_effect=boom):
        out = add_subtitles(_state(tmp_draft))

    assert "subtitle_scout_report" not in out
    assert "node_08_add_subtitles_done" in out["status_log"]


def test_add_subtitles_scout_success_writes_report_to_state(
    tmp_draft: Path,
    scout_style,
) -> None:
    """``subtitle_scout`` 成功 → 报告落到 state,换算说明进 status_log。

    9 工具迁移 §7.1 原始片段写的是 ``{**default, **result.data["style"]}``,但 scout
    报告根本没有顶层 ``style`` 键——建议挂在 ``contrast_recommendation.style`` /
    ``font_recommendation.style``,而剪映 ``style`` 与 libass 键名、单位两套,所以
    必须经 ``STYLE_KEY_MAP`` 换算。B1 ASR 移除后 node_08 不再产生 ASR 字幕素材,
    换算结果暂无草稿落点,但 scout 报告与换算说明的 state/status 契约保持不变。
    """
    from unittest.mock import patch

    from assembly_capabilities.result import ToolResult

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

    # 换算说明进 status_log(字号 96/63 ≈ 1.5238 倍 → 5.0*1.5238;描边 ≈1.6 倍)
    assert "scout 样式已生效" in log_text
    assert "字号 5.0 → 7.62" in log_text
    assert "描边宽度 40.0 → 64.01" in log_text


def test_add_subtitles_no_video_path_skips_scout(tmp_draft: Path) -> None:
    """``video_input_path`` 缺失 → 不调 scout(避免 [ERROR] 噪声),沿用默认。"""
    from unittest.mock import patch

    state = {"draft_path": str(tmp_draft), "status_log": [], "error_log": []}

    with patch("nodes.node_08_add_subtitles.subtitle_scout") as mock_scout:
        out = add_subtitles(state)
        # 没传 video_path → _run_subtitle_scout 内部直接 return None,不会调 subtitle_scout
        mock_scout.assert_not_called()

    assert "subtitle_scout_report" not in out


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

# ---------------------------------------------------------------------------
# P4(决定 2 选 A):storyline 推荐(storyline_text_style_plan)叠加
# ---------------------------------------------------------------------------
def test_overlay_storyline_plan_absent_is_noop() -> None:
    """文档例 1「无推荐时行为不变」:plan 缺失/空/非法 → 样式原样、无 note。"""
    from nodes.node_08_add_subtitles import (
        _JIANYING_DEFAULT_STYLE,
        _overlay_storyline_style,
    )

    base = _JIANYING_DEFAULT_STYLE
    for plan in (None, {}, "not-a-dict", {"items": []}, {"default": "not-a-dict"}):
        style, notes = _overlay_storyline_style(dict(base), plan, lang="zh")
        assert style == base, plan
        assert notes == [], plan


def test_overlay_storyline_plan_overrides_size_position_color() -> None:
    """文档例 2「有推荐时覆盖字号和位置」(+颜色):48px→2.0 夹取、top→0.8、红。"""
    from nodes.node_08_add_subtitles import (
        _JIANYING_DEFAULT_STYLE,
        _overlay_storyline_style,
    )

    plan = {"default": {"size": 48, "color": "#FF0000", "position": "top"}}
    style, notes = _overlay_storyline_style(
        dict(_JIANYING_DEFAULT_STYLE), plan, lang="zh"
    )

    assert style["size"] == 10.0  # 48/24 = 2.0 → 触顶夹取
    assert style["transform_y"] == 0.8
    assert style["color"] == [1.0, 0.0, 0.0]
    # 其他字段不被触碰
    assert style["border"] == _JIANYING_DEFAULT_STYLE["border"]
    assert style["bold"] == _JIANYING_DEFAULT_STYLE["bold"]
    assert any("字号" in n for n in notes)
    assert any("字幕颜色" in n for n in notes)
    assert any("字幕位置" in n for n in notes)


def test_overlay_storyline_font_unavailable_notes_fallback() -> None:
    """文档例 3「字体不可用时回落」:资源库查不到 → 只记 note,样式不变。"""
    from nodes.node_08_add_subtitles import (
        _JIANYING_DEFAULT_STYLE,
        _overlay_storyline_style,
    )

    plan = {"default": {"font_zh": "NoSuchFont-Bold.otf", "size": 24}}
    style, notes = _overlay_storyline_style(
        dict(_JIANYING_DEFAULT_STYLE), plan, lang="zh"
    )

    assert style == _JIANYING_DEFAULT_STYLE  # 无 font 键、其他字段不变
    assert any("NoSuchFont-Bold.otf" in n and "不存在" in n for n in notes)
    assert any("回落" in n for n in notes)


def test_overlay_storyline_stub_defaults_identity() -> None:
    """推荐 stub 默认(24/bottom/#FFFFFF/SourceHanSansCN-Bold.otf)→ 恒等变换。

    「无推荐时行为不变」的构造性证明:size 24/24=1.0、bottom=-0.8、#FFFFFF=白,
    逐项等于剪映默认;字体不存在只记 note,不改样式。
    """
    from nodes.node_08_add_subtitles import (
        _JIANYING_DEFAULT_STYLE,
        _overlay_storyline_style,
    )

    plan = {
        "default": {
            "font_zh": "SourceHanSansCN-Bold.otf",
            "font_en": "Roboto-Bold.ttf",
            "size": 24,
            "color": "#FFFFFF",
            "position": "bottom",
        }
    }
    style, notes = _overlay_storyline_style(
        dict(_JIANYING_DEFAULT_STYLE), plan, lang="zh"
    )
    assert style == _JIANYING_DEFAULT_STYLE
    assert any("SourceHanSansCN-Bold.otf" in n for n in notes)  # 字体检查有记录


def test_node_08_logs_storyline_overlay_notes(tmp_draft: Path) -> None:
    """节点级接线:state 带 plan → status_log 出现 storyline 推荐样式说明;无 plan → 不出现。"""
    plan = {"default": {"size": 30, "position": "center"}}

    state_with = _state(tmp_draft)
    state_with["storyline_text_style_plan"] = plan
    out_with = add_subtitles(state_with)
    assert any(
        "[node_08] storyline 推荐样式:" in entry for entry in out_with["status_log"]
    )

    out_without = add_subtitles(_state(tmp_draft))
    assert not any(
        "[node_08] storyline 推荐样式:" in entry for entry in out_without["status_log"]
    )


# ---------------------------------------------------------------------------
# 阶段 1(2026-11-09 效果缺失修复):中文旁白字幕原生落草稿
# ---------------------------------------------------------------------------
import copy as _copy


def _zh_plan() -> dict:
    """两段两组的 storyline_plan:clip_0001→group_0001(0-3s),clip_0002→group_0002(3-7s)。"""
    return {
        "schema_version": "1.0",
        "job_id": "job",
        "created_at_ms": 0,
        "source_media": [
            {"media_id": "media_0001", "file_uri": "file:///C:/v.mp4", "duration_ms": 6000, "media_type": "video"}
        ],
        "clips": [
            {
                "clip_id": "clip_0001", "source_media_id": "media_0001",
                "source_in_ms": 0, "source_out_ms": 3000,
                "timeline_in_ms": 0, "timeline_out_ms": 3000,
                "voiceover_id": "voiceover_0001",
            },
            {
                "clip_id": "clip_0002", "source_media_id": "media_0001",
                "source_in_ms": 3000, "source_out_ms": 6000,
                "timeline_in_ms": 3000, "timeline_out_ms": 7000,
                "voiceover_id": "voiceover_0002",
            },
        ],
        "audio": {
            "bgm_ref": None,
            "voiceover": json.dumps(
                {"group_0001": "voiceover_0001", "group_0002": "voiceover_0002"}
            ),
        },
        "subtitles": {"zh": None, "en": None},
        "options": {"enable_ai_transition": False, "enable_voiceover": True, "max_duration_ms": 7000},
    }


def _zh_draft() -> dict:
    """与 _zh_plan 源范围一致的两段原生草稿(路径用 / 形态,模拟剪映保存后改写)。"""
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 7_000_000,
        "materials": {
            "videos": [{"id": "v1", "path": "C:/v.mp4"}],
            "texts": [],
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {
                        "id": "s1", "material_id": "v1",
                        "source_timerange": {"start": 0, "duration": 3_000_000},
                        "target_timerange": {"start": 0, "duration": 3_000_000},
                    },
                    {
                        "id": "s2", "material_id": "v1",
                        "source_timerange": {"start": 3_000_000, "duration": 3_000_000},
                        "target_timerange": {"start": 3_000_000, "duration": 4_000_000},
                    },
                ],
            },
        ],
    }


def _zh_state(draft_path: Path, tmp_path: Path, narration_g1: str, narration_g2: str) -> dict:
    script_file = tmp_path / "script.json"
    script_file.write_text(
        json.dumps(
            {
                "group_scripts": [
                    {"group_id": "group_0001", "narration": narration_g1},
                    {"group_id": "group_0002", "narration": narration_g2},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state = _state(draft_path)
    state["storyline_plan"] = _zh_plan()
    state["storyline_script_artifact"] = str(script_file)
    return state


def _write_draft(tmp_path: Path, draft: dict) -> Path:
    draft_file = tmp_path / "draft" / "draft_content.json"
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_file


def test_zh_subtitles_written_from_narration(tmp_path: Path) -> None:
    """有旁白 → materials.texts 原生条目 + text 轨段(material_id 可解析)+ state 段落。"""
    draft_file = _write_draft(tmp_path, _zh_draft())
    state = _zh_state(draft_file, tmp_path, "第一句。第二句!", "第三句")

    out = add_subtitles(state)

    assert "node_08_add_subtitles_done" in out["status_log"]
    segs = out["subtitle_segments_zh"]
    assert len(segs) == 3
    # 组1(0-3s)两句按字符比例(4:4)各 1.5s;组2(3-7s)一句 4s
    assert (segs[0]["start_ms"], segs[0]["end_ms"]) == (0, 1500)
    assert (segs[1]["start_ms"], segs[1]["end_ms"]) == (1500, 3000)
    assert (segs[2]["start_ms"], segs[2]["end_ms"]) == (3000, 7000)

    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    assert [t["id"] for t in texts] == ["text-zh-1", "text-zh-2", "text-zh-3"]
    # 原生 content JSON(text 字段回显句子)
    for t, expect in zip(texts, ["第一句。", "第二句!", "第三句"]):
        assert json.loads(t["content"])["text"] == expect
    # text 轨段引用完整
    text_tracks = [t for t in draft["tracks"] if t["type"] == "text"]
    assert len(text_tracks) == 1
    assert [s["material_id"] for s in text_tracks[0]["segments"]] == [
        "text-zh-1", "text-zh-2", "text-zh-3",
    ]


def test_zh_subtitles_replay_is_idempotent(tmp_path: Path) -> None:
    """重放(幂等):同一 state 跑两遍,草稿逐字节一致。"""
    draft_file = _write_draft(tmp_path, _zh_draft())
    state = _zh_state(draft_file, tmp_path, "第一句。第二句!", "第三句")

    add_subtitles(state)
    first = draft_file.read_text(encoding="utf-8")
    add_subtitles(state)
    assert draft_file.read_text(encoding="utf-8") == first


def test_zh_subtitles_written_without_voiceover_id_via_subtitle_style(tmp_path: Path) -> None:
    """TTS 失败(has_tts=False)→ clips 无 voiceover_id:subtitle_style.group_id 兜底。

    2026-11-10 console-4bf596407c19 复盘:voiceover_id_map 空 → 组映射全空 →
    字幕静默跳过,配音挂了字幕陪葬。修复后组定位双通道(voiceover_id 桥优先,
    subtitle_style.group_id 兜底)。
    """
    plan = _zh_plan()
    for c in plan["clips"]:
        c.pop("voiceover_id")
        c["subtitle_style"] = {
            "group_id": "group_0001" if c["clip_id"] == "clip_0001" else "group_0002",
        }
    plan["audio"]["voiceover"] = None  # has_tts=False 的实机形态:映射为空
    draft_file = _write_draft(tmp_path, _zh_draft())
    state = _zh_state(draft_file, tmp_path, "第一句。第二句!", "第三句")
    state["storyline_plan"] = plan

    out = add_subtitles(state)

    assert "node_08_add_subtitles_done" in out["status_log"]
    segs = out["subtitle_segments_zh"]
    assert len(segs) == 3
    assert (segs[0]["start_ms"], segs[0]["end_ms"]) == (0, 1500)
    assert (segs[2]["start_ms"], segs[2]["end_ms"]) == (3000, 7000)
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    assert [t["id"] for t in draft["materials"]["texts"]] == [
        "text-zh-1", "text-zh-2", "text-zh-3",
    ]


def test_zh_subtitles_skipped_when_no_group_key_at_all(tmp_path: Path) -> None:
    """voiceover_id 与 subtitle_style.group_id 都缺 → 不猜,跳过并留 note。"""
    plan = _zh_plan()
    for c in plan["clips"]:
        c.pop("voiceover_id")
    plan["audio"]["voiceover"] = None
    draft_file = _write_draft(tmp_path, _zh_draft())
    state = _zh_state(draft_file, tmp_path, "第一句。", "第三句")
    state["storyline_plan"] = plan

    out = add_subtitles(state)

    assert any(
        "无法把当前草稿视频段映射到分组" in str(s) for s in out["status_log"]
    )
    assert "subtitle_segments_zh" not in out
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    assert draft["materials"]["texts"] == []


def test_zh_subtitles_empty_narration_leaves_draft_untouched(tmp_path: Path) -> None:
    """空旁白(上游断裂的常态输入)→ 不写草稿、不产 subtitle_segments_zh、只记 note。"""
    draft = _zh_draft()
    draft_file = _write_draft(tmp_path, draft)
    state = _zh_state(draft_file, tmp_path, "", "  ")

    out = add_subtitles(state)

    after = json.loads(draft_file.read_text(encoding="utf-8"))
    assert after == draft
    assert "subtitle_segments_zh" not in out
    assert any("中文字幕跳过" in x for x in out["status_log"])


def test_zh_subtitles_without_plan_leaves_draft_untouched(tmp_path: Path) -> None:
    """无 storyline_plan → 无法映射组区间,跳过(草稿字节不变;既有回归锁语义)。"""
    draft = _zh_draft()
    draft_file = _write_draft(tmp_path, draft)
    state = _zh_state(draft_file, tmp_path, "有旁白", "也有")
    del state["storyline_plan"]

    out = add_subtitles(state)

    after = json.loads(draft_file.read_text(encoding="utf-8"))
    assert after == draft
    assert "subtitle_segments_zh" not in out
    assert any("无法把当前草稿视频段映射到分组" in x for x in out["status_log"])


def test_zh_subtitles_survive_human_reorder(tmp_path: Path) -> None:
    """关卡①人工调序后:target 位置互换、源范围不变 → 组区间跟随新位置。"""
    draft = _zh_draft()
    segs = draft["tracks"][0]["segments"]
    segs[0], segs[1] = segs[1], segs[0]
    segs[0]["target_timerange"] = {"start": 0, "duration": 4_000_000}
    segs[1]["target_timerange"] = {"start": 4_000_000, "duration": 3_000_000}
    draft_file = _write_draft(tmp_path, draft)
    state = _zh_state(draft_file, tmp_path, "第一组旁白", "第二组旁白")

    out = add_subtitles(state)

    segs_zh = out["subtitle_segments_zh"]
    # 源范围匹配:clip_0002(源 3000-6000)现落在 0-4s,其组字幕应在 0-4s
    assert (segs_zh[0]["start_ms"], segs_zh[0]["end_ms"]) == (0, 4000)
    # clip_0001(源 0-3000)现落在 4-7s
    assert (segs_zh[1]["start_ms"], segs_zh[1]["end_ms"]) == (4000, 7000)


def test_zh_subtitles_preserves_foreign_texts_and_replaces_own(tmp_path: Path) -> None:
    """人工文本素材/轨道保留;本节点旧产物(text-zh-*/track-text-1)被定向替换。"""
    draft = _zh_draft()
    draft["materials"]["texts"] = [
        {"id": "manual-1", "type": "text", "content": "人工标题",
         "target_timerange": {"start": 0, "duration": 1_000_000}},
        {"id": "text-zh-9", "type": "text", "content": "上一轮旧字幕",
         "target_timerange": {"start": 0, "duration": 1_000_000}},
    ]
    draft["tracks"].append(
        {"type": "text", "id": "track-manual", "segments": [
            {"id": "m1", "material_id": "manual-1",
             "target_timerange": {"start": 0, "duration": 1_000_000}}]},
    )
    draft["tracks"].append(
        {"type": "text", "id": "track-text-1", "segments": [
            {"id": "old1", "material_id": "text-zh-9",
             "target_timerange": {"start": 0, "duration": 1_000_000}}]},
    )
    draft_file = _write_draft(tmp_path, draft)
    state = _zh_state(draft_file, tmp_path, "第一组旁白", "第二组旁白")

    out = add_subtitles(state)

    after = json.loads(draft_file.read_text(encoding="utf-8"))
    texts = {t["id"]: t for t in after["materials"]["texts"]}
    # 人工文本条目原样保留(内容/时间未被触碰)
    assert texts["manual-1"]["content"] == "人工标题"
    assert texts["manual-1"]["target_timerange"] == {"start": 0, "duration": 1_000_000}
    # 我方旧条目(text-zh-9)被新产物整体替换,不再残留
    assert "text-zh-9" not in texts
    zh_ids = [i for i in texts if i.startswith("text-zh-")]
    assert len(zh_ids) == 2, f"应恰好 2 条我方新字幕,实际 {sorted(texts)}"
    # track-text-1 重建:只指向新产物,旧段(old1)不残留;人工轨道原样
    tracks = {t.get("id"): t for t in after["tracks"]}
    assert [t["id"] for t in after["tracks"] if t["type"] == "text"] == ["track-manual", "track-text-1"]
    assert {s["material_id"] for s in tracks["track-manual"]["segments"]} == {"manual-1"}
    zh_segs = tracks["track-text-1"]["segments"]
    assert {s["material_id"] for s in zh_segs} == set(zh_ids)
    assert all(s["id"] != "old1" for s in zh_segs)
    # 字幕句数正确:两组各 1 句旁白 → 2 条字幕(state 段落与草稿条目同数)
    assert len(zh_segs) == 2
    assert len(out["subtitle_segments_zh"]) == 2
    assert "node_08_add_subtitles_done" in out["status_log"]


# ---------------------------------------------------------------------------
# 阶段 2(2026-11-09 效果缺失修复):中文 TTS 落草稿(权威配音轨重写)
# ---------------------------------------------------------------------------
import wave


def _write_wav(path: Path, duration_ms: int) -> str:
    """写一段真实静音 WAV(stdlib wave;时长可被 read_wav_duration_ms 读回)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * duration_ms / 1000))
    return str(path)


def _vo_state(draft_path: Path, tmp_path: Path, entries: list[dict]) -> dict:
    vo_dir = tmp_path / "storyline"
    vo_file = vo_dir / "voiceover.json"
    vo_file.write_text(
        json.dumps(
            {"voiceover": entries, "provider": "firered", "stub_count": 0, "errors": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state = _state(draft_path)
    state["storyline_plan"] = _zh_plan()  # 组→区间映射依赖 plan(同字幕口径)
    state["storyline_voiceover_artifact"] = str(vo_file)
    return state


def _vo_entries(tmp_path: Path) -> list[dict]:
    return [
        {
            "voiceover_id": "voiceover_0001", "group_id": "group_0001",
            "path": _write_wav(tmp_path / "storyline" / "voiceover_0001.wav", 2000),
            "duration": 2000, "provider": "firered", "error": None,
        },
        {
            "voiceover_id": "voiceover_0002", "group_id": "group_0002",
            "path": _write_wav(tmp_path / "storyline" / "voiceover_0002.wav", 1500),
            "duration": 1500, "provider": "firered", "error": None,
        },
    ]


def test_zh_voiceover_written_at_group_spans(tmp_path: Path) -> None:
    """有效段按当前草稿组区间落 native extract_music 素材 + Voiceover_zh 轨。"""
    draft_file = _write_draft(tmp_path, _zh_draft())
    state = _vo_state(draft_file, tmp_path, _vo_entries(tmp_path))

    out = add_subtitles(state)

    summary = out["voiceover_summary"]
    assert summary == {"provider": "firered", "stub_count": 0, "total": 2, "written": 2, "failed": 0}
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    vo_tracks = [t for t in draft["tracks"] if t["type"] == "audio"]
    assert len(vo_tracks) == 1
    assert vo_tracks[0]["name"] == "Voiceover_zh"
    # 组1(0-3s)放 2s;组2(3-7s)放 1.5s
    tr = {s["target_timerange"]["start"]: s["target_timerange"]["duration"] for s in vo_tracks[0]["segments"]}
    assert tr[0] == 2_000_000 and tr[3_000_000] == 1_500_000
    audios = draft["materials"]["audios"]
    assert len(audios) == 2
    assert all(a["type"] == "extract_music" and a["path"].endswith(".wav") for a in audios)
    assert any("中文配音已落草稿:2 段" in x for x in out["status_log"])


def test_zh_voiceover_replay_is_idempotent(tmp_path: Path) -> None:
    """重放:同一 state 跑两遍,轨/素材不翻倍。"""
    draft_file = _write_draft(tmp_path, _zh_draft())
    state = _vo_state(draft_file, tmp_path, _vo_entries(tmp_path))

    add_subtitles(state)
    first = json.loads(draft_file.read_text(encoding="utf-8"))
    add_subtitles(state)
    second = json.loads(draft_file.read_text(encoding="utf-8"))
    assert first == second
    vo_tracks = [t for t in second["tracks"] if t["type"] == "audio"]
    assert len(vo_tracks) == 1 and len(vo_tracks[0]["segments"]) == 2


def test_zh_voiceover_clamped_to_draft_duration(tmp_path: Path) -> None:
    """配音长于组区间到草稿末尾:钳制到草稿总时长(同 node_17 口径)。"""
    draft = _zh_draft()
    draft["duration"] = 4_000_000  # 草稿只有 4s
    # 组2 从 3s 起,wav 60s → 段时长钳到 1s
    draft["tracks"][0]["segments"] = [draft["tracks"][0]["segments"][0]]
    draft["tracks"][0]["segments"].append(
        {"id": "s2", "material_id": "v1",
         "source_timerange": {"start": 3_000_000, "duration": 3_000_000},
         "target_timerange": {"start": 3_000_000, "duration": 1_000_000}}
    )
    draft_file = _write_draft(tmp_path, draft)
    entries = _vo_entries(tmp_path)
    entries[1]["path"] = _write_wav(tmp_path / "storyline" / "long.wav", 60_000)
    entries[1]["duration"] = 60_000
    state = _vo_state(draft_file, tmp_path, entries)

    out = add_subtitles(state)

    draft_after = json.loads(draft_file.read_text(encoding="utf-8"))
    vo_tracks = [t for t in draft_after["tracks"] if t["type"] == "audio"]
    seg2 = next(s for s in vo_tracks[0]["segments"] if s["target_timerange"]["start"] == 3_000_000)
    assert seg2["target_timerange"]["duration"] == 1_000_000
    assert out["voiceover_summary"]["written"] == 2


def test_zh_voiceover_bad_wav_counted_failed(tmp_path: Path) -> None:
    """WAV 缺失的段跳过并计入 failed;有效段照常写入。"""
    draft_file = _write_draft(tmp_path, _zh_draft())
    entries = _vo_entries(tmp_path)
    entries[1]["path"] = str(tmp_path / "storyline" / "missing.wav")
    state = _vo_state(draft_file, tmp_path, entries)

    out = add_subtitles(state)

    assert out["voiceover_summary"]["written"] == 1
    assert out["voiceover_summary"]["failed"] == 1
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    vo_tracks = [t for t in draft["tracks"] if t["type"] == "audio"]
    assert len(vo_tracks[0]["segments"]) == 1


def test_zh_voiceover_preserves_human_music_track(tmp_path: Path) -> None:
    """删除守卫:人工音乐轨(素材路径在 storyline 目录之外)保留;我方旧轨被替换。"""
    draft = _zh_draft()
    human_mat = {"id": "human-music", "type": "audio", "name": "Island Sway 3",
                 "path": "C:/Music/Cache/abc.mp3", "duration": 25_000_000}
    draft["materials"]["audios"] = [human_mat]
    draft["tracks"].append(
        {"type": "audio", "id": "track-human", "name": "",
         "segments": [{"id": "hs1", "material_id": "human-music",
                       "target_timerange": {"start": 0, "duration": 25_000_000}}]}
    )
    # 我方上一轮旧配音轨(素材路径在 storyline 目录内 → 属我方,应被替换)
    old_mat = {"id": "old-vo", "type": "audio", "name": "voiceover_0001.wav",
               "path": str(tmp_path / "storyline" / "stale.wav"), "duration": 1_000_000}
    draft["materials"]["audios"].append(old_mat)
    draft["tracks"].append(
        {"type": "audio", "id": "track-audio-zh-voiceover", "name": "Voiceover_zh",
         "segments": [{"id": "os1", "material_id": "old-vo",
                       "target_timerange": {"start": 0, "duration": 1_000_000}}]}
    )
    draft_file = _write_draft(tmp_path, draft)
    state = _vo_state(draft_file, tmp_path, _vo_entries(tmp_path))

    add_subtitles(state)

    after = json.loads(draft_file.read_text(encoding="utf-8"))
    vo_tracks = [t for t in after["tracks"] if t["type"] == "audio"]
    assert {t["id"] for t in vo_tracks} == {"track-human", "track-audio-zh-voiceover"}
    human_track = next(t for t in vo_tracks if t["id"] == "track-human")
    assert human_track["segments"][0]["material_id"] == "human-music"
    ids = {a["id"] for a in after["materials"]["audios"]}
    assert "human-music" in ids and "old-vo" not in ids


def test_zh_voiceover_without_artifact_skips(tmp_path: Path) -> None:
    """无 voiceover.json → 不写配音轨、不产 voiceover_summary,只记 note。"""
    draft = _zh_draft()
    draft_file = _write_draft(tmp_path, draft)
    state = _state(draft_file)

    out = add_subtitles(state)

    after = json.loads(draft_file.read_text(encoding="utf-8"))
    assert not [t for t in after["tracks"] if t["type"] == "audio"]
    assert "voiceover_summary" not in out
    assert any("中文配音跳过" in x for x in out["status_log"])

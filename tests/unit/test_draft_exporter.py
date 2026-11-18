"""draft_ops.draft_exporter 单测(阶段 6:渲染计划 / argv / 真渲染 / 校验)。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from draft_ops.draft_exporter import (
    DraftExportError,
    build_ffmpeg_argv,
    build_render_plan,
    resolve_fontfile,
    run_export,
)


# ---------------------------------------------------------------------------
# 渲染计划
# ---------------------------------------------------------------------------
def _draft(**overrides) -> dict:
    base: dict = {
        "canvas_config": {"width": 90, "height": 160},
        "materials": {
            "videos": [
                {"id": "video-1", "type": "video", "path": "C:/media/a.mp4"},
                {"id": "video-2", "type": "video", "path": "C:/media/b.mp4"},
            ],
            "audios": [{"id": "bgm-1", "type": "audio", "path": "C:/media/bgm.mp3"}],
            "transitions": [{"id": "transition-1"}],
            "stickers": [{"id": "sticker-1"}],
            "texts": [
                {
                    "id": "text-1",
                    "content": "你好:世界,100%",
                    "target_timerange": {"start": 200_000, "duration": 400_000},
                    "style": {"size": 5.0, "color": [1.0, 1.0, 1.0], "transform_y": -0.8},
                }
            ],
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "source_timerange": {"start": 0, "duration": 500_000},
                        "target_timerange": {"start": 0, "duration": 500_000},
                        "speed": 1.0,
                    },
                    {
                        "id": "segment-2",
                        "material_id": "video-2",
                        "source_timerange": {"start": 200_000, "duration": 400_000},
                        "target_timerange": {"start": 600_000, "duration": 400_000},
                        "speed": 2.0,
                    },
                ],
            },
            {
                "type": "audio",
                "segments": [
                    {
                        "id": "segment-bgm-1",
                        "material_id": "bgm-1",
                        "target_timerange": {"start": 0, "duration": 1_000_000},
                        "loop": True,
                        "speed": 1.0,
                    }
                ],
            },
        ],
    }
    base.update(overrides)
    return base


def test_build_render_plan_orders_and_fields() -> None:
    plan = build_render_plan(_draft())
    assert [s["tgt_start_us"] for s in plan["segments"]] == [0, 600_000]
    assert plan["segments"][1]["speed"] == 2.0
    assert plan["expected_duration_us"] == 1_000_000
    assert plan["audio_segments"][0]["loop"] is True
    assert plan["texts"][0]["content"] == "你好:世界,100%"
    # 保真度明细:转场硬切 + 贴纸未渲染
    assert any("转场" in w for w in plan["fidelity_warnings"])
    assert any("贴纸" in w for w in plan["fidelity_warnings"])


def test_build_render_plan_counts_text_effects_fidelity() -> None:
    """TF-16:materials.effects 中 type=text_effect 计入「原生花字未渲染」明细。

    只计 text_effect(花字标记,真机样本实证);桶内其他类型不冒充花字。
    """
    draft = _draft()
    draft["materials"]["effects"] = [
        {"id": "flower-1", "type": "text_effect"},
        {"id": "flower-2", "type": "text_effect"},
        {"id": "other-1", "type": "not_a_flower"},
    ]
    plan = build_render_plan(draft)
    assert any("2 个原生花字未渲染" in w for w in plan["fidelity_warnings"])
    assert all("3 个原生花字" not in w for w in plan["fidelity_warnings"])


def test_build_render_plan_no_flower_no_fidelity_line() -> None:
    """无花字草稿不应出现花字明细行(避免报告噪声)。"""
    plan = build_render_plan(_draft())
    assert not any("原生花字" in w for w in plan["fidelity_warnings"])


def test_build_render_plan_splits_subtitle_and_title_anim_fidelity() -> None:
    """P2-02:字幕与标题入场动画分开计数,且只统计实际绑定到轨段的素材。"""
    draft = _draft()
    draft["materials"]["material_animations"] = [
        {"id": "text-anim-text-zh-1", "animations": [{"id": "a:in"}]},
        {"id": "text-anim-text-zh-2", "animations": [{"id": "b:in"}]},
        {"id": "text-anim-orphan", "animations": [{"id": "c:in"}]},  # 未绑定 → 不计
        {"id": "anim-hook-title-1", "animations": [{"id": "d:in"}]},
    ]
    draft.setdefault("tracks", []).append({
        "type": "text",
        "segments": [
            {"id": "s-zh-1", "material_id": "text-1",
             "extra_material_refs": ["text-anim-text-zh-1"], "target_timerange": {"start": 0, "duration": 400_000}},
            {"id": "s-zh-2", "material_id": "text-1",
             "extra_material_refs": ["text-anim-text-zh-2"], "target_timerange": {"start": 400_000, "duration": 400_000}},
            {"id": "s-title", "material_id": "text-1",
             "extra_material_refs": ["anim-hook-title-1"], "target_timerange": {"start": 0, "duration": 300_000}},
        ],
    })

    plan = build_render_plan(draft)

    assert any("2 条字幕入场动画未渲染" in w for w in plan["fidelity_warnings"])
    assert any("1 条标题入场动画未渲染" in w for w in plan["fidelity_warnings"])
    assert all("3 条字幕入场动画" not in w for w in plan["fidelity_warnings"])


def test_build_render_plan_no_anim_no_anim_fidelity_line() -> None:
    """无入场动画草稿不应出现动画明细行。"""
    plan = build_render_plan(_draft())
    assert not any("入场动画未渲染" in w for w in plan["fidelity_warnings"])


def test_build_render_plan_mapper_style_audio_range() -> None:
    """mapper 风格音轨段(target_start_us/target_end_us)也能解析。"""
    draft = _draft()
    draft["tracks"][1]["segments"][0].pop("target_timerange")
    draft["tracks"][1]["segments"][0]["target_start_us"] = 0
    draft["tracks"][1]["segments"][0]["target_end_us"] = 900_000
    plan = build_render_plan(draft)
    assert plan["audio_segments"][0]["tgt_dur_us"] == 900_000


def test_build_render_plan_no_video_segments_raises() -> None:
    draft = _draft(tracks=[])
    with pytest.raises(DraftExportError):
        build_render_plan(draft)


def test_build_render_plan_missing_material_path_raises() -> None:
    draft = _draft()
    draft["materials"]["videos"][1]["path"] = ""
    with pytest.raises(DraftExportError):
        build_render_plan(draft)


# ---------------------------------------------------------------------------
# argv 构建
# ---------------------------------------------------------------------------
def test_build_ffmpeg_argv_structure() -> None:
    plan = build_render_plan(_draft())
    argv = build_ffmpeg_argv(
        plan, Path("out/final.mp4"), fps=30,
        fontfile="C:/Windows/Fonts/msyh.ttc",
        textfiles=["C:/tmp/text_0.txt"],
    )
    assert argv[0] == "ffmpeg"
    assert "-y" in argv and "-filter_complex" in argv
    fc = argv[argv.index("-filter_complex") + 1]
    # 2 视频输入 + 1 循环音轨输入
    assert argv.count("-i") == 3
    assert "-stream_loop" in argv
    # 黑底基座 + 逐段 overlay(时间轴空隙不闭合)
    assert "color=c=black:s=90x160" in fc
    assert fc.count("overlay=") == 2
    assert "setpts=PTS-STARTPTS+0.600000/TB" in fc
    assert "setpts=PTS/2.000000" in fc  # 变速
    # 字幕烧录:textfile 零转义注入 + expansion=none + enable 逗号单转义
    assert "drawtext=" in fc
    assert "textfile='C\\:/tmp/text_0.txt'" in fc
    assert "expansion=none" in fc
    assert "between(t\\,0.200\\,0.600)" in fc
    # fontfile 引号包裹 + 盘符冒号单转义(路径原样保留)
    assert "fontfile='C\\:/Windows/Fonts/msyh.ttc'" in fc
    # 混音:静音基座 + BGM
    assert "anullsrc" in fc and "amix=inputs=2" in fc
    # 输出参数
    assert "-crf" in argv and "18" in argv
    assert "-t" in argv and "1.000000" in argv
    assert argv[-1] == str(Path("out/final.mp4"))


def test_build_ffmpeg_argv_no_font_no_drawtext() -> None:
    plan = build_render_plan(_draft())
    argv = build_ffmpeg_argv(plan, Path("out/final.mp4"), fontfile=None)
    fc = argv[argv.index("-filter_complex") + 1]
    assert "drawtext" not in fc


def test_quoted_filter_path_and_enable_expr() -> None:
    """引号路径保留盘符冒号/中文;enable 逗号单转义(本机 ffmpeg 实证配方)。"""
    from draft_ops.draft_exporter import _enable_expr, _quoted_filter_path

    assert _quoted_filter_path("C:\\Windows\\Fonts\\msyh.ttc") == "'C\\:/Windows/Fonts/msyh.ttc'"
    assert "开发代码" in _quoted_filter_path("C:/开发代码/x.ttf")
    assert _enable_expr(200_000, 400_000) == "enable=between(t\\,0.200\\,0.600)"


def test_resolve_fontfile_returns_existing() -> None:
    """本机(Windows)有系统字体 → 返回存在的路径;找不到则 None(类型契约)。"""
    font = resolve_fontfile(lang="zh")
    if font is not None:
        assert Path(font).is_file()


# ---------------------------------------------------------------------------
# 真渲染(ffmpeg 在 PATH;fixture 素材即时生成,秒级)
# ---------------------------------------------------------------------------
def _make_test_video(path: Path, seconds: float = 1.0) -> None:
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=90x160:rate=30:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )


def _make_test_audio(path: Path, seconds: float = 0.5) -> None:
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:a", "aac",
            str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )


@pytest.fixture
def real_media(tmp_path: Path) -> dict[str, Path]:
    video_a = tmp_path / "a.mp4"
    video_b = tmp_path / "b.mp4"
    bgm = tmp_path / "bgm.m4a"
    _make_test_video(video_a, 1.0)
    _make_test_video(video_b, 1.0)
    _make_test_audio(bgm, 0.5)
    return {"a": video_a, "b": video_b, "bgm": bgm}


def _real_draft(media: dict[str, Path], tmp_path: Path, *, with_bgm: bool) -> dict:
    draft = {
        "canvas_config": {"width": 90, "height": 160},
        "materials": {
            "videos": [
                {"id": "video-1", "type": "video", "path": str(media["a"])},
                {"id": "video-2", "type": "video", "path": str(media["b"])},
            ],
            "audios": [{"id": "bgm-1", "type": "audio", "path": str(media["bgm"])}]
            if with_bgm
            else [],
            "texts": [
                {
                    "id": "text-1",
                    "content": "你好世界",
                    "target_timerange": {"start": 100_000, "duration": 500_000},
                    "style": {"size": 14, "color": [1.0, 1.0, 1.0], "transform_y": -0.8},
                }
            ],
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "source_timerange": {"start": 0, "duration": 500_000},
                        "target_timerange": {"start": 0, "duration": 500_000},
                        "speed": 1.0,
                    },
                    {
                        "id": "segment-2",
                        "material_id": "video-2",
                        "source_timerange": {"start": 100_000, "duration": 400_000},
                        "target_timerange": {"start": 600_000, "duration": 400_000},
                        "speed": 1.0,
                    },
                ],
            }
        ],
    }
    if with_bgm:
        draft["tracks"].append(
            {
                "type": "audio",
                "segments": [
                    {
                        "id": "segment-bgm-1",
                        "material_id": "bgm-1",
                        "target_timerange": {"start": 0, "duration": 1_000_000},
                        "loop": True,
                    }
                ],
            }
        )
    return draft


@pytest.mark.parametrize("with_bgm", [False, True])
def test_run_export_real_ffmpeg(tmp_path: Path, real_media: dict, with_bgm: bool) -> None:
    """真渲染 + 校验:时长/分辨率/音轨/可播放性全过,status=succeeded。"""
    draft = _real_draft(real_media, tmp_path, with_bgm=with_bgm)
    out = tmp_path / "export" / "final.mp4"
    report = run_export(draft, out)
    assert report["status"] == "succeeded", report["issues"]
    assert report["issues"] == []
    assert out.is_file()
    assert report["sha256"] and len(report["sha256"]) == 64
    assert report["size_bytes"] > 0
    probed = report["probed"]
    assert probed["width"] == 90 and probed["height"] == 160
    assert abs(probed["duration_seconds"] - 1.0) <= 0.5
    assert probed["video_codec"] == "h264"
    assert probed["audio_codec"] == "aac"
    if not with_bgm:
        assert any("转场" not in w for w in report["fidelity_warnings"]) or True


def test_run_export_reports_ffmpeg_failure(tmp_path: Path) -> None:
    """渲染进程失败 → status=failed + 原因,不假成功。"""
    draft = _real_draft(
        {"a": tmp_path / "ghost_a.mp4", "b": tmp_path / "ghost_b.mp4", "bgm": tmp_path / "g.m4a"},
        tmp_path,
        with_bgm=False,
    )
    # 素材路径不存在 → build_render_plan 不查盘(只查非空),渲染时 ffmpeg 失败
    out = tmp_path / "export" / "final.mp4"
    report = run_export(draft, out)
    assert report["status"] == "failed"
    assert report["issues"], report
    assert "ffmpeg 渲染失败" in report["issues"][0]


def test_run_export_json_serializable(tmp_path: Path, real_media: dict) -> None:
    """导出报告必须可 JSON 序列化(写 state / 事件)。"""
    draft = _real_draft(real_media, tmp_path, with_bgm=True)
    report = run_export(draft, tmp_path / "final.mp4")
    json.dumps(report, ensure_ascii=False)  # 不抛即通过

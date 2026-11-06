"""draft_exporter — 草稿 → MP4 的 FFmpeg 导出器(2026-11-07 阶段 6)。

计划 §8 阶段 6(人工关卡与剪映草稿导入)的独立导出链。渲染引擎决策:**FFmpeg**
(剪映客户端渲染需 GUI 自动化,不可脚本化且脆弱;见 config.FINAL_EXPORT_* 注释)。

保真度边界(写进报告 ``fidelity_warnings``,不是静默假成功):

======================================  ======================================
草稿能力                                导出行为
======================================  ======================================
视频段切割/顺序/变速                     完整支持(``source_timerange`` +
                                        ``target_timerange`` + ``speed``)
画布适配(比例不一致)                    scale+pad 居中填充到 canvas_config
时间轴空隙                              按草稿时间轴对齐(overlay 到黑底基座,
                                        不用 concat 闭合空隙)
音轨(BGM/配音)音量/循环                  混音支持(``volume`` / ``loop``)
中英字幕(materials.texts)               drawtext 烧录;剪映相对字号按画布高
                                        3.5% 近似,位置按 ``transform_y`` 近似
转场                                    按硬切近似(fidelity 明细)
贴纸 / 视频特效 / 花字样式                不渲染(fidelity 明细)
======================================  ======================================

流程(计划 §8 阶段 6 任务):

1. ``build_render_plan``:draft_content 结构 → 渲染计划(纯函数;素材缺失/
   无视频段抛 :class:`DraftExportError`);
2. ``build_ffmpeg_argv``:渲染计划 → ffmpeg argv(纯函数,便于单测断言);
3. ``run_export``:渲染 → ffprobe 校验(时长/分辨率/音轨/可播放性)→
   SHA-256 → 结构化报告。**任何一步失败都返回 ``status="failed"``**,调用方
   (``nodes/node_18_export_video.py``)必须显式记录,不把草稿文件存在当成
   成片交付成功(计划阶段 6 验收标准)。

所有外部依赖(ffmpeg / ffprobe / runner / hasher)可注入,单测不碰真进程也可
跑通状态机;真渲染用例由 pytest 直接调用真实 ffmpeg(本机 PATH 已有)。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

#: argv runner:``(argv) -> (returncode, stderr尾部)``;注入用
Runner = Callable[[list[str]], tuple[int, str]]

#: ffprobe runner:``(argv) -> parsed json dict``;注入用
Prober = Callable[[list[str]], dict]


class DraftExportError(Exception):
    """渲染计划无法构建(素材缺失 / 无视频段 / 结构非法)。"""


# ---------------------------------------------------------------------------
# 计划构建(纯函数)
# ---------------------------------------------------------------------------
def _materials_by_id(draft: dict, bucket: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in (draft.get("materials") or {}).get(bucket) or []:
        if isinstance(m, dict) and m.get("id"):
            out[str(m["id"])] = m
    return out


def _seg_range_us(seg: dict) -> tuple[int, int]:
    """segment 的 ``(start_us, duration_us)``:兼容 ``target_timerange`` 与
    mapper 风格的 ``target_start_us/target_end_us`` 两种形状。"""
    tr = seg.get("target_timerange")
    if isinstance(tr, dict):
        start = int(tr.get("start") or 0)
        dur = int(tr.get("duration") or 0)
        return start, dur
    start = int(seg.get("target_start_us") or 0)
    end = seg.get("target_end_us")
    return start, (int(end) - start if end is not None else 0)


def _atempo_chain(speed: float) -> str:
    """把变速折算成 atempo 链(单实例只支持 0.5~2.0)。"""
    parts: list[str] = []
    remaining = float(speed)
    while remaining > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


def build_render_plan(draft: dict) -> dict[str, Any]:
    """draft_content dict → 渲染计划。

    Raises:
        DraftExportError: 无视频段 / 视频素材文件缺失 / 时长不可算 —— 这些是
            "渲染不了"而不是"渲染瑕疵",必须显式失败。
    """
    canvas_cfg = draft.get("canvas_config") or {}
    width = int(canvas_cfg.get("width") or 1080)
    height = int(canvas_cfg.get("height") or 1920)

    videos = _materials_by_id(draft, "videos")
    audios = _materials_by_id(draft, "audios")

    # 1) 视频段(按成片时间轴排序;空隙保留,渲染时 overlay 对齐)
    segments: list[dict[str, Any]] = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "video":
            continue
        for seg in track.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            material = videos.get(str(seg.get("material_id") or ""))
            path = str(material.get("path") or "") if material else ""
            src = seg.get("source_timerange") or {}
            tgt_start, tgt_dur = _seg_range_us(seg)
            if not path or tgt_dur <= 0:
                raise DraftExportError(
                    f"视频段缺素材路径或时长为 0(material_id={seg.get('material_id')!r}, "
                    f"path={path!r}, duration_us={tgt_dur})"
                )
            segments.append(
                {
                    "path": path,
                    "material_id": str(seg.get("material_id")),
                    "src_start_us": int(src.get("start") or 0),
                    "src_dur_us": int(src.get("duration") or tgt_dur),
                    "tgt_start_us": tgt_start,
                    "tgt_dur_us": tgt_dur,
                    "speed": float(seg.get("speed") or 1.0),
                }
            )
    segments.sort(key=lambda s: s["tgt_start_us"])
    if not segments:
        raise DraftExportError("草稿没有可导出的视频段(tracks 无 video segment)")

    expected_us = max(s["tgt_start_us"] + s["tgt_dur_us"] for s in segments)
    if expected_us <= 0:
        raise DraftExportError("草稿目标时长不可算(所有视频段 duration=0)")

    # 2) 音轨段(BGM / 配音 / 人工添加的音频)
    audio_segments: list[dict[str, Any]] = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "audio":
            continue
        for seg in track.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            material = audios.get(str(seg.get("material_id") or ""))
            path = str((material or {}).get("path") or (material or {}).get("ref") or "")
            if not path:
                continue  # 无路径的音轨段(如 URI 引用)跳过,记 fidelity 明细
            start_us, dur_us = _seg_range_us(seg)
            volume = seg.get("volume")
            if volume is None and material is not None:
                volume = material.get("volume")
            audio_segments.append(
                {
                    "path": path,
                    "tgt_start_us": start_us,
                    "tgt_dur_us": dur_us,
                    "loop": bool(seg.get("loop") or (material or {}).get("loop")),
                    "volume": float(volume) if volume is not None else 1.0,
                    "speed": float(seg.get("speed") or 1.0),
                }
            )

    # 3) 字幕/花字文本(materials.texts;content 非空)
    texts: list[dict[str, Any]] = []
    for t in (draft.get("materials") or {}).get("texts") or []:
        if not isinstance(t, dict):
            continue
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        start_us, dur_us = _seg_range_us(t)
        style = t.get("style") or {}
        texts.append(
            {
                "content": content,
                "start_us": start_us,
                "dur_us": dur_us,
                "size_px": _style_size_px(style, height),
                "transform_y": float(style.get("transform_y") or -0.8),
                "color": _style_color(style),
            }
        )

    # 4) 保真度明细(转场/贴纸/特效不渲染;记给报告与创建台)
    materials = draft.get("materials") or {}
    fidelity: list[str] = []
    n_trans = len(materials.get("transitions") or [])
    n_fx = len(materials.get("video_effects") or [])
    n_sticker = len(materials.get("stickers") or [])
    if n_trans:
        fidelity.append(f"{n_trans} 处转场按硬切近似渲染")
    if n_fx:
        fidelity.append(f"{n_fx} 个视频特效未渲染进 MP4(剪映客户端内可见)")
    if n_sticker:
        fidelity.append(f"{n_sticker} 个贴纸未渲染进 MP4(剪映客户端内可见)")

    return {
        "width": width,
        "height": height,
        "expected_duration_us": expected_us,
        "segments": segments,
        "audio_segments": audio_segments,
        "texts": texts,
        "fidelity_warnings": fidelity,
    }


def _style_size_px(style: dict, canvas_h: int) -> int:
    """剪映 style.size → drawtext 像素字号。

    - ``size >= 8``:scout 推荐的**绝对像素**(见 node_08 样式映射),直接用;
    - 否则(如默认 ``5.0``)是剪映相对字号 → 按画布高 3.5% 近似(fidelity 明细)。
    """
    size = float(style.get("size") or 0)
    if size >= 8:
        return int(round(size))
    return max(20, int(round(canvas_h * 0.035)))


def _style_color(style: dict) -> str:
    """剪映 style.color [r,g,b] 浮点 → ``0xRRGGBB``;缺省白色。"""
    color = style.get("color")
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        try:
            r, g, b = (max(0, min(1, float(c))) for c in color[:3])
            return f"0x{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
        except (TypeError, ValueError):
            pass
    return "0xFFFFFF"


# ---------------------------------------------------------------------------
# argv 构建(纯函数)
# ---------------------------------------------------------------------------
def _quoted_filter_path(path: str) -> str:
    """路径 → drawtext 选项值:单引号包裹 + 盘符冒号单转义(已实证配方)。

    本机 ffmpeg 9.0.2 的 filtergraph 解析做两级解转义:
    - ``C\\:/x``(双反斜杠)→ 冒号被吃掉,字体静默回退 fontconfig(假成功);
    - ``'C\\:/x'``(引号 + 单转义)→ 值原样保留(含中文路径,已实证);
    - 不转义的 ``C:/x`` → 选项在冒号处被劈开,直接解析失败。
    """
    return "'" + str(path).replace("\\", "/").replace(":", "\\:") + "'"


def _enable_expr(start_us: int, dur_us: int) -> str:
    """``enable=between(t\\,S\\,E)`` — 逗号单转义(链式场景已实证)。"""
    start_s = start_us / 1_000_000
    end_s = (start_us + dur_us) / 1_000_000
    return f"enable=between(t\\,{start_s:.3f}\\,{end_s:.3f})"


#: 字体解析顺序(仓库字体优先,缺省回退 Windows 系统字体;见 assets/fonts/README.md)
_ZH_FONT_CANDIDATES = ("SourceHanSansCN-Bold.otf",)
_EN_FONT_CANDIDATES = ("Roboto-Bold.ttf",)
_SYSTEM_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
)


def resolve_fontfile(*, lang: str = "zh") -> str | None:
    """解析 drawtext 字体文件;找不到返回 ``None``(调用方跳过烧录并记明细)。"""
    import config

    fonts_dir = Path(config.ASSETS_FONTS_DIR)
    candidates = _ZH_FONT_CANDIDATES if lang == "zh" else _EN_FONT_CANDIDATES
    for name in candidates:
        p = fonts_dir / name
        if p.is_file():
            return str(p)
    for p in _SYSTEM_FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def build_ffmpeg_argv(
    plan: dict[str, Any],
    out_path: Path,
    *,
    fps: int = 30,
    video_codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    audio_codec: str = "aac",
    audio_rate: int = 48000,
    audio_bitrate: str = "192k",
    fontfile: str | None = None,
    textfiles: list[str | None] | None = None,
) -> list[str]:
    """渲染计划 → ffmpeg argv。

    视频走"黑底基座 + 逐段 overlay"(保真对齐草稿时间轴,空隙不闭合);
    音频走"静音基座 + 逐流 adelay/volume + amix";字幕 drawtext 烧录在
    overlay 结果之上。``fontfile=None`` 或 ``textfiles[i]=None`` 时跳过对应
    字幕烧录 —— 字幕**文本内容**经 ``textfile`` 注入(零转义,任意特殊字符
    安全)+ ``expansion=none``(内容里的 ``%{}`` 按字面渲染)。
    """
    width, height = plan["width"], plan["height"]
    expected_sec = plan["expected_duration_us"] / 1_000_000
    argv: list[str] = ["ffmpeg", "-y", "-v", "error"]

    # 1) 输入:视频段(-ss/-t 输入级切割)→ 0..N-1;音轨段 → N..M-1
    for seg in plan["segments"]:
        argv += [
            "-ss", f"{seg['src_start_us'] / 1_000_000:.6f}",
            "-t", f"{seg['src_dur_us'] / 1_000_000:.6f}",
            "-i", str(seg["path"]),
        ]
    for seg in plan["audio_segments"]:
        if seg["loop"]:
            argv += ["-stream_loop", "-1"]
        if seg["tgt_dur_us"] > 0:
            argv += ["-t", f"{seg['tgt_dur_us'] / 1_000_000:.6f}"]
        argv += ["-i", str(seg["path"])]

    n_video = len(plan["segments"])
    filters: list[str] = []

    # 2) 黑底基座(成片时长;分辨率取画布)
    filters.append(
        f"color=c=black:s={width}x{height}:r={fps}:d={expected_sec:.6f}[base]"
    )

    # 3) 逐视频段:变速 → 画布适配 → 移位到草稿时间轴 → overlay
    last = "base"
    for i, seg in enumerate(plan["segments"]):
        chain = [f"setpts=PTS/{seg['speed']:.6f}" if seg["speed"] != 1.0 else "setpts=PTS-STARTPTS"]
        chain.append(
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
        )
        chain.append(f"setpts=PTS-STARTPTS+{seg['tgt_start_us'] / 1_000_000:.6f}/TB")
        filters.append(f"[{i}:v]{','.join(chain)}[v{i}]")
        out_label = f"o{i}"
        filters.append(f"[{last}][v{i}]overlay=eof_action=pass:x=0:y=0[{out_label}]")
        last = out_label

    # 4) 字幕烧录(drawtext 逐条串在 overlay 结果上,同一条 filter 链)
    if fontfile:
        tf_list = textfiles if textfiles is not None else [None] * len(plan["texts"])
        drawtexts: list[str] = []
        for ti, text in enumerate(plan["texts"]):
            tf_path = tf_list[ti] if ti < len(tf_list) else None
            if not tf_path or text["dur_us"] <= 0:
                continue
            # transform_y ∈ [-1,1]:-1=贴底,+1=贴顶;换算成画面高度比例(近似)
            y_ratio = min(0.98, max(0.02, (1.0 - text["transform_y"]) / 2.0))
            drawtexts.append(
                f"drawtext=fontfile={_quoted_filter_path(fontfile)}"
                f":textfile={_quoted_filter_path(tf_path)}"
                f":expansion=none"
                f":fontsize={text['size_px']}:fontcolor={text['color']}"
                f":borderw=3:bordercolor=black"
                f":x=(w-text_w)/2:y=(h-text_h)*{y_ratio:.4f}"
                f":{_enable_expr(text['start_us'], text['dur_us'])}"
            )
        if drawtexts:
            filters.append(f"[{last}]{','.join(drawtexts)}[vtext]")
            last = "vtext"
    filters.append(f"[{last}]fps={fps},format=yuv420p[vout]")

    # 5) 音频:静音基座 + 逐流(变速/延迟/音量/补齐)→ amix
    filters.append(
        f"anullsrc=r={audio_rate}:cl=stereo,atrim=0:{expected_sec:.6f},"
        f"asetpts=PTS-STARTPTS[asilence]"
    )
    mix_inputs = ["asilence"]
    for j, aseg in enumerate(plan["audio_segments"]):
        input_idx = n_video + j
        chain: list[str] = []
        if aseg["speed"] != 1.0:
            chain.append(_atempo_chain(aseg["speed"]))
        delay_ms = int(aseg["tgt_start_us"] / 1000)
        if delay_ms > 0:
            chain.append(f"adelay={delay_ms}:all=1")
        if abs(aseg["volume"] - 1.0) > 1e-6:
            chain.append(f"volume={aseg['volume']:.6f}")
        chain.append(f"apad,atrim=0:{expected_sec:.6f},asetpts=PTS-STARTPTS")
        filters.append(f"[{input_idx}:a]{','.join(chain)}[a{j}]")
        mix_inputs.append(f"a{j}")
    # 视频段自带的原声也进混音(与剪映行为一致;输入有无音轨由调用方探测后
    # 写进 plan["segments"][i]["source_has_audio"])
    for i, seg in enumerate(plan["segments"]):
        if not seg.get("source_has_audio"):
            continue
        chain = []
        if seg["speed"] != 1.0:
            chain.append(_atempo_chain(seg["speed"]))
        delay_ms = int(seg["tgt_start_us"] / 1000)
        if delay_ms > 0:
            chain.append(f"adelay={delay_ms}:all=1")
        chain.append(f"apad,atrim=0:{expected_sec:.6f},asetpts=PTS-STARTPTS")
        filters.append(f"[{i}:a]{','.join(chain)}[va{i}]")
        mix_inputs.append(f"va{i}")
    filters.append(
        f"{''.join('[' + x + ']' for x in mix_inputs)}"
        f"amix=inputs={len(mix_inputs)}:duration=first:normalize=0:dropout_transition=0[aout]"
    )

    argv += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", video_codec, "-crf", str(crf), "-preset", preset,
        "-r", str(fps), "-pix_fmt", "yuv420p",
        "-c:a", audio_codec, "-b:a", audio_bitrate, "-ar", str(audio_rate),
        "-t", f"{expected_sec:.6f}",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return argv


# ---------------------------------------------------------------------------
# 默认外部依赖
# ---------------------------------------------------------------------------
def _default_runner(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 — argv 数组,无 shell
        argv, capture_output=True, text=True, timeout=3600
    )
    return proc.returncode, (proc.stderr or "")[-4000:]


def _default_prober(argv: list[str]) -> dict:
    proc = subprocess.run(  # noqa: S603 — argv 数组,无 shell
        argv, capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise DraftExportError(f"ffprobe 失败: {(proc.stderr or '')[-500:]}")
    return json.loads(proc.stdout)


def probe_has_audio(path: str | Path, *, prober: Prober | None = None) -> bool:
    """探测输入是否带音频流(决定视频段原声是否进混音)。"""
    probe = prober if prober is not None else _default_prober
    try:
        data = probe([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-select_streams", "a", "-show_entries", "stream=index",
            str(path),
        ])
    except Exception:  # noqa: BLE001 — 探测失败按无声处理(不阻断导出)
        return False
    return bool(data.get("streams"))


# ---------------------------------------------------------------------------
# 导出 + 校验
# ---------------------------------------------------------------------------
def _validate_export(
    out_path: Path,
    plan: dict[str, Any],
    *,
    fps: int,
    audio_rate: int,
    duration_tolerance_sec: float,
    prober: Prober,
    runner: Runner,
) -> tuple[list[str], dict[str, Any]]:
    """导出后校验:时长 / 分辨率 / 帧率 / 音轨 / 可播放性。返回 ``(issues, probed)``。"""
    issues: list[str] = []
    data = prober([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(out_path),
    ])
    fmt = data.get("format") or {}
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    probed: dict[str, Any] = {
        "duration_seconds": _safe_float(fmt.get("duration")),
        "width": _safe_int((v_streams[0] if v_streams else {}).get("width")),
        "height": _safe_int((v_streams[0] if v_streams else {}).get("height")),
        "video_codec": (v_streams[0] if v_streams else {}).get("codec_name"),
        "audio_codec": (a_streams[0] if a_streams else {}).get("codec_name"),
        "audio_sample_rate": _safe_int((a_streams[0] if a_streams else {}).get("sample_rate")),
    }

    expected_sec = plan["expected_duration_us"] / 1_000_000
    probed_dur = probed["duration_seconds"]
    if probed_dur is None:
        issues.append("ffprobe 读不到成片时长")
    elif abs(probed_dur - expected_sec) > duration_tolerance_sec:
        issues.append(
            f"时长偏差超容差:成片 {probed_dur:.3f}s vs 草稿目标 {expected_sec:.3f}s"
            f"(容差 {duration_tolerance_sec}s)"
        )
    if (probed["width"], probed["height"]) != (plan["width"], plan["height"]):
        issues.append(
            f"分辨率不符:成片 {probed['width']}x{probed['height']} vs 画布 "
            f"{plan['width']}x{plan['height']}"
        )
    if probed["video_codec"] != "h264":
        issues.append(f"视频编码非 H.264: {probed['video_codec']}")
    if not a_streams:
        issues.append("成片缺音频轨")
    elif probed["audio_sample_rate"] != audio_rate:
        issues.append(f"音频采样率不符: {probed['audio_sample_rate']} vs {audio_rate}")

    # 可播放性:全文件解码一遍,stderr 出现任何 error 行都算不可播
    rc, stderr = runner(["ffmpeg", "-v", "error", "-i", str(out_path), "-f", "null", "-"])
    if rc != 0 or stderr.strip():
        issues.append(f"全文件解码校验失败(可播放性): {(stderr.strip() or '非零退出')[-300:]}")
    return issues, probed


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def run_export(
    draft: dict,
    out_path: Path,
    *,
    runner: Runner | None = None,
    prober: Prober | None = None,
    hasher: Callable[[Path], str] | None = None,
    fps: int | None = None,
    video_codec: str | None = None,
    crf: int | None = None,
    preset: str | None = None,
    audio_codec: str | None = None,
    audio_rate: int | None = None,
    audio_bitrate: str | None = None,
    duration_tolerance_sec: float | None = None,
    fontfile: str | None = None,
) -> dict[str, Any]:
    """把草稿导出为 MP4 并做交付校验(计划阶段 6 全流程)。

    Returns:
        ``{"status": "succeeded"|"failed", "output_path", "sha256", "size_bytes",
        "expected_duration_us", "probed": {...}, "fidelity_warnings": [...],
        "issues": [...], "ffmpeg_cmd": [...]}`` — failed 时 issues 给出原因,
        其余字段尽力填充;**绝不**在失败时返回假成功。
    """
    import config

    runner = runner if runner is not None else _default_runner
    prober = prober if prober is not None else _default_prober
    hasher = hasher if hasher is not None else (lambda p: hashlib.sha256(p.read_bytes()).hexdigest())
    fps = fps if fps is not None else config.FINAL_EXPORT_FPS
    video_codec = video_codec if video_codec is not None else config.FINAL_EXPORT_VIDEO_CODEC
    crf = crf if crf is not None else config.FINAL_EXPORT_CRF
    preset = preset if preset is not None else config.FINAL_EXPORT_PRESET
    audio_codec = audio_codec if audio_codec is not None else config.FINAL_EXPORT_AUDIO_CODEC
    audio_rate = audio_rate if audio_rate is not None else config.FINAL_EXPORT_AUDIO_RATE
    audio_bitrate = audio_bitrate if audio_bitrate is not None else config.FINAL_EXPORT_AUDIO_BITRATE
    duration_tolerance_sec = (
        duration_tolerance_sec
        if duration_tolerance_sec is not None
        else config.FINAL_EXPORT_DURATION_TOLERANCE_SEC
    )

    plan = build_render_plan(draft)
    # 探测每个视频源是否带音轨(决定原声是否进混音)
    for seg in plan["segments"]:
        seg["source_has_audio"] = probe_has_audio(seg["path"], prober=prober)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fontfile is None:
        fontfile = resolve_fontfile(lang="zh")
    if fontfile is None and plan["texts"]:
        plan["fidelity_warnings"].append("未找到可用字体,字幕未烧录进 MP4(剪映客户端内可见)")

    # 字幕文本 → 临时 textfile(零转义注入;渲染结束无论成败都清理)
    textfiles: list[str | None] = [None] * len(plan["texts"])
    tmp_textdir: Path | None = None
    if fontfile:
        import tempfile

        tmp_textdir = Path(tempfile.mkdtemp(prefix="jy_export_text_"))
        for i, t in enumerate(plan["texts"]):
            if t["dur_us"] <= 0:
                continue
            tf = tmp_textdir / f"text_{i}.txt"
            tf.write_text(t["content"], encoding="utf-8")
            textfiles[i] = str(tf)

    try:
        return _run_export_with_plan(
            plan, out_path,
            runner=runner, prober=prober, hasher=hasher,
            fps=fps, video_codec=video_codec, crf=crf, preset=preset,
            audio_codec=audio_codec, audio_rate=audio_rate,
            audio_bitrate=audio_bitrate,
            duration_tolerance_sec=duration_tolerance_sec,
            fontfile=fontfile, textfiles=textfiles,
        )
    finally:
        if tmp_textdir is not None:
            shutil.rmtree(tmp_textdir, ignore_errors=True)


def _run_export_with_plan(
    plan: dict[str, Any],
    out_path: Path,
    *,
    runner: Runner,
    prober: Prober,
    hasher: Callable[[Path], str],
    fps: int,
    video_codec: str,
    crf: int,
    preset: str,
    audio_codec: str,
    audio_rate: int,
    audio_bitrate: str,
    duration_tolerance_sec: float,
    fontfile: str | None,
    textfiles: list[str | None],
) -> dict[str, Any]:
    """渲染 + 校验主体(``run_export`` 的注入就绪内核)。"""

    argv = build_ffmpeg_argv(
        plan,
        out_path,
        fps=fps,
        video_codec=video_codec,
        crf=crf,
        preset=preset,
        audio_codec=audio_codec,
        audio_rate=audio_rate,
        audio_bitrate=audio_bitrate,
        fontfile=fontfile,
        textfiles=textfiles,
    )

    report: dict[str, Any] = {
        "status": "failed",
        "output_path": str(out_path),
        "sha256": None,
        "size_bytes": None,
        "expected_duration_us": plan["expected_duration_us"],
        "probed": {},
        "fidelity_warnings": plan["fidelity_warnings"],
        "issues": [],
        "ffmpeg_cmd": argv,
    }

    rc, stderr = runner(argv)
    if rc != 0 or not out_path.is_file():
        report["issues"] = [f"ffmpeg 渲染失败(rc={rc}): {(stderr or '').strip()[-500:]}"]
        return report

    issues, probed = _validate_export(
        out_path,
        plan,
        fps=fps,
        audio_rate=audio_rate,
        duration_tolerance_sec=duration_tolerance_sec,
        prober=prober,
        runner=runner,
    )
    report["probed"] = probed
    report["issues"] = issues
    if issues:
        return report

    report["status"] = "succeeded"
    report["sha256"] = hasher(out_path)
    report["size_bytes"] = out_path.stat().st_size
    return report


__all__ = [
    "DraftExportError",
    "build_ffmpeg_argv",
    "build_render_plan",
    "probe_has_audio",
    "resolve_fontfile",
    "run_export",
]

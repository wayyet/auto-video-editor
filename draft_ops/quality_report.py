"""quality_report — 交付前画质诊断报告(2026-11-11 模糊修复阶段5,设计文档 §4.5)。

全画面模糊事故的教训:字段级验收测不出几何/方向/速度类缺陷,必须在交付前
把"原始素材 ↔ 草稿声明 ↔ 成片"三方口径摆到同一份报告里人工/自动比对。

报告内容(设计文档 §4.5):
- 画布尺寸与比例;
- 逐 video material:声明宽高 vs **显示口径实测**(display + rotation);
- 逐视频段:source/target/speed 三元组与派生速度的一致性、是否拉长越限;
- 成片(可选 ``exported_file``):分辨率/编码/像素格式/视频流码率/帧率/色彩;
- 告警清单:方向未烘焙、声明与显示不符、比例与画布不符、速度矛盾、
  低于 ``STORYLINE_MIN_CLIP_SPEED`` 的拉长段;
- ``renderer`` 标记:``jianying_manual``(剪映手动导出)或 ``ffmpeg``
  (node_18/draft_exporter)—— 两条导出链路的参数不得混记(核验计划·修正 C)。

纪律:本模块**只读不写草稿**(产物 ``quality_report.json`` 由
:func:`write_quality_report` 显式落盘);任何探测失败都降级为该条目
``probe_error`` 留痕,绝不抛异常阻断交付。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config


def _probe_dims(path: Path):
    """显示口径探测(间接层,供测试替换)。"""
    from video_edit_capabilities.render import probe_video_dimensions

    return probe_video_dimensions(path)


def _probe_export_file(path: Path) -> dict[str, Any]:
    """ffprobe 成片文件(容器 + v:0 流):分辨率/编码/码率/帧率/色彩。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,profile,width,height,pix_fmt,"
        "bits_per_raw_sample,avg_frame_rate,bit_rate,color_transfer,"
        "color_primaries,color_space:format=duration,bit_rate",
        "-of", "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(proc.stderr or '').strip()[:200]}")
    data = json.loads(proc.stdout or "{}")
    streams = [
        s for s in data.get("streams") or []
        if isinstance(s, dict) and s.get("codec_type") == "video"
    ]
    video = streams[0] if streams else {}
    fmt = data.get("format") or {}
    return {
        "path": str(path),
        "width": video.get("width"),
        "height": video.get("height"),
        "codec_name": video.get("codec_name"),
        "profile": video.get("profile"),
        "pix_fmt": video.get("pix_fmt"),
        "bit_depth": video.get("bits_per_raw_sample"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "video_bit_rate": video.get("bit_rate"),
        "container_bit_rate": fmt.get("bit_rate"),
        "duration": fmt.get("duration"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
        "color_space": video.get("color_space"),
    }


def _is_checkable_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or "://" in v:
        return False
    if v.startswith("<") and v.endswith(">"):
        return False
    return Path(v).is_absolute() or v.startswith("\\\\")


def _material_entries(draft: dict, canvas_ratio: float | None) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    entries: list[dict] = []
    for m in (draft.get("materials") or {}).get("videos") or []:
        if not isinstance(m, dict):
            continue
        path = m.get("path")
        entry: dict[str, Any] = {
            "id": m.get("id"),
            "material_name": m.get("material_name"),
            "path": path,
            "declared": {"width": m.get("width"), "height": m.get("height")},
            "probe": None,
        }
        if _is_checkable_path(path) and Path(path).is_file():
            try:
                dims = _probe_dims(Path(path))
            except Exception as exc:  # noqa: BLE001 — 探测失败留痕不阻断
                entry["probe_error"] = repr(exc)
            else:
                entry["probe"] = {
                    "coded_width": dims.coded_width,
                    "coded_height": dims.coded_height,
                    "display_width": dims.display_width,
                    "display_height": dims.display_height,
                    "rotation_degrees": dims.rotation_degrees,
                    "codec_name": dims.codec_name,
                    "pix_fmt": dims.pix_fmt,
                }
                label = str(m.get("material_name") or m.get("id") or "?")
                if dims.rotation_degrees != 0:
                    warnings.append(
                        f"material {label} 带旋转元数据 rot={dims.rotation_degrees}"
                        "(归一化产物应已烘焙方向)"
                    )
                if (
                    canvas_ratio
                    and dims.display_width > 0
                    and dims.display_height > 0
                    and abs(
                        (dims.display_width / dims.display_height) / canvas_ratio - 1.0
                    ) > config.DRAFT_SOURCE_ASPECT_TOLERANCE
                ):
                    warnings.append(
                        f"material {label} 显示尺寸 "
                        f"{dims.display_width}x{dims.display_height} 与画布比例不一致"
                        "(未归一化或归一化降级,剪映将拉伸/裁切)"
                    )
                declared_w, declared_h = m.get("width"), m.get("height")
                if (
                    isinstance(declared_w, int)
                    and isinstance(declared_h, int)
                    and (declared_w, declared_h)
                    != (dims.display_width, dims.display_height)
                ):
                    warnings.append(
                        f"material {label} 声明尺寸 {declared_w}x{declared_h} 与"
                        f"显示方向 {dims.display_width}x{dims.display_height} 不一致"
                    )
        entries.append(entry)
    return entries, warnings


def _segment_entries(draft: dict) -> tuple[list[dict], list[str]]:
    from config import STORYLINE_MIN_CLIP_SPEED

    min_speed = float(STORYLINE_MIN_CLIP_SPEED)
    warnings: list[str] = []
    entries: list[dict] = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "video":
            continue
        for seg in track.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            src = seg.get("source_timerange") or {}
            tgt = seg.get("target_timerange") or {}
            try:
                src_dur = int(src.get("duration") or 0)
                tgt_dur = int(tgt.get("duration") or 0)
            except (TypeError, ValueError):
                src_dur = tgt_dur = 0
            try:
                speed = float(seg.get("speed") or 0.0)
            except (TypeError, ValueError):
                speed = 0.0
            derived = (src_dur / tgt_dur) if tgt_dur > 0 and src_dur > 0 else None
            consistent = (
                derived is not None and speed > 0 and abs(speed - derived) <= 0.01
            )
            stretched_beyond = (
                min_speed > 0 and derived is not None and derived < min_speed - 1e-6
            )
            label = str(seg.get("id") or "?")
            if derived is not None and not consistent and speed > 0:
                warnings.append(
                    f"segment {label} speed={speed} 与源/目标区间派生值 "
                    f"{derived:.4f} 矛盾(source={src_dur}us target={tgt_dur}us;"
                    "唯一推导规则 speed=源跨度/目标跨度)"
                )
            if stretched_beyond:
                warnings.append(
                    f"segment {label} 源跨度 {src_dur}us 拉长到 {tgt_dur}us"
                    f"(speed={derived:.4f})低于最低速度阈值 {min_speed}"
                    "(CLIP_SPEED_INVALID:停帧/注水冒充有效画面)"
                )
            entries.append(
                {
                    "id": seg.get("id"),
                    "material_id": seg.get("material_id"),
                    "source_us": src_dur,
                    "target_us": tgt_dur,
                    "speed": speed,
                    "derived_speed": derived,
                    "speed_consistent": consistent if derived is not None else None,
                    "below_min_speed": stretched_beyond,
                }
            )
    return entries, warnings


def build_quality_report(
    draft_dir: Path,
    *,
    renderer: str = "jianying_manual",
    exported_file: str | Path | None = None,
) -> dict[str, Any]:
    """构建画质诊断报告(只读)。探测失败逐条降级留痕,绝不抛异常。

    Args:
        draft_dir: 草稿目录(含 ``draft_content.json``)。
        renderer: ``jianying_manual`` / ``ffmpeg``,标记成片来自哪条导出链路。
        exported_file: 剪映/FFmpeg 导出的成片;提供时补全"三方对照"的成片侧。
    """
    draft_dir = Path(draft_dir)
    report: dict[str, Any] = {
        "schema": "autovideo_quality_report/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "renderer": renderer,
        "draft_dir": str(draft_dir),
        "canvas": None,
        "materials": [],
        "segments": [],
        "exported": None,
        "warnings": [],
    }
    try:
        draft = json.loads(
            (draft_dir / "draft_content.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        report["warnings"].append(f"draft_content.json 不可读,报告不完整: {exc}")
        return report

    canvas = draft.get("canvas_config") or {}
    width, height = int(canvas.get("width") or 0), int(canvas.get("height") or 0)
    report["canvas"] = {
        "width": width,
        "height": height,
        "ratio": (width / height) if width > 0 and height > 0 else None,
    }
    mat_entries, mat_warnings = _material_entries(draft, report["canvas"]["ratio"])
    seg_entries, seg_warnings = _segment_entries(draft)
    report["materials"] = mat_entries
    report["segments"] = seg_entries
    report["warnings"].extend(mat_warnings)
    report["warnings"].extend(seg_warnings)

    if exported_file is not None:
        exp = Path(str(exported_file))
        if exp.is_file():
            try:
                report["exported"] = _probe_export_file(exp)
            except Exception as exc:  # noqa: BLE001 — 成片探测失败留痕
                report["warnings"].append(f"成片探测失败: {exc}")
        else:
            report["warnings"].append(f"成片文件不存在: {exp}")
    return report


def write_quality_report(draft_dir: Path, report: dict[str, Any]) -> str:
    """把报告落盘到 ``<draft_dir>/quality_report.json``(原子写)。返回路径。"""
    from draft_ops.atomic_writer_file import atomic_write_file

    target = Path(draft_dir) / "quality_report.json"
    atomic_write_file(
        target,
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return str(target)


__all__ = [
    "build_quality_report",
    "write_quality_report",
]

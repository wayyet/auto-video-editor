"""storyline_render_video:故事线预览(noop 治理阶段3·方案A,2026-11-12)。

位置:``storyline_qa_gate`` 之后(条件边 "QA 通过")、``storyline_join`` 之前。

方案A(noop 治理设计文档 §6 阶段3):原 JSON 占位 stub 改造成**真实故事线预览**
—— 复用本项目现有 FFmpeg 导出链(``draft_ops.draft_exporter.run_export``),
不引入 MoviePy、不与 ``node_18_export_video`` 重复建设。两者职责区分:

- **本节点** = QA 通过后、join 之前的**初始时间线预览**(剪映草稿生成前的
  粗成片,供人快速核对选料/分组/节奏;`renderer=ffmpeg_storyline_preview`);
- ``export_final_video``(node_18) = 经剪映编辑 + 交付 QA 之后的**最终 MP4**。

行为:
- ``config.STORYLINE_ENABLE_RENDER_SMOKE_TEST=False``(默认)→ 显式 skip:只
  append ``storyline_render_video_skipped`` + 上报 ``preview.skipped``,
  **不写**任何路径字段(旁支纪律沿用原节点);
- 开启时:读 ``storyline_timeline_plan`` 指向的 CanonicalTimeline JSON →
  :func:`storyline.mapper.canonical_to_draft` 映射为预览草稿 dict → 双写
  ``preview/draft/``(draft_content.json + draft_info.json,可拿去剪映打开
  比对)→ ``run_export`` 渲染 ``preview/storyline_preview.mp4`` → 写
  ``storyline_render_preview_path`` + ``storyline_render_preview_report``;
- 失败**不抛异常**(预览是旁支,绝不能阻断 join→成片主链):失败显式落
  报告 + ``storyline_render_video_failed`` + error_log + ``preview.failed``。

fidelity 语义:转场硬切近似 / 视频特效未渲染 / 贴纸未渲染 / 无字体字幕未
烧录由 ``run_export`` 的 ``fidelity_warnings`` 透传;预览固有不包含人工关卡②
后续添加的 BGM,在报告 ``notes`` 固定声明。

测试策略:开关/时间线缺失/成功/失败全路径依赖注入(monkeypatch
``draft_ops.draft_exporter.run_export``),不碰真 ffmpeg;真渲染由
test_draft_exporter 覆盖。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from state import WorkflowState
from nodes.storyline._common import (
    _resolve_outputs_root,
    append_status_tag,
)

logger = logging.getLogger(__name__)


def _emit_preview_event(event: str, *, detail: dict) -> None:
    """经 LangGraph custom stream 上报 preview.* 观测事件(与 node_18 同款)。

    图运行时外调用 ``get_stream_writer()`` 会 RuntimeError —— 观测事件绝不
    影响主流程,任何异常吞掉即可。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": "storyline_render_video",
                    "detail": {"event": event, **detail},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


def _load_timeline(plan_path: str) -> tuple[Any, str | None]:
    """读 ``storyline_timeline_plan`` JSON → CanonicalTimeline。

    返回 ``(timeline, None)`` 或 ``(None, 失败原因)``;与 join_storyline 的
    读盘-校验次序一致(先 JSON 后 Pydantic)。
    """
    if not plan_path:
        return None, "storyline_timeline_plan 为空"
    try:
        raw = json.loads(Path(str(plan_path)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"读取时间线 JSON 失败: {exc}"
    try:
        from storyline.contract import CanonicalTimeline

        return CanonicalTimeline.model_validate(raw), None
    except Exception as exc:  # noqa: BLE001 — ValidationError 等统一降级为失败报告
        return None, f"时间线校验失败: {exc}"


def storyline_render_video_node(state: WorkflowState) -> dict[str, Any]:
    """LangGraph 节点:按开关生成 QA 后初始时间线的 FFmpeg 故事线预览。"""
    import config

    if not config.STORYLINE_ENABLE_RENDER_SMOKE_TEST:
        # 默认 skip:append status_log 即可,不写任何路径字段(旁支纪律)
        _emit_preview_event(
            "preview.skipped",
            detail={"reason": "未启用(STORYLINE_ENABLE_RENDER_SMOKE_TEST=0)"},
        )
        return {
            **state,
            "status_log": append_status_tag(
                state, "storyline_render_video_skipped"
            ),
        }

    outputs_root = _resolve_outputs_root(state)
    preview_dir = outputs_root / "storyline" / "preview"
    out_path = preview_dir / "storyline_preview.mp4"
    plan_path = str(state.get("storyline_timeline_plan") or "")

    def _failed_report(issues: list[str]) -> dict[str, Any]:
        return {
            "status": "failed",
            "renderer": "ffmpeg_storyline_preview",
            "output_path": str(out_path),
            "timeline_plan": plan_path,
            "rendered_at_ms": int(time.time() * 1000),
            "issues": issues,
            "fidelity_warnings": [],
            "notes": [],
        }

    timeline, load_err = _load_timeline(plan_path)
    if timeline is None:
        reason = load_err or "时间线加载失败"
        _emit_preview_event("preview.failed", detail={"reason": reason})
        return {
            **state,
            "storyline_render_preview_report": _failed_report([reason]),
            "error_log": [
                f"[storyline:render_preview] "
                f"{_error_code('CONTRACT_INVALID')}: {reason}"
            ],
            "status_log": append_status_tag(
                state, "storyline_render_video_failed"
            ),
        }

    # 预览草稿:canonical_to_draft 是纯函数(不落盘),这里显式双写到
    # preview/draft/,操作员可拿剪映打开与 MP4 对照。
    try:
        from storyline.mapper import canonical_to_draft

        draft = canonical_to_draft(timeline)
    except Exception as exc:  # noqa: BLE001 — mapper 防御性 ValueError 等统一降级
        reason = f"时间线映射为预览草稿失败: {exc}"
        _emit_preview_event("preview.failed", detail={"reason": reason})
        return {
            **state,
            "storyline_render_preview_report": _failed_report([reason]),
            "error_log": [
                f"[storyline:render_preview] "
                f"{_error_code('CONTRACT_INVALID')}: {reason}"
            ],
            "status_log": append_status_tag(
                state, "storyline_render_video_failed"
            ),
        }

    draft_dir = preview_dir / "draft"
    try:
        from draft_ops.atomic_writer import atomic_write_draft_pair

        atomic_write_draft_pair(draft_dir, draft)
    except (TypeError, OSError) as exc:
        reason = f"写预览草稿失败: {exc}"
        _emit_preview_event("preview.failed", detail={"reason": reason})
        return {
            **state,
            "storyline_render_preview_report": _failed_report([reason]),
            "error_log": [
                f"[storyline:render_preview] "
                f"{_error_code('TOOL_EXECUTION_FAILED')}: {reason}"
            ],
            "status_log": append_status_tag(
                state, "storyline_render_video_failed"
            ),
        }

    try:
        from draft_ops.draft_exporter import DraftExportError, run_export

        run_report = run_export(draft, out_path)
    except DraftExportError as exc:
        run_report = {"status": "failed", "issues": [str(exc)]}
    except Exception as exc:  # noqa: BLE001 — 预览失败不炸工作流,显式记失败
        run_report = {"status": "failed", "issues": [repr(exc)]}

    report: dict[str, Any] = dict(run_report)
    report["renderer"] = "ffmpeg_storyline_preview"
    report["preview_draft_dir"] = str(draft_dir)
    report["timeline_plan"] = plan_path
    report["rendered_at_ms"] = int(time.time() * 1000)
    report["notes"] = [
        "预览为剪映草稿生成前的初始时间线粗成片;不包含人工关卡②后续添加的 BGM。",
        "转场/特效/贴纸为硬切近似,字幕需系统可用字体(见 fidelity_warnings)。",
    ]

    if report.get("status") == "succeeded":
        probed = report.get("probed") or {}
        _emit_preview_event(
            "preview.succeeded",
            detail={
                "output_path": report.get("output_path"),
                "duration_seconds": probed.get("duration_seconds"),
                "sha256": report.get("sha256"),
                "fidelity_warnings": report.get("fidelity_warnings") or [],
            },
        )
        return {
            **state,
            "storyline_render_preview_path": str(out_path),
            "storyline_render_preview_report": report,
            "status_log": append_status_tag(
                state, "storyline_render_video_done"
            ),
        }

    reason = "; ".join(report.get("issues") or ["未知原因"])[:500]
    _emit_preview_event("preview.failed", detail={"reason": reason})
    return {
        **state,
        "storyline_render_preview_report": report,
        "error_log": [
            f"[storyline:render_preview] "
            f"{_error_code('TOOL_EXECUTION_FAILED')}: {reason}"
        ],
        "status_log": append_status_tag(
            state, "storyline_render_video_failed"
        ),
    }


def _error_code(code: str) -> str:
    """惰性取 ``StorylineErrorCode`` 成员(避免模块级 import 环)。"""
    from storyline.contract import StorylineErrorCode

    return getattr(StorylineErrorCode, code, code)


__all__ = ["storyline_render_video_node"]

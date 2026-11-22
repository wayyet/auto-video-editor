"""SDK 编译 → 安全发布 的节点级编排(迁移设计文档 §5.5/§9阶段7)。

``compile_and_publish_draft`` 是 node_11(SDK 汇合编译入口)与未来 node_05
SDK 初始草稿路径共用的编排:

    读 spec(缺失 → 显式跳过,不静默建空计划,§阶段7 兼容)
    → 以**当前草稿**刷新 timeline 节段(提取器:视频/音频/AI 转场审计)
    → 指纹比对(一致 → 跳过重编译,§5.3)
    → compile_spec(内存构建;blocked_features 非空 → 拒绝重建,§7.2)
    → 经 nodes._draft_io.write_draft 发布(原子双写 + 快照 + 校验 + 审计,
      复用全链统一写入入口;绝不调用 ScriptFile.save)
    → 回写 spec.compile(last_compiled_fingerprint / last_result)

失败语义(§11.1):任何失败都不覆盖当前有效草稿 —— 编译失败在内存里、
发布失败由 safe_write_draft 快照回退;调用方拿到 ok=False 后按 legacy
产物继续,记 error_log。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jianying_sdk.compiler import COMPILER_VERSION, compile_spec
from jianying_sdk.fingerprint import compute_build_fingerprint
from jianying_sdk.spec import DraftBuildSpec
from jianying_sdk.timeline_extractor import canvas_of, extract_timeline_snapshot
from nodes._draft_build_spec import load_build_spec, write_build_spec
from nodes._draft_io import write_draft


def compile_and_publish_draft(
    draft_dir: Path | str,
    draft_path: Path | str,
    *,
    job_id: str,
    canonical_plan: dict | None = None,
    audit_node: str = "node_11_compile",
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """把当前草稿 + spec 编译为 SDK 重建稿并安全发布。

    Args:
        draft_dir: 草稿目录(spec 文件所在;也是提取 sidecar 的来源)。
        draft_path: ``draft_content.json`` 文件路径(发布目标)。
        job_id: 任务 ID(spec 缺失时的降级日志用;spec 身份由 node_07 建立)。
        canonical_plan: ``state["storyline_plan"]``(AI 转场审计恢复;可选)。
        audit_node: draft_mutations 审计节点名。
        force_rebuild: 跳过"指纹相同"短路并传 ``force=True`` 给 compile_spec。
            普通 node_11 首编保持默认 False(保留指纹跳过优化);交付 QA repair
            对已判定缺失的构建块置 True —— 指纹未变 ≠ 草稿当前完整(草稿中的
            转场/动画可能已被删),不能凭旧指纹跳过重建(审查 R-05)。**只绕过
            指纹优化,不绕过 blocked_features fail-closed 与安全写入**。

    Returns:
        摘要 dict::

            {"status": "compiled"|"skipped_fingerprint"|"skipped_no_spec"
                       |"failed_blocked_features"|"failed",
             "fingerprint": str, "counts": {...}, "degradations": [...],
             "write": {...}  # safe_write_draft 结果(compiled 时)
            }
    """
    draft_dir = Path(draft_dir)
    draft_path = Path(draft_path)

    spec = load_build_spec(draft_dir)
    if spec is None:
        # §阶段7:旧线程无 spec → 明确跳过,不静默创建空计划(否则编译出
        # 只有时间线的"效果全丢"草稿)。legacy 产物原样保留。
        return {
            "status": "skipped_no_spec",
            "note": f"DraftBuildSpec 缺失({spec_path_note(draft_dir)}),"
                    "SDK 编译跳过,保留 legacy 草稿",
        }

    draft = _load_draft_safe(draft_path)
    if draft is None:
        return {
            "status": "failed",
            "note": f"当前草稿不可读: {draft_path}(编译中止,保留原草稿)",
        }

    # timeline 以当前草稿为准(node_07 基线之后的配音重写/人工编辑全部吸收)
    subtitle_ids = [sub.subtitle_id for sub in spec.subtitles]
    subtitle_texts = [sub.wrap_content or sub.text for sub in spec.subtitles]
    report = extract_timeline_snapshot(
        draft,
        draft_dir=draft_dir,
        canonical_plan=canonical_plan if isinstance(canonical_plan, dict) else None,
        spec_subtitle_ids=subtitle_ids,
        spec_subtitle_texts=subtitle_texts,
    )
    spec.timeline = report.section
    spec.canvas = canvas_of(draft)

    fingerprint = compute_build_fingerprint(spec)
    if not force_rebuild and spec.compile.last_compiled_fingerprint == fingerprint:
        return {
            "status": "skipped_fingerprint",
            "fingerprint": fingerprint,
            "note": "构建指纹未变化,跳过重编译(§5.3)",
        }

    try:
        result = compile_spec(spec, force=force_rebuild)
    except Exception as exc:  # noqa: BLE001 - 编译失败不覆盖当前有效草稿(§11.1)
        status = (
            "failed_blocked_features"
            if type(exc).__name__ == "UnsupportedManualFeatureError"
            else "failed"
        )
        record = {
            "status": status,
            "fingerprint": fingerprint,
            "note": repr(exc),
        }
        _record_compile_result(spec, record)
        write_build_spec(draft_dir, spec)
        return record

    try:
        write_result = write_draft(
            draft_path,
            result.payload,
            duration_us=result.duration_us or None,
            audit_node=audit_node,
        )
    except Exception as exc:  # noqa: BLE001 - 发布异常已由 safe_write_draft 快照回退
        record = {
            "status": "failed",
            "fingerprint": result.fingerprint,
            "note": f"发布失败(已回退快照): {exc!r}",
        }
        _record_compile_result(spec, record)
        write_build_spec(draft_dir, spec)
        return record
    ok = bool(write_result.get("ok"))
    record: dict[str, Any] = {
        "status": "compiled" if ok else "failed",
        "fingerprint": result.fingerprint,
        "counts": result.summary.get("counts", {}),
        "degradations": result.summary.get("degradations", []),
        "compiler_version": COMPILER_VERSION,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "write": {
            "ok": ok,
            "reason": write_result.get("reason"),
            "jianying_running": bool(write_result.get("jianying_running")),
        },
    }
    _record_compile_result(spec, record)
    write_build_spec(draft_dir, spec)
    return record


def _record_compile_result(spec: DraftBuildSpec, record: dict[str, Any]) -> None:
    """把编译结果回写进 spec.compile(观测通道;失败不抛)。"""
    spec.compile.last_compiled_fingerprint = (
        record.get("fingerprint") if record.get("status") in ("compiled", "skipped_fingerprint") else None
    )
    spec.compile.input_fingerprint = record.get("fingerprint")
    spec.compile.compiler_version = str(record.get("compiler_version") or COMPILER_VERSION)
    spec.compile.last_compiled_at = record.get("compiled_at")
    spec.compile.last_result = record


def _load_draft_safe(draft_path: Path) -> dict | None:
    import json

    try:
        return json.loads(Path(draft_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def spec_path_note(draft_dir: Path) -> str:
    from nodes._draft_build_spec import SPEC_FILE_NAME

    return str(Path(draft_dir) / SPEC_FILE_NAME)


__all__ = ["compile_and_publish_draft", "spec_path_note"]

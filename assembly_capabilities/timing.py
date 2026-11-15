"""节点计时汇总(P2-2):六节点 + 内部阶段耗时落 ``node_timing.json``。

计划文档 P2-2 验收:"输出完整 node_timing.json,包含六节点和内部阶段耗时;
区分模型请求、FFmpeg、OpenCV 和磁盘 I/O"。实现口径:

- 每个 assembly 节点在收尾时调 :func:`record_node_timing` 追加一条 entry
  (node_id / total_seconds / stages / recorded_at_ms);
- ``stages`` 由各节点自行填关键阶段(模型请求=llm_seconds,FFmpeg=
  probe/render_seconds,OpenCV=observe_seconds 等,以节点实际可测为准);
- 链内节点串行执行(discover→observe→build→validate→repair→report,
  repair 回环也是串行),读-改-写无并发竞争;
- ``assembly_write_report`` 最终把 timing 汇总渲染进 report.md;
- 计时是观测产物:任何失败都绝不阻断主流程(调用方包 try/except)。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

TIMING_SCHEMA_VERSION = 1

#: 六节点 + 消费方的 node_id 全集(测试契约)
TIMED_NODE_IDS = (
    "assembly_discover_and_probe",
    "assembly_visual_observe",
    "assembly_build_timeline",
    "assembly_validate_render_qc",
    "assembly_repair_loop",
    "assembly_write_report",
)


def record_node_timing(
    out_dir: Path,
    *,
    node_id: str,
    total_seconds: float,
    stages: dict[str, float] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """追加一条节点计时到 ``node_timing.json``,返回文件路径(绝不抛异常)。"""
    try:
        path = Path(out_dir) / "node_timing.json"
        data: dict[str, Any] = {"schema_version": TIMING_SCHEMA_VERSION, "entries": []}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("entries"), list):
                    data = existing
            except (OSError, json.JSONDecodeError):
                pass  # 损坏则重建(观测产物,允许丢历史)
        entry: dict[str, Any] = {
            "node_id": str(node_id),
            "total_seconds": round(float(total_seconds or 0.0), 4),
            "stages": {
                str(k): round(float(v), 4) for k, v in (stages or {}).items()
            },
            "recorded_at_ms": int(time.time() * 1000),
        }
        if extra:
            entry["extra"] = extra
        data["entries"].append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(path)
    except Exception:  # noqa: BLE001 — 计时绝不阻断主流程
        return ""


def timed_assembly_node(node_id: str):
    """节点装饰器:自动记录整节点 wall time 到 node_timing.json(绝不阻断)。

    适用于返回 ``dict`` 的 assembly 节点(``fn(state) -> dict`` 形态);
    需要记录内部阶段耗时(probe/analyze/llm 等)的节点(discover)自行调
    :func:`record_node_timing`,不要再挂本装饰器(避免重复条目)。
    """

    def _deco(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):  # noqa: ANN001 — LangGraph 节点签名
            started = time.perf_counter()
            out = fn(state, *args, **kwargs)
            try:
                from nodes.storyline._common import _resolve_outputs_root

                out_dir = Path(str(_resolve_outputs_root(state))) / "assembly"
                record_node_timing(
                    out_dir,
                    node_id=node_id,
                    total_seconds=time.perf_counter() - started,
                )
            except Exception:  # noqa: BLE001 — 计时绝不阻断主流程
                pass
            return out

        return wrapper

    return _deco


def summarize_timing(timing_path: str | Path | None) -> str:
    """timing.json → Markdown 摘要(write_report 消费;损坏/缺失降级)。"""
    if not timing_path:
        return "- (node_timing.json 未生成)"
    p = Path(str(timing_path))
    if not p.is_file():
        return "- (node_timing.json 未生成)"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "- node_timing.json 存在但无法解析"
    entries = [
        e for e in (data.get("entries") or [])
        if isinstance(e, dict) and e.get("node_id")
    ]
    if not entries:
        return "- (无计时条目)"
    lines = ["| 节点 | 总耗时(s) | 关键阶段(s) |", "|---|---|---|"]
    for e in entries:
        stages = ", ".join(f"{k}={v}" for k, v in (e.get("stages") or {}).items())
        lines.append(
            f"| {e.get('node_id')} | {e.get('total_seconds')} | {stages or '—'} |"
        )
    return "\n".join(lines)


__all__ = [
    "TIMED_NODE_IDS",
    "TIMING_SCHEMA_VERSION",
    "record_node_timing",
    "summarize_timing",
]

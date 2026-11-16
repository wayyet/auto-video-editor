#!/usr/bin/env python3
"""归档一次 assembly 运行的脱敏证据包(P2-5 质量基线)。

收集 ``outputs/<job>/assembly/`` 下的关键产物 + 计算四级 hash + 环境指纹,
脱敏后写入 ``outputs/<job>/assembly/evidence/<UTC 时间戳>/``:

- manifest.json:commit SHA / 监控 schema 版本 / 配置开关 / 模型后端 /
  输入素材摘要 / 各产物 sha256 与四级 plan hash 链;
- 产物副本:media.json / visual_observation.json / candidate_plan.json /
  draft_patch.json / final_plan.json(如有)/ influence_trace.json /
  candidate_qc_report.json / preview_qc_report.json / repair_history.json /
  node_timing.json / report.md;
- draft_video_diff.json:最终计划 clips ↔ 正式草稿视频轨的逐段对账(需
  --draft 给出 draft_content.json 路径;缺省跳过该文件)。

脱敏口径:所有绝对路径中的盘符/用户目录/任务根替换为 ``<REDACTED>``,
只保留 basename 可读性(素材文件名/产物文件名不脱敏)。

用法:
    python scripts/archive_assembly_evidence.py --job-id <job_id> [--draft <draft_content.json>]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 归档的产物文件清单(缺失的跳过并在 manifest 缺失清单留痕)
EVIDENCE_FILES = [
    "media.json",
    "visual_observation.json",
    "candidate_plan.json",
    "draft_patch.json",
    "final_plan.json",
    "influence_trace.json",
    "candidate_qc_report.json",
    "preview_qc_report.json",
    "repair_history.json",
    "node_timing.json",
    "report.md",
]

_REDACT_PATTERNS = [
    # ``\\+`` = 一个或多个字面反斜杠(JSON dump 会把路径反斜杠转义成 ``\\``)
    r"[A-Za-z]:\\+Users\\+[^\\\"']+",
    r"[A-Za-z]:/+[Uu]sers/+[^/\"']+",
    r"/home/[^/\"']+",
    r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_. -]+",
    r"[A-Za-z]:/+[Uu]sers/+[A-Za-z0-9_. -]+",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_text(text: str) -> str:
    """绝对路径脱敏:用户目录/家目录前缀整体替换为 ``<REDACTED>``。

    只保留其后相对段与 basename(产物文件名/素材文件名可读);**不回填
    用户名** —— `<REDACTED>\\<basename>` 这种"伪 basename"恰好是泄漏源。
    """
    out = text
    for pattern in _REDACT_PATTERNS:
        out = re.sub(pattern, "<REDACTED>", out)
    return out


def redact_json(obj):  # noqa: ANN001 — 递归脱敏 dict/list/str
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    return obj


def git_commit_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def input_summary(manifest: dict) -> list[dict]:  # noqa: ANN001
    sources = []
    for s in manifest.get("sources") or []:
        if isinstance(s, dict):
            sources.append(
                {
                    "asset_id": s.get("asset_id"),
                    "source_basename": Path(str(s.get("source_path") or "")).name,
                    "duration_seconds": s.get("duration_seconds"),
                    "probe_status": s.get("probe_status"),
                    "analysis_status": s.get("analysis_status"),
                    "candidate_count": len(s.get("candidate_segments") or []),
                }
            )
    return sources


def draft_video_diff(final_plan: dict, draft: dict) -> list[dict]:  # noqa: ANN001
    """最终计划 clips ↔ 正式草稿视频轨逐段对账(源区间 µs 对 ms)。"""
    clips = [c for c in final_plan.get("clips") or [] if isinstance(c, dict)]
    segs = []
    for track in draft.get("tracks") or []:
        if isinstance(track, dict) and track.get("type") == "video":
            segs.extend(seg for seg in track.get("segments") or [] if isinstance(seg, dict))
    rows = []
    for i, seg in enumerate(segs):
        tr = seg.get("source_timerange") or {}
        start_us = int(tr.get("start") or 0)
        dur_us = int(tr.get("duration") or 0)
        clip = clips[i] if i < len(clips) else None
        rows.append(
            {
                "segment_index": i,
                "draft_source_start_ms": start_us // 1000,
                "draft_source_end_ms": (start_us + dur_us) // 1000,
                "final_clip_id": (clip or {}).get("clip_id"),
                "final_source_in_ms": (clip or {}).get("source_in_ms"),
                "final_source_out_ms": (clip or {}).get("source_out_ms"),
                "match": bool(
                    clip
                    and (start_us // 1000) == int(clip.get("source_in_ms") or 0)
                    and ((start_us + dur_us) // 1000) == int(clip.get("source_out_ms") or 0)
                ),
            }
        )
    if len(segs) != len(clips):
        rows.append(
            {
                "segment_index": "COUNT_MISMATCH",
                "draft_segments": len(segs),
                "final_clips": len(clips),
                "match": False,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, help="任务 job_id(session_id)")
    parser.add_argument(
        "--outputs-root", type=Path, default=None,
        help="outputs 根(缺省 <repo>/outputs)",
    )
    parser.add_argument("--draft", type=Path, default=None, help="正式 draft_content.json 路径(可选)")
    args = parser.parse_args()

    outputs_root = args.outputs_root or (REPO_ROOT / "outputs")
    assembly_dir = outputs_root / args.job_id / "assembly"
    if not assembly_dir.is_dir():
        print(f"[archive] 目录不存在:{assembly_dir}", file=sys.stderr)
        return 1

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = assembly_dir / "evidence" / stamp
    evidence_dir.mkdir(parents=True, exist_ok=True)

    manifest_entry: dict = {
        "archive_utc": stamp,
        "commit_sha": git_commit_sha(),
        "monitor_schema_version": None,
        "config": {
            "ASSEMBLY_QC_GATE_ENABLED": bool(getattr(__import__("config"), "ASSEMBLY_QC_GATE_ENABLED", None)),
            "ASSEMBLY_QC_MAX_RETRY": getattr(__import__("config"), "ASSEMBLY_QC_MAX_RETRY", None),
            "VIDEO_LLM_BACKEND": __import__("os").environ.get("VIDEO_LLM_BACKEND", "stub"),
            "ASSEMBLY_PROBE_CONCURRENCY": getattr(__import__("config"), "ASSEMBLY_PROBE_CONCURRENCY", None),
            "ASSEMBLY_MAX_FRAMES_PER_CANDIDATE": getattr(__import__("config"), "ASSEMBLY_MAX_FRAMES_PER_CANDIDATE", None),
        },
        "model_info": {
            "video_llm_backend": __import__("os").environ.get("VIDEO_LLM_BACKEND", "stub"),
            "llm_diagnostics": None,
        },
        "input_summary": None,
        "hash_chain": None,
        "files": {},
        "missing": [],
        "draft_video_diff": None,
    }

    # 监控 schema 版本
    try:
        from monitoring.workflow_graph import SCHEMA_VERSION as _monitor_schema
        manifest_entry["monitor_schema_version"] = _monitor_schema
    except Exception:
        pass

    # 复制产物(脱敏)+ 记 hash
    copied = 0
    for name in EVIDENCE_FILES:
        src = assembly_dir / name
        if not src.is_file():
            manifest_entry["missing"].append(name)
            continue
        try:
            raw = src.read_text(encoding="utf-8")
            (evidence_dir / name).write_text(redact_text(raw), encoding="utf-8")
        except UnicodeDecodeError:
            data = src.read_bytes()
            (evidence_dir / name).write_bytes(data)
        manifest_entry["files"][name] = sha256_file(evidence_dir / name)
        copied += 1
        if name == "media.json":
            try:
                m = json.loads(raw)
                manifest_entry["input_summary"] = input_summary(m)
            except json.JSONDecodeError:
                pass
        if name == "influence_trace.json":
            try:
                t = json.loads(raw)
                manifest_entry["hash_chain"] = t.get("hash_chain")
                manifest_entry["model_info"]["llm_diagnostics"] = t.get(
                    "llm_diagnostics"
                )
            except json.JSONDecodeError:
                pass

    # 最终草稿对账(可选)
    if args.draft and args.draft.is_file():
        final_plan_file = evidence_dir / "final_plan.json"
        if final_plan_file.is_file():
            try:
                final_plan = json.loads(final_plan_file.read_text(encoding="utf-8"))
                draft = json.loads(args.draft.read_text(encoding="utf-8"))
                rows = draft_video_diff(final_plan, draft)
                (evidence_dir / "draft_video_diff.json").write_text(
                    json.dumps(redact_json(rows), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                manifest_entry["draft_video_diff"] = {
                    "all_match": all(r.get("match") for r in rows),
                    "rows": len(rows),
                }
            except json.JSONDecodeError as exc:
                manifest_entry["draft_video_diff"] = {"error": repr(exc)}

    (evidence_dir / "manifest.json").write_text(
        json.dumps(redact_json(manifest_entry), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[archive] {copied} 个产物已脱敏归档 → {evidence_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

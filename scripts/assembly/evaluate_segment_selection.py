"""阶段三选段质量评估脚本(plan §十 阶段三交付物:选段质量评估记录)。

不依赖 ffmpeg / 真实视频:对一组预设的 ``media.json`` fixture 调
``assembly_capabilities.build_timeline`` 的选段路径,记录:
1. 用了什么 client(default StubLLMClient vs 注入的真实 LLM client)
2. LLM 是否给出选段 / fallback 是否被触发
3. timeline 结构(轨道数、clip 数、reason 列表、beat 列表)
4. 与"理想答案"(fixture 内嵌)的偏差

用法::

    .venv/Scripts/python.exe scripts/assembly/evaluate_segment_selection.py --fixture-dir tests/fixtures/assembly_selection

fixture 目录结构::

    case_01/
      meta.json           # {"name": "...", "ideal_segments": [...]}
      media.json          # assembly_discover_and_probe 产物
      transcript.json     # 可选
      video_ingest.json   # 可选
    case_02/
      ...

每个 case 输出 ``case_XX/report.md`` 含:输入摘要、输出 timeline 摘要、
选段 vs 理想答案的对比(命中 / 缺失 / 多选)、与人工评分位。

**注意**:本机无 ffmpeg / 无真实视觉观察产物时,只能跑基于 metadata 的
LLM 选段(stub LLM 只看候选片段 index/start/end,不看图)。视觉选段质量
的真实评估要等到阶段五(端到端验证)接入真实 VLM 后再做。本脚本在阶段三
主要用于验证"prompt + 输出契约 + 选段算法"正确性,以及验证 fallback 路径。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 允许从仓库根目录跑
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


from storyline_capabilities.vlm_client import StubLLMClient, set_default_client
from assembly_capabilities.build_timeline import build_timeline_from_paths


def _safe_read_json(path: Path) -> object:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _diff_against_ideal(
    selected_indices: list[int],
    ideal_segments: list[dict],
) -> dict:
    """对比 LLM 选段 vs 理想答案(用 index 集合)。

    Returns:
        dict 含 hits / misses / extras 三个集合 + 命中数 / 总数。
    """
    ideal_indices = {int(s.get("index")) for s in ideal_segments if "index" in s}
    selected_set = set(selected_indices)
    hits = sorted(selected_set & ideal_indices)
    misses = sorted(ideal_indices - selected_set)
    extras = sorted(selected_set - ideal_indices)
    return {
        "hits": hits,
        "misses": misses,
        "extras": extras,
        "hit_count": len(hits),
        "ideal_count": len(ideal_indices),
        "selected_count": len(selected_indices),
    }


def _format_diff(diff: dict) -> str:
    if diff["ideal_count"] == 0:
        return "(无理想答案)"
    if diff["hit_count"] == diff["ideal_count"]:
        return f"✅ 全部命中 ({diff['hit_count']}/{diff['ideal_count']})"
    extra_str = (
        f", 多选 {diff['extras']}" if diff["extras"] else ""
    )
    miss_str = (
        f", 漏选 {diff['misses']}" if diff["misses"] else ""
    )
    return (
        f"{'✅' if diff['hit_count'] == diff['ideal_count'] else '⚠️'} "
        f"命中 {diff['hit_count']}/{diff['ideal_count']}"
        f"{extra_str}{miss_str}"
    )


def _evaluate_one(
    case_dir: Path,
    *,
    client_factory,
) -> dict:
    """跑一个 fixture,返回评测 dict。"""
    meta = _safe_read_json(case_dir / "meta.json") or {}
    name = str(meta.get("name") or case_dir.name)
    ideal_segments = list(meta.get("ideal_segments") or [])
    notes = str(meta.get("notes") or "")

    media_path = case_dir / "media.json"
    transcript_path = case_dir / "transcript.json"
    ingest_path = case_dir / "video_ingest.json"

    if not media_path.is_file():
        return {"case": name, "error": "missing media.json", "ok": False}

    client = client_factory(name)
    if client is not None:
        set_default_client(client)

    timeline = build_timeline_from_paths(
        media_artifact=str(media_path),
        transcript_artifact=str(transcript_path) if transcript_path.is_file() else None,
        ingest_artifact=str(ingest_path) if ingest_path.is_file() else None,
        lang="zh",
        client=client,
    )

    project = timeline.get("project") or {}
    tracks = timeline.get("tracks") or []
    clips = tracks[0].get("clips") if tracks else []
    # 阶段三契约:build_timeline 把每个 clip 的"原始 candidate_index"写在
    # clip.candidate_index,评测脚本直接读这个做精准对比。
    selected_indices = [
        int(c.get("candidate_index", -1))
        for c in clips
        if c.get("candidate_index") is not None and int(c.get("candidate_index", -1)) >= 0
    ]

    # 取候选片段的总 indices 做差分
    media_reports = _safe_read_json(media_path) or []
    candidate_indices: list[int] = []
    for rep in media_reports:
        if not isinstance(rep, dict):
            continue
        analysis = rep.get("analysis") or {}
        if not isinstance(analysis, dict):
            continue
        for seg in analysis.get("candidate_segments") or []:
            if isinstance(seg, dict) and "index" in seg:
                candidate_indices.append(int(seg["index"]))

    return {
        "case": name,
        "notes": notes,
        "ok": True,
        "project": {
            "llm_used": project.get("llm_used"),
            "fallback_used": project.get("fallback_used"),
            "selected_segments_count": project.get("selected_segments_count"),
            "candidate_segments_count": project.get("candidate_segments_count"),
            "elapsed_seconds": project.get("elapsed_seconds"),
            "truncated_for_llm": project.get("truncated_for_llm"),
            "task_assumption": project.get("task_assumption"),
            "selected_strategy": project.get("selected_strategy"),
            "editorial_structure": project.get("editorial_structure"),
        },
        "tracks_count": len(tracks),
        "clips_count": len(clips),
        "clip_summaries": [
            {
                "id": c.get("id"),
                "reason": c.get("reason"),
                "beat": c.get("beat"),
                "start": c.get("start"),
                "end": c.get("end"),
                "timeline_start": c.get("timeline_start"),
            }
            for c in clips[:20]
        ],
        "ideal_diff": _diff_against_ideal(selected_indices, ideal_segments),
        "ideal_count": len(ideal_segments),
        "candidate_indices": candidate_indices,
    }


def _render_report(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append(f"# 选段评估报告 — {result['case']}\n")
    if not result["ok"]:
        lines.append(f"❌ 评测失败:{result.get('error')}")
        (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
        return
    if result.get("notes"):
        lines.append(f"> {result['notes']}\n")
    proj = result["project"]
    lines.append("## 输入摘要")
    lines.append(f"- 候选片段数:{proj['candidate_segments_count']}")
    lines.append(f"- 是否被截断:{proj['truncated_for_llm']}")
    lines.append("")
    lines.append("## LLM 调用结果")
    lines.append(f"- llm_used:`{proj['llm_used']}`")
    lines.append(f"- fallback_used:`{proj['fallback_used']}`")
    lines.append(f"- 选段数:`{proj['selected_segments_count']}`")
    lines.append(f"- 耗时:{proj['elapsed_seconds']}s")
    lines.append("")
    if proj.get("task_assumption"):
        lines.append(f"- task_assumption:`{proj['task_assumption']}`")
    if proj.get("selected_strategy"):
        lines.append(f"- selected_strategy:`{proj['selected_strategy']}`")
    if proj.get("editorial_structure"):
        lines.append(f"- editorial_structure:`{proj['editorial_structure']}`")
    lines.append("")
    lines.append("## 输出 timeline 结构")
    lines.append(f"- tracks 数:{result['tracks_count']}")
    lines.append(f"- 视频 clip 数:{result['clips_count']}")
    lines.append("")
    if result["clip_summaries"]:
        lines.append("## clip 明细")
        for c in result["clip_summaries"]:
            beat = c.get("beat") or ""
            lines.append(
                f"- `{c.get('id')}` start={c.get('start')}s end={c.get('end')}s "
                f"timeline_start={c.get('timeline_start')}s beat=`{beat}` "
                f"reason=`{c.get('reason')}`"
            )
        lines.append("")
    lines.append("## 与理想答案对比")
    lines.append(_format_diff(result["ideal_diff"]))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("### 人工评分")
    lines.append("")
    lines.append("- [ ] 选段顺序合理(hook → core → end)")
    lines.append("- [ ] 没有把同一场景切成两段")
    lines.append("- [ ] 没有漏掉关键语音/画面")
    lines.append("- [ ] 备注:")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _default_client_factory(name: str):
    """默认:每次跑都用全新 StubLLMClient(避免全局污染)。"""
    return StubLLMClient()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--fixture-dir",
        default=str(REPO_ROOT / "tests" / "fixtures" / "assembly_selection"),
        help="含多个 case_* 子目录的 fixture 根目录",
    )
    p.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "outputs" / "assembly_selection_eval"),
        help="评测结果输出目录(每个 case 一个子目录 + report.md)",
    )
    args = p.parse_args()
    fixture_dir = Path(args.fixture_dir)
    out_root = Path(args.output_dir)
    if not fixture_dir.is_dir():
        print(f"fixture dir not found: {fixture_dir}", file=sys.stderr)
        return 1
    case_dirs = sorted([p for p in fixture_dir.iterdir() if p.is_dir()])
    if not case_dirs:
        print(f"no case dirs under {fixture_dir}", file=sys.stderr)
        return 1
    summaries: list[dict] = []
    for case_dir in case_dirs:
        out_dir = out_root / case_dir.name
        result = _evaluate_one(case_dir, client_factory=_default_client_factory)
        _render_report(result, out_dir)
        summaries.append(
            {
                "case": result["case"],
                "ok": result["ok"],
                "llm_used": result.get("project", {}).get("llm_used") if result["ok"] else None,
                "fallback_used": result.get("project", {}).get("fallback_used") if result["ok"] else None,
                "clips_count": result.get("clips_count") if result["ok"] else None,
            }
        )
        print(
            f"[{case_dir.name}] ok={result['ok']} "
            f"llm={result.get('project', {}).get('llm_used') if result['ok'] else '-'} "
            f"fallback={result.get('project', {}).get('fallback_used') if result['ok'] else '-'} "
            f"clips={result.get('clips_count') if result['ok'] else '-'}"
        )
    # 汇总 summary.md
    lines = ["# 选段评估汇总\n"]
    for s in summaries:
        lines.append(
            f"- `{s['case']}` ok={s['ok']} "
            f"llm_used={s['llm_used']} fallback={s['fallback_used']} "
            f"clips={s['clips_count']}"
        )
    (out_root / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n汇总:{out_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
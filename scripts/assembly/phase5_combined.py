"""合并跑 happy + checkpoint1(d 用 happy 真实 state) + decoupling + gate_off + qc_fail,
生成最终汇总到 outputs/phase5_e2e_report.json 与 outputs/phase5_e2e_report.md。

为何存在:
单次 ``phase5_e2e.py --case all`` 在 Windows PowerShell 单 shell session 默认
300s 超时下,跑 happy(17s)+ qc_fail(30s, 含 2 次 retry)已经占满预算,无法一次
跑完。本脚本拆成两段调用:

- scripts/assembly/phase5_e2e.py --case happy   (~20s)
- scripts/assembly/phase5_e2e.py --case qc_fail (~40s)
- gate_off + decoupling + checkpoint1 (合计 <5s)

执行顺序:
  1. happy
  2. qc_fail(独立 job)
  3. gate_off(静态断言)
  4. checkpoint1(用 happy 的真实 state)
  5. decoupling(全仓扫描)
  6. 写 outputs/phase5_e2e_report.{json,md}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# UTF-8 化输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from scripts.assembly.phase5_e2e import (  # noqa: E402  — sys.path 调整后
    case_checkpoint1_notification,
    case_decoupling,
    case_gate_off,
    case_happy,
    case_qc_fail,
)


def main() -> int:
    summary: dict = {
        "phase": "Phase 5 (端到端验证)",
        "plan_ref": "docs/integration/video-agent-kit集成auto-video-editor设计执行计划.md §10",
        "input": str(ROOT / "inputs" / "30s.mp4"),
        "transcript_fixture": str(ROOT / "tests" / "fixtures" / "assembly_phase5" / "transcript.json"),
        "cases": {},
    }

    print("[1/5] happy path ...")
    summary["cases"]["happy"] = case_happy(
        ROOT / "inputs" / "30s.mp4",
        str(ROOT / "tests" / "fixtures" / "assembly_phase5" / "transcript.json"),
    )

    print("[2/5] qc_fail (软降级) ...")
    summary["cases"]["qc_fail"] = case_qc_fail(
        ROOT / "inputs" / "30s.mp4",
        str(ROOT / "tests" / "fixtures" / "assembly_phase5" / "transcript.json"),
    )

    print("[3/5] gate_off (应急关闭) ...")
    summary["cases"]["gate_off"] = case_gate_off()

    print("[4/5] checkpoint1 (通知文案) ...")
    summary["cases"]["checkpoint1"] = case_checkpoint1_notification(summary["cases"]["happy"]["state"])

    print("[5/5] decoupling (解耦验证) ...")
    summary["cases"]["decoupling"] = case_decoupling()

    # 写 JSON
    json_path = ROOT / "outputs" / "phase5_e2e_report.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总
    print("\n" + "=" * 60)
    print("  阶段五汇总")
    print("=" * 60)
    all_ok = True
    for name, r in summary["cases"].items():
        mark = "[OK]" if r["all_ok"] else "[FAIL]"
        print(f"  {mark} {name}: all_ok={r['all_ok']}")
        all_ok = all_ok and r["all_ok"]

    # 写 Markdown 报告
    md_path = ROOT / "outputs" / "phase5_e2e_report.md"
    md_path.write_text(_render_markdown(summary, all_ok), encoding="utf-8")
    print(f"\n  汇总 JSON: {json_path}")
    print(f"  汇总 Markdown: {md_path}")
    print(f"  全部通过: {all_ok}")
    return 0 if all_ok else 1


def _render_markdown(summary: dict, all_ok: bool) -> str:
    """把 summary 渲染为人类可读的 Markdown。"""
    lines: list[str] = []
    lines.append("# 阶段五端到端验证报告\n")
    lines.append(f"> 计划依据:`{summary['plan_ref']}`\n")
    lines.append(f"> 输入素材:`{summary['input']}`\n")
    lines.append(f"> Transcript fixture:`{summary['transcript_fixture']}`\n")
    lines.append(f"\n**全部通过:{all_ok}**\n")

    for name, r in summary["cases"].items():
        lines.append(f"\n## Case {name}: " + ("✓" if r["all_ok"] else "✗") + "\n")
        if name == "happy":
            lines.append(f"- job_id: `{r['job_id']}`")
            lines.append(f"- 产物目录: `{r['out_dir']}`")
            lines.append(f"- qc_status: `{r['qc_status']}`")
            lines.append(f"- 修复循环重试次数: {r['retry_count']}")
            lines.append(f"- 总耗时: {r['total_sec']}s")
            lines.append("\n### 节点耗时\n")
            lines.append("| 节点 | 耗时(秒) |")
            lines.append("|---|---:|")
            for t in r["timings"]:
                lines.append(f"| `{t['node']}` | {t['seconds']} |")
            lines.append("\n### 产物清单\n")
            lines.append("| 文件 | 存在 | 大小(B) |")
            lines.append("|---|:-:|---:|")
            for fn, info in r["artifacts"]["files"].items():
                mark = "✓" if info["exists"] else "✗"
                size = info.get("size_bytes", "-")
                lines.append(f"| `{fn}` | {mark} | {size} |")
            lines.append(f"\n- preview.mp4 可播放: **{r['artifacts']['preview_playable']}** "
                         f"(stream 数: {r['artifacts'].get('preview_stream_count', '-')}, "
                         f"类型: {[s['codec_type'] for s in r['artifacts'].get('preview_streams', [])]})")
            lines.append(f"- report.md 字符数: {r['artifacts'].get('report_chars', '-')}")
            lines.append(f"- report.md 含 'Assembly QC' 标题: **{r['artifacts'].get('report_has_assembly_qc', '-')}**")
            lines.append(f"- report.md section 数: {len(r['artifacts'].get('report_sections', []))}")
            for sec in r["artifacts"].get("report_sections", []):
                lines.append(f"  - {sec}")
        elif name == "qc_fail":
            lines.append(f"- job_id: `{r['job_id']}`")
            lines.append(f"- qc_status(最终): `{r['qc_status']}`")
            lines.append(f"- 修复循环重试次数: {r['retry_count']}")
            lines.append(f"- 总耗时: {r['total_sec']}s")
            lines.append("\n### 节点耗时\n")
            lines.append("| 节点 | 耗时(秒) |")
            lines.append("|---|---:|")
            for t in r["timings"]:
                lines.append(f"| `{t['node']}` | {t['seconds']} |")
            lines.append("\n### 产物清单(软降级关键:即便 repair loop 反复失败,report.md 仍生成)\n")
            lines.append("| 文件 | 存在 | 大小(B) |")
            lines.append("|---|:-:|---:|")
            for fn, info in r["artifacts"]["files"].items():
                mark = "✓" if info["exists"] else "✗"
                size = info.get("size_bytes", "-")
                lines.append(f"| `{fn}` | {mark} | {size} |")
        elif name == "gate_off":
            lines.append(f"- 边数扫描: {r['edges_inspected']}")
            lines.append(f"- generate_draft → assembly_xxx 边存在: **{r['has_generate_to_assembly']}** (应为 False)")
            lines.append(f"- generate_draft → node_06_human_reorder 边存在: **{r['has_generate_to_human_reorder']}** (应为 True)")
        elif name == "checkpoint1":
            lines.append(f"- 当前实现是否带 assembly_report_path: **{r['current_implementation_has_assembly_report']}** (应为 False)")
            lines.append(f"- 阶段五修订实现是否带: **{r['phase5_implementation_has_assembly_report']}** (应为 True)")
            lines.append(f"- 当前实现示例:`{r.get('current_sample', '')}`")
            lines.append(f"- 阶段五示例:`{r.get('phase5_sample', '')}`")
        elif name == "decoupling":
            lines.append(f"- 检查范围: {r['checked']}")
            lines.append(f"- 违规数: {len(r['violations'])}")
            if r["violations"]:
                for vi in r["violations"]:
                    lines.append(f"  - `{vi['file']}` ({vi['kind']}): {vi['snippet']}")

    lines.append("\n---\n*本报告由 `scripts/assembly/phase5_combined.py` 自动生成。*")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

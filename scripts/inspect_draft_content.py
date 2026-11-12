#!/usr/bin/env python3
r"""检查剪映草稿 draft_content.json 的「真实内容」(只读;CLI 外壳)。

单一实现在 :mod:`draft_ops.draft_content_audit`(第四期 §3.3-1):统计逻辑与
交付前 QA(nodes/node_join_before_delivery 的转场/特效孤儿检测)共用同一口径,
本文件只保留命令行参数解析与退出码语义。

用法(在仓库根目录执行,路径当参数传):
    .\.venv\Scripts\python.exe scripts\inspect_draft_content.py <draft_content.json 或 草稿目录>
        [--json] [--label 标签] [--sidecar]
        [--min 指标=N ...] [--max 指标=N ...]

门禁用法:--min / --max 可重复;任一不满足则退出码为 2(全部满足为 0,读取失败为 1)。
可用指标(英文键,--json 输出里同名;含义见 draft_content_audit.METRIC_LABELS):
    video_segments audio_segments text_segments sticker_segments effect_segments
    transitions_referenced video_effects_referenced text_animations_referenced
    stickers_referenced audio_fades_referenced
    orphan_materials misplaced_references dangling_references placeholder_values shape_warnings
示例:
    --min text_segments=3 --min transitions_referenced=2 --max orphan_materials=0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # scripts 直接运行时 sys.path[0] 是 scripts/(同 backfill_sticker_library.py)

from draft_ops.draft_content_audit import (  # noqa: E402
    METRIC_LABELS,
    inspect,
    load_draft_target,
    read_sidecar,
    render,
    sidecar_summary,
)


def _parse_limits(items: list[str], flag: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for it in items:
        key, sep, num = it.partition("=")
        if not sep or key not in METRIC_LABELS or not num.lstrip("-").isdigit():
            raise SystemExit(f"{flag} 参数格式应为 指标=整数,可用指标: {', '.join(METRIC_LABELS)};收到: {it!r}")
        out.append((key, int(num)))
    return out


def main() -> int:
    try:  # Windows 控制台/重定向时避免因个别字符编码失败而崩溃
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="检查剪映草稿的真实内容(被引用口径)")
    ap.add_argument("target", help="draft_content.json 或包含它的草稿目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--label", default="草稿")
    ap.add_argument("--sidecar", action="store_true", help="同时打印 sidecar 里记录的被丢弃内容")
    ap.add_argument("--min", action="append", default=[], metavar="指标=N", help="指标至少为 N")
    ap.add_argument("--max", action="append", default=[], metavar="指标=N", help="指标至多为 N")
    args = ap.parse_args()
    mins = _parse_limits(args.min, "--min")
    maxs = _parse_limits(args.max, "--max")

    try:
        draft, path = load_draft_target(Path(args.target))
    except (OSError, ValueError) as exc:
        print(f"读取失败: {exc}", file=sys.stderr)
        return 1
    rep = inspect(draft)
    sc = read_sidecar(path) if args.sidecar else None
    if args.json:
        if sc is not None:
            rep["sidecar_dropped_keys"] = sorted((sc.get("dropped") or {}).keys())
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep, args.label, path))
        if args.sidecar:
            if sc is None:
                from draft_ops.draft_content_audit import SIDECAR_NAME

                print(f"-- 未找到 {SIDECAR_NAME}(草稿目录里没有 sidecar)")
            else:
                print("\n".join(sidecar_summary(sc)))

    failed: list[str] = []
    for key, n in mins:
        if rep["metrics"][key] < n:
            failed.append(f"{key}={rep['metrics'][key]} 小于要求的最小值 {n}")
    for key, n in maxs:
        if rep["metrics"][key] > n:
            failed.append(f"{key}={rep['metrics'][key]} 大于允许的最大值 {n}")
    if failed:
        print("门禁不通过:", file=sys.stderr)
        for f in failed:
            print(f"  - {f}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

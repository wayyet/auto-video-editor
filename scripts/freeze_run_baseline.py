"""冻结一次运行的任务基线证据(只读;《剪映草稿效果缺失根因分析与修复设计
执行计划_20261108》阶段 0)。

用途
--------

改代码之前拿到指定 Thread 的真实文件、节点状态和草稿结构,避免凭节点名
推断运行结果。对 ``console-d103e894da2b`` 的首次冻结是本脚本的验收用例。

.. code-block:: powershell

    # 常规取证(任务目录盘点 + staging/官方双草稿 SHA/计数/引用完整性 + 备份)
    .\\.venv\\Scripts\\python.exe scripts\\freeze_run_baseline.py --thread-id console-d103e894da2b

    # 额外导出 Postgres checkpoint 当前 state 与挂起 interrupt(只读 aget_state)
    .\\.venv\\Scripts\\python.exe scripts\\freeze_run_baseline.py --thread-id console-d103e894da2b --dump-state

行为与边界
--------
- **只读**:不恢复工作流、不写任务目录与草稿;checkpointer 仅 ``aget_state``
  (同 ``resume_workflow.py --dry-run`` 的只读语义;但本脚本不跑 preflight,
  Postgres 需自行可达,容器 ``edb-postgres16`` 不自启)。
- 盘点排除大媒体目录 ``inputs/`` 与 ``normalized_sources/``(只记目录存在,
  不逐文件列出、不备份);其余(console_run.*、manifest.json、initial_state.json、
  ``storyline/``、``assembly/``、``drafts/`` 等)全量列表并备份。
- staging 草稿 = ``outputs/<tid>/drafts/``;官方草稿 = 剪映草稿库
  ``AutoVideo_<tid>``(``jianying_registry.official_draft_dir``)。两者分别做
  SHA-256(``draft_ops.draft_lock.hash_draft_files``)、轨道/素材计数与
  ``draft_ops.draft_integrity.check_draft_dir`` 结构校验。
- 备份 + ``report.json`` + ``report.md`` 落在 ``--out-dir``
  (默认 ``outputs/baseline-<thread_id>/``;``outputs/`` 已 gitignore,本地留存;
  ``report.md``/``report.json`` 摘要可人工拷入 docs 执行记录)。
- checkpoint state 值整体落 ``state_values.json``(仅 ``--dump-state``);
  ``report`` 只带摘要(键清单 / draft_path / status_log 尾部 / 挂起 interrupt)。

退出码:``0`` 成功(部分证据缺失只影响 report 内容);``1`` 参数/致命错误。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 让 ``import config`` / ``import graph`` 能解析到项目根
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 默认 stdout 是 GBK;任务/素材路径含中文,强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

#: 盘点/备份时跳过的大媒体目录(相对任务目录;只记存在性,不逐文件展开)
SKIP_HEAVY_DIRS = ("inputs", "normalized_sources")
#: 备份的证据目录/文件(任务目录下的相对路径;其余顶层文件也一并备份)
BACKUP_DIRS = ("storyline", "assembly", "drafts")

STAGING_DRAFT_FILES = ("draft_content.json", "draft_info.json")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="freeze_run_baseline",
        description="冻结一次运行的任务基线证据(只读;阶段 0)",
    )
    p.add_argument("--thread-id", required=True, help="LangGraph thread_id(必填)")
    p.add_argument(
        "--out-dir",
        default=None,
        help="证据输出目录(默认 outputs/baseline-<thread_id>/)",
    )
    p.add_argument(
        "--dump-state",
        action="store_true",
        help="额外只读导出 Postgres checkpoint 当前 state + 挂起 interrupt",
    )
    p.add_argument(
        "--skip-backup",
        action="store_true",
        help="只出报告,不复制证据文件(报告仍落 --out-dir)",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# 任务目录盘点
# ---------------------------------------------------------------------------
def locate_task_dir(thread_id: str) -> Path:
    """创建台任务目录:CONSOLE_OUTPUTS_ROOT 优先,缺省 <repo>/outputs。"""
    import config

    base = Path(config.CONSOLE_OUTPUTS_ROOT)
    return base / thread_id


def inventory_dir(task_dir: Path) -> dict[str, Any]:
    """任务目录文件清单(相对路径 / 字节 / mtime ISO);大媒体目录只记存在。"""
    inv: dict[str, Any] = {"files": [], "heavy_dirs": {}, "total_bytes": 0}
    if not task_dir.is_dir():
        return inv
    for entry in sorted(task_dir.iterdir()):
        if entry.name in SKIP_HEAVY_DIRS:
            inv["heavy_dirs"][entry.name] = entry.is_dir()
            continue
        if entry.is_file():
            st = entry.stat()
            inv["files"].append(
                {
                    "path": entry.name,
                    "bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
            inv["total_bytes"] += st.st_size
        elif entry.is_dir():
            for f in sorted(entry.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(task_dir).as_posix()
                st = f.stat()
                inv["files"].append(
                    {
                        "path": rel,
                        "bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
                inv["total_bytes"] += st.st_size
    return inv


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 草稿结构
# ---------------------------------------------------------------------------
def draft_counts(draft: dict) -> dict[str, Any]:
    """按 type 统计轨道/段,按 bucket 统计素材(计数专用,不校验)。"""
    tracks: dict[str, dict[str, int]] = {}
    for t in draft.get("tracks") or []:
        ttype = str(t.get("type") or "unknown")
        segs = list(t.get("segments") or [])
        agg = tracks.setdefault(ttype, {"tracks": 0, "segments": 0})
        agg["tracks"] += 1
        agg["segments"] += len(segs)
    materials = {
        str(k): len(list(v or []))
        for k, v in (draft.get("materials") or {}).items()
        if isinstance(v, list)
    }
    return {"tracks": tracks, "materials": materials, "duration_us": draft.get("duration")}


def inspect_draft_dir(draft_dir: Path, *, tag: str, registered_expected: bool) -> dict[str, Any]:
    """单草稿目录:SHA-256 + 计数 + draft_integrity 结构校验,合并成报告块。"""
    from draft_ops.draft_integrity import check_draft_dir
    from draft_ops.draft_lock import hash_draft_files
    from draft_ops.jianying_registry import resolve_draft_root

    block: dict[str, Any] = {
        "tag": tag,
        "draft_dir": str(draft_dir),
        "present": draft_dir.is_dir(),
        "sha256": {},
        "counts": None,
        "integrity": {"issues": [], "checks": []},
    }
    content_file = draft_dir / "draft_content.json"
    if not content_file.is_file():
        block["integrity"]["issues"].append(
            {"severity": "error", "message": f"draft_content.json 不存在: {content_file}"}
        )
        return block

    block["sha256"] = hash_draft_files(content_file)
    try:
        draft = json.loads(content_file.read_text(encoding="utf-8"))
        block["counts"] = draft_counts(draft)
    except (OSError, ValueError) as e:
        block["integrity"]["issues"].append({"severity": "error", "message": f"解析失败: {e!r}"})
        return block

    root = resolve_draft_root() if registered_expected else None
    result = check_draft_dir(draft_dir, tag=tag, registered_expected=registered_expected, root=root)
    block["integrity"] = {
        "checks": result.get("checks") or [],
        "issues": result.get("issues") or [],
    }
    return block


# ---------------------------------------------------------------------------
# checkpoint state(只读)
# ---------------------------------------------------------------------------
async def dump_checkpoint_state(thread_id: str) -> dict[str, Any]:
    """aget_state 读当前 state + 挂起 interrupt(只读;不 resume)。"""
    from graph import build_graph, get_checkpointer, normalize_thread_id

    tid = normalize_thread_id(thread_id)
    async with get_checkpointer() as saver:
        g = build_graph(
            checkpointer=saver,
            thread_id=tid,
            start_heartbeat_thread=False,
            run_preflight=False,
        )
        snapshot = await g.aget_state({"configurable": {"thread_id": tid}})
    values = dict(snapshot.values or {})
    interrupts = []
    for i in list(getattr(snapshot, "interrupts", None) or []):
        v = i.value if isinstance(i.value, dict) else {"value": i.value}
        interrupts.append(
            {
                "id": i.id,
                "checkpoint": v.get("checkpoint"),
                "legacy_id": v.get("legacy_id"),
                "step": v.get("step"),
                "instructions": v.get("instructions"),
                "payload": v,
            }
        )
    return {"thread_id": tid, "values": values, "interrupts": interrupts}


STATE_SUMMARY_KEYS = (
    "draft_path",
    "draft_dir_en_branch",
    "snapshot_path",
    "snapshot2_path",
    "video_input_path",
    "storyline_voiceover_artifact",
    "storyline_script_artifact",
    "storyline_timeline_plan",
    "join_qa_report",
    "final_export_status",
)


def summarize_state(values: dict[str, Any]) -> dict[str, Any]:
    """report 用的 state 摘要(全量值另落 state_values.json)。"""
    summary: dict[str, Any] = {"state_keys": sorted(values.keys())}
    for key in STATE_SUMMARY_KEYS:
        if key in values:
            summary[key] = values.get(key)
    status_log = values.get("status_log")
    if isinstance(status_log, list):
        summary["status_log_tail"] = [str(x) for x in status_log[-30:]]
    error_log = values.get("error_log")
    if isinstance(error_log, list):
        summary["error_log_tail"] = [str(x) for x in error_log[-30:]]
    return summary


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def backup_evidence(task_dir: Path, out_dir: Path) -> dict[str, Any]:
    """复制证据文件到 out_dir/backup/(保持相对结构);返回复制统计。"""
    backup_root = out_dir / "backup"
    copied: list[str] = []
    skipped: list[str] = []
    if not task_dir.is_dir():
        return {"backup_root": str(backup_root), "copied": copied, "skipped": skipped}

    for entry in sorted(task_dir.iterdir()):
        if entry.name in SKIP_HEAVY_DIRS:
            skipped.append(entry.name)
            continue
        dest = backup_root / entry.name
        if entry.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, dest)
            copied.append(entry.name)
        elif entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
            copied.append(entry.name + "/")
    return {"backup_root": str(backup_root), "copied": copied, "skipped": skipped}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def render_report_md(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# 运行基线冻结报告 — {report['thread_id']}")
    lines.append("")
    lines.append(f"- 冻结时间: {report['frozen_at']}")
    lines.append(f"- 任务目录: {report['task_dir']} (存在: {report['task_dir_present']})")
    lines.append(f"- 大媒体目录(未展开/未备份): {', '.join(report['heavy_dirs']) or '无'}")
    lines.append("")
    lines.append("## 双草稿证据")
    lines.append("")
    for block in report["drafts"]:
        lines.append(f"### {block['tag']} — `{block['draft_dir']}`")
        lines.append("")
        lines.append(f"- 存在: {block['present']}")
        if block["sha256"]:
            for name, sha in block["sha256"].items():
                lines.append(f"- sha256[{name}] = `{sha}`")
        counts = block.get("counts")
        if counts:
            lines.append(f"- duration_us: {counts.get('duration_us')}")
            lines.append(f"- tracks: `{json.dumps(counts.get('tracks'), ensure_ascii=False)}`")
            lines.append(f"- materials: `{json.dumps(counts.get('materials'), ensure_ascii=False)}`")
        issues = (block.get("integrity") or {}).get("issues") or []
        if issues:
            lines.append(f"- 完整性问题 ×{len(issues)}:")
            for i in issues[:20]:
                lines.append(f"  - [{i.get('severity')}] {i.get('message') or i}")
        else:
            lines.append("- 完整性问题: 无")
        lines.append("")
    if report.get("state_summary") is not None:
        s = report["state_summary"]
        lines.append("## checkpoint state 摘要(只读)")
        lines.append("")
        lines.append(f"- state 键 ×{len(s.get('state_keys') or [])}")
        for key in STATE_SUMMARY_KEYS:
            if key in s:
                lines.append(f"- {key} = `{s[key]}`")
        interrupts = report.get("interrupts") or []
        lines.append(f"- 挂起 interrupt ×{len(interrupts)}")
        for i in interrupts:
            lines.append(
                f"  - checkpoint={i.get('checkpoint')!r} legacy_id={i.get('legacy_id')!r} "
                f"step={i.get('step')} id={i.get('id')}"
            )
            if i.get("instructions"):
                lines.append(f"    指引: {i['instructions']}")
        tail = s.get("status_log_tail") or []
        if tail:
            lines.append("- status_log 尾部:")
            lines.extend(f"  - {x}" for x in tail)
        lines.append("")
    lines.append("## 文件清单")
    lines.append("")
    inv = report.get("inventory") or {}
    lines.append(f"- 文件 ×{len(inv.get('files') or [])},合计 {inv.get('total_bytes', 0)} 字节")
    for f in inv.get("files") or []:
        lines.append(f"  - {f['path']} ({f['bytes']} B)")
    lines.append("")
    lines.append("## 备份")
    lines.append("")
    bk = report.get("backup") or {}
    lines.append(f"- 备份根: `{bk.get('backup_root')}`")
    lines.append(f"- 已复制: {', '.join(bk.get('copied') or []) or '无(--skip-backup?)'}")
    lines.append(f"- 跳过(大媒体): {', '.join(bk.get('skipped') or []) or '无'}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    thread_id = args.thread_id

    task_dir = locate_task_dir(thread_id)
    out_dir = Path(args.out_dir) if args.out_dir else task_dir.parent / f"baseline-{thread_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "thread_id": thread_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "task_dir": str(task_dir),
        "task_dir_present": task_dir.is_dir(),
        "heavy_dirs": [d for d in SKIP_HEAVY_DIRS if (task_dir / d).exists()],
        "inventory": inventory_dir(task_dir),
        "drafts": [],
        "state_summary": None,
        "interrupts": [],
        "backup": None,
    }

    # 1. staging 草稿(任务目录 drafts/;不要求注册)
    report["drafts"].append(
        inspect_draft_dir(task_dir / "drafts", tag="staging", registered_expected=False)
    )
    # 2. 官方剪映草稿库(要求已注册 + 根索引)
    from draft_ops.jianying_registry import official_draft_dir, resolve_draft_root

    official = official_draft_dir(resolve_draft_root(), thread_id)
    report["drafts"].append(
        inspect_draft_dir(official, tag="official", registered_expected=True)
    )

    # 3. checkpoint state(只读)
    if args.dump_state:
        # Windows: psycopg async 需 SelectorEventLoop(与 resume_workflow 同口径)
        import selectors

        try:
            state = asyncio.run(
                dump_checkpoint_state(thread_id),
                loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
            )
            (out_dir / "state_values.json").write_text(
                json.dumps(state["values"], ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            report["state_summary"] = summarize_state(state["values"])
            report["interrupts"] = state["interrupts"]
        except Exception as e:  # noqa: BLE001 — 证据脚本:PG 不可达时报告并继续
            report["state_summary"] = {"error": repr(e)}
            print(f"[freeze] checkpoint state 导出失败: {e!r}", file=sys.stderr)

    # 4. 备份
    if not args.skip_backup:
        report["backup"] = backup_evidence(task_dir, out_dir)

    # 5. 报告落盘
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_report_md(report), encoding="utf-8")

    print(f"[freeze] 报告: {out_dir / 'report.md'}")
    print(f"[freeze] 报告: {out_dir / 'report.json'}")
    for block in report["drafts"]:
        n_issues = len((block.get("integrity") or {}).get("issues") or [])
        print(
            f"[freeze] draft[{block['tag']}] present={block['present']} "
            f"issues={n_issues} sha256={list(block['sha256'].values()) or '无'}"
        )
    if report.get("interrupts"):
        for i in report["interrupts"]:
            print(
                f"[freeze] 挂起: checkpoint={i.get('checkpoint')!r} step={i.get('step')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

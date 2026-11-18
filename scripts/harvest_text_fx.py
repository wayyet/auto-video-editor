"""字幕动画全池 GUI 实证收割工具(2026-11-19 审查阶段 4 / P1-04)。

职责边界(审查 §7 阶段 4):本工具只做三件事 ——
1. ``gen``:生成小批次收割草稿(staging 双文件,可选发布到剪映草稿库);
2. ``manifest``:生成/打印单个资源的缓存 manifest(与诊断脚本共用
   :mod:`jy_common.cache_manifest`);
3. ``apply-verification``:按 GUI 实证结果文件**受控回填**验证记录。

**绝不自行伪造 ``verified``** —— 只有「GUI 预览 + 保存 + 重开」证据齐全的资源
才允许经 ``apply-verification`` 写入;回填按 ``(name, resource_id, effect_id,
md5)`` 身份精确匹配,verified 必须携带 ``evidence_path``,写库前自动备份。

审查 §4.2 明确:不采用「一份草稿放 78 条、打开一次即全部下载」假设。先用
``gen --count 3`` 做小批次实验,观察「打开/滚动/选中/动画面板/预览/保存」各
步骤哪些触发下载;确认可靠触发方式后再决定批次大小。

GUI 实证规程(每条资源,审查 §7 阶段 4.3):打开草稿 → 选中对应段 → 确认
动画面板显示目标动画名 → 播放动画区间(无丢失徽章/静止替代/闪烁/弹窗)→
保存并关闭 → 重新打开再预览 → ``manifest`` 留档 → 记录截图与剪映版本 →
结果写入 JSON 后 ``apply-verification`` 回填。

用法::

    python scripts/harvest_text_fx.py list
    python scripts/harvest_text_fx.py gen --count 3 --start-idx 0 --publish
    python scripts/harvest_text_fx.py manifest --effect-id 1644278 --md5 d3e2...
    python scripts/harvest_text_fx.py apply-verification --results r.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.stdout.reconfigure(encoding="utf-8")

POOL_PATH = _REPO_ROOT / "templates" / "text_resource_library.json"
#: GUI 实证失败分类(审查 §7 阶段 4.4);与 verified 并列,禁止笼统归为"下载不完整"
FAILED_STATUSES = (
    "download_failed",
    "vip_denied",
    "client_incompatible",
    "resource_unavailable",
    "render_failed",
)
_SEG_DURATION_US = 2_000_000
_SEG_GAP_US = 500_000


def _load_pool(pool_path: Path) -> dict[str, Any]:
    return json.loads(pool_path.read_text(encoding="utf-8"))


def _identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("name") or ""),
        str(row.get("resource_id") or ""),
        str(row.get("effect_id") or ""),
        str(row.get("md5") or ""),
    )


def _iter_intro_rows(lib: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in lib.get("intro", []) if row.get("is_vip")]


# --------------------------------------------------------------------------- list


def cmd_list(_args: argparse.Namespace) -> int:
    lib = _load_pool(POOL_PATH)
    rows = _iter_intro_rows(lib)
    verified = [r for r in rows if r.get("verified")]
    print(f"[pool] intro VIP 共 {len(rows)} 条;verified {len(verified)} 条:")
    for row in verified:
        record = row.get("verification") or {}
        print(
            f"  ✔ {row['name']}  effect_id={row['effect_id']}  "
            f"status={record.get('status')}  verified_at={record.get('verified_at')}"
        )
    staled = [
        r
        for r in rows
        if not r.get("verified")
        and isinstance(r.get("verification"), dict)
        and r["verification"].get("status") in ("stale", *FAILED_STATUSES)
    ]
    if staled:
        print(f"[pool] 曾验证后失效/失败 {len(staled)} 条:")
        for row in staled:
            print(
                f"  ✖ {row['name']}  effect_id={row['effect_id']}  "
                f"status={(row.get('verification') or {}).get('status')}  "
                f"note={(row.get('verification') or {}).get('note')}"
            )
    print("[pool] 其余为 metadata_only(未验证,守卫不可选)。")
    return 0


# ---------------------------------------------------------------------------- gen


def _build_harvest_staging(rows: list[dict[str, Any]], staging_dir: Path) -> Path:
    """按池条目子集生成收割 staging 草稿(双文件一致;每条资源一个 2s 文字段)。"""
    texts: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    animations: list[dict[str, Any]] = []

    from jy_common.native_fx import build_native_text_animation_material

    for idx, row in enumerate(rows):
        start = idx * (_SEG_DURATION_US + _SEG_GAP_US)
        text_id = f"harvest-{idx:02d}"
        anim_id = f"text-anim-{text_id}"
        display = str(row.get("name") or f"待验证{idx}")
        content = json.dumps(
            {"styles": [{"range": [0, len(display)], "size": 8.0}], "text": display},
            ensure_ascii=False,
        )
        texts.append(
            {
                "id": text_id,
                "type": "text",
                "content": content,
                "target_timerange": {"start": start, "duration": _SEG_DURATION_US},
                "style": {"size": 8.0},
            }
        )
        animations.append(
            build_native_text_animation_material(
                material_id=anim_id, anim_meta=row, kind="in"
            )
        )
        segments.append(
            {
                "id": f"harvest-seg-{idx:02d}",
                "material_id": text_id,
                "target_timerange": {"start": start, "duration": _SEG_DURATION_US},
                "extra_material_refs": [anim_id],
            }
        )

    total = (
        len(rows) * _SEG_DURATION_US
        + max(len(rows) - 1, 0) * _SEG_GAP_US
    )
    draft: dict[str, Any] = {
        "new_version": "110.0.0",  # 原生形态标记:publisher 幂等直通
        "id": f"harvest-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
        "duration": total,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"texts": texts, "material_animations": animations},
        "tracks": [
            {
                "type": "text",
                "id": "track-harvest",
                "segments": segments,
            }
        ],
    }
    serialized = json.dumps(draft, ensure_ascii=False, separators=(",", ":"))
    staging_dir.mkdir(parents=True, exist_ok=True)
    for name in ("draft_content.json", "draft_info.json"):
        (staging_dir / name).write_text(serialized, encoding="utf-8")

    plan = [
        {"index": idx, "name": r.get("name"), **dict(zip(
            ("resource_id", "effect_id", "md5"), _identity(r)[1:]
        ))}
        for idx, r in enumerate(rows)
    ]
    (staging_dir / "harvest_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return staging_dir


def cmd_gen(args: argparse.Namespace) -> int:
    lib = _load_pool(POOL_PATH)
    rows = _iter_intro_rows(lib)

    if args.names:
        wanted = [n.strip() for n in args.names.split(",") if n.strip()]
        by_name = {r["name"]: r for r in rows}
        missing = [n for n in wanted if n not in by_name]
        if missing:
            print(f"[gen] 池内不存在: {missing}", file=sys.stderr)
            return 2
        selected = [by_name[n] for n in wanted]
    else:
        selected = rows[args.start_idx : args.start_idx + args.count]
        if not selected:
            print("[gen] 选段为空(--start-idx 越界?)", file=sys.stderr)
            return 2

    staging = _build_harvest_staging(
        selected,
        _REPO_ROOT / "outputs" / "harvest" / f"staging-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
    )
    print(f"[gen] staging: {staging}")
    print(f"[gen] 计划 {len(selected)} 条 → {staging / 'harvest_plan.json'}")

    if not args.publish:
        print("[gen] 未发布(--publish 才会注册进剪映草稿库);先在 GUI 验证触发条件。")
        return 0

    from draft_ops.draft_publisher import DraftPublishError, publish_draft

    try:
        result = publish_draft(
            staging,
            thread_id=args.thread_id,
            duration_us=selected and (
                len(selected) * _SEG_DURATION_US
                + max(len(selected) - 1, 0) * _SEG_GAP_US
            ),
        )
    except DraftPublishError as exc:
        print(f"[gen] 发布失败: {exc.reason}", file=sys.stderr)
        return 1
    print(f"[gen] 已发布: {result['published_dir']}(registry draft_id={result['registry'].get('draft_id')})")
    print("[gen] 下一步:打开草稿 → 按 §4.3 规程逐条实证 → manifest → apply-verification")
    return 0


# ----------------------------------------------------------------------- manifest


def cmd_manifest(args: argparse.Namespace) -> int:
    import config

    from jy_common.cache_manifest import build_cache_manifest

    cache_root = Path(args.cache_root) if args.cache_root else Path(config.JIANYING_CACHE_ROOT)
    manifest = build_cache_manifest(cache_root, args.effect_id, args.md5)
    if manifest is None:
        print(f"[manifest] 缓存缺失: {cache_root / args.effect_id / args.md5}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[manifest] written: {out}")
    return 0


# -------------------------------------------------------------- apply-verification


def _backup_pool(pool_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup = pool_path.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(pool_path, backup)
    return backup


def cmd_apply_verification(args: argparse.Namespace) -> int:
    lib = _load_pool(Path(args.pool))
    try:
        results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[apply] 结果文件读取失败: {exc}", file=sys.stderr)
        return 2
    if not isinstance(results, list):
        print("[apply] 结果文件应为 JSON 数组", file=sys.stderr)
        return 2

    index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for section in ("intro", "loop", "outro"):
        for row in lib.get(section, []):
            index[_identity(row)] = row

    applied = 0
    rejected: list[dict[str, Any]] = []
    for record in results:
        if not isinstance(record, dict):
            rejected.append({"reason": "非对象条目", "record": record})
            continue
        key = (
            str(record.get("name") or ""),
            str(record.get("resource_id") or ""),
            str(record.get("effect_id") or ""),
            str(record.get("md5") or ""),
        )
        row = index.get(key)
        if row is None:
            rejected.append({"reason": "身份四元组与池不匹配(拒绝回填)", "record": record})
            continue
        status = str(record.get("status") or "")
        if status == "verified":
            if not record.get("evidence_path"):
                rejected.append(
                    {"reason": "verified 缺 evidence_path(GUI 证据必填)", "record": record}
                )
                continue
            row["verified"] = True
            row["verification"] = {
                "status": "verified",
                "verified_at": record.get("verified_at")
                or datetime.now(timezone.utc).date().isoformat(),
                "jianying_version": record.get("jianying_version"),
                "resource_identity": {
                    "resource_id": key[1],
                    "effect_id": key[2],
                    "md5": key[3],
                },
                "cache_manifest_sha256": record.get("cache_manifest_sha256"),
                "evidence_path": record.get("evidence_path"),
                "note": record.get("note"),
            }
            applied += 1
        elif status in FAILED_STATUSES:
            if row.get("verified"):
                row.pop("verified", None)
            row["verification"] = {
                "status": status,
                "verified_at": datetime.now(timezone.utc).date().isoformat(),
                "evidence_path": record.get("evidence_path"),
                "note": record.get("note"),
            }
            applied += 1
        else:
            rejected.append(
                {"reason": f"status={status or '(空)'} 不在回填范围(verified/{'/'.join(FAILED_STATUSES)})", "record": record}
            )

    summary = {
        "applied": applied,
        "rejected": rejected,
        "pool": str(args.pool),
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(
        {
            **summary,
            "rejected_count": len(rejected),
            "rejected_preview": rejected[:5],
        },
        ensure_ascii=False, indent=2,
    ))
    if args.dry_run:
        print("[apply] dry-run:不写池文件。")
        return 0
    if applied == 0:
        print("[apply] 无可回填条目,池文件不改动。")
        return 1
    backup = _backup_pool(Path(args.pool))
    Path(args.pool).write_text(
        json.dumps(lib, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[apply] 已回填 {applied} 条;池备份: {backup}")
    return 0


# ---------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="字幕动画全池 GUI 实证收割工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="池条目验证状态概览")

    p_gen = sub.add_parser("gen", help="生成小批次收割草稿")
    p_gen.add_argument("--names", default="", help="逗号分隔动画名(优先于 --start-idx/--count)")
    p_gen.add_argument("--start-idx", type=int, default=0, help="按池序起始 index")
    p_gen.add_argument("--count", type=int, default=3, help="批次大小(小批次实验建议 3~5)")
    p_gen.add_argument("--thread-id", default="textfx-harvest-20261119", help="收割草稿目录名后缀")
    p_gen.add_argument("--publish", action="store_true", help="发布到剪映草稿库(剪映在跑会被拒绝)")

    p_mf = sub.add_parser("manifest", help="生成单个资源缓存 manifest")
    p_mf.add_argument("--effect-id", required=True)
    p_mf.add_argument("--md5", required=True)
    p_mf.add_argument("--cache-root", default="")
    p_mf.add_argument("--out", default="")

    p_apply = sub.add_parser("apply-verification", help="按 GUI 实证结果受控回填验证记录")
    p_apply.add_argument("--results", required=True, help="GUI 实证结果 JSON 数组文件")
    p_apply.add_argument("--pool", default=str(POOL_PATH))
    p_apply.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    handler = {
        "list": cmd_list,
        "gen": cmd_gen,
        "manifest": cmd_manifest,
        "apply-verification": cmd_apply_verification,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

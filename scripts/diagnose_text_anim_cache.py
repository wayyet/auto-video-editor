"""字幕动画缓存诊断基线收集(2026-11-19 审查阶段 0 / P1-05,只读)。

在动任何修复/收割动作之前,把本机可复现证据固化成 JSON:

- 环境:剪映版本、Windows 用户、``Path.home()``、实际 ``JIANYING_CACHE_ROOT``、
  缓存根存在性(P1-06:Worker 与剪映 GUI 必须读同一缓存位置);
- 资源缓存 manifest:失败样本(螺旋上升)与正常样本(兔子弹跳/逐字旋入/
  冲屏位移)及标题候选的逐文件清单 + SHA-256 + config 版本 —— 供区分
  「下载不完整 / 版本不兼容 / 权限限制」,不得以文件数量单独下结论;
- 草稿备份:zh/en ``draft_content.json`` 原样复制 + SHA-256(不覆盖原始草稿);
- 剪映日志目录清单(名字/大小/mtime;内容摘录留人工)。

不做任何写剪映库/删缓存动作。审查 §7 阶段 0 要求的四时点快照(打开前/打开后/
应用动画后/预览后/保存后)由操作员按规程多次运行本脚本收集,``--label`` 区分。

用法::

    python scripts/diagnose_text_anim_cache.py                     # 全基线
    python scripts/diagnose_text_anim_cache.py --label open-after  # 时点快照
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from jy_common.cache_manifest import build_cache_manifest  # noqa: E402
from jy_common.hook_title import HOOK_ANIMATION_CANDIDATES  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_OUT = _REPO_ROOT / "outputs" / "diagnostics" / "text-animation-cache-baseline-20261119"

#: 固定复现样本(审查 §7 阶段 0 任务 1):失败样本 + 正常样本 + 标题候选。
#: 身份字段来自池 JSON / hook_title 内置元数据,不以动画名称单独识别资源。
_SUBJECT_NAMES_FROM_POOL = ("螺旋上升", "兔子弹跳", "逐字旋入")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_subjects(repo_root: Path) -> list[dict[str, Any]]:
    """从池 JSON + hook_title 内置元数据取固定样本的资源身份。"""
    subjects: list[dict[str, Any]] = []
    pool_path = repo_root / "templates" / "text_resource_library.json"
    try:
        intro = json.loads(pool_path.read_text(encoding="utf-8")).get("intro", [])
    except (OSError, ValueError):
        intro = []
    for name in _SUBJECT_NAMES_FROM_POOL:
        row = next((r for r in intro if r.get("name") == name), None)
        if row is not None:
            subjects.append(
                {
                    "name": name,
                    "resource_id": row.get("resource_id"),
                    "effect_id": row.get("effect_id"),
                    "md5": row.get("md5"),
                    "source": "text_resource_library.json intro",
                    "verified": bool(row.get("verified")),
                    "note": "失败样本:草稿console-b0b2a64a78c6动画丢失"
                    if name == "螺旋上升"
                    else "正常样本",
                }
            )
    for cand in HOOK_ANIMATION_CANDIDATES:
        subjects.append(
            {
                "name": cand.get("name"),
                "resource_id": cand.get("resource_id"),
                "effect_id": cand.get("effect_id"),
                "md5": cand.get("md5"),
                "source": "jy_common/hook_title.py HOOK_ANIMATION_CANDIDATES",
                "verified": bool(cand.get("verified")),
                "note": "标题动画候选(免费内置档)",
            }
        )
    return subjects


def _collect_environment(cache_root: Path) -> dict[str, Any]:
    from config import JIANYING_VERSION

    gui_cache_default = Path.home() / "AppData" / "Local" / "JianyingPro" / "User Data" / "Cache" / "effect"
    return {
        "jianying_version_config": JIANYING_VERSION,
        "windows_user": getpass.getuser(),
        "path_home": str(Path.home()),
        "jianying_cache_root": str(cache_root),
        "cache_root_exists": cache_root.is_dir(),
        "cache_root_matches_default_home": cache_root.resolve() == gui_cache_default.resolve()
        if cache_root.exists()
        else None,
        "gui_default_cache_root": str(gui_cache_default),
        "note": "P1-06:Worker 缓存根必须与剪映 GUI 用户缓存目录完全一致,否则守卫会把"
        "GUI 实际存在的缓存判为缺失",
    }


def _collect_draft_backups(out_dir: Path) -> list[dict[str, Any]]:
    """备份 zh/en 原始草稿 draft_content.json(只复制,不动原稿)。"""
    import config

    records: list[dict[str, Any]] = []
    draft_root = Path(config.JIANYING_DRAFT_ROOT) if hasattr(config, "JIANYING_DRAFT_ROOT") else None
    if draft_root is None or not draft_root.is_dir():
        return records
    candidates = {
        "console-b0b2a64a78c6": draft_root / "AutoVideo_console-b0b2a64a78c6",
        "console-b0b2a64a78c6-en": draft_root / "en_branch_console-b0b2a64a78c6_20261118_235720_944093",
    }
    for label, src_dir in candidates.items():
        src = src_dir / "draft_content.json"
        if not src.is_file():
            records.append({"label": label, "source": str(src), "found": False})
            continue
        dst_dir = out_dir / "draft_backup" / label
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "draft_content.json"
        dst.write_bytes(src.read_bytes())
        records.append(
            {
                "label": label,
                "source": str(src),
                "backup": str(dst),
                "sha256": _sha256_file(src),
                "size": src.stat().st_size,
                "found": True,
            }
        )
    return records


def _collect_log_listing() -> list[dict[str, Any]]:
    log_dir = Path.home() / "AppData" / "Local" / "JianyingPro" / "User Data" / "Log"
    if not log_dir.is_dir():
        return []
    listing = []
    for p in sorted(log_dir.glob("*.log"), key=lambda x: x.stat().st_mtime):
        listing.append(
            {
                "file": p.name,
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return listing


def main(argv: list[str] | None = None) -> int:
    import config

    parser = argparse.ArgumentParser(description="字幕动画缓存诊断基线收集(只读)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    parser.add_argument(
        "--cache-root", type=Path, default=None,
        help="缓存根(缺省读 config.JIANYING_CACHE_ROOT;约定为 Cache/effect 目录本身)",
    )
    parser.add_argument("--label", default="baseline", help="快照标签(时点快照用,如 open-after)")
    parser.add_argument("--skip-drafts", action="store_true", help="跳过草稿备份")
    args = parser.parse_args(argv)

    cache_root = Path(args.cache_root) if args.cache_root else Path(config.JIANYING_CACHE_ROOT)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
    out_dir = args.out / f"{args.label}-{stamp}"

    environment = _collect_environment(cache_root)
    subjects = _collect_subjects(_REPO_ROOT)

    manifests = []
    manifest_dir = out_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    for subject in subjects:
        manifest = build_cache_manifest(
            cache_root, subject["effect_id"], subject["md5"]
        )
        record = {**subject, "cache_missing": manifest is None, "manifest": manifest}
        manifests.append(record)
        manifest_file = manifest_dir / f"{subject['name']}__{subject['effect_id']}.json"
        manifest_file.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    draft_records = [] if args.skip_drafts else _collect_draft_backups(out_dir)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": args.label,
        "environment": environment,
        "subjects": [
            {
                k: v
                for k, v in m.items()
                if k != "manifest"
            }
            | {"manifest_sha256": (m["manifest"] or {}).get("manifest_sha256"),
               "file_count": (m["manifest"] or {}).get("file_count"),
               "config_version": (m["manifest"] or {}).get("config_version"),
               "total_size": (m["manifest"] or {}).get("total_size")}
            for m in manifests
        ],
        "draft_backups": draft_records,
        "log_files": _collect_log_listing(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[diagnose] report: {out_dir / 'report.json'}")
    for m in manifests:
        state = "cache_missing" if m["cache_missing"] else (
            f"files={m['manifest']['file_count']} cfg_v={m['manifest']['config_version']}"
        )
        print(f"[diagnose] {m['name']:<6} effect_id={m['effect_id']:<10} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

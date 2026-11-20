"""剪映特效缓存 manifest(2026-11-19 审查阶段 0 / P2-03)。

「缓存目录存在」≠「资源包完整可用」——「螺旋上升」坏包只有 4 个文件仍占着目录
(2026-11-19 根因实证)。本模块把缓存目录内容固化为可审计清单:逐文件相对路径/
大小/SHA-256 + ``config.json`` 的 version 字段 + 整个清单的摘要。

用法:
- GUI 实证时随验证记录写入 ``verification.cache_manifest_sha256``;
- 诊断/收割脚本用它输出资源缓存全清单(阶段 0 交付物);
- 后续可在任务开始前做一次轻量预检:清单摘要与已验证记录不一致 → 验证标
  ``stale``(P2-03 失效机制的基础;运行时预检暂未接入流水线)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_cache_manifest(
    cache_root: str | Path, effect_id: str, md5: str
) -> dict[str, Any] | None:
    """生成单个资源 ``<cache_root>/<effect_id>/<md5>`` 的缓存 manifest。

    ``cache_root`` 由调用方决定:字幕动画传 ``Cache/effect``(JIANYING_CACHE_ROOT),
    原生花字传 ``Cache/artistEffect``(``JIANYING_ARTIST_EFFECT_CACHE_ROOT``,
    2026-11-21 审查 R-04 —— 两类资源包在不同缓存根,不可混用)。目录不存在
    (或不是目录)返回 ``None`` —— 调用方按 cache_missing 处理。

    Returns:
        ``{"effect_id", "md5", "cache_root", "file_count", "total_size",
        "config_version", "files": [{"path","size","sha256"}...],
        "manifest_sha256"}``;``manifest_sha256`` 对 files 数组(规范化 JSON,
        sort_keys)取 SHA-256,同目录内容稳定复现。
    """
    if not effect_id or not md5:
        return None
    root = Path(cache_root) / str(effect_id) / str(md5)
    if not root.is_dir():
        return None

    files: list[dict[str, Any]] = []
    config_version: str | None = None
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        files.append(
            {"path": rel, "size": p.stat().st_size, "sha256": _sha256_file(p)}
        )
        if rel == "config.json":
            try:
                config_version = str(
                    json.loads(p.read_text(encoding="utf-8")).get("version")
                )
            except (OSError, ValueError):
                config_version = None

    manifest_digest = hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "effect_id": str(effect_id),
        "md5": str(md5),
        "cache_root": str(Path(cache_root)),
        "file_count": len(files),
        "total_size": sum(f["size"] for f in files),
        "config_version": config_version,
        "files": files,
        "manifest_sha256": manifest_digest,
    }


__all__ = ["build_cache_manifest"]

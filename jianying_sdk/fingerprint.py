"""DraftBuildSpec 内容指纹(迁移设计文档 §5.3/§11;阶段1)。

SDK 构建对象携带随机 UUID —— 同一 spec 两次编译产出字节级不同的 JSON,
重试/恢复路径会产生无意义文件变化与重放比较困难。约定:

- 指纹覆盖 spec 的**全部构建内容**(schema_version、canvas、timeline、
  subtitles、text_decorations、video_effects、video_animations、transitions、
  stickers、job_id、language),**不含** ``compile`` 块自身。
- 指纹一致 → 跳过重编译(:func:`needs_compile` 返回 False)。
- 指纹变化 → **从空白 SDK 草稿完整重建**(不做增量 upsert,避免残留 refs)。
"""
from __future__ import annotations

import hashlib
import json

from jianying_sdk.spec import DraftBuildSpec

#: 参与指纹的字段(顺序无关,序列化按 sort_keys 规范化)
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "job_id",
    "language",
    "canvas",
    "timeline",
    "subtitles",
    "text_decorations",
    "video_effects",
    "video_animations",
    "transitions",
    "stickers",
)


def compute_build_fingerprint(spec: DraftBuildSpec) -> str:
    """对 spec 的构建内容计算规范化 SHA-256 指纹。"""
    dumped = spec.model_dump()
    payload = {key: dumped[key] for key in _FINGERPRINT_FIELDS}
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def needs_compile(spec: DraftBuildSpec) -> bool:
    """指纹未记录或不一致 → True(需要编译)。"""
    return spec.compile.last_compiled_fingerprint != compute_build_fingerprint(spec)


__all__ = ["compute_build_fingerprint", "needs_compile"]

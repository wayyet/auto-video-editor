"""SDK 编译结果 → 项目安全写入器的发布桥接(迁移设计文档 §5.5;阶段1)。

``ScriptFile.dump()`` / ``save()`` 是普通文件写入,**不含**本项目已有的
快照、双写、校验、回滚与审计。本桥接只取 ``ScriptFile.dumps()`` 的 JSON
dict,统一交给 :func:`draft_ops.atomic_writer.safe_write_draft` 完成:

    DraftBuildSpec → ScriptFile → dumps() → validate_sdk_payload()
    → safe_write_draft(draft_dir, payload) → verify_draft_loadable()
    → 成功 / 快照回退

SDK 编译在内存中完成,失败不触盘;发布失败由 safe_write_draft 从快照整目录
回退 —— 两层都满足"编译/发布失败不覆盖当前有效草稿"(§9 阶段1)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from draft_ops.atomic_writer import safe_write_draft


def validate_sdk_payload(payload: Any) -> None:
    """发布前的最小结构防线(fail-closed,不修改 payload)。

    校验 SDK ``dumps()`` 产物的顶层形状:canvas_config / tracks / materials
    必须存在且类型正确;轨道必须有 type 与 segments。结构不对说明编译产物
    不可信,宁可拒绝发布也不写出残缺草稿。
    """
    if not isinstance(payload, dict):
        raise ValueError(f"SDK 载荷必须是 dict,实际 {type(payload).__name__}")
    canvas = payload.get("canvas_config")
    if not isinstance(canvas, dict):
        raise ValueError("SDK 载荷缺少 canvas_config(dict)")
    for key in ("width", "height"):
        value = canvas.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"canvas_config.{key} 必须为正整数,实际 {value!r}")
    if not isinstance(payload.get("materials"), dict):
        raise ValueError("SDK 载荷缺少 materials(dict)")
    tracks = payload.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("SDK 载荷 tracks 必须为非空列表")
    for index, track in enumerate(tracks):
        if not isinstance(track, dict) or not track.get("type"):
            raise ValueError(f"tracks[{index}] 缺少 type 字段")
        if not isinstance(track.get("segments"), list):
            raise ValueError(f"tracks[{index}].segments 必须为列表")


def publish_compiled_draft(
    draft_dir: Path | str,
    payload: dict[str, Any],
    *,
    duration_us: int | None = None,
    **safe_write_kwargs: Any,
) -> dict[str, Any]:
    """把 SDK 编译载荷经 ``safe_write_draft`` 安全发布到草稿目录。

    Args:
        draft_dir: 草稿**目录**(safe_write_draft 双写 draft_content.json +
            draft_info.json;目录级入参契约见 nodes/_draft_io.py 模块注释)。
        payload: ``ScriptFile.dumps()`` 反序列化后的 dict。
        duration_us: 成片时长(µs),同步时长索引;None 不更新。
        **safe_write_kwargs: 透传 safe_write_draft 的可注入钩子
            (proc_checker/snapshotter/restorer/verifier 等,测试隔离用)。

    Returns:
        ``safe_write_draft`` 的结果字典(ok/snapshot/verified/...)。

    Raises:
        ValueError: payload 结构校验失败(发布前防线)。
        RuntimeError: 剪映互斥 / 校验失败(已回退)/ 回退失败(见
            safe_write_draft 文档)。
    """
    validate_sdk_payload(payload)
    # dumps() 产物再反序列化一次,保证进入双写的是"纯 JSON dict"
    # (排除 SDK 对象残留引用导致的序列化意外)。
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    return safe_write_draft(Path(draft_dir), payload, duration_us=duration_us, **safe_write_kwargs)


__all__ = ["publish_compiled_draft", "validate_sdk_payload"]

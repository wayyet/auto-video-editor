"""剪映草稿库根目录与注册表维护(2026-11-06 人工关卡与剪映草稿导入 阶段 1)。

计划 §8 阶段 1:工作流草稿要"出现在剪映客户端首页",光把 ``draft_content.json``
复制进草稿库根目录不够 —— 剪映 5.9 的首页列表以根目录 ``root_meta_info.json`` 的
``all_draft_store[]`` 为准(与 :mod:`draft_ops.duration_index` 固化的契约一致),
单草稿侧还有 ``<draft_dir>/draft_meta_info.json`` 维护 ``tm_duration`` 等元数据。

本模块职责(计划 §7.3 组件表中的 ``jianying_registry.py``):
- **发现/校验**草稿库根目录:env 覆盖(config.JIANYING_DRAFT_ROOT)→ 安全性校验
  (拒绝盘根、拒绝普通文件)→ 确保 exists;
- **唯一目录命名**:``AutoVideo_<thread_id>``,不同任务互不覆盖;thread_id 中
  不安全字符保守替换为 ``_``(创建台 thread_id 已过正则,这里兜直接 CLI 入口);
- **注册**:``all_draft_store[]`` 按 ``draft_name`` upsert + 写
  ``draft_meta_info.json``;已存在的 root_meta_info.json 修改前先落 ``.bak`` 备份
  (该通配已在 config 缓存清理清单内,不会长期堆积)。

⚠️ 字段级免责声明(计划 §8 阶段 1:"按真实剪映样本验证,不能凭字段名称猜测"):
阶段 0 诊断确认本机草稿库为空、无新建草稿样本可对照。本模块的条目字段集 =
``duration_index.py`` 已固化的 5.9 契约(``draft_name`` / ``draft_id`` /
``tm_duration``)+ 常用 ``tm_draft_create`` / ``tm_draft_update`` /
``draft_root_parent``(毫秒时间戳)。**阶段 5 实机验收必须**用"剪映新建空白草稿"
产生的真实 ``root_meta_info.json`` 逐字段核对;若有出入,只改 :func:`build_store_entry`
与 :func:`build_meta_info`,调用方不动。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JYRegistryError(Exception):
    """草稿库根目录/注册表操作失败(调用方按发布失败处理)。"""


#: thread_id / 目录名里允许的字符(其余替换为 ``_``,防路径穿越与非法名)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]")

#: root_meta_info.json 备份后缀(与 config 缓存清理清单 ``*.bak`` 通配对齐)
_ROOT_META_BACKUP_SUFFIX = ".bak"


# ---------------------------------------------------------------------------
# 根目录
# ---------------------------------------------------------------------------
def sanitize_name_segment(raw: str) -> str:
    """把 thread_id 等原始段转成安全目录名片段。"""
    cleaned = _SAFE_NAME_RE.sub("_", str(raw or "").strip())
    return cleaned or "unknown"


def official_draft_dir_name(thread_id: str) -> str:
    """中文主草稿在草稿库中的目录名:``AutoVideo_<thread_id>``(计划 §8 阶段 1)。"""
    return f"AutoVideo_{sanitize_name_segment(thread_id)}"


def official_draft_dir(root: Path, thread_id: str) -> Path:
    """草稿库根目录 + 唯一目录名 → 中文主草稿的正式目录。"""
    return Path(root) / official_draft_dir_name(thread_id)


def validate_draft_root(root: Path) -> Path:
    """校验并准备草稿库根目录;不安全/不可用直接抛 :class:`JYRegistryError`。

    - 必须是绝对路径;
    - 拒绝盘根/文件系统根(误配 ``C:\\`` 会把整个盘当草稿库);
    - 已存在且是普通文件 → 拒绝;
    - 不存在则创建(parents=True)。
    """
    root = Path(root)
    if not root.is_absolute():
        raise JYRegistryError(f"剪映草稿库根目录必须是绝对路径: {root}")
    resolved = Path(os.path.abspath(str(root)))
    if resolved.parent == resolved:
        raise JYRegistryError(f"剪映草稿库根目录不能是盘根/文件系统根: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise JYRegistryError(f"剪映草稿库根目录被普通文件占用: {resolved}")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise JYRegistryError(f"剪映草稿库根目录创建失败: {resolved} ({e})") from e
    return resolved


def resolve_draft_root() -> Path:
    """从 config 读 JIANYING_DRAFT_ROOT 并校验(运行时读,单测可 monkeypatch)。"""
    import config

    return validate_draft_root(Path(config.JIANYING_DRAFT_ROOT))


# ---------------------------------------------------------------------------
# 时长换算
# ---------------------------------------------------------------------------
def compute_draft_duration_us(content: dict[str, Any]) -> int | None:
    """从 draft_content 结构里取最大 ``target_timerange`` 终点(微秒)。

    mapper 产出的片段形状是剪映原生 ``target_timerange = {start, duration}``
    (见 ``storyline/mapper.py``);取 max(start + duration)。没有任何可算片段
    时返回 ``None``(调用方按"注册不带时长"处理,不猜 0)。
    """
    best: int | None = None
    for track in (content.get("tracks") or []):
        if not isinstance(track, dict):
            continue
        for seg in (track.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            tr = seg.get("target_timerange")
            if not isinstance(tr, dict):
                continue
            try:
                end = int(tr.get("start") or 0) + int(tr.get("duration") or 0)
            except (TypeError, ValueError):
                continue
            if best is None or end > best:
                best = end
    return best


# ---------------------------------------------------------------------------
# 注册表条目
# ---------------------------------------------------------------------------
def now_epoch_ms() -> int:
    """当前 UTC 毫秒时间戳(剪映 tm_* 字段口径;阶段 5 实机样本核对后如有出入只改这里)。"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def build_store_entry(
    *,
    draft_name: str,
    draft_id: str,
    draft_root_parent: str,
    duration_us: int | None,
    create_ms: int,
    update_ms: int,
) -> dict[str, Any]:
    """构造 ``all_draft_store[]`` 条目(字段集免责声明见模块 docstring)。"""
    entry: dict[str, Any] = {
        "draft_id": draft_id,
        "draft_name": draft_name,
        "draft_root_parent": draft_root_parent,
        "tm_draft_create": int(create_ms),
        "tm_draft_update": int(update_ms),
    }
    if duration_us is not None:
        entry["tm_duration"] = int(duration_us)
    return entry


def build_meta_info(
    *,
    draft_name: str,
    draft_id: str,
    duration_us: int | None,
    create_ms: int,
    update_ms: int,
) -> dict[str, Any]:
    """构造 ``<draft_dir>/draft_meta_info.json``(与 store 条目同口径)。"""
    meta: dict[str, Any] = {
        "draft_id": draft_id,
        "draft_name": draft_name,
        "tm_draft_create": int(create_ms),
        "tm_draft_update": int(update_ms),
    }
    if duration_us is not None:
        meta["tm_duration"] = int(duration_us)
    return meta


def new_draft_id() -> str:
    """生成草稿 id(uuid4 hex;真实字段口径待阶段 5 实机样本核对)。"""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# JSON 读写(根目录级;同盘 mkstemp + os.replace 原子覆盖)
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, content: dict[str, Any]) -> None:
    serialized = json.dumps(content, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".jyreg_tmp_", suffix=".json")
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def read_json_dict(path: Path) -> dict[str, Any] | None:
    """读 JSON dict;不存在返回 None;存在但解析失败/非 dict 抛 :class:`JYRegistryError`。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise JYRegistryError(f"注册表文件解析失败: {path} ({e})") from e
    if not isinstance(data, dict):
        raise JYRegistryError(f"注册表文件顶层不是 dict: {path}")
    return data


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def upsert_root_meta(
    root: Path,
    *,
    draft_name: str,
    draft_id: str,
    duration_us: int | None,
    draft_dir: Path,
    ts_ms: int | None = None,
) -> dict[str, Any]:
    """把草稿 upsert 进 ``<root>/root_meta_info.json`` 的 ``all_draft_store[]``。

    - 文件不存在 → 新建(仅含本条目);
    - 已存在 → 先备份为 ``root_meta_info.json.bak``,再按 ``draft_name`` 匹配:
      命中则原位更新(``tm_duration`` / ``tm_draft_update``,保留既有其他字段),
      未命中则追加;
    - 文件存在但损坏 → 抛 :class:`JYRegistryError`(fail-closed,不覆盖用户数据)。

    Returns:
        ``{"registered": bool, "mode": "created"|"merged", "path": str,
        "backed_up": bool}``。
    """
    root = Path(root)
    meta_path = root / "root_meta_info.json"
    ts = int(ts_ms if ts_ms is not None else now_epoch_ms())

    existing = read_json_dict(meta_path)
    backed_up = False
    if existing is None:
        data: dict[str, Any] = {"all_draft_store": []}
        mode = "created"
    else:
        # 修改前备份(已有文件才备份;文件损坏在 read_json_dict 已抛)
        try:
            import shutil

            shutil.copy2(meta_path, meta_path.with_suffix(meta_path.suffix + _ROOT_META_BACKUP_SUFFIX))
            backed_up = True
        except OSError:
            backed_up = False  # 备份失败不阻断注册(注册本身是幂等 upsert)
        data = existing
        mode = "merged"

    store = data.get("all_draft_store")
    if not isinstance(store, list):
        # 已有文件但缺 all_draft_store:补一个,保留其余字段(不丢用户数据)
        store = []
        data["all_draft_store"] = store

    new_entry = build_store_entry(
        draft_name=draft_name,
        draft_id=draft_id,
        draft_root_parent=str(root),
        duration_us=duration_us,
        create_ms=ts,
        update_ms=ts,
    )
    matched = False
    for i, entry in enumerate(store):
        if isinstance(entry, dict) and entry.get("draft_name") == draft_name:
            merged = dict(entry)
            merged.update(new_entry)
            # 首次注册不动既有 create 时间(有则保留)
            if entry.get("tm_draft_create") is not None:
                merged["tm_draft_create"] = entry["tm_draft_create"]
            store[i] = merged
            matched = True
            break
    if not matched:
        store.append(new_entry)
        if mode == "merged":
            mode = "merged"  # 已有文件 + 新条目,仍算 merged
        data["all_draft_store"] = store

    _atomic_write_json(meta_path, data)
    return {
        "registered": True,
        "mode": mode,
        "matched_existing": matched,
        "path": str(meta_path),
        "backed_up": backed_up,
    }


def register_draft(
    draft_dir: Path,
    *,
    thread_id: str,
    duration_us: int | None,
    root: Path | None = None,
    draft_id: str | None = None,
) -> dict[str, Any]:
    """完整注册一个正式草稿目录:draft_meta_info.json + root_meta_info.json。

    Args:
        draft_dir: 正式草稿目录(已含 draft_content.json / draft_info.json)。
        thread_id: 任务标识(决定目录名 ``AutoVideo_<thread_id>`` 与注册名)。
        duration_us: 草稿时长(微秒);``None`` 表示不带时长字段。
        root: 草稿库根目录;缺省 :func:`resolve_draft_root`。
        draft_id: 显式草稿 id(重发布幂等场景复用);缺省新生成。

    Returns:
        ``{"draft_dir": str, "draft_name": str, "draft_id": str,
        "duration_us": int|None, "meta_info": bool, "root_meta": {...}}``。
    """
    draft_dir = Path(draft_dir)
    final_root = validate_draft_root(root) if root is not None else resolve_draft_root()
    draft_name = draft_dir.name
    ts = now_epoch_ms()
    did = draft_id or new_draft_id()

    meta = build_meta_info(
        draft_name=draft_name,
        draft_id=did,
        duration_us=duration_us,
        create_ms=ts,
        update_ms=ts,
    )
    # draft_meta_info.json 已存在(重发布)→ 保留原 create/update 里的 create
    old_meta = read_json_dict(draft_dir / "draft_meta_info.json")
    if old_meta and old_meta.get("tm_draft_create") is not None:
        meta["tm_draft_create"] = old_meta["tm_draft_create"]
    _atomic_write_json(draft_dir / "draft_meta_info.json", meta)

    root_meta = upsert_root_meta(
        final_root,
        draft_name=draft_name,
        draft_id=did,
        duration_us=duration_us,
        draft_dir=draft_dir,
        ts_ms=ts,
    )
    return {
        "draft_dir": str(draft_dir),
        "draft_name": draft_name,
        "draft_id": did,
        "duration_us": duration_us,
        "meta_info": True,
        "root_meta": root_meta,
    }


__all__ = [
    "JYRegistryError",
    "build_meta_info",
    "build_store_entry",
    "compute_draft_duration_us",
    "new_draft_id",
    "official_draft_dir",
    "official_draft_dir_name",
    "read_json_dict",
    "register_draft",
    "resolve_draft_root",
    "sanitize_name_segment",
    "upsert_root_meta",
    "validate_draft_root",
]

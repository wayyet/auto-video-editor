"""剪映云音乐本机曲库加载器(BGM选曲与自动铺轨 20261114 阶段1)。

从 harvest 脚本产出的本机 JSONL 索引读取剪映已缓存的音乐素材,归一化成
``select_bgm()`` 可直接消费的外层契约::

    {"library": [...], "default_match": {"bgm_id": ...},
     "matching": {"llm_assist": True, "max_candidates_for_llm": 10}}

设计纪律(修订执行计划 §4.1/§4.2):
1. **索引与代码分离** —— 默认索引在 ``runtime/jianying_music_library.local.jsonl``
   (gitignored,含本机缓存路径与 raw_material,严禁入库);
2. **fail-soft** —— 索引缺失/空库/坏行不抛异常,返回空 library 或跳过该行;
3. **素材可用性把关** —— 缓存文件不存在或为空的曲目从 library 剔除;
4. **缓存刷新** —— 按 (路径, mtime_ns) 记忆,文件变化自动重载,亦可显式 reload;
5. **本机路径解析** —— ``cache_relative_path`` 相对 ``JY_USER_DATA_DIR``(缺省
   ``%LOCALAPPDATA%\\JianyingPro\\User Data``)解析,不把个人绝对路径写死进仓库。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 索引路径优先级:显式参数 > JY_MUSIC_LIBRARY_PATH > 仓库 runtime 本机索引
MUSIC_LIBRARY_PATH_ENV = "JY_MUSIC_LIBRARY_PATH"
DEFAULT_INDEX_RELPATH = ("runtime", "jianying_music_library.local.jsonl")

#: 剪映用户数据根目录覆盖环境变量(缓存相对路径的解析基准)
JY_USER_DATA_DIR_ENV = "JY_USER_DATA_DIR"

#: 仓库根(jy_common/ 的上一级)
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: matching 段常量(与 bgm_meta_ref.yaml 的行为开关保持同一口径)
_DEFAULT_MATCHING: dict[str, Any] = {
    "llm_assist": True,
    "max_candidates_for_llm": 10,
}


def jianying_user_data_dir() -> Path:
    """剪映用户数据根目录:``JY_USER_DATA_DIR`` > ``%LOCALAPPDATA%\\JianyingPro\\User Data``。"""
    override = os.environ.get(JY_USER_DATA_DIR_ENV, "").strip()
    if override:
        return Path(override)
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "JianyingPro" / "User Data"
    # 兜底(非 Windows / 无 LOCALAPPDATA):相对家目录推导,保证可预测
    return Path.home() / "AppData" / "Local" / "JianyingPro" / "User Data"


def default_music_library_path() -> Path:
    """默认本机索引路径(仓库 ``runtime/`` 下,gitignored)。"""
    return _REPO_ROOT.joinpath(*DEFAULT_INDEX_RELPATH)


def resolve_music_library_path(explicit: str | Path | None = None) -> Optional[Path]:
    """按优先级解析索引路径;没有任何来源时返回 None。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get(MUSIC_LIBRARY_PATH_ENV, "").strip()
    if env:
        return Path(env)
    return default_music_library_path()


def resolve_music_cache_path(
    entry: dict[str, Any], user_data_dir: Path | None = None
) -> str:
    """把曲库条目解析成当前设备可用的缓存绝对路径字符串(不校验存在性)。

    优先级:raw_material.path 的绝对路径且当前存在 > ``cache_relative_path``
    相对剪映用户数据根目录 > raw_material.path 原样。都解析不出时返回空串。
    """
    raw = entry.get("raw_material") if isinstance(entry.get("raw_material"), dict) else {}
    raw_path = str((raw or {}).get("path") or "").strip()
    cand = Path(raw_path) if raw_path else None
    if cand and cand.is_absolute() and cand.is_file():
        return str(cand)
    rel = str(entry.get("cache_relative_path") or "").strip()
    if rel:
        root = Path(user_data_dir) if user_data_dir else jianying_user_data_dir()
        return str(root / rel)
    if cand:
        return str(cand)
    return ""


def _cache_file_usable(cache_path: str) -> bool:
    """素材可用性最低检查:文件存在且大小 > 0(修订 §2.17)。"""
    if not cache_path:
        return False
    try:
        p = Path(cache_path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _normalize_entry(raw_entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """JSONL 行 -> 归一化曲库条目;缺 music_id 视为坏记录返回 None。"""
    music_id = str(raw_entry.get("music_id") or "").strip()
    if not music_id:
        return None
    duration_us = int(raw_entry.get("duration_us") or 0)
    cache_path = resolve_music_cache_path(raw_entry)
    return {
        "bgm_id": music_id,
        "music_id": music_id,
        "name": str(raw_entry.get("name") or music_id),
        "mood": list(raw_entry.get("mood") or []),
        "genre": list(raw_entry.get("genre") or []),
        "tempo": str(raw_entry.get("tempo") or "unknown"),
        "beat_period_ms": raw_entry.get("beat_period_ms"),
        "duration_us": duration_us,
        "duration_ms": duration_us // 1000,
        "default_volume": float(raw_entry.get("default_volume") or 0.35),
        "notes": str(raw_entry.get("notes") or ""),
        # 兼容旧 select_bgm 消费键(path);cache_path 为新键,两者同值
        "path": cache_path,
        "cache_path": cache_path,
        "request_id": str(raw_entry.get("request_id") or ""),
        "category_id": str(raw_entry.get("category_id") or ""),
        "category_name": str(raw_entry.get("category_name") or ""),
        "asset_available": _cache_file_usable(cache_path),
        "asset_kind": "jianying_music",
        "is_default": bool(raw_entry.get("is_default")),
        "raw_material": raw_entry.get("raw_material")
        if isinstance(raw_entry.get("raw_material"), dict)
        else {},
    }


# ---------------------------------------------------------------------------
# 模块级缓存:(解析后路径, mtime_ns) -> 加载结果;文件变化自动重载
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def reset_music_library_cache() -> None:
    """清空模块级缓存(单测注入临时索引后用)。"""
    _CACHE.clear()


def load_jianying_music_library(
    *,
    path: str | Path | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """读取本机剪映音乐曲库索引,返回与 ``select_bgm()`` 兼容的外层契约。

    Returns:
        恒为 dict;索引缺失/空库时 ``library=[]`` 且 ``default_match={}``,
        不抛异常(调用方按空库走既有 stub 降级路径)。
    """
    lib_path = resolve_music_library_path(path)
    if lib_path is None or not Path(lib_path).is_file():
        logger.info("[music_library] 索引不存在,返回空曲库: %s", lib_path)
        return {"library": [], "default_match": {}, "matching": dict(_DEFAULT_MATCHING)}

    try:
        mtime_ns = Path(lib_path).stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    cache_key = (str(Path(lib_path).resolve()), mtime_ns)
    if not refresh and cache_key in _CACHE:
        return _CACHE[cache_key]

    entries_by_id: dict[str, dict[str, Any]] = {}
    bad_lines = 0
    usable = 0
    with open(lib_path, "r", encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw_entry = json.loads(line)
            except json.JSONDecodeError as e:
                bad_lines += 1
                logger.warning("[music_library] 第%d行JSON解析失败,跳过: %r", lineno, e)
                continue
            if not isinstance(raw_entry, dict):
                bad_lines += 1
                logger.warning("[music_library] 第%d行不是JSON对象,跳过", lineno)
                continue
            entry = _normalize_entry(raw_entry)
            if entry is None:
                bad_lines += 1
                logger.warning("[music_library] 第%d行缺 music_id,跳过", lineno)
                continue
            # 同 music_id 多行:最后一次有效记录胜出(修订 §4.1)
            entries_by_id[entry["music_id"]] = entry

    library: list[dict[str, Any]] = []
    invalid: list[str] = []
    for entry in entries_by_id.values():
        if entry["asset_available"]:
            library.append(entry)
            usable += 1
        else:
            invalid.append(f"{entry['music_id']}({entry['name']})")
    if invalid:
        logger.warning("[music_library] 缓存失效曲目剔除,不进入候选: %s", ", ".join(invalid))

    default_id = ""
    for entry in library:
        if entry.get("is_default"):
            default_id = entry["bgm_id"]
            break
    if not default_id and library:
        default_id = library[0]["bgm_id"]

    result = {
        "library": library,
        "default_match": {"bgm_id": default_id} if default_id else {},
        "matching": dict(_DEFAULT_MATCHING),
    }
    _CACHE[cache_key] = result
    logger.info(
        "[music_library] 加载完成: 可用%d首, 剔除%d首, 坏行%d行, 默认=%s",
        usable, len(invalid), bad_lines, default_id or "-",
    )
    return result


def reload_music_library(*, path: str | Path | None = None) -> dict[str, Any]:
    """显式刷新(harvest 写盘后调用;修订 §2.18)。"""
    return load_jianying_music_library(path=path, refresh=True)


__all__ = [
    "default_music_library_path",
    "jianying_user_data_dir",
    "load_jianying_music_library",
    "reload_music_library",
    "resolve_music_cache_path",
    "resolve_music_library_path",
    "reset_music_library_cache",
]

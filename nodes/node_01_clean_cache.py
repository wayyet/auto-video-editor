"""节点 1:clean_cache — 对应 /kuaishou-clean-cache 技能(附件 1.2 节)。

清理剪映、OpenStoryline、FireRed-OpenStoryline、auto-video-editor 自身的
可再生缓存(技能「一类·常规再生缓存」清单),外加「一键回收根」三目录的
内容清空(2026-11-01 用户指定)。所有路径来源:

- ``config.CACHE_PATHS_TO_CLEAN``:简单路径(直接 Path)。
- ``config.CACHE_GLOB_SPECS``:递归/通配 spec(``__pycache__``、``tmp`` 子目录、
  ``.DS_Store``、剪映草稿 ``.bak`` / ``.backup``、runtime 日志等)。
- ``config.CACHE_TRASH_CHILDREN_ROOTS``:一键回收根(outputs/ inputs/ out/),
  只清空直接子项、保留根目录本身。
- ``config.resolved_cache_paths()`` / ``config.resolved_trash_children()``:
  把上述两类 spec 扁平化为当下磁盘上的 ``list[Path]``。

删除方式(2026-11-01 用户决定):**所有删除项一律移入 Windows 回收站**
(``_send_to_recycle_bin``:SHFileOperationW + FO_DELETE + FOF_ALLOWUNDO),
不再 ``shutil.rmtree`` / ``unlink`` 彻底删除。单项回收失败只写入 error_log,
该项保留原处,绝不回退成永久删除。已知边界:单项大于回收站容量上限时,
Windows 在 FOF_NOCONFIRMATION 下可能改为永久删除(默认上限约为卷容量的
5%~10%,常规素材/成片不会触发)。

调用时机(2026-09 计划):
- graph.invoke **不**自动触发本节点(START 边已剥离,见 graph.py 与
  ``tests/integration/test_no_autoclean_cache.py``)。
- 仅由 FireRed-OpenStoryline Web UI 的「清理缓存」按钮通过
  ``POST /api/system/clean-cache`` 端点显式调用。

单测要点(附件 1.2 节 + 2026-11-01 增补):
- 目标目录不存在时不报错(正常跳过)
- 回收站操作失败时异常被捕获并写入 error_log,不中断、目标保留原处
- cache_cleaned_paths 只包含实际清理成功的路径
- 三个回收根只清空直接子项,根目录本身保留
- 禁止清理剪映 ``User Data\\Cache`` / ``User Data\\Log``(技能三类·禁删项)
- ``__pycache__`` 递归须排除 ``.venv/`` 与 ``venv/``
- ``tmp/`` 根下 4 个模板 JSON 不被删除(只动子目录)
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from config import resolved_cache_paths, resolved_trash_children
from state import WorkflowState


# ---------------------------------------------------------------------------
# Windows 回收站:SHFileOperationW(FO_DELETE + FOF_ALLOWUNDO)
# ---------------------------------------------------------------------------
_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x0040  # 删除进回收站(可恢复),而不是彻底删除
_FOF_SILENT = 0x0004  # 不弹进度对话框
_FOF_NOCONFIRMATION = 0x0010  # 对确认框自动「是」
_FOF_NOERRORUI = 0x0400  # 不弹错误框


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """shell32 ``SHFileOperationW`` 的参数结构(Unicode 版)。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def _send_to_recycle_bin(path: Path) -> None:
    """把 ``path`` 移入 Windows 回收站;失败抛 OSError,由调用方记 error_log。

    - 文件/目录都支持,整目录作为**一个**回收站条目移入(同卷内 rename 级开销)。
    - 单测通过 monkeypatch 本函数避免污染真实回收站(见 test_node_01_clean_cache.py)。
    - 非 Windows 直接抛 OSError(本仓库生产/测试环境均为 Windows)。
    """
    if os.name != "nt":
        raise OSError(f"移入回收站仅支持 Windows,当前 os.name={os.name!r}: {path}")
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    # pFrom 要求「双 \0 结尾」;ctypes c_wchar_p 会自动补一个,这里再手动拼一个。
    op.pFrom = str(path) + "\x00"
    op.fFlags = _FOF_ALLOWUNDO | _FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOERRORUI
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise OSError(
            f"SHFileOperationW 失败(未移入回收站,目标保留原处): "
            f"code={code}, aborted={op.fAnyOperationsAborted}, path={path}"
        )


def _recycle_one(path: Path, cleaned: list[str], errors: list[str]) -> None:
    """清理单个路径:存在才回收;失败写 error_log,不中断、不回退永久删除。"""
    try:
        if not path.exists():
            return  # 不存在的路径不算成功,也不算失败
        _send_to_recycle_bin(path)
        cleaned.append(str(path))
    except Exception as e:  # noqa: BLE001 - 故意吞掉所有 IO 异常
        errors.append(f"[node_01] 清理失败 {path}: {e}")


def clean_cache(state: WorkflowState) -> dict:
    cleaned: list[str] = []
    errors = list(state.get("error_log", []) or [])
    # ① 一键回收根(outputs/ inputs/ out/)的直接子项:清空内容、保留根目录
    for child in resolved_trash_children():
        _recycle_one(child, cleaned, errors)
    # ② 原有再生缓存清单(同样移回收站)
    for path in resolved_cache_paths():
        _recycle_one(path, cleaned, errors)
    return {
        **state,
        "cache_cleaned": True,
        "cache_cleaned_paths": cleaned,
        "error_log": errors,
    }


def clean_cache_paths(paths: list[Path], state: WorkflowState) -> dict:
    """测试/扩展入口:允许显式传入路径列表(覆盖 config 中的全局值)。"""
    cleaned: list[str] = []
    errors = list(state.get("error_log", []) or [])
    for path in paths:
        _recycle_one(path, cleaned, errors)
    return {
        **state,
        "cache_cleaned": True,
        "cache_cleaned_paths": cleaned,
        "error_log": errors,
    }

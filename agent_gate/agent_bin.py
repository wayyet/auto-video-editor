"""在 PATH 之外解析外部可执行文件(Windows 本机的坑)。

为什么需要这一层
----------------
MiniMax Code 装在 ``C:\\Users\\<user>\\.minimax-code\\`` —— **默认常常不在 PATH**。
少了 PATH 的后果都很隐蔽:

1. kit 测试里 ``@needs_mcode`` 的用例被**静默 skip**。汇总数字
   (``17 skip / 0 failure``)分不出「被 skip」和「真跑通过」,回归会被当成没发生。
2. ``drive`` 拉起 mcode 报 ``WinError 2`` → 连续 2 个会话没进展 → 退出码 4(stalled)。

所以解析顺序固定为:**环境变量 → PATH → 约定俗成的安装目录**。
所有外部工具(mcode / claude / ffmpeg / ffprobe)只认这一个入口,
不许各处各写一份 ``shutil.which`` —— 那样就会出现「一处能找到、另一处找不到」。

**找不到就返回 ``None``,由调用方决定是跳过还是报错 —— 绝不静默降级。**
"""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import Optional, Sequence

#: Windows 上 PATHEXT 覆盖的可执行后缀(``.ps1`` 不算:CreateProcess 跑不了)。
#: 无后缀那项给 POSIX 用。
EXE_SUFFIXES: tuple[str, ...] = (".exe", ".cmd", ".bat", ".com", "")

#: 各 CLI 自己的安装约定,相对用户目录。不依赖 PATH。
WELL_KNOWN_DIRS: dict[str, tuple[str, ...]] = {
    "mcode": (".minimax-code", ".minimax-code/bin"),
    "claude": (".claude/local", ".claude/bin", ".claude"),
}

#: ffmpeg 没有统一的安装位置,列几种常见摆法(支持 ``*`` 通配)。
GLOBAL_DIRS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("C:/ffmpeg/*/bin", "C:/ffmpeg/bin", "C:/Program Files/ffmpeg/bin",
               "~/ffmpeg/*/bin", "~/ffmpeg/bin"),
    "ffprobe": ("C:/ffmpeg/*/bin", "C:/ffmpeg/bin", "C:/Program Files/ffmpeg/bin",
                "~/ffmpeg/*/bin", "~/ffmpeg/bin"),
}

#: 工具名 → 操作员可用的环境变量。设了就以它为准。
ENV_VARS: dict[str, str] = {
    "mcode": "AGENT_GATE_MCODE_BIN",
    "claude": "AGENT_GATE_CLAUDE_BIN",
    "ffmpeg": "AGENT_GATE_FFMPEG_BIN",
    "ffprobe": "AGENT_GATE_FFPROBE_BIN",
}

#: 额外的搜索目录(所有工具通用),``os.pathsep`` 分隔。
EXTRA_DIRS_ENV = "AGENT_GATE_BIN_DIRS"


def home_dir() -> Optional[Path]:
    """用户目录。Windows 取 ``USERPROFILE``,POSIX 取 ``HOME``;都没有才算 ``Path.home()``。"""
    order = ("USERPROFILE", "HOME") if os.name == "nt" else ("HOME", "USERPROFILE")
    for var in order:
        raw = os.environ.get(var)
        if raw:
            return Path(raw)
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def _extra_dirs_from_env() -> list[Path]:
    raw = os.environ.get(EXTRA_DIRS_ENV) or ""
    return [Path(p) for p in raw.split(os.pathsep) if p]


def _glob_dirs(patterns: Sequence[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns:
        expanded = os.path.expanduser(pattern)
        for hit in sorted(glob.glob(expanded)):
            out.append(Path(hit))
    return out


def candidate_dirs(name: str, extra_dirs: Sequence[str | Path] = ()) -> list[Path]:
    """这个工具可能被装在哪些目录里(按查找顺序)。"""
    dirs: list[Path] = [Path(d) for d in extra_dirs]
    dirs += _extra_dirs_from_env()
    home = home_dir()
    if home is not None:
        for rel in WELL_KNOWN_DIRS.get(name, ()):
            dirs.append(home / rel)
    dirs += _glob_dirs(GLOBAL_DIRS.get(name, ()))
    return dirs


def _find_in_dir(name: str, directory: Path) -> Optional[str]:
    try:
        if not directory.is_dir():
            return None
    except OSError:
        return None
    for suffix in EXE_SUFFIXES:
        cand = directory / f"{name}{suffix}"
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def resolve_executable(
    name: str,
    *,
    env_var: Optional[str] = None,
    extra_dirs: Sequence[str | Path] = (),
) -> Optional[str]:
    """解析外部可执行文件,解析到就返回路径,解析不到返回 ``None``。

    顺序:

    1. **环境变量**(默认按工具名从 :data:`ENV_VARS` 找)—— 操作员的显式设置
       **原样返回、不校验存在性**:写错了要**响亮报错**,不能变成静默跳过。
    2. **PATH** —— ``shutil.which``,Windows 上按 PATHEXT 找到真正的 ``.CMD``/``.EXE``
       (裸名 ``mcode`` 交给 ``CreateProcess`` 一定是 ``WinError 2``)。
    3. **extra_dirs** / :data:`EXTRA_DIRS_ENV`。
    4. **约定俗成的安装目录**(:data:`WELL_KNOWN_DIRS` / :data:`GLOBAL_DIRS`)。
    """
    if not name:
        return None
    var = env_var if env_var is not None else ENV_VARS.get(name)
    if var:
        raw = os.environ.get(var)
        if raw:
            return raw
    found = shutil.which(name)
    if found:
        return found
    for directory in candidate_dirs(name, extra_dirs):
        hit = _find_in_dir(name, directory)
        if hit:
            return hit
    return None


def resolve_agent_bin(name: str) -> str:
    """把 Agent 可执行文件名解析成**能直接交给 CreateProcess** 的路径。

    Windows 上 ``CreateProcess`` 不做 PATHEXT 补全:裸名 ``mcode`` 一定
    ``WinError 2``,即使 ``mcode.CMD`` 就在 PATH 里(本机
    ``C:\\Users\\wayyet\\.minimax-code\\mcode.CMD`` 就是这种情况)。
    ``shutil.which`` 会按 PATHEXT 找到真正的 ``.CMD`` / ``.EXE``;
    PATH 里没有时,再按 :func:`resolve_executable` 去安装目录里找。

    解析不到就原样返回,让调用方看到「找不到可执行文件」的真实错误。
    """
    if not name:
        return name
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        return name
    return resolve_executable(name) or name


def tool_dirs(names: Sequence[str]) -> list[str]:
    """返回装着这些工具的目录(去重、保持顺序)。"""
    out: list[str] = []
    for name in names:
        path = resolve_executable(name)
        if not path:
            continue
        parent = str(Path(path).parent)
        if parent not in out:
            out.append(parent)
    return out


def augment_path(path_value: Optional[str], names: Sequence[str]) -> str:
    """把解析到的工具目录补到 PATH 前面(已有的不重复加)。

    给**子进程**用:后台 / 子进程的 PATH 常常比交互式 shell 干净,
    少了 ffmpeg 目录,抽帧会**静默失败**(一张图都取不到,而且不报错)。
    """
    current = path_value if path_value is not None else os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    seen = {os.path.normcase(os.path.normpath(p)) for p in parts}
    for directory in tool_dirs(names):
        key = os.path.normcase(os.path.normpath(directory))
        if key not in seen:
            parts.insert(0, directory)
            seen.add(key)
    return os.pathsep.join(parts)

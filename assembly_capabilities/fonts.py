"""跨平台 CJK 字体发现(精简版)。

原 video-agent-kit 0.4.3 ``fonts.py`` 的核心目的是给 drawtext fontfile=
找一个真实存在的 CJK 字体文件;Linux / macOS / Windows 三平台覆盖。

本仓库裁剪:
- 保留 ``font_dirs`` / ``find_cjk_font`` / ``family_of_file`` / ``ass_font_name``。
- 删除 ``covers_cjk``(依赖 fontTools,本仓库无此重型依赖)。
- 删除 ``diagnose``(env doctor 才用,本仓库不需要)。
- ``_DEFAULT_FAMILY`` 保留三个平台默认族名,Windows 直接用 "Microsoft YaHei"。
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

OS = ("macos" if sys.platform == "darwin"
      else "windows" if os.name == "nt" else "linux")

# 原 0.4.3 一贯搜索的 Linux 字体目录(Linux 批处理机的传统位置)。
_LEGACY_DIRS = ["/root/.fonts", "/usr/share/fonts"]

_PLATFORM_DIRS = {
    "linux": ["/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts"],
    "macos": ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
              "/Library/Fonts", "~/Library/Fonts"],
    "windows": [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Microsoft", "Windows", "Fonts")],
}

_WEIGHT_FILES = {
    "black": ["NotoSansSC-900.ttf", "NotoSansCJK-Black.ttc", "NotoSansCJKsc-Black.otf"],
    "bold": ["NotoSansSC-700.ttf", "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf",
             "SourceHanSansSC-Bold.otf", "msyhbd.ttc", "simhei.ttf",
             "PingFang.ttc", "Hiragino Sans GB.ttc"],
    "medium": ["NotoSansSC-500.ttf", "NotoSansCJK-Medium.ttc", "NotoSansCJKsc-Medium.otf"],
    "regular": ["NotoSansSC-400.ttf", "PingFang.ttf",
                "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf",
                "NotoSansSC-Regular.otf", "SourceHanSansSC-Regular.otf",
                "PingFang.ttc", "Hiragino Sans GB.ttc", "STHeiti Light.ttc",
                "msyh.ttc", "simhei.ttf", "simsun.ttc", "Deng.ttf",
                "wqy-zenhei.ttc", "wqy-microhei.ttc", "DroidSansFallbackFull.ttf"],
}

_CJK_NAME_HINTS = ("notosanscjk", "notoserifcjk", "notosanssc", "notosanstc",
                   "sourcehansans", "sourcehanserif", "pingfang", "hiragino sans gb",
                   "stheiti", "songti", "heiti", "yuppy", "msyh", "msjh", "yahei",
                   "simhei", "simsun", "simkai", "deng", "fangsong", "kaiti",
                   "wqy-", "uming", "ukai", "droidsansfallback", "arialunicode",
                   "unifont")

_FONT_SUFFIXES = {".ttf", ".ttc", ".otf", ".otc"}

_FAMILY_BY_STEM = (
    ("notosanscjk", "Noto Sans CJK SC"),
    ("notosanssc", "Noto Sans SC"),
    ("notosanstc", "Noto Sans TC"),
    ("sourcehansans", "Source Han Sans SC"),
    ("pingfang", "PingFang SC"),
    ("hiragino sans gb", "Hiragino Sans GB"),
    ("stheiti", "Heiti SC"),
    ("songti", "Songti SC"),
    ("msyhbd", "Microsoft YaHei"),
    ("msyh", "Microsoft YaHei"),
    ("msjh", "Microsoft JhengHei"),
    ("simhei", "SimHei"),
    ("simsun", "SimSun"),
    ("deng", "DengXian"),
    ("wqy-zenhei", "WenQuanYi Zen Hei"),
    ("wqy-microhei", "WenQuanYi Micro Hei"),
    ("uming", "AR PL UMing CN"),
    ("ukai", "AR PL UMai CN"),
    ("droidsansfallback", "Droid Sans Fallback"),
)

_DEFAULT_FAMILY = {"linux": "Noto Sans CJK SC",
                   "macos": "PingFang SC",
                   "windows": "Microsoft YaHei"}


def override_dirs() -> list[Path]:
    """``VE_FONT_DIRS`` / ``VIDEO_EDIT_FONT_DIRS`` 环境变量里的覆盖目录。"""
    raw = (os.environ.get("VE_FONT_DIRS")
           or os.environ.get("VIDEO_EDIT_FONT_DIRS") or "")
    return [Path(p.strip()).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def font_dirs() -> list[Path]:
    """所有值得搜索的字体目录(高优先级在前)。

    不缓存:VE_FONT_DIRS 是每次调用现读,允许工具动态指向刚生成的字体目录。
    """
    dirs = [*override_dirs(),
            *(Path(p) for p in _LEGACY_DIRS),
            *(Path(p).expanduser() for p in _PLATFORM_DIRS[OS] if p)]
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _find_named(name: str, dirs: list[Path]) -> str | None:
    """按文件名精确查:先直接子级,再递归。发行版常把字体埋在 opentype/noto 下。"""
    for directory in dirs:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    for directory in dirs:
        try:
            if not directory.is_dir():
                continue
            for hit in directory.rglob(name):
                if hit.is_file():
                    return str(hit)
        except OSError:
            continue
    return None


def _scan_by_hint(dirs: list[Path], prefer_bold: bool) -> str | None:
    for directory in dirs:
        try:
            if not directory.is_dir():
                continue
            files = [p for p in sorted(directory.rglob("*"))
                     if p.is_file() and p.suffix.lower() in _FONT_SUFFIXES
                     and any(h in p.stem.lower() for h in _CJK_NAME_HINTS)]
        except OSError:
            continue
        if not files:
            continue
        if prefer_bold:
            bold = [p for p in files if "bold" in p.stem.lower() or "bd" in p.stem.lower()]
            if bold:
                return str(bold[0])
        return str(files[0])
    return None


@functools.lru_cache(maxsize=8)
def _find_cjk_font(weight: str, dirs_key: tuple[str, ...]) -> str | None:
    dirs = [Path(d) for d in dirs_key]
    names = _WEIGHT_FILES.get(weight) or _WEIGHT_FILES["regular"]
    for name in [*names, *(n for n in _WEIGHT_FILES["regular"] if n not in names)]:
        hit = _find_named(name, dirs)
        if hit:
            return hit
    hit = _scan_by_hint(dirs, prefer_bold=weight in ("bold", "black"))
    if hit:
        return hit
    for path in _fc_list_zh_files():
        return path
    return None


def find_cjk_font(weight: str = "regular") -> str | None:
    """返回一个真实存在的 CJK 字体文件路径;找不到返回 None。

    不会瞎猜不存在的路径——调用方需要硬失败时可以自己 raise。
    """
    return _find_cjk_font((weight or "regular").lower(),
                          tuple(str(d) for d in font_dirs()))


@functools.lru_cache(maxsize=1)
def _fc_list_zh_families() -> frozenset[str]:
    """fontconfig 知道的、能覆盖中文的字体族。Linux 之外返回空。"""
    if not shutil.which("fc-list"):
        return frozenset()
    try:
        proc = subprocess.run(["fc-list", ":lang=zh", "family"],
                              capture_output=True, text=True, timeout=30)
    except Exception:
        return frozenset()
    families: set[str] = set()
    for line in proc.stdout.splitlines():
        for fam in line.split(","):
            fam = fam.strip()
            if fam:
                families.add(fam)
    return frozenset(families)


@functools.lru_cache(maxsize=1)
def _fc_list_zh_files() -> tuple[str, ...]:
    """fontconfig 列出的、能覆盖中文的字体文件路径(仅保留存在的)。"""
    if not shutil.which("fc-list"):
        return ()
    try:
        proc = subprocess.run(["fc-list", ":lang=zh", "file"],
                              capture_output=True, text=True, timeout=30)
    except Exception:
        return ()
    files: list[str] = []
    for line in proc.stdout.splitlines():
        path = line.strip().rstrip(":").strip()
        if path and path not in files and Path(path).is_file():
            files.append(path)
    return tuple(files)


def family_of_file(font_file: str | Path) -> str | None:
    """按文件名推断字体族名,不需要 fontconfig。"""
    stem = Path(font_file).stem.lower()
    for prefix, family in _FAMILY_BY_STEM:
        if stem.startswith(prefix) or prefix in stem:
            return family
    return None


def ass_font_name() -> str | None:
    """``force_style=FontName=`` 用的族名;机器无 CJK 字体时返回 None。

    fontconfig 优先(Linux 下 libass 真正会用的解析),再退回具体文件的族名,
    再退回平台默认族(Windows / macOS 用 DirectWrite / CoreText,族名有效)。
    """
    families = _fc_list_zh_families()
    for preferred in ("Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans SC",
                      "Source Han Sans SC", "WenQuanYi Zen Hei"):
        if preferred in families:
            return preferred
    if families:
        return sorted(families)[0]
    found = find_cjk_font("regular")
    if found:
        return family_of_file(found) or _DEFAULT_FAMILY[OS]
    return None

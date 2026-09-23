"""``ffproc.py``(原样搬自 video-agent-kit 0.4.3,删除 MCP-stdio 特定注释)。

核心职责:
- ``run_proc``:子进程安全 wrapper,默认 ``stdin=DEVNULL``、自动注入
  ffmpeg ``-nostdin``、默认 3600s 超时。
- ``escape_text`` / ``escape_option`` / ``safe_expr``:ffmpeg filtergraph
  转义,防 LLM 用户输入突破 filter 字符串。
- ``require_ffmpeg``:fail-fast 编码器/滤镜能力检测。
"""
from __future__ import annotations

import re as _re
import shutil
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT = 3600


def run_proc(cmd: list[str], *args, timeout: float | None = None, **kwargs):
    """``subprocess.run`` 的安全 wrapper(本仓库 LangGraph 节点场景)。

    与 ``subprocess.run`` 的差异:
    - ``stdin`` 默认 ``subprocess.DEVNULL``(可显式覆盖)。
    - 第一个参数是 ``ffmpeg`` 且没 ``-nostdin`` 时,自动在 ffmpeg 二进制后注入
      ``-nostdin``,防止 ffmpeg 读 stdin 字节被截留。
    - ``timeout`` 默认 ``DEFAULT_TIMEOUT=3600``,防止长任务永远卡住。

    其余参数(``capture_output`` / ``text`` / ``check`` / ``stdout`` ...)透传。
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if cmd and Path(cmd[0]).name == "ffmpeg" and "-nostdin" not in cmd:
        cmd = [cmd[0], "-nostdin", *cmd[1:]]
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    return subprocess.run(cmd, *args, timeout=timeout, **kwargs)


# ---------------------------------------------------------------------------
# ffmpeg filtergraph 转义
# ---------------------------------------------------------------------------
def escape_text(value) -> str:
    """转义 ffmpeg ``drawtext=text='...'`` 字符串内的文本。

    内部反斜杠先转义,然后单引号走 ``'\\''``(关闭-转义-重开)标准手法。
    """
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace("'", "'\\''")


_UNQUOTED_SPECIAL = ("\\", ":", "'", ",", ";", "[", "]")


def escape_option(value) -> str:
    """转义 ffmpeg filter 未加引号 option 值(fontfile/fontcolor/boxcolor/...)。

    反斜杠必须先转义,否则后续插入的反斜杠会被再翻倍。
    """
    s = "" if value is None else str(value)
    for ch in _UNQUOTED_SPECIAL:
        s = s.replace(ch, "\\" + ch)
    return s


_EXPR_OK = _re.compile(r"^[A-Za-z0-9_+\-*/().<> ,]*$")


def safe_expr(value, default: str) -> str:
    """``value`` 是良性的 drawtext/overlay 几何表达式才返回,否则返回 ``default``。

    仅接受 ``A-Za-z0-9_+\\-*/().<> ,`` 这些算术/比较/括号字符;任何能突破
    option 边界的 ``: ; ' \" \\ [ ] =`` 都强制走默认值。
    """
    s = "" if value is None else str(value).strip()
    if s and _EXPR_OK.match(s):
        return s
    return default


# ---------------------------------------------------------------------------
# Fail-fast ffmpeg 能力检测
# ---------------------------------------------------------------------------
_FEATURE_CACHE: dict[str, str] = {}


def _ffmpeg_feature_text(kind: str) -> str:
    """缓存的 ``ffmpeg -<kind>`` 输出(``encoders`` 或 ``filters``)字符串。

    ffmpeg 不存在或拿不到列表时返回空字符串。
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        return ""
    if kind not in _FEATURE_CACHE:
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", f"-{kind}"],
                capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
            )
            _FEATURE_CACHE[kind] = (proc.stdout or "") + (proc.stderr or "")
        except Exception:  # noqa: BLE001
            _FEATURE_CACHE[kind] = ""
    return _FEATURE_CACHE[kind]


def require_ffmpeg(*, encoders: tuple[str, ...] = (), filters: tuple[str, ...] = ()) -> str | None:
    """若 ffmpeg 缺失或不具备任何请求的 encoder/filter,返回错误字符串;OK 时返回 None。

    长渲染前调用,feature 缺失时毫秒级失败,而不是渲染到最后一步才报 stderr。
    """
    if not shutil.which("ffmpeg"):
        return "[ERROR] ffmpeg not found on PATH"
    enc_text = _ffmpeg_feature_text("encoders")
    fil_text = _ffmpeg_feature_text("filters")
    miss_enc = [e for e in encoders if enc_text and e not in enc_text]
    miss_fil = [f for f in filters if fil_text and f" {f} " not in fil_text]
    if not (miss_enc or miss_fil):
        return None
    parts = []
    if miss_enc:
        parts.append("encoders missing: " + ", ".join(miss_enc))
    if miss_fil:
        parts.append("filters missing: " + ", ".join(miss_fil))
    return (
        "[ERROR] this ffmpeg build lacks required features (" + "; ".join(parts)
        + "). It was compiled without them; install a full build (conda-forge ffmpeg, "
        "or your platform's full package)."
    )

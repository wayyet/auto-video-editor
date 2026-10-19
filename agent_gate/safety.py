"""唯一的标识符与路径校验入口。

设计原则:**先白名单,再归属**。
- 白名单:标识符必须逐字符匹配正则(不给任何通配)。
- 归属:算出来的绝对路径必须 ``is_relative_to`` 某个被授权的根。

只要两步里任何一步不过,抛 :class:`UnsafeIdentifier` / :class:`UnsafePath`。
**拒绝时绝不回显本机绝对路径** —— 返回给 Agent 的 message 只带一个 code,
绝对路径只写进本机 ``events.jsonl``,见 :func:`rejection_message`。

环境变量:

``AGENT_GATE_MEDIA_ROOTS``
    素材白名单,``os.pathsep`` 分隔的若干绝对路径(``;`` on Windows,``:`` on POSIX)。
    未设置 = 不允许任何外部素材,``safe_media_path`` 一律拒绝。
    由 ``agent_gate init --env AGENT_GATE_MEDIA_ROOTS=...`` 写进 ``.mcp.json``。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

# thread_id:字母数字开头,后面可跟字母数字/下划线/连字符,总长 1-64。
#   拒绝 `../x`、`..\x`、`/tmp/x`、`C:\Windows\x`、空串、含空格/中文、超 64 字符。
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# task_id:`t_` + 恰好 16 位小写十六进制 —— 与 protocol.task_id_for() 的产出形状一致。
#   拒绝 15 位/17 位 hex、大写 hex、缺前缀、含路径分隔符。
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{16}$")

MEDIA_ROOTS_ENV = "AGENT_GATE_MEDIA_ROOTS"


class UnsafeIdentifier(ValueError):
    """标识符不合法(白名单没过)。``str(e)`` 只含错误码与规则名,不含用户输入。"""


class UnsafePath(ValueError):
    """路径没落在被授权的根目录下(归属没过)。``str(e)`` 不含本机绝对路径。"""


# ------------------------------------------------------------------ 标识符白名单
def safe_thread_id(thread_id: str) -> str:
    """thread_id 过白名单则原样返回,否则抛 UnsafeIdentifier。"""
    if not isinstance(thread_id, str) or not THREAD_ID_RE.match(thread_id):
        raise UnsafeIdentifier("bad_thread_id")
    return thread_id


def safe_task_id(task_id: str) -> str:
    """task_id 过白名单则原样返回,否则抛 UnsafeIdentifier。"""
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise UnsafeIdentifier("bad_task_id")
    return task_id


# ------------------------------------------------------------------ 路径归属
def _contained(root: Path, candidate: Path) -> bool:
    """candidate 是否在 root 之内(含 root 自身)。

    两边都 ``resolve()`` 过 —— 这会自动吃掉 `..`、符号链接、Windows 的 8.3 短名,
    所以 `..\\x` / `%2e%2e/x` 这类输入在 resolve 之后就落在 root 外面了。
    """
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (ValueError, OSError):      # Windows 上跨盘 resolve 可能抛
        return False


def safe_gate_root(outputs_root: Union[Path, str], thread_id: str) -> Path:
    """<outputs_root>/<thread_id>/agent_gate,三步缺一不可。

    1. thread_id 过 :data:`THREAD_ID_RE`;
    2. 拼出 root 并 ``resolve()``;
    3. root 必须仍在 outputs_root 之内。
    """
    safe_thread_id(thread_id)
    base = Path(outputs_root).resolve()
    root = (base / thread_id / "agent_gate").resolve()
    if not _contained(base, root):
        raise UnsafePath("gate_root_escape")
    return root


def safe_task_path(mailbox: "MailboxLike", task_id: str) -> Path:  # noqa: F821
    """信箱里某个任务的文件路径。task_id 过白名单,且结果仍在 tasks/ 内。"""
    safe_task_id(task_id)
    p = (Path(mailbox.tasks_dir) / f"{task_id}.task.json").resolve()
    if not _contained(Path(mailbox.tasks_dir).resolve(), p):
        raise UnsafePath("task_path_escape")
    return p


def safe_frame_dir(mailbox: "MailboxLike", task_id: str) -> Path:  # noqa: F821
    """某个任务的抽帧目录。"""
    safe_task_id(task_id)
    d = (Path(mailbox.frames_root) / task_id).resolve()
    if not _contained(Path(mailbox.frames_root).resolve(), d):
        raise UnsafePath("frames_escape")
    return d


def media_roots(value: Optional[str] = None) -> list[Path]:
    """读素材白名单。``value`` 为 None 时读环境变量 ``AGENT_GATE_MEDIA_ROOTS``。"""
    raw = os.environ.get(MEDIA_ROOTS_ENV, "") if value is None else value
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(Path(part).resolve())
        except (OSError, ValueError):
            continue
    return out


def safe_media_path(
    video_path: str,
    roots: Optional[Sequence[Union[Path, str]]] = None,
    *,
    must_exist: bool = True,
) -> Path:
    """素材路径必须 resolve 到某个白名单根之内。

    - ``roots`` 为 None 时读 :func:`media_roots`;为空列表 → 一律拒绝
      (``media_roots_unset``),这是 fail closed 的默认姿态。
    - ``must_exist=True``(默认)时还要求它确实是个已存在的文件。
    """
    if not isinstance(video_path, str) or not video_path.strip():
        raise UnsafePath("empty_media_path")
    allowed = [Path(r).resolve() for r in (media_roots() if roots is None else roots)]
    if not allowed:
        raise UnsafePath("media_roots_unset")
    try:
        p = Path(video_path).resolve()
    except (OSError, ValueError):
        raise UnsafePath("bad_media_path") from None
    if not any(_contained(r, p) for r in allowed):
        raise UnsafePath("media_path_not_allowed")
    if must_exist and not p.is_file():
        raise UnsafePath("media_not_found")
    return p


def safe_state_file(path: Union[Path, str], roots: Optional[Iterable[Union[Path, str]]] = None) -> Path:
    """操作员侧给的初始 state 文件:过白名单根 + 必须存在。

    这是 CLI ``--initial-state-file`` 的纵深防御 —— CLI 由人操作、可信度高于 MCP,
    但仍然只允许读白名单目录里的文件。
    """
    allowed = [Path(r).resolve() for r in (roots if roots is not None else media_roots())]
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        raise UnsafePath("bad_state_file") from None
    if not allowed:
        raise UnsafePath("media_roots_unset")
    if not any(_contained(r, p) for r in allowed):
        raise UnsafePath("state_file_not_allowed")
    if not p.is_file():
        raise UnsafePath("state_file_missing")
    return p


# ------------------------------------------------------------------ 拒绝反馈
#: 异常 → 对外稳定的错误码。Agent 只看得到这个码。
CODES: dict[str, str] = {
    "bad_thread_id": "bad_thread_id",
    "bad_task_id": "bad_task_id",
    "gate_root_escape": "gate_root_escape",
    "task_path_escape": "task_path_escape",
    "frames_escape": "frames_escape",
    "empty_media_path": "empty_media_path",
    "media_roots_unset": "media_roots_unset",
    "bad_media_path": "bad_media_path",
    "media_path_not_allowed": "media_path_not_allowed",
    "media_not_found": "media_not_found",
    "bad_state_file": "bad_state_file",
    "state_file_not_allowed": "state_file_not_allowed",
    "state_file_missing": "state_file_missing",
}

#: 给 Agent 看的固定文案。**刻意不含本机绝对路径,也不回显用户输入**。
MESSAGES: dict[str, str] = {
    "bad_thread_id": "thread_id 不合法:只允许字母数字开头,后接字母数字/下划线/连字符,总长 1-64。",
    "bad_task_id": "task_id 不合法:必须是 t_ 加 16 位小写十六进制。",
    "gate_root_escape": "thread_id 解析后落在 outputs 根目录之外,已拒绝。",
    "task_path_escape": "task_id 解析后落在任务目录之外,已拒绝。",
    "frames_escape": "task_id 解析后落在抽帧目录之外,已拒绝。",
    "empty_media_path": "video_path 为空。给一个位于允许的素材目录内的视频文件绝对路径。",
    "media_roots_unset": "服务端没有配置素材白名单(AGENT_GATE_MEDIA_ROOTS),拒绝接受任何素材路径。"
                         "请让用户重新生成操作员目录并重启 Agent 会话。",
    "bad_media_path": "video_path 不是可用的路径。",
    "media_path_not_allowed": "video_path 不在服务端允许的素材目录内,已拒绝。"
                              "请让用户把视频放到允许的素材目录,或重新配置素材白名单。",
    "media_not_found": "video_path 指向的文件不存在。",
    "bad_state_file": "initial state 文件路径不可用。",
    "state_file_not_allowed": "initial state 文件不在允许的目录内,已拒绝。",
    "state_file_missing": "initial state 文件不存在。",
}


def code_of(exc: BaseException) -> str:
    """从异常里取稳定错误码。"""
    text = str(exc)
    return text if text in CODES else "unsafe_input"


def rejection_message(exc: BaseException) -> str:
    """给 Agent 看的说明。**永远不含本机绝对路径。**"""
    return MESSAGES.get(code_of(exc), "输入未通过安全校验,已拒绝。")


def rejection(exc: BaseException) -> dict[str, object]:
    """统一的拒绝返回体。"""
    return {"ok": False, "code": code_of(exc), "message": rejection_message(exc)}

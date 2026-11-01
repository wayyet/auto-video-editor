"""标准事件协议:事件类型、构造校验、脱敏与 SSE 帧(设计《工作流流程图与实时节点追踪》§7)。

职责与边界
----------
- **协议层,纯函数**:定义 §7.1 全部事件类型;``make_event`` 构造并校验事件;
  ``sanitize_*`` 家族执行敏感字段白名单与脱敏;``to_sse_frame`` 产出 §7.2 的
  SSE 文本帧。不持久化(那在 :mod:`monitoring.workflow_event_store`)、
  不消费 LangGraph 流(那在阶段 3 的 runner/adapter)。
- **白名单是拒绝式的**:``make_event`` 收到白名单外的顶层 payload 键直接抛
  ``ValueError``(设计 §9.1 不变量 4 —— 宁可在构造期失败,也不把未脱敏的
  内容漏到浏览器)。允许的键见 :data:`ALLOWED_PAYLOAD_KEYS`。
- 脱敏规则(:func:`sanitize_text`):密钥/令牌打码、DSN 密码打码、
  **绝对路径降级为文件名**(设计 §11.7:不推送绝对路径;§4.2 允许文件名)、
  超长文本截断(ASR 全文、提示词天然被截断防线挡下)。

事件 dict 形态(make_event 产出 / 事件表行 / SSE data 共用)::

    {
      "id": 42,                              # 仅事件表读出后有;SSE id 行用它
      "event_type": "node.started",          # §7.1 常量
      "thread_id": "video-001",
      "run_id": "run-abc",                   # 可选关联键
      "node_id": "assembly_build_timeline",  # 运行级事件为 None
      "attempt": 1,                          # 节点第几次尝试(重试递增)
      "occurred_at": "2026-11-01T07:20:30.123Z",
      "payload": {...}                       # 白名单内、已脱敏
    }

SSE 帧(``to_sse_frame``,payload 字段平铺进 data,对齐 §7.2 示例)::

    id: 42
    event: node.started
    data: {"thread_id":"video-001","node_id":"...","attempt":1,...}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_RUN_STARTED", "EVENT_NODE_STARTED", "EVENT_NODE_PROGRESS",
    "EVENT_NODE_SUCCEEDED", "EVENT_NODE_WAITING_INPUT", "EVENT_NODE_RESUMED",
    "EVENT_NODE_FAILED", "EVENT_NODE_RETRYING", "EVENT_BRANCH_SELECTED",
    "EVENT_RUN_INTERRUPTED", "EVENT_RUN_RESUMED", "EVENT_RUN_COMPLETED",
    "EVENT_RUN_FAILED",
    "NODE_EVENT_TYPES", "RUN_EVENT_TYPES", "BRANCH_EVENT_TYPES", "ALL_EVENT_TYPES",
    "WAITING_KIND_INTERRUPT", "WAITING_KIND_WEB_FILE_HANDSHAKE",
    "ALLOWED_PAYLOAD_KEYS", "SENSITIVE_KEY_NAMES",
    "make_event", "validate_event",
    "sanitize_text", "sanitize_value", "sanitize_payload", "sanitize_exception",
    "to_sse_frame",
]

# ---------------------------------------------------------------------------
# §7.1 事件类型常量
# ---------------------------------------------------------------------------
EVENT_RUN_STARTED = "run.started"
EVENT_NODE_STARTED = "node.started"
EVENT_NODE_PROGRESS = "node.progress"
EVENT_NODE_SUCCEEDED = "node.succeeded"
EVENT_NODE_WAITING_INPUT = "node.waiting_input"
EVENT_NODE_RESUMED = "node.resumed"
EVENT_NODE_FAILED = "node.failed"
EVENT_NODE_RETRYING = "node.retrying"
EVENT_BRANCH_SELECTED = "branch.selected"
EVENT_RUN_INTERRUPTED = "run.interrupted"
EVENT_RUN_RESUMED = "run.resumed"
EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"

NODE_EVENT_TYPES = frozenset({
    EVENT_NODE_STARTED, EVENT_NODE_PROGRESS, EVENT_NODE_SUCCEEDED,
    EVENT_NODE_WAITING_INPUT, EVENT_NODE_RESUMED, EVENT_NODE_FAILED,
    EVENT_NODE_RETRYING,
})
RUN_EVENT_TYPES = frozenset({
    EVENT_RUN_STARTED, EVENT_RUN_INTERRUPTED, EVENT_RUN_RESUMED,
    EVENT_RUN_COMPLETED, EVENT_RUN_FAILED,
})
BRANCH_EVENT_TYPES = frozenset({EVENT_BRANCH_SELECTED})
ALL_EVENT_TYPES = frozenset(NODE_EVENT_TYPES | RUN_EVENT_TYPES | BRANCH_EVENT_TYPES)

#: 等待类别(设计 §11.3:interrupt 与外部网页握手是两种等待,必须可区分)
WAITING_KIND_INTERRUPT = "interrupt"
WAITING_KIND_WEB_FILE_HANDSHAKE = "web_file_handshake"

# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------
#: 顶层 payload 允许的键(make_event 遇白名单外的键直接 ValueError —— 拒绝式)
ALLOWED_PAYLOAD_KEYS = frozenset({
    "summary",       # 状态增量摘要(嵌套 dict;值再经 sanitize_value)
    "detail",        # 事件细节(嵌套 dict;值再经 sanitize_value)
    "error",         # {"type","message"};由 sanitize_exception 产出/校验
    "waiting_kind",  # WAITING_KIND_* 之一
    "duration_ms",   # int,节点耗时
    "trigger",       # str,触发来源(LangGraph triggers 摘要,如 "branch:to:x"/"join:a+b:x")
    "label",         # str,branch.selected 的边标签
    "inferred",      # bool,branch.selected 是否为时序推断(§7.3 规则 8)
    "reason",        # str,运行级事件的简短原因
})

#: 任何嵌套层级都不得出现的键名(大小写不敏感;出现即丢弃该键)
SENSITIVE_KEY_NAMES = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "credentials", "credential", "dsn", "cookie",
    "api-key", "api-key-id", "secret_key",
})

# ---------------------------------------------------------------------------
# 脱敏参数与规则
# ---------------------------------------------------------------------------
MAX_TEXT_LEN = 500
MAX_LIST_ITEMS = 20
MAX_DICT_KEYS = 40
MAX_DEPTH = 4
MAX_PAYLOAD_JSON_CHARS = 8000

_TRUNC_SUFFIX = "…[截断]"

# 密钥/令牌:键值对形式(api_key=xxx / token: xxx),值整体打码
_SECRET_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|authorization|"
    r"credential[s]?|access[_-]?key)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
#: OpenAI 风格 sk- 令牌
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
#: Bearer 头
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}\b")
#: DSN 用户名:密码段(postgres://user:pass@ → postgres://***@)
_DSN_RE = re.compile(r"(?i)\b(postgres(ql)?|mysql|redis|mongodb)://([^:/\s@]+):([^@\s/]+)@")
#: Windows 盘符绝对路径(E:\a\b\c.mp4 / E:/a/b/c.mp4)→ 保留最后一段
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/])*[^\\/:*?\"<>|\r\n]*")
#: UNC 路径(\\server\share\f.mp4)→ 保留最后一段
_UNC_PATH_RE = re.compile(r"\\\\[^\\/\r\n]+(?:[\\/][^\\/:*?\"<>|\r\n]+)+")
#: POSIX 常见根目录绝对路径(/home/...、/Users/... 等)→ 保留最后一段
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:])/(?:home|Users|root|var|tmp|opt|mnt|srv|etc|usr|data)"
    r"(?:/[^/\s]+)+/?"
)


def _mask_paths(text: str) -> str:
    """绝对路径降级为最后一段(文件名);相对路径不动(§4.2 允许文件名)。"""

    def _last_seg(match: re.Match[str]) -> str:
        seg = re.split(r"[\\/]", match.group(0).rstrip("/\\"))[-1]
        return seg or "<path>"

    text = _WIN_PATH_RE.sub(_last_seg, text)
    text = _UNC_PATH_RE.sub(_last_seg, text)
    text = _POSIX_PATH_RE.sub(_last_seg, text)
    return text


def sanitize_text(text: str, *, max_len: int = MAX_TEXT_LEN) -> str:
    """对一段要进事件/浏览器可见的文本脱敏:密钥打码 → 绝对路径降级 → 截断。"""
    if not isinstance(text, str):
        text = str(text)
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    out = _SK_RE.sub("sk-***", out)
    out = _BEARER_RE.sub("Bearer ***", out)
    out = _DSN_RE.sub(lambda m: f"{m.group(1)}://***@", out)
    out = _mask_paths(out)
    if len(out) > max_len:
        out = out[:max_len] + _TRUNC_SUFFIX
    return out


def _is_sensitive_key(key: str) -> bool:
    return key.strip().lower() in SENSITIVE_KEY_NAMES


def sanitize_value(value: Any, depth: int = 0) -> Any:
    """递归脱敏任意 JSON 值:字符串过 :func:`sanitize_text`,敏感键丢弃,容器限幅。"""
    if depth > MAX_DEPTH:
        return "<超深>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, bytes):
        return sanitize_text(value.decode("utf-8", "replace"))
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_DICT_KEYS:
                out["<超限>"] = f"键数超过 {MAX_DICT_KEYS},已截断"
                break
            key = str(k)
            if _is_sensitive_key(key):
                continue
            out[key] = sanitize_value(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_LIST_ITEMS]
        return [sanitize_value(v, depth + 1) for v in items]
    return sanitize_text(repr(value))


def sanitize_payload(payload: Any) -> dict[str, Any]:
    """按白名单过滤顶层键并递归脱敏;白名单外的顶层键**直接拒绝**。

    Raises:
        ValueError: ``payload`` 不是 dict,或含白名单外的顶层键(明确拒绝,
            防止未脱敏内容靠"静默丢弃"溜进真实事件)。
    """
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"事件 payload 必须是 dict,得到 {type(payload).__name__}")
    rejected = [str(k) for k in payload if k not in ALLOWED_PAYLOAD_KEYS]
    if rejected:
        raise ValueError(
            f"事件 payload 含白名单外的键:{rejected};"
            f"允许的键:{sorted(ALLOWED_PAYLOAD_KEYS)}(白名单见 workflow_events.ALLOWED_PAYLOAD_KEYS)"
        )
    out: dict[str, Any] = {}
    for key in ALLOWED_PAYLOAD_KEYS & payload.keys():
        value = payload[key]
        if key == "error":
            out[key] = _sanitize_error_value(value)
        else:
            out[key] = sanitize_value(value)
    return _cap_payload_size(out)


def _sanitize_error_value(value: Any) -> dict[str, str]:
    """error 字段归一为 {"type","message"} 并脱敏(接受 str / Exception / dict)。"""
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": sanitize_text(str(value))}
    if isinstance(value, str):
        return {"type": "error", "message": sanitize_text(value)}
    if isinstance(value, dict):
        etype = value.get("type")
        message = value.get("message", "")
        return {
            "type": sanitize_text(str(etype), max_len=100),
            "message": sanitize_text(str(message)),
        }
    return {"type": "error", "message": sanitize_text(repr(value))}


def _cap_payload_size(payload: dict[str, Any]) -> dict[str, Any]:
    """整体体积防线:超限时把 summary/detail 换成截断标记(其余键保留)。"""
    if len(json.dumps(payload, ensure_ascii=False)) <= MAX_PAYLOAD_JSON_CHARS:
        return payload
    capped = {k: v for k, v in payload.items() if k not in ("summary", "detail")}
    capped["detail"] = {"truncated": True, "reason": "payload 超过体积上限"}
    return capped


def sanitize_exception(exc: BaseException | str) -> dict[str, str]:
    """异常 → {"type","message"}(脱敏);runner 兜底 node.failed/run.failed 用。"""
    if isinstance(exc, BaseException):
        return {"type": type(exc).__name__, "message": sanitize_text(str(exc))}
    return {"type": "error", "message": sanitize_text(str(exc))}


# ---------------------------------------------------------------------------
# 事件构造与校验
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_occurred_at(value: Any) -> str:
    """统一成 UTC ISO8601 + Z 后缀(存库时 PG 的 timestamptz 可直接解析)。"""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    text = str(value or "").strip()
    if not text:
        return _utc_now_iso()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)  # 非法格式抛 ValueError → make_event 透传
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_event(
    event_type: str,
    *,
    thread_id: str,
    node_id: Optional[str] = None,
    attempt: int = 1,
    run_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    occurred_at: Any = None,
) -> dict[str, Any]:
    """构造一条标准事件(校验 + 脱敏);非法输入抛 ``ValueError``。

    - ``node.*`` 事件必须带非空 ``node_id``;``run.*`` 事件必须不带;
      ``branch.*`` 可选。
    - ``attempt`` 必须 >= 1(重试由调用方递增,协议层只校验)。
    - payload 过 :func:`sanitize_payload`(白名单拒绝式 + 递归脱敏)。
    """
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(
            f"未知事件类型 {event_type!r};合法取值:{sorted(ALL_EVENT_TYPES)}"
        )
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id 必须是非空 str")
    if event_type in NODE_EVENT_TYPES:
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError(f"{event_type} 事件必须带非空 node_id")
    elif event_type in RUN_EVENT_TYPES:
        if node_id is not None:
            raise ValueError(f"{event_type} 是运行级事件,node_id 必须为 None(得到 {node_id!r})")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError(f"attempt 必须 >= 1 的 int(得到 {attempt!r})")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError(f"run_id 必须是非空 str 或 None(得到 {run_id!r})")

    event: dict[str, Any] = {
        "event_type": event_type,
        "thread_id": thread_id.strip(),
        "run_id": run_id.strip() if isinstance(run_id, str) else None,
        "node_id": node_id.strip() if isinstance(node_id, str) else None,
        "attempt": attempt,
        "occurred_at": _normalize_occurred_at(occurred_at) if occurred_at else _utc_now_iso(),
        "payload": sanitize_payload(payload),
    }
    return event


def validate_event(event: Any) -> dict[str, Any]:
    """校验事件 dict 形态(事件表 append / SSE 帧输出前的防线)。

    与 :func:`make_event` 的区别:不重新脱敏、不补默认值,只验证必需字段;
    ``id`` 可选(事件表读出后有)。
    """
    if not isinstance(event, dict):
        raise ValueError(f"事件必须是 dict(得到 {type(event).__name__})")
    missing = [k for k in ("event_type", "thread_id", "attempt", "occurred_at", "payload")
               if k not in event]
    if missing:
        raise ValueError(f"事件缺少必需字段:{missing}")
    event_type = event["event_type"]
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(f"未知事件类型 {event_type!r}")
    if not isinstance(event["thread_id"], str) or not event["thread_id"].strip():
        raise ValueError("thread_id 必须是非空 str")
    if not isinstance(event["attempt"], int) or event["attempt"] < 1:
        raise ValueError(f"attempt 必须 >= 1(得到 {event['attempt']!r})")
    if not isinstance(event["payload"], dict):
        raise ValueError("payload 必须是 dict")
    if event_type in NODE_EVENT_TYPES:
        if not isinstance(event.get("node_id"), str) or not event["node_id"].strip():
            raise ValueError(f"{event_type} 事件必须带非空 node_id")
    return event


# ---------------------------------------------------------------------------
# SSE 帧(§7.2)
# ---------------------------------------------------------------------------
def to_sse_frame(event: dict[str, Any]) -> str:
    """把事件渲染成 SSE 文本帧;payload 字段平铺进 data(对齐 §7.2 示例)。

    ``event["id"]`` 存在时输出 ``id:`` 行(SSE 断线重连的 Last-Event-ID 游标,
    值 = 事件表 BIGSERIAL id);不存在(尚未落库)时省略 id 行。
    """
    validate_event(event)
    lines: list[str] = []
    if event.get("id") is not None:
        lines.append(f"id: {event['id']}")
    lines.append(f"event: {event['event_type']}")
    data = {
        "thread_id": event["thread_id"],
        "run_id": event.get("run_id"),
        "node_id": event.get("node_id"),
        "attempt": event["attempt"],
        "occurred_at": event["occurred_at"],
        **event["payload"],
    }
    lines.append("data: " + json.dumps(data, ensure_ascii=False))
    return "\n".join(lines) + "\n\n"

"""``workflow_chat_links`` 会话—工作流关联存储(执行设计《对话框显示 LangGraph 当前运行节点》§4.3/§4.4)。

分工与设计要点
--------------
- ``chat_sessions`` 是聊天会话事实源;``workflow_runs`` / ``workflow_run_events``
  按 **thread_id** 组织;本表补上两者之间缺失的一环 —— 每个聊天会话当前关联
  哪个工作流线程,对话页凭会话 ID 解析 thread_id(执行设计 §5.1)。
- **每会话单行**(主键 ``chat_session_id``,"最近一次"语义):新工作流启动时
  upsert 覆盖;完成 / 失败后保留终态行(刷新页面仍能显示终态),新运行再覆盖。
- **写降级、读抛错**(§4.4 第 5 条):写入失败记 ERROR(含 chat_session_id /
  thread_id / 目标状态)+ 降级计数,**绝不阻塞工作流主流程**;查询失败向上抛
  :class:`ChatLinkStoreUnavailable` —— API 层依赖"查无关联"与"查不了"的区别,
  后者必须 503,不能冒充"无关联"(§5.1)。
- **三个 ID 严格分离**(§10.3 红线 1)::func:`validate_chat_session_id`
  (32 位十六进制,与 ``chat_sessions`` CHECK 一致)由 API 与 Store 共用;
  ``thread_id`` 规则与 ``workflow_runs`` CHECK 一致。
- URI 默认走 ``db.postgres_config.resolve_postgres_target()``(与事件存储同一
  优先级);测试可显式传 ``uri`` 指向隔离库。
- 连接复用 ``db.connection``(client_encoding=UTF-8;本机库 SQL_ASCII,不设的
  话中文查询串直接报错)。
- 顶层 import 仅 stdlib(psycopg / db 均延迟)—— Web 进程跨 venv 轻量导入。
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "CHAT_LINKS_TABLE",
    "CHAT_SESSION_ID_RE",
    "VALID_LINK_STATUSES",
    "ChatLinkStoreUnavailable",
    "ChatLinkValidationError",
    "WorkflowChatLinkStore",
    "degraded_count",
    "get_by_chat_session",
    "update_status",
    "upsert_running",
    "validate_chat_session_id",
    "validate_thread_id",
]

CHAT_LINKS_TABLE = "workflow_chat_links"

#: 会话 ID 形如 ``uuid.uuid4().hex`` —— 32 位小写十六进制;与
#: ``chat_sessions.session_id`` 的 CHECK(db/migrations.py 迁移 001)保持一致。
CHAT_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: thread_id 规则与 ``workflow_runs.workflow_runs_thread_id_check`` 同
#: (禁 bytes 字面量 / 路径注入,方案 §3.2 A2)。
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")

#: 关联状态四态(§4.3 表 CHECK 同款)。
VALID_LINK_STATUSES = frozenset({"running", "waiting_human", "completed", "failed"})


class ChatLinkValidationError(ValueError):
    """ID / 状态取值非法(调用方错误;API 层转 400,runner 侧降级不阻塞)。"""


class ChatLinkStoreUnavailable(RuntimeError):
    """关联存储不可用(连接 / 查询失败;API 层转 503,不得冒充"无关联")。"""


# ---------------------------------------------------------------------------
# ID / 状态校验(API 与 Store 共用,执行设计 §10.4 任务 1.4)
# ---------------------------------------------------------------------------
def validate_chat_session_id(raw: Any) -> str:
    """校验并归一化聊天会话 ID(去空白 + 转小写后须为 32 位十六进制)。

    Raises:
        ChatLinkValidationError: 格式非法。
    """
    value = str(raw or "").strip().lower()
    if not CHAT_SESSION_ID_RE.match(value):
        raise ChatLinkValidationError(
            "chat_session_id 非法:应为 32 位十六进制(与 chat_sessions CHECK 一致),"
            f"实际收到长度 {len(value)}"
        )
    return value


def validate_thread_id(raw: Any) -> str:
    """校验 thread_id(规则与 ``workflow_runs`` 表 CHECK 一致)。

    Raises:
        ChatLinkValidationError: 格式非法。
    """
    value = str(raw or "").strip()
    if not _THREAD_ID_RE.match(value):
        raise ChatLinkValidationError(
            "thread_id 非法:只允许 [A-Za-z0-9_.:-],长度 1-128"
            "(与 workflow_runs 表 CHECK 一致)"
        )
    return value


# ---------------------------------------------------------------------------
# 降级计数(runs_table / workflow_event_store 风格:ERROR 日志 + 计数,可观测)
# ---------------------------------------------------------------------------
_degraded_count = 0
_degraded_lock = threading.Lock()


def degraded_count() -> int:
    """本进程累计的关联存储降级次数;> 0 说明关联没有真正落库,应当告警。"""
    with _degraded_lock:
        return _degraded_count


def _record_degraded(operation: str, exc: BaseException, *, context: str) -> None:
    global _degraded_count
    with _degraded_lock:
        _degraded_count += 1
        total = _degraded_count
    logger.error(
        "[workflow_chat_link_store] %s failed (%s: %s) — 已降级(累计 %s 次);%s"
        "关联未落库,对话页将查不到该工作流关联,需排查 Postgres",
        operation, type(exc).__name__, exc, total, context,
    )


def _text(value: Any) -> Any:
    """TEXT 列防御性解码(新连接 client_encoding=UTF-8 返回 str;旧连接给 bytes)。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------
class WorkflowChatLinkStore:
    """``workflow_chat_links`` 仓储(每会话单行,upsert 覆盖式)。

    所有方法同步阻塞(psycopg 同步驱动)—— runner / API 经线程池调用;
    失败语义见模块 docstring(写降级、读抛错)。
    """

    _SELECT_COLUMNS = "chat_session_id, thread_id, run_id, status, updated_at"

    def __init__(self, uri: Optional[str] = None) -> None:
        self._uri = uri

    # -- 连接 ---------------------------------------------------------------
    def _target_uri(self) -> str:
        if self._uri:
            return self._uri
        from db.postgres_config import resolve_postgres_target

        return resolve_postgres_target().uri

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        from db.connection import connection as _db_connection

        with _db_connection(self._target_uri()) as conn:
            yield conn

    # -- 写入 ---------------------------------------------------------------
    def upsert_running(
        self,
        chat_session_id: str,
        thread_id: str,
        run_id: Optional[str] = None,
    ) -> bool:
        """建立 / 刷新关联为 ``running``(工作流启动、恢复前调用;§4.4 第 3 条)。

        同一会话重复调用 = 覆盖为最近一次运行(§4.3"每会话单行")。

        Returns:
            True = 已落库;False = 存储不可用 / 写入失败(已记 ERROR + 计数)。

        Raises:
            ChatLinkValidationError: 会话 ID / thread_id 格式非法(不触碰数据库)。
        """
        csid = validate_chat_session_id(chat_session_id)
        tid = validate_thread_id(thread_id)
        rid = str(run_id).strip() if run_id else None
        context = f"(chat_session={csid}, thread={tid}, status=running)"
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {CHAT_LINKS_TABLE} "
                        "(chat_session_id, thread_id, run_id, status) "
                        "VALUES (%s, %s, %s, 'running') "
                        "ON CONFLICT (chat_session_id) DO UPDATE SET "
                        "thread_id = EXCLUDED.thread_id, "
                        "run_id = EXCLUDED.run_id, "
                        "status = 'running', "
                        "updated_at = now()",
                        (csid, tid, rid),
                    )
                conn.commit()
            return True
        except Exception as e:  # noqa: BLE001 — 降级语义见模块 docstring
            _record_degraded("upsert_running", e, context=context)
            return False

    def update_status(
        self,
        chat_session_id: str,
        status: str,
        *,
        thread_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> bool:
        """生命周期状态流转(§4.4 第 4 条):running / waiting_human / completed / failed。

        Args:
            thread_id: 行不存在但提供了 thread_id 时补插(防 upsert 失败后
                状态更新被静默丢弃);行存在时仅作日志上下文。
            run_id: 提供 时覆盖行上的 run_id(缺省保留原值)。

        Returns:
            True = 已落库;False = 存储不可用 / 写入失败(已记 ERROR + 计数)。

        Raises:
            ChatLinkValidationError: 会话 ID / 状态取值非法(不触碰数据库)。
        """
        csid = validate_chat_session_id(chat_session_id)
        if status not in VALID_LINK_STATUSES:
            raise ChatLinkValidationError(
                f"status 非法:只允许 {sorted(VALID_LINK_STATUSES)},实际 {status!r}"
            )
        tid = validate_thread_id(thread_id) if thread_id else None
        rid = str(run_id).strip() if run_id else None
        context = f"(chat_session={csid}, thread={tid or '?'}, status={status})"
        try:
            updated = False
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {CHAT_LINKS_TABLE} "
                        "SET status = %s, run_id = COALESCE(%s, run_id), "
                        "updated_at = now() "
                        "WHERE chat_session_id = %s",
                        (status, rid, csid),
                    )
                    updated = cur.rowcount > 0
                    if not updated and tid:
                        # 行不存在但有 thread_id(理论少见:upsert 已先行)→ 补插,
                        # 避免状态更新被静默丢弃。
                        cur.execute(
                            f"INSERT INTO {CHAT_LINKS_TABLE} "
                            "(chat_session_id, thread_id, run_id, status) "
                            "VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (chat_session_id) DO UPDATE SET "
                            "status = EXCLUDED.status, "
                            "run_id = EXCLUDED.run_id, "
                            "updated_at = now()",
                            (csid, tid, rid, status),
                        )
                        updated = True
                conn.commit()
            if not updated:
                logger.warning(
                    "[workflow_chat_link_store] update_status 无关联行可更新,已跳过 %s",
                    context,
                )
            return updated
        except Exception as e:  # noqa: BLE001 — 降级语义见模块 docstring
            _record_degraded("update_status", e, context=context)
            return False

    # -- 读取 ---------------------------------------------------------------
    def get_by_chat_session(self, chat_session_id: str) -> Optional[dict[str, Any]]:
        """按聊天会话 ID 查最近一次关联;无关联返回 ``None``。

        Returns:
            dict::

                {"chat_session_id", "thread_id", "run_id", "status",
                 "updated_at"}   # updated_at 为 UTC ISO-8601(Z)字符串

            无关联时 ``None``。

        Raises:
            ChatLinkValidationError: 会话 ID 格式非法(API 层转 400)。
            ChatLinkStoreUnavailable: 存储不可用(API 层转 503,
                **不得**冒充"无关联",执行设计 §5.1)。
        """
        csid = validate_chat_session_id(chat_session_id)
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {self._SELECT_COLUMNS} FROM {CHAT_LINKS_TABLE} "
                        "WHERE chat_session_id = %s",
                        (csid,),
                    )
                    row = cur.fetchone()
        except Exception as e:  # noqa: BLE001 — 读失败必须显式上抛(§5.1)
            raise ChatLinkStoreUnavailable(
                f"workflow_chat_links 查询失败(chat_session={csid}): "
                f"{type(e).__name__}: {e}"
            ) from e
        if row is None:
            return None
        csid_v, tid, rid, status, updated_at = row
        result: dict[str, Any] = {
            "chat_session_id": _text(csid_v),
            "thread_id": _text(tid),
            "run_id": _text(rid),
            "status": _text(status),
            "updated_at": None,
        }
        if isinstance(updated_at, datetime):
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            result["updated_at"] = (
                updated_at.astimezone(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        return result


# ---------------------------------------------------------------------------
# 模块级便捷入口(默认 URI 动态解析;测试用 uri= 指向隔离库)
# ---------------------------------------------------------------------------
def upsert_running(
    chat_session_id: str,
    thread_id: str,
    run_id: Optional[str] = None,
    *,
    uri: Optional[str] = None,
) -> bool:
    """模块级入口,参数见 :meth:`WorkflowChatLinkStore.upsert_running`。"""
    return WorkflowChatLinkStore(uri=uri).upsert_running(chat_session_id, thread_id, run_id)


def update_status(
    chat_session_id: str,
    status: str,
    *,
    thread_id: Optional[str] = None,
    run_id: Optional[str] = None,
    uri: Optional[str] = None,
) -> bool:
    """模块级入口,参数见 :meth:`WorkflowChatLinkStore.update_status`。"""
    return WorkflowChatLinkStore(uri=uri).update_status(
        chat_session_id, status, thread_id=thread_id, run_id=run_id
    )


def get_by_chat_session(
    chat_session_id: str,
    *,
    uri: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """模块级入口,参数见 :meth:`WorkflowChatLinkStore.get_by_chat_session`。"""
    return WorkflowChatLinkStore(uri=uri).get_by_chat_session(chat_session_id)

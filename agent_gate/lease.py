"""线程级操作员租约(ZCode 接入设计 §6.4 / §10.5 阶段 D)。

为什么需要
----------
同一个 thread_id 的任务信箱不允许两个 Agent 同时消费:``gate_next_tasks``
没有认领机制时,任务对所有消费者广播,谁先 ``gate_submit_result`` 谁落盘,
另一个消费者迟到提交会被丢弃或答非所问。租约以 thread_id 为 key 记录
「当前操作员」,第二个消费者拿到稳定错误码 ``operator_already_claimed``。

存储与原子性(D-T1 / D-T2)
--------------------------
- 表 ``agent_gate_operator_leases``:``thread_id`` PRIMARY KEY —— PostgreSQL
  唯一约束保证并发 INSERT 只有一个成功;取得 / 续约 / 接管都在**单条事务**
  (SELECT ... FOR UPDATE 行锁)里完成,不用进程内锁。
- 表由本模块自建(``ensure_lease_table``,IF NOT EXISTS 幂等),和 LangGraph
  checkpoint 表同一套分工先例:saver 自己建自己的表,不进 ``db/migrations``
  —— agent_gate 的 DSN(``AGENT_GATE_POSTGRES_URI`` 优先)可能与应用库不同,
  放迁移框架里反而对不上库。
- TTL 回收:``expires_at`` 过后任何 owner 都可接管,覆盖「进程崩溃后由 TTL
  回收」(§6.4)。
- 崩溃快速回收:持有人进程在**同一主机**且 PID 已死 → 立即允许接管。这让
  「MCode 每轮 exec 一个新进程」「MCP 会话结束后换新会话续跑」这类**串行**
  消费者不必干等 TTL;PID 复用等边界情况仍由 TTL 兜底。PID 存活检测不引入
  新依赖:Windows 用 ctypes OpenProcess,POSIX 用 ``os.kill(pid, 0)``。

Owner 身份(D-T1)
------------------
- 环境变量 ``AGENT_GATE_OPERATOR_ID`` 优先(部署方显式指定,例如给某个
  长驻消费者固定身份)。
- 否则按进程自动生成 ``<host>-<pid>-<rand>``:每个 MCP server / CLI drive
  进程都是独立操作员 —— 并发会话之间互斥,会话结束后新会话可立即接管。

哪些入口接入(D-T3)
--------------------
- **检查**(不取得):``run_start`` / ``continue_after_crash``(``runner.spawn_worker``)。
  只挡「别的活操作员正持有租约」的启动;取得交给第一次真正的消费动作。
  这样 drive 模式(父进程拉起 worker、由 agent 会话消费)不会被父进程的
  身份挡住。``mode=resume`` 是人工放行,**永不**被租约拦截。
- **取得 / 续约**:``gate_next_tasks``(``service.wait_for_work``)与
  ``gate_submit_result``(``service.submit_result``,batch 逐个走它)。
  取得即登记到进程内 ``_OWNED``,``release_owned_leases`` 供正常退出时释放。
- **不拦**:``run_status`` / ``gate_get_task`` / ``events``(只读)与人工路径。

失败语义(§7 约束 7 不静默回退)
------------------------------
PostgreSQL 不可用 → ``lease_backend_unavailable`` 明确报错,fail-closed;
不降级成「无租约放行」。

TTL 与开关
----------
- ``AGENT_GATE_LEASE_TTL_S`` 租约时长,默认 900 秒;每次消费触点续约。
- ``AGENT_GATE_LEASE_PID_CHECK`` 置 ``0`` 关闭崩溃快速回收(只用 TTL)。
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Optional

from agent_gate.safety import UnsafeIdentifier, safe_thread_id

#: 冲突时的稳定错误码(§6.4:同一线程被其他 Agent 占用时返回它)。
CODE_CLAIMED = "operator_already_claimed"
CODE_BACKEND = "lease_backend_unavailable"

TTL_ENV = "AGENT_GATE_LEASE_TTL_S"
DEFAULT_TTL_S = 900.0

PID_CHECK_ENV = "AGENT_GATE_LEASE_PID_CHECK"

LEASE_TABLE = "agent_gate_operator_leases"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEASE_TABLE} (
    thread_id   TEXT PRIMARY KEY
                CONSTRAINT agent_gate_leases_thread_id_check
                CHECK (thread_id ~ '^[A-Za-z0-9_-]{{1,64}}$'),
    owner       TEXT NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    pid         INTEGER NOT NULL DEFAULT 0,
    run_id      TEXT NOT NULL DEFAULT '',
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    renew_count INTEGER NOT NULL DEFAULT 0
);
"""

_TABLE_READY = False

#: 本进程成功取得的租约 {(dsn, thread_id): owner}。正常退出时统一释放,
#: 让「同一个消费者的下一段旅程」(MCode 下一轮、MCP 新会话)立即接管,
#: 不必等 TTL。进程被强杀时不会有 atexit —— 由 PID 存活检测 / TTL 兜底。
_OWNED: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------- owner
_PROCESS_OWNER: str = ""


def _auto_owner() -> str:
    return f"{socket.gethostname()}-pid{os.getpid()}-{uuid.uuid4().hex[:8]}"


def owner_id(default: Optional[str] = None) -> str:
    """解析本进程的操作员身份:环境变量 > 显式 default > 进程级自动 ID。

    自动 ID 按**进程**缓存(不是每次调用重新生成):同一个消费者进程
    (MCP server / drive)在 run_start 检查、gate_next_tasks 取得、
    submit 续约、continue_after_crash 再检查之间必须是**同一个**身份,
    否则会把自己锁在门外。
    """
    global _PROCESS_OWNER
    env = (os.environ.get("AGENT_GATE_OPERATOR_ID") or "").strip()
    if env:
        return env[:128]
    if default:
        return str(default)[:128]
    if not _PROCESS_OWNER:
        _PROCESS_OWNER = _auto_owner()
    return _PROCESS_OWNER


def _pid_check_enabled() -> bool:
    return (os.environ.get(PID_CHECK_ENV) or "1").strip().lower() not in ("0", "false", "off")


def _pid_alive(pid: int) -> bool:
    """进程还活着吗(尽力而为;检测不了时按「活着」处理,TTL 兜底)。"""
    if pid <= 0:
        return False
    try:
        import psutil  # 装了就用它(语义最全:权限不足也按存在算)

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # 87 = 参数无效(PID 不存在);5 = 拒绝访问(存在,只是看不见)
            return k32.GetLastError() != 87
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- 连接
def _connect():
    """连到 worker 用的那套 Postgres(runner.postgres_uri 的解析顺序)。

    直接用 psycopg 而不是 ``db.connection.connect``:那里不带
    ``row_factory=dict_row``,而租约代码按列名取值。``client_encoding``
    与 ``connect_timeout`` 按 db.connection 的同款约定显式给出。
    """
    from agent_gate import runner  # 函数内 import:runner -> service -> lease 会成环

    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(runner.postgres_uri(), connect_timeout=10,
                           client_encoding="utf-8", row_factory=dict_row)


def _ensure_table(conn: Any) -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()
    _TABLE_READY = True


# ---------------------------------------------------------------- 核心
def acquire(
    thread_id: str,
    owner: str,
    *,
    run_id: str = "",
    ttl_s: Optional[float] = None,
    record_owned: bool = True,
) -> dict[str, Any]:
    """取得 / 续约 / 接管一个 thread 的操作员租约(原子)。

    Returns:
        成功:``{"ok": True, "action": "acquired"|"renewed"|"takeover",
        "expires_at": iso, "previous_owner": str|None}``。
        失败:``{"ok": False, "code": "operator_already_claimed"|"lease_backend_unavailable", ...}``。
    """
    try:
        safe_thread_id(thread_id)
    except UnsafeIdentifier as e:
        return {"ok": False, "code": "bad_thread_id", "message": str(e)}
    ttl = float(ttl_s) if ttl_s is not None else _default_ttl()
    my_host = socket.gethostname()

    try:
        with _connect() as conn:
            _ensure_table(conn)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT owner, host, pid, expires_at FROM {LEASE_TABLE} "
                        "WHERE thread_id = %s FOR UPDATE",
                        (thread_id,),
                    )
                    row = cur.fetchone()

                    if row is None:
                        cur.execute(
                            f"INSERT INTO {LEASE_TABLE} "
                            "(thread_id, owner, host, pid, run_id, expires_at) "
                            "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s)) "
                            "ON CONFLICT (thread_id) DO NOTHING "
                            "RETURNING expires_at",
                            (thread_id, owner, my_host, os.getpid(), run_id, ttl),
                        )
                        ins = cur.fetchone()
                        if ins is None:
                            # 并发对手抢先把行插进来了:交给下一轮重读(行锁已在手)
                            cur.execute(
                                f"SELECT owner, host, pid, expires_at FROM {LEASE_TABLE} "
                                "WHERE thread_id = %s FOR UPDATE",
                                (thread_id,),
                            )
                            row = cur.fetchone()
                        else:
                            return _ok("acquired", ins["expires_at"], None, thread_id, owner, record_owned)

                    if row is None:  # pragma: no cover — 理论不可达,防御
                        return _backend_error(RuntimeError("lease row vanished"))

                    holder_owner = str(row["owner"])
                    if holder_owner == owner:
                        cur.execute(
                            f"UPDATE {LEASE_TABLE} "
                            "SET expires_at = now() + make_interval(secs => %s), "
                            "renew_count = renew_count + 1, "
                            "host = %s, pid = %s, "
                            "run_id = CASE WHEN %s <> '' THEN %s ELSE run_id END "
                            "WHERE thread_id = %s AND owner = %s "
                            "RETURNING expires_at",
                            (ttl, my_host, os.getpid(), run_id, run_id, thread_id, owner),
                        )
                        return _ok("renewed", cur.fetchone()["expires_at"], None,
                                   thread_id, owner, record_owned)

                    expired = row["expires_at"] is not None and row["expires_at"] <= _db_now(conn)
                    holder_gone = (
                        str(row["host"] or "") == my_host
                        and _pid_check_enabled()
                        and not _pid_alive(int(row["pid"] or 0))
                    )
                    if not (expired or holder_gone):
                        return {
                            "ok": False,
                            "code": CODE_CLAIMED,
                            "message": (
                                f"thread {thread_id} 已由其他操作员消费中"
                                f"(owner={holder_owner})。同一 thread 同一时刻只允许一个"
                                "消费者;等它退出/TTL 过期后再接管,或换一个 thread_id。"
                            ),
                            "owner": holder_owner,
                            "expires_at": _iso(row["expires_at"]),
                        }
                    reason = "expired" if expired else "holder_gone"
                    cur.execute(
                        f"UPDATE {LEASE_TABLE} "
                        "SET owner = %s, host = %s, pid = %s, run_id = %s, "
                        "acquired_at = now(), expires_at = now() + make_interval(secs => %s), "
                        "renew_count = 0 "
                        "WHERE thread_id = %s AND owner = %s "
                        "RETURNING expires_at",
                        (owner, my_host, os.getpid(), run_id, ttl, thread_id, holder_owner),
                    )
                    taken = cur.fetchone()
                    if taken is None:  # pragma: no cover — 行锁在手,理论不可达
                        return {
                            "ok": False, "code": CODE_CLAIMED,
                            "message": f"thread {thread_id} 租约状态在事务内变化,请重试。",
                            "owner": holder_owner,
                        }
                    return _ok("takeover", taken["expires_at"], holder_owner,
                               thread_id, owner, record_owned, reason=reason)
    except UnsafeIdentifier:
        raise
    except Exception as e:  # noqa: BLE001 — DB 不可用必须显式报错(不静默放行)
        return _backend_error(e)


def check_not_claimed_by_other(thread_id: str, owner: str) -> Optional[dict[str, Any]]:
    """只读检查:活着的其他操作员是否持有租约。

    供 ``run_start``(spawn_worker)用:**检查而不取得** —— drive 模式里
    拉起 worker 的父进程和真正消费的 agent 会话不是同一进程,这里取得
    租约会把消费者挡在门外。返回 None = 放行;dict = 拒绝体。

    注意:检查通过≠一定取得得到(检查与首次消费之间是窗口),真正的互斥
    由 ``acquire`` 在 ``gate_next_tasks`` / ``submit`` 处保证。
    """
    try:
        safe_thread_id(thread_id)
    except UnsafeIdentifier as e:
        return {"ok": False, "code": "bad_thread_id", "message": str(e)}
    try:
        with _connect() as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT owner, host, pid, expires_at FROM {LEASE_TABLE} "
                    "WHERE thread_id = %s",
                    (thread_id,),
                )
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — fail-closed(§7 约束 7)
        return _backend_error(e)
    if row is None:
        return None
    holder = str(row["owner"])
    if holder == owner:
        return None
    expired = _expired_of_read(row)
    holder_gone = (
        str(row["host"] or "") == socket.gethostname()
        and _pid_check_enabled()
        and not _pid_alive(int(row["pid"] or 0))
    )
    if expired or holder_gone:
        return None
    return {
        "ok": False,
        "code": CODE_CLAIMED,
        "message": (
            f"thread {thread_id} 已由其他操作员消费中(owner={holder})。"
            "不能由第二个消费者启动或接管;等它退出/TTL 过期后再来。"
        ),
        "owner": holder,
        "expires_at": _iso(row["expires_at"]),
    }


def release(thread_id: str, owner: str) -> dict[str, Any]:
    """释放自己持有的租约(只能释放自己的)。"""
    try:
        safe_thread_id(thread_id)
    except UnsafeIdentifier as e:
        return {"ok": False, "code": "bad_thread_id", "message": str(e)}
    try:
        with _connect() as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {LEASE_TABLE} WHERE thread_id = %s AND owner = %s",
                    (thread_id, owner),
                )
                deleted = cur.rowcount
            conn.commit()
        for key in [k for k in _OWNED if k[0] == thread_id and k[1] == owner]:
            _OWNED.pop(key, None)
        return {"ok": True, "released": deleted > 0}
    except Exception as e:  # noqa: BLE001 — 释放失败不影响主流程,但要留痕
        return {"ok": False, "code": CODE_BACKEND, "message": str(e)[:300]}


def release_owned_leases() -> int:
    """释放本进程取得的所有租约(正常退出路径,尽力而为)。"""
    if not _OWNED:
        return 0
    released = 0
    for (thread_id, owner) in list(_OWNED):
        res = release(thread_id, owner)
        if res.get("ok") and res.get("released"):
            released += 1
    return released


def lease_view(thread_id: str) -> Optional[dict[str, Any]]:
    """读租约现状(诊断 / 测试用);没有租约返回 None。"""
    try:
        with _connect() as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT owner, host, pid, run_id, acquired_at, expires_at, renew_count "
                    f"FROM {LEASE_TABLE} WHERE thread_id = %s",
                    (thread_id,),
                )
                row = cur.fetchone()
    except Exception:  # noqa: BLE001 — 诊断接口,失败按"无租约"
        return None
    if row is None:
        return None
    return {
        "thread_id": thread_id,
        "owner": str(row["owner"]),
        "host": str(row["host"] or ""),
        "pid": int(row["pid"] or 0),
        "run_id": str(row["run_id"] or ""),
        "acquired_at": _iso(row["acquired_at"]),
        "expires_at": _iso(row["expires_at"]),
        "renew_count": int(row["renew_count"] or 0),
    }


# ---------------------------------------------------------------- 服务层守卫
def require_operator(mb: Any, *, operator: Optional[str] = None,
                     run_id: str = "") -> dict[str, Any]:
    """消费触点(gate_next_tasks / submit)的守卫:取得或续约;失败返回拒绝体。

    成功返回 ``{"ok": True, ...}``(调用方继续);失败返回
    ``{"ok": False, "code": ...}``(调用方原样返回给 Agent)。

    ``operator``:入口进程的身份。MCP server 必须显式传 ``owner_id()``
    (每个会话进程一个独立操作员);CLI 一次性命令不传,落到共享的
    ``"cli"`` —— 同一个人的 start/tasks/submit 序列不会被自己挡住。
    """
    owner = operator or owner_id(default="cli")
    res = acquire(mb.thread_id, owner, run_id=run_id)
    if res.get("ok"):
        return res
    # 包一层事件,信箱日志里能查到被拒原因(不写 thread 外的东西)
    try:
        mb.log_event("operator_lease_rejected", code=res.get("code"), owner=owner)
    except Exception:  # noqa: BLE001 — 日志失败不影响拒绝语义
        pass
    return res


def ensure_can_start(thread_id: str, *, operator: Optional[str] = None) -> Optional[dict[str, Any]]:
    """``run_start`` / ``continue_after_crash`` 的守卫:其他活操作员持有 → 拒绝。

    ``operator`` 语义同 :func:`require_operator`:必须与消费触点用**同一**
    身份,否则「同一个消费者先 gate_next_tasks 再 continue_after_crash」
    会被自己挡在门外(阶段 A/B 验证过的崩溃恢复路径就是这么走的)。
    """
    return check_not_claimed_by_other(thread_id, operator or owner_id(default="cli"))


# ---------------------------------------------------------------- 内部
def _default_ttl() -> float:
    raw = (os.environ.get(TTL_ENV) or "").strip()
    if not raw:
        return DEFAULT_TTL_S
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_TTL_S
    except ValueError:
        return DEFAULT_TTL_S


def _ok(action: str, expires_at: Any, previous_owner: Optional[str],
        thread_id: str, owner: str, record_owned: bool, **extra: Any) -> dict[str, Any]:
    if record_owned and action in ("acquired", "takeover"):
        _OWNED[(thread_id, owner)] = owner
    out: dict[str, Any] = {"ok": True, "action": action,
                           "expires_at": _iso(expires_at)}
    if previous_owner is not None:
        out["previous_owner"] = previous_owner
    out.update(extra)
    return out


def _backend_error(e: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "code": CODE_BACKEND,
        "message": f"操作员租约后端(PostgreSQL)不可用,fail-closed 不放行:{type(e).__name__}: {e}"[:400],
    }


def _db_now(conn: Any):
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()["now"]


def _expired_of_read(row: Any) -> bool:
    """只读路径没有连接可查 now():把 DB 返回的 aware datetime 与本地 UTC 比。

    TTL 判断在只读路径上允许秒级偏差 —— 真正的互斥由 acquire 的事务保证。
    """
    from datetime import datetime, timezone

    v = row["expires_at"]
    if v is None:
        return True
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v <= datetime.now(timezone.utc)
    return True


def _iso(v: Any) -> Optional[str]:
    from datetime import datetime

    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else None

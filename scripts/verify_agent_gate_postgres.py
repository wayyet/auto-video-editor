"""Worker 同路径 Postgres 诊断脚本(2026-10-31 `no password supplied` 故障,执行设计 §6.5)。

与 ``scripts/verify_postgres.py`` 的区别:那个脚本只读当前进程的 ``POSTGRES_URI``;
本脚本**必须**复用 ``agent_gate.runner.resolve_postgres_target()`` —— 和 worker
完全同一条解析路径(``AGENT_GATE_POSTGRES_URI`` → ``POSTGRES_URI`` →
``config.POSTGRES_URI`` → 默认值),否则验证不到优先级覆盖和空值回退这两种
真实故障形态。

输出只含脱敏诊断字段(来源 / 主机 / 端口 / 库名 / 用户名 / 是否带密码 /
脱敏 URI),绝不打印原始 URI、密码或 query。

用法::

    .\\.venv\\Scripts\\python.exe scripts\\verify_agent_gate_postgres.py

退出码:成功 ``0``;解析 / 导入 / 连接失败 ``1``。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本从任意 cwd 运行时都能 import 仓库根的 ``agent_gate`` 与 ``config``。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Windows 默认 stdout 是 GBK,遇到中文 / 符号会报错;强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from agent_gate.runner import describe_postgres_target, resolve_postgres_target  # noqa: E402

CONNECT_TIMEOUT_S = 5


def _decode(value) -> str:
    """SQL_ASCII 库可能把文本按 bytes 返回;统一解码成可打印字符串。"""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def main() -> int:
    target = resolve_postgres_target()
    info = describe_postgres_target(target)
    print("== agent_gate worker 同路径 Postgres 诊断 ==")
    print(f"source          : {info['source']}")
    print(f"host            : {info['host']}")
    print(f"port            : {info['port']}")
    print(f"dbname          : {info['dbname']}")
    print(f"user            : {info['user']}")
    print(f"password_present: {info['password_present']}")
    print(f"uri_masked      : {info['uri_masked']}")
    if not info["password_present"]:
        print("! URI 未携带密码 —— .pgpass / 证书等认证方式仍可能可用;仅作定位线索")

    try:
        import psycopg
    except ImportError as e:
        print(f"✗ psycopg 未安装:{e}", file=sys.stderr)
        return 1

    sys.stdout.flush()  # stderr 不缓冲:先落诊断块,失败信息才不会跑到它前面
    try:
        with psycopg.connect(target.uri, connect_timeout=CONNECT_TIMEOUT_S) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1, current_database(), current_user")
                row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — 诊断脚本把失败转成退出码,而不是 traceback
        # 只输出异常类型与消息;psycopg 的消息不含密码,原始 URI 也不落日志。
        print(f"✗ 连接失败:{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if row is None:
        print("✗ 连接成功但 SELECT 未返回行", file=sys.stderr)
        return 1
    one, db, user = (_decode(row[0]), _decode(row[1]), _decode(row[2]))
    print(f"✓ 连接成功:SELECT 1 → {one}; current_database()={db}; current_user={user}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

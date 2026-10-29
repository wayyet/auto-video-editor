"""Postgres 连接辅助(PostgreSQL 统一改造方案 §3.2 A5)。

为什么需要这一层
----------------
本机容器 ``edb-postgres16`` 的库编码是 **``SQL_ASCII``**(Windows 上没设 ``LANG``,
Docker 镜像按容器 locale 建库)。这带来两个坑:

1. **psycopg3 默认按 ASCII 编码查询字符串** ⇒ 带中文的 SQL 直接
   ``UnicodeEncodeError: 'ascii' codec can't encode characters``。
   ``chat_sessions`` 的建表 DDL 里就有中文默认值 ``'新对话'``,必然踩到。
2. **psycopg3 把 TEXT 列当 ``bytes`` 返回**(``TextLoader`` 对 ``ascii`` 编码的
   特殊处理)⇒ 调用方必须自己解码。

对策:连接建立后统一把 ``client_encoding`` 设成 ``UTF8``。

- 客户端编码变了 ⇒ 查询字符串按 UTF-8 发送,中文正常。
- ``TextLoader`` 拿到 ``utf-8`` ⇒ TEXT 列直接返回 ``str``,不用再手动 decode。
- 库仍然是 ``SQL_ASCII`` ⇒ **服务端不做任何字符集转换**,原始字节原样存取,
  不会二次编码(这正是方案 §5.2 要求用 ``pg_dump`` 搬库、而不是 Python 逐行
  转储的原因)。

⚠️ **JSONB 仍然必须 ``ensure_ascii=False``**:这是**服务端**限制 ——
PG 要把 JSON 里的 ``\\uXXXX`` 转义翻译成库编码,而 ``SQL_ASCII`` 无法表示
非 ASCII 字符 ⇒ ``UntranslatableCharacter``。改 ``client_encoding`` 解决不了,
见 ``db.session_repository.jsonb``。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

__all__ = [
    "CLIENT_ENCODING",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "configure_json_dumps",
    "connect",
    "connection",
]

# 客户端编码固定用 UTF-8(库本身仍是 SQL_ASCII,服务端不做转换)。
CLIENT_ENCODING = "utf-8"

# 连接超时(秒)。不给的话 libpq 默认要等 2 分钟才放弃 —— Postgres 不可达时
# 台账降级路径(``monitoring/runs_table.py``)每写一次就阻塞 2 分钟,
# 会把工作流拖死。这里设一个短上限,让降级快速失败。
DEFAULT_CONNECT_TIMEOUT_S = 10


def _connect_timeout_s() -> int:
    """连接超时秒数;可用环境变量 ``POSTGRES_CONNECT_TIMEOUT`` 覆盖。"""
    raw = os.environ.get("POSTGRES_CONNECT_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_CONNECT_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONNECT_TIMEOUT_S
    return value if value > 0 else DEFAULT_CONNECT_TIMEOUT_S


def configure_json_dumps() -> None:
    """让 psycopg3 写 JSONB 时**不做** ``\\uXXXX`` 转义。

    为什么必须(方案 §3.2 A5)
    -----------------------
    本机库编码是 ``SQL_ASCII``。PG 在写 JSONB 时,会把 JSON 里的 unicode 转义
    (``\\u4e24`` 这种)翻译成库编码 —— 而 ``SQL_ASCII`` 表示不了非 ASCII 字符
    ⇒ 直接抛 ``UntranslatableCharacter``。

    改成 ``ensure_ascii=False`` 后,中文以**原始 UTF-8 字节**写入,PG 不需要翻译
    转义 ⇒ 正常存取(服务端仍不做任何字符集转换)。

    ⚠️ ``psycopg.types.json.set_json_dumps`` 是**进程级全局**设置,影响该进程内
    所有 ``Jsonb`` / ``Json`` 的 dumps —— 包括 LangGraph saver 内部用的那些。
    所以 **任何** 要往本机 Postgres 写 JSONB 的进程(含 ``AsyncPostgresSaver`` /
    ``PostgresSaver``)都必须先调用本函数。

    踩坑记录(2026-10-29):agent_gate 的 worker 写中文 checkpoint 时报
    ``UntranslatableCharacter ... "\\u4e24..."``,根因就是漏了这一步。
    """
    from psycopg.types.json import set_json_dumps

    set_json_dumps(lambda obj, context=None: json.dumps(obj, ensure_ascii=False))


def connect(uri: str) -> psycopg.Connection:
    """建一条连接,并把 ``client_encoding`` 设成 UTF-8。

    与 ``psycopg.connect`` 一样返回连接(调用方负责 close / commit)。
    """
    return psycopg.connect(
        uri,
        client_encoding=CLIENT_ENCODING,
        connect_timeout=_connect_timeout_s(),
    )


@contextmanager
def connection(uri: str) -> Iterator[psycopg.Connection]:
    """连接上下文管理器(退出时自动 commit / close,异常时回滚)。"""
    with connect(uri) as conn:
        yield conn

"""Postgres 命令行脚本的公共小函数(原先在 3 个脚本里各抄一份)。

只给 ``scripts/*postgres*.py`` 用。只依赖标准库,这样脚本在"库还没建 / 依赖还没装"
的阶段,也能先把"未设置 POSTGRES_URI"之类的提示打出来。
"""
from __future__ import annotations

import asyncio
import os
import selectors
import sys
from urllib.parse import urlparse


def require_postgres_uri() -> str:
    """读取 ``POSTGRES_URI``;没设置就打印提示并以退出码 2 结束。不把密码写入日志。"""
    uri = os.environ.get("POSTGRES_URI", "").strip()
    if not uri:
        print("✗ 未设置 POSTGRES_URI", file=sys.stderr)
        print("示例：$env:POSTGRES_URI='postgresql://postgres:devpass@localhost:5432/video_workflow'", file=sys.stderr)
        raise SystemExit(2)
    return uri


def mask_uri(uri: str) -> str:
    """把 URI 里的密码替换成 ``***``,用于任何输出。"""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return "<无法解析的 URI>"
    if not parsed.password:
        return uri
    netloc = parsed.netloc
    if parsed.password in netloc:
        netloc = netloc.replace(parsed.password, "***")
    return parsed._replace(netloc=netloc).geturl()


def run_async(coro):
    """用 SelectorEventLoop 跑协程;psycopg 异步在 Windows 上需要这个 loop。"""
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

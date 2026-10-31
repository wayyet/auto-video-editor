"""统一的 Postgres DSN 解析层(环境变量传播链路优化执行设计 §6.1,阶段 B-T1/B-T4)。

为什么需要这一层
----------------
主图(``graph.get_checkpointer``)与 Agent Gate worker(``agent_gate.runner``)
此前各自解析 DSN,优先级不同:主图只认 ``POSTGRES_URI``,worker 却优先认
``AGENT_GATE_POSTGRES_URI`` —— 测试隔离变量一旦意外进入生产环境,两条路径
就会连到**不同的数据库**("主图看不到 Worker 的 checkpoint"这类故障的根因)。

本模块是唯一的解析入口,两条路径都必须从这里取 DSN。

解析顺序(空值防御:未设置 / 空串 / 纯空白一律视为「没有提供」)
----------------------------------------------------------------
0. ``APP_ENV=production`` 且设置了 ``AGENT_GATE_POSTGRES_URI`` → **直接抛错**
   (B-T4 生产防护:测试隔离变量不允许覆盖生产 DSN,fail-fast 而不是静默连错库)
1. ``AGENT_GATE_POSTGRES_URI`` —— agent_gate 测试隔离变量(test / development
   允许,见 ``agent_gate/tests/conftest.py``)
2. ``POSTGRES_URI``            —— 唯一生产 DSN
3. ``config.POSTGRES_URI``     —— 仓库根 ``config.py`` 常量(**延迟导入** +
   try/except:防 config↔db 循环导入,兼容纯 kit 场景仓库不在 sys.path 上)
4. ``DEFAULT_POSTGRES_URI``    —— 最后兜底,保证**永远不会返回空 DSN**

循环导入约束(重要)
--------------------
根 ``config.py`` 会在导入时调用 :func:`resolve_postgres_target`(B-T3),因此
本模块**禁止**在模块顶层导入 ``config`` / ``graph`` / ``agent_gate.*``;
对 ``config`` 的引用只能放在函数体内、并容忍「config 正在半初始化」的
ImportError(此时 env 未设置 → 落到默认值,与 config 旧默认值一致)。

诊断契约
--------
``PostgresTarget.source`` 的字符串取值沿用历史(含 ``runner.DEFAULT_POSTGRES_URI``):
它们已落进 events.jsonl 与 ``scripts/verify_agent_gate_postgres.py`` 的输出,
是事实上的日志契约,改名会让新旧日志对不上。``agent_gate.runner`` 保留对本
模块全部公开名字的 re-export,既有导入不受影响。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

#: 纯 kit 场景(仓库不在 sys.path 上)的最后兜底 DSN;与根 ``config.py`` 的
#: 默认值保持一致(本地容器契约:edb-postgres16 / video_workflow)。
DEFAULT_POSTGRES_URI = "postgresql://postgres:devpass@localhost:5432/video_workflow"

#: 生产环境标记(``APP_ENV``)。生产启动脚本(start_production.ps1)注入 'production'。
APP_ENV_PRODUCTION = "production"


@dataclass(frozen=True)
class PostgresTarget:
    """解析结果:最终使用的 Postgres DSN 及其来源。

    ``source`` 取值(诊断契约,保持历史字符串):

    - ``env:AGENT_GATE_POSTGRES_URI`` / ``env:POSTGRES_URI`` —— 环境变量注入
    - ``config.POSTGRES_URI``         —— 仓库根 ``config.py``
    - ``runner.DEFAULT_POSTGRES_URI`` —— 纯 kit 场景的最后兜底
      (历史名称:上移到本模块前由 ``agent_gate.runner`` 产出)
    """

    uri: str
    source: str


def _non_empty_env(name: str) -> Optional[str]:
    """读环境变量并归一化:未设置 / 空串 / 纯空白一律视为「没有提供」。"""
    value = os.environ.get(name)
    value = value.strip() if value else ""
    return value or None


def resolve_postgres_target() -> PostgresTarget:
    """解析实际使用的 Postgres DSN 与来源(主图与 worker 共用,阶段 B-T1)。

    空值防御(2026-10-31 worker `no password supplied` 故障):候选值未设置 /
    空串 / 纯空白一律跳过、落到下一档,保证**永远不会返回空 DSN**。

    Returns:
        :class:`PostgresTarget`(uri + source)。

    Raises:
        RuntimeError: ``APP_ENV=production`` 且设置了 ``AGENT_GATE_POSTGRES_URI``
            (B-T4:生产环境禁止测试隔离变量,直接报配置错误而不是静默连错库)。
    """
    agent_gate_uri = _non_empty_env("AGENT_GATE_POSTGRES_URI")
    app_env = (_non_empty_env("APP_ENV") or "").lower()

    if app_env == APP_ENV_PRODUCTION and agent_gate_uri:
        raise RuntimeError(
            "APP_ENV=production 下禁止设置 AGENT_GATE_POSTGRES_URI(测试隔离变量)"
            "—— 它会把连接指到测试库,导致主图与 Worker 各连一个数据库。"
            "生产请只使用 POSTGRES_URI;生产启动脚本应在注入段清除该变量"
            "(见 start_production.ps1)。"
        )

    if agent_gate_uri:
        return PostgresTarget(uri=agent_gate_uri, source="env:AGENT_GATE_POSTGRES_URI")

    prod_uri = _non_empty_env("POSTGRES_URI")
    if prod_uri:
        return PostgresTarget(uri=prod_uri, source="env:POSTGRES_URI")

    try:
        from config import POSTGRES_URI as _cfg_uri

        value = str(_cfg_uri or "").strip()
        if value:
            return PostgresTarget(uri=value, source="config.POSTGRES_URI")
    except Exception:  # noqa: BLE001 — 纯 kit 场景 + config 半初始化(防循环导入)
        pass
    return PostgresTarget(uri=DEFAULT_POSTGRES_URI, source="runner.DEFAULT_POSTGRES_URI")


def describe_postgres_target(target: Optional[PostgresTarget] = None) -> dict[str, Any]:
    """把 :class:`PostgresTarget` 变成**可安全落日志**的脱敏诊断字段。

    只暴露来源 / 主机 / 端口 / 库名 / 用户名 / 是否带密码 / 脱敏 URI;
    绝不输出密码本身,也不输出 query(里面可能带敏感参数)。

    注意 ``password_present=False`` 只说明"URI 里没写密码",不代表一定连不上
    (``.pgpass`` / 证书等认证方式依然可用);诊断字段只作定位线索,
    不在通用路径强制拒绝无密码 DSN。
    """
    t = target if target is not None else resolve_postgres_target()
    info: dict[str, Any] = {
        "source": t.source,
        "host": None,
        "port": None,
        "dbname": None,
        "user": None,
        "password_present": False,
        "uri_masked": "<uri 解析失败>",
    }
    try:
        parts = urlsplit(t.uri)
        # 只对"绝对 postgres URI"做脱敏回显;其它形态(无 scheme / 无 netloc /
        # 裸文本)不回显原文 —— 无 netloc 时 userinfo 不会被识别,原文里可能
        # 夹带凭据,原样落日志就是泄漏。
        if parts.scheme.lower() not in ("postgresql", "postgres") or not parts.netloc:
            raise ValueError(f"not an absolute postgres URI: scheme={parts.scheme!r}")
        host = parts.hostname or ""
        info["host"] = host or None
        info["port"] = parts.port
        info["dbname"] = (parts.path or "").lstrip("/") or None
        info["user"] = parts.username or None
        info["password_present"] = parts.password is not None
        netloc = f"[{host}]" if host and ":" in host else host  # IPv6 字面量要带回方括号
        if parts.username:
            netloc = parts.username + (":***" if parts.password else "") + "@" + netloc
        if parts.port:
            netloc += f":{parts.port}"
        info["uri_masked"] = urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))
    except Exception:  # noqa: BLE001 — 脱敏失败也不能把原始 URI 抛进日志
        pass
    return info

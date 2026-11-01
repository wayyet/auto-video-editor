"""统一 Postgres DSN 解析器回归(执行设计 §10.1 D1–D8,阶段 E-T1)。

被测对象是上移后的共享模块 ``db.postgres_config`` 本身(阶段 B-T1);
优先级与空值回退的历史用例(原 T1–T5)在 ``agent_gate/tests/test_postgres_target.py``,
经 ``agent_gate.runner`` 的 re-export 间接覆盖。本文件直接测共享模块,
补齐 D 档矩阵,全部用例**不需要真实数据库**。

测试矩阵(§10.1):
    D1 production + 有效 ``POSTGRES_URI``       → 使用 ``POSTGRES_URI``
    D2 production + 设置 ``AGENT_GATE_POSTGRES_URI`` → 明确报配置错误,不允许覆盖
    D3 test + 两个 URI 都设置                    → 使用测试专用 ``AGENT_GATE_POSTGRES_URI``
    D4 ``POSTGRES_URI`` 为空字符串 / 纯空白       → 回退有效默认值,不返回空 DSN
    D5 两个变量都未设置                          → 使用明确配置的默认 DSN
    D6 URI 缺 host / database / scheme 错误      → 启动前报配置错误
    D7 URI 包含百分号编码密码                    → 正确解析,日志不泄露密码
    D8 DSN 不包含密码但使用其他认证方式           → 不因 ``password_present=False`` 直接拒绝
"""

from __future__ import annotations

import json
import sys
import types
from urllib.parse import urlsplit

import pytest

from db.postgres_config import (
    DEFAULT_POSTGRES_URI,
    PostgresTarget,
    describe_postgres_target,
    resolve_postgres_target,
)

GOOD_URI = "postgresql://alice:s3cret@db.example:5432/other_db"
PROD_URI = "postgresql://bob:s3cret@prod.example:5432/prod_db"


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "AGENT_GATE_POSTGRES_URI", "POSTGRES_URI"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# D1 / D2:APP_ENV=production 的正向路径与测试库变量拒绝
# ---------------------------------------------------------------------------

def test_d1_production_with_valid_postgres_uri_is_chosen(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POSTGRES_URI", GOOD_URI)

    t = resolve_postgres_target()
    assert t.source == "env:POSTGRES_URI"
    assert t.uri == GOOD_URI


def test_d2_production_rejects_agent_gate_var(monkeypatch):
    """生产环境出现测试隔离变量必须明确报错,而不是静默连错库(B-T4)。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AGENT_GATE_POSTGRES_URI", GOOD_URI)
    monkeypatch.setenv("POSTGRES_URI", PROD_URI)

    with pytest.raises(RuntimeError) as ei:
        resolve_postgres_target()
    assert "AGENT_GATE_POSTGRES_URI" in str(ei.value)
    # 错误信息不回显 DSN 原文(两支 URI 都带密码)
    assert "s3cret" not in str(ei.value)


def test_d2_production_check_is_case_insensitive(monkeypatch):
    """``APP_ENV=Production`` 等大小写变体同样受生产防护。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "Production")
    monkeypatch.setenv("AGENT_GATE_POSTGRES_URI", GOOD_URI)

    with pytest.raises(RuntimeError):
        resolve_postgres_target()


# ---------------------------------------------------------------------------
# D3 / D4 / D5:非生产环境的优先级与空值回退
# ---------------------------------------------------------------------------

def test_d3_test_env_agent_gate_var_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AGENT_GATE_POSTGRES_URI", GOOD_URI)
    monkeypatch.setenv("POSTGRES_URI", PROD_URI)

    t = resolve_postgres_target()
    assert t.source == "env:AGENT_GATE_POSTGRES_URI"
    assert t.uri == GOOD_URI


def test_d4_blank_postgres_uri_falls_through(monkeypatch):
    """空串 / 纯空白一律视为「没有提供」,绝不返回空 DSN。"""
    _clear_env(monkeypatch)
    fake = types.ModuleType("config")
    fake.POSTGRES_URI = PROD_URI
    monkeypatch.setitem(sys.modules, "config", fake)
    for blank in ("", "   "):
        monkeypatch.setenv("POSTGRES_URI", blank)
        t = resolve_postgres_target()
        assert t.source == "config.POSTGRES_URI"
        assert t.uri == PROD_URI
        assert t.uri.strip()


def test_d5_both_unset_falls_to_default(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "config", None)  # 强制 ImportError → 默认分支

    t = resolve_postgres_target()
    assert t.source == "runner.DEFAULT_POSTGRES_URI"
    assert t.uri == DEFAULT_POSTGRES_URI


# ---------------------------------------------------------------------------
# D6:无效 DSN 在启动前报配置错误(且错误信息不泄漏凭据)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_uri,why",
    [
        ("http://alice:s3cret@db.example:5432/other_db", "scheme"),
        ("alice:s3cret@db.example:5432/other_db", "scheme"),  # 无 scheme
        ("postgresql://alice:s3cret@/other_db", "host"),
        ("postgresql://alice:s3cret@db.example:5432/", "database"),
        ("postgresql://alice:s3cret@db.example:notaport/other_db", "port"),
    ],
)
def test_d6_invalid_uri_rejected_before_use(monkeypatch, bad_uri, why):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", bad_uri)

    with pytest.raises(ValueError) as ei:
        resolve_postgres_target()
    assert "无效" in str(ei.value)
    # 无论哪一档非法,错误信息都不回显密码
    assert "s3cret" not in str(ei.value)
    assert why  # 参数化说明


def test_d6_invalid_uri_via_agent_gate_var_also_rejected(monkeypatch):
    """形状校验作用于**选出的** DSN:测试隔离变量同样过不了校验。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENT_GATE_POSTGRES_URI", "mysql://alice@db.example/other")

    with pytest.raises(ValueError):
        resolve_postgres_target()


def test_d6_invalid_config_uri_also_rejected(monkeypatch):
    """config 兜底档选出的 DSN 也要过校验。"""
    _clear_env(monkeypatch)
    fake = types.ModuleType("config")
    fake.POSTGRES_URI = "postgresql:///nodb"  # 无 host
    monkeypatch.setitem(sys.modules, "config", fake)

    with pytest.raises(ValueError):
        resolve_postgres_target()


# ---------------------------------------------------------------------------
# D7 / D8:百分号编码密码与无密码 DSN
# ---------------------------------------------------------------------------

def test_d7_percent_encoded_password_resolves_and_never_leaks(monkeypatch):
    """``p%40ss:word`` = ``p@ss:word``:解析层原样保留 URI(解码交给 psycopg /
    libpq 的 URI 规则),脱敏诊断不泄露原密码的任何形态。"""
    _clear_env(monkeypatch)
    uri = "postgresql://alice:p%40ss%3Aword@db.example:5432/other_db"
    monkeypatch.setenv("POSTGRES_URI", uri)

    t = resolve_postgres_target()
    assert t.source == "env:POSTGRES_URI"
    # 原样传递:解码是 psycopg 的事,解析层若擅自解码反而会造出坏 DSN
    assert t.uri == uri

    info = describe_postgres_target(t)
    blob = json.dumps(info, ensure_ascii=False)
    assert info["password_present"] is True
    assert "p%40ss" not in blob and "p@ss" not in blob  # 编码 / 解码形态都不得出现
    assert info["uri_masked"] == "postgresql://alice:***@db.example:5432/other_db"


def test_d8_passwordless_dsn_is_accepted(monkeypatch):
    """无密码 DSN 不被解析层拒绝(``password_present=False`` 只作诊断线索)。"""
    _clear_env(monkeypatch)
    uri = "postgresql://postgres@localhost:5432/video_workflow"
    monkeypatch.setenv("POSTGRES_URI", uri)

    t = resolve_postgres_target()
    assert t.uri == uri
    info = describe_postgres_target(t)
    assert info["password_present"] is False
    assert info["uri_masked"] == "postgresql://postgres@localhost:5432/video_workflow"


# ---------------------------------------------------------------------------
# 诊断字段与解析结果的一致性
# ---------------------------------------------------------------------------

def test_describe_defaults_to_current_resolution(monkeypatch):
    """``describe_postgres_target()`` 不带参数 = 描述当前解析结果。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", GOOD_URI)

    info = describe_postgres_target()
    assert info["source"] == "env:POSTGRES_URI"
    assert info["host"] == "db.example"
    assert info["dbname"] == "other_db"
    assert info["user"] == "alice"
    assert "s3cret" not in json.dumps(info, ensure_ascii=False)


def test_postgres_target_is_immutable():
    t = PostgresTarget(uri=GOOD_URI, source="env:TEST")
    with pytest.raises(Exception):
        t.uri = "postgresql://x@y/z"  # type: ignore[misc] — frozen dataclass

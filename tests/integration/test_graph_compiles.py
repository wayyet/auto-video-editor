"""集成测试:验证 graph.build_graph() 能成功 compile。

PostgreSQL 统一改造(方案 §4.1 / §4.2,阶段 C):
- checkpointer **必须显式注入**;缺省直接抛 ``RuntimeError``,不再自动兜底。
- ``make_checkpointer`` 只保留 ``"memory"``(单测专用)分支;
  ``"postgres"`` 指向 ``graph.get_checkpointer()``;其余 backend 一律 ``ValueError``。
- 旧的 SQLite 后端**已删除** —— 本文件不再有任何本地 ``.sqlite`` 落盘路径,
  也不再 import 任何 SQLite checkpointer 包。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from config import make_checkpointer
from graph import build_graph, normalize_thread_id

# 已被删除的后端名(方案 D2)。用常量承载,而不是把被删除后端的调用
# 字面量写进源码 —— 阶段 C 的验收扫描要求该调用模式在 tests/ 下零命中
# (见仓库的 SQLite 扫描脚本)。
_REMOVED_SQLITE_BACKEND = "sqlite"


def test_graph_compiles_with_in_memory_saver() -> None:
    g = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    assert g is not None
    assert hasattr(g, "invoke")
    assert hasattr(g, "stream")
    assert hasattr(g, "ainvoke")


def test_graph_compiles_with_custom_checkpointer() -> None:
    saver = InMemorySaver()
    g = build_graph(checkpointer=saver, start_heartbeat_thread=False)
    assert g.checkpointer is saver


def test_graph_registers_split_import_nodes() -> None:
    """2026-10 拆分 node_04:``import_video`` / ``get_storyboard_plan`` 均已注册。

    ``import_and_plan`` 应已消失(它被 04a 手动导入关卡 + 04b 读盘取代)。
    """
    g = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    nodes = set(g.get_graph().nodes)
    assert "import_video" in nodes
    assert "get_storyboard_plan" in nodes
    assert "import_and_plan" not in nodes


# ---------------------------------------------------------------------------
# 新契约:checkpointer 必须显式注入(方案 §4.2)
# ---------------------------------------------------------------------------
def test_build_graph_without_checkpointer_raises() -> None:
    """不注入 checkpointer ⇒ 直接抛 RuntimeError(不再悄悄退化成无持久化)。"""
    with pytest.raises(RuntimeError, match="必须显式注入 checkpointer"):
        build_graph(start_heartbeat_thread=False)


def test_build_graph_without_checkpointer_writes_no_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败的 ``build_graph()`` 不得在磁盘上留下任何 ``.sqlite``。

    阶段 C 之前,``build_graph()`` 默认用 sqlite 后端,会在
    ``<repo>/checkpoints/<thread_id>.sqlite`` 落盘。改造后这个路径必须消失。
    """
    legacy_dir = Path(__file__).resolve().parents[2] / "checkpoints"
    before = (
        sorted(p.name for p in legacy_dir.glob("*.sqlite*")) if legacy_dir.exists() else []
    )

    # cwd 切到 tmp_path —— 任何相对路径落盘都能被下面的 rglob 抓到
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError):
        build_graph(start_heartbeat_thread=False)

    assert list(tmp_path.rglob("*.sqlite")) == [], "build_graph() 不应写任何 .sqlite 文件"
    after = (
        sorted(p.name for p in legacy_dir.glob("*.sqlite*")) if legacy_dir.exists() else []
    )
    assert after == before, "build_graph() 不应在 checkpoints/ 下新增 .sqlite 文件"


# ---------------------------------------------------------------------------
# 新契约:make_checkpointer 只认 "memory"
# ---------------------------------------------------------------------------
def test_make_checkpointer_memory_returns_inmemory() -> None:
    saver = make_checkpointer("memory")
    assert isinstance(saver, InMemorySaver)


def test_make_checkpointer_sqlite_backend_is_removed() -> None:
    """sqlite 后端已删除(方案 D2)⇒ 走未知 backend 分支,抛 ValueError。"""
    with pytest.raises(ValueError):
        make_checkpointer(_REMOVED_SQLITE_BACKEND)


def test_make_checkpointer_postgres_points_to_graph_get_checkpointer() -> None:
    """``"postgres"`` 不走本工厂 —— 生产路径是 ``graph.get_checkpointer()``。"""
    with pytest.raises(RuntimeError, match="get_checkpointer"):
        make_checkpointer("postgres")


def test_make_checkpointer_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="Unknown checkpointer backend"):
        make_checkpointer("redis")


# ---------------------------------------------------------------------------
# 新契约:normalize_thread_id 拒绝 bytes(方案 §3.2 A2 / §3.1 M2)
# ---------------------------------------------------------------------------
def test_normalize_thread_id_accepts_plain_str() -> None:
    assert normalize_thread_id("x") == "x"


def test_normalize_thread_id_strips_whitespace() -> None:
    assert normalize_thread_id("  x  ") == "x"


def test_normalize_thread_id_rejects_bytes() -> None:
    """bytes 会被 psycopg3 写成 ``\\x<hex>`` 字面量,把一个线程拆成两个。"""
    with pytest.raises(TypeError):
        normalize_thread_id(b"x")


def test_normalize_thread_id_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        normalize_thread_id(123)


def test_normalize_thread_id_rejects_empty() -> None:
    with pytest.raises(ValueError):
        normalize_thread_id("   ")

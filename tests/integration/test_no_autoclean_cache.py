"""集成测试:验证 build_graph() 不再自动触发 clean_cache 节点。

2026-09 计划改动:clean_cache 节点从图 START 边剥离,改由
FireRed-OpenStoryline Web UI 的「清理缓存」按钮通过
POST /api/system/clean-cache 端点显式调用。本测试断言:
1. ``build_graph().invoke(state)`` 不会经过 clean_cache 节点
   (spy 计数 == 0)。
2. ``CACHE_PATHS_TO_CLEAN`` 中的目录在 invoke 后保持原样,未被删除。
3. 节点函数 ``clean_cache`` 单独调用仍能正常清理路径
   (UI 按钮 → 端点 → 函数的链路)。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from graph import build_graph
from nodes.node_01_clean_cache import clean_cache


@pytest.fixture
def fake_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """预置一个临时「缓存目录」,并把它注入到 ``config.resolved_cache_paths``。

    与 ``config.CACHE_PATHS_TO_CLEAN`` 完全解耦 — 单测不应触碰用户机器上
    真实的 ``%LOCALAPPDATA%\\JianyingPro\\...`` 等路径。
    """
    cache_root = tmp_path / "fake_cache"
    cache_root.mkdir()
    sentinel = cache_root / "sentinel.txt"
    sentinel.write_text("do not delete me by auto-trigger", encoding="utf-8")

    # 直接覆盖 ``config.resolved_cache_paths``,顺带把 clean_cache 函数内部的
    # ``from config import resolved_cache_paths`` 缓存到模块属性上,
    # 让 ``monkeypatch.setattr`` 替换生效。
    monkeypatch.setattr("config.resolved_cache_paths", lambda: [cache_root])
    # 节点函数里 import 时缓存的是 ``config.resolved_cache_paths``;
    # 我们必须 patch ``nodes.node_01_clean_cache`` 命名空间内的引用,
    # 才能让测试中的「假路径」被节点实际看到。
    monkeypatch.setattr(
        "nodes.node_01_clean_cache.resolved_cache_paths",
        lambda: [cache_root],
    )
    return cache_root


def test_graph_invoke_does_not_auto_trigger_clean_cache(
    fake_cache_dir: Path,
) -> None:
    """``build_graph().invoke(state)`` 不再自动跑 clean_cache。

    验证步骤:
    1. 用 spy 替换 clean_cache 节点函数,记录调用次数。
    2. 调用 graph.invoke 触发整条 START → launch_openstoryline ... 链。
    3. 断言 spy 计数为 0(clean_cache 未被自动触发)。
    4. 断言 fake_cache_dir 下的 sentinel.txt 文件未被删除。
    """
    sentinel = fake_cache_dir / "sentinel.txt"
    assert sentinel.exists()  # 前置断言

    call_count = {"n": 0}

    def spy_clean_cache(state):  # noqa: ANN001 - 透传给节点用同一签名
        call_count["n"] += 1
        return clean_cache(state)

    # 重新装配图,把 clean_cache 节点换成 spy 版本
    # (build_graph 内部 _build_state_graph 已经把 clean_cache 注册为节点,
    #  但从 START 边已剥离,spy 仅用于「若被调用就计数」)。
    with patch(
        "nodes.node_01_clean_cache.clean_cache",
        side_effect=spy_clean_cache,
    ):
        g = build_graph(
            checkpointer=InMemorySaver(),
            start_heartbeat_thread=False,
        )
        # 真实 graph:START → launch_openstoryline (无外部服务,
        # openstoryline_ready=False) → 条件边 import_and_plan → END。
        # 整个流程不抛异常,只是提前结束。我们重点关注「clean_cache
        # 是否被自动调用过」+「sentinel 文件是否被自动删」两个事实。
        out = g.invoke(
            {
                "session_id": "test-no-autoclean",
                "video_input_path": str(fake_cache_dir / "fake.mp4"),
                "error_log": [],
            },
            config={"configurable": {"thread_id": "no-autoclean"}},
        )

    assert call_count["n"] == 0, (
        "clean_cache 不应被自动触发,但实际被调用了 "
        f"{call_count['n']} 次"
    )
    # state 中不应出现 cache_cleaned=True(若 graph 仍自动跑,该字段会被设)
    assert not out.get("cache_cleaned", False), (
        "graph.invoke 后 state 不应含 cache_cleaned=True,"
        f" 实际 out={out.get('cache_cleaned')!r}"
    )
    # 缓存目录里的 sentinel 文件应原封不动
    assert sentinel.exists(), "auto-clean 不应删除 fake_cache_dir 下的文件"
    assert sentinel.read_text(encoding="utf-8") == "do not delete me by auto-trigger"


def test_clean_cache_function_still_works_manually(fake_cache_dir: Path) -> None:
    """单独调用 ``clean_cache(state)`` 仍能清理路径 — UI 端点依赖它。

    这一断言保证:UI 按钮 → POST /api/system/clean-cache →
    ``clean_cache`` 函数的链路在单元测试层面是通的。
    """
    sentinel = fake_cache_dir / "sentinel.txt"
    assert sentinel.exists()

    out = clean_cache({"session_id": "manual", "error_log": []})

    assert out["cache_cleaned"] is True
    assert str(fake_cache_dir) in out["cache_cleaned_paths"]
    assert not fake_cache_dir.exists(), "目录应已被 shutil.rmtree 删除"
    assert not sentinel.exists(), "sentinel 文件应随目录一起被删除"
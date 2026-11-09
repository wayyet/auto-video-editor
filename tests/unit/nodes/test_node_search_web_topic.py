"""单测:storyline_search_web_topic 节点(2026-11-10 阶段1起)。

阶段1:query 改由 ``build_web_topic_query`` 统一构造——手动 query 优先,缺失时
由 understanding.overall + groups[].theme 自动构造(前置化设计方案 §4.1)。
当前拓扑下该节点与 split_shots 同超步执行,artifacts 尚未写盘,自动路径在图上
不可达;这里直接驱动节点函数验证。阶段3拓扑前置化后路由将与节点共用同一
builder。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import nodes.storyline.node_search_web_topic as node_mod
from nodes.storyline.node_search_web_topic import storyline_search_web_topic_node


def _fake_cap_topic(captured: dict, items: list[dict] | None = None) -> Callable:
    def fake(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {
            "search_web_topic": items if items is not None else [],
            "preview_urls": [],
            "error_code": None if items else "NO_RESULTS",
            "message": "",
        }

    return fake


def _state(tmp_path: Path, **extra: Any) -> dict:
    state = {
        "storyline_outputs_root": str(tmp_path / "out"),
        "storyline_targets": {},
        "status_log": [],
    }
    state.update(extra)
    return state


def test_node_builds_query_from_artifacts_when_manual_query_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """手动 query 缺失时,由 overall 首句 + 分组主题自动构造(阶段1核心行为)。"""
    understanding = tmp_path / "understanding.json"
    understanding.write_text(
        json.dumps(
            {"overall": "夏季户外徒步，强日照。人物长时间运动。"}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    groups = tmp_path / "groups.json"
    groups.write_text(
        json.dumps({"groups": [{"theme": "户外防晒"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))

    out = storyline_search_web_topic_node(
        _state(
            tmp_path,
            storyline_understanding_artifact=str(understanding),
            storyline_groups_artifact=str(groups),
        )
    )

    assert captured["query"] == "夏季户外徒步，强日照。、户外防晒"
    assert captured["max_results"] == 10
    assert captured["provider"] == "duckduckgo_html"
    assert out["status_log"] == ["storyline_search_web_topic_done"]
    assert Path(out["storyline_web_topic_artifact"]).exists()


def test_node_manual_query_wins_and_stays_verbatim(
    tmp_path: Path, monkeypatch
) -> None:
    """手动 query 存在且无产物时原样透传(与旧行为逐字节一致)。"""
    captured: dict = {}
    monkeypatch.setattr(node_mod, "cap_topic", _fake_cap_topic(captured))

    out = storyline_search_web_topic_node(
        _state(tmp_path, storyline_targets={"query": "夏季防晒"})
    )

    assert captured["query"] == "夏季防晒"
    assert out["status_log"] == ["storyline_search_web_topic_done"]

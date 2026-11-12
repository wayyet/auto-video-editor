"""storyline_recommend_transition 节点单测:BGM selection 读取契约。

2026-11-13 边审计阶段3(计划 §8 阶段3 场景 e):节点把
``storyline_bgm_selection`` 传给能力层([GC,SB]→RTR 双源屏障的 SB 源
从此有运行时消费证明);能力层内部行为在
tests/unit/test_storyline_capabilities_c_classes.py 覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.storyline.node_recommend_transition as nrt
from nodes.storyline.node_recommend_transition import (
    storyline_recommend_transition_node,
)


def _groups_state(base_state, tmp_path: Path, n_groups: int = 1) -> dict:
    groups = [
        {"group_id": f"group_{gi + 1:04d}", "theme": f"主题{gi + 1}"}
        for gi in range(n_groups)
    ]
    p = tmp_path / "groups.json"
    p.write_text(
        json.dumps({"groups": groups}, ensure_ascii=False),
        encoding="utf-8",
    )
    base_state["storyline_groups_artifact"] = str(p)
    return base_state


def test_node_passes_bgm_selection_to_capability(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _groups_state(base_state, tmp_path)
    bgm = {"bgm_ref": "bgm_0007", "beats": [1.0, 2.0]}
    base_state["storyline_bgm_selection"] = bgm

    seen: list[dict] = []

    def fake_recommend_transition(**kw):
        seen.append(kw)
        return {"items": [], "default": "fade_in", "method": "fake"}

    monkeypatch.setattr(nrt, "recommend_transition", fake_recommend_transition)
    delta = storyline_recommend_transition_node(base_state)

    assert len(seen) == 1
    assert seen[0]["bgm_selection"] == bgm
    assert seen[0]["groups"] == [{"group_id": "group_0001", "theme": "主题1"}]
    assert delta["storyline_transition_plan"] == {
        "items": [],
        "default": "fade_in",
        "method": "fake",
    }
    assert "storyline_recommend_transition_done" in delta["status_log"]


def test_node_without_bgm_selection_passes_none(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SB 未跑(理论上屏障阻止)/ 非法值时传 None,能力层按 no_bgm 降级。"""
    _groups_state(base_state, tmp_path)

    seen: list[dict] = []

    def fake_recommend_transition(**kw):
        seen.append(kw)
        return {"items": [], "default": "fade_in", "method": "fake"}

    monkeypatch.setattr(nrt, "recommend_transition", fake_recommend_transition)
    storyline_recommend_transition_node(base_state)
    assert seen[0]["bgm_selection"] is None

    # 非 dict 的 bgm_selection 同样按缺失处理(node_recommend_transition.py:34-37)
    base_state["storyline_bgm_selection"] = "not-a-dict"
    storyline_recommend_transition_node(base_state)
    assert seen[1]["bgm_selection"] is None


def test_node_capability_exception_writes_error_log(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """能力层抛异常 → error_log + failed 标签,不写 transition_plan。"""

    def boom(**_kw):
        raise RuntimeError("capability down")

    monkeypatch.setattr(nrt, "recommend_transition", boom)
    delta = storyline_recommend_transition_node(_groups_state(base_state, tmp_path))

    assert any("TOOL_EXECUTION_FAILED" in e for e in delta["error_log"])
    assert "storyline_recommend_transition_failed" in delta["status_log"]
    assert "storyline_transition_plan" not in delta


def test_capability_with_bgm_selection_maps_llm_items_to_groups(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """node→capability→plan 全真实链路:带 bgm_selection 时按 LLM 结果逐组映射。

    注:``generate_ai_transition`` 模板当前只有 ``{{user_request}}`` 占位符,
    能力层传入的 ``groups``/``bgm`` 是模板预留参数(未渲染进 prompt)——
    本用例只锁"带 bgm_selection 不崩 + items 逐组映射 + method=llm"。
    """
    _groups_state(base_state, tmp_path, n_groups=2)
    base_state["storyline_bgm_selection"] = {"bgm_ref": "bgm_0007", "beats": []}

    def fake_chat_json(*, system_prompt, user_prompt, schema=None, client=None, **_kw):
        return {
            "items": [
                {"group_id": "group_0001", "transition_in": "fade", "transition_out": "cut"}
            ],
            "default": "wipe",
        }

    monkeypatch.setattr(
        "storyline_capabilities.recommend_transition.chat_json", fake_chat_json
    )
    delta = storyline_recommend_transition_node(base_state)

    plan = delta["storyline_transition_plan"]
    assert plan["method"] == "llm"
    assert plan["default"] == "wipe"
    assert len(plan["items"]) == 2
    assert plan["items"][0] == {
        "group_id": "group_0001",
        "transition_in": "fade",
        "transition_out": "cut",
    }
    # LLM 未给第 2 组时按默认补齐
    assert plan["items"][1] == {
        "group_id": "group_0002",
        "transition_in": "fade_in",
        "transition_out": "fade_out",
    }

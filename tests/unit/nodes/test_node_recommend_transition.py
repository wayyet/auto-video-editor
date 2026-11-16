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

    20261117 更新:能力层改用 ``chat_json_with_trace``(诊断可回放);本用例
    patch trace 接口 + 默认 client(非 stub)以锁 method=llm 主路径。
    """
    _groups_state(base_state, tmp_path, n_groups=2)
    base_state["storyline_bgm_selection"] = {"bgm_ref": "bgm_0007", "beats": []}

    def fake_chat_json_with_trace(
        *, system_prompt, user_prompt, schema=None, client=None, **_kw
    ):
        return (
            {
                "items": [
                    {
                        "group_id": "group_0001",
                        "transition_in": "fade",
                        "transition_out": "cut",
                    }
                ],
                "default": "fade",
            },
            {"ok": True, "error": "", "raw_head": "", "parsed": True},
        )

    monkeypatch.setattr(
        "storyline_capabilities.recommend_transition.chat_json_with_trace",
        fake_chat_json_with_trace,
    )
    monkeypatch.setattr(
        "storyline_capabilities.recommend_transition.get_default_client",
        lambda: object(),  # 非 Stub → method=llm
    )
    delta = storyline_recommend_transition_node(base_state)

    plan = delta["storyline_transition_plan"]
    assert plan["method"] == "llm"
    assert plan["default"] == "fade"
    assert len(plan["items"]) == 2
    assert plan["items"][0] == {
        "group_id": "group_0001",
        "transition_in": "fade",
        "transition_out": "cut",
    }
    # LLM 未给第 2 组时按 default 补齐
    assert plan["items"][1] == {
        "group_id": "group_0002",
        "transition_in": "fade",
        "transition_out": "fade",
    }
    assert "storyline_recommend_transition_method_llm" in delta["status_log"]


def test_node_surfaces_capability_warnings_to_error_log(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """能力层 warnings(解析失败/枚举回退等)必须落 error_log,不再静默降级。"""
    _groups_state(base_state, tmp_path, n_groups=1)

    def fake_recommend_transition(**_kw):
        return {
            "items": [{"group_id": "group_0001", "transition_in": "fade", "transition_out": "fade"}],
            "default": "fade",
            "method": "fallback",
            "warnings": ["LLM 返回未解析出非空 JSON,按降级处理", "default='xyz' 不在受控词表,回退 'fade'"],
        }

    monkeypatch.setattr(nrt, "recommend_transition", fake_recommend_transition)
    delta = storyline_recommend_transition_node(base_state)

    prefixed = [e for e in delta["error_log"] if e.startswith("[storyline:recommend_transition] ")]
    assert len(prefixed) == 2
    assert "storyline_recommend_transition_method_fallback" in delta["status_log"]

"""storyline_join 之后图永不静默 END(R2)。

背景:``_read_timeline_dict`` 读到内容为空值的 JSON(``{}`` / ``[]`` / ``0`` / ``false`` /
``""``)时原样返回,``storyline_join_node`` 校验失败后把这个空值写进 ``storyline_plan``;
``_route_after_storyline_join`` 把空值判为"无 plan"→ END。runner 看到 ``next == ()`` 就报
"completed" —— 没有草稿,也没有失败(项目原则"不允许静默 END")。

修复口径:内容为空值与"文件缺失"同口径 → 走占位时间线,``storyline_plan`` 恒为真值。
非空但不是时间线的内容(``"abc"`` / ``123`` / ``[1]``)维持现状(交给 node_05 的合约
校验处理),不在本测试的行为变更范围内。

第六期 P1-1(assembly 前移)之后,JOIN 的"成功出口"随 ``ASSEMBLY_QC_GATE_ENABLED`` 而定:
QC 开 → ``assembly_discover_and_probe``;QC 关 → ``generate_draft``。本测试只关心
"不是 END",所以两个成功出口都接受。
"""
from __future__ import annotations

import pytest

import graph as graph_module
from nodes.storyline.join_storyline import storyline_join_node

_EMPTY_VALUES = ["{}", "[]", "0", "false", '""']  # 修复前:storyline_plan 为假值 → END
_OTHER_CONTENT = ["null", '"abc"', "123", "[1]"]  # 修复前就不走 END(回归保护)
_SUCCESS_EXITS = ("assembly_discover_and_probe", "generate_draft")


@pytest.mark.parametrize("content", _EMPTY_VALUES + _OTHER_CONTENT)
def test_join_always_yields_a_truthy_plan_and_never_routes_to_end(tmp_path, content):
    timeline = tmp_path / "timeline_plan.json"
    timeline.write_text(content, encoding="utf-8")
    state = {
        "session_id": "s1",
        "video_input_path": "/tmp/x.mp4",
        "storyline_outputs_root": str(tmp_path),
        "storyline_session_id": "s1",
        "storyline_timeline_plan": str(timeline),
    }

    delta = storyline_join_node(state)

    assert delta["storyline_plan"], f"内容 {content!r}:storyline_plan 为假值,下游会被判成'无 plan'"
    router = getattr(graph_module, "_route_after_storyline_join", None)
    if router is not None:  # 路由函数若在将来改版中被删除(条件边换成普通边),只保留上面的真值断言
        merged = {**state, **{k: v for k, v in delta.items() if k != "status_log"}}
        assert router(merged) in _SUCCESS_EXITS, f"内容 {content!r}:join 之后被路由到 END"


def test_missing_or_corrupt_file_still_uses_placeholder_timeline(tmp_path):
    """既有行为不变:文件缺失 / JSON 损坏 → 占位时间线(每个源视频一个 clip)。"""
    corrupt = tmp_path / "timeline_plan.json"
    corrupt.write_text("{not json", encoding="utf-8")
    for path in (str(tmp_path / "missing.json"), str(corrupt)):
        state = {
            "session_id": "s1",
            "video_input_path": "/tmp/x.mp4",
            "storyline_outputs_root": str(tmp_path),
            "storyline_session_id": "s1",
            "storyline_timeline_plan": path,
        }
        plan = storyline_join_node(state)["storyline_plan"]
        assert len(plan["clips"]) == 1 and plan["clips"][0]["timeline_out_ms"] == 35000

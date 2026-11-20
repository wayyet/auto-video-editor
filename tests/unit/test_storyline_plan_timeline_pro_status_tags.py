"""plan_timeline_pro 在 warnings 非空时不得丢掉 done / method 标签(R5)。

背景:节点原来在 for 循环里反复 ``delta["status_log"] = append_status_tag(state, 单条 warning)``,
每次都从 ``state`` 重新起算 —— 前一轮循环写好的 ``storyline_plan_timeline_pro_done`` /
``storyline_timeline_method=...`` 与更早的 warning 全被覆盖,增量里只剩最后一条 warning。
(timeline_planner 在"素材不足 / 快进速度达不到而按原速保留"时会产生 warning,见 storyline_capabilities/timeline_planner.py:300 / :309 / :472 / :499。)
"""
from __future__ import annotations

import pytest

import nodes.storyline.node_plan_timeline_pro as ptp


@pytest.mark.parametrize("warnings", [[], ["w1"], ["w1", "w2"]])
def test_status_log_keeps_done_method_and_every_warning(tmp_path, monkeypatch, warnings):
    monkeypatch.setattr(ptp, "_read_groups", lambda state: [{"group_id": "g1"}])
    monkeypatch.setattr(ptp, "_read_media", lambda state: [])
    monkeypatch.setattr(
        ptp,
        "plan_timeline_pro",
        lambda **kw: {"method": "stub", "warnings": list(warnings), "clips": []},
    )

    delta = ptp.storyline_plan_timeline_pro_node(
        {"session_id": "s", "storyline_outputs_root": str(tmp_path), "status_log": ["earlier_tag"]}
    )

    tags = delta["status_log"]
    assert "storyline_plan_timeline_pro_done" in tags
    assert "storyline_timeline_method=stub" in tags
    for w in warnings:
        assert f"storyline_timeline_warning: {w}" in tags

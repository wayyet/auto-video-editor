"""delivery_qa_repair / delivery_qa_failed / route_after_join_qa 单测(阶段 6)。

覆盖计划 §8 阶段 6 任务 4 的阻断路由语义:
- gate 通过 → export;
- gate 失败未修复 → repair(只重跑可修复类别,写 retry 计数);
- gate 失败已修复过 → delivery_qa_failed raise(终止为 run.failed,不静默 END)。
"""

from __future__ import annotations

import pytest

from graph import route_after_join_qa
from nodes.node_delivery_qa_repair import (
    DELIVERY_QA_MAX_REPAIRS,
    _blocked_categories,
    _build_dispatch,
    delivery_qa_failed,
    delivery_qa_repair,
)


def _state(gate_failed: bool, retry: int | None = None, categories: dict | None = None) -> dict:
    state: dict = {
        "join_qa_report": {
            "gate_failed": gate_failed,
            "required_features": {
                "subtitles_zh": True, "subtitles_en": True, "voiceover_zh": True,
                "voiceover_en": True, "bgm": False, "transitions": True,
                "text_animation": True, "stickers": False, "volume_fades": True,
            },
            "categories": categories or {},
        },
        "status_log": [],
    }
    if retry is not None:
        state["delivery_qa_retry_count"] = retry
    return state


def test_max_repairs_is_one() -> None:
    """修复上限 1 次:repair → join 复检一轮,再失败即终止。"""
    assert DELIVERY_QA_MAX_REPAIRS == 1


def test_route_gate_ok_exports() -> None:
    assert route_after_join_qa(_state(gate_failed=False)) == "export_final_video"


def test_route_gate_failed_routes_to_repair() -> None:
    assert route_after_join_qa(_state(gate_failed=True)) == "delivery_qa_repair"


def test_route_gate_failed_after_retry_terminates() -> None:
    assert route_after_join_qa(_state(gate_failed=True, retry=1)) == "delivery_qa_failed"


def test_blocked_categories_only_required_failed_or_degraded() -> None:
    state = _state(
        gate_failed=True,
        categories={
            "transitions": {"status": "failed", "required": True},
            "stickers": {"status": "failed", "required": False},  # 未 required 不算
            "voiceover_zh": {"status": "degraded", "required": True},
            "subtitles_zh": {"status": "not_applicable", "required": True},
        },
    )
    assert _blocked_categories(state) == ["voiceover_zh", "transitions"]


def test_dispatch_covers_all_repairable_categories() -> None:
    dispatch = _build_dispatch()
    assert set(dispatch.keys()) == {"transitions", "text_animation", "stickers", "volume_fades"}
    # 数据性缺失类别不可修复
    for key in ("subtitles_zh", "subtitles_en", "voiceover_zh", "voiceover_en", "bgm"):
        assert key not in dispatch


def test_repair_retries_only_repairable(monkeypatch) -> None:
    called: list[str] = []

    def _fake_action(state):
        called.append("ok")
        return {}

    monkeypatch.setattr(
        "nodes.node_delivery_qa_repair._build_dispatch",
        lambda: {"transitions": _fake_action, "stickers": _fake_action},
    )
    state = _state(
        gate_failed=True,
        categories={
            "transitions": {"status": "failed", "required": True},
            "stickers": {"status": "failed", "required": True},
            "bgm": {"status": "failed", "required": True},  # 不可修复
        },
    )
    state["join_qa_report"]["required_features"] = {
        "transitions": True, "stickers": True, "bgm": True,
    }
    out = delivery_qa_repair(state)
    assert called == ["ok", "ok"]
    assert out["delivery_qa_retry_count"] == DELIVERY_QA_MAX_REPAIRS
    tags = out["status_log"]
    # 2026-11-23 修复 §4.6:attempted ≠ compiled;"已尝试"不再伪装成"已修复"
    assert "delivery_qa_repair_attempted:transitions,stickers" in tags
    assert "delivery_qa_unrepairable:bgm" in tags


def test_repair_merges_action_state_updates(monkeypatch) -> None:
    """R-04:action 返回的 status_log/error_log 增量必须并入 repair 节点 state。"""

    def _fake_action(state):
        return {
            "status_log": ["fake_action_status"],
            "error_log": ["[fake] 编译未成功"],
        }

    monkeypatch.setattr(
        "nodes.node_delivery_qa_repair._build_dispatch",
        lambda: {"transitions": _fake_action},
    )
    state = _state(
        gate_failed=True,
        categories={"transitions": {"status": "failed", "required": True}},
    )
    out = delivery_qa_repair(state)
    assert "fake_action_status" in out["status_log"]
    assert "[fake] 编译未成功" in out["error_log"]


def test_repair_compile_receipts_consumed_per_branch(monkeypatch) -> None:
    """R-04/R-05:分支级编译回执进 delivery_qa_compile;zh 成功/en 失败独立记录。"""

    def _fake_action(state):
        return {
            "status_log": ["delivery_qa_recompile_zh:compiled"],
            "error_log": ["[delivery_qa] SDK 重编译未成功(en): failed; SegmentOverlap('x')"],
            "delivery_qa_compile": {
                "zh": {"status": "compiled", "note": "", "fingerprint": "fp-zh"},
                "en": {"status": "failed", "note": "SegmentOverlap('x')", "fingerprint": "fp-en"},
            },
        }

    monkeypatch.setattr(
        "nodes.node_delivery_qa_repair._build_dispatch",
        lambda: {"transitions": _fake_action},
    )
    state = _state(
        gate_failed=True,
        categories={"transitions": {"status": "failed", "required": True}},
    )
    out = delivery_qa_repair(state)
    tags = out["status_log"]
    assert "delivery_qa_repair_compiled:zh" in tags
    assert "delivery_qa_repair_failed:en" in tags
    assert out["delivery_qa_compile"]["zh"]["status"] == "compiled"
    assert out["delivery_qa_compile"]["en"]["status"] == "failed"
    assert any("重编译未成功(en)" in line for line in out["error_log"])


def test_repair_replays_hook_only_for_compiled_branches(monkeypatch, tmp_path) -> None:
    """R-07:编译成功的分支重放 hook;失败/跳过分支不重放;日志增量并入 state。"""
    zh_draft = tmp_path / "draft_content.json"
    zh_draft.write_text("{}", encoding="utf-8")
    en_dir = tmp_path / "en"
    en_dir.mkdir()
    (en_dir / "draft_content.json").write_text("{}", encoding="utf-8")

    replayed_files: list[str] = []

    def _fake_replay(draft_file, state, *, lang):
        replayed_files.append(lang)
        return {"status_log": [f"node_09b_hook_replay_{lang}_done"], "error_log": []}

    monkeypatch.setattr(
        "nodes.node_09b_inject_opening_hook.replay_hook_for_draft", _fake_replay
    )

    def _fake_action(state):
        return {
            "delivery_qa_compile": {
                "zh": {"status": "compiled", "note": "", "fingerprint": "fp"},
                "en": {"status": "failed", "note": "blocked", "fingerprint": "fp"},
            },
        }

    monkeypatch.setattr(
        "nodes.node_delivery_qa_repair._build_dispatch",
        lambda: {"transitions": _fake_action},
    )
    state = _state(
        gate_failed=True,
        categories={"transitions": {"status": "failed", "required": True}},
    )
    state["draft_path"] = str(zh_draft)
    state["draft_dir_en_branch"] = str(en_dir)
    out = delivery_qa_repair(state)
    assert replayed_files == ["zh"]  # 只重放编译成功的分支
    assert "delivery_qa_repair_replayed_hook:zh" in out["status_log"]
    assert "node_09b_hook_replay_zh_done" in out["status_log"]


def test_compile_receipt_status_write_failed_counts_as_failed() -> None:
    """R-05:compiled 但 write.ok=false 按失败处理,不得算修复成功。"""
    from nodes.node_delivery_qa_repair import _compile_receipt_status

    assert _compile_receipt_status({"status": "compiled", "write": {"ok": True}}) == "compiled"
    assert _compile_receipt_status({"status": "compiled", "write": {"ok": False}}) == "failed"
    assert _compile_receipt_status({"status": "failed_blocked_features"}) == "failed_blocked_features"
    assert _compile_receipt_status({"status": "skipped_no_spec"}) == "skipped_no_spec"
    assert _compile_receipt_status({"status": "skipped_fingerprint"}) == "skipped_fingerprint"


def test_recompile_drafts_consumes_pipeline_receipt(monkeypatch, tmp_path) -> None:
    """_recompile_drafts 消费 compile_and_publish_draft 回执并按分支记录。"""
    import nodes.node_delivery_qa_repair as repair_mod

    zh_draft = tmp_path / "draft_content.json"
    zh_draft.write_text("{}", encoding="utf-8")
    calls: list[dict] = []

    def _fake_compile(draft_dir, draft_path, *, job_id, canonical_plan, audit_node, force_rebuild):
        calls.append({"audit_node": audit_node, "force_rebuild": force_rebuild})
        if audit_node.endswith("_zh"):
            return {"status": "compiled", "fingerprint": "fp", "write": {"ok": True}}
        return {
            "status": "failed_blocked_features",
            "fingerprint": "fp",
            "note": "UnsupportedManualFeatureError([...])",
        }

    monkeypatch.setattr(
        "jianying_sdk.pipeline.compile_and_publish_draft", _fake_compile
    )
    state = {
        "draft_path": str(zh_draft),
        "draft_dir_en_branch": str(tmp_path / "en_missing"),  # 目录不存在 → 不编译 en
        "session_id": "sess-test",
    }
    out = repair_mod._recompile_drafts(state)
    assert len(calls) == 1 and calls[0]["force_rebuild"] is True
    assert out["delivery_qa_compile"]["zh"]["status"] == "compiled"
    assert "delivery_qa_recompile_zh:compiled" in out["status_log"]



def test_repair_no_blocked_categories_still_sets_retry_count() -> None:
    """无阻断类别(防御路径,路由正常不会进)→ 不重跑,只写计数。"""
    out = delivery_qa_repair(_state(gate_failed=True, categories={}))
    assert out["delivery_qa_retry_count"] == DELIVERY_QA_MAX_REPAIRS
    assert out["status_log"] == ["delivery_qa_repair_done"]


def test_failed_node_raises_with_blocked_detail() -> None:
    state = _state(
        gate_failed=True,
        retry=1,
        categories={
            "transitions": {
                "status": "failed",
                "required": True,
                "issues": [{"check": "转场", "severity": "error", "detail": "转场缺失", "draft": "zh"}],
            },
        },
    )
    with pytest.raises(RuntimeError) as excinfo:
        delivery_qa_failed(state)
    assert "必需功能未通过" in str(excinfo.value)
    assert "transitions" in str(excinfo.value)


def test_repair_nodes_registered_in_graph() -> None:
    """图编译包含 repair/failed 节点与回边(阻断路由接线存在)。"""
    from config import make_checkpointer
    import graph as g

    compiled = g.build_graph(checkpointer=make_checkpointer("memory"))
    node_names = set(compiled.get_graph().nodes.keys())
    assert "delivery_qa_repair" in node_names
    assert "delivery_qa_failed" in node_names
    edges = {(e.source, e.target) for e in compiled.get_graph().edges}
    assert ("join_before_delivery", "delivery_qa_repair") in edges
    assert ("delivery_qa_repair", "join_before_delivery") in edges
    assert ("delivery_qa_failed", "__end__") in edges

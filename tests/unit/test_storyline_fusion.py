"""human 模式融合:侧支包装(nodes/storyline/fusion.py)的单元测试。

覆盖:规格表完整性、挂载数上限、白名单、缺前置跳过、异常隔离、产物目录隔离、
重命名发布(不覆盖 human 主图字段)、最终时间线选择、QA 标签改名、禁用开关。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
from nodes.storyline import fusion
from nodes.storyline.fusion import FUSION_NODE_NAMES, FUSION_SPECS, fusion_layout, wrap_node


# ---------------------------------------------------------------------------
# 规格表
# ---------------------------------------------------------------------------
def test_specs_cover_exactly_the_21_storyline_nodes():
    import graph

    registered = {n for n in graph._build_state_graph().nodes if n.startswith("storyline_")}
    assert len(FUSION_NODE_NAMES) == 21
    assert len(set(FUSION_NODE_NAMES)) == 21
    assert set(FUSION_NODE_NAMES) == registered


@pytest.mark.parametrize("qc", [True, False])
def test_layout_is_dispersed(qc):
    layout = fusion_layout(qc)
    assert sum(len(v) for v in layout.values()) == 21
    # 不得集中:每处最多 2 个,只有"交付闸门"例外(RV / QA / JOIN 三个一组)
    for anchor, names in layout.items():
        cap = 3 if anchor == "join_before_delivery" else 2
        assert len(names) <= cap, (anchor, names)
    assert len(layout) >= 12
    if not qc:
        assert not any(a.startswith("assembly_") for a in layout)


def test_qc_off_only_moves_the_six_assembly_anchored_nodes():
    on, off = fusion_layout(True), fusion_layout(False)
    anchor_on = {n: a for a, ns in on.items() for n in ns}
    anchor_off = {n: a for a, ns in off.items() for n in ns}
    moved = {n for n in anchor_on if anchor_on[n] != anchor_off[n]}
    assert moved == {
        "storyline_split_shots", "storyline_local_asr",
        "storyline_understand_clips", "storyline_speech_rough_cut",
        "storyline_filter_clips", "storyline_group_clips",
    }


# ---------------------------------------------------------------------------
# 包装行为
# ---------------------------------------------------------------------------
def _spy(returns):
    calls: list[dict] = []

    def fn(state):
        calls.append(dict(state))
        return returns(state) if callable(returns) else returns

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: root)
    return root


def test_missing_prerequisite_is_skipped_without_calling_node():
    fn = _spy({"storyline_groups_artifact": "x"})
    out = wrap_node("storyline_group_clips", fn)({"session_id": "s"})
    assert out == {"status_log": ["storyline_group_clips_fusion_skipped:storyline_filtered_clips"]}
    assert fn.calls == []


def test_load_media_only_runs_after_successful_import():
    fn = _spy({"storyline_media_artifact": "m.json"})
    w = wrap_node("storyline_load_media", fn)
    timeout = w({"session_id": "s", "import_video_status": "timeout", "video_input_paths": ["a.mp4"]})
    assert "import_video_status=imported" in timeout["status_log"][0]
    no_paths = w({"session_id": "s", "import_video_status": "imported"})
    assert "video_input_paths" in no_paths["status_log"][0]
    assert fn.calls == []
    ok = w({"session_id": "s", "import_video_status": "imported", "video_input_paths": ["a.mp4"]})
    assert ok["storyline_media_artifact"] == "m.json"
    assert len(fn.calls) == 1


def test_search_media_needs_query_and_pexels_key(monkeypatch):
    fn = _spy({"storyline_media_artifact": "search.json"})
    w = wrap_node("storyline_search_media", fn)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    assert fn.calls == [] and "storyline_targets.query" in w({"session_id": "s"})["status_log"][0]
    assert "PEXELS_API_KEY" in w({"session_id": "s", "storyline_targets": {"query": "cat"}})["status_log"][0]
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    out = w({"session_id": "s", "storyline_targets": {"query": "cat"}})
    # 重命名发布:不覆盖 load_media 的 storyline_media_artifact(审计文档 P0-2)
    assert out["storyline_search_media_artifact"] == "search.json"
    assert "storyline_media_artifact" not in out


def test_whitelist_blocks_whole_state_echo():
    """STR 原实现 ``return {**state, ...}``:不过滤会把整份 state 回写,与并行节点冲突。"""
    state = {"session_id": "s", "storyline_groups_artifact": "g.json", "draft_path": "D", "storyline_plan": {"a": 1}}
    fn = _spy(lambda st: {**st, "status_log": [*st.get("status_log", []), "x_done"]})
    out = wrap_node("storyline_script_template_recommendation", fn)(state)
    assert out == {"status_log": ["x_done"]}


def test_exception_is_isolated():
    def boom(_state):
        raise RuntimeError("kaboom")

    out = wrap_node("storyline_filter_clips", boom)({"session_id": "s", "storyline_understanding_artifact": "u"})
    assert out["status_log"] == ["storyline_filter_clips_fusion_failed"]
    assert out["error_log"] == ["[fusion:storyline_filter_clips] RuntimeError: kaboom"]
    assert set(out) == {"status_log", "error_log"}


def test_outputs_are_isolated_from_openstoryline_session_dir(out_root):
    """node_04b 会把 storyline_outputs_root 设成网页会话目录;融合节点不得往里写。"""
    seen = {}

    def fn(state):
        seen["root"] = state["storyline_outputs_root"]
        return {}

    wrap_node("storyline_filter_clips", fn)(
        {"session_id": "job1", "storyline_understanding_artifact": "u", "storyline_outputs_root": "WEB/SESSION"}
    )
    assert Path(seen["root"]) == out_root / "job1" / "fusion"


def test_join_publishes_to_auto_plan_and_drops_foreign_keys(tmp_path):
    tl = tmp_path / "tl.json"
    tl.write_text("{}", encoding="utf-8")
    fn = _spy({
        "storyline_plan": {"clips": []},
        "storyline_session_id": "x",
        "storyline_outputs_root": "y",
        "storyline_error_code": "CONTRACT_INVALID",
        "status_log": ["storyline_join_done"],
    })
    out = wrap_node("storyline_join", fn)({"session_id": "s", "storyline_timeline_plan": str(tl)})
    assert out == {"storyline_auto_plan": {"clips": []}, "status_log": ["storyline_join_done"]}


def test_final_timeline_prefers_ai_variant(tmp_path):
    base, ai = tmp_path / "base.json", tmp_path / "ai.json"
    base.write_text("{}", encoding="utf-8")
    ai.write_text("{}", encoding="utf-8")
    fn = _spy({})
    w = wrap_node("storyline_render_video", fn)
    w({"session_id": "s", "storyline_timeline_plan": str(base)})
    assert fn.calls[-1]["storyline_timeline_plan"] == str(base)
    w({"session_id": "s", "storyline_timeline_plan": str(base), "storyline_timeline_plan_ai": str(ai)})
    assert fn.calls[-1]["storyline_timeline_plan"] == str(ai)


def test_ai_timeline_is_published_under_its_own_key():
    fn = _spy({"storyline_timeline_plan": "ai.json", "status_log": ["storyline_plan_timeline_ai_transition_done"]})
    out = wrap_node("storyline_plan_timeline_ai_transition", fn)(
        {"session_id": "s", "storyline_groups_artifact": "g.json", "storyline_timeline_plan": "base.json"}
    )
    assert out["storyline_timeline_plan_ai"] == "ai.json"
    assert "storyline_timeline_plan" not in out


def test_qa_retry_tag_is_renamed_and_retry_counter_not_published(tmp_path):
    tl = tmp_path / "tl.json"
    tl.write_text("{}", encoding="utf-8")
    fn = _spy({"status_log": ["storyline_qa_retry"], "storyline_qa_retry_count": 1})
    out = wrap_node("storyline_qa_gate", fn)({"session_id": "s", "storyline_timeline_plan": str(tl)})
    assert out == {"status_log": ["storyline_qa_over_target"]}


def test_status_log_is_published_as_delta():
    fn = _spy({"status_log": ["a", "b", "x_done"], "error_log": ["e1", "e2"]})
    out = wrap_node("storyline_recommend_transition", fn)(
        {"session_id": "s", "storyline_groups_artifact": "g", "status_log": ["a", "b"], "error_log": ["e1"]}
    )
    assert out["status_log"] == ["x_done"]
    assert out["error_log"] == ["e2"]


def test_disable_switch_skips_node(monkeypatch):
    monkeypatch.setattr(config, "STORYLINE_FUSION_DISABLE", frozenset({"storyline_recommend_text"}))
    fn = _spy({"storyline_text_style_plan": {"a": 1}})
    out = wrap_node("storyline_recommend_text", fn)({"session_id": "s", "storyline_groups_artifact": "g"})
    assert out == {"status_log": ["storyline_recommend_text_fusion_disabled"]}
    assert fn.calls == []


def test_every_spec_writes_only_declared_storyline_keys():
    """白名单键必须都是 state.py 里声明过的 storyline_* 字段(防止笔误悄悄丢产物)。"""
    import state

    declared = set(state.WorkflowState.__annotations__)
    for spec in FUSION_SPECS:
        for key in spec.writes:
            assert key in declared, (spec.node_name, key)
        for new_key in spec.rename.values():
            assert new_key in declared, (spec.node_name, new_key)


# ---------------------------------------------------------------------------
# 开关解析(用子进程重新 import config:tests/conftest.py 的夹具会把进程内的值强制成关)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]


def _config_value_in_fresh_process(var: str, value: str | None, expr: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != var}
    if value is not None:
        env[var] = value
    out = subprocess.run(
        [sys.executable, "-c", f"import config; print({expr})"],
        env=env, cwd=str(_ROOT), capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    "raw,expected",
    [(None, "False"), ("", "False"), ("0", "False"), ("no", "False"),
     ("1", "True"), ("true", "True"), ("YES", "True"), (" 1 ", "True")],
)
def test_fusion_flag_defaults_off_and_parses_env(raw, expected):
    assert _config_value_in_fresh_process(
        "STORYLINE_HUMAN_FUSION", raw, "config.STORYLINE_HUMAN_FUSION") == expected


def test_fusion_disable_env_is_a_comma_separated_set():
    got = _config_value_in_fresh_process(
        "STORYLINE_FUSION_DISABLE", " storyline_a , storyline_b ,, ",
        "sorted(config.STORYLINE_FUSION_DISABLE)")
    assert got == "['storyline_a', 'storyline_b']"
    assert _config_value_in_fresh_process(
        "STORYLINE_FUSION_DISABLE", None, "sorted(config.STORYLINE_FUSION_DISABLE)") == "[]"

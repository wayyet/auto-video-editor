"""单测:AI 转场两个节点(2026-10 接入主图后的新行为)。

- ``storyline_generate_ai_transition``:与 plan_timeline_pro 并排;相邻 group 的衔接点出清单;
  转场方案按 group_id 作为 ``recommended_transition`` 提示传给客户端;失败只隔离、不抛。
- ``storyline_plan_timeline_ai_transition``:在 plan_timeline_pro 写出的基础时间线上只挂清单,
  不重算、不改 clips;任何失败都保留基础时间线。
- 两个节点都在 qa 重试回路里:**状态标签必须固定**(不带条数 / 不分 done_empty 之类),
  否则新标签会排到 ``storyline_qa_retry`` 之后,qa 路由会悄悄跳过重试。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from nodes.storyline import node_generate_ai_transition as gen_mod
from nodes.storyline.node_generate_ai_transition import (
    _adjacent_pairs,
    storyline_generate_ai_transition_node,
)
from nodes.storyline.node_plan_timeline_ai_transition import (
    storyline_plan_timeline_ai_transition_node,
)
from storyline.contract import CanonicalTimeline
from storyline_capabilities.ai_transition_client import (
    AITransitionResult,
    set_default_ai_transition_client,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _flag_on_and_reset_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", True)
    set_default_ai_transition_client(None)
    yield
    set_default_ai_transition_client(None)


def _groups(*clip_id_lists: list[str]) -> list[dict]:
    return [
        {
            "group_id": f"g{i + 1}",
            "clips": [{"clip_id": cid, "media_id": "m1"} for cid in ids],
        }
        for i, ids in enumerate(clip_id_lists)
    ]


def _write(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _gen_state(tmp_path: Path, groups: list[dict] | None) -> dict:
    state: dict = {"session_id": "unit", "storyline_outputs_root": str(tmp_path), "status_log": [], "error_log": []}
    if groups is not None:
        state["storyline_groups_artifact"] = _write(tmp_path / "in" / "groups.json", {"groups": groups})
    return state


class _FakeClient:
    """按调用顺序决定 provider;可指定第 n 次调用抛异常。"""

    def __init__(self, providers: list[str], raise_on: set[int] | None = None) -> None:
        self.providers = providers
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, str]] = []
        self.args: list[tuple[dict, dict]] = []

    def generate(self, *, from_clip: dict, to_clip: dict, output_dir: Path) -> AITransitionResult:
        n = len(self.calls)
        self.calls.append((from_clip["clip_id"], to_clip["clip_id"]))
        self.args.append((dict(from_clip), dict(to_clip)))
        if n in self.raise_on:
            raise RuntimeError("模型服务挂了")
        out = Path(output_dir) / f"fake_{n}.json"
        out.write_text("{}", encoding="utf-8")
        return AITransitionResult(
            artifact_path=out,
            from_clip_id=from_clip["clip_id"],
            to_clip_id=to_clip["clip_id"],
            duration_ms=700,
            provider=self.providers[n % len(self.providers)],
        )


def _read_doc(state_update: dict) -> dict:
    return json.loads(Path(state_update["storyline_ai_transition_artifact"]).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _adjacent_pairs
# ---------------------------------------------------------------------------
def test_adjacent_pairs_use_last_clip_of_previous_group_and_first_of_next() -> None:
    pairs = _adjacent_pairs(_groups(["a1", "a2", "a3"], ["b1", "b2"], ["c1"]))
    assert [(f["clip_id"], t["clip_id"]) for f, t in pairs] == [("a3", "b1"), ("b2", "c1")]


def test_adjacent_pairs_single_group_or_nothing_gives_no_pairs() -> None:
    assert _adjacent_pairs(_groups(["a1", "a2"])) == []
    assert _adjacent_pairs([]) == []


def test_adjacent_pairs_skip_empty_groups_and_bad_entries() -> None:
    groups = [
        {"group_id": "g1", "clips": [{"clip_id": "a1"}]},
        {"group_id": "g2", "clips": []},
        "不是 dict",
        {"group_id": "g4", "clips": [None, {"clip_id": "d1"}, 7]},
        {"group_id": "g5"},
    ]
    pairs = _adjacent_pairs(groups)  # type: ignore[arg-type]
    assert [(f["clip_id"], t["clip_id"]) for f, t in pairs] == [("a1", "d1")]


# ---------------------------------------------------------------------------
# storyline_generate_ai_transition_node
# ---------------------------------------------------------------------------
def test_generate_with_default_stub_client(tmp_path: Path) -> None:
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"], ["c1"])))
    doc = _read_doc(out)
    assert out["status_log"][-1] == "storyline_generate_ai_transition_done"
    assert doc["provider"] == "stub" and doc["is_stub"] is True
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in doc["items"]] == [("a1", "b1"), ("b1", "c1")]
    for it in doc["items"]:
        assert it["provider"] == "stub" and it["is_stub"] is True and it["duration_ms"] == 500
        assert Path(it["artifact"]).is_file()
    # 合法的契约条目
    CanonicalTimeline.model_validate(
        {
            "job_id": "u",
            "created_at_ms": 1,
            "source_media": [{"media_id": "m1", "file_uri": "file:///C:/a.mp4", "duration_ms": 1000}],
            "ai_transitions": doc["items"],
        }
    )


def test_generate_marks_real_provider_as_not_stub_and_mixed(tmp_path: Path) -> None:
    set_default_ai_transition_client(_FakeClient(["real_gpu", "stub"]))
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"], ["c1"])))
    doc = _read_doc(out)
    assert [i["is_stub"] for i in doc["items"]] == [False, True]
    assert doc["provider"] == "mixed" and doc["is_stub"] is False


def test_generate_all_real_provider(tmp_path: Path) -> None:
    set_default_ai_transition_client(_FakeClient(["real_gpu"]))
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"], ["c1"])))
    doc = _read_doc(out)
    assert doc["provider"] == "real_gpu" and doc["is_stub"] is False
    assert all(i["duration_ms"] == 700 for i in doc["items"])


def test_generate_skips_only_the_pair_that_raises(tmp_path: Path) -> None:
    client = _FakeClient(["stub"], raise_on={0})
    set_default_ai_transition_client(client)
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"], ["c1"])))
    doc = _read_doc(out)
    assert len(client.calls) == 2  # 两个衔接点都试过
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in doc["items"]] == [("b1", "c1")]
    assert out["status_log"][-1] == "storyline_generate_ai_transition_done"
    assert "error_log" not in out


def test_generate_with_one_group_writes_empty_items_provider_none(tmp_path: Path) -> None:
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1", "a2"])))
    doc = _read_doc(out)
    assert doc["items"] == [] and doc["provider"] == "none"
    assert out["status_log"][-1] == "storyline_generate_ai_transition_done"


def test_generate_without_groups_artifact_is_not_an_error(tmp_path: Path) -> None:
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, None))
    assert _read_doc(out)["items"] == []
    assert "error_log" not in out


def test_generate_flag_off_only_writes_skipped_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", False)
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"])))
    assert out == {"status_log": ["storyline_generate_ai_transition_skipped"]}


def test_generate_body_failure_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_state):
        raise RuntimeError("读盘炸了")

    monkeypatch.setattr(gen_mod, "_read_groups", boom)
    state = _gen_state(tmp_path, _groups(["a1"], ["b1"]))
    out = storyline_generate_ai_transition_node(state)
    assert out["status_log"][-1] == "storyline_generate_ai_transition_failed"
    assert "storyline_ai_transition_artifact" not in out
    assert any("[storyline:generate_ai_transition] TOOL_EXECUTION_FAILED" in e for e in out["error_log"])


def test_generate_keeps_earlier_error_log_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_mod, "_read_groups", lambda _s: (_ for _ in ()).throw(RuntimeError("x")))
    state = _gen_state(tmp_path, _groups(["a1"], ["b1"]))
    state["error_log"] = ["早先的错误"]
    out = storyline_generate_ai_transition_node(state)
    assert out["error_log"][0] == "早先的错误" and len(out["error_log"]) == 2


# ---------------------------------------------------------------------------
# storyline_plan_timeline_ai_transition_node
# ---------------------------------------------------------------------------
def _base_timeline() -> dict:
    return {
        "schema_version": "1.0",
        "job_id": "unit",
        "created_at_ms": 1,
        "source_media": [{"media_id": "m1", "file_uri": "file:///C:/a.mp4", "duration_ms": 60000}],
        "clips": [
            {
                "clip_id": cid,
                "source_media_id": "m1",
                "source_in_ms": i * 4000,
                "source_out_ms": i * 4000 + 4000,
                "timeline_in_ms": i * 4000,
                "timeline_out_ms": i * 4000 + 4000,
                "transcript": f"字幕{cid}",
            }
            for i, cid in enumerate(["c1", "c2", "c3"])
        ],
        "audio": {"bgm_ref": "bgm_0001", "voiceover": None},
        "subtitles": {"zh": "整段中文文案", "en": None},
        "options": {"enable_ai_transition": False, "enable_voiceover": True, "max_duration_ms": 35000},
        "method": "tts_align",
        "warnings": ["提示一条"],
    }


def _item(a: str, b: str, **kw: Any) -> dict:
    d = {"from_clip_id": a, "to_clip_id": b, "duration_ms": 500, "provider": "stub", "is_stub": True,
         "error": None, "artifact": None}
    d.update(kw)
    return d


def _plan_state(tmp_path: Path, items: list[dict] | None, base: dict | None = None) -> dict:
    state: dict = {"session_id": "unit", "storyline_outputs_root": str(tmp_path), "status_log": [], "error_log": []}
    if base is not None:
        state["storyline_timeline_plan"] = _write(tmp_path / "storyline" / "timeline_plan.json", base)
    if items is not None:
        state["storyline_ai_transition_artifact"] = _write(tmp_path / "storyline" / "ai_transition.json", {"items": items})
    return state


def test_plan_attaches_items_writes_new_file_and_leaves_base_untouched(tmp_path: Path) -> None:
    base = _base_timeline()
    state = _plan_state(tmp_path, [_item("c1", "c2"), _item("c2", "c3")], base)
    base_bytes = Path(state["storyline_timeline_plan"]).read_bytes()

    out = storyline_plan_timeline_ai_transition_node(state)

    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_done"
    new_path = Path(out["storyline_timeline_plan"])
    assert new_path.name == "timeline_plan_ai.json"
    assert Path(state["storyline_timeline_plan"]).read_bytes() == base_bytes  # 基础时间线原封不动
    merged = json.loads(new_path.read_text(encoding="utf-8"))
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in merged["ai_transitions"]] == [("c1", "c2"), ("c2", "c3")]
    CanonicalTimeline.model_validate(merged)


def test_plan_does_not_touch_clips_subtitles_transcripts_or_duration(tmp_path: Path) -> None:
    """旧版会重算时间线并丢掉字幕 / transcript;新版只能多出 ai_transitions。"""
    base = _base_timeline()
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, [_item("c1", "c2")], base))
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    expected = copy.deepcopy(base)
    expected["ai_transitions"] = [_item("c1", "c2")]
    assert merged == expected  # stub → enable_ai_transition 仍为 False,其余逐字段相同
    assert [c["transcript"] for c in merged["clips"]] == ["字幕c1", "字幕c2", "字幕c3"]
    assert merged["subtitles"]["zh"] == "整段中文文案"


def test_plan_enable_flag_true_only_when_a_real_item_exists(tmp_path: Path) -> None:
    only_stub = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path / "a", [_item("c1", "c2")], _base_timeline())
    )
    assert json.loads(Path(only_stub["storyline_timeline_plan"]).read_text(encoding="utf-8"))["options"]["enable_ai_transition"] is False

    with_real = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path / "b", [_item("c1", "c2"), _item("c2", "c3", provider="real", is_stub=False)], _base_timeline())
    )
    assert json.loads(Path(with_real["storyline_timeline_plan"]).read_text(encoding="utf-8"))["options"]["enable_ai_transition"] is True


def test_plan_filters_items_whose_clips_are_not_in_the_timeline(tmp_path: Path) -> None:
    """qa 重试 / 重新分组后 clip_id 可能变化:悬空引用必须被剔除,而不是让契约校验失败。"""
    items = [_item("c1", "c2"), _item("c2", "ghost"), _item("ghost", "c3")]
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, items, _base_timeline()))
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in merged["ai_transitions"]] == [("c1", "c2")]


def test_plan_all_items_dangling_keeps_base_plan(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("x", "y")], _base_timeline())
    )
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_done"
    assert "storyline_timeline_plan" not in out  # 不改指针 → 下游继续用基础时间线


@pytest.mark.parametrize("items", [None, []])
def test_plan_without_items_keeps_base_plan(tmp_path: Path, items) -> None:
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, items, _base_timeline()))
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_done"
    assert "storyline_timeline_plan" not in out


def test_plan_without_base_timeline_is_skipped(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, [_item("c1", "c2")], None))
    assert out == {"status_log": ["storyline_plan_timeline_ai_transition_skipped"]}


def test_plan_invalid_merge_keeps_base_and_logs_error(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("c1", "c2", duration_ms=-5)], _base_timeline())
    )
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_failed"
    assert "storyline_timeline_plan" not in out
    assert any("[storyline:plan_timeline_ai_transition]" in e for e in out["error_log"])
    assert not (tmp_path / "storyline" / "timeline_plan_ai.json").exists()  # 没写出半成品


def test_plan_unknown_field_in_item_is_rejected_not_written(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("c1", "c2", surprise=1)], _base_timeline())
    )
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_failed"
    assert not (tmp_path / "storyline" / "timeline_plan_ai.json").exists()


def test_plan_flag_off_only_writes_skipped_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", False)
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("c1", "c2")], _base_timeline())
    )
    assert out == {"status_log": ["storyline_plan_timeline_ai_transition_skipped"]}


def test_plan_creates_options_when_base_has_none(tmp_path: Path) -> None:
    base = _base_timeline()
    del base["options"]
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, [_item("c1", "c2")], base))
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    assert merged["options"]["enable_ai_transition"] is False
    CanonicalTimeline.model_validate(merged)


def test_plan_keeps_earlier_error_log_entries_on_failure(tmp_path: Path) -> None:
    state = _plan_state(tmp_path, [_item("c1", "c2", duration_ms=-1)], _base_timeline())
    state["error_log"] = ["早先的错误"]
    out = storyline_plan_timeline_ai_transition_node(state)
    assert out["error_log"][0] == "早先的错误" and len(out["error_log"]) == 2


# ---------------------------------------------------------------------------
# 2026-10 新增:状态标签固定(qa 重试回路里不能每轮都变)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("group_sizes", [[["a1"]], [["a1"], ["b1"]], [["a1"], ["b1"], ["c1"], ["d1"]]])
def test_generate_status_tag_is_constant_for_any_item_count(tmp_path: Path, group_sizes) -> None:
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(*group_sizes)))
    assert out["status_log"] == ["storyline_generate_ai_transition_done"]


def test_plan_every_success_path_uses_the_same_done_tag(tmp_path: Path) -> None:
    attached = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path / "a", [_item("c1", "c2")], _base_timeline())
    )
    nothing_to_attach = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path / "b", [], _base_timeline())
    )
    assert attached["status_log"] == nothing_to_attach["status_log"] == [
        "storyline_plan_timeline_ai_transition_done"
    ]


# ---------------------------------------------------------------------------
# 2026-10 新增:转场方案(recommend_transition 的产物)作为"仅参考"提示传给客户端
# ---------------------------------------------------------------------------
def test_adjacent_pairs_carry_group_id_and_do_not_mutate_input() -> None:
    groups = _groups(["a1", "a2"], ["b1"])
    before = copy.deepcopy(groups)
    (pair,) = _adjacent_pairs(groups)
    assert pair[0]["group_id"] == "g1" and pair[1]["group_id"] == "g2"
    assert groups == before  # 返回的是副本


def test_generate_passes_recommended_transitions_to_client(tmp_path: Path) -> None:
    client = _FakeClient(["stub"])
    set_default_ai_transition_client(client)
    state = _gen_state(tmp_path, _groups(["a1"], ["b1"], ["c1"]))
    state["storyline_transition_plan"] = {
        "items": [
            {"group_id": "g1", "transition_in": "fade_in", "transition_out": "wipe_left"},
            {"group_id": "g2", "transition_in": "zoom_in", "transition_out": "dissolve"},
            {"group_id": "g3", "transition_in": "slide_up", "transition_out": "fade_out"},
        ]
    }
    storyline_generate_ai_transition_node(state)
    (a_from, a_to), (b_from, b_to) = client.args
    # 衔接点 g1→g2:前一组的"出"、后一组的"入"
    assert a_from == {"clip_id": "a1", "group_id": "g1", "recommended_transition": "wipe_left"}
    assert a_to == {"clip_id": "b1", "group_id": "g2", "recommended_transition": "zoom_in"}
    # 衔接点 g2→g3
    assert b_from["recommended_transition"] == "dissolve"
    assert b_to["recommended_transition"] == "slide_up"


@pytest.mark.parametrize(
    "plan",
    [None, {}, {"items": None}, {"items": "不是列表"}, ["x"], {"items": [None, 3, {"transition_in": "x"}]}],
)
def test_generate_without_usable_transition_plan_still_works(tmp_path: Path, plan) -> None:
    client = _FakeClient(["stub"])
    set_default_ai_transition_client(client)
    state = _gen_state(tmp_path, _groups(["a1"], ["b1"]))
    if plan is not None:
        state["storyline_transition_plan"] = plan
    out = storyline_generate_ai_transition_node(state)
    assert out["status_log"] == ["storyline_generate_ai_transition_done"]
    (from_arg, to_arg), = client.args
    assert "recommended_transition" not in from_arg and "recommended_transition" not in to_arg
    assert from_arg == {"clip_id": "a1", "group_id": "g1"}


def test_transition_hints_indexes_by_group_id_and_ignores_bad_items() -> None:
    hints = gen_mod._transition_hints(
        {
            "items": [
                {"group_id": "g1", "transition_in": "a", "transition_out": "b"},
                {"group_id": "g2"},  # 缺字段 → 空串
                {"transition_in": "x"},  # 没有 group_id → 丢弃
                "垃圾",
                None,
            ]
        }
    )
    assert hints == {
        "g1": {"transition_in": "a", "transition_out": "b"},
        "g2": {"transition_in": "", "transition_out": ""},
    }

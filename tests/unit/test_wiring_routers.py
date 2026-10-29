"""单测:2026-10 接入"不可达节点"新增 / 改动的路由函数(纯函数,不编译图)。

- ``_web_topic_branch_enabled``         网页话题分支:开关 AND query 非空
- ``_asr_branch_enabled``               ASR 分支:开关 AND 素材有音轨
- ``_storyline_route_after_load_media`` 返回 str 或 list[str](并行 fan-out)
- ``_storyline_route_after_split_shots`` 同上
- qa_gate 之后的路由不再被包装(AI 转场两节点在 qa_gate 之前,由建图时的屏障接线决定)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import graph as g
from nodes.storyline import qa_gate as qa_gate_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_COUNTER = {"n": 0}


def _media_artifact(tmp_path: Path, *media: dict) -> str:
    """每次调用写一个**新文件**(同一个测试里要造多份不同的素材产物,不能互相覆盖)。"""
    _COUNTER["n"] += 1
    p = tmp_path / f"load_media_{_COUNTER['n']}.json"
    p.write_text(json.dumps({"media": list(media)}), encoding="utf-8")
    return str(p)


def _video(has_audio: bool, media_type: str = "video") -> dict:
    return {
        "media_id": "media_0001",
        "media_type": media_type,
        "metadata": {"has_audio": has_audio},
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORYLINE_PEXELS_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# _web_topic_branch_enabled / _asr_branch_enabled
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "flag,targets,expected",
    [
        (True, {"query": "夏季防晒"}, True),
        (True, {"query": "  夏季防晒  "}, True),
        (True, {"query": "   "}, False),
        (True, {"query": ""}, False),
        (True, {"query": None}, False),
        (True, {"query": 123}, False),
        (True, {}, False),
        (True, None, False),
        (False, {"query": "夏季防晒"}, False),
    ],
)
def test_web_topic_branch_needs_flag_and_non_empty_query(
    monkeypatch: pytest.MonkeyPatch, flag: bool, targets, expected: bool
) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", flag)
    state = {} if targets is None else {"storyline_targets": targets}
    assert g._web_topic_branch_enabled(state) is expected


def test_asr_branch_needs_flag_and_audio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with_audio = {"storyline_media_artifact": _media_artifact(tmp_path, _video(True))}
    no_audio = {"storyline_media_artifact": _media_artifact(tmp_path, _video(False))}

    monkeypatch.setattr(config, "STORYLINE_ENABLE_ASR_BRANCH", True)
    assert g._asr_branch_enabled(with_audio) is True
    assert g._asr_branch_enabled(no_audio) is False
    assert g._asr_branch_enabled({}) is False  # 没有素材产物:保守不跑

    monkeypatch.setattr(config, "STORYLINE_ENABLE_ASR_BRANCH", False)
    assert g._asr_branch_enabled(with_audio) is False


def test_asr_branch_ignores_non_video_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_ASR_BRANCH", True)
    only_image = {"storyline_media_artifact": _media_artifact(tmp_path, _video(True, media_type="image"))}
    assert g._asr_branch_enabled(only_image) is False
    mixed = {
        "storyline_media_artifact": _media_artifact(
            tmp_path, _video(True, media_type="image"), _video(True, media_type="video")
        )
    }
    assert g._asr_branch_enabled(mixed) is True


# ---------------------------------------------------------------------------
# _storyline_route_after_load_media
# ---------------------------------------------------------------------------
def test_after_load_media_default_is_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    # 没有 query → 没有网页分支,返回值仍是单个 str(与接入前完全一致)
    assert g._storyline_route_after_load_media({}) == "storyline_split_shots"


def test_after_load_media_adds_web_topic_as_parallel_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    state = {"storyline_targets": {"query": "夏季防晒"}}
    assert g._storyline_route_after_load_media(state) == [
        "storyline_split_shots",
        "storyline_search_web_topic",
    ]


def test_after_load_media_keeps_pexels_bypass_and_web_topic_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", True)
    monkeypatch.setenv("STORYLINE_PEXELS_API_KEY", "k")
    state = {"storyline_targets": {"query": "夏季防晒"}}
    assert g._storyline_route_after_load_media(state) == [
        "storyline_search_media",
        "storyline_search_web_topic",
    ]
    assert g._storyline_route_after_load_media({}) == "storyline_search_media"


def test_after_load_media_flag_off_equals_pre_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_WEB_TOPIC", False)
    state = {"storyline_targets": {"query": "夏季防晒"}}
    assert g._storyline_route_after_load_media(state) == "storyline_split_shots"


# ---------------------------------------------------------------------------
# _storyline_route_after_split_shots
# ---------------------------------------------------------------------------
def test_after_split_shots_adds_asr_when_audio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_ASR_BRANCH", True)
    state = {"storyline_media_artifact": _media_artifact(tmp_path, _video(True))}
    assert g._storyline_route_after_split_shots(state) == [
        "storyline_understand_clips",
        "storyline_local_asr",
    ]


def test_after_split_shots_plain_string_without_audio_or_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "STORYLINE_ENABLE_ASR_BRANCH", True)
    no_audio = {"storyline_media_artifact": _media_artifact(tmp_path, _video(False))}
    assert g._storyline_route_after_split_shots(no_audio) == "storyline_understand_clips"
    monkeypatch.setattr(config, "STORYLINE_ENABLE_ASR_BRANCH", False)
    with_audio = {"storyline_media_artifact": _media_artifact(tmp_path, _video(True))}
    assert g._storyline_route_after_split_shots(with_audio) == "storyline_understand_clips"


# ---------------------------------------------------------------------------
# qa_gate 之后的路由:AI 转场两节点已挪到 qa_gate 之前(与 plan_timeline_pro 并排),
# 所以 qa 之后的路由**不再被包装**,仍是原函数(重试 → group_clips,否则 → render_video)。
# ---------------------------------------------------------------------------
def test_qa_route_is_the_original_function_and_no_wrapper_left() -> None:
    assert not hasattr(g, "_route_after_storyline_qa")
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["x", "storyline_qa_retry"]}) == (
        "storyline_group_clips"
    )
    assert qa_gate_mod.route_after_storyline_qa({"status_log": ["storyline_qa_passed"]}) == (
        "storyline_render_video"
    )

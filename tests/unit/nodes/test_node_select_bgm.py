"""storyline_select_bgm 节点单测:配音时长聚合与 BGM 消费契约。

2026-11-13 边审计阶段3(任务B 运行时回归):``_read_voiceover_total_ms``
的聚合容缺省规则 + 节点把聚合值传给能力层 + 能力层异常兜底不崩。
屏障形状锁定在 tests/unit/test_storyline_edge_governance_topology.py,
真实编译图上的消费链路锁定在 tests/integration/test_unreachable_nodes_wiring.py
(``test_select_bgm_consumes_voiceover_and_rtr_reads_bgm_selection``)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.storyline.node_select_bgm as nsb
from nodes.storyline.node_select_bgm import (
    _read_voiceover_total_ms,
    storyline_select_bgm_node,
)


def _write_json(tmp_path: Path, name: str, obj) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _groups_state(base_state, tmp_path: Path) -> dict:
    """select_bgm 节点的前置输入:分组 artifact + 脚本 artifact(带 narration)。"""
    base_state["storyline_groups_artifact"] = _write_json(
        tmp_path,
        "groups.json",
        {"groups": [{"group_id": "group_0001", "theme": "主题1"}]},
    )
    base_state["storyline_script_artifact"] = _write_json(
        tmp_path,
        "script.json",
        {"group_scripts": [{"group_id": "group_0001", "narration": "旁白文本"}]},
    )
    return base_state


# ---------------------------------------------------------------------------
# _read_voiceover_total_ms 聚合规则(node_select_bgm.py:52-80)
# ---------------------------------------------------------------------------
def test_read_voiceover_total_ms_missing_artifact(base_state) -> None:
    """GV 整体失败不写 artifact(2026-11-10 配音/字幕解耦纪律)→ 按 0 容缺省。"""
    assert _read_voiceover_total_ms(base_state) == 0


def test_read_voiceover_total_ms_malformed_json(base_state, tmp_path: Path) -> None:
    p = tmp_path / "vo.json"
    p.write_text("{not-json", encoding="utf-8")
    base_state["storyline_voiceover_artifact"] = str(p)
    assert _read_voiceover_total_ms(base_state) == 0


def test_read_voiceover_total_ms_non_dict_doc(base_state, tmp_path: Path) -> None:
    p = tmp_path / "vo.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    base_state["storyline_voiceover_artifact"] = str(p)
    assert _read_voiceover_total_ms(base_state) == 0


def test_read_voiceover_total_ms_aggregation_rules(base_state, tmp_path: Path) -> None:
    """error 段跳过、负数钳 0、非 dict 项跳过、缺失/None duration 按 0。"""
    base_state["storyline_voiceover_artifact"] = _write_json(
        tmp_path,
        "vo.json",
        {
            "voiceover": [
                {"group_id": "group_0001", "duration": 3500},
                {"group_id": "group_0002", "duration": -100},
                {"group_id": "group_0003", "error": "tts failed", "duration": 9999},
                "junk",
                {"group_id": "group_0004"},
                {"group_id": "group_0005", "duration": None},
            ]
        },
    )
    assert _read_voiceover_total_ms(base_state) == 3500


def test_read_voiceover_total_ms_empty_voiceover_list(base_state, tmp_path: Path) -> None:
    base_state["storyline_voiceover_artifact"] = _write_json(
        tmp_path, "vo.json", {"voiceover": []}
    )
    assert _read_voiceover_total_ms(base_state) == 0


# ---------------------------------------------------------------------------
# 节点 → 能力层数据流(spy 注入,与 wiring 测试的 spy_generate_script 同款)
# ---------------------------------------------------------------------------
def test_node_passes_aggregated_voiceover_total_ms(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _groups_state(base_state, tmp_path)
    base_state["storyline_voiceover_artifact"] = _write_json(
        tmp_path,
        "vo.json",
        {"voiceover": [{"duration": 3500}, {"duration": 2000}]},
    )

    seen: list[dict] = []

    def fake_select_bgm(**kw):
        seen.append(kw)
        return {
            "bgm_ref": "bgm_x",
            "method": "fake",
            "stub": False,
            "voiceover_total_ms": kw["voiceover_total_ms"],
        }

    monkeypatch.setattr(nsb, "select_bgm", fake_select_bgm)
    delta = storyline_select_bgm_node(base_state)

    assert len(seen) == 1
    assert seen[0]["voiceover_total_ms"] == 5500
    assert seen[0]["groups"] == [{"group_id": "group_0001", "theme": "主题1"}]
    assert seen[0]["narration_text"] == "旁白文本"
    assert delta["storyline_bgm_selection"]["voiceover_total_ms"] == 5500
    assert "storyline_select_bgm_done:bgm_ref=bgm_x" in delta["status_log"]
    assert "error_log" not in delta


def test_node_without_voiceover_artifact_passes_zero(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _groups_state(base_state, tmp_path)

    seen: list[dict] = []

    def fake_select_bgm(**kw):
        seen.append(kw)
        return {"bgm_ref": "bgm_x", "method": "fake", "stub": False}

    monkeypatch.setattr(nsb, "select_bgm", fake_select_bgm)
    storyline_select_bgm_node(base_state)

    assert len(seen) == 1
    assert seen[0]["voiceover_total_ms"] == 0


def test_node_capability_exception_falls_back(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """能力层抛异常 → 节点 stub 兜底,不崩、不阻断下游(node_select_bgm.py:100-112)。"""

    def boom(**_kw):
        raise RuntimeError("capability down")

    monkeypatch.setattr(nsb, "select_bgm", boom)
    delta = storyline_select_bgm_node(_groups_state(base_state, tmp_path))

    sel = delta["storyline_bgm_selection"]
    assert sel["bgm_ref"] == "bgm_fallback"
    assert sel["stub"] is True
    assert "select_bgm exception" in sel["reason"]
    assert any("BGM_FALLBACK" in e for e in delta["error_log"])


# ---------------------------------------------------------------------------
# 20261114 阶段2:云音乐命中时的可读状态日志(曲名/music_id/选择来源)
# ---------------------------------------------------------------------------
def test_node_status_log_includes_music_metadata(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """云音乐素材命中:status_log 带曲名+music_id+selection_source;无 error_log。"""

    def fake_select_bgm(**_kw):
        return {
            "bgm_ref": "7553871402870491155",
            "method": "llm",
            "stub": False,
            "music_id": "7553871402870491155",
            "music_name": "旅途的风景",
            "selection_source": "llm",
            "asset_available": True,
        }

    monkeypatch.setattr(nsb, "select_bgm", fake_select_bgm)
    delta = storyline_select_bgm_node(_groups_state(base_state, tmp_path))

    tags = [s for s in delta["status_log"] if "storyline_select_bgm_done" in s]
    assert tags and "music=旅途的风景(7553871402870491155)" in tags[0]
    assert ":source=llm" in tags[0]
    assert "error_log" not in delta


def test_node_stub_without_music_id_keeps_legacy_log(
    base_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧 YAML 降级路径(无 music_id):日志保持旧形态,BGM_FALLBACK 照记。"""

    def fake_select_bgm(**_kw):
        return {
            "bgm_ref": "bgm_calm_lofi",
            "method": "heuristic",
            "stub": True,
            "reason": "素材不可用",
        }

    monkeypatch.setattr(nsb, "select_bgm", fake_select_bgm)
    delta = storyline_select_bgm_node(_groups_state(base_state, tmp_path))

    tags = [s for s in delta["status_log"] if "storyline_select_bgm_done" in s]
    assert tags and tags[0] == "storyline_select_bgm_done:bgm_ref=bgm_calm_lofi"
    assert any("BGM_FALLBACK" in e for e in delta["error_log"])

"""节点级单测:storyline_generate_script(LLM 留痕落盘 + narration fail-fast)。

2026-11-10 交付 QA 阻断复盘(console-5aa2606e546b):此前节点把能力层异常吞成
error_log 照常绿灯,空 narration 一路静默流到交付 QA 才暴露且零证据。契约:
- LLM 调用/解析留痕先落盘 ``<storyline>/script_llm_trace.json``(成败都写);
- required_features 里 subtitles_zh / voiceover_zh 任一必选而 narration 全空
  → raise(节点红,源头早失败);
- 未 required 时空 narration 不阻断(保持旧行为);
- 能力层异常 → 留痕后 re-raise,不再吞。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nodes.storyline.node_generate_script import storyline_generate_script_node
from storyline_capabilities.vlm_client import StubLLMClient


def _write_groups(tmp_path: Path) -> str:
    p = tmp_path / "groups.json"
    p.write_text(
        json.dumps(
            {
                "groups": [
                    {"group_id": "group_0001", "theme": "海边", "clips": [{"clip_id": "c1"}]},
                    {"group_id": "group_0002", "theme": "寺庙", "clips": [{"clip_id": "c2"}]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(p)


def _state(tmp_path: Path, **overrides: Any) -> dict:
    base: dict[str, Any] = {
        "session_id": "t1",
        "storyline_outputs_root": str(tmp_path),
        "storyline_groups_artifact": _write_groups(tmp_path),
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def test_node_writes_trace_and_extracts_template_keys(tmp_path: Path, monkeypatch) -> None:
    """模板键名(group_scripts/raw_text)→ narration 提取成功 + trace 落盘。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段旁白"},
                    {"group_id": "group_0002", "raw_text": "第二段旁白"},
                ],
                "title": "标题",
            }
        ),
    )
    out = storyline_generate_script_node(_state(tmp_path))

    assert "storyline_generate_script_done" in out["status_log"]
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert [g["narration"] for g in script["group_scripts"]] == ["第一段旁白", "第二段旁白"]
    trace = json.loads(
        (tmp_path / "storyline" / "script_llm_trace.json").read_text(encoding="utf-8")
    )
    assert trace["ok"] is True
    assert "group_scripts" in trace["obj_keys"]


def test_node_fails_fast_when_narration_required_and_empty(tmp_path: Path, monkeypatch) -> None:
    """真实后端 + subtitles_zh 默认必选 + narration 全空 → raise,trace/script 先落盘。"""
    class _EmptyScriptsClient:
        """非 stub 的最小 LLMClient:返回合法 JSON 但没有可用旁白。"""

        def chat(self, **_kw: Any) -> str:
            return json.dumps({"group_scripts": []})

    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT", _EmptyScriptsClient()
    )
    with pytest.raises(RuntimeError, match="narration 全空"):
        storyline_generate_script_node(_state(tmp_path))

    trace = json.loads((tmp_path / "storyline" / "script_llm_trace.json").read_text(encoding="utf-8"))
    assert trace["ok"] is True
    assert trace["obj_keys"] == ["group_scripts"]
    assert (tmp_path / "storyline" / "script.json").exists()


def test_node_stub_backend_lenient_even_when_required(tmp_path: Path, monkeypatch) -> None:
    """stub 联调不 fail-fast(阶段 6 口径:stub 是降级可见不是阻断)。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(default_json={"scripts": []}),
    )
    out = storyline_generate_script_node(_state(tmp_path))
    assert "storyline_generate_script_done" in out["status_log"]


def test_node_lenient_when_narration_not_required(tmp_path: Path, monkeypatch) -> None:
    """字幕/配音都未必选 → 空 narration 不阻断(旧行为)。"""
    class _EmptyScriptsClient:
        def chat(self, **_kw: Any) -> str:
            return json.dumps({"group_scripts": []})

    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT", _EmptyScriptsClient()
    )
    out = storyline_generate_script_node(
        _state(tmp_path, required_features={"subtitles_zh": False, "voiceover_zh": False})
    )
    assert "storyline_generate_script_done" in out["status_log"]


def test_node_reraises_capability_exception_with_trace(tmp_path: Path, monkeypatch) -> None:
    """能力层异常 → trace 落盘(node_exception)后 re-raise,不再吞。"""
    def _boom(**_kw: Any) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "nodes.storyline.node_generate_script.generate_script", _boom
    )
    with pytest.raises(RuntimeError, match="boom"):
        storyline_generate_script_node(_state(tmp_path))

    trace = json.loads((tmp_path / "storyline" / "script_llm_trace.json").read_text(encoding="utf-8"))
    assert trace["stage"] == "node_exception"
    assert "boom" in trace["error"]

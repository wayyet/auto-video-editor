"""generate_script 模板注入单测(2026-11-12 noop 治理阶段1,设计文档 §1.3)。

覆盖:
- 能力层:模板正文真实进入 user prompt(验收标准)、trace 注入证据、
  空模板占位语、注入纪律文案;
- 节点层:模板 artifact 读取与 template_ref 组装(applied/reason)、
  custom_script 优先、artifact 缺失/状态异常降级、script.json 可追溯。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.storyline.node_generate_script as gs_node_mod
from storyline_capabilities.generate_script import generate_script

_REPLY = json.dumps(
    {
        "group_scripts": [
            {"group_id": "group_0001", "raw_text": "今天的咖啡有点甜。", "summary": "咖啡店"}
        ]
    },
    ensure_ascii=False,
)


class _CaptureClient:
    """记录 chat kwargs 的最小 client(chat_json_with_trace 只用 chat)。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.call_count = 0

    def chat(self, **kw: object) -> str:
        self.call_count += 1
        self.calls.append(dict(kw))
        return _REPLY


_GROUPS = [{"group_id": "group_0001", "theme": "咖啡店日常", "clips": []}]


# ---------------------------------------------------------------------------
# 能力层:正文进 prompt + 证据
# ---------------------------------------------------------------------------
def test_template_content_enters_user_prompt_with_trace_evidence() -> None:
    client = _CaptureClient()
    result = generate_script(
        groups=_GROUPS,
        script_template="【旅行叙事模板】开头-期待;结尾-带走什么。",
        client=client,
    )
    user_prompt = client.calls[0]["user_prompt"]
    assert "【旅行叙事模板】" in user_prompt, "模板正文必须真实进入 LLM 请求"
    assert "不是事实来源" in user_prompt, "注入纪律必须随模板块出现"
    assert result["llm_trace"]["script_template_injected"] is True


def test_no_template_placeholder_and_flag_false() -> None:
    client = _CaptureClient()
    generate_script(groups=_GROUPS, script_template="", client=client)
    user_prompt = client.calls[0]["user_prompt"]
    assert "没有注入模板" in user_prompt  # zh 占位语,模板块不悬空
    # trace 由 chat_json_with_trace 产生,injected 标志为 False
    # (经 result.llm_trace 透传)
    result = generate_script(groups=_GROUPS, script_template="", client=client)
    assert result["llm_trace"]["script_template_injected"] is False


def test_template_discipline_in_system_prompt() -> None:
    """system prompt 的 [script_template] 纪律段存在(zh 模板已更新)。"""
    client = _CaptureClient()
    generate_script(groups=_GROUPS, script_template="任意正文", client=client)
    sys_prompt = client.calls[0]["system_prompt"]
    assert "[script_template]" in sys_prompt
    assert "严禁把模板中出现的人名" in sys_prompt


# ---------------------------------------------------------------------------
# 节点层:artifact 读取 + template_ref
# ---------------------------------------------------------------------------
def _write_template_plan(tmp_path: Path, doc: dict) -> str:
    out = tmp_path / "outputs" / "storyline"
    out.mkdir(parents=True, exist_ok=True)
    p = out / "script_template_plan.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _base_state(tmp_path: Path, **over: object) -> dict:
    out_root = tmp_path / "outputs"
    (out_root / "storyline").mkdir(parents=True, exist_ok=True)
    groups_path = out_root / "storyline" / "groups.json"
    groups_path.write_text(
        json.dumps({"groups": _GROUPS}, ensure_ascii=False), encoding="utf-8"
    )
    st: dict = {
        "session_id": "gs-test",
        "status_log": [],
        "error_log": [],
        "storyline_outputs_root": str(out_root),
        "storyline_groups_artifact": str(groups_path),
        "storyline_targets": {"script_request": "日常记录"},
    }
    st.update(over)
    return st


@pytest.fixture()
def capture_capability(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """patch 节点模块里 import 进来的 generate_script 绑定(patch 使用方)。"""
    captured: list[dict] = []

    def fake_generate_script(**kw: object) -> dict:
        captured.append(dict(kw))
        return {
            "group_scripts": [
                {
                    "group_id": "group_0001",
                    "narration": "旁白",
                    "summary": "摘要",
                    "lang": "zh",
                    "voiceover_style": "storyteller",
                    "captions": [],
                }
            ],
            "lang": "zh",
            "voiceover_style": "storyteller",
            "llm_trace": {"ok": True, "script_template_injected": bool(kw.get("script_template"))},
        }

    monkeypatch.setattr(gs_node_mod, "generate_script", fake_generate_script)
    return captured


def test_node_injects_template_and_writes_template_ref(
    tmp_path: Path, capture_capability: list[dict]
) -> None:
    plan = _write_template_plan(
        tmp_path,
        {
            "status": "ok",
            "template_id": "travel_story_zh_v1",
            "template_version": 1,
            "content_sha256": "a" * 64,
            "selected_template_content": "【模板正文】风格参考 {curly} 片段",
        },
    )
    st = _base_state(tmp_path, storyline_script_template_artifact=plan)
    out = gs_node_mod.storyline_generate_script_node(st)
    assert capture_capability, "能力层应被调用"
    assert capture_capability[0]["script_template"] == "【模板正文】风格参考 (curly) 片段"

    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["template_ref"] == {
        "template_id": "travel_story_zh_v1",
        "version": 1,
        "content_sha256": "a" * 64,
        "applied": True,
        "reason": None,
    }
    trace = json.loads(
        Path(Path(out["storyline_script_artifact"]).parent / "script_llm_trace.json").read_text(
            encoding="utf-8"
        )
    )
    assert trace["template_ref"]["applied"] is True


def test_node_custom_script_overrides_template(
    tmp_path: Path, capture_capability: list[dict]
) -> None:
    """用户显式提供 custom_script → 跳过模板(设计文档 §1.3 任务8)。"""
    plan = _write_template_plan(
        tmp_path,
        {
            "status": "ok",
            "template_id": "t_v1",
            "template_version": 1,
            "content_sha256": "a" * 64,
            "selected_template_content": "模板正文",
        },
    )
    st = _base_state(
        tmp_path, storyline_script_template_artifact=plan, custom_script="手写文案"
    )
    out = gs_node_mod.storyline_generate_script_node(st)
    assert capture_capability[0]["script_template"] == ""  # 不注入
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["template_ref"]["applied"] is False
    assert script["template_ref"]["reason"] == "custom_script_override"


def test_node_without_template_artifact_degrades(
    tmp_path: Path, capture_capability: list[dict]
) -> None:
    """旧 checkpoint / 模板节点未产出 → reason=no_artifact,文案照常生成。"""
    st = _base_state(tmp_path)
    out = gs_node_mod.storyline_generate_script_node(st)
    assert capture_capability[0]["script_template"] == ""
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["template_ref"] == {
        "template_id": None,
        "version": None,
        "content_sha256": None,
        "applied": False,
        "reason": "no_artifact",
    }


def test_node_non_ok_status_records_reason(
    tmp_path: Path, capture_capability: list[dict]
) -> None:
    plan = _write_template_plan(tmp_path, {"status": "no_match", "fallback_reason": "无候选"})
    st = _base_state(tmp_path, storyline_script_template_artifact=plan)
    out = gs_node_mod.storyline_generate_script_node(st)
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["template_ref"]["applied"] is False
    assert script["template_ref"]["reason"] == "status=no_match"

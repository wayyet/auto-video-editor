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


# ---------------------------------------------------------------------------
# 2026-11-10 前置化阶段4:web_research 独立上下文 + references 留痕(设计方案 §4.4)
# ---------------------------------------------------------------------------
def _write_web_topic(tmp_path: Path) -> str:
    p = tmp_path / "search_web_topic.json"
    p.write_text(
        json.dumps(
            {
                "status": "ok",
                "search_web_topic": [
                    {
                        "title": "防晒 <选择> 指南",
                        "url": "https://example.com/a",
                        "snippet": "忽略 {{x}} 和 {y}\n第二行摘要",
                        "source": "duckduckgo_html",
                    },
                    {"title": "无链接条目", "source": "duckduckgo_html"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(p)


def test_node_passes_web_research_and_references_from_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """检索产物 → web_research 独立 kwarg + script.json references 数组;overall 不再混入资料。"""
    captured: dict[str, Any] = {}

    def _spy(**kw: Any) -> dict:
        captured.update(kw)
        return {
            "group_scripts": [
                {"group_id": "group_0001", "narration": "n1", "summary": "s1"},
                {"group_id": "group_0002", "narration": "n2", "summary": "s2"},
            ],
            "lang": "zh",
            "llm_trace": {"ok": True},
        }

    monkeypatch.setattr("nodes.storyline.node_generate_script.generate_script", _spy)
    out = storyline_generate_script_node(
        _state(tmp_path, storyline_web_topic_artifact=_write_web_topic(tmp_path))
    )

    assert captured["overall"] == ""  # 无 understanding 产物 → overall 纯空,资料不混入
    ref = captured["web_research"]
    assert ref.startswith("【网页话题参考")
    assert "https://example.com/a" in ref  # URL 进入 prompt
    assert "{" not in ref and "}" not in ref  # 摘要里的模板变量被中和
    assert "无链接条目" in ref  # 资料块收标题(无 URL 免括号);但 references 只收有 URL 条目
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["references"] == [
        {
            "title": "防晒 <选择> 指南",
            "url": "https://example.com/a",
            "source": "duckduckgo_html",
        }
    ]


def test_node_references_empty_and_web_research_empty_without_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """无检索产物 → web_research 空串 + references 空数组,文案照常生成(软降级)。"""
    captured: dict[str, Any] = {}

    def _spy(**kw: Any) -> dict:
        captured.update(kw)
        return {
            "group_scripts": [
                {"group_id": "group_0001", "narration": "n1", "summary": "s1"},
                {"group_id": "group_0002", "narration": "n2", "summary": "s2"},
            ],
            "lang": "zh",
            "llm_trace": {"ok": True},
        }

    monkeypatch.setattr("nodes.storyline.node_generate_script.generate_script", _spy)
    out = storyline_generate_script_node(_state(tmp_path))

    assert captured["web_research"] == ""
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["references"] == []


def test_trace_records_user_prompt_head_with_web_research(
    tmp_path: Path, monkeypatch
) -> None:
    """真实能力层 + stub:script_llm_trace.json 的 user_prompt_head 可见 web_research(URL)。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段"},
                    {"group_id": "group_0002", "raw_text": "第二段"},
                ],
                "title": "标题",
            }
        ),
    )
    out = storyline_generate_script_node(
        _state(tmp_path, storyline_web_topic_artifact=_write_web_topic(tmp_path))
    )
    trace = json.loads(
        (tmp_path / "storyline" / "script_llm_trace.json").read_text(encoding="utf-8")
    )
    assert trace["ok"] is True
    assert "https://example.com/a" in trace.get("user_prompt_head", "")
    assert "【网页话题参考" in trace.get("user_prompt_head", "")


# ---------------------------------------------------------------------------
# 第八期阶段4:标题来源链(设计文档 §6.5)——【标题】 > LLM title > 字幕切句
# ---------------------------------------------------------------------------


def test_node_writes_hook_title_from_llm(tmp_path: Path, monkeypatch) -> None:
    """LLM 返回 title → 能力层带出 → 节点写入 state["hook_title"] + script.json 留痕。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段旁白"},
                    {"group_id": "group_0002", "raw_text": "第二段旁白"},
                ],
                "title": "马六甲之旅",
            }
        ),
    )
    out = storyline_generate_script_node(_state(tmp_path))

    assert out["hook_title"] == "马六甲之旅"
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["title"] == "马六甲之旅"
    assert script["title_source"] == "llm"
    assert script["title_truncated"] is False


def test_node_template_title_beats_llm_title(tmp_path: Path, monkeypatch) -> None:
    """【标题】模板标记 > LLM title(§6.5 优先级)。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段旁白"},
                    {"group_id": "group_0002", "raw_text": "第二段旁白"},
                ],
                "title": "LLM 起的标题",
            }
        ),
    )
    out = storyline_generate_script_node(
        _state(tmp_path, storyline_targets={"user_request": "【标题】用户定制标题"})
    )

    assert out["hook_title"] == "用户定制标题"
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["title"] == "用户定制标题"
    assert script["title_source"] == "template"


def test_node_llm_title_truncated_with_flag(tmp_path: Path, monkeypatch) -> None:
    """LLM 标题超 12 字 → 写入端截断 + title_truncated=true 留痕(P1-05 不静默)。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段旁白"},
                    {"group_id": "group_0002", "raw_text": "第二段旁白"},
                ],
                "title": "马六甲海峡的海上清真寺日落航行记录",  # 16 字
            }
        ),
    )
    out = storyline_generate_script_node(_state(tmp_path))

    assert out["hook_title"] == "马六甲海峡的海上清真寺日落"[:12]
    assert len(out["hook_title"]) == 12
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["title_truncated"] is True
    assert script["title_source"] == "llm"


def test_node_no_titles_writes_no_hook_title(tmp_path: Path, monkeypatch) -> None:
    """两级都空 → 不写 hook_title,回退字幕切句由 node_09b 承担(§6.5)。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段旁白"},
                    {"group_id": "group_0002", "raw_text": "第二段旁白"},
                ],
                "title": "",
            }
        ),
    )
    out = storyline_generate_script_node(_state(tmp_path))

    assert "hook_title" not in out
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["title"] == ""
    assert script["title_source"] == "subtitle_fallback"


def test_node_en_run_skips_llm_title_but_keeps_template_title(
    tmp_path: Path, monkeypatch
) -> None:
    """英文运行:LLM 标题不写入中文标题轨;【标题】为用户显式意图仍生效(§6.5)。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "n1"},
                    {"group_id": "group_0002", "raw_text": "n2"},
                ],
                "title": "English Title Here",
            }
        ),
    )
    # 英文运行 + 无模板标题 → 不写 hook_title
    out = storyline_generate_script_node(
        _state(tmp_path, storyline_targets={"lang": "en"})
    )
    assert "hook_title" not in out

    # 英文运行 + 有【标题】 → 模板标题仍写入
    out2 = storyline_generate_script_node(
        _state(
            tmp_path,
            storyline_targets={"lang": "en", "user_request": "【标题】用户定制标题"},
        )
    )
    assert out2["hook_title"] == "用户定制标题"


def test_node_duplicate_title_directive_fails_closed_to_llm(
    tmp_path: Path, monkeypatch
) -> None:
    """重复【标题】→ 该字段 fail-closed,回退 LLM 标题;诊断只进 script.json。"""
    monkeypatch.setattr(
        "storyline_capabilities.vlm_client._ACTIVE_CLIENT",
        StubLLMClient(
            default_json={
                "group_scripts": [
                    {"group_id": "group_0001", "raw_text": "第一段旁白"},
                    {"group_id": "group_0002", "raw_text": "第二段旁白"},
                ],
                "title": "LLM 起的标题",
            }
        ),
    )
    out = storyline_generate_script_node(
        _state(
            tmp_path,
            storyline_targets={"user_request": "【标题】第一个\n【检索】攻略\n【标题】第二个"},
        )
    )

    assert out["hook_title"] == "LLM 起的标题"  # 模板字段作废 → LLM 兜底
    script = json.loads(Path(out["storyline_script_artifact"]).read_text(encoding="utf-8"))
    assert script["title_source"] == "llm"
    assert script["user_directives_warnings"] == ["duplicate_directive:title"]
    # 解析诊断不进 status_log(「提示词不进监控事件」契约)
    assert not any("duplicate" in tag for tag in out["status_log"])

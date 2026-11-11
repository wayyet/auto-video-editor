"""AI 转场提示词生成单测(文本 LLM 方案 + 确定性兜底)。

覆盖:Stub 后端直接兜底、LLM 成功走 text_llm、LLM 异常/空返回兜底、
兜底提示词包含 FireRed 纪律要素(锁定运镜/过渡介质/反幻觉后缀)。
"""
from __future__ import annotations

import pytest

from storyline_capabilities.ai_transition_prompt import (
    PROMPT_METHOD_FALLBACK,
    PROMPT_METHOD_LLM,
    compose_transition_prompt,
    deterministic_fallback_prompt,
)
from storyline_capabilities.vlm_client import StubLLMClient, set_default_client


@pytest.fixture(autouse=True)
def _stub_default_client():
    set_default_client(StubLLMClient())
    yield
    set_default_client(None)


def _clips() -> tuple[dict, dict]:
    return (
        {"clip_id": "c1", "captions": ["夜晚的街道", "车流"], "summary": "街景收尾"},
        {"clip_id": "c2", "captions": ["清晨的咖啡馆"], "summary": "咖啡馆开场"},
    )


def test_stub_backend_falls_back_deterministically() -> None:
    prompt, method = compose_transition_prompt(from_clip=_clips()[0], to_clip=_clips()[1])
    assert method == PROMPT_METHOD_FALLBACK
    assert prompt.startswith("Smooth continuous single shot")
    assert "motion blur" in prompt or "light flare" in prompt
    assert "No text" in prompt


def test_llm_success_uses_text_llm_method() -> None:
    class _LLM:
        def chat(self, **kw: object) -> str:
            assert "夜晚的街道" in str(kw.get("user_prompt"))
            assert "Smooth continuous single shot" in str(kw.get("system_prompt"))
            return "Smooth continuous single shot. Slow push-in with a light flare wipe."

    prompt, method = compose_transition_prompt(
        from_clip=_clips()[0], to_clip=_clips()[1], client=_LLM()
    )
    assert method == PROMPT_METHOD_LLM
    assert prompt.startswith("Smooth continuous single shot")


def test_llm_exception_falls_back() -> None:
    class _Boom:
        def chat(self, **kw: object) -> str:
            raise RuntimeError("后端挂了")

    prompt, method = compose_transition_prompt(
        from_clip=_clips()[0], to_clip=_clips()[1], client=_Boom()
    )
    assert method == PROMPT_METHOD_FALLBACK
    assert prompt


def test_llm_empty_return_falls_back() -> None:
    class _Empty:
        def chat(self, **kw: object) -> str:
            return "   "

    _, method = compose_transition_prompt(
        from_clip=_clips()[0], to_clip=_clips()[1], client=_Empty()
    )
    assert method == PROMPT_METHOD_FALLBACK


def test_fallback_uses_clip_briefs() -> None:
    prompt = deterministic_fallback_prompt("街景收尾", "咖啡馆开场")
    assert "街景收尾" in prompt and "咖啡馆开场" in prompt


def test_empty_briefs_still_produce_valid_prompt() -> None:
    prompt, method = compose_transition_prompt(from_clip={}, to_clip={})
    assert method == PROMPT_METHOD_FALLBACK
    assert prompt.startswith("Smooth continuous single shot")

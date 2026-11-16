"""jy_common/translate_client 单测 — LLMTranslateClient 适配器(2026-11-17 P1)。

覆盖修订计划 §8.4 场景:正常输出 / index 错位与缺重段 / 空译文 / 非法 JSON /
异常重试后失败 / 时间码继承 / 分批 / translator 身份 / 失败 trace 不含密钥。
"""

from __future__ import annotations

import json

import pytest

from jy_common.translate_client import (
    TRANSLATION_PROMPT_VERSION,
    LLMTranslateClient,
    MockTranslateClient,
    TranslationError,
    translate_segments,
    set_default_client,
    get_default_client,
)


def _segs(*texts: str) -> list[dict]:
    return [
        {"index": i, "start_ms": i * 2_000, "end_ms": i * 2_000 + 1_500, "text_zh": t}
        for i, t in enumerate(texts)
    ]


class _FakeLLM:
    """可编程假 LLM:按注册的响应序列返回,可抛异常。"""

    def __init__(self, responses: list[str], *, raise_on: set[int] | None = None) -> None:
        self._responses = list(responses)
        self._raise_on = raise_on or set()
        self.calls = 0
        self.last_payload = None

    def chat(self, *, system_prompt, user_prompt, media=None, json_schema=None,
             temperature=0.3, top_p=0.9, max_tokens=4096):
        self.calls += 1
        self.last_payload = user_prompt
        if self.calls in self._raise_on:
            raise RuntimeError("transport boom")
        return self._responses[min(self.calls, len(self._responses)) - 1]


def _ok_response(rows: list[dict]) -> str:
    return json.dumps({"translations": rows}, ensure_ascii=False)


def _rows_for(segs: list[dict], render) -> list[dict]:
    return [{"index": s["index"], "text_en": render(s)} for s in segs]


# ---------------------------------------------------------------------------
# 正常路径与对齐
# ---------------------------------------------------------------------------
def test_happy_path_returns_index_aligned_rows() -> None:
    llm = _FakeLLM([_ok_response(_rows_for(_segs("你好", "世界"), lambda s: f"Hi {s['text_zh']}"))])
    out = LLMTranslateClient(llm).translate(_segs("你好", "世界"))
    assert [r["text_en"] for r in out] == ["Hi 你好", "Hi 世界"]
    assert [r["index"] for r in out] == [0, 1]


def test_output_order_follows_source_not_model() -> None:
    """模型乱序返回 → 结果仍按源段顺序对齐,不错位。"""
    llm = _FakeLLM([_ok_response([
        {"index": 1, "text_en": "World"},
        {"index": 0, "text_en": "Hello"},
    ])])
    out = LLMTranslateClient(llm).translate(_segs("你好", "世界"))
    assert [r["text_en"] for r in out] == ["Hello", "World"]


def test_timecodes_inherited_from_source() -> None:
    """start_ms/end_ms 恒从源段继承,不采信模型返回的时间。"""
    llm = _FakeLLM([_ok_response([
        {"index": 0, "text_en": "Hello", "start_ms": 999, "end_ms": 9999},
    ])])
    out = LLMTranslateClient(llm).translate(_segs("你好"))
    assert out[0]["start_ms"] == 0 and out[0]["end_ms"] == 1_500


# ---------------------------------------------------------------------------
# 输出契约校验(缺/重/多/空)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rows, why",
    [
        ([{"index": 0, "text_en": "A"}], "缺段"),
        (
            [
                {"index": 0, "text_en": "A"},
                {"index": 0, "text_en": "A2"},
                {"index": 1, "text_en": "B"},
            ],
            "重段",
        ),
        (
            [
                {"index": 0, "text_en": "A"},
                {"index": 1, "text_en": "B"},
                {"index": 2, "text_en": "C"},
            ],
            "多余段",
        ),
        ([{"index": 0, "text_en": "  "}], "空译文"),
        ([{"index": "0", "text_en": "A"}], "index 非整数"),
        ([{"index": 0}], "缺 text_en"),
    ],
)
def test_contract_violations_retry_then_raise(rows, why) -> None:
    llm = _FakeLLM([_ok_response(rows), _ok_response(rows)])
    with pytest.raises(TranslationError, match="重试 1 次后仍失败"):
        LLMTranslateClient(llm).translate(_segs("你好", "世界"))
    assert llm.calls == 2  # 恰好重试一次


def test_invalid_json_and_exception_retry_then_raise() -> None:
    llm = _FakeLLM(["not-json", "still-not-json"])
    with pytest.raises(TranslationError):
        LLMTranslateClient(llm).translate(_segs("你好"))

    llm2 = _FakeLLM(["irrelevant"], raise_on={1, 2})
    with pytest.raises(TranslationError, match="重试 1 次后仍失败") as exc_info:
        LLMTranslateClient(llm2).translate(_segs("你好"))
    # 传输层异常细节在 trace.error 里(不含密钥,含原始异常文本)
    assert "transport boom" in str(exc_info.value.trace.get("error", ""))


def test_translation_error_carries_trace_without_retry_on_success() -> None:
    """首轮即成功 → 不重试;失败抛出的异常带 trace 排障字段。"""
    llm = _FakeLLM([_ok_response([{"index": 0, "text_en": "Hi"}])])
    out = LLMTranslateClient(llm).translate(_segs("你好"))
    assert llm.calls == 1 and out[0]["text_en"] == "Hi"

    llm2 = _FakeLLM(["bad"])
    with pytest.raises(TranslationError) as exc_info:
        LLMTranslateClient(llm2).translate(_segs("你好"))
    assert "parsed" in exc_info.value.trace


# ---------------------------------------------------------------------------
# 分批
# ---------------------------------------------------------------------------
def test_batching_by_char_budget() -> None:
    """超过字符预算切段分批;每批独立校验,批间按原始 index 对齐。"""
    segs = _segs(*[f"第{i}句" * 20 for i in range(6)])
    llm = _FakeLLM([
        _ok_response(_rows_for(segs[:2], lambda s: f"E{s['index']}")),
        _ok_response(_rows_for(segs[2:4], lambda s: f"E{s['index']}")),
        _ok_response(_rows_for(segs[4:], lambda s: f"E{s['index']}")),
    ])
    client = LLMTranslateClient(llm, batch_char_budget=250)
    out = client.translate(segs)
    assert llm.calls == 3
    assert [r["index"] for r in out] == list(range(6))
    assert all(r["text_en"].startswith("E") for r in out)


def test_one_batch_failure_fails_whole_translation() -> None:
    """任一批失败 → 整体抛错,不把未翻译段当成功。"""
    segs = _segs("甲" * 200, "乙" * 200, "丙" * 200)
    ok_rows = [{"index": 0, "text_en": "A"}]
    bad_rows = [{"index": 2, "text_en": "C"}]  # 缺 index 1
    llm = _FakeLLM([
        _ok_response(ok_rows), _ok_response(bad_rows), _ok_response(bad_rows),
    ])
    client = LLMTranslateClient(llm, batch_char_budget=400)
    with pytest.raises(TranslationError):
        client.translate(segs)


# ---------------------------------------------------------------------------
# 身份与默认客户端
# ---------------------------------------------------------------------------
def test_translator_identity_defaults_and_override() -> None:
    llm = _FakeLLM([_ok_response([{"index": 0, "text_en": "Hi"}])])
    client = LLMTranslateClient(llm)
    assert client.translator_id == "_FakeLLM"
    assert client.prompt_version == TRANSLATION_PROMPT_VERSION

    client2 = LLMTranslateClient(llm, translator_id="mcode-cli")
    assert client2.translator_id == "mcode-cli"


def test_empty_segments_returns_empty() -> None:
    llm = _FakeLLM([])
    assert LLMTranslateClient(llm).translate([]) == []
    assert llm.calls == 0


def test_mock_client_unchanged_and_default_restored() -> None:
    """Mock 桩行为不变;默认客户端显式还原(防用例污染)。"""
    out = MockTranslateClient().translate(_segs("你好"))
    assert out[0]["text_en"] == "[EN] 你好"
    assert isinstance(get_default_client(), MockTranslateClient)
    assert translate_segments(_segs("你好"))[0]["text_en"].startswith("[EN] ")
    set_default_client(MockTranslateClient())  # 显式还原

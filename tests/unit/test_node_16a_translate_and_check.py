"""node_16a_translate_and_check 单测 — 英文字幕翻译节点(第六期改版)。

覆盖:
- marker(schema v2 指纹)已存在 → 跳过翻译,只读草稿
- 旧 list marker(legacy mock)→ 一律重翻,不把 [EN] 假翻译当命中
- 指纹/翻译器/提示词任一变化 → marker 失效重翻
- marker 不存在 → 调翻译 + 写 marker + 写 state + 写 SRT
- 翻译状态三元组:stub(默认 Mock)/ real(LLMTranslateClient)写入 state
- 真实翻译失败(TranslationError)→ fail-closed 上抛,marker 不落盘
- subtitle_segments_zh(兼容 asr_segments_zh)缺/空 → 正常跳过
  (skipped_no_source_subtitles,非错误)
- draft_dir_en_branch 缺失 → 跳过(error_log)
- 翻译源切换:subtitle_segments_zh 优先于 asr_segments_zh
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.translate_client import (
    TRANSLATION_PROMPT_VERSION,
    LLMTranslateClient,
    MockTranslateClient,
    TranslationError,
    set_default_client,
)
from nodes.node_16_translate_subtitles import _compute_source_fingerprint
from nodes.node_16a_translate_and_check import node_16a_translate_and_check


@pytest.fixture(autouse=True)
def _no_jianying_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件多个用例会让节点**真实写草稿**(safe_write_draft):剪映客户端
    开着时写入互斥严格模式直接 raise,造成环境性失败(2026-11-08 全量回归
    实测 3 例)。翻译/写盘用例验证的是翻译与草稿契约,互斥行为由
    test_safe_write_guard 单独覆盖 —— 这里统一注入"剪映不在跑"的桩,
    写入路径(快照/双写/verify)仍真实执行。"""
    import draft_ops.atomic_writer as _aw

    monkeypatch.setattr(_aw, "_default_proc_checker", lambda: False)


@pytest.fixture(autouse=True)
def _restore_default_client():
    """用例改写全局默认翻译客户端后必须还原,避免用例间相互污染。"""
    yield
    set_default_client(MockTranslateClient())


def _seed_en_branch(tmp_path: Path) -> Path:
    draft_dir = tmp_path / "en_branch"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                {"content": "你好", "target_timerange": {"start": 0, "duration": 2_000_000}},
                {"content": "世界", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_dir


def _state(asr_segs: list[dict], draft_dir: Path) -> dict:
    return {
        "asr_segments_zh": asr_segs,
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }


def _normal_asr() -> list[dict]:
    return [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
    ]


def test_node_16a_happy_path_writes_state_and_srt(tmp_path: Path) -> None:
    """正常路径:翻译 + 写 marker + 写 state + 写 SRT;默认 Mock → status=stub。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)

    # state 字段应有值
    assert out.get("subtitle_segments_en")
    assert "node_16a_translate_done" in out["status_log"]
    # 翻译状态三元组:默认 Mock = stub,不冒充真实翻译
    assert out["subtitle_translation_status"] == "stub"
    assert out["subtitle_translator_id"] == "mock"
    assert out["subtitle_translation_fingerprint"]
    # 不应触发 interrupt(payload=None 不应存在)
    assert "interrupt" not in out
    # SRT 正常生成
    srt_path = out.get("subtitle_srt_path")
    assert srt_path
    assert Path(srt_path).exists()
    # marker 落盘为 v2 dict(含指纹与身份)
    marker = json.loads((draft_dir / "subtitle_en.json").read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert marker["translator_id"] == "mock"
    assert marker["source_fingerprint"] == out["subtitle_translation_fingerprint"]


def _write_v2_marker(draft_dir: Path, segments_zh: list[dict], texts: list[str]) -> None:
    """按当前 mock 身份写一枚 v2 marker(命中用)。"""
    fingerprint = _compute_source_fingerprint(
        segments_zh, translator_id="mock", prompt_version=TRANSLATION_PROMPT_VERSION
    )
    payload = {
        "schema_version": 2,
        "target_language": "en",
        "source_fingerprint": fingerprint,
        "translator_id": "mock",
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "segments": [
            {"index": seg["index"], "start_ms": seg["start_ms"],
             "end_ms": seg["end_ms"], "text_en": texts[seg["index"]]}
            for seg in segments_zh
        ],
    }
    (draft_dir / "subtitle_en.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_node_16a_marker_exists_skips_translate(tmp_path: Path) -> None:
    """v2 marker 全维度命中 → 不调翻译客户端,直接读草稿。"""
    draft_dir = _seed_en_branch(tmp_path)
    _write_v2_marker(
        draft_dir, _normal_asr(), ["PreExisting", "PreExisting2"]
    )

    class _ShouldNotCall:
        def translate(self, segments):
            raise AssertionError("marker 存在时不应调翻译")

    set_default_client(_ShouldNotCall())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)
    assert out.get("subtitle_segments_en")
    assert out["subtitle_translation_status"] == "stub"


def test_node_16a_legacy_list_marker_retranslates(tmp_path: Path) -> None:
    """旧 list marker(legacy mock)→ 不命中,重翻;旧假翻译不复用。"""
    draft_dir = _seed_en_branch(tmp_path)
    legacy = [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_en": "[EN] 你好"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_en": "[EN] 世界"},
    ]
    (draft_dir / "subtitle_en.json").write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    calls: list[list[dict]] = []

    class _SpyClient:
        def translate(self, segments):
            calls.append(list(segments))
            return [
                {"index": s["index"], "start_ms": s["start_ms"],
                 "end_ms": s["end_ms"], "text_en": f"Real {s['text_zh']}"}
                for s in segments
            ]

    set_default_client(_SpyClient())
    out = node_16a_translate_and_check(_state(_normal_asr(), draft_dir))

    assert calls, "legacy marker 必须重翻"
    assert [s["text_en"] for s in out["subtitle_segments_en"]] == [
        "Real 你好", "Real 世界"
    ]
    marker = json.loads((draft_dir / "subtitle_en.json").read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2


def test_node_16a_source_change_invalidates_marker(tmp_path: Path) -> None:
    """同段数但源文本变化 → 指纹不匹配,重翻(修订计划 §5.5)。"""
    draft_dir = _seed_en_branch(tmp_path)
    _write_v2_marker(draft_dir, _normal_asr(), ["Stale A", "Stale B"])

    calls: list[list[dict]] = []

    class _SpyClient:
        def translate(self, segments):
            calls.append(list(segments))
            return [
                {"index": s["index"], "start_ms": s["start_ms"],
                 "end_ms": s["end_ms"], "text_en": f"Fresh {s['text_zh']}"}
                for s in segments
            ]

    set_default_client(_SpyClient())
    changed = [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好呀"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
    ]
    out = node_16a_translate_and_check(_state(changed, draft_dir))

    assert calls, "源文本变化必须重翻"
    assert out["subtitle_segments_en"][0]["text_en"] == "Fresh 你好呀"


def test_node_16a_real_client_reports_real_status(tmp_path: Path) -> None:
    """LLMTranslateClient(假 LLM)→ status=real,translator_id 透传。"""
    draft_dir = _seed_en_branch(tmp_path)

    class _FakeLLM:
        def chat(self, *, system_prompt, user_prompt, media=None, json_schema=None,
                 temperature=0.3, top_p=0.9, max_tokens=4096):
            payload = json.loads(user_prompt)
            return json.dumps(
                {
                    "translations": [
                        {"index": s["index"], "text_en": f"EN<{s['text_zh']}>"}
                        for s in payload["segments"]
                    ]
                },
                ensure_ascii=False,
            )

    set_default_client(LLMTranslateClient(_FakeLLM(), translator_id="mcode-cli"))
    out = node_16a_translate_and_check(_state(_normal_asr(), draft_dir))

    assert out["subtitle_translation_status"] == "real"
    assert out["subtitle_translator_id"] == "mcode-cli"
    assert [s["text_en"] for s in out["subtitle_segments_en"]] == [
        "EN<你好>", "EN<世界>"
    ]
    # 时间码恒从源段继承
    assert [s["start_ms"] for s in out["subtitle_segments_en"]] == [0, 2_000]


def test_node_16a_translation_failure_fails_closed(tmp_path: Path) -> None:
    """真实翻译失败(重试后仍不符)→ TranslationError 上抛,marker 不落盘;
    trace 取证落盘 diagnostics/(2026-11-20 形状漂移事故的排障证据)。"""
    draft_dir = _seed_en_branch(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()

    class _BadLLM:
        def chat(self, **kwargs):
            return "not-json-at-all"

    set_default_client(LLMTranslateClient(_BadLLM(), max_retries=1))
    state = _state(_normal_asr(), draft_dir)
    state["storyline_outputs_root"] = str(outputs_root)
    with pytest.raises(TranslationError):
        node_16a_translate_and_check(state)

    assert not (draft_dir / "subtitle_en.json").exists()

    # 取证文件存在且带关键证据字段:raw_head = 模型原始输出(兜底文案
    # "缺少 translations 数组"判定的空 dict 会把它整个吞掉)
    dumps = list((outputs_root / "diagnostics").glob("translation-failure-*.json"))
    assert len(dumps) == 1
    evidence = json.loads(dumps[0].read_text(encoding="utf-8"))
    assert evidence["node"] == "node_16a_translate_and_check"
    assert evidence["segments_count"] == 2
    assert evidence["translator_id"] == "_BadLLM"
    assert evidence["trace"]["parsed"] is False
    assert "not-json-at-all" in evidence["trace"]["raw_head"]


def test_node_16a_translation_failure_without_outputs_root_still_raises(
    tmp_path: Path,
) -> None:
    """storyline_outputs_root 缺失 → 取证静默跳过,异常照常上抛(不掩盖错误)。"""
    draft_dir = _seed_en_branch(tmp_path)

    class _BadLLM:
        def chat(self, **kwargs):
            return "not-json-at-all"

    set_default_client(LLMTranslateClient(_BadLLM(), max_retries=1))
    with pytest.raises(TranslationError):
        node_16a_translate_and_check(_state(_normal_asr(), draft_dir))


def test_node_16a_no_asr_segments_skips(tmp_path: Path) -> None:
    """asr_segments_zh 缺失/为空 → 正常跳过(无源字幕是预期场景),不写 error_log。

    B1 ASR 移除后这是英文分支的常态输入:状态用 skipped_no_source_subtitles,
    不再算错误,也不得阻断后续 node_17(2026-11-15 关卡③删除后 10b 恒直连 17)。
    """
    state = {
        "draft_dir_en_branch": str(tmp_path),
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "skipped_no_source_subtitles" in out["status_log"]
    assert not out.get("error_log")
    assert "node_16a_translate_skipped" not in out["status_log"]


def test_node_16a_no_draft_dir_skips() -> None:
    """draft_dir_en_branch 缺失 → 跳过。"""
    state = {
        "asr_segments_zh": _normal_asr(),
        "draft_dir_en_branch": None,
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "node_16a_translate_skipped" in out["status_log"]
    assert any("draft_dir_en_branch" in e for e in out["error_log"])


# ---------------------------------------------------------------------------
# 阶段 2(2026-11-09 效果缺失修复):翻译源切换到 subtitle_segments_zh
# ---------------------------------------------------------------------------
def test_node_16a_translates_subtitle_segments_zh(tmp_path: Path) -> None:
    """阶段 2:翻译源 = node_08 产出的 subtitle_segments_zh(优先于已废弃的
    asr_segments_zh)。B1 后 ASR 段恒空,英文链(16a→08b→10b→17)靠本字段复活。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = {
        "subtitle_segments_zh": _normal_asr(),  # 同形状:node_08 复用 SubtitleSegment
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)

    assert "skipped_no_source_subtitles" not in out["status_log"]
    assert "node_16a_translate_done" in out["status_log"]
    segs_en = out.get("subtitle_segments_en") or []
    assert [s["start_ms"] for s in segs_en] == [0, 2_000]
    assert all(s.get("text_en") for s in segs_en)

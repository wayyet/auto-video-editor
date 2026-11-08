"""TTS Provider Contract 契约测试(冗余模块合并计划 阶段 4)。

两部分(计划 §6.6 任务 4.1/4.3):

1. **契约自身一致性**——用 Stub(落盘)与 Mock(内存)两种实现验证
   ``jy_common.provider_contracts`` 的协议签名与结果对象语义;
2. **现有适配器的形状锚定**——Storyline Stub / ToolResult 适配器的现有签名
   与返回形状逐项钉死(OpenStoryline MCP Node 属独立运行时,由
   ``tests_openstoryline`` 覆盖,不在主 venv 测试范围)。

(2026-11-08 B1 ASR 移除:jy_common.asr_client / storyline_capabilities.asr_runner
已删除,``ASRResult`` / ``ASRProvider`` 契约与全部 ASR 侧用例一并移除;
TTS 侧锚定保留。)
"""

from __future__ import annotations

import inspect
from pathlib import Path

from jy_common.provider_contracts import (
    TTSProvider,
    TTSResult,
)

# ---------------------------------------------------------------------------
# 1a. 契约签名(草案即契约:kw-only 参数集合逐字钉死)
# ---------------------------------------------------------------------------
def test_tts_provider_protocol_signature_is_kw_only() -> None:
    params = inspect.signature(TTSProvider.synthesize).parameters
    assert list(params) == ["self", "text", "output_path", "voice", "speed"]
    for name in ("text", "output_path", "voice", "speed"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# 1b. 结果对象语义
# ---------------------------------------------------------------------------
def test_tts_result_duration_unit_is_milliseconds() -> None:
    """duration_ms 语义为毫秒:1.5 秒音频必须写成 1500,不是 1.5。"""
    result = TTSResult(
        audio_path=Path("out/tts.wav"), duration_ms=1500, provider="stub"
    )
    assert isinstance(result.duration_ms, int) and result.duration_ms == 1500
    assert result.error is None


# ---------------------------------------------------------------------------
# 1c. Stub 实现(落盘)— 证明契约可被自然实现
# ---------------------------------------------------------------------------
class _StubTTSProvider:
    """契约 Stub:在 output_path 落一个占位 wav 并返回 TTSResult。"""

    def synthesize(
        self, *, text: str, output_path: Path, voice: str, speed: float
    ) -> TTSResult:
        Path(output_path).write_bytes(b"RIFF....WAVE")
        return TTSResult(
            audio_path=Path(output_path),
            duration_ms=max(500, len(text) * 250),
            provider="stub",
        )


def test_stub_tts_provider_satisfies_contract(tmp_path: Path) -> None:
    stub = _StubTTSProvider()
    assert isinstance(stub, TTSProvider)
    result = stub.synthesize(
        text="四字文本", output_path=tmp_path / "a.wav", voice="zh_female", speed=1.0
    )
    assert result.audio_path == tmp_path / "a.wav"
    assert result.duration_ms == 1000  # 4 字 × 250ms
    assert result.error is None


# ---------------------------------------------------------------------------
# 2. 现有适配器的形状锚定(现状 ↔ 契约映射的前置事实)
# ---------------------------------------------------------------------------
def test_http_client_category_shape_jy_common() -> None:
    """HTTP 客户端(jy_common):synthesize → dict。

    (ASR 侧 ``transcribe(video_path) → list[dict] 秒`` 已随 2026-11-08 B1 ASR
    移除删除,只剩 TTS 侧锚定。)
    """
    from jy_common.tts_client import MockTTSClient

    # TTS:返回 dict 键集合固定
    tts = MockTTSClient().synthesize(text="你好", voice="zh", speed=1.0)
    assert set(tts) == {"audio_path", "duration_ms", "voice", "speed"}


def test_storyline_stub_category_shape(tmp_path: Path) -> None:
    """Storyline runner Stub:kw-only (text, wav_path, params) → TTSResult。"""
    from storyline_capabilities.tts_runner import StubTTSClient

    tts_params = inspect.signature(StubTTSClient.synthesize).parameters
    assert list(tts_params) == ["self", "text", "wav_path", "params"]
    tts = StubTTSClient().synthesize(
        text="你好世界", wav_path=tmp_path / "a.wav", params={"voice": "v"}
    )
    assert tts.provider == "stub" and tts.error is None
    assert tts.duration_ms > 0 and tts.params_used == {"voice": "v"}
    assert (tmp_path / "a.wav").is_file()


def test_toolresult_adapter_category_shape() -> None:
    """ToolResult 适配器(assembly/video_edit):位置参 (args: dict, ctx) → ToolResult。

    (B1 ASR 移除,2026-11-08:assembly 侧锚定对象从 speech_transcribe
    换成仍存活的 video_ingest。)
    """
    from assembly_capabilities.result import ToolResult
    from assembly_capabilities.visual_observe import video_ingest
    from video_edit_capabilities.speech_synthesize import speech_synthesize

    for fn in (video_ingest, speech_synthesize):
        params = inspect.signature(fn).parameters
        assert list(params) == ["args", "ctx"]
        ann = params["args"].annotation
        assert "dict" in str(ann)
    assert ToolResult is not None  # 返回类型即 ToolResult(text, data, artifacts)


def test_openstoryline_mcp_node_out_of_main_venv_scope() -> None:
    """MCP Node 类适配器属 OpenStoryline 独立运行时(ADR-001),主 venv 契约测试不 import。

    映射表已记录其 NodeState/MCP 会话输入输出;行为回归由 tests_openstoryline 覆盖。
    本测试只固化"不在主 venv 引入该依赖"的边界:pyproject 不得新增
    torch / torchaudio / moviepy(计划 G-A8)。
    """
    import tomllib

    pyproject = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    )
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = str(data.get("project", {}).get("dependencies", []))
    for heavy in ("torch", "torchaudio", "moviepy"):
        assert heavy not in deps, f"主项目不得引入 {heavy}(ADR-001 环境隔离)"

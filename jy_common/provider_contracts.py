"""ASR/TTS Provider Contract 草案(冗余模块合并计划 阶段 4)。

**这是目标接口草案,不是新的运行时。**当前四类适配器——jy_common HTTP 客户端、
``storyline_capabilities`` 子进程/Stub runner、``assembly_capabilities`` /
``video_edit_capabilities`` 的 ToolResult 适配器、OpenStoryline MCP Node——
各自保留,本批次不做任何运行时合并(计划 §6.6 任务 4.2/4.4;ADR-001 环境隔离
继续有效)。每个现有适配器与本契约的字段映射、单位差异(秒/毫秒)与错误策略
差异(异常 vs error 字段)见:

``docs/修改文档/auto-video-editor_ASR_TTS_ProviderContract草案与字段映射_20261103.md``

本模块只定义契约本身,供后续迁移与契约测试(``tests/unit/test_provider_contracts.py``)
引用;不 import 任何现有适配器,避免制造新的耦合。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class ASRResult:
    """统一 ASR 转写结果(草案)。

    Attributes:
        artifact_path: 转写 JSON 产物路径;失败且无产物时为 ``None``
            (storyline 现状恒为 ``Path``,迁移时保留原值即可)。
        segments: 转写段列表。字段迁移规则见映射表:
            ``jy_common`` 段为 ``{text, start_s, end_s}``(秒),
            契约统一为 ``{text, start_ms, end_ms}``(毫秒)。
        provider: 实际使用的提供方标识(如 ``"firered"`` / ``"vendored"`` /
            ``"stub"`` / ``"external"``)。
        error: 失败描述,成功为 ``None``。契约用 error 字段表达失败,
            不抛异常(调用方统一降级);现用异常的适配器
            (``ASRUnavailable``)迁移时改为填充本字段。
        duration_ms: 转写耗时(毫秒)。**不是**媒体时长。
    """

    artifact_path: Path | None
    segments: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    error: str | None = None
    duration_ms: int = 0


@runtime_checkable
class ASRProvider(Protocol):
    """统一 ASR 提供方协议(草案)。

    与 ``storyline_capabilities.asr_runner.ASRClient`` 的现有签名一致
    (kw-only ``media_path`` / ``output_dir``,返回结果对象);差异仅在
    ``artifact_path`` 允许 ``None``。
    """

    def transcribe(
        self,
        *,
        media_path: Path,
        output_dir: Path,
    ) -> ASRResult: ...


@dataclass
class TTSResult:
    """统一 TTS 合成结果(草案)。

    Attributes:
        audio_path: 音频产物路径(现有实现的 ``wav_path`` / ``audio_path``
            键统一到本字段);失败且无产物时为 ``None``。
        duration_ms: 音频实际时长(**毫秒**;现有实现即毫秒,迁移不变)。
        provider: 实际使用的提供方(``"firered"`` / ``"minimax"`` /
            ``"stub"`` / ``"error"`` 表示失败)。
        error: 失败描述,成功为 ``None``。
    """

    audio_path: Path | None
    duration_ms: int
    provider: str
    error: str | None = None


@runtime_checkable
class TTSProvider(Protocol):
    """统一 TTS 提供方协议(草案)。

    参数为扁平签名(kw-only ``text`` / ``output_path`` / ``voice`` / ``speed``);
    storyline 现有签名为 ``(text, wav_path, params: dict)``,迁移时由适配层把
    ``wav_path`` → ``output_path``、``params["voice"]/params["speed"]`` 展开
    (见映射表)。
    """

    def synthesize(
        self,
        *,
        text: str,
        output_path: Path,
        voice: str,
        speed: float,
    ) -> TTSResult: ...


__all__ = [
    "ASRResult",
    "ASRProvider",
    "TTSResult",
    "TTSProvider",
]

"""TTS Provider Contract 草案(冗余模块合并计划 阶段 4)。

**这是目标接口草案,不是新的运行时。**当前适配器——jy_common HTTP 客户端、
``storyline_capabilities`` Stub runner、``assembly_capabilities`` /
``video_edit_capabilities`` 的 ToolResult 适配器、OpenStoryline MCP Node——
各自保留,不做运行时合并。字段映射与错误策略差异见:

``docs/修改文档/auto-video-editor_ASR_TTS_ProviderContract草案与字段映射_20261103.md``

本模块只定义契约本身,供契约测试(``tests/unit/test_provider_contracts.py``)
引用;不 import 任何现有适配器,避免制造新的耦合。

(B1 ASR 移除,2026-11-08)``ASRResult`` / ``ASRProvider`` 草案已随 ASR 能力
整体删除;TTS 侧契约保留(node_17 / storyline_generate_voiceover 仍在用 TTS)。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


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
    "TTSResult",
    "TTSProvider",
]

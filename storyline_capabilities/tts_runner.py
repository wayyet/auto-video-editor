"""TTS 调用骨架(plan_v4 §5 阶段 4)。

设计纪律(plan §5 阶段 4 决策):
1. **API Key 不进本模块**:真实 Key 走主项目 `.env`(``TTS_<PROVIDER>_*``),
   这里只读 ``config.STORYLINE_TTS_PROVIDER`` 与 ``tts_ref.yaml`` 描述。
2. **Protocol 抽象**:`TTSClient` Protocol 让 capability 函数不绑死供应商。
   `StubTTSClient` 默认实现(无 Key 或测试用):写最小无声 wav,时长按字符数估算。
3. **永不抛**(plan §4.4):失败时返回 ``(wav_path, duration_ms, error_str)``,
   由 `generate_voiceover` 把 error_str append 到 error_log。
4. **失败兜底**:API Key 缺失 / 网络失败 → 走 stub,允许 auto-mode 端到端跑通
   但下游要能识别(``duration_ms`` 极短 + error_log 记录)。

本模块**不**直接 ``requests.post`` 调真实供应商 — 真实调用留给后续接入的
`RealTTSClient`(对接 minimax / bytedance / 302 的 HTTP 协议);
单元测试用 `StubTTSClient`,CI 烟测也是 stub。
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class TTSClient(Protocol):
    """统一 TTS 调用入口(plan §5 阶段 4 Protocol 抽象)。"""

    def synthesize(
        self,
        *,
        text: str,
        wav_path: Path,
        params: dict[str, Any],
    ) -> "TTSResult":
        """合成一段文本 → 写 wav 到 ``wav_path``,返回 ``TTSResult``。"""
        ...


@dataclass
class TTSResult:
    """TTS 合成结果。

    Attributes:
        wav_path: wav 文件实际路径(可能与入参相同)。
        duration_ms: 实际时长(由 wav header 或 wav 内音频长度计算)。
        provider: 实际使用的供应商(``minimax`` / ``bytedance`` / ``302`` /
                       ``error`` 表示失败)。
        error: 错误描述(成功时为 ``None``)。
        params_used: 实际使用的 TTS 参数(dict,便于日志)。
    """

    wav_path: Path
    duration_ms: int
    provider: str
    error: Optional[str] = None
    params_used: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# 默认 Stub 实现 — plan §5 阶段 4 决策 5 "API Key 缺失时 stub 写最小 wav"
# ---------------------------------------------------------------------------
# 中文常用语速 ≈ 4 字/秒,英文 ≈ 12 词/秒;取 4 chars/sec 作为估算基准
# (UI 草稿够用,真实 TTS 由 RealTTSClient 替换时覆盖)。
_STUB_CHARS_PER_SECOND: float = 4.0
_STUB_MIN_DURATION_MS: int = 500        # 0.5s 静音占位
_STUB_SAMPLE_RATE: int = 16_000
_STUB_CHANNELS: int = 1
_STUB_SAMPLE_WIDTH: int = 2             # 16-bit PCM


def _estimate_text_duration_ms(text: str) -> int:
    """字符数 → 时长估算(中文 / 英文 / 标点都按 1 字符算)。"""
    n = max(1, len((text or "").strip()))
    est_ms = int(round(n / _STUB_CHARS_PER_SECOND * 1000.0))
    return max(_STUB_MIN_DURATION_MS, est_ms)


def _write_silent_wav(path: Path, duration_ms: int) -> None:
    """写一段静音 PCM wav(16-bit / 16kHz / mono)。

    最小 wav header + 全零采样,文件大小 = 44 byte header + 32000 byte/sec × dur。
    """
    n_samples = max(int(_STUB_SAMPLE_RATE * duration_ms / 1000), 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(_STUB_CHANNELS)
        wf.setsampwidth(_STUB_SAMPLE_WIDTH)
        wf.setframerate(_STUB_SAMPLE_RATE)
        wf.writeframes(b"\x00\x00" * n_samples)


class StubTTSClient:
    """永远返回写好的静音 wav + 时长估算 — 用于 auto-mode CI 烟测。

    不读 API Key,不调网络,不对 yaml 依赖;在没有任何配置时仍能工作。
    """

    def synthesize(
        self,
        *,
        text: str,
        wav_path: Path,
        params: dict[str, Any],
    ) -> TTSResult:
        duration_ms = _estimate_text_duration_ms(text)
        try:
            _write_silent_wav(Path(wav_path), duration_ms)
        except OSError as e:
            return TTSResult(
                wav_path=Path(wav_path),
                duration_ms=0,
                provider="stub",
                error=f"wav write failed: {e!r}",
                params_used=dict(params or {}),
            )
        return TTSResult(
            wav_path=Path(wav_path),
            duration_ms=duration_ms,
            provider="stub",
            params_used=dict(params or {}),
        )


# ---------------------------------------------------------------------------
# 默认 client 单例(plan §5 阶段 2 LLMClient 同模式)
# ---------------------------------------------------------------------------
_ACTIVE_TTS_CLIENT: Optional[TTSClient] = None


def get_default_tts_client() -> TTSClient:
    global _ACTIVE_TTS_CLIENT
    if _ACTIVE_TTS_CLIENT is None:
        _ACTIVE_TTS_CLIENT = StubTTSClient()
    return _ACTIVE_TTS_CLIENT


def set_default_tts_client(client: Optional[TTSClient]) -> None:
    global _ACTIVE_TTS_CLIENT
    _ACTIVE_TTS_CLIENT = client


# ---------------------------------------------------------------------------
# 工具:wav 时长探测(不依赖 torchaudio / librosa,plan §5 阶段 4 决策 1)
# ---------------------------------------------------------------------------
def read_wav_duration_ms(path: Path) -> int:
    """读 wav header 算时长(毫秒)。

    仅支持 PCM 标准 wav;不支持 mp3 / opus / flac(那些走 RealTTSClient 内部处理)。

    Returns:
        时长(毫秒),文件不存在或解析失败时返回 0(不抛,与 plan §4.4 对齐)。
    """
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0
            return int(round(frames / rate * 1000))
    except (OSError, wave.Error, EOFError):
        return 0


# ---------------------------------------------------------------------------
# tts_ref.yaml 加载 + Key 解析
# ---------------------------------------------------------------------------
@dataclass
class TTSProviderConfig:
    """从 ``tts_ref.yaml`` 解析出来的供应商配置。"""

    name: str
    description: str
    default_base_url: str
    env_var_overrides: dict[str, str]
    params_schema: dict[str, Any]
    env_key_prefix: str


def load_tts_providers(yaml_path: Path) -> dict[str, TTSProviderConfig]:
    """读 tts_ref.yaml。

    用最小依赖:不引入 PyYAML,直接正则解析 ``key: value`` 行(yaml 形状在
    ``tts_ref.yaml`` 里是严格的,顶层是扁平的 ``key: value`` + 嵌套 dict,
    但嵌套层只有一层,正则够用)。失败返回 ``{}``。
    """
    if not yaml_path.exists():
        return {}
    out: dict[str, TTSProviderConfig] = {}
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    # 简化解析:把整个文件按 `name:` 块切分,每个块里抓 description / base_url 等。
    # 仅支持本地化 YAML 形状,不再扩展到通用 YAML。
    blocks = re.split(r"^  (\w+):\s*$", text, flags=re.MULTILINE)
    # blocks: [pre, name1, body1, name2, body2, ...]
    pre = blocks[0]
    default_provider_match = re.search(
        r'^default_provider:\s*"([^"]+)"', pre, flags=re.MULTILINE
    )
    default_provider = (
        default_provider_match.group(1) if default_provider_match else "minimax"
    )

    for i in range(1, len(blocks), 2):
        name = blocks[i].strip()
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        desc_m = re.search(r'description:\s*"([^"]*)"', body)
        url_m = re.search(r'default_base_url:\s*"([^"]*)"', body)
        prefix_m = re.search(r'env_key_prefix:\s*"([^"]*)"', body)
        out[name] = TTSProviderConfig(
            name=name,
            description=desc_m.group(1) if desc_m else "",
            default_base_url=url_m.group(1) if url_m else "",
            env_var_overrides={},        # 简化:暂不解析(单测不需要)
            params_schema={},            # 简化:暂不解析 schema(单测不需要)
            env_key_prefix=prefix_m.group(1) if prefix_m else "",
        )
    return out


def resolve_provider_credentials(
    provider: TTSProviderConfig,
    env_overrides: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """从 ``os.environ`` 读供应商 Key,缺 Key 返回 ``{}``(触发 stub 兜底)。

    Args:
        provider: ``TTSProviderConfig``,提供 ``env_key_prefix``(如 ``TTS_MINIMAX_``)。
        env_overrides: 测试注入位(直接覆盖实际 env),key 名不带前缀。

    Returns:
        已解析的 credentials dict,如 ``{"api_key": "...", "base_url": "..."}``。
        为空时表示无 Key,RealTTSClient 应走 stub 兜底。
    """
    out: dict[str, str] = {}
    prefix = (provider.env_key_prefix or "").strip()
    for env_key, env_val in os.environ.items():
        if prefix and env_key.startswith(prefix):
            short = env_key[len(prefix):].lower()
            if short and env_val:
                out[short] = env_val
    if env_overrides:
        for k, v in env_overrides.items():
            if v:
                out[k.lower()] = v
    return out


__all__ = [
    "TTSClient",
    "TTSProviderConfig",
    "TTSResult",
    "StubTTSClient",
    "get_default_tts_client",
    "set_default_tts_client",
    "load_tts_providers",
    "read_wav_duration_ms",
    "resolve_provider_credentials",
]
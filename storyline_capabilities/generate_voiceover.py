"""本地化 generate_voiceover(plan_v4 §5 阶段 4 / C 类)。

按 group_scripts 逐段生成配音 wav + duration_ms。**算法/Prompt** 来自 vendored
OpenStoryline copy,但去 ``NodeState`` 依赖、去掉 BaseNode 框架 — 本地版本是
纯函数 + Path 输入。

设计纪律(plan §5 阶段 4 决策):
1. **LLM 解析 TTS 参数**:用 ``prompts/generate_voiceover/{zh,en}/{system,user}``
   让 LLM 从 user_request + provider schema 推断 model/voice/emotion/speed。
2. **Provider dispatch** 默认走 ``StubTTSClient``(主 venv 不能装 torch 也不能
   装 vendor SDK);真实调用留给后续 ``RealTTSClient`` 注入位。
3. **API Key 缺失不阻塞**(plan §6.5 兜底):tts_runner.resolve_provider_credentials
   返回空时,StubTTSClient 写静音 wav 占位,节点 append ``_stub=true`` 进
   error_log(不阻断下游,与 plan §8 风险表对齐)。
4. **单段失败不阻断整组**:每段 try/except 隔离,失败的段返回
   ``duration_ms=0, error=...``,下游 plan_timeline_pro 看到 duration_ms=0 时
   退化为该段素材时长(已经验证与 vendored 行为一致)。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from storyline_capabilities.prompts import render_prompt
from storyline_capabilities.tts_runner import (
    StubTTSClient,
    TTSClient,
    TTSProviderConfig,
    TTSResult,
    get_default_tts_client,
    load_tts_providers,
)
from storyline_capabilities.vlm_client import (
    LLMClient,
    chat_json,
    parse_json_loose,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认 TTS 供应商解析(plan §6.5 阶段 4 开工前定 key 名)
# ---------------------------------------------------------------------------
DEFAULT_TTS_PROVIDER: str = os.environ.get(
    "STORYLINE_TTS_PROVIDER", "minimax"
).lower().strip() or "minimax"

_DEFAULT_TTS_REF_PATH: Path = (
    Path(__file__).resolve().parent / "resource" / "tts_ref.yaml"
)


def _load_default_providers() -> dict[str, TTSProviderConfig]:
    return load_tts_providers(_DEFAULT_TTS_REF_PATH)


def _get_provider(name: str) -> Optional[TTSProviderConfig]:
    providers = _load_default_providers()
    return providers.get(name)


# ---------------------------------------------------------------------------
# LLM 推断 TTS 参数(vendored logic)
# ---------------------------------------------------------------------------
def _infer_tts_params(
    *,
    provider_name: str,
    provider_cfg: TTSProviderConfig,
    user_request: str,
    client: Optional[LLMClient],
) -> dict[str, Any]:
    """LLM 从 user_request + provider params_schema 推断 TTS 参数。

    与 vendored ``GenerateVoiceoverNode._infer_tts_params_with_llm`` 对齐:
    - schema 空 → 直接返回 ``{}``
    - LLM 失败 / parse 失败 → 返回 ``{}``
    - 不抛(plan §4.4)。
    """
    schema = provider_cfg.params_schema or {}
    if not schema:
        return {}

    sys_p = render_prompt("generate_voiceover", "system", lang="zh")
    user_p = render_prompt(
        "generate_voiceover",
        "user",
        lang="zh",
        provider_name=provider_name,
        user_request=str(user_request or ""),
        schema_text=json.dumps(schema, ensure_ascii=False, indent=2),
    )
    try:
        raw = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    k: {"type": v.get("type", "string") if isinstance(v, dict) else "string"}
                    for k, v in schema.items()
                },
            },
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM infer_tts_params failed: {e!r}")
        return {}

    if not isinstance(raw, dict):
        return {}

    # schema 白名单 + 类型约束(简化版,严格模式在 RealTTSClient 里再做)
    out: dict[str, Any] = {}
    for k, rule in schema.items():
        if not isinstance(rule, dict):
            continue
        if k not in raw:
            continue
        out[k] = raw[k]
    return out


# ---------------------------------------------------------------------------
# 单段合成(核心)
# ---------------------------------------------------------------------------
def _synthesize_segment(
    *,
    text: str,
    output_dir: Path,
    voiceover_id: str,
    tts_client: TTSClient,
    tts_params: dict[str, Any],
) -> TTSResult:
    """单段文本 → wav,返回 TTSResult(失败也带 wav_path 占位 + error_str)。"""
    ts_ms = int(time.time() * 1000)
    wav_path = output_dir / f"{voiceover_id}_{ts_ms}.wav"
    return tts_client.synthesize(
        text=text, wav_path=wav_path, params=tts_params
    )


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def generate_voiceover(
    *,
    group_scripts: list[dict[str, Any]],
    output_dir: Path,
    user_request: str = "",
    provider_name: str = DEFAULT_TTS_PROVIDER,
    tts_client: Optional[TTSClient] = None,
    llm_client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """按 group_scripts 逐段生成配音 wav。

    Args:
        group_scripts: ``generate_script`` 输出的 ``group_scripts`` 列表,
            每个 dict 至少含 ``group_id`` + ``narration``(原始文案)。
        output_dir: 写 wav 的目录(由节点壳子传入 ``<outputs_root>/storyline``)。
        user_request: 用户要求(音色 / 性别 / 语速偏好)。
        provider_name: TTS 供应商名(``minimax`` / ``bytedance`` / ``302``)。
        tts_client: 注入位;None 时用 ``tts_runner.get_default_tts_client()``
            (默认 ``StubTTSClient``)。
        llm_client: 注入位;None 时用 ``vlm_client.get_default_client()``。

    Returns:
        dict 形如::

            {
                "voiceover": [
                    {
                        "voiceover_id": "voiceover_0001",
                        "group_id": "group_0001",
                        "path": "<output_dir>/voiceover_0001_<ts>.wav",
                        "duration": 3500,
                        "provider": "stub" | "minimax" | ...,
                        "error": null | "...",
                    },
                    ...
                ],
                "provider": "...",
                "params": {...},                  # 推断出的 TTS 参数
                "errors": [...],                  # 每段失败原因
                "stub_count": int,                # 走 stub 兜底的段数
            }

    失败语义:
        - **单段失败** → 该段 ``duration=0, error="..."``,append ``errors``,
          不影响其它段。
        - **整组空** → 返回 ``{"voiceover": [], ...}``,**不**写 error_log。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not group_scripts:
        return {
            "voiceover": [],
            "provider": provider_name,
            "params": {},
            "errors": [],
            "stub_count": 0,
            "method": "no_input",
        }

    # 1. 取供应商配置 + 推断参数
    provider_cfg = _get_provider(provider_name)
    if provider_cfg is None:
        # 未识别供应商 → fallback stub,不报错
        provider_cfg = TTSProviderConfig(
            name="stub",
            description="unknown provider fallback",
            default_base_url="",
            env_var_overrides={},
            params_schema={},
            env_key_prefix="",
        )
        provider_name = "stub"

    tts_params = _infer_tts_params(
        provider_name=provider_name,
        provider_cfg=provider_cfg,
        user_request=user_request,
        client=llm_client,
    )

    # 2. 选 TTS client(默认 stub)
    client = tts_client or get_default_tts_client()

    # 3. 逐段合成
    voiceover: list[dict[str, Any]] = []
    errors: list[str] = []
    stub_count = 0

    for i, group in enumerate(group_scripts, start=1):
        group_id = (group or {}).get("group_id") or f"group_{i:04d}"
        raw_text = (group or {}).get("narration") or ""
        voiceover_id = f"voiceover_{i:04d}"

        if not raw_text.strip():
            voiceover.append(
                {
                    "voiceover_id": voiceover_id,
                    "group_id": group_id,
                    "path": "",
                    "duration": 0,
                    "provider": provider_name,
                    "error": "empty narration text",
                }
            )
            errors.append(f"{voiceover_id}: empty narration")
            continue

        try:
            result = _synthesize_segment(
                text=raw_text,
                output_dir=output_dir,
                voiceover_id=voiceover_id,
                tts_client=client,
                tts_params=tts_params,
            )
        except Exception as e:  # noqa: BLE001
            voiceover.append(
                {
                    "voiceover_id": voiceover_id,
                    "group_id": group_id,
                    "path": "",
                    "duration": 0,
                    "provider": provider_name,
                    "error": f"synthesize exception: {e!r}",
                }
            )
            errors.append(f"{voiceover_id}: synthesize exception: {e!r}")
            continue

        voiceover.append(
            {
                "voiceover_id": voiceover_id,
                "group_id": group_id,
                "path": str(result.wav_path),
                "duration": int(result.duration_ms or 0),
                "provider": result.provider,
                "error": result.error,
            }
        )
        if result.provider == "stub":
            stub_count += 1
        if result.error:
            errors.append(f"{voiceover_id}: {result.error}")

    return {
        "voiceover": voiceover,
        "provider": provider_name,
        "params": tts_params,
        "errors": errors,
        "stub_count": stub_count,
        "method": "llm_inferred_params" if tts_params else "default_params",
    }


__all__ = ["generate_voiceover", "DEFAULT_TTS_PROVIDER"]
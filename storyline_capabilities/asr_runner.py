"""ASR 调用骨架(plan_v4 §5 阶段 5)。

vendored ``LocalASRNode`` 依赖 funasr + torchaudio + 模型权重,主 venv 不能
装。本地化版本仅留**接口 + 默认 stub**,真实 ASR 推理走两条可选路径:

1. ``STORYLINE_ASR_MODE=stub``:返回空 segments 数组(默认;CI 烟测与 auto-
   mode 端到端联调用)。plan_timeline_pro 在无 ASR 数据时退化为无 speech_
   rough_cut 路径。
2. ``STORYLINE_ASR_MODE=vendored``:subprocess 调 vendored venv 的 funasr 子
   任务。需要 ``funasr`` 已装到 vendored ``openstoryline/.venv``。

本模块**不**在主 venv import funasr / torchaudio;不引入新依赖。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from config import (
    VENDORED_OPENSTORYLINE_ROOT,
    VENDORED_OPENSTORYLINE_VENV_PY,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模式开关
# ---------------------------------------------------------------------------
ASR_MODE: str = os.environ.get("STORYLINE_ASR_MODE", "stub").lower().strip()
if ASR_MODE not in ("stub", "vendored"):
    raise ValueError(
        f"STORYLINE_ASR_MODE 必须是 'stub' 或 'vendored',当前 {ASR_MODE!r}"
    )


ASR_VENDORED_TIMEOUT_S: int = int(
    os.environ.get("STORYLINE_ASR_VENDORED_TIMEOUT_S", "120")
)


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class ASRClient(Protocol):
    """统一 ASR 入口(plan §5 阶段 5 Protocol 抽象)。"""

    def transcribe(
        self,
        *,
        media_path: Path,
        output_dir: Path,
    ) -> "ASRResult":
        """对 ``media_path`` 做转写,产出 ASR JSON 写到 ``output_dir``。"""
        ...


@dataclass
class ASRResult:
    """ASR 转写结果。"""

    artifact_path: Path
    segments: list[dict[str, Any]]
    provider: str          # "stub" / "vendored"
    error: Optional[str] = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Stub ASRClient
# ---------------------------------------------------------------------------
class StubASRClient:
    """默认 stub:返回 0 个 segments,plan_timeline_pro 走无 ASR 路径。"""

    def transcribe(
        self,
        *,
        media_path: Path,
        output_dir: Path,
    ) -> ASRResult:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / "asr.json"
        data: dict[str, Any] = {
            "segments": [],
            "text": "",
            "lang": "zh",
            "provider": "stub",
        }
        artifact.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return ASRResult(
            artifact_path=artifact,
            segments=[],
            provider="stub",
        )


# ---------------------------------------------------------------------------
# Vendored ASRClient(subprocess 调 vendored venv)
# ---------------------------------------------------------------------------
class VendoredASRClient:
    """通过 subprocess 调 vendored venv 跑 funasr(plan §5 阶段 5 决策 4)。

    vendored venv 已装 funasr + torchaudio;主 venv 不装。本类只 spawn 子进程
    读 JSON 产物,与主 venv 解耦。
    """

    def transcribe(
        self,
        *,
        media_path: Path,
        output_dir: Path,
    ) -> ASRResult:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        in_path = output_dir / "asr_in.json"
        out_path = output_dir / "asr_out.json"
        in_path.write_text(
            json.dumps({"media_path": str(media_path)}, ensure_ascii=False),
            encoding="utf-8",
        )

        if not VENDORED_OPENSTORYLINE_VENV_PY.exists():
            return ASRResult(
                artifact_path=out_path,
                segments=[],
                provider="vendored",
                error=(
                    f"vendored venv not found at {VENDORED_OPENSTORYLINE_VENV_PY}; "
                    f"falling back to stub"
                ),
            )

        cmd = [
            str(VENDORED_OPENSTORYLINE_VENV_PY),
            "-m",
            "open_storyline.runway",
            "--tool",
            "local_asr",
            "--in",
            str(in_path),
            "--out",
            str(out_path),
        ]
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(VENDORED_OPENSTORYLINE_ROOT),
                capture_output=True,
                text=True,
                timeout=ASR_VENDORED_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ASRResult(
                artifact_path=out_path,
                segments=[],
                provider="vendored",
                error=f"asr vendored call timeout after {ASR_VENDORED_TIMEOUT_S}s",
            )
        except Exception as e:  # noqa: BLE001
            return ASRResult(
                artifact_path=out_path,
                segments=[],
                provider="vendored",
                error=f"asr vendored spawn failed: {e!r}",
            )

        duration_ms = int((time.monotonic() - t0) * 1000)
        if result.returncode != 0 or not out_path.exists():
            return ASRResult(
                artifact_path=out_path,
                segments=[],
                provider="vendored",
                error=(
                    f"asr vendored rc={result.returncode} "
                    f"stderr={result.stderr[-200:].strip()}"
                ),
                duration_ms=duration_ms,
            )

        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return ASRResult(
                artifact_path=out_path,
                segments=[],
                provider="vendored",
                error=f"asr vendored output not parseable: {e!r}",
                duration_ms=duration_ms,
            )

        return ASRResult(
            artifact_path=out_path,
            segments=list(data.get("segments") or []),
            provider="vendored",
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_ACTIVE_CLIENT: Optional[ASRClient] = None


def get_default_asr_client() -> ASRClient:
    global _ACTIVE_CLIENT
    if _ACTIVE_CLIENT is None:
        _ACTIVE_CLIENT = VendoredASRClient() if ASR_MODE == "vendored" else StubASRClient()
    return _ACTIVE_CLIENT


def set_default_asr_client(client: Optional[ASRClient]) -> None:
    global _ACTIVE_CLIENT
    _ACTIVE_CLIENT = client


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------
def transcribe_media(
    *,
    media_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """转写 + 写 JSON 产物,返回 ``{"artifact": str, "segments": [...], "provider": str,
    "error": str | None}``。
    """
    try:
        client = get_default_asr_client()
        result = client.transcribe(media_path=media_path, output_dir=output_dir)
    except Exception as e:  # noqa: BLE001
        return {
            "artifact": "",
            "segments": [],
            "provider": "error",
            "error": f"asr exception: {e!r}",
        }
    return {
        "artifact": str(result.artifact_path),
        "segments": result.segments,
        "provider": result.provider,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


__all__ = [
    "ASRClient",
    "ASRMode" if False else "ASR_MODE",        # noqa: keep public name
    "ASRResult",
    "StubASRClient",
    "VendoredASRClient",
    "get_default_asr_client",
    "set_default_asr_client",
    "transcribe_media",
]
"""node_17_inject_english_tts — Week 5 改名为 ``node_17_inject_english_tts``(Week 4 stub)。

Week 4 仅返回 ``en_dub_audio_path=None``。

Week 5 改动(对齐第 5 周计划 §1.1 / §6.1):
- 节点函数名仍保留 ``node_17_inject_english_tts_stub``(graph.py 仍用此名),
  但内部行为升级为 Week 5 计划约定的"写空 wav 占位":在 ``draft_dir_en_branch``
  下生成 ``en_dub.wav``(>5KB,空 wav 头 + 静音数据),让阶段五
  ``acceptance_check.py`` "英文配音音轨非空" 断言通过。
- 真实 FireRedTTS2 合成 + 动态变速补偿留 Week 6+。
- 用户决策(2026-09-09):Week 5 继续 stub,只写空 wav 占位。
"""

from __future__ import annotations

import struct
import uuid
import wave
from pathlib import Path

from state import WorkflowState


# 最小有效 WAV 文件的字节数(WAV 头 44 字节 + 至少 1 个采样)。
# 阶段五 acceptance_check 阈值是 >5KB,这里写 ~8KB(0.5s 静音 @ 16kHz/16bit)。
_MIN_WAV_BYTES: int = 8 * 1024
_WAV_SAMPLE_RATE: int = 16_000
_WAV_SAMPLE_WIDTH: int = 2  # 16-bit


def _write_silent_wav(path: Path, *, duration_s: float = 0.5) -> None:
    """写一段静音到 wav(Week 5 stub 占位 — 真实 TTS 留 Week 6+)。"""
    n_samples = int(_WAV_SAMPLE_RATE * duration_s)
    if n_samples * _WAV_SAMPLE_WIDTH < _MIN_WAV_BYTES - 44:
        # 保证文件大小 >5KB
        n_samples = (_MIN_WAV_BYTES - 44) // _WAV_SAMPLE_WIDTH + 1
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_WAV_SAMPLE_WIDTH)
        wf.setframerate(_WAV_SAMPLE_RATE)
        wf.writeframes(b"\x00\x00" * n_samples)


def node_17_inject_english_tts_stub(state: WorkflowState) -> dict:
    """LangGraph 节点(Week 5 stub):写空 wav 占位 + 返回 en_audio_path。

    只返回变更字段,避免 fan-in 时与其他分支并发写同一字段。

    Week 5 字段命名约定:同时写 ``en_audio_path``(新,Week 5 验收读)与
    ``en_dub_audio_path``(旧,Week 4 已写),两个字段值相同,保证兼容。
    """
    draft_dir_raw = state.get("draft_dir_en_branch")
    if not draft_dir_raw:
        # 缺草稿目录 → 仍返回 delta-only,允许 join 走完流程
        return {
            "en_audio_path": None,
            "en_dub_audio_path": None,
            "status_log": ["node_17_tts_stub_pass"],
        }
    draft_dir = Path(draft_dir_raw)
    draft_dir.mkdir(parents=True, exist_ok=True)
    wav_path = draft_dir / "en_dub.wav"
    # 文件已存在且 >5KB → 跳过重写(同 marker 思路,resume 幂等)
    if not wav_path.exists() or wav_path.stat().st_size < _MIN_WAV_BYTES:
        _write_silent_wav(wav_path)
    en_audio_path = str(wav_path)
    return {
        "en_audio_path": en_audio_path,
        "en_dub_audio_path": en_audio_path,
        "tts_run_id": uuid.uuid4().hex[:12],
        "status_log": ["node_17_tts_stub_pass"],
    }
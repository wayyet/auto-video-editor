"""node_17_inject_english_tts_stub — Week 4 骨架(对照计划 §4.5)。

Week 4 仅返回 ``en_dub_audio_path=None``。

TODO Week 5:替换为 FireRedTTS2 合成 + 动态变速补偿。
"""

from __future__ import annotations

from state import WorkflowState


def node_17_inject_english_tts_stub(state: WorkflowState) -> dict:
    """LangGraph 节点(Week 4 骨架):返回 en_dub_audio_path=None。

    只返回变更字段,避免 fan-in 时与其他分支并发写同一字段。
    """
    return {
        "en_dub_audio_path": None,
        "status_log": ["node_17_tts_stub_pass"],
    }
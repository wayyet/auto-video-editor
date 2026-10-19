"""沙箱用的玩具图:复刻仓库的关键形状,不含仓库业务代码。

形状(与真实图一致的几点):
- 能力函数里 chat 调用包在 ``try/except Exception``,失败就降级(沙箱实测 GraphInterrupt 会被它吞掉)
- 2 个并行节点同一超步里同时调 chat
- 人工关卡用 ``interrupt({"checkpoint": "①", ...})``
- build_graph 签名与仓库一致,默认 SqliteSaver(同步)

    understand(3 次 chat) → filter(1 次) → [script ∥ transition] → gate① → finalize(1 次) → END
"""
from __future__ import annotations

import operator
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from storyline_capabilities.vlm_client import chat_json, chat_text

CLIPS = ["c1", "c2", "c3"]
CAPTION_SCHEMA = {
    "type": "object",
    "properties": {"caption": {"type": "string"}, "aes_score": {"type": "number"}},
    "required": ["caption", "aes_score"],
}
FILTER_SCHEMA = {
    "type": "object",
    "properties": {"keep_clip_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["keep_clip_ids"],
}
TRANSITION_SCHEMA = {
    "type": "object",
    "properties": {"transition": {"type": "string", "enum": ["cut", "fade", "wipe"]}},
    "required": ["transition"],
}


class S(TypedDict, total=False):
    session_id: str
    video_input_path: str
    captions: dict
    kept: list
    script: str
    transition: str
    final: dict
    status_log: Annotated[list, operator.add]
    error_log: Annotated[list, operator.add]


def understand(state: S) -> dict:
    caps: dict[str, Any] = {}
    errs: list[str] = []
    for cid in CLIPS:
        try:  # 与仓库同款:吞掉一切 Exception,降级为占位
            obj = chat_json(
                system_prompt="你是视频镜头理解助手。只输出 JSON。",
                user_prompt=f"请描述镜头 {cid} 的画面,并给出 0-10 的美学评分。",
                media=[{"path": state.get("video_input_path", ""), "in_sec": 0.0, "out_sec": 2.0}]
                if state.get("video_input_path") else None,
                schema=CAPTION_SCHEMA,
            )
        except Exception as e:  # noqa: BLE001
            obj = {}
            errs.append(f"{cid}:{type(e).__name__}")
        caps[cid] = obj or {"caption": "Error: no answer", "aes_score": -1.0}
    return {"captions": caps, "status_log": ["understand_done"], "error_log": errs}


def filter_clips(state: S) -> dict:
    try:
        obj = chat_json(
            system_prompt="你是镜头筛选助手。只输出 JSON。",
            user_prompt="镜头描述:" + "; ".join(f"{k}={v.get('caption')}" for k, v in state["captions"].items())
            + "。请给出要保留的 clip id。",
            schema=FILTER_SCHEMA,
        )
    except Exception:  # noqa: BLE001
        obj = {}
    kept = obj.get("keep_clip_ids") or list(state["captions"])  # 降级:全保留
    return {"kept": kept, "status_log": ["filter_done" if obj else "filter_fallback"]}


def script(state: S) -> dict:
    try:
        text = chat_text(system_prompt="你是文案助手。", user_prompt=f"为镜头 {state['kept']} 写一句 20 字内文案。")
    except Exception:  # noqa: BLE001
        text = ""
    return {"script": text or "(降级文案)", "status_log": ["script_done"]}


def transition(state: S) -> dict:
    try:
        obj = chat_json(system_prompt="你是转场助手。只输出 JSON。",
                        user_prompt="为这条片子选一个转场:cut / fade / wipe。",
                        schema=TRANSITION_SCHEMA)
    except Exception:  # noqa: BLE001
        obj = {}
    return {"transition": obj.get("transition", "cut"), "status_log": ["transition_done"]}


def gate1(state: S) -> dict:
    interrupt({"checkpoint": "①", "legacy_id": "checkpoint1_reorder", "step": 6,
               "instructions": "请在剪映里调整分镜顺序,完成后确认继续"})
    return {"status_log": ["gate1_resumed"]}


def finalize(state: S) -> dict:
    try:
        obj = chat_json(system_prompt="你是总结助手。只输出 JSON。",
                        user_prompt=f"文案:{state.get('script')};转场:{state.get('transition')}。给出一句总结。",
                        schema={"type": "object", "properties": {"summary": {"type": "string"}},
                                "required": ["summary"]})
    except Exception:  # noqa: BLE001
        obj = {}
    return {"final": obj or {"summary": "(降级总结)"}, "status_log": ["finalize_done"]}


def _state_graph() -> StateGraph:
    g = StateGraph(S)
    for name, fn in [("understand", understand), ("filter_clips", filter_clips), ("script", script),
                     ("transition", transition), ("gate1", gate1), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "understand")
    g.add_edge("understand", "filter_clips")
    g.add_edge("filter_clips", "script")
    g.add_edge("filter_clips", "transition")
    g.add_edge(["script", "transition"], "gate1")
    g.add_edge("gate1", "finalize")
    g.add_edge("finalize", END)
    return g


def build_graph(checkpointer=None, *, thread_id: str = "default", start_heartbeat_thread: bool = True,
                run_preflight: bool = False, preflight_config: Optional[object] = None):
    """签名与仓库 graph.build_graph 一致。默认 SqliteSaver,落盘到 $TOY_CKPT_DIR/<thread_id>.sqlite。"""
    if checkpointer is None:
        d = Path(os.environ.get("TOY_CKPT_DIR", "checkpoints"))
        d.mkdir(parents=True, exist_ok=True)
        checkpointer = SqliteSaver(sqlite3.connect(str(d / f"{thread_id}.sqlite"), check_same_thread=False))
    return _state_graph().compile(checkpointer=checkpointer)

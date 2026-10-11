"""节点 12:human_add_bgm — 关卡②(对应原文档 4.7 节)。

结构与 ``node_06_human_reorder`` 同构,仅替换通知文案与操作范围。
用户被提示在剪映客户端手动添加 BGM,完成后通过 ``Command(resume=True)`` 唤醒。

Week 5 改动(对齐第 5 周计划 §1.1):
- interrupt payload ``checkpoint`` 字段统一为 ``"②"``,便于
  ``resume_all_pending`` 自动匹配多个 interrupt。旧值
  ``"checkpoint2_add_bgm"`` 保留在 ``legacy_id`` 字段,保证向后兼容。

2026-10 K2=B 改动:一次选曲、两份草稿。
人工只在**中文主草稿**里加一次 BGM(尾链整体推迟到 ``node_17`` 之后,人工不必
在两份草稿间来回切),resume 后本节点把中文草稿的音轨整体镜像进英文分支草稿 ——
关卡② 的挂起次数仍是 1 次。

测试策略:与 node_06 一致 — 纯函数单测覆盖 payload + post_resume,
真实 interrupt() 行为由集成测试覆盖。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from nodes._checkpoint import build_interrupt_payload, post_resume
from nodes._draft_io import load_draft, write_draft
from nodes._dual_draft import en_draft_file, missing_en_note
from state import WorkflowState


def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    """Week 3 占位通知 — 实际接入企业微信 webhook 留 Week 4。"""
    print(f"[notify] {checkpoint} thread={state.get('session_id')} draft={state.get('draft_path')}")


def _build_interrupt_payload(state: WorkflowState) -> dict:
    """构造 interrupt payload — 纯函数,便于单测。

    Week 5:``checkpoint`` 字段统一为 ``"②"``;保留 ``legacy_id`` 兼容旧版。
    样板部分见 ``nodes._checkpoint.build_interrupt_payload``;本函数只声明
    关卡② 特有的 4 个参数。
    """
    return build_interrupt_payload(
        checkpoint="②",
        legacy_id="checkpoint2_add_bgm",
        step=12,
        extra_payload={"draft_path": state.get("draft_path")},
        instructions="请从剪映 VIP 音乐库选取 BGM 并拖入音轨,完成后确认继续",
    )


# ---------------------------------------------------------------------------
# K2=B:resume 后把中文草稿的 BGM 镜像进英文分支草稿
# ---------------------------------------------------------------------------
def mirror_bgm_to_en_draft(state: WorkflowState) -> list[str]:
    """把中文主草稿的音频轨(含其引用的 audio materials)复制到英文分支草稿。

    为什么整轨复制而不是按 selector 匹配:关卡② 是人工在剪映里拖的 BGM,轨道名
    不可控(可能叫 ``audio_bgm`` / ``BGM_01`` / 中文名)。按名字匹配会让镜像静默
    失效;整轨复制则不依赖命名(见计划 §6 R3)。

    id 不 remap:两份草稿的视频时间轴完全一致(英文草稿 fork 自同一份快照②),
    segment id 与 audio material id 可以直接复用。

    **幂等**:英文草稿里已有的 audio 轨被**整体替换**而非追加,重放得到同一结果。
    英文草稿本来就没有音频轨(``node_17`` 只合成音频文件、不写草稿),所以替换
    不会丢掉任何东西。

    Returns:
        要追加进 ``status_log`` / ``error_log`` 的说明列表(空列表=一切正常)。
        失败一律降级通过(记 error_log),中文草稿不受影响。
    """
    notes: list[str] = []
    zh_path = state.get("draft_path")
    if not zh_path:
        notes.append("[node_12] state.draft_path 缺失,跳过 BGM 镜像")
        return notes

    # 从未走过英文分支(fork 早退)→ 不是降级,静默跳过,与节点 09/11/13 口径一致
    if not state.get("draft_dir_en_branch"):
        return notes

    en_file = en_draft_file(state)
    if en_file is None:
        notes.append(missing_en_note(state))
        return notes

    try:
        zh_draft = load_draft(zh_path)
    except (FileNotFoundError, ValueError) as exc:
        notes.append(f"[node_12] 读取中文草稿失败,BGM 未镜像到英文草稿: {exc}")
        return notes

    audio_tracks = [
        t for t in (zh_draft.get("tracks", []) or []) if t.get("type") == "audio"
    ]
    if not audio_tracks:
        notes.append("[node_12] 中文草稿无 audio track(关卡②可能未添加),BGM 未镜像到英文草稿")
        return notes

    try:
        en_draft = load_draft(en_file)
    except (FileNotFoundError, ValueError) as exc:
        notes.append(f"[node_12] 读取英文草稿失败,BGM 未镜像: {exc}")
        return notes

    # 1) 音轨:整体替换英文草稿里的 audio 轨(幂等:不 append)
    tracks = en_draft.setdefault("tracks", [])
    tracks[:] = [t for t in tracks if t.get("type") != "audio"] + [
        dict(t) for t in audio_tracks
    ]

    # 2) audio materials:把中文草稿里被这些 segment 引用的 material 一并复制
    referenced_ids = {
        seg.get("id")
        for track in audio_tracks
        for seg in (track.get("segments", []) or [])
        if seg.get("id")
    }
    if referenced_ids:
        zh_materials = zh_draft.get("materials", {}) or {}
        wanted = [
            m for m in (zh_materials.get("audios", []) or [])
            if m.get("id") in referenced_ids
        ]
        en_materials = en_draft.setdefault("materials", {})
        existing_audios = en_materials.setdefault("audios", [])
        kept = [m for m in existing_audios if m.get("id") not in referenced_ids]
        en_materials["audios"] = kept + [dict(m) for m in wanted]

    try:
        write_draft(en_file, en_draft)
    except (OSError, ValueError) as exc:
        notes.append(f"[node_12] BGM 镜像到英文草稿失败: {exc}")
        return notes

    notes.append(f"node_12_bgm_mirrored_en_done:{len(audio_tracks)}")
    return notes


def _post_resume(state: WorkflowState, notifier=_send_notification) -> dict:
    """薄封装:样板见 ``nodes._checkpoint.post_resume``(notifier 保持可注入)。

    K2=B:在样板收尾之后追加"BGM 镜像到英文草稿"。镜像失败只写 error_log 降级通过,
    不影响关卡② 本身的 resume 语义。
    """
    out: dict[str, Any] = post_resume(
        state,
        log_tag="checkpoint2_resumed",
        notifier=notifier,
        notified_field="bgm_notified",
    )

    status_log = list(out.get("status_log", []) or [])
    error_log = list(out.get("error_log", []) or [])
    for note in mirror_bgm_to_en_draft(state):
        if note.startswith("node_12_bgm_mirrored_en_done"):
            status_log.append(note)
        else:
            error_log.append(note)
    out["status_log"] = status_log
    out["error_log"] = error_log
    return out


def human_add_bgm(state: WorkflowState) -> dict:
    """关卡②:挂起等待用户确认,resume 后发通知、把 BGM 镜像到英文草稿并通过。"""
    interrupt(_build_interrupt_payload(state))
    return _post_resume(state)
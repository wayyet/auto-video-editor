"""节点 12:human_add_bgm — 关卡②(对应原文档 4.7 节)。

结构与 ``node_06_human_reorder`` 同构,仅替换通知文案与操作范围。
用户被提示在剪映客户端手动添加 BGM,完成后通过 ``Command(resume=True)`` 唤醒。

Week 5 改动(对齐第 5 周计划 §1.1):
- interrupt payload ``checkpoint`` 字段统一为 ``"②"``,便于
  ``resume_all_pending`` 自动匹配多个 interrupt。旧值
  ``"checkpoint2_add_bgm"`` 保留在 ``legacy_id`` 字段,保证向后兼容。

2026-10 K2=B 改动:一次选曲、两份草稿。
人工只在**中文主草稿**里加一次 BGM(尾链整体推迟到 ``node_17`` 之后,人工不必
在两份草稿间来回切),resume 后本节点把中文草稿的音轨镜像进英文分支草稿 ——
关卡② 的挂起次数仍是 1 次。P3b(2026-11-08)起镜像改**按角色合并**:
``node_17`` 写入英文分支草稿的配音轨(``EN_VOICEOVER_TRACK_NAME`` 标记)原样
保留,只替换其余 audio 轨。

测试策略:与 node_06 一致 — 纯函数单测覆盖 payload + post_resume,
真实 interrupt() 行为由集成测试覆盖。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from draft_ops.draft_integrity import (
    _bgm_segment_count,
    check_track_references,
    resolve_required_features,
)
from draft_ops.draft_lock import hash_draft_files
from nodes._checkpoint import build_interrupt_payload, post_resume
from nodes._draft_io import load_draft, write_draft
from nodes._dual_draft import EN_VOICEOVER_TRACK_NAME, en_draft_file, missing_en_note
from state import WorkflowState


def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    """Week 3 占位通知 — 实际接入企业微信 webhook 留 Week 4。"""
    print(f"[notify] {checkpoint} thread={state.get('session_id')} draft={state.get('draft_path')}")


def _build_interrupt_payload(state: WorkflowState, *, bgm_missing: bool = False) -> dict:
    """构造 interrupt payload — 纯函数,便于单测。

    Week 5:``checkpoint`` 字段统一为 ``"②"``;保留 ``legacy_id`` 兼容旧版。
    样板部分见 ``nodes._checkpoint.build_interrupt_payload``;本函数只声明
    关卡② 特有的 4 个参数。

    ``bgm_missing=True``(2026-11-10):resume 后校验未检出 BGM 轨,带告警
    重新挂起时的 payload —— 指令文案点名问题,``extra_payload`` 带
    ``bgm_missing`` 标记供监控页/创建台展示。
    """
    return build_interrupt_payload(
        checkpoint="②",
        legacy_id="checkpoint2_add_bgm",
        step=12,
        extra_payload={
            "draft_path": state.get("draft_path"),
            **({"bgm_missing": True} if bgm_missing else {}),
        },
        instructions=(
            "未检测到 BGM 音轨:请从剪映 VIP 音乐库选取 BGM 并拖入音轨,完成后确认继续"
            if bgm_missing
            else "请从剪映 VIP 音乐库选取 BGM 并拖入音轨,完成后确认继续"
        ),
    )


def _zh_bgm_missing(draft: dict | None) -> bool:
    """关卡② 校验(纯函数):中文草稿是否没有可用的 BGM 音轨。

    ``_bgm_segment_count`` 只数**非配音** audio 轨段数(配音轨按轨名豁免),
    0 段 = BGM 缺失。与交付 QA ``build_qa_categories`` 的 bgm 判据同口径。
    """
    return _bgm_segment_count(draft) == 0


def _load_zh_draft_for_gate(state: WorkflowState) -> dict | None:
    """读中文主草稿;读取失败返回 None(校验降级放行,不无限卡关卡)。"""
    zh_path = state.get("draft_path")
    if not zh_path:
        return None
    try:
        return load_draft(zh_path)
    except (FileNotFoundError, ValueError, OSError):
        return None


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

    **幂等**:英文草稿里的镜像轨被**替换**而非追加,重放得到同一结果。
    P3b(2026-11-08)起改**按角色合并**:``node_17`` 会把英文配音写进英文分支
    草稿(``name=EN_VOICEOVER_TRACK_NAME`` 标记轨),镜像时只替换其余 audio 轨,
    配音轨原样保留;materials 侧本就按 id 保留英文特有素材,无需改动。

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

    # 1) 音轨:按角色合并(P3b)—— 只替换镜像轨,保留 node_17 写入的
    #    英文配音轨(name=EN_VOICEOVER_TRACK_NAME);幂等:镜像轨不 append。
    tracks = en_draft.setdefault("tracks", [])
    tracks[:] = [
        t
        for t in tracks
        if t.get("type") != "audio" or t.get("name") == EN_VOICEOVER_TRACK_NAME
    ] + [dict(t) for t in audio_tracks]

    # 2) audio materials:把中文草稿里被这些 segment 引用的 material 一并复制。
    # 匹配键双口径:原生草稿(人工在剪映加的 BGM)用 segment.material_id 引用
    # 素材;旧约定(mapper 自产段)segment.id == material.id —— 两个键都收,
    # 兼容人工编辑过的原生稿与既有测试 fixture。
    referenced_ids = {
        seg.get(key)
        for track in audio_tracks
        for seg in (track.get("segments", []) or [])
        for key in ("material_id", "id")
        if seg.get(key)
    }
    en_materials = en_draft.setdefault("materials", {})
    zh_materials = zh_draft.get("materials", {}) or {}
    if referenced_ids:
        wanted = [
            m for m in (zh_materials.get("audios", []) or [])
            if m.get("id") in referenced_ids
        ]
        existing_audios = en_materials.setdefault("audios", [])
        kept = [m for m in existing_audios if m.get("id") not in referenced_ids]
        en_materials["audios"] = kept + [dict(m) for m in wanted]

    # 3) 伴随素材(阶段 5 修复,阶段 0 基线实证 ×4 悬空):镜像段引用的
    #    speeds/canvases/sound_channel_mappings/vocal_separations/audio_fades
    #    一并复制 —— 旧实现只镜像 audios 桶,整轨深拷把 companion refs 带到
    #    英文侧却无处解析。
    companion_refs = {
        str(r)
        for track in audio_tracks
        for seg in (track.get("segments", []) or [])
        for r in (seg.get("extra_material_refs") or [])
    }
    companion_buckets = (
        "speeds",
        "canvases",
        "sound_channel_mappings",
        "vocal_separations",
        "audio_fades",
    )
    for bucket_name in companion_buckets:
        if not companion_refs:
            break
        wanted = [
            m
            for m in (zh_materials.get(bucket_name) or [])
            if str(m.get("id") or "") in companion_refs
        ]
        if not wanted:
            continue
        en_bucket = en_materials.setdefault(bucket_name, [])
        existing_ids = {m.get("id") for m in en_bucket}
        for m in wanted:
            if m.get("id") not in existing_ids:
                en_bucket.append(dict(m))

    # 4) 尽力剥离无法镜像的 refs(指向未复制桶的引用会变成悬空,QA error):
    #    只处理镜像轨上的段,人工素材不受影响。
    known_ids = {
        str(m.get("id"))
        for bucket in en_materials.values()
        if isinstance(bucket, list)
        for m in bucket
        if isinstance(m, dict) and m.get("id")
    }
    stripped = 0
    for track in tracks:
        if track.get("type") != "audio" or track.get("name") == EN_VOICEOVER_TRACK_NAME:
            continue
        for seg in (track.get("segments", []) or []):
            refs = seg.get("extra_material_refs")
            if not isinstance(refs, list):
                continue
            kept_refs = [r for r in refs if str(r) in known_ids]
            stripped += len(refs) - len(kept_refs)
            seg["extra_material_refs"] = kept_refs
    if stripped:
        notes.append(
            f"[node_12] 镜像段有 {stripped} 个无法镜像的 companion ref,已剥离(防悬空)"
        )

    try:
        write_draft(en_file, en_draft, audit_node="node_12_mirror")
    except (OSError, ValueError, RuntimeError) as exc:
        # RuntimeError = 剪映在跑且严格模式拒写(JY_WRITE_FAIL_ON_JIANYING_RUNNING)
        # —— 关卡② resume 后剪映很可能还开着,降级不崩(阶段 5 加固)。
        notes.append(f"[node_12] BGM 镜像到英文草稿失败: {exc}")
        return notes

    # 5) 镜像后校验(阶段 5 任务 2):重读英文草稿跑引用完整性,失败不留假成功。
    try:
        reloaded = load_draft(en_file)
    except (OSError, ValueError) as exc:
        notes.append(f"[node_12] 镜像后回读英文草稿失败: {exc}")
        return notes
    ref_issues = check_track_references(reloaded, "en")
    errors = [i for i in ref_issues if i.get("severity") == "error"]
    if errors:
        notes.append(
            f"[node_12] 镜像后英文草稿引用完整性校验失败({len(errors)} 项):"
            f"{errors[0].get('message', '')}"
        )
        return notes

    notes.append(f"node_12_bgm_mirrored_en_done:{len(audio_tracks)}")
    return notes


def _record_manual_draft_sha(state: WorkflowState) -> dict:
    """关卡② 恢复时记录人工修改后的草稿 SHA-256(计划阶段 5 任务 1)。

    zh 是人工编辑过的权威草稿;en 记录的是**镜像前**快照(随后会被 mirror
    改写,留此值用于审计人工时点的英文侧状态)。任一文件缺失则少一个键。
    """
    out: dict[str, dict[str, str]] = {}
    zh_path = state.get("draft_path")
    if zh_path:
        out["zh"] = hash_draft_files(zh_path)
    en_file = en_draft_file(state)
    if en_file is not None:
        out["en_pre_mirror"] = hash_draft_files(en_file)
    return out


def _post_resume(state: WorkflowState, notifier=_send_notification) -> dict:
    """薄封装:样板见 ``nodes._checkpoint.post_resume``(notifier 保持可注入)。

    K2=B:在样板收尾之后追加
    1) 人工修改后草稿 SHA-256 落 ``bgm_manual_draft_sha``(阶段 5 任务 1);
    2) "BGM 镜像到英文草稿"。镜像失败只写 error_log 降级通过,
       不影响关卡② 本身的 resume 语义。
    """
    out: dict[str, Any] = post_resume(
        state,
        log_tag="checkpoint2_resumed",
        notifier=notifier,
    )

    status_log = list(out.get("status_log", []) or [])
    error_log = list(out.get("error_log", []) or [])

    manual_sha = _record_manual_draft_sha(state)
    if manual_sha:
        out["bgm_manual_draft_sha"] = manual_sha
        status_log.append("node_12_manual_draft_sha_recorded")

    for note in mirror_bgm_to_en_draft(state):
        if note.startswith("node_12_bgm_mirrored_en_done"):
            status_log.append(note)
        else:
            error_log.append(note)
    out["status_log"] = status_log
    out["error_log"] = error_log
    return out


def human_add_bgm(state: WorkflowState) -> dict:
    """关卡②:挂起等待用户确认;BGM 必选时校验草稿确有 BGM 轨后才放行。

    2026-11-10(console-5aa2606e546b 复盘):此前 resume 即通过,操作员未真正
    拖入 BGM 也会放行,缺口要拖到交付 QA 才兜住。现 required_features.bgm 为真
    时,resume 后重读磁盘上的中文草稿做校验,BGM 缺失 → 带告警再次 interrupt
    (LangGraph 顺序多次 interrupt:重跑时首个 interrupt 回放已答复的 resume 值,
    新 interrupt 重新挂起;草稿每次从磁盘现读,人工编辑即时可见)。未勾选 BGM
    的任务保持原行为(确认即过);草稿读取失败也放行(降级,不无限卡关)。
    """
    bgm_required = bool(resolve_required_features(state).get("bgm"))
    interrupt(_build_interrupt_payload(state))
    while bgm_required:
        draft = _load_zh_draft_for_gate(state)
        if not _zh_bgm_missing(draft):
            break
        interrupt(_build_interrupt_payload(state, bgm_missing=True))
    return _post_resume(state)
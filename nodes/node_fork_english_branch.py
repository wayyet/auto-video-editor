"""fork_draft_for_english_branch — 英文分支起点(Week 4 新增,对照计划 §4.1)。

职责:把 snapshot② 草稿目录递归复制成独立副本,产出 ``draft_dir_en_branch``。

关键设计(对齐计划 §4.1):
- 目标目录 ``<drafts_root>/en_branch_<session_id>_<timestamp>/``,避免 thread 冲突
- ``shutil.copytree(src, dst, dirs_exist_ok=True)``,**不**做 symlink
- **幂等**:目标目录已存在且含 ``draft_content.json`` 时,复用并仅更新 state
- **不**调 ``detect_draft_encryption``(snapshot② 已确认明文)

2026-11-17 P0(英文分支配音隔离,修订计划 §4.2/§4.3):
- 新建/本任务重放的英文分支一律执行"中文配音清理"(轨名精确匹配
  ``ZH_VOICEOVER_TRACK_NAME`` + 链产孤儿配音素材 GC),写回后回读断言 +
  引用完整性校验,**fail-fast**(清理不干净不让英文草稿进下游);
- 写回成功后置 ``en_branch_audio_policy_version = "v2"``(node_12 镜像过滤与
  QA 语言隔离检查仅对 v2 生效);
- 复用**无策略标记的历史目录**(旧任务/旧 checkpoint)不追溯改写:state 置
  ``"legacy"`` 并记 ``legacy_en_branch_cleanup_skipped``,保持旧行为。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from config import resolve_draft_dir
from draft_ops.draft_integrity import check_track_references
from nodes._draft_io import load_draft, write_draft
from nodes._dual_draft import (
    EN_BRANCH_AUDIO_POLICY_LEGACY,
    EN_BRANCH_AUDIO_POLICY_V2,
    ZH_VOICEOVER_TRACK_NAME,
    remove_chinese_voiceover_from_draft,
)
from state import WorkflowState

#: 音频策略版本别名(权威定义 nodes/_dual_draft.py)
POLICY_V2 = EN_BRANCH_AUDIO_POLICY_V2
POLICY_LEGACY = EN_BRANCH_AUDIO_POLICY_LEGACY


def _compute_target_dir(state: WorkflowState) -> Path:
    """根据 session_id + 时间戳计算 en_branch 目录的绝对路径。"""
    base = resolve_draft_dir(state)
    session = state.get("session_id") or "default"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return base.parent / f"en_branch_{session}_{timestamp}"


def _find_existing_target(state: WorkflowState) -> Path | None:
    """查找已存在的 en_branch 目录(同 session_id)。

    实现:在 drafts_root.parent 下匹配 ``en_branch_<session_id>_*`` 的目录,
    取 mtime 最新且含 draft_content.json 的那个。
    """
    base = resolve_draft_dir(state)
    session = state.get("session_id") or "default"
    parent = base.parent
    if not parent.exists():
        return None
    prefix = f"en_branch_{session}_"
    candidates: list[Path] = []
    for entry in parent.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            if (entry / "draft_content.json").exists():
                candidates.append(entry)
    if not candidates:
        return None
    # 取最新 mtime
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _copy_snapshot(src_dir: Path, dst_dir: Path) -> None:
    """递归复制 src_dir 到 dst_dir(覆盖),不做 symlink。

    阶段 4(人工关卡与剪映草稿导入):排除 ``.snapshots`` —— 与
    :func:`draft_ops.draft_publisher._copy_tree` 同款 ignore 规则。快照是
    safe_write_draft 的写前备份机制,属于工作流内部产物;en_branch 是要在剪映
    首页出现的正式草稿目录,不该把快照垃圾(可能含整份旧草稿副本)带进剪映
    草稿库。
    """
    shutil.copytree(
        src=str(src_dir),
        dst=str(dst_dir),
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".snapshots"),
    )


def _storyline_root_of(state: WorkflowState) -> str:
    """本次运行 storyline 输出目录(链产配音素材 GC 的路径边界)。

    与 node_08 同口径:voiceover.json 所在目录。缺失时返回空串(跳过 GC,
    只做轨名精确匹配清理)。
    """
    raw = state.get("storyline_voiceover_artifact")
    if not raw:
        return ""
    return str(Path(str(raw)).parent)


def _cleanup_chinese_voiceover(target: Path, storyline_root: str) -> dict:
    """清理英文分支目录里的中文配音轨与链产孤儿配音素材,写回并校验。

    fail-fast 契约(修订计划 §4.2/§8.1):写回失败 / 回读仍残留 VO 轨 /
    引用完整性不过 —— 一律抛异常,不让不干净的英文草稿进入下游。
    """
    draft_file = target / "draft_content.json"
    if not draft_file.exists():
        raise FileNotFoundError(f"英文分支草稿缺失,无法清理中文配音: {draft_file}")

    draft = load_draft(draft_file)
    summary = remove_chinese_voiceover_from_draft(draft, storyline_root=storyline_root)
    if summary["changed"]:
        result = write_draft(draft_file, draft, audit_node="node_fork_english_branch")
        if not result.get("ok"):
            raise RuntimeError(
                f"英文分支中文配音清理写回失败: {result.get('reason')!r}"
            )

    reloaded = load_draft(draft_file)
    residual = [
        str(t.get("id") or "?")
        for t in reloaded.get("tracks") or []
        if isinstance(t, dict)
        and t.get("type") == "audio"
        and t.get("name") == ZH_VOICEOVER_TRACK_NAME
    ]
    if residual:
        raise RuntimeError(f"英文分支清理后仍残留中文配音轨: {residual}")

    ref_errors = [
        i for i in check_track_references(reloaded, "en") if i.get("severity") == "error"
    ]
    if ref_errors:
        raise RuntimeError(
            f"英文分支清理后引用完整性校验失败: {ref_errors[0].get('detail', '')}"
        )
    return summary


def _fork(state: WorkflowState) -> dict:
    """纯函数:根据 state 计算或复用 en_branch 目录,并按策略清理中文配音。

    Returns:
        ``{"draft_dir_en_branch", "policy", "cleanup"}``;cleanup 为清理摘要
        (未执行清理时为 ``None``)。清理失败直接抛异常(fail-fast)。
    """
    snapshot2 = state.get("snapshot2_path")
    if not snapshot2:
        # snapshot2 路径缺失(节点 7 失败?),早退
        return {"draft_dir_en_branch": None, "policy": None, "cleanup": None}

    src_dir = Path(snapshot2).parent
    if not (src_dir / "draft_content.json").exists():
        return {"draft_dir_en_branch": None, "policy": None, "cleanup": None}

    # 策略判定(修订计划 §4.3):本次运行 state 已带 v2 标记(新建过或重放)
    # 才允许清理;历史目录(无标记)一律 legacy 不改写。
    state_policy = str(state.get("en_branch_audio_policy_version") or "")

    # 幂等:state 或文件系统已有匹配目录 → 复用
    state_existing = state.get("draft_dir_en_branch")
    if state_existing and Path(state_existing).exists() and (Path(state_existing) / "draft_content.json").exists():
        return _finalize_reuse(state, Path(state_existing), state_policy)

    fs_existing = _find_existing_target(state)
    if fs_existing is not None:
        return _finalize_reuse(state, fs_existing, state_policy)

    target = _compute_target_dir(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_snapshot(src_dir, target)
    summary = _cleanup_chinese_voiceover(target, _storyline_root_of(state))
    return {"draft_dir_en_branch": str(target), "policy": POLICY_V2, "cleanup": summary}


def _finalize_reuse(state: WorkflowState, target: Path, state_policy: str) -> dict:
    """复用既有目录:state 已带 v2 → 幂等重清;否则 legacy 不改写。"""
    if state_policy != POLICY_V2:
        return {"draft_dir_en_branch": str(target), "policy": POLICY_LEGACY, "cleanup": None}
    summary = _cleanup_chinese_voiceover(target, _storyline_root_of(state))
    return {"draft_dir_en_branch": str(target), "policy": POLICY_V2, "cleanup": summary}


def fork_draft_for_english_branch(state: WorkflowState) -> dict:
    """LangGraph 节点:复制 snapshot② 到 en_branch 目录,写 state。

    返回**只含变更字段**的 dict(避免 fan-in 时与其他分支并发写同一字段)。
    ``status_log`` 用 reducer 合并,见 state._append_unique。

    阶段 1(人工关卡与剪映草稿导入):en_branch 目录随
    ``_draft_dir_override`` 已落在剪映草稿库根下,这里补注册(draft_meta_info +
    root_meta_info upsert,幂等),让英文草稿也能出现在剪映首页。注册失败只改
    status_log 标记降级 —— fork 本体(复制)已成功,英文线照常工作。
    """
    fork_result = _fork(state)
    target = fork_result.get("draft_dir_en_branch")
    policy = fork_result.get("policy")
    cleanup = fork_result.get("cleanup")

    fork_tag = f"node_fork_english_branch_done:{target}"
    cleanup_tags: list[str] = []
    if policy == POLICY_V2 and cleanup is not None:
        cleanup_tags.append(
            f"node_fork_zh_voiceover_removed:{cleanup['removed_tracks']}tracks"
            f"/{cleanup['removed_materials']}mats"
        )
    elif policy == POLICY_LEGACY:
        cleanup_tags.append("legacy_en_branch_cleanup_skipped")
    if target and Path(target).is_dir():
        try:
            import json as _json

            import config

            from draft_ops.jianying_registry import (
                compute_draft_duration_us,
                register_draft,
            )

            en_content = None
            reg = None
            if config.JY_DRAFT_PUBLISH_ENABLED:
                en_content = _json.loads(
                    (Path(target) / "draft_content.json").read_text(encoding="utf-8")
                )
                reg = register_draft(
                    Path(target),
                    thread_id=str(state.get("session_id") or "en_branch"),
                    duration_us=compute_draft_duration_us(en_content),
                )
            if reg is not None:
                fork_tag = f"{fork_tag}|en_branch_registered:{reg.get('draft_name')}"
        except Exception as exc:  # noqa: BLE001 — 注册是增强路径,失败降级
            fork_tag = f"{fork_tag}|en_branch_register_skipped:{exc!r}"[:220]

    return {
        "draft_dir_en_branch": fork_result.get("draft_dir_en_branch"),
        **({"en_branch_audio_policy_version": policy} if policy else {}),
        "status_log": [fork_tag, *cleanup_tags],
    }
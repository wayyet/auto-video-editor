"""节点 5:generate_initial_jianying_draft — 对应 /openstoryline-to-jianying(附件 3.2 节)。

节点 1-5 中唯一读写 draft_content.json 的节点,严格按以下顺序执行:
1. detect_draft_encryption(draft_dir) → 若 ENCRYPTED → 早退
2. resolve_strategy(jianying_version=config.JIANYING_VERSION) → 记录策略
3. _build_draft_content(shot_plan, video_path) → 按附件 6.1 节构造
4. atomic_write_draft(draft_file, draft_content) → **必须用**,禁止裸写
"""

from __future__ import annotations

from pathlib import Path

from config import CANVAS_HEIGHT, CANVAS_WIDTH, JIANYING_VERSION
from draft_ops.atomic_writer import atomic_write_draft
from draft_ops.encryption_detector import DraftStatus, detect_draft_encryption
from draft_ops.version_strategy import resolve_strategy
from state import WorkflowState


def generate_initial_jianying_draft(
    state: WorkflowState,
    draft_dir: Path,
    *,
    encrypt_detector=detect_draft_encryption,
    writer=atomic_write_draft,
) -> dict:
    """生成剪映初始草稿文件(对照附件 3.2 节)。

    Args:
        state: 当前工作流状态(需含 shot_plan)。
        draft_dir: 剪映草稿目录(含或待生成 draft_content.json)。
        encrypt_detector: 可注入的加密检测函数,默认 detect_draft_encryption。
        writer: 可注入的原子写入函数,默认 atomic_write_draft(必须用)。
    """
    errors = list(state.get("error_log", []) or [])

    # 1. 加密检测
    status = encrypt_detector(draft_dir)
    if status == DraftStatus.ENCRYPTED:
        errors.append(
            "[node_05] 检测到已加密草稿,中止写入,需人工核查剪映版本"
        )
        return {
            **state,
            "draft_path": None,
            "draft_encryption_status": status.value,
            "error_log": errors,
        }

    # 2. 策略选择
    strategy = resolve_strategy(jianying_version=JIANYING_VERSION)

    # 3. 构造草稿内容
    shot_plan = state.get("shot_plan") or {}
    video_path = state.get("video_input_path") or ""
    draft_content = _build_draft_content(shot_plan=shot_plan, video_path=video_path)

    # 4. 原子写入
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    writer(draft_file, draft_content)

    return {
        **state,
        "draft_path": str(draft_file),
        "draft_encryption_status": status.value,
        "draft_version_strategy": strategy.value,
        "error_log": errors,
    }


def _build_draft_content(shot_plan: dict, video_path: str) -> dict:
    """按附件 6.1 节字段映射构造 draft_content.json。

    canvas_config    — 画布宽高比与分辨率(竖屏 1080x1920,与步骤 14"9:16"一致)
    materials.videos — 影片/图片素材清单(由 shot_plan.shots 推导)
    tracks           — 时间轴轨道(初始化为空轨道,具体片段填充留给后续周次)
    """
    return {
        "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "materials": {"videos": _shot_plan_to_materials(shot_plan, video_path)},
        "tracks": _init_empty_tracks(),
    }


def _shot_plan_to_materials(shot_plan: dict, video_path: str) -> list[dict]:
    """最小可用版:把每个 shot 的 video_ref 转为一个 material 条目。"""
    shots = shot_plan.get("shots", []) if isinstance(shot_plan, dict) else []
    materials: list[dict] = []
    for idx, shot in enumerate(shots):
        materials.append(
            {
                "id": f"video-{idx + 1}",
                "type": "video",
                "path": video_path,
                "shot_id": shot.get("id") if isinstance(shot, dict) else None,
                "start_s": shot.get("start_s") if isinstance(shot, dict) else None,
                "end_s": shot.get("end_s") if isinstance(shot, dict) else None,
            }
        )
    return materials


def _init_empty_tracks() -> list[dict]:
    """最小可用版:返回空轨道结构,片段填充留给后续周次(步骤 7 变速节点等)。"""
    return []

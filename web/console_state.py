"""创建台 initial state 构建器(第二期阶段 1)。

契约(冻结):docs/第二期开发/auto-video-editor_工作流创建台_阶段0_基线与契约冻结_20261105.md §3。

关键纪律:
- 键集合**固定**为下方 ``build_initial_state`` 写死的键(2026-11-09 阶段 6 起
  含 ``required_features``,值全为布尔、键名走 ``REQUIRED_FEATURE_KEYS`` 白名单;
  2026-11-14 起 ``storyline_targets`` 含 ``speed_multiplier`` 快进速度)——
  ``scripts/run_workflow.py --initial-state-json`` 对 JSON 无字段白名单
  (整体透传进 WorkflowState),所以白名单责任在本创建台,不透传任何额外键;
- 提示词只映射 ``storyline_targets.user_request / script_request``,
  **绝不写 ``query``**(它是手动检索词覆盖位:``build_web_topic_query`` 会把
  它拼进检索词,误写会污染网页话题检索的自动构造);
- 提示词不进监控事件 payload(``monitoring/workflow_runner.py`` 的
  ``STATE_SUMMARY_KEYS`` 白名单本就不含它)。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

#: 服务端生成的 thread_id 形状(与 web/console_uploads.py 保持同一规则)
from web.console_uploads import CONSOLE_THREAD_ID_RE


def new_thread_id() -> str:
    """生成 ``console-<12hex>`` 形式的唯一任务 ID(服务端生成,浏览器不可指定)。"""
    return f"console-{uuid.uuid4().hex[:12]}"


def is_valid_thread_id(thread_id: str) -> bool:
    return bool(CONSOLE_THREAD_ID_RE.match(str(thread_id or "").strip()))


def build_initial_state(
    *,
    thread_id: str,
    video_paths: list[str],
    prompt: str,
    target_duration_ms: int,
    ratio: str,
    lang: str,
    outputs_root: Path,
    required_features: dict[str, bool] | None = None,
    speed_multiplier: float = 1.0,
) -> dict:
    """构建冻结契约的初始 WorkflowState。

    Args:
        thread_id: ``console-<12hex>``。
        video_paths: 已落盘视频的**绝对路径**列表(由 console_uploads 返回),
            非空;第一个同时写入 ``video_input_path``(兼容单视频消费者)。
        prompt: 用户提示词(strip 后非空;长度上限由 API 层校验)。
        target_duration_ms / ratio / lang: 结构化参数(API 层已校验)。
        outputs_root: 任务根目录;``storyline_outputs_root`` 指向任务目录本身,
            工作流产物按 thread_id 隔离。
        required_features: 阶段 6(效果缺失修复)必需功能显式声明(9 类布尔,
            API 层白名单过滤);``None`` = 不写该键,由 QA 侧
            ``resolve_required_features`` 按运行时能力推导。
        speed_multiplier: 2026-11-14 创建台「快进速度(倍)」;每段最低播放倍数,
            1.0 = 不启用(规划器仍全自动),>1 智能保同步语义(API 层已校验
            1.0 ≤ v ≤ ``config.CONSOLE_SPEED_MULTIPLIER_MAX``)。

    Returns:
        只含冻结键集合的 dict(可直接 json.dumps 落盘)。
    """
    if not is_valid_thread_id(thread_id):
        raise ValueError(f"thread_id 形状非法: {thread_id!r}")
    if not video_paths:
        raise ValueError("video_paths 不能为空")
    if not str(prompt or "").strip():
        raise ValueError("prompt 不能为空")

    paths = [str(Path(p).resolve()) for p in video_paths]
    task_dir = str((Path(outputs_root) / thread_id).resolve())
    state: dict = {
        "session_id": thread_id,
        "video_input_path": paths[0],
        "video_input_paths": paths,
        "storyline_outputs_root": task_dir,
        "storyline_targets": {
            "user_request": prompt,
            "script_request": prompt,
            "target_duration_ms": int(target_duration_ms),
            "ratio": str(ratio),
            "lang": str(lang),
            # 2026-11-14:快进速度(倍);1.0 = 自动。消费方
            # nodes/storyline/_common.speed_multiplier → plan_timeline_pro/timeline_planner
            "speed_multiplier": float(speed_multiplier),
        },
        "error_log": [],
    }
    # 阶段 6:必需功能走白名单键(只保留 9 类布尔;非布尔值过滤,与
    # draft_integrity.resolve_required_features 同口径)。
    if required_features:
        from draft_ops.draft_integrity import REQUIRED_FEATURE_KEYS

        state["required_features"] = {
            key: required_features[key]
            for key in REQUIRED_FEATURE_KEYS
            if isinstance(required_features.get(key), bool)
        }
        if not state["required_features"]:
            del state["required_features"]
    return state


def write_initial_state_json(task_dir: Path, state: dict) -> Path:
    """把 initial state 写到 ``<task_dir>/initial_state.json``(UTF-8 无 BOM)。

    与 agent_gate/_write_initial_state、start_production.ps1 Phase 0 的落盘
    约定一致(UTF-8 编码、ensure_ascii=False 保中文可读)。
    """
    path = Path(task_dir) / "initial_state.json"
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path

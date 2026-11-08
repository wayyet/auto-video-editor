"""节点 5:generate_initial_jianying_draft — Phase 2 mapper 接入版。

行为升级(对照 plan §O5 / §5 Phase 2):
- 用 :func:`storyline.mapper.canonical_to_draft` 替换旧
  ``_build_draft_content(shot_plan, video_path)``。
- 输入优先级:
    1. ``state["storyline_plan"]``(来自 ``storyline_join`` 反序列化时间线产物)→ mapper
    2. ``state["shot_plan"]``(旧 checkpoint / 测试兜底)→ 旧 ``_build_draft_content`` 兜底
- 原子写入 ``draft_content.json`` + ``draft_info.json`` 双写(Week 5 起走
  :func:`draft_ops.atomic_writer.safe_write_draft`,剪映 5.9+ 需要二者一致)。
- 写完回填 ``manifest.draft_path``(幂等键下一次直接命中)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import config
from config import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    JIANYING_VERSION,
    WORKFLOW_ENV,
    storyline_outputs_root,
)
from assembly_capabilities.run_context import RunContext
from draft_ops.atomic_writer import safe_write_draft
from draft_ops.encryption_detector import DraftStatus, detect_draft_encryption
from draft_ops.jianying_registry import compute_draft_duration_us
from draft_ops.version_strategy import resolve_strategy
from state import WorkflowState
from storyline.contract import CanonicalTimeline, ContractInvalid
from storyline.mapper import canonical_to_draft
from storyline.native_draft import storyline_to_native, write_sidecar
from storyline.output_isolation import (
    OutputJobPaths,
    build_manifest,
    compute_idempotency_key,
    compute_input_sha256,
    is_idempotent_hit,
    load_manifest,
    write_manifest,
)
from video_edit_capabilities.media_operation import video_basic_operation
from video_edit_capabilities.render import probe_video_size
from nodes.storyline._common import resolve_video_input_paths


def generate_initial_jianying_draft(
    state: WorkflowState,
    draft_dir: Path,
    *,
    encrypt_detector=detect_draft_encryption,
    writer=safe_write_draft,
) -> dict:
    """生成剪映初始草稿文件(Phase 2 接入 Canonical Timeline mapper)。

    Args:
        state: 当前工作流状态(优先 ``storyline_plan``;fallback 到 ``shot_plan``)。
        draft_dir: 剪映草稿目录(含或待生成 draft_content.json / draft_info.json)。
        encrypt_detector: 可注入的加密检测函数,默认 detect_draft_encryption。
        writer: 可注入的写入函数(Week 5 起签名 ``(draft_dir, content)``),
            默认 :func:`draft_ops.atomic_writer.safe_write_draft`(剪映 5.9+ 双写)。
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

    # 3. 优先 storyline_plan(真实 MCP);fallback shot_plan(Mock)
    storyline_plan = state.get("storyline_plan")
    shot_plan = state.get("shot_plan")
    video_path = state.get("video_input_path") or ""
    job_id = state.get("session_id")
    # 全部源视频(多视频导入)。单视频时 == [video_input_path]。
    source_paths = resolve_video_input_paths(state)

    # 3a. 阶段五(§6.5)素材宽高比归一化:比例不一致先裁切,再进草稿。
    # ``video_path`` 保持原样(manifest / 幂等键记的是**输入**素材),
    # 草稿 materials 用归一化后的 ``draft_video_path``。
    #
    # 多视频:
    # - **legacy 路径**(shot_plan 兜底):逐视频归一化,shots 轮转分配到各源。
    # - **canonical 路径**(storyline_plan):素材引用在 timeline 里,归一化要重写
    #   timeline(风险远大于收益)→ 只归一化第一个(与改动前行为一致),
    #   多源素材保持原比例(已知限制,见 docs/integration 多视频导入设计 R3)。
    normalize_targets = (
        source_paths
        if (not storyline_plan and source_paths)
        else ([video_path] if video_path else [])
    )
    draft_video_paths: list[str] = []
    normalized_path: str | None = None
    normalize_errors: list[str] = []
    for src in normalize_targets:
        dp, np_, errs = _normalize_source_video(src, job_id=job_id)
        draft_video_paths.append(dp)
        normalize_errors.extend(errs)
        if np_ and normalized_path is None:
            normalized_path = np_
    draft_video_path = draft_video_paths[0] if draft_video_paths else video_path
    errors.extend(normalize_errors)

    try:
        if storyline_plan:
            draft_content = _build_from_canonical(storyline_plan)
        elif shot_plan:
            draft_content = _build_draft_content_legacy(
                shot_plan=shot_plan, video_path=draft_video_paths or draft_video_path
            )
        else:
            # 两个都空:写空草稿(materials.audios 留空,tracks 给占位空)
            draft_content = {
                "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
                "materials": {"videos": [], "audios": [], "texts": []},
                "tracks": [],
            }
    except (ContractInvalid, ValueError) as e:
        errors.append(f"[node_05] canonical plan invalid: {e!r}")
        # mapper 失败:走早退,不污染 draft_content.json
        return {
            **state,
            "draft_path": None,
            "draft_encryption_status": status.value,
            "draft_version_strategy": strategy.value,
            "draft_source_normalized_path": normalized_path,
            "error_log": errors,
        }

    # 3a-2. 空 storyline fail-closed(2026-11-07 实机回归):切镜 0 镜头时
    # canonical plan 本身合法(Pydantic 可过)但 clips=0,旧行为会发布空草稿
    # 并挂关卡① —— 人工面对一张 00:00 空时间轴的"调整镜头顺序"死局。
    # 直接终止运行,经 node.failed 把原因透出到监控(与 7860 fail-closed 纪律
    # 一致;qa_gate 对空 plan 的放行是 plan §4.2 既有设计,不在此翻转)。
    if storyline_plan and not storyline_plan.get("clips"):
        raise ContractInvalid(
            "[node_05] storyline 时间轴为空(clips=0,典型原因:切镜无产出,"
            "见 storyline/split_shots.json),拒绝发布空草稿并挂关卡①",
        )

    # 3b. 原生转换(2026-11-07 剪映导入修复):三路产物统一转剪映原生 schema
    # (mapper 4 键格式首页可列但双击必失败);转换失败降级保留 storyline 格式,
    # 发布链(draft_publisher)会对旧格式再做一次转换兜底。
    native_sidecar: dict | None = None
    try:
        draft_content, native_sidecar = storyline_to_native(draft_content)
    except Exception as exc:  # noqa: BLE001 — 转换是增强路径,失败不阻断草稿生成
        native_sidecar = None
        errors.append(f"[node_05] 原生草稿转换失败,保留 storyline 格式: {exc!r}")

    # 4. 原子双写(content + info,Week 5 新增 — 剪映 5.9+ 期望二者一致)
    draft_dir = Path(draft_dir)
    draft_dir.mkdir(parents=True, exist_ok=True)
    write_result = writer(draft_dir, draft_content)
    # writer 返回值可能是 dict(safe_write_draft)或 None(legacy atomic_write_draft),
    # 兼容两种形态。
    log_tags = list(state.get("status_log", []) or [])
    if isinstance(write_result, dict) and write_result.get("jianying_running"):
        log_tags.append("[node_05] 剪映进程在跑,写入仍继续(告警不阻断)")
    draft_file = draft_dir / "draft_content.json"
    if native_sidecar is not None:
        # 私有元数据(storyline_meta/shot 映射/未落盘 BGM 占位)随目录存活,
        # 发布 copytree 自动携带(剪映忽略未知文件)
        try:
            write_sidecar(draft_dir, native_sidecar)
            # 阶段 1(2026-11-09 效果缺失修复):丢弃必须有痕 —— 原生转换把无法
            # 落地的数据(text 轨/unresolved 音频引用)写进 sidecar,这里同步把
            # 计数透出到 status_log,避免"产物看着成功、数据其实被丢"。
            dropped = native_sidecar.get("dropped") or {}
            if dropped:
                dropped_count = sum(
                    len(v) if isinstance(v, (list, dict)) else 1 for v in dropped.values()
                )
                names = ", ".join(sorted(str(k) for k in dropped))
                log_tags.append(
                    f"[node_05] 原生转换丢弃 {dropped_count} 项({names}),"
                    "详见 drafts/autovideo_storyline_source.json"
                )
        except Exception as exc:  # noqa: BLE001 — sidecar 失败不阻断草稿
            errors.append(f"[node_05] sidecar 写入失败(不阻断): {exc!r}")
    _emit_monitor_event(
        "draft.generated", detail={"draft_dir": str(draft_dir)}
    )

    # 4a. 阶段 1(人工关卡与剪映草稿导入):staging → 剪映草稿库发布注册。
    # 计划 §8 阶段 1:发布成功后正式草稿路径回填 state.draft_path,并把
    # _draft_dir_override 指到正式目录 —— 后续节点、snapshot②、英文分支 fork
    # 全部跟随同一份权威草稿。发布失败**降级不阻断**(error_log 记录,继续用
    # staging 草稿,与仓库"环境级失败降级纪律"一致)。
    published_dir: str | None = None
    publish_error: str | None = None
    if config.JY_DRAFT_PUBLISH_ENABLED and job_id:
        try:
            from draft_ops.draft_publisher import publish_draft

            publish_result = publish_draft(
                draft_dir,
                thread_id=str(job_id),
                duration_us=compute_draft_duration_us(draft_content),
            )
            published_dir = str(publish_result["published_dir"])
            draft_file = Path(publish_result["draft_content_path"])
            log_tags.append(f"draft.published:{published_dir}")
            _emit_monitor_event(
                "draft.published",
                detail={
                    "source": str(draft_dir),
                    "target": published_dir,
                    "duration_us": publish_result.get("duration_us"),
                    "reused": bool(publish_result.get("reused")),
                },
            )
        except Exception as exc:  # noqa: BLE001 — 发布是增强路径,任何失败都降级
            publish_error = repr(exc)
            errors.append(f"[node_05] 草稿发布失败,继续使用 staging 草稿: {exc}")
            log_tags.append(f"draft.publish_failed:{publish_error[:200]}")
            _emit_monitor_event(
                "draft.publish_failed", detail={"reason": publish_error[:200]}
            )

    # 5. 回填 manifest.draft_path(Phase 2 幂等键)
    if job_id:
        paths = OutputJobPaths.for_job(job_id=str(job_id), root=storyline_outputs_root())
        paths.ensure()
        manifest = load_manifest(paths) or build_manifest(
            job_id=str(job_id),
            video_path=str(video_path),
            config_snapshot={
                "WORKFLOW_ENV": WORKFLOW_ENV,
                "CANVAS_WIDTH": CANVAS_WIDTH,
                "CANVAS_HEIGHT": CANVAS_HEIGHT,
                "JIANYING_VERSION": JIANYING_VERSION,
            },
            storyline_session_id=state.get("storyline_session_id"),
            storyline_artifact_ids=[
                a.get("artifact_id", "") for a in (state.get("storyline_artifacts") or [])
            ],
            draft_path=str(draft_file),
        )
        manifest.draft_path = str(draft_file)
        # 重新算幂等键(因为 draft_path 进入 candidates)
        manifest.idempotency_key = compute_idempotency_key(
            job_id=str(job_id),
            input_sha256=manifest.input_sha256,
            config_sha256=manifest.config_sha256,
        )
        write_manifest(paths, manifest)

    out: dict = {
        **state,
        "draft_path": str(draft_file),
        "draft_encryption_status": status.value,
        "draft_version_strategy": strategy.value,
        "draft_source_normalized_path": normalized_path,
        "error_log": errors,
        # delta 语义:state.status_log 是 _append_unique reducer,这里带的既有
        # 条目会被去重(修复了改动前"剪映在跑"告警拼了 log 却从不返回的丢失问题)
        "status_log": log_tags,
    }
    if published_dir is not None:
        # 后续节点经 resolve_draft_dir 读 override → 全链路改写正式草稿
        out["_draft_dir_override"] = published_dir
        out["draft_published_dir"] = published_dir
    if publish_error is not None:
        out["draft_publish_error"] = publish_error
    return out


def _emit_monitor_event(event: str, *, detail: dict | None = None) -> None:
    """经 LangGraph custom stream 上报节点级观测事件(runner 归一为 node.progress)。

    图运行时外调用 ``get_stream_writer()`` 会 RuntimeError —— 观测事件绝不影响
    主流程,任何异常吞掉即可。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(
                {
                    "monitor_event": "progress",
                    "node_id": "generate_draft",
                    "detail": {"event": event, **(detail or {})},
                }
            )
    except Exception:  # noqa: BLE001 — 观测通道失败不阻断工作流
        pass


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _normalize_source_video(
    video_path: str, *, job_id: str | None = None
) -> tuple[str, str | None, list[str]]:
    """把源素材居中裁切到草稿画布比例。

    阶段五(9 工具迁移计划 §6.5):混剪素材宽高比不一致时(横屏素材直接进竖屏
    草稿),剪映会按画布拉伸,画面明显变形。这里先用 ``video_basic_operation``
    的 ``crop`` 子操作**居中裁切**到 CANVAS 比例 —— 宁可切掉两侧画面,
    也不让主体被拉长(混剪素材宁可裁不宁拉)。

    返回 ``(草稿用素材路径, 归一化产物路径或 None, 错误日志列表)``:

    - 开关关闭 / 文件不存在(合成路径、远程 URL、测试占位)→ 原样返回,零开销
    - 比例已在容差内 → 原样返回,``None``
    - 任何失败(ffmpeg 缺失、ffprobe 失败、裁切报错)→ 退回原路径 + 写错误日志,
      **不阻断草稿生成**(计划 §5.4 环境级失败降级纪律)

    注意:``storyline_plan`` 路径的素材引用在 canonical timeline 里,不在本步骤
    归一化范围内(改它要重写 plan,风险远大于收益);本步骤只管 ``video_input_path``
    这条 Mock / legacy 路径。
    """
    if not video_path or not config.DRAFT_SOURCE_NORMALIZE_ENABLED:
        return video_path, None, []

    source = Path(video_path)
    if not source.is_file():
        return video_path, None, []

    try:
        width, height = probe_video_size(source)
    except Exception as exc:
        return video_path, None, [f"[node_05] 素材归一化跳过:探测分辨率失败({exc})"]

    if width <= 0 or height <= 0:
        return video_path, None, [f"[node_05] 素材归一化跳过:非法分辨率 {width}x{height}"]

    canvas_ratio = CANVAS_WIDTH / CANVAS_HEIGHT
    src_ratio = width / height
    if abs(src_ratio / canvas_ratio - 1.0) <= config.DRAFT_SOURCE_ASPECT_TOLERANCE:
        # 比例已对齐(容差内):零开销放行
        return video_path, None, []

    # 居中取最大画布比例区域:太宽切左右,太高切上下
    if src_ratio > canvas_ratio:
        crop_h = height
        crop_w = int(round(height * canvas_ratio))
    else:
        crop_w = width
        crop_h = int(round(width / canvas_ratio))
    # libx264 + yuv420p 要求偶数边
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    crop_x = max(0, (width - crop_w) // 2)
    crop_y = max(0, (height - crop_h) // 2)

    out_root = storyline_outputs_root() / (str(job_id) if job_id else "unknown")
    output_path = out_root / "normalized_sources" / f"{source.stem}_{crop_w}x{crop_h}.mp4"

    ctx = RunContext(session_kind="pipeline")
    result = video_basic_operation(
        {
            "operation": "crop",
            "input_path": str(source),
            "width": crop_w,
            "height": crop_h,
            "x": crop_x,
            "y": crop_y,
            "output_path": str(output_path),
        },
        ctx,
    )
    if result.text.startswith("[ERROR]"):
        return video_path, None, [f"[node_05] 素材归一化失败,沿用原素材: {result.text}"]
    return str(output_path), str(output_path), []


def _build_from_canonical(storyline_plan: dict) -> dict:
    """``state['storyline_plan']`` → draft_content.json dict(走 mapper)。

    Phase 2 必跑 Pydantic 校验;失败抛 :class:`ContractInvalid` 让 node 早退。
    """
    # CanonicalTimeline.model_validate 会跑不变量校验(单调用,慢路径)
    canonical = CanonicalTimeline.model_validate(storyline_plan)
    return canonical_to_draft(canonical)


def _build_draft_content_legacy(
    shot_plan: dict, video_path: str | list[str]
) -> dict:
    """旧 ``_build_draft_content`` 行为保留(shot_plan Mock fallback 用)。

    ``video_path`` 可传单个路径(旧行为)或多个路径(多视频:shots 轮转分配)。
    """
    return {
        "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "materials": {"videos": _shot_plan_to_materials(shot_plan, video_path)},
        "tracks": _init_empty_tracks(),
    }


# 向后兼容别名:既有测试 ``test_node_05_generate_draft.py`` 仍以
# ``_build_draft_content`` 名字引用该函数,沿用即可。
_build_draft_content = _build_draft_content_legacy


def _shot_plan_to_materials(
    shot_plan: dict, video_path: str | list[str]
) -> list[dict]:
    """最小可用版:把每个 shot 的 video_ref 转为一个 material 条目。

    单路径:所有 shot 都用该路径(与改动前一致)。
    多路径:shot 按顺序轮转分配到各源视频(每个源都至少进成片)。
    """
    shots = shot_plan.get("shots", []) if isinstance(shot_plan, dict) else []
    if isinstance(video_path, str):
        paths = [video_path]
    else:
        paths = [str(p) for p in (video_path or []) if str(p or "").strip()]
    if not paths:
        paths = [""]
    materials: list[dict] = []
    for idx, shot in enumerate(shots):
        materials.append(
            {
                "id": f"video-{idx + 1}",
                "type": "video",
                "path": paths[idx % len(paths)],
                "shot_id": shot.get("id") if isinstance(shot, dict) else None,
                "start_s": shot.get("start_s") if isinstance(shot, dict) else None,
                "end_s": shot.get("end_s") if isinstance(shot, dict) else None,
            }
        )
    return materials


def _init_empty_tracks() -> list[dict]:
    """最小可用版:返回空轨道结构,片段填充留给后续周次(步骤 7 变速节点等)。"""
    return []


__all__ = ["generate_initial_jianying_draft"]
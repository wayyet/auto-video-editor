"""Phase 4/5 C 类旁支节点:storyline_generate_ai_transition(plan_v4 §5 阶段 5 / ADR-005)。

2026-11-12 noop 治理阶段2:从 Stub JSON 占位改造成 **FireRed 风格真实转场生成**
(设计文档 §2):相邻 group 边界配对 → 首尾帧抽取(ffmpeg)→ 文本 LLM 生成运动
提示词 → MiniMax Hailuo-02 生成真实 MP4 → ffprobe 复检 → 产物+缓存落盘。
单 pair 失败降级为硬切并留痕;**不再产生任何假 JSON 充当转场**(旧 Stub 路径删除)。

授权门禁(设计文档 §2.3,付费执行的第二道闸):
- ``STORYLINE_ENABLE_AI_TRANSITION`` 只表示功能可用(建图接线);
- 本次任务还必须有显式批准:state ``ai_transition_approved=true`` 或环境变量
  ``STORYLINE_AI_TRANSITION_APPROVED=true``(阶段6 创建台 UI 接确认控件);
- 未批准 → 只写估算(pair 数/预计时长/费用风险),**0 次 Provider 外呼**;
- 已批准但未配 Provider(``STORYLINE_AI_TRANSITION_PROVIDER`` 空/Key 缺失)→
  显式 ``provider_unconfigured``,同样 0 次外呼。

覆盖口径:``pair_scope`` 显式写进产物(默认 ``group_boundaries``,即本节点的
``_adjacent_pairs``;预留 ``all_adjacent`` 档位),产品端不误判覆盖范围。

⚠️ 本节点在 qa 重试回路里,会随 plan_timeline_pro **每轮重跑一次**。真实生成的
每个 pair 按输入哈希缓存(``ai_transitions/cache.json``),重试命中不重复付费。
状态标签必须固定(细节见本文件下方"状态标签"注释;qa 路由已改用
``storyline_qa_route_signal`` 信号字段,标签去重不再影响路由)。

输入:
- ``storyline_groups_artifact``:衔接点 = 前一组最后一个 clip → 后一组第一个 clip
- ``storyline_transition_plan``(仅参考):推荐转场作为提示随 pair 进入提示词
- ``ai_transition_approved``(可选):本次任务的付费生成授权

输出:
- ``storyline_ai_transition_artifact``:``ai_transition.json``,items 含
  ``video_path``(真实 MP4)/``status``(generated/degraded/skipped_*)/
  ``cache_hit`` 等字段;顶层带 ``pair_scope``/``approved``/``status``/``estimate``
- ``status_log`` append ``storyline_generate_ai_transition_done``(固定标签)

失败语义:整个节点 try/except,任何异常只写 ``error_log`` + ``_failed`` 标签,
不抛、不产出 artifact;下游 ``plan_timeline_ai_transition`` 保留基础时间线。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from state import WorkflowState
from storyline_capabilities.ai_transition_client import (
    is_ai_transition_enabled,
)
from storyline_capabilities.ai_transition_media import (
    TransitionFileCache,
    build_pair_cache_key,
    check_aspect_compatible,
    extract_frame,
    is_image,
)
from storyline_capabilities.ai_transition_prompt import compose_transition_prompt
from storyline_capabilities.ai_transition_provider import (
    PROVIDER_STATUS_GENERATED,
    ProviderNotConfigured,
    resolve_default_provider,
)
from nodes.storyline._common import (
    _read_groups,
    _read_media,
    append_status_tag,
    _resolve_outputs_root,
)

import config as _config

logger = logging.getLogger(__name__)

#: 条目状态(ai_transition.json items[].status;与 contract.AITransitionItem.status 对齐)
ITEM_STATUS_GENERATED = "generated"
ITEM_STATUS_DEGRADED = "degraded"
ITEM_STATUS_SKIPPED_BUDGET = "skipped_budget_exceeded"
ITEM_STATUS_SKIPPED_MAX_PAIRS = "skipped_max_pairs"
ITEM_STATUS_SKIPPED_ASPECT = "incompatible_aspect_ratio"
ITEM_STATUS_UNRESOLVABLE = "unresolvable_media"
#: 产物顶层状态
ART_STATUS_OK = "ok"
ART_STATUS_AWAITING = "awaiting_approval"
ART_STATUS_NO_PROVIDER = "provider_unconfigured"
ART_STATUS_NO_PAIRS = "no_pairs"


def _adjacent_pairs(groups: list[dict]) -> list[tuple[dict, dict]]:
    """相邻 group 的衔接点:前一组最后一个 clip → 后一组第一个 clip。

    返回的 clip 是**副本**,并补上所属 ``group_id``(用来对上转场方案)。
    """
    ends: list[tuple[dict, dict]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        clips = [c for c in (g.get("clips") or []) if isinstance(c, dict)]
        if clips:
            gid = str(g.get("group_id") or "")
            ends.append(({**clips[0], "group_id": gid}, {**clips[-1], "group_id": gid}))  # (第一个, 最后一个)
    return [(ends[i][1], ends[i + 1][0]) for i in range(len(ends) - 1)]


def _all_adjacent_pairs(groups: list[dict]) -> list[tuple[dict, dict]]:
    """``all_adjacent`` 档位(预留,设计文档 §2.2 任务1):组内相邻 + 组间边界,
    共 N-1 个 pair(FireRed 口径)。默认档 ``group_boundaries`` 不走这里。"""
    flat: list[dict] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id") or "")
        clips = [c for c in (g.get("clips") or []) if isinstance(c, dict)]
        flat.extend({**c, "group_id": gid} for c in clips)
    return [(flat[i], flat[i + 1]) for i in range(len(flat) - 1)]


def _transition_hints(plan: object) -> dict[str, dict[str, str]]:
    """把 ``storyline_transition_plan.items`` 按 ``group_id`` 建索引(仅参考)。

    格式不对 / 缺字段一律当作"没有提示",绝不抛。
    """
    out: dict[str, dict[str, str]] = {}
    items = plan.get("items") if isinstance(plan, dict) else None
    for it in items or []:
        if isinstance(it, dict) and it.get("group_id"):
            out[str(it["group_id"])] = {
                "transition_in": str(it.get("transition_in") or ""),
                "transition_out": str(it.get("transition_out") or ""),
            }
    return out


def _is_approved(state: WorkflowState) -> bool:
    """本次任务授权:state ``ai_transition_approved`` 或 env 配置任一为 true。"""
    if bool(state.get("ai_transition_approved")):
        return True
    return bool(getattr(_config, "STORYLINE_AI_TRANSITION_APPROVED", False))


def _resolve_clip_media(
    clip: dict, media_index: dict[str, dict]
) -> dict[str, Any]:
    """clip → 素材信息 {path, media_type, width, height}。

    解析链:clip 自带 path → media_id/source_media_id 查 load_media 索引
    (path / file_uri)。解析不到抛 KeyError(调用方降级该 pair)。
    """
    path = str(clip.get("path") or clip.get("media_path") or "").strip()
    media: dict[str, Any] = {}
    mid = str(clip.get("media_id") or clip.get("source_media_id") or "")
    if mid:
        media = media_index.get(mid) or {}
    if not path:
        path = str(media.get("path") or "").strip()
    if not path and media.get("file_uri"):
        from storyline.mapper import file_uri_to_path

        path = str(file_uri_to_path(str(media["file_uri"])))
    if not path:
        raise KeyError(f"clip {clip.get('clip_id')!r} 解析不到素材路径")
    meta = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
    width = int(media.get("width") or meta.get("width") or 0)
    height = int(media.get("height") or meta.get("height") or 0)
    media_type = str(media.get("media_type") or ("image" if is_image(path) else "video"))
    return {"path": path, "media_type": media_type, "width": width, "height": height}


def _skip_item(from_clip: dict, to_clip: dict, status: str, detail: str = "") -> dict:
    return {
        "from_clip_id": str(from_clip.get("clip_id") or ""),
        "to_clip_id": str(to_clip.get("clip_id") or ""),
        "duration_ms": 0,
        "provider": "",
        "is_stub": False,
        "error": detail or None,
        "artifact": None,
        "video_path": None,
        "status": status,
        "cache_hit": False,
    }


def storyline_generate_ai_transition_node(state: WorkflowState) -> dict:
    if not is_ai_transition_enabled():
        return {
            "status_log": append_status_tag(
                state, "storyline_generate_ai_transition_skipped"
            ),
        }

    try:
        outputs_root = _resolve_outputs_root(state)
        out_dir = outputs_root / "storyline"
        trans_dir = out_dir / "ai_transitions"
        out_dir.mkdir(parents=True, exist_ok=True)

        groups = _read_groups(state)
        hints = _transition_hints(state.get("storyline_transition_plan"))
        pair_scope = str(
            getattr(_config, "STORYLINE_AI_TRANSITION_PAIR_SCOPE", "group_boundaries")
        )
        pairs = (
            _all_adjacent_pairs(groups)
            if pair_scope == "all_adjacent"
            else _adjacent_pairs(groups)
        )
        approved = _is_approved(state)
        provider_name = str(
            getattr(_config, "STORYLINE_AI_TRANSITION_PROVIDER", "") or ""
        ).strip().lower()
        target_duration_ms = int(
            getattr(_config, "STORYLINE_AI_TRANSITION_DURATION_MS", 6000) or 6000
        )
        max_pairs = int(getattr(_config, "STORYLINE_AI_TRANSITION_MAX_PAIRS", 8) or 8)
        max_total_ms = int(
            getattr(_config, "STORYLINE_AI_TRANSITION_MAX_TOTAL_MS", 10000) or 10000
        )

        items: list[dict] = []
        warnings: list[str] = []
        estimate = {
            "pair_count": len(pairs),
            "pair_scope": pair_scope,
            "per_transition_ms": target_duration_ms,
            "estimated_total_ms": len(pairs) * target_duration_ms,
            "cost_note": "第三方 AIGC 生成按次计费,费用相对较高;确认前不会发起任何调用",
        }
        status = ART_STATUS_OK

        if not pairs:
            status = ART_STATUS_NO_PAIRS
        elif not approved:
            # 未批准:只估算,0 次 Provider 外呼(设计文档 §2.3)
            status = ART_STATUS_AWAITING
        else:
            try:
                provider = resolve_default_provider()
            except ProviderNotConfigured as e:
                provider = None
                warnings.append(str(e))
            if provider is None:
                status = ART_STATUS_NO_PROVIDER
                if not warnings:
                    warnings.append(
                        "未配置 STORYLINE_AI_TRANSITION_PROVIDER,真实生成不可用;"
                        "本节点按硬切降级,不产生占位产物"
                    )
            else:
                media_index = {
                    str(m.get("media_id") or ""): m for m in _read_media(state) if isinstance(m, dict)
                }
                cache = TransitionFileCache(trans_dir / "cache.json")
                generated_total_ms = 0
                generated_count = 0
                for from_clip, to_clip in pairs:
                    if generated_count >= max_pairs:
                        items.append(
                            _skip_item(from_clip, to_clip, ITEM_STATUS_SKIPPED_MAX_PAIRS)
                        )
                        continue
                    if generated_total_ms + target_duration_ms > max_total_ms:
                        items.append(
                            _skip_item(from_clip, to_clip, ITEM_STATUS_SKIPPED_BUDGET)
                        )
                        continue
                    try:
                        from_media = _resolve_clip_media(from_clip, media_index)
                        to_media = _resolve_clip_media(to_clip, media_index)
                    except (KeyError, TypeError, ValueError) as e:
                        warnings.append(f"素材解析失败: {e}")
                        items.append(
                            _skip_item(from_clip, to_clip, ITEM_STATUS_UNRESOLVABLE, str(e))
                        )
                        continue

                    if not check_aspect_compatible(
                        (from_media["width"], from_media["height"]),
                        (to_media["width"], to_media["height"]),
                    ):
                        from storyline_capabilities.ai_transition_media import (
                            MAX_ASPECT_RATIO_FACTOR,
                        )

                        items.append(
                            _skip_item(
                                from_clip,
                                to_clip,
                                ITEM_STATUS_SKIPPED_ASPECT,
                                "两侧素材宽高比差异超过 "
                                f"{MAX_ASPECT_RATIO_FACTOR:.1f},跳过该衔接点",
                            )
                        )
                        continue

                    try:
                        frames_dir = trans_dir / "frames"
                        from_in = int(from_clip.get("source_in_ms") or 0)
                        from_out = int(from_clip.get("source_out_ms") or 0)
                        to_in = int(to_clip.get("source_in_ms") or 0)
                        pair_tag = f"{from_clip.get('clip_id')}__{to_clip.get('clip_id')}"
                        first_frame = extract_frame(
                            from_media["path"],
                            at_ms=max(from_in, from_out),
                            output_path=frames_dir / f"from_{pair_tag}.png",
                            position="end",
                        )
                        last_frame = extract_frame(
                            to_media["path"],
                            at_ms=to_in,
                            output_path=frames_dir / f"to_{pair_tag}.png",
                            position="exact",
                        )

                        hint_txt = " / ".join(
                            h
                            for h in (
                                hints.get(str(from_clip.get("group_id") or ""), {}).get("transition_out", ""),
                                hints.get(str(to_clip.get("group_id") or ""), {}).get("transition_in", ""),
                            )
                            if h
                        )
                        prompt, prompt_method = compose_transition_prompt(
                            from_clip=from_clip,
                            to_clip=to_clip,
                            user_request=(
                                str(state.get("ai_transition_user_request") or "")
                                or (f"推荐转场提示: {hint_txt}" if hint_txt else "")
                            ),
                        )
                        cache_key = build_pair_cache_key(
                            from_clip_id=str(from_clip.get("clip_id") or ""),
                            to_clip_id=str(to_clip.get("clip_id") or ""),
                            # 大视频整文件 sha256 每轮代价过高;用 路径+大小+mtime
                            # 指纹(任务内确定性,文件变化即失效;跨任务缓存本就
                            # 按 job 隔离,设计文档 §2.3 的"素材 hash"在本项目
                            # 落为该指纹)
                            from_media_sha=f"{from_media['path']}|{Path(from_media['path']).stat().st_size}|{int(Path(from_media['path']).stat().st_mtime_ns)}",
                            to_media_sha=f"{to_media['path']}|{Path(to_media['path']).stat().st_size}|{int(Path(to_media['path']).stat().st_mtime_ns)}",
                            from_at_ms=max(from_in, from_out),
                            to_at_ms=to_in,
                            prompt=prompt,
                            provider=provider.name,
                            model=provider.model,
                            duration_ms=target_duration_ms,
                            resolution=(from_media["width"] or 0, from_media["height"] or 0),
                        )
                        cached = cache.get(cache_key)
                        if cached is not None:
                            items.append({**cached, "cache_hit": True})
                            generated_total_ms += int(cached.get("duration_ms") or 0)
                            generated_count += 1
                            continue

                        video_path = trans_dir / f"transition_{cache_key[:16]}.mp4"
                        result = provider.generate_transition(
                            first_frame=first_frame,
                            last_frame=last_frame,
                            prompt=prompt,
                            duration_ms=target_duration_ms,
                            resolution=(from_media["width"] or 0, from_media["height"] or 0),
                            output_path=video_path,
                        )
                        if result.status != PROVIDER_STATUS_GENERATED or result.path is None:
                            # 单 pair 失败 → 降级硬切,留痕;不伪造产物
                            items.append(
                                _skip_item(
                                    from_clip,
                                    to_clip,
                                    ITEM_STATUS_DEGRADED,
                                    result.error or "provider 返回失败",
                                )
                            )
                            warnings.append(
                                f"pair {pair_tag} 生成失败,已降级硬切: "
                                f"{result.error or 'unknown'}"
                            )
                            continue

                        item = {
                            "from_clip_id": str(from_clip.get("clip_id") or ""),
                            "to_clip_id": str(to_clip.get("clip_id") or ""),
                            "duration_ms": int(result.duration_ms),
                            "provider": result.provider or provider.name,
                            "is_stub": False,
                            "error": None,
                            "artifact": str(result.path),
                            "video_path": str(result.path),
                            "status": ITEM_STATUS_GENERATED,
                            "cache_hit": False,
                            "prompt_method": prompt_method,
                            "width": int(result.width),
                            "height": int(result.height),
                            "fps": float(result.fps),
                            "sha256": result.sha256,
                            "request_id": result.request_id,
                        }
                        items.append(item)
                        cache.put(cache_key, {**item, "cache_hit": False})
                        generated_total_ms += int(result.duration_ms)
                        generated_count += 1
                    except Exception as e:  # noqa: BLE001 — 单 pair 失败降级为硬切
                        logger.warning("ai_transition pair 处理失败: %r", e)
                        items.append(
                            _skip_item(
                                from_clip,
                                to_clip,
                                ITEM_STATUS_DEGRADED,
                                repr(e),
                            )
                        )
                        warnings.append(f"pair {from_clip.get('clip_id')}->{to_clip.get('clip_id')} 降级: {e!r}")

        out_path = out_dir / "ai_transition.json"
        out_path.write_text(
            json.dumps(
                {
                    "items": items,
                    "provider": provider_name or "none",
                    "is_stub": False,
                    "pair_scope": pair_scope,
                    "approved": approved,
                    "status": status,
                    "estimate": estimate,
                    "warnings": warnings,
                },
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        logger.info(
            "generate_ai_transition: pairs=%d items=%d status=%s approved=%s provider=%s",
            len(pairs), len(items), status, approved, provider_name or "none",
        )
    except Exception as e:  # noqa: BLE001 — 旁支节点,绝不拖垮主线
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:generate_ai_transition] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_generate_ai_transition_failed"
            ),
        }

    return {
        "storyline_ai_transition_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_generate_ai_transition_done"),
    }

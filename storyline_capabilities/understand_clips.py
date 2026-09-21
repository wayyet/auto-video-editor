"""本地化 understand_clips(plan_v4 §5 阶段 2 / B 类第一批)。

vendored copy ``open_storyline.nodes.core_nodes.understand_clips``:对每个 clip
调 VLM,产出 ``clip_captions`` + ``overall``。本模块去 FireRed 依赖,纯函数化。

设计纪律(plan §5 阶段 2):
1. **Prompt 文件 cp**:zh / en 双语保留(``prompts/understand_clips/{zh,en}/``)
2. **LLM 调用**走 ``vlm_client.LLMClient`` Protocol,可注入
3. **绝不吞错**(plan §4.4):vlm 抛 / parse 失败时返回 error_log 结构化,默认
   aes_score=-1.0(对照 vendored 修复 UnboundLocalError 的补丁)

默认 LLM 是 ``StubLLMClient``,阶段 5 接入真实 AI Gateway。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from storyline_capabilities.vlm_client import (
    LLMClient,
    StubLLMClient,
    chat_json,
    chat_text,
    parse_json_loose,
)
from storyline_capabilities.prompts import render_prompt


_SYSTEM_DETAIL = "system_detail"
_USER_DETAIL = "user_detail"
_SYSTEM_OVERALL = "system_overall"
_USER_OVERALL = "user_overall"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:  # noqa: BLE001
        return default


def _media_for_clip(clip: dict[str, Any], media_item: dict[str, Any]) -> list[dict[str, Any]]:
    """按 clip kind 拼 media 列表(图片或带 in_sec/out_sec 的视频段)。"""
    kind = str(clip.get("kind", "") or "").strip().lower()
    path = str(media_item.get("path", "") or "").strip()
    if not path:
        return []
    if kind == "image":
        return [{"path": path}]
    if kind == "video":
        src = clip.get("source_ref") or {}
        in_sec = _safe_float(src.get("start", 0) / 1000.0, 0.0)
        if src.get("end") is not None:
            out_sec = _safe_float(src.get("end", 0) / 1000.0, in_sec)
        else:
            dur = _safe_float(src.get("duration", 0.0), 0.0)
            out_sec = in_sec + max(0.0, dur)
        if out_sec <= in_sec:
            out_sec = in_sec + 0.1
        return [{"path": path, "in_sec": in_sec, "out_sec": out_sec}]
    return []


def understand_clips(
    *,
    split_shots_artifact: dict[str, Any],
    media_artifact: dict[str, Any],
    lang: str = "zh",
    max_retries: int = 2,
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """理解每个 clip,产出 ``clip_captions`` + ``overall``。

    Args:
        split_shots_artifact: ``split_shots_for_media_list`` 的输出;
            形如 ``{"shots": {media_id: [clip_dict, ...]}}``。
        media_artifact: ``load_media`` 输出;形如 ``{"media": [media_dict, ...]}``。
        lang: ``"zh"`` / ``"en"``,决定用哪一份 prompt。
        max_retries: 单个 clip 的 VLM 重试次数(plan 经验值:2)。
        client: 注入位;None 时走 ``vlm_client.get_default_client()``。

    Returns:
        dict 形如 vendored 兼容 ``{"clip_captions": [...], "overall": "..."}``。
        失败时 ``clip_captions[*].aes_score = -1.0``(plan §1.3 案例二防复发)。
    """
    media_by_id: dict[str, dict[str, Any]] = {}
    for m in (media_artifact.get("media") or []):
        if m.get("media_id"):
            media_by_id[str(m["media_id"])] = m

    # 拉平 shots(每个 clip 都跑一次)
    clips: list[dict[str, Any]] = []
    for media_id, media_clips in (
        (split_shots_artifact.get("shots") or {}).items()
    ):
        for c in media_clips or []:
            clip = dict(c)
            if not clip.get("media_id"):
                clip["media_id"] = media_id
            clips.append(clip)

    sys_detail = render_prompt("understand_clips", _SYSTEM_DETAIL, lang=lang)
    user_detail = render_prompt("understand_clips", _USER_DETAIL, lang=lang)

    captions: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    vlm_errors: list[str] = []

    for clip in clips:
        clip_id = str(clip.get("clip_id") or "").strip() or "(unknown_clip)"
        kind = str(clip.get("kind", "") or "").strip().lower()
        src = clip.get("source_ref") or {}
        media_id = str(src.get("media_id") or clip.get("media_id") or "")
        media_item = media_by_id.get(media_id)

        out: dict[str, Any] = {"clip_id": clip_id}

        if not media_item:
            out["caption"] = f"Error: Media not found for media_id={media_id}"
            out["aes_score"] = -1.0
            captions.append(out)
            continue
        if not str(media_item.get("path", "") or ""):
            out["caption"] = f"Error: No path specified for media_id={media_id}"
            out["aes_score"] = -1.0
            captions.append(out)
            continue

        media = _media_for_clip(clip, media_item)
        if not media:
            out["caption"] = f"Error: Clip kind not supported: {kind}"
            out["aes_score"] = -1.0
            captions.append(out)
            continue

        raw: Optional[str] = None
        last_exc: Optional[BaseException] = None
        for attempt in range(max_retries + 1):
            try:
                raw = (client or StubLLMClient()).chat(
                    system_prompt=sys_detail,
                    user_prompt=user_detail,
                    media=media,
                    temperature=0.3,
                    top_p=0.9,
                    max_tokens=4096,
                )
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < max_retries:
                    continue
        if raw is None:
            out["caption"] = "Error: VLM request failed"
            out["aes_score"] = -1.0
            if last_exc is not None:
                vlm_errors.append(f"{clip_id}:{last_exc!r}")
            captions.append(out)
            continue

        obj = parse_json_loose(raw)
        if not obj:
            text = raw.strip() if raw else ""
            out["caption"] = text if text else "Error: Unable to parse model output"
            out["aes_score"] = -1.0
            parse_errors.append(clip_id)
            captions.append(out)
            continue

        out["caption"] = str(obj.get("caption", "") or "").strip()
        out["source_ref"] = {"media_id": media_id}
        captions.append(out)

    # Overall summary
    desc_lines = [
        f"- {d.get('clip_id', '?')}: {d.get('caption', '')}" for d in captions
    ]
    overall: str = ""
    if desc_lines and client is not None:
        sys_overall = render_prompt(
            "understand_clips", _SYSTEM_OVERALL, lang=lang
        )
        user_overall = render_prompt(
            "understand_clips",
            _USER_OVERALL,
            lang=lang,
            clips_captions="\n".join(desc_lines),
        )
        try:
            overall = chat_text(
                system_prompt=sys_overall,
                user_prompt=user_overall,
                media=None,
                temperature=0.3,
                max_tokens=4096,
                client=client,
            )
        except Exception as e:  # noqa: BLE001
            overall = f"Error: Summary generation failed: {type(e).__name__}: {e}"

    return {
        "clip_captions": captions,
        "overall": overall,
        "vlm_errors": vlm_errors,
        "parse_errors": parse_errors,
    }


__all__ = ["understand_clips"]

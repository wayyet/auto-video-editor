"""Phase 4/5 C 类本地化节点:storyline_local_asr(plan_v4 §5 阶段 5 条件旁支)。

``LM → local_asr`` 边的终点(条件:``有语音``)。

阶段 0 时是 ``_mcp_passthrough`` 壳子;阶段 5 起改为调本地化的
``storyline_capabilities.asr_runner.transcribe_media`` — 默认 stub(返回空
segments),``STORYLINE_ASR_MODE=vendored`` 时走 vendored venv subprocess 调
funasr/torchaudio。

输入:
- ``storyline_media_artifact`` (JSON 路径,含 ``media`` 数组)

输出:
- ``storyline_asr_artifact`` (ASR JSON 路径)
- ``status_log`` append ``storyline_local_asr_done``
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from state import WorkflowState
from storyline_capabilities.asr_runner import (
    ASR_MODE,
    transcribe_media,
)
from nodes.storyline._common import (
    _read_media,
    append_status_tag,
    _resolve_outputs_root,
)

logger = logging.getLogger(__name__)


# ``_read_media`` 已收敛到 ``nodes/storyline/_common.py``(与其他 storyline 节点共用一份)。
# 失败语义与收敛前逐条一致:路径为空 / 文件不存在 / 非法 JSON 一律降级成空列表,
# 不抛异常 —— 本节点的下游(stub 写空 ASR)依赖这个"永远拿得到 list"的契约。
def _pick_audio_media(media: list[dict]) -> dict | None:
    """挑第一个 ``has_audio=True`` 的视频,否则第一个视频;无则 None。"""
    for m in media:
        if (m.get("media_type") or "").lower() == "video":
            meta = m.get("metadata") or {}
            if isinstance(meta, dict) and meta.get("has_audio"):
                return m
    for m in media:
        if (m.get("media_type") or "").lower() == "video":
            return m
    return None


def _pick_audio_media_list(media: list[dict]) -> list[dict]:
    """挑出**全部**视频素材(有音轨的排前面)。

    多视频:每个源视频都参与 ASR(单源时与 ``_pick_audio_media`` 等价)。
    """
    with_audio: list[dict] = []
    without_audio: list[dict] = []
    for m in media:
        if (m.get("media_type") or "").lower() != "video":
            continue
        meta = m.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("has_audio"):
            with_audio.append(m)
        else:
            without_audio.append(m)
    return with_audio + without_audio


def storyline_local_asr_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    media = _read_media(state)
    # 多视频:每个源视频都转写;单视频时 == [_pick_audio_media(...)]
    videos = _pick_audio_media_list(media)

    # 1. 无视频 → 写空 ASR(stub),不写 error_log(因为可能根本没语音)
    if not videos:
        empty_path = out_dir / "asr.json"
        empty_path.write_text(
            json.dumps(
                {"segments": [], "text": "", "lang": "zh", "provider": "stub"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "storyline_asr_artifact": str(empty_path),
            "status_log": append_status_tag(state, "storyline_local_asr_skipped"),
        }

    multi = len(videos) > 1
    merged_segments: list[dict] = []
    merged_texts: list[str] = []
    errors: list[str] = []
    total_segments = 0
    artifact_path: Path | None = None
    provider = "stub"

    for idx, video in enumerate(videos):
        media_id = str(video.get("media_id") or f"media_{idx + 1:04d}")
        # 每个源视频用独立输出目录(否则 asr_in/asr_out 会互相覆盖)
        target_dir = out_dir / f"asr_{media_id}" if multi else out_dir
        try:
            result = transcribe_media(
                media_path=Path(str(video.get("path") or "")),
                output_dir=target_dir,
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"[storyline:local_asr] TOOL_EXECUTION_FAILED: {e!r}")
            continue

        provider = str(result.get("provider") or provider)
        if result.get("error"):
            errors.append(f"[storyline:local_asr] ASR_PARTIAL: {result['error']}")

        segs = result.get("segments") or []
        total_segments += len(segs)
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            item = dict(seg)
            # 带上源标识 + 源内时间(便于下游映射到成片时间轴)
            item["media_id"] = media_id
            item["source_media"] = str(video.get("path") or "")
            merged_segments.append(item)

        raw_artifact = str(result.get("artifact") or "")
        if raw_artifact and Path(raw_artifact).exists():
            artifact_path = Path(raw_artifact)
        elif artifact_path is None:
            artifact_path = Path(raw_artifact or (out_dir / "asr.json"))

    if errors and not total_segments:
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                *errors,
            ],
            "status_log": append_status_tag(
                state, "storyline_local_asr_failed"
            ),
        }

    # 多视频:合并产物(asr.json),单段带 media_id + 源内时间
    if multi:
        merged_path = out_dir / "asr.json"
        merged_path.write_text(
            json.dumps(
                {
                    "segments": merged_segments,
                    "text": "\n".join(merged_texts),
                    "lang": "zh",
                    "provider": provider,
                    "source_count": len(videos),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        artifact_path = merged_path

    # 3. 写产物 + delta
    if artifact_path is None:
        artifact_path = out_dir / "asr.json"
    if not artifact_path.exists():
        # 防御:transcribe 没写出文件 → 补一份 stub,标记 failed
        artifact_path.write_text(
            json.dumps(
                {"segments": [], "text": "", "lang": "zh", "provider": provider},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # 2026-10:单视频时,把被转写素材的 ID 写进产物顶层(多视频时每段已自带 media_id)。
    # plan_timeline_pro 回填 Clip.transcript 时靠它按 source_media_id 对号入座
    # (单视频 + 图片混排时必需)。失败只记 warning,不影响产物本身。
    if not multi:
        try:
            single_id = str(videos[0].get("media_id") or "media_0001")
            doc = json.loads(artifact_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                doc["media_id"] = single_id
                artifact_path.write_text(
                    json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8"
                )
        except (OSError, ValueError) as e:
            logger.warning("local_asr: 写入 media_id 失败(忽略): %r", e)

    delta_log = [
        "storyline_local_asr_done",
        f"storyline_local_asr_mode={ASR_MODE}",
        f"storyline_local_asr_segments={total_segments}",
    ]
    delta: dict = {
        "storyline_asr_artifact": str(artifact_path),
        "status_log": append_status_tag(state, *delta_log),
    }

    if errors:
        delta["error_log"] = [
            *list(state.get("error_log", []) or []),
            *errors,
        ]
    return delta
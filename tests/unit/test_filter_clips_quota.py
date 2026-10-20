"""``filter_clips`` 多源保底单测(2026-10 多选导入)。

背景:``filter_clips`` 按 ``aes_score`` 对**池化**后的所有 clip 排序,保留前
``keep_ratio``。多视频下,一个高质量视频会把其它源全部挤掉 —— 那些源在成片里
就彻底消失了。故加"每源至少保留 1 个 clip"的保底。
"""
from __future__ import annotations

from storyline_capabilities.filter_clips import filter_clips
from storyline_capabilities.vlm_client import StubLLMClient


def _cap(clip_id: str, score: float, media_id: str) -> dict:
    return {
        "clip_id": clip_id,
        "caption": f"caption {clip_id}",
        "aes_score": score,
        "media_id": media_id,
    }


def test_multi_source_keeps_at_least_one_clip_per_source() -> None:
    """3 个源各 2 clip,keep_ratio=0.2 → 每个源至少保留 1 个。"""
    captions = [
        _cap("clip_0001", 0.9, "media_0001"),
        _cap("clip_0002", 0.8, "media_0001"),
        _cap("clip_0003", 0.2, "media_0002"),
        _cap("clip_0004", 0.1, "media_0002"),
        _cap("clip_0005", 0.05, "media_0003"),
        _cap("clip_0006", 0.01, "media_0003"),
    ]
    res = filter_clips(
        understanding_artifact={"clip_captions": captions},
        keep_ratio=0.2,
        client=StubLLMClient(default_caption="{}"),  # 不覆盖
    )
    kept_sources = {c["media_id"] for c in res["filtered_clips"]}
    assert kept_sources == {"media_0001", "media_0002", "media_0003"}, (
        "每个源都应至少保留 1 个 clip"
    )
    # 每源保留的是该源得分最高的那个
    by_source = {c["media_id"]: c for c in res["filtered_clips"]}
    assert by_source["media_0002"]["clip_id"] == "clip_0003"
    assert by_source["media_0003"]["clip_id"] == "clip_0005"


def test_single_source_equivalent_to_legacy() -> None:
    """单源时结果与"纯 top-N"一致(保底是空操作)。"""
    captions = [_cap(f"c{i}", float(10 - i), "media_0001") for i in range(5)]
    res = filter_clips(
        understanding_artifact={"clip_captions": captions},
        keep_ratio=0.4,
        client=StubLLMClient(default_caption="{}"),
    )
    assert res["method"] == "score_topN"
    assert len(res["filtered_clips"]) == 2
    assert res["filtered_clips"][0]["aes_score"] >= res["filtered_clips"][-1]["aes_score"]
    assert len(res["dropped_clips"]) == 3


def test_no_media_id_is_treated_as_single_source() -> None:
    """没有 media_id(老数据)→ 视为单源,行为与改动前一致。"""
    captions = [
        {"clip_id": f"c{i}", "caption": "x", "aes_score": float(10 - i)}
        for i in range(5)
    ]
    res = filter_clips(
        understanding_artifact={"clip_captions": captions},
        keep_ratio=0.4,
        client=StubLLMClient(default_caption="{}"),
    )
    assert len(res["filtered_clips"]) == 2
    assert len(res["dropped_clips"]) == 3


def test_source_floor_applies_to_llm_overlay_too() -> None:
    """LLM 覆盖时也要保底(否则 LLM 丢掉整个源 → 该源彻底消失)。"""
    captions = [
        _cap("clip_0001", 0.9, "media_0001"),
        _cap("clip_0002", 0.8, "media_0001"),
        _cap("clip_0003", 0.2, "media_0002"),
    ]
    client = StubLLMClient(
        default_caption='{"keep_clip_ids": ["clip_0001"], "drop_clip_ids": []}'
    )
    res = filter_clips(
        understanding_artifact={"clip_captions": captions},
        client=client,
    )
    kept_sources = {c["media_id"] for c in res["filtered_clips"]}
    assert "media_0002" in kept_sources, "被 LLM 漏掉的源仍应保底保留 1 个 clip"

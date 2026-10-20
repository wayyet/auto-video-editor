"""多视频下 ``clip_id`` 必须全局唯一(2026-10 多选导入)。

背景:``frame_difference_split_shots`` 在**单个视频内**把 clip 重编号为
``clip_0001, clip_0002, ...``。N 个视频 → N 组同号 clip,而下游
``filter_clips`` 把 ``clip_id`` 当 LLM 决策标识符 → 决策歧义(误保留/误丢弃)。

本测试用桩替掉逐视频切分(真切分要 ffmpeg 抽帧,太重),只验证**合并阶段的
全局重编号**这一层契约。
"""
from __future__ import annotations

from typing import Any

import pytest

import storyline_capabilities.split_shots as ss


@pytest.fixture
def stub_split(monkeypatch: pytest.MonkeyPatch):
    """把 ``split_shots`` 换成桩:每个视频返回固定 3 个 clip(按视频内编号)。"""

    def _fake(media_artifact: Any, **kwargs: Any) -> dict:
        media_id = ""
        if isinstance(media_artifact, dict):
            media_id = str(media_artifact.get("media_id") or "")
        return {
            "clips": [
                {"clip_id": f"clip_{i:04d}", "media_id": media_id}
                for i in range(1, 4)
            ],
            "method": "frame_diff",
        }

    monkeypatch.setattr(ss, "split_shots", _fake)
    return _fake


def test_two_videos_get_globally_unique_clip_ids(stub_split) -> None:
    """2 个视频各 3 clip → 6 个 clip_id 全局唯一。"""
    media_list = [
        {"media_id": "media_0001", "path": "E:/a.mp4", "media_type": "video"},
        {"media_id": "media_0002", "path": "E:/b.mp4", "media_type": "video"},
    ]
    result = ss.split_shots_for_media_list(media_list)

    all_ids: list[str] = []
    for clips in result["shots"].values():
        all_ids.extend(c["clip_id"] for c in clips)
    assert len(all_ids) == 6
    assert len(set(all_ids)) == 6, f"clip_id 必须全局唯一,实际: {all_ids}"
    # 顺序 = 媒体顺序 + 视频内顺序
    assert all_ids == [f"clip_{i:04d}" for i in range(1, 7)]


def test_single_video_keeps_original_ids(stub_split) -> None:
    """单视频时 clip_id 与改动前**完全一致**(clip_0001..clip_0003)。"""
    media_list = [
        {"media_id": "media_0001", "path": "E:/a.mp4", "media_type": "video"},
    ]
    result = ss.split_shots_for_media_list(media_list)
    ids = [c["clip_id"] for c in result["shots"]["media_0001"]]
    assert ids == ["clip_0001", "clip_0002", "clip_0003"]


def test_non_video_media_skipped(stub_split) -> None:
    """非视频素材不进 shots(与改动前一致)。"""
    media_list = [
        {"media_id": "media_0001", "path": "E:/a.mp4", "media_type": "video"},
        {"media_id": "media_0002", "path": "E:/b.png", "media_type": "image"},
    ]
    result = ss.split_shots_for_media_list(media_list)
    assert set(result["shots"].keys()) == {"media_0001"}

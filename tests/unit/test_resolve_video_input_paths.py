"""``resolve_video_input_paths`` / ``video_input_paths_key`` 单测。

这是**多视频导入的单一事实源**:所有需要"本次流水线的全部源视频"的节点都走它。
优先级:``state["video_input_paths"]`` > ``[state["video_input_path"]]`` > ``[]``。
"""
from __future__ import annotations

from nodes.storyline._common import resolve_video_input_paths, video_input_paths_key


def test_explicit_list_wins_over_single_value() -> None:
    """显式列表优先于单值字段。"""
    state = {
        "video_input_path": "E:/a.mp4",
        "video_input_paths": ["E:/a.mp4", "E:/b.mp4"],
    }
    assert resolve_video_input_paths(state) == ["E:/a.mp4", "E:/b.mp4"]


def test_falls_back_to_single_value() -> None:
    """没有列表 → 回退单值(老 checkpoint / 老 initial_state)。"""
    assert resolve_video_input_paths({"video_input_path": "E:/a.mp4"}) == ["E:/a.mp4"]


def test_empty_when_nothing_available() -> None:
    """什么都没有 → 空列表(不抛异常)。"""
    assert resolve_video_input_paths({}) == []
    assert resolve_video_input_paths({"video_input_path": ""}) == []
    assert resolve_video_input_paths({"video_input_path": None}) == []  # type: ignore[dict-item]


def test_filters_empty_and_non_string_entries() -> None:
    """空串 / None / 非字符串元素一律过滤。"""
    state = {
        "video_input_path": "E:/a.mp4",
        "video_input_paths": ["E:/a.mp4", "", None, 123, "  E:/b.mp4  "],  # type: ignore[list-item]
    }
    assert resolve_video_input_paths(state) == ["E:/a.mp4", "E:/b.mp4"]


def test_deduplicates_preserving_first_occurrence() -> None:
    """重复路径按首次出现保留(第一个 = 主视频)。"""
    state = {"video_input_paths": ["E:/a.mp4", "E:/b.mp4", "E:/a.mp4"]}
    assert resolve_video_input_paths(state) == ["E:/a.mp4", "E:/b.mp4"]


def test_empty_list_falls_back_to_single_value() -> None:
    """列表为空 → 回退单值(而不是返回空)。"""
    state = {"video_input_path": "E:/a.mp4", "video_input_paths": []}
    assert resolve_video_input_paths(state) == ["E:/a.mp4"]


def test_paths_key_is_stable_and_order_independent() -> None:
    """幂等键原料:排序后拼接 → 顺序变化不影响键;单视频时等于该路径本身。"""
    assert video_input_paths_key(["E:/a.mp4"]) == "E:/a.mp4"
    assert video_input_paths_key(["E:/b.mp4", "E:/a.mp4"]) == video_input_paths_key(
        ["E:/a.mp4", "E:/b.mp4"]
    )
    assert video_input_paths_key([]) == ""

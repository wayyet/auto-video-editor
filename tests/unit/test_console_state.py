"""web/console_state.py 单元测试(第二期阶段 1)。

覆盖计划 §12.1 的 thread_id 与 State 行:
- thread_id 唯一性和合法字符;
- State:单视频、多视频、提示词、参数;
- 提示词绝不写入 storyline_targets.query(防误触网页话题搜索分支);
- JSON 落盘往返。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web.console_state import (  # noqa: E402
    build_initial_state,
    is_valid_thread_id,
    new_thread_id,
    write_initial_state_json,
)
from web.console_uploads import CONSOLE_THREAD_ID_RE  # noqa: E402


class TestThreadId:
    def test_format_matches_frozen_rule(self):
        tid = new_thread_id()
        assert CONSOLE_THREAD_ID_RE.match(tid), tid
        assert is_valid_thread_id(tid)

    def test_unique_across_calls(self):
        ids = {new_thread_id() for _ in range(200)}
        assert len(ids) == 200

    @pytest.mark.parametrize(
        "bad", ["", "video-001", "console-XYZ", "../escape", "console-12345"]
    )
    def test_invalid_shapes_rejected(self, bad):
        assert not is_valid_thread_id(bad)


class TestBuildInitialState:
    TID = "console-" + "0123456789ab"

    def _videos(self, tmp_path: Path, count: int) -> list[str]:
        out = []
        for i in range(1, count + 1):
            p = tmp_path / "inputs" / f"{i:04d}.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            out.append(str(p))
        return out

    def test_single_video_mapping(self, tmp_path):
        videos = self._videos(tmp_path, 1)
        state = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="剪一条旅行短片",
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path,
        )
        assert state["session_id"] == self.TID
        assert state["video_input_path"] == videos[0]
        assert state["video_input_paths"] == videos
        assert state["storyline_outputs_root"] == str((tmp_path / self.TID).resolve())
        targets = state["storyline_targets"]
        assert targets["user_request"] == "剪一条旅行短片"
        assert targets["script_request"] == "剪一条旅行短片"
        assert targets["target_duration_ms"] == 35000
        assert targets["ratio"] == "9:16"
        assert targets["lang"] == "zh"
        assert state["error_log"] == []

    def test_multi_video_first_is_video_input_path(self, tmp_path):
        videos = self._videos(tmp_path, 3)
        state = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="p",
            target_duration_ms=30000, ratio="16:9", lang="en",
            outputs_root=tmp_path,
        )
        assert state["video_input_path"] == videos[0]
        assert state["video_input_paths"] == videos

    def test_prompt_never_written_to_query(self, tmp_path):
        """防漂移:query 键只属于网页话题搜索分支,提示词绝不能落进去。"""
        videos = self._videos(tmp_path, 1)
        state = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="洛阳博物馆一日游",
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path,
        )
        assert "query" not in state["storyline_targets"]

    def test_frozen_key_set(self, tmp_path):
        """State 键集合冻结(顶层 + targets):白名单责任在创建台。"""
        videos = self._videos(tmp_path, 1)
        state = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="p",
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path,
        )
        assert set(state.keys()) == {
            "session_id", "video_input_path", "video_input_paths",
            "storyline_outputs_root", "storyline_targets", "error_log",
        }
        assert set(state["storyline_targets"].keys()) == {
            "user_request", "script_request", "target_duration_ms", "ratio", "lang",
            "speed_multiplier",
        }

    def test_speed_multiplier_default_and_passthrough(self, tmp_path):
        """2026-11-14 快进速度:缺省 1.0(不启用);显式值透传 storyline_targets。"""
        videos = self._videos(tmp_path, 1)
        state = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="p",
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path,
        )
        assert state["storyline_targets"]["speed_multiplier"] == 1.0
        state2 = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="p",
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path, speed_multiplier=2.5,
        )
        assert state2["storyline_targets"]["speed_multiplier"] == 2.5

    def test_paths_are_absolute(self, tmp_path):
        videos = self._videos(tmp_path, 1)
        state = build_initial_state(
            thread_id=self.TID, video_paths=videos, prompt="p",
            target_duration_ms=35000, ratio="9:16", lang="zh",
            outputs_root=tmp_path,
        )
        assert Path(state["video_input_path"]).is_absolute()
        assert Path(state["storyline_outputs_root"]).is_absolute()

    def test_empty_prompt_or_videos_rejected(self, tmp_path):
        videos = self._videos(tmp_path, 1)
        with pytest.raises(ValueError):
            build_initial_state(
                thread_id=self.TID, video_paths=videos, prompt="   ",
                target_duration_ms=35000, ratio="9:16", lang="zh",
                outputs_root=tmp_path,
            )
        with pytest.raises(ValueError):
            build_initial_state(
                thread_id=self.TID, video_paths=[], prompt="p",
                target_duration_ms=35000, ratio="9:16", lang="zh",
                outputs_root=tmp_path,
            )

    def test_invalid_thread_id_rejected(self, tmp_path):
        videos = self._videos(tmp_path, 1)
        with pytest.raises(ValueError):
            build_initial_state(
                thread_id="video-001", video_paths=videos, prompt="p",
                target_duration_ms=35000, ratio="9:16", lang="zh",
                outputs_root=tmp_path,
            )


class TestWriteInitialStateJson:
    def test_round_trip_utf8_no_bom(self, tmp_path):
        state = {
            "session_id": "console-0123456789ab",
            "storyline_targets": {"user_request": "中文提示词"},
            "error_log": [],
        }
        path = write_initial_state_json(tmp_path, state)
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")  # 无 BOM
        loaded = json.loads(raw.decode("utf-8"))
        assert loaded == state


# ---------------------------------------------------------------------------
# 阶段 6(2026-11-09 效果缺失修复):required_features 白名单键
# ---------------------------------------------------------------------------
def test_required_features_whitelisted(tmp_path):
    """显式声明只保留 9 类布尔键;未知键/非布尔值过滤。"""
    videos = [str(tmp_path / "a.mp4")]
    (tmp_path / "a.mp4").write_bytes(b"x")
    state = build_initial_state(
        thread_id="console-aaaaaaaaaaaa", video_paths=videos, prompt="p",
        target_duration_ms=35000, ratio="9:16", lang="zh",
        outputs_root=tmp_path,
        required_features={"bgm": True, "unknown_key": True, "transitions": "yes"},
    )
    assert state["required_features"] == {"bgm": True}


def test_required_features_omitted_when_none(tmp_path):
    """不声明时不写该键(冻结键集不膨胀,由 QA 侧运行时推导)。"""
    videos = [str(tmp_path / "a.mp4")]
    (tmp_path / "a.mp4").write_bytes(b"x")
    state = build_initial_state(
        thread_id="console-bbbbbbbbbbbb", video_paths=videos, prompt="p",
        target_duration_ms=35000, ratio="9:16", lang="zh",
        outputs_root=tmp_path,
    )
    assert "required_features" not in state

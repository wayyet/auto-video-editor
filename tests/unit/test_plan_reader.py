"""``storyline.plan_reader`` 单测(plan §4.4)。

覆盖路径:
1. ``read_latest_plan_timeline_pro`` 在 v2 payload schema 下正确剥包装;
2. ``find_latest_session_dir`` 返回 mtime 最新子目录;
3. ``PlanTimelineProData.to_canonical`` 拍平 tracks.video 为 CanonicalTimeline,
   source_path 去重,single source_media;
4. 兼容 v1 schema(无 ``payload`` 包装);
5. 异常路径:目录不存在 / 无匹配文件 / JSON 损坏。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from storyline.contract import CanonicalTimeline
from storyline.plan_reader import (
    PlanReaderError,
    PlanTimelineProData,
    find_latest_session_dir,
    read_latest_plan_timeline_pro,
)


# ---------------------------------------------------------------------------
# find_latest_session_dir
# ---------------------------------------------------------------------------
def test_find_latest_session_dir_returns_none_when_root_missing(tmp_path: Path) -> None:
    assert find_latest_session_dir(tmp_path / "no_such_dir") is None


def test_find_latest_session_dir_returns_none_when_empty(tmp_path: Path) -> None:
    (tmp_path / "empty_dir").mkdir()
    assert find_latest_session_dir(tmp_path / "empty_dir") is None


def test_find_latest_session_dir_picks_latest_mtime(tmp_path: Path) -> None:
    import time

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    time.sleep(0.05)  # 确保 mtime 差异
    b.mkdir()
    assert find_latest_session_dir(tmp_path) == b


# ---------------------------------------------------------------------------
# read_latest_plan_timeline_pro — 正常路径
# ---------------------------------------------------------------------------
def _write_payload(ptp_dir: Path, payload: dict, *, name: str = "plan_timeline_pro_x.json") -> Path:
    ptp_dir.mkdir(parents=True, exist_ok=True)
    f = ptp_dir / name
    f.write_text(
        json.dumps({"payload": payload, "artifact_id": "art-x", "create_time": 1000},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return f


def test_read_latest_strips_payload_wrapper(tmp_path: Path) -> None:
    session = tmp_path / "sid-1"
    ptp = session / "plan_timeline_pro"
    _write_payload(
        ptp,
        {
            "tracks": {
                "video": [
                    {
                        "source_path": "E:/c/a.mp4",
                        "source_window": {"start": 0, "end": 5000, "duration": 10000},
                        "timeline_window": {"start": 0, "end": 5000, "duration": 5000},
                        "clip_id": "c1",
                    }
                ],
                "subtitles": [],
                "voiceover": [],
                "bgm": [],
            },
        },
    )
    plan_file, data = read_latest_plan_timeline_pro(session)
    assert plan_file.exists()
    assert data.artifact_id == "art-x"
    assert "tracks" in data.raw
    assert data.raw["tracks"]["video"][0]["clip_id"] == "c1"


def test_read_latest_compatible_with_v1_no_wrapper(tmp_path: Path) -> None:
    """v1 schema:无 ``payload`` 包装,直接 ``tracks``。"""
    session = tmp_path / "sid-1"
    ptp = session / "plan_timeline_pro"
    ptp.mkdir(parents=True, exist_ok=True)
    f = ptp / "plan_timeline_pro_v1.json"
    f.write_text(
        json.dumps({
            "tracks": {"video": [{"source_path": "E:/c/a.mp4"}]},
            "artifact_id": "v1",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    plan_file, data = read_latest_plan_timeline_pro(session)
    assert data.artifact_id == "v1"
    assert data.raw["tracks"]["video"][0]["source_path"] == "E:/c/a.mp4"


# ---------------------------------------------------------------------------
# read_latest_plan_timeline_pro — 异常路径
# ---------------------------------------------------------------------------
def test_read_raises_when_ptp_dir_missing(tmp_path: Path) -> None:
    session = tmp_path / "sid-empty"
    session.mkdir()
    with pytest.raises(PlanReaderError, match="产物目录不存在"):
        read_latest_plan_timeline_pro(session)


def test_read_raises_when_no_matching_files(tmp_path: Path) -> None:
    session = tmp_path / "sid-files"
    ptp = session / "plan_timeline_pro"
    ptp.mkdir(parents=True)
    (ptp / "other.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PlanReaderError, match="未在 .* 找到"):
        read_latest_plan_timeline_pro(session)


def test_read_raises_on_broken_json(tmp_path: Path) -> None:
    session = tmp_path / "sid-bad"
    ptp = session / "plan_timeline_pro"
    ptp.mkdir(parents=True)
    (ptp / "plan_timeline_pro_broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanReaderError, match="JSON 解析失败"):
        read_latest_plan_timeline_pro(session)


# ---------------------------------------------------------------------------
# PlanTimelineProData.to_canonical
# ---------------------------------------------------------------------------
def test_to_canonical_dedups_source_media(tmp_path: Path) -> None:
    """两个 clip 同一 source_path → 只 1 个 SourceMedia,2 个 Clip。"""
    raw = {
        "tracks": {
            "video": [
                {
                    "source_path": "E:/c/a.mp4",
                    "source_window": {"start": 0, "end": 5000, "duration": 10000},
                    "timeline_window": {"start": 0, "end": 5000, "duration": 5000},
                    "clip_id": "c1",
                },
                {
                    "source_path": "E:/c/a.mp4",
                    "source_window": {"start": 5000, "end": 10000, "duration": 10000},
                    "timeline_window": {"start": 5000, "end": 10000, "duration": 5000},
                    "clip_id": "c2",
                },
            ],
            "subtitles": [{"text": "hi"}, {"text": "  "}],
            "voiceover": [],
            "bgm": [],
        },
        "create_time": 12345,
    }
    data = PlanTimelineProData(artifact_id="art-1", raw=raw)
    canonical = data.to_canonical(job_id="job-x")
    assert isinstance(canonical, CanonicalTimeline)
    assert canonical.job_id == "job-x"
    assert canonical.created_at_ms == 12345
    assert len(canonical.source_media) == 1
    assert canonical.source_media[0].duration_ms == 10000
    assert len(canonical.clips) == 2
    assert canonical.clips[0].clip_id == "c1"
    assert canonical.clips[0].source_media_id == canonical.source_media[0].media_id
    # subtitles 拼成 zh
    assert canonical.subtitles.zh == "hi"


def test_to_canonical_raises_when_video_empty() -> None:
    raw = {"tracks": {"video": [], "subtitles": [], "voiceover": [], "bgm": []}}
    data = PlanTimelineProData(artifact_id="x", raw=raw)
    with pytest.raises(PlanReaderError, match="tracks.video 为空"):
        data.to_canonical(job_id="j")


def test_to_canonical_computes_duration_when_missing() -> None:
    """``source_window.duration`` 缺失时,用 ``end - start`` 兜底。"""
    raw = {
        "tracks": {
            "video": [
                {
                    "source_path": "E:/c/a.mp4",
                    "source_window": {"start": 0, "end": 10000},  # 无 duration
                    "timeline_window": {"start": 0, "end": 10000, "duration": 10000},
                    "clip_id": "c1",
                },
            ],
            "subtitles": [],
            "voiceover": [],
            "bgm": [],
        },
    }
    data = PlanTimelineProData(artifact_id="x", raw=raw)
    canonical = data.to_canonical(job_id="j")
    # duration_ms = end - start = 10000
    assert canonical.source_media[0].duration_ms == 10000
    assert canonical.clips[0].source_out_ms == 10000


def test_to_canonical_enforces_source_out_within_duration() -> None:
    """clip source_out_ms > media.duration_ms 时,CanonicalTimeline 校验失败。"""
    raw = {
        "tracks": {
            "video": [
                {
                    "source_path": "E:/c/a.mp4",
                    "source_window": {"start": 0, "end": 12000, "duration": 5000},  # bad
                    "timeline_window": {"start": 0, "end": 5000, "duration": 5000},
                    "clip_id": "c1",
                },
            ],
            "subtitles": [],
            "voiceover": [],
            "bgm": [],
        },
    }
    data = PlanTimelineProData(artifact_id="x", raw=raw)
    with pytest.raises(Exception, match="source_out_ms"):
        data.to_canonical(job_id="j")
"""P0 修复(20261117)契约:候选投影 speed 语义与 mapper 同一推导规则。

背景:candidate_projection_timeline 此前把 ``speed`` 硬编码 1.0,而
``start/end`` 写素材原区间、``timeline_start/end`` 写目标槽位 —— 三者
自相矛盾,validate_timeline 把素材原长当渲染时长 → 同轨假重叠 +
sequence.duration 越界 → QC 恒 escalated、preview.mp4 恒不生成。

锁定(审查文档 §7.1 / §2.1):
- speed = 源跨度 / 目标槽位跨度,与 storyline/mapper.canonical_to_draft
  同规则同容差(1e-6 归一 1.0);
- 非法区间防御:slot≤0 / src≤0 / 非有限 → 回退 1.0(契约层本应拦截);
- sequence.duration = 所有 clip 的最大 timeline_end(不假设排序);
- 压缩槽位投影经 check_asset_references_and_track_layout 零 error、零假重叠;
- mapper parity:同一 CanonicalTimeline 下投影 speed 与正式草稿 speed 一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assembly_capabilities.build_timeline import candidate_projection_timeline
from assembly_capabilities.timeline_ops import check_asset_references_and_track_layout
from storyline.mapper import canonical_to_draft, file_uri_to_path
from tests.support.draft_influence import load_canonical_sample


def _media_reports(plan: dict) -> list[dict]:
    """plan → 投影所需的最小 media_reports 形状(source/asset_id/probe)。"""
    reports = []
    for m in plan["source_media"]:
        reports.append({
            "source": file_uri_to_path(str(m["file_uri"])),
            "asset_id": f"asset_{m['media_id'][-4:]}",
            "probe": {"duration_seconds": m["duration_ms"] / 1000.0},
        })
    return reports


def _plan(clips: list[dict], *, duration_ms: int = 60000) -> dict:
    """构造最小候选计划(job/media/clips),file_uri 指向 tmp 真实文件。"""
    media_ids = sorted({c["source_media_id"] for c in clips})
    source_media = []
    for mid in media_ids:
        source_media.append({
            "media_id": mid,
            "file_uri": f"file:///C:/virtual/{mid}.mov",
            "duration_ms": duration_ms,
            "media_type": "video",
        })
    return {
        "schema_version": "1.0",
        "job_id": "projection-speed-test",
        "source_media": source_media,
        "clips": clips,
    }


def _clip(clip_id: str, media_id: str, s_in: int, s_out: int, t_in: int, t_out: int) -> dict:
    return {
        "clip_id": clip_id,
        "source_media_id": media_id,
        "source_in_ms": s_in,
        "source_out_ms": s_out,
        "timeline_in_ms": t_in,
        "timeline_out_ms": t_out,
    }


def _speeds(projection: dict) -> dict[str, float]:
    return {c["id"]: c["speed"] for c in projection["tracks"][0]["clips"]}


# ---------------------------------------------------------------------------
# §7.1 speed 表
# ---------------------------------------------------------------------------
class TestProjectionSpeedTable:
    def _project(self, tmp_path: Path, s_in: int, s_out: int, t_in: int, t_out: int) -> dict:
        plan = _plan([_clip("clip_0001", "media_0001", s_in, s_out, t_in, t_out)])
        return candidate_projection_timeline(plan, _media_reports(plan))

    def test_normal_1x(self, tmp_path: Path) -> None:
        proj = self._project(tmp_path, 1000, 2000, 0, 1000)
        assert _speeds(proj)["clip_0001"] == 1.0

    def test_fast_2x(self, tmp_path: Path) -> None:
        proj = self._project(tmp_path, 0, 2000, 0, 1000)
        assert _speeds(proj)["clip_0001"] == pytest.approx(2.0)

    def test_high_speed_tts_align_shape(self, tmp_path: Path) -> None:
        """本任务实锤形状:15442ms 源压进 1351ms 槽位 → ≈11.43,不再恒 1.0。"""
        proj = self._project(tmp_path, 0, 15442, 0, 1351)
        speed = _speeds(proj)["clip_0001"]
        assert speed == pytest.approx(15442 / 1351, rel=1e-9)
        assert speed != 1.0

    def test_invalid_slot_falls_back_to_1(self, tmp_path: Path) -> None:
        """slot≤0(契约层本应拒绝)→ 投影层防御回退 1.0,不产生伪合法速度。"""
        proj = self._project(tmp_path, 0, 2000, 1000, 1000)
        assert _speeds(proj)["clip_0001"] == 1.0

    def test_invalid_source_falls_back_to_1(self, tmp_path: Path) -> None:
        proj = self._project(tmp_path, 1000, 1000, 0, 1000)
        assert _speeds(proj)["clip_0001"] == 1.0


# ---------------------------------------------------------------------------
# P0-03:sequence.duration = 最大 timeline_end
# ---------------------------------------------------------------------------
def test_sequence_duration_covers_max_timeline_end(tmp_path: Path) -> None:
    """clips 列表末尾的 timeline_end 不是最大值时,取最大(不假设排序)。"""
    clips = [
        _clip("clip_0001", "media_0001", 0, 1000, 0, 1000),
        _clip("clip_0002", "media_0001", 1000, 7000, 2000, 8000),
    ]
    plan = _plan(clips)
    proj = candidate_projection_timeline(plan, _media_reports(plan))
    assert proj["sequence"]["duration"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# P0-05:压缩槽位投影不再产生假重叠 / duration 越界
# ---------------------------------------------------------------------------
def test_compressed_projection_passes_track_layout(tmp_path: Path) -> None:
    """本任务实锤场景(4 clips,tts_align 压缩槽位)→ 零 error、零假重叠。"""
    clips = [
        _clip("clip_0001", "media_0001", 0, 15442, 0, 1351),
        _clip("clip_0002", "media_0001", 15442, 30883, 1351, 2702),
        _clip("clip_0003", "media_0002", 0, 27935, 2702, 4053),
        _clip("clip_0004", "media_0003", 0, 17118, 4053, 12467),
    ]
    plan = _plan(clips)
    plan["source_media"][0]["duration_ms"] = 30883
    plan["source_media"][1]["duration_ms"] = 27935
    plan["source_media"][2]["duration_ms"] = 34235
    proj = candidate_projection_timeline(plan, _media_reports(plan))

    issues: list[dict] = []
    check_asset_references_and_track_layout(proj, issues)
    errors = [i for i in issues if i.get("severity") == "error"]
    assert not errors, f"压缩槽位投影不应产生 error: {errors}"
    overlaps = [i for i in issues if "overlaps" in str(i.get("message", ""))]
    assert not overlaps, f"顺序落位 + 正确 speed 不应有同轨假重叠: {overlaps}"
    # 渲染时长 = 源跨度/speed = 槽位跨度 → content_end == sequence.duration
    assert proj["sequence"]["duration"] == pytest.approx(12.467)


# ---------------------------------------------------------------------------
# D12:mapper parity —— 投影与正式草稿同一速度语义
# ---------------------------------------------------------------------------
def test_projection_speed_parity_with_mapper(tmp_path: Path) -> None:
    """同一 CanonicalTimeline:投影 clip speed == canonical_to_draft segment speed。"""
    from storyline.contract import CanonicalTimeline

    plan = load_canonical_sample()
    # 把 clip_0001 变成压缩片段:源 0→15442,时间轴 0→4000(≈3.86×)
    plan["clips"][0]["source_out_ms"] = 15442
    model = CanonicalTimeline.model_validate(plan)

    proj_plan = json.loads(model.model_dump_json())
    for m, uri in zip(proj_plan["source_media"], [x["file_uri"] for x in plan["source_media"]]):
        m["file_uri"] = uri
    proj = candidate_projection_timeline(proj_plan, _media_reports(proj_plan))
    proj_speeds = _speeds(proj)

    draft = canonical_to_draft(model)
    video_track = next(t for t in draft["tracks"] if t.get("type") == "video")
    segs = {s["material_id"]: s for s in video_track["segments"]}
    for idx, clip in enumerate(model.clips, start=1):
        seg_speed = segs[f"video-{idx}"]["speed"]
        assert proj_speeds[clip.clip_id] == pytest.approx(seg_speed, rel=1e-9), (
            f"clip {clip.clip_id}: 投影 speed({proj_speeds[clip.clip_id]})与 "
            f"mapper speed({seg_speed})必须一致"
        )

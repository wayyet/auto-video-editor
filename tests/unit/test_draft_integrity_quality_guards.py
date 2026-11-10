"""draft_integrity 画质防线单测(2026-11-11 模糊修复)。

- ``check_video_crops``:crop 必须为全幅单位正方形(退化裁切会被剪映拉伸
  铺满画面,成片出现定向拉丝——本轮事故);
- ``check_blur_video_effects``:我方 ``vfx-`` 特效名字含"模糊"且全片挂载
  → 告警("抖动模糊" 0.3 全片挂载曾把成片染成全画面模糊——本轮事故)。
"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.draft_integrity import (
    check_blur_video_effects,
    check_draft_dir,
    check_video_crops,
    check_video_material_aspect,
)

FULL_CROP = {
    "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
    "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
    "upper_right_x": 1.0, "upper_right_y": 0.0,
}


def _draft(crop: dict | None = FULL_CROP, vfx: list | None = None, seg_refs=None, n_segments: int = 2) -> dict:
    segments = []
    for i in range(n_segments):
        seg: dict = {"id": f"s{i}", "material_id": "v1"}
        if seg_refs is not None:
            seg["extra_material_refs"] = list(seg_refs(i))
        segments.append(seg)
    materials: dict = {"videos": [{"id": "v1", "material_name": "clip_0001"}]}
    if crop is not None:
        materials["videos"][0]["crop"] = dict(crop)
    if vfx is not None:
        materials["video_effects"] = vfx
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": materials,
        "tracks": [{"type": "video", "segments": segments}],
    }


class TestCheckVideoCrops:
    def test_full_frame_crop_passes(self) -> None:
        assert check_video_crops(_draft(), "zh") == []

    def test_degenerate_crop_flags_error(self) -> None:
        """实际事故值:lower_right_y=0.0 使右下角与右上角重合。"""
        issues = check_video_crops(_draft(crop=dict(FULL_CROP, lower_right_y=0.0)), "zh")
        assert len(issues) == 1
        assert issues[0]["severity"] == "error"
        assert issues[0]["check"] == "素材裁切"
        assert "lower_right_y" in issues[0]["detail"]

    def test_missing_crop_skipped(self) -> None:
        d = _draft(crop=None)
        assert check_video_crops(d, "zh") == []


class TestCheckBlurVideoEffects:
    @staticmethod
    def _vfx() -> list[dict]:
        return [{"id": "vfx-1", "type": "video_effect", "name": "抖动模糊"}]

    def test_no_blur_fx_passes(self) -> None:
        assert check_blur_video_effects(_draft(), "zh") == []

    def test_blur_fx_on_all_segments_warns(self) -> None:
        d = _draft(vfx=self._vfx(), seg_refs=lambda i: ["vfx-1"])
        issues = check_blur_video_effects(d, "zh")
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert issues[0]["check"] == "模糊特效"
        assert "全片挂载" in issues[0]["detail"]

    def test_blur_fx_one_id_per_segment_warns(self) -> None:
        """实际事故形态:node_09 每段写一个独立 vfx-N,各段引用不同 id。"""
        vfx = [
            {"id": "vfx-1", "type": "video_effect", "name": "抖动模糊"},
            {"id": "vfx-2", "type": "video_effect", "name": "抖动模糊"},
        ]
        d = _draft(vfx=vfx, seg_refs=lambda i: [f"vfx-{i + 1}"])
        issues = check_blur_video_effects(d, "zh")
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"

    def test_blur_fx_partial_attach_not_flagged(self) -> None:
        d = _draft(vfx=self._vfx(), seg_refs=lambda i: ["vfx-1"] if i == 0 else [])
        assert check_blur_video_effects(d, "zh") == []

    def test_non_blur_our_fx_not_flagged(self) -> None:
        vfx = [{"id": "vfx-2", "type": "video_effect", "name": "胶片暖棕"}]
        d = _draft(vfx=vfx, seg_refs=lambda i: ["vfx-2"])
        assert check_blur_video_effects(d, "zh") == []

    def test_human_uuid_blur_fx_not_flagged(self) -> None:
        """人工在剪映里加的特效是 uuid id,不属于我方 vfx- 命名空间,不误伤。"""
        vfx = [{"id": "4F1A2B3C-0000-1111-2222-333344445555", "type": "video_effect", "name": "模糊"}]
        d = _draft(vfx=vfx, seg_refs=lambda i: [vfx[0]["id"]])
        assert check_blur_video_effects(d, "zh") == []


class TestCheckDraftDirRegistration:
    def test_healthy_dir_lists_new_checks_and_clean(self, tmp_path: Path) -> None:
        d = _draft(vfx=[], seg_refs=lambda i: [])
        (tmp_path / "draft_content.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "draft_info.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        result = check_draft_dir(tmp_path, tag="zh", registered_expected=False)
        assert "video_crops" in result["checks"]
        assert "blur_video_effects" in result["checks"]
        assert result["issues"] == []

    def test_degenerate_crop_surfaces_in_draft_dir(self, tmp_path: Path) -> None:
        d = _draft(crop=dict(FULL_CROP, lower_right_y=0.0))
        (tmp_path / "draft_content.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "draft_info.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        result = check_draft_dir(tmp_path, tag="zh", registered_expected=False)
        assert any(i["check"] == "素材裁切" for i in result["issues"])


class TestCheckVideoMaterialAspect:
    """阶段2(R3 修复)QA 防线:素材显示比例/方向必须与画布一致。"""

    @staticmethod
    def _draft_with_path(path: str) -> dict:
        d = _draft()
        d["materials"]["videos"][0]["path"] = path
        return d

    @staticmethod
    def _dims(w: int, h: int, rot: int = 0):
        from video_edit_capabilities.render import VideoDimensions

        return VideoDimensions(
            coded_width=h if rot in (90, 270) else w,
            coded_height=w if rot in (90, 270) else h,
            rotation_degrees=rot,
            display_width=w,
            display_height=h,
        )

    def test_portrait_material_passes(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.mp4"
        f.write_bytes(b"\x00" * 8)
        monkeypatch.setattr(
            "video_edit_capabilities.render.probe_video_dimensions",
            lambda _p: self._dims(1080, 1920),
        )
        assert check_video_material_aspect(self._draft_with_path(str(f)), "zh") == []

    def test_landscape_material_warns(self, tmp_path: Path, monkeypatch) -> None:
        """横屏素材直进竖屏草稿 = 未归一化/降级放行,必须可观测。"""
        f = tmp_path / "a.mp4"
        f.write_bytes(b"\x00" * 8)
        monkeypatch.setattr(
            "video_edit_capabilities.render.probe_video_dimensions",
            lambda _p: self._dims(1920, 1080),
        )
        issues = check_video_material_aspect(self._draft_with_path(str(f)), "zh")
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert issues[0]["check"] == "素材比例"
        assert "1920x1080" in issues[0]["detail"]

    def test_rotated_material_warns_on_metadata(self, tmp_path: Path, monkeypatch) -> None:
        """归一化产物应已烘焙方向;仍带旋转元数据 → 告警。"""
        f = tmp_path / "a.mp4"
        f.write_bytes(b"\x00" * 8)
        monkeypatch.setattr(
            "video_edit_capabilities.render.probe_video_dimensions",
            lambda _p: self._dims(1080, 1920, rot=90),
        )
        issues = check_video_material_aspect(self._draft_with_path(str(f)), "zh")
        assert len(issues) == 1
        assert "rot=90" in issues[0]["detail"]

    def test_missing_file_and_bad_path_skipped(self, tmp_path: Path) -> None:
        assert check_video_material_aspect(self._draft_with_path(str(tmp_path / "nope.mp4")), "zh") == []
        assert check_video_material_aspect(self._draft_with_path(""), "zh") == []
        assert check_video_material_aspect(_draft(), "zh") == []

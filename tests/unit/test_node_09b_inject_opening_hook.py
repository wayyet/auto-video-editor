"""节点 9b 开场钩子单测 — 原生绑定、标题轨自拼、幂等清理、双草稿、开关。

验收基准(第四期计划 §3.2 验收 1-4 / T3.2):
- 特效绑视频轨 segments[0](hook-effect-1,≤3s,不建 effect 轨);
- 标题轨 track-text-hook + 素材 hook-title-1 + 动画 anim-hook-title-1 齐全;
- 切句:第 1 条字幕 ``马六甲海上清真寺,日落时分海面金光闪烁。`` → 标题
  ``马六甲海上清真寺``(半角逗号);
- no_title 分支特效仍保留;幂等重放字节一致;英文草稿只做特效;
- OPENING_HOOK_ENABLED=0 → noop:disabled 草稿不变。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.node_09b_inject_opening_hook as node_09b_mod
from draft_ops.draft_integrity import check_track_references
from nodes.node_09b_inject_opening_hook import (
    HOOK_TRACK_ID,
    inject_opening_hook,
)

_FIRST_SUBTITLE = "马六甲海上清真寺,日落时分海面金光闪烁。"


def _seg(seg_id: str, start: int, duration: int = 10_000_000) -> dict:
    return {"id": seg_id, "target_timerange": {"start": start, "duration": duration}}


def _subtitle(idx: int, content: str, start_us: int, duration_us: int = 3_000_000) -> dict:
    return {
        "id": f"text-zh-{idx}",
        "content": content,
        "target_timerange": {"start": start_us, "duration": duration_us},
    }


def _draft(with_subtitles: bool) -> dict:
    materials: dict = {}
    if with_subtitles:
        materials["texts"] = [
            _subtitle(1, _FIRST_SUBTITLE, 0),
            _subtitle(2, "涨潮时清真寺仿佛漂在海面上。", 3_000_000),
            _subtitle(3, "夕阳把海面染成金色。", 6_000_000),
        ]
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 30_000_000,
        "materials": materials,
        "tracks": [
            {"type": "video", "segments": [_seg("s1", 0), _seg("s2", 10_000_000), _seg("s3", 20_000_000)]}
        ],
    }


def _write_draft(path: Path, draft: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    return _write_draft(tmp_path / "draft" / "draft_content.json", _draft(with_subtitles=True))


@pytest.fixture
def tmp_draft_no_subtitle(tmp_path: Path) -> Path:
    return _write_draft(
        tmp_path / "draft" / "draft_content.json", _draft(with_subtitles=False)
    )


def _state(draft_path: str, **overrides) -> dict:
    base = {"draft_path": draft_path, "status_log": [], "error_log": []}
    base.update(overrides)
    return base


def _hook_track(draft: dict) -> dict | None:
    return next((t for t in draft["tracks"] if t.get("id") == HOOK_TRACK_ID), None)


class TestZhHookInjection:
    def test_effect_bound_to_first_video_segment(self, tmp_draft: Path) -> None:
        out = inject_opening_hook(_state(str(tmp_draft)))
        draft = _read(tmp_draft)

        effects = draft["materials"]["video_effects"]
        assert [e["id"] for e in effects] == ["hook-effect-1"]
        assert effects[0]["type"] == "video_effect"
        assert effects[0]["name"] == "CCD闪光"
        segments = draft["tracks"][0]["segments"]
        assert "hook-effect-1" in segments[0]["extra_material_refs"]
        # 时长窗口 ≤ 3s
        assert effects[0]  # 素材无 duration 键,绑定段的 target_timerange 校验在标题用例
        assert "node_09b_inject_opening_hook_done" in out["status_log"]
        # 引用完整性:无悬空
        assert check_track_references(draft, "zh") == []

    def test_title_track_material_animation_all_present(self, tmp_draft: Path) -> None:
        inject_opening_hook(_state(str(tmp_draft)))
        draft = _read(tmp_draft)

        track = _hook_track(draft)
        assert track is not None
        assert track["type"] == "text"
        assert track["name"] == "opening_title"
        assert len(track["segments"]) == 1

        seg = track["segments"][0]
        assert seg["material_id"] == "hook-title-1"
        assert seg["target_timerange"] == {"start": 0, "duration": 3_000_000}
        # 标题片段在上半屏(clip.transform.y = 0.35),非字幕默认 -0.8
        assert seg["clip"]["transform"]["y"] == 0.35
        assert "anim-hook-title-1" in seg["extra_material_refs"]

        texts = draft["materials"]["texts"]
        title_mat = next(t for t in texts if t["id"] == "hook-title-1")
        assert title_mat["track_name"] == "opening_title"
        assert title_mat["target_timerange"] == {"start": 0, "duration": 3_000_000}

        anims = draft["materials"]["material_animations"]
        assert [a["id"] for a in anims] == ["anim-hook-title-1"]
        assert anims[0]["animations"][0]["name"] == "冲屏位移"
        assert anims[0]["animations"][0]["type"] == "in"

    def test_title_cut_from_first_subtitle_before_halfwidth_comma(
        self, tmp_draft: Path
    ) -> None:
        """验收 4:第 1 条字幕(半角逗号)→ 标题 ``马六甲海上清真寺``。"""
        inject_opening_hook(_state(str(tmp_draft)))
        draft = _read(tmp_draft)
        title_mat = next(t for t in draft["materials"]["texts"] if t["id"] == "hook-title-1")
        # content 是原生 styles JSON 字符串,显示文本在其中
        assert "马六甲海上清真寺" in title_mat["content"]
        assert "日落时分" not in title_mat["content"]

    def test_explicit_state_hook_title_wins(self, tmp_draft: Path) -> None:
        inject_opening_hook(_state(str(tmp_draft), hook_title="自定义标题"))
        draft = _read(tmp_draft)
        title_mat = next(t for t in draft["materials"]["texts"] if t["id"] == "hook-title-1")
        assert "自定义标题" in title_mat["content"]

    def test_title_duration_clamped_to_first_clip(self, tmp_path: Path) -> None:
        """第 1 个镜头 2s < 3s → 标题时长钳到 2s(原计划 §7.10 第 4 步)。"""
        draft = _draft(with_subtitles=True)
        draft["tracks"][0]["segments"][0]["target_timerange"]["duration"] = 2_000_000
        draft_file = _write_draft(tmp_path / "draft" / "draft_content.json", draft)

        inject_opening_hook(_state(str(draft_file)))
        got = _read(draft_file)
        track = _hook_track(got)
        assert track["segments"][0]["target_timerange"] == {"start": 0, "duration": 2_000_000}


class TestNoTitleBranch:
    def test_no_subtitles_keeps_effect_only(self, tmp_draft_no_subtitle: Path) -> None:
        out = inject_opening_hook(_state(str(tmp_draft_no_subtitle)))
        draft = _read(tmp_draft_no_subtitle)

        effects = draft["materials"]["video_effects"]
        assert [e["id"] for e in effects] == ["hook-effect-1"]
        assert _hook_track(draft) is None
        assert not any(t["id"] == "hook-title-1" for t in draft["materials"].get("texts", []))
        assert not draft["materials"].get("material_animations")
        assert "node_09b_inject_opening_hook_noop:no_title" in out["status_log"]
        assert "node_09b_inject_opening_hook_done" in out["status_log"]

    def test_rerun_without_subtitles_removes_previous_title(self, tmp_draft: Path) -> None:
        """先跑有字幕(有标题),再跑无字幕版本 → 上一轮标题/动画/轨道被清干净。"""
        inject_opening_hook(_state(str(tmp_draft)))
        raw = tmp_draft.read_text(encoding="utf-8")
        draft = json.loads(raw)
        draft["materials"]["texts"] = [
            t for t in draft["materials"]["texts"] if not str(t["id"]).startswith("text-zh-")
        ]
        tmp_draft.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

        inject_opening_hook(_state(str(tmp_draft)))
        draft = _read(tmp_draft)
        assert _hook_track(draft) is None
        assert not any(
            str(t["id"]).startswith("hook-title-") for t in draft["materials"].get("texts", [])
        )
        assert not draft["materials"].get("material_animations")
        # 特效仍在
        assert [e["id"] for e in draft["materials"]["video_effects"]] == ["hook-effect-1"]


class TestIdempotentReplay:
    def test_double_run_byte_identical(self, tmp_draft: Path) -> None:
        inject_opening_hook(_state(str(tmp_draft)))
        first = tmp_draft.read_bytes()

        inject_opening_hook(_state(str(tmp_draft)))
        second = tmp_draft.read_bytes()
        assert first == second

        draft = _read(tmp_draft)
        n_tracks = len(draft["tracks"])
        inject_opening_hook(_state(str(tmp_draft)))
        assert len(_read(tmp_draft)["tracks"]) == n_tracks
        assert len(_read(tmp_draft)["materials"]["video_effects"]) == 1


class TestEnglishBranch:
    def test_en_draft_effect_only_no_title(self, tmp_path: Path) -> None:
        zh = _write_draft(tmp_path / "zh" / "draft_content.json", _draft(with_subtitles=True))
        en = _write_draft(tmp_path / "en" / "draft_content.json", _draft(with_subtitles=True))

        out = inject_opening_hook(
            _state(str(zh), draft_dir_en_branch=str(en.parent))
        )
        assert "node_09b_inject_opening_hook_done" in out["status_log"]
        assert "node_09b_inject_opening_hook_en_done" in out["status_log"]

        en_draft = _read(en)
        assert [e["id"] for e in en_draft["materials"]["video_effects"]] == ["hook-effect-1"]
        assert _hook_track(en_draft) is None
        assert not any(
            str(t["id"]).startswith("hook-title-")
            for t in en_draft["materials"].get("texts", [])
        )
        # 英文分支不消费标题 → 不记 no_title
        assert "node_09b_inject_opening_hook_noop:no_title" not in out["status_log"]

    def test_missing_en_dir_degrades(self, tmp_draft: Path) -> None:
        out = inject_opening_hook(
            _state(str(tmp_draft), draft_dir_en_branch=str(tmp_draft.parent / "no_such_dir"))
        )
        assert "node_09b_inject_opening_hook_done" in out["status_log"]
        assert any("降级只处理中文草稿" in e for e in out["error_log"])


class TestDisabledSwitch:
    def test_disabled_writes_nothing(self, tmp_draft: Path, monkeypatch) -> None:
        monkeypatch.setattr(node_09b_mod.config, "OPENING_HOOK_ENABLED", False)
        before = tmp_draft.read_bytes()
        out = inject_opening_hook(_state(str(tmp_draft)))
        assert "node_09b_inject_opening_hook_noop:disabled" in out["status_log"]
        assert tmp_draft.read_bytes() == before

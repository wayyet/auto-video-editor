"""节点 9 转场 + 视频特效单测 — 模板库加载、注入、降级。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_09_inject_fx import inject_fx


@pytest.fixture
def tmp_draft(tmp_path: Path) -> Path:
    """构造含 3 段视频的草稿。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 30_000_000,
        "materials": {},
        "tracks": [{
            "type": "video",
            "segments": [
                {"id": "s1", "target_timerange": {"start": 0, "duration": 10_000_000}},
                {"id": "s2", "target_timerange": {"start": 10_000_000, "duration": 10_000_000}},
                {"id": "s3", "target_timerange": {"start": 20_000_000, "duration": 10_000_000}},
            ],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(**overrides) -> dict:
    base = {
        "draft_path": "",
        "shot_plan": {
            "shots": [
                {"id": "sh1", "style_tag": "default"},
                {"id": "sh2", "style_tag": "default"},
                {"id": "sh3", "style_tag": "default"},
            ],
        },
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def test_inject_fx_adds_transitions_and_video_effects(tmp_draft: Path) -> None:
    """3 段 → 2 个转场;1 个全局视频特效。"""
    state = _state(draft_path=str(tmp_draft))
    out = inject_fx(state)

    draft = json.loads(tmp_draft.read_text(encoding="utf-8"))
    transitions = draft["materials"]["transitions"]
    video_effects = draft["materials"]["video_effects"]

    assert len(transitions) == 2  # 3 段有 2 个边界
    assert all(t.get("resource_id") for t in transitions)
    assert len(video_effects) >= 1
    assert all(ve.get("resource_id") for ve in video_effects)
    assert "node_09_inject_fx_done" in out["status_log"]


def test_inject_fx_handles_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模板库缺失时,降级通过 — status_log 标记 + error_log 警告,不动草稿。"""
    # 把节点 9 的 load_template_library 替换为返回空库
    from jy_common import template_library as tl_mod

    empty_lib = tl_mod.TemplateLibrary({"_missing": True, "_path": "/nope.json"})

    def fake_loader(_path):
        return empty_lib

    monkeypatch.setattr("nodes.node_09_inject_fx.load_template_library", fake_loader)

    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 30_000_000,
        "materials": {},
        "tracks": [{"type": "video", "segments": []}],
    }
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    state = _state(draft_path=str(draft_path))
    out = inject_fx(state)

    assert "node_09_inject_fx_no_template" in out["status_log"]
    assert any("模板库缺失" in e for e in out["error_log"])
    # 草稿 materials.transitions 应为空或不存在
    draft_after = json.loads(draft_path.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("transitions")


def test_inject_fx_idempotent_keys_use_avail_assets(tmp_draft: Path) -> None:
    """两次跑同节点:首次写 N 个转场,第二次运行应保持(本节点每次都 append — Week 4 引入幂等优化)。"""
    state = _state(draft_path=str(tmp_draft))
    inject_fx(state)
    draft_first = json.loads(tmp_draft.read_text(encoding="utf-8"))
    n_first = len(draft_first["materials"]["transitions"])

    inject_fx(state)
    draft_second = json.loads(tmp_draft.read_text(encoding="utf-8"))
    n_second = len(draft_second["materials"]["transitions"])

    # 第二次跑仍追加 — Week 3 占位实现,Week 4 引入去重
    assert n_second == n_first * 2
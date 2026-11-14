"""E2E TC07 — 节点 13 阶段 5(角色化音量 + 原生淡入淡出)端到端验收。

验证目标(对齐计划 §8 阶段 5):
- 走到节点 13 后 ``draft["materials"]["audio_fades"]`` 为原生条目且绑定到 BGM 段
- BGM 段级 volume=0.35(config.NODE13_VOLUME_BGM),配音/主音轨角色互不干扰
- ``state["audio_fade_targets"]`` 至少 1 条(执行结果由 status_log 标签留痕)
- 重放幂等:整份草稿字节级一致(fade 按 id 覆盖,不翻倍)

依赖:既有 fixtures (conftest) + 单测覆盖过的角色化入口;为不依赖真实剪映
客户端,本测试不启动 LangGraph 图,而是直接调用节点函数(与既有集成测试策略一致)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_13_adjust_volume import adjust_volume


@pytest.fixture
def audio_draft(tmp_path: Path) -> Path:
    """构造 BGM 轨 + 中文配音轨 + video 段的草稿,模拟关卡② 之后的状态。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {"audio_fades": []},
        "tracks": [
            {"type": "video", "segments": [
                {"id": "v1", "target_timerange": {"start": 0, "duration": 35_000_000}}]},
            {"type": "audio", "id": "track-audio-bgm", "segments": [
                {"id": "audio-bgm-1", "material_id": "audio-bgm-1",
                 "target_timerange": {"start": 0, "duration": 35_000_000}}]},
            {"type": "audio", "id": "track-audio-zh-voiceover", "name": "Voiceover_zh", "segments": [
                {"id": "audio-vo-1", "material_id": "audio-vo-1",
                 "target_timerange": {"start": 0, "duration": 35_000_000}}]},
        ],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_tc07_node_13_writes_native_fades_and_role_volumes(audio_draft: Path) -> None:
    """TC07:节点 13 后原生 audio_fades 绑定 BGM 段,分角色段级 volume 生效。"""
    out_state = adjust_volume({"draft_path": str(audio_draft), "status_log": [], "error_log": []})

    # 验证 1 — state 标记
    assert "node_13_adjust_volume_done" in out_state["status_log"]
    assert len(out_state["audio_fade_targets"]) >= 1, \
        "audio_fade_targets 应至少含 BGM 一条 fade"

    # 验证 2 — 原生 fade 条目 + 段绑定
    draft = json.loads(audio_draft.read_text(encoding="utf-8"))
    fades = draft["materials"]["audio_fades"]
    assert fades, "materials.audio_fades 应非空"
    (bgm_fade, *rest) = fades
    assert bgm_fade["type"] == "audio_fade"
    assert bgm_fade["fade_in_duration"] == 2_000_000
    assert bgm_fade["fade_out_duration"] == 3_000_000
    assert not rest, "fade 不应翻倍"

    # 验证 3 — 分角色段级音量:BGM 0.35,配音 1.0,主音轨 0.0(2026-11-14
    # 起默认静音原始视频,config.NODE13_VOLUME_MAIN=0);fade 只绑 BGM 段
    bgm_seg = draft["tracks"][1]["segments"][0]
    vo_seg = draft["tracks"][2]["segments"][0]
    video_seg = draft["tracks"][0]["segments"][0]
    assert bgm_seg["volume"] == 0.35
    assert bgm_fade["id"] in bgm_seg["extra_material_refs"]
    assert vo_seg["volume"] == 1.0
    assert not any(str(r).startswith("audio-fade-") for r in vo_seg.get("extra_material_refs", []))
    assert video_seg["volume"] == 0.0


def test_tc07_node_13_replay_is_idempotent(audio_draft: Path) -> None:
    """TC07 幂等:重放(模拟 resume)整份草稿字节级一致,fade/refs 不翻倍。"""
    state = {"draft_path": str(audio_draft), "status_log": [], "error_log": []}

    out1 = adjust_volume(state)
    first = audio_draft.read_bytes()

    out2 = adjust_volume({**out1, "audio_fade_targets": []})
    second = audio_draft.read_bytes()

    assert first == second
    assert "node_13_adjust_volume_done" in out2["status_log"]


def test_tc07_node_13_uses_safe_write_draft_entry_point(audio_draft: Path) -> None:
    """TC07:节点 13 走 ``draft_ops.atomic_writer.safe_write_draft`` 入口(Week 5 双写契约)。"""
    import inspect

    from nodes import node_13_adjust_volume

    src = inspect.getsource(node_13_adjust_volume)
    assert "safe_write_draft" in src, (
        "节点 13 应通过 draft_ops.atomic_writer.safe_write_draft 入口写入"
        "(Week 5 计划 §6.2 双写契约)"
    )
    # 反向断言:不再走旧的 jy_common.draft_writer.atomic_write_draft_json
    assert "atomic_write_draft_json" not in src, (
        "节点 13 不应再走 atomic_write_draft_json(Week 5 已切到 safe_write_draft)"
    )

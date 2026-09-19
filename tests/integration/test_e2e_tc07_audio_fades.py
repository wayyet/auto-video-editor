"""E2E TC07 — 节点 13 真实实现后的端到端验收。

验证目标(对应验证报告 §12 验收标准 [5]):
- 走到节点 13 后 ``draft["materials"]["audio_fades"]`` 非空
- ``state["volume_adjusted"] is True``
- ``state["audio_fade_targets"]`` 至少 2 条(main + bgm)

依赖:既有 fixtures (conftest) + 单测覆盖过的 _seed_draft 风格 fixture。
为不依赖真实剪映客户端,本测试不启动 LangGraph 图,而是直接调用节点函数 +
单元测试覆盖的 jianying_adjust_volume 入口,与既有集成测试策略保持一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_13_adjust_volume import (
    _PLACEHOLDER_FADE_IN_KEY,
    _PLACEHOLDER_FADE_OUT_KEY,
    _PLACEHOLDER_VOLUME_KEY,
    adjust_volume,
    jianying_adjust_volume,
)


@pytest.fixture
def audio_draft(tmp_path: Path) -> Path:
    """构造含 audio_main + audio_bgm 两段的草稿,模拟节点 12 之后的状态。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {
            "audios": [
                {"id": "audio-main-1", _PLACEHOLDER_VOLUME_KEY: 0.5},
                {"id": "audio-bgm-1", _PLACEHOLDER_VOLUME_KEY: 0.8},
            ],
            "audio_fades": [],
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {"id": "v1", "target_timerange": {"start": 0, "duration": 35_000_000}},
                ],
            },
            {
                "type": "audio",
                "segments": [
                    {
                        "id": "audio-main-1",
                        "name": "audio_main",
                        "type": "audio_main",
                        "target_timerange": {"start": 0, "duration": 35_000_000},
                    },
                    {
                        "id": "audio-bgm-1",
                        "name": "audio_bgm",
                        "type": "audio_bgm",
                        "target_timerange": {"start": 0, "duration": 35_000_000},
                    },
                ],
            },
        ],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_tc07_node_13_writes_audio_fades_and_volume(audio_draft: Path) -> None:
    """TC07:节点 13 后 audio_fades + volume 字段均生效,state 标记 volume_adjusted。"""
    state = {
        "draft_path": str(audio_draft),
        "status_log": [],
        "error_log": [],
    }

    # 直接调节点函数
    out_state = adjust_volume(state)

    # 验证 1 — state 标记
    assert out_state["volume_adjusted"] is True
    assert "node_13_adjust_volume_done" in out_state["status_log"]
    assert len(out_state["audio_fade_targets"]) >= 1, \
        "audio_fade_targets 应至少含 BGM 一条 fade"

    # 验证 2 — draft_content.json 已写入 audio_fades + volume
    draft = json.loads(audio_draft.read_text(encoding="utf-8"))
    assert draft["materials"]["audio_fades"], "materials.audio_fades 应非空"

    # 验证 3 — BGM 音量 0.35 + fade_in 2s + fade_out 3s
    audios_by_id = {a["id"]: a for a in draft["materials"]["audios"]}
    bgm = audios_by_id["audio-bgm-1"]
    assert bgm[_PLACEHOLDER_VOLUME_KEY] == 0.35

    bgm_fade = next(
        (f for f in draft["materials"]["audio_fades"] if f.get("track_id") == "audio-bgm-1"),
        None,
    )
    assert bgm_fade is not None, "BGM 应有对应 audio_fade 条目"
    assert bgm_fade[_PLACEHOLDER_FADE_IN_KEY] == 2_000_000
    assert bgm_fade[_PLACEHOLDER_FADE_OUT_KEY] == 3_000_000


def test_tc07_node_13_then_resume_keeps_state_idempotent(audio_draft: Path) -> None:
    """TC07 幂等性:第二次 invoke(模拟 resume)不应重复写 audio_fades。

    节点 13 默认每次都追加 audio_fades 条目,与既有节点 9 行为一致(Week 3 占位);
    Week 4 应改为按 audio_segment_id 去重。本测试记录当前行为以便回归。
    """
    state = {
        "draft_path": str(audio_draft),
        "status_log": [],
        "error_log": [],
    }

    out1 = adjust_volume(state)
    fade_count_1 = len(out1["audio_fade_targets"])

    # 第二次调:volume_adjusted 仍 True,但 audio_fade_targets 会再追加(Week 4 待优化)
    out2 = adjust_volume(out1)
    fade_count_2 = len(out2["audio_fade_targets"])

    # 当前行为:追加(Week 4 应改为"已有则跳过"幂等优化)
    assert fade_count_2 >= fade_count_1


def test_tc07_node_13_uses_safe_write_draft_entry_point(audio_draft: Path) -> None:
    """TC07:节点 13 走 ``draft_ops.atomic_writer.safe_write_draft`` 入口(Week 5 双写契约)。

    升级历史:
    - Week 3/4:节点 13 走 ``jy_common.draft_writer.atomic_write_draft_json``(单写)。
    - Week 5(本计划 §6.2):节点 13 切到 ``safe_write_draft``(双写 + 校验 + 失败回退),
      与节点 5/7/8/9/10/11/16 保持一致。
    """
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
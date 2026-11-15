"""node_16a_translate_and_check 单测 — 英文字幕翻译节点(第六期改版)。

覆盖:
- marker 已存在 → 跳过翻译,只读草稿
- marker 不存在 → 调翻译 + 写 marker + 写 state + 写 SRT
- subtitle_segments_zh(兼容 asr_segments_zh)缺/空 → 正常跳过
  (skipped_no_source_subtitles,非错误)
- draft_dir_en_branch 缺失 → 跳过(error_log)
- 翻译源切换:subtitle_segments_zh 优先于 asr_segments_zh

历史:Week 5 曾覆盖"layout 校验写 state(不触发 interrupt)"与 qc 链
(subtitle_build/render/qc + validate_layout 降级)用例;2026-11-15 关卡③
整体删除,布局检测随实现一并移除,相关用例同步删除(能力层另有独立测试)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.translate_client import MockTranslateClient, set_default_client
from nodes.node_16a_translate_and_check import node_16a_translate_and_check


@pytest.fixture(autouse=True)
def _no_jianying_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件多个用例会让节点**真实写草稿**(safe_write_draft):剪映客户端
    开着时写入互斥严格模式直接 raise,造成环境性失败(2026-11-08 全量回归
    实测 3 例)。翻译/写盘用例验证的是翻译与草稿契约,互斥行为由
    test_safe_write_guard 单独覆盖 —— 这里统一注入"剪映不在跑"的桩,
    写入路径(快照/双写/verify)仍真实执行。"""
    import draft_ops.atomic_writer as _aw

    monkeypatch.setattr(_aw, "_default_proc_checker", lambda: False)


def _seed_en_branch(tmp_path: Path) -> Path:
    draft_dir = tmp_path / "en_branch"
    draft_dir.mkdir()
    draft = {
        "canvas_config": {},
        "materials": {
            "texts": [
                {"content": "你好", "target_timerange": {"start": 0, "duration": 2_000_000}},
                {"content": "世界", "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
            ]
        },
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_dir


def _state(asr_segs: list[dict], draft_dir: Path) -> dict:
    return {
        "asr_segments_zh": asr_segs,
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }


def _normal_asr() -> list[dict]:
    return [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
    ]


def test_node_16a_happy_path_writes_state_and_srt(tmp_path: Path) -> None:
    """正常路径:翻译 + 写 marker + 写 state + 写 SRT。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)

    # state 字段应有值
    assert out.get("subtitle_segments_en")
    assert "node_16a_translate_done" in out["status_log"]
    # 不应触发 interrupt(payload=None 不应存在)
    assert "interrupt" not in out
    # SRT 正常生成
    srt_path = out.get("subtitle_srt_path")
    assert srt_path
    assert Path(srt_path).exists()


def test_node_16a_marker_exists_skips_translate(tmp_path: Path) -> None:
    """marker 已存在 → 不调翻译客户端,直接读草稿。"""
    draft_dir = _seed_en_branch(tmp_path)
    marker_data = [
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_en": "PreExisting"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_en": "PreExisting2"},
    ]
    (draft_dir / "subtitle_en.json").write_text(
        json.dumps(marker_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    class _ShouldNotCall:
        def translate(self, segments):
            raise AssertionError("marker 存在时不应调翻译")

    set_default_client(_ShouldNotCall())
    state = _state(_normal_asr(), draft_dir)
    out = node_16a_translate_and_check(state)
    assert out.get("subtitle_segments_en")


def test_node_16a_no_asr_segments_skips(tmp_path: Path) -> None:
    """asr_segments_zh 缺失/为空 → 正常跳过(无源字幕是预期场景),不写 error_log。

    B1 ASR 移除后这是英文分支的常态输入:状态用 skipped_no_source_subtitles,
    不再算错误,也不得阻断后续 node_17(2026-11-15 关卡③删除后 10b 恒直连 17)。
    """
    state = {
        "draft_dir_en_branch": str(tmp_path),
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "skipped_no_source_subtitles" in out["status_log"]
    assert not out.get("error_log")
    assert "node_16a_translate_skipped" not in out["status_log"]


def test_node_16a_no_draft_dir_skips() -> None:
    """draft_dir_en_branch 缺失 → 跳过。"""
    state = {
        "asr_segments_zh": _normal_asr(),
        "draft_dir_en_branch": None,
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)
    assert "node_16a_translate_skipped" in out["status_log"]
    assert any("draft_dir_en_branch" in e for e in out["error_log"])


# ---------------------------------------------------------------------------
# 阶段 2(2026-11-09 效果缺失修复):翻译源切换到 subtitle_segments_zh
# ---------------------------------------------------------------------------
def test_node_16a_translates_subtitle_segments_zh(tmp_path: Path) -> None:
    """阶段 2:翻译源 = node_08 产出的 subtitle_segments_zh(优先于已废弃的
    asr_segments_zh)。B1 后 ASR 段恒空,英文链(16a→08b→10b→17)靠本字段复活。"""
    draft_dir = _seed_en_branch(tmp_path)
    set_default_client(MockTranslateClient())
    state = {
        "subtitle_segments_zh": _normal_asr(),  # 同形状:node_08 复用 SubtitleSegment
        "draft_dir_en_branch": str(draft_dir),
        "status_log": [],
        "error_log": [],
    }
    out = node_16a_translate_and_check(state)

    assert "skipped_no_source_subtitles" not in out["status_log"]
    assert "node_16a_translate_done" in out["status_log"]
    segs_en = out.get("subtitle_segments_en") or []
    assert [s["start_ms"] for s in segs_en] == [0, 2_000]
    assert all(s.get("text_en") for s in segs_en)

"""AI 转场 → 剪映草稿结构 集成测试(2026-11-12 noop 治理阶段2b,设计文档 §2.5)。

节点链:generate_ai_transition(fake Provider + 假抽帧)→ plan_timeline_ai_transition
(真实物化)→ mapper(canonical_to_draft)。验收口径:

- 真实产物文件存在且被时间线引用;
- 剪映草稿结构含转场的 video material 与 video 轨 segment(转场即普通视频片段);
- 总时长不超预算(预算吸收生效);
- 基础时间线文件保持不变(可回退)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from nodes.storyline.node_generate_ai_transition import (
    storyline_generate_ai_transition_node,
)
from nodes.storyline.node_plan_timeline_ai_transition import (
    storyline_plan_timeline_ai_transition_node,
)
from storyline.contract import CanonicalTimeline
from storyline.mapper import canonical_to_draft
from storyline_capabilities.ai_transition_provider import (
    PROVIDER_STATUS_GENERATED,
    AITransitionProviderResult,
    set_default_provider,
)


class _FakeProvider:
    name = "minimax"
    model = "MiniMax-Hailuo-02"

    def __init__(self, duration_ms: int = 2000) -> None:
        self.duration_ms = duration_ms
        self.calls = 0

    def generate_transition(
        self, *, first_frame, last_frame, prompt, duration_ms, resolution, output_path
    ) -> AITransitionProviderResult:
        self.calls += 1
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"integration fake mp4")
        return AITransitionProviderResult(
            path=out,
            provider=self.name,
            model=self.model,
            duration_ms=self.duration_ms,
            width=1080,
            height=1920,
            fps=30.0,
            sha256="c" * 64,
            request_id=f"it-{self.calls}",
            status=PROVIDER_STATUS_GENERATED,
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", True)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "minimax")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_APPROVED", True)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PAIR_SCOPE", "group_boundaries")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_PAIRS", 8)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_TOTAL_MS", 100000)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_DURATION_MS", 2000)
    yield
    set_default_provider(None)


def _fake_extract(media_path, *, at_ms, output_path, position="exact", timeout=60.0):
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color=(9, 9, 9)).save(out, format="PNG")
    return out


def _write(tmp_path: Path, rel: str, obj) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _make_state(tmp_path: Path, provider: _FakeProvider) -> dict:
    out_root = tmp_path / "outputs"
    media_file = tmp_path / "media_src.mp4"
    media_file.write_bytes(b"0" * 256)

    groups = [
        {"group_id": "g1", "clips": [{"clip_id": "a1", "media_id": "m1", "path": str(media_file),
                                      "source_in_ms": 0, "source_out_ms": 4000}]},
        {"group_id": "g2", "clips": [{"clip_id": "b1", "media_id": "m1", "path": str(media_file),
                                      "source_in_ms": 4000, "source_out_ms": 8000}]},
    ]
    media = [{
        "media_id": "m1", "path": str(media_file), "media_type": "video",
        "metadata": {"width": 1080, "height": 1920, "duration_ms": 60000},
    }]
    # 基础时间线(与分组一致的两段,预算 10s → 插 2s 转场需要吸收 2s)
    base = {
        "schema_version": "1.0",
        "job_id": "it",
        "created_at_ms": 1,
        "source_media": [{"media_id": "m1", "file_uri": f"file:///{media_file.as_posix()}",
                          "duration_ms": 60000, "media_type": "video",
                          "width": 1080, "height": 1920, "fps": 30.0}],
        "clips": [
            {"clip_id": "a1", "source_media_id": "m1", "source_in_ms": 0, "source_out_ms": 4000,
             "timeline_in_ms": 0, "timeline_out_ms": 4000, "transcript": "第一段字幕"},
            {"clip_id": "b1", "source_media_id": "m1", "source_in_ms": 4000, "source_out_ms": 8000,
             "timeline_in_ms": 4000, "timeline_out_ms": 8000, "transcript": "第二段字幕"},
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": "整段中文文案", "en": None},
        "options": {"enable_ai_transition": False, "enable_voiceover": True, "max_duration_ms": 10000},
        "method": "tts_align",
        "warnings": [],
    }
    return {
        "session_id": "it",
        "status_log": [],
        "error_log": [],
        "storyline_outputs_root": str(out_root),
        "storyline_groups_artifact": _write(tmp_path, "in/groups.json", {"groups": groups}),
        "storyline_media_artifact": _write(tmp_path, "in/media.json", {"media": media}),
        "storyline_timeline_plan": _write(tmp_path, "storyline/timeline_plan.json", base),
    }


def test_ai_transition_reaches_jianying_draft_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批准 + fake Provider:GAT 产物 → PTAT 物化 → mapper 出转场 material/segment。"""
    monkeypatch.setattr(
        "nodes.storyline.node_generate_ai_transition.extract_frame", _fake_extract
    )
    provider = _FakeProvider(duration_ms=2000)
    set_default_provider(provider)

    state = _make_state(tmp_path, provider)
    # 基础时间线文件留底,供"可回退"断言
    base_bytes = Path(state["storyline_timeline_plan"]).read_bytes()

    # 1) GAT:真实生成(fake Provider,链路真实)
    gen_out = storyline_generate_ai_transition_node(state)
    assert gen_out["status_log"] == ["storyline_generate_ai_transition_done"]
    doc = json.loads(Path(gen_out["storyline_ai_transition_artifact"]).read_text(encoding="utf-8"))
    assert doc["status"] == "ok" and doc["approved"] is True
    assert len(doc["items"]) == 1 and doc["items"][0]["status"] == "generated"
    transition_video = Path(doc["items"][0]["video_path"])
    assert transition_video.is_file()
    state["storyline_ai_transition_artifact"] = gen_out["storyline_ai_transition_artifact"]

    # 2) PTAT:物化进时间线
    plan_out = storyline_plan_timeline_ai_transition_node(state)
    assert plan_out["status_log"] == ["storyline_plan_timeline_ai_transition_done"]
    assert Path(plan_out["storyline_timeline_plan"]).name == "timeline_plan_ai.json"
    merged = json.loads(Path(plan_out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    base_state = json.loads(base_bytes.decode("utf-8"))
    assert Path(state["storyline_timeline_plan"]).read_bytes() == base_bytes  # 基础时间线可回退
    timeline = CanonicalTimeline.model_validate(merged)
    pairs = [c.ai_transition_pair for c in timeline.clips if c.ai_transition_pair]
    assert pairs == ["a1->b1"]
    # 时长预算不超限(2s 转场被吸收)
    total = timeline.clips[-1].timeline_out_ms
    assert total <= (merged["options"]["max_duration_ms"] or total)

    # 3) mapper:转场 = 普通视频片段,进剪映草稿结构
    draft = canonical_to_draft(timeline, canvas_w=1080, canvas_h=1920)
    videos = draft["materials"]["videos"]
    trans_materials = [v for v in videos if v["media_id"].startswith("transition_media_")]
    assert len(trans_materials) == 1
    assert Path(trans_materials[0]["path"]).is_file()
    segs = sorted(
        (s for tr in draft["tracks"] if tr.get("type") == "video" for s in tr["segments"]),
        key=lambda s: s["target_timerange"]["start"],
    )
    assert len(segs) == 3
    trans_seg = segs[1]
    assert trans_seg["speed"] == 1.0
    assert trans_seg["target_timerange"]["duration"] == 2_000_000  # 2s(微秒)
    # 原两段字幕不丢
    transcripts = [s.get("transcript") for s in segs if s.get("transcript")]
    assert "第一段字幕" in transcripts and "第二段字幕" in transcripts
    # base clips 的字幕文本不被转场插入破坏
    assert [c.transcript for c in timeline.clips if not c.ai_transition_pair] == [
        "第一段字幕", "第二段字幕",
    ]
    # base_state(可回退基线)不含转场
    assert all("ai_transition_pair" not in c for c in base_state["clips"])

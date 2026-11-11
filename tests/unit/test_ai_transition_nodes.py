"""单测:AI 转场两个节点(2026-11-12 noop 治理阶段2 重写 GAT 段)。

- ``storyline_generate_ai_transition``:真实生成链(授权门禁 → Provider →
  首尾帧 → 提示词 → 缓存 → MP4)。未批准/未配 Provider 时 0 次外呼、显式状态;
  单 pair 失败降级硬切留痕;**不再产生 stub 占位**。
- ``storyline_plan_timeline_ai_transition``:在 plan_timeline_pro 写出的基础时间线
  上只挂清单,不重算、不改 clips;任何失败都保留基础时间线。
- 两个节点都在 qa 重试回路里:**状态标签必须固定**(qa 路由已改用
  ``storyline_qa_route_signal`` 信号字段,标签去重不再影响路由,但固定标签
  仍是通道整洁性纪律)。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from nodes.storyline import node_generate_ai_transition as gen_mod
from nodes.storyline.node_generate_ai_transition import (
    _adjacent_pairs,
    _all_adjacent_pairs,
    storyline_generate_ai_transition_node,
)
from nodes.storyline.node_plan_timeline_ai_transition import (
    storyline_plan_timeline_ai_transition_node,
)
from storyline.contract import CanonicalTimeline
from storyline_capabilities.ai_transition_provider import (
    PROVIDER_STATUS_FAILED,
    PROVIDER_STATUS_GENERATED,
    AITransitionProviderResult,
    set_default_provider,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _flag_on_and_reset_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", True)
    # 配置默认:未配 Provider、未批准(测试按用例 monkeypatch config)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_APPROVED", False)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PAIR_SCOPE", "group_boundaries")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_PAIRS", 8)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_TOTAL_MS", 100000)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_DURATION_MS", 4000)
    set_default_provider(None)
    yield
    set_default_provider(None)


@pytest.fixture()
def fast_frames(monkeypatch: pytest.MonkeyPatch):
    """把节点里的抽帧换成 1x1 PNG 落盘(不跑真 ffmpeg;真实抽帧另有专测)。"""
    import io

    from PIL import Image

    def fake_extract(media_path, *, at_ms, output_path, position="exact", timeout=60.0):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color=(200, 100, 50)).save(out, format="PNG")
        return out

    monkeypatch.setattr(gen_mod, "extract_frame", fake_extract)
    return fake_extract


def _groups(*clip_id_lists: list[str]) -> list[dict]:
    return [
        {
            "group_id": f"g{i + 1}",
            "clips": [{"clip_id": cid, "media_id": "m1"} for cid in ids],
        }
        for i, ids in enumerate(clip_id_lists)
    ]


def _write(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _media_artifact(tmp_path: Path, *, width: int = 1080, height: int = 1920) -> str:
    """load_media 形状的媒体索引(m1=指定尺寸,m2=1:1 方形,供宽高比守卫用例)。"""
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    m1 = media_dir / "m1.mp4"
    m1.write_bytes(b"0" * 128)
    m2 = media_dir / "m2.jpg"
    m2.write_bytes(b"1" * 64)
    return _write(
        tmp_path / "in" / "media.json",
        {
            "media": [
                {
                    "media_id": "m1",
                    "path": str(m1),
                    "media_type": "video",
                    "metadata": {"width": width, "height": height, "duration_ms": 60000},
                },
                {
                    "media_id": "m2",
                    "path": str(m2),
                    "media_type": "image",
                    "metadata": {"width": 100, "height": 100, "duration_ms": 0},
                },
            ]
        },
    )


def _gen_state(tmp_path: Path, groups: list[dict] | None, **over: object) -> dict:
    state: dict = {"session_id": "unit", "storyline_outputs_root": str(tmp_path), "status_log": [], "error_log": []}
    if groups is not None:
        state["storyline_groups_artifact"] = _write(tmp_path / "in" / "groups.json", {"groups": groups})
    state.update(over)
    return state


class _FakeProvider:
    """可编程 Provider:记录调用、按序失败、产物写假字节(节点只看返回字段)。"""

    name = "minimax"
    model = "MiniMax-Hailuo-02"

    def __init__(self, fail_on: set[int] | None = None, duration_ms: int = 4000) -> None:
        self.calls: list[dict] = []
        self.fail_on = set(fail_on or ())
        self.duration_ms = duration_ms

    def generate_transition(
        self,
        *,
        first_frame: Path,
        last_frame: Path,
        prompt: str,
        duration_ms: int,
        resolution: tuple[int, int],
        output_path: Path,
    ) -> AITransitionProviderResult:
        n = len(self.calls)
        self.calls.append(
            {
                "prompt": prompt,
                "first_frame": str(first_frame),
                "last_frame": str(last_frame),
                "duration_ms": duration_ms,
                "resolution": resolution,
            }
        )
        if n in self.fail_on:
            return AITransitionProviderResult(
                provider=self.name,
                model=self.model,
                status=PROVIDER_STATUS_FAILED,
                error="模型服务挂了",
            )
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        return AITransitionProviderResult(
            path=out,
            provider=self.name,
            model=self.model,
            duration_ms=self.duration_ms,
            width=1080,
            height=1920,
            fps=30.0,
            sha256="a" * 64,
            request_id=f"task_{n + 1}",
            status=PROVIDER_STATUS_GENERATED,
        )


def _read_doc(state_update: dict) -> dict:
    return json.loads(Path(state_update["storyline_ai_transition_artifact"]).read_text(encoding="utf-8"))


def _approved_minimax(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "minimax")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_APPROVED", True)
    set_default_provider(provider)


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------
def test_adjacent_pairs_use_last_clip_of_previous_group_and_first_of_next() -> None:
    pairs = _adjacent_pairs(_groups(["a1", "a2", "a3"], ["b1", "b2"], ["c1"]))
    assert [(f["clip_id"], t["clip_id"]) for f, t in pairs] == [("a3", "b1"), ("b2", "c1")]


def test_all_adjacent_pairs_cover_every_neighbor() -> None:
    """all_adjacent 档位:组内相邻 + 组间边界 = N-1 个 pair(FireRed 口径)。"""
    pairs = _all_adjacent_pairs(_groups(["a1", "a2"], ["b1", "b2"]))
    assert [(f["clip_id"], t["clip_id"]) for f, t in pairs] == [
        ("a1", "a2"),
        ("a2", "b1"),
        ("b1", "b2"),
    ]


def test_adjacent_pairs_single_group_or_nothing_gives_no_pairs() -> None:
    assert _adjacent_pairs(_groups(["a1", "a2"])) == []
    assert _adjacent_pairs([]) == []


def test_adjacent_pairs_skip_empty_groups_and_bad_entries() -> None:
    groups = [
        {"group_id": "g1", "clips": [{"clip_id": "a1"}]},
        {"group_id": "g2", "clips": []},
        "不是 dict",
        {"group_id": "g4", "clips": [None, {"clip_id": "d1"}, 7]},
        {"group_id": "g5"},
    ]
    pairs = _adjacent_pairs(groups)  # type: ignore[arg-type]
    assert [(f["clip_id"], t["clip_id"]) for f, t in pairs] == [("a1", "d1")]


# ---------------------------------------------------------------------------
# 授权门禁 / Provider 配置(付费执行的闸门)
# ---------------------------------------------------------------------------
def test_not_approved_writes_estimate_and_calls_provider_zero_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未批准:只写估算,0 次 Provider 外呼,items 为空(设计文档 §2.3)。"""
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)  # provider 已就绪,但未批准
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_APPROVED", False)

    out = storyline_generate_ai_transition_node(
        _gen_state(tmp_path, _groups(["a1"], ["b1"], ["c1"]))
    )
    assert out["status_log"] == ["storyline_generate_ai_transition_done"]
    doc = _read_doc(out)
    assert doc["status"] == "awaiting_approval"
    assert doc["approved"] is False
    assert doc["items"] == []
    assert doc["estimate"]["pair_count"] == 2
    assert provider.calls == [], "未批准绝不允许外呼"
    assert not (tmp_path / "storyline" / "ai_transitions" / "cache.json").exists()


def test_state_approval_field_also_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    """state.ai_transition_approved=true 单独即可批准(env 不必开)。"""
    provider = _FakeProvider()
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "minimax")
    set_default_provider(provider)
    out = storyline_generate_ai_transition_node(
        _gen_state(
            tmp_path,
            _groups(["a1"], ["b1"]),
            ai_transition_approved=True,
            storyline_media_artifact=_media_artifact(tmp_path),
        )
    )
    doc = _read_doc(out)
    assert doc["approved"] is True
    assert len(provider.calls) == 1


def test_approved_without_provider_is_explicitly_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_APPROVED", True)
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"])))
    doc = _read_doc(out)
    assert doc["status"] == "provider_unconfigured"
    assert doc["items"] == []
    assert any("STORYLINE_AI_TRANSITION_PROVIDER" in w for w in doc["warnings"])


def test_provider_name_without_key_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名字配了但 Key 缺失 → ProviderNotConfigured 被接住,显式降级不抛。"""
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "minimax")
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_APPROVED", True)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    set_default_provider(None)  # 走真实 resolve(env 无 Key → 抛 ProviderNotConfigured)

    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"])))
    doc = _read_doc(out)
    assert doc["status"] == "provider_unconfigured"
    assert any("MINIMAX_API_KEY" in w for w in doc["warnings"])


# ---------------------------------------------------------------------------
# 真实生成链(批准 + Provider 就绪)
# ---------------------------------------------------------------------------
def test_generate_real_items_with_cache_and_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    state = _gen_state(
        tmp_path,
        _groups(["a1"], ["b1"], ["c1"]),
        storyline_media_artifact=_media_artifact(tmp_path),
    )
    out = storyline_generate_ai_transition_node(state)
    doc = _read_doc(out)
    assert doc["status"] == "ok" and doc["approved"] is True
    assert doc["pair_scope"] == "group_boundaries"
    assert doc["is_stub"] is False
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in doc["items"]] == [("a1", "b1"), ("b1", "c1")]
    for it in doc["items"]:
        assert it["status"] == "generated" and it["is_stub"] is False
        assert it["video_path"] and Path(it["video_path"]).is_file()
        assert it["duration_ms"] == 4000 and it["provider"] == "minimax"
        assert it["cache_hit"] is False
    # 首尾帧真实抽了两对(文件存在)
    frames_dir = tmp_path / "storyline" / "ai_transitions" / "frames"
    assert len(list(frames_dir.glob("from_*.png"))) == 2
    assert len(list(frames_dir.glob("to_*.png"))) == 2
    # 缓存落盘
    assert (tmp_path / "storyline" / "ai_transitions" / "cache.json").is_file()
    # 契约条目合法
    CanonicalTimeline.model_validate(
        {
            "job_id": "u",
            "created_at_ms": 1,
            "source_media": [{"media_id": "m1", "file_uri": "file:///C:/a.mp4", "duration_ms": 1000}],
            "ai_transitions": doc["items"],
        }
    )


def test_qa_retry_hits_cache_and_skips_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    """qa 重试:同输入第二轮命中缓存,Provider 0 次新调用(不重复付费)。"""
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    state = _gen_state(
        tmp_path,
        _groups(["a1"], ["b1"]),
        storyline_media_artifact=_media_artifact(tmp_path),
    )
    storyline_generate_ai_transition_node(state)
    assert len(provider.calls) == 1
    second = storyline_generate_ai_transition_node(state)
    assert len(provider.calls) == 1, "第二轮不允许再调 Provider"
    doc = _read_doc(second)
    assert doc["items"][0]["cache_hit"] is True
    assert doc["items"][0]["video_path"]


def test_single_pair_failure_degrades_to_hard_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    provider = _FakeProvider(fail_on={0})
    _approved_minimax(monkeypatch, provider)
    out = storyline_generate_ai_transition_node(
        _gen_state(
            tmp_path,
            _groups(["a1"], ["b1"], ["c1"]),
            storyline_media_artifact=_media_artifact(tmp_path),
        )
    )
    doc = _read_doc(out)
    statuses = [i["status"] for i in doc["items"]]
    assert statuses == ["degraded", "generated"]
    assert doc["items"][0]["video_path"] is None and doc["items"][0]["error"]
    assert out["status_log"] == ["storyline_generate_ai_transition_done"]  # 固定标签
    assert any("降级" in w for w in doc["warnings"])


def test_unresolvable_media_degrades_that_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    """clip 无 path 且 media 索引无记录 → 该 pair unresolvable_media,不抛。"""
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    out = storyline_generate_ai_transition_node(
        _gen_state(tmp_path, _groups(["a1"], ["b1"]))  # 没有 storyline_media_artifact
    )
    doc = _read_doc(out)
    assert doc["items"][0]["status"] == "unresolvable_media"
    assert provider.calls == []


def test_aspect_ratio_guard_skips_incompatible_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    """两侧宽高比之比 > 1.1 → 跳过并记 incompatible_aspect_ratio(FireRed 守卫)。"""
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    mixed_groups = [
        {"group_id": "g1", "clips": [{"clip_id": "a1", "media_id": "m1"}]},  # 1080x1920
        {"group_id": "g2", "clips": [{"clip_id": "b1", "media_id": "m2"}]},  # 100x100
    ]
    out = storyline_generate_ai_transition_node(
        _gen_state(
            tmp_path,
            mixed_groups,
            storyline_media_artifact=_media_artifact(tmp_path),
        )
    )
    doc = _read_doc(out)
    assert doc["items"][0]["status"] == "incompatible_aspect_ratio"
    assert provider.calls == []


def test_budget_caps_block_extra_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    """max_pairs 与 max_total_ms 双闸:超出部分显式 skipped,不静默截断。"""
    provider = _FakeProvider(duration_ms=4000)
    _approved_minimax(monkeypatch, provider)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_PAIRS", 1)
    out = storyline_generate_ai_transition_node(
        _gen_state(
            tmp_path,
            _groups(["a1"], ["b1"], ["c1"]),
            storyline_media_artifact=_media_artifact(tmp_path),
        )
    )
    doc = _read_doc(out)
    assert [i["status"] for i in doc["items"]] == ["generated", "skipped_max_pairs"]

    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_PAIRS", 8)
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_MAX_TOTAL_MS", 5000)
    provider2 = _FakeProvider(duration_ms=4000)
    set_default_provider(provider2)
    out2 = storyline_generate_ai_transition_node(
        _gen_state(
            tmp_path / "b",
            _groups(["a1"], ["b1"], ["c1"]),
            storyline_media_artifact=_media_artifact(tmp_path / "b"),
        )
    )
    doc2 = _read_doc(out2)
    assert [i["status"] for i in doc2["items"]] == ["generated", "skipped_budget_exceeded"]


def test_all_adjacent_scope_writes_pair_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PAIR_SCOPE", "all_adjacent")
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    out = storyline_generate_ai_transition_node(
        _gen_state(
            tmp_path,
            _groups(["a1", "a2"], ["b1"]),
            storyline_media_artifact=_media_artifact(tmp_path),
        )
    )
    doc = _read_doc(out)
    assert doc["pair_scope"] == "all_adjacent"
    assert len(provider.calls) == 2  # a1→a2, a2→b1


def test_prompt_receives_pair_context_and_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames
) -> None:
    """提示词组装收到 pair 上下文;推荐转场 hints 作为参考进入 user_request。"""
    captured: list[dict] = []

    def fake_compose(*, from_clip, to_clip, user_request="", client=None):
        captured.append({"from": from_clip["clip_id"], "to": to_clip["clip_id"], "req": user_request})
        return ("Smooth continuous single shot. test.", "text_llm")

    monkeypatch.setattr(gen_mod, "compose_transition_prompt", fake_compose)
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    state = _gen_state(
        tmp_path,
        _groups(["a1"], ["b1"]),
        storyline_media_artifact=_media_artifact(tmp_path),
    )
    state["storyline_transition_plan"] = {
        "items": [{"group_id": "g1", "transition_in": "", "transition_out": "wipe_left"}]
    }
    storyline_generate_ai_transition_node(state)
    assert captured[0]["from"] == "a1" and captured[0]["to"] == "b1"
    assert "wipe_left" in captured[0]["req"]


# ---------------------------------------------------------------------------
# 空输入 / 开关 / 隔离
# ---------------------------------------------------------------------------
def test_generate_with_one_group_writes_no_pairs(tmp_path: Path) -> None:
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1", "a2"])))
    doc = _read_doc(out)
    assert doc["items"] == [] and doc["status"] == "no_pairs"
    assert out["status_log"] == ["storyline_generate_ai_transition_done"]


def test_generate_without_groups_artifact_is_not_an_error(tmp_path: Path) -> None:
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, None))
    assert _read_doc(out)["items"] == []
    assert "error_log" not in out


def test_generate_flag_off_only_writes_skipped_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", False)
    out = storyline_generate_ai_transition_node(_gen_state(tmp_path, _groups(["a1"], ["b1"])))
    assert out == {"status_log": ["storyline_generate_ai_transition_skipped"]}


def test_generate_body_failure_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_state):
        raise RuntimeError("读盘炸了")

    monkeypatch.setattr(gen_mod, "_read_groups", boom)
    state = _gen_state(tmp_path, _groups(["a1"], ["b1"]))
    out = storyline_generate_ai_transition_node(state)
    assert out["status_log"][-1] == "storyline_generate_ai_transition_failed"
    assert "storyline_ai_transition_artifact" not in out
    assert any("[storyline:generate_ai_transition] TOOL_EXECUTION_FAILED" in e for e in out["error_log"])


def test_generate_keeps_earlier_error_log_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_mod, "_read_groups", lambda _s: (_ for _ in ()).throw(RuntimeError("x")))
    state = _gen_state(tmp_path, _groups(["a1"], ["b1"]))
    state["error_log"] = ["早先的错误"]
    out = storyline_generate_ai_transition_node(state)
    assert out["error_log"][0] == "早先的错误" and len(out["error_log"]) == 2


@pytest.mark.parametrize("group_sizes", [[["a1"]], [["a1"], ["b1"]], [["a1"], ["b1"], ["c1"], ["d1"]]])
def test_generate_status_tag_is_constant_for_any_item_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_frames, group_sizes
) -> None:
    provider = _FakeProvider()
    _approved_minimax(monkeypatch, provider)
    out = storyline_generate_ai_transition_node(
        _gen_state(tmp_path, _groups(*group_sizes), storyline_media_artifact=_media_artifact(tmp_path))
    )
    assert out["status_log"] == ["storyline_generate_ai_transition_done"]


def test_transition_hints_indexes_by_group_id_and_ignores_bad_items() -> None:
    hints = gen_mod._transition_hints(
        {
            "items": [
                {"group_id": "g1", "transition_in": "a", "transition_out": "b"},
                {"group_id": "g2"},  # 缺字段 → 空串
                {"transition_in": "x"},  # 没有 group_id → 丢弃
                "垃圾",
                None,
            ]
        }
    )
    assert hints == {
        "g1": {"transition_in": "a", "transition_out": "b"},
        "g2": {"transition_in": "", "transition_out": ""},
    }


# ---------------------------------------------------------------------------
# storyline_plan_timeline_ai_transition_node(阶段2b 将改造为物化;以下为现行为)
# ---------------------------------------------------------------------------
def _base_timeline() -> dict:
    return {
        "schema_version": "1.0",
        "job_id": "unit",
        "created_at_ms": 1,
        "source_media": [{"media_id": "m1", "file_uri": "file:///C:/a.mp4", "duration_ms": 60000}],
        "clips": [
            {
                "clip_id": cid,
                "source_media_id": "m1",
                "source_in_ms": i * 4000,
                "source_out_ms": i * 4000 + 4000,
                "timeline_in_ms": i * 4000,
                "timeline_out_ms": i * 4000 + 4000,
                "transcript": f"字幕{cid}",
            }
            for i, cid in enumerate(["c1", "c2", "c3"])
        ],
        "audio": {"bgm_ref": "bgm_0001", "voiceover": None},
        "subtitles": {"zh": "整段中文文案", "en": None},
        "options": {"enable_ai_transition": False, "enable_voiceover": True, "max_duration_ms": 35000},
        "method": "tts_align",
        "warnings": ["提示一条"],
    }


def _item(a: str, b: str, **kw: Any) -> dict:
    d = {"from_clip_id": a, "to_clip_id": b, "duration_ms": 500, "provider": "stub", "is_stub": True,
         "error": None, "artifact": None}
    d.update(kw)
    return d


def _plan_state(tmp_path: Path, items: list[dict] | None, base: dict | None = None) -> dict:
    state: dict = {"session_id": "unit", "storyline_outputs_root": str(tmp_path), "status_log": [], "error_log": []}
    if base is not None:
        state["storyline_timeline_plan"] = _write(tmp_path / "storyline" / "timeline_plan.json", base)
    if items is not None:
        state["storyline_ai_transition_artifact"] = _write(tmp_path / "storyline" / "ai_transition.json", {"items": items})
    return state


def test_plan_attaches_items_writes_new_file_and_leaves_base_untouched(tmp_path: Path) -> None:
    base = _base_timeline()
    state = _plan_state(tmp_path, [_item("c1", "c2"), _item("c2", "c3")], base)
    base_bytes = Path(state["storyline_timeline_plan"]).read_bytes()

    out = storyline_plan_timeline_ai_transition_node(state)

    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_done"
    new_path = Path(out["storyline_timeline_plan"])
    assert new_path.name == "timeline_plan_ai.json"
    assert Path(state["storyline_timeline_plan"]).read_bytes() == base_bytes  # 基础时间线原封不动
    merged = json.loads(new_path.read_text(encoding="utf-8"))
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in merged["ai_transitions"]] == [("c1", "c2"), ("c2", "c3")]
    CanonicalTimeline.model_validate(merged)


def test_plan_does_not_touch_clips_subtitles_transcripts_or_duration(tmp_path: Path) -> None:
    """旧版会重算时间线并丢掉字幕 / transcript;新版只能多出 ai_transitions。"""
    base = _base_timeline()
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, [_item("c1", "c2")], base))
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    expected = copy.deepcopy(base)
    expected["ai_transitions"] = [_item("c1", "c2")]
    assert merged == expected  # stub → enable_ai_transition 仍为 False,其余逐字段相同
    assert [c["transcript"] for c in merged["clips"]] == ["字幕c1", "字幕c2", "字幕c3"]
    assert merged["subtitles"]["zh"] == "整段中文文案"


def test_plan_filters_items_whose_clips_are_not_in_the_timeline(tmp_path: Path) -> None:
    """qa 重试 / 重新分组后 clip_id 可能变化:悬空引用必须被剔除,而不是让契约校验失败。"""
    items = [_item("c1", "c2"), _item("c2", "ghost"), _item("ghost", "c3")]
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, items, _base_timeline()))
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    assert [(i["from_clip_id"], i["to_clip_id"]) for i in merged["ai_transitions"]] == [("c1", "c2")]


def test_plan_all_items_dangling_keeps_base_plan(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("x", "y")], _base_timeline())
    )
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_done"
    assert "storyline_timeline_plan" not in out  # 不改指针 → 下游继续用基础时间线


@pytest.mark.parametrize("items", [None, []])
def test_plan_without_items_keeps_base_plan(tmp_path: Path, items) -> None:
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, items, _base_timeline()))
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_done"
    assert "storyline_timeline_plan" not in out


def test_plan_without_base_timeline_is_skipped(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, [_item("c1", "c2")], None))
    assert out == {"status_log": ["storyline_plan_timeline_ai_transition_skipped"]}


def test_plan_invalid_merge_keeps_base_and_logs_error(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("c1", "c2", duration_ms=-5)], _base_timeline())
    )
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_failed"
    assert "storyline_timeline_plan" not in out
    assert any("[storyline:plan_timeline_ai_transition]" in e for e in out["error_log"])
    assert not (tmp_path / "storyline" / "timeline_plan_ai.json").exists()  # 没写出半成品


def test_plan_unknown_field_in_item_is_rejected_not_written(tmp_path: Path) -> None:
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("c1", "c2", surprise=1)], _base_timeline())
    )
    assert out["status_log"][-1] == "storyline_plan_timeline_ai_transition_failed"
    assert not (tmp_path / "storyline" / "timeline_plan_ai.json").exists()


def test_plan_flag_off_only_writes_skipped_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION", False)
    out = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path, [_item("c1", "c2")], _base_timeline())
    )
    assert out == {"status_log": ["storyline_plan_timeline_ai_transition_skipped"]}


def test_plan_creates_options_when_base_has_none(tmp_path: Path) -> None:
    base = _base_timeline()
    del base["options"]
    out = storyline_plan_timeline_ai_transition_node(_plan_state(tmp_path, [_item("c1", "c2")], base))
    merged = json.loads(Path(out["storyline_timeline_plan"]).read_text(encoding="utf-8"))
    assert merged["options"]["enable_ai_transition"] is False
    CanonicalTimeline.model_validate(merged)


def test_plan_keeps_earlier_error_log_entries_on_failure(tmp_path: Path) -> None:
    state = _plan_state(tmp_path, [_item("c1", "c2", duration_ms=-1)], _base_timeline())
    state["error_log"] = ["早先的错误"]
    out = storyline_plan_timeline_ai_transition_node(state)
    assert out["error_log"][0] == "早先的错误" and len(out["error_log"]) == 2


def test_plan_every_success_path_uses_the_same_done_tag(tmp_path: Path) -> None:
    attached = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path / "a", [_item("c1", "c2")], _base_timeline())
    )
    nothing_to_attach = storyline_plan_timeline_ai_transition_node(
        _plan_state(tmp_path / "b", [], _base_timeline())
    )
    assert attached["status_log"] == nothing_to_attach["status_log"] == [
        "storyline_plan_timeline_ai_transition_done"
    ]

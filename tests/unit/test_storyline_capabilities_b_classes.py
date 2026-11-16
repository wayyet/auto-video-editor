"""B 类(阶段 2/3)单元测试。

覆盖:
- vlm_client Protocol + StubLLMClient + parse_json_loose
- prompts 加载器(双语 + 变量替换)
- understand_clips / filter_clips / group_clips capability(plan §5 阶段 2)
- generate_script / recommend_transition / recommend_text capability(plan §5 阶段 3)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from storyline_capabilities.vlm_client import (
    StubLLMClient,
    chat_json,
    chat_text,
    get_default_client,
    parse_json_loose,
    set_default_client,
)
from storyline_capabilities.prompts import load_prompt, render_prompt
from storyline_capabilities.understand_clips import understand_clips
from storyline_capabilities.filter_clips import filter_clips
from storyline_capabilities.group_clips import group_clips
from storyline_capabilities.generate_script import generate_script
from storyline_capabilities.recommend_transition import recommend_transition
from storyline_capabilities.recommend_text import recommend_text


# ---------------------------------------------------------------------------
# vlm_client
# ---------------------------------------------------------------------------
def test_stub_llm_returns_text() -> None:
    s = StubLLMClient(default_caption="hello")
    out = s.chat(
        system_prompt="x",
        user_prompt="y",
    )
    assert out == "hello"
    assert s.call_count == 1


def test_stub_llm_returns_json_with_schema() -> None:
    s = StubLLMClient()
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
        },
    }
    out = s.chat(system_prompt="", user_prompt="", json_schema=schema)
    obj = json.loads(out)
    assert obj == {"a": "", "b": 0}


def test_parse_json_loose_strips_think() -> None:
    raw = "<think>...思考段...</think>\n{\"x\": 1, \"y\": \"ok\"}"
    obj = parse_json_loose(raw)
    assert obj == {"x": 1, "y": "ok"}


def test_parse_json_loose_returns_empty_on_garbage() -> None:
    assert parse_json_loose("") == {}
    assert parse_json_loose("not json at all") == {}


def test_chat_json_returns_dict_with_default_client() -> None:
    """默认 client 是 StubLLM,chat_json 用 json_schema 生成 stub dict。"""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
    }
    obj = chat_json(system_prompt="", user_prompt="", schema=schema)
    assert isinstance(obj, dict)


def test_set_and_get_default_client_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = StubLLMClient(default_caption="custom")
    set_default_client(custom)
    out = chat_text(system_prompt="x", user_prompt="y")
    assert out == "custom"
    # 还原
    set_default_client(None)
    default = get_default_client()
    assert isinstance(default, StubLLMClient)


def test_chat_text_returns_str() -> None:
    out = chat_text(system_prompt="x", user_prompt="y")
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# prompts 加载器
# ---------------------------------------------------------------------------
def test_load_prompt_zh_exists() -> None:
    text = load_prompt("understand_clips", "system_detail", lang="zh")
    assert isinstance(text, str)
    assert len(text) > 0


def test_load_prompt_en_exists() -> None:
    text = load_prompt("understand_clips", "system_detail", lang="en")
    assert isinstance(text, str)
    assert len(text) > 0


def test_load_prompt_missing() -> None:
    text = load_prompt("doesnt_exist", "nope", lang="zh")
    assert text == ""


def test_render_prompt_variable_substitution() -> None:
    # 注入一个临时 md 文件测替换
    import tempfile

    with tempfile.NamedTemporaryFile(
        suffix=".md", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write("Hello {name}, today is {day}.")
        f.flush()
        path = Path(f.name)
        # 直接用 path 加载渲染
        out = path.read_text(encoding="utf-8").replace("{name}", "Alice").replace(
            "{day}", "Monday"
        )
        assert "Hello Alice" in out
        assert "Monday" in out


# ---------------------------------------------------------------------------
# understand_clips(plan §5 阶段 2)
# ---------------------------------------------------------------------------
def test_understand_clips_missing_media(tmp_path: Path) -> None:
    res = understand_clips(
        split_shots_artifact={"shots": {}},
        media_artifact={"media": []},
    )
    assert res["clip_captions"] == []


def test_understand_clips_basic_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_img = tmp_path / "img.jpg"
    fake_img.write_bytes(b"\x00" * 64)
    media_artifact = {
        "media": [
            {
                "media_id": "media_0001",
                "path": str(fake_img),
                "media_type": "image",
            }
        ]
    }
    splits = {
        "shots": {
            "media_0001": [
                {
                    "clip_id": "clip_0001",
                    "media_id": "media_0001",
                    "kind": "image",
                    "source_in_ms": 0,
                    "source_out_ms": 1000,
                    "source_ref": {"media_id": "media_0001", "start": 0, "end": 1000},
                }
            ]
        }
    }
    client = StubLLMClient(
        default_caption='{"caption": "test_caption", "aes_score": 0.5}'
    )
    res = understand_clips(
        split_shots_artifact=splits,
        media_artifact=media_artifact,
        client=client,
    )
    captions = res["clip_captions"]
    assert len(captions) == 1
    assert captions[0]["caption"] == "test_caption"


def test_understand_clips_handles_vlm_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vlm 抛异常 → 写入 ``aes_score=-1.0`` 而**不**吞错(plan §1.3 案例二防复发)。"""

    fake_img = tmp_path / "img.jpg"
    fake_img.write_bytes(b"\x00" * 64)
    splits = {
        "shots": {
            "media_0001": [
                {
                    "clip_id": "clip_x",
                    "media_id": "media_0001",
                    "kind": "image",
                    "source_in_ms": 0,
                    "source_out_ms": 1000,
                    "source_ref": {"media_id": "media_0001", "start": 0, "end": 1000},
                }
            ]
        }
    }
    media_artifact = {
        "media": [{"media_id": "media_0001", "path": str(fake_img), "media_type": "image"}]
    }

    class _BoomClient:
        def chat(self, **kwargs):
            raise RuntimeError("forced vlm boom")

    res = understand_clips(
        split_shots_artifact=splits,
        media_artifact=media_artifact,
        client=_BoomClient(),
    )
    assert res["clip_captions"][0]["aes_score"] == -1.0
    assert "forced vlm boom" in str(res.get("vlm_errors", []))


def test_understand_clips_swallows_unsupported_kind(tmp_path: Path) -> None:
    splits = {
        "shots": {
            "media_0001": [
                {
                    "clip_id": "clip_a",
                    "kind": "audio",  # 不支持
                    "source_ref": {"media_id": "media_0001"},
                }
            ]
        }
    }
    res = understand_clips(
        split_shots_artifact=splits,
        media_artifact={"media": [{"media_id": "media_0001", "path": "x.mp3"}]},
        client=StubLLMClient(),
    )
    assert res["clip_captions"][0]["aes_score"] == -1.0


# ---------------------------------------------------------------------------
# filter_clips
# ---------------------------------------------------------------------------
def test_filter_clips_no_input() -> None:
    res = filter_clips(understanding_artifact={})
    assert res["filtered_clips"] == []


def test_filter_clips_top_n_score(tmp_path: Path) -> None:
    captions = [
        {"clip_id": f"c{i}", "caption": f"capt {i}", "aes_score": float(10 - i)}
        for i in range(5)
    ]
    res = filter_clips(
        understanding_artifact={"clip_captions": captions},
        keep_ratio=0.4,
        client=StubLLMClient(default_caption="{}"),  # no llm override
    )
    assert res["method"] == "score_topN"
    # 5 clip × 40% = 2 clip kept
    assert len(res["filtered_clips"]) == 2
    assert res["filtered_clips"][0]["aes_score"] >= res["filtered_clips"][-1]["aes_score"]


def test_filter_clips_llm_override(tmp_path: Path) -> None:
    captions = [
        {"clip_id": "c1", "caption": "x", "aes_score": 1.0},
        {"clip_id": "c2", "caption": "y", "aes_score": 0.5},
        {"clip_id": "c3", "caption": "z", "aes_score": 0.0},
    ]
    # LLM 给出"keep c3 / drop c1"
    client = StubLLMClient(
        default_caption='{"keep_clip_ids": ["c3"], "drop_clip_ids": ["c1"]}'
    )
    res = filter_clips(
        understanding_artifact={"clip_captions": captions},
        client=client,
    )
    assert res["method"] == "llm_overlay"


# ---------------------------------------------------------------------------
# group_clips
# ---------------------------------------------------------------------------
def test_group_clips_empty(tmp_path: Path) -> None:
    res = group_clips(filtered_clips=[])
    assert res["groups"] == []


def test_group_clips_three_clips_per_group(tmp_path: Path) -> None:
    clips = [
        {
            "clip_id": f"c{i}",
            "media_id": "media_0001",
            "source_in_ms": i * 1000,
            "source_out_ms": (i + 1) * 1000,
            "caption": f"clip {i}",
        }
        for i in range(7)
    ]
    res = group_clips(filtered_clips=clips, group_size=3)
    assert len(res["groups"]) == 3  # 3 + 3 + 1
    assert res["groups"][0]["group_id"] == "group_0001"
    assert res["groups"][-1]["group_id"] == "group_0003"


# ---------------------------------------------------------------------------
# generate_script / recommend_transition / recommend_text(plan §5 阶段 3)
# ---------------------------------------------------------------------------
def test_generate_script_no_input() -> None:
    res = generate_script(groups=[])
    assert res["group_scripts"] == []


def test_generate_script_with_three_groups(tmp_path: Path) -> None:
    groups = [
        {"group_id": f"group_{i}", "theme": "", "clips": [{"clip_id": f"c{i}"}]}
        for i in range(3)
    ]
    client = StubLLMClient(
        default_caption=json.dumps(
            {"scripts": [{"narration": f"旁白 {i}"} for i in range(3)]}
        )
    )
    res = generate_script(groups=groups, client=client)
    assert len(res["group_scripts"]) == 3
    for i, s in enumerate(res["group_scripts"]):
        assert s["group_id"] == f"group_{i}"


def test_recommend_transition_no_input() -> None:
    res = recommend_transition(groups=[])
    assert res["items"] == []
    assert res["default"] == "fade"
    assert res["method"] == "no_input"
    assert res["warnings"] == []


def test_recommend_transition_with_groups(tmp_path: Path) -> None:
    """懒加载 StubLLMClient:按 schema 返回 {"items": [], "default": ""}。

    20261117 契约:method 如实标 stub(不再让 truthy dict 伪装 llm);
    缺失组按校验过的 default(fade)补齐,warnings 记录降级原因。
    """
    groups = [{"group_id": "g1"}, {"group_id": "g2"}]
    res = recommend_transition(groups=groups)
    assert len(res["items"]) == 2
    for it in res["items"]:
        assert it.get("transition_in") == "fade"
        assert it.get("transition_out") == "fade"
    assert res["method"] == "stub"
    assert res["warnings"]


def _patch_transition_trace(
    monkeypatch: pytest.MonkeyPatch,
    obj: dict,
    *,
    ok: bool = True,
    parsed: bool = True,
    error: str = "",
) -> None:
    """替换 chat_json_with_trace,返回受控 (obj, trace)。"""

    def fake_trace(*, system_prompt, user_prompt, schema=None, client=None, **_kw):
        return obj, {"ok": ok, "error": error, "raw_head": "", "parsed": parsed}

    monkeypatch.setattr(
        "storyline_capabilities.recommend_transition.chat_json_with_trace",
        fake_trace,
    )


def test_recommend_transition_aligns_by_group_id_not_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """乱序 + 漏组 + 未知组:按 group_id 对齐,缺失按 default 补齐,输出保持输入顺序。"""
    obj = {
        "items": [
            {"group_id": "g2", "transition_in": "cut", "transition_out": "whip"},
            {"group_id": "ghost", "transition_in": "fade", "transition_out": "fade"},
            {"group_id": "g1", "transition_in": "zoom", "transition_out": "slide"},
        ],
        "default": "fade",
    }
    _patch_transition_trace(monkeypatch, obj, ok=True, parsed=True)
    res = recommend_transition(
        groups=[{"group_id": "g1"}, {"group_id": "g2"}, {"group_id": "g3"}],
        client=object(),
    )
    assert res["method"] == "llm"
    assert res["items"][0] == {
        "group_id": "g1",
        "transition_in": "zoom",
        "transition_out": "slide",
    }
    assert res["items"][1] == {
        "group_id": "g2",
        "transition_in": "cut",
        "transition_out": "whip",
    }
    # g3 未给 → 按 default 补齐
    assert res["items"][2] == {
        "group_id": "g3",
        "transition_in": "fade",
        "transition_out": "fade",
    }
    assert any("未知 group_id='ghost'" in w for w in res["warnings"])
    assert any("缺少推荐" in w for w in res["warnings"])


def test_recommend_transition_invalid_enum_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法枚举值回退 default;非法 default 再回退 fade;全部可诊断。"""
    obj = {
        "items": [
            {"group_id": "g1", "transition_in": "旋转跳跃", "transition_out": ""},
        ],
        "default": "spin-around",
    }
    _patch_transition_trace(monkeypatch, obj, ok=True, parsed=True)
    res = recommend_transition(groups=[{"group_id": "g1"}], client=object())
    assert res["default"] == "fade"
    assert res["items"][0]["transition_in"] == "fade"
    assert res["items"][0]["transition_out"] == "fade"
    assert any("不在受控词表" in w for w in res["warnings"])


def test_recommend_transition_duplicate_and_empty_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输出重复 ID 保留首条;空 ID 条目忽略并记录。"""
    obj = {
        "items": [
            {"group_id": "g1", "transition_in": "cut", "transition_out": "fade"},
            {"group_id": "g1", "transition_in": "zoom", "transition_out": "zoom"},
            {"group_id": "", "transition_in": "whip", "transition_out": "whip"},
        ],
        "default": "fade",
    }
    _patch_transition_trace(monkeypatch, obj, ok=True, parsed=True)
    res = recommend_transition(groups=[{"group_id": "g1"}], client=object())
    assert res["items"][0] == {
        "group_id": "g1",
        "transition_in": "cut",
        "transition_out": "fade",
    }
    assert any("重复 group_id='g1'" in w for w in res["warnings"])
    assert any("空 group_id" in w for w in res["warnings"])


def test_recommend_transition_llm_failure_marks_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 client 调用异常 → method=fallback,全组 fade 补齐,原因落 warnings。"""
    _patch_transition_trace(
        monkeypatch, {}, ok=False, parsed=False, error="RuntimeError('boom')"
    )
    res = recommend_transition(
        groups=[{"group_id": "g1"}, {"group_id": "g2"}], client=object()
    )
    assert res["method"] == "fallback"
    assert res["default"] == "fade"
    assert all(it["transition_in"] == "fade" for it in res["items"])
    assert any("LLM 调用异常" in w for w in res["warnings"])


def test_recommend_transition_garbage_text_marks_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 client 返回散文(解析不出 JSON)→ method=fallback(旧 bug 的直接回归)。"""
    _patch_transition_trace(monkeypatch, {}, ok=True, parsed=False)
    res = recommend_transition(groups=[{"group_id": "g1"}], client=object())
    assert res["method"] == "fallback"
    assert any("未解析出非空 JSON" in w for w in res["warnings"])


def test_recommend_transition_prompts_render_all_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """专用提示词 zh/en 都存在且真正渲染 groups/bgm/user_request(核验报告 §6.3)。"""
    seen: dict[str, str] = {}

    def fake_trace(*, system_prompt, user_prompt, schema=None, client=None, **_kw):
        seen["system"] = system_prompt
        seen["user"] = user_prompt
        return {"items": [], "default": "fade"}, {
            "ok": True,
            "error": "",
            "raw_head": "",
            "parsed": True,
        }

    monkeypatch.setattr(
        "storyline_capabilities.recommend_transition.chat_json_with_trace",
        fake_trace,
    )
    for lang in ("zh", "en"):
        seen.clear()
        res = recommend_transition(
            groups=[{"group_id": "g1", "theme": "城市漫游"}],
            bgm_selection={"bgm_ref": "bgm_0007"},
            lang=lang,
            user_request="节奏快点",
            client=object(),
        )
        assert res["method"] == "llm"
        assert "g1" in seen["user"]
        assert "城市漫游" in seen["user"]
        assert "bgm_0007" in seen["user"]
        assert "节奏快点" in seen["user"]
        # 受控词表进入 system prompt
        assert "cut" in seen["system"]
        assert "creative" in seen["system"]


def test_recommend_text_returns_default_style() -> None:
    res = recommend_text(groups=[])
    assert res["items"] == []
    assert res["default"]["font_zh"] == "SourceHanSansCN-Bold.otf"


def test_recommend_text_for_two_groups(tmp_path: Path) -> None:
    groups = [{"group_id": "g1"}, {"group_id": "g2"}]
    res = recommend_text(groups=groups)
    assert len(res["items"]) == 2
    assert res["items"][0]["font_zh"] == "SourceHanSansCN-Bold.otf"

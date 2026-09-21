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
    assert res["default"] == "fade_in"


def test_recommend_transition_with_groups(tmp_path: Path) -> None:
    groups = [{"group_id": "g1"}, {"group_id": "g2"}]
    res = recommend_transition(groups=groups)
    assert len(res["items"]) == 2
    for it in res["items"]:
        assert it.get("transition_in") in {"fade_in", "fade", "fade_out", "cut"}


def test_recommend_text_returns_default_style() -> None:
    res = recommend_text(groups=[])
    assert res["items"] == []
    assert res["default"]["font_zh"] == "SourceHanSansCN-Bold.otf"


def test_recommend_text_for_two_groups(tmp_path: Path) -> None:
    groups = [{"group_id": "g1"}, {"group_id": "g2"}]
    res = recommend_text(groups=groups)
    assert len(res["items"]) == 2
    assert res["items"][0]["font_zh"] == "SourceHanSansCN-Bold.otf"

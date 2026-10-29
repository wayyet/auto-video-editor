"""单测:契约 ``CanonicalTimeline.ai_transitions``(2026-10)+ JSON Schema 对齐。

D9 教训:契约是 ``extra="forbid"``,生产端多写一个字段就会让三处校验全挂。
所以这里除了测新字段本身,还锁住"pydantic 字段 == JSON Schema 属性"这条对齐关系。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from storyline.contract import (
    AITransitionItem,
    CanonicalTimeline,
    Clip,
    SourceMedia,
    TimelineAudio,
    TimelineOptions,
    TimelineSubtitles,
)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "storyline" / "schemas" / "canonical_timeline.schema.json"


def _base() -> dict:
    return {
        "schema_version": "1.0",
        "job_id": "unit",
        "created_at_ms": 1,
        "source_media": [
            {"media_id": "m1", "file_uri": "file:///C:/a.mp4", "duration_ms": 60000}
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": 4000,
                "timeline_in_ms": 0,
                "timeline_out_ms": 4000,
            },
            {
                "clip_id": "c2",
                "source_media_id": "m1",
                "source_in_ms": 4000,
                "source_out_ms": 8000,
                "timeline_in_ms": 4000,
                "timeline_out_ms": 8000,
            },
        ],
    }


def test_ai_transitions_defaults_to_empty_list() -> None:
    tl = CanonicalTimeline.model_validate(_base())
    assert tl.ai_transitions == []


def test_ai_transitions_accepts_valid_items_and_keeps_defaults() -> None:
    d = _base()
    d["ai_transitions"] = [{"from_clip_id": "c1", "to_clip_id": "c2", "duration_ms": 500}]
    tl = CanonicalTimeline.model_validate(d)
    item = tl.ai_transitions[0]
    assert isinstance(item, AITransitionItem)
    assert (item.provider, item.is_stub, item.error, item.artifact) == ("stub", True, None, None)


def test_ai_transition_item_rejects_unknown_field() -> None:
    d = _base()
    d["ai_transitions"] = [
        {"from_clip_id": "c1", "to_clip_id": "c2", "duration_ms": 500, "surprise": 1}
    ]
    with pytest.raises(ValidationError):
        CanonicalTimeline.model_validate(d)


@pytest.mark.parametrize("bad", [-1, "abc", None])
def test_ai_transition_item_rejects_bad_duration(bad) -> None:
    d = _base()
    d["ai_transitions"] = [{"from_clip_id": "c1", "to_clip_id": "c2", "duration_ms": bad}]
    with pytest.raises(ValidationError):
        CanonicalTimeline.model_validate(d)


def test_root_still_rejects_unknown_field() -> None:
    d = _base()
    d["ai_transition"] = []  # 少了个 s —— 手误必须被拒,而不是悄悄通过
    with pytest.raises(ValidationError):
        CanonicalTimeline.model_validate(d)


def test_timeline_without_ai_transitions_is_unchanged_by_the_new_field() -> None:
    """旧存档 / 旧产物(没有 ai_transitions 键)照样通过,dump 出来只多一个空列表。"""
    tl = CanonicalTimeline.model_validate(_base())
    dumped = tl.model_dump()
    assert dumped["ai_transitions"] == []


# ---------------------------------------------------------------------------
# JSON Schema ↔ pydantic 对齐
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _props(node: dict) -> set[str]:
    return set((node.get("properties") or {}).keys())


@pytest.mark.parametrize(
    "label,pick,model",
    [
        ("root", lambda s: s, CanonicalTimeline),
        ("source_media[]", lambda s: s["properties"]["source_media"]["items"], SourceMedia),
        ("clips[]", lambda s: s["properties"]["clips"]["items"], Clip),
        ("audio", lambda s: s["properties"]["audio"], TimelineAudio),
        ("subtitles", lambda s: s["properties"]["subtitles"], TimelineSubtitles),
        ("options", lambda s: s["properties"]["options"], TimelineOptions),
        ("ai_transitions[]", lambda s: s["properties"]["ai_transitions"]["items"], AITransitionItem),
    ],
)
def test_json_schema_properties_match_pydantic_fields(label, pick, model, schema) -> None:
    node = pick(schema)
    assert _props(node) == set(model.model_fields.keys()), f"{label}:JSON Schema 与 pydantic 字段不一致"
    assert node.get("additionalProperties") is False, f"{label}:应与 extra='forbid' 对应"


@pytest.mark.parametrize(
    "label,pick,model",
    [
        ("root", lambda s: s, CanonicalTimeline),
        ("source_media[]", lambda s: s["properties"]["source_media"]["items"], SourceMedia),
        ("clips[]", lambda s: s["properties"]["clips"]["items"], Clip),
        ("ai_transitions[]", lambda s: s["properties"]["ai_transitions"]["items"], AITransitionItem),
    ],
)
def test_json_schema_required_covers_pydantic_required(label, pick, model, schema) -> None:
    """Schema 的 required 可以比 pydantic 更严(给生产端),但绝不能更松。"""
    pyd_required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert pyd_required <= set(pick(schema).get("required") or []), label


def test_example_document_validates_against_both() -> None:
    """同一份文档:pydantic 通过 ⇒ jsonschema(若装了)也通过。"""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    d = copy.deepcopy(_base())
    d["ai_transitions"] = [
        {
            "from_clip_id": "c1",
            "to_clip_id": "c2",
            "duration_ms": 500,
            "provider": "stub",
            "is_stub": True,
            "error": None,
            "artifact": "C:/x/ai.json",
        }
    ]
    CanonicalTimeline.model_validate(d)
    jsonschema.validate(d, schema)

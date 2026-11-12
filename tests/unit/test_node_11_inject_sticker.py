"""节点 11 贴纸单测(阶段 4:原生素材 + 占位资源 fail-closed)。

真实 resource_id 在模板回填前不可得,正向路径用 monkeypatch 把解析结果替换成
19 位真实形态 ID 模拟"已回填";占位 fail-closed 用真实模板断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.node_11_inject_sticker as node_11_mod
from nodes.node_11_inject_sticker import inject_sticker


_REAL_ID = "7325648367747338802"  # 19 位真实形态(模拟已回填)


@pytest.fixture
def tmp_draft_with_texts(tmp_path: Path) -> Path:
    """构造含字幕 + 视频段的草稿。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                {
                    "id": "t1",
                    "content": "真棒,给你点赞",   # 命中关键词 "赞"
                    "target_timerange": {"start": 0, "duration": 2_000_000},
                },
                {
                    "id": "t2",
                    "content": "继续努力加油",  # 命中关键词 "加油"
                    "target_timerange": {"start": 2_000_000, "duration": 2_000_000},
                },
                {
                    "id": "t3",
                    "content": "没有匹配的关键词",  # 不命中
                    "target_timerange": {"start": 4_000_000, "duration": 2_000_000},
                },
            ],
        },
        "tracks": [{
            "type": "video",
            "segments": [
                {"id": "s1", "target_timerange": {"start": 0, "duration": 6_000_000}},
            ],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


@pytest.fixture
def real_id_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """把解析结果替换成 19 位真实形态 ID(模拟操作员完成实机回填)。"""
    monkeypatch.setattr(
        node_11_mod, "resolve_sticker_resource_id", lambda _content: _REAL_ID
    )


def test_inject_sticker_placeholder_fail_closed(tmp_draft_with_texts: Path) -> None:
    """模板仍是 PLACEHOLDER_*(现状):不落任何贴纸素材/引用,degraded 计数 + 回填指引。"""
    out = inject_sticker(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    assert draft["materials"]["stickers"] == []
    assert all(
        not any(str(r).startswith("sticker-") for r in s.get("extra_material_refs", []))
        for s in draft["tracks"][0]["segments"]
    )
    # 匹配 3 条:"赞"/"加油" 命中 + 第 3 条走"默认"兜底(Week 3 既有行为)
    assert out["sticker_summary"]["zh"]["matched"] == 3
    assert out["sticker_summary"]["zh"]["degraded_placeholder"] == 3
    assert out["sticker_summary"]["zh"]["written"] == 0
    assert any("PLACEHOLDER" in e and "回填" in e for e in out["error_log"])
    assert "node_11_inject_sticker_done" in out["status_log"]


def test_inject_sticker_matches_keywords(
    tmp_draft_with_texts: Path, real_id_resolver: None
) -> None:
    """(已回填)字幕文本命中关键词 → 原生贴纸 material 创建并挂到视频段。"""
    out = inject_sticker(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    stickers = draft["materials"]["stickers"]
    assert len(stickers) >= 2
    for sticker in stickers:
        assert sticker["type"] == "sticker"
        assert sticker["resource_id"] == _REAL_ID
        assert int(sticker["duration"]) > 0
    seg_refs = draft["tracks"][0]["segments"][0].get("extra_material_refs", [])
    assert {s["id"] for s in stickers} <= set(seg_refs)
    assert out["sticker_summary"]["zh"]["written"] >= 2
    assert "node_11_inject_sticker_done" in out["status_log"]


def test_inject_sticker_end_to_end_after_real_backfill(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回填完成后端到端:真实模板文件(pending=false + 19 位 ID)→ 真实解析 → 正常写入。

    与 ``real_id_resolver`` 的桩不同,本用例不动 resolver —— 验证
    ``resolve_sticker_resource_id`` 读回填模板 → node_11 落草稿的完整链路
    (console-ef8504d3a62c 的反例:同一条链路,占位模板走到 fail-closed)。
    """
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    zh_id, en_id, fallback_id = (
        "7325648367747338802",
        "7325648367747338803",
        "7325648367747338804",
    )
    (tpl_dir / "sticker_template.json").write_text(
        json.dumps(
            {
                "_reverse_engineering_pending": False,
                "sticker_by_keyword": {"赞": zh_id, "加油": en_id, "默认": fallback_id},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)  # node_11 的模板路径是 CWD 相对的既有契约

    out = inject_sticker(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    stickers = draft["materials"]["stickers"]
    assert len(stickers) == 3
    rid_by_sticker_id = {s["id"]: s["resource_id"] for s in stickers}
    assert set(rid_by_sticker_id.values()) == {zh_id, en_id, fallback_id}
    seg_refs = draft["tracks"][0]["segments"][0].get("extra_material_refs", [])
    assert set(rid_by_sticker_id) <= set(seg_refs)
    assert out["sticker_summary"]["zh"]["matched"] == 3
    assert out["sticker_summary"]["zh"]["written"] == 3
    assert out["sticker_summary"]["zh"]["degraded_placeholder"] == 0
    assert not any("PLACEHOLDER" in e for e in out["error_log"])


def test_inject_sticker_preserves_timerange_zero_deviation(
    tmp_draft_with_texts: Path, real_id_resolver: None
) -> None:
    """贴纸时长应与对应字幕 timerange 偏差为 0(精确复制)。"""
    inject_sticker(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    texts = draft["materials"]["texts"]
    stickers = draft["materials"]["stickers"]

    target_texts = [t for t in texts if "赞" in t["content"] or "加油" in t["content"]]
    for t in target_texts:
        matching = [
            s for s in stickers
            if s["id"] == f"sticker-{t['id']}"
        ]
        assert matching, f"未找到对应贴纸 for {t['content']}"
        assert matching[0]["duration"] == t["target_timerange"]["duration"]


def test_inject_sticker_handles_missing_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解析不出资源(None)时:不创建贴纸 material,节点通过。"""
    monkeypatch.setattr(
        node_11_mod, "resolve_sticker_resource_id", lambda _content: None
    )

    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [{"id": "t1", "content": "赞", "target_timerange": {"start": 0, "duration": 1_000_000}}]},
        "tracks": [{"type": "video", "segments": []}],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft), encoding="utf-8")
    out = inject_sticker(_state(p))
    assert "node_11_inject_sticker_done" in out["status_log"]
    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("stickers")


def test_inject_sticker_idempotent_on_replay(
    tmp_draft_with_texts: Path, real_id_resolver: None
) -> None:
    """K2=B 幂等:重放整份草稿字节级一致,extra_material_refs 不重复追加。"""
    inject_sticker(_state(tmp_draft_with_texts))
    first = tmp_draft_with_texts.read_bytes()

    inject_sticker(_state(tmp_draft_with_texts))
    second = tmp_draft_with_texts.read_bytes()

    assert first == second
    second_draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    seg = second_draft["tracks"][0]["segments"][0]
    refs = seg.get("extra_material_refs", [])
    assert len(refs) == len(set(refs))


def test_inject_sticker_cleans_stale_after_text_removed(
    tmp_draft_with_texts: Path, real_id_resolver: None
) -> None:
    """字幕被删后重放:其贴纸素材与引用被清理。"""
    inject_sticker(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["texts"] = [t for t in draft["materials"]["texts"] if t["id"] != "t2"]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_sticker(_state(tmp_draft_with_texts))
    draft2 = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    sticker_ids = {s["id"] for s in draft2["materials"]["stickers"]}
    assert "sticker-t2" not in sticker_ids
    assert "sticker-t1" in sticker_ids
    refs = draft2["tracks"][0]["segments"][0].get("extra_material_refs", [])
    assert "sticker-t2" not in refs


def test_inject_sticker_applies_to_en_draft_when_present(
    tmp_draft_with_texts: Path, tmp_path: Path, real_id_resolver: None
) -> None:
    """K2=B:英文分支草稿存在时同样注入贴纸。"""
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir(parents=True)
    en_file = en_dir / "draft_content.json"
    en_file.write_text(tmp_draft_with_texts.read_text(encoding="utf-8"), encoding="utf-8")

    state = {"draft_path": str(tmp_draft_with_texts),
             "draft_dir_en_branch": str(en_dir), "status_log": [], "error_log": []}
    out = inject_sticker(state)

    zh = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    en = json.loads(en_file.read_text(encoding="utf-8"))
    assert len(en["materials"]["stickers"]) == len(zh["materials"]["stickers"]) > 0
    assert "node_11_inject_sticker_done" in out["status_log"]
    assert "node_11_inject_sticker_en_done" in out["status_log"]


def test_inject_sticker_degrades_when_en_draft_missing(
    tmp_draft_with_texts: Path, tmp_path: Path
) -> None:
    """K2=B 降级:英文草稿不存在 → 只处理中文,不抛异常。"""
    state = {"draft_path": str(tmp_draft_with_texts),
             "draft_dir_en_branch": str(tmp_path / "nope"),
             "status_log": [], "error_log": []}
    out = inject_sticker(state)

    assert "node_11_inject_sticker_done" in out["status_log"]
    assert "node_11_inject_sticker_en_done" not in out["status_log"]
    assert any("英文草稿不存在" in e for e in out["error_log"])


def test_inject_sticker_skips_hook_title_texts(
    tmp_draft_with_texts: Path, real_id_resolver
) -> None:
    """node_09b 的开场大字标题(hook-title- 前缀)不被贴贴纸(第四期计划 §3.2 对位表 #8)。"""
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["texts"].append(
        {
            "id": "hook-title-1",           # 开场标题 id 前缀
            "content": "真棒,给你点赞",      # 内容命中关键词,若无前缀过滤必被贴
            "target_timerange": {"start": 0, "duration": 3_000_000},
        }
    )
    tmp_draft_with_texts.write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    inject_sticker(_state(tmp_draft_with_texts))

    got = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    sticker_ids = [s["id"] for s in got["materials"]["stickers"]]
    assert sticker_ids, "普通字幕仍应产出贴纸"
    assert all(not sid.startswith("sticker-hook-title-") for sid in sticker_ids)

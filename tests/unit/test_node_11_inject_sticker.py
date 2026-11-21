"""节点 11 贴纸单测(2026-11-22 SDK 链转正裁剪版)。

保留意图:占位 fail-closed + 回填指引、关键词匹配计数、贴纸时长=字幕时长
零偏差、幂等重放、双语两份草稿、en 缺失降级、hook-title 排除。
删除:legacy 原生 materials.stickers 写入/视频段 refs 绑定/旧产物清理断言
(随六段 legacy 链退役;独立 sticker 轨由 SDK 编译器落,见
test_jianying_sdk_phase6 编译用例)。node_11 现为「spec 发射 + SDK 编译汇合」;
本文件用最小草稿(编译必被 dangling material 阻断)隔离验证发射层,
编译链路行为由 test_jianying_sdk_phase7 锁定。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.node_11_inject_sticker as node_11_mod
from nodes.node_11_inject_sticker import inject_sticker


_REAL_ID = "7325648367747338802"  # 19 位真实形态(模拟已回填)


def _tmp_spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "draft_content.json").write_text(
        json.dumps({
            "canvas_config": {"width": 1080, "height": 1920},
            "tracks": [{"type": "video", "segments": [
                {"id": "s1", "target_timerange": {"start": 0, "duration": 6_000_000}},
            ]}],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return tmp_path


def _seed_spec_subtitles(tmp_path: Path, rows: list[tuple[str, str]]) -> None:
    from jianying_sdk.spec import SubtitleSpec
    from nodes._draft_build_spec import replace_spec_sections

    replace_spec_sections(tmp_path, subtitles=[
        SubtitleSpec(
            subtitle_id=sid, text=text,
            start_us=i * 2_000_000, end_us=(i + 1) * 2_000_000, language="zh",
        )
        for i, (sid, text) in enumerate(rows)
    ])


def _default_rows() -> list[tuple[str, str]]:
    return [
        ("t1", "真棒,给你点赞"),    # 命中关键词 "赞"
        ("t2", "继续努力加油"),    # 命中关键词 "加油"
        ("t3", "没有匹配的关键词"),  # 不命中 → "默认"兜底
    ]


def _state(draft_dir: Path) -> dict:
    return {"draft_path": str(draft_dir / "draft_content.json"), "status_log": [], "error_log": []}


def _spec_stickers(draft_dir: Path) -> list:
    from nodes._draft_build_spec import load_build_spec

    return list(load_build_spec(draft_dir).stickers)


@pytest.fixture
def real_id_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """把解析结果替换成 19 位真实形态 ID(模拟操作员完成实机回填)。"""
    monkeypatch.setattr(
        node_11_mod, "resolve_sticker_resource_id", lambda _content: _REAL_ID
    )


def test_inject_sticker_placeholder_fail_closed(tmp_path: Path) -> None:
    """模板仍是 PLACEHOLDER_*(现状):不进 spec,degraded 计数 + 回填指引。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    out = inject_sticker(_state(d))

    assert _spec_stickers(d) == []
    # 匹配 3 条:"赞"/"加油" 命中 + 第 3 条走"默认"兜底(Week 3 既有行为)
    assert out["sticker_summary"]["zh"]["matched"] == 3
    assert out["sticker_summary"]["zh"]["degraded_placeholder"] == 3
    assert out["sticker_summary"]["zh"]["written"] == 0
    assert any("PLACEHOLDER" in e and "回填" in e for e in out["error_log"])
    assert "node_11_inject_sticker_done" in out["status_log"]


def test_inject_sticker_matches_keywords(tmp_path: Path, real_id_resolver: None) -> None:
    """(已回填)字幕文本命中关键词 → spec 记录贴纸计划(SDK 编译落独立轨)。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    out = inject_sticker(_state(d))

    stickers = _spec_stickers(d)
    assert len(stickers) >= 2
    for sticker in stickers:
        assert sticker.resource_id == _REAL_ID
        assert sticker.duration_us > 0
    assert out["sticker_summary"]["zh"]["written"] >= 2
    assert "node_11_inject_sticker_done" in out["status_log"]


def test_inject_sticker_end_to_end_after_real_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回填完成后端到端:真实模板文件(pending=false + 19 位 ID)→ 真实解析 → 正常发射。

    与 ``real_id_resolver`` 的桩不同,本用例不动 resolver —— 验证
    ``resolve_sticker_resource_id`` 读回填模板 → node_11 发射 spec 的完整链路
    (console-ef8504d3a62c 的反例:同一条链路,占位模板走到 fail-closed)。
    """
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
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

    out = inject_sticker(_state(d))

    stickers = _spec_stickers(d)
    assert len(stickers) == 3
    rid_by_sticker_id = {s.sticker_id: s.resource_id for s in stickers}
    assert set(rid_by_sticker_id.values()) == {zh_id, en_id, fallback_id}
    assert out["sticker_summary"]["zh"]["matched"] == 3
    assert out["sticker_summary"]["zh"]["written"] == 3
    assert out["sticker_summary"]["zh"]["degraded_placeholder"] == 0
    assert not any("PLACEHOLDER" in e for e in out["error_log"])


def test_inject_sticker_preserves_timerange_zero_deviation(
    tmp_path: Path, real_id_resolver: None
) -> None:
    """贴纸时长应与对应字幕 spec 时间偏差为 0(精确复制)。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    inject_sticker(_state(d))

    from nodes._draft_build_spec import load_build_spec

    subs = {s.subtitle_id: s for s in load_build_spec(d).subtitles}
    for sticker in _spec_stickers(d):
        sub = subs[sticker.source_subtitle_id]
        assert sticker.duration_us == sub.end_us - sub.start_us


def test_inject_sticker_handles_missing_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解析不出资源(None)时:不进 spec,节点通过。"""
    monkeypatch.setattr(
        node_11_mod, "resolve_sticker_resource_id", lambda _content: None
    )
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, [("t1", "赞")])
    out = inject_sticker(_state(d))
    assert "node_11_inject_sticker_done" in out["status_log"]
    assert _spec_stickers(d) == []


def test_inject_sticker_idempotent_on_replay(
    tmp_path: Path, real_id_resolver: None
) -> None:
    """K2=B 幂等:重放 spec 与草稿字节级一致,无重复条目。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    inject_sticker(_state(d))
    draft_file = d / "draft_content.json"
    first = draft_file.read_bytes()
    first_spec = json.dumps(
        [s.model_dump() for s in _spec_stickers(d)], ensure_ascii=False, sort_keys=True
    )

    inject_sticker(_state(d))
    second = draft_file.read_bytes()
    second_spec = json.dumps(
        [s.model_dump() for s in _spec_stickers(d)], ensure_ascii=False, sort_keys=True
    )

    assert first == second
    ids = [s.sticker_id for s in _spec_stickers(d)]
    assert len(ids) == len(set(ids))
    assert first_spec == second_spec


def test_inject_sticker_stale_entry_cleared_after_text_removed(
    tmp_path: Path, real_id_resolver: None
) -> None:
    """字幕被删后重放:其贴纸计划条目被清(spec 段整体替换=幂等清理)。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    inject_sticker(_state(d))
    assert {s.sticker_id for s in _spec_stickers(d)} >= {"sticker-t1", "sticker-t2"}

    # 模拟字幕 t2 消失(node_08 重发射 spec 时不再含 t2)
    _seed_spec_subtitles(d, [r for r in _default_rows() if r[0] != "t2"])
    inject_sticker(_state(d))
    ids = {s.sticker_id for s in _spec_stickers(d)}
    assert "sticker-t2" not in ids
    assert "sticker-t1" in ids


def test_inject_sticker_applies_to_en_draft_when_present(
    tmp_path: Path, real_id_resolver: None
) -> None:
    """K2=B:英文分支草稿存在时同样发射 spec。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir(parents=True)
    (en_dir / "draft_content.json").write_text(
        (d / "draft_content.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _seed_spec_subtitles(en_dir, [("e1", "great, like it")])

    state = {"draft_path": str(d / "draft_content.json"),
             "draft_dir_en_branch": str(en_dir), "status_log": [], "error_log": []}
    out = inject_sticker(state)

    assert len(_spec_stickers(d)) > 0
    assert len(_spec_stickers(en_dir)) > 0
    assert "node_11_inject_sticker_done" in out["status_log"]
    assert "node_11_inject_sticker_en_done" in out["status_log"]


def test_inject_sticker_degrades_when_en_draft_missing(
    tmp_path: Path, real_id_resolver: None
) -> None:
    """K2=B 降级:英文草稿不存在 → 只处理中文,不抛异常。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, _default_rows())
    state = {"draft_path": str(d / "draft_content.json"),
             "draft_dir_en_branch": str(tmp_path / "nope"),
             "status_log": [], "error_log": []}
    out = inject_sticker(state)

    assert "node_11_inject_sticker_done" in out["status_log"]
    assert "node_11_inject_sticker_en_done" not in out["status_log"]
    assert any("英文草稿不存在" in e for e in out["error_log"])


def test_inject_sticker_skips_hook_title_texts(
    tmp_path: Path, real_id_resolver: None
) -> None:
    """开场大字标题(hook-title- 前缀)不被贴贴纸(第四期计划 §3.2 对位表 #8;防御性过滤)。"""
    d = _tmp_spec_dir(tmp_path)
    _seed_spec_subtitles(d, [
        ("t1", "真棒,给你点赞"),
        ("hook-title-1", "真棒,给你点赞"),  # 内容命中关键词,若无前缀过滤必被贴
    ])
    inject_sticker(_state(d))

    sticker_ids = [s.sticker_id for s in _spec_stickers(d)]
    assert sticker_ids, "普通字幕仍应产出贴纸"
    assert all(not sid.startswith("sticker-hook-title-") for sid in sticker_ids)

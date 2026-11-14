"""节点 5 单测(附件 3.2 节要点):加密检测 → 策略 → 构造 → 原子写入。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from draft_ops.encryption_detector import DraftStatus
from nodes.node_05_generate_draft import (
    _build_draft_content,
    _init_empty_tracks,
    _shot_plan_to_materials,
    generate_initial_jianying_draft,
)


def test_encryption_detected_early_exits(tmp_path: Path) -> None:
    """加密状态 → 早退,draft_path=None,error_log 有记录。"""

    def fake_encrypt(_draft_dir):
        return DraftStatus.ENCRYPTED

    state = {
        "shot_plan": {"shots": [{"id": "s1", "video_ref": "v1"}]},
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }
    out = generate_initial_jianying_draft(
        state, tmp_path, encrypt_detector=fake_encrypt
    )
    assert out["draft_path"] is None
    assert out["draft_encryption_status"] == "encrypted"
    assert any("检测到已加密草稿" in e for e in out["error_log"])


def test_happy_path_produces_draft_and_uses_atomic_writer(tmp_path: Path) -> None:
    """正常路径:产出 draft_path,顶层含 canvas_config/materials/tracks,写走原子写入。"""

    def fake_encrypt(_draft_dir):
        return DraftStatus.PLAINTEXT

    state = {
        "shot_plan": {
            "shots": [
                {"id": "s1", "video_ref": "v1", "start_s": 0.0, "end_s": 5.0},
                {"id": "s2", "video_ref": "v1", "start_s": 5.0, "end_s": 10.0},
            ]
        },
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }

    writer_called = []

    def fake_writer(draft_dir, content):
        # Week 5:writer 签名变更为 (draft_dir, content),走 safe_write_draft 路径
        writer_called.append((draft_dir, content))
        from draft_ops.atomic_writer import safe_write_draft

        # proc_checker 桩:本用例验证原子双写,不验证写入互斥(那是
        # test_safe_write_guard 的职责);剪映开着跑单测也不应环境性失败
        safe_write_draft(draft_dir, content, proc_checker=lambda: False)

    out = generate_initial_jianying_draft(
        state, tmp_path, encrypt_detector=fake_encrypt, writer=fake_writer
    )

    assert len(writer_called) == 1, "应当调用一次原子写入"
    draft_path = Path(out["draft_path"])
    assert draft_path.exists()
    assert draft_path.name == "draft_content.json"
    # Week 5:safe_write_draft 双写 → draft_info.json 也应存在
    assert (tmp_path / "draft_info.json").exists()
    loaded = json.loads(draft_path.read_text(encoding="utf-8"))
    assert set(loaded.keys()) >= {"canvas_config", "materials", "tracks"}
    assert loaded["canvas_config"]["width"] == 1080
    assert loaded["canvas_config"]["height"] == 1920
    assert len(loaded["materials"]["videos"]) == 2
    # 2026-11-07 导入修复:staging 出生即原生 schema + 私有元数据 sidecar
    assert "new_version" in loaded, "storyline 草稿应转换为剪映原生 schema"
    assert (tmp_path / "autovideo_storyline_source.json").exists()
    assert out["draft_encryption_status"] == "plaintext"
    assert out["draft_version_strategy"] == "strategy_a_version_lock"


def test_writer_called_not_bare_open(tmp_path: Path) -> None:
    """禁止节点 5 直接 open().write() —— 必须走注入的 writer(单测层断言)。"""
    from draft_ops.atomic_writer import safe_write_draft

    state = {
        "shot_plan": {"shots": [{"id": "s1", "video_ref": "v1"}]},
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
    }

    captured = {}

    def fake_writer(draft_dir, content):
        # Week 5:新签名 (draft_dir, content)
        captured["called"] = True
        captured["draft_dir"] = draft_dir
        captured["content_keys"] = set(content.keys())
        # proc_checker 桩:同上,写入互斥由 test_safe_write_guard 单独覆盖
        safe_write_draft(draft_dir, content, proc_checker=lambda: False)

    generate_initial_jianying_draft(
        state,
        tmp_path,
        encrypt_detector=lambda d: DraftStatus.PLAINTEXT,
        writer=fake_writer,
    )
    assert captured.get("called") is True
    # Week 5:writer 收到的是 draft_dir(tmp_path),不是 draft_file
    assert captured.get("draft_dir") == tmp_path
    assert captured.get("content_keys") >= {"canvas_config", "materials", "tracks"}


# ---------------------------------------------------------------------------
# 辅助函数小测
# ---------------------------------------------------------------------------
def test_shot_plan_to_materials_basic() -> None:
    mats = _shot_plan_to_materials(
        {"shots": [{"id": "s1", "video_ref": "v1", "start_s": 0.0, "end_s": 1.0}]},
        "/tmp/v.mp4",
    )
    assert len(mats) == 1
    assert mats[0]["id"] == "video-1"
    assert mats[0]["path"] == "/tmp/v.mp4"
    assert mats[0]["shot_id"] == "s1"


def test_shot_plan_to_materials_empty() -> None:
    assert _shot_plan_to_materials({}, "/tmp/v.mp4") == []
    assert _shot_plan_to_materials({"shots": []}, "/tmp/v.mp4") == []


def test_init_empty_tracks() -> None:
    assert _init_empty_tracks() == []


def test_build_draft_content_top_level_shape() -> None:
    content = _build_draft_content(
        {"shots": [{"id": "s1", "video_ref": "v1"}]},
        "/tmp/v.mp4",
    )
    assert set(content.keys()) == {"canvas_config", "materials", "tracks"}
    assert "videos" in content["materials"]
    assert isinstance(content["tracks"], list)


# ---------------------------------------------------------------------------
# 20261114 阶段5:云音乐自动铺轨接线(修订执行计划 §阶段5)
# node_05 内对 helper/曲库是调用点延迟 import —— 按源模块打桩即可命中。
# ---------------------------------------------------------------------------
from copy import deepcopy  # noqa: E402

from nodes._auto_bgm import AUTO_BGM_TRACK_NAME  # noqa: E402

_GOLDEN_MUSIC_ID = "7553871402870491155"


def _fake_library_loader(entries: list[dict]):
    """load_jianying_music_library 替身:返回固定曲库(外层契约三键)。"""

    def _load(*, path=None, refresh=False):
        return {
            "library": [dict(e) for e in entries],
            "default_match": {},
            "matching": {},
        }

    return _load


def _bgm_state(selection: dict | None) -> dict:
    return {
        "shot_plan": {
            "shots": [
                {"id": "s1", "video_ref": "v1", "start_s": 0.0, "end_s": 5.0},
                {"id": "s2", "video_ref": "v1", "start_s": 5.0, "end_s": 10.0},
            ]
        },
        "video_input_path": "/tmp/30s.mp4",
        "error_log": [],
        "storyline_bgm_selection": selection,
    }


def _plain_encrypt(_draft_dir):
    return DraftStatus.PLAINTEXT


def _fake_writer_factory(called: list):
    def fake_writer(draft_dir, content):
        called.append((draft_dir, content))
        from draft_ops.atomic_writer import safe_write_draft

        safe_write_draft(draft_dir, content, proc_checker=lambda: False)

    return fake_writer


def test_auto_bgm_injected_before_writer(tmp_path: Path, monkeypatch) -> None:
    """云音乐 selection → helper 在 writer 前调用;writer 收到的草稿含 BGM 轨。"""
    calls: dict = {}

    def fake_apply(*, draft, selection, library_entry, job_id):
        calls["draft_native"] = "new_version" in draft
        calls["selection_music_id"] = selection.get("music_id")
        calls["entry_music_id"] = (library_entry or {}).get("music_id")
        calls["job_id"] = job_id
        out = deepcopy(draft)
        out.setdefault("tracks", []).append(
            {
                "type": "audio",
                "name": AUTO_BGM_TRACK_NAME,
                "segments": [{"id": "seg-bgm-test", "marker": "auto-bgm"}],
            }
        )
        return {
            "applied": True,
            "draft": out,
            "music_id": selection["music_id"],
            "music_name": "旅途的风景",
            "track_id": "track-x",
            "status_notes": [f"自动铺轨: 旅途的风景({_GOLDEN_MUSIC_ID})"],
            "errors": [],
        }

    monkeypatch.setattr("nodes._auto_bgm.apply_auto_bgm_track", fake_apply)
    monkeypatch.setattr(
        "jy_common.jianying_music_library.load_jianying_music_library",
        _fake_library_loader([{"music_id": _GOLDEN_MUSIC_ID, "name": "旅途的风景"}]),
    )

    called: list = []
    out = generate_initial_jianying_draft(
        _bgm_state({"music_id": _GOLDEN_MUSIC_ID, "method": "llm"}),
        tmp_path,
        encrypt_detector=_plain_encrypt,
        writer=_fake_writer_factory(called),
    )

    # helper 收到:原生草稿(已过 storyline_to_native)/ selection / 曲库条目
    assert calls["draft_native"] is True, "helper 应收到原生格式草稿"
    assert calls["selection_music_id"] == _GOLDEN_MUSIC_ID
    assert calls["entry_music_id"] == _GOLDEN_MUSIC_ID
    # BGM 落在写盘前:writer 只被调一次,写出的就是含 BGM 轨的草稿
    assert len(called) == 1
    loaded = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    assert any(t.get("name") == AUTO_BGM_TRACK_NAME for t in loaded.get("tracks") or [])
    assert any("自动铺轨" in s for s in out["status_log"])
    assert out["error_log"] == []


def test_auto_bgm_disabled_env_skips(tmp_path: Path, monkeypatch) -> None:
    """JY_AUTO_BGM=0(紧急回退)→ 完全不调 helper,草稿照常生成(关卡②兜底)。"""
    import config

    monkeypatch.setattr(config, "JY_AUTO_BGM", False)

    def _must_not_call(**_kwargs):
        raise AssertionError("开关关闭时不应调用自动铺轨 helper")

    monkeypatch.setattr("nodes._auto_bgm.apply_auto_bgm_track", _must_not_call)
    monkeypatch.setattr(
        "jy_common.jianying_music_library.load_jianying_music_library",
        _fake_library_loader([]),
    )

    called: list = []
    out = generate_initial_jianying_draft(
        _bgm_state({"music_id": _GOLDEN_MUSIC_ID}),
        tmp_path,
        encrypt_detector=_plain_encrypt,
        writer=_fake_writer_factory(called),
    )
    assert len(called) == 1
    loaded = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    assert not any(
        t.get("name") == AUTO_BGM_TRACK_NAME for t in loaded.get("tracks") or []
    )
    assert out["error_log"] == []


def test_auto_bgm_legacy_selection_skips(tmp_path: Path, monkeypatch) -> None:
    """旧 selection(无 music_id,YAML 占位形态)→ 行为不变,不触发自动铺轨。"""

    def _must_not_call(**_kwargs):
        raise AssertionError("旧 selection 不应触发自动铺轨")

    monkeypatch.setattr("nodes._auto_bgm.apply_auto_bgm_track", _must_not_call)

    called: list = []
    out = generate_initial_jianying_draft(
        _bgm_state({"bgm_ref": "bgm_calm_lofi", "stub": True}),
        tmp_path,
        encrypt_detector=_plain_encrypt,
        writer=_fake_writer_factory(called),
    )
    assert len(called) == 1
    assert out["error_log"] == []


def test_auto_bgm_conversion_failed_skips(tmp_path: Path, monkeypatch) -> None:
    """原生转换失败 → 不注入(禁混写 schema);转换失败原因照旧记录,写盘不阻断。"""

    def _boom(_draft):
        raise RuntimeError("转换炸了")

    monkeypatch.setattr("nodes.node_05_generate_draft.storyline_to_native", _boom)

    def _must_not_call(**_kwargs):
        raise AssertionError("转换失败分支不应注入 BGM")

    monkeypatch.setattr("nodes._auto_bgm.apply_auto_bgm_track", _must_not_call)

    called: list = []
    out = generate_initial_jianying_draft(
        _bgm_state({"music_id": _GOLDEN_MUSIC_ID}),
        tmp_path,
        encrypt_detector=_plain_encrypt,
        writer=_fake_writer_factory(called),
    )
    assert len(called) == 1, "转换失败仍应写盘(既有降级策略)"
    assert any("原生草稿转换失败" in e for e in out["error_log"])


def test_auto_bgm_helper_failure_continues(tmp_path: Path, monkeypatch) -> None:
    """helper 报错(曲库无此曲/缓存失效)→ error_log 留痕、草稿照常写出、流程继续。"""
    monkeypatch.setattr(
        "jy_common.jianying_music_library.load_jianying_music_library",
        _fake_library_loader([]),  # 曲库查无此 music_id → entry=None
    )

    def fake_apply(*, draft, selection, library_entry, job_id):
        assert library_entry is None, "曲库查无此曲时应传 None(helper 内 fail-closed)"
        return {
            "applied": False,
            "draft": None,
            "music_id": selection.get("music_id", ""),
            "music_name": "",
            "track_id": "",
            "status_notes": [],
            "errors": [
                f"曲库条目缺 raw_material(music_id={selection.get('music_id')}),"
                "重新 harvest 后重试"
            ],
        }

    monkeypatch.setattr("nodes._auto_bgm.apply_auto_bgm_track", fake_apply)

    called: list = []
    out = generate_initial_jianying_draft(
        _bgm_state({"music_id": _GOLDEN_MUSIC_ID}),
        tmp_path,
        encrypt_detector=_plain_encrypt,
        writer=_fake_writer_factory(called),
    )
    assert len(called) == 1, "铺轨失败不阻断草稿生成"
    loaded = json.loads(Path(out["draft_path"]).read_text(encoding="utf-8"))
    assert not any(
        t.get("name") == AUTO_BGM_TRACK_NAME for t in loaded.get("tracks") or []
    )
    assert any("曲库条目缺 raw_material" in e for e in out["error_log"])

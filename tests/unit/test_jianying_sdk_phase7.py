"""第10期阶段7 单测:SDK 编译汇合管线 + node_11 唯一编译入口 + 后端开关 + 兼容。

覆盖迁移设计文档 §9 阶段7 验收(结构级):
- ``compile_and_publish_draft``:spec 缺失显式跳过(不建空计划,§阶段7 兼容);
  编译失败/blocked 不覆盖当前有效草稿(§11.1);指纹一致跳过重编译;
- 编译产物经 ``safe_write_draft`` 双写发布(继承快照/校验/审计链);
- ``DRAFT_BUILDER_BACKEND=pyjianyingdraft`` 时 node_11 以当前草稿+全量 spec
  SDK 重建;legacy 后端行为零变化。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_sdk._sdk_loader import ensure_sdk_on_path

ensure_sdk_on_path()

import pyJianYingDraft
from pyJianYingDraft.local_materials import CropSettings
from tests.unit.test_jianying_sdk_phase2 import StubAudioMaterial, StubVideoMaterial


@pytest.fixture(autouse=True)
def _stub_sdk_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    """管线级测试替身:跳过 MediaInfo 探测(生产路径走真探测)。"""
    monkeypatch.setattr(
        pyJianYingDraft, "VideoMaterial",
        lambda path, material_name=None, crop_settings=None: StubVideoMaterial(
            path, crop_settings
        ),
    )
    monkeypatch.setattr(
        pyJianYingDraft, "AudioMaterial",
        lambda path, material_name=None: StubAudioMaterial(path),
    )


@pytest.fixture(autouse=True)
def _hermetic_proc_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    """写入互斥探测封闭化(测试进程不探测真实剪映)。"""
    import draft_ops.atomic_writer as aw

    monkeypatch.setattr(aw, "_default_proc_checker", lambda: False)


def _base_draft() -> dict:
    return {
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "original"},
        "fps": 30.0,
        "duration": 7_000_000,
        "materials": {
            "videos": [
                {"id": "v1", "path": "C:/media/source_1.mp4", "material_name": "clip_0001",
                 "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
                          "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
                          "upper_right_x": 1.0, "upper_right_y": 0.0}},
                {"id": "v2", "path": "C:/media/source_2.mp4", "material_name": "clip_0002",
                 "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
                          "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
                          "upper_right_x": 1.0, "upper_right_y": 0.0}},
            ],
            "audios": [], "speeds": [], "canvases": [],
            "sound_channel_mappings": [], "vocal_separations": [],
            "texts": [], "transitions": [], "video_effects": [], "stickers": [],
            "material_animations": [], "audio_fades": [], "effects": [],
        },
        "tracks": [{
            "type": "video", "id": "tv", "segments": [
                {"id": "s1", "material_id": "v1",
                 "target_timerange": {"start": 0, "duration": 5_000_000},
                 "source_timerange": {"start": 0, "duration": 5_000_000},
                 "speed": 1.0, "volume": 1.0,
                 "extra_material_refs": ["sp1", "cv1", "sc1", "vo1"],
                 "common_keyframes": [], "keyframe_refs": []},
                {"id": "s2", "material_id": "v2",
                 "target_timerange": {"start": 5_000_000, "duration": 2_000_000},
                 "source_timerange": {"start": 2_000_000, "duration": 4_000_000},
                 "speed": 2.0, "volume": 1.0,
                 "extra_material_refs": ["sp2", "cv2", "sc2", "vo2"],
                 "common_keyframes": [], "keyframe_refs": []},
            ],
        }],
    }


def _companion_materials(draft: dict) -> None:
    mats = draft["materials"]
    for i in (1, 2):
        mats["speeds"].append({"id": f"sp{i}", "speed": 1.0, "mode": 0, "type": "speed"})
        mats["canvases"].append({"id": f"cv{i}", "type": "canvas_color"})
        mats["sound_channel_mappings"].append({"id": f"sc{i}", "audio_channel_mapping": 0})
        mats["vocal_separations"].append({"id": f"vo{i}", "type": "vocal_separation"})


def _setup_task(tmp_path: Path) -> Path:
    draft = _base_draft()
    _companion_materials(draft)
    (tmp_path / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _write_spec_with_subtitles(tmp_path: Path) -> None:
    from jianying_sdk.spec import SubtitleSpec
    from nodes._draft_build_spec import replace_spec_sections

    replace_spec_sections(
        tmp_path,
        job_id="sess-p7",
        language="zh",
        subtitles=[SubtitleSpec(
            subtitle_id="text-zh-1", text="你好", start_us=0, end_us=2_000_000,
            language="zh",
            style={"size": 5.0, "bold": True, "color": [1.0, 1.0, 1.0], "align": 1,
                   "border": {"width": 40.0}, "transform_y": -0.8},
            wrap_content="你好",
        )],
    )


# ---------------------------------------------------------------------------
# 管线
# ---------------------------------------------------------------------------
def test_pipeline_compiles_and_publishes(tmp_path: Path) -> None:
    _setup_task(tmp_path)
    _write_spec_with_subtitles(tmp_path)
    from jianying_sdk.pipeline import compile_and_publish_draft

    record = compile_and_publish_draft(tmp_path, tmp_path / "draft_content.json", job_id="sess-p7")
    assert record["status"] == "compiled", record
    assert record["counts"]["video_clips"] == 2
    assert record["counts"]["subtitles"] == 1
    assert record["write"]["ok"] is True

    payload = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    # SDK 重建稿:字幕来自 spec,时间线来自提取
    assert len(payload["materials"]["texts"]) == 1
    video_track = next(t for t in payload["tracks"] if t["type"] == "video")
    assert len(video_track["segments"]) == 2
    # 双写一致
    info = json.loads((tmp_path / "draft_info.json").read_text(encoding="utf-8"))
    assert info == payload
    # spec.compile 回写
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert spec.compile.last_compiled_fingerprint == record["fingerprint"]
    assert spec.compile.last_result["status"] == "compiled"


def test_pipeline_fingerprint_skip(tmp_path: Path) -> None:
    _setup_task(tmp_path)
    _write_spec_with_subtitles(tmp_path)
    from jianying_sdk.pipeline import compile_and_publish_draft

    first = compile_and_publish_draft(tmp_path, tmp_path / "draft_content.json", job_id="s")
    assert first["status"] == "compiled"
    second = compile_and_publish_draft(tmp_path, tmp_path / "draft_content.json", job_id="s")
    assert second["status"] == "skipped_fingerprint"


def test_pipeline_no_spec_explicit_skip(tmp_path: Path) -> None:
    """旧线程兼容(§阶段7):无 spec → 显式跳过,legacy 草稿原样保留。"""
    _setup_task(tmp_path)
    from jianying_sdk.pipeline import compile_and_publish_draft

    record = compile_and_publish_draft(tmp_path, tmp_path / "draft_content.json", job_id="s")
    assert record["status"] == "skipped_no_spec"
    payload = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    assert payload["tracks"][0]["segments"][0]["material_id"] == "v1"  # legacy 原样


def test_pipeline_blocked_features_preserve_draft(tmp_path: Path) -> None:
    """§7.2/§11.1:人工高级特征 → 编译拒绝,当前有效草稿不被覆盖。"""
    _setup_task(tmp_path)
    _write_spec_with_subtitles(tmp_path)
    # 注入人工关键帧(下一轮提取 → blocked)
    draft = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    draft["tracks"][0]["segments"][0]["common_keyframes"] = [{"id": "human-kf"}]
    (tmp_path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    from jianying_sdk.pipeline import compile_and_publish_draft

    record = compile_and_publish_draft(tmp_path, tmp_path / "draft_content.json", job_id="s")
    assert record["status"] == "failed_blocked_features"
    # 草稿原样保留(含人工关键帧)
    payload = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    assert payload["tracks"][0]["segments"][0]["common_keyframes"]
    from nodes._draft_build_spec import load_build_spec

    spec = load_build_spec(tmp_path)
    assert spec.compile.last_compiled_fingerprint is None  # 失败不记指纹


def test_pipeline_corrupt_spec_treated_as_missing(tmp_path: Path) -> None:
    _setup_task(tmp_path)
    (tmp_path / ".autovideo_draft_build_spec.json").write_text("{broken", encoding="utf-8")
    from jianying_sdk.pipeline import compile_and_publish_draft

    record = compile_and_publish_draft(tmp_path, tmp_path / "draft_content.json", job_id="s")
    assert record["status"] == "skipped_no_spec"


# ---------------------------------------------------------------------------
# node_11 后端开关接线
# ---------------------------------------------------------------------------
def test_node_11_sdk_backend_compiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = _base_draft()
    _companion_materials(draft)
    (tmp_path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    _write_spec_with_subtitles(tmp_path)

    monkeypatch.setattr("config.DRAFT_BUILDER_BACKEND", "pyjianyingdraft")
    from nodes.node_11_inject_sticker import inject_sticker

    state = {
        "draft_path": str(tmp_path / "draft_content.json"),
        "session_id": "sess-p7",
        "status_log": [],
        "error_log": [],
    }
    out = inject_sticker(state)
    assert any("node_11_compile_zh:compiled" in tag for tag in out["status_log"])
    payload = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    assert payload["materials"]["texts"]  # SDK 重建稿带 spec 字幕
    assert len(next(t for t in payload["tracks"] if t["type"] == "video")["segments"]) == 2


def test_node_11_legacy_backend_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = _base_draft()
    _companion_materials(draft)
    (tmp_path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    _write_spec_with_subtitles(tmp_path)

    monkeypatch.setattr("config.DRAFT_BUILDER_BACKEND", "legacy")
    from nodes.node_11_inject_sticker import inject_sticker

    state = {
        "draft_path": str(tmp_path / "draft_content.json"),
        "session_id": "sess-p7",
        "status_log": [],
        "error_log": [],
    }
    out = inject_sticker(state)
    assert not any("node_11_compile" in tag for tag in out["status_log"])
    payload = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    # legacy:草稿仍是原生形态(node_11 未做 SDK 重建)
    assert payload["tracks"][0]["segments"][0]["material_id"] == "v1"
    assert payload["materials"]["texts"] == []


def test_node_11_sdk_backend_failure_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 编译管线异常 → node_11 兜底捕获,legacy 草稿保留,节点不崩。"""
    draft = _base_draft()
    _companion_materials(draft)
    (tmp_path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    _write_spec_with_subtitles(tmp_path)

    import jianying_sdk.pipeline as pipeline

    monkeypatch.setattr("config.DRAFT_BUILDER_BACKEND", "pyjianyingdraft")
    monkeypatch.setattr(
        pipeline, "compile_and_publish_draft",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    from nodes.node_11_inject_sticker import inject_sticker

    state = {
        "draft_path": str(tmp_path / "draft_content.json"),
        "session_id": "sess-p7",
        "status_log": [],
        "error_log": [],
    }
    out = inject_sticker(state)
    assert any("node_11_compile_zh:failed" in tag for tag in out["status_log"])
    assert any("SDK 编译失败" in e for e in out["error_log"])
    # legacy 草稿未被 SDK 覆盖
    payload = json.loads((tmp_path / "draft_content.json").read_text(encoding="utf-8"))
    assert payload["tracks"][0]["segments"][0]["material_id"] == "v1"


def test_config_default_backend_is_sdk() -> None:
    """2026-11-22 六效果段灰度开:未设 env 时 config 默认 pyjianyingdraft。

    用子进程隔离 —— config 在模块导入时读 env,进程内 reload 会波及同进程
    其他模块已绑定的属性;env 覆盖回滚路径(DRAFT_BUILDER_BACKEND=legacy)
    由 test_node_11_legacy_backend_untouched 与钉 legacy 的 node_11 单测锁定。
    """
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "DRAFT_BUILDER_BACKEND"}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", "import config; print(config.DRAFT_BUILDER_BACKEND)"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "pyjianyingdraft"

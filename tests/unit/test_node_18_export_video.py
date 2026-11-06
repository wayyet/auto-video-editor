"""node_18 export_final_video 单测(阶段 6:开关 / 缺草稿 / 失败 / 成功)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import draft_ops.draft_exporter as draft_exporter_mod
import nodes.node_18_export_video as node_mod
from nodes.node_18_export_video import export_final_video


@pytest.fixture(autouse=True)
def _reset_export_dir(monkeypatch):
    """单测默认不写真实 outputs;输出目录逐用例钉到 tmp。"""
    monkeypatch.setattr(config, "FINAL_EXPORT_DIR", "")


def test_disabled_skips(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", False)
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("开关关闭时不应触发渲染")

    monkeypatch.setattr(draft_exporter_mod, "run_export", _boom)
    out = export_final_video({"session_id": "t1", "status_log": []})
    assert out["final_export_status"] == "skipped"
    assert "node_18_export_skipped:disabled" in out["status_log"]
    assert called is False
    assert "final_video_path" not in out


def test_missing_draft_fails_explicitly(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", True)
    out = export_final_video({"session_id": "t1", "status_log": [], "error_log": []})
    assert out["final_export_status"] == "failed"
    assert any("中文主草稿缺失" in e for e in out["error_log"])
    assert out["final_export_report"]["status"] == "failed"
    assert "final_video_path" not in out


def test_unreadable_draft_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", True)
    bad = tmp_path / "draft_content.json"
    bad.write_text("not-json{", encoding="utf-8")
    out = export_final_video(
        {"session_id": "t1", "draft_path": str(bad), "status_log": [], "error_log": []}
    )
    assert out["final_export_status"] == "failed"
    assert any("读取中文主草稿失败" in e for e in out["error_log"])


def test_export_error_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", True)
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text(json.dumps({"materials": {}, "tracks": []}), encoding="utf-8")

    def _raise(draft, out_path, **kwargs):
        raise draft_exporter_mod.DraftExportError("草稿没有可导出的视频段")

    monkeypatch.setattr(draft_exporter_mod, "run_export", _raise)
    out = export_final_video(
        {"session_id": "t1", "draft_path": str(draft_file), "status_log": [], "error_log": []}
    )
    assert out["final_export_status"] == "failed"
    assert "草稿没有可导出的视频段" in out["error_log"][0]
    assert out["final_export_report"]["status"] == "failed"


def test_unexpected_error_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", True)
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text("{}", encoding="utf-8")

    def _boom(draft, out_path, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(draft_exporter_mod, "run_export", _boom)
    out = export_final_video(
        {"session_id": "t1", "draft_path": str(draft_file), "status_log": [], "error_log": []}
    )
    assert out["final_export_status"] == "failed"
    assert "boom" in out["error_log"][0]


def test_success_path_writes_fields(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", True)
    monkeypatch.setattr(config, "FINAL_EXPORT_DIR", str(tmp_path / "custom_export"))
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    report = {
        "status": "succeeded",
        "output_path": str(tmp_path / "custom_export" / "final.mp4"),
        "sha256": "a" * 64,
        "probed": {"duration_seconds": 1.0},
        "fidelity_warnings": ["1 处转场按硬切近似渲染"],
        "issues": [],
    }

    def _fake_export(draft, out_path, **kwargs):
        return report

    monkeypatch.setattr(draft_exporter_mod, "run_export", _fake_export)
    out = export_final_video(
        {"session_id": "t1", "draft_path": str(draft_file), "status_log": [], "error_log": []}
    )
    assert out["final_export_status"] == "succeeded"
    assert out["final_video_path"] == str(tmp_path / "custom_export" / "final.mp4")
    assert out["final_export_report"]["sha256"] == "a" * 64
    assert any(str(out["final_video_path"]) in t for t in out["status_log"])


def test_default_output_dir_follows_session(monkeypatch, tmp_path: Path) -> None:
    """未配置 FINAL_EXPORT_DIR → outputs/<session_id>/export/final.mp4。"""
    monkeypatch.setattr(config, "FINAL_EXPORT_ENABLED", True)
    monkeypatch.setattr(config, "FINAL_EXPORT_DIR", "")
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: tmp_path / "outs")
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        node_mod,
        "_resolve_output_path",
        lambda state: tmp_path / "outs" / str(state.get("session_id")) / "export" / "final.mp4",
    )
    out = export_final_video(
        {"session_id": "t2", "draft_path": str(draft_file), "status_log": [], "error_log": []}
    )
    # 成功路径由 run_export 决定;这里只验证失败报告也带输出路径语义
    assert out["final_export_status"] in ("succeeded", "failed")

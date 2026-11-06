"""阶段 2(人工关卡与剪映草稿导入)单测:关卡展示载荷 + gates/open-draft-dir API。

- ``web.human_gate_payload.build_gate_view``:①/②/③ 三种 payload 形态 + 未知兜底;
- ``GET  /api/console/runs/{tid}/gates``:载荷下发 / checkpoint 不可达 503;
- ``POST /api/console/runs/{tid}/open-draft-dir``:路径只取自 state、白名单根校验、
  zh/en 分支、目录缺失 404、越界 400。

aget_state 接缝(``_fetch_gate_data_sync``)在 API 测试里整体 monkeypatch,
不连真实 PG。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.api_console as api_console  # noqa: E402
from monitor_fastapi import app  # noqa: E402
from web.human_gate_payload import build_gate_views  # noqa: E402

client = TestClient(app)

TID = "console-ab12cd34ef56"


# ---------------------------------------------------------------------------
# human_gate_payload
# ---------------------------------------------------------------------------
class TestBuildGateView:
    def test_gate1_uses_payload_draft_path(self) -> None:
        interrupts = [{
            "id": "i-1", "checkpoint": "①", "legacy_id": "checkpoint1_reorder",
            "step": 6, "instructions": "请在剪映客户端手动调整分镜顺序,完成后确认继续",
            "payload": {"checkpoint": "①", "legacy_id": "checkpoint1_reorder", "step": 6,
                        "draft_path": "X:/t/drafts/draft_content.json",
                        "instructions": "请在剪映客户端手动调整分镜顺序,完成后确认继续"},
        }]
        gates = build_gate_views(interrupts, {"draft_path": "X:/t/drafts/draft_content.json"})
        assert len(gates) == 1
        g = gates[0]
        assert g["checkpoint"] == "①"
        assert g["node"] == "node_06_human_reorder"
        assert "分镜顺序" in g["title"]
        assert g["draft_paths"] == ["X:/t/drafts/draft_content.json"]
        assert g["draft_dirs"] == [str(Path("X:/t/drafts"))]
        assert not g["issues"]
        assert g["instructions"].startswith("请在剪映客户端")

    def test_gate2_hint_single_bgm(self) -> None:
        interrupts = [{
            "id": "i-2", "checkpoint": "②", "legacy_id": "checkpoint2_add_bgm",
            "step": 12, "instructions": "请从剪映 VIP 音乐库选取 BGM 并拖入音轨,完成后确认继续",
            "payload": {"checkpoint": "②", "draft_path": "X:/t/drafts/draft_content.json"},
        }]
        g = build_gate_views(interrupts, {})[0]
        assert g["node"] == "node_12_human_add_bgm"
        assert "一次" in g["action_hint"] and "镜像" in g["action_hint"]

    def test_gate3_issues_and_preview_from_state(self) -> None:
        interrupts = [{
            "id": "i-3", "checkpoint": "③", "legacy_id": "checkpoint3_layout_review",
            "step": 16, "instructions": None,
            "payload": {"checkpoint": "③", "legacy_id": "checkpoint3_layout_review",
                        "step": 16, "reason": "英文字幕排版异常",
                        "issues": [{"index": 0, "text": "HELLO", "width": 2000, "reason": "超宽"}],
                        "draft_dir": "X:/t/en_branch"},
        }]
        values = {
            "draft_dir_en_branch": "X:/t/en_branch",
            "preview_video_path": "X:/t/preview.mp4",
            "subtitle_qc_evidence_frames": ["X:/t/f1.jpg", "X:/t/f2.jpg"],
        }
        g = build_gate_views(interrupts, values)[0]
        assert g["node"] == "node_checkpoint3_layout_review"
        # ③ 没有 instructions 键 → reason 兜底
        assert g["instructions"] == "英文字幕排版异常"
        assert g["issues"] == [{"index": 0, "text": "HELLO", "width": 2000, "reason": "超宽"}]
        assert g["draft_dirs"] == [str(Path("X:/t/en_branch"))]
        assert g["preview_video_path"] == "X:/t/preview.mp4"
        assert g["evidence_frames"] == ["X:/t/f1.jpg", "X:/t/f2.jpg"]

    def test_unknown_checkpoint_falls_back(self) -> None:
        interrupts = [{"id": "i-x", "checkpoint": "⑨", "legacy_id": None, "step": 99,
                       "instructions": None, "payload": {}}]
        g = build_gate_views(interrupts, {"draft_path": "X:/d/draft_content.json"})[0]
        assert g["title"] == "人工关卡"
        assert g["draft_paths"] == ["X:/d/draft_content.json"], "state 兜底"

    def test_order_preserved_for_multi_gate(self) -> None:
        interrupts = [
            {"id": "a", "checkpoint": "①", "legacy_id": "l1", "step": 6,
             "instructions": "x", "payload": {}},
            {"id": "b", "checkpoint": "②", "legacy_id": "l2", "step": 12,
             "instructions": "y", "payload": {}},
        ]
        gates = build_gate_views(interrupts, {})
        assert [g["checkpoint"] for g in gates] == ["①", "②"]


# ---------------------------------------------------------------------------
# GET /gates
# ---------------------------------------------------------------------------
class TestGatesEndpoint:
    def test_gates_ok(self, monkeypatch):
        sample = [{"checkpoint": "①", "node": "node_06_human_reorder", "title": "① 分镜顺序调整",
                   "draft_paths": ["X:/d"], "draft_dirs": ["X:/d"], "issues": []}]
        monkeypatch.setattr(api_console, "_fetch_gate_views_sync", lambda tid: sample)
        resp = client.get(f"/api/console/runs/{TID}/gates")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True and data["gate_count"] == 1
        assert data["gates"][0]["checkpoint"] == "①"
        assert "不会替你完成" in data["common_note"]

    def test_gates_invalid_thread_id_400(self, monkeypatch):
        resp = client.get("/api/console/runs/console-zzzzzzzzzzzz/gates")
        assert resp.status_code == 400

    def test_gates_checkpoint_unreachable_503(self, monkeypatch):
        def boom(tid):
            raise RuntimeError("PG down")
        monkeypatch.setattr(api_console, "_fetch_gate_views_sync", boom)
        resp = client.get(f"/api/console/runs/{TID}/gates")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /open-draft-dir
# ---------------------------------------------------------------------------
class TestOpenDraftDir:
    @pytest.fixture()
    def gate_env(self, tmp_path, monkeypatch):
        """state 路径全部指向 tmp_path 内;开放根 = outputs + jy_root。"""
        zh_dir = tmp_path / "drafts"
        zh_dir.mkdir()
        (zh_dir / "draft_content.json").write_text("{}", encoding="utf-8")
        values = {
            "draft_path": str(zh_dir / "draft_content.json"),
            "draft_dir_en_branch": None,
        }
        monkeypatch.setattr(api_console, "_fetch_gate_data_sync", lambda tid: ([], values))
        monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
        monkeypatch.setattr(config, "JIANYING_DRAFT_ROOT", tmp_path / "jy_root")
        opened = []
        if hasattr(os, "startfile"):
            monkeypatch.setattr(os, "startfile", lambda p: opened.append(str(p)))
        else:  # pragma: no cover — POSIX CI
            monkeypatch.setattr(
                "subprocess.Popen", lambda argv, **k: opened.append(argv[-1])
            )
        return {"tmp": tmp_path, "values": values, "opened": opened, "zh_dir": zh_dir}

    def test_open_zh_uses_state_path(self, gate_env):
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "zh"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["opened"] == str(gate_env["zh_dir"])
        assert len(gate_env["opened"]) == 1

    def test_open_en_not_created_409(self, gate_env):
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "en"})
        assert resp.status_code == 409

    def test_open_en_ok(self, gate_env):
        en_dir = gate_env["tmp"] / "en_branch_x"
        en_dir.mkdir()
        gate_env["values"]["draft_dir_en_branch"] = str(en_dir)
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "en"})
        assert resp.status_code == 200
        assert resp.json()["opened"] == str(en_dir)

    def test_open_missing_dir_404(self, gate_env):
        gate_env["values"]["draft_path"] = str(gate_env["tmp"] / "gone" / "draft_content.json")
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "zh"})
        assert resp.status_code == 404

    def test_open_outside_allowed_roots_400(self, gate_env, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside") / "evil"
        outside.mkdir()
        gate_env["values"]["draft_path"] = str(outside / "draft_content.json")
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "zh"})
        assert resp.status_code == 400

    def test_open_without_origin_needs_no_csrf(self, gate_env):
        """无 Origin 的本机脚本豁免 CSRF(与 cancel/resume 同纪律)。"""
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "zh"})
        assert resp.status_code == 200

    def test_open_unreachable_503(self, gate_env, monkeypatch):
        def boom(tid, draft):
            raise RuntimeError("PG down")
        monkeypatch.setattr(api_console, "_fetch_gate_data_sync", boom)
        resp = client.post(f"/api/console/runs/{TID}/open-draft-dir", json={"draft": "zh"})
        assert resp.status_code == 503

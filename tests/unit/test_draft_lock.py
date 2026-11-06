"""阶段 3(人工关卡与剪映草稿导入)单测:draft_ops/draft_lock.py。

覆盖恢复前校验(存在/JSON/mtime/占用)、审计哈希、恢复总门禁
(剪映未关闭拒绝 / 放行开关 / 通过返回 SHA-256)。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import config
from draft_ops import draft_lock
from draft_ops.draft_lock import (
    assert_resume_ready,
    draft_dir_locked,
    hash_draft_files,
    validate_draft_for_resume,
)


def _make_draft(tmp_path: Path, content: dict | None = None) -> Path:
    d = tmp_path / "drafts" / "AutoVideo_t1"
    d.mkdir(parents=True, exist_ok=True)
    obj = content if content is not None else {"tracks": []}
    text = json.dumps(obj, ensure_ascii=False)
    (d / "draft_content.json").write_text(text, encoding="utf-8")
    (d / "draft_info.json").write_text(text, encoding="utf-8")
    return d / "draft_content.json"


class TestValidateDraftForResume:
    def test_ok_passes(self, tmp_path: Path) -> None:
        f = _make_draft(tmp_path)
        assert validate_draft_for_resume(f) == []

    def test_missing_path(self) -> None:
        assert validate_draft_for_resume(None) != []
        assert validate_draft_for_resume("") != []

    def test_file_deleted(self, tmp_path: Path) -> None:
        problems = validate_draft_for_resume(tmp_path / "gone" / "draft_content.json")
        assert any("不存在" in p for p in problems)

    def test_unparseable_json(self, tmp_path: Path) -> None:
        d = tmp_path / "drafts" / "d2"
        d.mkdir(parents=True)
        f = d / "draft_content.json"
        f.write_text("{half-saved", encoding="utf-8")
        problems = validate_draft_for_resume(f)
        assert any("解析失败" in p for p in problems)

    def test_future_mtime_flagged(self, tmp_path: Path) -> None:
        f = _make_draft(tmp_path)
        future = time.time() + 3600
        os.utime(f, (future, future))
        problems = validate_draft_for_resume(f)
        assert any("未来" in p for p in problems)

    def test_locked_dir_flagged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = _make_draft(tmp_path)
        monkeypatch.setattr(draft_lock.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "locked")))
        problems = validate_draft_for_resume(f)
        assert any("占用" in p for p in problems)


class TestDraftDirLocked:
    def test_no_file_not_locked(self, tmp_path: Path) -> None:
        assert draft_dir_locked(tmp_path) is False

    def test_permission_error_means_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = tmp_path / "d"
        d.mkdir()
        (d / "draft_content.json").write_text("{}", encoding="utf-8")

        def denied(*a, **k):
            raise PermissionError(13, "locked by JianYing")

        monkeypatch.setattr(draft_lock.os, "open", denied)
        assert draft_dir_locked(d) is True


class TestHashDraftFiles:
    def test_hashes_stable_and_content_sensitive(self, tmp_path: Path) -> None:
        f = _make_draft(tmp_path)
        h1 = hash_draft_files(f)
        assert set(h1) == {"draft_content.json", "draft_info.json"}
        assert len(h1["draft_content.json"]) == 64
        assert h1["draft_content.json"] == h1["draft_info.json"], "双文件一致 → 哈希一致"
        # 人工修改内容 → 哈希变化(审计可检测)
        f.write_text(json.dumps({"tracks": [], "edited": True}), encoding="utf-8")
        assert hash_draft_files(f)["draft_content.json"] != h1["draft_content.json"]

    def test_missing_info_only_hashes_content(self, tmp_path: Path) -> None:
        d = tmp_path / "d3"
        d.mkdir()
        (d / "draft_content.json").write_text("{}", encoding="utf-8")
        assert set(hash_draft_files(d / "draft_content.json")) == {"draft_content.json"}


class TestAssertResumeReady:
    def test_ok_returns_hashes(self, tmp_path: Path) -> None:
        f = _make_draft(tmp_path)
        out = assert_resume_ready(f, proc_checker=lambda: False)
        assert out["ok"] is True
        assert out["problems"] == []
        assert "draft_content.json" in out["hashes"]
        assert out["jianying_running"] is False

    def test_jianying_running_blocks_by_default(self, tmp_path: Path) -> None:
        f = _make_draft(tmp_path)
        out = assert_resume_ready(f, proc_checker=lambda: True)
        assert out["ok"] is False
        assert any("剪映" in p and ("关闭" in p or "退出" in p) for p in out["problems"])

    def test_jianying_running_allowed_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "JY_RESUME_REQUIRE_CLOSED", False)
        f = _make_draft(tmp_path)
        out = assert_resume_ready(f, proc_checker=lambda: True)
        assert out["ok"] is True
        assert out["jianying_running"] is True

    def test_missing_draft_and_running_lists_all_problems(self, tmp_path: Path) -> None:
        out = assert_resume_ready(
            tmp_path / "nope" / "draft_content.json", proc_checker=lambda: True
        )
        assert out["ok"] is False
        assert any("不存在" in p for p in out["problems"])
        assert any("剪映" in p for p in out["problems"])

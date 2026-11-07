"""阶段 1(人工关卡与剪映草稿导入)单测:剪映草稿库注册 + staging 发布 + node_05 接线。

全部用 tmp_path 伪造草稿库根目录(config.JIANYING_DRAFT_ROOT monkeypatch 或
root 参数注入),绝不触碰本机真实 %LOCALAPPDATA% 剪映库。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config
from draft_ops.atomic_writer import safe_write_draft
from draft_ops.draft_publisher import DraftPublishError, publish_draft
from draft_ops.encryption_detector import DraftStatus
from draft_ops.jianying_registry import (
    JYRegistryError,
    compute_draft_duration_us,
    official_draft_dir_name,
    sanitize_name_segment,
    official_draft_dir,
    register_draft,
    upsert_root_meta,
    validate_draft_root,
)


def _make_staging(tmp_path: Path, *, duration_us: int = 1_500_000) -> Path:
    """构造一个合法 staging 目录(双文件字节级一致 + 一段 1.5s 的视频轨)。

    形状对齐 mapper 真实产出(段带 id/material_id、素材带 duration),发布链
    会把这种 storyline 旧格式转换为剪映原生格式(2026-11-07 导入修复)。
    """
    staging = tmp_path / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    content = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {
            "videos": [
                {
                    "id": "video-1",
                    "type": "video",
                    "path": "",
                    "duration": duration_us,
                    "material_name": "clip_0001",
                }
            ],
            "audios": [],
            "texts": [],
        },
        "tracks": [
            {
                "type": "video",
                "id": "track-video-1",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "target_timerange": {"start": 0, "duration": duration_us},
                        "source_timerange": {"start": 0, "duration": duration_us},
                        "speed": 1.0,
                    }
                ],
            }
        ],
    }
    text = json.dumps(content, ensure_ascii=False, indent=2)
    (staging / "draft_content.json").write_text(text, encoding="utf-8")
    (staging / "draft_info.json").write_text(text, encoding="utf-8")
    return staging


@pytest.fixture
def jy_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时剪映草稿库根目录,并 monkeypatch config.JIANYING_DRAFT_ROOT。"""
    root = tmp_path / "jy_drafts"
    monkeypatch.setattr(config, "JIANYING_DRAFT_ROOT", root)
    return root


@pytest.fixture
def publish_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JY_DRAFT_PUBLISH_ENABLED", True)


# ---------------------------------------------------------------------------
# jianying_registry
# ---------------------------------------------------------------------------
class TestValidateRoot:
    def test_creates_missing_and_returns_resolved(self, tmp_path: Path) -> None:
        root = tmp_path / "new_root"
        out = validate_draft_root(root)
        assert out.is_dir()
        assert out == Path(os.path.abspath(str(root)))

    def test_rejects_filesystem_root(self) -> None:
        anchor = Path("C:\\" if os.name == "nt" else "/")
        with pytest.raises(JYRegistryError):
            validate_draft_root(anchor)

    def test_rejects_relative(self) -> None:
        with pytest.raises(JYRegistryError):
            validate_draft_root(Path("relative/root"))

    def test_rejects_regular_file(self, tmp_path: Path) -> None:
        f = tmp_path / "occupied"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(JYRegistryError):
            validate_draft_root(f)


class TestDurationAndNaming:
    def test_compute_duration(self) -> None:
        content = {
            "tracks": [
                {
                    "type": "video",
                    "segments": [
                        {"target_timerange": {"start": 100, "duration": 500}},
                        {"target_timerange": {"start": 0, "duration": 900}},
                    ],
                },
                {
                    "type": "audio",
                    "segments": [{"target_timerange": {"start": 0, "duration": 2000}}],
                },
            ]
        }
        assert compute_draft_duration_us(content) == 2000

    def test_compute_duration_empty_returns_none(self) -> None:
        assert compute_draft_duration_us({"tracks": []}) is None
        assert compute_draft_duration_us({}) is None

    def test_sanitize_name_segment(self) -> None:
        assert sanitize_name_segment("console-abc123") == "console-abc123"
        assert sanitize_name_segment("../evil") == "___evil"
        assert sanitize_name_segment("") == "unknown"

    def test_official_draft_dir_name(self) -> None:
        assert official_draft_dir_name("tid1") == "AutoVideo_tid1"


class TestUpsertRootMeta:
    def test_created_then_merged(self, tmp_path: Path) -> None:
        root = validate_draft_root(tmp_path / "root")
        r1 = upsert_root_meta(
            root, draft_name="AutoVideo_a", draft_id="id1",
            duration_us=1000, draft_dir=root / "AutoVideo_a", ts_us=111,
        )
        assert r1["mode"] == "created"
        assert not r1["backed_up"]
        data = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
        assert len(data["all_draft_store"]) == 1

        # 同名 upsert:更新、保留 create 时间(毫秒口径自动升微秒)
        r2 = upsert_root_meta(
            root, draft_name="AutoVideo_a", draft_id="id1",
            duration_us=2000, draft_dir=root / "AutoVideo_a", ts_us=222,
        )
        assert r2["mode"] == "merged"
        assert r2["backed_up"], "已存在文件修改前应落 .bak"
        assert (root / "root_meta_info.json.bak").exists()
        data = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
        entry = data["all_draft_store"][0]
        assert entry["tm_duration"] == 2000
        assert entry["tm_draft_create"] == 111_000, "重注册不覆盖既有 create 时间(111ms→111000µs)"
        assert entry["tm_draft_modified"] == 222

    def test_append_new_entry(self, tmp_path: Path) -> None:
        root = validate_draft_root(tmp_path / "root")
        upsert_root_meta(root, draft_name="A", draft_id="i1", duration_us=None,
                         draft_dir=root / "A", ts_us=1)
        upsert_root_meta(root, draft_name="B", draft_id="i2", duration_us=None,
                         draft_dir=root / "B", ts_us=2)
        data = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
        assert [e["draft_name"] for e in data["all_draft_store"]] == ["A", "B"]

    def test_corrupt_file_fails_closed(self, tmp_path: Path) -> None:
        root = validate_draft_root(tmp_path / "root")
        (root / "root_meta_info.json").write_text("{oops", encoding="utf-8")
        with pytest.raises(JYRegistryError):
            upsert_root_meta(root, draft_name="A", draft_id="i1", duration_us=None,
                             draft_dir=root / "A")
        # 原文件未被覆盖
        assert (root / "root_meta_info.json").read_text(encoding="utf-8") == "{oops"


class TestRegisterDraft:
    def test_roundtrip(self, tmp_path: Path) -> None:
        root = validate_draft_root(tmp_path / "root")
        target = official_draft_dir(root, "tid-1")
        target.mkdir(parents=True)
        (target / "draft_content.json").write_text("{}", encoding="utf-8")

        reg = register_draft(target, thread_id="tid-1", duration_us=123, root=root)
        assert reg["draft_name"] == "AutoVideo_tid-1"
        meta = json.loads((target / "draft_meta_info.json").read_text(encoding="utf-8"))
        assert meta["tm_duration"] == 123
        assert meta["draft_id"] == reg["draft_id"]
        # 原生 34 键 meta(2026-11-07 实机样本对齐)
        assert set(meta.keys()) >= {
            "draft_cover", "draft_fold_path", "draft_root_path",
            "draft_materials", "draft_timeline_materials_size_",
        }
        assert meta["tm_draft_create"] > 10**14, "tm_draft_create 原生口径为微秒"
        assert meta["draft_fold_path"].endswith("AutoVideo_tid-1")
        assert meta["draft_root_path"] == str(root)
        root_data = json.loads(
            (root / "root_meta_info.json").read_text(encoding="utf-8")
        )
        assert root_data["all_draft_store"][0]["draft_name"] == "AutoVideo_tid-1"
        entry = root_data["all_draft_store"][0]
        # 原生 22 键索引条目
        assert set(entry.keys()) >= {
            "draft_cover", "draft_json_file", "draft_fold_path",
            "draft_timeline_materials_size", "tm_draft_modified",
        }
        assert entry["draft_fold_path"].endswith("AutoVideo_tid-1")
        assert entry["draft_json_file"].endswith("draft_content.json")

    def test_republish_reuses_draft_id(self, tmp_path: Path) -> None:
        root = validate_draft_root(tmp_path / "root")
        target = official_draft_dir(root, "tid-1")
        target.mkdir(parents=True)
        (target / "draft_content.json").write_text("{}", encoding="utf-8")
        r1 = register_draft(target, thread_id="tid-1", duration_us=1, root=root)
        r2 = register_draft(target, thread_id="tid-1", duration_us=2, root=root,
                            draft_id=r1["draft_id"])
        assert r2["draft_id"] == r1["draft_id"]
        data = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
        assert len(data["all_draft_store"]) == 1


# ---------------------------------------------------------------------------
# draft_publisher
# ---------------------------------------------------------------------------
class TestPublishDraft:
    def test_happy_path(self, tmp_path: Path, jy_root: Path) -> None:
        staging = _make_staging(tmp_path)
        result = publish_draft(
            staging, thread_id="console-abc123", proc_checker=lambda: False
        )
        target = official_draft_dir(jy_root, "console-abc123")
        assert result["ok"] is True
        assert not result["reused"]
        assert result["draft_content_path"] == str(target / "draft_content.json")
        assert result["duration_us"] == 1_500_000
        # 双文件 + 双索引全部落位
        assert (target / "draft_content.json").is_file()
        assert (target / "draft_info.json").is_file()
        assert (target / "draft_meta_info.json").is_file()
        assert (jy_root / "root_meta_info.json").is_file()
        # 回读校验:storyline 旧格式 staging 发布时转换为原生(语义一致)
        target_content = json.loads(
            (target / "draft_content.json").read_text(encoding="utf-8")
        )
        assert "new_version" in target_content, "旧格式 staging 应转换为原生格式"
        assert target_content["duration"] == 1_500_000
        assert target_content == json.loads(
            (target / "draft_info.json").read_text(encoding="utf-8")
        ), "双文件保持一致"

    def test_idempotent_republish(self, tmp_path: Path, jy_root: Path) -> None:
        staging = _make_staging(tmp_path)
        r1 = publish_draft(staging, thread_id="t1", proc_checker=lambda: False)
        r2 = publish_draft(staging, thread_id="t1", proc_checker=lambda: False)
        assert r1["reused"] is False and r2["reused"] is True
        data = json.loads((jy_root / "root_meta_info.json").read_text(encoding="utf-8"))
        assert len(data["all_draft_store"]) == 1, "重发布不产生重复注册条目"

    def test_publish_converts_storyline_staging_to_native(
        self, tmp_path: Path, jy_root: Path
    ) -> None:
        """2026-11-07 导入修复:storyline 旧格式 staging 发布时转原生 + sidecar。"""
        staging = _make_staging(tmp_path)
        result = publish_draft(
            staging, thread_id="tid-native", proc_checker=lambda: False
        )
        target = Path(result["published_dir"])
        content = json.loads((target / "draft_content.json").read_text(encoding="utf-8"))
        assert "new_version" in content and len(content) >= 28
        assert (target / "autovideo_storyline_source.json").is_file()

        # 已是原生的 staging:幂等直通(不再转换,内容逐字节不变)
        text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        (staging / "draft_content.json").write_text(text, encoding="utf-8")
        (staging / "draft_info.json").write_text(text, encoding="utf-8")
        result2 = publish_draft(
            staging, thread_id="tid-native2", proc_checker=lambda: False
        )
        target2 = Path(result2["published_dir"])
        assert (target2 / "draft_content.json").read_text(encoding="utf-8") == text
        assert not (target2 / "autovideo_storyline_source.json").exists(), (
            "原生 staging 直通时不应产生 sidecar"
        )

    def test_refuses_when_jianying_running(self, tmp_path: Path, jy_root: Path) -> None:
        staging = _make_staging(tmp_path)
        with pytest.raises(DraftPublishError):
            publish_draft(staging, thread_id="t1", proc_checker=lambda: True)
        assert not jy_root.exists(), "拒绝发布时不写草稿库"

    def test_running_allowed_when_strict_off(
        self, tmp_path: Path, jy_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "JY_PUBLISH_FAIL_ON_JIANYING_RUNNING", False)
        staging = _make_staging(tmp_path)
        result = publish_draft(staging, thread_id="t1", proc_checker=lambda: True)
        assert result["ok"] is True

    @pytest.mark.parametrize(
        "mutate",
        ["drop_info", "mismatch", "bad_json", "missing_dir"],
    )
    def test_staging_validation(self, tmp_path: Path, jy_root: Path, mutate: str) -> None:
        staging = _make_staging(tmp_path)
        if mutate == "drop_info":
            (staging / "draft_info.json").unlink()
        elif mutate == "mismatch":
            (staging / "draft_info.json").write_text("{}", encoding="utf-8")
        elif mutate == "bad_json":
            (staging / "draft_content.json").write_text("{oops", encoding="utf-8")
        elif mutate == "missing_dir":
            staging = tmp_path / "no_such_dir"
        with pytest.raises(DraftPublishError):
            publish_draft(staging, thread_id="t1", proc_checker=lambda: False)

    def test_corrupt_root_meta_fails_closed(
        self, tmp_path: Path, jy_root: Path
    ) -> None:
        jy_root.mkdir(parents=True)
        (jy_root / "root_meta_info.json").write_text("{oops", encoding="utf-8")
        staging = _make_staging(tmp_path)
        with pytest.raises(DraftPublishError):
            publish_draft(staging, thread_id="t1", proc_checker=lambda: False)
        # 用户数据未被覆盖
        assert (jy_root / "root_meta_info.json").read_text(encoding="utf-8") == "{oops"

    def test_snapshots_excluded_from_publish(
        self, tmp_path: Path, jy_root: Path
    ) -> None:
        staging = _make_staging(tmp_path)
        (staging / ".snapshots" / "s1").mkdir(parents=True)
        (staging / ".snapshots" / "s1" / "draft_content.json").write_text(
            "{}", encoding="utf-8"
        )
        publish_draft(staging, thread_id="t1", proc_checker=lambda: False)
        target = official_draft_dir(jy_root, "t1")
        assert not (target / ".snapshots").exists(), "快照目录不进剪映草稿库"


# ---------------------------------------------------------------------------
# node_05 接线
# ---------------------------------------------------------------------------
class TestNode05PublishWiring:
    def _state(self, tmp_path: Path) -> dict:
        return {
            "session_id": "test-pub-1",
            "shot_plan": {
                "shots": [{"id": "s1", "video_ref": "v1", "start_s": 0.0, "end_s": 3.0}]
            },
            "video_input_path": str(tmp_path / "in.mp4"),
            "error_log": [],
            "status_log": [],
        }

    def test_publish_success_backfills_official_path(
        self, tmp_path: Path, jy_root: Path, publish_on: None
    ) -> None:
        from nodes.node_05_generate_draft import generate_initial_jianying_draft

        state = self._state(tmp_path)
        staging = tmp_path / "staging"
        out = generate_initial_jianying_draft(
            state, staging, encrypt_detector=lambda d: __import__(
                "draft_ops.encryption_detector", fromlist=["DraftStatus"]
            ).DraftStatus.PLAINTEXT,
            writer=lambda d, c: __import__(
                "draft_ops.atomic_writer", fromlist=["safe_write_draft"]
            ).safe_write_draft(d, c),
        )
        assert out.get("draft_publish_error") is None
        assert out["draft_published_dir"] == str(official_draft_dir(jy_root, "test-pub-1"))
        assert out["draft_path"] == str(
            official_draft_dir(jy_root, "test-pub-1") / "draft_content.json"
        )
        assert out["_draft_dir_override"] == out["draft_published_dir"]
        assert any(t.startswith("draft.published:") for t in out["status_log"])
        # 正式目录就位
        assert (official_draft_dir(jy_root, "test-pub-1") / "draft_content.json").is_file()

    def test_publish_failure_degrades_to_staging(
        self, tmp_path: Path, jy_root: Path, publish_on: None
    ) -> None:
        from nodes.node_05_generate_draft import generate_initial_jianying_draft

        state = self._state(tmp_path)
        staging = tmp_path / "staging"
        # 根目录被普通文件占用 → 发布必然失败
        jy_root.write_text("occupied", encoding="utf-8")
        out = generate_initial_jianying_draft(
            state, staging,
            encrypt_detector=lambda d: __import__(
                "draft_ops.encryption_detector", fromlist=["DraftStatus"]
            ).DraftStatus.PLAINTEXT,
            writer=lambda d, c: __import__(
                "draft_ops.atomic_writer", fromlist=["safe_write_draft"]
            ).safe_write_draft(d, c),
        )
        # 降级:staging 照常可用,错误进 error_log
        assert out["draft_path"] == str(staging / "draft_content.json")
        assert "_draft_dir_override" not in out
        assert out["draft_publish_error"] is not None
        assert any("草稿发布失败" in e for e in out["error_log"])
        assert any(t.startswith("draft.publish_failed:") for t in out["status_log"])
        # staging 双文件仍在
        assert (staging / "draft_content.json").is_file()

    def test_publish_disabled_keeps_legacy_behavior(
        self, tmp_path: Path, jy_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "JY_DRAFT_PUBLISH_ENABLED", False)
        from nodes.node_05_generate_draft import generate_initial_jianying_draft

        state = self._state(tmp_path)
        staging = tmp_path / "staging"
        out = generate_initial_jianying_draft(
            state, staging,
            encrypt_detector=lambda d: __import__(
                "draft_ops.encryption_detector", fromlist=["DraftStatus"]
            ).DraftStatus.PLAINTEXT,
            writer=lambda d, c: __import__(
                "draft_ops.atomic_writer", fromlist=["safe_write_draft"]
            ).safe_write_draft(d, c),
        )
        assert out["draft_path"] == str(staging / "draft_content.json")
        assert "draft_published_dir" not in out
        assert not jy_root.exists()


# ---------------------------------------------------------------------------
# fork 节点注册接线
# ---------------------------------------------------------------------------
class TestForkEnglishBranchRegistration:
    def test_fork_registers_when_enabled(
        self, tmp_path: Path, jy_root: Path, publish_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nodes.node_fork_english_branch import fork_draft_for_english_branch

        snapshot_dir = tmp_path / "snapshot2"
        snapshot_dir.mkdir()
        (snapshot_dir / "draft_content.json").write_text(
            json.dumps({"tracks": []}), encoding="utf-8"
        )
        # override 指到"草稿库根下的中文主草稿目录"(生产发布后的形态)
        zh_dir = official_draft_dir(jy_root, "tid-fork")
        monkeypatch.setattr(
            config, "JIANYING_DRAFT_ROOT", jy_root
        )
        state = {
            "session_id": "tid-fork",
            "snapshot2_path": str(snapshot_dir / "draft_content.json"),
            "_draft_dir_override": str(zh_dir),
            "status_log": [],
        }
        out = fork_draft_for_english_branch(state)
        en_dir = Path(out["draft_dir_en_branch"])
        assert en_dir.parent == jy_root, "en_branch 应落在剪映草稿库根下"
        assert en_dir.name.startswith("en_branch_tid-fork_")
        assert (en_dir / "draft_content.json").is_file()
        # 注册:root_meta 有条目 + meta_info 落位
        assert (en_dir / "draft_meta_info.json").is_file()
        data = json.loads((jy_root / "root_meta_info.json").read_text(encoding="utf-8"))
        names = {e["draft_name"] for e in data["all_draft_store"]}
        assert en_dir.name in names
        assert any("en_branch_registered:" in t for t in out["status_log"])

    def test_fork_skips_registration_when_disabled(
        self, tmp_path: Path, jy_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "JY_DRAFT_PUBLISH_ENABLED", False)
        from nodes.node_fork_english_branch import fork_draft_for_english_branch

        snapshot_dir = tmp_path / "snapshot2"
        snapshot_dir.mkdir()
        (snapshot_dir / "draft_content.json").write_text(
            json.dumps({"tracks": []}), encoding="utf-8"
        )
        state = {
            "session_id": "tid-fork2",
            "snapshot2_path": str(snapshot_dir / "draft_content.json"),
            "_draft_dir_override": str(tmp_path / "staging"),
            "status_log": [],
        }
        out = fork_draft_for_english_branch(state)
        assert Path(out["draft_dir_en_branch"]).is_dir()
        assert not any("en_branch_registered" in t for t in out["status_log"])

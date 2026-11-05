"""web/console_uploads.py 单元测试(第二期阶段 1)。

覆盖计划 §12.1 单元测试矩阵的上传/路径安全/ffprobe 行:
单/多文件保存、扩展名/大小/数量拒绝、空文件、探测失败清理、路径穿越。
ffprobe 真实探测用例在 ffprobe 不在 PATH 或缺 fixture 时自动跳过。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web.console_uploads import (  # noqa: E402
    ALLOWED_VIDEO_EXTS,
    ConsoleUploadError,
    probe_video_file,
    save_uploaded_videos,
    validate_thread_id,
)

TID = "console-" + "a" * 12


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _fake_probe(path: Path) -> dict:
    return {"width": 1920, "height": 1080, "duration": 30.0}


class TestThreadIdValidation:
    def test_accepts_server_generated_shape(self):
        assert validate_thread_id("console-0123456789ab") == "console-0123456789ab"

    @pytest.mark.parametrize(
        "bad", ["../etc", "video-001", "console-", "console-ZZZ", "", "console-abc"]
    )
    def test_rejects_non_server_shapes(self, bad):
        with pytest.raises(ConsoleUploadError) as ei:
            validate_thread_id(bad)
        assert ei.value.status_code == 400


class TestSaveUploadedVideos:
    def test_single_file_saved_with_server_name(self, tmp_path):
        data = b"\x00\x01mp4-bytes"
        result = save_uploaded_videos(
            TID, [_upload("我的旅行.mp4", data)],
            outputs_root=tmp_path, probe=_fake_probe,
        )
        assert len(result) == 1
        saved = Path(result[0]["path"])
        assert saved.name == "0001.mp4"
        assert saved.parent.name == "inputs"
        assert saved.read_bytes() == data
        assert result[0]["original_name"] == "我的旅行.mp4"
        assert result[0]["bytes"] == len(data)

    def test_multi_files_keep_order_and_ext(self, tmp_path):
        result = save_uploaded_videos(
            TID,
            [_upload("a.MOV", b"mov"), _upload("b.mp4", b"mp4")],
            outputs_root=tmp_path, probe=_fake_probe,
        )
        names = [Path(item["path"]).name for item in result]
        assert names == ["0001.mov", "0002.mp4"]

    def test_disallowed_extension_rejected_before_write(self, tmp_path):
        with pytest.raises(ConsoleUploadError) as ei:
            save_uploaded_videos(
                TID, [_upload("evil.txt", b"text")],
                outputs_root=tmp_path, probe=_fake_probe,
            )
        assert ei.value.status_code == 415
        assert not (tmp_path / TID / "inputs").exists() or not any(
            (tmp_path / TID / "inputs").iterdir()
        )

    @pytest.mark.parametrize("bad", ["a.avi", "b.mkv", "c", "d.mp4.exe"])
    def test_extension_whitelist(self, bad):
        assert Path(bad).suffix.lower() not in ALLOWED_VIDEO_EXTS

    def test_count_limit_rejected(self, tmp_path):
        with pytest.raises(ConsoleUploadError) as ei:
            save_uploaded_videos(
                TID,
                [_upload(f"{i}.mp4", b"x") for i in range(3)],
                outputs_root=tmp_path, max_videos=2, probe=_fake_probe,
            )
        assert ei.value.status_code == 400

    def test_empty_upload_list_rejected(self, tmp_path):
        with pytest.raises(ConsoleUploadError) as ei:
            save_uploaded_videos(TID, [], outputs_root=tmp_path, probe=_fake_probe)
        assert ei.value.status_code == 400

    def test_size_limit_aborts_stream(self, tmp_path):
        with pytest.raises(ConsoleUploadError) as ei:
            save_uploaded_videos(
                TID,
                [_upload("big.mp4", b"x" * 100)],
                outputs_root=tmp_path, max_bytes=10, probe=_fake_probe,
            )
        assert ei.value.status_code == 413
        inputs = tmp_path / TID / "inputs"
        assert not inputs.exists() or not any(inputs.iterdir())

    def test_empty_file_rejected(self, tmp_path):
        with pytest.raises(ConsoleUploadError) as ei:
            save_uploaded_videos(
                TID, [_upload("empty.mp4", b"")],
                outputs_root=tmp_path, probe=_fake_probe,
            )
        assert ei.value.status_code == 415

    def test_probe_failure_cleans_saved_files(self, tmp_path):
        def _bad_probe(path: Path) -> dict:
            raise ConsoleUploadError("ffprobe 校验失败", status_code=415)

        with pytest.raises(ConsoleUploadError):
            save_uploaded_videos(
                TID,
                [_upload("a.mp4", b"aa"), _upload("b.mp4", b"bb")],
                outputs_root=tmp_path, probe=_bad_probe,
            )
        inputs = tmp_path / TID / "inputs"
        assert not inputs.exists() or not any(inputs.iterdir())

    def test_tmp_files_cleaned_on_failure(self, tmp_path):
        calls = {"n": 0}

        def _probe(path: Path) -> dict:
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConsoleUploadError("第二个文件损坏", status_code=415)
            return {"ok": True}

        with pytest.raises(ConsoleUploadError):
            save_uploaded_videos(
                TID,
                [_upload("a.mp4", b"aa"), _upload("b.mp4", b"bb")],
                outputs_root=tmp_path, probe=_probe,
            )
        inputs = tmp_path / TID / "inputs"
        leftovers = list(inputs.iterdir()) if inputs.exists() else []
        assert all(not p.name.startswith(".tmp-") for p in leftovers)

    def test_traversal_thread_id_rejected(self, tmp_path):
        with pytest.raises(ConsoleUploadError):
            save_uploaded_videos(
                "../../outside", [_upload("a.mp4", b"x")],
                outputs_root=tmp_path, probe=_fake_probe,
            )
        # 确认没有任何越界写入
        assert not (tmp_path.parent / "outside").exists()


class TestProbeVideoFileReal:
    """真实 ffprobe 探测(无 ffprobe / 无 fixture 则跳过;仿 assembly 测试约定)。"""

    FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "30s.mp4"

    def test_real_probe_on_fixture(self):
        import shutil

        if shutil.which("ffprobe") is None:
            pytest.skip("ffprobe 不在 PATH")
        if not self.FIXTURE.exists():
            pytest.skip(f"缺 fixture: {self.FIXTURE}")
        meta = probe_video_file(self.FIXTURE, timeout=30.0)
        assert meta["width"] > 0
        assert meta["height"] > 0
        assert meta["duration"] > 0

    def test_probe_rejects_garbage_bytes(self, tmp_path):
        import shutil

        if shutil.which("ffprobe") is None:
            pytest.skip("ffprobe 不在 PATH")
        fake = tmp_path / "fake.mp4"
        fake.write_bytes(b"not a video at all" * 10)
        with pytest.raises(ConsoleUploadError) as ei:
            probe_video_file(fake, timeout=30.0)
        assert ei.value.status_code == 415

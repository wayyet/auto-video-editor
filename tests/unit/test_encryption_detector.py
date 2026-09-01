"""draft_ops.encryption_detector 单测(附件 2.5 节 4 种输入)。"""

from __future__ import annotations

import json
from pathlib import Path

from draft_ops.encryption_detector import (
    DraftStatus,
    default_decrypt_runner,
    detect_draft_encryption,
)


def test_detect_plaintext_json(tmp_draft_dir: Path) -> None:
    """明文 draft_content.json → PLAINTEXT,无需调 capcut。"""
    draft_file = tmp_draft_dir / "draft_content.json"
    draft_file.write_text(json.dumps({"canvas_config": {"width": 1080, "height": 1920}}), encoding="utf-8")
    assert detect_draft_encryption(tmp_draft_dir) == DraftStatus.PLAINTEXT


def test_detect_corrupted_json(tmp_draft_dir: Path) -> None:
    """损坏 JSON → 落到二次确认;若 capcut 失败则判 ENCRYPTED。"""
    draft_file = tmp_draft_dir / "draft_content.json"
    draft_file.write_text("{not valid json", encoding="utf-8")

    def fail_runner(_draft_dir: Path) -> int:
        return 1  # 非 0 → 视作加密

    assert detect_draft_encryption(tmp_draft_dir, decrypt_runner=fail_runner) == DraftStatus.ENCRYPTED


def test_detect_encrypted_aes(tmp_draft_dir: Path) -> None:
    """AES 加密文件(非 UTF-8 二进制)→ 落到二次确认,capcut 失败 → ENCRYPTED。"""
    draft_file = tmp_draft_dir / "draft_content.json"
    draft_file.write_bytes(b"\x00\x01\x02\x03\x04aes-crypt-bytes-not-utf8")

    def fail_runner(_draft_dir: Path) -> int:
        return 2

    assert detect_draft_encryption(tmp_draft_dir, decrypt_runner=fail_runner) == DraftStatus.ENCRYPTED


def test_detect_not_found(tmp_draft_dir: Path) -> None:
    """draft_content.json 不存在 → NOT_FOUND。"""
    assert detect_draft_encryption(tmp_draft_dir) == DraftStatus.NOT_FOUND


def test_detect_default_runner_invoked_on_non_plaintext(tmp_draft_dir: Path) -> None:
    """损坏 JSON 时,默认 runner 应被调用(此处用 monkeypatch 拦截)。"""
    from draft_ops import encryption_detector as mod

    draft_file = tmp_draft_dir / "draft_content.json"
    draft_file.write_text("{bad", encoding="utf-8")

    calls: list[Path] = []

    def fake_runner(p: Path) -> int:
        calls.append(p)
        return 1

    monkey = getattr(mod, "default_decrypt_runner", default_decrypt_runner)
    original = monkey
    mod.default_decrypt_runner = fake_runner  # type: ignore[assignment]
    try:
        assert detect_draft_encryption(tmp_draft_dir) == DraftStatus.ENCRYPTED
        assert calls == [tmp_draft_dir]
    finally:
        mod.default_decrypt_runner = original  # type: ignore[assignment]

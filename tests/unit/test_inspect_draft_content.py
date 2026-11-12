"""inspect_draft_content.py CLI 冒烟 — 退出码语义(0 通过 / 1 读取失败 / 2 门禁不满足)。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.unit.test_draft_content_audit import _draft

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "inspect_draft_content.py"


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _write_draft(tmp_path: Path) -> Path:
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text(json.dumps(_draft(), ensure_ascii=False), encoding="utf-8")
    return draft_file


def test_cli_default_exit_0_and_render(tmp_path: Path) -> None:
    draft_file = _write_draft(tmp_path)
    proc = _run(str(draft_file), "--label", "冒烟草稿")
    assert proc.returncode == 0, proc.stderr
    assert "冒烟草稿" in proc.stdout
    assert "被引用的转场数: 1" in proc.stdout


def test_cli_gate_failure_exit_2(tmp_path: Path) -> None:
    draft_file = _write_draft(tmp_path)
    proc = _run(str(draft_file), "--min", "text_segments=99")
    assert proc.returncode == 2
    assert "门禁不通过" in proc.stderr
    assert "text_segments=1 小于要求的最小值 99" in proc.stderr


def test_cli_missing_target_exit_1(tmp_path: Path) -> None:
    proc = _run(str(tmp_path / "ghost" / "draft_content.json"))
    assert proc.returncode == 1
    assert "读取失败" in proc.stderr


def test_cli_json_output_parses(tmp_path: Path) -> None:
    draft_file = _write_draft(tmp_path)
    proc = _run(str(draft_file), "--json")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["metrics"]["transitions_referenced"] == 1
    assert rep["metrics"]["video_segments"] == 2


def test_cli_bad_gate_format_exits_with_usage_error(tmp_path: Path) -> None:
    draft_file = _write_draft(tmp_path)
    proc = _run(str(draft_file), "--min", "not_a_metric=1")
    assert proc.returncode != 0
    assert "可用指标" in (proc.stdout + proc.stderr)

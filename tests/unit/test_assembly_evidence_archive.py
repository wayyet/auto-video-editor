"""P2-5:证据归档脚本(scripts/archive_assembly_evidence.py)单测。

验收对照(计划文档 P2-5):
- 证据包记录 commit SHA / schema 版本 / 配置开关 / 模型信息 / 输入素材摘要;
- 运行证据中的 final plan hash 与 influence trace hash_chain 对得上;
- 绝对路径脱敏(盘符/用户目录不外泄,basename 保留);
- 最终计划 ↔ 正式草稿视频轨对账(draft_video_diff)。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from assembly_capabilities.draft_patch import plan_sha256
from tests.support.draft_influence import load_canonical_sample

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive_assembly_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("archive_assembly_evidence", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    """伪造一个已跑完 assembly 链的任务目录。"""
    plan = load_canonical_sample()
    assembly = tmp_path / "outputs" / "job-p25" / "assembly"
    assembly.mkdir(parents=True)
    (assembly / "media.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-p25",
                "base_plan_sha256": plan_sha256(plan),
                "sources": [
                    {
                        "asset_id": "a1",
                        "source_path": r"C:\Users\wayyet\Downloads\开发代码\secret_a.mp4",
                        "duration_seconds": 12.0,
                        "probe_status": "ok",
                        "analysis_status": "ok",
                        "candidate_segments": [{"index": 0, "start": 0.0, "end": 4.0}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    trace = {
        "schema_version": 1,
        "base_plan_sha256": plan_sha256(plan),
        "node_contributions": [],
        "final_plan_sha256": plan_sha256(plan),
        "mapped_draft_sha256": "f" * 64,
        "hash_chain": {
            "base_plan_sha256": plan_sha256(plan),
            "candidate_plan_sha256": plan_sha256(plan),
            "final_plan_sha256": plan_sha256(plan),
            "repair_rounds": [],
        },
    }
    (assembly / "influence_trace.json").write_text(
        json.dumps(trace, ensure_ascii=False), encoding="utf-8"
    )
    (assembly / "final_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    return assembly


def _run_archive(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    mod = _load_module()
    monkeypatch.setattr(sys, "argv", ["archive_assembly_evidence.py"] + argv)
    return mod.main()


def test_archive_happy_path(job_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc = _run_archive(
        monkeypatch,
        ["--job-id", "job-p25", "--outputs-root", str(tmp_path / "outputs")],
    )
    assert rc == 0
    evidence_root = job_dir / "evidence"
    stamps = [d for d in evidence_root.iterdir() if d.is_dir()]
    assert stamps, "应生成时间戳证据目录"
    ev = stamps[0]
    # manifest 与关键副本存在
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit_sha"], "必须记录 commit SHA"
    assert manifest["hash_chain"]["final_plan_sha256"] == manifest["hash_chain"]["base_plan_sha256"]
    assert (ev / "media.json").is_file()
    assert (ev / "final_plan.json").is_file()
    # 输入摘要 + hash 对账
    assert manifest["input_summary"][0]["asset_id"] == "a1"
    assert manifest["files"]["media.json"]
    # 脱敏:绝对用户路径不外泄,basename 保留
    archived_media = (ev / "media.json").read_text(encoding="utf-8")
    assert "C:\\Users\\wayyet" not in archived_media
    assert "wayyet" not in archived_media
    assert "secret_a.mp4" in archived_media, "basename 保留可读性"
    assert "<REDACTED>" in archived_media


def test_archive_draft_video_diff(
    job_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 3 段与 sample 计划的 3 个 clip 逐一对应(0-4000/1000-5000/8000-12000ms)
    plan = load_canonical_sample()
    segments = [
        {
            "id": f"s{i + 1}",
            "source_timerange": {
                "start": int(c["source_in_ms"]) * 1000,
                "duration": (int(c["source_out_ms"]) - int(c["source_in_ms"])) * 1000,
            },
        }
        for i, c in enumerate(plan["clips"])
    ]
    draft = {"tracks": [{"type": "video", "segments": segments}]}
    draft_file = tmp_path / "draft_content.json"
    draft_file.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    rc = _run_archive(
        monkeypatch,
        [
            "--job-id", "job-p25",
            "--outputs-root", str(tmp_path / "outputs"),
            "--draft", str(draft_file),
        ],
    )
    assert rc == 0
    ev = next(d for d in (job_dir / "evidence").iterdir() if d.is_dir())
    diff = json.loads((ev / "draft_video_diff.json").read_text(encoding="utf-8"))
    assert diff[0]["match"] is True, "最终计划 clip_0001(0-4000ms)应与草稿段一致"
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["draft_video_diff"]["all_match"] is True


def test_archive_missing_job_dir_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc = _run_archive(
        monkeypatch,
        ["--job-id", "no-such-job", "--outputs-root", str(tmp_path / "outputs")],
    )
    assert rc == 1


def test_redact_various_path_styles() -> None:
    mod = _load_module()
    text = (
        "file:///C:/Users/wayyet/OneDrive/Downloads/开发代码/kuaishou/x.mp4 "
        "C:\\Users\\wayyet\\AppData\\Local\\Temp\\y.json /home/alice/z.mp4"
    )
    red = mod.redact_text(text)
    assert "wayyet" not in red
    assert "alice" not in red
    assert "x.mp4" in red and "y.json" in red and "z.mp4" in red

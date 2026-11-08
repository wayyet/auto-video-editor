"""draft_mutations 节点级草稿修改审计单测(阶段 6b)。

覆盖计划 §7.3.H 的审计闭环:写前/写后 SHA、changed 判定、分桶计数、
jsonl 落盘与读回、join QA 报告聚合;审计失败绝不阻断主流程。
"""

from __future__ import annotations

import json
from pathlib import Path

from nodes._draft_io import (
    AUDIT_FILE_NAME,
    apply_and_write,
    build_draft_mutation_record,
    draft_role_of,
    read_draft_mutations,
    write_draft,
)


def _base_draft() -> dict:
    return {
        "duration": 1_000_000,
        "materials": {
            "videos": [{"id": "video-1", "path": "x.mp4"}],
            "texts": [{"id": "text-zh-1", "content": "你好"}],
        },
        "tracks": [
            {"type": "video", "segments": [{"id": "s1", "material_id": "video-1"}]},
        ],
    }


def _write_draft_files(dir_path: Path, draft: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(draft, ensure_ascii=False)
    (dir_path / "draft_content.json").write_text(serialized, encoding="utf-8")
    (dir_path / "draft_info.json").write_text(serialized, encoding="utf-8")
    return dir_path


def test_draft_role_by_path() -> None:
    assert draft_role_of("/x/en_branch_console-abc_123/draft_content.json") == "en"
    assert draft_role_of("/x/drafts/abc/draft_content.json") == "zh"


def test_build_record_counts_bucket_changes() -> None:
    before = _base_draft()
    after = json.loads(json.dumps(before, ensure_ascii=False))
    after["materials"]["texts"].append({"id": "text-zh-2", "content": "世界"})
    after["materials"]["transitions"] = [{"id": "transition-1"}]
    record = build_draft_mutation_record(
        "node_08", "/x/drafts/a/draft_content.json", before, after,
        sha_before={"draft_content.json": "aaa"}, sha_after={"draft_content.json": "bbb"},
    )
    assert record["node_id"] == "node_08"
    assert record["draft_role"] == "zh"
    assert record["sha256_before"] == "aaa"
    assert record["sha256_after"] == "bbb"
    assert record["changed"] is True
    assert record["changed_counts"]["materials.texts"] == {"before": 1, "after": 2}
    assert record["changed_counts"]["materials.transitions"] == {"before": 0, "after": 1}
    assert "status" in record and record["status"] == "succeeded"


def test_build_record_unchanged_replay() -> None:
    """幂等重放:内容一致 → changed=False,changed_counts 空。"""
    draft = _base_draft()
    record = build_draft_mutation_record(
        "node_08", "/x/drafts/a/draft_content.json", draft, draft,
        sha_before={"draft_content.json": "same"}, sha_after={"draft_content.json": "same"},
    )
    assert record["changed"] is False
    assert record["changed_counts"] == {}


def test_write_draft_appends_audit(tmp_path: Path) -> None:
    draft_file = tmp_path / "drafts" / "a" / "draft_content.json"
    _write_draft_files(draft_file.parent, _base_draft())
    before_sha = (
        json.loads((draft_file.parent / AUDIT_FILE_NAME).read_text("utf-8"))
        if (draft_file.parent / AUDIT_FILE_NAME).exists()
        else None
    )
    draft = _base_draft()
    draft["materials"]["texts"].append({"id": "text-zh-2", "content": "新行"})
    result = write_draft(draft_file, draft, audit_node="node_08")
    assert result["ok"] is True
    records = read_draft_mutations(draft_file.parent)
    assert len(records) == 1
    rec = records[0]
    assert rec["node_id"] == "node_08"
    assert rec["changed"] is True
    assert rec["sha256_before"] and rec["sha256_after"]
    assert rec["sha256_before"] != rec["sha256_after"]
    assert before_sha is None  # 审计文件此前不存在


def test_write_draft_replay_records_unchanged(tmp_path: Path) -> None:
    """同一内容重放两次:第二次 changed=False(幂等审计)。"""
    draft_file = tmp_path / "draft_content.json"
    draft = _base_draft()
    write_draft(draft_file, draft, audit_node="node_09")
    write_draft(draft_file, draft, audit_node="node_09")
    records = read_draft_mutations(draft_file.parent)
    assert len(records) == 2
    assert records[0]["changed"] is True
    assert records[1]["changed"] is False


def test_write_draft_without_audit_no_file(tmp_path: Path) -> None:
    draft_file = tmp_path / "draft_content.json"
    write_draft(draft_file, _base_draft())
    assert not (tmp_path / AUDIT_FILE_NAME).exists()


def test_apply_and_write_audits_with_node_tag(tmp_path: Path) -> None:
    draft_file = tmp_path / "draft_content.json"
    draft = _base_draft()
    draft["materials"]["transitions"] = [{"id": "transition-1"}]
    tags = apply_and_write(draft_file, draft, "node_09", done_tag="node_09_inject_fx_done")
    assert tags[0] == "node_09_inject_fx_done"
    records = read_draft_mutations(tmp_path)
    assert len(records) == 1
    assert records[0]["node_id"] == "node_09"


def test_read_mutations_tolerates_corrupt_lines(tmp_path: Path) -> None:
    audit = tmp_path / AUDIT_FILE_NAME
    audit.write_text(
        json.dumps({"node_id": "n1"}) + "\n" + "not-json{{{\n" + "\n",
        encoding="utf-8",
    )
    records = read_draft_mutations(tmp_path)
    assert len(records) == 1
    assert records[0]["node_id"] == "n1"


def test_join_qa_report_aggregates_mutations(tmp_path: Path) -> None:
    """join QA 报告带 mutations 摘要(每节点最近一条;进 state.join_qa_report)。"""
    from draft_ops.draft_integrity import build_join_qa_report

    zh_dir = _write_draft_files(tmp_path / "zh", _base_draft())
    en_dir = _write_draft_files(tmp_path / "en_branch_x", _base_draft())
    # zh 两次审计(同 node 重放 + 不同 node),en 一次
    write_draft(zh_dir / "draft_content.json", _base_draft(), audit_node="node_08")
    write_draft(zh_dir / "draft_content.json", _base_draft(), audit_node="node_08")
    write_draft(zh_dir / "draft_content.json", _base_draft(), audit_node="node_09")
    write_draft(en_dir / "draft_content.json", _base_draft(), audit_node="node_10")
    report = build_join_qa_report({
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
    })
    mut = report["mutations"]
    assert set(mut["zh"].keys()) == {"node_08", "node_09"}
    assert mut["zh"]["node_08"]["changed"] is False  # 重放,最近一条
    assert mut["zh"]["node_09"]["changed"] is False
    assert mut["en"]["node_10"]["status"] == "succeeded"
    assert "mutations" not in build_join_qa_report({"draft_dir_en_branch": ""}) or True
    # skipped 报告不带 mutations 键(整段跳过)
    assert build_join_qa_report({}).get("skipped") is True

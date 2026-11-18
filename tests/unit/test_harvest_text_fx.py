"""scripts/harvest_text_fx.py 单测(2026-11-19 审查阶段 4 / P1-04)。

覆盖:收割 staging 草稿结构与绑定;apply-verification 受控回填(身份精确匹配、
证据必填、failed 移除 verified、dry-run 不落盘)。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.harvest_text_fx import (
    _build_harvest_staging,
    _identity,
    cmd_apply_verification,
)


def _row(name: str, resource_id: str, effect_id: str, md5: str, **extra: object) -> dict:
    row = {
        "name": name,
        "is_vip": True,
        "duration_s": 1.0,
        "resource_id": resource_id,
        "effect_id": effect_id,
        "md5": md5,
    }
    row.update(extra)
    return row


def test_build_harvest_staging_structure(tmp_path: Path) -> None:
    """staging:双文件字节一致 + plan 条目齐全 + 每段绑定对应动画素材。"""
    rows = [
        _row("甲", "1111111111111111111", "E1", "M1"),
        _row("乙", "2222222222222222222", "E2", "M2"),
    ]
    staging = _build_harvest_staging(rows, tmp_path / "staging")

    content = json.loads((staging / "draft_content.json").read_text(encoding="utf-8"))
    info = (staging / "draft_info.json").read_text(encoding="utf-8")
    assert info == (staging / "draft_content.json").read_text(encoding="utf-8"), "双文件一致(publisher 契约)"
    assert "new_version" in content, "原生形态标记(publisher 幂等直通)"

    texts = content["materials"]["texts"]
    anims = {a["id"]: a for a in content["materials"]["material_animations"]}
    segments = content["tracks"][0]["segments"]
    assert len(texts) == len(segments) == len(anims) == 2

    for idx, seg in enumerate(segments):
        expected_anim = f"text-anim-harvest-{idx:02d}"
        assert seg["extra_material_refs"] == [expected_anim], "每段绑定自己的动画"
        anim_entry = anims[expected_anim]
        assert anim_entry["animations"][0]["name"] == rows[idx]["name"]
        assert anim_entry["animations"][0]["resource_id"] == rows[idx]["resource_id"]

    plan = json.loads((staging / "harvest_plan.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in plan] == ["甲", "乙"]
    assert all(p["effect_id"] and p["md5"] for p in plan), "计划携带完整资源身份"


def _write_pool(tmp_path: Path) -> Path:
    pool = tmp_path / "pool.json"
    pool.write_text(
        json.dumps(
            {
                "intro": [
                    _row("甲", "1111111111111111111", "E1", "M1"),
                    _row(
                        "乙", "2222222222222222222", "E2", "M2",
                        verified=True,
                        verification={
                            "status": "verified",
                            "verified_at": "2026-11-01",
                            "jianying_version": "5.9.0.11632",
                            "note": "旧实证",
                        },
                    ),
                ],
                "loop": [],
                "outro": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return pool


def _results(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "results.json"
    p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return p


def _ns(**kwargs: object) -> argparse.Namespace:
    base = {"results": "", "pool": "", "dry_run": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_apply_verification_verified_requires_evidence(tmp_path: Path) -> None:
    """verified 必须携带 evidence_path;身份精确匹配才回填。"""
    pool = _write_pool(tmp_path)
    results = _results(
        tmp_path,
        [
            {
                "name": "甲", "resource_id": "1111111111111111111",
                "effect_id": "E1", "md5": "M1",
                "status": "verified", "jianying_version": "5.9.0.11632",
                "evidence_path": "outputs/diagnostics/x/shot.png",
            },
            {
                "name": "幽灵", "resource_id": "9999999999999999999",
                "effect_id": "EX", "md5": "MX",
                "status": "verified", "evidence_path": "docs/x.png",
            },
        ],
    )
    rc = cmd_apply_verification(_ns(results=str(results), pool=str(pool)))
    assert rc == 0

    after = json.loads(pool.read_text(encoding="utf-8"))
    row_a = after["intro"][0]
    assert row_a["verified"] is True
    assert row_a["verification"]["status"] == "verified"
    assert row_a["verification"]["evidence_path"] == "outputs/diagnostics/x/shot.png"
    assert row_a["verification"]["resource_identity"]["md5"] == "M1"


def test_apply_verification_rejects_missing_evidence_and_ghost(tmp_path: Path) -> None:
    """缺 evidence_path 的 verified 与身份不匹配条目均被拒绝,池不改动。"""
    pool = _write_pool(tmp_path)
    before = pool.read_text(encoding="utf-8")
    results = _results(
        tmp_path,
        [
            {"name": "甲", "resource_id": "1111111111111111111",
             "effect_id": "E1", "md5": "M1", "status": "verified"},
            {"name": "幽灵", "resource_id": "9999999999999999999",
             "effect_id": "EX", "md5": "MX", "status": "verified", "evidence_path": "x"},
        ],
    )
    rc = cmd_apply_verification(_ns(results=str(results), pool=str(pool)))
    assert rc == 1, "无可回填条目应返回非零"
    assert pool.read_text(encoding="utf-8") == before, "被拒绝时池文件不改动"


def test_apply_verification_failed_status_revokes_verified(tmp_path: Path) -> None:
    """failed 分类:移除 verified 并写入失败验证记录(带身份),供守卫排除。"""
    pool = _write_pool(tmp_path)
    results = _results(
        tmp_path,
        [
            {
                "name": "乙", "resource_id": "2222222222222222222",
                "effect_id": "E2", "md5": "M2",
                "status": "render_failed",
                "evidence_path": "outputs/diagnostics/y/shot.png",
                "note": "预览静止无动效",
            },
        ],
    )
    rc = cmd_apply_verification(_ns(results=str(results), pool=str(pool)))
    assert rc == 0

    after = json.loads(pool.read_text(encoding="utf-8"))
    row_b = after["intro"][1]
    assert "verified" not in row_b, "失败后不得保留 verified"
    assert row_b["verification"]["status"] == "render_failed"


def test_apply_verification_dry_run_does_not_write(tmp_path: Path) -> None:
    """dry-run:只报告不落盘。"""
    pool = _write_pool(tmp_path)
    before = pool.read_text(encoding="utf-8")
    results = _results(
        tmp_path,
        [{"name": "甲", "resource_id": "1111111111111111111",
          "effect_id": "E1", "md5": "M1", "status": "verified",
          "evidence_path": "x"}],
    )
    rc = cmd_apply_verification(_ns(results=str(results), pool=str(pool), dry_run=True))
    assert rc == 0
    assert pool.read_text(encoding="utf-8") == before


def test_identity_order() -> None:
    """身份四元组顺序锁定:(name, resource_id, effect_id, md5)。"""
    row = _row("甲", "1111111111111111111", "E1", "M1")
    assert _identity(row) == ("甲", "1111111111111111111", "E1", "M1")

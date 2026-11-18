"""scripts/build_resource_library.py 单测 — 用最小 fixture 模拟 metadata 文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_resource_library import (
    extract_text_animations,
    extract_transitions,
    extract_video_effects,
    main,
)


FIXTURE_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "build_resource_library_stub"


def test_extract_transitions_includes_free_and_vip() -> None:
    """从 fixture 抽 TransitionMeta,免费 + VIP 都保留(阶段3:fade 语义需要免费"叠化")。

    注:extract_transitions 不去重(去重是 main 里的 _dedup_by_name)。
    """
    rows = extract_transitions(FIXTURE_SRC)
    names = [r["name"] for r in rows]
    # 免费A 保留,is_vip 取元数据实值
    assert "免费A" in names
    free_a = [r for r in rows if r["name"] == "免费A"][0]
    assert free_a["is_vip"] is False
    assert free_a["resource_id"] == "1111111111111111111"
    # VIP_A 出现 2 次(原数据有同名重复)
    assert names.count("VIP_A") == 2
    assert "VIP_B" in names
    # 字段映射正确
    vip_a_first = [r for r in rows if r["name"] == "VIP_A"][0]
    assert vip_a_first["is_vip"] is True
    assert vip_a_first["resource_id"] == "2222222222222222222"
    assert vip_a_first["default_duration_s"] == 1.0
    assert vip_a_first["is_overlap"] is False


def test_dedup_by_name_keeps_first() -> None:
    """main 流程会调 _dedup_by_name:VIP_A 重复保留首条。"""
    from scripts.build_resource_library import _dedup_by_name

    rows = extract_transitions(FIXTURE_SRC)
    deduped, dropped = _dedup_by_name(rows)
    assert dropped == 1  # VIP_A_DUP 被丢
    vip_a = [r for r in deduped if r["name"] == "VIP_A"]
    assert len(vip_a) == 1
    assert vip_a[0]["resource_id"] == "2222222222222222222"  # 首条



def test_extract_video_effects_filters_vip() -> None:
    rows = extract_video_effects(FIXTURE_SRC)
    names = [r["name"] for r in rows]
    assert names == ["VIP_VFX_A", "VIP_VFX_B"]
    assert rows[0]["resource_id"] == "5555555555555555555"


def test_extract_text_animations_intro_loop_outro() -> None:
    intro = extract_text_animations(FIXTURE_SRC, "intro", "text_intro.py")
    loop = extract_text_animations(FIXTURE_SRC, "loop", "text_loop.py")
    outro = extract_text_animations(FIXTURE_SRC, "outro", "text_outro.py")
    assert [r["name"] for r in intro] == ["VIP_intro_A"]
    assert [r["name"] for r in loop] == ["VIP_loop"]
    assert [r["name"] for r in outro] == ["VIP_outro"]


def test_main_writes_json_files(tmp_path: Path) -> None:
    """完整跑 main:把 fixture 写进 tmp_path。"""
    rc = main([
        "--source", str(FIXTURE_SRC),
        "--out-dir", str(tmp_path),
    ])
    assert rc == 0

    fx = json.loads((tmp_path / "fx_resource_library.json").read_text(encoding="utf-8"))
    text = json.loads((tmp_path / "text_resource_library.json").read_text(encoding="utf-8"))

    assert fx["_counts"]["transitions_total"] == 3
    assert fx["_counts"]["transitions_free"] == 1
    assert fx["_counts"]["transitions_vip"] == 2
    assert fx["_counts"]["video_effects_vip"] == 2
    assert text["_counts"]["intro_vip"] == 1
    assert text["_counts"]["loop_vip"] == 1
    assert text["_counts"]["outro_vip"] == 1
    # 所有转场(含免费)的 resource_id 都是 19 位数字
    assert all(len(t["resource_id"]) == 19 for t in fx["transitions"])
    assert all(len(v["resource_id"]) == 19 for v in fx["video_effects"])


def test_main_idempotent(tmp_path: Path) -> None:
    """跑两次,JSON 字节级一致(_generated_at 在第二次跑时复用第一次的时间戳)。"""
    main(["--source", str(FIXTURE_SRC), "--out-dir", str(tmp_path)])
    first_fx = (tmp_path / "fx_resource_library.json").read_bytes()
    first_text = (tmp_path / "text_resource_library.json").read_bytes()

    main(["--source", str(FIXTURE_SRC), "--out-dir", str(tmp_path)])
    second_fx = (tmp_path / "fx_resource_library.json").read_bytes()
    second_text = (tmp_path / "text_resource_library.json").read_bytes()

    assert first_fx == second_fx
    assert first_text == second_text


def test_main_diff_mode_no_write(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """--diff 模式:有现有 JSON 时,diff_lines=0;无现有 JSON 时,打印 + (new file)。"""
    # 先写一次
    main(["--source", str(FIXTURE_SRC), "--out-dir", str(tmp_path)])
    # 再 --diff,应该看到 diff_lines=0
    rc = main(["--source", str(FIXTURE_SRC), "--out-dir", str(tmp_path), "--diff"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "diff_lines=0" in captured.err


def test_main_missing_source_returns_nonzero(tmp_path: Path) -> None:
    """源目录不存在 → 退出码 2。"""
    rc = main(["--source", str(tmp_path / "no_such_dir"), "--out-dir", str(tmp_path)])
    assert rc == 2


# -------------------------------- verified 身份继承(2026-11-19 审查 P1-03)


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


def _verified_record() -> dict:
    return {
        "status": "verified",
        "verified_at": "2026-11-18",
        "jianying_version": "5.9.0.11632",
        "resource_identity": {
            "resource_id": "1111111111111111111",
            "effect_id": "E1",
            "md5": "M1",
        },
        "evidence_path": "docs/x.md",
        "note": "GUI 实证",
    }


def test_carry_over_verified_same_identity_inherits() -> None:
    """同名且 resource_id/effect_id/md5 全相同 + 记录合法 → 继承 verified 与验证记录。"""
    from scripts.build_resource_library import _carry_over_verified

    old = [_row("A", "1111111111111111111", "E1", "M1",
                verified=True, verification=_verified_record())]
    new = [_row("A", "1111111111111111111", "E1", "M1")]

    carried, staled = _carry_over_verified(old, new)

    assert carried == 1 and staled == 0
    assert new[0]["verified"] is True
    assert new[0]["verification"]["status"] == "verified"
    assert new[0]["verification"]["verified_at"] == "2026-11-18", "审计信息随记录继承"


def test_carry_over_verified_same_name_different_effect_id_stales() -> None:
    """同名但 effect_id 变化 → 不继承,标 stale 并记录旧身份。"""
    from scripts.build_resource_library import _carry_over_verified

    old = [_row("A", "1111111111111111111", "E1", "M1",
                verified=True, verification=_verified_record())]
    new = [_row("A", "1111111111111111111", "E9", "M1")]

    carried, staled = _carry_over_verified(old, new)

    assert carried == 0 and staled == 1
    assert "verified" not in new[0]
    assert new[0]["verification"]["status"] == "stale"
    assert new[0]["verification"]["previous_identity"]["effect_id"] == "E1"


def test_carry_over_verified_same_name_different_md5_stales() -> None:
    """同名但 md5 变化 → 不继承,标 stale。"""
    from scripts.build_resource_library import _carry_over_verified

    old = [_row("A", "1111111111111111111", "E1", "M1",
                verified=True, verification=_verified_record())]
    new = [_row("A", "1111111111111111111", "E1", "M9")]

    carried, staled = _carry_over_verified(old, new)

    assert carried == 0 and staled == 1
    assert "verified" not in new[0]
    assert new[0]["verification"]["status"] == "stale"


def test_carry_over_verified_no_prior_keeps_metadata_only() -> None:
    """旧库无同名条目(旧资源被删后新资源复用其他名字)→ 不动,不转移验证。"""
    from scripts.build_resource_library import _carry_over_verified

    old = [_row("OLD", "1111111111111111111", "E1", "M1",
                verified=True, verification=_verified_record())]
    new = [_row("B", "2222222222222222222", "E2", "M2")]

    carried, staled = _carry_over_verified(old, new)

    assert carried == 0 and staled == 0
    assert "verified" not in new[0] and "verification" not in new[0]


def test_carry_over_verified_corrupted_record_stales() -> None:
    """verified=True 但 verification 缺失/状态非法 → 安全降级为 stale,不继承。"""
    from scripts.build_resource_library import _carry_over_verified

    old_missing = [_row("A", "1111111111111111111", "E1", "M1", verified=True)]
    old_bad_status = [_row("A", "1111111111111111111", "E1", "M1",
                           verified=True, verification={"status": "bogus"})]
    new = [_row("A", "1111111111111111111", "E1", "M1")]

    carried1, staled1 = _carry_over_verified(old_missing, [_row("A", "1111111111111111111", "E1", "M1")])
    carried2, staled2 = _carry_over_verified(old_bad_status, [_row("A", "1111111111111111111", "E1", "M1")])

    assert (carried1, staled1) == (0, 1)
    assert (carried2, staled2) == (0, 1)
    assert "verified" not in new[0]


def test_main_verified_carry_over_byte_stable(tmp_path: Path) -> None:
    """端到端:预置带验证记录的旧 JSON → 两次生成字节级一致且验证记录保留。"""
    import copy as _copy

    main(["--source", str(FIXTURE_SRC), "--out-dir", str(tmp_path)])
    text_path = tmp_path / "text_resource_library.json"
    lib = json.loads(text_path.read_text(encoding="utf-8"))
    # 给 intro 条目人工盖 GUI 实证记录(模拟收割回填后的库)
    for row in lib.get("intro", []):
        record = _copy.deepcopy(_verified_record())
        record["resource_identity"] = {
            "resource_id": row["resource_id"],
            "effect_id": row["effect_id"],
            "md5": row["md5"],
        }
        row["verified"] = True
        row["verification"] = record
    text_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    first = text_path.read_bytes()
    main(["--source", str(FIXTURE_SRC), "--out-dir", str(tmp_path)])
    second = text_path.read_bytes()

    assert first == second, "同源同验证记录重复生成应字节级稳定"
    after = json.loads(text_path.read_text(encoding="utf-8"))
    intro_rows = after.get("intro", [])
    assert intro_rows and all(r.get("verified") is True for r in intro_rows), "身份未变时验证记录应保留"

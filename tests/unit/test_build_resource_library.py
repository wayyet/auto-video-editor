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


def test_extract_transitions_filters_vip() -> None:
    """从 fixture 抽 TransitionMeta,只保留 is_vip=True。

    注:extract_transitions 只做 VIP 过滤,不去重(去重是 main 里的 _dedup_by_name)。
    """
    rows = extract_transitions(FIXTURE_SRC)
    names = [r["name"] for r in rows]
    # 免费A 被过滤
    assert "免费A" not in names
    # VIP_A 出现 2 次(原数据有同名重复)
    assert names.count("VIP_A") == 2
    assert "VIP_B" in names
    # 字段映射正确
    vip_a_first = [r for r in rows if r["name"] == "VIP_A"][0]
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

    assert fx["_counts"]["transitions_vip"] == 2
    assert fx["_counts"]["video_effects_vip"] == 2
    assert text["_counts"]["intro_vip"] == 1
    assert text["_counts"]["loop_vip"] == 1
    assert text["_counts"]["outro_vip"] == 1
    # VIP_A 的 resource_id 是 19 位数字
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

"""scripts/harvest_jianying_music.py 单测(BGM选曲 20261114 阶段1)。

覆盖:单/多草稿收割 / dry-run / 幂等重跑保留人工标签 / CLI 覆盖 /
坏JSON与缓存缺失跳过 / 不复制二进制不改源草稿 / 索引坏行清理。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HARVEST_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "harvest_jianying_music.py"
)
_spec = importlib.util.spec_from_file_location("harvest_jianying_music", _HARVEST_PATH)
harvest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harvest)  # type: ignore[union-attr]


def _music_material(
    music_id: str,
    name: str,
    cache_path: Path,
    *,
    duration_us: int = 190766666,
) -> dict:
    return {
        "id": f"MAT-{music_id}",
        "type": "music",
        "music_id": music_id,
        "name": name,
        "category_id": "7405872200315882277",
        "category_name": "会员热榜",
        "duration": duration_us,
        "request_id": f"REQ_{music_id}",
        "path": str(cache_path),
    }


def _make_draft(
    tmp_path: Path,
    draft_name: str,
    materials: list[dict],
) -> Path:
    """建一个仿剪映草稿目录(不经剪映,直接落 draft_content.json)。"""
    d = tmp_path / draft_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "draft_content.json").write_text(
        json.dumps({"materials": {"audios": materials}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return d


@pytest.fixture(autouse=True)
def _user_data_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把剪映用户数据根指向 tmp,使 cache_relative_path 可预期。"""
    root = tmp_path / "UserData"
    (root / "Cache" / "music").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JY_USER_DATA_DIR", str(root))
    yield root


def _make_cache(user_data_root: Path, name: str, size: int = 2048) -> Path:
    p = user_data_root / "Cache" / "music" / name
    p.write_bytes(b"a" * size)
    return p


def test_harvest_single_draft(tmp_path: Path) -> None:
    root = tmp_path / "UserData"
    cache = _make_cache(root, "db0778.mp3")
    draft = _make_draft(tmp_path, "AutoVideo_x", [_music_material("111", "旅途的风景", cache)])
    idx = tmp_path / "lib.jsonl"
    rc = harvest.main(["--draft-dir", str(draft), "--index", str(idx)])
    assert rc == 0
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["music_id"] == "111"
    assert entry["name"] == "旅途的风景"
    assert entry["cache_relative_path"] == "Cache/music/db0778.mp3"
    assert entry["raw_material"]["music_id"] == "111"
    assert entry["request_id"] == "REQ_111"
    assert entry["duration_us"] == 190766666
    assert entry["tempo"] == "unknown"  # 不伪造 medium


def test_auto_mood_derivation(tmp_path: Path) -> None:
    """曲名含"舒缓"自动映射 calm/soft;CLI --mood 覆盖优先。"""
    root = tmp_path / "UserData"
    cache = _make_cache(root, "a.mp3")
    draft = _make_draft(tmp_path, "D1", [_music_material("1", "舒缓背景音乐  Full Track", cache)])
    idx = tmp_path / "lib.jsonl"
    harvest.main(["--draft-dir", str(draft), "--index", str(idx)])
    entry = json.loads(idx.read_text(encoding="utf-8").strip())
    assert "calm" in entry["mood"] and "soft" in entry["mood"]

    harvest.main(
        ["--draft-dir", str(draft), "--index", str(idx), "--mood", "travel,fresh", "--default"]
    )
    entry = json.loads(idx.read_text(encoding="utf-8").strip())
    assert entry["mood"] == ["travel", "fresh"]
    assert entry["is_default"] is True


def test_dry_run_no_write(tmp_path: Path) -> None:
    root = tmp_path / "UserData"
    cache = _make_cache(root, "a.mp3")
    draft = _make_draft(tmp_path, "D1", [_music_material("1", "曲", cache)])
    idx = tmp_path / "lib.jsonl"
    assert harvest.main(["--draft-dir", str(draft), "--index", str(idx), "--dry-run"]) == 0
    assert not idx.exists()


def test_rerun_preserves_manual_tags(tmp_path: Path) -> None:
    """重跑幂等:不加覆盖时人工标签保留,机械字段刷新。"""
    root = tmp_path / "UserData"
    cache = _make_cache(root, "a.mp3")
    draft = _make_draft(tmp_path, "D1", [_music_material("1", "曲", cache)])
    idx = tmp_path / "lib.jsonl"
    harvest.main(["--draft-dir", str(draft), "--index", str(idx), "--mood", "calm", "--notes", "人工备注"])
    first = json.loads(idx.read_text(encoding="utf-8").strip())
    assert harvest.main(["--draft-dir", str(draft), "--index", str(idx)]) == 0
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # 零重复新增
    second = json.loads(lines[0])
    assert second["mood"] == ["calm"]
    assert second["notes"] == "人工备注"
    assert second["harvested_at"] >= first["harvested_at"]


def test_multi_drafts_and_dedupe(tmp_path: Path) -> None:
    root = tmp_path / "UserData"
    c1 = _make_cache(root, "1.mp3")
    c2 = _make_cache(root, "2.mp3")
    d1 = _make_draft(tmp_path, "D1", [_music_material("1", "甲", c1)])
    d2 = _make_draft(tmp_path, "D2", [_music_material("2", "乙", c2), _music_material("1", "甲", c1)])
    idx = tmp_path / "lib.jsonl"
    assert harvest.main(["--draft-dir", str(d1), str(d2), "--index", str(idx)]) == 0
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # 同曲跨草稿去重


def test_corrupt_draft_and_missing_cache_skipped(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    bad = tmp_path / "Bad"
    bad.mkdir()
    (bad / "draft_content.json").write_text("{broken", encoding="utf-8")
    dead = _make_draft(tmp_path, "Dead", [_music_material("9", "无缓存", tmp_path / "gone.mp3")])
    idx = tmp_path / "lib.jsonl"
    rc = harvest.main(["--draft-dir", str(bad), str(dead), "--index", str(idx)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "JSON读取失败" in out
    assert "缓存不可用" in out
    assert not idx.exists()


def test_source_draft_untouched_no_binary_copy(tmp_path: Path) -> None:
    """只读纪律:源草稿字节不变;不产生任何音频拷贝。"""
    root = tmp_path / "UserData"
    cache = _make_cache(root, "a.mp3")
    draft = _make_draft(tmp_path, "D1", [_music_material("1", "曲", cache)])
    before = (draft / "draft_content.json").read_bytes()
    idx = tmp_path / "lib.jsonl"
    harvest.main(["--draft-dir", str(draft), "--index", str(idx)])
    assert (draft / "draft_content.json").read_bytes() == before
    assert cache.read_bytes() == b"a" * 2048  # 原缓存未动
    assert list(idx.parent.glob("*.mp3")) == []  # 无二进制复制


def test_bad_lines_in_existing_index_dropped(tmp_path: Path) -> None:
    root = tmp_path / "UserData"
    cache = _make_cache(root, "a.mp3")
    draft = _make_draft(tmp_path, "D1", [_music_material("1", "曲", cache)])
    idx = tmp_path / "lib.jsonl"
    idx.write_text("{legacy-broken-line\n", encoding="utf-8")
    assert harvest.main(["--draft-dir", str(draft), "--index", str(idx)]) == 0
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["music_id"] == "1"

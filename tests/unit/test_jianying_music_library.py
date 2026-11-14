"""jy_common.jianying_music_library 加载器单测(BGM选曲 20261114 阶段1)。

覆盖:正常契约 / 索引缺失 / 坏行 / 缺 music_id / 重复 id / 缓存失效剔除 /
路径解析(JY_USER_DATA_DIR) / 默认曲标记 / mtime 刷新与显式 reload。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jy_common.jianying_music_library import (
    default_music_library_path,
    jianying_user_data_dir,
    load_jianying_music_library,
    reload_music_library,
    reset_music_library_cache,
)


def _make_cache_file(tmp_path: Path, name: str, size: int = 1024) -> Path:
    p = tmp_path / "cache" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def _line(
    music_id: str,
    name: str,
    cache: Path | None,
    *,
    mood: list[str] | None = None,
    genre: list[str] | None = None,
    tempo: str = "unknown",
    is_default: bool = False,
    duration_us: int = 190766666,
) -> str:
    obj = {
        "music_id": music_id,
        "name": name,
        "category_id": "7405872200315882277",
        "category_name": "会员热榜",
        "duration_us": duration_us,
        "mood": mood or [],
        "genre": genre or [],
        "tempo": tempo,
        "beat_period_ms": None,
        "cache_relative_path": "",
        "request_id": f"REQ_{music_id}",
        "is_default": is_default,
        "raw_material": {
            "id": "MAT-ID",
            "type": "music",
            "music_id": music_id,
            "name": name,
            "duration": duration_us,
            "path": str(cache) if cache else "",
        },
    }
    return json.dumps(obj, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_music_library_cache()
    yield
    reset_music_library_cache()


def test_missing_index_returns_empty_contract(tmp_path: Path) -> None:
    """索引不存在 → 空 library + 匹配段常量,不抛异常。"""
    meta = load_jianying_music_library(path=tmp_path / "nope.jsonl")
    assert meta["library"] == []
    assert meta["default_match"] == {}
    assert meta["matching"]["llm_assist"] is True
    assert meta["matching"]["max_candidates_for_llm"] == 10


def test_load_contract_and_normalization(tmp_path: Path) -> None:
    """正常加载:外层契约 + 归一化字段逐项断言。"""
    cache = _make_cache_file(tmp_path, "a.mp3")
    idx = tmp_path / "lib.jsonl"
    idx.write_text(
        _line("111", "旅途的风景", cache, mood=["calm", "soft", "travel"],
              genre=["piano"], tempo="unknown", is_default=True),
        encoding="utf-8",
    )
    meta = load_jianying_music_library(path=idx)
    assert len(meta["library"]) == 1
    e = meta["library"][0]
    assert e["bgm_id"] == e["music_id"] == "111"
    assert e["name"] == "旅途的风景"
    assert e["mood"] == ["calm", "soft", "travel"]
    assert e["genre"] == ["piano"]
    assert e["tempo"] == "unknown"
    assert e["duration_us"] == 190766666
    assert e["duration_ms"] == 190766
    assert e["asset_available"] is True
    assert e["asset_kind"] == "jianying_music"
    assert e["cache_path"] == str(cache)
    assert e["path"] == str(cache)  # 兼容旧消费键
    assert e["request_id"] == "REQ_111"
    assert e["raw_material"]["music_id"] == "111"
    assert meta["default_match"] == {"bgm_id": "111"}


def test_cache_missing_or_empty_filtered(tmp_path: Path) -> None:
    """缓存不存在或 0 字节 → 剔除出候选。"""
    dead = tmp_path / "cache" / "gone.mp3"
    empty = _make_cache_file(tmp_path, "empty.mp3", size=0)
    idx = tmp_path / "lib.jsonl"
    idx.write_text(
        _line("1", "无缓存", dead) + "\n" + _line("2", "空文件", empty) + "\n"
        + _line("3", "健康", _make_cache_file(tmp_path, "ok.mp3")) + "\n",
        encoding="utf-8",
    )
    meta = load_jianying_music_library(path=idx)
    assert [e["bgm_id"] for e in meta["library"]] == ["3"]


def test_bad_lines_skipped(tmp_path: Path) -> None:
    """坏 JSON 行 / 非对象行 / 缺 music_id 行 → 跳过不崩。"""
    good = _make_cache_file(tmp_path, "ok.mp3")
    idx = tmp_path / "lib.jsonl"
    idx.write_text(
        "{broken json\n"
        + json.dumps(["not", "an", "object"], ensure_ascii=False) + "\n"
        + json.dumps({"name": "没有id"}, ensure_ascii=False) + "\n"
        + _line("9", "幸存", good) + "\n",
        encoding="utf-8",
    )
    meta = load_jianying_music_library(path=idx)
    assert [e["bgm_id"] for e in meta["library"]] == ["9"]


def test_duplicate_music_id_last_wins(tmp_path: Path) -> None:
    cache = _make_cache_file(tmp_path, "ok.mp3")
    idx = tmp_path / "lib.jsonl"
    idx.write_text(
        _line("7", "旧名", cache, mood=["calm"]) + "\n"
        + _line("7", "新名", cache, mood=["happy"]) + "\n",
        encoding="utf-8",
    )
    meta = load_jianying_music_library(path=idx)
    assert len(meta["library"]) == 1
    assert meta["library"][0]["name"] == "新名"
    assert meta["library"][0]["mood"] == ["happy"]


def test_default_flag_fallback_to_first(tmp_path: Path) -> None:
    """无 is_default 标记时默认曲回退为第一首;有标记用标记。"""
    c1 = _make_cache_file(tmp_path, "1.mp3")
    c2 = _make_cache_file(tmp_path, "2.mp3")
    idx = tmp_path / "lib.jsonl"
    idx.write_text(_line("a", "甲", c1) + "\n" + _line("b", "乙", c2) + "\n", encoding="utf-8")
    assert load_jianying_music_library(path=idx)["default_match"] == {"bgm_id": "a"}

    idx.write_text(_line("a", "甲", c1) + "\n" + _line("b", "乙", c2, is_default=True) + "\n", encoding="utf-8")
    assert load_jianying_music_library(path=idx)["default_match"] == {"bgm_id": "b"}


def test_relative_path_resolution_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无绝对路径时用 cache_relative_path + JY_USER_DATA_DIR 解析。"""
    root = tmp_path / "UserData"
    cache = root / "Cache" / "music" / "rel.mp3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"x" * 512)
    monkeypatch.setenv("JY_USER_DATA_DIR", str(root))
    idx = tmp_path / "lib.jsonl"
    obj = json.loads(_line("5", "相对路径曲", None))
    obj["raw_material"]["path"] = ""  # 清空绝对路径
    obj["cache_relative_path"] = "Cache/music/rel.mp3"
    idx.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    meta = load_jianying_music_library(path=idx)
    e = meta["library"][0]
    assert Path(e["cache_path"]) == cache
    assert e["asset_available"] is True


def test_mtime_auto_reload(tmp_path: Path) -> None:
    """索引文件变化后无需 refresh 即可读到新数据(mtime 缓存键)。"""
    cache = _make_cache_file(tmp_path, "ok.mp3")
    idx = tmp_path / "lib.jsonl"
    idx.write_text(_line("1", "甲", cache) + "\n", encoding="utf-8")
    assert [e["bgm_id"] for e in load_jianying_music_library(path=idx)["library"]] == ["1"]
    idx.write_text(_line("1", "甲", cache) + "\n" + _line("2", "乙", cache) + "\n", encoding="utf-8")
    assert len(load_jianying_music_library(path=idx)["library"]) == 2


def test_explicit_reload_and_reset(tmp_path: Path) -> None:
    cache = _make_cache_file(tmp_path, "ok.mp3")
    idx = tmp_path / "lib.jsonl"
    idx.write_text(_line("1", "甲", cache) + "\n", encoding="utf-8")
    assert reload_music_library(path=idx)["library"][0]["name"] == "甲"
    reset_music_library_cache()
    assert default_music_library_path().name == "jianying_music_library.local.jsonl"
    assert jianying_user_data_dir().name == "User Data"

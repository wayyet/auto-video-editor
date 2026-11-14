"""从剪映草稿收割云音乐素材登记到本机曲库(BGM选曲与自动铺轨 20261114 阶段1)。

从一份或多份剪映草稿 ``draft_content.json`` 中提取 ``materials.audios`` 里
``type=="music"`` 且 ``music_id`` 有效的素材,**登记**到本机 JSONL 曲库索引
(默认 ``runtime/jianying_music_library.local.jsonl``,gitignored):

- 保存完整 ``raw_material``(原始素材对象,构造器 deepcopy 用);
- 缓存文件存在且非空才登记(fail-closed);
- 不复制、不上传音乐二进制;不修改任何源草稿(只读);
- 按 ``music_id`` 幂等合并:重跑更新机械字段,保留人工标签
  (mood/genre/tempo/notes/is_default,除非 CLI 显式覆盖);
- 临时文件 + ``os.replace`` 原子写,避免加载器读到半写状态。

用法示例::

    # 预览不写盘
    python scripts/harvest_jianying_music.py --draft-dir <草稿目录> --dry-run
    # 写入本机索引
    python scripts/harvest_jianying_music.py --draft-dir <草稿目录>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from jy_common.jianying_music_library import (  # noqa: E402
    default_music_library_path,
    jianying_user_data_dir,
)

sys.stdout.reconfigure(encoding="utf-8")

#: 曲名/分类文案 -> 规范情绪标签的自动推导映射(保守;CLI --mood 覆盖)
_MOOD_HINTS: list[tuple[str, str]] = [
    ("舒缓", "calm"), ("舒缓", "soft"),
    ("安静", "calm"), ("平静", "calm"), ("轻", "calm"),
    ("柔和", "soft"), ("温柔", "soft"),
    ("旅行", "travel"), ("旅途", "travel"), ("travel", "travel"), ("journey", "travel"),
    ("清新", "fresh"), ("清爽", "fresh"), ("fresh", "fresh"),
    ("欢快", "happy"), ("开心", "happy"), ("愉快", "happy"), ("快乐", "happy"),
    ("活力", "energetic"), ("动感", "energetic"),
    ("叙事", "storytelling"), ("故事", "storytelling"),
    ("紧张", "tense"), ("戏剧", "dramatic"), ("史诗", "epic"), ("壮阔", "epic"),
    ("伤感", "sad"), ("忧伤", "sad"), ("悲伤", "sad"),
    ("怀旧", "memory"), ("回忆", "memory"), ("温暖", "warm"), ("温馨", "warm"),
    ("island", "happy"), ("sunny", "happy"), ("upbeat", "happy"),
]


def _derive_mood(text: str) -> list[str]:
    lowered = (text or "").lower()
    tags: list[str] = []
    for kw, tag in _MOOD_HINTS:
        if kw.lower() in lowered and tag not in tags:
            tags.append(tag)
    return tags


def _find_draft_files(draft_dir: Path) -> list[Path]:
    """草稿目录内定位 draft_content.json(目录本身或一层子目录)。"""
    direct = draft_dir / "draft_content.json"
    if direct.is_file():
        return [direct]
    return sorted(p for p in draft_dir.glob("*/draft_content.json") if p.is_file())


def _cache_relative(path: Path, user_data_dir: Path) -> str:
    """把缓存绝对路径转成相对剪映用户数据根目录的 POSIX 相对路径;失败返回空。"""
    try:
        return path.resolve().relative_to(user_data_dir.resolve()).as_posix()
    except ValueError:
        return ""


def _collect_from_draft(
    draft_file: Path, user_data_dir: Path
) -> tuple[list[dict[str, Any]], list[str]]:
    """从单份草稿提取 (候选条目, 跳过原因列表)。只读。"""
    skips: list[str] = []
    try:
        draft = json.loads(draft_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        return [], [f"{draft_file.parent.name}: 草稿JSON读取失败 {e!r}"]
    audios = (draft.get("materials") or {}).get("audios") or []
    if not isinstance(audios, list):
        return [], [f"{draft_file.parent.name}: materials.audios 缺失或非列表"]
    out: list[dict[str, Any]] = []
    for m in audios:
        if not isinstance(m, dict) or m.get("type") != "music":
            continue
        music_id = str(m.get("music_id") or "").strip()
        if not music_id:
            skips.append(f"{draft_file.parent.name}: music 素材缺 music_id,跳过")
            continue
        raw_path = Path(str(m.get("path") or ""))
        if not (raw_path.is_file() and raw_path.stat().st_size > 0):
            skips.append(
                f"{draft_file.parent.name}: {music_id} 缓存不可用({raw_path}),跳过"
            )
            continue
        out.append(
            {
                "music_id": music_id,
                "name": str(m.get("name") or music_id),
                "category_id": str(m.get("category_id") or ""),
                "category_name": str(m.get("category_name") or ""),
                "duration_us": int(m.get("duration") or 0),
                "request_id": str(m.get("request_id") or ""),
                "cache_relative_path": _cache_relative(raw_path, user_data_dir),
                "raw_material": m,
                "source_draft": draft_file.parent.name,
            }
        )
    return out, skips


def _load_existing_index(index_path: Path) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    if not index_path.is_file():
        return existing
    with open(index_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行不阻断收割,写入时会被剔除
            if isinstance(obj, dict) and obj.get("music_id"):
                existing[str(obj["music_id"])] = obj
    return existing


def _merge_entry(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """按 music_id 合并:机械字段以本次收割为准;人工标签除非显式覆盖否则保留。"""
    if existing is None:
        auto_mood = _derive_mood(
            f"{candidate['name']} {candidate['category_name']}"
        )
        entry = {
            "music_id": candidate["music_id"],
            "name": candidate["name"],
            "category_id": candidate["category_id"],
            "category_name": candidate["category_name"],
            "duration_us": candidate["duration_us"],
            "mood": auto_mood,
            "genre": [],
            "tempo": "unknown",
            "beat_period_ms": None,
            "notes": "",
            "is_default": False,
        }
    else:
        entry = dict(existing)
        entry.update(
            {
                "name": candidate["name"],
                "category_id": candidate["category_id"],
                "category_name": candidate["category_name"],
                "duration_us": candidate["duration_us"],
            }
        )
    entry.update(
        {
            "request_id": candidate["request_id"],
            "cache_relative_path": candidate["cache_relative_path"],
            "raw_material": candidate["raw_material"],
            "source_draft": candidate["source_draft"],
            "harvested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    for key in ("mood", "genre", "tempo", "notes", "is_default"):
        if overrides.get(key) is not None:
            entry[key] = overrides[key]
    return entry


def _write_index(index_path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """临时文件 + 原子替换写盘(修订 §2.18)。"""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(index_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for entry in entries.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, index_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从剪映草稿收割云音乐素材(只读源草稿)")
    parser.add_argument(
        "--draft-dir", nargs="+", required=True, help="草稿目录(可多份;自动定位 draft_content.json)"
    )
    parser.add_argument("--index", default="", help="曲库索引路径(默认 runtime 本机索引)")
    parser.add_argument("--dry-run", action="store_true", help="预览,不写盘")
    parser.add_argument("--mood", default=None, help="情绪标签覆盖(逗号分隔规范标签,如 calm,soft)")
    parser.add_argument("--genre", default=None, help="风格覆盖(逗号分隔)")
    parser.add_argument("--tempo", default=None, help="tempo 覆盖(slow/medium/fast/unknown)")
    parser.add_argument("--notes", default=None, help="备注覆盖")
    parser.add_argument("--default", action="store_true", help="把本次收割曲目标记为默认曲")
    args = parser.parse_args(argv)

    index_path = Path(args.index) if args.index else default_music_library_path()
    user_data_dir = jianying_user_data_dir()

    candidates: list[dict[str, Any]] = []
    skips: list[str] = []
    for d in args.draft_dir:
        draft_dir = Path(d)
        if not draft_dir.is_dir():
            skips.append(f"{draft_dir}: 目录不存在")
            continue
        files = _find_draft_files(draft_dir)
        if not files:
            skips.append(f"{draft_dir}: 未找到 draft_content.json")
            continue
        for f in files:
            found, s = _collect_from_draft(f, user_data_dir)
            candidates.extend(found)
            skips.extend(s)

    # music_id 去重(同曲多草稿只留一份,后到覆盖)
    by_id: dict[str, dict[str, Any]] = {}
    for c in candidates:
        by_id[c["music_id"]] = c

    overrides: dict[str, Any] = {
        "mood": [t.strip() for t in args.mood.split(",") if t.strip()] if args.mood else None,
        "genre": [t.strip() for t in args.genre.split(",") if t.strip()] if args.genre else None,
        "tempo": args.tempo,
        "notes": args.notes,
        "is_default": True if args.default else None,
    }

    if not by_id:
        print("[harvest] 未发现可登记的云音乐素材")
        for s in skips:
            print(f"  [skip] {s}")
        return 2

    existing = _load_existing_index(index_path)
    merged: dict[str, dict[str, Any]] = dict(existing)
    new_cnt = 0
    for music_id, cand in by_id.items():
        is_new = music_id not in merged
        entry = _merge_entry(None if is_new else merged[music_id], cand, overrides)
        merged[music_id] = entry
        new_cnt += 1 if is_new else 0
        tag = "新增" if is_new else "更新"
        print(f"  [{tag}] {music_id} {entry['name']} mood={entry['mood']} "
              f"cache={entry.get('cache_relative_path') or '(绝对路径见raw_material)'}")

    if args.dry_run:
        print(f"[dry-run] 不写盘;索引将变为 {len(merged)} 首(新增 {new_cnt})")
    else:
        _write_index(index_path, merged)
        print(f"[ok] 索引已写盘 {index_path} 共 {len(merged)} 首(本次新增 {new_cnt})")
    for s in skips:
        print(f"  [skip] {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

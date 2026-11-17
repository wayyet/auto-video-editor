"""把本机曲库索引指向的剪映缓存音频备份进项目 resource/bgms(20261117 曲库池扩充配套)。

- 从 ``runtime/jianying_music_library.local.jsonl`` 逐条取 ``cache_relative_path``,
  校验剪映缓存 mp3 存在且非空后,复制为 ``resource/bgms/{music_id}_{曲名}.mp3``;
- 幂等:目标文件已存在且字节数一致则跳过复制(--force 强制重拷);
- 生成/刷新 ``resource/bgms/bgm_info.json`` 清单(与 ``resource/fonts/font_info.json``
  同款数组约定,一律仓库相对路径,不写绝对路径);
- 给索引每条补 ``project_audio_path`` 字段:harvest 重跑按 music_id 合并时
  会原样保留该字段,后续扩充曲库后重跑本脚本即可把新增曲目同步备份;
- 只读剪映缓存目录,不修改任何剪映数据;原子写盘。

用法::

    python scripts/backup_bgm_audio.py           # 增量备份 + 刷新清单/索引
    python scripts/backup_bgm_audio.py --force   # 无视幂等全部重拷
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from jy_common.jianying_music_library import (  # noqa: E402
    default_music_library_path,
    jianying_user_data_dir,
)

sys.stdout.reconfigure(encoding="utf-8")

#: 索引/清单的备份音频落点(仓库相对路径前缀)
BGMDIR_RELPATH = ("resource", "bgms")


def _safe_name(name: str) -> str:
    """曲名 -> 文件名安全片段:替换 Windows 非法字符与空白,保中文。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).replace(" ", "_")
    return cleaned.strip("._") or "track"


def _backup(index_path: Path, bgm_dir: Path, force: bool) -> int:
    jy_root = Path(jianying_user_data_dir())
    entries = [
        json.loads(line)
        for line in index_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not entries:
        print(f"[backup] 索引为空({index_path}),无事可做")
        return 0

    bgm_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    copied = skipped = 0
    for entry in entries:
        music_id = str(entry.get("music_id") or "").strip()
        if not music_id:
            print(f"  [skip] 索引条目缺 music_id: {entry.get('name')!r}")
            continue
        cache = jy_root / str(entry.get("cache_relative_path") or "")
        if not (cache.is_file() and cache.stat().st_size > 0):
            print(f"  [skip] {music_id} 缓存不可用({cache.name}),不备份")
            continue

        filename = f"{music_id}_{_safe_name(str(entry.get('name') or music_id))}.mp3"
        rel_path = f"{'/'.join(BGMDIR_RELPATH)}/{filename}"
        dest = bgm_dir / filename
        if dest.is_file() and not force and dest.stat().st_size == cache.stat().st_size:
            skipped += 1
        else:
            shutil.copy2(cache, dest)
            if dest.stat().st_size != cache.stat().st_size:
                raise SystemExit(f"[backup] 字节数不一致,中止: {filename}")
            copied += 1

        entry["project_audio_path"] = rel_path
        manifest.append(
            {
                "name": entry.get("name"),
                "music_id": music_id,
                "mood": entry.get("mood") or [],
                "duration_ms": int(entry.get("duration_us") or 0) // 1000,
                "category_name": entry.get("category_name") or "",
                "file_path": rel_path,
                "jianying_cache_relative_path": entry.get("cache_relative_path"),
                "source_draft": entry.get("source_draft"),
                "harvested_at": entry.get("harvested_at"),
            }
        )

    manifest_path = bgm_dir / "bgm_info.json"
    tmp_manifest = manifest_path.with_suffix(".json.tmp")
    with open(tmp_manifest, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=4)
        f.write("\n")
    os.replace(tmp_manifest, manifest_path)

    tmp_index = index_path.with_suffix(".jsonl.tmp")
    with open(tmp_index, "w", encoding="utf-8", newline="\n") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp_index, index_path)

    total_mb = sum(p.stat().st_size for p in bgm_dir.glob("*.mp3")) / 1048576
    print(
        f"[backup] 新拷 {copied} / 幂等跳过 {skipped};清单 {manifest_path.name} "
        f"共 {len(manifest)} 首;音频总量 {total_mb:.1f} MB"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份曲库缓存音频到项目 resource/bgms")
    parser.add_argument(
        "--index", default="", help="曲库索引路径(默认 runtime 本机索引)"
    )
    parser.add_argument("--force", action="store_true", help="无视幂等全部重拷")
    args = parser.parse_args(argv)

    index_path = Path(args.index) if args.index else default_music_library_path()
    bgm_dir = _REPO_ROOT.joinpath(*BGMDIR_RELPATH)
    return _backup(index_path, bgm_dir, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())

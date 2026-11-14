"""阶段0 黄金样本结构盘点(只读,BGM选曲与自动铺轨 20261114)。

对一份或多份剪映草稿 ``draft_content.json`` 做只读盘点,产出:

1. ``materials.audios`` 中 ``type=="music"`` 的完整素材 JSON(37 键云音乐形态);
2. 引用这些素材的 audio track + segment(含 ``extra_material_refs`` 原值);
3. 每个 ref 的 ``id -> materials 桶 -> 素材 JSON`` 映射,未命中标
   ``found=False``(不允许猜成 beats);
4. ``materials.audio_fades`` 全量(与 ``build_native_audio_fade_material``
   的 fade_in_type/fade_out_type 结构对比用);
5. 多份草稿时,按 ``music_id`` 对素材键集合与公共键取值做差异对比
   (阶段0 第6项:至少 3 首曲目字段差异)。

输出(写盘两份,stdout 打掩码摘要):
- ``<out-dir>/golden_sample_audit.json``  原始数据(**含本机绝对路径,
  outputs/ 已 gitignored,严禁拷贝进入库文档**);
- ``<out-dir>/golden_sample_audit_summary.md``  路径掩码后的摘要,可入库。

只读:不修改任何草稿,不复制音乐二进制。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

#: 黄金样本默认路径(console-50cd4c6af941 发布副本,素材为人工在剪映内添加)
DEFAULT_GOLDEN_DRAFT = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"
    / "AutoVideo_console-50cd4c6af941" / "draft_content.json"
)


def _mask(text: str) -> str:
    """把本机用户目录绝对路径掩码成 C:/Users/<user>/...,便于入库。"""
    home = str(Path.home())
    return text.replace(home, "C:/Users/<user>").replace(
        home.replace("\\", "/"), "C:/Users/<user>"
    )


def _music_materials(draft: dict[str, Any]) -> list[dict[str, Any]]:
    mats = (draft.get("materials") or {}).get("audios") or []
    return [m for m in mats if isinstance(m, dict) and m.get("type") == "music"]


def _audio_segments_for(
    draft: dict[str, Any], material_ids: set[str]
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for t in draft.get("tracks") or []:
        if not isinstance(t, dict) or t.get("type") != "audio":
            continue
        for s in t.get("segments") or []:
            if isinstance(s, dict) and s.get("material_id") in material_ids:
                hits.append(
                    {
                        "track_id": t.get("id"),
                        "track_name": t.get("name"),
                        "segment": s,
                    }
                )
    return hits


def _bucket_index(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for bucket, items in (draft.get("materials") or {}).items():
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                idx.setdefault(str(it["id"]), {"bucket": bucket, "material": it})
    return idx


def _ref_map(
    draft: dict[str, Any], refs: list[str]
) -> dict[str, dict[str, Any]]:
    idx = _bucket_index(draft)
    out: dict[str, dict[str, Any]] = {}
    for r in refs:
        hit = idx.get(r)
        out[r] = {
            "found": hit is not None,
            "bucket": hit["bucket"] if hit else None,
            "material": hit["material"] if hit else None,
        }
    return out


def _video_max_end_us(draft: dict[str, Any]) -> int | None:
    best: int | None = None
    for t in draft.get("tracks") or []:
        if not isinstance(t, dict) or t.get("type") != "video":
            continue
        for s in t.get("segments") or []:
            tr = (s or {}).get("target_timerange") or {}
            try:
                end = int(tr.get("start") or 0) + int(tr.get("duration") or 0)
            except (TypeError, ValueError):
                continue
            best = end if best is None else max(best, end)
    return best


def _audit_one(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    draft = json.loads(raw)
    musics = _music_materials(draft)
    mat_ids = {str(m.get("id")) for m in musics}
    seg_hits = _audio_segments_for(draft, mat_ids)
    refs: list[str] = []
    for h in seg_hits:
        refs.extend(h["segment"].get("extra_material_refs") or [])
    fades = (draft.get("materials") or {}).get("audio_fades") or []
    return {
        "draft_path": str(path),
        "draft_path_masked": _mask(str(path)),
        "draft_duration_us": draft.get("duration"),
        "video_max_end_us": _video_max_end_us(draft),
        "music_materials": musics,
        "audio_segments": seg_hits,
        "extra_material_ref_map": _ref_map(draft, refs),
        "audio_fades": fades,
    }


def _compare(audits: list[dict[str, Any]]) -> dict[str, Any]:
    """按 music_id 对比多首素材:键集合差异 + 公共键取值差异。"""
    per_id: dict[str, list[dict[str, Any]]] = {}
    for a in audits:
        for m in a["music_materials"]:
            per_id.setdefault(str(m.get("music_id")), []).append(
                {"draft": a["draft_path_masked"], "material": m}
            )
    comparison: dict[str, Any] = {}
    for music_id, entries in per_id.items():
        key_sets = [set(e["material"].keys()) for e in entries]
        union = sorted(set().union(*key_sets))
        common = sorted(set.intersection(*key_sets))
        diffs: dict[str, list[dict[str, Any]]] = {}
        for key in common:
            vals = [json.dumps(e["material"].get(key), ensure_ascii=False, sort_keys=True) for e in entries]
            if len(set(vals)) > 1:
                diffs[key] = [
                    {"draft": e["draft"], "value": e["material"].get(key)}
                    for e in entries
                ]
        comparison[music_id] = {
            "name": entries[0]["material"].get("name"),
            "occurrences": [
                {"draft": e["draft"], "keys": sorted(e["material"].keys())}
                for e in entries
            ],
            "keys_union": union,
            "keys_common": common,
            "keys_not_in_all": sorted(set(union) - set(common)),
            "common_key_value_diffs": diffs,
        }
    return comparison


def _cross_track_compare(audits: list[dict[str, Any]]) -> dict[str, Any]:
    """跨曲目(不同 music_id)对比:字段差异表 + 恒定不变键清单。"""
    entries: list[dict[str, Any]] = []
    for a in audits:
        for m in a["music_materials"]:
            entries.append(
                {
                    "music_id": str(m.get("music_id")),
                    "name": m.get("name"),
                    "material": m,
                }
            )
    if len(entries) < 2:
        return {}
    key_sets = [set(e["material"].keys()) for e in entries]
    union = sorted(set().union(*key_sets))
    common = sorted(set.intersection(*key_sets))
    value_diffs: dict[str, list[dict[str, Any]]] = {}
    invariant: list[str] = []
    for key in common:
        vals = {json.dumps(e["material"].get(key), ensure_ascii=False, sort_keys=True) for e in entries}
        if len(vals) > 1:
            value_diffs[key] = [
                {"music_id": e["music_id"], "name": e["name"], "value": e["material"].get(key)}
                for e in entries
            ]
        else:
            invariant.append(key)
    return {
        "keys_union": union,
        "keys_common": common,
        "keys_not_in_all": sorted(set(union) - set(common)),
        "value_diffs_by_key": value_diffs,
        "invariant_keys": sorted(invariant),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="黄金样本 BGM 素材结构盘点(只读)")
    parser.add_argument(
        "--draft",
        nargs="+",
        default=[str(DEFAULT_GOLDEN_DRAFT)],
        help="draft_content.json 路径(可多份,多份时做 music_id 字段对比)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent.parent / "outputs" / "audit_bgm"),
    )
    args = parser.parse_args(argv)

    audits: list[dict[str, Any]] = []
    for p in args.draft:
        path = Path(p)
        if not path.is_file():
            print(f"[skip] 草稿不存在: {_mask(str(path))}")
            continue
        audits.append(_audit_one(path))
    if not audits:
        print("无可用草稿,退出")
        return 2

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drafts": audits,
        "music_id_comparison": _compare(audits),
        "cross_track_comparison": _cross_track_compare(audits),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "golden_sample_audit.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 掩码摘要(可入库)
    lines: list[str] = ["# 黄金样本 BGM 盘点摘要(路径已掩码)", ""]
    for a in audits:
        lines.append(f"## 草稿 `{a['draft_path_masked']}`")
        lines.append(f"- draft.duration={a['draft_duration_us']} video_max_end={a['video_max_end_us']}")
        for m in a["music_materials"]:
            mm = dict(m)
            if "path" in mm:
                mm["path"] = _mask(str(mm["path"]))
            lines.append(f"- music 素材 id={m.get('id')} music_id={m.get('music_id')} name={m.get('name')} 键数={len(m)}")
            lines.append(f"  - 素材 JSON: `{json.dumps(mm, ensure_ascii=False)}`")
        for h in a["audio_segments"]:
            lines.append(f"- audio 轨 {h['track_id']}(name={h['track_name']!r}) segment: `{json.dumps(h['segment'], ensure_ascii=False)}`")
        for r, info in a["extra_material_ref_map"].items():
            state = f"{info['bucket']}" if info["found"] else "UNRESOLVED"
            mat = json.dumps(info["material"], ensure_ascii=False) if info["found"] else "-"
            lines.append(f"- ref {r} -> {state} {mat}")
        lines.append(f"- audio_fades: `{json.dumps(a['audio_fades'], ensure_ascii=False)}`")
        lines.append("")
    lines.append("## music_id 键集合/取值差异")
    for mid, comp in report["music_id_comparison"].items():
        lines.append(
            f"- {mid}({comp['name']}): 键数={len(comp['keys_union'])} "
            f"非全曲目共有键={comp['keys_not_in_all']} "
            f"公共键取值差异键={sorted(comp['common_key_value_diffs'].keys())}"
        )
    cross = report.get("cross_track_comparison") or {}
    lines.append("")
    lines.append("## 跨曲目字段差异(不同 music_id 之间)")
    lines.append(f"- 全部曲目键并集数={len(cross.get('keys_union', []))} "
                 f"公共键数={len(cross.get('keys_common', []))} "
                 f"非公共键={cross.get('keys_not_in_all', [])}")
    lines.append(f"- 公共键中取值随曲目变化的键: {sorted((cross.get('value_diffs_by_key') or {}).keys())}")
    for key, vals in (cross.get("value_diffs_by_key") or {}).items():
        for v in vals:
            shown = _mask(str(v["value"]))
            lines.append(f"  - {key} [{v['music_id']}({v['name']})] = {shown}")
    invariant = cross.get("invariant_keys") or []
    lines.append(f"- 跨曲目恒定不变键({len(invariant)}): {invariant}")
    summary_path = out_dir / "golden_sample_audit_summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"[ok] 原始报告(含本机路径,勿入库): {_mask(str(json_path))}")
    print(f"[ok] 掩码摘要(可入库): {_mask(str(summary_path))}")
    print(f"[ok] 草稿数={len(audits)} music素材数={sum(len(a['music_materials']) for a in audits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

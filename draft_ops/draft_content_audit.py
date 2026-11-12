"""draft_content_audit — 剪映草稿「真实内容」的引用口径统计(第四期阶段 0 入库)。

核心判据(剪映的渲染口径,原计划《剪映草稿缺失转场/动画/文案/配音》附录 B):
- 素材桶(materials.*)里有条目 ≠ 画面里能看到;只有被某个 segment 通过
  ``material_id`` 或 ``extra_material_refs`` 引用的素材才会出现在时间轴上;
- 没被引用的条目叫「孤儿素材」,剪映不会显示(剪映保存时也可能把它们丢弃)。

单一实现(第四期 §3.3-1):检查脚本 ``scripts/inspect_draft_content.py`` 与
交付前 QA(:func:`nodes.node_join_before_delivery` 的转场/特效孤儿检测)共用
本模块的引用统计 —— 只有一套口径,不会各自漂移。

只用 Python 标准库,没有第三方依赖;对草稿 dict 只读不写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 本项目在草稿目录里写的旁路文件(记录「转换时没能写进草稿的内容」)
SIDECAR_NAME = "autovideo_storyline_source.json"

# 用户可见内容相关的素材桶(其余是伴随桶:speeds / canvases 等,不算用户可见内容)
CONTENT_BUCKETS = (
    "videos", "audios", "texts", "transitions", "video_effects",
    "material_animations", "stickers", "audio_fades", "effects", "text_templates",
)

# 形状检查:各桶条目至少应含的键(依据 pyJianYingDraft 0.3.1 生成的原生样本;
# 待 G5① 取得剪映 5.9.0.11632 真实样本后以真样本为准修正 —— G4)。只产生
# 告警,不改变内容统计。
REQUIRED_KEYS = {
    "transitions": ("effect_id", "resource_id", "duration"),
    "video_effects": ("effect_id", "resource_id", "type"),
    "audio_fades": ("fade_in_duration", "fade_out_duration"),
    "stickers": ("resource_id", "type"),
    "material_animations": ("animations",),
}

# 渲染口径:素材必须被「对的轨道类型」的片段引用才会显示。
# 例如贴纸只被 video 轨片段的 extra_material_refs 引用 = 不会渲染(称为「错位引用」)。
EXPECT_TRACKS = {
    "texts": ("text",),
    "audios": ("audio",),
    "transitions": ("video",),
    "video_effects": ("effect", "video"),
    "material_animations": ("text", "video"),
    "stickers": ("sticker",),
    "audio_fades": ("audio", "video"),
}

METRIC_LABELS = {
    "video_segments": "视频片段数(video 轨)",
    "audio_segments": "音频片段数(audio 轨上真有的)",
    "text_segments": "文字片段数(text 轨)",
    "sticker_segments": "贴纸片段数(sticker 轨)",
    "effect_segments": "特效片段数(effect 轨)",
    "transitions_referenced": "被引用的转场数",
    "video_effects_referenced": "被引用的视频特效数",
    "text_animations_referenced": "被引用的文字动画数",
    "stickers_referenced": "被引用的贴纸数",
    "audio_fades_referenced": "被引用的音频淡入淡出数",
    "orphan_materials": "孤儿素材总数(内容桶内未被任何片段引用的条目)",
    "misplaced_references": "错位引用数(被引用,但引用方轨道类型不对,剪映不渲染)",
    "dangling_references": "悬空引用数(片段引用的 id 在任何素材桶里都找不到)",
    "placeholder_values": "占位符取值个数(值以 PLACEHOLDER 开头)",
    "shape_warnings": "形状告警数(缺少原生必需键)",
}


def load_draft_target(target: Path) -> tuple[dict[str, Any], Path]:
    """读 ``draft_content.json`` 或包含它的草稿目录;返回 ``(draft, 实际文件路径)``。"""
    p = Path(target) / "draft_content.json" if Path(target).is_dir() else Path(target)
    return json.loads(p.read_text(encoding="utf-8")), p


def collect_referenced_ids(draft: dict[str, Any]) -> set[str]:
    """全部片段的 ``material_id`` 与 ``extra_material_refs``(「被片段引用」唯一口径)。

    QA 孤儿判定与 :func:`inspect` 共用本函数,保证两处口径不漂移;
    ``keyframe_refs`` 是关键帧对素材的引用,不算「片段引用素材」,
    孤儿/错位判定不含它(:func:`inspect` 内部另行叠加,与原计划附录 B 口径一致)。
    """
    refs: set[str] = set()
    for track in (draft.get("tracks") or []):
        if not isinstance(track, dict):
            continue
        for seg in (track.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            mid = seg.get("material_id")
            if mid:
                refs.add(str(mid))
            refs.update(str(r) for r in (seg.get("extra_material_refs") or []) if r)
    return refs


def orphan_ids(draft: dict[str, Any], bucket: str) -> list[str]:
    """素材桶内未被任何片段引用的条目 id(保序;空桶返回空列表)。

    交付前 QA 的转场/特效孤儿检测(第四期 §3.3-2)与检查脚本共用此函数。
    """
    referenced = collect_referenced_ids(draft)
    return [
        str(e.get("id"))
        for e in ((draft.get("materials") or {}).get(bucket) or [])
        if isinstance(e, dict) and e.get("id") and str(e.get("id")) not in referenced
    ]


def _text_plain(mat: dict[str, Any]) -> str:
    """原生文本素材的 content 是 JSON 字符串({"text": "...", "styles": [...]});尽力取出纯文本。"""
    raw = mat.get("content")
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "text" in obj:
                return str(obj["text"])
        except (ValueError, TypeError):
            return raw
        return raw
    return str(raw)


def inspect(draft: dict[str, Any]) -> dict[str, Any]:
    """对草稿 dict 做引用口径统计;返回报告(结构见 :func:`scripts.inspect_draft_content`)。"""
    materials = draft.get("materials") or {}
    tracks = [t for t in (draft.get("tracks") or []) if isinstance(t, dict)]

    referenced = collect_referenced_ids(draft)
    material_refs = set(referenced)  # 悬空检查只看 material_id / extra_material_refs(不含 keyframe_refs)
    ref_by_type: dict[str, set[str]] = {}
    track_rows: list[dict[str, Any]] = []
    seg_counts: dict[str, int] = {}
    for t in tracks:
        ttype = str(t.get("type") or "?")
        segs = [s for s in (t.get("segments") or []) if isinstance(s, dict)]
        seg_counts[ttype] = seg_counts.get(ttype, 0) + len(segs)
        type_refs = ref_by_type.setdefault(ttype, set())
        rows = []
        for s in segs:
            mid = s.get("material_id")
            if mid:
                type_refs.add(str(mid))
            refs = [str(r) for r in (s.get("extra_material_refs") or []) if r]
            type_refs.update(refs)
            for kr in (s.get("keyframe_refs") or []):
                if kr:
                    referenced.add(str(kr))
                    type_refs.add(str(kr))
            tr = s.get("target_timerange") or {}
            rows.append({
                "id": s.get("id"),
                "material_id": mid,
                "start_us": tr.get("start", s.get("target_start_us")),
                "duration_us": tr.get("duration"),
                "extra_refs": len(refs),
                "volume": s.get("volume"),
            })
        track_rows.append({
            "type": ttype, "id": t.get("id"), "name": t.get("name"),
            "segments": len(segs), "rows": rows,
        })

    buckets: dict[str, Any] = {}
    shape_warnings: list[str] = []
    placeholders: list[str] = []
    for name, entries in materials.items():
        if not isinstance(entries, list) or not entries:
            continue
        ids = [str(e.get("id")) for e in entries if isinstance(e, dict) and e.get("id")]
        orphan = [i for i in ids if i not in referenced]
        buckets[name] = {
            "total": len(entries),
            "referenced": len(ids) - len(orphan),
            "orphan": len(orphan),
            "orphan_ids_sample": orphan[:4],
        }
        if name in REQUIRED_KEYS:
            need = REQUIRED_KEYS[name]
            for e in entries:
                if isinstance(e, dict):
                    miss = [k for k in need if k not in e]
                    if miss:
                        shape_warnings.append(f"{name}[{e.get('id')}] 缺少键 {miss}")
        if name in CONTENT_BUCKETS:
            for e in entries:
                if isinstance(e, dict):
                    for k, v in e.items():
                        if isinstance(v, str) and v.startswith("PLACEHOLDER"):
                            placeholders.append(f"{name}[{e.get('id')}].{k}={v}")

    def _ids(bucket: str) -> set[str]:
        return {str(e.get("id")) for e in (materials.get(bucket) or []) if isinstance(e, dict) and e.get("id")}

    def ref_count(bucket: str) -> int:
        """被「对的轨道类型」的片段引用的条目数(渲染口径)。"""
        ok: set[str] = set()
        for tt in EXPECT_TRACKS.get(bucket, ()):
            ok |= ref_by_type.get(tt, set())
        return len(_ids(bucket) & ok)

    def misplaced(bucket: str) -> int:
        ok: set[str] = set()
        for tt in EXPECT_TRACKS.get(bucket, ()):
            ok |= ref_by_type.get(tt, set())
        return len((_ids(bucket) & referenced) - ok)

    for _name, _b in buckets.items():
        if _name in EXPECT_TRACKS:
            _b["misplaced"] = misplaced(_name)

    orphan_total = sum(b["orphan"] for n, b in buckets.items() if n in CONTENT_BUCKETS)

    # 悬空引用:片段指向的 id 在任何素材桶(含伴随桶)里都不存在 → 剪映找不到素材
    all_material_ids: set[str] = set()
    for entries in materials.values():
        if isinstance(entries, list):
            all_material_ids.update(str(e["id"]) for e in entries if isinstance(e, dict) and e.get("id"))
    dangling = sorted(material_refs - all_material_ids)

    metrics = {
        "video_segments": seg_counts.get("video", 0),
        "audio_segments": seg_counts.get("audio", 0),
        "text_segments": seg_counts.get("text", 0),
        "sticker_segments": seg_counts.get("sticker", 0),
        "effect_segments": seg_counts.get("effect", 0),
        "transitions_referenced": ref_count("transitions"),
        "video_effects_referenced": ref_count("video_effects"),
        "text_animations_referenced": ref_count("material_animations"),
        "stickers_referenced": ref_count("stickers"),
        "audio_fades_referenced": ref_count("audio_fades"),
        "orphan_materials": orphan_total,
        "misplaced_references": sum(b.get("misplaced", 0) for b in buckets.values()),
        "dangling_references": len(dangling),
        "placeholder_values": len(placeholders),
        "shape_warnings": len(shape_warnings),
    }
    texts = [_text_plain(m) for m in (materials.get("texts") or []) if isinstance(m, dict)]
    return {
        "duration_us": draft.get("duration"),
        "canvas": draft.get("canvas_config"),
        "native_marker": "new_version" in draft,
        "tracks": track_rows,
        "buckets_nonempty": buckets,
        "texts_plain": texts,
        "metrics": metrics,
        "shape_warnings": shape_warnings,
        "placeholders": placeholders,
        "dangling_ids_sample": dangling[:4],
    }


def read_sidecar(draft_file: Path) -> dict[str, Any] | None:
    """读草稿目录旁的 sidecar 文件;不存在/不可解析返回 ``None``。"""
    side = Path(draft_file).parent / SIDECAR_NAME
    if not side.is_file():
        return None
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sidecar_summary(sc: dict[str, Any]) -> list[str]:
    """sidecar 内容 → 人读摘要行(dropped 键逐项给计数)。"""
    dropped = sc.get("dropped") or {}
    lines = [f"-- sidecar({SIDECAR_NAME})里记录的「没能写进草稿」的内容"]
    if not dropped:
        lines.append("  dropped 为空")
        return lines
    for key, val in dropped.items():
        if key == "dropped_text_track":
            segs = (val or {}).get("segments") or []
            preview = [str(s.get("text", ""))[:30] for s in segs if isinstance(s, dict)]
            lines.append(f"  dropped_text_track: {len(segs)} 段文字被丢弃,开头预览 {preview[:2]}")
        elif key == "dropped_audio_segments":
            lines.append(f"  dropped_audio_segments: {len(val)} 段音频被丢弃(引用的是资源 ID / 找不到本地文件)")
        elif key == "unresolved_audio_refs":
            lines.append(f"  unresolved_audio_refs: {json.dumps(val, ensure_ascii=False)[:200]}")
        elif isinstance(val, (dict, list)):
            lines.append(f"  {key}: {len(val)} 项")
        else:
            lines.append(f"  {key}: {val}")
    return lines


def render(report: dict[str, Any], label: str, path: Path) -> str:
    """报告 → 人读文本(CLI 默认输出)。"""
    L: list[str] = []
    L.append(f"=== {label}  ({path})")
    L.append(f"duration_us={report['duration_us']}  canvas={report['canvas']}  原生稿标记={report['native_marker']}")
    L.append("-- 轨道")
    for t in report["tracks"]:
        L.append(f"  [{t['type']}] id={t['id']} name={t['name']!r} segments={t['segments']}")
        for r in t["rows"][:6]:
            L.append(
                f"      seg id={r['id']} mat={r['material_id']} start={r['start_us']} dur={r['duration_us']} "
                f"extra_refs={r['extra_refs']} vol={r['volume']}"
            )
        if len(t["rows"]) > 6:
            L.append(f"      ... 共 {len(t['rows'])} 段")
    L.append("-- 非空素材桶(总数 / 被引用 / 孤儿)")
    for name in CONTENT_BUCKETS:
        b = report["buckets_nonempty"].get(name)
        if b:
            mis = f" misplaced={b['misplaced']}" if b.get("misplaced") else ""
            L.append(f"  {name:<20} total={b['total']:<3} referenced={b['referenced']:<3} orphan={b['orphan']:<3}{mis} {b['orphan_ids_sample'] or ''}")
    other = {k: v for k, v in report["buckets_nonempty"].items() if k not in CONTENT_BUCKETS}
    if other:
        L.append("  (伴随桶) " + ", ".join(f"{k}={v['total']}" for k, v in other.items()))
    if report["texts_plain"]:
        L.append(f"-- 文字素材纯文本: {report['texts_plain'][:4]}")
    L.append("-- 用户在剪映里能看到的(按『被引用』口径)")
    for k, label_zh in METRIC_LABELS.items():
        L.append(f"  {label_zh}: {report['metrics'][k]}")
    for w in report["shape_warnings"][:6]:
        L.append(f"  [形状告警] {w}")
    for p in report["placeholders"][:6]:
        L.append(f"  [占位符] {p}")
    for d in report["dangling_ids_sample"]:
        L.append(f"  [悬空引用] {d}")
    return "\n".join(L)


__all__ = [
    "SIDECAR_NAME", "CONTENT_BUCKETS", "REQUIRED_KEYS", "EXPECT_TRACKS",
    "METRIC_LABELS", "load_draft_target", "collect_referenced_ids", "orphan_ids",
    "inspect", "read_sidecar", "sidecar_summary", "render",
]

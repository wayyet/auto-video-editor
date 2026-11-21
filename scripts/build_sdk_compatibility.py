"""阶段0交付物:项目资源库 × pyJianYingDraft SDK 0.3.1 metadata 资源交叉表。

按迁移设计文档 §4.6/§9阶段0 的要求,把 ``templates/*_resource_library.json`` 与
``templates/fx_template.json`` 池的每一条资源,与 vendored SDK
(``vendor/pyJianYingDraft/sdk/pyJianYingDraft/metadata``) 的枚举做 **ID 级**
映射,产出 ``templates/sdk_resource_compatibility.json``:

- ``supported``:effect_id+resource_id 双 ID 与 SDK metadata 一致(名称不一致时
  标 ``supported_name_drift`` 并附两边名称)。
- ``unsupported_missing_in_sdk``:SDK 0.3.1 枚举里没有该 effect_id。
- ``unsupported_resource_id_mismatch``:effect_id 命中但 resource_id 不同。
- ``needs_sdk_extension``:花字 effect_id != resource_id(SDK
  ``TextSegment.add_effect(effect_id)`` 把 effect_id 同时当 resource_id 用,
  双 ID 不同的资源无法无损表达)。
- ``unsupported_placeholder``:贴纸占位 ID(sticker_template.json 的
  ``PLACEHOLDER_*``,fail-closed,见迁移文档 §4.7)。

映射键一律是 ID,禁止按名称猜映射(迁移文档 §13.2);名称只做一致性核对。
池(transition_pools / video_effects 白名单 / transition_excluded)按"名称 →
库行 → ID → SDK 成员"解析,池条目状态继承其库行状态。

用法::

    .venv/Scripts/python.exe scripts/build_sdk_compatibility.py
    # 可选 --output <path> 默认 templates/sdk_resource_compatibility.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = REPO_ROOT / "vendor" / "pyJianYingDraft" / "sdk"

#: 上游锁定 commit(PROVENANCE.md 同源;禁止浮动 main)
SDK_COMMIT = "1e228479dda361e312e85f6037002346cf6dca9a"
SDK_VERSION = "0.3.1"

#: 分类 → (资源库文件, 列名, SDK 枚举名, 转场特有字段)
# flower / sticker 不走枚举映射,单独处理。
_ENUM_CATEGORIES = [
    ("transition", "fx_resource_library.json", "transitions", "TransitionType"),
    ("video_effect", "fx_resource_library.json", "video_effects", None),  # 场景+人物两枚举合并索引
    ("text_intro", "text_resource_library.json", "intro", "TextIntro"),
    ("text_loop", "text_resource_library.json", "loop", "TextLoopAnim"),
    ("text_outro", "text_resource_library.json", "outro", "TextOutro"),
]


def _bootstrap_sdk() -> None:
    sys.path.insert(0, str(SDK_ROOT))


def _load_sdk_enums():
    """返回 (枚举名 → {effect_id: [meta,...]}),video_effect 合并场景/人物两枚举。"""
    from pyJianYingDraft import (
        GroupAnimationType,
        IntroType,
        OutroType,
        TextIntro,
        TextLoopAnim,
        TextOutro,
        TransitionType,
        VideoCharacterEffectType,
        VideoSceneEffectType,
    )

    def index(enum_type) -> dict[str, list]:
        out: dict[str, list] = {}
        for member in enum_type:
            meta = member.value
            out.setdefault(str(meta.effect_id), []).append(meta)
        return out

    return {
        "TransitionType": index(TransitionType),
        "VideoSceneEffectType": index(VideoSceneEffectType),
        "VideoCharacterEffectType": index(VideoCharacterEffectType),
        "IntroType": index(IntroType),
        "OutroType": index(OutroType),
        "GroupAnimationType": index(GroupAnimationType),
        "TextIntro": index(TextIntro),
        "TextLoopAnim": index(TextLoopAnim),
        "TextOutro": index(TextOutro),
    }


def _meta_name(meta) -> str:
    """EffectMeta 用 ``.name``,AnimationMeta 用 ``.title``。"""
    return str(getattr(meta, "name", None) or getattr(meta, "title", "") or "")


def _sdk_meta_summary(meta) -> dict:
    out = {
        "sdk_name": _meta_name(meta),
        "sdk_effect_id": str(meta.effect_id),
        "sdk_resource_id": str(meta.resource_id),
        "sdk_is_vip": bool(getattr(meta, "is_vip", False)),
    }
    if hasattr(meta, "is_overlap"):
        out["sdk_is_overlap"] = bool(meta.is_overlap)
        out["sdk_default_duration_us"] = int(meta.default_duration)
    if hasattr(meta, "duration"):
        out["sdk_default_duration_us"] = int(meta.duration)
    return out


def _classify(records: list[dict], sdk_indexes: dict, enum_names: list[str]) -> dict:
    """对一组库行做 SDK 映射分类。enum_names 为 None 时表示场景+人物合并索引。"""
    items: list[dict] = []
    status_counter: Counter[str] = Counter()
    for rec in records:
        effect_id = str(rec.get("effect_id") or "")
        resource_id = str(rec.get("resource_id") or "")
        name = str(rec.get("name") or "")
        matches: list = []
        for enum_name in enum_names:
            matches.extend(sdk_indexes.get(enum_name, {}).get(effect_id, []))
        if not matches:
            status = "unsupported_missing_in_sdk"
        else:
            exact = [m for m in matches if str(m.resource_id) == resource_id]
            if exact:
                meta = exact[0]
                status = (
                    "supported" if _meta_name(meta) == name else "supported_name_drift"
                )
            else:
                status = "unsupported_resource_id_mismatch"
                meta = matches[0]
        status_counter[status] += 1
        item: dict = {
            "name": name,
            "effect_id": effect_id,
            "resource_id": resource_id,
            "is_vip": bool(rec.get("is_vip")),
            "status": status,
        }
        if rec.get("md5"):
            item["md5"] = str(rec["md5"])
        if status in ("supported", "supported_name_drift", "unsupported_resource_id_mismatch"):
            item["sdk_member"] = _sdk_meta_summary(meta)
            if status == "supported_name_drift":
                item["project_name"] = name
        if status == "supported":
            # 字段级核对:转场 is_overlap / 默认时长与库行一致性
            diffs = {}
            if "default_duration_s" in rec and hasattr(meta, "default_duration"):
                lib_us = int(round(float(rec["default_duration_s"]) * 1_000_000))
                if lib_us != int(meta.default_duration):
                    diffs["default_duration"] = {
                        "library_us": lib_us,
                        "sdk_us": int(meta.default_duration),
                    }
            if "is_overlap" in rec and hasattr(meta, "is_overlap"):
                if bool(rec["is_overlap"]) != bool(meta.is_overlap):
                    diffs["is_overlap"] = {
                        "library": bool(rec["is_overlap"]),
                        "sdk": bool(meta.is_overlap),
                    }
            if diffs:
                item["field_diffs"] = diffs
        items.append(item)
    return {"total": len(records), **dict(status_counter), "items": items}


def _classify_flower(records: list[dict]) -> dict:
    """花字:SDK ``add_effect(effect_id)`` 双 ID 合一,资源库行须 effect_id==resource_id。"""
    items = []
    status_counter: Counter[str] = Counter()
    for rec in records:
        effect_id = str(rec.get("effect_id") or "")
        resource_id = str(rec.get("resource_id") or "")
        status = (
            "supported" if effect_id and effect_id == resource_id else "needs_sdk_extension"
        )
        status_counter[status] += 1
        items.append({
            "name": str(rec.get("name") or ""),
            "effect_id": effect_id,
            "resource_id": resource_id,
            "is_vip": bool(rec.get("is_vip")),
            "status": status,
            "sdk_api": "TextSegment.add_effect(effect_id) — effect_id 同时作为 resource_id",
        })
    return {"total": len(records), **dict(status_counter), "items": items}


def _classify_sticker(sticker_template: dict) -> dict:
    """贴纸:PLACEHOLDER_* 一律 unsupported_placeholder(fail-closed)。"""
    by_keyword = sticker_template.get("sticker_by_keyword") or {}
    items = []
    for keyword, rec in sorted(by_keyword.items()):
        resource_id = str(rec.get("resource_id") or "") if isinstance(rec, dict) else ""
        items.append({
            "keyword": keyword,
            "resource_id": resource_id,
            "status": "unsupported_placeholder",
        })
    return {"total": len(items), "unsupported_placeholder": len(items), "items": items}


def _resolve_pools(fx_template: dict, library: dict, categories: dict) -> dict:
    """池条目(按名称引用)→ 库行 → 交叉表状态继承。"""
    trans_by_name: dict[str, list[dict]] = {}
    for rec in library.get("transitions") or []:
        trans_by_name.setdefault(str(rec.get("name") or ""), []).append(rec)
    vfx_by_name: dict[str, list[dict]] = {}
    for rec in library.get("video_effects") or []:
        vfx_by_name.setdefault(str(rec.get("name") or ""), []).append(rec)

    def status_of(rec: dict | None, kind: str) -> str:
        if rec is None:
            return "not_in_library"
        for item in categories[kind]["items"]:
            if (
                item["effect_id"] == str(rec.get("effect_id") or "")
                and item["resource_id"] == str(rec.get("resource_id") or "")
            ):
                return item["status"]
        return "not_in_library"

    pools_out: dict[str, dict] = {}
    for pool_name, members in (fx_template.get("transition_pools") or {}).items():
        if pool_name.startswith("_"):
            continue
        resolved = []
        for member_name in members or []:
            recs = trans_by_name.get(str(member_name)) or []
            rec = recs[0] if recs else None
            resolved.append({
                "name": member_name,
                "status": status_of(rec, "transition"),
                "effect_id": str(rec.get("effect_id") or "") if rec else None,
                "resource_id": str(rec.get("resource_id") or "") if rec else None,
            })
        pools_out[pool_name] = {
            "total": len(resolved),
            "statuses": dict(Counter(r["status"] for r in resolved)),
            "items": resolved,
        }
    vfx_whitelist = []
    for entry in fx_template.get("video_effects") or []:
        recs = vfx_by_name.get(str(entry.get("name") or "")) or []
        rec = recs[0] if recs else None
        vfx_whitelist.append({
            "name": entry.get("name"),
            "intensity": entry.get("intensity"),
            "status": status_of(rec, "video_effect"),
            "effect_id": str(rec.get("effect_id") or "") if rec else None,
            "resource_id": str(rec.get("resource_id") or "") if rec else None,
        })
    excluded = []
    for member_name in fx_template.get("transition_excluded") or []:
        recs = trans_by_name.get(str(member_name)) or []
        excluded.append({
            "name": member_name,
            "in_library": bool(recs),
        })
    return {
        "transition_pools": pools_out,
        "video_effects_whitelist": {
            "total": len(vfx_whitelist),
            "statuses": dict(Counter(r["status"] for r in vfx_whitelist)),
            "items": vfx_whitelist,
        },
        "transition_excluded": {
            "total": len(excluded),
            "statuses": dict(Counter(r["in_library"] for r in excluded)),
            "items": excluded,
        },
    }


def build(output: Path) -> dict:
    _bootstrap_sdk()
    sdk_indexes = _load_sdk_enums()

    library = json.loads((REPO_ROOT / "templates" / "fx_resource_library.json").read_text(encoding="utf-8"))
    text_library = json.loads((REPO_ROOT / "templates" / "text_resource_library.json").read_text(encoding="utf-8"))
    flower_library = json.loads((REPO_ROOT / "templates" / "text_flower_resource_library.json").read_text(encoding="utf-8"))
    sticker_template = json.loads((REPO_ROOT / "templates" / "sticker_template.json").read_text(encoding="utf-8"))
    fx_template = json.loads((REPO_ROOT / "templates" / "fx_template.json").read_text(encoding="utf-8"))

    categories: dict[str, dict] = {}
    for cat, lib_file, list_key, enum_names in _ENUM_CATEGORIES:
        lib = library if lib_file == "fx_resource_library.json" else text_library
        records = lib.get(list_key) or []
        if cat == "video_effect":
            # 场景+人物两枚举合并索引(视频特效不区分入池时归属)
            categories[cat] = _classify(records, sdk_indexes, ["VideoSceneEffectType", "VideoCharacterEffectType"])
        else:
            categories[cat] = _classify(records, sdk_indexes, [enum_names])

    categories["flower"] = _classify_flower(flower_library.get("flowers") or [])
    categories["sticker"] = _classify_sticker(sticker_template)

    pools = _resolve_pools(fx_template, library, categories)

    summary = {}
    for cat, block in categories.items():
        statuses = {k: v for k, v in block.items() if k not in ("items", "total")}
        summary[cat] = {"total": block.get("total", 0), **statuses}

    report = {
        "_comment": "阶段0资源交叉表:项目资源库 × pyJianYingDraft SDK 0.3.1 metadata(ID 级映射,禁止按名称猜映射)。由 scripts/build_sdk_compatibility.py 生成,勿手改。",
        "_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sdk": {
            "repo": "https://github.com/GuanYixuan/pyJianYingDraft",
            "commit": SDK_COMMIT,
            "version": SDK_VERSION,
            "vendored_package": "vendor/pyJianYingDraft/sdk/pyJianYingDraft",
        },
        "sdk_enum_sizes": {name: sum(len(v) for v in idx.values()) for name, idx in sdk_indexes.items()},
        "categories": categories,
        "pools": pools,
        "summary": summary,
    }

    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 SDK 资源兼容交叉表")
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "templates" / "sdk_resource_compatibility.json"),
    )
    args = parser.parse_args()
    report = build(Path(args.output))
    print(f"written: {args.output}")
    for cat, block in report["summary"].items():
        statuses = ", ".join(f"{k}={v}" for k, v in block.items() if k != "total")
        print(f"  {cat}: total={block['total']} {statuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

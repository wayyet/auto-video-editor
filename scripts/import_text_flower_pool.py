"""花字资源池离线导入工具(2026-11-21 审查修订版执行计划阶段 2 / R-02)。

从**已入库的脱敏证据 JSON**(docs/第七期开发/花字审计证据_20261118/
rpdb_flower_record.json 的 ``authoritative_own_item_records``)导入剪映原生花字
元数据,按 ``(effect_id, resource_id, md5)`` 资源身份**去重**(R-02:证据里 2 条
记录是同一资源「互动立体」,去重后仅 1 个唯一资源),生成
``templates/text_flower_resource_library.json``。

设计约束(审查 §4.1/§4.2):
- 运行时**不读取** rp.db(它属剪映本机缓存实现,可能随版本改变);rp.db 的
  只读正规查询只在采集时做过(见证据目录采集说明),本工具只消费其脱敏产物。
- ``verified`` 只授予有 GUI 实证证据的资源(互动立体:2026-11-18 UI 应用 +
  保存 + 重开 + 缓存清单实证);未实测资源一律 ``verified:false``,不进入可用候选。
- ``verification.cache_manifest_sha256`` 由本机实际缓存目录经
  :func:`jy_common.cache_manifest.build_cache_manifest` 现算(可用性守卫的
  完整性比对基准,审查 R-05);缓存缺失时留空 —— 运行时守卫按
  ``cache_unverified`` 处理,不静默当作可用。
- 授权字段(is_vip/paid_type)只入池元数据,**不代表当前账号订阅状态**(R-11)。

用法::

    python scripts/import_text_flower_pool.py            # 生成/覆盖池文件
    python scripts/import_text_flower_pool.py --dry-run  # 只打印解析结果
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.stdout.reconfigure(encoding="utf-8")

#: 已入库的脱敏证据(唯一合法数据源;真实 rp.db 不进仓库、运行时不依赖)
EVIDENCE_PATH = (
    _REPO_ROOT / "docs" / "第七期开发" / "花字审计证据_20261118" / "rpdb_flower_record.json"
)
POOL_PATH = _REPO_ROOT / "templates" / "text_flower_resource_library.json"

#: GUI 实证资源(2026-11-18 console-52373c6f9e11 中文稿 UI 应用「互动立体」,
#: 保存 + 重开存活,两次 UI 保存条目数不变;证据 = zh_draft_flower_extract.json
#: + flower_cache_inventory.json + 采集说明.md 时间线)
_VERIFIED_NAMES = {"互动立体"}
_VERIFIED_EVIDENCE = "docs/第七期开发/花字审计证据_20261118/"
_VERIFIED_NOTE = (
    "草稿52373c6f9e11中文稿GUI应用「互动立体」(effect_id 7407340226189757733):"
    "materials.effects type=text_effect + text轨段refs实证,经两次UI保存存活;"
    "缓存Cache/artistEffect/7407340226189757733/c093f229a5b9529beab4d27c00bc0c4f 5文件就位"
    "(核验文档20261118,采集说明.md时间线)"
)


def _parse_auth_items(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """从脱敏证据的 authoritative_own_item_records 提取花字 item 自身字段。

    item 位于每条记录的 ``common_attr``;授权信息解析 ``business_info.json_str``
    (item **自身**的字段,不以相邻 item 位置归属 —— 证据目录记载的教训)。
    """
    items: list[dict[str, Any]] = []
    for rec in evidence.get("authoritative_own_item_records") or []:
        attr = rec.get("common_attr") or {}
        if not attr.get("effect_id"):
            continue
        paid: dict[str, Any] = {}
        try:
            paid = json.loads(attr.get("business_info", {}).get("json_str") or "{}")
        except ValueError:
            paid = {}
        extra = attr.get("extra") if isinstance(attr.get("extra"), dict) else {}
        items.append(
            {
                "name": str(attr.get("title") or ""),
                "effect_id": str(attr.get("effect_id") or ""),
                "resource_id": str(attr.get("id") or ""),
                "md5": str(attr.get("md5") or ""),
                "is_vip": bool(paid.get("is_vip") or extra.get("is_vip") or False),
                "paid_type": str(paid.get("paid_type") or ""),
                "source_snapshot": {
                    "rowid": rec.get("rowid"),
                    "url_head": rec.get("url_head"),
                    "cache_timestamp": rec.get("cache_timestamp"),
                },
            }
        )
    return items


def build_pool(
    cache_root: str | Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """生成花字资源池 dict(纯函数,便于单测;落盘由 :func:`main` 负责)。"""
    from jy_common.cache_manifest import build_cache_manifest

    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    items = _parse_auth_items(evidence)

    # 按资源身份去重(R-02:同资源多条记录只保留一条,保序)
    seen: set[tuple[str, str, str]] = set()
    flowers: list[dict[str, Any]] = []
    for item in items:
        identity = (item["effect_id"], item["resource_id"], item["md5"])
        if identity in seen:
            continue
        seen.add(identity)
        name = item["name"]
        verified = name in _VERIFIED_NAMES
        row: dict[str, Any] = {
            "name": name,
            "category_id": "panel-text-flower",
            "effect_id": item["effect_id"],
            "resource_id": item["resource_id"],
            "md5": item["md5"],
            "is_vip": item["is_vip"],
            "paid_type": item["paid_type"],
            "enabled": True,
            "verified": verified,
            "source_snapshot": item["source_snapshot"],
        }
        if verified:
            manifest = build_cache_manifest(cache_root, item["effect_id"], item["md5"])
            row["verification"] = {
                "status": "verified",
                "verified_at": "2026-11-18",
                "jianying_version": "5.9.0.11632",
                "resource_identity": {
                    "resource_id": item["resource_id"],
                    "effect_id": item["effect_id"],
                    "md5": item["md5"],
                },
                "evidence_path": _VERIFIED_EVIDENCE,
                "cache_manifest_sha256": (manifest or {}).get("manifest_sha256", ""),
                "note": _VERIFIED_NOTE,
            }
        flowers.append(row)

    return {
        "_source": (
            "docs/第七期开发/花字审计证据_20261118/rpdb_flower_record.json"
            " authoritative_own_item_records(脱敏证据离线导入,运行时不依赖 rp.db;"
            "按 (effect_id, resource_id, md5) 资源身份去重)"
        ),
        "_generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_counts": {
            "records_raw": len(items),
            "flowers_unique": len(flowers),
            "verified": sum(1 for f in flowers if f.get("verified")),
        },
        "flowers": flowers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="花字资源池离线导入(去重+GUI实证标记)")
    parser.add_argument("--dry-run", action="store_true", help="只打印解析结果,不写池文件")
    args = parser.parse_args(argv)

    from config import JIANYING_ARTIST_EFFECT_CACHE_ROOT

    pool = build_pool(JIANYING_ARTIST_EFFECT_CACHE_ROOT)
    print(
        f"解析:原始记录 {pool['_counts']['records_raw']} 条 → 唯一资源 "
        f"{pool['_counts']['flowers_unique']} 个(verified {pool['_counts']['verified']} 个)"
    )
    for row in pool["flowers"]:
        manifest_state = "有" if (row.get("verification") or {}).get("cache_manifest_sha256") else "无"
        print(
            f"  - {row['name']}: effect_id={row['effect_id']} md5={row['md5'][:8]}… "
            f"verified={row['verified']} manifest={manifest_state}"
        )
    if args.dry_run:
        return 0
    POOL_PATH.write_text(
        json.dumps(pool, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写入 {POOL_PATH.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

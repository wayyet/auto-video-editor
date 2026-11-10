"""贴纸资源库实机回填 — 完成 templates/sticker_template.json 的 _reverse_engineering_steps。

背景(阶段 6 贴纸 fail-closed 的收尾):
- ``templates/sticker_template.json`` 的 ``sticker_by_keyword`` 自 Week 3 起全是
  ``PLACEHOLDER_*`` 占位;pyJianYingDraft 不带贴纸元数据(贴纸是云端素材),
  真实 19 位 resource_id 只能由操作员在剪映实机建含贴纸样本草稿后提取。
- 本脚本从样本草稿 ``draft_content.json`` 的 ``materials.stickers`` 提取真实
  条目,按操作员给的 ``贴纸名=关键词`` 映射回填模板;回填后 node_11 自动转为
  正常写入、创建台护栏(``web/api_console.py::_sticker_library_ready``)自动放行。

用法:
    # dry-run:只列出样本草稿里的贴纸条目,不写任何文件
    python scripts/backfill_sticker_library.py --draft-dir <剪映样本草稿目录>

    # 回填(原子写;必须包含 "默认" 兜底关键词;resource_id 须为真实 19 位)
    python scripts/backfill_sticker_library.py --draft-dir DIR \
        --map "点赞爱心=赞" --map "加油鸭=加油" --map "星星闪闪=惊喜" \
        --map "通用星星=默认" --apply

注意:
- 样本草稿须保存并**完全退出剪映**后才完整落盘(内存:复验须先关剪映)。
- 路径一律走命令行参数,脚本内不嵌绝对路径;全程 UTF-8 读写。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jy_common.native_fx import is_19_digit_resource_id  # noqa: E402

DEFAULT_TEMPLATE = ROOT / "templates" / "sticker_template.json"
FALLBACK_KEYWORD = "默认"


def _load_draft_stickers(draft_dir: Path) -> list[dict]:
    """读样本草稿的 ``materials.stickers``;缺失/为空直接报错退出。"""
    content_path = draft_dir / "draft_content.json"
    if not content_path.exists():
        print(f"[backfill_sticker] 找不到 {content_path};--draft-dir 须指向剪映草稿目录", file=sys.stderr)
        raise SystemExit(1)
    with open(content_path, "r", encoding="utf-8") as f:
        draft = json.load(f)
    stickers = ((draft.get("materials") or {}).get("stickers")) or []
    if not stickers:
        print(
            f"[backfill_sticker] {content_path} 的 materials.stickers 为空 —— "
            "样本草稿里没有贴纸;请在剪映里加贴纸后保存、完全退出剪映再试",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return stickers


def _print_listing(stickers: list[dict]) -> None:
    """逐条打印贴纸条目(dry-run 输出;完整字段 JSON 供字段口径对照)。"""
    print(f"[backfill_sticker] 样本草稿共 {len(stickers)} 条贴纸:\n")
    for i, item in enumerate(stickers):
        name = str(item.get("name") or "")
        rid = str(item.get("resource_id") or "")
        ok = is_19_digit_resource_id(rid)
        print(f"  [{i}] name={name!r} resource_id={rid!r} 19位合法={ok}")
        print(f"      字段全集: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    print(
        "\n下一步:确认 贴纸名=关键词 映射后加 --map ... --apply;"
        f" 关键词须含兜底 {FALLBACK_KEYWORD!r}(node_11 对未命中字幕走兜底)"
    )


def _parse_map_args(pairs: list[str]) -> dict[str, tuple[str, str]]:
    """``贴纸名=关键词`` → {贴纸名: (关键词, 原始参数)};同名多映射报错。"""
    mapping: dict[str, tuple[str, str]] = {}
    for raw in pairs:
        if "=" not in raw:
            print(f"[backfill_sticker] --map 格式非法: {raw!r}(应为 贴纸名=关键词)", file=sys.stderr)
            raise SystemExit(1)
        name, keyword = raw.split("=", 1)
        name, keyword = name.strip(), keyword.strip()
        if not name or not keyword:
            print(f"[backfill_sticker] --map 贴纸名/关键词不能为空: {raw!r}", file=sys.stderr)
            raise SystemExit(1)
        if name in mapping:
            print(f"[backfill_sticker] 贴纸 {name!r} 被映射多次", file=sys.stderr)
            raise SystemExit(1)
        mapping[name] = (keyword, raw)
    return mapping


def _validate_mapping(
    stickers: list[dict], mapping: dict[str, tuple[str, str]]
) -> dict[str, str]:
    """校验映射后返回 关键词→resource_id;任何违规直接退出,不写模板。"""
    by_name: dict[str, dict] = {}
    for item in stickers:
        name = str(item.get("name") or "").strip()
        if name and name not in by_name:
            by_name[name] = item

    keyword_map: dict[str, str] = {}
    for name, (keyword, raw) in mapping.items():
        item = by_name.get(name)
        if item is None:
            known = ", ".join(repr(n) for n in by_name) or "(无)"
            print(
                f"[backfill_sticker] 样本草稿里没有名为 {name!r} 的贴纸({raw});实际有: {known}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        rid = str(item.get("resource_id") or "")
        if not is_19_digit_resource_id(rid):
            print(
                f"[backfill_sticker] 贴纸 {name!r} 的 resource_id={rid!r} 不是真实 19 位 ID;"
                "疑似云端资源未同步完成,请重新打开剪映确认素材可用后再回填",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if keyword in keyword_map:
            print(
                f"[backfill_sticker] 关键词 {keyword!r} 被映射到多个贴纸"
                f"({keyword_map[keyword]!r} 与 {rid!r});关键词唯一",
                file=sys.stderr,
            )
            raise SystemExit(1)
        keyword_map[keyword] = rid

    if FALLBACK_KEYWORD not in keyword_map:
        print(
            f"[backfill_sticker] 映射缺少兜底关键词 {FALLBACK_KEYWORD!r}"
            "(node_11 对未命中关键词的字幕走兜底贴纸);请补一条 --map",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return keyword_map


def _apply(keyword_map: dict[str, str], draft_dir: Path, template_path: Path) -> None:
    """回填模板(原子写):替换 sticker_by_keyword、pending 翻转、留痕。"""
    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    template["sticker_by_keyword"] = dict(keyword_map)
    template["_reverse_engineering_pending"] = False
    template["_week3_status"] = "real_backfilled"
    template["_backfilled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    template["_backfilled_from"] = draft_dir.name

    fd, tmp_path = tempfile.mkstemp(
        dir=str(template_path.parent), prefix=template_path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, template_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    print(
        f"[backfill_sticker] 已回填 {template_path}\n"
        f"  sticker_by_keyword = {json.dumps(keyword_map, ensure_ascii=False)}\n"
        "  _reverse_engineering_pending = false(node_11 转正常写入;"
        "创建台护栏自动放行;web 服务无需重启 —— 模板每次调用现读)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从剪映样本草稿提取真实贴纸 resource_id 回填 templates/sticker_template.json"
    )
    parser.add_argument(
        "--draft-dir",
        required=True,
        help="剪映样本草稿目录(内含 draft_content.json;先完全退出剪映再跑)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="贴纸名=关键词",
        help="贴纸素材名 → 模板关键词映射,可多组;关键词须含 '默认' 兜底",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行回填(缺省 dry-run:只列出条目,不写文件)",
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="模板路径(缺省 templates/sticker_template.json;测试用)",
    )
    args = parser.parse_args()

    draft_dir = Path(args.draft_dir).expanduser().resolve()
    template_path = Path(args.template).expanduser().resolve()
    stickers = _load_draft_stickers(draft_dir)

    if not args.apply:
        _print_listing(stickers)
        return 0

    mapping = _parse_map_args(args.map)
    keyword_map = _validate_mapping(stickers, mapping)
    _apply(keyword_map, draft_dir, template_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

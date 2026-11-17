"""修复存量剪映草稿的单行超宽字幕 —— 按画布宽×字号折行(默认 dry-run)。

2026-11-18 背景:AutoVideo_console-31a8a8e9edd2 真机截图实证,竖屏 1080 画布上
39 字长句单行 ≈1560px 被两端裁剪;英文分支最长 cue 157 字符 ≈2760px。流水线侧
已由 ``jy_common/subtitle_wrap.py`` + ``nodes/_subtitle_texts.make_text_material``
根治(新草稿自动折行),本脚本回填**已生成**的存量草稿。

用法::

    python scripts/fix_draft_subtitle_wrap.py --draft-dir <草稿目录> [--draft-dir ...] [--apply]

- 路径一律走命令行参数,脚本内不嵌绝对路径(仓库约定)。
- 默认 dry-run 只打印折行明细;``--apply`` 经 ``safe_write_draft`` 原子落盘
  (剪映进程互斥 / .snapshots 整目录快照 / draft_content+draft_info 双文件
  字节一致 / 校验失败整目录回退)。
- 只改原生 content 的 ``text`` 字段(内嵌 \\n)并重置 ``styles[0].range``;
  颜色/描边/加粗/字号与外层私有键一概不动。已含 \\n 的行**永不合并**
  (人工换行尊重,重复执行幂等)。
- 非原生 content / 多 style 块(range 重排有风险)/ 空文本的素材跳过并计数,
  不猜语义。

前置条件:剪映客户端必须已关闭(进程互斥 fail-closed,--apply 前显式预检)。
退出码:0 成功;1 前置条件失败;2 apply 后自校验违规。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from draft_ops.atomic_writer import safe_write_draft  # noqa: E402
from draft_ops.safe_write_guard import check_jianying_not_running  # noqa: E402
from jy_common.subtitle_wrap import (  # noqa: E402
    LINE_WIDTH_RATIO,
    estimate_line_px,
    max_line_units,
    wrap_subtitle_text,
)


def _parse_content(material: dict[str, Any]) -> dict[str, Any] | None:
    """原生 content(JSON 字符串)→ dict;非原生/多 style 块 → None。

    注意:不走 ``native_content_text``(它返回的是显示文本而非 dict),
    这里需要的是完整 content 结构(styles[0] 的 size/range 都要改)。
    """
    raw = material.get("content")
    if not isinstance(raw, str) or not raw.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    styles = data.get("styles")
    if (
        not isinstance(styles, list)
        or len(styles) != 1
        or not isinstance(styles[0], dict)
        or not isinstance(data.get("text"), str)
    ):
        return None
    return data


def _style_size(data: dict[str, Any]) -> float:
    """content styles[0].size,非法回落 5.0(与构造器默认一致)。"""
    try:
        return float(data["styles"][0].get("size") or 5.0)
    except (TypeError, ValueError):
        return 5.0


def _fix_material(
    material: dict[str, Any], canvas_width: int
) -> tuple[str, str, float] | None:
    """(旧文本, 新文本, size);无需折行/无法安全处理 → None。"""
    data = _parse_content(material)
    if data is None or not data["text"].strip():
        return None
    size = _style_size(data)
    new_text = wrap_subtitle_text(data["text"], size=size, canvas_width=canvas_width)
    if new_text == data["text"]:
        return None
    return data["text"], new_text, size


def _apply_new_text(material: dict[str, Any], new_text: str) -> None:
    """只改 text 与 range;颜色/描边/加粗/字号等样式键一概不动。"""
    data = json.loads(material["content"])
    data["text"] = new_text
    data["styles"][0]["range"] = [0, len(new_text)]
    material["content"] = json.dumps(data, ensure_ascii=False)


def _verify_dir(draft_dir: Path) -> list[str]:
    """apply 后自校验:可解析素材的每一行估宽 ≤ 画布×LINE_WIDTH_RATIO。"""
    violations: list[str] = []
    draft = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    canvas_width = int((draft.get("canvas_config") or {}).get("width") or 1080)
    limit_px = canvas_width * LINE_WIDTH_RATIO
    for mat in (draft.get("materials") or {}).get("texts") or []:
        if not isinstance(mat, dict):
            continue
        data = _parse_content(mat)
        if data is None:
            continue
        size = _style_size(data)
        for line in data["text"].split("\n"):
            px = estimate_line_px(line, size)
            if px > limit_px + 1e-6:
                violations.append(
                    f"id={mat.get('id')} 行估宽 {px:.0f}px > 上限 {limit_px:.0f}px: {line}"
                )
    return violations


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="修复存量草稿的单行超宽字幕(默认 dry-run,--apply 落盘)"
    )
    parser.add_argument(
        "--draft-dir", action="append", required=True, help="草稿目录(可多次指定)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="真实落盘(默认 dry-run 只打印明细)"
    )
    parser.add_argument(
        "--canvas-width", type=int, default=None,
        help="画布宽(缺省读草稿 canvas_config,再回落 config.CANVAS_WIDTH)",
    )
    args = parser.parse_args()

    import config

    default_canvas = int(config.CANVAS_WIDTH)

    if args.apply and check_jianying_not_running():
        print(
            "[fail-closed] 检测到剪映客户端正在运行:请先保存并关闭剪映再执行 --apply",
            file=sys.stderr,
        )
        return 1

    total_changed = 0
    for raw_dir in args.draft_dir:
        draft_dir = Path(raw_dir)
        content_file = draft_dir / "draft_content.json"
        if not content_file.is_file():
            print(f"[fail] 草稿目录不存在或无 draft_content.json: {draft_dir}", file=sys.stderr)
            return 1
        draft = json.loads(content_file.read_text(encoding="utf-8"))
        canvas_width = int(
            args.canvas_width
            or (draft.get("canvas_config") or {}).get("width")
            or default_canvas
        )
        texts = (draft.get("materials") or {}).get("texts") or []
        print(
            f"\n== {draft_dir.name}(canvas {canvas_width}px,texts {len(texts)} 条,"
            f"基准容量 {max_line_units(5.0, canvas_width)} 全角单位)=="
        )
        changed = 0
        skipped_foreign = 0
        for mat in texts:
            if not isinstance(mat, dict):
                continue
            result = _fix_material(mat, canvas_width)
            if result is None:
                if _parse_content(mat) is None:
                    skipped_foreign += 1
                    print(f"  [skip] id={mat.get('id')} 非原生 content 或多 style 块")
                continue
            old, new, size = result
            changed += 1
            new_lines = new.split("\n")
            widest = max(estimate_line_px(l, size) for l in new_lines)
            print(
                f"  [折行] id={mat.get('id')} size={size:g}"
                f" 估宽 {estimate_line_px(old, size):.0f}px → {len(new_lines)} 行"
                f"(最宽行 {widest:.0f}px)"
            )
            print(f"    旧: {old}")
            for line in new_lines:
                print(f"    新: {line}")
            if args.apply:
                _apply_new_text(mat, new)
        print(
            f"  小计: 需折行 {changed}/{len(texts)} 条"
            + (f",跳过不可安全处理 {skipped_foreign} 条" if skipped_foreign else "")
            + ("(已写入)" if args.apply and changed else "(dry-run 未落盘)" if not args.apply else "(无需写入)")
        )

        if args.apply and changed:
            result = safe_write_draft(draft_dir, draft)
            if not result.get("ok"):
                print(f"[fail] safe_write_draft 失败: {result}", file=sys.stderr)
                return 1
            print(f"  已原子双写 draft_content.json + draft_info.json(快照 {result.get('snapshot')})")
            violations = _verify_dir(draft_dir)
            if violations:
                for v in violations:
                    print(f"  [self-check FAIL] {v}", file=sys.stderr)
                return 2
            print("  自校验通过:所有行估宽 ≤ 画布×0.78")
        total_changed += changed

    print(f"\n完成:{'已落盘' if args.apply else 'dry-run'};共需折行 {total_changed} 条")
    if not args.apply:
        print("确认无误后加 --apply 落盘(执行前先关闭剪映)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

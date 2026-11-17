"""修复存量剪映草稿的单行超宽字幕 —— 按画布宽×字号折行(默认 dry-run)。

2026-11-18 背景:AutoVideo_console-31a8a8e9edd2 真机截图实证,竖屏 1080 画布上
39 字长句单行 ≈1560px 被两端裁剪;英文分支最长 cue 157 字符 ≈2760px。流水线侧
已由 ``jy_common/subtitle_wrap.py`` + ``nodes/_subtitle_texts.make_text_material``
在生成期自动折行(按当前标定模型保守折行,最终以剪映真机渲染为准),本脚本
回填**已生成**的存量草稿。

用法::

    python scripts/fix_draft_subtitle_wrap.py --draft-dir <草稿目录> [--draft-dir ...] [--apply]

- 路径一律走命令行参数,脚本内不嵌绝对路径(仓库约定)。
- 默认 dry-run 只打印折行明细;``--apply`` 经 ``safe_write_draft`` 原子落盘。

执行四步(设计审查 20261118 §5.3):

1. **Preflight** — 全部目录先预检(存在性 / draft_content.json 可解析 /
   顶层 dict),``--apply`` 时再做三态进程门禁(running / unknown 一律拒绝);
   任何目录不过 → 整体拒绝,**不开始写入**(P1-06)。
2. **Plan** — 只在内存中生成修改计划:每条素材旧文本/新文本/字号/估宽/
   跳过原因/布局告警;dry-run 到此为止。
3. **Apply** — 每份草稿**独立事务**:内存改写 → ``safe_write_draft``(快照 +
   双写 + 双文件一致性 + **布局自校验,违规在事务内整目录回滚**)(P1-04)。
4. **Verify** — 在事务内完成;成功后打印快照路径,事务后无需二次校验。

语义要点:
- 多目录 = **每份草稿独立提交**,不是跨目录整体原子事务;中途失败时已成功
  的目录不回退,结尾汇总"已落盘 / 已回滚 / 失败"明细(P1-06)。
- 布局自校验与折行用**同一画布口径**(优先级:--canvas-width > 草稿
  canvas_config.width > config.CANVAS_WIDTH),消除旧版"折行一个口径、自校验
  另一个口径"的不一致。
- 只改原生 content 的 ``text`` 字段(内嵌 \\n)并重置 ``styles[0].range``;
  颜色/描边/加粗/字号与外层私有键一概不动。已含 \\n 的行**永不合并**
  (人工换行尊重,重复执行幂等)。断点处恰好吸收一个空格作换行分隔,
  其余空格/Tab 原样保留(20261118 P2-01)。
- 非原生 content / 多 style 块 / 空文本 / 无需折行的素材**分类计数**,不猜语义
  (unsupported_format / multi_style / empty / unchanged / wrapped;§4.2 P2-04)。

前置条件:剪映客户端必须已关闭(进程状态门禁 fail-closed:检测失败/未知
一律拒绝写入,设计审查 P1-02)。

退出码:0 成功(含 dry-run);1 前置条件失败或写入失败;2 布局自校验失败
(**已回滚**,草稿保持原状)。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from draft_ops.atomic_writer import safe_write_draft  # noqa: E402
from draft_ops.safe_write_guard import (  # noqa: E402
    PROC_RUNNING,
    PROC_UNKNOWN,
    probe_jianying_state,
)
from jy_common.subtitle_wrap import (  # noqa: E402
    LINE_WIDTH_RATIO,
    estimate_line_px,
    layout_warnings,
    max_line_units,
    wrap_subtitle_text,
)

# 跳过/折行分类(20261118 审查 §4.2 P2-04:dry-run 汇总与文档口径一致)
KIND_WRAPPED = "wrapped"
KIND_EMPTY = "empty"
KIND_UNCHANGED = "unchanged"
KIND_UNSUPPORTED = "unsupported_format"
KIND_MULTI_STYLE = "multi_style"
KIND_NON_DICT = "non_dict"

#: 布局违规的失败标记(用于把退出码 2 与其他写入失败区分开)
_LAYOUT_FAIL_TAG = "布局自校验违规"


def _parse_content_ex(
    material: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """原生 content(JSON 字符串)→ (dict, None);不可安全处理 → (None, 原因)。

    原因 ∈ {unsupported_format(非字符串/非 JSON/非 dict), multi_style(styles
    块数 ≠ 1 或结构异常)} —— 两种风险不同:前者根本不是折行能处理的形态,
    后者 range 重排有破坏多样式渲染的风险,分开计数便于人工复查。

    注意:不走 ``native_content_text``(它返回的是显示文本而非 dict),
    这里需要的是完整 content 结构(styles[0] 的 size/range 都要改)。
    """
    raw = material.get("content")
    if not isinstance(raw, str) or not raw.lstrip().startswith("{"):
        return None, KIND_UNSUPPORTED
    try:
        data = json.loads(raw)
    except ValueError:
        return None, KIND_UNSUPPORTED
    if not isinstance(data, dict):
        return None, KIND_UNSUPPORTED
    styles = data.get("styles")
    if (
        not isinstance(styles, list)
        or len(styles) != 1
        or not isinstance(styles[0], dict)
        or not isinstance(data.get("text"), str)
    ):
        return None, KIND_MULTI_STYLE
    return data, None


def _parse_content(material: dict[str, Any]) -> dict[str, Any] | None:
    """兼容旧签名的薄包装(布局校验用);要跳过原因请用 :func:`_parse_content_ex`。"""
    return _parse_content_ex(material)[0]


def _style_size(data: dict[str, Any]) -> float:
    """content styles[0].size;非法/NaN/Inf/非正数回落 5.0(与构造器默认一致,P2-06)。"""
    try:
        size = float(data["styles"][0].get("size") or 5.0)
    except (TypeError, ValueError):
        return 5.0
    if not math.isfinite(size) or size <= 0:
        return 5.0
    return size


def _classify_material(
    material: dict[str, Any], canvas_width: int
) -> tuple[str, tuple[str, str, float] | None]:
    """素材分类 → (kind, payload)。

    kind ∈ wrapped / empty / unchanged / unsupported_format / multi_style;
    payload 仅 wrapped 有:(旧文本, 新文本, size)。
    """
    data, reason = _parse_content_ex(material)
    if data is None:
        return reason, None
    if not data["text"].strip():
        return KIND_EMPTY, None
    size = _style_size(data)
    new_text = wrap_subtitle_text(data["text"], size=size, canvas_width=canvas_width)
    if new_text == data["text"]:
        return KIND_UNCHANGED, None
    return KIND_WRAPPED, (data["text"], new_text, size)


def _apply_new_text(material: dict[str, Any], new_text: str) -> None:
    """只改 text 与 range;颜色/描边/加粗/字号等样式键一概不动。"""
    data = json.loads(material["content"])
    data["text"] = new_text
    data["styles"][0]["range"] = [0, len(new_text)]
    material["content"] = json.dumps(data, ensure_ascii=False)


def _material_duration_us(material: dict[str, Any]) -> int | None:
    """私有键 target_timerange.duration(本流水线素材才有);读不到 → None。"""
    tr = material.get("target_timerange")
    if not isinstance(tr, dict):
        return None
    try:
        value = int(tr.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _layout_violations(draft: dict[str, Any], canvas_width: int) -> list[str]:
    """对(内存或磁盘上的)draft 逐行估宽校验;返回违规列表(空 = 通过)。

    与折行共用同一 ``canvas_width`` 口径 —— 调用方必须传 ``_resolve_canvas`` 的
    结果,不允许自行从草稿重读(旧版"折行与自校验口径不一致"的根源)。
    """
    violations: list[str] = []
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


def _make_tx_verifier(canvas_width: int) -> Callable[[Path], tuple[bool, str]]:
    """事务内组合校验器(P1-04):双文件一致性 → 字幕行宽预算。

    传给 ``safe_write_draft`` 的 ``verifier`` 参数;返回 False 会在**提交前**
    触发整目录快照回滚 —— 草稿不会停留在"已折行但超宽"的中间态。
    """
    from draft_ops.verify_after_write import verify_draft_loadable

    def _verify(draft_dir: Path) -> tuple[bool, str]:
        ok, reason = verify_draft_loadable(draft_dir)
        if not ok:
            return False, reason
        draft = json.loads(
            (draft_dir / "draft_content.json").read_text(encoding="utf-8")
        )
        violations = _layout_violations(draft, canvas_width)
        if violations:
            return False, f"{_LAYOUT_FAIL_TAG}: " + "; ".join(violations[:5])
        return True, "ok"

    return _verify


def _load_draft_dir(draft_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Preflight 单目录:存在性 + JSON 可解析 + 顶层 dict。返回 (draft, error)。"""
    content_file = draft_dir / "draft_content.json"
    if not content_file.is_file():
        return None, f"草稿目录不存在或无 draft_content.json: {draft_dir}"
    try:
        draft = json.loads(content_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"draft_content.json 读取/解析失败: {draft_dir}({exc})"
    if not isinstance(draft, dict):
        return None, f"draft_content.json 顶层不是 dict: {draft_dir}"
    return draft, None


def _resolve_canvas(
    draft: dict[str, Any], canvas_width_override: int | None, default_canvas: int
) -> int:
    """折行/自校验统一画布口径:--canvas-width > 草稿 canvas_config.width > config。"""
    if canvas_width_override:
        return int(canvas_width_override)
    draft_width = int((draft.get("canvas_config") or {}).get("width") or 0)
    return draft_width if draft_width > 0 else default_canvas


def _plan_draft(draft: dict[str, Any], canvas_width: int) -> dict[str, Any]:
    """Plan:只在内存中生成修改计划,不落盘(设计审查 §5.3 第 2 步)。

    Returns:
        {"texts", "counts", "entries"};entries 元素含
        {id, mat, old, new, size, n_lines, widest_px, warnings, duration_us}。
    """
    texts = (draft.get("materials") or {}).get("texts") or []
    counts = {
        KIND_WRAPPED: 0,
        KIND_EMPTY: 0,
        KIND_UNCHANGED: 0,
        KIND_UNSUPPORTED: 0,
        KIND_MULTI_STYLE: 0,
        KIND_NON_DICT: 0,
    }
    entries: list[dict[str, Any]] = []
    for mat in texts:
        if not isinstance(mat, dict):
            counts[KIND_NON_DICT] += 1
            continue
        kind, payload = _classify_material(mat, canvas_width)
        counts[kind] += 1
        if kind != KIND_WRAPPED:
            continue
        old, new, size = payload
        new_lines = new.split("\n")
        duration_us = _material_duration_us(mat)
        entries.append(
            {
                "id": mat.get("id"),
                "mat": mat,
                "old": old,
                "new": new,
                "size": size,
                "n_lines": len(new_lines),
                "widest_px": max(estimate_line_px(line, size) for line in new_lines),
                # P2-04:布局质量告警(行数/垂直占位/阅读速度)只提示不改时间轴
                "warnings": layout_warnings(
                    new,
                    size=size,
                    canvas_width=canvas_width,
                    duration_us=duration_us,
                ),
                "duration_us": duration_us,
            }
        )
    return {"texts": texts, "counts": counts, "entries": entries}


_SKIP_LABELS = {
    KIND_EMPTY: "空文本",
    KIND_UNSUPPORTED: "非原生 content",
    KIND_MULTI_STYLE: "多 style 块",
    KIND_NON_DICT: "非 dict 条目",
}


def _print_plan(draft_dir: Path, canvas_width: int, plan: dict[str, Any]) -> None:
    """打印单目录 Plan 报告(dry-run 与 apply 共用)。"""
    counts = plan["counts"]
    texts = plan["texts"]
    print(
        f"\n== {draft_dir.name}(canvas {canvas_width}px,texts {len(texts)} 条,"
        f"基准容量 {max_line_units(5.0, canvas_width)} 全角单位)=="
    )
    for e in plan["entries"]:
        print(
            f"  [折行] id={e['id']} size={e['size']:g}"
            f" 估宽 {estimate_line_px(e['old'], e['size']):.0f}px → {e['n_lines']} 行"
            f"(最宽行 {e['widest_px']:.0f}px)"
        )
        print(f"    旧: {e['old']}")
        for line in e["new"].split("\n"):
            print(f"    新: {line}")
        for w in e["warnings"]:
            print(f"    [告警] {w}")
    for kind, label in _SKIP_LABELS.items():
        if counts[kind]:
            print(f"  [skip] {label} {counts[kind]} 条")
    if counts[KIND_UNCHANGED]:
        print(f"  [unchanged] 无需折行 {counts[KIND_UNCHANGED]} 条")


def _run_one_draft(
    draft_dir: Path,
    draft: dict[str, Any],
    *,
    apply: bool,
    canvas_width: int,
    writer: Callable[..., dict[str, Any]] | None = None,
    verifier_factory: Callable[[int], Callable[[Path], tuple[bool, str]]] | None = None,
) -> dict[str, Any]:
    """处理单份草稿:Plan →(apply 时)内存改写 → 独立事务 → 状态记录。

    writer / verifier_factory 支持依赖注入(单测不触碰真实落盘路径);
    None = 运行时解析默认实现(便于测试 monkeypatch 模块属性)。

    Returns:
        {"dir", "status", "changed", "snapshot", "error"};status ∈
        written / dry_run / no_change / rolled_back / failed。
    """
    if writer is None:
        writer = safe_write_draft
    if verifier_factory is None:
        verifier_factory = _make_tx_verifier
    plan = _plan_draft(draft, canvas_width)
    _print_plan(draft_dir, canvas_width, plan)
    changed = len(plan["entries"])
    base = {"dir": draft_dir, "changed": changed, "snapshot": None, "error": None}

    if not apply:
        return {**base, "status": "dry_run"}
    if changed == 0:
        print("  (无需写入)")
        return {**base, "status": "no_change"}

    for e in plan["entries"]:
        _apply_new_text(e["mat"], e["new"])

    # 独立事务:布局校验在事务内(P1-04),违规/异常 → 整目录回滚后 raise
    try:
        result = writer(draft_dir, draft, verifier=verifier_factory(canvas_width))
    except RuntimeError as exc:
        msg = str(exc)
        if _LAYOUT_FAIL_TAG in msg:
            print(f"  [rolled-back] {draft_dir.name}: {msg}", file=sys.stderr)
            return {**base, "status": "rolled_back", "error": msg}
        print(f"  [fail] {draft_dir.name}: {msg}", file=sys.stderr)
        return {**base, "status": "failed", "error": msg}
    except Exception as exc:  # 快照创建等裸 OSError/TypeError → 写入失败
        print(f"  [fail] {draft_dir.name}: {exc!r}", file=sys.stderr)
        return {**base, "status": "failed", "error": repr(exc)}

    print(f"  已原子双写 draft_content.json + draft_info.json(快照 {result.get('snapshot')})")
    print("  事务内自校验通过:双文件一致 + 所有行估宽 ≤ 画布×0.78")
    return {**base, "status": "written", "snapshot": result.get("snapshot")}


def _positive_int(raw: str) -> int:
    """argparse 类型:画布宽必须是正整数(P2-06;0/负数会让折行预算归 0 静默跳过)。"""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"画布宽必须是整数,收到: {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"画布宽必须为正数,收到: {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="修复存量草稿的单行超宽字幕(默认 dry-run,--apply 事务落盘)"
    )
    parser.add_argument(
        "--draft-dir", action="append", required=True, help="草稿目录(可多次指定)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="真实落盘(默认 dry-run 只打印明细)"
    )
    parser.add_argument(
        "--canvas-width", type=_positive_int, default=None,
        help="画布宽(正整数;缺省读草稿 canvas_config,再回落 config.CANVAS_WIDTH)",
    )
    args = parser.parse_args(argv)

    import config

    default_canvas = int(config.CANVAS_WIDTH)

    # ---- 1. Preflight:全部目录先预检,任何失败 → 整体拒绝,不开始写入 ----
    drafts: list[tuple[Path, dict[str, Any]]] = []
    for raw_dir in args.draft_dir:
        draft_dir = Path(raw_dir)
        draft, err = _load_draft_dir(draft_dir)
        if err:
            print(f"[preflight FAIL] {err}", file=sys.stderr)
            return 1
        drafts.append((draft_dir, draft))

    # ---- 2. 进程状态门禁(--apply;三态 fail-closed,P1-02)----
    if args.apply:
        state = probe_jianying_state()
        if state == PROC_RUNNING:
            print(
                "[fail-closed] 检测到剪映客户端正在运行:"
                "请先保存并关闭剪映再执行 --apply",
                file=sys.stderr,
            )
            return 1
        if state == PROC_UNKNOWN:
            print(
                "[fail-closed] 剪映进程状态未知(进程检测命令失败/超时/工具缺失):"
                "为避免写坏正在编辑的草稿,已拒绝执行。"
                "请人工确认剪映已关闭后重试。",
                file=sys.stderr,
            )
            return 1

    if args.apply and len(drafts) > 1:
        print(
            "[语义] 多目录 = 每份草稿独立事务(非跨目录整体原子);"
            "失败目录会回滚/记录,已成功目录不回退。"
        )

    # ---- 3. Plan + Apply(每份草稿独立事务)----
    results = []
    for draft_dir, draft in drafts:
        canvas_width = _resolve_canvas(draft, args.canvas_width, default_canvas)
        results.append(
            _run_one_draft(draft_dir, draft, apply=args.apply, canvas_width=canvas_width)
        )

    # ---- 4. 汇总 ----
    total_changed = sum(r["changed"] for r in results)
    n_written = sum(1 for r in results if r["status"] == "written")
    n_rolled = sum(1 for r in results if r["status"] == "rolled_back")
    n_failed = sum(1 for r in results if r["status"] == "failed")
    print(
        f"\n完成:{'已落盘' if args.apply else 'dry-run'};共需折行 {total_changed} 条;"
        f"目录结果: {n_written} 落盘, {n_rolled} 回滚, {n_failed} 失败"
    )
    for r in results:
        print(f"  - {Path(r['dir']).name}: {r['status']}(需折行 {r['changed']} 条)")
    if not args.apply:
        print("确认无误后加 --apply 落盘(执行前先关闭剪映)。")

    if n_failed:
        return 1
    if n_rolled:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

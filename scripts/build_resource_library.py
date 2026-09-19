"""一次性生成器 — 把 pyJianYingDraft metadata 里的 VIP 资源 dump 成 JSON。

设计目标:
- 只依赖 stdlib(ast + json),不引入 pyJianYingDraft 为运行时依赖(参考对照报告 §3)。
- 过滤 is_vip == True,产出 templates/fx_resource_library.json(转场 + 视频特效)和
  templates/text_resource_library.json(文字入场/循环/出场)。
- 幂等:同一份源文件再跑,JSON 字节级一致(同名取首条)。
- 可选 --diff 模式:与已有 JSON 比对,仅打印新增/删除,不写文件(用于代码审查)。

用法:
    python scripts/build_resource_library.py                # 写入 templates/
    python scripts/build_resource_library.py --diff         # 只打印 diff,不写
    python scripts/build_resource_library.py --source DIR   # 自定义源目录
    python scripts/build_resource_library.py --out-dir DIR  # 自定义输出目录
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------- 路径常量 ----------

# 默认源 = FireRed-OpenStoryline 仓库里 jianying-editor 技能 vendor 的 metadata 目录。
DEFAULT_SOURCE = Path(
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\.claude\skills\jianying-editor\scripts\vendor\pyJianYingDraft\metadata"
)
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "templates"


# ---------- AST 工具 ----------


def _call_name(node: ast.Call) -> str | None:
    """取 ast.Call 的函数名(支持简单 Name 与 Attribute)。"""
    func = node.func
    return getattr(func, "id", None) or getattr(func, "attr", None)


def _const(node: ast.AST) -> Any:
    """把 ast.Constant 转成 Python 原生值;否则返回其 repr 字符串(便于诊断)。"""
    if isinstance(node, ast.Constant):
        return node.value
    return ast.unparse(node)


def _parse_ctor_assigns(
    src_path: Path,
    ctor_names: Iterable[str],
    start_index: int,
    end_index: int,
    extra_dedup_keys: tuple[int, ...] = (),
) -> list[dict[str, Any]]:
    """解析 metadata 文件,提取所有形如 ``Name = Ctor(args...)`` 的赋值。

    Args:
        src_path: metadata 文件路径。
        ctor_names: 关注的构造器名集合,例如 {"TransitionMeta"}。
        start_index: 关键字段从第几个位置参数开始(0-based;TransitionMeta/EffectMeta
            的 resource_id 都是 index 2)。
        end_index: 关键字段结束位置(不含)。用于截取 [name, is_vip, resource_id, effect_id, md5, ...]。
        extra_dedup_keys: 用于"同名保留首条"去重的辅助索引列表(基于 name 的索引)。

    Returns:
        列表,每条 dict 的 key 由 end_index - start_index 决定,按位置参数顺序命名为
        f0, f1, f2...。"VIP 过滤"由调用方在拿到结果后做。
    """
    src = src_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # pragma: no cover - 直接报错给上游
        raise SystemExit(f"[build_resource_library] AST parse failed for {src_path}: {exc}") from exc

    ctor_set = set(ctor_names)
    results: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        if _call_name(node.value) not in ctor_set:
            continue
        args = node.value.args
        if len(args) < end_index:
            # 防御:args 数量不足说明源文件 schema 变了,跳过不抛
            continue
        rec = {f"f{i - start_index}": _const(args[i]) for i in range(start_index, end_index)}
        results.append(rec)
    return results


# ---------- 各资源类型提取函数 ----------


def extract_transitions(src_dir: Path) -> list[dict[str, Any]]:
    """从 transition_meta.py 抽 TransitionMeta,过滤 is_vip。"""
    rows = _parse_ctor_assigns(
        src_dir / "transition_meta.py",
        ctor_names=("TransitionMeta",),
        start_index=0,
        end_index=7,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        # r: f0=name, f1=is_vip, f2=resource_id, f3=effect_id, f4=md5, f5=default_duration(s), f6=is_overlap
        if r["f1"] is not True:
            continue
        out.append(
            {
                "name": r["f0"],
                "is_vip": True,
                "resource_id": r["f2"],
                "effect_id": r["f3"],
                "md5": r["f4"],
                "default_duration_s": r["f5"],
                "is_overlap": r["f6"],
            }
        )
    return out


def extract_video_effects(src_dir: Path) -> list[dict[str, Any]]:
    """从 video_scene_effect.py 抽 EffectMeta,过滤 is_vip。"""
    rows = _parse_ctor_assigns(
        src_dir / "video_scene_effect.py",
        ctor_names=("EffectMeta",),
        start_index=0,
        end_index=5,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["f1"] is not True:
            continue
        out.append(
            {
                "name": r["f0"],
                "is_vip": True,
                "resource_id": r["f2"],
                "effect_id": r["f3"],
                "md5": r["f4"],
            }
        )
    return out


def extract_text_animations(src_dir: Path, kind: str, filename: str) -> list[dict[str, Any]]:
    """从 text_intro.py / text_loop.py / text_outro.py 抽 AnimationMeta,过滤 is_vip。

    AnimationMeta(title, is_vip, duration, resource_id, effect_id, md5) — 第 2 位是 is_vip。
    """
    rows = _parse_ctor_assigns(
        src_dir / filename,
        ctor_names=("AnimationMeta",),
        start_index=0,
        end_index=6,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["f1"] is not True:
            continue
        out.append(
            {
                "name": r["f0"],
                "is_vip": True,
                "duration_s": r["f2"],
                "resource_id": r["f3"],
                "effect_id": r["f4"],
                "md5": r["f5"],
            }
        )
    return out


# ---------- 主流程 ----------


def _dedup_by_name(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按 name 去重,保留首条;返回 (去重后列表, 丢弃的重复计数)。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        name = row.get("name")
        if name in seen:
            dropped += 1
            continue
        seen.add(name)
        out.append(row)
    return out, dropped


def _diff_against_existing(out_dir: Path, fx_lib: dict, text_lib: dict) -> int:
    """与已有 JSON 比对,打印 + - 行;返回差异条目数(无差异返回 0)。"""
    diffs: list[str] = []

    fx_path = out_dir / "fx_resource_library.json"
    if fx_path.exists():
        existing = json.loads(fx_path.read_text(encoding="utf-8"))
        existing_tx = {r["name"] for r in existing.get("transitions", [])}
        new_tx = {r["name"] for r in fx_lib.get("transitions", [])}
        for n in sorted(new_tx - existing_tx):
            diffs.append(f"+ transitions: {n}")
        for n in sorted(existing_tx - new_tx):
            diffs.append(f"- transitions: {n}")

        existing_vfx = {r["name"] for r in existing.get("video_effects", [])}
        new_vfx = {r["name"] for r in fx_lib.get("video_effects", [])}
        for n in sorted(new_vfx - existing_vfx):
            diffs.append(f"+ video_effects: {n}")
        for n in sorted(existing_vfx - new_vfx):
            diffs.append(f"- video_effects: {n}")
    else:
        diffs.append(f"+ (new file) {fx_path.name}")

    text_path = out_dir / "text_resource_library.json"
    if text_path.exists():
        existing = json.loads(text_path.read_text(encoding="utf-8"))
        for kind in ("intro", "loop", "outro"):
            existing_set = {r["name"] for r in existing.get(kind, [])}
            new_set = {r["name"] for r in text_lib.get(kind, [])}
            for n in sorted(new_set - existing_set):
                diffs.append(f"+ text_{kind}: {n}")
            for n in sorted(existing_set - new_set):
                diffs.append(f"- text_{kind}: {n}")
    else:
        diffs.append(f"+ (new file) {text_path.name}")

    for line in diffs:
        print(line)
    return len(diffs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 pyJianYingDraft metadata 生成 templates/*_resource_library.json"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"metadata 源目录(默认 {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"输出目录(默认 {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="只打印与已有 JSON 的差异,不写文件",
    )
    args = parser.parse_args(argv)

    src_dir: Path = args.source
    out_dir: Path = args.out_dir

    if not src_dir.exists():
        print(f"[build_resource_library] source dir missing: {src_dir}", file=sys.stderr)
        return 2

    # ---- 提取 + 去重 ----
    transitions, tx_dropped = _dedup_by_name(extract_transitions(src_dir))
    video_effects, vfx_dropped = _dedup_by_name(extract_video_effects(src_dir))
    intro = _dedup_by_name(extract_text_animations(src_dir, "intro", "text_intro.py"))[0]
    loop = _dedup_by_name(extract_text_animations(src_dir, "loop", "text_loop.py"))[0]
    outro = _dedup_by_name(extract_text_animations(src_dir, "outro", "text_outro.py"))[0]

    # _generated_at:复用已有 JSON 里的时间戳(若存在),保证字节级幂等。
    fx_path_probe = out_dir / "fx_resource_library.json"
    text_path_probe = out_dir / "text_resource_library.json"
    existing_generated_at: str | None = None
    for probe in (fx_path_probe, text_path_probe):
        if probe.exists():
            try:
                existing_generated_at = json.loads(probe.read_text(encoding="utf-8")).get(
                    "_generated_at"
                )
            except (OSError, json.JSONDecodeError):
                existing_generated_at = None
            break
    generated_at = existing_generated_at or datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    source_desc = (
        "FireRed-OpenStoryline .claude/skills/jianying-editor/scripts/vendor/"
        "pyJianYingDraft/metadata"
    )

    fx_lib = {
        "_source": source_desc,
        "_source_dir": str(src_dir),
        "_generated_at": generated_at,
        "_counts": {
            "transitions_vip": len(transitions),
            "video_effects_vip": len(video_effects),
        },
        "transitions": transitions,
        "video_effects": video_effects,
    }
    text_lib = {
        "_source": source_desc,
        "_source_dir": str(src_dir),
        "_generated_at": generated_at,
        "_counts": {
            "intro_vip": len(intro),
            "loop_vip": len(loop),
            "outro_vip": len(outro),
        },
        "intro": intro,
        "loop": loop,
        "outro": outro,
    }

    # ---- diff / write ----
    if args.diff:
        n = _diff_against_existing(out_dir, fx_lib, text_lib)
        print(
            f"[diff] transitions_vip={len(transitions)} "
            f"video_effects_vip={len(video_effects)} "
            f"intro_vip={len(intro)} loop_vip={len(loop)} outro_vip={len(outro)} "
            f"diff_lines={n}",
            file=sys.stderr,
        )
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    fx_path = out_dir / "fx_resource_library.json"
    text_path = out_dir / "text_resource_library.json"
    fx_path.write_text(
        json.dumps(fx_lib, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    text_path.write_text(
        json.dumps(text_lib, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"[build_resource_library] wrote {fx_path} ({fx_path.stat().st_size} bytes); "
        f"wrote {text_path} ({text_path.stat().st_size} bytes)",
        file=sys.stderr,
    )
    print(
        f"[counts] transitions_vip={len(transitions)} "
        f"video_effects_vip={len(video_effects)} "
        f"intro_vip={len(intro)} loop_vip={len(loop)} outro_vip={len(outro)} "
        f"dropped_dup=tx:{tx_dropped}/vfx:{vfx_dropped}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

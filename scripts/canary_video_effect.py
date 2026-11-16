#!/usr/bin/env python
"""视频特效 canary 复核脚本(2026-11-17 核验报告 §6.6 第一步 / §10.2)。

对**副本草稿**做单资源、单片段的特效注入,供操作员在剪映 GUI 里按清单
验证原生特效 material 字段(apply_target_type/enable_mask 等此前"待实机
复核"的字段)在真实客户端的加载/下载/调参/保存/重开/导出行为。

流程:
  1. 整目录复制源草稿 → ``<名>_canary``(已存在则拒绝,除非 --force);
  2. 从 ``fx_resource_library.json`` **video_effects 段**精确解析特效
     (同名转场不会跨段误中,核验报告 §4.3),19 位 resource_id 校验;
  3. 在副本的第 N 个视频段(按 start 排序,默认第 1 段)注入一个
     ``vfx-canary`` material 并挂 ``extra_material_refs``;
  4. ``safe_write_draft`` 原子双写(剪映在跑时拒绝写入);
  5. 打印剪映 GUI 验证步骤清单。

用法:
  .venv/Scripts/python.exe scripts/canary_video_effect.py --draft <AutoVideo_xxx> \\
      [--effect-name 柔和辉光] [--segment-index 0] [--intensity 0.5] [--force]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from jy_common.native_fx import build_native_video_effect_material  # noqa: E402
from jy_common.template_library import load_resource_libraries  # noqa: E402
from nodes._draft_io import find_video_track, load_draft, write_draft  # noqa: E402

CANARY_SUFFIX = "_canary"
CANARY_MATERIAL_ID = "vfx-canary"

_GUI_CHECKLIST_ZH = """\
剪映 GUI 验证清单(核验报告 §10.2,逐项打勾):
  [ ] 1. 打开剪映 → 草稿库找到「{draft_name}」并打开(注意选 _canary 副本,别选原稿)
  [ ] 2. 播放器确认特效只在目标段(第 {seg_index_plus} 段)可见,画面无全片模糊/色偏/频闪
  [ ] 3. VIP 资源首次打开会联网下载:若面板提示下载/会员,确认完成
  [ ] 4. 选中特效调整强度 → 保存 → 关闭剪映 → 重新打开草稿 → 参数仍保留
  [ ] 5. 预览/导出小样:效果只出现在目标段,导出成片正常
  [ ] 6. 通过后无需操作(生产白名单已全量启用);异常则把「{effect_name}」从
         templates/fx_template.json 的 video_effects 移除并反馈
"""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="视频特效 canary 复核(副本草稿单资源单片段注入)")
    p.add_argument("--draft", required=True, help="源草稿目录(含 draft_content.json)")
    p.add_argument("--effect-name", default="柔和辉光", help="特效名(默认 柔和辉光)")
    p.add_argument("--segment-index", type=int, default=0, help="目标视频段 0-based 索引(默认 0)")
    p.add_argument("--intensity", type=float, default=None, help="强度 0-1(默认取白名单行,缺省 0.5)")
    p.add_argument("--force", action="store_true", help="覆盖已存在的 _canary 副本")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    src_dir = Path(args.draft).expanduser().resolve()
    if not src_dir.is_dir():
        print(f"[canary] ✗ 源草稿目录不存在: {src_dir}", file=sys.stderr)
        return 2
    src_content = src_dir / "draft_content.json"
    if not src_content.is_file():
        print(f"[canary] ✗ 源草稿目录缺 draft_content.json: {src_dir}", file=sys.stderr)
        return 2

    dst_dir = src_dir.parent / (src_dir.name + CANARY_SUFFIX)
    if dst_dir.exists():
        if not args.force:
            print(
                f"[canary] ✗ 副本已存在: {dst_dir}(确认无用时删除,或加 --force 覆盖)",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)
    print(f"[canary] ✓ 副本就绪: {dst_dir}")

    # 特效解析:只在资源库 video_effects 段精确命中(核验报告 §4.3)
    lib = load_resource_libraries(
        fx_template=str(REPO_ROOT / "templates" / "fx_template.json"),
        fx_resource=str(REPO_ROOT / "templates" / "fx_resource_library.json"),
        text_resource=str(REPO_ROOT / "templates" / "text_resource_library.json"),
    )
    meta = lib.pick_video_effect_by_name(args.effect_name)
    if meta is None:
        available = ", ".join(r["name"] for r in lib.video_effect_candidates())
        print(
            f"[canary] ✗ 特效 {args.effect_name!r} 不在 fx_resource_library.json 的 "
            f"video_effects 段(同名可能是转场资源)。白名单可用: {available}",
            file=sys.stderr,
        )
        return 2
    intensity = args.intensity
    if intensity is None:
        intensity = next(
            (float(r.get("intensity")) for r in lib.video_effect_candidates() if r.get("name") == args.effect_name),
            0.5,
        )
    intensity = min(max(float(intensity), 0.0), 1.0)

    # 注入:副本第 N 个视频段(按 start 排序)
    content = load_draft(src_content)
    segments = sorted(
        find_video_track(content).get("segments") or [],
        key=lambda s: int((s.get("target_timerange") or {}).get("start", 0) or 0),
    )
    if not segments:
        print("[canary] ✗ 副本草稿没有视频段", file=sys.stderr)
        return 2
    if not 0 <= args.segment_index < len(segments):
        print(
            f"[canary] ✗ --segment-index {args.segment_index} 越界(共 {len(segments)} 段)",
            file=sys.stderr,
        )
        return 2
    seg = segments[args.segment_index]
    material = build_native_video_effect_material(
        material_id=CANARY_MATERIAL_ID, meta=meta, value=intensity
    )
    content.setdefault("materials", {}).setdefault("video_effects", []).append(material)
    refs = seg.setdefault("extra_material_refs", [])
    if CANARY_MATERIAL_ID not in refs:
        refs.append(CANARY_MATERIAL_ID)

    try:
        result = write_draft(dst_dir / "draft_content.json", content)
    except RuntimeError as e:
        print(
            f"[canary] ✗ 写入被拒: {e}(剪映正在运行?请先关闭剪映再跑 canary)",
            file=sys.stderr,
        )
        return 2
    if not result.get("ok"):
        print(f"[canary] ✗ 写入失败: {result.get('reason')}", file=sys.stderr)
        return 2

    print(
        "[canary] ✓ 注入完成:\n"
        f"    副本草稿 : {dst_dir.name}\n"
        f"    特效     : {meta.get('name')} (resource_id={meta.get('resource_id')})\n"
        f"    强度     : {intensity}\n"
        f"    目标段   : 第 {args.segment_index + 1} 段 / 共 {len(segments)} 段\n"
        f"    material : {CANARY_MATERIAL_ID} → materials.video_effects + extra_material_refs\n"
    )
    print(_GUI_CHECKLIST_ZH.format(
        draft_name=dst_dir.name,
        seg_index_plus=args.segment_index + 1,
        effect_name=meta.get("name"),
    ))
    # 保存注入摘要(便于验收记录回填)
    summary_path = dst_dir / "canary_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "source_draft": str(src_dir),
                "effect_name": meta.get("name"),
                "resource_id": meta.get("resource_id"),
                "intensity": intensity,
                "segment_index": args.segment_index,
                "material_id": CANARY_MATERIAL_ID,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[canary] ✓ 验收摘要已写: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

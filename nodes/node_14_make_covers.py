"""node_14_make_covers — 三比例封面生成(Week 4 新增,对照计划 §4.2)。

并行模式:``asyncio.gather`` 节点内并发(三种比例互不依赖,不同文件名)。

子任务:
1. ``_render_cover_pillow(ratio)`` — 16:9 / 4:3 走本地 Pillow
2. ``_render_cover_9x16_via_jianying(draft_dir)`` — 9:16 走剪映(Mock:仍 Pillow)

产出命名(对齐计划 §8):
- ``<draft_dir>/covers/cover_<ratio>_zh.png``
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from draft_ops.atomic_writer_file import atomic_write_file
from mcp_clients.jianying_cover_client import call_write_cover_9x16
from state import WorkflowState


# ---------------------------------------------------------------------------
# 渲染规格常量(对齐计划 §8 + 用户文档 §9)
# ---------------------------------------------------------------------------
COVER_SPECS: dict[str, dict] = {
    "9:16": {"w": 1080, "h": 1920, "name": "9x16"},
    "16:9": {"w": 1920, "h": 1080, "name": "16x9"},
    "4:3": {"w": 1440, "h": 1080, "name": "4x3"},
}


def _default_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """找一个能用的字体,缺字体时回退到默认字体。"""
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _png_bytes(img: Image.Image) -> bytes:
    """把 PIL Image 序列化为 PNG 字节流(用于 atomic_write_file)。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _resize_and_crop(src: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """等比 resize 后 center crop 成目标尺寸。"""
    target_ratio = target_w / target_h
    src_w, src_h = src.size
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        new_h = src_h
        new_w = int(src_h * target_ratio)
    else:
        new_w = src_w
        new_h = int(src_w / target_ratio)
    src_resized = src.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return src_resized.crop((left, top, left + target_w, top + target_h))


def _load_source_frame(draft_dir: Path) -> Image.Image:
    """从草稿目录读 ``cover_source_frame.jpg``,缺则用纯色占位。"""
    source = draft_dir / "cover_source_frame.jpg"
    if source.exists():
        try:
            with Image.open(source) as img:
                img.load()
                return img.copy()
        except Exception:
            pass
    # 占位:1080x1920 纯色
    return Image.new("RGB", (1080, 1920), color=(20, 20, 28))


def _draw_title(img: Image.Image, title: str, ratio: str) -> dict:
    """在画布下 78% 位置画标题,返回 text_bbox dict。"""
    draw = ImageDraw.Draw(img)
    canvas_w, canvas_h = img.size
    font_size = int(canvas_h * 0.07)
    font = _default_font(font_size)
    bbox = draw.textbbox((0, 0), title, font=font, stroke_width=8)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    cx = (canvas_w - text_w) // 2 - bbox[0]
    cy = int(canvas_h * 0.78) - bbox[1]
    draw.text(
        (cx, cy),
        title,
        font=font,
        fill=(255, 255, 255),
        stroke_width=8,
        stroke_fill=(0, 0, 0),
    )
    return {
        "x": cx,
        "y": cy,
        "w": text_w,
        "h": text_h,
        "font_path": "<default>",
        "font_size": font_size,
    }


# ---------------------------------------------------------------------------
# 同步渲染函数(asyncio.to_thread 包装)
# ---------------------------------------------------------------------------
def _render_cover_pillow(draft_dir: Path, ratio: str) -> dict:
    """Pillow 渲染 16:9 / 4:3 封面(同步函数,供 asyncio.to_thread 调用)。

    Returns:
        CoverAsset 字典(含 zh_path 与 text_bbox)。
    """
    spec = COVER_SPECS[ratio]
    src = _load_source_frame(draft_dir)
    img = _resize_and_crop(src, spec["w"], spec["h"])
    bbox = _draw_title(img, _get_title(draft_dir), ratio)

    covers_dir = draft_dir / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    out_path = covers_dir / f"cover_{spec['name']}_zh.png"
    atomic_write_file(out_path, _png_bytes(img))
    return {"ratio": ratio, "zh_path": str(out_path), "text_bbox": bbox}


def _render_cover_9x16_sync(draft_dir: Path) -> dict:
    """9:16 封面:调 ``call_write_cover_9x16``(Week 4 Mock = Pillow)。

    Returns:
        CoverAsset 字典。
    """
    covers_dir = draft_dir / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    out_path = covers_dir / "cover_9x16_zh.png"
    title = _get_title(draft_dir)
    call_write_cover_9x16(draft_dir=str(draft_dir), title=title, output_path=out_path)

    # 读回 PNG 估算 text_bbox(简化:复用 _draw_title 的位置参数)
    spec = COVER_SPECS["9:16"]
    img = Image.open(out_path)
    bbox = _draw_title(img, title, "9:16")  # 第二次画不影响已写出的 PNG(仅作 bbox 计算)
    return {"ratio": "9:16", "zh_path": str(out_path), "text_bbox": bbox}


# ---------------------------------------------------------------------------
# 标题读取
# ---------------------------------------------------------------------------
def _get_title(draft_dir: Path) -> str:
    """从 ``draft_content.json`` 的 ``materials.texts[0].content`` 读标题,缺失时占位。"""
    draft_file = draft_dir / "draft_content.json"
    if draft_file.exists():
        try:
            draft = json.loads(draft_file.read_text(encoding="utf-8"))
            texts = draft.get("materials", {}).get("texts", [])
            if texts and texts[0].get("content"):
                return str(texts[0]["content"])
        except (json.JSONDecodeError, OSError, KeyError, IndexError):
            pass
    return "默认标题"


# ---------------------------------------------------------------------------
# 异步并发编排
# ---------------------------------------------------------------------------
async def _render_all_async(draft_dir: Path) -> list[dict]:
    """asyncio.gather 并发三种比例。"""
    results = await asyncio.gather(
        asyncio.to_thread(_render_cover_pillow, draft_dir, "16:9"),
        asyncio.to_thread(_render_cover_pillow, draft_dir, "4:3"),
        asyncio.to_thread(_render_cover_9x16_sync, draft_dir),
    )
    return list(results)


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def node_14_make_covers(state: WorkflowState) -> dict:
    """LangGraph 节点:三比例并发渲染,写 ``covers`` 到 state。

    同步包装:LangGraph ``g.invoke()`` 不支持直接调用 async 节点,所以在内部
    用 ``asyncio.run`` 跑 ``_node_14_make_covers_async``。Week 4 联调可正常使用。

    返回**只含变更字段**的 dict,避免 fan-in 时的并发写冲突。
    """
    return asyncio.run(_node_14_make_covers_async(state))


async def _node_14_make_covers_async(state: WorkflowState) -> dict:
    """异步实现 — 真正的并发渲染。"""
    draft_path = state.get("draft_path")
    if not draft_path:
        return {
            "error_log": ["[node_14] draft_path 缺失,跳过封面渲染"],
            "status_log": ["node_14_make_covers_skipped"],
            "covers": [],
        }

    draft_dir = Path(draft_path).parent
    covers = await _render_all_async(draft_dir)
    return {"covers": covers, "status_log": ["node_14_make_covers_done"]}


# 同步别名 — 兼容某些 graph 配置
def node_14_make_covers_sync(state: WorkflowState) -> dict:
    """同步版本(单测 / 老 graph 配置用)。"""
    return node_14_make_covers(state)
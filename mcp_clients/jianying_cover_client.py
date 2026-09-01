"""剪映 9:16 封面写入客户端(Week 4,对照计划 §4.2.2)。

设计:pyJianYingDraft 写 ``cover_info`` + uiautomation 触发导出。

Week 4 真实 PoC 风险(计划 §10):剪映 V7+ 可能不支持 uiautomation 触发导出,
失败则降级到 Pillow(与 16:9/4:3 同路径)。

本文件 Week 4 仅提供:
- ``JianyingCoverClient`` Protocol
- ``MockJianyingCoverClient`` — 直接调 Pillow 生成 9:16 PNG(规避剪映 UI 风险)
- ``HTTPJianyingCoverClient`` — 真实协议接入留 Week 5
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont


CANVAS_9X16_WIDTH = 1080
CANVAS_9X16_HEIGHT = 1920


class JianyingCoverClient(Protocol):
    """剪映封面客户端协议。

    ``write_cover_9x16`` 把封面 PNG 写到 ``output_path``,**等价**于 Pillow
    渲染:在 ``cover_info`` 标记写完后由剪映 UI 导出。Mock 实现直接用 Pillow
    写出等效 PNG。
    """

    def write_cover_9x16(
        self,
        *,
        draft_dir: str,
        title: str,
        output_path: Path,
    ) -> None:
        ...


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


def _draw_text_centered(
    img: Image.Image,
    text: str,
    *,
    fill=(255, 255, 255),
    stroke_fill=(0, 0, 0),
    stroke_width: int = 8,
    margin_top_ratio: float = 0.78,
) -> tuple[dict, dict]:
    """把文字画到画布下 78% 位置(剪映习惯),返回 bbox + 字体信息。"""
    draw = ImageDraw.Draw(img)
    canvas_w, canvas_h = img.size
    font = _default_font(int(canvas_h * 0.07))
    # bbox = 文字包围盒(left, top, right, bottom)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    cx = (canvas_w - text_w) // 2 - bbox[0]
    cy = int(canvas_h * margin_top_ratio) - bbox[1]
    draw.text(
        (cx, cy),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    return (
        {"x": cx, "y": cy, "w": text_w, "h": text_h, "font_path": "<default>", "font_size": font.size},
        {},
    )


class MockJianyingCoverClient:
    """Week 4 Mock — 直接用 Pillow 生成 9:16 PNG(规避剪映 UI 风险)。

    实现要点:从 ``draft_dir`` 取一张 ``cover_source_frame.jpg``,若不存在则用
    纯黑占位;按 9:16 比例 resize+crop 后画标题文字,写到 ``output_path``。
    """

    def write_cover_9x16(
        self,
        *,
        draft_dir: str,
        title: str,
        output_path: Path,
    ) -> None:
        draft_path = Path(draft_dir)
        source_frame = draft_path / "cover_source_frame.jpg"

        # 占位:用纯色背景 + 文字
        img = Image.new(
            "RGB",
            (CANVAS_9X16_WIDTH, CANVAS_9X16_HEIGHT),
            color=(20, 20, 28),
        )

        if source_frame.exists():
            try:
                with Image.open(source_frame) as src:
                    src.load()
                # resize + center crop 成 9:16
                src_resized = _resize_and_crop_9x16(src)
                img.paste(src_resized, (0, 0))
            except Exception:
                pass  # 失败 → 用纯色背景

        bbox, _ = _draw_text_centered(img, title)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, format="PNG")


def _resize_and_crop_9x16(src: Image.Image) -> Image.Image:
    """把 src 等比 resize 后 center crop 成 9:16。"""
    target_ratio = CANVAS_9X16_WIDTH / CANVAS_9X16_HEIGHT  # ≈ 0.5625
    src_w, src_h = src.size
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # src 更宽 → 按高缩放,横向裁剪
        new_h = src_h
        new_w = int(src_h * target_ratio)
    else:
        new_w = src_w
        new_h = int(src_w / target_ratio)
    src_resized = src.resize((new_w, new_h), Image.LANCZOS)
    # center crop
    left = (new_w - CANVAS_9X16_WIDTH) // 2
    top = (new_h - CANVAS_9X16_HEIGHT) // 2
    return src_resized.crop((left, top, left + CANVAS_9X16_WIDTH, top + CANVAS_9X16_HEIGHT))


class HTTPJianyingCoverClient:
    """Week 5 占位:真实 uiautomation + pyJianYingDraft 路径留待接入。"""

    def __init__(self, endpoint: str, *, timeout_s: float = 30.0) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    def write_cover_9x16(
        self,
        *,
        draft_dir: str,
        title: str,
        output_path: Path,
    ) -> None:
        raise NotImplementedError("HTTPJianyingCoverClient 留 Week 5 接入")


# ---------------------------------------------------------------------------
# 默认 client 注册(Week 4 用 Mock)
# ---------------------------------------------------------------------------
_default_client: JianyingCoverClient = MockJianyingCoverClient()


def get_default_client() -> JianyingCoverClient:
    return _default_client


def set_default_client(client: JianyingCoverClient) -> None:
    """允许单测/Week 5 替换默认客户端。"""
    global _default_client
    _default_client = client


def call_write_cover_9x16(*, draft_dir: str, title: str, output_path: Path) -> None:
    """便捷调用入口 — 节点 14 内的 9:16 子任务。"""
    return get_default_client().write_cover_9x16(
        draft_dir=draft_dir, title=title, output_path=output_path
    )
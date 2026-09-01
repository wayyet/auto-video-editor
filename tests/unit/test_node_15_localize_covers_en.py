"""node_15_localize_covers_en 单测 — Mock Image-Edit 调用 3 次、en_path 写入、缺图降级。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcp_clients.firered_image_edit_client import (
    MockFireRedImageEditClient,
    set_default_client,
)
from nodes.node_15_localize_covers_en import (
    _build_prompt,
    _get_titles,
    _localize_one_cover,
    _node_15_localize_covers_en_async,
)


def _seed_draft(draft_dir: Path) -> Path:
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"texts": [{"content": "原中文标题"}]},
    }
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_file


def _seed_cover(zh_path: Path, content: bytes = b"fake-zh-png") -> None:
    zh_path.parent.mkdir(parents=True, exist_ok=True)
    zh_path.write_bytes(content)


@pytest.fixture(autouse=True)
def _reset_client():
    """每个 case 结束后重置 default client,避免污染。"""
    yield
    set_default_client(MockFireRedImageEditClient())


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------
def test_get_titles_pair(tmp_path: Path) -> None:
    """_get_titles 从 draft 读 title_zh,并构造 title_en 占位。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    state = {"draft_path": str(draft_dir / "draft_content.json")}

    zh, en = _get_titles(state)
    assert zh == "原中文标题"
    assert en == "[EN] 原中文标题"


def test_get_titles_default_when_no_draft(tmp_path: Path) -> None:
    """draft_path 缺失时使用默认标题。"""
    zh, en = _get_titles({})
    assert zh  # 非空
    assert en.startswith("[EN] ")


def test_build_prompt_with_bbox(tmp_path: Path) -> None:
    """_build_prompt 在有 bbox 时应包含位置信息。"""
    bbox = {"x": 100, "y": 200, "font_size": 60}
    p = _build_prompt("你好", "Hello", bbox)
    assert "你好" in p and "Hello" in p
    assert "x=100" in p and "y=200" in p


def test_build_prompt_without_bbox(tmp_path: Path) -> None:
    """bbox 缺失时 prompt 仅含基本替换语义。"""
    p = _build_prompt("你好", "Hello", None)
    assert "你好" in p and "Hello" in p


def test_localize_one_cover_missing_zh_returns_none(tmp_path: Path) -> None:
    """zh_path 不存在 → 返回 None。"""
    cover = {
        "ratio": "16:9",
        "zh_path": str(tmp_path / "missing.png"),
        "text_bbox": {},
    }
    en_path = _localize_one_cover(cover, "原标题", "[EN] 原标题")
    assert en_path is None


def test_localize_one_cover_success(tmp_path: Path) -> None:
    """正常本地化:zh → en 文件被生成,命名规范。"""
    zh_path = tmp_path / "covers" / "cover_16x9_zh.png"
    _seed_cover(zh_path, b"source-bytes")
    cover = {
        "ratio": "16:9",
        "zh_path": str(zh_path),
        "text_bbox": {"x": 0, "y": 0, "font_size": 60},
    }
    set_default_client(MockFireRedImageEditClient(modify_marker=b"EN"))

    en_path = _localize_one_cover(cover, "原标题", "[EN] 原标题")
    assert en_path is not None
    en = Path(en_path)
    assert en.name == "cover_16x9_en.png"
    assert en.exists()
    # Mock 应附加 marker
    assert en.read_bytes().endswith(b"EN")


# ---------------------------------------------------------------------------
# 节点函数测试
# ---------------------------------------------------------------------------
def test_node_15_produces_en_path_for_each_cover(tmp_path: Path) -> None:
    """node_15 应为每张 cover 写入 en_path,共 3 张。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    covers = []
    for ratio, name in [("16:9", "16x9"), ("4:3", "4x3"), ("9:16", "9x16")]:
        zh = draft_dir / "covers" / f"cover_{name}_zh.png"
        _seed_cover(zh, f"src-{ratio}".encode())
        covers.append({"ratio": ratio, "zh_path": str(zh), "text_bbox": {}})

    state = {
        "covers": covers,
        "draft_path": str(draft_dir / "draft_content.json"),
        "status_log": [],
        "error_log": [],
    }

    out = asyncio.run(_node_15_localize_covers_en_async(state))
    en_paths = [c.get("en_path") for c in out["covers"]]
    assert all(p is not None for p in en_paths), en_paths
    assert "node_15_localize_covers_en_done" in out["status_log"]
    # 每个 en_path 文件都应存在
    for p in en_paths:
        assert Path(p).exists()


def test_node_15_graceful_degradation_when_image_missing(tmp_path: Path) -> None:
    """某张 zh_path 不存在 → en_path=None,累积 error_log,但不抛异常。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    covers = [
        {"ratio": "16:9", "zh_path": str(tmp_path / "missing1.png"), "text_bbox": {}},
        {"ratio": "4:3", "zh_path": str(tmp_path / "missing2.png"), "text_bbox": {}},
        {"ratio": "9:16", "zh_path": str(tmp_path / "missing3.png"), "text_bbox": {}},
    ]

    state = {
        "covers": covers,
        "draft_path": str(draft_dir / "draft_content.json"),
        "status_log": [],
        "error_log": [],
    }

    out = asyncio.run(_node_15_localize_covers_en_async(state))
    en_paths = [c.get("en_path") for c in out["covers"]]
    assert all(p is None for p in en_paths)
    # error_log 应有 3 条(每个 ratio)
    failures = [e for e in out["error_log"] if "封面本地化失败" in e]
    assert len(failures) == 3
    # 节点仍标记完成
    assert "node_15_localize_covers_en_done" in out["status_log"]


def test_node_15_empty_covers_skips(tmp_path: Path) -> None:
    """state.covers 为空 → 跳过节点,但仍标记 status(Week 4 delta-only)。"""
    state = {"covers": [], "status_log": [], "error_log": []}
    out = asyncio.run(_node_15_localize_covers_en_async(state))
    assert "node_15_localize_covers_skipped" in out["status_log"]
    # Week 4:跳过路径不返回 error_log(LangGraph 保留上游)
    assert "error_log" not in out
"""node_15_localize_covers_en 单测 — Mock Image-Edit 调用 3 次、en_path 写入、缺图降级。

2026-11-17 P2:英文标题走真实翻译(修订计划 §5.6)——
- 真实客户端(LLMTranslateClient)→ title_en 为模型译文,status="real";
- 默认 Mock 桩 → title_en 为空、status="stub",全部 en_path=None 降级,
  绝不生成 "[EN] 中文" 伪英文封面。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jy_common.translate_client import (
    LLMTranslateClient,
    MockTranslateClient,
    set_default_client as set_translate_default,
)
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


class _FakeTitleLLM:
    """恒定返回 "Real Title" 的假 LLM(实现 vlm LLMClient 的 chat 契约)。"""

    def chat(self, *, system_prompt, user_prompt, media=None, json_schema=None,
             temperature=0.3, top_p=0.9, max_tokens=4096):
        payload = json.loads(user_prompt)
        return json.dumps(
            {"translations": [
                {"index": s["index"], "text_en": f"Real {s['text_zh']}"}
                for s in payload["segments"]
            ]},
            ensure_ascii=False,
        )


@pytest.fixture(autouse=True)
def _restore_translate_default():
    """node_15 读全局翻译默认客户端:用例后还原为 Mock,防污染。"""
    yield
    set_translate_default(MockTranslateClient())


def _install_real_translator() -> None:
    set_translate_default(LLMTranslateClient(_FakeTitleLLM(), translator_id="mcode-cli"))


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
def test_get_titles_real_translation(tmp_path: Path) -> None:
    """真实翻译客户端 → title_en 为模型译文(无 [EN] 前缀),status=real。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    _install_real_translator()
    state = {"draft_path": str(draft_dir / "draft_content.json")}

    zh, en, status = _get_titles(state)
    assert zh == "原中文标题"
    assert en == "Real 原中文标题"
    assert "[EN]" not in en
    assert status == "real"


def test_get_titles_stub_returns_empty_en(tmp_path: Path) -> None:
    """默认 Mock 桩 → title_en 空串 + status=stub(绝不拼 [EN] 假标题)。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    state = {"draft_path": str(draft_dir / "draft_content.json")}

    zh, en, status = _get_titles(state)
    assert zh == "原中文标题"
    assert en == ""
    assert status == "stub"


def test_get_titles_default_when_no_draft() -> None:
    """draft_path 缺失时使用默认标题;stub 桩下 title_en 为空。"""
    zh, en, status = _get_titles({})
    assert zh  # 非空
    assert en == ""
    assert status == "stub"


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
    """真实翻译可用 → node_15 为每张 cover 写入 en_path,共 3 张,status=real。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    _install_real_translator()
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
    assert out["cover_translation_status"] == "real"
    # 每个 en_path 文件都应存在
    for p in en_paths:
        assert Path(p).exists()


def test_node_15_stub_degrades_without_fake_title(tmp_path: Path) -> None:
    """翻译桩(stub)→ 全部 en_path=None + 降级说明,不生成伪英文封面。"""
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
    assert all(c.get("en_path") is None for c in out["covers"])
    assert out["cover_translation_status"] == "stub"
    assert any("封面标题翻译不可用" in e for e in out["error_log"])
    assert not any(Path(c["zh_path"]).with_name(
        Path(c["zh_path"]).name.replace("_zh.png", "_en.png")
    ).exists() for c in covers)


def test_node_15_graceful_degradation_when_image_missing(tmp_path: Path) -> None:
    """真实翻译可用但 zh_path 不存在 → en_path=None,累积 error_log,不抛异常。"""
    draft_dir = tmp_path / "draft"
    _seed_draft(draft_dir)
    _install_real_translator()
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
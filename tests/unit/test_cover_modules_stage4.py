"""2026-11-11 模糊修复阶段4单测 —— 封面模块修复。

实机证据(console-64da836e2f3f):封面 6 张全部为 #14141C 纯色底 + 大段
字幕原生 JSON 文字 ——
1. ``cover_source_frame.jpg`` 全仓库只有消费者没有生产者 → 源帧永远缺失;
2. ``node_14/_get_title`` / ``node_15/_get_titles`` 把原生 content JSON 字符串
   直接当标题绘制。

本文件锁定:源帧生产者(含非黑帧挑选/失败显式留痕)、双形态标题解析。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from jy_common.native_text import build_text_content_json
from nodes.node_14_make_covers import (
    _get_title,
    _produce_cover_source_frame,
    node_14_make_covers,
)
from nodes.node_15_localize_covers_en import _get_titles


# ---------------------------------------------------------------------------
# 标题解析
# ---------------------------------------------------------------------------
def _write_draft_with_text(draft_dir: Path, content) -> Path:
    draft = {"materials": {"texts": [{"id": "text-zh-1", "content": content}]}}
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )
    return draft_dir


def test_get_title_parses_native_json_content(tmp_path) -> None:
    """原生 content JSON → 只取 text 字段,不再把整段 JSON 当标题。"""
    content = build_text_content_json("城市漫游指南", {"color": [1, 1, 1], "size": 5.0})
    d = _write_draft_with_text(tmp_path, content)
    assert _get_title(d) == "城市漫游指南"


def test_get_title_pure_text_content_passthrough(tmp_path) -> None:
    """工作流私有纯文本形态原样返回。"""
    d = _write_draft_with_text(tmp_path, "纯文本标题")
    assert _get_title(d) == "纯文本标题"


def test_get_title_missing_falls_back_to_default(tmp_path) -> None:
    assert _get_title(tmp_path) == "默认标题"


def test_node_15_titles_parse_native_json(tmp_path) -> None:
    content = build_text_content_json("周末探店", {})
    d = _write_draft_with_text(tmp_path, content)
    title_zh, title_en = _get_titles({"draft_path": str(d / "draft_content.json")})
    assert title_zh == "周末探店"
    assert title_en == "[EN] 周末探店"


# ---------------------------------------------------------------------------
# 封面源帧生产者
# ---------------------------------------------------------------------------
def _bright_jpg(path: Path, value: int = 200) -> None:
    img = Image.new("RGB", (64, 64), color=(value, value, value))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    path.write_bytes(buf.getvalue())


def test_producer_reuses_existing_frame(tmp_path) -> None:
    frame = tmp_path / "cover_source_frame.jpg"
    _bright_jpg(frame)

    def _boom(*_a, **_k):  # 已有源帧时不得再抽帧
        raise AssertionError("已有 cover_source_frame.jpg 时不应调用 extractor")

    path, logs = _produce_cover_source_frame(tmp_path, extractor=_boom)
    assert path == str(frame)
    assert logs == []


def test_producer_extracts_first_non_dark_frame(tmp_path, monkeypatch) -> None:
    video = tmp_path / "input.mp4"
    video.write_bytes(b"\x00" * 16)
    draft = {"materials": {"videos": [{"id": "video-1", "path": str(video)}]}}
    (tmp_path / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setattr(
        "nodes.node_14_make_covers.probe_video_dimensions",
        lambda _p: type("D", (), {"duration_ms": 10000})(),
    )

    calls: list[float] = []

    def _fake_extract(source, t, out):
        calls.append(round(t, 2))
        _bright_jpg(Path(out))
        return True

    path, logs = _produce_cover_source_frame(tmp_path, extractor=_fake_extract)
    assert path == str(tmp_path / "cover_source_frame.jpg")
    assert calls and calls[0] == 4.0, f"首选候选 0.4×10s=4s,实际 {calls}"
    assert any("已生产" in l for l in logs)


def test_producer_skips_dark_frames(tmp_path, monkeypatch) -> None:
    """黑帧候选逐个回退;全部过暗 → 删除残留文件并显式告警。"""
    video = tmp_path / "input.mp4"
    video.write_bytes(b"\x00" * 16)
    draft = {"materials": {"videos": [{"id": "video-1", "path": str(video)}]}}
    (tmp_path / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setattr(
        "nodes.node_14_make_covers.probe_video_dimensions",
        lambda _p: type("D", (), {"duration_ms": 10000})(),
    )

    def _dark_extract(source, t, out):
        _bright_jpg(Path(out), value=5)  # 全黑帧
        return True

    path, logs = _produce_cover_source_frame(tmp_path, extractor=_dark_extract)
    assert path is None
    assert not (tmp_path / "cover_source_frame.jpg").exists(), "过暗残留必须清理"
    assert any("抽取失败" in l for l in logs)


def test_producer_extractor_failure_warns(tmp_path, monkeypatch) -> None:
    """ffmpeg 全部失败:返回 None + 显式 warning,不静默伪装成功。"""
    video = tmp_path / "input.mp4"
    video.write_bytes(b"\x00" * 16)
    draft = {"materials": {"videos": [{"id": "video-1", "path": str(video)}]}}
    (tmp_path / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setattr(
        "nodes.node_14_make_covers.probe_video_dimensions",
        lambda _p: type("D", (), {"duration_ms": 10000})(),
    )
    path, logs = _produce_cover_source_frame(
        tmp_path, extractor=lambda *_a, **_k: False
    )
    assert path is None
    assert any("抽取失败" in l for l in logs)


def test_producer_no_video_material_warns(tmp_path) -> None:
    path, logs = _produce_cover_source_frame(tmp_path)
    assert path is None
    assert any("无可用视频素材" in l for l in logs)


def test_node_14_merges_frame_logs_into_status_log(tmp_path, monkeypatch) -> None:
    """节点级:源帧生产留痕必须并入 status_log(可观测,不静默)。"""
    video = tmp_path / "input.mp4"
    video.write_bytes(b"\x00" * 16)
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [{"id": "video-1", "path": str(video)}], "texts": []},
        "tracks": [],
    }
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setattr(
        "nodes.node_14_make_covers._produce_cover_source_frame",
        lambda d: (None, ["[node_14] 封面源帧未生产:草稿无可用视频素材路径,封面将使用占位底图"]),
    )

    out = node_14_make_covers({"draft_path": str(draft_dir / "draft_content.json")})
    assert any("封面源帧" in t for t in out["status_log"])

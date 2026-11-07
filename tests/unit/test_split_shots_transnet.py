"""TransNetV2 检测器接入(2026-11-08 选项C)单元测试 —— 全程无 torch 依赖。

覆盖四层:
1. 概率→切点数学(``_transnetv2_cut_frames`` 滑窗/门限语义,桩 torch);
2. ``transnetv2_split_shots`` 的 cuts→clips 契约(桩抽帧/模型/ffprobe);
3. 检测器分派(auto 回退 / 强制 transnetv2 失败回退 / frame_diff 直通);
4. 节点层:回退原因进 error_log + status tag,不静默。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import storyline_capabilities.split_shots as ss
from nodes.storyline.node_split_shots import storyline_split_shots_node


# ---------------------------------------------------------------------------
# 桩 torch / 桩模型:验证滑窗切分数学,不导入真 torch
# ---------------------------------------------------------------------------
class _FakeNoGrad:
    def __enter__(self) -> "_FakeNoGrad":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _FakeTensor:
    def __init__(self, arr: np.ndarray) -> None:
        self._arr = np.asarray(arr)

    def unsqueeze(self, dim: int) -> "_FakeTensor":
        return _FakeTensor(np.expand_dims(self._arr, dim))

    def __getitem__(self, item: Any) -> "_FakeTensor":
        return _FakeTensor(self._arr[item])

    def numpy(self) -> np.ndarray:
        return self._arr


class _FakeTorch:
    """够 ``_transnetv2_cut_frames`` 用的最小 torch 面(模块替身)。"""

    @staticmethod
    def no_grad() -> _FakeNoGrad:
        return _FakeNoGrad()

    @staticmethod
    def from_numpy(arr: np.ndarray) -> _FakeTensor:
        return _FakeTensor(arr)

    @staticmethod
    def sigmoid(t: "_FakeTensor") -> _FakeTensor:
        a = t._arr.astype(np.float32)
        return _FakeTensor(1.0 / (1.0 + np.exp(-a)))


class _WindowModel:
    """按窗口步进的 logits 桩:cut_frames(原始帧号)→ padded 坐标给强 logit。

    模型返回 logits(官方口径),sigmoid 由被测代码外层做:±12 → ~1/~0。
    """

    def __init__(self, cut_frames: set[int], pad_start: int) -> None:
        self.cut_frames = cut_frames
        self.pad_start = pad_start
        self.ptr = 0

    def __call__(self, window: Any) -> tuple[Any, dict]:
        logits = np.full(100, -12.0, dtype=np.float32)
        for raw_idx in self.cut_frames:
            local = raw_idx + self.pad_start - self.ptr  # padded 坐标 → 窗内坐标
            if 25 <= local < 75:
                logits[local] = 12.0
        self.ptr += 50
        return _FakeTensor(logits.reshape(1, 100, 1)), {}


# ---------------------------------------------------------------------------
# 1. 概率 → 切点
# ---------------------------------------------------------------------------
def test_cut_frames_window_math() -> None:
    """滑窗步进 50、每窗取 [25:75];切点帧号 = 新镜头首帧。"""
    # 120 帧视频,切点在第 60 帧(新镜头从 60 开始)
    frames = np.zeros((120, 27, 48, 3), dtype=np.uint8)
    model = _WindowModel({60}, pad_start=25)
    cuts = ss._transnetv2_cut_frames(
        frames, model=model, torch_mod=_FakeTorch, threshold=0.5
    )
    assert cuts == [60]


def test_cut_frames_threshold_strict() -> None:
    """概率 == threshold 不记切(> 阈值才记)。"""
    frames = np.zeros((60, 27, 48, 3), dtype=np.uint8)
    # 全 0.5 → 无切点
    cuts = ss._transnetv2_cut_frames(
        frames, model=_WindowModel(set(), pad_start=25), torch_mod=_FakeTorch, threshold=0.5
    )
    assert cuts == []


# ---------------------------------------------------------------------------
# 2. transnetv2_split_shots → clips 契约
# ---------------------------------------------------------------------------
@pytest.fixture
def stubbed_transnet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """抽帧/模型/ffprobe 全桩;切点定在 10s(帧 250,fps=25)。"""
    video = tmp_path / "v.mov"
    video.write_bytes(b"fake")
    monkeypatch.setattr(ss, "_transnetv2_available", lambda: (True, ""))
    monkeypatch.setattr(
        ss,
        "_extract_frames_rgb24",
        lambda *a, **k: np.zeros((750, 27, 48, 3), dtype=np.uint8),  # 30s @ 25fps
    )
    monkeypatch.setattr(
        ss,
        "_load_transnetv2_model",
        lambda: (_WindowModel({250}, pad_start=25), _FakeTorch),
    )
    monkeypatch.setattr(ss, "_probe_duration_ms", lambda v: (30000, True))
    return video


def test_transnetv2_clip_contract(stubbed_transnet: Path) -> None:
    """切点 10s → 2 段(0-10s/10s-30s),契约字段与帧差版同构。"""
    result = ss.transnetv2_split_shots(stubbed_transnet, fps=25)
    clips = result["clips"]
    assert result["method"] == "transnetv2"
    assert result["cut_count"] == 1
    assert len(clips) == 2
    first, second = clips
    assert (first["source_in_ms"], first["source_out_ms"]) == (0, 10000)
    assert (second["source_in_ms"], second["source_out_ms"]) == (10000, 30000)
    assert first["split_method"] == "transnetv2"
    assert first["kind"] == "video"
    assert first["source_ref"]["duration"] == 10000
    assert first["path"] == str(stubbed_transnet)
    # 无缝
    assert second["source_in_ms"] == first["source_out_ms"]


def test_transnetv2_min_shot_merge(stubbed_transnet: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """<min_shot 的段并入前段(切点 10s/15s + min_shot=6000 → 15s 切点失效)。"""

    class _Dense(_WindowModel):
        def __init__(self) -> None:
            super().__init__({250, 375}, pad_start=25)  # 10s 与 15s 两个切点

    monkeypatch.setattr(
        ss, "_load_transnetv2_model", lambda: (_Dense(), _FakeTorch)
    )
    result = ss.transnetv2_split_shots(stubbed_transnet, fps=25, min_shot_ms=6000)
    clips = result["clips"]
    # 0-15s / 15-30s:10-15s 段(5s < 6s)按原语义并入**前段**
    assert len(clips) == 2
    assert (clips[0]["source_out_ms"], clips[1]["source_in_ms"]) == (15000, 15000)


def test_transnetv2_unavailable_raises(stubbed_transnet: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """权重缺失等不可用 → TransNetV2Unavailable(由上层决定回退)。"""
    monkeypatch.setattr(
        ss, "_transnetv2_available", lambda: (False, "weights_missing: x.pth")
    )
    with pytest.raises(ss.TransNetV2Unavailable):
        ss.transnetv2_split_shots(stubbed_transnet)


# ---------------------------------------------------------------------------
# 3. 检测器分派
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_frame_diff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        ss,
        "frame_difference_split_shots",
        lambda video, **k: {
            "clips": [
                {
                    "clip_id": "clip_0001",
                    "kind": "video",
                    "path": str(video),
                    "source_in_ms": 0,
                    "source_out_ms": 1000,
                    "timeline_in_ms": 0,
                    "timeline_out_ms": 1000,
                    "split_method": "frame_diff",
                    "source_ref": {"start": 0, "end": 1000, "duration": 1000},
                }
            ],
            "method": "frame_diff",
        },
    )


def test_auto_falls_back_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, stub_frame_diff, tmp_path: Path
) -> None:
    """auto + torch 缺失 → 用帧差,结果带 detector_fallback 原因,不静默。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(
        ss, "_transnetv2_available", lambda: (False, "torch_import_failed: ModuleNotFound")
    )
    result = ss.split_shots({"path": str(video), "media_id": "m1"}, detector="auto")
    assert result["detector_used"] == "frame_diff"
    assert result["detector_fallback"]["reason"].startswith("torch_import_failed")
    assert result["detector_fallback"]["requested"] == "auto"
    assert result["clips"][0]["media_id"] == "m1"


def test_forced_transnet_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch, stub_frame_diff, tmp_path: Path
) -> None:
    """强制 transnetv2 + 推理抛错 → 回退帧差并带原因。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(ss, "_transnetv2_available", lambda: (True, ""))

    def _boom(*a: Any, **k: Any) -> dict:
        raise RuntimeError("cuda exploded")

    monkeypatch.setattr(ss, "transnetv2_split_shots", _boom)
    result = ss.split_shots({"path": str(video), "media_id": "m1"}, detector="transnetv2")
    assert result["detector_used"] == "frame_diff"
    assert "transnetv2_failed" in result["detector_fallback"]["reason"]


def test_frame_diff_never_touches_transnet(
    monkeypatch: pytest.MonkeyPatch, stub_frame_diff, tmp_path: Path
) -> None:
    """detector=frame_diff 时绝不走 TransNetV2 分支。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(
        ss,
        "transnetv2_split_shots",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")),
    )
    result = ss.split_shots({"path": str(video), "media_id": "m1"}, detector="frame_diff")
    assert result["detector_used"] == "frame_diff"
    assert "detector_fallback" not in result


def test_transnet_success_wires_media_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto + 可用 + 成功 → method=transnetv2,media_id 接线。"""
    video = tmp_path / "v.mov"
    video.write_bytes(b"fake")
    monkeypatch.setattr(ss, "_transnetv2_available", lambda: (True, ""))
    monkeypatch.setattr(ss, "frame_difference_split_shots", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        ss,
        "transnetv2_split_shots",
        lambda *a, **k: {
            "clips": [
                {
                    "clip_id": "clip_0001",
                    "kind": "video",
                    "path": str(video),
                    "source_in_ms": 0,
                    "source_out_ms": 8000,
                    "timeline_in_ms": 0,
                    "timeline_out_ms": 8000,
                    "split_method": "transnetv2",
                    "source_ref": {"start": 0, "end": 8000, "duration": 8000},
                }
            ],
            "method": "transnetv2",
            "media_id": "",
        },
    )
    result = ss.split_shots({"path": str(video), "media_id": "media_0009"}, detector="auto")
    assert result["method"] == "transnetv2"
    assert result["detector_used"] == "transnetv2"
    assert result["clips"][0]["media_id"] == "media_0009"
    assert result["clips"][0]["source_ref"]["media_id"] == "media_0009"


def test_media_list_surfaces_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """media list 入口:回退信息聚合为 detector_fallbacks。"""

    def _fake(media_artifact: Any, **kwargs: Any) -> dict:
        return {
            "clips": [{"clip_id": "clip_0001", "media_id": "media_0001"}],
            "method": "frame_diff",
            "detector_used": "frame_diff",
            "detector_fallback": {"requested": "auto", "reason": "torch_import_failed: x"},
        }

    monkeypatch.setattr(ss, "split_shots", _fake)
    result = ss.split_shots_for_media_list(
        [{"media_id": "media_0001", "path": "a.mp4", "media_type": "video"}]
    )
    assert result["method"] == "frame_diff"
    assert result["detector_fallbacks"][0]["reason"].startswith("torch_import_failed")


# ---------------------------------------------------------------------------
# 4. 节点层:回退进监控事件流
# ---------------------------------------------------------------------------
def test_node_surfaces_transnet_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回退发生时节点写 TRANSNET_FALLBACK error_log + status tag。"""
    artifact = tmp_path / "load_media.json"
    artifact.write_text(
        json.dumps(
            {
                "media": [
                    {"media_id": "media_0001", "path": "a.mp4", "media_type": "video"}
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ss,
        "split_shots_for_media_list",
        lambda media_list, **k: {
            "shots": {"media_0001": [{"clip_id": "clip_0001"}]},
            "method": "frame_diff",
            "detector_fallbacks": [
                {"media_id": "media_0001", "requested": "auto", "reason": "torch_import_failed: x"}
            ],
        },
    )
    state = {
        "storyline_media_artifact": str(artifact),
        "storyline_outputs_root": str(tmp_path / "out"),
        "status_log": [],
        "error_log": [],
    }
    out = storyline_split_shots_node(state)
    assert any("TRANSNET_FALLBACK" in e for e in out.get("error_log", []))
    assert any(
        "storyline_split_shots_transnet_fallback" in t for t in out.get("status_log", [])
    )
    assert out.get("storyline_shots_artifact"), "回退路径仍要照常持久化产物"

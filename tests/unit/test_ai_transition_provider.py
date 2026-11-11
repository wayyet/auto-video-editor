"""MiniMaxHailuoProvider 单测(httpx.MockTransport 全链,不触网)。

覆盖:提交→轮询→取址→下载→ffprobe 复检的完整成功链、任务失败、提交异常、
超时、Key 缺失 fail-closed、payload 走 dataURL、resolve_default_provider 配置解析。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

import storyline_capabilities.ai_transition_provider as prov_mod
from storyline_capabilities.ai_transition_provider import (
    PROVIDER_STATUS_FAILED,
    PROVIDER_STATUS_GENERATED,
    MiniMaxHailuoProvider,
    ProviderNotConfigured,
    resolve_default_provider,
    set_default_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider():
    set_default_provider(None)
    yield
    set_default_provider(None)


def _make_tiny_mp4(tmp_path: Path, *, seconds: float = 1.0, size: str = "64x64") -> bytes:
    """ffmpeg 现场造一个小 mp4(供下载产物走真实 ffprobe 复检)。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / f"src_{size.replace(':', 'x')}_{seconds}.mp4"
    if not out.exists():
        cp = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c=red:s={size}:d={seconds}:r=30",
                "-pix_fmt", "yuv420p", str(out),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert cp.returncode == 0, cp.stderr
    return out.read_bytes()


def _provider_with_transport(
    tmp_path: Path,
    handler,
    *,
    poll_interval_s: int = 0,
    timeout_s: int = 5,
) -> MiniMaxHailuoProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MiniMaxHailuoProvider(
        "test-key", http_client=client, poll_interval_s=poll_interval_s, timeout_s=timeout_s
    )


def _png(tmp_path: Path, name: str) -> Path:
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(p, format="PNG")
    return p


def test_full_chain_generates_and_validates(tmp_path: Path) -> None:
    """提交→轮询(先 processing 后 Success)→取址→下载真实 mp4→ffprobe 实测。"""
    mp4_bytes = _make_tiny_mp4(tmp_path / "assets")
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request
        if request.url.path == "/v1/video_generation":
            body = json.loads(request.content)
            assert body["model"] == "MiniMax-Hailuo-02"
            assert body["first_frame_image"].startswith("data:image/png;base64,")
            assert body["last_frame_image"].startswith("data:image/png;base64,")
            assert body["duration"] == 2  # 2000ms → 2s
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/v1/query/video_generation":
            if not seen.get("_polled"):
                seen["_polled"] = request  # type: ignore[assignment]
                return httpx.Response(200, json={"status": "Processing"})
            return httpx.Response(200, json={"status": "Success", "file_id": "file-9"})
        if request.url.path == "/v1/files/retrieve":
            return httpx.Response(
                200, json={"file": {"download_url": "https://cdn.example/x.mp4"}}
            )
        if request.url.path == "/x.mp4":
            return httpx.Response(200, content=mp4_bytes)
        return httpx.Response(404)

    provider = _provider_with_transport(tmp_path, handler)
    first = _png(tmp_path, "first.png")
    last = _png(tmp_path, "last.png")
    out = tmp_path / "out" / "t.mp4"
    result = provider.generate_transition(
        first_frame=first,
        last_frame=last,
        prompt="Smooth continuous single shot. test.",
        duration_ms=2000,
        resolution=(1080, 1920),
        output_path=out,
    )
    assert result.status == PROVIDER_STATUS_GENERATED, result.error
    assert result.request_id == "task-1"
    assert result.path == out and out.is_file()
    # 产物字段是 ffprobe 实测值,不是提交参数
    assert 800 <= result.duration_ms <= 1400
    assert (result.width, result.height) == (64, 64)
    assert result.fps > 0
    assert len(result.sha256) == 64


def test_task_fail_returns_failed_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/video_generation":
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/v1/query/video_generation":
            return httpx.Response(200, json={"status": "Fail", "message": "bad prompt"})
        return httpx.Response(404)

    provider = _provider_with_transport(tmp_path, handler)
    result = provider.generate_transition(
        first_frame=_png(tmp_path, "f.png"),
        last_frame=_png(tmp_path, "l.png"),
        prompt="p",
        duration_ms=1000,
        resolution=(1080, 1920),
        output_path=tmp_path / "t.mp4",
    )
    assert result.status == PROVIDER_STATUS_FAILED
    assert "任务失败" in (result.error or "")
    assert result.path is None


def test_submit_missing_task_id_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/video_generation"
        return httpx.Response(200, json={"oops": 1})

    provider = _provider_with_transport(tmp_path, handler)
    result = provider.generate_transition(
        first_frame=_png(tmp_path, "f.png"),
        last_frame=_png(tmp_path, "l.png"),
        prompt="p",
        duration_ms=1000,
        resolution=(1080, 1920),
        output_path=tmp_path / "t.mp4",
    )
    assert result.status == PROVIDER_STATUS_FAILED
    assert "task_id" in (result.error or "")


def test_poll_timeout_returns_failed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/video_generation":
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/v1/query/video_generation":
            return httpx.Response(200, json={"status": "Processing"})
        return httpx.Response(404)

    provider = _provider_with_transport(tmp_path, handler, poll_interval_s=0, timeout_s=1)
    result = provider.generate_transition(
        first_frame=_png(tmp_path, "f.png"),
        last_frame=_png(tmp_path, "l.png"),
        prompt="p",
        duration_ms=1000,
        resolution=(1080, 1920),
        output_path=tmp_path / "t.mp4",
    )
    assert result.status == PROVIDER_STATUS_FAILED
    assert "超时" in (result.error or "")


def test_missing_api_key_fails_closed() -> None:
    with pytest.raises(ProviderNotConfigured):
        MiniMaxHailuoProvider("")


def test_http_error_status_surfaces_as_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    provider = _provider_with_transport(tmp_path, handler)
    result = provider.generate_transition(
        first_frame=_png(tmp_path, "f.png"),
        last_frame=_png(tmp_path, "l.png"),
        prompt="p",
        duration_ms=1000,
        resolution=(1080, 1920),
        output_path=tmp_path / "t.mp4",
    )
    assert result.status == PROVIDER_STATUS_FAILED
    assert result.error


# ---------------------------------------------------------------------------
# resolve_default_provider 配置解析
# ---------------------------------------------------------------------------
def test_resolve_returns_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "")
    assert resolve_default_provider() is None
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "none")
    assert resolve_default_provider() is None


def test_resolve_builds_minimax_from_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", "k-test")
    provider = resolve_default_provider()
    assert isinstance(provider, MiniMaxHailuoProvider)


def test_resolve_unknown_name_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "openai-video")
    with pytest.raises(ProviderNotConfigured):
        resolve_default_provider()


def test_injected_provider_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    class _P:
        name = "fake"
        model = "fake-1"

        def generate_transition(self, **kw):  # pragma: no cover
            raise AssertionError

    p = _P()
    set_default_provider(p)  # type: ignore[arg-type]
    monkeypatch.setattr("config.STORYLINE_AI_TRANSITION_PROVIDER", "")
    assert resolve_default_provider() is p

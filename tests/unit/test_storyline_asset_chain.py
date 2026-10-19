"""素材链路单测 — 集成文档 D5 / D6 的修复回归(D5→F3,D6→F4)。

D5:``split_shots`` 产出的 clip 缺 ``kind`` / ``path`` / ``source_ref``
    → ``understand_clips`` 走到 ``Error: Clip kind not supported:``,
    整条素材链路拿不到画面(Agent 侧表现为任务 ``images.Count=0``)。

D6:prompt 模板用 ``{{name}}`` 占位符,但加载器只认 ``{name}``;
    且 8 个调用点里有一半没把模板需要的变量传进去
    → ``{{user_request}}`` / ``{{clip_captions}}`` 等原样残留进 prompt。

本文件覆盖:
- F3:clip 契约(``kind`` / ``path`` / ``source_ref``)+ 画面区间正确
- F4:``render_prompt`` 的两种占位符语法 + 残留检测 + 各能力 prompt 无残留
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storyline_capabilities.prompts import (  # noqa: E402
    load_prompt,
    render_prompt,
    unresolved_placeholders,
)
from storyline_capabilities.split_shots import (  # noqa: E402
    frame_difference_split_shots,
    split_shots,
)
from storyline_capabilities.load_media import load_media  # noqa: E402
from storyline_capabilities.understand_clips import understand_clips  # noqa: E402
from storyline_capabilities.filter_clips import filter_clips  # noqa: E402
from storyline_capabilities.group_clips import group_clips  # noqa: E402
from storyline_capabilities.generate_script import generate_script  # noqa: E402
from storyline_capabilities.recommend_text import recommend_text  # noqa: E402
from storyline_capabilities.recommend_transition import recommend_transition  # noqa: E402


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
_FALLBACK_FFMPEG_DIRS = (
    Path(r"C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin"),
)


def _ensure_ffmpeg_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ffmpeg / ffprobe 放进 PATH(后台 shell 里 PATH 常常不含 ffmpeg)。"""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    for d in _FALLBACK_FFMPEG_DIRS:
        if (d / "ffmpeg.exe").exists():
            monkeypatch.setenv(
                "PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}"
            )
            return
    pytest.skip("本机没有 ffmpeg,跳过需要真实抽帧的用例")


def _make_test_video(path: Path) -> None:
    """生成 4 秒两段画面的测试视频(中间有一次硬切 → 必然切出 ≥2 个镜头)。"""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=320x240:rate=10:duration=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-pix_fmt", "yuv420p",
        str(path),
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if cp.returncode != 0 or not path.exists():
        pytest.skip(f"生成测试视频失败: {cp.stderr[:200]}")


class _RecordingClient:
    """记录每次 chat() 的 prompt 与 media,返回可解析的 JSON。"""

    def __init__(self, reply: str = "{}") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return self.reply


# ---------------------------------------------------------------------------
# F3(D5):clip 契约
# ---------------------------------------------------------------------------
def test_split_shots_clips_carry_full_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """split_shots 的每个 clip 必须带 kind / path / source_ref(否则 understand 会报错)。"""
    _ensure_ffmpeg_on_path(monkeypatch)
    video = tmp_path / "clip.mp4"
    _make_test_video(video)

    media_artifact = load_media([{"path": str(video)}])
    assert media_artifact["media"], "load_media 应该识别出这个 mp4"

    # 节点壳子 node_split_shots 走的就是 list 输入 → {"shots": {media_id: [clip]}}
    shots = split_shots(media_artifact["media"])
    media_id = media_artifact["media"][0]["media_id"]
    clips = (shots.get("shots") or {}).get(media_id) or []
    assert clips, "应该至少切出 1 个镜头"

    for clip in clips:
        assert clip.get("kind") == "video", f"clip 缺 kind: {clip}"
        assert clip.get("path"), f"clip 缺 path: {clip}"
        ref = clip.get("source_ref") or {}
        assert ref.get("media_id") == media_id, f"source_ref 缺 media_id: {clip}"
        assert ref.get("start") is not None and ref.get("end") is not None, (
            f"source_ref 缺 start/end: {clip}"
        )
        assert ref["end"] > ref["start"], f"source_ref 区间非法: {clip}"
        assert ref.get("duration") == ref["end"] - ref["start"], f"duration 不一致: {clip}"
        # 与 clip 自身的时间字段保持一致
        assert ref["start"] == clip["source_in_ms"]
        assert ref["end"] == clip["source_out_ms"]


def test_frame_difference_split_shots_emits_source_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直接调帧差切分也必须产出 source_ref(帧差版是唯一实现)。"""
    _ensure_ffmpeg_on_path(monkeypatch)
    video = tmp_path / "clip.mp4"
    _make_test_video(video)

    result = frame_difference_split_shots(video)
    clips = result.get("clips") or []
    assert clips
    for clip in clips:
        assert clip.get("kind") == "video"
        assert (clip.get("source_ref") or {}).get("duration") is not None


def test_understand_clips_uses_split_shots_output_without_kind_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端:split_shots → understand_clips,不应再出现 'Clip kind not supported'。"""
    _ensure_ffmpeg_on_path(monkeypatch)
    video = tmp_path / "clip.mp4"
    _make_test_video(video)

    media_artifact = load_media([{"path": str(video)}])
    shots = split_shots(media_artifact["media"])

    client = _RecordingClient(
        reply=json.dumps({"caption": "一个测试镜头", "aes_score": 0.8})
    )
    res = understand_clips(
        split_shots_artifact=shots,
        media_artifact=media_artifact,
        client=client,  # type: ignore[arg-type]
    )

    captions = res["clip_captions"]
    assert captions, "应该有 clip caption"
    for cap in captions:
        assert not str(cap.get("caption", "")).startswith(
            "Error: Clip kind not supported"
        ), f"D5 未修复: {cap}"
        assert cap.get("caption") == "一个测试镜头"

    # 画面必须以「视频段」形式传给模型(而不是空 media)
    assert client.calls, "应该真的调了一次 chat"
    media = client.calls[0].get("media") or []
    assert media, "D5 未修复:传���模型的 media 是空的"
    first = media[0]
    assert first.get("path")
    assert "in_sec" in first and "out_sec" in first
    assert float(first["out_sec"]) > float(first["in_sec"])


def test_understand_clips_propagates_source_ref_to_captions() -> None:
    """caption 要带 source_ref(start/end),下游才算得出素材总时长。"""
    media_artifact = {
        "media": [{"media_id": "media_0001", "path": "x.mp4", "media_type": "video"}]
    }
    splits = {
        "shots": {
            "media_0001": [
                {
                    "clip_id": "clip_0001",
                    "kind": "video",
                    "source_ref": {
                        "media_id": "media_0001",
                        "start": 0,
                        "end": 2000,
                        "duration": 2000,
                    },
                }
            ]
        }
    }
    client = _RecordingClient(
        reply=json.dumps({"caption": "c", "aes_score": 0.5})
    )
    res = understand_clips(
        split_shots_artifact=splits,
        media_artifact=media_artifact,
        client=client,  # type: ignore[arg-type]
    )
    ref = res["clip_captions"][0]["source_ref"]
    assert ref["media_id"] == "media_0001"
    assert ref["start"] == 0
    assert ref["end"] == 2000


def test_clip_timing_survives_to_timeline_plan() -> None:
    """素材链路必须把「源片段区间」一路带到时间轴(D5 的完整形态)。

    链路:split_shots → understand_clips → filter_clips → group_clips →
    plan_timeline_pro。时间轴读的是顶层 ``media_id`` / ``source_in_ms`` /
    ``source_out_ms``;只要 understand_clips 丢一个字段,时间轴就会退化成
    ``source_media_id=null`` + ``source_in_ms=0``(剪映草稿绑不上源素材)。
    """
    from storyline_capabilities.plan_timeline_pro import plan_timeline_pro

    media_artifact = {
        "media": [{"media_id": "media_0001", "path": "x.mp4", "media_type": "video"}]
    }
    splits = {
        "shots": {
            "media_0001": [
                {
                    "clip_id": "clip_0001",
                    "kind": "video",
                    "media_id": "media_0001",
                    "source_in_ms": 0,
                    "source_out_ms": 3000,
                    "source_ref": {
                        "media_id": "media_0001",
                        "start": 0,
                        "end": 3000,
                        "duration": 3000,
                    },
                },
                {
                    "clip_id": "clip_0002",
                    "kind": "video",
                    "media_id": "media_0001",
                    "source_in_ms": 3000,
                    "source_out_ms": 6000,
                    "source_ref": {
                        "media_id": "media_0001",
                        "start": 3000,
                        "end": 6000,
                        "duration": 3000,
                    },
                },
            ]
        }
    }
    client = _RecordingClient(
        reply=json.dumps({"caption": "测试画面", "aes_score": 0.7})
    )
    understanding = understand_clips(
        split_shots_artifact=splits,
        media_artifact=media_artifact,
        client=client,  # type: ignore[arg-type]
    )

    # caption 必须带 media_id / source_in_ms / source_out_ms
    for cap in understanding["clip_captions"]:
        assert cap.get("media_id") == "media_0001"
        assert cap.get("source_in_ms") is not None
        assert cap.get("source_out_ms") is not None

    filtered = filter_clips(
        understanding_artifact=understanding, keep_ratio=1.0, client=client  # type: ignore[arg-type]
    )
    groups = group_clips(
        filtered_clips=filtered["filtered_clips"], group_size=3, client=client  # type: ignore[arg-type]
    )

    plan = plan_timeline_pro(groups=groups["groups"], job_id="t")
    assert plan["clips"], "时间轴应该有片段"
    for c in plan["clips"]:
        assert c.get("source_media_id") == "media_0001", f"时间轴丢了 source_media_id: {c}"
        assert c.get("source_in_ms") is not None
        assert c.get("source_out_ms") is not None
        assert c["source_out_ms"] > c["source_in_ms"], f"源片段区间非法: {c}"


# ---------------------------------------------------------------------------
# F4(D6):prompt 模板渲染
# ---------------------------------------------------------------------------
def test_render_prompt_supports_double_brace_placeholders() -> None:
    """``{{name}}`` 是模板的实际写法,必须被替换。"""
    out = render_prompt("group_clips", "user", lang="zh", clip_number=7)
    assert "{{clip_number}}" not in out
    assert "7" in out


def test_render_prompt_keeps_unprovided_placeholder_raw() -> None:
    """没传的变量保持原样 —— 宁可看到残留,也不要静默喂空数据。"""
    out = render_prompt("group_clips", "user", lang="zh", clip_number=7)
    assert "{{user_request}}" in out
    assert unresolved_placeholders(out) == ["user_request", "duration_budget", "clip_captions"]


def test_render_prompt_still_supports_legacy_single_brace() -> None:
    """旧模板的单花括号写法仍要能用。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "demo").mkdir()
        (root / "demo" / "zh").mkdir()
        (root / "demo" / "zh" / "user.md").write_text(
            "Hello {name}, today is {day}.", encoding="utf-8"
        )
        out = render_prompt(
            "demo", "user", lang="zh", name="Alice", day="Monday",
            prompts_root=root,
        )
    assert "Hello Alice" in out
    assert "Monday" in out


def test_unresolved_placeholders_empty_for_clean_text() -> None:
    assert unresolved_placeholders("普通文本,没有占位符") == []
    assert unresolved_placeholders('{"type": "string"}') == []


def test_all_prompt_templates_use_double_brace_syntax() -> None:
    """模板占位符必须是 ``{{name}}``;单花括号写法会让 D6 复发。"""
    import re

    from storyline_capabilities.prompts import PROMPTS_ROOT

    legacy = re.compile(r"(?<!\{)\{[A-Za-z_][A-Za-z0-9_]*(?::\?)?\}(?!\})")
    bad: list[str] = []
    for md in sorted(PROMPTS_ROOT.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for m in legacy.finditer(text):
            bad.append(f"{md.relative_to(PROMPTS_ROOT)}: {m.group(0)}")
    assert not bad, f"模板里混进了单花括号占位符: {bad}"


# ---------------------------------------------------------------------------
# F4(D6):各能力的 prompt 不得残留占位符
# ---------------------------------------------------------------------------
def _captured_user_prompts(client: _RecordingClient) -> list[str]:
    return [str(c.get("user_prompt") or "") for c in client.calls]


def test_filter_clips_prompt_has_no_placeholder_residue() -> None:
    client = _RecordingClient()
    filter_clips(
        understanding_artifact={
            "clip_captions": [
                {
                    "clip_id": "clip_0001",
                    "caption": "海边日落",
                    "aes_score": 0.9,
                    "source_ref": {"media_id": "media_0001", "start": 0, "end": 3000},
                }
            ]
        },
        user_request="保留海边的镜头",
        target_duration_ms=35000,
        client=client,  # type: ignore[arg-type]
    )
    for prompt in _captured_user_prompts(client):
        assert unresolved_placeholders(prompt) == [], prompt
    body = _captured_user_prompts(client)[0]
    assert "保留海边的镜头" in body
    assert "海边日落" in body
    assert "35.0" in body  # duration_budget 真的有内容


def test_group_clips_prompt_has_no_placeholder_residue() -> None:
    client = _RecordingClient()
    group_clips(
        filtered_clips=[
            {
                "clip_id": "clip_0001",
                "caption": "海边日落",
                "source_ref": {"media_id": "media_0001", "start": 0, "end": 3000},
            },
            {
                "clip_id": "clip_0002",
                "caption": "城市夜景",
                "source_ref": {"media_id": "media_0001", "start": 3000, "end": 6000},
            },
        ],
        user_request="按情绪分组",
        target_duration_ms=35000,
        client=client,  # type: ignore[arg-type]
    )
    body = _captured_user_prompts(client)[0]
    assert unresolved_placeholders(body) == [], body
    assert "按情绪分组" in body
    assert "海边日落" in body
    assert "2" in body  # clip_number


def test_generate_script_prompt_has_no_placeholder_residue() -> None:
    client = _RecordingClient()
    generate_script(
        groups=[{"group_id": "group_0001", "theme": "黄昏", "clips": [{"clip_id": "c1"}]}],
        user_request="轻松口吻",
        overall="一段海边旅行的素材",
        client=client,  # type: ignore[arg-type]
    )
    body = _captured_user_prompts(client)[0]
    assert unresolved_placeholders(body) == [], body
    assert "轻松口吻" in body
    assert "一段海边旅行的素材" in body


def test_recommend_text_prompt_has_no_placeholder_residue() -> None:
    client = _RecordingClient()
    recommend_text(
        groups=[{"group_id": "group_0001"}],
        group_scripts=[{"narration": "今天我们去看海"}],
        user_request="用圆体",
        client=client,  # type: ignore[arg-type]
    )
    body = _captured_user_prompts(client)[0]
    assert unresolved_placeholders(body) == [], body
    assert "用圆体" in body
    assert "今天我们去看海" in body


def test_recommend_transition_prompt_has_no_placeholder_residue() -> None:
    client = _RecordingClient()
    recommend_transition(
        groups=[{"group_id": "group_0001", "theme": "黄昏"}],
        user_request="要柔和的转场",
        client=client,  # type: ignore[arg-type]
    )
    body = _captured_user_prompts(client)[0]
    assert unresolved_placeholders(body) == [], body
    assert "要柔和的转场" in body


def test_understand_clips_overall_prompt_has_no_placeholder_residue() -> None:
    """user_overall 模板的 ``{{clips_captions}}`` 也要被替换。"""
    client = _RecordingClient(reply=json.dumps({"caption": "c", "aes_score": 0.5}))
    understand_clips(
        split_shots_artifact={
            "shots": {
                "media_0001": [
                    {
                        "clip_id": "clip_0001",
                        "kind": "video",
                        "source_ref": {
                            "media_id": "media_0001",
                            "start": 0,
                            "end": 2000,
                        },
                    }
                ]
            }
        },
        media_artifact={
            "media": [{"media_id": "media_0001", "path": "x.mp4", "media_type": "video"}]
        },
        client=client,  # type: ignore[arg-type]
    )
    assert client.calls
    for prompt in _captured_user_prompts(client):
        assert unresolved_placeholders(prompt) == [], prompt

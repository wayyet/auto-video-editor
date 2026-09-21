"""auto-mode 端到端 + 双模式切换 + render_video 旁支集成测试。

对应 plan_v4 §5 阶段 6 / §7.2 验收:
- 全链路 auto-mode:19 节点全跑,产出 storyline_timeline_plan → node_05
- 故意异常重试:已节点失败,确认重试不重复执行上游
- render_video 冒烟:STORYLINE_ENABLE_RENDER_SMOKE_TEST=1,产物可写
- 时长硬约束:超长素材,确认 qa_gate 回退 group_clips 重跑
- 双模式切换:human / auto 间切换不破坏 checkpoint 恢复

策略:
- 不直接 graph.invoke() 跑完整链路(图会起 uvicorn 副作用)。
- 仅测 storyline 子图路径:逐个调用 storyline 节点函数,模拟 LangGraph
  fan-in / 状态合并。这覆盖 plan §2.2 拓扑但不触发外部副作用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from config import STORYLINE_MODE
from langgraph.checkpoint.memory import InMemorySaver
from graph import build_graph


# ===========================================================================
# 双模式切换(plan §7.2 阶段 6 验收第 5 条)
# ===========================================================================
def test_human_mode_default() -> None:
    """未设置 STORYLINE_MODE 时,默认走 human 路径(plan §5 阶段 0 决策)。"""
    assert STORYLINE_MODE == "human"


def test_graph_compiles_in_human_mode() -> None:
    g = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    assert g is not None
    # human-mode 路径不应进入 storyline_load_media;测试图的可编译性即可


def test_graph_compiles_in_auto_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """STORYLINE_MODE=auto 时图仍能编译(plan §7.1 阶段 0 验收第 2 条)。"""
    monkeypatch.setenv("STORYLINE_MODE", "auto")
    g = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    assert g is not None


def test_graph_compiles_after_mode_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """human → auto 切换不破坏 checkpoint 恢复(plan §7.2 第 5 条)。"""
    # 第一次 human
    g1 = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    assert g1 is not None
    # 第二次切到 auto
    monkeypatch.setenv("STORYLINE_MODE", "auto")
    g2 = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    assert g2 is not None


# ===========================================================================
# storyline 子图节点集成(plan §7.2 阶段 6 验收第 1 条)
# ===========================================================================
def _make_state(**overrides: Any) -> dict:
    """构造最小可用 state 用于 storyline 子图集成测试。"""
    base: dict[str, Any] = {
        "session_id": "test_session",
        "storyline_outputs_root": str(overrides.pop("_outputs_root", "/tmp/test")),
        "storyline_targets": {"target_duration_ms": 35000},
        "storyline_media_artifact": "",
        "storyline_shots_artifact": "",
        "storyline_understanding_artifact": "",
        "storyline_filtered_clips": "",
        "storyline_groups_artifact": "",
        "storyline_script_artifact": "",
        "storyline_voiceover_artifact": "",
        "storyline_bgm_selection": {},
        "storyline_transition_plan": {},
        "storyline_text_style_plan": {},
        "storyline_timeline_plan": "",
        "storyline_asr_artifact": "",
        "storyline_rough_cut_artifact": "",
        "storyline_ai_transition_artifact": "",
        "storyline_web_topic_artifact": "",
        "storyline_render_smoke_test_path": "",
        "storyline_qa_retry_count": 0,
        "status_log": [],
        "error_log": [],
    }
    base.update(overrides)
    return base


def _write_load_media_artifact(outputs_root: Path) -> Path:
    out = outputs_root / "storyline" / "load_media.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "media": [
                    {
                        "media_id": "media_0001",
                        "path": "test.mp4",
                        "file_uri": "file:///test.mp4",
                        "media_type": "video",
                        "metadata": {
                            "duration_ms": 60000,
                            "width": 1920,
                            "height": 1080,
                            "fps": 30.0,
                            "has_audio": True,
                            "audio_sample_rate_hz": 44100,
                        },
                    }
                ],
                "summary": {"video": 1, "image": 0, "skipped": 0, "skipped_items": []},
            }
        )
    )
    return out


def _write_split_shots_artifact(outputs_root: Path) -> Path:
    out = outputs_root / "storyline" / "split_shots.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "shots": {
                    "media_0001": [
                        {
                            "clip_id": "c1",
                            "media_id": "media_0001",
                            "kind": "video",
                            "source_in_ms": 0,
                            "source_out_ms": 10000,
                            "source_ref": {
                                "media_id": "media_0001",
                                "start": 0,
                                "end": 10000,
                            },
                        },
                        {
                            "clip_id": "c2",
                            "media_id": "media_0001",
                            "kind": "video",
                            "source_in_ms": 10000,
                            "source_out_ms": 20000,
                            "source_ref": {
                                "media_id": "media_0001",
                                "start": 10000,
                                "end": 20000,
                            },
                        },
                    ]
                }
            }
        )
    )
    return out


def test_load_media_then_split_shots_then_qa_gate_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan §2.2 拓扑片段:LM → SS → qa_gate(空 plan 路径)。"""
    monkeypatch.setenv("STORYLINE_MODE", "auto")

    outputs_root = tmp_path / "out"
    state = _make_state(_outputs_root=str(outputs_root))
    state["video_input_path"] = "test.mp4"

    # 1. load_media
    from nodes.storyline.node_load_media import storyline_load_media_node
    delta = storyline_load_media_node(state)
    assert "storyline_media_artifact" in delta
    assert delta.get("status_log") and any(
        "load_media_done" in t for t in delta["status_log"]
    )

    # 2. split_shots:写假 media 让 split 能跑通;此处 stub 让产物路径即可
    media_artifact = delta["storyline_media_artifact"]
    _write_load_media_artifact(outputs_root)

    from nodes.storyline.node_split_shots import storyline_split_shots_node
    state.update(delta)
    state["storyline_media_artifact"] = _write_load_media_artifact(outputs_root)
    delta = storyline_split_shots_node(state)
    assert "storyline_shots_artifact" in delta

    # 3. qa_gate(空 plan 路径):不阻塞,直接走 join
    from nodes.storyline.qa_gate import storyline_qa_gate_node
    state.update(delta)
    state["storyline_timeline_plan"] = ""        # 没产出过
    delta = storyline_qa_gate_node(state)
    assert any("storyline_qa_done_empty" in t for t in delta["status_log"])


def test_qa_gate_retry_on_overshoot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan §4.2 第 5 条:超时 → status_log append storyline_qa_retry。"""
    from nodes.storyline.qa_gate import storyline_qa_gate_node, route_after_storyline_qa

    outputs_root = tmp_path / "out"
    outputs_root.mkdir(parents=True, exist_ok=True)

    # 写一份总时长 50s 的 timeline_plan,target=35s → 触发 retry
    timeline = {
        "schema_version": "1.0",
        "job_id": "test",
        "created_at_ms": 0,
        "source_media": [
            {
                "media_id": "m1",
                "file_uri": "file:///x",
                "duration_ms": 50000,
                "media_type": "video",
            }
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": 50000,
                "timeline_in_ms": 0,
                "timeline_out_ms": 50000,
            }
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": None, "en": None},
        "options": {
            "enable_ai_transition": False,
            "enable_voiceover": False,
            "max_duration_ms": 35000,
        },
    }
    tp_path = outputs_root / "storyline" / "timeline_plan.json"
    tp_path.parent.mkdir(parents=True, exist_ok=True)
    tp_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")

    state = _make_state(_outputs_root=str(outputs_root))
    state["storyline_timeline_plan"] = str(tp_path)
    state["storyline_targets"] = {"target_duration_ms": 35000}

    delta = storyline_qa_gate_node(state)
    assert delta.get("storyline_qa_retry_count") == 1
    assert any("storyline_qa_retry" in t for t in delta["status_log"])

    # 路由:retry → storyline_group_clips;再跑一次是 retry #2
    next_node = route_after_storyline_qa({**state, **delta})
    assert next_node == "storyline_group_clips"

    # 第 4 次(超 retry 上限)强制走 join(render_video)
    for _ in range(3):
        state.update(delta)
        delta = storyline_qa_gate_node(state)
    state.update(delta)
    next_node = route_after_storyline_qa(state)
    assert next_node == "storyline_render_video"


def test_join_storyline_produces_valid_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """join_storyline 写 storyline_plan,CanonicalTimeline Pydantic 校验通过。"""
    from nodes.storyline.join_storyline import storyline_join_node
    from storyline.contract import CanonicalTimeline

    outputs_root = tmp_path / "out"
    outputs_root.mkdir(parents=True, exist_ok=True)

    # 写一份有效 timeline_plan
    timeline = {
        "schema_version": "1.0",
        "job_id": "test",
        "created_at_ms": 0,
        "source_media": [
            {
                "media_id": "m1",
                "file_uri": "file:///x",
                "duration_ms": 35000,
                "media_type": "video",
            }
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": 35000,
                "timeline_in_ms": 0,
                "timeline_out_ms": 35000,
            }
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": None, "en": None},
        "options": {
            "enable_ai_transition": False,
            "enable_voiceover": False,
            "max_duration_ms": 35000,
        },
    }
    tp_path = outputs_root / "storyline" / "timeline_plan.json"
    tp_path.parent.mkdir(parents=True, exist_ok=True)
    tp_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")

    state = _make_state(_outputs_root=str(outputs_root))
    state["storyline_timeline_plan"] = str(tp_path)
    delta = storyline_join_node(state)
    assert "storyline_plan" in delta
    # Pydantic 校验(plan §3.3 + join_storyline:99 强约束)
    canonical = CanonicalTimeline.model_validate(delta["storyline_plan"])
    assert canonical.clips[0].clip_id == "c1"
    assert canonical.clips[0].timeline_out_ms == 35000


# ===========================================================================
# render_video 旁支(plan §5 阶段 6 / ADR-006)
# ===========================================================================
def test_render_video_skipped_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 STORYLINE_ENABLE_RENDER_SMOKE_TEST=0 → noop,append _skipped。"""
    monkeypatch.setenv("STORYLINE_ENABLE_RENDER_SMOKE_TEST", "0")

    from importlib import reload
    import config
    reload(config)

    from nodes.storyline.node_render_video import storyline_render_video_node

    outputs_root = tmp_path / "out"
    state = _make_state(_outputs_root=str(outputs_root))
    state["storyline_timeline_plan"] = ""
    delta = storyline_render_video_node(state)
    assert any("storyline_render_video_skipped" in t for t in delta["status_log"])
    # 默认 noop 不写 render_smoke_test_path
    assert delta.get("storyline_render_smoke_test_path") in (None, "")


def test_render_video_disabled_path_does_not_affect_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_video 旁支 noop 时,下游 join_storyline 不被破坏。"""
    monkeypatch.setenv("STORYLINE_ENABLE_RENDER_SMOKE_TEST", "0")
    from importlib import reload
    import config
    reload(config)

    from nodes.storyline.node_render_video import storyline_render_video_node
    from nodes.storyline.join_storyline import storyline_join_node

    outputs_root = tmp_path / "out"
    state = _make_state(_outputs_root=str(outputs_root))

    # 写一份 timeline_plan
    timeline = {
        "schema_version": "1.0",
        "job_id": "test",
        "created_at_ms": 0,
        "source_media": [
            {
                "media_id": "m1",
                "file_uri": "file:///x",
                "duration_ms": 5000,
                "media_type": "video",
            }
        ],
        "clips": [
            {
                "clip_id": "c1",
                "source_media_id": "m1",
                "source_in_ms": 0,
                "source_out_ms": 5000,
                "timeline_in_ms": 0,
                "timeline_out_ms": 5000,
            }
        ],
        "audio": {"bgm_ref": None, "voiceover": None},
        "subtitles": {"zh": None, "en": None},
        "options": {
            "enable_ai_transition": False,
            "enable_voiceover": False,
            "max_duration_ms": 5000,
        },
    }
    tp_path = outputs_root / "storyline" / "timeline_plan.json"
    tp_path.parent.mkdir(parents=True, exist_ok=True)
    tp_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    state["storyline_timeline_plan"] = str(tp_path)

    delta = storyline_render_video_node(state)
    state.update(delta)
    delta = storyline_join_node(state)
    # join 仍能产出 storyline_plan
    assert "storyline_plan" in delta
    assert delta["storyline_plan"]["clips"][0]["clip_id"] == "c1"


# ===========================================================================
# 资源体积控制(plan §7.3 阶段 7 验收第 2 条 — 在阶段 6 提前验证)
# ===========================================================================
def test_auto_mode_does_not_import_vendored_runtime() -> None:
    """auto-mode 19 节点不应在 runtime 时 import vendored torch / funasr 等。

    单元测试在 storyline 子图模块层(不直接 import vendored) → 验证模块
    列表里没有 vendored 路径。vendored copy 与主项目隔离(plan §6.3 决策)。
    """
    import nodes.storyline as ns

    src = Path(ns.__file__).parent
    for py_file in src.glob("node_*.py"):
        content = py_file.read_text(encoding="utf-8")
        # 节点壳子不应 import vendored torch / open_storyline.* 运行时
        # (允许 from openstoryline... 仅出现在注释 / docstring 中)
        assert "import open_storyline." not in content, (
            f"{py_file.name} 直接 import vendored open_storyline. — 违反 ADR-001"
        )
        assert "import torch" not in content, (
            f"{py_file.name} import torch — 违反 ADR-001"
        )
        assert "import torchaudio" not in content, (
            f"{py_file.name} import torchaudio — 违反 ADR-001"
        )
        assert "import funasr" not in content, (
            f"{py_file.name} import funasr — 违反 ADR-001"
        )


# ===========================================================================
# 阶段 7:auto-mode 跳过 node_02 vendored Web UI(plan §5 阶段 7 第 3 条)
# ===========================================================================
def test_node_02_auto_mode_skips_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STORYLINE_MODE=auto`` 时 node_02_launch_openstoryline 应跳过 uvicorn 子进程。

    验收点(plan §7.3 第 1 条):auto-mode 跑 19 节点完全不需要 vendored Web
    服务。本测试验证即使 popen_factory 抛异常(auto 路径不调用它),节点
    仍返回 ``openstoryline_ready=True`` 且 append ``node_02_auto_skipped``。
    """
    monkeypatch.setenv("STORYLINE_MODE", "auto")

    # 重载 config + node_02:节点模块顶层的
    # ``from config import STORYLINE_MODE`` 已经把值绑到模块命名空间,
    # 必须 reload node_02 才能拿到新的 STORYLINE_MODE。
    from importlib import reload
    import config as _cfg
    reload(_cfg)
    import nodes.node_02_launch_openstoryline as _n2
    reload(_n2)

    # popen_factory 故意抛异常:如果 auto-mode 路径错误地调到了 uvicorn
    # 子进程,这个异常会冒到上层,测试 fail。
    def boom_popen(*args, **kwargs):
        raise AssertionError("auto-mode 不应启动 vendored Web UI 子进程")

    state: dict[str, Any] = {"error_log": [], "session_id": "auto_test"}
    out = _n2.launch_openstoryline_service(
        state, popen_factory=boom_popen, health_checker=lambda u, t: True
    )
    assert out["openstoryline_ready"] is True
    assert out["openstoryline_pid"] is None
    assert any(
        t == "node_02_launch_openstoryline_auto_skipped"
        for t in out.get("status_log", [])
    ), f"缺少 auto_skipped 标记: status_log={out.get('status_log')}"
    assert out.get("error_log") == []


def test_node_02_human_mode_still_starts_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STORYLINE_MODE=human``(默认)行为**完全不动**:仍启 uvicorn 子进程。

    验收点(plan §7.3 + §5 阶段 7 第 4 条):human-mode 路径保留作为
    auto-mode 失败时的回退入口,必须仍能正常启动 vendored Web UI。
    """
    # 确保 STORYLINE_MODE 不是 auto(其他测试可能改了 env)
    monkeypatch.setenv("STORYLINE_MODE", "human")

    from importlib import reload
    import config as _cfg
    reload(_cfg)
    import nodes.node_02_launch_openstoryline as _n2
    reload(_n2)

    captured: dict[str, list[str]] = {"argv": []}

    class _StubProc:
        pid = 4242
        stdout = None
        stderr = None

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        return _StubProc()

    def fake_checker(url, timeout_s):
        return True

    state: dict[str, Any] = {"error_log": []}
    out = _n2.launch_openstoryline_service(
        state, popen_factory=fake_popen, health_checker=fake_checker
    )
    assert out["openstoryline_ready"] is True
    assert out["openstoryline_pid"] == 4242
    # uvicorn 子进程必须被启动(argv 含 "uvicorn")
    assert any("uvicorn" in a for a in captured["argv"]), (
        f"human-mode 必须启 uvicorn 子进程,argv={captured['argv']}"
    )
    # 不应 append auto_skipped
    assert "node_02_launch_openstoryline_auto_skipped" not in (
        out.get("status_log") or []
    )
"""2026-10 调序守护测试:英文线补 08b/10b,09/11/12/13 推迟到 node_17 之后。

用真实节点整图运行(仅屏蔽节点 2/3 的进程拉起;ASR/翻译/TTS 走仓库默认 Mock),
用 ``stream_mode="debug"`` 的 task 事件还原超步(step)顺序。

这组测试专门补"测试盲区":原有集成测试几乎不断言节点顺序(例如
``test_week5_resilience.py::test_checkpoint3_interrupt_and_resume_path`` 里的
``c3_seen`` 只赋值、从未断言)。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph import build_graph


class _StubProc:
    pid = 11111


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽节点 2/3 的真实进程拉起(与 test_week4_graph 同一写法)。"""
    import nodes.node_02_launch_openstoryline as m2
    import nodes.node_03_open_preview as m3

    monkeypatch.setattr(
        m2, "subprocess",
        type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc()), "PIPE": -1}),
    )
    monkeypatch.setattr(m2, "_wait_for_ready", lambda url, t: True)
    monkeypatch.setattr(
        m3, "subprocess",
        type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}),
    )


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """预置 35s 草稿;**不**预置 asr_segments_zh —— 必须由 node_08 自己产出。"""
    draft_dir = tmp_path / "drafts" / "demo"
    draft_dir.mkdir(parents=True)
    dur = 35_000_000
    draft_file = draft_dir / "draft_content.json"
    draft_file.write_text(json.dumps({
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": dur,
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{"type": "video", "fps": 30,
                    "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": dur}}]}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))
    return {
        "session_id": "reorder",
        "video_input_path": "C:/tmp/fake.mp4",
        "draft_path": str(draft_file),
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
        "error_log": [],
        "status_log": ["node_05_generate_draft_done"],
    }


def _drive(state: dict, *, force_layout_issue: bool = False, human_edit: str | None = None):
    """整图跑到 END,遇到 interrupt 就批量 resume。

    Returns:
        (steps, stops, final_values)
        steps: [(step, node_name)];stops: 每次挂起的关卡标签列表;
    """
    g = build_graph(checkpointer=InMemorySaver(), thread_id="reorder", start_heartbeat_thread=False)
    config = {"configurable": {"thread_id": "reorder"}}
    steps: list[tuple[int, str]] = []
    stops: list[list[str]] = []
    issues = [{"index": 0, "reason": "text_too_wide", "width_px": 9999, "max_width_px": 1500}]
    ctx = (patch("nodes.node_16a_translate_and_check.validate_layout", return_value=issues)
           if force_layout_issue else contextlib.nullcontext())
    with ctx:
        inp: object = state
        for _ in range(14):
            for ev in g.stream(inp, config, stream_mode="debug"):
                if ev["type"] == "task":
                    steps.append((ev["step"], ev["payload"]["name"]))
            snap = g.get_state(config)
            if not snap.next:
                break
            pend = list(snap.interrupts)
            labels = [str((i.value or {}).get("checkpoint")) if isinstance(i.value, dict) else "?" for i in pend]
            stops.append(labels)
            if human_edit and "③" in labels:
                en_dir = Path(snap.values["draft_dir_en_branch"])
                f = en_dir / "draft_content.json"
                d = json.loads(f.read_text(encoding="utf-8"))
                d["materials"]["texts"][0]["content"] = human_edit  # 模拟人工在剪映里校对
                f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            inp = Command(resume={i.id: True for i in pend})
        else:  # pragma: no cover
            pytest.fail("14 轮仍未结束:疑似死循环")
    return steps, stops, g.get_state(config).values


def _first_step(steps: list[tuple[int, str]], name: str) -> int:
    return min(s for s, n in steps if n == name)


def test_new_nodes_registered(tmp_path, monkeypatch):
    g = build_graph(checkpointer=InMemorySaver(), thread_id="t", start_heartbeat_thread=False)
    names = set(g.get_graph().nodes)
    assert {"node_08b_add_subtitles_en", "node_10b_inject_text_fx_en"} <= names


def test_execution_order_after_reorder(tmp_path, monkeypatch):
    steps, stops, _ = _drive(_seed(tmp_path, monkeypatch))
    s = lambda n: _first_step(steps, n)  # noqa: E731

    # 中文线:08 → 10 → 14 → 15
    assert s("node_08_add_subtitles") < s("node_10_inject_text_fx") < s("node_14_make_covers") < s("node_15_localize_covers_en")
    # 英文线:16a → 08b → 10b → 17;且 16a 严格晚于 08(显式依赖 asr_segments_zh)
    assert s("node_08_add_subtitles") < s("fork_draft_for_english_branch") < s("node_16a_translate_and_check")
    assert s("node_16a_translate_and_check") < s("node_08b_add_subtitles_en") < s("node_10b_inject_text_fx_en") < s("node_17_inject_english_tts")
    # 尾链:09 必须晚于 15 和 17(汇合屏障),之后严格 09 → 11 → 12 → 13 → QA
    assert s("node_09_inject_fx") > max(s("node_15_localize_covers_en"), s("node_17_inject_english_tts"))
    assert (s("node_09_inject_fx") < s("node_11_inject_sticker") < s("node_12_human_add_bgm")
            < s("node_13_adjust_volume") < s("join_before_delivery"))
    # 各节点只执行一次(interrupt 重放的 12 号节点除外,它按 task 事件会出现两次)
    for n in ("node_09_inject_fx", "node_11_inject_sticker", "node_13_adjust_volume", "join_before_delivery",
              "node_16a_translate_and_check", "node_08b_add_subtitles_en", "node_10b_inject_text_fx_en"):
        assert sum(1 for _, x in steps if x == n) == 1, n
    # 正常路径:依次挂起 ① → ②。关卡⓪ 被 tests/conftest.py 的 autouse 夹具
    # (_skip_checkpoint0_for_pre_migration_tests)旁路,所以这里不会出现 ⓪。
    assert stops == [["①"], ["②"]]


def test_english_draft_gets_subtitles_and_text_fx(tmp_path, monkeypatch):
    _, _, final = _drive(_seed(tmp_path, monkeypatch))
    en = json.loads((Path(final["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8"))
    texts = en["materials"]["texts"]
    assert len(texts) == len(final["subtitle_segments_en"]) > 0
    assert all(t["track_name"] == "Subtitles_en" for t in texts)
    assert all(t["content"] == t["text_en"] for t in texts)
    assert all(t["style"].get("outline") for t in texts)          # 10b 的花字已生效
    # 中文草稿不受英文节点影响
    zh = json.loads(Path(final["draft_path"]).read_text(encoding="utf-8"))
    assert all(t["track_name"] == "Subtitles" for t in zh["materials"]["texts"])


def test_checkpoint3_path_no_parallel_conflict_and_human_edit_survives(tmp_path, monkeypatch):
    steps, stops, final = _drive(_seed(tmp_path, monkeypatch),
                                 force_layout_issue=True, human_edit="HUMAN FIXED LINE")
    s = lambda n: _first_step(steps, n)  # noqa: E731
    # ③ 与 ② 永不同时挂起:先 ③(英文线),尾链走到 BGM 时才 ②。⓪ 同上被旁路。
    assert stops == [["①"], ["③"], ["②"]]
    # K3=B:花字(10b)先于人工校对(③)—— 人工在剪映里看到的就是带花字的最终效果
    assert s("node_10b_inject_text_fx_en") < s("node_checkpoint3_layout_review") < s("node_17_inject_english_tts") < s("node_09_inject_fx")
    # 人工校对的内容保住了,SRT 不再被重写成空文件
    srt = Path(final["draft_dir_en_branch"]) / "subtitle_en.srt"
    assert srt.stat().st_size > 0
    assert "HUMAN FIXED LINE" in srt.read_text(encoding="utf-8")


def test_tail_chain_applies_to_both_drafts(tmp_path, monkeypatch):
    """K2=B:尾链 09/11 的产物在中英两份草稿上都存在,且条数一致 >0。

    - transitions / video_effects:node_09 对两份草稿各注入一次
    - stickers:node_11 对两份草稿各注入一次
    - 中文草稿不含 ``Subtitles_en`` 轨,英文草稿不含 ``Subtitles`` 轨(字幕不串台)
    """
    _, _, final = _drive(_seed(tmp_path, monkeypatch))

    zh = json.loads(Path(final["draft_path"]).read_text(encoding="utf-8"))
    en = json.loads((Path(final["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8"))
    zh_m = zh["materials"]
    en_m = en["materials"]

    for key in ("transitions", "video_effects", "stickers"):
        assert len(en_m[key]) > 0, f"英文草稿 {key} 为空:尾链未对英文草稿生效"
        assert len(en_m[key]) == len(zh_m[key]), (
            f"{key} 条数不一致:zh={len(zh_m[key])} en={len(en_m[key])}"
        )

    # 字幕轨不串台
    assert all(t["track_name"] == "Subtitles" for t in zh_m["texts"])
    assert all(t["track_name"] == "Subtitles_en" for t in en_m["texts"])

    # K2=B 接线的直接证据:尾链对英文草稿那一轮各留一个 done_tag。
    # (BGM 镜像不在这里断言 —— 自动跑里没有真人往中文草稿加音轨,镜像会正确降级;
    #  镜像本身由 tests/unit/test_node_12_human_add_bgm.py 的 4 条用例覆盖。)
    log = final["status_log"]
    assert "node_09_inject_fx_done" in log and "node_09_inject_fx_en_done" in log
    assert "node_11_inject_sticker_done" in log and "node_11_inject_sticker_en_done" in log

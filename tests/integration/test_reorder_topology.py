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
from jy_common.native_text import text_display_text
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph import build_graph


def _seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    asr_segments_zh: list[dict] | None = None,
    storyline_plan: dict | None = None,
    script_narration: dict[str, str] | None = None,
) -> dict:
    """预置 35s 草稿;默认**不**预置 asr_segments_zh(B1 ASR 移除后的常态输入)。

    ``asr_segments_zh`` 仅用于显式验证"有源字幕"的老路径(关卡③ 布局人工校对),
    模拟旧线程恢复时 state 里已有段落的场景;新流程 node_08 恒产出空列表。

    ``storyline_plan`` + ``script_narration``(2026-11-09 阶段 1):给 node_08 的
    中文旁白字幕落草稿提供 storyline_plan 与 script 产物;同时给种子草稿的单一
    视频段补 source_timerange(0-35s,与 plan 的 clip 源范围一致)与素材路径,
    供源范围匹配。
    """
    draft_dir = tmp_path / "drafts" / "demo"
    draft_dir.mkdir(parents=True)
    dur = 35_000_000
    draft_file = draft_dir / "draft_content.json"
    segment: dict = {"id": "s1", "target_timerange": {"start": 0, "duration": dur}}
    if storyline_plan is not None:
        segment["source_timerange"] = {"start": 0, "duration": dur}
    draft_file.write_text(json.dumps({
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": dur,
        "materials": {"videos": [{"id": "v1", "path": "C:/tmp/fake.mp4"}]},
        "tracks": [{"type": "video", "fps": 30, "segments": [segment]}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))
    state = {
        "session_id": "reorder",
        "video_input_path": "C:/tmp/fake.mp4",
        "draft_path": str(draft_file),
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
        "error_log": [],
        "status_log": ["node_05_generate_draft_done"],
    }
    if asr_segments_zh is not None:
        state["asr_segments_zh"] = asr_segments_zh
    if storyline_plan is not None:
        state["storyline_plan"] = storyline_plan
    if script_narration is not None:
        script_file = draft_dir / "script.json"
        script_file.write_text(
            json.dumps(
                {
                    "group_scripts": [
                        {"group_id": gid, "narration": text}
                        for gid, text in script_narration.items()
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state["storyline_script_artifact"] = str(script_file)
    return state


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
    # 英文线:16a → 08b → 10b → 17;且 16a 严格晚于 08(经 fork 的图边,拓扑不变)
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
    # 正常路径:依次挂起 ① → ②(阶段3 后关卡⓪ 已随 human 模式链删除)。
    assert stops == [["①"], ["②"]]


def test_english_branch_skips_cleanly_without_source_subtitles(tmp_path, monkeypatch):
    """B1 ASR 移除:node_08 不再产 ASR 段 → 16a 正常 skip(非错误)、08b 只记 skip 状态。

    英文分支的**节点照常执行**(边不动),只是内容跳过:不产英文字幕素材,
    也不阻断 TTS(node_17)与尾链。
    """
    steps, stops, final = _drive(_seed(tmp_path, monkeypatch))
    s = lambda n: _first_step(steps, n)  # noqa: E731

    log = final["status_log"]
    assert "skipped_no_source_subtitles" in log
    assert "node_08b_skipped_no_en_segments" in log
    # 跳过不算错误
    assert not any("asr_segments_zh" in e for e in final.get("error_log") or [])

    # 英文线节点仍全部执行且顺序不变
    assert s("node_08_add_subtitles") < s("fork_draft_for_english_branch") < s("node_16a_translate_and_check")
    assert s("node_16a_translate_and_check") < s("node_08b_add_subtitles_en") < s("node_10b_inject_text_fx_en") < s("node_17_inject_english_tts")

    # 无源字幕 → 不写英文字幕素材/翻译段
    assert not (final.get("subtitle_segments_en") or [])
    en = json.loads((Path(final["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8"))
    assert not (en.get("materials", {}).get("texts") or [])
    assert not [t for t in en.get("tracks", []) if t.get("type") == "text" and "Subtitles_en" == t.get("track_name")]
    # 关卡③不触发(layout_issues_detected 未设置),正常路径 ① → ②
    assert stops == [["①"], ["②"]]


def test_english_draft_gets_subtitles_and_text_fx(tmp_path, monkeypatch):
    """有源字幕(模拟旧线程恢复时 state 已带 asr_segments_zh)→ 英文字幕照常写入。

    node_08 不写也不清该键(B1 §3.3 取"缺省"),state 里的段经 {**state} 透传给 16a。
    """
    _, _, final = _drive(_seed(tmp_path, monkeypatch, asr_segments_zh=[
        {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
        {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
    ]))
    en = json.loads((Path(final["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8"))
    texts = en["materials"]["texts"]
    assert len(texts) == len(final["subtitle_segments_en"]) > 0
    assert all(t["track_name"] == "Subtitles_en" for t in texts)
    # 2026-11-07 文本链原生升级:content 是原生 styles JSON 字符串,显示文本经
    # text_display_text 还原后仍与 text_en 一致;material 带原生必填键。
    assert all(text_display_text(t) == t["text_en"] for t in texts)
    assert all(t["type"] == "text" for t in texts)
    # 字幕铺上原生 text 轨道(关卡③ 人工可见可改),轨道段与 texts 一一对应
    text_tracks = [t for t in en["tracks"] if t.get("type") == "text"]
    assert len(text_tracks) == 1
    assert len(text_tracks[0]["segments"]) == len(texts)
    assert all(
        seg["material_id"] == mat["id"]
        for seg, mat in zip(text_tracks[0]["segments"], texts)
    )
    # 10b 的花字已生效(阶段 4:原生 material_animations + text 轨段引用绑定)
    anim_ids = {a["id"] for a in (en["materials"].get("material_animations") or [])}
    assert anim_ids, "英文草稿应有 material_animations"
    en_text_segs = [s for t in en["tracks"] if t.get("type") == "text" for s in t["segments"]]
    assert all(
        any(r in anim_ids for r in (s.get("extra_material_refs") or []))
        for s in en_text_segs
    )
    # 中文草稿不受英文节点影响;B1 ASR 移除后 node_08 不再产中文字幕
    zh = json.loads(Path(final["draft_path"]).read_text(encoding="utf-8"))
    assert not (zh.get("materials", {}).get("texts") or [])
    assert not [t for t in zh.get("tracks", []) if t.get("track_name") == "Subtitles_en"]


def test_checkpoint3_path_no_parallel_conflict_and_human_edit_survives(tmp_path, monkeypatch):
    steps, stops, final = _drive(
        _seed(tmp_path, monkeypatch, asr_segments_zh=[
            {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
            {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
        ]),
        force_layout_issue=True, human_edit="HUMAN FIXED LINE")
    s = lambda n: _first_step(steps, n)  # noqa: E731
    # ③ 与 ② 永不同时挂起:先 ③(英文线),尾链走到 BGM 时才 ②。
    assert stops == [["①"], ["③"], ["②"]]
    # K3=B:花字(10b)先于人工校对(③)—— 人工在剪映里看到的就是带花字的最终效果
    assert s("node_10b_inject_text_fx_en") < s("node_checkpoint3_layout_review") < s("node_17_inject_english_tts") < s("node_09_inject_fx")
    # 人工校对的内容保住了,SRT 不再被重写成空文件
    srt = Path(final["draft_dir_en_branch"]) / "subtitle_en.srt"
    assert srt.stat().st_size > 0
    assert "HUMAN FIXED LINE" in srt.read_text(encoding="utf-8")


def test_tail_chain_applies_to_both_drafts(tmp_path, monkeypatch):
    """K2=B:尾链 09/11 的产物在中英两份草稿上都存在,且条数一致。

    - transitions / video_effects:node_09 对两份草稿各注入一次(>0)
    - stickers:node_11 对两份草稿各注入一次(占位资源 fail-closed,当前 0;一致即对称)
    - 中文草稿不含 ``Subtitles_en`` 轨,英文草稿不含 ``Subtitles`` 轨(字幕不串台)
    """
    _, _, final = _drive(_seed(tmp_path, monkeypatch))

    zh = json.loads(Path(final["draft_path"]).read_text(encoding="utf-8"))
    en = json.loads((Path(final["draft_dir_en_branch"]) / "draft_content.json").read_text(encoding="utf-8"))
    zh_m = zh["materials"]
    en_m = en["materials"]

    for key in ("transitions", "video_effects"):
        assert len(en_m[key]) > 0, f"英文草稿 {key} 为空:尾链未对英文草稿生效"
        assert len(en_m[key]) == len(zh_m[key]), (
            f"{key} 条数不一致:zh={len(zh_m[key])} en={len(en_m[key])}"
        )

    # 贴纸(阶段 4):模板占位资源 fail-closed → 两侧一致(当前为 0);
    # 真实 ID 实机回填后自然转正。条数一致仍验证 node_11 双草稿对称性。
    assert len(en_m["stickers"]) == len(zh_m["stickers"])

    # 字幕轨不串台(B1 ASR 移除后本用例无源字幕,两侧 texts 为空,断言自然空真)
    assert all(t["track_name"] == "Subtitles" for t in zh_m.get("texts") or [])
    assert all(t["track_name"] == "Subtitles_en" for t in en_m.get("texts") or [])

    # K2=B 接线的直接证据:尾链对英文草稿那一轮各留一个 done_tag。
    # (BGM 镜像不在这里断言 —— 自动跑里没有真人往中文草稿加音轨,镜像会正确降级;
    #  镜像本身由 tests/unit/test_node_12_human_add_bgm.py 的 4 条用例覆盖。)
    log = final["status_log"]
    assert "node_09_inject_fx_done" in log and "node_09_inject_fx_en_done" in log
    assert "node_11_inject_sticker_done" in log and "node_11_inject_sticker_en_done" in log


def test_zh_draft_gets_native_subtitles(tmp_path, monkeypatch):
    """阶段 1(2026-11-09 效果缺失修复):有旁白 → node_08 把中文旁白逐句写成
    原生 text 素材 + text 轨道(计划 §8 阶段 1 集成验收:materials.texts > 0、
    text track 存在、每段 material_id 都存在)。

    storyline 链在集成桩下(fake 媒体)产出空 clips 并经 join 覆盖 storyline_plan、
    经 generate_script 覆盖 storyline_script_artifact(写到共享 outputs 目录)。
    这里把 join 的时间线读取替换为种子 plan、把 generate_script 替换为"保留种子
    script 产物"(join 的 Pydantic 校验等其余逻辑照常),使 generate_draft →
    node_08 走真实全链;被测对象是 05→07→08 的落草稿行为,不是 stub 链本身。
    """
    import nodes.storyline.join_storyline as join_mod


    plan = {
        "schema_version": "1.0", "job_id": "reorder", "created_at_ms": 0,
        "source_media": [{
            "media_id": "media_0001", "file_uri": "file:///C:/tmp/fake.mp4",
            "duration_ms": 35000, "media_type": "video",
        }],
        "clips": [{
            "clip_id": "clip_0001", "source_media_id": "media_0001",
            "source_in_ms": 0, "source_out_ms": 35000,
            "timeline_in_ms": 0, "timeline_out_ms": 35000,
            "voiceover_id": "voiceover_0001",
        }],
        "audio": {"bgm_ref": None,
                  "voiceover": json.dumps({"group_0001": "voiceover_0001"})},
        "subtitles": {"zh": None, "en": None},
        "options": {"enable_ai_transition": False, "enable_voiceover": True,
                    "max_duration_ms": 35000},
    }
    script_file = tmp_path / "script.json"
    script_file.write_text(
        json.dumps(
            {"group_scripts": [{"group_id": "group_0001", "narration": "你好。世界"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(join_mod, "_read_timeline_dict", lambda state: plan)
    # graph.py 是 `from ... import storyline_generate_script_node` 符号直连,
    # patch 必须打在 graph 模块的绑定上(join 走模块属性查找所以不受此影响)。
    monkeypatch.setattr(
        "graph.storyline_generate_script_node",
        lambda state: {
            "storyline_script_artifact": str(script_file),
            "status_log": ["storyline_generate_script_done"],
        },
    )
    _, _, final = _drive(_seed(
        tmp_path, monkeypatch, storyline_plan=plan,
        script_narration={"group_0001": "你好。世界"},
    ))

    zh = json.loads(Path(final["draft_path"]).read_text(encoding="utf-8"))
    texts = zh.get("materials", {}).get("texts") or []
    assert [t["id"] for t in texts] == ["text-zh-1", "text-zh-2"]  # 逐句:你好。/世界
    text_tracks = [t for t in zh.get("tracks", []) if t.get("type") == "text"]
    assert len(text_tracks) == 1
    ids = {t["id"] for t in texts}
    assert all(seg["material_id"] in ids for seg in text_tracks[0]["segments"])
    # state 侧段落(供阶段 2 的 node_16a 英文翻译消费)
    segs = final.get("subtitle_segments_zh") or []
    assert [s["text_zh"] for s in segs] == ["你好。", "世界"]

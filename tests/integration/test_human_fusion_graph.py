"""human 模式融合(``STORYLINE_HUMAN_FUSION=1``)整图测试。

把 auto 模式的 21 个 storyline 节点,以"侧支"挂到 human 主图相关节点旁
(设计见《剧情线自动模式节点并入人工模式主图设计与执行计划》)。

A 组 —— 拓扑(不运行,只比边集):
    开关关闭 = 与改动前逐边一致;开关开启 = 只多出 21 条"锚点 → 侧支"边,
    侧支无出边,human 节点之间的边一条不少;auto 模式不受开关影响。

B 组 —— 真实整图运行(真 LangGraph + InMemorySaver + ffmpeg 生成的真 mp4):
    - 21 个侧支节点各只执行一次,且恰好在锚点之后的下一个超步;
    - human 节点的执行顺序 / 超步号 / 挂起关卡 / 终值与开关关闭时完全一致;
    - 产物只写进 ``<outputs>/<job>/fusion/``;human 的 ``storyline_plan`` 不被覆盖;
    - 没人点【导入视频】时 load_media 调用 0 次(守护 test_no_auto_import_video 的约束);
    - 侧支异常 / 被 kill switch 禁用时,human 主链不受影响。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import config
import graph as graph_mod
from graph import build_graph
from nodes.storyline.fusion import FUSION_SPECS, anchor_of

SIDE_NAMES = {s.node_name for s in FUSION_SPECS}


def _is_side(name: str) -> bool:
    return name.startswith("storyline_")


# ===========================================================================
# A 组:拓扑
# ===========================================================================
def _topo(g) -> tuple[set, set, dict]:
    """``StateGraph`` 的边:普通边 / 汇合(列表)边 / 条件边(含目标映射)。"""
    plain = set(g.edges)
    waiting = {(tuple(sorted(src)), dst) for src, dst in g.waiting_edges}
    branches: dict = {}
    for src, named in g.branches.items():
        for name, spec in named.items():
            ends = getattr(spec, "ends", None)
            branches[(src, name)] = tuple(sorted(ends.items())) if ends else None
    return plain, waiting, branches


def _build(monkeypatch, *, fusion: bool, qc: bool = True, mode: str | None = None):
    monkeypatch.setattr(config, "STORYLINE_HUMAN_FUSION", fusion)
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", qc)
    token = graph_mod._STORYLINE_MODE_OVERRIDE.set(mode) if mode else None
    try:
        return graph_mod._build_state_graph()
    finally:
        if token is not None:
            graph_mod._STORYLINE_MODE_OVERRIDE.reset(token)


@pytest.mark.parametrize("qc", [True, False])
def test_flag_off_has_no_edge_from_human_node_to_storyline_node(monkeypatch, qc):
    plain, _, _ = _topo(_build(monkeypatch, fusion=False, qc=qc))
    assert not [(s, d) for s, d in plain if not _is_side(s) and _is_side(d)]


@pytest.mark.parametrize("qc", [True, False])
def test_flag_on_adds_exactly_the_layout_edges(monkeypatch, qc):
    off = _build(monkeypatch, fusion=False, qc=qc)
    on = _build(monkeypatch, fusion=True, qc=qc)
    po, wo, bo = _topo(off)
    pn, wn, bn = _topo(on)

    # 1. 多出的边 == 规格表里的 21 条"锚点 -> 侧支"
    expected_added = {(anchor_of(s, qc), s.node_name) for s in FUSION_SPECS}
    assert len(expected_added) == 21
    assert pn - po == expected_added
    # 2. 少掉的边全部来自 storyline 节点(auto 链内部边不接)
    assert all(_is_side(s) for s, _ in po - pn)
    # 3. human 节点之间的边一条不少、一条不多
    human_only = lambda edges: {(s, d) for s, d in edges if not _is_side(s) and not _is_side(d)}  # noqa: E731
    assert human_only(pn) == human_only(po)
    # 4. 侧支没有任何出边(普通边 / 汇合边 / 条件边)
    assert not [e for e in pn if _is_side(e[0])]
    assert not [w for w in wn if any(_is_side(x) for x in w[0])]
    assert not [k for k in bn if _is_side(k[0])]
    # 5. 汇合边、条件边:human 的原样保留
    assert wn == {w for w in wo if not any(_is_side(x) for x in w[0])}
    assert bn == {k: v for k, v in bo.items() if not _is_side(k[0])}
    # 6. 锚点都是已注册的 human 节点
    for anchor, _ in expected_added:
        assert anchor in on.nodes and not _is_side(anchor), anchor


@pytest.mark.parametrize("qc", [True, False])
def test_auto_mode_ignores_the_flag(monkeypatch, qc):
    off = _topo(_build(monkeypatch, fusion=False, qc=qc, mode="auto"))
    on = _topo(_build(monkeypatch, fusion=True, qc=qc, mode="auto"))
    assert on == off


def test_flag_on_graph_compiles(monkeypatch):
    for qc in (True, False):
        g = _build(monkeypatch, fusion=True, qc=qc)
        g.compile(checkpointer=InMemorySaver())


# ===========================================================================
# B 组:真实整图运行
# ===========================================================================
class _StubProc:
    pid = 11111


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽节点 2/3 的真实进程拉起(与 test_reorder_topology 同一写法)。"""
    import nodes.node_02_launch_openstoryline as m2
    import nodes.node_03_open_preview as m3

    monkeypatch.setattr(m2, "_wait_for_ready", lambda url, t: True)
    monkeypatch.setattr(
        m3, "subprocess",
        type("S", (), {"Popen": staticmethod(lambda *a, **k: _StubProc())}),
    )


@pytest.fixture(scope="module")
def real_mp4(tmp_path_factory) -> Path:
    """4 秒、带音轨的真 mp4(ffmpeg 合成)。没有 ffmpeg 就跳过整组真实运行测试。"""
    if not shutil.which("ffmpeg"):
        pytest.skip("需要 ffmpeg 生成测试视频")
    out = tmp_path_factory.mktemp("fusion_media") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", str(out)],
        check=True,
    )
    return out


@dataclass
class Run:
    tag: str
    work: Path
    out_root: Path
    steps: list          # [(超步号, 节点名)]
    stops: list          # 每次挂起的关卡标签
    final: dict

    @property
    def fusion_root(self) -> Path:
        return self.out_root / self.tag / "fusion"

    def step_of(self, node: str) -> int:
        return min(s for s, n in self.steps if n == node)

    def supersteps_of(self, node: str) -> set:
        """节点出现过的超步号集合(挂起重放只会让同一超步重复出事件,不会多出一个超步)。"""
        return {s for s, n in self.steps if n == node}


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
    *,
    fusion: bool,
    video: Path | str,
    qc: bool = True,
    auto_import: bool = True,
    disable: Iterable[str] = (),
) -> tuple[Path, Path, dict]:
    """铺好目录与开关,返回 ``(work, out_root, 初始 state)``。

    ``auto_import=True`` 用 ``IMPORT_VIDEO_MANUAL_REQUIRED=False`` 回滚路径模拟"网页点了导入"
    (节点自己把 ``video_input_path`` 当结果写文件,状态记为 imported)。
    """
    work = tmp_path / tag
    out_root = work / "outputs"
    draft_dir = work / "drafts" / "demo"
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
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: out_root)
    monkeypatch.setattr(config, "STORYLINE_HUMAN_FUSION", fusion)
    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", qc)
    monkeypatch.setattr(config, "IMPORT_VIDEO_MANUAL_REQUIRED", not auto_import)
    monkeypatch.setattr(config, "STORYLINE_FUSION_DISABLE", frozenset(disable))

    state = {
        "session_id": tag,
        "video_input_path": str(video),
        "draft_path": str(draft_file),
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
        "error_log": [],
        "status_log": ["node_05_generate_draft_done"],
    }
    return work, out_root, state


def _advance(g, tag: str, inp: object, *, until_first_stop: bool = False) -> tuple[list, list]:
    """推进整图:遇到 interrupt 就批量 resume,直到 END。

    ``until_first_stop=True`` 时只跑到第一次挂起就返回(用来制造"在途存档")。
    返回 ``(steps, stops)``;steps = ``[(超步号, 节点名)]``,stops = 每次挂起的关卡标签。
    """
    cfg = {"configurable": {"thread_id": tag}}
    steps: list = []
    stops: list = []
    for _ in range(14):
        for ev in g.stream(inp, cfg, stream_mode="debug"):
            if ev["type"] == "task":
                steps.append((ev["step"], ev["payload"]["name"]))
        snap = g.get_state(cfg)
        if not snap.next:
            break
        pend = list(snap.interrupts)
        stops.append([str((i.value or {}).get("checkpoint")) if isinstance(i.value, dict) else "?"
                      for i in pend])
        if until_first_stop:
            break
        inp = Command(resume={i.id: True for i in pend})
    else:  # pragma: no cover
        pytest.fail("14 轮仍未结束:疑似死循环")
    return steps, stops


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
    *,
    fusion: bool,
    video: Path | str,
    qc: bool = True,
    auto_import: bool = True,
    disable: Iterable[str] = (),
) -> Run:
    """整图跑到 END(InMemorySaver),返回超步轨迹与终态。"""
    work, out_root, state = _prepare(
        tmp_path, monkeypatch, tag, fusion=fusion, video=video, qc=qc,
        auto_import=auto_import, disable=disable,
    )
    g = build_graph(checkpointer=InMemorySaver(), thread_id=tag, start_heartbeat_thread=False)
    steps, stops = _advance(g, tag, state)
    final = g.get_state({"configurable": {"thread_id": tag}}).values
    return Run(tag, work, out_root, steps, stops, final)


_TS = re.compile(r"\d{8}_\d{6}_\d{6}")            # fork_draft 的英文分支目录时间戳
_MOCK_WAV = re.compile(r"tts_mock_[0-9a-f]{12}")      # Mock TTS 的随机文件名
# Windows ffmpeg 9 对越界 seek(测试 Mock ASR 的最后一条 cue 中点 4.05s 超出 4.0s 视频)
# 返回 rc≠0,stderr 里的日志前缀带每次进程都不同的堆指针地址(如 ``[mjpeg @ 0000027...]``),
# 会随 ``layout_issues[].evidence`` 进入 state。两次运行的指针必然不同,与时间戳同类,一并抹平。
# 且 frame-thread 编码器终止序列([vost#0:0/mjpeg] Could not open encoder before EOF /
# [vf#0:0] Terminating thread ...)的**行序与行集**随负载抖动 —— 指针抹平后仍会偶发不等。
# 故含指针占位的整条 stderr 串在 dumps 前归一成占位符(evidence 其余字段照常比较)。
_FF_PTR = re.compile(r"(?<=@ )[0-9a-f]{12,16}")


def _sanitize(value):
    """递归把内嵌 ffmpeg stderr(含 <PTR> 占位)整串替换为常量占位符。"""
    if isinstance(value, str):
        if "<PTR>" in _FF_PTR.sub("<PTR>", value):
            return "<FFMPEG_STDERR>"
        return value
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _scrub(text: str, run: Run) -> str:
    """把两次运行里必然不同的东西(目录名 / 会话名 / 时间戳 / Mock 随机名 / ffmpeg 日志指针)抹平。"""
    text = text.replace(str(run.work), "<WORK>").replace(run.tag, "<SESSION>")
    return _FF_PTR.sub(
        "<PTR>", _MOCK_WAV.sub("tts_mock_<ID>", _TS.sub("<TS>", text))
    )


def _norm(value, run: Run) -> str:
    return _scrub(
        json.dumps(_sanitize(value), ensure_ascii=False, sort_keys=True, default=str),
        run,
    )


def _human_view(run: Run) -> dict:
    """终态里"人工主图自己的字段":去掉日志和全部 storyline_* 键。"""
    skip = {"status_log", "error_log"}
    return {k: v for k, v in run.final.items() if k not in skip and not k.startswith("storyline_")}


@pytest.fixture
def call_counts(monkeypatch: pytest.MonkeyPatch) -> dict:
    """给 21 个 storyline 节点函数套"计数壳",返回 ``{节点名: 真实调用次数}``。

    为什么不数 stream 的 task 事件:关卡挂起后 resume 时,**同一超步里已跑完的侧支**会
    再发一次 task 事件(结果取自 checkpoint 里保存的 pending writes,函数并没有重跑)。
    只有计数壳才能区分"事件重复"和"真的重复执行"。
    """
    counts: dict[str, int] = {}

    def _wrap(owner, attr: str, node_name: str) -> None:
        orig = getattr(owner, attr)

        def counted(state):
            counts[node_name] = counts.get(node_name, 0) + 1
            return orig(state)

        monkeypatch.setattr(owner, attr, counted)

    for spec in FUSION_SPECS:
        name = spec.node_name
        if name == "storyline_qa_gate":
            _wrap(graph_mod._qa_gate_mod, "storyline_qa_gate_node", name)
        elif name == "storyline_join":
            _wrap(graph_mod._join_storyline_mod, "storyline_join_node", name)
        else:
            _wrap(graph_mod, f"{name}_node", name)
    return counts


def _human_storyline_view(run: Run, keys: Iterable[str]) -> dict:
    return {k: run.final.get(k) for k in keys}


# 关闭开关时终态里就已存在的 storyline_* 字段(来自 node_04b,human 主图自己的)
_HUMAN_OWN_STORYLINE_KEYS = ("storyline_session_id", "storyline_plan", "storyline_artifacts",
                             "storyline_error_code")


# ---------------------------------------------------------------------------
@pytest.fixture
def pair(tmp_path, monkeypatch, real_mp4, call_counts):
    """同一份输入,分别在开关关/开下各跑一遍(质检开启)。

    ``call_counts`` 只记录开关开启那一遍(关闭时 storyline 节点根本不会被调用,先断言这一点)。
    """
    off = _run(tmp_path, monkeypatch, "fusion-off", fusion=False, video=real_mp4)
    assert call_counts == {}, "开关关闭时不应调用任何 storyline 节点"
    on = _run(tmp_path, monkeypatch, "fusion-on", fusion=True, video=real_mp4)
    return off, on


# 本场景(无 storyline_targets.query)下,这两个节点因缺前置被包装层直接跳过,函数一次都不会被调用
_NO_QUERY_SKIPPED = {"storyline_search_web_topic", "storyline_search_media"}


def test_each_side_node_runs_once_right_after_its_anchor(pair, call_counts):
    _, on = pair
    for spec in FUSION_SPECS:
        name, anchor = spec.node_name, anchor_of(spec, True)
        expected_calls = 0 if name in _NO_QUERY_SKIPPED else 1
        assert call_counts.get(name, 0) == expected_calls, name           # 真实调用次数
        assert on.supersteps_of(name) == {on.step_of(anchor) + 1}, (name, anchor)   # 紧跟锚点的下一超步


def test_human_flow_is_identical_with_and_without_fusion(pair):
    off, on = pair
    # 1. 没有侧支时,图里没有任何 storyline 节点在跑
    assert not [n for _, n in off.steps if _is_side(n)]
    # 2. human 节点:执行顺序 + 超步号逐项一致
    assert [(s, n) for s, n in on.steps if not _is_side(n)] == off.steps
    # 3. 挂起的人工关卡一致,且首尾仍是 ① 与 ②
    assert on.stops == off.stops
    assert on.stops[0] == ["①"] and on.stops[-1] == ["②"]
    # 4. human 字段终值一致(不含日志与 storyline_*)
    assert _norm(_human_view(on), on) == _norm(_human_view(off), off)
    # 5. human 自己的 storyline_* 字段(plan / session_id / artifacts / error_code)不被侧支改写
    assert _norm(_human_storyline_view(on, _HUMAN_OWN_STORYLINE_KEYS), on) == \
        _norm(_human_storyline_view(off, _HUMAN_OWN_STORYLINE_KEYS), off)
    # 6. 日志:关闭开关时的每一条都按原顺序保留,多出来的全部来自侧支
    for key in ("status_log", "error_log"):
        a = [_scrub(x, off) for x in off.final[key]]
        b = [_scrub(x, on) for x in on.final[key]]
        assert [x for x in b if x in set(a)] == a, key
        extra = [x for x in b if x not in set(a)]
        assert all(x.startswith("storyline_") or "[storyline:" in x or "[fusion:" in x for x in extra), extra


def test_artifacts_are_isolated_and_published_under_new_keys(pair):
    off, on = pair
    final = on.final
    # 侧支产物只写进 <outputs>/<job>/fusion/
    produced = [
        "storyline_media_artifact", "storyline_shots_artifact", "storyline_asr_artifact",
        "storyline_rough_cut_artifact", "storyline_understanding_artifact",
        "storyline_filtered_clips", "storyline_groups_artifact", "storyline_script_artifact",
        "storyline_voiceover_artifact", "storyline_timeline_plan",
    ]
    for key in produced:
        path = Path(final[key])
        assert path.is_file(), key
        assert on.fusion_root in path.parents, (key, path)
    # 字典类产物直接进 state
    for key in ("storyline_bgm_selection", "storyline_transition_plan", "storyline_text_style_plan"):
        assert isinstance(final[key], dict) and final[key], key
    # JOIN 的计划发布到新键;human 的 storyline_plan 没被改写
    assert isinstance(final["storyline_auto_plan"], dict) and final["storyline_auto_plan"]["job_id"] == on.tag
    assert final.get("storyline_plan") == off.final.get("storyline_plan")
    # 重命名键:没被占用的原键不会凭空出现
    assert not final.get("storyline_search_media_artifact")      # 没配 query/key,SM 跳过
    assert not final.get("storyline_timeline_plan_ai")           # AI 转场默认关,PTAT 跳过
    # 关闭开关时,新增的 storyline 产物键一个都没有
    assert not [k for k in off.final if k in produced + ["storyline_auto_plan"] and off.final.get(k)]
    # 网页会话目录 / 项目 outputs 根下没有被侧支写入 storyline 目录
    assert not (on.out_root / on.tag / "storyline").exists()


def test_skipped_nodes_leave_a_reason_in_status_log(pair):
    _, on = pair
    log = on.final["status_log"]
    assert "storyline_search_web_topic_fusion_skipped:storyline_targets.query" in log
    assert "storyline_search_media_fusion_skipped:storyline_targets.query+PEXELS_API_KEY" in log \
        or "storyline_search_media_fusion_skipped:storyline_targets.query" in log
    # 跑成功的节点留下各自的 done 标签
    for tag in ("storyline_load_media_done", "storyline_split_shots_done", "storyline_understand_clips_done",
                "storyline_filter_clips_done", "storyline_group_clips_done", "storyline_generate_script_done",
                "storyline_recommend_text_done", "storyline_recommend_transition_done", "storyline_join_done"):
        assert tag in log, tag
    # QA 的"重试"标签被改名:融合版没有回到 group_clips 的重试环
    assert "storyline_qa_retry" not in log


def test_qc_off_layout_runs_each_node_once_and_keeps_human_flow(tmp_path, monkeypatch, real_mp4, call_counts):
    off = _run(tmp_path, monkeypatch, "fusion-qcoff-off", fusion=False, video=real_mp4, qc=False)
    assert call_counts == {}
    on = _run(tmp_path, monkeypatch, "fusion-qcoff-on", fusion=True, video=real_mp4, qc=False)
    assert not [n for _, n in on.steps if n.startswith("assembly_")]       # 6 个质检节点确实没连
    assert [(s, n) for s, n in on.steps if not _is_side(n)] == off.steps
    assert on.stops == off.stops
    assert _norm(_human_view(on), on) == _norm(_human_view(off), off)
    for spec in FUSION_SPECS:
        name, anchor = spec.node_name, anchor_of(spec, False)
        expected_calls = 0 if name in _NO_QUERY_SKIPPED else 1
        # 含与关卡①同一超步的 UC / SRC:挂起 → resume 不会让它们重跑
        assert call_counts.get(name, 0) == expected_calls, name
        assert on.supersteps_of(name) == {on.step_of(anchor) + 1}, (name, anchor)
    # UC / SRC 确实与关卡①(node_06)同一个超步
    assert on.supersteps_of("storyline_understand_clips") == on.supersteps_of("node_06_human_reorder")
    assert on.supersteps_of("storyline_speech_rough_cut") == on.supersteps_of("node_06_human_reorder")
    # 产物没有丢:挂起重放不会吞掉同超步侧支的写入
    assert Path(on.final["storyline_understanding_artifact"]).is_file()
    assert Path(on.final["storyline_rough_cut_artifact"]).is_file()


def test_no_import_means_no_load_media_and_whole_chain_skips(tmp_path, monkeypatch):
    """没人点网页【导入视频】:融合侧支不得自己导入(守护 test_no_auto_import_video 的硬约束)。"""
    import nodes.storyline.node_load_media as lm

    calls = {"n": 0}

    def _spy(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("融合侧支不应在未导入时调用 load_media")

    monkeypatch.setattr(lm, "cap_load_media", _spy)
    on = _run(tmp_path, monkeypatch, "fusion-noimport", fusion=True, video="C:/tmp/fake.mp4", auto_import=False)
    assert calls["n"] == 0
    assert not (on.out_root / on.tag / "import_video_result.json").exists()
    assert on.final.get("import_video_status") == "timeout"
    log = on.final["status_log"]
    assert "storyline_load_media_fusion_skipped:import_video_status=imported" in log
    for name in SIDE_NAMES:
        assert any(t.startswith(f"{name}_fusion_skipped:") for t in log), name
    assert not on.fusion_root.exists() or not list(on.fusion_root.rglob("*.json"))
    assert on.stops[0] == ["①"] and on.stops[-1] == ["②"]


def test_side_node_exception_is_isolated_in_real_graph(tmp_path, monkeypatch, real_mp4):
    def _boom(_state):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(graph_mod, "storyline_group_clips_node", _boom)
    off = _run(tmp_path, monkeypatch, "fusion-boom-off", fusion=False, video=real_mp4)
    on = _run(tmp_path, monkeypatch, "fusion-boom-on", fusion=True, video=real_mp4)
    assert on.stops == off.stops
    assert [(s, n) for s, n in on.steps if not _is_side(n)] == off.steps
    assert _norm(_human_view(on), on) == _norm(_human_view(off), off)
    assert "storyline_group_clips_fusion_failed" in on.final["status_log"]
    assert "[fusion:storyline_group_clips] RuntimeError: kaboom" in on.final["error_log"]
    # 下游依赖 groups 的侧支被正常跳过,而不是跟着报错
    assert "storyline_generate_script_fusion_skipped:storyline_groups_artifact" in on.final["status_log"]
    assert not on.final.get("storyline_groups_artifact")


def test_kill_switch_disables_a_node_without_touching_human_flow(tmp_path, monkeypatch, real_mp4):
    off = _run(tmp_path, monkeypatch, "fusion-ks-off", fusion=False, video=real_mp4)
    on = _run(tmp_path, monkeypatch, "fusion-ks-on", fusion=True, video=real_mp4,
              disable={"storyline_understand_clips"})
    assert [(s, n) for s, n in on.steps if not _is_side(n)] == off.steps
    assert on.stops == off.stops
    assert _norm(_human_view(on), on) == _norm(_human_view(off), off)
    log = on.final["status_log"]
    assert "storyline_understand_clips_fusion_disabled" in log
    assert "storyline_filter_clips_fusion_skipped:storyline_understanding_artifact" in log
    assert not on.final.get("storyline_understanding_artifact")


# ===========================================================================
# C 组:升级 / 回滚时的"在途存档"(真实 SqliteSaver 落盘,模拟进程重启)
# ===========================================================================
def _sqlite_saver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str):
    # 仓库已按 Postgres 统一改造(方案 D2)删除 config.make_checkpointer("sqlite")
    # 与 CHECKPOINTER_DB_DIR;本组测试要的语义是"新连接 = 新进程接管同一存档",
    # 与运行期后端无关,故直接用 langgraph-checkpoint-sqlite 落盘。
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "ckpt" / f"{tag}.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(db), check_same_thread=False))
    saver.setup()   # 幂等;首连建表,后续"重启"连接复用同一份存档
    return saver


def _pause_at_checkpoint1(tmp_path, monkeypatch, tag, *, fusion, qc, video):
    """用 ``fusion`` 开关的图跑到关卡①挂起,存档落到 sqlite;返回挂起时的快照。"""
    _, _, state = _prepare(tmp_path, monkeypatch, tag, fusion=fusion, video=video, qc=qc)
    g = build_graph(checkpointer=_sqlite_saver(tmp_path, monkeypatch, tag), thread_id=tag,
                    start_heartbeat_thread=False)
    _, stops = _advance(g, tag, state, until_first_stop=True)
    snap = g.get_state({"configurable": {"thread_id": tag}})
    assert stops == [["①"]] and snap.next == ("node_06_human_reorder",)
    return snap


def _resume_after_restart(tmp_path, monkeypatch, tag, *, fusion):
    """"重启":换开关、换 saver 连接(同一个 sqlite 文件),把挂起的存档接着跑完。"""
    monkeypatch.setattr(config, "STORYLINE_HUMAN_FUSION", fusion)
    g = build_graph(checkpointer=_sqlite_saver(tmp_path, monkeypatch, tag), thread_id=tag,
                    start_heartbeat_thread=False)
    pend = list(g.get_state({"configurable": {"thread_id": tag}}).interrupts)
    steps, stops = _advance(g, tag, Command(resume={i.id: True for i in pend}))
    final = g.get_state({"configurable": {"thread_id": tag}}).values
    return steps, stops, final


@pytest.mark.parametrize("qc", [True, False])
def test_upgrade_in_flight_thread_resumes_and_new_side_nodes_skip_gracefully(
    tmp_path, monkeypatch, real_mp4, qc
):
    """升级场景:老图(开关关)的任务停在关卡①,重启后开关开启 —— 能接着跑完。

    老图没跑过任何侧支,存档里没有它们的前置产物 ⇒ 之后才触发的侧支必须"缺前置就跳过",
    不报错;锚点在关卡①之前、已经过去的侧支不会被补跑。
    """
    tag = f"fusion-upgrade-{'qc' if qc else 'noqc'}"
    _pause_at_checkpoint1(tmp_path, monkeypatch, tag, fusion=False, qc=qc, video=real_mp4)
    steps, stops, final = _resume_after_restart(tmp_path, monkeypatch, tag, fusion=True)

    assert stops and stops[-1] == ["②"]
    log = final["status_log"]
    assert "join_before_delivery_done" in log                         # human 主链照常跑完
    ran_human = {n for _, n in steps if not _is_side(n)}
    for spec in FUSION_SPECS:
        name, anchor = spec.node_name, anchor_of(spec, qc)
        tags = [t for t in log if t.startswith(f"{name}_fusion_")]
        if anchor in ran_human:                                       # 锚点在 resume 之后才执行
            assert len(tags) == 1 and tags[0].startswith(f"{name}_fusion_skipped:"), (name, tags)
        else:                                                         # 锚点在存档之前就跑过了:不补跑
            assert not tags and name not in {n for _, n in steps}, (name, tags)
    assert not [t for t in log if t.endswith("_fusion_failed")]
    assert not [e for e in final["error_log"] if "[fusion:" in e]


@pytest.mark.parametrize("qc", [True, False])
def test_rollback_in_flight_thread_resumes_without_running_side_nodes(
    tmp_path, monkeypatch, real_mp4, call_counts, qc
):
    """回滚场景:开关开启的任务停在关卡①,把开关关掉重启 —— 能接着跑完,侧支函数不再被调用。

    质检关闭时,关卡①同一超步里 UC / SRC 已经跑完、写入暂存在存档里;关掉开关后新图仍认识这两个
    (已注册)节点,resume 时只"重放事件、取回暂存写入",函数不会再执行 —— 所以断言的是
    **真实调用次数不变**,而不是"事件里没有侧支"。
    """
    tag = f"fusion-rollback-{'qc' if qc else 'noqc'}"
    _pause_at_checkpoint1(tmp_path, monkeypatch, tag, fusion=True, qc=qc, video=real_mp4)
    calls_before = dict(call_counts)
    assert calls_before.get("storyline_load_media") == 1                   # 暂停前侧支确实跑过

    steps, stops, final = _resume_after_restart(tmp_path, monkeypatch, tag, fusion=False)

    assert stops and stops[-1] == ["②"]
    assert "join_before_delivery_done" in final["status_log"]
    assert call_counts == calls_before                                     # 回滚后没有任何侧支函数被调用
    replayed = {n for _, n in steps if _is_side(n)}
    assert replayed <= ({"storyline_understand_clips", "storyline_speech_rough_cut"} if not qc else set())
    # 回滚前已经产出的侧支产物还在 state 里(只是不再有人用)
    assert final.get("storyline_media_artifact")

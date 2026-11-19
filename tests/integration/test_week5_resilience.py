"""Week 5 集成测试 — 跨实例恢复 + 副作用幂等(2026-11-15 关卡③ 路径已随节点删除)。

对齐第 5 周计划 §5.2。

覆盖:
- 跨实例恢复关卡①/②(同一个 checkpointer + 全新 graph 实例)
- 关卡③ interrupt 路径:超长英文 → interrupt → resume → 重读草稿
- 副作用跨 resume 只执行 1 次(Week 5 附件"测试代码"段 2)
- 多日挂起:同一个存档跨多次 build_graph

2026-10(阶段 C):SQLite 后端已删除。"新数据库连接"级别的恢复覆盖
(等价于旧的跨进程 SQLite 语义)移到
``tests/integration/test_postgres_resume.py``,用真实 Postgres 跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph import build_graph
from jy_common.translate_client import LLMTranslateClient, MockTranslateClient, set_default_client
from state import WorkflowState
from draft_ops.draft_integrity import REQUIRED_FEATURE_KEYS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_draft(draft_dir: Path, duration_us: int = 35_000_000) -> Path:
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_file = draft_dir / "draft_content.json"
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": duration_us,
        "materials": {"videos": [{"id": "v1"}]},
        "tracks": [{
            "type": "video",
            "fps": 30,
            "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": duration_us}}],
        }],
    }
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_file


def _add_manual_bgm(draft_file: Path) -> None:
    """模拟操作员在关卡② 于剪映里人工拖入 BGM(20261114 阶段6 起过门必需)。

    关卡② 改造(修订 §2.11)后:恢复时磁盘草稿仍无 BGM 会**再次挂起** ——
    集成流程在最终 resume 前必须先把 BGM 写进磁盘草稿。
    """
    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    tracks = draft.setdefault("tracks", [])
    if not any(t.get("type") == "audio" and t.get("name") == "manual_bgm" for t in tracks):
        tracks.append({
            "type": "audio",
            "id": "track-audio-manual-bgm",
            "name": "manual_bgm",
            "segments": [{
                "id": "seg-manual-bgm",
                "material_id": "audio-manual-bgm",
                "target_timerange": {"start": 0, "duration": draft.get("duration") or 35_000_000},
            }],
        })
        mats = draft.setdefault("materials", {})
        mats.setdefault("audios", []).append({"id": "audio-manual-bgm", "name": "manual_bgm"})
    draft_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")


REQUIRED_FEATURES_NONE = {key: False for key in REQUIRED_FEATURE_KEYS}


@pytest.fixture(autouse=True)
def _qc_channel_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨实例恢复契约测试族:关闭 assembly QC 通道(2026-11-20 关卡①条件挂起
    改造后的用例分类)—— seed state 直接构造关卡①降级状态,① 仍挂起,
    恢复链路覆盖不变。"""
    import config as _config

    monkeypatch.setattr(_config, "ASSEMBLY_QC_GATE_ENABLED", False)


def _seed_state(draft_path: str, *, asr_segments_zh: list[dict] | None = None) -> WorkflowState:
    return {
        "session_id": "w5-int",
        "video_input_path": "C:/tmp/fake.mp4",
        "draft_path": draft_path,
        "draft_encryption_status": "plaintext",
        "draft_version_strategy": "strategy_a_version_lock",
        "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
        "asr_segments_zh": asr_segments_zh or [
            {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "你好"},
            {"index": 1, "start_ms": 2_000, "end_ms": 4_000, "text_zh": "世界"},
        ],
        "asr_segments_zh_written": True,
        # 2026-11-20 条件挂起:QC 已关(见 _qc_channel_off),直接构造关卡①
        # 降级状态 → ① 按 escalated 挂起
        "assembly_qc_gate_enabled": False,
        "assembly_qc_status": "escalated",
        "error_log": [],
        "status_log": ["node_05_generate_draft_done"],
        "required_features": REQUIRED_FEATURES_NONE,
    }


def _is_interrupted(g, config: dict) -> bool:
    snap = g.get_state(config)
    return bool(snap.interrupts) or snap.next != ()


# ---------------------------------------------------------------------------
# 测试 1:跨实例恢复关卡①/②(同一个 checkpointer + 全新 graph 实例;
# 2026-11-15 关卡③删除后剩两道人工关卡)
# ---------------------------------------------------------------------------
def test_resume_all_checkpoints_via_shared_checkpointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_mode_translator,
) -> None:
    """模拟"全新 graph 实例 + 同一个存档 = 跨实例恢复"。

    验证各关卡的 interrupt payload ``checkpoint`` 字段都是 "①"/"②" 统一格式。

    2026-10(阶段 C):SQLite 后端已删除,这里用共享的 ``InMemorySaver``;
    "新数据库连接"的恢复语义见 ``tests/integration/test_postgres_resume.py``。
    """
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = _seed_draft(draft_dir)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))

    state = _seed_state(str(draft_file))
    thread_id = "w5-cross-proc"
    config = {"configurable": {"thread_id": thread_id}}

    # ---- 用共享的 InMemorySaver 构造 g1,跑到关卡① ----
    saver = InMemorySaver()
    g1 = build_graph(
        checkpointer=saver,
        thread_id=thread_id,
        start_heartbeat_thread=False,
    )
    g1.invoke(state, config=config)
    assert _is_interrupted(g1, config), "关卡① 应挂起"

    # 验证关卡① payload 字段
    snap1 = g1.get_state(config)
    assert snap1.interrupts
    p1 = snap1.interrupts[0].value
    assert p1["checkpoint"] == "①"

    # ---- 模拟"进程重启":丢弃 g1,新建 g2(共享同一个存档) ----
    g2 = build_graph(
        checkpointer=saver,
        thread_id=thread_id,
        start_heartbeat_thread=False,
    )
    g2.invoke(Command(resume=True), config=config)
    assert _is_interrupted(g2, config), "关卡② 应挂起"
    snap2 = g2.get_state(config)
    p2 = snap2.interrupts[0].value
    assert p2["checkpoint"] == "②"

    # ---- 正常路径下:resume 关卡② → 流程一路到 END ----
    # 20261114 阶段6:先补 BGM(关卡② 恢复时重读磁盘,仍无 BGM 会再次挂起)
    _add_manual_bgm(draft_file)
    final = g2.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g2, config), "流程应已结束"
    log = final["status_log"]
    # 各节点只跑 1 次(无重跑)
    assert log.count("node_05_generate_draft_done") == 1
    assert log.count("checkpoint1_resumed") == 1
    assert log.count("checkpoint2_resumed") == 1
    # 节点 17 写空 wav 占位(Week 5)
    assert final.get("en_audio_path") is not None
    wav_path = Path(final["en_audio_path"])
    assert wav_path.exists()
    assert wav_path.stat().st_size > 5_000  # >5KB


# ---------------------------------------------------------------------------
# 测试 2:副作用跨 resume 只执行 1 次
# ---------------------------------------------------------------------------
def test_side_effects_only_once_across_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多次 resume,翻译客户端 / 通知 / 写 wav 都只执行 1 次。

    Week 5 附件"测试代码"段 2 的核心断言:副作用"挪到 interrupt 之后"
    模式 + 文件 marker 检查(marker / 真实文件存在),resume 重放时跳过。
    """
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = _seed_draft(draft_dir)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))

    state = _seed_state(str(draft_file))
    config = {"configurable": {"thread_id": "w5-side-effect"}}
    g = build_graph(
        checkpointer=InMemorySaver(),
        thread_id="w5-side-effect",
        start_heartbeat_thread=False,
    )

    # 2026-11-17 P1:翻译次数以真实适配器底层的 LLM chat 调用计数 ——
    # stub/Mock 译文被 fail-closed 门禁拦截,不再进入正式字幕/TTS。
    translate_calls = {"n": 0}

    class _CountingLLM:
        def chat(self, *, system_prompt, user_prompt, media=None,
                 json_schema=None, temperature=0.3, top_p=0.9, max_tokens=4096):
            translate_calls["n"] += 1
            payload = json.loads(user_prompt)
            return json.dumps(
                {"translations": [
                    {"index": s["index"], "text_en": f"EN-{s['index']}"}
                    for s in payload["segments"]
                ]},
                ensure_ascii=False,
            )

    set_default_client(
        LLMTranslateClient(_CountingLLM(), translator_id="integration-real")
    )

    # 完整跑通
    g.invoke(state, config=config)
    g.invoke(Command(resume=True), config=config)
    # 20261114 阶段6:先补 BGM(关卡② 恢复时重读磁盘,仍无 BGM 会再次挂起)
    _add_manual_bgm(draft_file)
    final = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config)
    # 2026-11-17 P2 起默认翻译客户端有两个消费方:字幕翻译(16a)1 次 +
    # 封面标题翻译(15)1 次;marker 幂等保证 16a 不因重放重复调用。
    assert translate_calls["n"] == 2, f"翻译调用次数异常:{translate_calls['n']}"

    # wav 文件存在
    en_audio = final.get("en_audio_path")
    assert en_audio and Path(en_audio).exists()
    wav_size_initial = Path(en_audio).stat().st_size

    # 模拟"再次 resume"(LangGraph 重放)— 翻译不调、wav 不重写
    final2 = g.invoke(Command(resume=True), config=config)
    assert translate_calls["n"] == 2, f"翻译被多次调用:{translate_calls['n']}"
    wav_size_after = Path(en_audio).stat().st_size
    assert wav_size_after == wav_size_initial, "wav 不应被重写"


# ---------------------------------------------------------------------------
# (2026-11-15 第六期)原测试 3(关卡③ interrupt/resume 路径)已随节点整体删除:
# "图中不再存在③"由 test_workflow_graph_schema.py 的 REMOVED_NODES 负向断言与
# test_checkpoint3_node_removed_and_10b_17_direct 正向断言明确验证。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 测试 4:node_17 写空 wav 占位 — 验收脚本能跑"英文配音音轨非空"断言
# ---------------------------------------------------------------------------
def test_node_17_writes_empty_wav_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_mode_translator,
) -> None:
    """节点 17 在 draft_dir_en_branch 下生成 en_dub.wav,>5KB,允许验收通过。

    2026-11-17 P1:真实翻译模式( stub 译文被门禁拦,不会送 TTS)。
    """
    draft_dir = tmp_path / "drafts" / "demo"
    draft_file = _seed_draft(draft_dir)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DRAFT_DIR", str(draft_dir))

    state = _seed_state(str(draft_file))
    config = {"configurable": {"thread_id": "w5-wav"}}
    g = build_graph(
        checkpointer=InMemorySaver(),
        thread_id="w5-wav",
        start_heartbeat_thread=False,
    )

    g.invoke(state, config=config)
    g.invoke(Command(resume=True), config=config)
    # 20261114 阶段6:先补 BGM(关卡② 恢复时重读磁盘,仍无 BGM 会再次挂起)
    _add_manual_bgm(draft_file)
    final = g.invoke(Command(resume=True), config=config)
    assert not _is_interrupted(g, config)

    en_audio = final.get("en_audio_path")
    assert en_audio
    wav = Path(en_audio)
    assert wav.exists()
    # Week 5 阶段五 acceptance_check.py 阈值
    assert wav.stat().st_size > 5_000
    # WAV 头校验:前 4 字节是 "RIFF"
    with open(wav, "rb") as f:
        magic = f.read(4)
    assert magic == b"RIFF", f"生成的 wav 文件头异常:{magic!r}"

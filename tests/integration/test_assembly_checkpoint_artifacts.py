"""P2-3 集成:DraftPatch 版本、幂等与回滚。

验收对照(计划文档 P2-3):
- 同一个 patch 重放两次,最终 plan 业务内容一致(apply 纯函数确定性);
- 不同 base hash 的 patch 应用失败(旧 patch 不套新计划);
- 同一 thread 重跑(等价 checkpoint resume 场景):assembly 链重新执行,
  不重复增加 clip/音轨/素材引用,最终草稿逐段一致;
- 写入失败后原始草稿/输入 plan 保持可恢复(safe_write_draft 快照回滚 +
  节点侧不污染上游 artifact)。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import config
from assembly_capabilities.draft_patch import PatchError, apply_patch, new_patch, add_operation, plan_sha256
from graph import _build_state_graph
from tests.integration.test_assembly_draft_influence import (  # noqa: F401 — pytest fixture
    live_draft_state,
    _inject_plan,
    _sample_plan_on_video,
)
from tests.integration.test_assembly_qc_graph import (  # noqa: F401 — pytest fixtures
    _patch_heartbeat,
    base_state,
    fake_video,
    patched_assembly_tools,
)
from tests.support.draft_influence import (
    draft_audio_materials,
    draft_video_summary,
    load_canonical_sample,
)


# ---------------------------------------------------------------------------
# apply_patch 纯函数幂等
# ---------------------------------------------------------------------------
def _sample_with_trim_patch():
    plan = load_canonical_sample()
    sha = plan_sha256(plan)
    patch = new_patch(sha, created_by="p2_3_test")
    add_operation(
        patch,
        operation="trim",
        target_clip_id="clip_0001",
        source_in_ms=500,
        source_out_ms=3500,
        reason="p2-3 replay test",
        evidence_refs=["black:1-2"],
        confidence=0.9,
    )
    return plan, sha, patch


def test_same_patch_replay_yields_identical_plan() -> None:
    """同一 patch 对同一基线重放两次 → 候选计划 sha 逐字一致(幂等)。"""
    plan, sha, patch = _sample_with_trim_patch()
    cand_a, report_a = apply_patch(plan, patch, expect_base_sha256=sha)
    cand_b, report_b = apply_patch(plan, patch, expect_base_sha256=sha)
    assert plan_sha256(cand_a) == plan_sha256(cand_b)
    assert report_a["candidate_plan_sha256"] == report_b["candidate_plan_sha256"]
    # 基线永不被覆盖(两次应用读同一基线,结果一致)
    assert plan_sha256(plan) == sha


def test_patch_with_wrong_base_hash_rejected() -> None:
    """patch 绑定的 base sha 与实际 plan 不匹配 → PatchError(旧 patch 不套新计划)。"""
    plan, _sha, patch = _sample_with_trim_patch()
    drifted = copy.deepcopy(plan)
    drifted["clips"][0]["source_out_ms"] = 3900  # 基线被(假想)修改
    with pytest.raises(PatchError):
        apply_patch(drifted, patch, expect_base_sha256=plan_sha256(drifted))


# ---------------------------------------------------------------------------
# 同一 thread 重跑(等价 resume 重放):不重复编辑、草稿逐段一致
# ---------------------------------------------------------------------------
def _run_to_before_node_06(state: dict, thread_id: str) -> dict:
    g = _build_state_graph().compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["node_06_human_reorder"],
    )
    return g.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 60},
    )


def test_rerun_same_thread_does_not_duplicate_edits(
    monkeypatch: pytest.MonkeyPatch,
    patched_assembly_tools,
    live_draft_state,
    fake_video: Path,
) -> None:
    """同一 thread 跑两遍:assembly 链重算但幂等 —— 草稿视频段/音频素材数一致。"""
    from tests.integration.test_assembly_draft_influence import _stub_manifest, _stub_observation

    monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", True)
    monkeypatch.setattr(config, "ASSEMBLY_QC_MAX_RETRY", 2)
    _inject_plan(monkeypatch, _sample_plan_on_video(fake_video))
    _stub_manifest(
        monkeypatch,
        fake_video,
        candidate_segments=[{"index": 0, "start": 0.0, "end": 10.0}],
        black_ranges=[{"start": 1.0, "end": 2.5}],
    )
    _stub_observation(monkeypatch)
    state, draft_dir = live_draft_state

    out1 = _run_to_before_node_06(state, "assembly-p23-rerun")
    draft_file = draft_dir / "draft_content.json"
    assert draft_file.is_file()
    draft1 = json.loads(draft_file.read_text(encoding="utf-8"))
    summary1 = draft_video_summary(draft1)
    audio1 = draft_audio_materials(draft1)
    cand1 = out1.get("assembly_candidate_plan_sha256")

    # 同一 thread + 同一输入再跑一遍(checkpoint resume/重放的等价形态)
    out2 = _run_to_before_node_06(state, "assembly-p23-rerun")
    draft2 = json.loads(draft_file.read_text(encoding="utf-8"))
    summary2 = draft_video_summary(draft2)
    audio2 = draft_audio_materials(draft2)

    assert summary1 == summary2, "重跑后视频轨必须逐段一致(不重复添加 clip)"
    assert audio1 == audio2, "重跑后音频素材必须一致(不重复添加音轨)"
    assert len(summary1) == 3
    assert out2.get("assembly_candidate_plan_sha256") == cand1, (
        "重跑产出的候选计划 sha 必须一致(同输入同规则确定性)"
    )
    # trim 仍然只有一次的效果(黑帧 trim 2500-4000,不是叠加两次)
    assert summary2[0]["source_start_us"] == 2_500_000


# ---------------------------------------------------------------------------
# 写入失败:原始草稿与上游产物保持可恢复
# ---------------------------------------------------------------------------
def test_failed_draft_write_preserves_prior_draft_and_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入中途失败 → safe_write_draft 抛错 → 旧草稿原样保留,storyline_plan
    与 final plan artifact 不被污染(fail-closed 可恢复)。"""
    import hashlib

    from draft_ops.atomic_writer import safe_write_draft
    from nodes.node_05_generate_draft import generate_initial_jianying_draft
    from draft_ops.encryption_detector import DraftStatus

    import tempfile

    work = Path(tempfile.mkdtemp(prefix="p23_write_fail_"))
    draft_dir = work / "draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    old_content = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [{"id": "old"}], "audios": [], "texts": []},
        "tracks": [],
    }
    (draft_dir / "draft_content.json").write_text(
        json.dumps(old_content, ensure_ascii=False), encoding="utf-8"
    )
    (draft_dir / "draft_info.json").write_text(
        json.dumps(old_content, ensure_ascii=False), encoding="utf-8"
    )
    old_digest = hashlib.sha256((draft_dir / "draft_content.json").read_bytes()).hexdigest()

    # final plan artifact(上游产物,写失败不得被污染)
    final_plan = load_canonical_sample()
    final_path = work / "final_plan.json"
    final_path.write_text(json.dumps(final_plan, ensure_ascii=False), encoding="utf-8")

    def _fake_encrypt(_d):
        return DraftStatus.PLAINTEXT

    def _exploding_writer(draft_dir, content):  # noqa: ANN001 — 模拟写入中途失败
        raise RuntimeError("模拟写入失败(磁盘满/杀进程)")

    state = {
        "storyline_plan": load_canonical_sample(),
        "assembly_final_plan_path": str(final_path),
        "assembly_final_plan_sha256": plan_sha256(final_plan),
        "assembly_base_plan_sha256": plan_sha256(load_canonical_sample()),
        "video_input_path": str(work / "input.mp4"),
        "error_log": [],
        "status_log": [],
    }
    with pytest.raises(RuntimeError, match="模拟写入失败"):
        generate_initial_jianying_draft(
            state, draft_dir, encrypt_detector=_fake_encrypt, writer=_exploding_writer
        )

    # 旧草稿原样保留(字节级一致)
    assert (
        hashlib.sha256((draft_dir / "draft_content.json").read_bytes()).hexdigest()
        == old_digest
    )
    # 上游 final plan 与 storyline_plan 不被污染
    assert json.loads(final_path.read_text(encoding="utf-8")) == final_plan
    assert state["storyline_plan"]["clips"][0]["source_in_ms"] == 0

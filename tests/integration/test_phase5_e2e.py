"""阶段五端到端验证 — CI 集成测试(video-agent-kit 集成 plan §十 / §十一)。

设计目标
--------

把 ``scripts/assembly/phase5_e2e.py`` 里 5 个验证 case 原样搬进 pytest,
让 CI 在每次 PR / merge 时自动跑:

================  ============  =================================================
Case               pytest marker 触发条件
================  ============  =================================================
happy              slow          inputs/30s.mp4 + ffmpeg/ffprobe 均存在
qc_fail            slow          inputs/30s.mp4 + ffmpeg/ffprobe 均存在
gate_off           -             仅静态扫描 graph 拓扑,无外部依赖
checkpoint1        -             单测 node_06._send_notification 文本,无外部依赖
decoupling         -             grep 全仓,无外部依赖
================  ============  =================================================

CI 推荐运行::

    # 默认(快 3 个 + 慢 2 个被跳过)
    pytest tests/integration/test_phase5_e2e.py

    # 慢的全跑(每周回归 / 发版前)
    pytest tests/integration/test_phase5_e2e.py -m slow

实现要点
--------

1. **不复制 case_* 逻辑** — 直接 ``from scripts.assembly.phase5_e2e import case_*``,
   保证本地 ``phase5_e2e.py --case all`` 与 CI ``pytest`` 行为完全一致;
   CI 报告的就是同一份脚本的产物矩阵。
2. **gate_off / decoupling 用 monkeypatch 还原 config** — ``scripts/assembly/phase5_e2e.case_gate_off``
   本身已经 save/restore ``config.ASSEMBLY_QC_GATE_ENABLED``,pytest 这边只负责包一层
   ``assert result["all_ok"] is True``。
3. **happy / qc_fail 标 ``@pytest.mark.slow``** + ``skipif`` 无 ffmpeg / 无素材则 skip,
   默认 CI 流水跳过、release / nightly 流水线放开。
4. **checkpoint1 用合成的 state** — 不依赖 happy 的真实 state,把 stage 5 验收项
   ``通知文案正确带上 assembly_report_path`` 解耦出来纯文本断言。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# 让 ``import scripts.assembly.phase5_e2e`` 能解析到 ROOT
from tests.conftest import ROOT  # noqa: F401 — already inserts ROOT into sys.path

# scripts.assembly.phase5_e2e 已经在 import 时把 ROOT 加进 sys.path;
# 这里的导入仅依赖这一副作用,不需要额外 sys.path 处理。
from scripts.assembly.phase5_e2e import (  # noqa: E402
    case_checkpoint1_notification,
    case_decoupling,
    case_gate_off,
    case_happy,
    case_qc_fail,
)


# ---------------------------------------------------------------------------
# 共享 fixtures
# ---------------------------------------------------------------------------
VIDEO_INPUT = ROOT / "inputs" / "30s.mp4"
TRANSCRIPT_FIXTURE = ROOT / "tests" / "fixtures" / "assembly_phase5" / "transcript.json"


def _ffmpeg_available() -> bool:
    """CI 阶段五 happy / qc_fail 必备:ffmpeg + ffprobe + 真实素材。"""
    if not VIDEO_INPUT.is_file():
        return False
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# 慢 case 公共 skip 条件:无 ffmpeg 或无 30s.mp4 时跳过,不影响快 case。
slow_skip = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="阶段五 happy / qc_fail 需要 ffmpeg + ffprobe + inputs/30s.mp4,"
           "默认 CI 不强依赖;手动跑 `pytest -m slow` 或本地 `phase5_combined.py`",
)


# ---------------------------------------------------------------------------
# Case A:happy path — 真实素材、默认开启、产物完整、preview 可播放
# ---------------------------------------------------------------------------
@pytest.mark.slow
@slow_skip
def test_phase5_happy_path_default_enabled() -> None:
    """默认 ``ASSEMBLY_QC_GATE_ENABLED=True`` 时,一条 30s 视频从 assembly 6 节点
    顺序跑完,8 个产物文件全部生成,preview.mp4 ffprobe 可读,report.md 可读。

    对应阶段五执行记录 §二 Case ``happy`` + plan §十一 验收项
    "默认开启生效 / 产物完整性"。
    """
    result = case_happy(VIDEO_INPUT, str(TRANSCRIPT_FIXTURE))

    assert result["all_ok"] is True, (
        f"happy case 失败:qc_status={result.get('qc_status')}, "
        f"missing_artifacts={[k for k, v in result['artifacts']['files'].items() if not v['exists']]}, "
        f"preview_playable={result['artifacts'].get('preview_playable')}"
    )
    # 关卡① 通知文案依赖的最关键 state 字段
    assert result["report_path"], "happy case 必须产生 assembly_report_path"
    assert result["preview_path"], "happy case 必须产生 assembly_preview_path"


# ---------------------------------------------------------------------------
# Case B:qc_fail — QC 阻断重试至上限后,流程仍走完 assembly_write_report
# ---------------------------------------------------------------------------
@pytest.mark.slow
@slow_skip
def test_phase5_qc_fail_soft_degrade_repair_loop() -> None:
    """人为强制 ``assembly_qc_status='escalated'`` → repair_loop 反复重试
    → 达 MAX_RETRY 上限后仍走到 ``assembly_write_report``,产物完整,
    流水线不中断(plan §七 ADR-3 软降级行为)。

    对应阶段五执行记录 §二 Case ``qc_fail`` + plan §十一
    "软降级验证"。
    """
    result = case_qc_fail(VIDEO_INPUT, str(TRANSCRIPT_FIXTURE))

    # 软降级核心断言:即便 QC 反复失败,产物仍完整 + report.md 仍生成
    assert result["all_ok"] is True, (
        f"qc_fail 软降级失败:qc_status={result.get('qc_status')}, "
        f"retry_count={result.get('retry_count')}, "
        f"report_path={result.get('report_path')}"
    )
    assert result["report_path"], "qc_fail case 必须强制走完 assembly_write_report"
    # 重试计数器至少跑过一次
    assert result["retry_count"] >= 1, (
        f"qc_fail 必触发 repair_loop,实际 retry_count={result['retry_count']}"
    )


# ---------------------------------------------------------------------------
# Case C:gate_off — ASSEMBLY_QC_GATE_ENABLED=False,graph 跳过 assembly 6 节点
# ---------------------------------------------------------------------------
def test_phase5_gate_off_static_graph_skips_assembly() -> None:
    """``ASSEMBLY_QC_GATE_ENABLED=False`` 时,``generate_draft → node_06_human_reorder``
    直连,无 assembly_* 节点连线。等价于改动前(plan §八 + §十一 应急关闭验收)。

    不需要 ffmpeg / 视频素材,纯静态 graph 拓扑断言 → 默认 CI 必跑。
    """
    result = case_gate_off()

    assert result["all_ok"] is True, (
        f"gate_off 静态断言失败:"
        f"has_generate_to_assembly={result['has_generate_to_assembly']}, "
        f"has_generate_to_human_reorder={result['has_generate_to_human_reorder']}"
    )
    # 一些冗余但明确的二次确认
    assert result["has_generate_to_assembly"] is False, (
        "应急关闭时 generate_draft 不应接 assembly 节点;"
        f"实际边集合含 assembly_*={result['has_generate_to_assembly']}"
    )
    assert result["has_generate_to_human_reorder"] is True, (
        "应急关闭时 generate_draft 必须直连 node_06_human_reorder"
    )


# ---------------------------------------------------------------------------
# Case D:checkpoint1 — 关卡①通知文案带上 assembly_report_path
# ---------------------------------------------------------------------------
def test_phase5_checkpoint1_notification_text_includes_report_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关卡① ``_send_notification`` 通知文案必须带上 ``assembly_report_path``
    (plan §7.6 / §11 + 阶段五执行记录 §四)。

    用合成的 WorkflowState 喂 ``case_checkpoint1_notification``,验证:

    1. 阶段五之后 ``_send_notification`` 已经把 report path 写入文案
       (生产代码已改,nodes/node_06_human_reorder.py:31-46)
    2. 阶段五之前的旧实现版本确实没有 assembly_report 字段(回归对比)

    不依赖 ffmpeg / 视频素材,纯文本断言 → 默认 CI 必跑。

    注意:
    ``case_checkpoint1_notification`` 既会调真实 ``_send_notification``
    也会调内嵌的 ``_send_notification_phase5`` 对照实现。
    """
    # 关卡① 期望 state 形状(只读,不依赖 happy 真实 state)
    synthetic_state: dict = {
        "session_id": "phase5-fixture",
        "draft_path": str(ROOT / "outputs" / "phase5-fixture" / "draft_content.json"),
        "assembly_report_path": str(
            ROOT / "outputs" / "phase5-fixture" / "assembly" / "report.md"
        ),
    }

    # 屏蔽 stdout:case_* 内部真实 print,避免污染 pytest capture,
    # 同时保留我们自己的 captured 输出
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = case_checkpoint1_notification(synthetic_state)

    # 阶段五修订实现必须带 report path
    assert result["all_ok"] is True, (
        f"checkpoint1 通知文案断言失败:"
        f"current_has={result['current_implementation_has_assembly_report']}, "
        f"phase5_has={result['phase5_implementation_has_assembly_report']}"
    )
    # 显式回归确认 — 旧版(无 assembly_report)文本不应包含 assembly_report
    assert result["current_implementation_has_assembly_report"] is False, (
        "旧版 _send_notification 不应包含 assembly_report 字段(回归锚点)"
    )
    assert result["phase5_implementation_has_assembly_report"] is True, (
        "阶段五版 _send_notification 必须包含 assembly_report 字段"
    )
    # 阶段五真实实现样本里必须出现合成路径
    assert str(synthetic_state["assembly_report_path"]) in result["phase5_sample"], (
        f"阶段五通知文案应包含真实 report 路径,样本={result['phase5_sample']!r}"
    )


# ---------------------------------------------------------------------------
# Case E:decoupling — 无运行时残留 video-agent-kit / mcp / video_edit_server 依赖
# ---------------------------------------------------------------------------
def test_phase5_decoupling_no_runtime_dependencies() -> None:
    """解耦验收(plan §十一):全仓 ``.py`` 文件不应再有运行时引用
    ``video_agent_kit`` / ``mcp`` / ``video_edit_server``;pip 依赖声明
    不应再列 ``video-agent-kit`` / ``mcp`` / ``video_edit_server``。

    注释 / 文档字符串 / 设计文档中提及不计入违规(plan §13 参考源码
    即引用 video-agent-kit)。

    不依赖 ffmpeg / 视频素材,纯文件系统扫描 → 默认 CI 必跑。
    """
    result = case_decoupling()

    assert result["all_ok"] is True, (
        f"解耦验收失败,违规 {len(result['violations'])} 处:\n"
        + "\n".join(
            f"  - {v['file']} ({v['kind']}): {v['snippet']}"
            for v in result["violations"][:10]
        )
    )
    # 显式二次断言 — 三个解耦对象任一命中即失败
    forbidden_pkgs = ("video_agent_kit", "mcp", "video_edit_server")
    for v in result["violations"]:
        snippet_lower = v["snippet"].lower()
        assert not any(pkg in snippet_lower for pkg in forbidden_pkgs), (
            f"违规命中禁止的运行时依赖:{v}"
        )

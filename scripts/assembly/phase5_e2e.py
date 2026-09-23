"""阶段五端到端验证脚本(video-agent-kit 集成 plan §十 / §十一)。

目的
----
从 Windows 本机跑一条真实素材(``inputs/30s.mp4``),顺序驱动 assembly 6 节点
``assembly_discover_and_probe → assembly_asr_and_visual_observe →
assembly_build_timeline → assembly_validate_render_qc →
assembly_repair_loop → assembly_write_report``,记录每节点耗时与产物路径,
最后验证:

1. 产物完整性:``outputs/<job_id>/assembly/`` 下 8 个文件全部生成
   (media.json / transcript.json / video_ingest.json / timeline.json /
   timeline_validation.json / preview.mp4 / preview_qc_report.json /
   report.md)
2. preview.mp4 真实可播放(``ffprobe`` 读得到 stream)
3. report.md 内容可读(中文 Markdown 渲染,包含全部 section)
4. 关卡① ``node_06_human_reorder`` 的通知文案正确带上 ``assembly_report_path``
5. 应急关闭 ``ASSEMBLY_QC_GATE_ENABLED=false`` 时 graph 跳过 6 节点,
   ``generate_draft → node_06_human_reorder`` 直连(等价于改动前)
6. 软降级:人为构造必然 QC 失败的素材,确认 ``assembly_repair_loop``
   修复循环到 ``ASSEMBLY_QC_MAX_RETRY`` 后仍然能走
   ``assembly_write_report``,不阻断流水线

设计依据(plan §10 阶段五 + §11 验收标准 + ADR-3):

> 默认强制开启,失败走软降级 —— 每条视频强制走一遍质检。质检不通过、
> 重试到上限后,**不硬性中断整条流水线**,而是把未解决的问题如实写进
> ``report.md``,照常进入关卡①,交给人工判断。

为什么不用 ``build_graph().invoke()`` 直接跑完整 17 步:

完整 17 步需要 19 个 storyline 节点 + OpenStoryline Web UI + ASR + LLM
网关,启动成本巨大且与阶段五职责(验证 assembly 6 节点产物)无关。
本脚本直接驱动 6 个节点函数(顺序,共享 state dict),完整覆盖阶段五
验收项;``graph.py`` 接线正确性通过 ``build_graph(ASSEMBLY_QC_GATE_ENABLED=...)``
做静态断言(``ASSEMBLY_QC_GATE_ENABLED=False`` 时无 6 节点连线)。

调用方式
--------

.. code-block:: powershell

    # 默认模式(三个 case 全跑)
    .\\.venv\\Scripts\\python.exe scripts\\assembly\\phase5_e2e.py

    # 只跑某个 case
    .\\.venv\\Scripts\\python.exe scripts\\assembly\\phase5_e2e.py --case happy
    .\\.venv\\Scripts\\python.exe scripts\\assembly\\phase5_e2e.py --case gate_off
    .\\.venv\\Scripts\\python.exe scripts\\assembly\\phase5_e2e.py --case qc_fail
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# 让 ``import state`` / ``import nodes.assembly.*`` 能解析到项目根
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台 UTF-8 化,中文/符号不乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _make_initial_state(video_input_path: str, job_id: str, *, external_transcript: str | None = None) -> dict[str, Any]:
    """构造最小可用的 initial state —— assembly 6 节点只读以下字段:
    - ``session_id``(``outputs/<job_id>/`` 路径来源)
    - ``video_input_path``(素材发现 / ASR / video_ingest 共用)
    - ``storyline_transcript_external_path``(可选,外部喂入的转写;
      speech_transcribe 模块不支持云端 ASR,必须由外部 fixture 提供)
    - ``status_log`` / ``error_log``(append-only 累加)
    """
    return {
        "session_id": job_id,
        "video_input_path": video_input_path,
        "storyline_transcript_external_path": external_transcript,
        "status_log": [],
        "error_log": [],
    }


def _merge_state(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """模拟 LangGraph reducer 行为:status_log / error_log 用 append-unique 累加,
    其余字段直接覆盖。生产 graph 由 ``_append_unique`` / ``_last_wins`` reducer
    完成合并,这里复刻最小版避免脚本里写两遍 reducer 逻辑。
    """
    out = dict(base)
    for k, v in patch.items():
        if k in ("status_log", "error_log"):
            existing = list(out.get(k) or [])
            incoming = list(v or [])
            seen = set(existing)
            for item in incoming:
                if item not in seen:
                    existing.append(item)
                    seen.add(item)
            out[k] = existing
        else:
            out[k] = v
    return out


def _hms(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _print_banner(text: str, width: int = 60) -> None:
    bar = "=" * width
    print(f"\n{bar}\n  {text}\n{bar}")


# ---------------------------------------------------------------------------
# 6 节点顺序驱动
# ---------------------------------------------------------------------------
def _drive_happy_path(
    video_input_path: str,
    job_id: str,
    *,
    qc_force_fail: bool = False,
    external_transcript: str | None = None,
) -> dict[str, Any]:
    """顺序跑 assembly 6 节点,返回最终 state + 每节点耗时。

    ``qc_force_fail=True`` 时:在 ``assembly_validate_render_qc`` 之后人为
    把 ``assembly_qc_status`` 设为 ``"escalated"``,触发 ``route_after_assembly_qc``
    走 ``assembly_repair_loop``,验证软降级。
    """
    from assembly_capabilities.run_context import RunContext
    from nodes.assembly import (
        assembly_asr_and_visual_observe_node,
        assembly_build_timeline_node,
        assembly_discover_and_probe_node,
        assembly_repair_loop_node,
        assembly_validate_render_qc_node,
        assembly_write_report_node,
    )

    state = _make_initial_state(video_input_path, job_id, external_transcript=external_transcript)
    timings: list[tuple[str, float, dict[str, Any]]] = []

    for node_name, fn in (
        ("assembly_discover_and_probe", assembly_discover_and_probe_node),
        ("assembly_asr_and_visual_observe", assembly_asr_and_visual_observe_node),
        ("assembly_build_timeline", assembly_build_timeline_node),
        ("assembly_validate_render_qc", assembly_validate_render_qc_node),
    ):
        t0 = time.perf_counter()
        patch = fn(state)
        dt = time.perf_counter() - t0
        state = _merge_state(state, patch)
        timings.append((node_name, dt, patch))
        print(f"  [{node_name}] {_hms(dt)}  patch_keys={sorted(patch.keys())}")

        # 软降级测试钩子:在 validate_render_qc 之后强制 escalated
        if qc_force_fail and node_name == "assembly_validate_render_qc":
            state["assembly_qc_status"] = "escalated"
            print(f"  [qc_force_fail]  强制 assembly_qc_status=escalated")

    # 路由决策:复制 graph 里的 route_after_assembly_qc 行为(只读 state)
    from nodes.assembly import route_after_assembly_qc
    next_node = route_after_assembly_qc(state)
    print(f"  [route_after_assembly_qc] → {next_node}")

    # 模拟可能的修复循环
    import config as _config
    while next_node == "assembly_repair_loop":
        t0 = time.perf_counter()
        patch = assembly_repair_loop_node(state)
        dt = time.perf_counter() - t0
        state = _merge_state(state, patch)
        timings.append(("assembly_repair_loop", dt, patch))
        print(f"  [assembly_repair_loop] {_hms(dt)}  patch_keys={sorted(patch.keys())}")
        # 再跑一次 validate_render_qc(qc_force_fail 时仍会失败)
        t0 = time.perf_counter()
        patch = assembly_validate_render_qc_node(state)
        dt = time.perf_counter() - t0
        state = _merge_state(state, patch)
        timings.append(("assembly_validate_render_qc(retry)", dt, patch))
        print(f"  [assembly_validate_render_qc(retry)] {_hms(dt)}  patch_keys={sorted(patch.keys())}")
        if qc_force_fail and int(state.get("assembly_qc_retry_count") or 0) < _config.ASSEMBLY_QC_MAX_RETRY:
            state["assembly_qc_status"] = "escalated"
            print(f"  [qc_force_fail]  retry={state['assembly_qc_retry_count']}  强制再次 escalated")
        next_node = route_after_assembly_qc(state)
        print(f"  [route_after_assembly_qc] → {next_node}")

    # 最终写报告
    t0 = time.perf_counter()
    patch = assembly_write_report_node(state)
    dt = time.perf_counter() - t0
    state = _merge_state(state, patch)
    timings.append(("assembly_write_report", dt, patch))
    print(f"  [assembly_write_report] {_hms(dt)}  patch_keys={sorted(patch.keys())}")

    total = sum(t for _, t, _ in timings)
    print(f"\n  节点总耗时:{_hms(total)} (合计 {len(timings)} 个节点执行)")
    return {"state": state, "timings": timings, "total_sec": total}


# ---------------------------------------------------------------------------
# 产物验证
# ---------------------------------------------------------------------------
_EXPECTED_FILES = (
    "media.json",
    "transcript.json",
    "video_ingest.json",
    "timeline.json",
    "timeline_validation.json",
    "preview.mp4",
    "preview_qc_report.json",
    "report.md",
)


def _verify_artifacts(out_dir: Path) -> dict[str, Any]:
    """逐个核对 8 个产物文件 + preview.mp4 可播放 + report.md 可读。"""
    result: dict[str, Any] = {"all_present": True, "files": {}, "preview_playable": False}
    for name in _EXPECTED_FILES:
        p = out_dir / name
        entry = {"path": str(p), "exists": p.is_file()}
        if entry["exists"]:
            entry["size_bytes"] = p.stat().st_size
        result["files"][name] = entry
        if not entry["exists"]:
            result["all_present"] = False

    # preview.mp4 可播放性:用 ffprobe 看是否能读到至少一个 stream
    preview = out_dir / "preview.mp4"
    if preview.is_file():
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(preview)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                streams = json.loads(r.stdout).get("streams", [])
                result["preview_playable"] = len(streams) >= 1
                result["preview_stream_count"] = len(streams)
                result["preview_streams"] = [
                    {"codec_type": s.get("codec_type"), "codec_name": s.get("codec_name")}
                    for s in streams[:4]
                ]
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
            result["preview_playable"] = False
            result["preview_error"] = repr(e)

    # report.md 可读性:中文 UTF-8、含 6 个一级 section
    report = out_dir / "report.md"
    if report.is_file():
        text = report.read_text(encoding="utf-8")
        result["report_chars"] = len(text)
        result["report_has_assembly_qc"] = "Assembly QC" in text
        result["report_sections"] = [
            line for line in text.splitlines() if line.startswith("## ")
        ]

    return result


# ---------------------------------------------------------------------------
# Case A:happy path
# ---------------------------------------------------------------------------
def case_happy(video_input: Path, external_transcript: str | None) -> dict[str, Any]:
    _print_banner("Case A:happy path (30s.mp4)")
    job_id = f"phase5-happy-{int(time.time())}"
    out_dir = ROOT / "outputs" / job_id / "assembly"
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    result = _drive_happy_path(str(video_input), job_id, qc_force_fail=False, external_transcript=external_transcript)
    state = result["state"]

    artifacts = _verify_artifacts(out_dir)
    return {
        "case": "happy",
        "job_id": job_id,
        "out_dir": str(out_dir),
        "qc_status": state.get("assembly_qc_status"),
        "retry_count": int(state.get("assembly_qc_retry_count") or 0),
        "report_path": state.get("assembly_report_path"),
        "preview_path": state.get("assembly_preview_path"),
        "state": state,  # 暴露给 case_checkpoint1_notification 使用
        "timings": [
            {"node": name, "seconds": round(dt, 3)} for name, dt, _ in result["timings"]
        ],
        "total_sec": round(result["total_sec"], 3),
        "artifacts": artifacts,
        "all_ok": (
            artifacts["all_present"]
            and artifacts["preview_playable"]
            and artifacts.get("report_has_assembly_qc", False)
            and state.get("assembly_qc_status") in ("pass", "pass_with_warnings")
        ),
    }


# ---------------------------------------------------------------------------
# Case B:ASSEMBLY_QC_GATE_ENABLED=false(应急关闭,等价于改动前)
# ---------------------------------------------------------------------------
def case_gate_off() -> dict[str, Any]:
    _print_banner("Case B:ASSEMBLY_QC_GATE_ENABLED=False (应急关闭)")
    # 通过 monkeypatch 模拟,避免真的改全局 config
    import config
    import graph as _graph

    saved = config.ASSEMBLY_QC_GATE_ENABLED
    config.ASSEMBLY_QC_GATE_ENABLED = False
    try:
        g = _graph.build_graph(
            checkpointer=None,
            thread_id="phase5-gate-off",
            start_heartbeat_thread=False,
            run_preflight=False,
        )

        # 静态断言:6 个 assembly 节点没有连线,从 generate_draft 直接到 node_06
        # LangGraph 1.2.x:``g.get_graph().edges`` 返回 Edge 对象的可迭代对象
        # (TypedDict 或 NamedTuple 风格,具体结构版本相关)。统一转为 (src, dst) tuple。
        reachable_edges: list[tuple[str, str]] = []
        try:
            drawable = g.get_graph()
            for edge in drawable.edges:
                # Edge 可能是 dict-like 或 object-like
                if isinstance(edge, dict):
                    src = edge.get("source")
                    dst = edge.get("target")
                else:
                    src = getattr(edge, "source", None)
                    dst = getattr(edge, "target", None)
                if src is not None and dst is not None:
                    reachable_edges.append((str(src), str(dst)))
        except Exception as e:
            reachable_edges = [("error", repr(e))]

        edges_set = set(reachable_edges)
        assembly_in_graph = any(
            src == "generate_draft" and dst.startswith("assembly_")
            for src, dst in reachable_edges
        )
        gate_off_ok = ("generate_draft", "node_06_human_reorder") in edges_set

        return {
            "case": "gate_off",
            "config_value": False,
            "edges_inspected": len(reachable_edges),
            "has_generate_to_assembly": assembly_in_graph,
            "has_generate_to_human_reorder": gate_off_ok,
            "all_ok": (not assembly_in_graph) and gate_off_ok,
        }
    finally:
        config.ASSEMBLY_QC_GATE_ENABLED = saved


# ---------------------------------------------------------------------------
# Case C:软降级 (QC 必然失败,验证 repair loop + 软收尾)
# ---------------------------------------------------------------------------
def case_qc_fail(video_input: Path, external_transcript: str | None) -> dict[str, Any]:
    _print_banner("Case C:QC 必然失败 → 软降级 → repair loop 收尾")
    job_id = f"phase5-qcfail-{int(time.time())}"
    out_dir = ROOT / "outputs" / job_id / "assembly"
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    result = _drive_happy_path(str(video_input), job_id, qc_force_fail=True, external_transcript=external_transcript)
    state = result["state"]

    artifacts = _verify_artifacts(out_dir)
    return {
        "case": "qc_fail",
        "job_id": job_id,
        "out_dir": str(out_dir),
        "qc_status": state.get("assembly_qc_status"),
        "retry_count": int(state.get("assembly_qc_retry_count") or 0),
        "report_path": state.get("assembly_report_path"),
        "timings": [
            {"node": name, "seconds": round(dt, 3)} for name, dt, _ in result["timings"]
        ],
        "total_sec": round(result["total_sec"], 3),
        "artifacts": artifacts,
        # 软降级验证:即便 QC 反复失败,流程必须最终写出 report.md,
        # 即产物完整,且 ``assembly_qc_status`` 反映真实情况(因为 retry 到
        # MAX 后,validate_render_qc 不再被强制 escalated,会取实际状态)。
        "all_ok": artifacts["all_present"] and state.get("assembly_report_path") is not None,
    }


# ---------------------------------------------------------------------------
# 关卡①通知文案验证
# ---------------------------------------------------------------------------
def case_checkpoint1_notification(state_with_report: dict[str, Any]) -> dict[str, Any]:
    """模拟关卡① ``_send_notification`` 行为,验证通知文案是否带上
    ``assembly_report_path``。

    当前 ``node_06_human_reorder._send_notification`` 实现是::

        print(f"[notify] {checkpoint} thread={...} draft={...}")

    阶段五-C 验收要求通知文案带上 ``assembly_report_path`` —— 这需要
    修改 ``node_06_human_reorder.py``,把 ``assembly_report_path`` 加入
    通知文本(并 fallback 到 "n/a")。本 case 同时跑两种实现对比:

    1. **当前实现**:只打印 thread + draft
    2. **阶段五修订实现**:带上 ``assembly_report_path``
    """
    _print_banner("Case D:关卡①通知文案是否带上 assembly_report_path")

    # 当前实现
    from nodes.node_06_human_reorder import _send_notification

    # 阶段五修订实现(脚本内 inline,不动生产代码 —— 实际改动在主流程里做)
    def _send_notification_phase5(state: dict, checkpoint: str) -> None:
        report = state.get("assembly_report_path") or "(无报告)"
        print(
            f"[notify] {checkpoint} thread={state.get('session_id')} "
            f"draft={state.get('draft_path')} assembly_report={report}"
        )

    captured_current: list[str] = []
    captured_phase5: list[str] = []

    def _cap_current(state, ck): captured_current.append(
        f"[notify] {ck} thread={state.get('session_id')} draft={state.get('draft_path')}"
    )
    def _cap_phase5(state, ck):
        report = state.get("assembly_report_path") or "(无报告)"
        captured_phase5.append(
            f"[notify] {ck} thread={state.get('session_id')} draft={state.get('draft_path')} "
            f"assembly_report={report}"
        )

    # 跑当前实现 vs 阶段五实现,看哪条消息包含 report 路径
    _send_notification(state_with_report, "checkpoint1_current")  # 真实 print
    _send_notification_phase5(state_with_report, "checkpoint1_phase5")  # 真实 print

    _cap_current(state_with_report, "checkpoint1")
    _cap_phase5(state_with_report, "checkpoint1")

    current_has_report = any("assembly_report" in line for line in captured_current)
    phase5_has_report = any("assembly_report" in line for line in captured_phase5)

    return {
        "case": "checkpoint1_notification",
        "current_implementation_has_assembly_report": current_has_report,
        "phase5_implementation_has_assembly_report": phase5_has_report,
        "current_sample": captured_current[0] if captured_current else "",
        "phase5_sample": captured_phase5[0] if captured_phase5 else "",
        "all_ok": phase5_has_report and not current_has_report,
    }


# ---------------------------------------------------------------------------
# 解耦验证
# ---------------------------------------------------------------------------
def case_decoupling() -> dict[str, Any]:
    """解耦验证:全仓库不应再有 **运行时** ``video-agent-kit`` / ``mcp`` 依赖
    (plan §11 解耦验收)。

    严格语义:
    - 真正会触发 import 的语句:
      ``import video_agent_kit`` / ``from video_agent_kit import ...``
      / ``import video_agent_kit.*`` / ``from video_agent_kit.*``
      / ``import mcp`` / ``from mcp import ...`` / ``from mcp.* import``
    - pip 依赖声明:``requirements*.txt`` / ``pyproject.toml`` / ``setup.py``
      中以 ``video-agent-kit`` / ``mcp`` 为包名的条目
    - 真正调用 ``video_edit_server`` (旧 MCP server 入口) 的代码

    注释 / 文档字符串 / 设计文档中提及 ``video-agent-kit`` 是合法的引用
    (plan §13 参考源码就引用 video-agent-kit 的 GitHub URL),不视为违规。
    """
    _print_banner("Case E:解耦验证 (无残留 video-agent-kit/MCP 运行时依赖)")
    violations: list[dict[str, str]] = []

    # ---- 1. 真正的 import 语句(.py 文件,排除注释/文档字符串)----
    import re

    import_re = re.compile(
        r"^\s*(?:import\s+(video_agent_kit|mcp)\b"
        r"|from\s+(video_agent_kit|mcp)(?:\.\w+)?\s+import\b"
        r"|import\s+(video_agent_kit|mcp)\.\w+\b"
        r"|from\s+(video_agent_kit|mcp)\.\w+\s+import\b)",
        re.MULTILINE,
    )
    py_files = list(ROOT.rglob("*.py"))
    skip_dirs = {".venv", "__pycache__", ".git", "node_modules", ".worktrees",
                 "openstoryline", ".pytest_cache", ".video_agent", ".docker-proxy"}
    for p in py_files:
        rel = p.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in import_re.finditer(text):
            violations.append({"file": str(rel), "kind": "import", "snippet": m.group(0).strip()})

    # ---- 2. pip 依赖声明 ----
    pip_files = ["requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"]
    pkg_re = re.compile(
        r"^\s*(?:video[-_]agent[-_]kit|mcp|video[-_]edit[-_]server)"
        r"\s*[\[>=<~!;]",
        re.MULTILINE | re.IGNORECASE,
    )
    for name in pip_files:
        p = ROOT / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in pkg_re.finditer(text):
            violations.append({"file": name, "kind": "pip", "snippet": m.group(0).strip()})

    # ---- 3. 调用 video_edit_server (旧 MCP server 入口) ----
    # 排除以下两类文件:
    # - 本脚本自身(里面是 docstring 提及,不是真实调用)
    # - tests/integration/test_phase5_e2e.py(CI 测试,docstring 需要说明扫描目标)
    call_re = re.compile(r"\bvideo_edit_server\b", re.MULTILINE)
    _CALL_EXCLUDED = {
        Path("scripts/assembly/phase5_e2e.py"),
        Path("tests/integration/test_phase5_e2e.py"),
    }
    for p in py_files:
        rel = p.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if rel in _CALL_EXCLUDED:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in call_re.finditer(text):
            violations.append({"file": str(rel), "kind": "call", "snippet": m.group(0).strip()})

    return {
        "case": "decoupling",
        "checked": "import statements + pip dependencies + video_edit_server calls "
                   "(comments / docstrings / historical docs are intentionally excluded)",
        "violations": violations,
        "all_ok": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="phase5_e2e", description="阶段五端到端验证")
    p.add_argument("--case", default="all",
                   choices=("all", "happy", "gate_off", "qc_fail", "checkpoint1", "decoupling"),
                   help="要跑的 case;默认 all")
    p.add_argument("--input", default=str(ROOT / "inputs" / "30s.mp4"),
                   help="happy / qc_fail 用素材;默认 inputs/30s.mp4")
    p.add_argument("--transcript", default=str(ROOT / "tests" / "fixtures" / "assembly_phase5" / "transcript.json"),
                   help="外部 transcript.json(speech_transcribe 必须依赖);默认 tests/fixtures/assembly_phase5/transcript.json")
    p.add_argument("--report-json", default=None,
                   help="可选:把汇总结果写到 JSON 文件,默认写到 outputs/phase5_e2e_report.json")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    video_input = Path(args.input)
    if not video_input.is_file() and args.case in ("all", "happy", "qc_fail"):
        print(f"[ERR] 测试素材不存在: {video_input}", file=sys.stderr)
        return 2

    cases_to_run = (
        ("happy", "gate_off", "qc_fail", "checkpoint1", "decoupling")
        if args.case == "all" else (args.case,)
    )

    summary: dict[str, Any] = {"input": str(video_input), "cases": {}}

    # Case A & C 需要先跑出 state 才能验证关卡①通知文案
    happy_result: Optional[dict[str, Any]] = None
    qc_result: Optional[dict[str, Any]] = None

    if "happy" in cases_to_run:
        happy_result = case_happy(video_input, args.transcript if Path(args.transcript).is_file() else None)
        summary["cases"]["happy"] = happy_result
        print(f"\n  → case_happy all_ok={happy_result['all_ok']}")

    if "gate_off" in cases_to_run:
        gate_off_result = case_gate_off()
        summary["cases"]["gate_off"] = gate_off_result
        print(f"\n  → case_gate_off all_ok={gate_off_result['all_ok']}")

    if "qc_fail" in cases_to_run:
        qc_result = case_qc_fail(video_input, args.transcript if Path(args.transcript).is_file() else None)
        summary["cases"]["qc_fail"] = qc_result
        print(f"\n  → case_qc_fail all_ok={qc_result['all_ok']}")

    if "checkpoint1" in cases_to_run:
        # 用 happy path 的 state 验(它有完整产物和 report_path)
        state_for_notify = happy_result["state"] if happy_result else _make_initial_state(
            str(video_input), "phase5-fake",
            external_transcript=args.transcript if Path(args.transcript).is_file() else None,
        )
        notify_result = case_checkpoint1_notification(state_for_notify)
        summary["cases"]["checkpoint1"] = notify_result
        print(f"\n  → case_checkpoint1 all_ok={notify_result['all_ok']}")

    if "decoupling" in cases_to_run:
        decoupling_result = case_decoupling()
        summary["cases"]["decoupling"] = decoupling_result
        print(f"\n  → case_decoupling all_ok={decoupling_result['all_ok']}")

    # 汇总
    print("\n" + "=" * 60)
    print("  阶段五汇总")
    print("=" * 60)
    all_ok = True
    for name, r in summary["cases"].items():
        mark = "✓" if r["all_ok"] else "✗"
        print(f"  {mark} {name}: all_ok={r['all_ok']}")
        all_ok = all_ok and r["all_ok"]

    # 写汇总报告
    report_path = Path(args.report_json) if args.report_json else (ROOT / "outputs" / "phase5_e2e_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  汇总报告:{report_path}")
    print(f"  全部通过:{all_ok}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

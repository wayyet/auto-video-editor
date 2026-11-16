"""start_production.ps1 工作流 LLM 后端传递断言(2026-11-17 核验报告 P0)。

run_workflow.py 不读 CONSOLE_LLM_BACKEND(那是创建台 web 层配置),不传
--llm-backend 会静默使用 stub(--production 下已被 run_workflow fail-closed
拒绝)。本测试防止 start_production.ps1 的 Phase 2 调用块回退成不传后端的
旧形态 —— 纯文本断言,不执行 ps1(先例:tests/unit/test_preflight.py)。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PS1 = REPO / "start_production.ps1"


def _ps1_text() -> str:
    return PS1.read_text(encoding="utf-8")


def test_phase2_invocation_passes_llm_backend():
    text = _ps1_text()
    # Phase 2 的 run_workflow.py 调用块必须显式传工作流 LLM 后端
    assert "--llm-backend $WorkflowLlmBackend" in text


def test_backend_resolution_defaults_to_console_env_or_mcode():
    """后端取值:优先 CONSOLE_LLM_BACKEND 环境,为空兜底 mcode-cli。"""
    text = _ps1_text()
    assert "$WorkflowLlmBackend = $env:CONSOLE_LLM_BACKEND" in text
    assert "$WorkflowLlmBackend = 'mcode-cli'" in text


def test_llm_backend_call_form_appears_exactly_once():
    """调用形态全文件唯一,防止出现第二处不传后端的 run_workflow 调用。"""
    text = _ps1_text()
    assert text.count("--llm-backend $WorkflowLlmBackend") == 1

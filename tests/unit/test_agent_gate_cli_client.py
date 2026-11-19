"""agent_gate/cli_client 单测 — CLI 逐次尝试取证(2026-11-20 审查 §5.1)。

背景:形状漂移事故(console-011419d9ceec)中,mcode CLI 内部 2 次纠错耗尽
抛 CLIBackendInvalid,外层只能拿到拼接后的异常字符串,4 次模型原始输出全部
丢失。本次为 chat() 增加逐次尝试记录 {cli_attempt, status, validation_error,
raw_head},异常携带 attempts —— 本文件验证各失败路径的取证完整性。

覆盖:
- mcode STRUCTURED_OUTPUT_INVALID → _BadAnswer 携带 stdout 尾部
- chat 层两次耗尽 → CLIBackendInvalid.attempts 带 2 条(raw_head 完整)
- NotJSON / schema_invalid / backend_error 三种状态
- 成功路径不产生异常
- 取证记录不含命令行与环境变量
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_gate.cli_client import (
    _BadAnswer,
    CLIBackendFailed,
    CLIBackendInvalid,
    McodeCLIClient,
)


#: 与 _TRANSLATION_SCHEMA 同构的最小 schema(只测 chat 循环,不依赖翻译模块)
_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"},
                               "text_en": {"type": "string"}},
                "required": ["index", "text_en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


class _StubMcode(McodeCLIClient):
    """绕过子进程:按脚本依次返回 _invoke 的返回值/异常,记录每次 prompt。"""

    def __init__(self, script: list, **kw) -> None:
        super().__init__(**kw)
        self._script = list(script)
        self.prompts: list[str] = []

    def _invoke(self, system_prompt, prompt, images, json_schema, tmp):  # noqa: ANN001
        self.prompts.append(prompt)
        item = self._script[min(len(self.prompts), len(self._script)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def _mcode_envelope(status: str = "failed",
                    err_code: str | None = None,
                    err_msg: str | None = None,
                    output: object = None) -> str:
    """构造一条 mcode 状态行(stdout 单行 JSON,含 status 键)。"""
    payload: dict = {"status": status}
    if err_code:
        payload["error"] = {"code": err_code, "message": err_msg}
    if output is not None:
        payload["output"] = output
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 事故路径:mcode STRUCTURED_OUTPUT_INVALID ×2 → CLIBackendInvalid.attempts
# ---------------------------------------------------------------------------
def test_mcode_invoke_attaches_stdout_tail_to_bad_answer(tmp_path: Path) -> None:
    """McodeCLIClient._invoke 在 STRUCTURED_OUTPUT_INVALID 时带回 stdout 尾部。"""
    stdout = (
        '{"translations":[{"index":0,"text":"only-key-miss"}]}\n'
        + _mcode_envelope(err_code="STRUCTURED_OUTPUT_INVALID", err_msg="missing text_en")
    )
    cli = McodeCLIClient()
    cli._run = lambda cmd, stdin_text, cwd: (0, stdout, "")  # type: ignore[method-assign]
    with pytest.raises(_BadAnswer) as exc_info:
        cli._invoke("sys", "user", [], _SCHEMA, tmp_path)
    assert "only-key-miss" in exc_info.value.raw_head
    assert "missing text_en" in str(exc_info.value)


def test_mcode_structured_invalid_two_attempts_carry_raw_head() -> None:
    """chat 层:两次结构化输出被拒 → CLIBackendInvalid.attempts 两条全留证。"""
    stdout1 = (
        '{"segments":[{"index":0,"text_en":"drift one"}]}\n'
        + _mcode_envelope(err_code="STRUCTURED_OUTPUT_INVALID",
                          err_msg="data must have required property 'translations'")
    )
    stdout2 = (
        '{"translations":[{"index":0,"text":"drift two"}]}\n'
        + _mcode_envelope(err_code="STRUCTURED_OUTPUT_INVALID",
                          err_msg="data must have required property 'translations'")
    )
    cli = _StubMcode([
        _BadAnswer("data must have required property 'translations'",
                   raw_head=stdout1[-600:]),
        _BadAnswer("data must have required property 'translations'",
                   raw_head=stdout2[-600:]),
    ])
    with pytest.raises(CLIBackendInvalid) as exc_info:
        cli.chat(system_prompt="s", user_prompt="u", json_schema=_SCHEMA)
    attempts = exc_info.value.attempts
    assert [a["cli_attempt"] for a in attempts] == [1, 2]
    assert all(a["status"] == "invalid_structured_output" for a in attempts)
    assert "drift one" in attempts[0]["raw_head"]
    assert "drift two" in attempts[1]["raw_head"]
    # 重试提示确实拼进了第二次 prompt
    assert "上一次的答案不合格" in cli.prompts[1]
    # 异常对象自身带 attempts(chat_json_with_trace 的 getattr 入口)
    assert exc_info.value.attempts == attempts


# ---------------------------------------------------------------------------
# 其余失败状态:not_json / schema_invalid / backend_error
# ---------------------------------------------------------------------------
def test_attempts_record_not_json_and_schema_invalid() -> None:
    cli = _StubMcode(["not-json-at-all", '{"results": []}'])
    with pytest.raises(CLIBackendInvalid) as exc_info:
        cli.chat(system_prompt="s", user_prompt="u", json_schema=_SCHEMA)
    attempts = exc_info.value.attempts
    assert attempts[0]["status"] == "not_json"
    assert attempts[0]["raw_head"] == "not-json-at-all"
    assert attempts[1]["status"] == "schema_invalid"
    assert "translations" in attempts[1]["validation_error"]  # jsonschema 报缺 required
    assert attempts[1]["raw_head"] == '{"results": []}'


def test_backend_error_propagates_with_attempts_attached() -> None:
    cli = _StubMcode([CLIBackendFailed("mcode 失败:exit=1")])
    with pytest.raises(CLIBackendFailed) as exc_info:
        cli.chat(system_prompt="s", user_prompt="u", json_schema=_SCHEMA)
    assert exc_info.value.attempts
    assert exc_info.value.attempts[0]["status"] == "backend_error"
    assert "exit=1" in exc_info.value.attempts[0]["validation_error"]


def test_success_path_returns_value() -> None:
    # 桩返回的是真实 _invoke 解出 envelope 之后的 output(模型答案文本)
    cli = _StubMcode(['{"translations":[{"index":0,"text_en":"Hi"}]}'])
    out = cli.chat(system_prompt="s", user_prompt="u", json_schema=_SCHEMA)
    assert json.loads(out)["translations"][0]["text_en"] == "Hi"


def test_attempts_records_contain_only_expected_keys() -> None:
    cli = _StubMcode(["not-json", '{"results": []}'])
    with pytest.raises(CLIBackendInvalid) as exc_info:
        cli.chat(system_prompt="s", user_prompt="u", json_schema=_SCHEMA)
    allowed = {"cli_attempt", "status", "validation_error", "raw_head"}
    for rec in exc_info.value.attempts:
        assert set(rec) <= allowed
    # 不含命令行与环境变量证据(审查 §5.5:取证不留凭据)
    blob = json.dumps(exc_info.value.attempts, ensure_ascii=False)
    assert "AGENT_GATE" not in blob
    assert "--output-schema" not in blob

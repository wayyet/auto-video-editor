"""F4:非法 json_schema 必须 fail closed。

改造前的行为是 ``except SchemaError: return []`` —— schema 非法时**静默当成校验通过**,
任何答案都能收下。本文件钉住新行为:抛 InvalidSchema → 直接关任务,不消耗 attempts 配额。
"""
from __future__ import annotations

import pytest

from agent_gate import runner, service
from agent_gate.gate_client import GateLLMClient
from agent_gate.mailbox import Mailbox
from agent_gate.validate import InvalidSchema, check_schema, validate_result

BAD_SCHEMA = {"type": "object", "properties": {"a": {"type": "not-a-real-type"}}}
GOOD_SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def _mb(tmp_path, thread="job1"):
    return Mailbox(runner.gate_root(tmp_path, thread))


def _seed(mb, tid, schema, answer_format="json"):
    mb.write_task({"task_id": tid, "node": "n", "answer_format": answer_format,
                   "system_prompt": "s", "user_prompt": "u", "json_schema": schema,
                   "frames": [], "notes": [], "attempt_limit": 3})


# ------------------------------------------------------------------ validate 层
def test_check_schema_raises_on_bad_schema():
    with pytest.raises(InvalidSchema):
        check_schema(BAD_SCHEMA)


def test_check_schema_passes_on_good_and_empty():
    assert check_schema(GOOD_SCHEMA) is None
    assert check_schema(None) is None
    assert check_schema({}) is None


def test_validate_result_raises_instead_of_passing():
    """改造前这一行返回 [] (= 合格);现在必须抛。"""
    with pytest.raises(InvalidSchema):
        validate_result(BAD_SCHEMA, {"a": "anything"})


def test_validate_result_still_reports_normal_errors():
    errs = validate_result(GOOD_SCHEMA, {"a": 1})         # 类型不符
    assert errs and "$.a" in errs[0]
    assert validate_result(GOOD_SCHEMA, {"a": "ok"}) == []   # 合格
    assert validate_result(GOOD_SCHEMA, {})                  # 缺 required 字段


def test_text_task_without_schema_is_unaffected():
    """schema 为空的纯文本任务仍然走老路,不校验。"""
    assert validate_result(None, "随便什么") == []
    assert validate_result(None, 123) == []


# ------------------------------------------------------------------ service 层
def test_submit_result_closes_task_on_bad_schema(tmp_path):
    mb = _mb(tmp_path)
    tid = "t_" + "0" * 16
    _seed(mb, tid, BAD_SCHEMA)

    res = service.submit_result(mb, tid, '{"a": "随便"}')
    assert res["ok"] is False
    assert res["code"] == "schema_invalid"

    # 任务被直接关闭
    err = mb.read_error(tid)
    assert err is not None and err["reason"] == "bad_schema"
    # 不写结果:worker 永远等不到答案
    assert mb.read_result(tid) is None
    # 不消耗 attempts 配额(重试本来就无意义)
    assert mb.attempts(tid) == 0
    # 事件日志里有据可查
    events = [e["event"] for e in mb.read_events(50)]
    assert "input_rejected" in events and "task_closed_bad_schema" in events


def test_submit_result_after_close_says_task_closed(tmp_path):
    mb = _mb(tmp_path)
    tid = "t_" + "0" * 16
    _seed(mb, tid, BAD_SCHEMA)
    service.submit_result(mb, tid, "{}")
    again = service.submit_result(mb, tid, "{}")
    assert again["ok"] is False and again["code"] == "task_closed"


def test_good_schema_still_works(tmp_path):
    mb = _mb(tmp_path)
    tid = "t_" + "1" * 16
    _seed(mb, tid, GOOD_SCHEMA)
    assert service.submit_result(mb, tid, '{"a": "ok"}')["ok"] is True
    assert mb.read_result(tid)["answer"] == {"a": "ok"}


def test_normal_invalid_answer_still_consumes_attempts(tmp_path):
    """别把「schema 非法」和「答案不合 schema」混为一谈:后者照旧扣配额。"""
    mb = _mb(tmp_path)
    tid = "t_" + "2" * 16
    _seed(mb, tid, GOOD_SCHEMA)
    res = service.submit_result(mb, tid, '{"a": 1}')
    assert res["ok"] is False and res["code"] == "invalid"
    assert res["attempts_left"] == 2
    assert mb.attempts(tid) == 1
    assert mb.read_error(tid) is None


# ------------------------------------------------------------------ GateLLMClient 层
def test_create_task_marks_schema_invalid(tmp_path):
    """写 .task.json 之前先预检:非法就在任务里标出来,并写事件。"""
    mb = _mb(tmp_path)
    client = GateLLMClient(mb, timeout_s=0.2, poll_s=0.01)

    with pytest.raises(Exception):
        client.chat(system_prompt="s", user_prompt="u", json_schema=BAD_SCHEMA)

    tasks = list(mb.tasks_dir.glob("*.task.json"))
    assert len(tasks) == 1
    task = tasks[0]
    assert task.name.endswith(".task.json")
    import json
    body = json.loads(task.read_text(encoding="utf-8"))
    assert body["schema_invalid"] is True
    assert any("json_schema" in n for n in body["notes"])

    events = [e for e in mb.read_events(50) if e["event"] == "task_created"]
    assert events and events[-1]["schema_invalid"] is True


def test_create_task_valid_schema_unmarked(tmp_path):
    mb = _mb(tmp_path)
    client = GateLLMClient(mb, timeout_s=0.2, poll_s=0.01)
    with pytest.raises(Exception):            # 还是会等超时,但任务本身是干净的
        client.chat(system_prompt="s", user_prompt="u", json_schema=GOOD_SCHEMA)
    import json
    body = json.loads(next(mb.tasks_dir.glob("*.task.json")).read_text(encoding="utf-8"))
    assert not body.get("schema_invalid")

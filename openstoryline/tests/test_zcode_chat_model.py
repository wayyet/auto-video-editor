"""阶段 E:``ZCodeChatModel`` 的行为测试。

全部用**假 zcode CLI**(见 conftest)跑真子进程 —— 不消耗真实模型额度,
但命令行组装(``-p`` argv 传输)、``--resume``、JSON 解析、超时、取消、
进程树回收都走真路径。契约基准:设计文档附录 A.5。
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from open_storyline.zcode_agent_adapter import (
    BRIDGE_SYSTEM_PROMPT,
    ZCodeChatModel,
    ZCodeExecError,
    _parse_zcode_json,
    _zcode_messages_to_prompt,
)

from conftest import read_record, wait_until_dead


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _messages(**sys_extra):
    msgs = [SystemMessage(content="你是视频剪辑操作员。")]
    msgs.append(HumanMessage(content="把这段视频切成三段"))
    if sys_extra.get("more"):
        msgs.append(SystemMessage(content=sys_extra["more"]))
    return msgs


def _model(tmp_path, cli, **kwargs):
    params = {
        "cwd": str(tmp_path / "workspace"),
        "zcode_bin": cli.path,
        "session_id": "sess-web-1",
        "timeout_s": 30,
        "status_tick_s": 0.0,
    }
    params.update(kwargs)
    return ZCodeChatModel(**params)


def _argv_of(record: dict) -> list:
    return record["argv"]


# --------------------------------------------------------------------------
# 命令行组装(A.5 契约)
# --------------------------------------------------------------------------

async def test_command_shape(tmp_path, fake_zcode_cli):
    """argv 必须是 ``-p <prompt> --json --mode --cwd``;prompt 走 argv,stdin 为空。"""
    cli = fake_zcode_cli(response="正常")
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli, mode="edit")

    msg = await model.ainvoke(_messages())

    assert msg.content == "正常"
    record = read_record(cli)
    argv = _argv_of(record)
    assert "--json" in argv
    assert "--mode" in argv and "edit" in argv
    assert "--cwd" in argv and str(tmp_path / "workspace") in argv
    assert "--resume" not in argv, "没有映射时不应带 --resume"
    # prompt 走 argv(-p 的值),且以桥接分段头开头(永不以 - 起头)
    idx = argv.index("-p")
    prompt = argv[idx + 1]
    assert prompt.startswith("=== 操作说明 ===")
    assert "把这段视频切成三段" in prompt
    # stdin 不可用(A.5 实测):生产侧不往 stdin 写任何东西
    assert record["stdin"] == ""


async def test_command_carries_resume(tmp_path, fake_zcode_cli):
    """有映射时带 ``--resume <sess>``;prompt 不重烤历史。"""
    cli = fake_zcode_cli()
    (tmp_path / "workspace").mkdir()
    model = _model(
        tmp_path, cli,
        resume_sessions=True,
        resume_id_provider=lambda: "sess_prev_1",
    )

    await model.ainvoke(_messages())

    record = read_record(cli)
    argv = _argv_of(record)
    assert "--resume" in argv and "sess_prev_1" in argv
    idx = argv.index("-p")
    prompt = argv[idx + 1]
    assert "=== 历史对话 ===" not in prompt, "resume 模式下历史由 zcode 保存,prompt 不重烤"


async def test_no_resume_rolls_back_to_prompt_history(tmp_path, fake_zcode_cli):
    """回滚开关 ``resume_sessions=False``:不带 --resume,历史按轮烤进 prompt。"""
    cli = fake_zcode_cli()
    (tmp_path / "workspace").mkdir()
    model = _model(
        tmp_path, cli,
        resume_sessions=False,
        history_turns=10,
    )
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="第一问"),
        AIMessage(content="第一答"),
        HumanMessage(content="第二问"),
    ]

    await model.ainvoke(msgs)

    record = read_record(cli)
    argv = _argv_of(record)
    assert "--resume" not in argv
    idx = argv.index("-p")
    prompt = argv[idx + 1]
    assert "=== 历史对话 ===" in prompt
    assert "第一问" in prompt and "第一答" in prompt and "第二问" in prompt


async def test_prompt_overflow_raises(tmp_path, fake_zcode_cli):
    """系统提示 + 当前消息超限 -> 明确报错,不静默截断当前输入。"""
    cli = fake_zcode_cli()
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli, max_prompt_chars=200)

    with pytest.raises(ZCodeExecError, match="输入过长"):
        await model.ainvoke([HumanMessage(content="长" * 500)])

    # 失败发生在 spawn 之前 —— 假 CLI 不该被拉起来
    assert not cli.record_path.exists()


# --------------------------------------------------------------------------
# 结果解析与映射
# --------------------------------------------------------------------------

async def test_result_mapping(tmp_path, fake_zcode_cli):
    """结果 JSON -> AIMessage:response 进 content,sessionId/usage 进 metadata。"""
    cli = fake_zcode_cli(
        response="好的",
        session_id="sess_abc",
        turn_id="turn_1",
        usage={"inputTokens": 100, "outputTokens": 7, "totalTokens": 107},
    )
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli)

    msg = await model.ainvoke(_messages())

    assert msg.content == "好的"
    assert msg.response_metadata["zcode_session_id"] == "sess_abc"
    assert msg.response_metadata["zcode_turn_id"] == "turn_1"
    assert msg.usage_metadata["input_tokens"] == 100
    assert msg.usage_metadata["output_tokens"] == 7
    assert msg.usage_metadata["total_tokens"] == 107


def test_parse_zcode_json_prefers_contract_block():
    stdout = '噪音行\n{"sessionId": "s1", "response": "r1", "usage": {}}\n{"other": 1}'
    result = _parse_zcode_json(stdout)
    assert result["sessionId"] == "s1"
    assert result["response"] == "r1"


def test_parse_zcode_json_missing_keys_raises():
    with pytest.raises(ZCodeExecError, match="缺少必需键"):
        _parse_zcode_json('{"foo": 1}')


def test_parse_zcode_json_empty_raises():
    with pytest.raises(ZCodeExecError, match="没有输出"):
        _parse_zcode_json("")


async def test_nonzero_exit_without_output_raises(tmp_path, fake_zcode_cli):
    """非零退出且无 stdout -> 明确报错。"""
    cli = fake_zcode_cli(raw="", exit_code=3)
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli)

    with pytest.raises(ZCodeExecError, match="退出码 3"):
        await model.ainvoke(_messages())


async def test_nonzero_exit_with_contract_block_warns_but_succeeds(
    tmp_path, fake_zcode_cli
):
    """退出码异常但契约块完整 -> 按成功处理(警告留痕)。"""
    cli = fake_zcode_cli(response="带伤返回", exit_code=1)
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli)

    msg = await model.ainvoke(_messages())
    assert msg.content == "带伤返回"
    assert msg.response_metadata["zcode_session_id"]


# --------------------------------------------------------------------------
# 状态事件通道
# --------------------------------------------------------------------------

async def test_event_stream_running_then_completed_then_final(tmp_path, fake_zcode_cli):
    """sink 收到的顺序:running -> completed -> final(没有工具事件,也不该有)。"""
    cli = fake_zcode_cli(response="最终答")
    (tmp_path / "workspace").mkdir()

    got: list = []

    async def _sink(event):
        got.append(event)

    model = _model(tmp_path, cli, event_sink_provider=lambda: _sink)
    await model.ainvoke(_messages())

    kinds = [e["kind"] for e in got]
    statuses = [e["data"].get("status") for e in got if e["kind"] == "status"]
    assert statuses[0] == "running"
    assert statuses[-1] == "completed"
    assert kinds[-1] == "final"
    assert got[-1]["data"]["text"] == "最终答"
    # 不伪造工具事件(设计 §6.3)
    assert not any(k == "tool.start" or k == "tool.end" for k in kinds)


async def test_event_failed_on_exec_error(tmp_path, fake_zcode_cli):
    """执行失败 -> failed 终态事件后抛错。"""
    cli = fake_zcode_cli(raw="", exit_code=9)
    (tmp_path / "workspace").mkdir()

    got: list = []

    async def _sink(event):
        got.append(event)

    model = _model(tmp_path, cli, event_sink_provider=lambda: _sink)
    with pytest.raises(ZCodeExecError):
        await model.ainvoke(_messages())

    statuses = [e["data"].get("status") for e in got if e["kind"] == "status"]
    assert statuses[-1] == "failed"


async def test_event_tick_during_long_run(tmp_path, fake_zcode_cli):
    """``status_tick_s > 0`` 时,长时间运行会产生心跳事件。"""
    cli = fake_zcode_cli(delay_s=2.5)
    (tmp_path / "workspace").mkdir()

    got: list = []

    async def _sink(event):
        got.append(event)

    model = _model(
        tmp_path, cli, timeout_s=30, status_tick_s=0.3,
        event_sink_provider=lambda: _sink,
    )
    await model.ainvoke(_messages())

    ticks = [e for e in got if "已运行" in str(e["data"].get("message", ""))]
    assert ticks, "长运行应产生心跳"


# --------------------------------------------------------------------------
# 超时 / 取消 / 进程树回收
# --------------------------------------------------------------------------

async def test_timeout_kills_tree(tmp_path, fake_zcode_cli):
    """超时 -> 强杀整棵进程树(含孙进程),抛明确超时错误。"""
    cli = fake_zcode_cli(delay_s=60, spawn_child=True)
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli, timeout_s=2)

    with pytest.raises(ZCodeExecError, match="超时"):
        await model.ainvoke(_messages())

    # .cmd shim 不会写 record(spawn 在 delay 之后才执行?不 —— spawn 在 delay 之前)
    # record 在 delay 之前落盘,所以能拿到 child_pid。
    assert cli.record_path.exists(), "record 应在 delay 之前落盘"
    record = read_record(cli)
    child_pid = record.get("child_pid")
    assert child_pid, "fake CLI 应已拉起孙进程"
    assert wait_until_dead(child_pid), "孙进程必须随进程树一起死"


async def test_cancel_kills_tree(tmp_path, fake_zcode_cli):
    """用户取消 -> 进程树被清理,CancelledError 向上传播。"""
    cli = fake_zcode_cli(delay_s=60, spawn_child=True)
    (tmp_path / "workspace").mkdir()
    model = _model(tmp_path, cli, timeout_s=120)

    task = asyncio.ensure_future(model.ainvoke(_messages()))
    await asyncio.sleep(1.5)  # 让子进程真的跑起来
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = read_record(cli)
    child_pid = record.get("child_pid")
    assert child_pid
    assert wait_until_dead(child_pid), "取消后孙进程必须死"


# --------------------------------------------------------------------------
# 纯函数:prompt 拼装
# --------------------------------------------------------------------------

def test_prompt_resume_mode_has_no_history_block():
    prompt = _zcode_messages_to_prompt(
        [
            SystemMessage(content="sys"),
            HumanMessage(content="第一问"),
            HumanMessage(content="第二问"),
        ],
        session_id="s1",
        max_prompt_chars=60000,
        resume_sessions=True,
    )
    assert "=== 历史对话 ===" not in prompt
    assert "=== 当前请求 ===" in prompt
    assert "第二问" in prompt
    assert "第一问" not in prompt, "resume 模式下轮次历史由 zcode 会话保存"


def test_prompt_bridge_always_inline():
    """zcode 没有 --append-system-prompt-file,桥接说明必须内联进 prompt。"""
    prompt = _zcode_messages_to_prompt(
        [HumanMessage(content="hi")],
        session_id="s1",
        max_prompt_chars=60000,
        resume_sessions=False,
        history_turns=0,
    )
    assert BRIDGE_SYSTEM_PROMPT.strip()[:20] in prompt


def test_prompt_media_manifest_included():
    prompt = _zcode_messages_to_prompt(
        [HumanMessage(content="剪一下")],
        session_id="s1",
        media_dir="E:/media/sess-1",
        media_manifest=[{"name": "a.mp4", "path": "E:/media/sess-1/a.mp4", "size": 123}],
        max_prompt_chars=60000,
        resume_sessions=True,
    )
    assert "E:/media/sess-1/a.mp4" in prompt
    assert "a.mp4" in prompt

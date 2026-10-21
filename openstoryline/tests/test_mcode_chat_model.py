"""阶段 B:``MCodeChatModel`` 的行为测试。

全部用**假 mcode CLI**(见 conftest)跑真子进程 —— 不消耗真实模型额度,
但命令行组装、stdin 传输、超时、取消、进程树回收都走真路径。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from open_storyline.mcode_chat_model import (
    BRIDGE_SYSTEM_PROMPT,
    MCodeChatModel,
    MCodeExecError,
    _messages_to_prompt,
    _parse_exec_json,
)

from conftest import read_record, wait_until_dead


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _prompt(messages, **kwargs):
    params = {
        "history_turns": 10,
        "max_prompt_chars": 60000,
        "session_id": "sess-1",
        "media_manifest": [],
    }
    params.update(kwargs)
    return _messages_to_prompt(messages, **params)


def _model(tmp_path, cli, **kwargs):
    params = {"cwd": str(tmp_path), "mcode_bin": cli.path}
    params.update(kwargs)
    return MCodeChatModel(**params)


# --------------------------------------------------------------------------
# prompt 拼装
# --------------------------------------------------------------------------

def test_prompt_keeps_system_messages_first():
    """系统消息去重后位于 prompt 最前。"""
    messages = [
        SystemMessage(content="系统A"),
        HumanMessage(content="问题1"),
        AIMessage(content="回答1"),
        SystemMessage(content="系统A"),  # 重复
        SystemMessage(content="系统B"),
        HumanMessage(content="当前问题"),
    ]
    prompt = _prompt(messages)

    assert prompt.count("系统A") == 1, "重复的系统消息必须去重"
    assert prompt.index("系统A") < prompt.index("系统B")
    assert prompt.index("系统B") < prompt.index("当前问题")
    assert prompt.startswith("===")


def test_prompt_keeps_last_user_message():
    """当前用户消息永远完整保留在末尾。"""
    current = "最终用户输入,一个字都不能少"
    messages = [
        HumanMessage(content="第一个问题"),
        AIMessage(content="第一个回答"),
        HumanMessage(content=current),
    ]
    prompt = _prompt(messages)

    assert prompt.rstrip().endswith(current)


def test_prompt_truncates_complete_turns():
    """按完整 Human/AI 轮次裁切,不是按消息条数切片。"""
    messages = [
        HumanMessage(content="第一轮用户"),
        AIMessage(content="第一轮助手"),
        HumanMessage(content="第二轮用户"),
        AIMessage(content="第二轮助手"),
        HumanMessage(content="当前问题"),
    ]

    prompt = _prompt(messages, history_turns=1)

    # 只留最近一个**完整轮次**(用户 + 助手两条都在)
    assert "第二轮用户" in prompt
    assert "第二轮助手" in prompt
    # 更早的整个轮次被丢掉
    assert "第一轮用户" not in prompt
    assert "第一轮助手" not in prompt

    # 轮次给足时都在
    prompt2 = _prompt(messages, history_turns=5)
    assert "第一轮用户" in prompt2
    assert "第一轮助手" in prompt2
    assert "第二轮助手" in prompt2


def test_prompt_handles_unpaired_messages():
    """孤立消息 / 工具消息不伪装成完整轮次。"""
    # 1) AI 前面没有 Human -> 孤立,不算轮次
    prompt = _prompt([AIMessage(content="孤立助手"), HumanMessage(content="当前问题")])
    assert "孤立助手" not in prompt
    assert "当前问题" in prompt

    # 2) Human 后面没有 AI -> 孤立,不算轮次
    prompt = _prompt([HumanMessage(content="没有回答的用户"), HumanMessage(content="当前问题")])
    assert "没有回答的用户" not in prompt
    assert "当前问题" in prompt

    # 3) Tool 消息不算轮次,也不进 prompt
    prompt = _prompt([
        HumanMessage(content="配对用户"),
        AIMessage(content="配对助手"),
        ToolMessage(content="工具结果不该出现", tool_call_id="c1"),
        HumanMessage(content="当前问题"),
    ])
    assert "配对助手" in prompt
    assert "工具结果不该出现" not in prompt


def test_prompt_rejects_oversized_current_message():
    """当前请求超限要显式失败,不静默丢字。"""
    messages = [HumanMessage(content="X" * 500)]

    with pytest.raises(MCodeExecError) as exc:
        _prompt(messages, max_prompt_chars=100)
    assert "输入过长" in str(exc.value)

    # 放宽上限后内容必须完整 —— 不会被截断
    prompt = _prompt(messages, max_prompt_chars=100000)
    assert "X" * 500 in prompt


def test_prompt_drops_oldest_turn_when_over_budget():
    """超预算时从最旧的完整轮次开始丢,当前用户消息仍然保留。"""
    messages = [
        HumanMessage(content="旧问题" + "A" * 400),
        AIMessage(content="旧回答" + "B" * 400),
        HumanMessage(content="新问题"),
        AIMessage(content="新回答"),
        HumanMessage(content="当前问题"),
    ]
    prompt = _prompt(messages, history_turns=10, max_prompt_chars=900)

    assert "当前问题" in prompt
    assert "新回答" in prompt
    assert "旧回答" + "B" * 400 not in prompt


def test_prompt_includes_media_manifest_and_session_id():
    """会话上下文里带 session_id 和服务端校验过的素材绝对路径。"""
    manifest = [{"name": "a.mp4", "path": "E:/inputs/sess-1/a.mp4", "size": 1234}]
    prompt = _prompt(
        [HumanMessage(content="做视频")],
        session_id="sess-1",
        media_manifest=manifest,
        media_dir="E:/inputs/sess-1",
    )

    assert "sess-1" in prompt
    assert "E:/inputs/sess-1/a.mp4" in prompt
    assert "a.mp4" in prompt


def test_prompt_does_not_inline_multimodal_attachments():
    """多模态附件不内联进 prompt。"""
    content = [
        {"type": "text", "text": "看这个视频"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"}},
    ]
    prompt = _prompt([HumanMessage(content=content)])

    assert "看这个视频" in prompt
    assert "base64" not in prompt


# --------------------------------------------------------------------------
# JSON 解析
# --------------------------------------------------------------------------

def test_parse_exec_json_takes_valid_result():
    """stdout 混杂多行时仍取到合法结果 JSON。"""
    stdout = "\n".join([
        "some log line",
        json.dumps({"type": "tool.event", "name": "x"}),
        json.dumps({"type": "exec.result", "status": "succeeded", "output": "最终答案"}),
    ])
    result = _parse_exec_json(stdout)

    assert result["type"] == "exec.result"
    assert result["output"] == "最终答案"


def test_parse_exec_json_takes_whole_output():
    """整段就是一个 JSON 对象时直接取它。"""
    result = _parse_exec_json(json.dumps({"type": "exec.result", "status": "succeeded", "output": "OK"}))

    assert result["output"] == "OK"


def test_parse_exec_json_rejects_garbage():
    """非 JSON 输出 -> MCodeExecError。"""
    with pytest.raises(MCodeExecError) as exc:
        _parse_exec_json("这不是 JSON,只是一段乱码")
    assert "JSON" in str(exc.value)


def test_parse_exec_json_rejects_empty_output():
    with pytest.raises(MCodeExecError):
        _parse_exec_json("")


# --------------------------------------------------------------------------
# 状态映射
# --------------------------------------------------------------------------

def test_model_raises_on_non_succeeded_status(tmp_path, fake_mcode_cli):
    """非 succeeded 抛异常并保留 status。"""
    cli = fake_mcode_cli(result={
        "type": "exec.result",
        "status": "failed",
        "error": "工具调用炸了",
    })
    model = _model(tmp_path, cli)

    with pytest.raises(MCodeExecError) as exc:
        model.invoke([HumanMessage(content="hi")])

    message = str(exc.value)
    assert "failed" in message
    assert "工具调用炸了" in message


def test_model_raises_on_nonzero_exit_code(tmp_path, fake_mcode_cli):
    """退出码非 0 且没有输出 -> 明确的失败。"""
    cli = fake_mcode_cli(raw="", exit_code=3)
    model = _model(tmp_path, cli)

    with pytest.raises(MCodeExecError) as exc:
        model.invoke([HumanMessage(content="hi")])

    assert "3" in str(exc.value)


# --------------------------------------------------------------------------
# 可执行文件解析
# --------------------------------------------------------------------------

def test_resolve_agent_bin_uses_project_resolver(tmp_path, monkeypatch):
    """走 ``agent_gate.agent_bin.resolve_agent_bin``(monkeypatch 断言调用)。"""
    import agent_gate.agent_bin as agent_bin

    fake_bin = tmp_path / "mcode.cmd"
    fake_bin.write_text("@echo off\r\n", encoding="ascii")

    calls = []

    def fake_resolve(name):
        calls.append(name)
        return str(fake_bin)

    monkeypatch.setattr(agent_bin, "resolve_agent_bin", fake_resolve)

    model = MCodeChatModel(cwd=str(tmp_path))
    resolved = model._resolve_executable()

    assert resolved == str(fake_bin)
    assert calls == ["mcode"]


def test_explicit_invalid_bin_fails(tmp_path, monkeypatch):
    """显式错误路径不静默回退到 PATH 里的另一个程序。"""
    import agent_gate.agent_bin as agent_bin

    def boom(name):  # pragma: no cover - 不该被调用
        raise AssertionError("显式指定了 bin,不该再走 PATH 解析")

    monkeypatch.setattr(agent_bin, "resolve_agent_bin", boom)

    missing = tmp_path / "不存在的.cmd"
    model = MCodeChatModel(cwd=str(tmp_path), mcode_bin=str(missing))

    with pytest.raises(MCodeExecError) as exc:
        model._resolve_executable()

    assert "不存在的.cmd" in str(exc.value)


def test_unresolvable_mcode_bin_raises(tmp_path, monkeypatch):
    """解析不到 mcode 就报错,不静默回退到别的模型。"""
    import agent_gate.agent_bin as agent_bin

    monkeypatch.setattr(agent_bin, "resolve_agent_bin", lambda name: "mcode")

    model = MCodeChatModel(cwd=str(tmp_path))

    with pytest.raises(MCodeExecError) as exc:
        model._resolve_executable()

    assert "找不到 mcode" in str(exc.value)


# --------------------------------------------------------------------------
# 命令组装与执行
# --------------------------------------------------------------------------

def test_command_uses_stdin_and_mode_flag(tmp_path, fake_mcode_cli):
    """prompt 走 stdin;命令含 --mode / --append-system-prompt-file。"""
    cli = fake_mcode_cli()
    model = _model(tmp_path, cli, mode="lightweight", session_id="sess-9")

    model.invoke([HumanMessage(content="请处理这个任务")])

    record = read_record(cli)
    argv = record["argv"]

    assert argv[0] == "exec"
    assert argv[argv.index("--input") + 1] == "-", "prompt 必须走 stdin"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission") + 1] == "smart"
    assert argv[argv.index("--max-steps") + 1] == "120"
    assert argv[argv.index("--timeout") + 1] == "900s"
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[argv.index("--mode") + 1] == "lightweight"
    assert "--append-system-prompt-file" in argv

    # prompt 内容只在 stdin 里,不在命令行里
    assert "请处理这个任务" in record["stdin"]
    assert "请处理这个任务" not in " ".join(argv)

    # 桥接提示词通过 --append-system-prompt-file 传
    assert record["bridge"] == BRIDGE_SYSTEM_PROMPT


def test_sync_generate_returns_ai_message(tmp_path, fake_mcode_cli):
    """同步路径返回规范化的 AIMessage。"""
    cli = fake_mcode_cli(result={
        "type": "exec.result",
        "status": "succeeded",
        "output": "好的",
        "durationMs": 1234,
        "usage": {"inputTokens": 10, "outputTokens": 7, "totalTokens": 17},
    })
    model = _model(tmp_path, cli, session_id="sess-1")

    message = model.invoke([HumanMessage(content="hi")])

    assert isinstance(message, AIMessage)
    assert message.content == "好的"
    assert message.response_metadata["mcode_status"] == "succeeded"
    assert message.response_metadata["mcode_duration_ms"] == 1234
    assert message.usage_metadata["output_tokens"] == 7
    assert message.usage_metadata["total_tokens"] == 17


async def test_async_generate_returns_ai_message(tmp_path, fake_mcode_cli):
    """异步路径(``ainvoke``)返回同样的结果。"""
    cli = fake_mcode_cli(result={
        "type": "exec.result",
        "status": "succeeded",
        "output": "异步回答",
    })
    model = _model(tmp_path, cli)

    message = await model.ainvoke([HumanMessage(content="hi")])

    assert isinstance(message, AIMessage)
    assert message.content == "异步回答"


def test_bind_tools_returns_self(tmp_path):
    """bind_tools 不注册任何 LangChain 工具,只满足接口兼容。"""
    model = MCodeChatModel(cwd=str(tmp_path))

    assert model.bind_tools([]) is model
    assert model.bind_tools([], tool_choice="auto", strict=True) is model


# --------------------------------------------------------------------------
# lightweight 模式会忽略 --append-system-prompt-file(本机实测)
# --------------------------------------------------------------------------

def test_lightweight_mode_embeds_bridge_instructions_in_prompt(tmp_path, fake_mcode_cli):
    """lightweight 忽略系统提示文件 -> 桥接说明必须拼进 user prompt。"""
    cli = fake_mcode_cli()
    model = _model(tmp_path, cli, mode="lightweight")

    model.invoke([HumanMessage(content="干活")])

    record = read_record(cli)
    assert "操作说明" in record["stdin"]
    assert "OpenStoryline" in record["stdin"]
    # 桥接说明也照样走系统提示文件(两种通道都发,不冲突)
    assert record["bridge"] == BRIDGE_SYSTEM_PROMPT


def test_standard_mode_does_not_duplicate_bridge_instructions(tmp_path, fake_mcode_cli):
    """standard 采纳系统提示文件 -> 桥接说明不重复拼进 prompt。"""
    cli = fake_mcode_cli()
    model = _model(tmp_path, cli, mode="standard")

    model.invoke([HumanMessage(content="干活")])

    record = read_record(cli)
    assert "操作说明" not in record["stdin"]
    assert record["bridge"] == BRIDGE_SYSTEM_PROMPT


def test_prompt_bridge_instructions_go_first():
    """``bridge_instructions`` 排在 prompt 最前。"""
    prompt = _prompt(
        [HumanMessage(content="干活")],
        bridge_instructions="规则一\n规则二",
    )
    assert prompt.index("规则一") < prompt.index("=== 会话上下文 ===")
    assert prompt.index("=== 会话上下文 ===") < prompt.index("干活")


# --------------------------------------------------------------------------
# 超时 / 取消 / 进程树回收
# --------------------------------------------------------------------------

def test_timeout_kills_process_tree(tmp_path, fake_mcode_cli):
    """超时后主进程与子进程都得退出,不能留孤儿。"""
    cli = fake_mcode_cli(delay_s=60, spawn_child=True)
    model = _model(tmp_path, cli, timeout_s=1)

    with pytest.raises(MCodeExecError) as exc:
        model.invoke([HumanMessage(content="hi")])

    assert "超时" in str(exc.value)

    record = read_record(cli)
    assert wait_until_dead(record["pid"]), "mcode 主进程没被清理"
    assert wait_until_dead(record["child_pid"]), "mcode 的子进程没被清理"


async def test_cancel_kills_process_tree(tmp_path, fake_mcode_cli):
    """async task 被取消后清理进程树,无孤儿进程。"""
    cli = fake_mcode_cli(delay_s=60, spawn_child=True)
    model = _model(tmp_path, cli, timeout_s=120)

    task = asyncio.ensure_future(model.ainvoke([HumanMessage(content="hi")]))
    # 等假 CLI 真的起来
    for _ in range(100):
        if cli.record_path.is_file():
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = read_record(cli)
    assert wait_until_dead(record["pid"]), "取消后 mcode 主进程还活着"
    assert wait_until_dead(record["child_pid"]), "取消后 mcode 的子进程还活着"

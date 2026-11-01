"""阶段 C:``MCodeChatModel`` 的流式化行为测试。

覆盖设计文档 §10.1 的:

* **UT-03** agent_message 多次增量 —— 按顺序产出,不等待 CLI 退出;
* **UT-04** 最终结果与增量并存 —— 不重复、不丢字;
* **UT-05** reasoning 事件 —— 原始内容不进入任何 chunk;
* 以及阶段 C 的三个专项:能力探测降级、sink provider 换 sink、零回归。

全部用**真子进程**(假 mcode CLI)跑 —— 进程时序、编码、退出码都走真路径。
"""
from __future__ import annotations

import json
import time

import pytest
from langchain_core.messages import HumanMessage

from open_storyline.mcode_chat_model import (
    TRANSPORT_AUTO,
    TRANSPORT_FINAL_JSON,
    TRANSPORT_STREAM_JSON,
    MCodeChatModel,
)


def _model(tmp_path, cli, **kwargs):
    params = {"cwd": str(tmp_path), "mcode_bin": cli.path}
    params.update(kwargs)
    return MCodeChatModel(**params)


async def _collect(model, text="干活"):
    """跑一次流式,返回 ``(文本列表, 首个 chunk 的到达时刻, 总耗时)``。"""
    texts = []
    started = time.monotonic()
    first_at = None
    async for chunk in model.astream([HumanMessage(content=text)]):
        if first_at is None:
            first_at = time.monotonic() - started
        content = getattr(chunk, "content", "")
        if isinstance(content, str) and content:
            texts.append(content)
    return texts, first_at, time.monotonic() - started


# --------------------------------------------------------------------------
# UT-03 增量按顺序产出
# --------------------------------------------------------------------------

async def test_astream_yields_deltas_in_order(tmp_path, fake_mcode_stream_cli):
    """UT-03:多次增量按顺序产出,内容一字不差。"""
    cli = fake_mcode_stream_cli(deltas=["甲", "乙", "丙"])
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    texts, _first_at, _elapsed = await _collect(model)

    assert texts == ["甲", "乙", "丙"]


async def test_first_delta_arrives_before_process_exit(tmp_path, fake_mcode_stream_cli):
    """首段文本必须在 CLI 进程退出**之前**到达(不是攒完一次性吐)。"""
    cli = fake_mcode_stream_cli(deltas=["第一段", "第二段"], hold_s=2.0)
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    texts, first_at, elapsed = await _collect(model)

    assert first_at is not None, "一个 chunk 都没产出"
    assert first_at < 1.5, f"首段等了 {first_at:.2f}s,像是等进程退出才给的"
    assert elapsed >= 2.0, "CLI 进程本身应该跑了 2 秒"
    assert texts == ["第一段", "第二段"]


async def test_streaming_command_uses_stream_json_format(tmp_path, fake_mcode_stream_cli):
    """流式路径必须用 ``--output-format stream-json``。"""
    cli = fake_mcode_stream_cli(deltas=["甲"])
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    await _collect(model)

    record = json.loads(cli.record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"


# --------------------------------------------------------------------------
# UT-04 最终结果与增量并存
# --------------------------------------------------------------------------

async def test_final_output_equal_to_deltas_is_not_duplicated(tmp_path, fake_mcode_stream_cli):
    """UT-04:最终正文 == 增量拼接时,不能把正文再发一遍。"""
    cli = fake_mcode_stream_cli(deltas=["甲", "乙"], final_output="甲乙")
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    texts, _f, _e = await _collect(model)

    assert texts == ["甲", "乙"]
    assert "".join(texts) == "甲乙"


async def test_missing_tail_is_appended_once(tmp_path, fake_mcode_stream_cli):
    """UT-04:增量漏了尾段时要补齐(不丢字),且不重复已发过的部分。"""
    cli = fake_mcode_stream_cli(deltas=["甲"], final_output="甲乙丙")
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    texts, _f, _e = await _collect(model)

    assert texts == ["甲", "乙丙"]
    assert "".join(texts) == "甲乙丙"


async def test_extra_process_text_is_not_duplicated(tmp_path, fake_mcode_stream_cli):
    """UT-04:一轮里有多条 agent_message 时,增量是「过程说明 + 最终答复」。

    最终正文只等于最后一条 —— 此时**不能**再把最终答复发一遍,
    以最终正文为准,差异只记诊断。
    """
    cli = fake_mcode_stream_cli(deltas=["我先看看。", "最终答复"], final_output="最终答复")
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    texts, _f, _e = await _collect(model)

    assert texts == ["我先看看。", "最终答复"]
    assert texts.count("最终答复") == 1


# --------------------------------------------------------------------------
# UT-05 reasoning 不外泄
# --------------------------------------------------------------------------

async def test_reasoning_never_becomes_a_chunk(tmp_path, fake_mcode_stream_cli):
    """隐藏思维链原文不能出现在任何 chunk 里。"""
    cli = fake_mcode_stream_cli(
        deltas=["正文"],
        include_reasoning=True,
        reasoning_text="绝密思维链XYZ",
    )
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_STREAM_JSON)

    texts, _f, _e = await _collect(model)

    assert texts == ["正文"]
    assert "绝密思维链" not in "".join(texts)


async def test_reasoning_is_not_forwarded_to_event_sink(tmp_path, fake_mcode_stream_cli):
    """事件通道里也不能出现 reasoning 原文。"""
    cli = fake_mcode_stream_cli(
        deltas=["正文"],
        include_reasoning=True,
        reasoning_text="绝密思维链XYZ",
    )
    seen = []

    async def sink(event):
        seen.append(event)

    model = _model(
        tmp_path, cli,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink,
    )

    await _collect(model)

    assert seen, "sink 一个事件都没收到"
    blob = json.dumps(
        [getattr(e, "raw", {}) for e in seen], ensure_ascii=False, default=str
    )
    assert "绝密思维链" not in blob


# --------------------------------------------------------------------------
# sink provider
# --------------------------------------------------------------------------

async def test_event_sink_receives_cli_events(tmp_path, fake_mcode_stream_cli):
    """sink provider 返回的回调能收到本轮的真实 CLI 事件。"""
    cli = fake_mcode_stream_cli(deltas=["甲", "乙"])
    seen = []

    async def sink(event):
        seen.append(event)

    model = _model(
        tmp_path, cli,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink,
    )

    await _collect(model)

    types = [e.type for e in seen]
    assert "exec.started" in types
    assert "exec.completed" in types
    # 文本增量也走同一条通道
    assert "".join(e.text_delta for e in seen) == "甲乙"


async def test_event_sink_provider_is_called_per_stream(tmp_path, fake_mcode_stream_cli):
    """provider 每轮调用一次 —— agent 按 session 缓存,每轮 sink 都不同。"""
    cli = fake_mcode_stream_cli(deltas=["甲"])
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return None

    model = _model(
        tmp_path, cli,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=provider,
    )

    await _collect(model)
    await _collect(model)

    assert calls["n"] == 2


async def test_broken_event_sink_does_not_break_the_call(tmp_path, fake_mcode_stream_cli):
    """sink 抛错不能拖垮模型调用本身。"""
    cli = fake_mcode_stream_cli(deltas=["甲"])

    async def sink(event):
        raise RuntimeError("sink 坏了")

    model = _model(
        tmp_path, cli,
        streaming_transport=TRANSPORT_STREAM_JSON,
        event_sink_provider=lambda: sink,
    )

    texts, _f, _e = await _collect(model)
    assert texts == ["甲"]


# --------------------------------------------------------------------------
# 能力探测与降级
# --------------------------------------------------------------------------

async def test_auto_transport_degrades_when_cli_lacks_stream_json(tmp_path, fake_mcode_stream_cli):
    """CLI 不支持 stream-json 时降级成整段 JSON,并留下降级标记。"""
    cli = fake_mcode_stream_cli(deltas=["甲"], supports_stream_json=False)
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_AUTO)

    texts, _f, _e = await _collect(model)

    assert model.transport_degraded is True
    assert texts == ["甲"], "降级后应该一次性给出最终正文"

    record = json.loads(cli.record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--output-format") + 1] == "json"


async def test_auto_transport_uses_stream_json_when_supported(tmp_path, fake_mcode_stream_cli):
    """CLI 支持 stream-json 时走实时事件流。"""
    cli = fake_mcode_stream_cli(deltas=["甲", "乙"])
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_AUTO)

    texts, _f, _e = await _collect(model)

    assert model.transport_degraded is False
    assert texts == ["甲", "乙"]


async def test_final_json_transport_skips_probe(tmp_path, fake_mcode_stream_cli):
    """显式 ``final_json`` 不探测,直接走旧路径(回滚开关)。"""
    cli = fake_mcode_stream_cli(deltas=["甲"], supports_stream_json=True)
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_FINAL_JSON)

    texts, _f, _e = await _collect(model)

    assert model.transport_degraded is False
    assert texts == ["甲"]

    record = json.loads(cli.record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--output-format") + 1] == "json"


# --------------------------------------------------------------------------
# 零回归:旧路径不受影响
# --------------------------------------------------------------------------

async def test_legacy_ainvoke_still_uses_json_format(tmp_path, fake_mcode_stream_cli):
    """``ainvoke``(没有流式 handler 时)仍走旧的整段 JSON 路径。"""
    cli = fake_mcode_stream_cli(deltas=["甲"], final_output="甲")
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_AUTO)

    message = await model.ainvoke([HumanMessage(content="hi")])

    assert message.content == "甲"

    record = json.loads(cli.record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--output-format") + 1] == "json"


async def test_legacy_invoke_still_uses_json_format(tmp_path, fake_mcode_stream_cli):
    """同步 ``invoke`` 完全不受流式改造影响。"""
    cli = fake_mcode_stream_cli(deltas=["甲"], final_output="甲")
    model = _model(tmp_path, cli, streaming_transport=TRANSPORT_AUTO)

    message = await model.ainvoke([HumanMessage(content="hi")])

    assert message.content == "甲"


@pytest.mark.parametrize("transport", [TRANSPORT_AUTO, TRANSPORT_STREAM_JSON, TRANSPORT_FINAL_JSON])
async def test_prompt_still_goes_through_stdin(tmp_path, fake_mcode_stream_cli, transport):
    """不管哪种传输方式,prompt 都走 stdin(不撞命令行长度上限)。"""
    cli = fake_mcode_stream_cli(deltas=["甲"])
    model = _model(tmp_path, cli, streaming_transport=transport)

    await _collect(model, text="请处理这个任务")

    record = json.loads(cli.record_path.read_text(encoding="utf-8"))
    assert "请处理这个任务" in record["stdin"]
    assert "请处理这个任务" not in " ".join(record["argv"])

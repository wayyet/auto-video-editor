"""阶段 E:ZCode Agent 的 live 冒烟(需真实 zcode CLI + 本机登录态)。

与 ``test_glm_live.py`` 同款门控:**不设环境变量就整组跳过**,不消耗额度。

运行::

    OPENSTORYLINE_ZCODE_LIVE=1 .venv-openstoryline/Scripts/python.exe -m pytest tests_openstoryline/test_zcode_live.py -q

(值任意非空即可。注意门控变量是 _LIVE 不是 _BIN —— 后者是真实的 bin 覆盖配置,
拿来当门控会把 "1" 当成可执行文件路径。CLI 从 PATH / [zcode].bin 解析。)

每次运行消耗 2 次小模型调用(附录 A.5 冒烟同款)。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from open_storyline.zcode_agent_adapter import (
    ZCodeChatModel,
    resolve_zcode_bin,
    zcode_selfcheck,
)
from open_storyline.config import ZCodeConfig, load_settings

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENSTORYLINE_ZCODE_LIVE"),
    reason="需要 OPENSTORYLINE_ZCODE_LIVE=1 显式开启(会消耗真实模型额度)",
)


def _cfg() -> ZCodeConfig:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    return cfg.zcode


def test_live_selfcheck_and_one_turn(tmp_path):
    """自检通过 + 一次真实无头调用(最小 prompt,控额度)。"""
    zcfg = _cfg()
    bin_path = resolve_zcode_bin(zcfg.bin or "")
    assert bin_path, "解析不到 zcode 可执行文件(装 zcode CLI 或设 [zcode].bin)"
    ok, reason = zcode_selfcheck(bin_path)
    assert ok, f"启动自检未通过: {reason}"

    ws = tmp_path / "ws"
    ws.mkdir()
    model = ZCodeChatModel(
        cwd=str(ws),
        zcode_bin=bin_path,
        mode="plan",  # 只读,最小副作用
        timeout_s=120,
        max_prompt_chars=zcfg.max_prompt_chars,
    )
    from langchain_core.messages import HumanMessage

    msg = model.invoke([HumanMessage(content="只回复两个字:正常")])
    assert isinstance(msg.content, str) and msg.content.strip()
    assert msg.response_metadata.get("zcode_session_id"), "结果块必须回传 sessionId"


async def test_live_resume_context(tmp_path):
    """--resume 续接:第二轮模型应凭第一轮上下文作答(A.5 冒烟同款)。"""
    zcfg = _cfg()
    bin_path = resolve_zcode_bin(zcfg.bin or "")
    ws = tmp_path / "ws"
    ws.mkdir()
    base = dict(
        cwd=str(ws),
        zcode_bin=bin_path,
        mode="plan",
        timeout_s=120,
        max_prompt_chars=zcfg.max_prompt_chars,
    )
    from langchain_core.messages import HumanMessage

    first = ZCodeChatModel(**base)
    m1 = await first.ainvoke([HumanMessage(content="记住这个暗号:柠檬。只回复:收到")])
    sid = str(m1.response_metadata.get("zcode_session_id") or "")
    assert sid, "首轮必须拿到 sessionId"

    second = ZCodeChatModel(**base, resume_id_provider=lambda: sid)
    m2 = await second.ainvoke([HumanMessage(content="我刚才说的暗号是什么?只回复暗号两个字")])
    assert "柠檬" in str(m2.content)

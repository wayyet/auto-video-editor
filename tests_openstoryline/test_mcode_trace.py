"""阶段 E:有界执行过程 trace(设计文档 §9 T7)。

要钉死的六条:

1. trace **不进** ``sess.history`` —— history 只装「用户看到的对话」;
2. 每轮条数有上限(``max_trace_events``,默认 300);
3. 单条摘要长度有上限(``max_event_text_chars``,默认 2000);
4. 写入前脱敏(密钥不能进 trace);
5. 字段白名单 —— prompt / 环境变量之类的东西不会顺手被存进去;
6. **写失败不能把成功的 turn 变成失败**。

外加:trace 能随会话状态落库 / 恢复(刷新后重建执行过程面板)。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

import agent_fastapi as web
from open_storyline.config import ProjectConfig, Settings, load_settings

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent


@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    monkeypatch.chdir(OPENSTORYLINE_DIR)


def _settings(tmp_path: Path) -> Settings:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    return cfg.model_copy(update={"project": project})


def _session(tmp_path: Path) -> web.ChatSession:
    return web.ChatSession(session_id="sess-trace", cfg=_settings(tmp_path))


def _row(**kw: Any) -> Dict[str, Any]:
    base = {
        "type": "agent.status",
        "source": "mcode",
        "session_id": "sess-trace",
        "turn_id": "turn-1",
        "run_id": "run-1",
        "seq": 1,
        "timestamp_ms": 1700000000000,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# 1) trace 不进 history
# --------------------------------------------------------------------------

def test_trace_does_not_land_in_history(tmp_path):
    """过程事件写 trace,**不**写 history。"""
    sess = _session(tmp_path)
    sess.history.append({"id": "u1", "role": "user", "content": "干活", "ts": time.time()})

    assert sess.record_mcode_trace(_row(message="正在分析任务")) is True

    assert len(sess.mcode_trace_snapshot()) == 1
    assert len(sess.history) == 1, "trace 不许写进 history"
    assert sess.history[0]["role"] == "user"


# --------------------------------------------------------------------------
# 2) 每轮条数有上限
# --------------------------------------------------------------------------

def test_trace_is_capped_per_turn(tmp_path):
    """一轮最多 max_trace_events 条 —— 高频输出不能把会话撑爆。"""
    sess = _session(tmp_path)
    for i in range(500):
        sess.record_mcode_trace(_row(seq=i + 1, message="x"))

    trace = sess.mcode_trace_snapshot()
    assert len(trace) == web.MCODE_TRACE_MAX_EVENTS


def test_trace_cap_follows_config(tmp_path):
    """上限来自 ``[mcode] max_trace_events``,不是写死的常量。"""
    cfg = _settings(tmp_path)
    cfg = cfg.model_copy(update={"mcode": cfg.mcode.model_copy(update={"max_trace_events": 7})})
    sess = web.ChatSession(session_id="sess-trace", cfg=cfg)

    for i in range(50):
        sess.record_mcode_trace(_row(seq=i + 1, message="x"))

    assert len(sess.mcode_trace_snapshot()) == 7


def test_trace_cap_is_per_turn(tmp_path):
    """换个 turn_id 就是新一轮 —— 上限按轮算,不是全局。"""
    sess = _session(tmp_path)
    for i in range(5):
        sess.record_mcode_trace(_row(turn_id="turn-1", seq=i + 1))
    for i in range(5):
        sess.record_mcode_trace(_row(turn_id="turn-2", seq=i + 1))

    assert len(sess.mcode_trace_snapshot()) == 10


# --------------------------------------------------------------------------
# 3) 单条摘要长度有上限
# --------------------------------------------------------------------------

def test_trace_entry_text_is_truncated(tmp_path):
    """单条摘要最多 max_event_text_chars 字符。"""
    sess = _session(tmp_path)
    long_text = "很长的摘要" * 2000
    sess.record_mcode_trace(_row(message=long_text, summary=long_text))

    rec = sess.mcode_trace_snapshot()[0]
    assert len(rec["message"]) < len(long_text)
    assert "截断" in rec["message"]
    assert len(rec["summary"]) < len(long_text)


def test_trace_truncation_limit_follows_config(tmp_path):
    """长度上限来自 ``[mcode] max_event_text_chars``。"""
    cfg = _settings(tmp_path)
    cfg = cfg.model_copy(update={"mcode": cfg.mcode.model_copy(update={"max_event_text_chars": 50})})
    sess = web.ChatSession(session_id="sess-trace", cfg=cfg)

    sess.record_mcode_trace(_row(message="字" * 500))

    rec = sess.mcode_trace_snapshot()[0]
    # 正文被截到 50 字,后面跟一小段「已截断」提示。
    assert rec["message"].startswith("字" * 50)
    assert "截断" in rec["message"]
    assert len(rec["message"]) <= 50 + 32


# --------------------------------------------------------------------------
# 4) 写入前脱敏
# --------------------------------------------------------------------------

def test_trace_masks_secrets_before_writing(tmp_path):
    """密钥不能进 trace。"""
    sess = _session(tmp_path)
    sess.record_mcode_trace(_row(
        message="调用失败: api_key=sk-abcdefghijklmnopqrstuvwxyz012345",
        summary="token: sk-abcdefghijklmnopqrstuvwxyz012345",
    ))

    blob = json.dumps(sess.mcode_trace_snapshot(), ensure_ascii=False)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in blob
    assert web.MASKED_SECRET in blob


# --------------------------------------------------------------------------
# 5) 字段白名单
# --------------------------------------------------------------------------

def test_trace_only_keeps_whitelisted_fields(tmp_path):
    """prompt / 环境变量之类的东西不会顺手被存进去。"""
    sess = _session(tmp_path)
    sess.record_mcode_trace(_row(
        message="ok",
        prompt="用户的完整 prompt 原文",
        env={"API_KEY": "sk-should-not-be-stored"},
        stderr="未清理的 stderr",
    ))

    rec = sess.mcode_trace_snapshot()[0]
    for banned in ("prompt", "env", "stderr"):
        assert banned not in rec
    blob = json.dumps(sess.mcode_trace_snapshot(), ensure_ascii=False)
    assert "用户的完整 prompt 原文" not in blob
    assert "sk-should-not-be-stored" not in blob


# --------------------------------------------------------------------------
# 6) 写失败不能把成功的 turn 变成失败
# --------------------------------------------------------------------------

def test_trace_write_failure_is_isolated(tmp_path, monkeypatch):
    """trace 写挂了只记日志,绝不能把异常抛给主链路。"""
    sess = _session(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("trace 存储挂了")

    monkeypatch.setattr(sess, "_build_trace_record", _boom)

    assert sess.record_mcode_trace(_row(message="x")) is False
    # 没抛异常 —— 调用方(已成功的 turn)不受影响。


def test_trace_write_failure_does_not_break_dispatch(tmp_path, monkeypatch):
    """真实链路:trace 写失败,WebSocket 事件照发。

    这里模拟的是**真实**的失败点(记录构造阶段炸了),而不是把
    ``record_mcode_trace`` 整个换掉 —— 后者绕过了它内部的兜底,测不到东西。
    """
    import asyncio

    sess = _session(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("trace 存储挂了")

    monkeypatch.setattr(sess, "_build_trace_record", _boom)

    from open_storyline.mcode_event_adapter import KIND_STATUS, MCodeInternalEvent

    emitted: List[Any] = []

    async def emit(type_: str, data: Any = None) -> bool:
        emitted.append((type_, data))
        return True

    async def flush_segment(send_flush_event: bool = True) -> None:
        return None

    internal = MCodeInternalEvent(
        kind=KIND_STATUS,
        data={"status": "running", "phase": "analyzing", "message": "正在分析任务",
              "source": "mcode", "run_id": "run-1", "seq": 1},
    )

    asyncio.run(web._dispatch_mcode_internal(
        internal,
        sess=sess,
        turn_id="turn-1",
        trace_enabled=True,
        emit=emit,
        flush_segment=flush_segment,
        append_text=lambda _d: None,
    ))

    assert [t for t, _ in emitted] == ["agent.status"], "状态事件必须照发"
    assert sess.mcode_trace_snapshot() == []


# --------------------------------------------------------------------------
# 落库 / 恢复(刷新后重建面板)
# --------------------------------------------------------------------------

def test_trace_is_persisted_and_restored(tmp_path):
    """trace 跟着会话状态落库,恢复后还在(刷新不丢执行过程)。"""
    sess = _session(tmp_path)
    sess.record_mcode_trace(_row(message="正在分析任务"))
    sess.record_mcode_trace(_row(type="tool.end", seq=2, tool_call_id="call_1", summary="ok"))

    state = sess.dump_state()
    assert "mcode_trace" in state
    assert len(state["mcode_trace"]) == 2

    # 快照也给前端一份
    snap = sess.snapshot()
    assert len(snap["mcode_trace"]) == 2

    # 恢复
    sess2 = _session(tmp_path)
    sess2.restore_mcode_trace(state["mcode_trace"])
    assert len(sess2.mcode_trace_snapshot()) == 2
    assert sess2.mcode_trace_snapshot()[0]["message"] == "正在分析任务"


def test_restore_reapplies_bounds(tmp_path):
    """恢复时也要重新套上限 —— 库里存了超量数据也不能全放出来。"""
    sess = _session(tmp_path)
    raw = [_row(seq=i + 1, message="x") for i in range(1000)]
    sess.restore_mcode_trace(raw)

    assert len(sess.mcode_trace_snapshot()) == web.MCODE_TRACE_MAX_EVENTS


def test_trace_keeps_only_recent_turns(tmp_path):
    """跨轮只留最近几轮 —— 会话状态不能无限长。"""
    sess = _session(tmp_path)
    for t in range(web.MCODE_TRACE_MAX_TURNS + 3):
        sess.record_mcode_trace(_row(turn_id=f"turn-{t}", seq=1, message=f"第 {t} 轮"))

    trace = sess.mcode_trace_snapshot()
    turn_ids = {rec["turn_id"] for rec in trace}
    assert len(turn_ids) <= web.MCODE_TRACE_MAX_TURNS
    assert "turn-0" not in turn_ids, "最老的轮次应该被丢掉"


def test_restore_ignores_garbage(tmp_path):
    """库里存了脏数据也不能把恢复搞崩。"""
    sess = _session(tmp_path)
    sess.restore_mcode_trace(["不是字典", 123, None, {"type": "agent.status"}])
    assert isinstance(sess.mcode_trace_snapshot(), list)

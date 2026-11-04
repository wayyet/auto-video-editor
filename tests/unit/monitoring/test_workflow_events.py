"""事件协议、脱敏与事件存储单测(设计《工作流流程图与实时节点追踪》§9.5 阶段2)。

覆盖面(§9.5 任务 4):

- 事件类型常量与分区(§7.1 全部 13 类);
- ``make_event`` 字段校验(node.* 必带 node_id / run.* 禁带 / attempt / 未知类型);
- attempt 递增语义(重试序列的事件区分);
- 脱敏:异常里夹路径/密钥(§9.5 明确要求的用例)、ASR 全文截断、
  白名单拒绝式校验、嵌套敏感键丢弃;
- ``to_sse_frame`` 帧格式(有 id / 无 id 两态,payload 平铺进 data);
- 事件存储:写入/读取/快照的成功路径(fake 连接),连接失败时的显式降级
  (返回 None/[]/available=False + ERROR 日志 + 降级计数)。

本文件不连真实 Postgres(那在验收命令 ``apply_migrations_to_db()`` 与
后续集成测试里)。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager

import pytest

from monitoring import workflow_event_store as store
from monitoring import workflow_events as ev
from monitoring.workflow_events import (
    ALL_EVENT_TYPES,
    BRANCH_EVENT_TYPES,
    NODE_EVENT_TYPES,
    RUN_EVENT_TYPES,
    make_event,
    sanitize_exception,
    sanitize_payload,
    sanitize_text,
    to_sse_frame,
    validate_event,
)


# ===========================================================================
# 事件类型常量(§7.1)
# ===========================================================================
def test_event_type_constants_complete():
    expected = {
        "run.started", "node.started", "node.progress", "node.succeeded",
        "node.waiting_input", "node.resumed", "node.failed", "node.retrying",
        "branch.selected", "run.interrupted", "run.resumed", "run.completed",
        "run.failed",
    }
    assert ALL_EVENT_TYPES == expected
    assert NODE_EVENT_TYPES | RUN_EVENT_TYPES | BRANCH_EVENT_TYPES == ALL_EVENT_TYPES
    # 分区互斥
    assert not (NODE_EVENT_TYPES & RUN_EVENT_TYPES)
    assert not (NODE_EVENT_TYPES & BRANCH_EVENT_TYPES)
    assert not (RUN_EVENT_TYPES & BRANCH_EVENT_TYPES)
    assert len(ALL_EVENT_TYPES) == 13


# ===========================================================================
# make_event 字段校验
# ===========================================================================
def test_make_event_defaults_and_shape():
    e = make_event(
        ev.EVENT_NODE_STARTED,
        thread_id="video-001",
        node_id="assembly_build_timeline",
        payload={"duration_ms": 12},
    )
    assert e["event_type"] == "node.started"
    assert e["thread_id"] == "video-001"
    assert e["node_id"] == "assembly_build_timeline"
    assert e["attempt"] == 1
    assert e["run_id"] is None
    assert e["occurred_at"].endswith("Z")
    assert "T" in e["occurred_at"]
    assert e["payload"] == {"duration_ms": 12}


def test_make_event_node_events_require_node_id():
    with pytest.raises(ValueError, match="node_id"):
        make_event("node.started", thread_id="t")


def test_make_event_run_events_reject_node_id():
    with pytest.raises(ValueError, match="运行级事件"):
        make_event("run.started", thread_id="t", node_id="x")


def test_make_event_branch_allows_optional_node_id():
    e = make_event("branch.selected", thread_id="t", payload={"label": "QA 通过"})
    assert e["node_id"] is None
    e2 = make_event("branch.selected", thread_id="t", node_id="storyline_qa_gate")
    assert e2["node_id"] == "storyline_qa_gate"


def test_make_event_rejects_unknown_type():
    with pytest.raises(ValueError, match="未知事件类型"):
        make_event("node.exploded", thread_id="t", node_id="x")


def test_make_event_rejects_empty_thread_id():
    with pytest.raises(ValueError, match="thread_id"):
        make_event("run.started", thread_id="  ")


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "2"])
def test_make_event_rejects_bad_attempt(bad):
    with pytest.raises(ValueError, match="attempt"):
        make_event("node.started", thread_id="t", node_id="x", attempt=bad)


def test_make_event_rejects_non_dict_payload():
    with pytest.raises(ValueError, match="payload"):
        make_event("run.started", thread_id="t", payload="not-a-dict")  # type: ignore[arg-type]


def test_attempt_increments_across_retry_sequence():
    """重试:同一 node_id 再次启动时 attempt 递增,事件互相可区分(§7.3 规则 5)。"""
    first = make_event("node.started", thread_id="t", node_id="node_07_speed_fit", attempt=1)
    retry = make_event("node.retrying", thread_id="t", node_id="node_07_speed_fit", attempt=2)
    second = make_event("node.started", thread_id="t", node_id="node_07_speed_fit", attempt=2)
    assert (first["attempt"], retry["attempt"], second["attempt"]) == (1, 2, 2)
    assert retry["event_type"] == "node.retrying"
    assert second["occurred_at"] >= first["occurred_at"]


def test_validate_event_rejects_missing_fields():
    with pytest.raises(ValueError, match="缺少必需字段"):
        validate_event({"event_type": "node.started"})
    ok = make_event("node.succeeded", thread_id="t", node_id="x")
    assert validate_event(dict(ok)) == ok
    with pytest.raises(ValueError, match="未知事件类型"):
        validate_event({**ok, "event_type": "bogus"})


# ===========================================================================
# 脱敏
# ===========================================================================
def test_sanitize_text_masks_secret_kv():
    out = sanitize_text("连接失败: api_key=PK1234567890, 请检查")
    assert "PK1234567890" not in out
    assert "api_key=***" in out or "api_key= ***" in out or "api_key***" in out.replace(" ", "")
    out2 = sanitize_text("token: abc123def456")
    assert "abc123def456" not in out2


def test_sanitize_text_masks_sk_and_bearer_and_dsn():
    assert "sk-realkey12345" not in sanitize_text("key sk-realkey12345 leaked")
    assert "sk-***" in sanitize_text("key sk-realkey12345 leaked")
    assert "BearereyJhbGciOi" not in sanitize_text("Authorization: BearereyjhbGciOiJIUzI1NiJ9.foo")
    masked = sanitize_text("postgresql://postgres:devpass@localhost:5432/db")
    assert "devpass" not in masked
    assert "postgresql://***@" in masked


def test_sanitize_text_strips_absolute_paths():
    win = sanitize_text("写失败 E:\\Documents\\kuaishou\\auto-video-editor\\outputs\\a1\\draft.json")
    assert "E:" not in win and "Documents" not in win
    assert "draft.json" in win
    posix = sanitize_text("读不到 /home/wayyet/recordings/session1/clip3.mp4")
    assert "/home" not in posix and "clip3.mp4" in posix
    unc = sanitize_text("\\\\NAS\\share\\jobs\\v.mp4")
    assert "NAS" not in unc and "v.mp4" in unc


def test_sanitize_text_truncates_long_asr_text():
    asr_full = "大家好,欢迎观看本期视频。" * 800  # ~10k+ 字符
    out = sanitize_text(asr_full)
    assert len(out) <= ev.MAX_TEXT_LEN + len(ev._TRUNC_SUFFIX)
    assert out.endswith(ev._TRUNC_SUFFIX)


def test_sanitize_payload_rejects_non_whitelisted_top_level_keys():
    # "明确拒绝":白名单外的顶层键直接 ValueError,不静默丢弃
    with pytest.raises(ValueError, match="白名单外"):
        sanitize_payload({"state": {"video_input_path": "E:\\x.mp4"}})
    with pytest.raises(ValueError, match="白名单外"):
        sanitize_payload({"asr_full_text": "......", "duration_ms": 5})


def test_sanitize_payload_allows_whitelisted_keys():
    payload = {
        "summary": {"timeline_items": 12},
        "duration_ms": 1250,
        "waiting_kind": ev.WAITING_KIND_WEB_FILE_HANDSHAKE,
        "detail": {"pending_files": 2},
        "inferred": True,
    }
    assert sanitize_payload(payload) == payload


def test_sanitize_payload_drops_nested_sensitive_keys_and_sanitizes_values():
    payload = {
        "detail": {
            "password": "super-secret",           # 敏感键 → 整键丢弃
            "error_message": "失败于 E:\\secret\\report.json, api_key=PK999",  # 值脱敏
            "nested": {"token": "t123"},          # 深层敏感键同样丢弃
        },
    }
    out = sanitize_payload(payload)
    assert "password" not in out["detail"]
    assert "token" not in out["detail"]["nested"]
    assert "E:" not in out["detail"]["error_message"]
    assert "report.json" in out["detail"]["error_message"]
    assert "PK999" not in out["detail"]["error_message"]


def test_sanitize_exception_with_path_and_secret():
    exc = RuntimeError("访问 E:\\auto-video-editor\\config.toml 失败(api_key=PK123456)")
    out = sanitize_exception(exc)
    assert out["type"] == "RuntimeError"
    assert "E:" not in out["message"]
    assert "config.toml" in out["message"]
    assert "PK123456" not in out["message"]


def test_sanitize_exception_accepts_plain_string():
    assert sanitize_exception("boom") == {"type": "error", "message": "boom"}


# ===========================================================================
# SSE 帧(§7.2)
# ===========================================================================
def test_to_sse_frame_with_id_matches_protocol_example():
    e = make_event(
        "node.succeeded",
        thread_id="video-001",
        node_id="assembly_build_timeline",
        payload={"duration_ms": 1250, "summary": {"timeline_items": 12}},
    )
    frame = to_sse_frame({**e, "id": 43})
    assert frame.startswith("id: 43\nevent: node.succeeded\ndata: ")
    assert frame.endswith("\n\n")
    data_line = frame.splitlines()[2]
    data = json.loads(data_line.removeprefix("data: "))
    assert data["thread_id"] == "video-001"
    assert data["node_id"] == "assembly_build_timeline"
    assert data["attempt"] == 1
    assert data["duration_ms"] == 1250          # payload 平铺进 data
    assert data["summary"] == {"timeline_items": 12}
    assert "payload" not in data                # 不出现嵌套 payload 键
    assert "id" not in data                     # id 只在 SSE id 行


def test_to_sse_frame_without_id_omits_id_line():
    e = make_event("node.waiting_input", thread_id="t", node_id="custom_handshake",
                   payload={"waiting_kind": "web_file_handshake"})
    frame = to_sse_frame(e)
    assert not frame.startswith("id:")
    assert frame.startswith("event: node.waiting_input\ndata: ")


def test_to_sse_frame_rejects_malformed_event():
    with pytest.raises(ValueError):
        to_sse_frame({"event_type": "node.started"})  # 缺字段


# ===========================================================================
# 事件存储(成功路径用 fake 连接;降级路径 mock 连接失败)
# ===========================================================================
class _Col:
    """模拟 psycopg 的 cursor.description 列对象(只用到 .name)。"""

    def __init__(self, name: str):
        self.name = name


class _FakeCursor:
    def __init__(self, results: list | None = None):
        self.executed: list[tuple[str, tuple]] = []
        self._results = results or []
        self.description = None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        for expect, result in list(self._results):
            if expect in sql:
                if result[0] is not None:
                    self.description = [_Col(n) for n in result[0]]
                self._rows = result[1]
                self._results.remove((expect, result))
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire_fake_conn(monkeypatch, cursor: _FakeCursor):
    conn = _FakeConn(cursor)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(store, "_conn", fake_conn)
    return conn


def test_append_event_success_path_uses_returning_and_ensure_ascii_false(monkeypatch):
    cursor = _FakeCursor(results=[("RETURNING id", (["id"], [(7,)]))])
    _wire_fake_conn(monkeypatch, cursor)
    e = make_event("node.started", thread_id="线程-中文", node_id="节点x",
                   payload={"summary": {"产物": 1}})
    assert store.append_event(e) == 7
    sql, params = cursor.executed[0]
    assert "INSERT INTO workflow_run_events" in sql
    assert "RETURNING id" in sql
    assert "%s::jsonb" in sql
    # thread_id 中文原样入参;payload JSON 不做 \uXXXX 转义
    assert params[0] == "线程-中文"
    payload_json = params[-1]
    assert "产物" in payload_json and "\\u" not in payload_json


def test_append_event_degrades_on_connection_failure(monkeypatch, caplog):
    @contextmanager
    def broken_conn():
        raise RuntimeError("connection refused (mock)")

    monkeypatch.setattr(store, "_conn", broken_conn)
    before = store.degraded_count()
    e = make_event("node.started", thread_id="t", node_id="x")
    with caplog.at_level(logging.ERROR, logger="monitoring.workflow_event_store"):
        result = store.append_event(e)
    assert result is None                      # 显式 None,不抛、不假装成功
    assert store.degraded_count() == before + 1
    assert any("append_event" in r.message and "已降级" in r.message for r in caplog.records)


def test_fetch_events_after_success_and_degrade(monkeypatch):
    rows = [
        (1, "t", None, "run.started", None, 1, __import__("datetime").datetime(2026, 11, 1, 7, 20, 30), {}),
        (2, "t", None, "node.started", "a", 1, __import__("datetime").datetime(2026, 11, 1, 7, 20, 31), {"duration_ms": 5}),
    ]
    cursor = _FakeCursor(results=[("ORDER BY id ASC", (["id", "thread_id", "run_id", "event_type", "node_id", "attempt", "occurred_at", "payload"], rows))])
    _wire_fake_conn(monkeypatch, cursor)
    events = store.fetch_events_after("t", last_id=0)
    assert [e["id"] for e in events] == [1, 2]
    assert events[1]["payload"] == {"duration_ms": 5}
    assert events[0]["occurred_at"].endswith("Z")

    @contextmanager
    def broken_conn():
        raise RuntimeError("down")

    monkeypatch.setattr(store, "_conn", broken_conn)
    before = store.degraded_count()
    assert store.fetch_events_after("t", 0) == []
    assert store.degraded_count() == before + 1


def test_latest_snapshot_success_and_degrade(monkeypatch):
    import datetime as _dt

    last_row = (2, "t", None, "node.started", "a", 1,
                _dt.datetime(2026, 11, 1, 7, 20, 31), {})
    cursor = _FakeCursor(results=[
        ("COUNT(*)", (None, [(2, 1, 2)])),
        ("GROUP BY node_id", (None, [("a", 1)])),
        ("ORDER BY id DESC", (["id", "thread_id", "run_id", "event_type", "node_id", "attempt", "occurred_at", "payload"], [last_row])),
    ])
    _wire_fake_conn(monkeypatch, cursor)
    snap = store.latest_snapshot("t")
    assert snap["available"] is True
    assert snap["event_count"] == 2
    assert snap["last_id"] == 2
    assert snap["node_attempts"] == {"a": 1}
    assert snap["last_event"]["event_type"] == "node.started"

    @contextmanager
    def broken_conn():
        raise RuntimeError("down")

    monkeypatch.setattr(store, "_conn", broken_conn)
    before = store.degraded_count()
    snap = store.latest_snapshot("t")
    assert snap["available"] is False
    assert snap["event_count"] == 0
    assert store.degraded_count() == before + 1


def test_append_event_rejects_malformed_event_without_touching_db(monkeypatch):
    """校验失败必须先于连库:坏事件不允许走到 INSERT。"""
    calls: list[str] = []

    @contextmanager
    def conn_spy():
        calls.append("connected")
        raise AssertionError("不应连库")

    monkeypatch.setattr(store, "_conn", conn_spy)
    assert store.append_event({"event_type": "node.started"}) is None  # 缺字段
    assert calls == []

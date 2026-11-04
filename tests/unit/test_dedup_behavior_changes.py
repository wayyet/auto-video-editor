"""去重合并里"行为有变化"的几处(M09 / M15 / M16),逐项锁定新行为。

只用环境变量和纯函数,不需要真实数据库、ffmpeg。
M12(取用户要求时多一步 strip)差别只在首尾空白,不单独加用例。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

GOOD_URI = "postgresql://alice:s3cret@db.example:5432/other_db"


# --------------------------------------------------------------------------- M09
def _clear_pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "AGENT_GATE_POSTGRES_URI", "POSTGRES_URI"):
        monkeypatch.delenv(name, raising=False)


def test_m09_uri_prefers_agent_gate_var(monkeypatch):
    from db.migrations import postgres_uri

    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", "postgresql://x:y@localhost:5432/prod_db")
    monkeypatch.setenv("AGENT_GATE_POSTGRES_URI", GOOD_URI)
    assert postgres_uri() == GOOD_URI


def test_m09_uri_rejects_agent_gate_var_in_production(monkeypatch):
    from db.migrations import postgres_uri

    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AGENT_GATE_POSTGRES_URI", GOOD_URI)
    with pytest.raises(RuntimeError):
        postgres_uri()


def test_m09_uri_rejects_malformed_dsn(monkeypatch):
    from db.migrations import postgres_uri

    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", "not-a-dsn")
    with pytest.raises(ValueError):
        postgres_uri()


# --------------------------------------------------------------------------- M15
@pytest.mark.parametrize(
    "clip",
    [
        {"start": 0, "end": True},  # bool 不算数字(旧 QC 版会当成 1.0)
        {"start": 0, "end": float("inf")},  # inf 当缺失(旧 timeline_ops 版会返回 inf)
        {"duration": float("inf")},
        {"start": 0, "end": float("nan")},
    ],
)
def test_m15_timeline_duration_treats_dirty_numbers_as_missing(clip):
    from assembly_capabilities.timeline_ops import timeline_duration

    assert timeline_duration({"clips": [clip]}) is None


def test_m15_timeline_duration_normal_case_and_single_definition():
    from assembly_capabilities import qc_preview, timeline_ops

    assert timeline_ops.timeline_duration({"clips": [{"start": 0, "end": 5}]}) == 5.0
    assert qc_preview.timeline_duration is timeline_ops.timeline_duration  # 只剩一份定义


# --------------------------------------------------------------------------- M16
def test_m16_match_by_checkpoint_field():
    from agent_gate.resume_match import match_resume_values

    interrupts = [NS(id="a", value={"checkpoint": "①"}), NS(id="b", value={"checkpoint": "②"})]
    resume_map, unmatched = match_resume_values(interrupts, {"①": 1, "②": 2})
    assert resume_map == {"a": 1, "b": 2}
    assert unmatched == []


def test_m16_single_interrupt_uses_fallback_key():
    from agent_gate.resume_match import match_resume_values

    resume_map, unmatched = match_resume_values([NS(id="a", value={})], {"default": True})
    assert resume_map == {"a": True}
    assert unmatched == []


def test_m16_non_dict_value_is_unmatched_not_a_crash():
    """旧的异步版遇到"真值但不是 dict"(如字符串)会 AttributeError;现在按"没有 checkpoint 字段"处理。"""
    from agent_gate.resume_match import match_resume_values

    resume_map, unmatched = match_resume_values([NS(id="a", value="text")], {"①": True})
    assert resume_map == {}
    assert [i.id for i in unmatched] == ["a"]

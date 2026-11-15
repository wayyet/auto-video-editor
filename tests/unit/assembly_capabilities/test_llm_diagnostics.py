"""P2-1:LLM 调用诊断(llm_diagnostics)契约单测。

验收对照(计划文档 P2-1):
- Stub 客户端必须显式 ``llm_client_type=stub``,不冒充真实成功;
- 真实成功 / 超时 / 空响应 / JSON 解析失败 / schema 不匹配各有不同 request_status;
- ``llm_used`` 不再是判断"是否发起请求"的唯一字段(诊断独立成 dict);
- 候选索引统计:returned / invalid / duplicate / adopted。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from assembly_capabilities.build_timeline import (
    LLM_REQUEST_STATUSES,
    build_candidate_plan_from_media,
    build_llm_diagnostics,
)
from assembly_capabilities.draft_patch import plan_sha256
from tests.support.draft_influence import load_canonical_sample


def _base_plan() -> tuple[dict, str]:
    plan = load_canonical_sample()
    return plan, plan_sha256(plan)


def _media_reports(candidates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "source": "X:/fixture/media_a.mp4",
            "asset_id": "a_test",
            "probe": {"duration_seconds": 10.0, "summary": {"duration_seconds": 10.0}},
            "analysis": {
                "input_path": "X:/fixture/media_a.mp4",
                "duration_seconds": 10.0,
                "candidate_segments": candidates
                if candidates is not None
                else [{"index": 0, "start": 0.0, "end": 4.0}],
            },
        }
    ]


def _run(client: Any, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    plan, sha = _base_plan()
    return build_candidate_plan_from_media(
        base_plan=plan,
        base_plan_sha256=sha,
        media_reports=_media_reports(candidates),
        observation_data=None,
        lang="zh",
        client=client,
    )


class _Scripted:
    """脚本化"真实"客户端:按预置答案返回(非 StubLLMClient → real)。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def chat(self, **_kwargs):  # noqa: ANN001 — LLMClient 协议
        return self.answer


class _Boom(Exception):
    pass


class _Raiser:
    def __init__(self, message: str) -> None:
        self.message = message

    def chat(self, **_kwargs):  # noqa: ANN001 — LLMClient 协议
        raise _Boom(self.message)


def _selection(index: int = 0) -> str:
    return json.dumps(
        {"selected_segments": [{"index": index, "order": 0, "reason": "test"}]}
    )


# ---------------------------------------------------------------------------
# 客户端类型显式分类
# ---------------------------------------------------------------------------
def test_stub_client_is_explicitly_labeled() -> None:
    from storyline_capabilities.vlm_client import StubLLMClient

    result = _run(StubLLMClient())
    diag = result["llm_diagnostics"]
    assert diag["llm_client_type"] == "stub"
    assert diag["model_name"] == "StubLLMClient"
    # stub 给 schema 形状空选段 → 合法调用、空响应(绝不冒充真实成功)
    assert diag["request_status"] == "empty_response"
    assert diag["llm_attempted"] is True
    assert diag["selection_applied"] is False
    # 旧口径 llm_used 对"有 selected_segments 字段(含空 list)"为 True ——
    # P2-1 的改进在于诊断字段:selection_applied=False + client_type=stub
    # 才是"stub 空选段"的准确刻画,llm_used 不再是唯一判据
    assert result["llm_used"] is True
    assert diag["request_status"] != "ok"


def test_real_client_success_ok_status() -> None:
    result = _run(_Scripted(_selection(0)))
    diag = result["llm_diagnostics"]
    assert diag["llm_client_type"] == "real"
    assert diag["request_status"] == "ok"
    assert diag["response_valid"] is True
    assert diag["selection_applied"] is True
    assert diag["selection_stats"]["adopted"] == 1
    assert diag["selection_stats"]["returned"] == 1
    assert result["llm_used"] is True


def test_parse_error_distinguished() -> None:
    result = _run(_Scripted("这不是 JSON <<<"))
    diag = result["llm_diagnostics"]
    assert diag["request_status"] == "parse_error"
    assert diag["response_valid"] is False
    assert diag["failure_reason"]
    assert diag["llm_client_type"] == "real"


def test_schema_mismatch_distinguished() -> None:
    # 解析成功但没有 selected_segments list → schema_mismatch(非 parse_error)
    result = _run(_Scripted(json.dumps({"foo": 1})))
    diag = result["llm_diagnostics"]
    assert diag["request_status"] == "schema_mismatch"
    assert diag["failure_reason"].startswith("解析成功但")


def test_exception_distinguished() -> None:
    result = _run(_Raiser("backend exploded"))
    diag = result["llm_diagnostics"]
    assert diag["request_status"] == "exception"
    assert "backend exploded" in diag["failure_reason"]
    assert diag["response_valid"] is False


def test_timeout_status_from_exception_text() -> None:
    result = _run(_Raiser("mcode exec timed out after 30s"))
    diag = result["llm_diagnostics"]
    assert diag["request_status"] == "timeout"


def test_invalid_and_duplicate_indices_counted() -> None:
    answer = json.dumps(
        {
            "selected_segments": [
                {"index": 99, "order": 0},   # invalid(超界)
                {"index": 0, "order": 1},    # adopted
                {"index": 0, "order": 2},    # duplicate
            ]
        }
    )
    result = _run(_Scripted(answer))
    stats = result["llm_diagnostics"]["selection_stats"]
    assert stats["returned"] == 3
    assert stats["invalid"] == 1
    assert stats["duplicate"] == 1
    assert stats["adopted"] == 1


def test_not_attempted_when_no_candidates() -> None:
    # analysis 无候选且无 duration → candidates 空 → 不发请求
    result = _run(None, candidates=None)
    # candidates=None 时 _media_reports 兜底给了默认候选 —— 用显式空报告构造
    plan, sha = _base_plan()
    reports = [
        {
            "source": "X:/fixture/media_a.mp4",
            "asset_id": "a_test",
            "probe": {"duration_seconds": 0.0},
            "analysis": {"input_path": "X:/fixture/media_a.mp4", "duration_seconds": 0.0},
        }
    ]
    result = build_candidate_plan_from_media(
        base_plan=plan,
        base_plan_sha256=sha,
        media_reports=reports,
        observation_data=None,
        lang="zh",
        client=None,
    )
    diag = result["llm_diagnostics"]
    assert diag["llm_attempted"] is False
    assert diag["request_status"] == "not_attempted"
    assert result["no_safe_candidate"] is True


def test_diagnostics_present_even_when_llm_used_false() -> None:
    """llm_used 不再是唯一信号:诊断 dict 无论如何都完整产出(状态可分类)。"""
    from storyline_capabilities.vlm_client import StubLLMClient

    result = _run(StubLLMClient())
    diag = result["llm_diagnostics"]
    assert diag["llm_attempted"] is True
    assert diag["request_status"] in LLM_REQUEST_STATUSES
    # parse_error 场景:llm_used=False 且诊断给出失败原因
    result_bad = _run(_Scripted("<<<not json>>>"))
    assert result_bad["llm_used"] is False
    assert result_bad["llm_diagnostics"]["request_status"] == "parse_error"
    assert result_bad["llm_diagnostics"]["failure_reason"]


# ---------------------------------------------------------------------------
# build_llm_diagnostics 纯函数直测(客户端类型三分类)
# ---------------------------------------------------------------------------
def test_client_type_classification() -> None:
    from storyline_capabilities.vlm_client import StubLLMClient

    assert build_llm_diagnostics(
        client=None, call_trace={}, raw_selection={}, selection_stats={},
        chosen_count=0, fallback_used=False, attempted=False,
    )["llm_client_type"] == "none"
    assert build_llm_diagnostics(
        client=StubLLMClient(), call_trace={}, raw_selection={}, selection_stats={},
        chosen_count=0, fallback_used=False, attempted=False,
    )["llm_client_type"] == "stub"
    assert build_llm_diagnostics(
        client=_Scripted("{}"), call_trace={}, raw_selection={}, selection_stats={},
        chosen_count=0, fallback_used=False, attempted=False,
    )["llm_client_type"] == "real"


def test_copy_of_base_plan_untouched_by_diagnostics() -> None:
    """诊断不影响基线不可变纪律:同一 plan 两次运行 hash 一致。"""
    plan, sha = _base_plan()
    snapshot = copy.deepcopy(plan)
    _run(None)
    assert plan == snapshot
    assert plan_sha256(plan) == sha

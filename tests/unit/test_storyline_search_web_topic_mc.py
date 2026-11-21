"""单测:search_web_topic 能力层的 mediacrawler provider + Bing 回退
(2026-11-20 审查修订版阶段3/6.1,artifact 语义=审查文档 §6.2,回退矩阵=§6.4)。

与 test_storyline_capabilities_search_web_topic.py 分文件:MC 分支的注入点
是 ``_mc_client.search_notes``(fake 返回原始记录)与 ``_bing_html_search``,
不涉及 HTTP/HTML 桩,单独成文件避免与存量 800 行用例互相干扰。
"""
from __future__ import annotations

import pytest

import storyline_capabilities.search_web_topic as cap_mod
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    mediacrawler_request_provider,
    search_web_topic,
)

_MC_REV = "09ed1ec62d8"

_MC_OK_DIAG = {"primary": {"returncode": 0}}


def _mc_note(idx: int = 1) -> dict:
    # 标题/摘要携带检索词 token(平台搜索结果的真实形态):低相关结果会被
    # 相关性守卫拦截(独立用例覆盖),成功路径数据必须真实可过守卫。
    return {
        "note_id": str(idx),
        "xsec_token": "should_not_leak",
        "title": f"夏季防晒笔记{idx}",
        "desc": f"夏季防晒 描述{idx}",
        "note_url": f"https://www.xiaohongshu.com/explore/{idx}",
        "nickname": f"作者{idx}",
    }


def _mc_options(platform: str = "xhs", revision: str = _MC_REV) -> dict:
    return {
        "platform": platform,
        "project_root": "/mc",
        "save_root": "/mc/runs",
        "timeout_seconds": 600,
        "headless": False,
        "mc_revision": revision,
    }


def _patch_mc(monkeypatch, records, error_code=None, message="") -> None:
    """替换 _mc_client.search_notes:恒返回 (records, error_code, message, diag)。"""

    def fake(**kwargs):
        return list(records), error_code, message, dict(_MC_OK_DIAG)

    monkeypatch.setattr(cap_mod._mc_client, "search_notes", fake)


def _patch_bing(monkeypatch, items, error_code=None, diagnostics=None) -> None:
    monkeypatch.setattr(
        cap_mod, "_bing_html_search", lambda **kw: (items, error_code, diagnostics or {})
    )


def _mc_call(query: str = "夏季防晒", **overrides):
    options = _mc_options()
    kwargs = dict(
        query=query, max_results=10, provider="mediacrawler", provider_options=options
    )
    kwargs.update(overrides)
    return search_web_topic(**kwargs)


# ---------------------------------------------------------------------------
# MC 成功路径(§6.2 语义)
# ---------------------------------------------------------------------------
def test_mc_success_maps_items_and_semantics(monkeypatch) -> None:
    _patch_mc(monkeypatch, [_mc_note(1), _mc_note(2)])
    res = _mc_call()
    assert res["status"] == "ok"
    assert res["provider"] == "mediacrawler"
    assert res["requested_provider"] == "mediacrawler"
    assert res["engine"] == "MediaCrawler(小红书)"
    assert res["error_code"] is None
    items = res["search_web_topic"]
    assert len(items) == 2
    assert items[0] == {
        "url": "https://www.xiaohongshu.com/explore/1",
        "title": "夏季防晒笔记1",
        "snippet": "夏季防晒 描述1",
        "source": "mediacrawler",
    }
    # 原始记录的敏感/无关字段不得进入产物(审查 P1-06 白名单四字段)
    assert "nickname" not in items[0] and "xsec_token" not in items[0]
    # request_hash = 首选策略 hash(平台+revision 编入),与 helper 逐字一致
    assert res["request_hash"] == build_request_hash(
        query="夏季防晒",
        provider=mediacrawler_request_provider("xhs", _MC_REV),
        max_results=10,
    )
    assert "fallback" not in res


def test_mc_success_max_results_is_respected(monkeypatch) -> None:
    _patch_mc(monkeypatch, [_mc_note(i) for i in range(1, 6)])
    res = search_web_topic(
        query="q",
        max_results=3,
        provider="mediacrawler",
        provider_options=_mc_options(),
    )
    assert len(res["search_web_topic"]) == 3


# ---------------------------------------------------------------------------
# 回退路径(§6.4 回退矩阵)
# ---------------------------------------------------------------------------
def test_mc_failure_falls_back_to_bing(monkeypatch) -> None:
    """MC 失败 → 自动回退 bing_html:provider=实际产出者,fallback 留痕。"""
    _patch_mc(monkeypatch, [], error_code="NETWORK_ERROR", message="down")
    _patch_bing(
        monkeypatch,
        [
            {
                "url": "https://e.com/1",
                "title": "Bing 标题",
                "snippet": "s",
                "source": "bing_html",
            }
        ],
    )
    res = _mc_call()
    assert res["status"] == "ok"
    assert res["provider"] == "bing_html"
    assert res["requested_provider"] == "mediacrawler"
    assert res["engine"] == "Bing(MediaCrawler 回退)"
    assert res["fallback"]["from_provider"] == "mediacrawler"
    assert res["fallback"]["from_platform"] == "xhs"
    assert res["fallback"]["to_provider"] == "bing_html"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"
    assert res["fallback"]["primary_message"] == "down"
    assert res["search_web_topic"][0]["source"] == "bing_html"
    assert "MediaCrawler(小红书)" in res["message"] and "已回退 Bing" in res["message"]
    # 回退成功仍保留首选策略 hash(QA 重试幂等,§6.2)
    assert res["request_hash"] == build_request_hash(
        query="夏季防晒",
        provider=mediacrawler_request_provider("xhs", _MC_REV),
        max_results=10,
    )


def test_mc_zero_valid_records_falls_back(monkeypatch) -> None:
    """MC 跑完但清洗后 0 条(全为非法 URL)→ NO_RESULTS 语义回退(§6.3)。"""
    _patch_mc(
        monkeypatch,
        [{"title": "无链接笔记", "desc": "d", "note_url": "javascript:void(0)"}],
    )
    _patch_bing(monkeypatch, [], error_code="NO_RESULTS")
    res = _mc_call(query="q")
    assert res["status"] == "no_results"
    assert res["provider"] == "bing_html"
    assert res["fallback"]["primary_error_code"] == "NO_RESULTS"


def test_mc_client_error_no_results_also_falls_back(monkeypatch) -> None:
    """client 层 NO_RESULTS(无结果文件/空列表)同样回退。"""
    _patch_mc(monkeypatch, [], error_code="NO_RESULTS", message="empty")
    _patch_bing(
        monkeypatch,
        [
            {
                "url": "https://e.com/2",
                "title": "t",
                "snippet": "",
                "source": "bing_html",
            }
        ],
    )
    res = _mc_call(query="q")
    assert res["status"] == "ok" and res["provider"] == "bing_html"


def test_mc_fail_bing_zero_results_is_soft_no_results(monkeypatch) -> None:
    """MC 失败 + Bing 0 条 → no_results(软态,§6.4 矩阵第 4 行)。"""
    _patch_mc(monkeypatch, [], error_code="NETWORK_ERROR", message="down")
    _patch_bing(monkeypatch, [], error_code="NO_RESULTS")
    res = _mc_call(query="q")
    assert res["status"] == "no_results"
    assert res["error_code"] == "NO_RESULTS"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"


# ---------------------------------------------------------------------------
# 双失败(P1-05:两端错误都不丢)
# ---------------------------------------------------------------------------
def test_double_failure_keeps_both_errors(monkeypatch) -> None:
    _patch_mc(monkeypatch, [], error_code="NETWORK_ERROR", message="mc down")
    _patch_bing(
        monkeypatch, [], error_code="PROVIDER_BLOCKED", diagnostics={"challenge": True}
    )
    res = _mc_call(query="q")
    assert res["status"] == "failed"
    assert res["error_code"] == "PROVIDER_BLOCKED"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"
    assert res["fallback"]["primary_message"] == "mc down"
    assert res["fallback_error"]["error_code"] == "PROVIDER_BLOCKED"
    assert "mc down" in res["message"]


# ---------------------------------------------------------------------------
# 缓存隔离(P2-01/P2-04)
# ---------------------------------------------------------------------------
def test_mc_platform_and_revision_hash_isolation() -> None:
    """平台/上游 revision 变化必得不同 hash。"""
    base = mediacrawler_request_provider("xhs", "rev1")
    assert base != mediacrawler_request_provider("wb", "rev1")
    assert base != mediacrawler_request_provider("xhs", "rev2")
    assert build_request_hash(query="q", provider=base, max_results=10) != (
        build_request_hash(
            query="q",
            provider=mediacrawler_request_provider("xhs", "rev2"),
            max_results=10,
        )
    )


def test_mc_request_hash_differs_from_plain_engine_key() -> None:
    """mc 动态键 hash ≠ 基础 mediacrawler 键 hash(防跨形态串缓存)。"""
    plain = build_request_hash(query="q", provider="mediacrawler", max_results=10)
    dynamic = build_request_hash(
        query="q", provider=mediacrawler_request_provider("xhs", "rev"), max_results=10
    )
    assert plain != dynamic


# ---------------------------------------------------------------------------
# 相关性守卫覆盖 mc 分支(2026-11-19 真机事故防线;并行会话审查提醒的缺项)
# ---------------------------------------------------------------------------
def test_mc_irrelevant_results_guard_falls_back(monkeypatch) -> None:
    """低相关笔记(0 条命中检索词 token)→ IRRELEVANT_RESULTS → 回退 Bing。"""
    records = [
        {
            "note_url": "https://www.xiaohongshu.com/explore/1",
            "title": "completely unrelated topic",
            "desc": "nothing to do with the query",
        },
        {
            "note_url": "https://www.xiaohongshu.com/explore/2",
            "title": "another unrelated one",
            "desc": "still nothing",
        },
    ]
    _patch_mc(monkeypatch, records)
    _patch_bing(
        monkeypatch,
        [
            {
                "url": "https://e.com/1",
                "title": "t",
                "snippet": "",
                "source": "bing_html",
            }
        ],
    )
    res = _mc_call(query="海上清真寺旅游攻略")
    assert res["status"] == "ok"
    assert res["provider"] == "bing_html"
    assert res["fallback"]["primary_error_code"] == "IRRELEVANT_RESULTS"
    assert "relevance matched" in res["fallback"]["primary_message"]


def test_mc_guard_stats_recorded_in_diagnostics(monkeypatch) -> None:
    """守卫命中统计进 diagnostics.primary,便于审计;全部命中不拦截。"""
    _patch_mc(monkeypatch, [_mc_note(1), _mc_note(2)])
    res = _mc_call()
    primary = res["diagnostics"]["primary"]
    assert primary["relevance_matched"] == 2
    assert primary["relevance_total"] == 2


# ---------------------------------------------------------------------------
# 入参校验与 _map_mc_records(§6.3)
# ---------------------------------------------------------------------------
def test_mc_missing_required_options_raises(monkeypatch) -> None:
    """缺 project_root → ValueError(编程错误,节点层 TOOL_EXECUTION_FAILED)。"""
    with pytest.raises(ValueError, match="project_root"):
        search_web_topic(
            query="q", provider="mediacrawler", provider_options={"platform": "xhs"}
        )


def test_map_mc_records_validity_rules() -> None:
    """scheme 白名单/空标题丢弃/同 URL 去重/限量/snippet 回退键。"""
    records = [
        {"note_url": "https://a.com/1", "title": "t1", "desc": "d1"},
        {"note_url": "https://a.com/1", "title": "t1-dup", "desc": "d1"},  # 重复 URL
        {"note_url": "ftp://a.com/2", "title": "t2"},  # 非法 scheme
        {"note_url": "https://a.com/3", "title": ""},  # 空标题
        {"url": "https://a.com/4", "title": "t4", "content": "c4"},  # url/content 回退键
        {"note_url": "https://a.com/5", "title": "t5"},  # 超量
    ]
    items = cap_mod._map_mc_records(records, max_results=3)
    assert [it["url"] for it in items] == [
        "https://a.com/1",
        "https://a.com/4",
        "https://a.com/5",
    ]
    assert items[1]["snippet"] == "c4"
    assert all(it["source"] == "mediacrawler" for it in items)


# ---------------------------------------------------------------------------
# 2026-11-21 审查修订版 §6.3:平台链执行矩阵(xhs → dy → Bing)
# ---------------------------------------------------------------------------
def _mc_aweme(idx: int = 1) -> dict:
    """抖音记录真实形状(store/douyin:aweme_url 承载链接)。"""
    return {
        "aweme_id": str(idx),
        "title": f"夏季防晒视频{idx}",
        "desc": f"夏季防晒 描述{idx}",
        "aweme_url": f"https://www.douyin.com/video/{idx}",
        "nickname": f"作者{idx}",
    }


def _mc_chain_options(
    fallback: tuple[str, ...] = ("dy",),
    revision: str = _MC_REV,
    chain_timeout: int = 600,
    timeout: int = 600,
) -> dict:
    options = _mc_options(revision=revision)
    options.update(
        {
            "fallback_platforms": fallback,
            "chain_timeout_seconds": chain_timeout,
            "timeout_seconds": timeout,
        }
    )
    return options


def _patch_mc_by_platform(monkeypatch, behavior: dict) -> list[dict]:
    """按平台分派 search_notes 桩;behavior[platform]=(records, error, message)。

    返回调用记录(断言"哪些平台被尝试/每平台超时预算")。
    """
    calls: list[dict] = []

    def fake(**kwargs):
        platform = str(kwargs.get("platform"))
        calls.append({"platform": platform, "kwargs": dict(kwargs)})
        rec, err, msg = behavior.get(platform, ([], "NETWORK_ERROR", "down"))
        return (
            list(rec),
            err,
            msg,
            {"platform": platform, "returncode": 0 if err is None else 1},
        )

    monkeypatch.setattr(cap_mod._mc_client, "search_notes", fake)
    return calls


def _mc_chain_call(monkeypatch=None, options: dict | None = None, **overrides):
    kwargs = dict(
        query="夏季防晒",
        max_results=10,
        provider="mediacrawler",
        provider_options=options if options is not None else _mc_chain_options(),
    )
    kwargs.update(overrides)
    return search_web_topic(**kwargs)


_BING_ONE = [
    {"url": "https://e.com/1", "title": "t", "snippet": "", "source": "bing_html"}
]


def test_chain_primary_success_skips_fallback_and_bing(monkeypatch) -> None:
    """§6.3-1:xhs 有效且相关 → 只调 xhs,不调 dy/Bing。"""
    calls = _patch_mc_by_platform(monkeypatch, {"xhs": ([_mc_note(1)], None, "")})
    _patch_bing(monkeypatch, _BING_ONE)
    res = _mc_chain_call()
    assert [c["platform"] for c in calls] == ["xhs"]
    assert res["status"] == "ok" and res["provider"] == "mediacrawler"
    assert res["engine"] == "MediaCrawler(小红书)"
    assert res["selected_platform"] == "xhs"
    assert "platform_fallback" not in res and "fallback" not in res
    assert res["message"] == ""


def test_chain_primary_fails_dy_succeeds_no_bing(monkeypatch) -> None:
    """§6.3-2:xhs 进程失败 → dy 有效 → 返回 dy 且不调 Bing。"""
    bing_calls: list[int] = []
    monkeypatch.setattr(
        cap_mod,
        "_bing_html_search",
        lambda **kw: bing_calls.append(1) or (_BING_ONE, None, {}),
    )
    calls = _patch_mc_by_platform(
        monkeypatch,
        {
            "xhs": ([], "NETWORK_ERROR", "xhs down"),
            "dy": ([_mc_aweme(1), _mc_aweme(2)], None, ""),
        },
    )
    res = _mc_chain_call()
    assert [c["platform"] for c in calls] == ["xhs", "dy"]
    assert bing_calls == []  # dy 成功不调 Bing
    assert res["status"] == "ok" and res["provider"] == "mediacrawler"
    assert res["engine"] == "MediaCrawler(抖音)"
    assert res["selected_platform"] == "dy"
    # 同 Provider 内平台切换:platform_fallback 留痕,不得伪装跨 Provider fallback
    pf = res["platform_fallback"]
    assert pf == {"from_platform": "xhs", "to_platform": "dy", "reason": "NETWORK_ERROR"}
    assert "fallback" not in res
    # §6.3-15:平台回退 message 记录切换原因(页面"平台回退说明"数据源)
    assert "MediaCrawler(小红书) NETWORK_ERROR" in res["message"]
    assert "已切换 MediaCrawler(抖音)" in res["message"]


def test_chain_dy_aweme_url_mapped_to_standard_url(monkeypatch) -> None:
    """§6.3-8:dy 记录经 _map_mc_records 从 aweme_url 生成标准四键条目。"""
    _patch_mc_by_platform(
        monkeypatch,
        {"xhs": ([], "NETWORK_ERROR", "down"), "dy": ([_mc_aweme(3)], None, "")},
    )
    res = _mc_chain_call()
    assert res["search_web_topic"][0] == {
        "url": "https://www.douyin.com/video/3",
        "title": "夏季防晒视频3",
        "snippet": "夏季防晒 描述3",
        "source": "mediacrawler",
    }


def test_chain_primary_empty_records_dy_succeeds(monkeypatch) -> None:
    """§6.3-3:xhs 无有效 JSON(空记录→NO_RESULTS)→ dy 有效接管。"""
    calls = _patch_mc_by_platform(
        monkeypatch, {"xhs": ([], None, ""), "dy": ([_mc_aweme(1)], None, "")}
    )
    res = _mc_chain_call()
    assert [c["platform"] for c in calls] == ["xhs", "dy"]
    assert res["engine"] == "MediaCrawler(抖音)"
    assert res["diagnostics"]["attempts"][0]["error_code"] == "NO_RESULTS"


def test_chain_primary_irrelevant_dy_relevant_wins(monkeypatch) -> None:
    """§6.3-4:xhs 有结果但相关性不达标 → dy 相关性通过接管。"""
    irrelevant = [
        {
            "note_url": "https://www.xiaohongshu.com/explore/1",
            "title": "completely unrelated topic",
            "desc": "nothing to do",
        }
    ]
    calls = _patch_mc_by_platform(
        monkeypatch,
        {"xhs": (irrelevant, None, ""), "dy": ([_mc_aweme(1)], None, "")},
    )
    res = _mc_chain_call()
    assert [c["platform"] for c in calls] == ["xhs", "dy"]
    assert res["diagnostics"]["attempts"][0]["error_code"] == "IRRELEVANT_RESULTS"
    assert res["selected_platform"] == "dy"


def test_chain_all_fail_falls_back_to_bing_once(monkeypatch) -> None:
    """§6.3-5/6:全链失败 → attempts 保留逐平台原因,Bing 只调一次。"""
    bing_calls: list[int] = []
    monkeypatch.setattr(
        cap_mod,
        "_bing_html_search",
        lambda **kw: bing_calls.append(1) or (_BING_ONE, None, {}),
    )
    _patch_mc_by_platform(
        monkeypatch,
        {
            "xhs": ([], "NETWORK_ERROR", "xhs down"),
            "dy": ([], "AUTH_REQUIRED", "scan-login may be required"),
        },
    )
    res = _mc_chain_call()
    assert bing_calls == [1]
    assert res["status"] == "ok" and res["provider"] == "bing_html"
    assert res["engine"] == "Bing(MediaCrawler 回退)"
    attempts = res["diagnostics"]["attempts"]
    assert [(a["platform"], a["error_code"]) for a in attempts] == [
        ("xhs", "NETWORK_ERROR"),
        ("dy", "AUTH_REQUIRED"),
    ]
    # 整链失败历史进 message(页面"回退说明"可读,审查修订版 §5.3)
    assert "MediaCrawler(小红书) NETWORK_ERROR: xhs down" in res["message"]
    assert "MediaCrawler(抖音) AUTH_REQUIRED" in res["message"]
    assert "已回退 Bing" in res["message"]
    # fallback 字段保持主平台语义(§4.4:只代表 MC→Bing 跨 Provider 回退)
    assert res["fallback"]["from_platform"] == "xhs"
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"
    assert "platform_fallback" not in res


def test_chain_empty_fallback_matches_legacy_behavior(monkeypatch) -> None:
    """§6.3-7:fallback_platforms=() → 与既有 xhs→Bing 行为逐字一致。"""
    calls = _patch_mc_by_platform(monkeypatch, {"xhs": ([], "NETWORK_ERROR", "down")})
    _patch_bing(monkeypatch, _BING_ONE)
    res = _mc_chain_call(options=_mc_chain_options(fallback=()))
    assert [c["platform"] for c in calls] == ["xhs"]
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"
    assert res["diagnostics"]["platform_chain"] == ["xhs"]


def test_chain_attempt_diagnostics_shape(monkeypatch) -> None:
    """attempts 携带逐平台诊断;primary 保持主平台诊断(§4.4 兼容)。"""
    _patch_mc_by_platform(
        monkeypatch,
        {"xhs": ([_mc_note(1), _mc_note(2)], None, ""), "dy": ([], "NETWORK_ERROR", "d")},
    )
    res = _mc_chain_call()
    assert res["diagnostics"]["platform_chain"] == ["xhs", "dy"]
    primary = res["diagnostics"]["primary"]
    assert primary["relevance_matched"] == 2  # 主平台诊断原样保留
    assert len(res["diagnostics"]["attempts"]) == 1
    assert res["diagnostics"]["attempts"][0]["attempt_index"] == 0


def test_chain_budget_exhausted_skips_next_platform(monkeypatch) -> None:
    """§6.3-12:整链预算耗尽 → 不启动下一平台(TIME_BUDGET_EXHAUSTED),进 Bing。"""
    seq = [1000.0, 1000.0, 5000.0]  # deadline → xhs remaining → dy remaining
    tic = {"n": 0}

    def fake_monotonic() -> float:
        v = seq[min(tic["n"], len(seq) - 1)]
        tic["n"] += 1
        return v

    monkeypatch.setattr(cap_mod.time, "monotonic", fake_monotonic)
    calls = _patch_mc_by_platform(
        monkeypatch, {"xhs": ([], "NETWORK_ERROR", "down")}
    )
    _patch_bing(monkeypatch, _BING_ONE)
    res = _mc_chain_call(options=_mc_chain_options(chain_timeout=600))
    assert [c["platform"] for c in calls] == ["xhs"]  # dy 从未启动
    attempts = res["diagnostics"]["attempts"]
    assert attempts[1] == {
        "platform": "dy",
        "attempt_index": 1,
        "error_code": "TIME_BUDGET_EXHAUSTED",
        "message": "chain time budget exhausted before this attempt started",
    }
    assert "TIME_BUDGET_EXHAUSTED" in res["message"]
    assert res["fallback"]["primary_error_code"] == "NETWORK_ERROR"


def test_chain_attempt_timeout_capped_by_remaining_budget(monkeypatch) -> None:
    """第二平台的子进程超时 = min(单平台上限, 链剩余预算)(§3.5)。"""
    calls = _patch_mc_by_platform(
        monkeypatch,
        {"xhs": ([], "NETWORK_ERROR", "down"), "dy": ([_mc_aweme(1)], None, "")},
    )
    res = _mc_chain_call(
        options=_mc_chain_options(chain_timeout=600, timeout=600)
    )
    assert res["selected_platform"] == "dy"
    # 两个平台拿到的 timeout_seconds 都 ≤ 各自剩余预算(桩内恒 600,不超限)
    assert all(c["kwargs"]["timeout_seconds"] <= 600 for c in calls)


def test_chain_programming_errors_fail_fast(monkeypatch) -> None:
    """§6.3-13:provider_options 形状错误 raise,不伪装成平台不可用。"""
    with pytest.raises(ValueError, match="project_root"):
        _mc_chain_call(options={"platform": "xhs", "fallback_platforms": ("dy",)})


def test_chain_auth_required_not_masked_as_no_results(monkeypatch) -> None:
    """§6.3-14:登录失败(AUTH_REQUIRED)如实进入 attempts/message,不宣称无结果。"""
    _patch_mc_by_platform(
        monkeypatch,
        {"xhs": ([], "NETWORK_ERROR", "down"), "dy": ([], "AUTH_REQUIRED", "login needed")},
    )
    _patch_bing(monkeypatch, _BING_ONE)
    res = _mc_chain_call()
    assert res["diagnostics"]["attempts"][1]["error_code"] == "AUTH_REQUIRED"
    assert "AUTH_REQUIRED" in res["message"]


def test_chain_hash_changes_with_chain_membership_or_order() -> None:
    """§6.3-10:链成员/顺序变化必得不同 hash(缓存隔离,§3.4)。"""
    rev = "09ed1ec62d8"
    base = mediacrawler_request_provider(("xhs", "dy"), rev)
    assert base != mediacrawler_request_provider(("xhs",), rev)
    assert base != mediacrawler_request_provider(("xhs", "ks"), rev)
    assert base != mediacrawler_request_provider(("dy", "xhs"), rev)
    assert build_request_hash(query="q", provider=base, max_results=10) != build_request_hash(
        query="q",
        provider=mediacrawler_request_provider(("xhs", "ks"), rev),
        max_results=10,
    )


def test_chain_hash_consistent_between_node_and_capability() -> None:
    """§6.3-11:节点层与能力层用同一 helper → 同链同 revision 得同 hash。"""
    assert mediacrawler_request_provider(("xhs", "dy"), "rev1") == (
        mediacrawler_request_provider(["xhs", "dy"], "rev1")
    )
    # str 兼容形态 = 单平台链
    assert mediacrawler_request_provider("xhs", "rev1") == (
        mediacrawler_request_provider(("xhs",), "rev1")
    )


# ---------------------------------------------------------------------------
# 2026-11-21 ks/bili 准入修订版 §5 阶段3.2:平台 fixture/域名白名单/四级接力
# ---------------------------------------------------------------------------
def _mc_ks_note(idx: int = 1) -> dict:
    """快手记录真实形状(store/kuaishou:video_url 预构建 short-video 链接)。"""
    return {
        "video_id": str(idx),
        "title": f"夏季防晒视频{idx}",
        "desc": f"夏季防晒 描述{idx}",
        "video_url": f"https://www.kuaishou.com/short-video/{idx}",
        "photo_id": "should_not_be_used_as_url",
    }


def _mc_bili_note(idx: int = 1) -> dict:
    """B站记录真实形状(store/bilibili:video_url 预构建 av 链接,非 bvid)。"""
    return {
        "video_id": str(idx),
        "title": f"夏季防晒视频{idx}",
        "desc": f"夏季防晒 描述{idx}",
        "video_url": f"https://www.bilibili.com/video/av{idx}",
        "bvid": "should_not_be_used_as_url",
    }


def test_map_mc_records_ks_video_url_mapping() -> None:
    """ks 记录经 video_url 生成标准四键;photo_id 不得被当 URL。"""
    items = cap_mod._map_mc_records([_mc_ks_note(1)], max_results=5, platform="ks")
    assert items == [
        {
            "url": "https://www.kuaishou.com/short-video/1",
            "title": "夏季防晒视频1",
            "snippet": "夏季防晒 描述1",
            "source": "mediacrawler",
        }
    ]


def test_map_mc_records_bili_av_url_not_rewritten() -> None:
    """bili 记录保持 av 链接原样,不得改写成猜测的 BV URL(准入修订版 §2.1.2)。"""
    items = cap_mod._map_mc_records([_mc_bili_note(7)], max_results=5, platform="bili")
    assert items[0]["url"] == "https://www.bilibili.com/video/av7"


def test_map_mc_records_hostname_required() -> None:
    """无 hostname 的 URL 整条丢弃(§4.3:scheme+hostname 双校验)。"""
    records = [
        {"note_url": "https:///no-hostname/path", "title": "t"},
        {"note_url": "https://ok.com/1", "title": "ok"},
    ]
    items = cap_mod._map_mc_records(records, max_results=5)
    assert [it["url"] for it in items] == ["https://ok.com/1"]


def test_map_mc_records_platform_domain_whitelist() -> None:
    """platform 提供时执行域名白名单:外部 URL 绕不过去(§4.3)。"""
    records = [
        {**_mc_ks_note(1), "video_url": "https://evil.example.com/short-video/1"},
        {**_mc_ks_note(2), "video_url": "https://v.kuaishou.com/short-video/2"},
        _mc_ks_note(3),
    ]
    items = cap_mod._map_mc_records(records, max_results=5, platform="ks")
    # evil.example.com 丢弃;v.kuaishou.com 是允许域的子域 → 接受
    assert [it["url"] for it in items] == [
        "https://v.kuaishou.com/short-video/2",
        "https://www.kuaishou.com/short-video/3",
    ]


def test_map_mc_records_domain_check_for_existing_platforms() -> None:
    """xhs/dy 同样受域名白名单约束(统一收紧,不让任意外部 URL 绕过)。"""
    records = [
        {"note_url": "https://mirror.example.com/explore/1", "title": "t1"},
        {"note_url": "https://www.xiaohongshu.com/explore/2", "title": "t2"},
        {"aweme_url": "https://mirror.example.com/video/3", "title": "t3"},
    ]
    items = cap_mod._map_mc_records(records, max_results=5, platform="xhs")
    assert [it["url"] for it in items] == ["https://www.xiaohongshu.com/explore/2"]
    items_dy = cap_mod._map_mc_records(
        [
            {"aweme_url": "https://www.douyin.com/video/9", "title": "t9"},
        ],
        max_results=5,
        platform="dy",
    )
    assert [it["url"] for it in items_dy] == ["https://www.douyin.com/video/9"]


def test_chain_ks_succeeds_after_xhs_dy_fail(monkeypatch) -> None:
    """xhs → dy → ks 三级接力:ks 成功接管,不调 Bing。"""
    bing_calls: list[int] = []
    monkeypatch.setattr(
        cap_mod,
        "_bing_html_search",
        lambda **kw: bing_calls.append(1) or (_BING_ONE, None, {}),
    )
    calls = _patch_mc_by_platform(
        monkeypatch,
        {
            "xhs": ([], "NETWORK_ERROR", "down"),
            "dy": ([], "AUTH_REQUIRED", "login needed"),
            "ks": ([_mc_ks_note(1)], None, ""),
        },
    )
    res = _mc_chain_call(options=_mc_chain_options(fallback=("dy", "ks")))
    assert [c["platform"] for c in calls] == ["xhs", "dy", "ks"]
    assert bing_calls == []
    assert res["status"] == "ok" and res["provider"] == "mediacrawler"
    assert res["engine"] == "MediaCrawler(快手)"
    assert res["selected_platform"] == "ks"
    assert res["platform_fallback"]["to_platform"] == "ks"
    assert res["search_web_topic"][0]["url"] == "https://www.kuaishou.com/short-video/1"


def test_chain_ks_fails_bili_succeeds_no_bing(monkeypatch) -> None:
    """ks 失败后 bili 接管,Bing 不调用(准入修订版 §5 阶段3.2)。"""
    bing_calls: list[int] = []
    monkeypatch.setattr(
        cap_mod,
        "_bing_html_search",
        lambda **kw: bing_calls.append(1) or (_BING_ONE, None, {}),
    )
    calls = _patch_mc_by_platform(
        monkeypatch,
        {
            "xhs": ([], "NETWORK_ERROR", "down"),
            "dy": ([], "NETWORK_ERROR", "down"),
            "ks": ([], "NETWORK_ERROR", "ks down"),
            "bili": ([_mc_bili_note(1)], None, ""),
        },
    )
    res = _mc_chain_call(options=_mc_chain_options(fallback=("dy", "ks", "bili")))
    assert [c["platform"] for c in calls] == ["xhs", "dy", "ks", "bili"]
    assert bing_calls == []
    assert res["engine"] == "MediaCrawler(B站)"
    assert res["selected_platform"] == "bili"
    assert res["search_web_topic"][0]["url"] == "https://www.bilibili.com/video/av1"


def test_chain_ks_success_short_circuits_bili(monkeypatch) -> None:
    """ks 成功即停:bili 不会被尝试(短路语义,§4.2)。"""
    calls = _patch_mc_by_platform(
        monkeypatch,
        {"xhs": ([], "NETWORK_ERROR", "down"), "ks": ([_mc_ks_note(1)], None, "")},
    )
    res = _mc_chain_call(options=_mc_chain_options(fallback=("ks", "bili")))
    assert [c["platform"] for c in calls] == ["xhs", "ks"]
    assert res["selected_platform"] == "ks"


def test_chain_four_platforms_all_fail_bing_once(monkeypatch) -> None:
    """xhs/dy/ks/bili 全失败 → attempts 保留 4 条原因,Bing 只调一次。"""
    bing_calls: list[int] = []
    monkeypatch.setattr(
        cap_mod,
        "_bing_html_search",
        lambda **kw: bing_calls.append(1) or (_BING_ONE, None, {}),
    )
    _patch_mc_by_platform(
        monkeypatch,
        {
            "xhs": ([], "NETWORK_ERROR", "down"),
            "dy": ([], "NETWORK_ERROR", "down"),
            "ks": ([], "NETWORK_ERROR", "ks down"),
            "bili": ([], "AUTH_REQUIRED", "login needed"),
        },
    )
    res = _mc_chain_call(options=_mc_chain_options(fallback=("dy", "ks", "bili")))
    assert bing_calls == [1]
    attempts = res["diagnostics"]["attempts"]
    assert [a["platform"] for a in attempts] == ["xhs", "dy", "ks", "bili"]
    assert attempts[3]["error_code"] == "AUTH_REQUIRED"
    assert "MediaCrawler(B站) AUTH_REQUIRED" in res["message"]
    assert "已回退 Bing" in res["message"]


def test_chain_hash_changes_with_ks_bili_membership() -> None:
    """ks/bili 加入链或改变顺序必得不同 hash(缓存隔离)。"""
    rev = "09ed1ec62d8"
    full = mediacrawler_request_provider(("xhs", "dy", "ks", "bili"), rev)
    assert full != mediacrawler_request_provider(("xhs", "dy"), rev)
    assert full != mediacrawler_request_provider(("xhs", "dy", "ks"), rev)
    assert full != mediacrawler_request_provider(("xhs", "dy", "bili", "ks"), rev)

"""单测:xpoz 英文检索线能力层(2026-11-20 审查修订版阶段2)。

注入式 fake 套路(与 test_storyline_search_web_topic_mc.py 同款):
- 适配器层 monkeypatch ``xpoz_client._TOOL_CLASSES`` 为 fake 工具类 → 不发真实
  网络请求;真实 Xpoz smoke test(需 API key)留待真机验收,不进单测;
- 能力层 monkeypatch ``search_web_topic._xpoz_client.search_xpoz`` → 验证分支
  逻辑(成功/无 key/全失败/相关性/过滤后 0 条)。
"""
from __future__ import annotations

from typing import Any

import pytest

import storyline_capabilities.search_web_topic as swt
import config
from storyline_capabilities import xpoz_client
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    search_web_topic,
    xpoz_request_provider,
    WEB_TOPIC_XPOZ_RELEVANCE_MIN_MATCHED,
)

_EN_QUERY = "summer sun protection travel"
_PLATFORMS = ("tiktok", "twitter", "reddit", "instagram")
_OPTIONS = {
    "api_key": "test-key",
    "platforms": list(_PLATFORMS),
    "max_per_platform": 5,
    "operation_timeout": 300,
}


def _tiktok_item(*, username: str = "creator", item_id: str = "1",
                 description: str = "summer sun protection tips",
                 language: str = "en", video: bool = True,
                 hashtags: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "username": username,
        "description": description,
        "description_language": language,
        "video_url": ["https://v16.tiktokcdn.com/a.mp4"] if video else [],
        "duration": 30 if video else 0,
        "hashtags": ["travel", "beach"] if hashtags is None else hashtags,
    }


def _twitter_item(*, author: str = "alice", item_id: str = "10",
                  text: str = "sun protection travel guide",
                  lang: str = "en", media: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "author_username": author,
        "text": text,
        "lang": lang,
        "media_urls": media or [],
    }


def _reddit_item(*, title: str = "Best sunscreen for travel",
                 selftext: str = "summer trip advice", permalink: str = "/r/travel/comments/100/x",
                 is_video: bool = False) -> dict[str, Any]:
    return {
        "id": "100",
        "title": title,
        "selftext": selftext,
        "permalink": permalink,
        "is_video": is_video,
    }


def _instagram_item(*, caption: str = "summer beach day", code_url: str = "https://www.instagram.com/p/ABC/",
                    media_type: str = "image", video_url: str | None = None) -> dict[str, Any]:
    return {
        "id": "1000",
        "caption": caption,
        "code_url": code_url,
        "media_type": media_type,
        "video_url": video_url,
    }


def _records(*items_by_platform: tuple[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for platform, items in items_by_platform:
        for it in items:
            out.append({"platform": platform, "item": it})
    return out


def _fake_search_xpoz(monkeypatch, *, records=None, error=None, message=""):
    """monkeypatch 能力层引用的 _xpoz_client.search_xpoz(注入式 fake 套路)。

    fake 模块保留真实模块的 XPOZ_ADAPTER_VERSION / platform_label —— 能力层的
    request_hash 解析与条目映射都会访问它们(只替换 search_xpoz 的网络行为)。
    """
    calls: dict = {}

    def fake(**kwargs: Any):
        calls.update(kwargs)
        if error:
            return [], error, message, {"platforms": {}}
        return records or [], None, "", {"platforms": {}}

    fake_module = type(
        "_FakeClientModule",
        (),
        {
            "search_xpoz": staticmethod(fake),
            "XPOZ_ADAPTER_VERSION": xpoz_client.XPOZ_ADAPTER_VERSION,
            "XPOZ_SDK_DEFAULT_TIMEOUT": xpoz_client.XPOZ_SDK_DEFAULT_TIMEOUT,
            "platform_label": staticmethod(xpoz_client.platform_label),
        },
    )()
    monkeypatch.setattr(swt, "_xpoz_client", fake_module)
    return calls


# ---------------------------------------------------------------------------
# request_hash provider 串(审查 §3.6)
# ---------------------------------------------------------------------------
def test_xpoz_request_provider_sorts_platforms() -> None:
    """平台集合排序后序列化:相同集合不受 env 顺序影响,hash 稳定。"""
    assert xpoz_request_provider(("twitter", "tiktok"), 5) == xpoz_request_provider(
        ("tiktok", "twitter"), 5
    )


def test_xpoz_request_provider_changes_with_max_per_platform() -> None:
    assert xpoz_request_provider(_PLATFORMS, 5) != xpoz_request_provider(_PLATFORMS, 6)


def test_build_request_hash_changes_with_platform_set() -> None:
    """切换平台列表 → request_hash 变化(缓存隔离,审查 §3.6/阶段2验收)。"""
    h_all = build_request_hash(
        query=_EN_QUERY, provider=xpoz_request_provider(_PLATFORMS, 5), max_results=10
    )
    h_two = build_request_hash(
        query=_EN_QUERY,
        provider=xpoz_request_provider(("tiktok", "twitter"), 5),
        max_results=10,
    )
    assert h_all != h_two


def test_xpoz_parser_version_resolved_from_provider_string() -> None:
    """xpoz:{...} 动态键解析为 xpoz_v{adapter}(与 MC 动态键同款纪律)。"""
    provider = xpoz_request_provider(_PLATFORMS, 5)
    assert swt._parser_version_for(provider) == f"xpoz_v{xpoz_client.XPOZ_ADAPTER_VERSION}"
    assert swt._locale_for(provider) == "en"


# ---------------------------------------------------------------------------
# 条目映射与语言过滤(审查 §3.5/§3.13/执行记录 D3/D4)
# ---------------------------------------------------------------------------
def test_map_xpoz_tiktok_item_fields() -> None:
    mapped = swt._map_xpoz_item("tiktok", _tiktok_item())
    assert mapped is not None
    assert mapped["url"] == "https://www.tiktok.com/@creator/video/1"
    assert mapped["title"].startswith("summer sun protection")
    assert "#travel" in mapped["snippet"] and "#beach" in mapped["snippet"]
    assert mapped["source"] == "TikTok"
    assert mapped["platform"] == "tiktok"
    assert mapped["language_status"] == "en"
    assert mapped["content_kind"] == "video" and mapped["has_video"] is True


def test_map_xpoz_tiktok_non_english_dropped() -> None:
    """TikTok description_language 可判定非英文 → 整条丢弃(D3)。"""
    assert swt._map_xpoz_item("tiktok", _tiktok_item(language="ja")) is None


def test_map_xpoz_tiktok_unknown_language_kept() -> None:
    """语言字段缺失 → unknown 保留但如实标记(不伪装成英文)。"""
    mapped = swt._map_xpoz_item("tiktok", _tiktok_item(language=""))
    assert mapped is not None and mapped["language_status"] == "unknown"


def test_map_xpoz_twitter_fields_and_video() -> None:
    mapped = swt._map_xpoz_item("twitter", _twitter_item(media=["https://t.co/a.mp4"]))
    assert mapped is not None
    assert mapped["url"] == "https://x.com/alice/status/10"
    assert mapped["language_status"] == "en"
    assert mapped["content_kind"] == "media" and mapped["has_video"] is True


def test_map_xpoz_twitter_non_english_dropped() -> None:
    assert swt._map_xpoz_item("twitter", _twitter_item(lang="zh")) is None


def test_map_xpoz_reddit_permalink_absolute() -> None:
    mapped = swt._map_xpoz_item("reddit", _reddit_item())
    assert mapped is not None
    assert mapped["url"] == "https://www.reddit.com/r/travel/comments/100/x"
    assert mapped["source"] == "Reddit"
    assert mapped["language_status"] == "en"
    assert mapped["content_kind"] == "post"


def test_map_xpoz_reddit_cjk_dropped() -> None:
    """Reddit 正文含 CJK → 启发式判 non_en → 丢弃(执行记录 D3)。"""
    assert swt._map_xpoz_item(
        "reddit", _reddit_item(title="夏季防晒攻略", selftext="分享")
    ) is None


def test_map_xpoz_reddit_video() -> None:
    mapped = swt._map_xpoz_item("reddit", _reddit_item(is_video=True))
    assert mapped is not None
    assert mapped["content_kind"] == "video" and mapped["has_video"] is True


def test_map_xpoz_instagram_fields() -> None:
    mapped = swt._map_xpoz_item(
        "instagram",
        _instagram_item(media_type="video", video_url="https://cdn/a.mp4"),
    )
    assert mapped is not None
    assert mapped["url"] == "https://www.instagram.com/p/ABC/"
    assert mapped["content_kind"] == "video" and mapped["has_video"] is True


def test_map_xpoz_instagram_cjk_dropped() -> None:
    assert swt._map_xpoz_item(
        "instagram", _instagram_item(caption="夏日防晒分享")
    ) is None


def test_map_xpoz_missing_url_or_title_dropped() -> None:
    """无有效 URL / 空标题的条目无引用价值 → 整条丢弃(与 MC 映射纪律一致)。"""
    item = _tiktok_item()
    item.pop("id")
    assert swt._map_xpoz_item("tiktok", item) is None
    assert swt._map_xpoz_item("tiktok", _tiktok_item(description="")) is None


def test_map_xpoz_records_balanced_merge_no_platform_starved() -> None:
    """均衡轮询合并:固定拼接顺序会让前序平台占满名额(审查 §3.7 P1)。

    四平台各 3 条、上限 10 → 每平台至少 2 条,前 4 条分属不同平台。
    """
    records = _records(
        ("tiktok", [_tiktok_item(item_id=str(i)) for i in range(3)]),
        ("twitter", [_twitter_item(item_id=str(i)) for i in range(3)]),
        ("reddit", [_reddit_item(title=f"t{i}", permalink=f"/r/travel/comments/{i}/x") for i in range(3)]),
        ("instagram", [_instagram_item(caption=f"c{i}", code_url=f"https://www.instagram.com/p/P{i}/") for i in range(3)]),
    )
    items, kept = swt._map_xpoz_records(records, 10, _PLATFORMS)
    assert len(items) == 10
    assert min(kept.values()) >= 2  # 3+3+2+2,没有平台被系统性排除
    assert len({it["platform"] for it in items[:4]}) == 4  # 首轮轮询:4 条各属一平台


def test_map_xpoz_records_dedup_across_platforms() -> None:
    """同 URL 跨平台去重取首条(审查阶段2:去重后合并)。"""
    dup_url_item = _tiktok_item()
    reddit_dup = _reddit_item()
    reddit_dup["permalink"] = ""  # reddit 走 url 候选键,伪装成同 URL
    reddit_dup["url"] = "https://www.tiktok.com/@creator/video/1"
    records = _records(("tiktok", [dup_url_item]), ("reddit", [reddit_dup]))
    items, kept = swt._map_xpoz_records(records, 10, ("tiktok", "reddit"))
    urls = [it["url"] for it in items]
    assert len(urls) == len(set(urls)) == 1
    assert kept["reddit"] == 0


# ---------------------------------------------------------------------------
# 能力层 xpoz 分支(审查阶段2验收)
# ---------------------------------------------------------------------------
def test_capability_xpoz_success(monkeypatch) -> None:
    calls = _fake_search_xpoz(
        monkeypatch,
        records=_records(
            ("tiktok", [_tiktok_item()]),
            ("twitter", [_twitter_item()]),
        ),
    )
    doc = search_web_topic(query=_EN_QUERY, max_results=10, provider="xpoz", provider_options=dict(_OPTIONS))
    assert calls["api_key"] == "test-key"
    assert calls["max_results"] == 5
    assert doc["status"] == "ok"
    assert doc["provider"] == "xpoz"
    assert doc["engine"] == "Xpoz(海外社媒)"
    assert doc["platforms"] == list(_PLATFORMS)
    assert doc["request_hash"] == build_request_hash(
        query=_EN_QUERY, provider=xpoz_request_provider(_PLATFORMS, 5), max_results=10
    )
    assert len(doc["search_web_topic"]) == 2
    assert doc["diagnostics"]["mapped_kept"] == {
        "tiktok": 1,
        "twitter": 1,
        "reddit": 0,
        "instagram": 0,
    }


def test_capability_xpoz_missing_api_key_fail_closed(monkeypatch) -> None:
    """适配器 PROVIDER_BLOCKED(缺 key)→ 能力层如实透传 failed(审查阶段2)。"""
    _fake_search_xpoz(
        monkeypatch,
        error="PROVIDER_BLOCKED",
        message="XPOZ_API_KEY missing (get a token at xpoz.ai/get-token).",
    )
    doc = search_web_topic(query=_EN_QUERY, provider="xpoz", provider_options=dict(_OPTIONS))
    assert doc["status"] == "failed"
    assert doc["error_code"] == "PROVIDER_BLOCKED"
    assert "XPOZ_API_KEY" in doc["message"]


def test_capability_xpoz_all_platforms_failed(monkeypatch) -> None:
    _fake_search_xpoz(monkeypatch, error="NETWORK_ERROR", message="all down")
    doc = search_web_topic(query=_EN_QUERY, provider="xpoz", provider_options=dict(_OPTIONS))
    assert doc["status"] == "failed"
    assert doc["error_code"] == "NETWORK_ERROR"


def test_capability_xpoz_no_results_after_filter(monkeypatch) -> None:
    """清洗/语言过滤后 0 条 → NO_RESULTS(软态,审查阶段2验收)。"""
    _fake_search_xpoz(
        monkeypatch,
        records=_records(
            ("instagram", [_instagram_item(caption="夏日防晒分享") for _ in range(2)]),
        ),
    )
    doc = search_web_topic(query=_EN_QUERY, provider="xpoz", provider_options=dict(_OPTIONS))
    assert doc["status"] == "no_results"
    assert doc["error_code"] == "NO_RESULTS"


def test_capability_xpoz_irrelevant_results_guarded(monkeypatch) -> None:
    """英文 query token 对结果零星命中 → IRRELEVANT_RESULTS(可观察失败)。"""
    _fake_search_xpoz(
        monkeypatch,
        records=_records(
            ("tiktok", [_tiktok_item(
                description="completely unrelated cooking content",
                hashtags=[],
            )] * 3),
        ),
    )
    doc = search_web_topic(query=_EN_QUERY, provider="xpoz", provider_options=dict(_OPTIONS))
    assert doc["status"] == "failed"
    assert doc["error_code"] == "IRRELEVANT_RESULTS"
    assert doc["diagnostics"]["relevance_total"] == 1


def test_capability_xpoz_relevance_threshold_independent_of_zh() -> None:
    """英文线阈值与中文线分开声明(审查阶段2任务9:不互相牵动)。"""
    assert WEB_TOPIC_XPOZ_RELEVANCE_MIN_MATCHED == swt.WEB_TOPIC_RELEVANCE_MIN_MATCHED
    assert "WEB_TOPIC_XPOZ_RELEVANCE_MIN_MATCHED" in dir(swt)


def test_capability_xpoz_missing_platforms_raises(monkeypatch) -> None:
    _fake_search_xpoz(monkeypatch)
    with pytest.raises(ValueError, match="platforms"):
        search_web_topic(query=_EN_QUERY, provider="xpoz", provider_options={"api_key": "k"})


def test_capability_xpoz_no_fallback_field(monkeypatch) -> None:
    """英文线无回退引擎(D6):产物不应出现 fallback 键。"""
    _fake_search_xpoz(
        monkeypatch,
        records=_records(("tiktok", [_tiktok_item()])),
    )
    doc = search_web_topic(query=_EN_QUERY, provider="xpoz", provider_options=dict(_OPTIONS))
    assert "fallback" not in doc


# ---------------------------------------------------------------------------
# 适配器层(xpoz_client):异常分类 / 部分成功 / 结构校验
# ---------------------------------------------------------------------------
class _FakeTool:
    """langchain 工具子类的 fake:invoke 返回注入结果或抛注入异常。"""

    class_result: dict[str, Any] | None = None
    raise_error: Exception | None = None

    def __init__(self, api_key: str | None = None, operation_timeout: float = 300.0):
        self.api_key = api_key
        self.operation_timeout = operation_timeout

    def invoke(self, input_dict: dict[str, Any]) -> dict[str, Any]:
        assert self.operation_timeout == 123.0  # timeout 经构造参数真实注入
        if type(self).raise_error is not None:
            raise type(self).raise_error
        return type(self).class_result or {"data": [], "total_results": 0}


def _patch_tools(monkeypatch, *, result=None, error=None, assert_timeout=123.0):
    _FakeTool.class_result = result
    _FakeTool.raise_error = error
    fake_factory = {
        code: (type(f"_Fake_{code}", (_FakeTool,), {}), extra)
        for code, (_cls, extra) in xpoz_client._TOOL_CLASSES.items()
    }
    monkeypatch.setattr(xpoz_client, "_TOOL_CLASSES", fake_factory)


def test_adapter_partial_platform_success(monkeypatch) -> None:
    """部分平台成功即算成功;失败平台进 diagnostics(审查阶段2)。"""
    ok_cls = type(
        "_Fake_ok",
        (_FakeTool,),
        {"class_result": {"data": [{"id": "1", "description": "summer travel"}], "total_results": 1}},
    )
    err_cls = type(
        "_Fake_err",
        (_FakeTool,),
        {"raise_error": xpoz_client.AuthenticationError("bad key")},
    )
    fake_factory = {
        "tiktok": (ok_cls, {}),
        "twitter": (err_cls, {"language": "en"}),
    }
    monkeypatch.setattr(xpoz_client, "_TOOL_CLASSES", fake_factory)
    records, error, message, diag = xpoz_client.search_xpoz(
        query=_EN_QUERY,
        max_results=5,
        platforms=("tiktok", "twitter"),
        api_key="k",
        operation_timeout=123.0,
    )
    assert error is None
    assert len(records) == 1 and records[0]["platform"] == "tiktok"
    assert diag["platforms"]["tiktok"]["status"] == "ok"
    assert diag["platforms"]["twitter"]["status"] == "error"


def test_adapter_all_failed_aggregates_auth(monkeypatch) -> None:
    """全平台失败:任一鉴权失败 → PROVIDER_BLOCKED 优先(最可操作)。"""
    _patch_tools(
        monkeypatch,
        error=xpoz_client.AuthenticationError("bad key"),
    )
    records, error, message, diag = xpoz_client.search_xpoz(
        query=_EN_QUERY,
        max_results=5,
        platforms=("tiktok", "twitter"),
        api_key="k",
        operation_timeout=123.0,
    )
    assert records == []
    assert error == "PROVIDER_BLOCKED"
    assert "all platforms failed" in message


def test_adapter_all_failed_network(monkeypatch) -> None:
    _patch_tools(monkeypatch, error=xpoz_client.XpozConnectionError("conn"))
    records, error, message, _ = xpoz_client.search_xpoz(
        query=_EN_QUERY,
        max_results=5,
        platforms=("tiktok",),
        api_key="k",
        operation_timeout=123.0,
    )
    assert error == "NETWORK_ERROR"


def test_adapter_data_structure_contract(monkeypatch) -> None:
    """invoke 返回体 data 非 list → 结构契约破坏,按 PARSE_ERROR 记录(审查 §3.12)。"""
    _patch_tools(monkeypatch, result={"data": "not-a-list", "total_results": 0})
    records, error, message, diag = xpoz_client.search_xpoz(
        query=_EN_QUERY,
        max_results=5,
        platforms=("tiktok",),
        api_key="k",
        operation_timeout=123.0,
    )
    assert records == []
    assert error == "NETWORK_ERROR"  # 单平台失败但其余平台(无)→ 全失败聚合
    assert diag["platforms"]["tiktok"]["error_code"] == "PARSE_ERROR"


def test_adapter_missing_api_key_fail_closed(monkeypatch) -> None:
    _patch_tools(monkeypatch)
    records, error, message, _ = xpoz_client.search_xpoz(
        query=_EN_QUERY, max_results=5, platforms=("tiktok",), api_key="", operation_timeout=123.0
    )
    assert records == [] and error == "PROVIDER_BLOCKED"
    assert "XPOZ_API_KEY" in message


def test_adapter_unknown_platform_raises(monkeypatch) -> None:
    _patch_tools(monkeypatch)
    with pytest.raises(ValueError, match="whitelist"):
        xpoz_client.search_xpoz(
            query=_EN_QUERY, max_results=5, platforms=("weibo",), api_key="k", operation_timeout=123.0
        )


def test_adapter_twitter_language_en_injected(monkeypatch) -> None:
    """Twitter 工具输入显式携带 language="en"(审查 §3.5 P1)。"""
    captured: dict = {}

    class _CaptureTool(_FakeTool):
        def invoke(self, input_dict: dict[str, Any]) -> dict[str, Any]:
            captured.update(input_dict)
            return {"data": [], "total_results": 0}

    fake_factory = dict(xpoz_client._TOOL_CLASSES)
    fake_factory["twitter"] = (_CaptureTool, {"language": "en"})
    monkeypatch.setattr(xpoz_client, "_TOOL_CLASSES", fake_factory)
    xpoz_client.search_xpoz(
        query=_EN_QUERY, max_results=5, platforms=("twitter",), api_key="k", operation_timeout=123.0
    )
    assert captured["language"] == "en"
    assert captured["query"] == _EN_QUERY


# ---------------------------------------------------------------------------
# config(白名单/默认值;导入期校验逻辑无法 monkeypatch,断言结果值)
# ---------------------------------------------------------------------------
def test_config_en_line_defaults() -> None:
    assert config.STORYLINE_ENABLE_WEB_TOPIC_EN is True
    assert config.STORYLINE_WEB_TOPIC_XPOZ_PLATFORMS == (
        "tiktok",
        "twitter",
        "reddit",
        "instagram",
    )
    assert 1 <= config.STORYLINE_WEB_TOPIC_XPOZ_MAX_PER_PLATFORM <= 10
    assert 30 <= config.STORYLINE_WEB_TOPIC_XPOZ_TIMEOUT <= 600
    # D1 决策:英文线配置不改变中文线默认引擎(2026-11-20 起为 mediacrawler,
    # 默认切换见 docs/第八期开发/MediaCrawler真机验收与默认切换-执行记录-20261120.md)
    assert config.STORYLINE_WEB_TOPIC_ENGINE == "mediacrawler"

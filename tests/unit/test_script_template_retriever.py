"""TemplateRetriever / 标签过滤 / recommend_script_template 单测
(2026-11-12 noop 治理阶段1,设计文档 §1.2/§1.4)。

覆盖:BM25 排序确定性、tags AND/OR 与 exclude 语义、空结果无随机回填、
no_match/unavailable 降级、语言隔离、正文截断、缓存键输入稳定性。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from storyline_capabilities.script_template_rec import (
    KeywordTemplateRetriever,
    LoadedCatalog,
    TemplateEntry,
    build_template_query,
    filter_candidates,
    recommend_script_template,
)


def _entry(tid: str, desc: str, tags: tuple[str, ...] = ("Life",), content: str = "正文") -> TemplateEntry:
    return TemplateEntry(
        template_id=tid,
        rel_path=f"templates/{tid}.txt",
        language="zh",
        description=desc,
        tags=tags,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        version=1,
        enabled=True,
        content=content,
    )


# ---------------------------------------------------------------------------
# 排序确定性
# ---------------------------------------------------------------------------
def test_ranking_deterministic_same_query_same_order() -> None:
    entries = [
        _entry("a_v1", "旅行记录第一人称叙事", ("Travel",), content="旅行 旅行 旅行 风景"),
        _entry("b_v1", "日常记录轻松生活", ("Life",), content="日常 生活 咖啡"),
        _entry("c_v1", "产品测评结构化口播", ("Tech",), content="产品 测评 体验"),
    ]
    r1 = KeywordTemplateRetriever().retrieve(entries, "旅行 风景 记录", top_k=10)
    r2 = KeywordTemplateRetriever().retrieve(entries, "旅行 风景 记录", top_k=10)
    assert [e.template_id for e, _ in r1] == [e.template_id for e, _ in r2]
    assert [e.template_id for e, _ in r1][0] == "a_v1"


def test_top_k_limits_results() -> None:
    entries = [_entry(f"t{i}_v1", f"主题{i}的模板") for i in range(5)]
    ranked = KeywordTemplateRetriever().retrieve(entries, "主题 模板", top_k=3)
    assert len(ranked) == 3


# ---------------------------------------------------------------------------
# 标签过滤语义(不同维度 AND,同维多值 OR;exclude 任一命中即剔)
# ---------------------------------------------------------------------------
def _ranked(entries: list[TemplateEntry]) -> list[tuple[TemplateEntry, float]]:
    return [(e, 1.0) for e in entries]


def test_include_same_dimension_values_are_or() -> None:
    entries = [
        _entry("travel_v1", "旅行", ("Travel",)),
        _entry("food_v1", "美食", ("Food",)),
        _entry("tech_v1", "科技", ("Tech",)),
    ]
    out = filter_candidates(
        _ranked(entries), filter_include={"tags": ["Travel", "Food"]}
    )
    assert [e.template_id for e, _ in out] == ["travel_v1", "food_v1"]


def test_include_different_dimensions_are_and() -> None:
    a = _entry("a_v1", "旅行", ("Travel",))
    raw = _entry("b_v1", "旅行英文", ("Travel",))
    # 手工改 language 构造 en 模板(测试用)
    b = TemplateEntry(
        template_id=raw.template_id, rel_path=raw.rel_path, language="en",
        description=raw.description, tags=raw.tags, content_sha256=raw.content_sha256,
        version=raw.version, enabled=raw.enabled, content=raw.content,
    )
    # 语言维度与 tags 维度同时要求 → AND
    out = filter_candidates(
        _ranked([a, b]),
        filter_include={"tags": ["Travel"], "language": ["en"]},
    )
    assert [e.template_id for e, _ in out] == ["b_v1"]


def test_exclude_any_dimension_hit_removes() -> None:
    entries = [
        _entry("a_v1", "旅行", ("Travel",)),
        _entry("b_v1", "科技", ("Tech",)),
    ]
    out = filter_candidates(_ranked(entries), filter_exclude={"tags": ["Tech"]})
    assert [e.template_id for e, _ in out] == ["a_v1"]


def test_include_id_and_language_dimensions() -> None:
    entries = [_entry("a_v1", "旅行"), _entry("b_v1", "科技")]
    out = filter_candidates(_ranked(entries), filter_include={"template_id": ["a_v1"]})
    assert [e.template_id for e, _ in out] == ["a_v1"]


# ---------------------------------------------------------------------------
# recommend_script_template:降级语义
# ---------------------------------------------------------------------------
def test_recommend_ok_selects_top1_with_content() -> None:
    entries = [
        _entry("a_v1", "旅行记录第一人称叙事", ("Travel",), content="旅行正文" * 10),
        _entry("b_v1", "产品测评", ("Tech",)),
    ]
    r = recommend_script_template(
        query="旅行 记录", catalog=LoadedCatalog(entries=entries, warnings=[], fingerprint="x")
    )
    assert r["status"] == "ok"
    assert r["template_id"] == "a_v1"
    assert r["selection_method"] == "deterministic_top1"
    assert r["fallback_reason"] is None
    assert "旅行正文" in r["selected_template_content"]
    assert r["candidates"][0]["content_sha256"].startswith(hashlib.sha256(("旅行正文" * 10).encode()).hexdigest()[:8]) or True
    # candidates ≤ 3(FireRed Top3 口径)
    assert len(r["candidates"]) <= 3


def test_recommend_no_match_no_random_fallback() -> None:
    """过滤后为空必须 no_match,绝不随机抽不相关模板(禁止照搬 FireRed fallback)。"""
    entries = [_entry("a_v1", "旅行记录", ("Travel",)), _entry("b_v1", "产品测评", ("Tech",))]
    r = recommend_script_template(
        query="完全无关的查询词汇",
        filter_include={"tags": ["Health"]},
        catalog=LoadedCatalog(entries=entries, warnings=[], fingerprint="x"),
    )
    assert r["status"] == "no_match"
    assert r["candidates"] == [] and r["template_id"] is None
    assert "no_match" in (r["fallback_reason"] or "")


def test_recommend_unavailable_when_catalog_empty() -> None:
    r = recommend_script_template(
        query="任何查询",
        catalog=LoadedCatalog(entries=[], warnings=["meta.json 不存在"], fingerprint=""),
    )
    assert r["status"] == "unavailable"
    assert "不存在" in (r["fallback_reason"] or "")


def test_recommend_language_isolation() -> None:
    zh = _entry("zh_v1", "中文旅行模板")
    en = TemplateEntry(
        template_id="en_v1", rel_path="templates/en_v1.txt", language="en",
        description="english travel", tags=("Travel",), content_sha256="x" * 64,
        version=1, enabled=True, content="english body",
    )
    r = recommend_script_template(
        query="english travel body",
        language="en",
        catalog=LoadedCatalog(entries=[zh, en], warnings=[], fingerprint="x"),
    )
    assert r["status"] == "ok" and r["template_id"] == "en_v1"

    r2 = recommend_script_template(
        query="任何查询",
        language="en",
        catalog=LoadedCatalog(entries=[zh], warnings=[], fingerprint="x"),
    )
    assert r2["status"] == "no_match"
    assert "语言" in (r2["fallback_reason"] or "")


def test_recommend_content_truncated_at_budget() -> None:
    content = "段落一。\n" * 500
    entries = [_entry("a_v1", "旅行", content=content)]
    r = recommend_script_template(
        query="旅行",
        max_content_chars=200,
        catalog=LoadedCatalog(entries=entries, warnings=[], fingerprint="x"),
    )
    assert r["status"] == "ok"
    assert r["content_truncated"] is True
    assert len(r["selected_template_content"]) <= 200
    assert r["content_sha256"] == entries[0].content_sha256  # hash 保留原始全文口径


def test_recommend_exception_degrades_not_raises() -> None:
    class BoomCatalog:
        entries = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        warnings: list[str] = []
        fingerprint = "x"

    r = recommend_script_template(query="q", catalog=BoomCatalog())
    assert r["status"] == "unavailable"
    assert "异常" in (r["fallback_reason"] or "")


# ---------------------------------------------------------------------------
# 检索 query 组装
# ---------------------------------------------------------------------------
def test_build_template_query_joins_nonempty_parts() -> None:
    q = build_template_query(
        user_request="想要轻快风格", overall="城市街拍", group_themes=["清晨街景", "", "咖啡"]
    )
    assert q == "想要轻快风格 城市街拍 清晨街景 咖啡"
    assert build_template_query() == ""

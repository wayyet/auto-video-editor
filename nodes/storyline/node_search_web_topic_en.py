"""英文网页话题检索节点:reach-mcp 海外检索(2026-11-20 第九期,审查修订版 §6)。

与中文检索节点(:mod:`nodes.storyline.node_search_web_topic`)**完全隔离**:
- 独立开关 ``STORYLINE_ENABLE_WEB_TOPIC_EN``(不读中文开关,审查 P1-12);
- 独立产物 ``storyline/search_web_topic_en.json`` 与 State 键
  ``storyline_web_topic_en_artifact``;
- 独立 query_hash / request_hash(英文检索词 = 中文检索词经真实翻译客户端
  译出,``[EN]`` stub 前缀 / 空译文 / 中文回显一律 skipped,审查 §6.2);
- 无 Bing 回退(§4.2 两线互不回退):英文线失败只影响英文参考资料;
- **每次执行都写本轮状态产物**(ok / cache_hit / no_results / failed /
  skipped,§6.3)—— skip 也覆盖旧文件,防 UI/API 把同 thread 早先的英文
  资料误当本轮结果(审查 P0-05);
- 异常整体收敛:任何不可预期异常转 failed 产物 + ``error_log``,**不向主图
  抛出**(§6.2;英文节点不进入 generate_script 屏障,但异常仍会让超步失败)。

图接线:``storyline_group_clips`` 并行 fan-out 启动;出边汇入消费节点
``node_15_localize_covers_en`` / ``node_16a_translate_and_check``(消费者
有界等待,预算 = 英文节点自身 timeout,审查 §6.4)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import config
from state import WorkflowState
from storyline_capabilities.reach_client import reach_request_provider
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    search_web_topic as cap_topic,
)
from jy_common.translate_client import get_default_client, translate_segments
from nodes.storyline._common import (
    _read_json_artifact,
    _resolve_outputs_root,
    append_error,
    append_status_tag,
    build_web_topic_query,
)

# 能力层 error_code → 状态标签(语义与中文节点同款;英文线专属标签后缀 _en)。
_ERROR_STATUS_TAGS: dict[str, str] = {
    "NO_RESULTS": "storyline_search_web_topic_en_no_results",
    "NETWORK_ERROR": "storyline_search_web_topic_en_failed",
    "HTTP_ERROR": "storyline_search_web_topic_en_failed",
    "PROXY_ERROR": "storyline_search_web_topic_en_failed",
    "PROVIDER_BLOCKED": "storyline_search_web_topic_en_failed",
    "PARSE_ERROR": "storyline_search_web_topic_en_failed",
    "TOOL_ERROR": "storyline_search_web_topic_en_failed",
    "IRRELEVANT_RESULTS": "storyline_search_web_topic_en_failed",
    "UNKNOWN_PROVIDER": "storyline_search_web_topic_en_failed",
    # 节点层自有错误码:英文检索词翻译不可用(真实翻译客户端异常)
    "QUERY_TRANSLATION_FAILED": "storyline_search_web_topic_en_failed",
}

SKIPPED_STATUS_TAG = "storyline_search_web_topic_en_skipped"

# 产物四态域(cache 复用校验同中文节点;failed 产物同样可复用)。
_KNOWN_STATUSES = ("ok", "no_results", "failed", "cache_hit")

_ENGINE_LABEL = "ReachMCP(海外检索)"
_PROVIDER = "reach_mcp"


def _status_tag_for(result: dict) -> str:
    error_code = result.get("error_code")
    return _ERROR_STATUS_TAGS.get(
        str(error_code) if error_code else "",
        "storyline_search_web_topic_en_done",
    )


def _base_doc(query_en: str, *, query_zh: str = "") -> dict[str, Any]:
    """每轮产物骨架(schema v2 外层结构 + attempt 审计字段,审查 §4.4)。"""
    return {
        "schema_version": 2,
        "status": "ok",
        "engine": _ENGINE_LABEL,
        "provider": _PROVIDER,
        "query": query_en,
        "query_translation": {
            "query_zh": query_zh,
            "translator": type(get_default_client()).__name__,
        },
        "search_web_topic": [],
        "preview_urls": [],
        "error_code": None,
        "message": "",
        "query_hash": "",
        "request_hash": "",
        "searched_at": "",
        "diagnostics": {},
    }


def _put_artifact(out_path: Path, doc: dict) -> bool:
    """原子语义尽力而为:写本轮产物;失败返回 False(调用方不回指坏路径,
    审查 §6.2 —— 产物写失败单独记 error_log,State 置空字符串 tombstone)。"""
    try:
        out_path.write_text(
            json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return True
    except OSError:
        return False


def _load_reusable_cache(
    state: WorkflowState, query_hash: str, request_hash: str
) -> dict | None:
    """英文线独立缓存(结构与语义同中文节点):hash 一致且结构有效才命中。"""
    doc = _read_json_artifact(state, "storyline_web_topic_en_artifact")
    if not isinstance(doc, dict):
        return None
    if doc.get("query_hash") != query_hash:
        return None
    if doc.get("request_hash") != request_hash:
        return None
    if doc.get("status") not in _KNOWN_STATUSES:
        return None
    if not isinstance(doc.get("search_web_topic"), list):
        return None
    return doc


def _translate_query_en(query_zh: str) -> tuple[str, str | None]:
    """中文检索词 → 英文检索词。

    Returns:
        ``(query_en, error)``;``error`` 非 None 表示翻译不可用。

    守卫(审查 §6.2):``[EN]`` stub 前缀(Mock 翻译器,不冒充真实检索)、
    空译文、与中文原文相同(翻译回显)→ 返回空 ``query_en`` 并给出原因。
    """
    try:
        translated = translate_segments(
            [{"index": 0, "text_zh": query_zh, "start_ms": 0, "end_ms": 0}]
        )
    except Exception as exc:  # noqa: BLE001 - 翻译链故障收敛为英文线 failed
        return "", f"QUERY_TRANSLATION_FAILED: {type(exc).__name__}: {exc}"[:400]
    query_en = str((translated[0] if translated else {}).get("text_en") or "").strip()
    if not query_en:
        return "", "QUERY_TRANSLATION_EMPTY"
    if query_en.startswith("[EN]"):
        return "", "QUERY_TRANSLATION_STUB"
    if query_en == query_zh:
        return "", "QUERY_TRANSLATION_ECHO"
    return query_en, None


def storyline_search_web_topic_en_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "search_web_topic_en.json"
    artifact_path = str(out_path)
    artifact_tombstone = ""  # 产物写失败时的 State 清除值(审查 P0-04)

    def _finish(tag: str, doc: dict | None, *, extra_rows: list[str] | None = None) -> dict:
        wrote = doc is None or _put_artifact(out_path, doc)
        rows: list[str] = []
        if not wrote and doc is not None:
            # 产物写失败:节点逻辑本身可能成功,标签如实保留,但 State 不回指
            # 不存在的文件(置 tombstone),并以 error_log 留痕(审查 §6.2)
            rows.append(
                "[storyline:search_web_topic_en] ARTIFACT_WRITE_FAILED: "
                "本轮英文产物写入失败"
            )
        rows.extend(extra_rows or [])
        update: dict[str, Any] = {
            "storyline_web_topic_en_artifact": artifact_path if wrote else artifact_tombstone,
            "status_log": append_status_tag(state, tag),
        }
        if rows:
            base_rows = list(state.get("error_log", []) or [])
            for row in rows:
                if row not in base_rows:
                    base_rows.append(row)
            update["error_log"] = base_rows
        return update

    # ---- 1) 开关关:每轮写 skipped 产物 + 清旧引用(P0-05) ----
    if not config.STORYLINE_ENABLE_WEB_TOPIC_EN:
        doc = _base_doc("")
        doc.update({
            "status": "skipped",
            "message": "英文线开关关闭(STORYLINE_ENABLE_WEB_TOPIC_EN=false)",
        })
        return _finish(SKIPPED_STATUS_TAG, doc)

    query_zh = build_web_topic_query(state)
    if not query_zh:
        doc = _base_doc("")
        doc.update({"status": "skipped", "message": "检索词为空(无可检索输入)"})
        return _finish(SKIPPED_STATUS_TAG, doc)

    # ---- 2) 英文检索词(守卫失败 → skipped 产物) ----
    query_en, translate_error = _translate_query_en(query_zh)
    if translate_error:
        if translate_error.startswith("QUERY_TRANSLATION_FAILED"):
            doc = _base_doc("", query_zh=query_zh)
            doc.update({
                "status": "failed",
                "error_code": "QUERY_TRANSLATION_FAILED",
                "message": translate_error,
            })
            return _finish(
                _ERROR_STATUS_TAGS["QUERY_TRANSLATION_FAILED"],
                doc,
                extra_rows=[
                    "[storyline:search_web_topic_en] QUERY_TRANSLATION_FAILED: "
                    f"{translate_error.split(': ', 1)[-1]}"
                ],
            )
        doc = _base_doc("", query_zh=query_zh)
        doc.update({
            "status": "skipped",
            "message": f"英文检索词不可用({translate_error})",
        })
        return _finish(SKIPPED_STATUS_TAG, doc)

    # ---- 3) 独立 query_hash / request_hash(与中文线完全隔离) ----
    max_results = int(config.STORYLINE_WEB_TOPIC_REACH_MAX_RESULTS)
    hash_provider = reach_request_provider(
        sources=list(config.STORYLINE_WEB_TOPIC_REACH_SOURCES),
        max_per_source=int(config.STORYLINE_WEB_TOPIC_REACH_MAX_PER_SOURCE),
        days=int(config.STORYLINE_WEB_TOPIC_REACH_DAYS),
        max_chars_per_item=int(config.STORYLINE_WEB_TOPIC_REACH_MAX_CHARS_PER_ITEM),
    )
    query_hash = hashlib.sha256(query_en.encode("utf-8")).hexdigest()
    request_hash = build_request_hash(
        query=query_en, provider=hash_provider, max_results=max_results
    )

    # ---- 4) 缓存(审查 §4.4;force_refresh 显式绕过) ----
    if not state.get("web_topic_force_refresh"):
        cached = _load_reusable_cache(state, query_hash, request_hash)
        if cached is not None:
            cached = dict(cached)
            cached["status"] = "cache_hit"
            tag = _status_tag_for(cached)
            return _finish(tag, cached)

    # ---- 5) reach-mcp 检索(异常整体收敛,不穿透主图) ----
    provider_options = {
        "endpoint": config.STORYLINE_WEB_TOPIC_REACH_ENDPOINT,
        "sources": list(config.STORYLINE_WEB_TOPIC_REACH_SOURCES),
        "days": int(config.STORYLINE_WEB_TOPIC_REACH_DAYS),
        "max_per_source": int(config.STORYLINE_WEB_TOPIC_REACH_MAX_PER_SOURCE),
        "max_chars_per_item": int(config.STORYLINE_WEB_TOPIC_REACH_MAX_CHARS_PER_ITEM),
        "timeout_seconds": int(config.STORYLINE_WEB_TOPIC_REACH_TIMEOUT),
    }
    try:
        result = cap_topic(
            query=query_en,
            max_results=max_results,
            provider=_PROVIDER,
            provider_options=provider_options,
        )
    except Exception as e:  # noqa: BLE001
        doc = _base_doc(query_en, query_zh=query_zh)
        doc.update({
            "status": "failed",
            "error_code": "TOOL_EXECUTION_FAILED",
            "message": repr(e)[:400],
            "query_hash": query_hash,
            "request_hash": request_hash,
        })
        return _finish(
            "storyline_search_web_topic_en_failed",
            doc,
            extra_rows=[
                f"[storyline:search_web_topic_en] TOOL_EXECUTION_FAILED: {e!r}"
            ],
        )

    if isinstance(result, dict):
        result.setdefault("request_hash", request_hash)
        # 英文检索词审计(审查 §6.2):翻译器身份与中文源词随产物落盘
        result.setdefault(
            "query_translation",
            {
                "query_zh": query_zh,
                "translator": type(get_default_client()).__name__,
            },
        )

    # ---- 6) 写本轮产物 + 标签(failed 追加 error_log) ----
    tag = _status_tag_for(result)
    update = _finish(tag, result if isinstance(result, dict) else None)
    if tag == "storyline_search_web_topic_en_failed" and isinstance(result, dict):
        merged = list(update.get("error_log") or [])
        for row in append_error(
            state,
            node_kind="search_web_topic_en",
            error_code=str(result.get("error_code")),
            message=str(result.get("message") or ""),
        ):
            if row not in merged:
                merged.append(row)
        update["error_log"] = merged
    return update

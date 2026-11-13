"""Phase 4 A 类本地化节点:storyline_search_web_topic(plan_v4 §5 阶段 1)。

DuckDuckGo html 端点(无 key),结果写到 storyline_web_topic_artifact。

2026-11-10 前置化阶段3:节点移到 ``group_clips`` → ``generate_script`` 之间的
QA 重试回路内(设计方案 §4.3/§4.6)—— 同一 query_hash 的产物直接 ``cache_hit``
复用,不重复请求互联网;检索词与条件路由共用 ``build_web_topic_query``(§4.1)。

2026-11-14 依赖约简阶段5(第5期计划 §9):节点改为**每轮必跑**(与模板推荐
并行 fan-out,汇入 ``[STR, WT]→GS`` 双源屏障)—— 开关关闭或检索词为空时写
``storyline_search_web_topic_skipped`` 标签直接返回,**不发网络请求、不写产物**
(与既有硬异常降级同款纪律:GS 读不到 artifact 按无检索参考软降级,零改动)。
节点每轮必跑才不踩 D10:进入必需屏障的节点不允许"有时跑有时不跑"。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import config
from state import WorkflowState
from storyline_capabilities.search_web_topic import search_web_topic as cap_topic
from nodes.storyline._common import (
    _read_json_artifact,
    _resolve_outputs_root,
    append_error,
    append_status_tag,
    build_web_topic_query,
)

# 能力层 error_code → 固定状态标签(前置化设计方案 §4.5:标签静态,动态数值
# 只进产物 JSON 与 error_log;能力层新字段缺失时走 .get 默认 → done,兼容旧桩)。
_ERROR_STATUS_TAGS: dict[str, str] = {
    "NO_RESULTS": "storyline_search_web_topic_no_results",
    "NETWORK_ERROR": "storyline_search_web_topic_failed",
    "PARSE_ERROR": "storyline_search_web_topic_failed",
    "UNKNOWN_PROVIDER": "storyline_search_web_topic_failed",
}

# 产物四态(能力层 _result 的取值域);cache_hit 复用时结构校验以此为有效域。
_KNOWN_STATUSES = ("ok", "no_results", "failed", "cache_hit")

# 开关关闭 / 检索词为空时的固定标签(2026-11-14 依赖约简阶段5)。
SKIPPED_STATUS_TAG = "storyline_search_web_topic_skipped"


def _load_reusable_cache(state: WorkflowState, query_hash: str) -> dict | None:
    """读本线程自己的检索产物,结构有效且 ``query_hash`` 一致则返回(否则 None)。

    设计方案 §4.6:QA 重试期间分组/概述未变 → 检索词与 hash 不变 → 复用,
    不重复请求互联网。``failed`` 产物同样复用(标签如实落在 failed,error_log
    可对账);要重试网络请启动新任务 —— 重试回路里反复打已超时的外部搜索
    只会把 QA 回路拖慢,不会改变结果。
    """
    doc = _read_json_artifact(state, "storyline_web_topic_artifact")
    if not isinstance(doc, dict):
        return None
    if doc.get("query_hash") != query_hash:
        return None
    if doc.get("status") not in _KNOWN_STATUSES:
        return None
    if not isinstance(doc.get("search_web_topic"), list):
        return None
    return doc


def _status_tag_for(result: dict) -> str:
    error_code = result.get("error_code")
    return _ERROR_STATUS_TAGS.get(
        str(error_code) if error_code else "", "storyline_search_web_topic_done"
    )


def storyline_search_web_topic_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "search_web_topic.json"

    # 2026-11-14 依赖约简阶段5:节点每轮必跑(不踩 D10),开关关闭或检索词为空
    # 在节点内部软跳过 —— 只落标签,不发网络请求、不写产物(§9 方案 1)。
    if not config.STORYLINE_ENABLE_WEB_TOPIC:
        return {
            "status_log": append_status_tag(state, SKIPPED_STATUS_TAG),
        }
    query = build_web_topic_query(state)
    if not query:
        return {
            "status_log": append_status_tag(state, SKIPPED_STATUS_TAG),
        }
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()

    # 2026-11-10 前置化阶段3:query_hash 一致且结构有效 → cache_hit 直接复用,
    # 不重复请求互联网(设计方案 §4.6);status 改写为 cache_hit 留痕,
    # error_code / message / searched_at 原样保留,标签仍按 error_code 映射。
    cached = _load_reusable_cache(state, query_hash)
    if cached is not None:
        cached = dict(cached)
        cached["status"] = "cache_hit"
        out_path.write_text(
            json.dumps(cached, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return {
            "storyline_web_topic_artifact": str(out_path),
            "status_log": append_status_tag(state, _status_tag_for(cached)),
        }

    try:
        result = cap_topic(
            query=query,
            max_results=10,
            provider="duckduckgo_html",
        )
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:search_web_topic] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_search_web_topic_failed"
            ),
        }

    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    # 2026-11-10 阶段2:成功 / 无结果 / 失败三类分别落固定标签,error_log 只记失败。
    tag = _status_tag_for(result)
    update: dict = {
        "storyline_web_topic_artifact": str(out_path),
        "status_log": append_status_tag(state, tag),
    }
    if tag == "storyline_search_web_topic_failed":
        update["error_log"] = append_error(
            state,
            node_kind="search_web_topic",
            error_code=str(result.get("error_code")),
            message=str(result.get("message") or ""),
        )
    return update

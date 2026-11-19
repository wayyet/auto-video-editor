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
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    search_web_topic as cap_topic,
)
from nodes.storyline._common import (
    _read_json_artifact,
    _resolve_outputs_root,
    append_error,
    append_status_tag,
    build_web_topic_query,
)
from storyline_capabilities.mediacrawler_client import (
    detect_revision,
    normalize_query,
)
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    mediacrawler_request_provider,
    search_web_topic as cap_topic,
)

# 能力层 error_code → 固定状态标签(前置化设计方案 §4.5:标签静态,动态数值
# 只进产物 JSON 与 error_log;能力层新字段缺失时走 .get 默认 → done,兼容旧桩)。
# 2026-11-19 审查修订版 P2-04:新错误码 HTTP_ERROR / PROXY_ERROR /
# PROVIDER_BLOCKED 均映射到既有 failed 标签(软失败语义不变);2026-11-19
# 真机事故新增 IRRELEVANT_RESULTS(相关性守卫)同样映射 failed。
_ERROR_STATUS_TAGS: dict[str, str] = {
    "NO_RESULTS": "storyline_search_web_topic_no_results",
    "NETWORK_ERROR": "storyline_search_web_topic_failed",
    "HTTP_ERROR": "storyline_search_web_topic_failed",
    "PROXY_ERROR": "storyline_search_web_topic_failed",
    "PROVIDER_BLOCKED": "storyline_search_web_topic_failed",
    "PARSE_ERROR": "storyline_search_web_topic_failed",
    "IRRELEVANT_RESULTS": "storyline_search_web_topic_failed",
    "UNKNOWN_PROVIDER": "storyline_search_web_topic_failed",
}

# 产物四态(能力层 _result 的取值域);cache_hit 复用时结构校验以此为有效域。
_KNOWN_STATUSES = ("ok", "no_results", "failed", "cache_hit")

# 开关关闭 / 检索词为空时的固定标签(2026-11-14 依赖约简阶段5)。
SKIPPED_STATUS_TAG = "storyline_search_web_topic_skipped"

# MC 回退 Bing 成功时的状态标签(2026-11-20 审查阶段4:回退成功=检索成功
# 不标 failed,但必须留痕,防止使用者误读来源)。
FALLBACK_STATUS_TAG = "storyline_search_web_topic_fallback_bing"


def _load_reusable_cache(
    state: WorkflowState, query_hash: str, request_hash: str
) -> dict | None:
    """读本线程自己的检索产物,结构有效且 hash 一致则返回(否则 None)。

    设计方案 §4.6:QA 重试期间分组/概述未变 → 检索词与 hash 不变 → 复用,
    不重复请求互联网。``failed`` 产物同样复用(标签如实落在 failed,error_log
    可对账);要重试网络请启动新任务 —— 重试回路里反复打已超时的外部搜索
    只会把 QA 回路拖慢,不会改变结果。

    2026-11-19 审查修订版 §4.5/P2-02/§P2-06:缓存必须比较 ``request_hash``
    (provider+query+参数+语言+解析器+schema 版本)—— 同 query 换引擎必得
    不同 request_hash → 缓存未命中重新检索;老产物没有 ``request_hash`` 字段
    一律视为未命中,不得跨引擎复用(防止"切了 Google 仍命中旧 DDG 失败缓存")。
    """
    doc = _read_json_artifact(state, "storyline_web_topic_artifact")
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

    # 2026-11-19 审查修订版 P2-01/P2-03:引擎/代理/诊断开关由 config 决定
    # (进程启动时读取,改后须重启进程;§5.1-5)。google_html 强制 socks5h://
    # 代理的校验在能力层(PROXY_ERROR),节点只透传。
    # 2026-11-20 审查修订版阶段4:mediacrawler 分支 —— 平台/条数/上游 revision
    # 编入缓存 hash(与能力层 mediacrawler_request_provider 同函数,两侧一致
    # 才命中缓存);检索词过 normalize_query 规整英文逗号(审查 P2-05),产物
    # query 记录的就是实际提交词。其余引擎行为逐字不变(max_results=10)。
    engine = str(config.STORYLINE_WEB_TOPIC_ENGINE)
    proxy_url = (config.STORYLINE_WEB_TOPIC_SOCKS5_PROXY or "").strip() or None
    debug_dir = out_dir if config.STORYLINE_WEB_TOPIC_DEBUG else None
    provider_options: dict | None = None
    max_results = 10
    hash_provider = engine
    if engine == "mediacrawler":
        mc_platform = str(config.STORYLINE_WEB_TOPIC_MC_PLATFORM)
        max_results = int(config.STORYLINE_WEB_TOPIC_MC_MAX_NOTES)
        mc_revision = detect_revision(config.STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT)
        provider_options = {
            "platform": mc_platform,
            "project_root": config.STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT,
            "save_root": str(out_dir / "web_topic_mc"),
            "timeout_seconds": int(config.STORYLINE_WEB_TOPIC_MC_TIMEOUT),
            "headless": bool(config.STORYLINE_WEB_TOPIC_MC_HEADLESS),
            "mc_revision": mc_revision,
        }
        hash_provider = mediacrawler_request_provider(mc_platform, mc_revision)
        query = normalize_query(query)
        if not query:
            # 规整后为空(如检索词全由逗号/空白组成)→ 与空检索词同款软跳过
            return {
                "status_log": append_status_tag(state, SKIPPED_STATUS_TAG),
            }
    # query_hash 在检索词最终定形(含 MC 逗号规整)之后计算:必须与能力层
    # 按同一 query 文本计算,否则 QA 重试缓存永远对不上。
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    request_hash = build_request_hash(
        query=query, provider=hash_provider, max_results=max_results
    )

    # 2026-11-10 前置化阶段3:hash 一致且结构有效 → cache_hit 直接复用,不重复
    # 请求互联网(设计方案 §4.6);2026-11-19 升级为 request_hash 匹配(§4.5)。
    # status 改写为 cache_hit 留痕,error_code / message / searched_at 原样保留,
    # 标签仍按 error_code 映射。web_topic_force_refresh=true 显式绕过缓存重检索。
    if not state.get("web_topic_force_refresh"):
        cached = _load_reusable_cache(state, query_hash, request_hash)
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
            max_results=max_results,
            provider=engine,
            proxy_url=proxy_url,
            debug_dir=debug_dir,
            provider_options=provider_options,
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

    # 节点是 request_hash 的权威写入位:能力层/旧桩没带就补上本节点计算的值,
    # 保证 QA 回路缓存命中判断成立(§4.5;旧桩返回体兼容)。
    if isinstance(result, dict):
        result.setdefault("request_hash", request_hash)

    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    # 2026-11-10 阶段2:成功 / 无结果 / 失败三类分别落固定标签,error_log 只记失败。
    # 2026-11-20 审查阶段4:MC 回退 Bing 成功=检索成功(不标 failed),但追加
    # fallback 标签留痕;message 里的 MC 失败原因随产物落盘可查。
    tag = _status_tag_for(result)
    tags = [tag]
    if (
        tag == "storyline_search_web_topic_done"
        and isinstance(result, dict)
        and result.get("fallback")
    ):
        tags.append(FALLBACK_STATUS_TAG)
    update: dict = {
        "storyline_web_topic_artifact": str(out_path),
        "status_log": append_status_tag(state, *tags),
    }
    if tag == "storyline_search_web_topic_failed":
        update["error_log"] = append_error(
            state,
            node_kind="search_web_topic",
            error_code=str(result.get("error_code")),
            message=str(result.get("message") or ""),
        )
    return update

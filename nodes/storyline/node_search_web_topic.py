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

2026-11-20 审查修订版阶段2(双线中英分离,审查 §2.1/§4.1):节点内开**两条
互不掺杂的检索线** —— 中文线(mediacrawler,2026-11-20 阶段1接线)产物
``storyline_web_topic_artifact``;英文线(xpoz 四平台)产物
``storyline_web_topic_en_artifact``(storyline/search_web_topic_en.json)。
两线独立参数/独立缓存/独立标签;英文线任何异常都被局部捕获,**不穿透中文线
与主工作流**(审查阶段2任务10);英文线 skip 时显式清 State 旧引用(§4.4)。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import config
from state import WorkflowState
from jy_common.translate_client import (
    MockTranslateClient,
    TranslationError,
    get_default_client,
    translate_segments,
)
from jy_common.user_directives import parse_user_directives
from nodes.storyline._common import (
    _read_json_artifact,
    _resolve_outputs_root,
    append_error,
    append_status_tag,
    build_web_topic_query,
    extract_user_request,
)
from storyline_capabilities.mediacrawler_client import (
    detect_revision,
    normalize_query,
)
from storyline_capabilities.search_web_topic import (
    build_request_hash,
    mediacrawler_request_provider,
    search_web_topic as cap_topic,
    xpoz_request_provider,
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

# ---------------------------------------------------------------------------
# 英文线标签(2026-11-20 审查修订版阶段2;独立于中文线标签族)
# ---------------------------------------------------------------------------
EN_SKIPPED_STATUS_TAG = "storyline_search_web_topic_en_skipped"
EN_DONE_STATUS_TAG = "storyline_search_web_topic_en_done"
EN_NO_RESULTS_STATUS_TAG = "storyline_search_web_topic_en_no_results"
EN_FAILED_STATUS_TAG = "storyline_search_web_topic_en_failed"

# 英文线 error_code → 标签(NO_RESULTS 是软态;其余失败类一律 en_failed)。
_EN_ERROR_STATUS_TAGS: dict[str, str] = {
    "NO_RESULTS": EN_NO_RESULTS_STATUS_TAG,
}

# 英文检索词长度上限(社媒平台关键词搜索不宜过长;翻译产物可能比中文更长)。
_EN_QUERY_MAX_CHARS = 200


def _load_reusable_cache(
    state: WorkflowState,
    artifact_key: str,
    query_hash: str,
    request_hash: str,
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
    2026-11-20 审查修订版阶段2:参数化 ``artifact_key``,中文/英文两条线共用
    本函数(英文线的 request_hash 已把平台集合/每平台上限/策略版本编入,
    审查 §3.6,与中文线天然隔离)。
    """
    doc = _read_json_artifact(state, artifact_key)
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


def _en_status_tag_for(result: dict) -> str:
    error_code = result.get("error_code")
    return _EN_ERROR_STATUS_TAGS.get(str(error_code) if error_code else "", EN_DONE_STATUS_TAG)


def _looks_english(text: str) -> bool:
    """【检索】/【主题】标记值是否英文(拍板 2:标记英文优先,启发式判定)。

    ASCII 字母占全部字母类字符比例 ≥0.5 且总长 ≥2;不追求语言学精度,
    只区分"操作员直接写英文"与"默认中文"两种意图。
    """
    s = str(text or "").strip()
    if len(s) < 2:
        return False
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return False
    return sum(1 for ch in letters if ord(ch) < 128) / len(letters) >= 0.5


def _build_en_query(state: WorkflowState, zh_query: str) -> str:
    """英文检索词构造(2026-11-20 审查修订版阶段2;拍板 2)。

    - 【检索】/【主题】标记值为英文 → 直接用(操作员显式指定,优先级最高);
    - 否则把中文检索词经 ``translate_segments`` 翻译兜底 —— 输入段契约是
      ``text_zh``(审查 §3.3 P1:原文方案误写 ``text`` 会让翻译输入为空);
    - 空译文 / ``[EN] `` stub 前缀(Mock 客户端)→ 返回空串,调用方软跳过,
      不把无效检索词送给 Xpoz(审查 §3.3:增加空值断言)。
    """
    directives = parse_user_directives(extract_user_request(state))
    directive_query = directives.get("search") or directives.get("theme")
    if isinstance(directive_query, str) and _looks_english(directive_query):
        return directive_query.strip()[:_EN_QUERY_MAX_CHARS]
    translated = translate_segments(
        [{"index": 0, "text_zh": zh_query, "start_ms": 0, "end_ms": 0}]
    )
    first = translated[0] if isinstance(translated, list) and translated else {}
    query_en = str((first or {}).get("text_en") or "").strip()
    if not query_en or query_en.upper().startswith("[EN]"):
        return ""
    return query_en[:_EN_QUERY_MAX_CHARS]


def _run_en_line(state: WorkflowState, out_dir: Path, zh_query: str) -> dict:
    """英文检索线(审查修订版阶段2):独立产物/缓存/标签;异常全隔离。

    返回部分更新:``storyline_web_topic_en_artifact``(产物路径或 None)、
    ``status_log_tags``(追加标签)、``error_message``(失败时的格式化串,
    与 ``append_error`` 输出同形;调用方负责与中文线 error_log 合并)。

    skip 语义(审查 §4.4):开关关 / key 缺失 / stub 翻译器 / 检索词构造失败
    → 不发请求不写产物,**显式清 State 旧引用**(防复用同 thread 早先路径)。
    """
    en_path = out_dir / "search_web_topic_en.json"
    base: dict = {"storyline_web_topic_en_artifact": None}

    def _skip() -> dict:
        base["status_log_tags"] = [EN_SKIPPED_STATUS_TAG]
        return base

    if not config.STORYLINE_ENABLE_WEB_TOPIC_EN:
        return _skip()
    if not config.STORYLINE_WEB_TOPIC_XPOZ_API_KEY:
        return _skip()
    # stub 翻译器守卫:MockTranslateClient 译文带 "[EN] " 前缀,污染检索词;
    # stub 模式下英文线整体软跳过(与 en 分支翻译 stub fail-closed 同纪律)。
    if isinstance(get_default_client(), MockTranslateClient):
        return _skip()

    try:
        query_en = _build_en_query(state, zh_query)
    except TranslationError as exc:
        return {
            **base,
            "status_log_tags": [EN_FAILED_STATUS_TAG],
            "error_message": (
                f"[storyline:search_web_topic_en] TRANSLATION_FAILED: {exc!r}"
            ),
        }
    except Exception as exc:  # noqa: BLE001 —— 检索词构造意外失败同样只影响英文线
        return {
            **base,
            "status_log_tags": [EN_FAILED_STATUS_TAG],
            "error_message": (
                f"[storyline:search_web_topic_en] QUERY_BUILD_FAILED: {exc!r}"
            ),
        }
    if not query_en:
        return _skip()

    platforms = tuple(config.STORYLINE_WEB_TOPIC_XPOZ_PLATFORMS)
    max_per_platform = int(config.STORYLINE_WEB_TOPIC_XPOZ_MAX_PER_PLATFORM)
    query_hash_en = hashlib.sha256(query_en.encode("utf-8")).hexdigest()
    request_hash_en = build_request_hash(
        query=query_en,
        provider=xpoz_request_provider(platforms, max_per_platform),
        max_results=10,
    )

    # 缓存与中文线同款语义(§4.6/§4.5):hash 一致且结构有效 → cache_hit 复用;
    # web_topic_force_refresh=true 显式绕过。英文产物独立于中文缓存。
    if not state.get("web_topic_force_refresh"):
        cached = _load_reusable_cache(
            state,
            "storyline_web_topic_en_artifact",
            query_hash_en,
            request_hash_en,
        )
        if cached is not None:
            cached = dict(cached)
            cached["status"] = "cache_hit"
            en_path.write_text(
                json.dumps(cached, ensure_ascii=False, default=str), encoding="utf-8"
            )
            return {
                "storyline_web_topic_en_artifact": str(en_path),
                "status_log_tags": [_en_status_tag_for(cached)],
            }

    try:
        result = cap_topic(
            query=query_en,
            max_results=10,
            provider="xpoz",
            # 代理不在此层注入:xpoz 经 SDK 自身 MCP 传输,httpx trust_env 默认
            # 生效,需要代理时设 HTTP(S)_PROXY env(审查阶段2;执行记录 D5)
            proxy_url=None,
            debug_dir=None,
            provider_options={
                "api_key": config.STORYLINE_WEB_TOPIC_XPOZ_API_KEY,
                "platforms": list(platforms),
                "max_per_platform": max_per_platform,
                "operation_timeout": int(config.STORYLINE_WEB_TOPIC_XPOZ_TIMEOUT),
            },
        )
    except Exception as e:  # noqa: BLE001 —— 英文线任何异常不穿透(审查阶段2任务10)
        return {
            **base,
            "status_log_tags": [EN_FAILED_STATUS_TAG],
            "error_message": (
                f"[storyline:search_web_topic_en] TOOL_EXECUTION_FAILED: {e!r}"
            ),
        }

    if isinstance(result, dict):
        result.setdefault("request_hash", request_hash_en)
    en_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )
    tag = _en_status_tag_for(result)
    out: dict = {
        "storyline_web_topic_en_artifact": str(en_path),
        "status_log_tags": [tag],
    }
    if tag == EN_FAILED_STATUS_TAG:
        out["error_message"] = (
            f"[storyline:search_web_topic_en] {result.get('error_code')}: "
            f"{result.get('message') or ''}"
        )
    return out


def _run_zh_line(state: WorkflowState, out_dir: Path, query: str) -> dict:
    """中文检索线(2026-11-20 阶段1接线,本函数原样搬移,逻辑逐字不变)。"""
    out_path = out_dir / "search_web_topic.json"

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
        cached = _load_reusable_cache(
            state, "storyline_web_topic_artifact", query_hash, request_hash
        )
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


def storyline_search_web_topic_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 2026-11-14 依赖约简阶段5:节点每轮必跑(不踩 D10),开关关闭或检索词为空
    # 在节点内部软跳过 —— 只落标签,不发网络请求、不写产物(§9 方案 1)。
    # 2026-11-20 审查修订版阶段2:主开关关闭/检索词为空 → **双线同跳过**;
    # 英文线同时显式清 State 旧引用(审查 §4.4,防复用同 thread 早先路径)。
    if not config.STORYLINE_ENABLE_WEB_TOPIC:
        return {
            "status_log": append_status_tag(
                state, SKIPPED_STATUS_TAG, EN_SKIPPED_STATUS_TAG
            ),
            "storyline_web_topic_en_artifact": None,
        }
    query = build_web_topic_query(state)
    if not query:
        return {
            "status_log": append_status_tag(
                state, SKIPPED_STATUS_TAG, EN_SKIPPED_STATUS_TAG
            ),
            "storyline_web_topic_en_artifact": None,
        }

    # 中文线:逻辑原样搬移到 _run_zh_line(所有内部 return 语义不变)。
    update = _run_zh_line(state, out_dir, query)

    # 2026-11-20 审查修订版阶段2:英文线。异常已在 _run_en_line 内全隔离,
    # 此处兜底双保险;status_log 与 error_log 手工合并(append_*/append_error
    # 均以 state 为基,两个来源不能各自调用后直接覆盖)。
    try:
        en_update = _run_en_line(state, out_dir, query)
    except Exception as e:  # noqa: BLE001 —— 双保险:英文线任何意外不穿透主线
        en_update = {
            "storyline_web_topic_en_artifact": None,
            "status_log_tags": [EN_FAILED_STATUS_TAG],
            "error_message": (
                f"[storyline:search_web_topic_en] TOOL_EXECUTION_FAILED: {e!r}"
            ),
        }
    update["status_log"] = [
        *list(update.get("status_log") or []),
        *en_update.get("status_log_tags", []),
    ]
    en_error = en_update.get("error_message")
    if en_error:
        if "error_log" in update:
            update["error_log"] = [*update["error_log"], en_error]
        else:
            update["error_log"] = [*list(state.get("error_log", []) or []), en_error]
    update["storyline_web_topic_en_artifact"] = en_update.get(
        "storyline_web_topic_en_artifact"
    )
    return update

"""本地化 select_bgm(plan_v4 §5 阶段 4 / C 类)。

按 user_request + narration_text + groups 选 BGM。算法去 ``NodeState`` 依赖,
把 vendored ``StorylineRecall``/``ElementFilter``/``analyze_music_metrics`` 三
段逻辑拆成 3 个纯函数(``bgm_selector.rank_candidates`` / ``rank_candidates_with_llm``
/ ``compute_beats_ms``)。

设计纪律(plan §5 阶段 4 + §6.1):
1. **资源描述驱动** — ``bgm_meta_ref.yaml`` 是真源;无音频文件时仍能选 ID
   并写 stub 路径,允许 auto-mode 端到端跑通。
2. **三段决策**:
   - 启发式 ``rank_candidates`` → top N
   - LLM ``rank_candidates_with_llm`` → 从 top N 选 1
   - fallback 启发式第一条 或 default_match
3. **永不抛**(plan §4.4):LLM / 资源解析失败时返回 ``None``,由调用方
   fallback,error_log 记录但不阻断。

输出(state 字段 ``storyline_bgm_selection``,plan §3.2)::
    {
        "bgm_ref": "<bgm_id>",
        "bgm_path": "<真实文件路径或空>",
        "beats": [int, ...],            # 毫秒等距节拍
        "duration_ms": int,
        "tempo": "slow"|"medium"|"fast",
        "default_volume": float,        # 0~1
        "reason": "<LLM 给出或启发式>",
        "method": "llm" | "heuristic" | "default_match",
        "stub": bool,                   # 无真实音频文件
    }
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from storyline_capabilities.bgm_selector import (
    compute_beats_ms,
    load_bgm_library,
    rank_candidates,
    rank_candidates_with_llm,
)
from storyline_capabilities.vlm_client import LLMClient, StubLLMClient

logger = logging.getLogger(__name__)


def select_bgm(
    *,
    user_request: str = "",
    groups: Optional[list[dict[str, Any]]] = None,
    narration_text: str = "",
    lang: str = "zh",
    library: Optional[list[dict[str, Any]]] = None,
    default_match: Optional[dict[str, Any]] = None,
    client: Optional[LLMClient] = None,
) -> dict[str, Any]:
    """选 BGM,产出可直接写进 ``storyline_bgm_selection`` 的 dict。

    Args:
        user_request: 用户对配乐的要求(可空)。
        groups: ``group_clips`` 输出,用于聚合 narration 做关键词抽取。
        narration_text: 已聚合的 narration 文本(可选;缺省时聚合 groups)。
        lang: ``"zh"`` / ``"en"``,LLM prompt 语言。
        library: 注入 BGM 库(单测 / 自定义资源时用);None 时读 yaml。
        default_match: 注入默认 fallback;None 时读 yaml。
        client: LLM client 注入位;None 时走 ``StubLLMClient``(端到端跑通)。

    Returns:
        与 plan §3.2 ``storyline_bgm_selection`` 字段约定一致的 dict;
        任何情况都返回 dict(永不返回 ``None``,让节点壳子无脑 merge)。
    """
    # 1. 加载资源描述(若未注入)
    meta = load_bgm_library() if library is None else {
        "library": library or [],
        "default_match": default_match or {},
    }
    lib: list[dict[str, Any]] = list(meta.get("library") or [])
    dflt: dict[str, Any] = dict(meta.get("default_match") or default_match or {})

    # 2. 聚合 narration 文本(启发式 + LLM prompt 都要用)
    if not narration_text:
        narrations: list[str] = []
        for g in groups or []:
            t = (g or {}).get("narration") or g.get("theme") or ""
            if t:
                narrations.append(str(t))
        narration_text = "\n".join(narrations)

    # 3. 启发式召回 top N
    candidates = rank_candidates(
        library=lib,
        user_request=user_request,
        narration_text=narration_text,
        top_n=int((meta.get("matching") or {}).get("max_candidates_for_llm", 5) or 5),
    )

    # 4. LLM 决策
    chosen: Optional[dict[str, Any]] = None
    method = "default_match"
    if candidates:
        llm_client = client
        if llm_client is None:
            # 默认走 StubLLMClient,行为:无 key → 返回 default_caption 文本
            llm_client = StubLLMClient(default_caption='{"bgm_id": ""}')
        try:
            chosen = rank_candidates_with_llm(
                candidates=candidates,
                user_request=user_request,
                lang=lang,
                client=llm_client,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"select_bgm LLM call failed: {e!r}")
            chosen = None

    # 5. fallback 启发式第一条 / default_match
    if chosen is None:
        chosen = candidates[0] if candidates else None
        method = "heuristic"
    else:
        method = "llm"

    if chosen is None:
        chosen = _lookup_default(lib, dflt)
        method = "default_match"

    if chosen is None:
        # 兜底再兜底:库空 / yaml 没解析到,返回最小可用 stub
        return _empty_bg_selection(reason="no library")

    # 6. 节拍 + 时长
    duration_ms = int(chosen.get("duration_ms") or 35000)
    beat_period = int(chosen.get("beat_period_ms") or 1000)
    beats = compute_beats_ms(duration_ms=duration_ms, beat_period_ms=beat_period)

    bgm_path = str(chosen.get("path") or "")

    return {
        "bgm_ref": str(chosen.get("bgm_id") or "bgm_unknown"),
        "bgm_path": bgm_path,
        "beats": beats,
        "duration_ms": duration_ms,
        "tempo": str(chosen.get("tempo") or "medium"),
        "default_volume": float(chosen.get("default_volume") or 0.35),
        "reason": str(chosen.get("reason") or ""),
        "method": method,
        "stub": (not bgm_path) or method == "default_match",
    }


def _lookup_default(
    library: list[dict[str, Any]],
    default_match: dict[str, Any],
) -> Optional[dict[str, Any]]:
    bid = default_match.get("bgm_id")
    if bid:
        for e in library:
            if e.get("bgm_id") == bid:
                return e
    return library[0] if library else None


def _empty_bg_selection(reason: str) -> dict[str, Any]:
    return {
        "bgm_ref": "bgm_none",
        "bgm_path": "",
        "beats": [],
        "duration_ms": 0,
        "tempo": "medium",
        "default_volume": 0.0,
        "reason": reason,
        "method": "empty",
        "stub": True,
    }


__all__ = ["select_bgm"]
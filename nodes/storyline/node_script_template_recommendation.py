"""文案模板推荐节点(2026-11-12 noop 治理阶段1 真实现,设计文档 §2.4 / §6 阶段1)。

历史:阶段 0 期间是 vendored 透传壳子 → 本地化 noop(仅 append status_log)。
2026-11-12 起改造成 **generate_script 的前置业务节点**(主链:
``group_clips → 本节点 →(条件路由)→ web_topic | generate_script``),
检索模板库 → 标签过滤 → 选模板 → 把正文片段写进 artifact,供
``generate_script`` 注入 prompt(风格/结构参考,不是事实来源)。

能力层在 ``storyline_capabilities/script_template_rec.py``(BM25 风格关键词
检索,零新增重依赖;FireRed 的 FAISS/向量召回如未来需要走独立 sidecar)。

输入:
- ``storyline_groups_artifact``(分组主题,参与检索 query)
- ``storyline_understanding_artifact``(``overall``,参与检索 query)
- 用户要求字段(script_request / style_request / user_request)
- ``resolve_lang``(中英文模板隔离)

输出:
- ``storyline_script_template_artifact``:``<outputs>/storyline/script_template_plan.json`` 路径
- ``status_log`` append ``storyline_script_template_recommendation_done``(**固定**标签;
  本节点在 qa 重试回路里每轮重跑,标签带条数会破坏 qa 路由,同 GAT/PTAT 纪律)

失败语义:本节点在主链上,任何异常都降级为 ``status=unavailable`` 的 artifact
照常返回,**绝不抛、绝不阻断文案生成**(设计文档 §1.4 降级表)。
qa 重试缓存:同 (query, language, 库指纹) 直接复用上次推荐结果(§1.3 任务10),
重试轮不重复检索。
"""
from __future__ import annotations

import json
import logging

from state import WorkflowState
from storyline_capabilities.script_template_rec import (
    build_template_query,
    load_template_catalog,
    recommend_script_template,
)
from nodes.storyline._common import (
    _read_groups,
    _read_json_artifact,
    _resolve_outputs_root,
    append_status_tag,
    extract_user_request,
    resolve_lang,
)

logger = logging.getLogger(__name__)

#: qa 重试缓存键 → 推荐结果(同 query + 同库指纹 ⇒ 同结果,直接复用)。
#: 进程内 memo;库内容(fingerprint)变化自动失效。
_RECO_CACHE: dict[tuple[str, str, str], dict] = {}


def _clean_template_body(raw: object) -> str:
    """模板正文净化:花括号中和(与 web_research 同款纪律,防占位符干扰)。"""
    return str(raw or "").replace("{", "(").replace("}", ")")


def storyline_script_template_recommendation_node(
    state: WorkflowState,
) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "script_template_plan.json"

    try:
        understanding = _read_json_artifact(state, "storyline_understanding_artifact")
        overall = ""
        if isinstance(understanding, dict):
            overall = str(understanding.get("overall") or "")
        groups = _read_groups(state)
        themes = [
            str(g.get("theme") or "") for g in groups if isinstance(g, dict)
        ]
        user_request = extract_user_request(
            state, "script_request", "style_request", "user_request"
        )
        language = resolve_lang(state)
        query = build_template_query(
            user_request=user_request, overall=overall, group_themes=themes
        )

        catalog = load_template_catalog()
        cache_key = (query, language, catalog.fingerprint)
        cached = _RECO_CACHE.get(cache_key)
        if cached is not None:
            artifact = dict(cached)
            artifact["from_cache"] = True
        else:
            artifact = recommend_script_template(
                query=query, language=language, catalog=catalog
            )
            artifact["from_cache"] = False
            _RECO_CACHE[cache_key] = dict(artifact)

        content = artifact.get("selected_template_content")
        if content:
            artifact["selected_template_content"] = _clean_template_body(content)

        logger.info(
            "script_template_recommendation: status=%s template=%s query_len=%d",
            artifact.get("status"),
            artifact.get("template_id"),
            len(query),
        )
    except Exception as e:  # noqa: BLE001 — 主链节点,降级不抛(设计文档 §1.4)
        logger.warning("script_template_recommendation 降级: %r", e)
        artifact = {
            "status": "unavailable",
            "query": "",
            "language": resolve_lang(state),
            "candidates": [],
            "selected_template": None,
            "template_id": None,
            "template_version": None,
            "content_sha256": None,
            "retrieval_method": "keyword_bm25",
            "selection_method": None,
            "fallback_reason": f"节点层异常降级: {e!r}",
            "selected_template_content": None,
            "content_truncated": False,
            "warnings": [],
            "from_cache": False,
        }

    out_path.write_text(
        json.dumps(artifact, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "storyline_script_template_artifact": str(out_path),
        "status_log": append_status_tag(
            state, "storyline_script_template_recommendation_done"
        ),
    }

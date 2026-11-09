"""Phase 4 B 类本地化节点:storyline_generate_script(plan_v4 §5 阶段 3)。

2026-11-10(交付 QA 阻断分析):本节点此前把能力层异常吞成 error_log 后照常
返回(节点绿灯),空 narration 一路静默流到交付 QA 才暴露,且零证据。现改为:

1. LLM 调用/解析留痕**先落盘** ``<storyline>/script_llm_trace.json``(证据优先);
2. required_features 里 ``subtitles_zh`` / ``voiceover_zh`` 任一必选而 narration
   全空时,**写完产物后 raise**(源头早失败,替代 30 分钟后交付 QA 阻断);
3. 能力层异常不再吞 —— 记 error_log 后 re-raise,节点红可见。
"""
from __future__ import annotations

import json

from draft_ops.draft_integrity import resolve_required_features
from state import WorkflowState
from storyline_capabilities.generate_script import generate_script
from storyline_capabilities.vlm_client import StubLLMClient, get_default_client
from nodes.storyline._common import (
    _read_groups,
    _read_json_artifact,
    append_status_tag,
    extract_user_request,
    format_web_topic_reference,
    resolve_lang,
    _resolve_outputs_root,
)


# 本文件原来的 ``_read_groups_dict`` 与 ``_common._read_groups`` 是同一份逻辑
# (同一个 key ``storyline_groups_artifact``、同一个 ``groups`` 数组、同一套失败降级),
# 只是函数名多带一个 ``_dict`` 后缀,已直接改用 _common 版本,不再单独定义。


def storyline_generate_script_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "storyline"
    out_dir.mkdir(parents=True, exist_ok=True)

    groups = _read_groups(state)
    # ``{{overall}}`` = 素材整体概述,来自 understand_clips 的产物(D6:此前没传,
    # 占位符原样残留,模型拿不到素材全貌)。
    understanding = _read_json_artifact(state, "storyline_understanding_artifact")
    overall = ""
    if isinstance(understanding, dict):
        overall = str(understanding.get("overall") or "")
    # 2026-10:网页话题参考(storyline_search_web_topic 的产物)软注入 overall。
    # 参考信息不是文案生成的必要输入 —— 产物缺失 / 格式不对 / 任何异常都直接忽略。
    try:
        ref = format_web_topic_reference(
            _read_json_artifact(state, "storyline_web_topic_artifact")
        )
        if ref:
            overall = f"{overall}\n\n{ref}" if overall else ref
    except Exception:  # noqa: BLE001
        pass

    required = resolve_required_features(state)
    # stub 联调不 fail-fast(阶段 6 口径:stub 是"降级可见"不是"阻断";
    # 交付 QA 对 stub 配音同口径)。只有真实后端才做 narration 源头裁决。
    active_is_stub = isinstance(get_default_client(), StubLLMClient)
    require_narration = (
        (not active_is_stub)
        and bool(required.get("subtitles_zh") or required.get("voiceover_zh"))
    )

    try:
        result = generate_script(
            groups=groups,
            lang=resolve_lang(state),
            user_request=extract_user_request(
                state, "script_request", "style_request", "user_request"
            ),
            overall=overall,
        )
    except Exception as e:  # noqa: BLE001
        # 能力层已不再吞异常;这里兜底留痕后 re-raise(节点红可见,不再假成功)。
        (out_dir / "script_llm_trace.json").write_text(
            json.dumps(
                {"ok": False, "error": repr(e), "stage": "node_exception"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        raise

    # 证据先落盘(无论成败),再做 fail-fast 裁决。
    trace = dict(result.get("llm_trace") or {})
    trace_path = out_dir / "script_llm_trace.json"
    trace_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    out_path = out_dir / "script.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )

    narrations = [
        str(g.get("narration") or "").strip()
        for g in (result.get("group_scripts") or [])
    ]
    if require_narration and groups and not any(narrations):
        raise RuntimeError(
            "[storyline:generate_script] narration 全空(字幕/配音源头断裂,"
            "LLM 返回无可用旁白)。trace 已落盘: "
            f"{trace_path}; raw_head={str(trace.get('raw_head') or '')[:200]!r}"
        )

    return {
        "storyline_script_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_generate_script_done"),
    }

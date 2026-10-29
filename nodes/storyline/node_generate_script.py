"""Phase 4 B 类本地化节点:storyline_generate_script(plan_v4 §5 阶段 3)。"""
from __future__ import annotations

import json

from state import WorkflowState
from storyline_capabilities.generate_script import generate_script
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
        return {
            "error_log": [
                *list(state.get("error_log", []) or []),
                f"[storyline:generate_script] TOOL_EXECUTION_FAILED: {e!r}",
            ],
            "status_log": append_status_tag(
                state, "storyline_generate_script_failed"
            ),
        }

    out_path = out_dir / "script.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return {
        "storyline_script_artifact": str(out_path),
        "status_log": append_status_tag(state, "storyline_generate_script_done"),
    }

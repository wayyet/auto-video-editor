"""Phase 4 B 类本地化节点:storyline_generate_script(plan_v4 §5 阶段 3)。"""
from __future__ import annotations

import json

from state import WorkflowState
from storyline_capabilities.generate_script import generate_script
from nodes.storyline._common import (
    _read_groups,
    append_status_tag,
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
    try:
        result = generate_script(groups=groups)
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

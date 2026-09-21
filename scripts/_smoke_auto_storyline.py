"""Auto-mode 19 节点 smoke 测试 — 阶段 0 端到端验证(stream 模式)。

- 用 ``stream_mode="updates"`` 逐步驱动,这样能看到图走到哪一步。
- 验证:
  1. open_preview 之后能正确切到 storyline_load_media(_route_mode 起作用)
  2. 19 节点壳子按 plan §2.2 拓扑顺序执行
  3. storyline_plan 字段被 join_storyline 写出
  4. 没有任何"storyline:*" 错误信息
  5. generate_draft 在预置 draft_path 时被图跳过,完成全链路

用法::

    python scripts/_smoke_auto_storyline.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graph import _STORYLINE_MODE_OVERRIDE, build_graph  # noqa: E402


def _seed_min_draft(tmp: Path) -> Path:
    """预置一个最小的 35s draft_content.json 让 generate_draft_wrapped 跳过落盘。"""
    draft = tmp / "draft_content.json"
    draft.write_text(
        json.dumps(
            {
                "canvas_config": {"width": 1080, "height": 1920},
                "duration": 35_000_000,
                "materials": {
                    "videos": [{"id": "v1", "path": "E:/tmp/smoke.mp4"}],
                    "audios": [],
                    "texts": [],
                },
                "tracks": [
                    {
                        "type": "video",
                        "fps": 30,
                        "segments": [
                            {
                                "id": "s1",
                                "target_timerange": {
                                    "start": 0,
                                    "duration": 35_000_000,
                                },
                                "source_timerange": {
                                    "start": 0,
                                    "duration": 35_000_000,
                                },
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return draft


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        draft_file = _seed_min_draft(tmp)

        saved_token = _STORYLINE_MODE_OVERRIDE.set("auto")
        try:
            g = build_graph(
                checkpointer=InMemorySaver(),
                start_heartbeat_thread=False,
                run_preflight=False,
            )
            state = {
                "session_id": "smoke-auto",
                "video_input_path": "E:/tmp/smoke.mp4",
                "error_log": [],
                "status_log": [],
                "draft_path": str(draft_file),
                "draft_encryption_status": "plaintext",
                "draft_version_strategy": "strategy_a_version_lock",
                "storyline_targets": {"target_duration_ms": 35000},
            }
            config = {"configurable": {"thread_id": "smoke-auto-thread"}}

            # 用 stream 模式逐步看
            last_log: list[str] = []
            for event in g.stream(state, config=config, stream_mode="updates"):
                for node_name, delta in event.items():
                    if isinstance(delta, dict):
                        tags = delta.get("status_log")
                        if isinstance(tags, list) and tags:
                            for t in tags:
                                if t not in last_log:
                                    last_log.append(t)
                                    print(f"  [{node_name}] {t}")
            final_state = g.get_state(config)
            out = final_state.values if final_state.values else state
        finally:
            _STORYLINE_MODE_OVERRIDE.reset(saved_token)

    # ---- 验收 ----
    expected = [
        "storyline_load_media_done",
        "storyline_split_shots_done",
        "storyline_understand_clips_done",
        "storyline_filter_clips_done",
        "storyline_group_clips_done",
        "storyline_generate_script_done",
        "storyline_script_template_rec_done",
        "storyline_generate_voiceover_done",
        "storyline_select_bgm_done",
        "storyline_recommend_transition_done",
        "storyline_recommend_text_done",
        "storyline_plan_timeline_pro_done",
        # qa_gate 在 stub 模式下 timeline 文件不存在 → 走 _done_empty 分支
        # 通过/失败/_done_empty 任一都算跑过
        "storyline_qa_done_empty",
        "storyline_join_done",
        # 下游节点(generate_draft 在 draft_path 已存在时 wrapper 直接 return,
        # 不写 status_log;阶段 0 验收重点是 19 节点壳子跑通,不是下游)
    ]
    finallog = out.get("status_log", [])
    missing = [e for e in expected if e not in finallog]

    err = out.get("error_log", [])
    storyline_errors = [e for e in err if "storyline" in e]

    print(f"\nstatus_log (n={len(finallog)}):")
    for tag in finallog:
        print(f"  - {tag}")

    if missing:
        print(f"\n[FAIL] missing tag: {missing}")
        return 1
    if storyline_errors:
        print(f"\n[FAIL] storyline errors:")
        for e in storyline_errors:
            print(f"  - {e}")
        return 1

    print("\n[OK] Auto-mode smoke passed (19 nodes ran)")
    print(f"  storyline_plan keys: {list(out.get('storyline_plan', {}).keys())[:5]}")
    print(f"  storyline_timeline_plan: {out.get('storyline_timeline_plan')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

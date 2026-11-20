"""QA 闸门 + JOIN 对"时间线文件内容"的实测矩阵(只读仓库;全部在临时目录内完成,不改仓库文件)。

对 15 种 `storyline_timeline_plan` 文件状态,依次调用**真实**的
  1) nodes.storyline.qa_gate.storyline_qa_gate_node        (QA 闸门)
  2) nodes.storyline.join_storyline.storyline_join_node     (JOIN)
  3) graph._route_after_storyline_join                      (JOIN 之后的条件路由,QC 开 / 关各算一次)
并打印 QA 的路由信号、QA 是否写 error_log、JOIN 写入 storyline_plan 的形态、路由结果。
对应正文:§4.5(JOIN 路由)、§5.1(R2)、§5.6(R4)。

用法(必须在仓库目录之外执行;Windows 上把 python 换成 .\\.venv\\Scripts\\python.exe):
    python -I qa_join_content_matrix.py <仓库根> <输出.json> <临时目录>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
out_json = Path(sys.argv[2]).resolve()
tmp_dir = Path(sys.argv[3]).resolve()
tmp_dir.mkdir(parents=True, exist_ok=True)

sys.dont_write_bytecode = True
sys.path.insert(0, str(repo))

import config as C  # noqa: E402
import graph as G  # noqa: E402
import nodes.storyline.join_storyline as JN  # noqa: E402
import nodes.storyline.qa_gate as QA  # noqa: E402

valid_tl = {
    "schema_version": "1.0",
    "job_id": "j",
    "created_at_ms": 0,
    "source_media": [
        {"media_id": "m1", "file_uri": "file:///a.mp4", "duration_ms": 10000, "media_type": "video"}
    ],
    "clips": [
        {
            "clip_id": "c1",
            "source_media_id": "m1",
            "source_in_ms": 0,
            "source_out_ms": 5000,
            "timeline_in_ms": 0,
            "timeline_out_ms": 5000,
            "semantic_tags": [],
        }
    ],
    "audio": {"bgm_ref": None, "voiceover": None},
    "subtitles": {"zh": None, "en": None},
    "options": {"enable_ai_transition": False, "enable_voiceover": True, "max_duration_ms": 35000},
}
bad_range = dict(valid_tl, clips=[dict(valid_tl["clips"][0], source_out_ms=999999)])
zero_clips = dict(valid_tl, clips=[])

# (标签, 文件内容;None = 状态里没有该键;"MISSING" = 路径指向不存在的文件)
CASES = [
    ("状态里没有 storyline_timeline_plan 键", None),
    ("路径指向不存在的文件", "MISSING"),
    ("文件内容不是合法 JSON", "not json"),
    ("内容 null", "null"),
    ("内容 {}", "{}"),
    ("内容 []", "[]"),
    ("内容 0", "0"),
    ("内容 false", "false"),
    ('内容 ""(空字符串)', '""'),
    ('内容 "abc"', '"abc"'),
    ("内容 123", "123"),
    ("内容 [1]", "[1]"),
    ("合法 JSON 但违反合约(source_out 越界)", json.dumps(bad_range)),
    ("合法 JSON 且合约合法,但 clips = 0", json.dumps(zero_clips)),
    ("完全合法的时间线", json.dumps(valid_tl)),
]


def shape(plan):
    """把 storyline_plan 描述成一小段可读文字。"""
    if plan is None:
        return "None"
    if isinstance(plan, dict) and plan.get("clips") is not None and plan.get("source_media") is not None:
        clips = plan["clips"]
        last = clips[-1].get("timeline_out_ms") if clips and isinstance(clips[-1], dict) else None
        return f"dict(clips={len(clips)}, 末段结束={last}ms)"
    return f"{type(plan).__name__}({plan!r})"


rows = []
for i, (label, content) in enumerate(CASES):
    root = tmp_dir / f"case{i:02d}"
    root.mkdir(exist_ok=True)
    st = {
        "session_id": "s1",
        "video_input_path": "/tmp/x.mp4",
        "video_input_paths": ["/tmp/x.mp4"],
        "storyline_outputs_root": str(root),
        "storyline_session_id": "s1",
    }
    if content == "MISSING":
        st["storyline_timeline_plan"] = str(root / "nope.json")
    elif content is not None:
        p = root / "timeline_plan.json"
        p.write_text(content, encoding="utf-8")
        st["storyline_timeline_plan"] = str(p)

    qa = QA.storyline_qa_gate_node(dict(st))
    qa_err = qa.get("error_log") or []

    delta = JN.storyline_join_node(dict(st))
    merged = dict(st)
    merged.update({k: v for k, v in delta.items() if k != "status_log"})
    plan = delta.get("storyline_plan")
    saved = C.ASSEMBLY_QC_GATE_ENABLED
    try:
        C.ASSEMBLY_QC_GATE_ENABLED = True
        route_on = G._route_after_storyline_join(merged)
        C.ASSEMBLY_QC_GATE_ENABLED = False
        route_off = G._route_after_storyline_join(merged)
    finally:
        C.ASSEMBLY_QC_GATE_ENABLED = saved
    rows.append(
        {
            "case": label,
            "qa_signal": qa.get("storyline_qa_route_signal"),
            "qa_error_log": len(qa_err),
            "join_plan": shape(plan),
            "plan_truthy": bool(plan),
            "route_qc_on": route_on,
            "route_qc_off": route_off,
        }
    )

print(f"仓库: {repo}")
print(f"{'#':>2}  {'时间线文件状态':<34} {'QA 路由信号':<24} {'QA error_log':<13} {'JOIN 写入的 plan':<36} {'路由(QC 开)':<28} 路由(QC 关)")
for i, r in enumerate(rows, 1):
    print(
        f"{i:>2}  {r['case']:<34} {r['qa_signal']:<24} {r['qa_error_log']:<13} {r['join_plan']:<36} {r['route_qc_on']:<28} {r['route_qc_off']}"
    )
ends = [r["case"] for r in rows if r["route_qc_on"] == "__end__"]
print(f"\n走到 __end__ 的情形(QC 开): {len(ends)} 种 -> {ends}")
out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已写出 {out_json}")

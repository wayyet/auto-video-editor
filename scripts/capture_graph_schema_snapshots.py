#!/usr/bin/env python3
"""捕获开关组合下的 build_graph_schema() 快照(只报告,不修改任何文件)。

用途:拓扑重构(阶段 3 auto-only 收敛)在改造前后各运行一次,对比节点与
边是否发生变化。快照只用于对比,不作为第二份运行时拓扑源。

阶段3 起 STORYLINE_MODE 恒为 "auto"(非 auto 值 config import 期 fail-fast),
fusion 开关已随 human 模式链删除,组合只保留 QC 通道与 AI 转场两个开关。

用法(从仓库根目录运行):
    python scripts/capture_graph_schema_snapshots.py [输出目录]

输出目录缺省为 docs/refactor-baseline-20261103/schema-snapshots。
每个组合一个 JSON 文件,包含组合环境变量、基准提交与完整 schema。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (文件名, ASSEMBLY_QC_GATE_ENABLED, STORYLINE_ENABLE_AI_TRANSITION)
COMBOS: list[tuple[str, str, str]] = [
    ("default_baseline", "true", "false"),
    ("assembly_qc_off", "false", "false"),
    ("ai_transition_on", "true", "true"),
]

_CHILD_BOOTSTRAP = r'''
import json, sys
sys.path.insert(0, "")
import config as _av_config
from graph import build_graph
from langgraph.checkpoint.memory import InMemorySaver
from monitoring.workflow_graph import build_graph_schema

compiled = build_graph(
    checkpointer=InMemorySaver(),
    thread_id="schema-snapshot",
    start_heartbeat_thread=False,
    run_preflight=False,
)
schema = build_graph_schema(compiled, _av_config)
json.dump(schema, sys.stdout, ensure_ascii=False)
'''


def capture_one(name: str, qc: str, ai_transition: str) -> dict:
    env = os.environ.copy()
    env["STORYLINE_MODE"] = "auto"
    env["ASSEMBLY_QC_GATE_ENABLED"] = qc
    env["STORYLINE_ENABLE_AI_TRANSITION"] = ai_transition
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_BOOTSTRAP],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"组合 {name} 建图失败:\n{proc.stderr[-2000:]}")
    schema = json.loads(proc.stdout)
    return {
        "meta": {
            "combo": name,
            "env": {
                "STORYLINE_MODE": "auto",
                "ASSEMBLY_QC_GATE_ENABLED": qc,
                "STORYLINE_ENABLE_AI_TRANSITION": ai_transition,
            },
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        },
        "schema": schema,
    }


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        REPO_ROOT / "docs" / "refactor-baseline-20261103" / "schema-snapshots"
    )
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, qc, ai in COMBOS:
        payload = capture_one(name, qc, ai)
        nodes = payload["schema"].get("nodes")
        n_nodes = len(nodes) if isinstance(nodes, (list, dict)) else "?"
        target = out_dir / f"{name}.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"OK {name}: nodes={n_nodes} -> {target.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

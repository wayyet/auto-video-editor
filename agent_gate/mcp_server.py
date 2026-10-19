"""video-agent MCP 服务(stdio)。

只暴露「Agent 可以安全做」的工具。人工关卡的 resume 默认**不在**这里
(只有加 --with-human-tool 才会注册,并带 requiresUserInteraction 标记)。

启动:python -m agent_gate.mcp_server --outputs-root outputs --graph-factory graph:build_graph
注意:stdout 是 MCP 协议通道,服务里不要 print;日志走 stderr。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from agent_gate import runner, service
from agent_gate.mailbox import Mailbox

MAX_WAIT_S = 90.0     # 单次长轮询上限:远小于各家的 MCP 工具超时
MAX_IMAGES = 12       # 单次返回的图片上限(正好装下两个 6 帧的视频任务);装不下的任务留到下一次取


def _safe_submission_path(mb: Mailbox, name: str) -> Path:
    """只允许读 <gate_root>/submissions/ 下的文件,防止 Agent 让服务读任意路径。"""
    base = (mb.root / "submissions").resolve()
    p = (base / name).resolve()
    if not p.is_relative_to(base):
        raise ValueError(f"只能读 {base} 下的文件")
    return p


def build_server(
    outputs_root: Path | str,
    graph_factory: str,
    *,
    backend: str = "gate",
    with_human_tool: bool = False,
) -> FastMCP:
    mcp = FastMCP("video-agent")

    def mb_for(thread_id: str) -> Mailbox:
        return Mailbox(runner.gate_root(outputs_root, thread_id))

    def image_blocks(pairs: list[tuple[str, str]]) -> list[Any]:
        out: list[Any] = []
        for tid, p in pairs:
            out.append(f"[任务 {tid} 的画面帧:{Path(p).name}]")
            out.append(Image(path=p))
        return out

    @mcp.tool()
    def run_start(thread_id: str, video_path: str = "", initial_state_file: str = "",
                  continue_after_crash: bool = False) -> dict[str, Any]:
        """开始一次新运行(后台 worker,立即返回)。

        给 video_path(视频绝对路径)或 initial_state_file(初始 state 的 JSON 文件)。
        worker 崩溃后用 continue_after_crash=true 从最近检查点继续。
        """
        mb = mb_for(thread_id)
        init: Path | None = None
        if initial_state_file:
            init = Path(initial_state_file)
        elif not continue_after_crash:
            if not video_path:
                return {"ok": False, "code": "need_input",
                        "message": "给 video_path 或 initial_state_file,或设 continue_after_crash=true"}
            init = mb.root / "initial_state.json"
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text(json.dumps(
                {"session_id": thread_id, "video_input_path": video_path, "error_log": []},
                ensure_ascii=False), encoding="utf-8")
        return runner.spawn_worker(thread_id=thread_id, outputs_root=outputs_root,
                                   graph_factory=graph_factory, initial_state_file=init,
                                   backend=backend)

    @mcp.tool()
    def run_status(thread_id: str) -> dict[str, Any]:
        """查看运行状态。看 next_action:start_run / answer_tasks / wait / ask_human / done / report_failure / restart_worker。"""
        mb = mb_for(thread_id)
        res = service.status_summary(mb, thread_id=thread_id)
        res["submissions_dir"] = str(mb.root / "submissions")
        return res

    @mcp.tool(structured_output=False)
    def gate_next_tasks(thread_id: str, wait_s: float = 0.0, limit: int = 3) -> list[Any]:
        """取待办 LLM 任务(含画面帧图片)。没有任务时最多等 wait_s 秒(上限 90)。

        你就是这条流水线的「大模型」:读任务里的 prompt 和图片,按要求产出答案,
        再用 gate_submit_result 提交。worker 在等你的答案。
        图片放不下时,多出来的任务不会返回(deferred_tasks 告诉你还剩几个),答完这批再取一次。
        """
        res = service.wait_for_work(mb_for(thread_id), thread_id=thread_id,
                                    wait_s=min(max(wait_s, 0.0), MAX_WAIT_S), limit=limit)
        res["tasks"], res["deferred_tasks"], pairs = service.pack_for_images(res["tasks"], MAX_IMAGES)
        return [json.dumps(res, ensure_ascii=False, indent=2), *image_blocks(pairs)]

    @mcp.tool(structured_output=False)
    def gate_get_task(thread_id: str, task_id: str) -> list[Any]:
        """取单个任务的完整内容(prompt 不截断)。"""
        view = service.get_task(mb_for(thread_id), task_id)
        if view is None:
            return [json.dumps({"ok": False, "code": "unknown_task"}, ensure_ascii=False)]
        _, _, pairs = service.pack_for_images([view], MAX_IMAGES)
        return [json.dumps(view, ensure_ascii=False, indent=2), *image_blocks(pairs)]

    @mcp.tool()
    def gate_submit_result(thread_id: str, task_id: str, answer: str) -> dict[str, Any]:
        """提交一个任务的答案。JSON 任务:answer 是符合 json_schema 的 JSON 字符串。
        不合格会返回 errors 和剩余次数,按提示改后重新提交。"""
        return service.submit_result(mb_for(thread_id), task_id, answer)

    @mcp.tool()
    def gate_submit_batch(thread_id: str, results_file: str) -> dict[str, Any]:
        """一次提交多个答案。先用文件写入工具把 {"<task_id>": <答案>, ...} 写到
        run_status 返回的 submissions_dir 下,再把文件名传给 results_file。"""
        mb = mb_for(thread_id)
        try:
            p = _safe_submission_path(mb, results_file)
            results = json.loads(p.read_text(encoding="utf-8-sig"))   # 容忍 BOM
        except (OSError, ValueError) as e:
            return {"ok": False, "code": "bad_file", "message": str(e)}
        return service.submit_batch(mb, results)

    if with_human_tool:
        @mcp.tool(meta={"anthropic/requiresUserInteraction": True})
        def human_resume(thread_id: str, checkpoint: str, value: str = "true") -> dict[str, Any]:
            """【需要真人点击确认】通过人工关卡后继续运行。Agent 不得自行调用。"""
            try:
                v: Any = json.loads(value)
            except json.JSONDecodeError:
                v = value
            return runner.spawn_worker(thread_id=thread_id, outputs_root=outputs_root,
                                       graph_factory=graph_factory,
                                       resume_values={checkpoint: v}, backend=backend)

    return mcp


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agent_gate.mcp_server")
    ap.add_argument("--outputs-root", default=os.environ.get("AGENT_GATE_OUTPUTS", "outputs"))
    ap.add_argument("--graph-factory", default=os.environ.get("AGENT_GATE_GRAPH", "graph:build_graph"))
    ap.add_argument("--backend", default="gate")
    ap.add_argument("--with-human-tool", action="store_true")
    a = ap.parse_args(argv)
    build_server(a.outputs_root, a.graph_factory, backend=a.backend,
                 with_human_tool=a.with_human_tool).run(transport="stdio")


if __name__ == "__main__":
    main()

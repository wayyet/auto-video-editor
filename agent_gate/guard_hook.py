#!/usr/bin/env python3
"""Claude Code 的 PreToolUse 钩子(软护栏)。

拦三类事:
    1. 调用 human_resume(放行人工关卡)。
    2. 直接读写检查点库(*.sqlite)或闸门的任务 / 结果文件。
    3. 在 Bash 里运行 `python -m agent_gate resume|worker`,或调用内部放行函数。

协议:stdin 收一个 JSON(含 tool_name、tool_input);退出码 2 = 拒绝,stderr 的文字会回给模型。
只用标准库,所以任何 python 3 都能跑它。这个文件会被 `agent_gate init` 复制到 .claude/hooks/ 下。

这是「防手滑」的软护栏,不是安全边界:有 Bash 的 Agent 可以绕过它(见文档「安全」一节)。
"""
from __future__ import annotations

import json
import re
import sys

# 任何文件类工具(Bash / Read / Write / Edit)的参数里出现这些,就拒绝
DENY_ANY = re.compile(
    r"\.sqlite"
    r"|\.(?:result|task|error|attempts)\.json"
    r"|[/\\]+agent_gate[/\\]+(?:tasks|status\.json|events\.jsonl|resume\.json)"
)
# 只对 Bash 生效
DENY_BASH = re.compile(r"-m\s+agent_gate\s+(?:resume|worker)\b|spawn_worker|run_worker|resume_pending_sync")


def check(event: dict) -> str | None:
    """返回拒绝理由;放行返回 None。"""
    name = str(event.get("tool_name") or "")
    text = json.dumps(event.get("tool_input") or {}, ensure_ascii=False)
    if name.endswith("human_resume"):
        return "禁止:人工关卡只能由真人放行,Agent 不得调用 human_resume。请停止并把关卡信息告诉用户。"
    if name in ("Bash", "Read", "Write", "Edit") and DENY_ANY.search(text):
        return "禁止:不得直接读写检查点库(*.sqlite)或闸门任务/结果文件。请改用 MCP 工具。"
    if name == "Bash" and DENY_BASH.search(text):
        return "禁止:Agent 不得运行 resume / worker,也不得调用内部放行函数。请停止并把关卡信息告诉用户。"
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return 0
    reason = check(event if isinstance(event, dict) else {})
    if reason:
        sys.stderr.buffer.write((reason + "\n").encode("utf-8"))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

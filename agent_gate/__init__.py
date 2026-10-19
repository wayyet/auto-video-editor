"""agent_gate — 让 Claude Code / MiniMax Code 等 Agent 程序驱动 auto-video-editor。

两种接法:
- 方案 A「LLM 任务闸门」:GateLLMClient 把每次 LLM 调用写成任务文件并等待结果,
  Agent 通过 MCP 服务(或命令行)领取任务、提交结果。
- 方案 B「CLI 后端」:ClaudeCLIClient / McodeCLIClient 每次调用拉起 `claude -p` / `mcode exec`。
"""

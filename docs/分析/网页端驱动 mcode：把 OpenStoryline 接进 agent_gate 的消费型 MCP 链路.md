# 网页端驱动 mcode：把 OpenStoryline 接进 agent_gate 的消费型 MCP 链路

## Summary

**目标**：让 OpenStoryline 网页端（`http://127.0.0.1:7860`）的对话框能驱动 mcode agent 干活——用户在网页里下指令，mcode 通过已有的 `video-agent` MCP 服务领任务、答题、推进 LangGraph 流水线。

**范围**：在 OpenStoryline 的模型下拉框里新增 `mcode` 选项；新增 `MCodeChatModel`（LangChain `BaseChatModel` 子类）把网页端对话转成 `mcode exec` 调用。**不改 `agent_gate` 任何代码**（其 MCP server 已开发好，直接复用）。

**关键设计决定**：

1. **网页端 = mcode 的操作台**，不是「网页端 agent 自己当操作员」。mcode 是完整的 agent（自带 `video-agent` MCP 工具 + `AGENTS.md` 操作规则），网页端只负责传递指令和展示结果。若让 LangChain 侧也去领任务，会与 mcode 内部抢同一批任务。
2. **mcode 当网页端 agent 的「大脑」**，用 `MCodeChatModel` 接进 LangChain 的 `create_agent` 链路。**不需要用户提供任何 API key** —— 本机 mcode 已登录且实测可用（v011 跑了 3 会话 / 11 任务）。相比之下，走 `deepseek-v4-pro` 需要用户额外提供可用密钥，而 `config.toml` 里现在是占位符 `<YOUR_DEEPSEEK_API_KEY>`，必然 401。
3. **对话历史由网页端自己拼进 prompt**，不依赖 mcode 的会话状态。实测：`--session <id>` 不带上下文；`--continue` 带上下文但续的是「cwd 里最近的 session」——`E:\Documents\kuaishou\video-ops` 是网页端与 `agent_gate drive` 共用的目录，会串到别的 run 的会话。
4. **两种 mcode 档位**：`mcode-operator`（standard 模式，带 MCP 工具，可驱动流水线，~21K input token/轮）与 `mcode-chat`（lightweight 模式，无工具，纯聊天，~857 token/轮）。
5. **8001 storyline MCP server 不纳入本次范围**（用户已确认）。它当前未运行，实测连接被拒——这是既有缺口，不是本次改动造成的。但必须做**容错**：某个 MCP server 连不上时跳过它，而不是让整个 `build_agent` 失败。

---

## 1. 目标与范围

### 目标

- OpenStoryline 网页端模型下拉框出现 `mcode-operator` / `mcode-chat` 两项，选中后网页对话由 mcode 执行。
- 用户在网页端输入「处理 `inputs/30s.mp4`」这类指令时，mcode 领到 `video-agent` 的任务、答题、推进 worker，网页端能看到最终结果。
- 切换回 `deepseek-v4-pro` 时行为与今天一致（仍然 401，除非用户填了真 key）——本次不修这个既有缺口。

### 范围内

| 项 | 说明 |
| --- | --- |
| 新增 `openstoryline/src/open_storyline/mcode_chat_model.py` | `MCodeChatModel` + prompt 拼装 + JSON 解析 + 错误映射 |
| 改 `openstoryline/src/open_storyline/agent.py` | `build_agent` 增加 mcode 分支：跳过 key 校验、跳过 8001 连接、`tools=[]` |
| 改 `openstoryline/agent_fastapi.py` | 模型下拉加两项；`_resolve_builtin_model_override` 识别 mcode；`build_agent` 调用传参 |
| 改 `openstoryline/config.toml` | 新增 `[mcode]` 段：`cwd`（操作员目录）、`mode`、`max_steps`、`timeout_s`、`history_turns` |
| 新增 `openstoryline/tests/` | `MCodeChatModel` 单测 + `build_agent` 分支单测（用假 mcode 可执行文件，不真调模型） |
| 改 `E:\Documents\kuaishou\video-ops\AGENTS.md` | 加一条「来自网页端的指令」说明，让 mcode 知道它可能从网页收到自由对话 |

### 明确不做（Non-goals）

1. **不改 `agent_gate` 任何模块**。它的 MCP server、任务信箱、安全校验都按现状复用。
2. **不修 OpenStoryline 自己的 8001 MCP server 未运行的问题**（用户已确认不纳入）。同时**不修** `deepseek-v4-pro` 的 401（占位符 key 保持原样）。
3. **不实现 LangChain 侧直接调用 `video-agent` 工具**（即网页端 agent 自己当操作员）。会与 mcode 内部工具抢任务。若后续要，作为独立二期（需要可用 LLM）。
4. **不改 `mcode` 本体、不装 Claude Code、不做生产 Postgres 路径**。
5. **不提交 git**（除非用户另行要求）。按用户既定约定，若要提交则直接进 `main`。

---

## 2. 用户约束

| 约束 | 内容 |
| --- | --- |
| 写入范围 | 本次实现只允许改 `E:\Documents\kuaishou\auto-video-editor\openstoryline\` 下的文件，以及仓库外的 `E:\Documents\kuaishou\video-ops\AGENTS.md`。**不得改动 `outputs/`、`checkpoints/`、`logs/`、`inputs/` 里的既有数据** |
| Git | 不新建分支。若用户要求提交，直接提交 `main`（用户既定约定：不建 feature 分支、不走 PR） |
| 测试 | 可以跑测试。`openstoryline` 有独立 venv（`openstoryline\.venv\Scripts\python.exe`），**必须用它跑**，主仓 venv 缺 `langchain-mcp-adapters` |
| 网络 | 不需要外网。mcode 用本机已有登录态；deepseek 路径保持现状 |
| 密钥 | 本次**不需要**写任何 API key。`mcode` 的加锁配置 `C:\Users\wayyet\.minimax\config.locked.yaml` 在仓库外，禁止 `git add` |
| 作用域 | 只验证 `mcode` 这一个 Agent 程序，不扩展到 Claude Code |
| Plan 模式 | 只写本 Plan 文件；不实现、不改仓库、不动 Git 状态 |

---

## 3. 现状（已核实）

### 3.1 两条链路的事实

```
今天：网页端对话框 ──▶ OpenStoryline FastAPI ──▶ ChatOpenAI ──▶ deepseek-v4-pro
                       (7860)                     (401)          new-api.ai4c.cn

今天：mcode agent ◀──MCP──▶ agent_gate.mcp_server (video-agent) ◀──文件信箱──▶ worker(LangGraph)
                        .mcp.json / stdio                        outputs/<thread>/agent_gate/
```

- 网页端对 `mcode` / `agent_gate` **零引用**：`openstoryline\src` 全文搜 `mcode|agent_gate|gate_next_tasks` 无命中。
- `openstoryline\config.toml` 的 `api_key = "<YOUR_DEEPSEEK_API_KEY>"` 是占位符 → 每次调用 401（截图中的报错即来自 `agent.py:71` 的 `validate_api_key`）。

### 3.2 OpenStoryline 已有的消费型 MCP 客户端（关键利好）

`openstoryline/src/open_storyline/agent.py:211-231`：

```python
sampling_callback = make_sampling_callback(llm, vlm)
connections = {cfg.local_mcp_server.server_name: {...}}   # 仅 8001 一个
client = MultiServerMCPClient(connections=connections, tool_interceptors=..., tool_name_prefix=True)
tools = await client.get_tools()
```

- 依赖 `langchain-mcp-adapters==0.2.1`（openstoryline 自己的 venv）。
- `MultiServerMCPClient` **原生支持多 server** 与 `stdio` 连接（`{"command","args","env","transport":"stdio"}`），与 `.mcp.json` 里 `video-agent` 的写法完全同构。
- `get_tools()` 对 stdio 是**每次工具调用新建一个会话**（= 每次起一个子进程），因此本方案**不**把 `video-agent` 挂到 LangChain 侧（见 §4.3）。

### 3.3 三个硬阻塞（必须在实现里解决）

| # | 阻塞 | 证据 | 处理 |
| --- | --- | --- | --- |
| B1 | `validate_api_key` 对 LLM **和** VLM 无条件执行 | `agent.py:171` / `agent.py:196` | mcode 分支**跳过两处校验**，VLM 也用 `MCodeChatModel` |
| B2 | `client.get_tools()` 连 8001，8001 无监听 → 抛异常 → `build_agent` 整个失败 | 实测 `MultiServerMCPClient` 连 `http://127.0.0.1:8001/mcp` → 连接被拒 | mcode 分支**不建 storyline 连接**、`tools=[]`；同时给 `get_tools()` 加**按 server 容错**（连不上就跳过） |
| B3 | 素材路径不在白名单 | 网页端上传落在 `openstoryline\outputs\<sid>\media\`，而 `AGENT_GATE_MEDIA_ROOTS` 只含 `auto-video-editor\inputs` 与 `tests\fixtures` | 网页端上传时把视频**复制**到 `auto-video-editor\inputs\<sid>\`（该目录已在白名单内），不需放宽白名单 |

### 3.4 mcode CLI 的实测行为（本机，2026-10-20）

| 项 | 实测值 |
| --- | --- |
| 可执行文件 | `C:\Users\wayyet\.minimax-code\mcode.cmd`，`mcode --version` → `0.6.12`；`shutil.which("mcode")` 能解析到 `.CMD` |
| 输出格式 | `mcode exec --output-format json` 的 stdout = **一行 JSON**：`{"schemaVersion":1,"type":"exec.result","runId":...,"sessionId":"mvs_...","turnId":...,"status":"succeeded","output":"<最终答案纯文本>","model":{...},"usage":{...},"durationMs":4963}` |
| `output` 字段 | 最终答案的**纯文本**（不是 JSON 字符串） |
| `--session <id>` | 复用 session 身份，但**不带对话历史**（实测：问「我刚才说了什么」→ 复述问题而非历史内容；`inputTokens` 21712 ≈ 全新 run 的 20990） |
| `--continue` | **带**对话历史（实测：答出上一条原文）；但续的是「`--cwd` 里最近的 active session」，`video-ops` 是共享目录 → 会串到 `drive` 的 run |
| standard 模式 | `inputTokens` ≈ 21K，`durationMs` ≈ 4.5–9.6s（agent 耗时），端到端 ≈ 10.8s |
| lightweight 模式 | `inputTokens` ≈ 857，**请求里不带任何工具**（硬隔离），无 MCP 能力 |
| `--append-system-prompt` | 支持追加 system prompt（`--help` 明列）；lightweight 是否忽略它**未实测**，实施时需验证 |

### 3.5 其他已核实事实

- web `session_id` 是 32 位 hex（如 `149cb2a628b84abdad3f60006251cba5`）→ 满足 `agent_gate` 的 `THREAD_ID_RE = ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`，**可直接当 `thread_id` 用**。
- `agent_gate` 的 `gate_root = <outputs_root>/<thread_id>/agent_gate`；`.mcp.json` 已配 `--outputs-root E:/Documents/kuaishou/auto-video-editor/outputs` → 与 worker 一致，网页端与 `drive` 共享同一批线程目录。
- 网页端 chat 走 WebSocket `/ws/sessions/{session_id}/chat`；流式取值 `extract_text_delta`（`agent_fastapi.py:3147`）对「content 为 str 的整段 AIMessage」兼容 → 非流式 `BaseChatModel` 可直接复用现有流式管道。
- 模型下拉来源：`agent_fastapi.py:1290` `self.chat_models = [default_llm, CUSTOM_MODEL_KEY]`（`CUSTOM_MODEL_KEY = "__custom__"`）。
- `openstoryline/` **被主仓 git 跟踪**（`git ls-files openstoryline` 有输出）→ 改动会进主仓提交历史。
- `openstoryline` **没有 tests 目录**；主仓 `pyproject.toml` 的 `testpaths = ["tests"]` 不会收集它 → 新测试放 `openstoryline/tests/`，**单独跑**（与 `agent_gate/tests` 同一模式，见 `agent_gate/README.md`）。
- 主仓 venv **没有** `langchain-mcp-adapters`；只有 openstoryline 自己的 venv 有（`langchain-mcp-adapters 0.2.1` / `mcp 1.26.0` / `langchain 1.2.18` / `langchain-openai 1.1.6`）。

---

## 4. 目标架构与端到端流程

### 4.1 架构

```
浏览器对话框
   │  WebSocket /ws/sessions/{sid}/chat
   ▼
OpenStoryline FastAPI (7860)
   │  agent.astream(...)   ← create_agent(model=MCodeChatModel(...))
   ▼
MCodeChatModel._generate(messages)
   │  1) 把系统提示 + 最近 N 轮对话 + 当前消息 拼成 prompt
   │  2) mcode exec --input - --output-format json --cwd <video-ops>
   │     --append-system-prompt "<网页端桥接说明>"
   ▼
mcode agent（完整 agent，自带 video-agent MCP 工具 + AGENTS.md 规则）
   │  MCP over stdio（.mcp.json）
   ▼
agent_gate.mcp_server  (video-agent)
   │  run_start / run_status / gate_next_tasks / gate_submit_result
   ▼
<outputs>/<sid>/agent_gate/tasks/*.task.json   ← 文件信箱
   ▲
   │  GateLLMClient 写任务 / 阻塞读结果
worker (python -m agent_gate worker)  →  graph:build_graph (LangGraph)
```

### 4.2 一次网页端对话的时序

1. 用户在网页输入「把 `inputs/30s.mp4` 处理一下」→ WS `chat.send`。
2. `agent_fastapi` 解析 `chat_model_key == "mcode-operator"` → `build_agent` 走 mcode 分支 → `MCodeChatModel`（`cwd = video-ops`，`mode = standard`）。
3. `sess.agent.astream({"messages": _merged_messages})` → `MCodeChatModel._generate` 收到消息。
4. `_messages_to_prompt` 拼 prompt：网页端 bridge system prompt + 最近 N 轮对话 + 当前用户消息。
5. `MCodeChatModel` 起 `mcode.cmd exec` 子进程，prompt 走 stdin。
6. mcode 读 `video-ops/.mcp.json` → 挂上 `video-agent` MCP → 读 `AGENTS.md` → 决定调 `run_start(thread_id=<sid>, video_path=<inputs/<sid>/xxx.mp4>)`。
7. `run_start` → `spawn_worker(mode="start")` → worker 跑 LangGraph → 节点里 `GateLLMClient` 写任务并阻塞。
8. mcode 调 `gate_next_tasks` → 拿到任务 + 画面帧图片 → 产出答案 → `gate_submit_result` → worker 继续。
9. mcode 跑完 → stdout 一行 JSON → `MCodeChatModel` 取 `output` → 作为 `AIMessage` 返回。
10. LangChain 把整段文本作为一个 chunk 发出 → `extract_text_delta` 取到 → WS 推给浏览器 → 同时落进 `session_state.json`。

### 4.3 为什么不让 LangChain 侧直接挂 `video-agent` 工具

| 方案 | 问题 |
| --- | --- |
| LangChain 侧挂 `video-agent`（`MultiServerMCPClient` 加第二条连接） | ① 网页端 agent 与 mcode **会抢同一批任务**（谁先 `gate_next_tasks` 谁拿到）；② 需要可用 LLM，而当前只有占位符 key；③ `get_tools()` 对 stdio 每次调用起一个子进程，`gate_next_tasks(wait_s)` 又可能阻塞 90s → 进程开销与超时叠加 |

所以本次**不**把 `video-agent` 挂到 LangChain 侧。mcode 本身就是那个「消费型 MCP 客户端」，网页端通过它接入——这正是用户要求的「接入到原来已经开发好的消费型 MCP 客户端」。

### 4.4 两种模式

| 模式 | model key | mcode 档位 | 工具 | 典型用途 |
| --- | --- | --- | --- | --- |
| 操作员 | `mcode-operator` | `--mode standard` | 有（`video-agent` MCP） | 驱动流水线：领任务、答题、推进 worker |
| 对话 | `mcode-chat` | `--mode lightweight` | 无 | 纯聊天 / 问状态（~857 token，快） |

`mcode-chat` 下若需要查流水线状态，用户可显式说「跑 `python -m agent_gate status --thread-id <sid>`」——但 lightweight 无 bash 工具，实际不可用；因此 `mcode-chat` 仅定位为**便宜的多轮问答**，状态查询走 `mcode-operator`。

---

## 5. 落地文件清单

### 5.1 新增（仓库内）

```
openstoryline/src/open_storyline/mcode_chat_model.py
    class MCodeChatModel(BaseChatModel)
    class MCodeExecError(RuntimeError)
    def _messages_to_prompt(messages, *, history_turns) -> str
    def _parse_exec_json(stdout) -> tuple[str, str]      # (status, output)
    BRIDGE_SYSTEM_PROMPT: str

openstoryline/tests/
    conftest.py
    test_mcode_chat_model.py
    test_agent_mcode_branch.py
```

### 5.2 修改（仓库内）

| 文件 | 改动 |
| --- | --- |
| `openstoryline/src/open_storyline/agent.py` | ① `build_agent` 增加 mcode 分支（§6.2）；② `client.get_tools()` 改为**按 server 容错加载**（§6.3） |
| `openstoryline/agent_fastapi.py` | ① `self.chat_models` 增加 `mcode-operator` / `mcode-chat`；② `_resolve_builtin_model_override` 识别 mcode model key，返回 `{"model": "mcode-operator", ...}` 而非 base_url/api_key；③ 传给 `build_agent` 的 `cfg` 增加 mcode 配置读取 |
| `openstoryline/config.toml` | 新增 `[mcode]` 段（§6.5） |
| `openstoryline/README.md` | 补一段「网页端用 mcode 驱动」的说明与启动前提 |

### 5.3 修改（仓库外）

| 文件 | 改动 |
| --- | --- |
| `E:\Documents\kuaishou\video-ops\AGENTS.md` | 加一节「网页端来令」：说明指令可能来自网页自由对话；只有在用户明确要求处理素材/推进流水线时才调用 `video-agent` 工具；thread_id 用调用方给的 |

---

## 6. 关键组件规格

### 6.1 `MCodeChatModel`

```python
class MCodeChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    cwd: str                      # 操作员目录（video-ops），决定 .mcp.json / AGENTS.md
    mode: Literal["standard", "lightweight"] = "standard"
    max_steps: int = 120
    timeout_s: int = 900
    permission: str = "smart"
    mcode_bin: Optional[str] = None       # None → shutil.which("mcode")
    history_turns: int = 10
    extra_args: List[str] = []

    @property
    def _llm_type(self) -> str: return "mcode-cli"

    def bind_tools(self, tools, **kwargs): return self    # mcode 自带工具，不吃 LangChain 工具

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        prompt = _messages_to_prompt(messages, history_turns=self.history_turns)
        raw = self._run_mcode(prompt)
        status, output = _parse_exec_json(raw)
        if status != "succeeded":
            raise MCodeExecError(f"mcode exec 失败: status={status}")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=output))])
```

**调用规格**（与 `agent_gate/launcher.py:153-161` 保持一致，已实测）：

```
mcode.cmd exec --input - --output-format json --permission <p> --max-steps <n>
              --timeout <n>s --cwd <video-ops>
              [--mode standard|lightweight]
              --append-system-prompt <bridge prompt>
```

- prompt 走 **stdin**（Windows 命令行有长度上限，历史可能很长）。
- **不实现 `_stream`**：基类 `astream` 会退化为「整段作为一个 chunk」，与 `extract_text_delta` 兼容（§3.5）。
- stdout 是**一行 JSON**（实测），`json.loads(stdout.strip())` 即可；仍要做「取最后一个 JSON 行」的容错，因为子进程可能往 stdout 混进别的输出。
- 子进程 stdout/stderr 必须 `encoding="utf-8"`（Windows 默认 GBK 会炸）。
- 超时 → 杀整个进程树 → 抛 `MCodeExecError`，消息里给出「超时」而非原始 traceback。

### 6.2 `build_agent` 的 mcode 分支

`agent.py:146` `build_agent(...)` 内的分流顺序（**顺序不能颠倒**）：

```python
is_mcode = (llm_override.get("model") or "").startswith("mcode")

if is_mcode:
    # 1) 跳过 validate_api_key（LLM 与 VLM 都不校验）
    # 2) llm = MCodeChatModel(mode=..., cwd=cfg.mcode.cwd, ...)
    # 3) vlm = 同一个 MCodeChatModel 实例（或同配置的新实例）
    # 4) sampling_callback = make_sampling_callback(llm, vlm)
    # 5) connections = {}          ← 不连 8001（本次不纳入）
    # 6) tools = []                ← 避免与 mcode 内部工具抢任务
    # 7) skills 仍可加载（skill 只是提示词，无副作用）
else:
    # 现有逻辑一字不改
```

- `create_agent(model=llm, tools=tools + skills, middleware=[...], store=store, context_schema=ClientContext)` 的调用签名不变。
- **注意**：现有 `create_agent` 会调 `model.bind_tools(tools)`。`MCodeChatModel.bind_tools` 必须实现（返回 `self`），否则 `NotImplementedError`。

### 6.3 MCP 连接容错（顺带修既有缺陷）

现状：`tools = await client.get_tools()` 一把梭，任意一个 server 连不上就整体抛异常 → 网页端 agent 建不起来（8001 未运行）。

改成按 server 逐个加载、失败即跳过并记日志：

```python
tools = []
for name, conn in connections.items():
    try:
        tools += await client.get_tools(server_name=name)
    except Exception as e:
        logger.warning("MCP server %s 不可用,已跳过: %s", name, e)
```

- 这条改动**不改变**「8001 可用时的行为」，只是把「不可用时整体崩」降级为「该 server 的工具缺失」。
- 本次 `connections` 在 mcode 分支为空，在 deepseek 分支仍只有 8001（不新增 `video-agent` 连接，见 §4.3）。

### 6.4 prompt 拼装

```python
def _messages_to_prompt(messages, *, history_turns: int = 10) -> str:
    # 1) SystemMessage 全部提到最前，去重后拼接
    # 2) 取最后 (history_turns * 2) 条非系统消息作为历史
    # 3) 格式：用户：xxx / 助手：xxx
    # 4) 最后一条 HumanMessage 原样放最后（不带"用户："前缀，避免歧义）
```

- 历史来自网页端 `session_state.json` 里的消息（LangChain `BaseMessage` 列表），**不依赖 mcode 的会话状态**（§3.4 的实测依据）。
- 历史里的多模态内容（网页端上传的媒体）不内联进 prompt（会撑爆 stdin）；改为在 bridge system prompt 里给出**素材目录路径**，让 mcode 自己用 MCP 工具去读。
- 最终 prompt 结构：

```
<网页端 bridge 说明 + 当前 session 的素材目录 + thread_id 提示>

[历史]
用户：...
助手：...

[当前消息]
用户实际输入
```

### 6.5 `config.toml` 新增段

```toml
[mcode]
cwd = "E:/Documents/kuaishou/video-ops"      # 操作员目录（读 .mcp.json / AGENTS.md）
bin = ""                                       # 空 → shutil.which("mcode")
max_steps = 120
timeout_s = 900
permission = "smart"
history_turns = 10
```

对应 `openstoryline/src/open_storyline/config.py` 新增 `MCodeConfig`（`ConfigBaseModel` 子类）+ `Settings.mcode: MCodeConfig`，与现有 `MCPConfig` 同风格。

### 6.6 thread_id 与素材路径映射

| 概念 | 取值 |
| --- | --- |
| `thread_id` | = 网页端 `session_id`（32 位 hex，满足 `THREAD_ID_RE`） |
| `outputs_root` | `E:/Documents/kuaishou/auto-video-editor/outputs`（`.mcp.json` 已配，与 worker 一致） |
| 素材路径 | 网页端上传的视频**复制**到 `E:\Documents\kuaishou\auto-video-editor\inputs\<session_id>\<原名>`；`inputs` 已在 `AGENT_GATE_MEDIA_ROOTS` 内，**不需要放宽白名单** |

- 复制动作放在网页端上传接口（`POST /api/sessions/{sid}/media`）里，**仅在 mcode 模式下复制**（或无条件复制，代价仅是一次文件拷贝——推荐无条件，避免模式判断分支）。
- bridge system prompt 里告知 mcode：「本 session 的素材目录是 `inputs/<session_id>/`，thread_id 是 `<session_id>`」。

---

## 7. 状态、并发、失败与恢复

### 7.1 并发模型

| 维度 | 语义 |
| --- | --- |
| 同一网页 session 的多轮对话 | **串行**。LangChain `create_agent` 的 loop 本身串行；一次 `mcode exec` 跑完才返回 |
| 不同网页 session | 各自独立的 `mcode exec` 子进程，互不干扰 |
| 网页端 mcode vs `agent_gate drive` 的 mcode | **平级双操作员**。两者都从同一个文件信箱领任务，**先到先得**（`gate_next_tasks` 不做归属标记）。风险：任务被另一方领走时，本方 `gate_submit_result` 会拿到「task not pending」错误 → mcode 会自行重试/放弃，bridge prompt 里要写明这一点 |
| `mcode exec` vs `mcode exec` | 每次调用独立进程，mcode 自己保证不并发改同一 session（不同 session id） |

### 7.2 失败模式与恢复

| 失败 | 表现 | 处理 |
| --- | --- | --- |
| `mcode` 不在 PATH / 未安装 | `FileNotFoundError` | `MCodeChatModel` 启动前用 `shutil.which` 探测；找不到 → 抛 `MCodeExecError("找不到 mcode 可执行文件")`，网页端显示明确提示（**不**静默回退到 deepseek） |
| `mcode exec` 超时 | `timeout_s` 到 | 杀进程树 → `MCodeExecError("mcode 执行超时")` |
| `mcode exec` 返回 `status != succeeded` | JSON 里 `status` 字段 | 抛 `MCodeExecError`，把 `output` 作为原因带出 |
| stdout 不是合法 JSON | 解析失败 | 取最后一行以 `{` 开头的文本再试；仍失败 → `MCodeExecError("无法解析 mcode 输出")`，并把原始 stdout 前 500 字符带进错误（本地日志） |
| WebSocket 断开 | 客户端关页面 | mcode 子进程在 `asyncio` 取消时被杀；已领的任务会留在信箱里等下一个人 |
| 任务被另一操作员领走 | `gate_submit_result` 报 task not pending | mcode 自行处理；网页端把该错误如实回显 |
| 网页端 LLM 选了 deepseek 且 key 仍是占位符 | 401 | **保持现状**（不在本次范围），但错误文案沿用现有格式，用户能看出是 key 问题 |

### 7.3 恢复语义

- 网页端的对话历史**存在网页端**（`session_state.json`），mcode 每次是全新 run → 任何一次 mcode 崩溃都不丢历史。
- 流水线的进度存在**文件信箱 + SQLite checkpoint** → 与网页端会话生命周期解耦。网页端会话被删不影响 `drive` 的 run，反之亦然。

---

## 8. 测试与验收标准

### 8.1 自动测试（新增 `openstoryline/tests/`，用 openstoryline 自己的 venv 单独跑）

```powershell
cd E:\Documents\kuaishou\auto-video-editor
.\openstoryline\.venv\Scripts\python.exe -m pytest openstoryline\tests -q
```

| 用例 | 断言 |
| --- | --- |
| `test_prompt_keeps_system_first_and_last_user_message` | `_messages_to_prompt` 输出：系统提示在最前、最后一条用户消息原样在末尾、历史轮次不超过 `history_turns` |
| `test_prompt_truncates_history` | 传 30 轮历史 → 只保留最近 `history_turns` 轮 |
| `test_parse_exec_json_takes_last_json_line` | stdout 混入前后杂行 → 仍能取出 `output` |
| `test_parse_exec_json_rejects_garbage` | 非 JSON → 抛 `MCodeExecError` |
| `test_model_raises_on_non_succeeded_status` | `status="failed"` → 抛 `MCodeExecError`，消息含 status |
| `test_model_skips_api_key_validation` | `build_agent` 走 mcode 分支时，**不**调用 `validate_api_key`（monkeypatch 成抛异常来验证没被调） |
| `test_model_does_not_connect_8001` | mcode 分支下 `MultiServerMCPClient` 不被构造（monkeypatch 记录调用） |
| `test_get_tools_skips_dead_server` | 一个 server 连不上 → 另一个 server 的工具仍被加载 |
| `test_bind_tools_returns_self` | `bind_tools([...])` 不抛异常 |

- 单测**不真调 mcode**：用一个假的 `mcode.cmd`（批处理/Python 脚本）输出固定 JSON 来测 `MCodeChatModel`。
- `agent_gate/tests` 的既有测试**不得回归**（`mcode` 分支不碰 `agent_gate` 代码，理论上零影响；仍要跑一遍确认）。

### 8.2 手工验收（分步，每步都有可观测证据）

| 步 | 动作 | 通过标准 |
| --- | --- | --- |
| A1 | 启动网页端（`openstoryline\.venv\Scripts\python.exe -m uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`，cwd=`openstoryline`） | `GET /` 200 |
| A2 | 网页端模型下拉 | 出现 `mcode-operator` 与 `mcode-chat` 两项 |
| A3 | 选 `mcode-chat`，发「你好」 | 有回复；**不**报 401；进程列表出现 `mcode.cmd exec` 子进程，结束后消失 |
| A4 | 选 `mcode-operator`，发「用 `run_status` 查一下 thread `v011` 的状态」 | 回复里含 v011 的 `worker_state=waiting_human` 与关卡①信息 → **证明 mcode 成功调到了 `video-agent` MCP** |
| A5 | 发「把 `inputs/30s.mp4` 处理一下」，thread_id 用当前 session | mcode 调 `run_start` → worker 被拉起 → `outputs/<sid>/agent_gate/events.jsonl` 出现 `worker_started` / `task_created` / `task_answered`；网页端拿到最终总结 |
| A6 | 检查 `outputs/<sid>/agent_gate/status.json` | `state` 为 `waiting_human`（关卡①）或 `done`；`answered_tasks > 0` |
| A7 | 切回 `deepseek-v4-pro` | 行为与今天一致（401），**未引入新回归** |

### 8.3 回归

- `pytest openstoryline/tests`（新增）全绿。
- `pytest agent_gate/tests`（用主仓 venv）**与改动前逐用例一致**。
- 主仓 `pytest tests`（用主仓 venv）：与基线一致。注意 `STORYLINE_MODE` 必须是 `human`（或不设），否则 8 条 `tests/integration/` 用例会假失败。

---

## 9. 未决事项与风险

| # | 项 | 说明 | 处理 |
| --- | --- | --- | --- |
| R1 | `--mode lightweight` 是否忽略 `--append-system-prompt` | 记忆里只确认它忽略 `--system-prompt-file` | 实施时先实测一次；`mcode-chat` 档若被忽略，就把 bridge 说明拼进 user prompt |
| R2 | 双操作员抢任务 | 网页端 mcode 与 `drive` 的 mcode 领同一批任务 | 本次**接受**（用户未指定分工）。缓解：bridge prompt 里写明「若任务已被领走，如实说明，不要反复重试」；如需独占，后续在 `drive` 侧加避让开关 |
| R3 | 每轮 ~21K input token | `mcode-operator` standard 模式的固定开销 | 提供 `mcode-chat` 档（857 token）作廉价替代；不改变 operator 档的语义 |
| R4 | 网页端上传素材的复制 | 会把视频复制一份到 `inputs/<sid>/` | `inputs` 已被 `.gitignore`；复制失败时在网页端提示「请手动把素材放到 inputs 目录」 |
| R5 | `mcode exec` 的并发上限 | 多个网页 session 同时提问 → 多个 mcode 进程 | 本机不限；观察后再决定是否加信号量 |
| R6 | 8001 storyline MCP 仍未运行 | 既有缺口，本次不纳入 | 已在 §3.3 B2 用「按 server 容错」保证它**不再导致网页端整体不可用** |
| R7 | 未提交 git | 本次实现后工作区会有未提交改动 | 按用户既定约定，需要提交时直接进 `main`；`openstoryline/config.toml.local`（含 key）不得入库 |

---

## 10. 实施顺序建议

| 阶段 | 内容 | 退出门槛 |
| --- | --- | --- |
| **P1** | `MCodeChatModel` + `_messages_to_prompt` + JSON 解析 + 单测 | `pytest openstoryline/tests` 全绿 |
| **P2** | `build_agent` mcode 分支 + MCP 按 server 容错 + 单测 | 单测全绿；deepseek 分支行为不变 |
| **P3** | `agent_fastapi.py` 接线（模型下拉 + 参数透传）+ `config.toml` `[mcode]` 段 + `config.py` `MCodeConfig` | A1–A4 手工验收通过 |
| **P4** | 素材复制到 `inputs/<sid>/` + `AGENTS.md` 增补 | A5–A6 手工验收通过 |
| **P5** | 文档（`openstoryline/README.md`）+ 回归对比 | 三套测试结果与基线一致 |

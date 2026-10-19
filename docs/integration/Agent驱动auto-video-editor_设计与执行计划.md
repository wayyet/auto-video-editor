# Agent 程序直接驱动 auto-video-editor：设计与执行计划

> 本文是 `agent_gate` 接入本仓库的设计与执行记录。配套参考实现见交付包
> `D:\Agent驱动auto-video-editor_交付包`（`2_代码\agent_gate_kit`）。
>
> 落地基线：`main` @ `61cf78d`。按用户既定约定，代码直接提交 `main`，不建 feature 分支、不推 PR。

---

## 0. 结论速览

| 项 | 结论 |
| --- | --- |
| 接入方式 | 项目内 `LLMClient` 协议 + `set_default_client()`，只改 2 个文件；工作流拓扑 / LangGraph 图 / 人工关卡语义全部不动 |
| 主方案 | **MCP 文件信箱**：worker 阻塞等 Agent 通过 MCP 提交答案。备选 `claude-cli` / `mcode-cli` 一问一答适配器一并落地，但不作为验收主线 |
| 安全修复 | 交付包 4 个必修安全问题（路径遍历 / `initial_state_file` 任意读 / 重复启动 / 非法 schema 静默通过）本次直接修好，附 4 个负向测试 |
| 验收主线 | **MiniMax Code 0.6.11**（本机已装）。Claude Code 本机未安装，只写后续步骤 |
| 存储路径 | **只支持 SQLite 本地开发路径**。检测到 `WORKFLOW_ENV=production` 或 Postgres checkpointer 时 worker **显式拒绝启动**，不静默降级 |
| 接入层验证 | ✅ **通过**。真实主图上 12/12 任务由 Agent 作答（0 降级）、人工关卡 ①③② 依次放行成功 |
| 端到端出片 | ✅ **通过**（2026-10-20）。人工关卡放行后剪映草稿正常生成：`drafts/draft_content.json` + `draft_info.json` 双写、封面 6 张、`assembly/preview.mp4` QC `pass`；关卡依次 ①→③→② 放行后 worker `completed` |

**一句话**：Agent 驱动接入层已跑通，端到端也出了片；上一轮卡住的三个项目侧缺陷（D7 `draft_path=None` 崩溃 / D9 契约字段集不一致 / D10 qa_gate 重试回环死锁）已于 2026-10-20 全部修掉，见 [§10.5](#105-d7--d9--d10-修复记录2026-10-20)。

---

## 1. 目标与范围

### 目标

- 仓库里新增 `agent_gate` 包，`python -m agent_gate` 可用。
- 项目 11 处 LLM 调用在 `--backend gate` 下由 Agent 程序（MiniMax Code / Claude Code）作答，不再由 `StubLLMClient` 返回假数据。
- 一条 `drive` 命令能无人值守跑完，直到停在人工关卡（剪映分镜 / 加 BGM），由真人在交互终端 `resume`。

### 11 处 LLM 调用点

`--backend gate` 下全部会被接管：

```
assembly_capabilities/build_timeline.py:268
storyline_capabilities/understand_clips.py:143   (硬编码 StubLLMClient — 已改)
storyline_capabilities/understand_clips.py:194
storyline_capabilities/generate_script.py:46
storyline_capabilities/generate_voiceover.py:99
storyline_capabilities/recommend_text.py:63
storyline_capabilities/recommend_transition.py:52
storyline_capabilities/filter_clips.py:66
storyline_capabilities/group_clips.py:72
storyline_capabilities/bgm_selector.py:225
storyline_capabilities/speech_rough_cut.py:76
storyline_capabilities/select_bgm.py              (硬编码 StubLLMClient — 已改)
```

### 范围内

| 项 | 说明 |
| --- | --- |
| `agent_gate` 包（18 个 Python 模块） | 从交付包复制 + P3 安全修复 + `README.md` |
| 交付包 7 个原测试文件 + fixtures | 原样保留，验证修复未破坏原行为 |
| 新增 4 个安全测试文件 | 覆盖 F1 / F2+F6 / F3 / F4 |
| 接入补丁（2 个源文件） | 直接改，不保留 `.patch` 文件 |
| `requirements.txt` | 加 `mcp==1.26.0` / `jsonschema==4.26.0` + 修正一处已失效注释 |
| 文档 | 本文（`docs/integration/`） |
| 操作员目录生成 | 落在**仓库外**，不进 git |

### 明确不做（Non-goals）

1. **不改**项目工作流拓扑、节点逻辑、人工关卡语义、LangGraph 编译方式。
   （例外：2026-10-20 为跑通 P8 门禁，做了两处**最小**拓扑/节点调整 —— D10 把 `split_shots` 移出
   `plan_timeline_pro` 的 fan-in 屏障、D7 给 4 个下游节点加 `draft_path=None` 降级。见 §10.5。）
2. **不修**项目既有缺陷（见 [§10](#10-已知缺陷与后续项)），只记录、不混入本次接入。
   （例外：D5 / D6 / D7 / D8 / D9 / D10 已分别在 §10.4 / §10.5 修掉；D1 / D2 / D3 / D4 仍只记录。）
3. **不做** Postgres 生产路径的异步 worker。
4. **不装** Claude Code、不做它的登录与验证（写成文档步骤）。
5. **不接**真实的 LLM API key；`mcode-lock` 产物留在仓库外。

---

## 2. 用户约束

| 约束 | 内容 |
| --- | --- |
| 写入范围 | 只允许改本仓库与新建的**仓库外**操作员目录；不得改动 `outputs/`、`checkpoints/`、`logs/`、`inputs/` 里的既有数据 |
| Git | 直接提交 `main`；不新建分支、不推 PR |
| 测试 | 可以跑测试；`pytest` 在 Windows 中文环境需先设 `$env:PYTHONIOENCODING='utf-8'`，否则 capture teardown 会 `UnicodeDecodeError` |
| 网络 | 不需要外网；`mcode` 用本机已有登录态 |
| 密钥 | `mcode` 加锁配置含明文 API key，**必须**落在仓库外，禁止 `git add` |
| 作用域 | 只验证 `claude code` 与 `MiniMax Code` 两个 Agent 程序 |

---

## 3. 现状（落地时已核实）

| 项 | 实测值 |
| --- | --- |
| 路径 | `E:\Documents\kuaishou\auto-video-editor` |
| 分支 / HEAD（落地起点） | `main` / `61cf78d43d136e92c036868377c391d7238ffa25` |
| Python | `.venv\Scripts\python.exe` → 3.13.11 |
| 接入点 | `storyline_capabilities/vlm_client.py`：`LLMClient` 协议 + `get_default_client()` / `set_default_client()`；`register_default_clients()` 是空函数 |
| 图构建 | `graph.py` `build_graph(checkpointer=None, *, thread_id, start_heartbeat_thread, run_preflight, preflight_config)` — 与 worker 调用签名对得上 |
| checkpointer | `config.py` `CHECKPOINTER_BACKEND="sqlite"`，落盘 `checkpoints/<thread_id>.sqlite` |
| 模式开关 | `config.STORYLINE_MODE`：`human`（默认，17 节点）/ `auto`（19 节点确定性图）。**模块导入时读取**，必须在 worker 进程 env 里提前设好 |
| 人工关卡 | `⓪` `node_checkpoint0_storyline_plan`（仅 human 模式）、`①` `node_06_human_reorder`、`②` `node_12_human_add_bgm`、`③` `node_checkpoint3_layout_review`（条件触发） |

### 补丁语义（应用后必须保持不变）

- `understand_clips`：新增 `effective = client or get_default_client()`；逐镜头调用从 `(client or StubLLMClient()).chat(...)` 改为 `effective.chat(...)`；总体总结的触发条件从 `client is not None` 改为 `client is not None or not isinstance(effective, StubLLMClient)`。
- `select_bgm`：`llm_client = get_default_client()`，且当它 `isinstance(..., StubLLMClient)` 时才回退到 `StubLLMClient(default_caption='{"bgm_id": ""}')`。
- 两者都**保持默认 Stub 行为不变**——这是既有单测不回归的前提。

---

## 4. 目标架构与端到端流程

```
人 ──drive──> agent_gate.drive ──拉起──> mcode exec (cwd=操作员目录)
                                            │ 读 .mcp.json / AGENTS.md
                                            ▼
                                      MCP 服务 video-agent
                                      (python -m agent_gate.mcp_server)
                                            │ gate_next_tasks / gate_submit_result
                                            ▼
                                     <outputs>/<thread_id>/agent_gate/
                                          tasks/*.task.json
                                          frames/*/f*.jpg
                                          tasks/*.result.json
                                            ▲
                                            │ GateLLMClient 写任务 / 阻塞读结果
                                            │
                              worker (python -m agent_gate worker)
                                            │  set_default_client(GateLLMClient)
                                            ▼
                                    graph:build_graph (LangGraph)
                                            │
                        ┌───────────────────┴────────────────────┐
                     正常节点                              人工关卡 ⓪①②③
                        │                                       │ interrupt()
                    继续 invoke                        status=waiting_human → drive 退出码 10
                                                                    │
                                                        人在交互终端 resume
```

### 端到端时序（一次 `drive`）

1. `drive` 读 `status.json`。没有 → 用 `--video` 生成 `initial_state.json` 并 `spawn_worker(mode="start")`。
2. worker 进程：`install_client(GateLLMClient)` → `build_graph(thread_id=..., start_heartbeat_thread=False, run_preflight=False)` → `graph.invoke(state, cfg)`。
3. 节点里 `chat_json(...)` → `get_default_client()` 返回 `GateLLMClient` → 抽帧 → 写 `tasks/t_xxx.task.json` → **阻塞轮询** `.result.json`。
4. Agent 调 `gate_next_tasks` 拿到 prompt + 图片 → 生成答案 → `gate_submit_result`。
5. `service.submit_result` 校验（JSON 任务走 `Draft7Validator`）→ 通过则写 `.result.json`。
6. worker 读到结果 → `chat()` 返回 → 图继续。
7. 到人工关卡 → worker 写 `status.json: waiting_human` + `events.jsonl` → 退出码 0。
8. `drive` 看到 `next_action=ask_human` → 返回退出码 **10**（不是失败）。

### 为什么用文件信箱而不是在 `chat()` 里调 `interrupt()`

仓库 11 处 `chat()` 调用**全部**包在 `except Exception` 里，而 LangGraph 的 `GraphInterrupt` 继承 `Exception` → 会被吞掉，图不会暂停。
阻塞等待不抛异常，绕开这个问题。交付包用玩具图实测过这个行为（`test_interrupt_inside_except_exception_is_swallowed`）。

---

## 5. 落地文件清单

### 5.1 新增（仓库内）

```
agent_gate/
  __init__.py  __main__.py  cli.py  cli_client.py  drive.py  factory.py
  gate_client.py  guard_hook.py  launcher.py  mailbox.py  mcp_server.py
  media.py  protocol.py  runner.py  safety.py  scaffold.py  service.py
  validate.py                                    ← safety.py 是 P3 新增
  README.md                                      ← P1 新增（最短用法 + 测试隔离说明）
  fixtures/
    toy_graph.py
    storyline_capabilities/__init__.py
  tests/
    conftest.py  support.py  mock_llm_server.py  slow_mcp_server.py
    test_agent_security.py  test_cli_client.py  test_drive.py
    test_gate.py  test_images.py  test_mcp.py   test_scaffold.py
    test_path_safety.py                          ← P3 新增（F1）
    test_lifecycle.py                            ← P3 新增（F3）
    test_schema_failclosed.py                    ← P3 新增（F4）
    test_input_guard.py                          ← P3 新增（F2 + F6）

docs/integration/
  Agent驱动auto-video-editor_设计与执行计划.md   ← P9 新增（本文）
```

**目录选择理由**：`agent_gate/` 放仓库根，和已有的 `nodes/`、`draft_ops/`、`mcp_clients/`、`storyline_capabilities/` 平级。
`pyproject.toml` 的 `testpaths=["tests"]` **保持不变** → 默认 `pytest` 不会碰 kit 测试。

> ⚠️ `agent_gate/fixtures/storyline_capabilities/` 是**影子包**，`agent_gate/tests/conftest.py` 会把它插进 `sys.path`。
> 因此 kit 测试**必须单独跑**：`pytest agent_gate/tests`。这条已写进 `agent_gate/README.md`。

### 5.2 修改（仓库内）

| 文件 | 改动 | 阶段 |
| --- | --- | --- |
| `storyline_capabilities/understand_clips.py` | 接入补丁（接默认 client） | P2 |
| `storyline_capabilities/select_bgm.py` | 接入补丁（接默认 client） | P2 |
| `requirements.txt` | 新增 `mcp==1.26.0`、`jsonschema==4.26.0`；**修正**现有注释「`mcp` 仍由 `langgraph-sdk==0.4.4` 间接传递,主环境不再主动声明」——agent_gate 现在直接依赖它，该注释已失效 | P0 / P2 |
| `scripts/assembly/phase5_e2e.py` | E2E 脚本适配新的接入点与素材白名单 | P3 |

不单独保留 `requirements-agent-gate.txt`（依赖并入 `requirements.txt` 单点管理，避免漂移）。
不保留 `.patch` 文件（直接改源码，回滚用 `git checkout --` / `git revert`）。

### 5.3 新增（仓库外）

```
E:\Documents\kuaishou\video-ops\      ← 操作员目录，Agent 从这里启动
  .mcp.json            （含 venv python 绝对路径 + PYTHONPATH + STORYLINE_MODE=auto）
  AGENTS.md            （MiniMax Code 读）
  CLAUDE.md            （Claude Code 读，内容同 AGENTS.md）
  mcode.lock.snippet.yaml
  .claude\settings.json
  .claude\hooks\guard_hook.py

C:\Users\wayyet\.minimax\config.locked.yaml   ← mcode-lock 产物，含明文 API key
```

**必须在仓库外**：仓库根已有自己的 agent 指令文件；`config.locked.yaml` 含 API key；`.mcp.json` 含本机绝对路径。

已核对的 `.mcp.json` 实际内容：

```json
{
  "mcpServers": {
    "video-agent": {
      "command": "E:/Documents/kuaishou/auto-video-editor/.venv/Scripts/python.exe",
      "args": ["-m", "agent_gate.mcp_server",
               "--outputs-root", "E:/Documents/kuaishou/auto-video-editor/outputs",
               "--graph-factory", "graph:build_graph"],
      "env": {
        "PYTHONPATH": "E:/Documents/kuaishou/auto-video-editor",
        "AGENT_GATE_REPO_ROOT": "E:/Documents/kuaishou/auto-video-editor",
        "PYTHONUTF8": "1",
        "STORYLINE_MODE": "auto",
        "AGENT_GATE_MEDIA_ROOTS": "E:\\...\\auto-video-editor\\inputs;E:\\...\\auto-video-editor\\tests\\fixtures"
      },
      "timeout": 150000
    }
  }
}
```

`timeout=150000` > `gate_next_tasks` 最长 90 秒等待；**没有** `--with-human-tool` → Agent 拿不到 `human_resume`。

---

## 6. 安全与生命周期修复规格

`agent_gate/safety.py` 是**唯一**的标识符与路径校验入口。全部在 **P3** 实现。

### F1（P0-1）thread_id / task_id 白名单 + 路径归属

```python
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TASK_ID_RE   = re.compile(r"^t_[0-9a-f]{16}$")

class UnsafeIdentifier(ValueError): ...
class UnsafePath(ValueError): ...

def safe_thread_id(thread_id: str) -> str
def safe_task_id(task_id: str) -> str
def safe_gate_root(outputs_root, thread_id) -> Path
def safe_task_path(mailbox, task_id) -> Path
def safe_frame_dir(mailbox, task_id) -> Path
def safe_media_path(video_path, roots) -> Path
def safe_state_file(path, roots=None) -> Path
```

`safe_gate_root` 三步，缺一不可：

1. `thread_id` 过 `THREAD_ID_RE`；
2. `root = (Path(outputs_root).resolve() / thread_id / "agent_gate")`；
3. `root.resolve().is_relative_to(Path(outputs_root).resolve())` 否则抛错。

**必须重复校验的调用点**（不能只在入口校验一次）：

| 位置 | 动作 |
| --- | --- |
| `runner.gate_root()` | 改走 `safe_gate_root`（CLI / MCP / drive 全部经过它，一处覆盖多入口） |
| `runner.spawn_worker()` / `run_worker()` | 入口显式 `safe_thread_id` |
| `mailbox.Mailbox.task_path/result_path/error_path/attempts_path/frames_dir` | 全部先 `safe_task_id`，返回路径再过一次目录归属 |
| `service.submit_result()` / `get_task()` | 对 Agent 传入的 `task_id` 先 `safe_task_id`，非法返回 `{"ok": False, "code": "bad_task_id"}` |
| `mcp_server.mb_for()` | 走 `safe_gate_root` |
| `cli._mb()` | 走 `safe_gate_root` |

必须被拒绝并写事件的输入：`../x`、`..\x`、`/tmp/x`、`C:\Windows\x`、`%2e%2e/x`、空串、`t_` + 15 位 hex、`t_` + 大写 hex、超过 64 字符、含空格/中文。

### F2（P0-2）`initial_state_file` 与视频路径

MCP `run_start` 的签名**已删除** `initial_state_file` 参数：

```python
def run_start(thread_id: str, video_path: str = "",
              continue_after_crash: bool = False) -> dict[str, Any]
```

理由：MCP 工具参数由 Agent 发起，不能把 Agent 提供的文件路径当可信配置。

- `video_path`：必须通过 `safe_media_path`。
- **素材根目录白名单**：环境变量 `AGENT_GATE_MEDIA_ROOTS`（`os.pathsep` 分隔多个绝对路径），由 `agent_gate init --env AGENT_GATE_MEDIA_ROOTS=...` 写进 `.mcp.json`（P4 传入）。本机默认 `<repo>\inputs;<repo>\tests\fixtures`。
- `initial_state.json` 仍写到 `<gate_root>/initial_state.json`（在 `outputs/` 下，已 gitignore）。该路径由**服务端**生成，不再由 Agent 提供。
- CLI `start --initial-state-file` **保留**（操作员侧可信路径），但同样过 `resolve()` + 白名单，做纵深防御。

**拒绝事件的处理**：写 `events.jsonl`（`event="input_rejected", code=...`），**返回给 Agent 的 message 不含本机绝对路径**，只给一个不含路径的说明 + 一个 code。

### F3（P1-1）运行生命周期状态机

`spawn_worker()` 新增必填参数 `mode`：

| mode | 允许的前置条件 | 违反时返回 |
| --- | --- | --- |
| `start` | `status.json` 不存在 **且** `checkpoints/<thread_id>.sqlite` 不存在 | `{"ok": False, "code": "already_used"}` |
| `continue_after_crash` | `status.json.state ∈ {crashed, failed}` 且 checkpoint 文件存在 | `{"ok": False, "code": "no_checkpoint"}` |
| `resume` | `status.json.state == "waiting_human"` 且 `checkpoint` 出现在 `status.interrupts[*].checkpoint` 中 | `{"ok": False, "code": "not_waiting_human"}` |

实际实现中细分出的错误码：`already_used` / `not_restartable` / `no_checkpoint` / `not_waiting_human` / `bad_checkpoint`。

要点：

- 已 `completed` 的 `thread_id` 不能再喂初始 state；重跑必须换新 ID。
- 三种 mode **互斥**，同时给 `resume` 和 `continue_after_crash` 报错。
- `drive` 的 `restart_worker` 分支改为显式调 `mode="continue_after_crash"`；MCP `human_resume` 走 `mode="resume"`。
- `already_running`（`starting`/`running` 时拒绝）**保留**，作为第 4 条独立检查。

### F4（P1-3）非法 schema 必须 fail closed

`agent_gate/validate.py`：

```python
class InvalidSchema(ValueError): ...
```

- `validate_result()` 遇到 `SchemaError` → 抛 `InvalidSchema`（**不再**静默返回 `[]` 当作通过）。
- `service.submit_result()` 捕获 → 返回 `{"ok": False, "code": "schema_invalid"}`，并写 `input_rejected` 事件（`code=schema_invalid`）**直接关闭该任务**（重试无用，不消耗 attempts 配额）。
- `GateLLMClient._create_task()`：写 `.task.json` 前先 `check_schema`，非法则在 task 里打 `"schema_invalid": True` 并写事件。

### F5（P1-4）刻意不改：镜头描述任务的 schema

审查版建议给 `understand_clips.py:143` 补 `json_schema`。**本次不做**，理由：

1. 该调用当前**不传** `json_schema`，加上后 `answer_format` 从 `text` 变 `json`。在默认 `stub` 后端下，`StubLLMClient` 会改走 `_fake_obj_from_schema` 分支，返回 `{"caption": "", "aes_score": 0.0}` 而非现在的 `"no caption"` —— **会改变项目既有行为，可能打挂现有单测**。
2. 审查版 §4.2 明确要求「不要把项目既有缺陷混入 Agent 接入补丁，否则难以区分接入故障和项目既有缺陷」。

**代价（接受）**：镜头描述任务在 gate 里是**文本任务**，只校验非空；`parse_json_loose` 兜底解析。验收时必须人工检查该字段的实际产出。

**实测印证**（`v001` 运行，thread `v001`）：8 个任务中 `t_676905527d010e7d` 的 `json_schema` 为空（其余 7 个均为 `yes`），
该任务即 `storyline_understand_clips` 的镜头描述任务。Agent 对它的回答是纯文本：

> 如实说明 3 条素材描述全是错误、无画面帧,无法总结

后续项改法见 [§10.2](#102-后续项)。

### F6（P1-2）生产护栏 + preflight 开关

- `runner.run_worker()` 新增 `require_preflight: bool = False`，透传 `build_graph(run_preflight=...)`。
- **新增生产护栏** `runner.production_blocker()`：启动时检查 `os.environ.get("WORKFLOW_ENV") == "production"` 或 checkpointer backend 为 `postgres`，命中则写 `STATE_FAILED` + `events.jsonl: production_unsupported`，返回退出码 **3**，**不静默退回 SQLite**。
- CLI `start` 新增 `--require-preflight` 透传。

---

## 7. 分阶段执行步骤

### 阶段前置：统一变量

每个 PowerShell 窗口开头先设一次：

```powershell
$REPO = "E:\Documents\kuaishou\auto-video-editor"
$OPS  = "E:\Documents\kuaishou\video-ops"
$KIT  = "D:\Agent驱动auto-video-editor_交付包\2_代码\agent_gate_kit"
$PY   = "$REPO\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"     # 规避 Windows 中文 locale 下 pytest capture 的 UnicodeDecodeError
$env:PATH = "C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin;$env:PATH"   # ffmpeg 需在 PATH
Set-Location $REPO
```

> ⚠️ **ffmpeg 必须显式加入 PATH**。本机 ffmpeg 装在 `C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin`，
> 但**非交互 shell / 后台任务里不在 PATH** → kit 测试中 6 个用例会报
> `FileNotFoundError: [WinError 2] 系统找不到指定的文件`。这是环境问题，不是代码回归。

---

### P0 — 环境准备与基线快照 ✅

```powershell
git rev-parse HEAD          # 期望 61cf78d...
git status --short          # 期望无输出

& $PY -m pip install "mcp==1.26.0" "jsonschema==4.26.0"
& $PY -m pip check
& $PY -c "import mcp, jsonschema; print('deps OK', jsonschema.__version__)"

& $PY -m pytest tests -q --no-header -p no:cacheprovider --tb=no --junit-xml="$env:TEMP\baseline.xml"
$LASTEXITCODE
```

**产出**：`requirements.txt` 加两行依赖（注释一并修正）。**退出门槛**：`$LASTEXITCODE -eq 0`。

---

### P1 — 落地 `agent_gate` 包 ✅

```powershell
Test-Path "$REPO\agent_gate"        # 期望 False，True 则停下来备份后再决定

Copy-Item -Recurse "$KIT\agent_gate" $REPO
Copy-Item -Recurse "$KIT\tests"      "$REPO\agent_gate\tests"
Copy-Item -Recurse "$KIT\fixtures"   "$REPO\agent_gate\fixtures"

Get-ChildItem "$REPO\agent_gate" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

Get-ChildItem "$REPO\agent_gate" -Recurse -File | Measure-Object | Select-Object Count
& $PY -m agent_gate --help
```

同时写 `agent_gate/README.md`：最短用法 + **「kit 测试必须单独跑 `pytest agent_gate/tests`」的隔离说明**。

**产出**：`agent_gate/`（18 模块 + fixtures + 11 个测试文件，实测 71 个文件）、`agent_gate/README.md`。
**退出门槛**：`python -m agent_gate --help` 退出码 0。

---

### P2 — 应用接入补丁 ✅

**不要用 `git apply`**（patch 文件不进仓库），手工改这 2 个文件，严格按 §3 的语义：

1. `storyline_capabilities/understand_clips.py`
   - import 区加 `get_default_client`；
   - 逐镜头循环前加 `effective = client or get_default_client()`；
   - `(client or StubLLMClient()).chat(...)` → `effective.chat(...)`；
   - `if desc_lines and client is not None:` → `if desc_lines and (client is not None or not isinstance(effective, StubLLMClient)):`
2. `storyline_capabilities/select_bgm.py`
   - import 加 `get_default_client`；
   - `llm_client = get_default_client()`，当它 `isinstance(..., StubLLMClient)` 时才回退到 `StubLLMClient(default_caption='{"bgm_id": ""}')`。
3. `requirements.txt` 注释修正。

```powershell
git diff --stat -- storyline_capabilities/understand_clips.py storyline_capabilities/select_bgm.py requirements.txt
git diff
```

**退出门槛**：`git diff --stat` **只有这 3 个文件**，且差异语义与 §3 完全一致。多一行少一行都要停下来逐行核对。

---

### P3 — 安全与生命周期加固 ✅

**实现顺序**（严格按此顺序，后面的依赖前面的）：

| 步 | 做什么 | 落到哪 | 对应测试 |
| --- | --- | --- | --- |
| 1 | 新建 `safety.py`，实现 `safe_thread_id` / `safe_task_id` / `safe_gate_root` / `safe_task_path` / `safe_frame_dir` / `safe_media_path` / `safe_state_file` | `agent_gate/safety.py` | `test_path_safety.py` |
| 2 | `runner.gate_root` / `spawn_worker` / `run_worker`、`mailbox` 全部路径方法、`service.submit_result`/`get_task`、`mcp_server.mb_for`、`cli._mb`、`drive.py` 改走 `safe_*` | F1 | `test_path_safety.py` |
| 3 | MCP `run_start` 删 `initial_state_file`；`video_path` 过素材白名单；拒绝事件不泄露本机路径 | F2 | `test_input_guard.py` |
| 4 | `spawn_worker` 加 `mode` 与三条状态转换规则；`drive` / `human_resume` 显式传 mode | F3 | `test_lifecycle.py` |
| 5 | `validate.py` 抛 `InvalidSchema`；`service.submit_result` 转 `schema_invalid` 并关任务；`_create_task` 预检 schema | F4 | `test_schema_failclosed.py` |
| 6 | `run_worker` 生产护栏 + `require_preflight`；CLI `start --require-preflight` | F6 | `test_input_guard.py` |
| 7 | 记录后续项清单（F5 刻意不改 + 项目既有缺陷），P9 落文档 | F5 | — |

```powershell
& $PY -m pytest agent_gate/tests/test_path_safety.py agent_gate/tests/test_lifecycle.py `
           agent_gate/tests/test_schema_failclosed.py agent_gate/tests/test_input_guard.py -q -p no:cacheprovider
& $PY -m pytest agent_gate/tests -q -p no:cacheprovider
```

**退出门槛**：4 个新测试文件**全绿**；kit 原有 7 个测试文件**无回归**。

---

### P4 — 生成操作员目录 ✅

让 Agent 在一个**干净的、仓库外的**目录里启动，只拿到 6 个 MCP 工具。

```powershell
Set-Location $REPO
& $PY -m agent_gate init `
  --project-dir $OPS `
  --repo-root $REPO `
  --outputs-root "$REPO\outputs" `
  --python $PY `
  --graph-factory graph:build_graph `
  --env "STORYLINE_MODE=auto" `
  --env "AGENT_GATE_MEDIA_ROOTS=$REPO\inputs;$REPO\tests\fixtures"

Get-Content "$OPS\.mcp.json"

& $PY -m agent_gate mcode-lock `
  --base "$env:USERPROFILE\.minimax\config.yaml" `
  --out "$env:USERPROFILE\.minimax\config.locked.yaml"
```

**`.mcp.json` 逐项核对**：

- [x] `command` = `$REPO\.venv\Scripts\python.exe` 的绝对路径（正斜杠）
- [x] `env.PYTHONPATH` 含 repo 根；`env.AGENT_GATE_REPO_ROOT` 指向 repo 根
- [x] `env.STORYLINE_MODE=auto` 已写入 ← **`config.py` 导入时就读它**
- [x] `env.AGENT_GATE_MEDIA_ROOTS` 已写入
- [x] `timeout` = 150000
- [x] **没有** `--with-human-tool` → Agent 拿不到 `human_resume`

**`config.locked.yaml` 需人工收紧**（脚本不会自动做）：

- [ ] `permissionMode`：全局现在是 `bypassPermissions`，改成受限值
- [ ] `dataContribution`：显式关闭
- [x] 文件在仓库外，**绝不入库**（已含明文 `apiKey` 字段）

**产出**：`$OPS\`（6 个文件）、`%USERPROFILE%\.minimax\config.locked.yaml`。

---

### P5 — 主图 stub 冒烟（分水岭）✅

**不接 Agent**，先用 `--backend stub` 证明真实主图能跑到人工关卡。

```powershell
$env:STORYLINE_MODE = "auto"
$VIDEO = "$REPO\tests\fixtures\30s.mp4"

& $PY -m agent_gate start --thread-id smoke-stub --outputs-root "$REPO\outputs" `
   --graph-factory graph:build_graph --backend stub --video $VIDEO

Start-Sleep -Seconds 20
& $PY -m agent_gate status --thread-id smoke-stub --outputs-root "$REPO\outputs"
```

**实测结果**：`state = "waiting_human"`，`interrupts[0].checkpoint = "①"`，`error = null` → **通过**。

**诊断顺序（失败时严格按这个来）**：

1. `Get-Content "$REPO\outputs\smoke-stub\agent_gate\worker.log" -Tail 40`
2. `& $PY -m agent_gate events --thread-id smoke-stub --outputs-root "$REPO\outputs" --limit 30`
3. 若怀疑是项目本身问题 → 先复现原项目：`& $PY scripts\run_workflow.py --thread-id baseline-check --skip-preflight`
   **不要一上来就把错误归因到 agent_gate 适配层。**

---

### P6 — 任务信箱人工闭环 ✅

不用 Agent，用 CLI 手动走一遍「出题 → 提交 → worker 继续」。

```powershell
& $PY -m agent_gate start --thread-id smoke-gate --outputs-root "$REPO\outputs" `
   --graph-factory graph:build_graph --video $VIDEO

& $PY -m agent_gate tasks --thread-id smoke-gate --outputs-root "$REPO\outputs" --wait-s 120 --limit 1
```

拿到 `task_id` 后，**两步验证**：

```powershell
# a) 先提交空答案 —— 必须被拒
& $PY -m agent_gate submit --thread-id smoke-gate --outputs-root "$REPO\outputs" `
   --task-id <task_id> --answer ""

# b) 按该任务的真实画面提交有效答案
& $PY -m agent_gate submit --thread-id smoke-gate --outputs-root "$REPO\outputs" `
   --task-id <task_id> --answer-file "$env:TEMP\ans.json"
```

**实测结果**：`smoke-gate` 同样停在 `state = "waiting_human"` / `checkpoint ①` → **通过**。

---

### P7 — 回归对比 ✅（kit 侧）

**目的**：证明 P2 的接入改动和 P3 的加固**没有改变项目既有行为**。

```powershell
# 1) 项目测试：与 P0 基线逐项对比
& $PY -m pytest tests -q --no-header -p no:cacheprovider --tb=no --junit-xml="$env:TEMP\after.xml"
$LASTEXITCODE

# 2) kit 测试：单独跑
& $PY -m pytest agent_gate/tests -q -p no:cacheprovider `
   --junit-xml="$env:TEMP\kit_after.xml"
```

**实测结果**：

| 套件 | 结果 | 备注 |
| --- | --- | --- |
| `pytest agent_gate/tests` | **197 收集 / 180 通过 / 17 skip / 0 失败 / 0 错误**（394.7s） | 含 4 个 P3 新增测试文件（108 个用例） |
| `pytest tests` | 见 [§11 回归对比](#11-回归对比) | 项目侧基线对比 |

**kit 用例分布**（`--junit-xml` 权威计数）：

| 测试文件 | 用例数 | 归属 |
| --- | --- | --- |
| `test_path_safety.py` | 52 | **P3 新增（F1）** |
| `test_scaffold.py` | 31 | 交付包原有 |
| `test_input_guard.py` | 24 | **P3 新增（F2 + F6）** |
| `test_lifecycle.py` | 21 | **P3 新增（F3）** |
| `test_cli_client.py` | 15 | 交付包原有 |
| `test_gate.py` | 12 | 交付包原有 |
| `test_schema_failclosed.py` | 11 | **P3 新增（F4）** |
| `test_images.py` | 10 | 交付包原有 |
| `test_drive.py` | 10 | 交付包原有 |
| `test_agent_security.py` | 8 | 交付包原有 |
| `test_mcp.py` | 3 | 交付包原有 |
| **合计** | **197** | 其中 P3 新增 108 |

**17 个 skip 的如实归因**（**skip 不算 pass**）：

| 数量 | 用例 | 原因 |
| --- | --- | --- |
| 11 | `test_agent_security` / `test_cli_client` / `test_drive` / `test_images` 中的 `claude` 相关用例 | 本机**未安装 Claude Code** → `@needs_claude` 自动 skip |
| 1 | `test_path_safety::test_gate_root_is_idempotent_under_symlink` | Windows 创建符号链接需管理员 / 开发者模式 |
| 1 | `test_cli_client::test_timeout_kills_whole_process_tree` | 依赖 POSIX 进程组语义（`killpg`），Windows 不适用 |
| 1 | `test_agent_security::test_mcode_shell_cannot_resume_a_human_gate` | 依赖 mcode shell 工具的 POSIX 行为 |
| 3 | 其它 claude / mcode 环境相关用例 | 同上 |

> ⚠️ **ffmpeg 与 mcode 必须在 PATH**，否则相关用例会**误报失败或误 skip**：
> - `ffmpeg` 不在 PATH → 6 个用例报 `FileNotFoundError: [WinError 2]`（**失败**，非 skip）
> - `mcode` 不在 PATH → `shutil.which("mcode")` 返回 `None` → 10 个 mcode 用例被**误 skip**
>
> 本机实际值：`C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin` 与 `C:\Users\wayyet\.minimax-code` 均**不在**默认 PATH，需显式前置。

> 注：kit 测试耗时约 4–5 分钟，**必须用后台任务跑**，前台会超时。
> 另：未装 Claude Code 时相关用例会 skip —— **skip 必须写「skip」，不能算 pass**。

---

### P8 — Agent 实跑 + 人工关卡 ✅ 通过（2026-10-20 重跑）

**目的**：让 MiniMax Code 真正接管答题，一路跑到剪映人工关卡，再由真人放行。

**P8-1 无人值守跑到关卡**

```powershell
# Windows 上必须先把 mcode 放进 PATH,或用 AGENT_GATE_MCODE_BIN 指到 .CMD
# (CreateProcess 不做 PATHEXT 补全,裸名 "mcode" 必 WinError 2)
$env:AGENT_GATE_MCODE_BIN = "C:\Users\wayyet\.minimax-code\mcode.cmd"

& $PY -m agent_gate drive --thread-id v006 --agent mcode `
   --project-dir $OPS --mcode-config "$env:USERPROFILE\.minimax\config.locked.yaml" `
   --outputs-root "$REPO\outputs" --graph-factory graph:build_graph --video $VIDEO
$LASTEXITCODE

& $PY -m agent_gate status --thread-id v006 --outputs-root "$REPO\outputs"
& $PY -m agent_gate events --thread-id v006 --outputs-root "$REPO\outputs" --limit 50
```

> **实测踩坑**：第一次跑 `v005` 直接 `drive` 退出码 4（`stalled`），会话日志里是
> `找不到可执行文件 'mcode':[WinError 2]`。原因是本机 PATH 里**没有**
> `C:\Users\wayyet\.minimax-code`（`mcode.cmd` 只在那个目录里）。
> `resolve_agent_bin()` 只做 `shutil.which()`——目录不在 PATH 就找不到。
> 两个解法:①把该目录加进 PATH；②设 `AGENT_GATE_MCODE_BIN` 指到 `mcode.cmd`（本次用的就是这个）。
>
> **期望退出码 `10`** = 停在人工关卡，**不是失败**。

**P8-2 人工关卡放行**（必须真人在**交互终端**、repo 根目录内）

```powershell
& $PY -m agent_gate status --thread-id v006 --outputs-root "$REPO\outputs"

& $PY -m agent_gate resume --thread-id v006 --outputs-root "$REPO\outputs" `
   --graph-factory graph:build_graph --checkpoint "①" --value true
```

- 终端打不出 `①` 时用码点：`--checkpoint ([string][char]0x2460)`
- `resume` 在非交互环境会直接返回 `need_human_terminal` —— **这是设计的安全边界，不要用 `--force-no-tty` 绕过**
- 本轮实测依次放行 **① → ③ → ②**（③ 是条件触发：英文字幕排版复核），
  每次放行后 worker 重启继续跑，直到 `worker_state=completed`

**实测结果（2026-10-20 重跑，thread `v006`）**：

`events.jsonl` 事件统计（`v006`）：

| 事件 | 次数 | 含义 |
| --- | --- | --- |
| `task_created` | 12 | 出题 12 次 |
| `task_answered` | 12 | **Agent 作答 12 次，全部被收** |
| `agent_session` | 4 | mcode 起了 4 个会话，全部 `status=succeeded` |
| `waiting_human` | 3 | 依次到达人工关卡 `①` → `③` → `②` |
| `drive_finished` | 1 | `outcome=ask_human`, `sessions=4` → **drive 退出码 10** ✅ |
| `worker_started` | 4 | 首次启动 + 3 次人工 resume 后重启 |
| `worker_failed` | 0 | ✅ 无 |

`resume.json` = `{"①": true}` → **人工关卡放行成功** ✅（3 次全部 `RESUME_EXIT=0`，真实交互终端）。

**最终状态**：`worker_state=completed`、`answered_tasks=12`、`degraded_tasks=0`、`error=null`。

**端到端产物**（`outputs/v006/`）：

| 产物 | 说明 |
| --- | --- |
| `drafts/draft_content.json` + `draft_info.json` | 剪映草稿**双写**（剪映 5.9+ 要求二者一致），13.4 KB |
| `drafts/draft_meta_info.json` | 草稿元信息 |
| `drafts/covers/cover_{16x9,4x3,9x16}_{zh,en}.png` | 6 张封面 |
| `en_branch_.../draft_content.json` + `subtitle_en.json` + `subtitle_en.srt` | 英文分支草稿 + 英文字幕 |
| `snapshots/snapshot2/draft_content.json` | 节点 7 快照② |
| `assembly/preview.mp4` | 预览成片（78 KB） |
| `assembly/preview_qc_report.json` | QC `status=pass` |
| `normalized_sources/chain-test-12s_202x360.mp4` | 宽银幕 → 竖屏归一化产物 |

草稿内容实测：画布 1080×1920、4 条 video material、1 条 BGM（`bgm_calm_lofi`）、5 条字幕 material、
4 段 video segment（`target_timerange` / `source_timerange` 齐备）。

**上一轮（2026-10-19，`v001`）为什么失败**：

```json
{
  "state": "failed",
  "error": {
    "type": "TypeError",
    "message": "argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'NoneType'",
    "traceback": "... node_07_speed_fit.py\", line 91, in speed_fit\n    draft_path = Path(state[\"draft_path\"])"
  }
}
```

根因链（**全部在项目自己代码里**，与 agent_gate 无关）：

1. **D9（根因）**：`plan_timeline_pro` 产出的 plan 与 `CanonicalTimeline` 契约字段集不一致
   （`source_media` 多了 `path`/`metadata`/`orig_path`/`orig_md5`、少 `duration_ms`；`clips` 多了
   `voiceover_id`/`transition_*`/`subtitle_style`；顶层多了 `method`/`warnings`；
   `subtitles.zh` 被错填成**样式 dict** 而契约要求 **str**）→ `qa_gate` / `join_storyline` / `node_05`
   三处 `model_validate` 全挂 → `ContractInvalid` → `draft_path=None`。
2. **D7**：`node_07_speed_fit` 直接 `Path(state["draft_path"])`，**没有 None 防御** → `TypeError` → worker 失败。
3. **D10**：`plan_timeline_pro` 的 fan-in barrier 含 `split_shots`，而 qa_gate 超时重试回到 `group_clips`
   （`split_shots` 的**下游**，不会重跑）→ barrier 永远补不齐 → 图**静默 END**（`next=[]`），
   worker 却报「completed」。

> D5 / D6（`clip.kind` 为空、prompt 模板未渲染）已于 2026-10-19 修掉；
> **D7 / D9 / D10 于 2026-10-20 一并修掉**，见 [§10.5](#105-d7--d9--d10-修复记录2026-10-20)。

**判定**：

- ✅ **接入层（agent_gate）全绿**：出题 / 收答案 / schema 校验 / 人工关卡 / resume / drive 退出码 10，全部符合设计。
- ✅ **端到端出片完成**：剪映草稿生成、预览成片 QC `pass`、三个关卡依次放行后 worker `completed`。
- ⚠️ **A1-8 的一半仍未在本机验证**：本机**没装剪映客户端**，「草稿能在剪映里打开、字幕/分镜/转场/BGM 人工过目」
  这一步需在装了剪映 5.9.0 的机器上补做；草稿 JSON 本身结构完整、双写一致。
- ⚠️ **字幕内容是 Mock**：本机未配置云端 ASR（`ASR_NOT_CONFIGURED`），`node_08` 用 `Mock 字幕 N` 兜底。
  字幕**轨道的排布**是真实的，**文字内容**需接真实 ASR 后才有意义。

---

### P9 — 文档与提交

**目的**：把设计和验证结论固化进仓库，然后按用户约定直接提交 `main`。

**P9-1 写文档** → 本文 `docs/integration/Agent驱动auto-video-editor_设计与执行计划.md`。

**P9-2 提交前核验**

```powershell
git status --short
```

逐条确认**没有被纳入**：`config.locked.yaml`、`.mcp.json`、`*.sqlite`、`outputs/`、`checkpoints/`、`drafts/`、`logs/`、`inputs/`。

**P9-3 分 3 个 commit（直接 `main`，不建分支、不推 PR）**

| # | 内容 | 目的 |
| --- | --- | --- |
| C1 | `agent_gate` 包 + `requirements.txt` + 文档 | 纯新增，不改既有行为 |
| C2 | `understand_clips.py` / `select_bgm.py` | 接入补丁，便于单独 revert |
| C3 | 安全修复 F1–F4、F6 + 4 个新测试 | 便于单独 revert |

---

### 回滚总表

| 阶段 | 回退命令 |
| --- | --- |
| P0 | 无需（只装了 pip 包） |
| P1 | `Remove-Item -Recurse -Force "$REPO\agent_gate"` |
| P2 | `git checkout -- storyline_capabilities/understand_clips.py storyline_capabilities/select_bgm.py requirements.txt` |
| P3 | 同 P1 + 同 P2 |
| P4 | `Remove-Item -Recurse -Force $OPS` + 删除 `config.locked.yaml` |
| P5–P8 | 无需（验证性，产物在已 gitignore 的 `outputs/` 下） |
| P9 | `git revert --no-edit <C3>` → `<C2>` → `<C1>` |

> **不要**删 `outputs/`、`checkpoints/`、`drafts/` 里的既有数据。`config.locked.yaml` 按 §2 要求处理明文密钥。

---

## 8. 测试与验收标准

### A. 功能

| # | 判据 | 怎么验 | 状态 |
| --- | --- | --- | --- |
| A1-1 | `python -m agent_gate --help` 可用 | P1，退出码 0 | ✅ |
| A1-2 | MCP 工具可被 Agent 加载 | 事件日志有成功会话；`agent_sessions/` 有输出 | ✅ 3 会话全部 succeeded |
| A1-3 | 主图至少一个 LLM 任务由 Agent 作答，不是 Stub | `tasks/*.task.json` 数量 > 0 且与 `*.result.json` 一一对应 | ✅ 12/12(v004) |
| A1-4 | 画面帧真的传给了 Agent | `frames/<task_id>/*.jpg` 非空，任务视图里 `images.shown > 0` | ✅ **已修**(2026-10-19,D5):`storyline_understand_clips` 任务 `frames: 0` → `frames: 6` |
| A1-5 | `answered_tasks > 0` 且 `degraded_tasks == 0` | `agent_gate status` | ✅ 12 answered / 0 degraded(v004) |
| A1-6 | 崩溃恢复不重复提问 | 手动 kill worker → `continue_after_crash` → 已答任务不再出现 | ✅ `worker_started` 出现 4 次（首启 + 3 次人工 resume），12 个任务未重复 |
| A1-7 | 人工关卡真的暂停 | `worker_state=waiting_human`、`interrupts` 非空；Agent 会话**未**调用 resume | ✅ `interrupts[0].checkpoint = "①"`（`v006` 依次 ①→③→②） |
| A1-8 | 最终产物合理 | 剪映草稿可打开；字幕/分镜/转场/BGM 人工过目 | ⚠️ **草稿已生成**（`drafts/draft_content.json` + `draft_info.json` 双写、封面 6 张、preview QC `pass`），结构完整；**本机没装剪映客户端**，「能在剪映里打开 + 人工过目」需在装了剪映 5.9.0 的机器上补做 |

### B. 安全

| # | 判据 | 阶段 | 状态 |
| --- | --- | --- | --- |
| B-1 | `thread_id` 遍历攻击全被拒 | P3 | ✅ `test_path_safety.py` |
| B-2 | `task_id` 非 `^t_[0-9a-f]{16}$` 一律拒绝 | P3 | ✅ |
| B-3 | 拒绝发生在**三处**：CLI、MCP 服务、`Mailbox` 底层文件操作 | P3 | ✅ |
| B-4 | MCP `run_start` 不再有 `initial_state_file` 参数 | P3 | ✅ 已核对签名 |
| B-5 | `video_path` 超出 `AGENT_GATE_MEDIA_ROOTS` 被拒 | P3 | ✅ `test_input_guard.py` |
| B-6 | 非法 `json_schema` → `code=schema_invalid`，任务被关闭 | P3 | ✅ `test_schema_failclosed.py` |
| B-7 | 状态机：同 ID 重复 `start` / 无 checkpoint 的 `continue_after_crash` / 非 `waiting_human` 的 `resume` 全被拒 | P3 | ✅ `test_lifecycle.py` |
| B-8 | `WORKFLOW_ENV=production` 时 worker 拒绝启动 | P3 | ✅ `runner.production_blocker()` |
| B-9 | Agent 默认拿不到 `human_resume`；`resume` 在非交互终端返回 `need_human_terminal` | P4 + P8 | ✅ `.mcp.json` 无 `--with-human-tool`；`test_cli_resume_refuses_without_tty` |
| B-10 | 拒绝事件写进 `events.jsonl`，返回给 Agent 的 message 不含本机绝对路径 | P3 | ✅ `safety.rejection_message()` |
| B-11 | 没有任何密钥 / 视频 / 任务结果 / checkpoint 进入 git | P9-2 | ✅ 见 [§12](#12-提交前核验结果) |

### C. 环境与回归

| # | 判据 | 状态 |
| --- | --- | --- |
| C-1 | P0 基线与 P7 之后的项目测试**逐项一致**（多 0 少 0） | ✅ **954 / 0 失败 / 0 错误 / 11 skip**（M6），比基线多 35 条新用例，失败/错误仍为 0、**逐用例状态变化 0**（见 [§11](#11-回归对比)） |
| C-2 | `pytest agent_gate/tests` 全绿；claude 相关用例 skip 并如实记为 skip | ✅ **208 收集 / 0 失败 / 0 错误 / 17 skip**（M6）。17 条 skip 全是**环境限制**：14 条「本机没有 `claude`」+ 1 条「脚本用了 POSIX `env`」+ 1 条「Windows 上不跑 shell 假进程」+ 1 条「本机不支持符号链接」；**所有 `@needs_mcode` 用例真跑并通过**（前提：`C:\Users\wayyet\.minimax-code` 在 PATH 上，否则会被 skip 而不是跑） |
| C-3 | 记录版本 | ✅ 见 [§13](#13-版本与环境) |
| C-4 | 声明「只支持 SQLite 本地开发路径」，不声称 Postgres 已验收 | ✅ |
| C-5 | 声明平台与覆盖范围 | ✅ 见下 |

> **C-5 声明（实测口径）**：
> - **Windows 已实测**（本机 Windows 11 + PowerShell 7）。
> - **Linux 未复测**：交付包在 Linux 沙箱跑过 89 测试（Claude Code 2.1.302 / mcode 0.6.9），该结论**不作为本机验收依据**，只作参考。
> - **接入层端到端跑通**：真实主图上 8/8 任务由 MiniMax Code 作答、人工关卡①放行成功、drive 退出码 10。
> - **素材链路修复后重跑通过（2026-10-19）**：D5/D6 修好后，真实主图跑完 12 个 LLM 任务
>   （`degraded_tasks=0`）、每题 prompt **零 `{{...}}` 残留**、镜头描述**零 Error**、
>   画面帧 `frames: 6`、时间轴 `source_media_id=media_0001`、assembly 质检
>   `pass_with_warnings` 且 `preview.mp4` 已生成、**停在人工关卡 ①**。
>   ⚠️ 该轮答案是**人工手写**的：MiniMax Token Plan 额度已用尽（429），Agent 自动答题跑不了。
>   ⚠️ 本轮还修掉两个**只有 Windows 才暴露**的接入层缺陷（PATHEXT 裸名拉不起 mcode、
>   正在启动的 worker 被误判成 crashed），见 §10.4。
> - **端到端出片跑通（2026-10-20，`v006`）**：D7 / D9 / D10 修好后，MiniMax Code **自动**作答
>   12 个任务（4 个会话全部 `succeeded`，`degraded_tasks=0`）、drive 退出码 10 停在 ①、
>   真人在交互终端依次放行 ①→③→②，最终 `worker_state=completed`，
>   剪映草稿（双写）+ 6 张封面 + 预览成片（QC `pass`）全部产出。
> - **仍需人工补做**：① 本机没装剪映客户端，草稿「能在剪映里打开 + 字幕/分镜/转场/BGM 过目」待补；
>   ② 本机没配云端 ASR，字幕文字是 `Mock 字幕 N` 兜底（轨道排布是真实的）。
> - **真实模型答案质量需人工判断**：本次 Agent 如实报告了素材链路缺陷而未编造内容，但真实成片质量未评估。

---

## 9. 状态、并发、失败与恢复语义

### 9.1 状态机

```
starting ──> running ──┬──> waiting_human ──(人工 resume)──> starting
                       ├──> completed        (终态，同 ID 不可再 start)
                       ├──> failed
                       └──> crashed         (心跳过期 >30s，由 status 推断)
```

`next_action`（`service.status_summary`）驱动 Agent 与 `drive`：

| `next_action` | 触发 | Agent 该做什么 |
| --- | --- | --- |
| `start_run` | 无 `status.json` | 调 `run_start` |
| `answer_tasks` | 有 pending 任务 | `gate_next_tasks` → `gate_submit_result` |
| `wait` | worker 在算 | 再取一次 |
| `ask_human` | `waiting_human` | **停下并报告，禁止 resume** |
| `done` | `completed` | 汇报结果 |
| `restart_worker` | `crashed` | `run_start(continue_after_crash=true)`，最多 1 次 |
| `report_failure` | `failed` | 汇报 error，**不要自己重跑** |

### 9.2 任务身份与幂等

- `task_id = "t_" + sha256(canonical_json({system_prompt, user_prompt, media, json_schema}))[:16]`
- 相同请求 → 相同 ID → 崩溃重启后 `read_result` 命中即直接复用，**不会重复问 Agent**。
- ⚠️ **已知局限（本次未修）**：哈希**不含** `temperature` / `top_p` / `max_tokens`。同 prompt + 同 media + 不同参数会复用旧结果。
  当前项目这 3 个参数在各调用点都是常量，风险低；记入 §10.2 后续项。
- `mailbox.write_task()` 已存在则不覆盖（先到先得）。

### 9.3 并发

- worker 进程单例：`status.json` 原子写（临时文件 + `os.replace`，Windows 下 `PermissionError` 重试 10 次）。
- 心跳线程每 `AGENT_GATE_HEARTBEAT_S`（默认 5s）写一次，与主线程共用一把锁。
- Agent 侧可并发调多个 MCP 工具；信箱写入原子，`submit_result` 对同一 task 幂等（已答返回 `already_answered`）。
- `attempts` 计数用同一把 mailbox 原子写，不存在竞态丢失。

### 9.4 失败与恢复

| 场景 | 行为 |
| --- | --- |
| Agent 提交不合格 JSON | 返回 `errors` + `attempts_left`；用尽（默认 3 次）→ 写 `.error.json`，worker 抛 `LLMGateRejected`，被节点 `except Exception` 接住并降级 |
| 非法 schema | 立即 `schema_invalid` 并关闭任务（不消耗 attempts） |
| 等待超时 | 默认 `DEFAULT_TIMEOUT_S=1800` → 写 `.error.json`（reason=timeout）→ 抛 `LLMGateTimeout` |
| 抽帧失败 | **不阻塞**：任务照发，`notes` 里写明「抽帧失败 / 没有取到画面帧」 |
| 图片预算超限 | `MAX_IMAGES=12`；超出的任务**延后**并在 `deferred_tasks` 报数，绝不静默缺图 |
| worker 崩溃 | 心跳过期 → `crashed` → `continue_after_crash` 从最近 checkpoint 续跑，已答任务复用 |
| `resume` 匹配不到 checkpoint | `resume_pending_sync` 抛 `ValueError`，message 里列出**实际**可用 checkpoint 列表 |
| graph 既没结束也没挂起 | 判为 `failed`（`Incomplete`），不是静默成功 |
| 生产配置误启 | `production_unsupported`，退出码 3 |

---

## 10. 已知缺陷与后续项

### 10.1 项目既有缺陷

| # | 缺陷 | 位置 | 影响 | 本次实测表现 |
| --- | --- | --- | --- | --- |
| D1 | `aes_score` 在成功路径被丢弃 | `storyline_capabilities/understand_clips.py`（`clip_captions` 只含 `caption`/`clip_id`/`source_ref`） | `filter_clips` 按 `aes_score` 排序时全按 0 处理 | 交付包审计 P4 已确认；本项目现有单测不覆盖 |
| D2 | `storyline_local_asr` / `storyline_speech_rough_cut` 路由可能不可达 | `nodes/storyline/` | 部分节点不会被触发 | 本次未触发该路径 |
| D3 | 多个 JSON schema 只有 `properties` 没有 `required` | 项目内多处 | 字段可缺失，Agent 可能漏填 | 8 个任务中 7 个带 schema，其中 1 个（`t_676905527d010e7d`）完全无 schema（F5） |
| D4 | 镜头描述调用没传 schema | `storyline_capabilities/understand_clips.py:143` | 见 §6 F5 | Agent 只能给纯文本答案 |
| D5 | ~~**素材链路缺陷：`clip.kind` 为空**~~ **✅ 已修** | `storyline_capabilities/split_shots.py`（产出 clip 缺 `kind`/`path`/`source_ref`） | `Error: Clip kind not supported:` → 镜头描述全失败 | ✅ 2026-10-19 修复，见 §10.4 |
| D6 | ~~**prompt 模板变量未渲染**~~ **✅ 已修** | `storyline_capabilities/prompts/__init__.py`（`render_prompt` 只认 `{name}`，模板是 `{{name}}`）+ 8 个调用点漏传变量 | `{{user_request}}` / `{{clip_captions}}` / `{{clip_number}}` / `{{duration_budget}}` / `{{style}}` 原样残留 → 下游拿空数据 | ✅ 2026-10-19 修复，见 §10.4 |
| D7 | ~~`node_07_speed_fit` 不防御 `draft_path=None`~~ **✅ 已修** | `nodes/node_07_speed_fit.py`（另修 `node_08` / `node_09` / `node_10`） | `TypeError` 直接崩 worker，无友好降级 | ✅ 2026-10-20 修复，见 §10.5 |
| D8 | `node_05_generate_draft` 早退时把 `draft_path` 置 `None` 且下游未检查 | `nodes/node_05_generate_draft.py:71-76`、`:112-119` | 早退信息只进 `error_log`,下游看不到 | ✅ 2026-10-20 随 D7 一起修（下游全部降级跳过并写 `error_log`） |
| D9 | ~~**生产端 / 契约端字段集不一致**~~ **✅ 已修** | `storyline_capabilities/plan_timeline_pro.py`(生产)vs `storyline/contract.py`(校验,`extra="forbid"`) | `node_05` 的 `CanonicalTimeline.model_validate()` 必抛 `ContractInvalid` → `draft_path=None` → D7 | ✅ 2026-10-20 修复，见 §10.5 |
| D10 | **qa_gate 超时重试回环后 fan-in 屏障死锁**（图静默 END） | `graph.py`（`plan_timeline_pro` 的 fan-in 含 `split_shots`） | 重试回到 `group_clips`（`split_shots` 下游，不重跑）→ barrier 补不齐 → `next=[]`，worker 却报 `completed` | ✅ 2026-10-20 修复，见 §10.5 |

> D5–D8 是 2026-10-19 P8 实跑**新发现**的,之前不在审查版 §4.2 清单里;D9 / D10 是 2026-10-20 重跑时新定位的。
> **D5–D10 已于 2026-10-19 ~ 2026-10-20 全部修掉**（D5/D6 见 §10.4，D7/D9/D10 见 §10.5）。

### 10.4 D5 / D6 修复记录(2026-10-19)

D5 / D6 属于**素材链路**——「Agent 拿不到素材,也没拿到正确的问题」,是端到端出片的
前置条件,因此本次一并修掉(计划里的 F3 / F4 两项后续项)。

#### D5：clip 契约不完整 → 画面全丢

| 环节 | 修复前 | 修复后 |
| --- | --- | --- |
| `split_shots` 产出的 clip | 只有 `clip_id` / `source_in_ms` / `source_out_ms` / `timeline_*` / `split_method` | 追加 `kind="video"`、`path`、`source_ref{media_id,start,end,duration}` |
| `split_shots` 关联 media | 只写 `media_id` 到顶层 | 顶层 `media_id` + `path` + `source_ref.media_id` 都写全 |
| `understand_clips` 产出的 caption | 只有 `clip_id` / `caption` / `source_ref` | 追加顶层 `media_id` / `source_in_ms` / `source_out_ms` / `timeline_*`(**必须**:`plan_timeline_pro` 读的就是这几个顶层字段,少了时间轴就退化成 `source_media_id=null` + `source_in_ms=0`,剪映草稿绑不上源素材) |

改的文件:`storyline_capabilities/split_shots.py`、`storyline_capabilities/understand_clips.py`。

#### D6：prompt 模板变量一个都没渲染

根因有两层,**缺一不可**:

1. **语法不匹配**：模板正文用 **双花括号** `{{name}}`(与 vendored
   `open_storyline.utils.prompts.get_prompt` 的 `re.sub(r"{{(.*?)}}")` 一致),
   而本地 `render_prompt` 只替换 **单花括号** `{name}` → 所有占位符原样残留。
2. **调用点漏传变量**：即使语法对了,8 个 `render_prompt` 调用点里
   `filter_clips` 传 `captions=`(模板要的是 `clip_captions`)、`group_clips` 传
   `groups=`(模板要的是 `clip_captions` / `clip_number` / `duration_budget` /
   `selected_clips`)、`generate_script` 少传 `user_request` / `overall`、
   `recommend_text` 少传 `candidates` / `user_request`、
   `recommend_transition` 少传 `user_request` —— **变量名对不上,值也没给全**。

修复:

| 改动 | 说明 |
| --- | --- |
| `storyline_capabilities/prompts/__init__.py` | `render_prompt` 一次正则同时支持 `{{name}}` 与 `{name}` / `{name:?}`;**未提供的变量保持原样**(宁可让调用方看到残留,也不静默把空数据喂给模型);新增 `unresolved_placeholders()` 自检函数 |
| `storyline_capabilities/duration_budget.py`(新增) | 本地化 vendored `utils/duration_budget.py`( storyline 能力层要求无 vendored 依赖),产出 `{{duration_budget}}` 文本 |
| `filter_clips` / `group_clips` / `generate_script` / `recommend_text` / `recommend_transition` | 补齐模板需要的全部变量;新增 `user_request` / `target_duration_ms` 入参 |
| `nodes/storyline/_common.py` | 新增 `resolve_lang` / `extract_user_request` / `target_duration_ms`,把 `state.storyline_targets` 里的用户要求与目标时长透传给能力层 |
| 5 个节点壳子 | 透传 `lang` / `user_request` / `target_duration_ms`;`node_generate_script` 额外读 `understanding.overall` 填 `{{overall}}` |

#### 修复后的实测证据

| 证据 | 修复前(v001) | 修复后(v004) |
| --- | --- | --- |
| `storyline_understand_clips` 任务帧数 | `frames: 0` | `frames: 6` |
| 镜头描述 caption | 3 条 `Error: Clip kind not supported:` | 4 条真实描述,**0 条 Error** |
| 传给 Agent 的 media | 空 | `{"path": "...mp4", "in_sec": 0.0, "out_sec": 3.0}` |
| 各任务 prompt 残留 `{{...}}` | 5 类占位符原样残留 | **0**(`residue=[]`) |
| 任务总数 / 降级 | 8 题 / — | 12 题 / `degraded_tasks=0` |
| 时间轴 `source_media_id` | — | `media_0001`(修复前是 `null`) |
| 到达人工关卡 ① | ✅ | ✅ `waiting_human` / `next_action=ask_human` |
| assembly 质检 | 未跑到 | `pass_with_warnings`,`preview.mp4` 已生成 |

新增单测:`tests/unit/test_storyline_asset_chain.py`(16 条)——
覆盖 clip 契约、占位符两种语法、残留检测、以及「clip 时间区间一路带到时间轴」。

> **冒烟素材**:本轮用的是 `inputs/chain-test-12s.mp4`(ffmpeg lavfi 合成的 12 秒 4 场景测试片,
> `inputs/` 已被 `.gitignore` 忽略,不入库)。生成命令见下,可用它复现:
>
> ```powershell
> ffmpeg -y -f lavfi -i "testsrc=size=640x360:rate=10:duration=3" `
>        -f lavfi -i "smptebars=size=640x360:rate=10:duration=3" `
>        -f lavfi -i "color=c=navy:size=640x360:rate=10:duration=3" `
>        -f lavfi -i "gradients=size=640x360:rate=10:duration=3" `
>        -filter_complex "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0" -pix_fmt yuv420p `
>        inputs/chain-test-12s.mp4
> ```
>
> 它在 `AGENT_GATE_MEDIA_ROOTS` 白名单内,`--video` 可直接用。

#### 顺带修掉的接入层缺陷(不修就跑不了重跑)

| 缺陷 | 现象 | 修法 |
| --- | --- | --- |
| `agent_gate/launcher.py` 在 Windows 上拉不起 `mcode` | `找不到可执行文件 'mcode':[WinError 2]` → 连续 2 个会话没进展 → `drive` 退出码 4 | `CreateProcess` 不做 PATHEXT 补全,裸名必失败。新增 `resolve_agent_bin()`,用 `shutil.which()` 解析成真正的 `mcode.CMD` / `claude.exe` 再执行 |
| `agent_gate/service.py` 把正在启动的 worker 误判成崩溃 | `spawn_worker` 先写一份 `state="starting"`,worker 进程还要好几秒才写出第一次心跳;判定却用 `STALE_AFTER_S`(测试里 1.5s)→ **正常启动被判 crashed** → `drive` 误报退出码 5,甚至去重启一个好好着的 worker | `protocol.py` 新增 `STARTING_STALE_AFTER_S`(默认 120s,可用 `AGENT_GATE_STARTING_STALE_AFTER_S` 覆盖);`status_summary` 里 `starting` 与 `running` **用不同阈值**,`running` 的崩溃检测不受影响 |
| `.mcp.json` 的 `env` 里没有 `PATH` | worker 进程里 `ffmpeg` / `ffprobe` 不可见 → 抽帧全失败 | 在操作员目录的 `.mcp.json` `env.PATH` 里补上 ffmpeg 目录(仓库外文件,不入库) |

配套回归测试:`agent_gate/tests/test_agent_bin.py`(7 条)+ `agent_gate/tests/test_heartbeat.py`(4 条)。

> ⚠️ 这两条接入层缺陷**在交付包的 Linux 沙箱里测不出来**:PATHEXT 补全是 Windows 独有行为;
> worker 启动耗时也是 Windows 明显更慢(import langgraph + 拉子进程)。

#### 仍未解决(不在本次范围)

> **D7 / D8 / D9 / D10 已于 2026-10-20 修掉**,见 §10.5。2026-10-19 这一轮当时记的「仍未解决」清单如下,保留作对照。

- **D9(2026-10-19 定位,2026-10-20 已修)——最终剪映草稿生成不出来**:
  `storyline_capabilities/plan_timeline_pro.py` 产出的 plan 里,每个 clip 多了
  `voiceover_id` / `transition_in` / `transition_out` / `subtitle_style`,
  顶层多了 `method` / `warnings`,且 `subtitles.zh` 是 dict 而契约要求 str;
  而 `storyline/contract.py` 的 `CanonicalTimeline` 是 `extra="forbid"`。
  → `node_05` 的 `CanonicalTimeline.model_validate()` **必然**抛 `ContractInvalid`
  → 早退 → `draft_path = None` → 关卡 ① 之后 resume 时触发 D7 的 `TypeError`。
- **D7 / D8**:`draft_path=None` 未防御 → `node_07_speed_fit` 抛 `TypeError`。
  2026-10-19 这一轮**停在人工关卡 ①**,尚未触发关卡 ① 之后的 resume,因此 D7 未复现。
- **D1**:`aes_score` 成功路径仍被丢弃(Non-goals 2 明确不修)。
- **真实模型答案质量**:2026-10-19 那一轮 `v004` 的答案是**人工手写**的(见下),不是模型产出;
  MiniMax Token Plan 额度已用尽(429 `已达到 Token Plan 用量上限`)。
  → 2026-10-20 额度恢复,`v006` 跑的是**模型自动作答**(4 个会话全部 `succeeded`)。

### 10.2 后续项

| # | 项 | 具体改法 | 需要连带调整的测试 |
| --- | --- | --- | --- |
| F1 | 给镜头描述任务补 `json_schema`（解 F5） | 在 `understand_clips.py:143` 的调用传 `json_schema={"type":"object","properties":{"caption":{"type":"string"},"aes_score":{"type":"number"}},"required":["caption"]}` | ⚠️ 补上后 `answer_format` 从 `text` 变 `json`，**`stub` 后端行为会变**（`StubLLMClient` 改走 `_fake_obj_from_schema`，返回 `{"caption":"","aes_score":0.0}` 而非 `"no caption"`）→ 必须同步改 `tests/` 里依赖 `"no caption"` 的断言；还需新增 `required` 字段校验用例 |
| F2 | 任务哈希纳入推理参数 | `task_id` 计算时把 `temperature` / `top_p` / `max_tokens` 加进 `canonical_json` 的输入 | `agent_gate/tests/test_gate.py::test_task_id_stable_and_sensitive` 的期望值会变，需重算；还要补一条「参数不同 → ID 不同」的用例 |
| F3 | ~~修 D5:`clip.kind` 取值~~ **✅ 已修**(2026-10-19) | 追 `clip` 字典的构造点,确保 `kind` 落在 `understand_clips` 允许列表内 —— 实际还发现 `understand_clips` 丢了顶层时间字段,一并补齐(见 §10.4) | ✅ 已加:`tests/unit/test_storyline_asset_chain.py` |
| F4 | ~~修 D6:prompt 模板渲染~~ **✅ 已修**(2026-10-19) | 追 prompt 拼装点,确认 `{{user_request}}` 等占位符在调用前被替换 —— 实际是**语法不匹配**(`{name}` vs `{{name}}`)+ **8 个调用点漏传变量**,两层都修了(见 §10.4) | ✅ 已加:同上,含残留检测与「渲染后不含 `{{...}}`」断言 |
| F5 | ~~修 D7/D8/D9/D10~~ **✅ 已修**(2026-10-20) | ①契约与生产端字段集对齐(D9):`CanonicalTimeline` 显式声明 `method`/`warnings`,`Clip` 显式声明 `voiceover_id`/`transition_in`/`transition_out`/`subtitle_style`——**保持 `extra="forbid"` 不放宽**;生产端 `normalize_source_media()` 把 `load_media` 的运维形状整形成契约形状;`subtitles.zh` 改成**旁白文本**、样式移到 `clips[*].subtitle_style`。②下游节点 `draft_path=None` 统一降级跳过(D7/D8)。③`plan_timeline_pro` 的 fan-in 去掉 `split_shots`(D10) | ✅ 已加:`tests/unit/test_storyline_contract_alignment.py`(12 条)+ `tests/unit/test_draft_path_degradation.py`(7 条) |
| F6 | 补 `required`（D3） | 逐个 schema 补 `required` | 会让原本能通过的答案变成不合格 → 需同步放宽/调整 Agent 侧提示词 |

### 10.3 本次明确不覆盖的验证

- **Claude Code**：本机未安装 → 未验证。只给后续步骤（见 §14）。
- **Linux**：交付包在 Linux 沙箱跑过 89 测试；本机是 Windows，**不复用该结论**。
- **Postgres 生产**：未实现、未验证（worker 显式拒绝启动）。
- **真实模型答案质量**：无法自动化，需人工判断。
- **交付包 `3_脚本/`**：里面脚本指向原 Linux 沙箱路径，**不迁移、不执行**。
- **交付包 `4_验证记录/` 的 89-passed 结论**：只作参考，**不作为本机验收依据**。

### 10.5 D7 / D9 / D10 修复记录(2026-10-20)

上一轮(2026-10-19)P8 的结论是「端到端出片未通过」,阻断点有三个,**全在项目自己代码里**,
与 `agent_gate` 接入层无关。本轮按用户要求把 P8 门禁跑通,这三个一并修掉。

#### D9：生产端 / 契约端字段集不一致（根因）

`plan_timeline_pro` 的产出必须能过 `CanonicalTimeline`（`extra="forbid"`）,
但两边字段集不同。实测 **20 处校验错误**：

| 位置 | 差异 | 修法 |
| --- | --- | --- |
| `source_media[]` | 多了 `path` / `metadata` / `orig_path` / `orig_md5`;少了顶层 `duration_ms` | 生产端新增 `normalize_source_media()`:把 `metadata.duration_ms` 提到顶层,`width/height/fps` 一并提取,冗余字段丢掉;`file_uri` 缺失时从 `path` 推导 |
| `clips[]` | 多了 `voiceover_id` / `transition_in` / `transition_out` / `subtitle_style` | **契约显式声明**这 4 个可选字段（不放宽 `extra`）——这几个字段下游 mapper / 剪映草稿要用,不能删 |
| 顶层 | 多了 `method` / `warnings` | **契约显式声明**这 2 个可选字段 |
| `subtitles.zh` | 被错填成**字幕样式 dict**,契约要求 `str` | 改成**旁白文本**:新增 `group_scripts` 入参,取 `generate_script` 的 `narration` 按 group 顺序拼接;样式改挂到 `clips[*].subtitle_style`（无 per-group 样式时回落 `default`） |

> **为什么是「显式加字段」而不是「放宽 `extra="allow"`」**：
> `extra="forbid"` 的价值就是**接住未知字段漂移**。放宽成 `allow` 之后,
> 任何上游字段集变化都会被静默吞掉,契约就退化成摆设。
> 显式声明 = 字段有名字、有类型、有文档,仍然拒绝没声明过的字段。

> `duration_ms` 兜底:素材没报时长时,用引用它的 clip 的最大 `source_out_ms` 顶上,
> 否则契约不变量 `source_out_ms <= duration_ms` 会因 `duration=0` 误判失败。

#### D7 / D8：`draft_path=None` 的防御

`node_05` 契约不合法时早退,`draft_path` 是 `None`。原来 `node_07` 直接
`Path(state["draft_path"])` → `TypeError` → 整个 worker 被带走。

改成**统一降级跳过**（与 `node_13` 既有写法一致：写 `error_log` + `status_log` tag,不抛）：

| 节点 | 改前 | 改后 |
| --- | --- | --- |
| `node_07_speed_fit` | `Path(state["draft_path"])` | `state.get("draft_path")` 为空 → 跳过 + `node_07_speed_fit_skipped` |
| `node_07.route_after_speed_fit` | `Path(state["draft_path"])` | 为空 → 放行到 `node_08_add_subtitles`（让下游自己降级） |
| `node_07.get_snapshot2_path` | `Path(state["draft_path"])` | 为空 → 返回 `None` |
| `node_08_add_subtitles` | `Path(state["draft_path"])` | 为空 → 跳过 + `node_08_add_subtitles_skipped` |
| `node_09_inject_fx` | `raise FileNotFoundError` | 为空 → 跳过 + `node_09_inject_fx_skipped` |
| `node_10_inject_text_fx` | `Path(state["draft_path"])` | 为空 → 跳过 + `node_10_inject_text_fx_skipped` |

> 降级 ≠ 静默:每个跳过都写 `error_log` + `status_log`,图能继续往下走,
> 人工从日志一眼看出「草稿没生成」。

#### D10：qa_gate 重试回环后 fan-in 屏障死锁（图静默 END）

D9 修好后暴露出来的**第三个**问题:qa_gate 发现时间轴超时会 `storyline_qa_retry`,
路由回 `storyline_group_clips` 重跑。但——

`plan_timeline_pro` 的 fan-in 是 `add_edge([...], target)` 的**屏障**语义
（等所有 source 完成才触发目标一次）。原来的 source 列表里有 `storyline_split_shots`,
而它是 `group_clips` 的**上游**——重试时**不会重跑**。屏障渠道首次触发后被清空,
第二轮只有 `group_clips` / `generate_script` / `generate_voiceover` / `select_bgm` 会重新写入,
`split_shots` 的名字**永远补不回来** → `plan_timeline_pro` 再也不触发 → 图在
`recommend_transition` 处**静默 END**（`next=[]`）,而 worker 因为 `next` 为空
反而报「**completed**」。

修法:把 `split_shots` 从 PTP 的 fan-in 列表里去掉。`split_shots` 在
`split_shots → understand_clips → filter_clips → group_clips` 这条链上,
`group_clips` 完成即蕴含 `split_shots` 完成,**去掉它不放松任何时序约束**。

修完的实测(superstep 级):

```
step 26: storyline_plan_timeline_pro   ← 第 2 轮 PTP 正常触发
step 29: storyline_qa_gate              ← 第 2 次 qa
step 38: storyline_plan_timeline_pro   ← 第 3 轮
step 41: storyline_qa_gate
step 50: storyline_plan_timeline_pro   ← 第 3 次(重试上限 3)
step 53: storyline_qa_gate              ← retry_count=3 → 强制走 join
step 56: storyline_join
step 57: generate_draft                ← 草稿生成 ✅
step 63: __interrupt__                  ← 人工关卡 ①
```

#### 配套回归测试

| 文件 | 条数 | 覆盖 |
| --- | --- | --- |
| `tests/unit/test_storyline_contract_alignment.py` | 12 | 生产端输出过契约 / 契约仍拒未知字段 / `subtitles.zh` 是文本 / `source_media` 整形 / 时长兜底 / 新字段可用 |
| `tests/unit/test_draft_path_degradation.py` | 7 | 6 个节点的 `draft_path=None` 降级 / PTP fan-in 不含 `split_shots` / 重试边仍指 `group_clips` |

---

## 11. 回归对比

### 11.1 测量方法

P0 阶段产出的 `$env:TEMP\baseline.xml` 在后续维护中已被清理，无法复用。
因此重新用**四种环境**分别测量同一套 `pytest tests`，用**交叉验证**的方式定位每个失败的归属：

| # | 测量 | 环境 | 素材 | `agent_gate/` 在位 |
| --- | --- | --- | --- | --- |
| M1 | `baseline_true` | 独立 worktree @ `61cf78d` | ✅ 已放 | ❌ |
| M2 | `baseline_rebuild` | 独立 worktree @ `61cf78d` | ❌ 缺 | ❌ |
| M3 | `baseline_final` | 主工作区，`git stash` 掉改动 | ✅ | ✅ |
| M4 | **`after_p9`** | **主工作区，含 P2/P3 改动** | ✅ | ✅ |
| M5 | **`after_d5d6`** | **主工作区，含 D5/D6 修复**（2026-10-19） | ✅ | ✅ |
| M6 | **`after_p8fix`** | **主工作区，含 D7/D9/D10 修复**（2026-10-20） | ✅ | ✅ |

### 11.2 实测结果

| 测量 | tests | failures | errors | skipped | 失败用例 |
| --- | --- | --- | --- | --- | --- |
| M1 `baseline_true` | 919 | 1 | 0 | 11 | `test_no_auto_import_video::test_button_click_unblocks_graph_and_import_succeeds` |
| M2 `baseline_rebuild` | 919 | 1 | 0 | 35 | 同上（另 +24 个素材相关 skip） |
| M3 `baseline_final` | 919 | 1 | 0 | 11 | `test_phase5_e2e::test_phase5_decoupling_no_runtime_dependencies` |
| **M4 `after_p9`** | **919** | **0** | **0** | **11** | **无** ✅ |
| **M5 `after_d5d6`** | **935** | **0** | **0** | **11** | **无** ✅ |
| **M6 `after_p8fix`** | **954** | **0** | **0** | **11** | **无** ✅ |

> M6 比 M5 多 **19** 条 = `tests/unit/test_storyline_contract_alignment.py`(12)
> + `tests/unit/test_draft_path_degradation.py`(7);
> failures / errors 仍为 **0**,skipped 仍为 **11**（未新增跳过）。
> **逐个用例比对**:仅 P0 基线有 **0** 个、仅 M6 有 35 个（全是新增用例）、
> **状态变化 0 个** → D7/D9/D10 的修复**没有改变任何既有用例的行为**。
> ⚠️ 跑测试前必须把 ffmpeg 放进 PATH（`C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin`），
> 否则 `tests/unit/video_edit_capabilities/` 会有 7 条因「找不到 ffmpeg」误报失败——
> 这是**环境问题，与代码无关**。

### 11.3 归属判定

**用例总数四次测量完全一致（919）** → P2 的接入补丁与 P3 的安全加固**没有增删、跳过或改变任何用例**。

两个基线失败**都不是 P2/P3 引入的**，而是测量环境的产物：

| 失败用例 | 出现于 | 根因 | 是否 P2/P3 引入 |
| --- | --- | --- | --- |
| `test_no_auto_import_video::test_button_click_unblocks_graph_and_import_succeeds` | M1 / M2 | 依赖主工作区的运行时状态（独立 worktree 里没有 `outputs/`、`checkpoints/` 等既有数据）。在 M3 / M4 的主工作区里**通过** | ❌ 否 |
| `test_phase5_e2e::test_phase5_decoupling_no_runtime_dependencies` | M3 | 原始 import 守卫禁止 `import mcp`，而 `agent_gate/` 正是 MCP 文件信箱实现 → 守卫命中。**P3 对 `scripts/assembly/phase5_e2e.py` 的改动（`agent_gate` 受控豁免）正好修掉这一条** | ❌ 否（反而是本次修掉的） |

### 11.4 结论

- **改动后 954 个用例 0 失败 / 0 错误**（M6），比任一基线都严格更优。
- 计划的 C-1 判据是「与基线逐项一致（多 0 少 0）」→ 实测**多 0、少 1**，满足且优于该判据。
- **逐用例比对：状态变化 0 个** —— D7/D9/D10 的修复没有改变任何既有用例的行为，
  新增的 19 条用例是纯增量。
- 唯一的 skip 差异（11 vs 35）是 M2 缺本地素材导致，与代码无关。
- `pytest agent_gate/tests`：**208 收集 / 0 失败 / 0 错误 / 17 skip**（M6，与 M5 逐用例一致，状态变化 0）。
  比上一轮多 11 条 = 新增的 `test_agent_bin.py`(7) + `test_heartbeat.py`(4);
  skip 从 27 降到 17,因为 `mcode` 已在 PATH 上 → **mcode 相关的 10 条用例从「skip」变成「真跑并通过」**。
  > ⚠️ **本机 `C:\Users\wayyet\.minimax-code` 默认不在 PATH**。不把它加进 PATH,
  > 这 10 条会被**静默 skip**（不是 pass!)——数字上看不出来,只有逐用例比对才看得见。
  > 本轮第一次跑 kit 就踩了这个坑,已重跑修正。

---

## 12. 提交前核验结果（P9-2）

### 12.1 2026-10-20（D7/D9/D10 修复轮）核验

`git status --short` 实测输出（提交前）：

```
 M docs/integration/Agent驱动auto-video-editor_设计与执行计划.md
 M graph.py
 M nodes/node_07_speed_fit.py
 M nodes/node_08_add_subtitles.py
 M nodes/node_09_inject_fx.py
 M nodes/node_10_inject_text_fx.py
 M nodes/storyline/node_plan_timeline_pro.py
 M storyline/contract.py
 M storyline_capabilities/plan_timeline_pro.py
?? tests/unit/test_draft_path_degradation.py
?? tests/unit/test_storyline_contract_alignment.py
```

禁入项逐条核对（与 §12.2 同一批规则）：

| 项 | 结果 |
| --- | --- |
| `config.locked.yaml` | ✅ 未纳入（在 `C:\Users\wayyet\.minimax\`，含明文 `apiKey`） |
| `.mcp.json` | ✅ 未纳入（在仓库外的 `E:\Documents\kuaishou\video-ops\`） |
| `outputs/` / `checkpoints/` / `drafts/` / `logs/` / `inputs/` | ✅ 全部未纳入（`.gitignore` 已忽略） |
| `*.sqlite` | ✅ 无任何 sqlite 文件进入暂存区 |
| 剪映草稿 / 封面 / 预览成片 | ✅ 全在 `outputs/v006/` 下（已忽略） |

> `docs/分析/auto-video-editor-main-langgraph-node-audit.md` 是本轮**之前**就存在的未跟踪文件，
> 与本次修复无关，**不纳入**本次提交。

### 12.2 2026-10-19（首次接入轮）核验

`git status --short` 实测输出（提交前）：

```
 M requirements.txt
 M scripts/assembly/phase5_e2e.py
 M storyline_capabilities/select_bgm.py
 M storyline_capabilities/understand_clips.py
?? agent_gate/
?? docs/integration/Agent驱动auto-video-editor_设计与执行计划.md
```

### 禁入项逐条核对

| 项 | 结果 |
| --- | --- |
| `config.locked.yaml` | ✅ 未纳入（在 `C:\Users\wayyet\.minimax\`，含明文 `apiKey`） |
| `.mcp.json` | ✅ 未纳入（在仓库外的 `E:\Documents\kuaishou\video-ops\`） |
| `*.sqlite` | ✅ 无任何 sqlite 文件进入暂存区 |
| `outputs/` | ✅ 未纳入（`.gitignore` 已忽略） |
| `checkpoints/` | ✅ 未纳入 |
| `drafts/` | ✅ 未纳入 |
| `logs/` | ✅ 未纳入 |
| `inputs/` | ✅ 未纳入 |

### 密钥扫描

| 范围 | 结果 |
| --- | --- |
| `sk-` 模式（`agent_gate/` 全目录） | 仅 1 处命中：`agent_gate/tests/support.py:72` 的 `"sk-ant-api03-fake-local-mock-key"` —— **本地假模型服务的测试桩**，配 `ANTHROPIC_BASE_URL=http://127.0.0.1:{port}` + 隔离 `HOME`，无法认证任何真实服务，可安全入库 |
| `sk-` 模式（新文档） | 0 命中 |
| 赋值型 `api_key` / `password` / `Bearer` | 0 命中 |

### 本机绝对路径检查

`agent_gate/` 目录内仅 `README.md:16` 一处出现本机路径（`$REPO = "E:\Documents\kuaishou\auto-video-editor"`，用法示例）。
其余 `D:/o/v1/...` 均为 `test_scaffold.py` 里的**合成**测试夹具路径，非真实路径。

---

## 13. 版本与环境

| 项 | 本机实测值 |
| --- | --- |
| 操作系统 | Windows 11（10.0.26300 x64） |
| Shell | PowerShell 7.6.7 |
| Python | 3.13.11（`.venv`） |
| `mcp` | 1.26.0 |
| `jsonschema` | 4.26.0 |
| `langgraph` | 1.2.9 |
| `langgraph-checkpoint-sqlite` | 3.1.1 |
| ffmpeg | 9.0.2（`C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin`，**需手动加入 PATH**） |
| MiniMax Code | 0.6.11（`C:\Users\wayyet\.minimax-code\mcode.CMD`） |
| Claude Code | ❌ 未安装 |
| checkpointer | SQLite（`checkpoints/<thread_id>.sqlite`） |
| `STORYLINE_MODE` | `auto`（19 节点确定性图） |

**交付包沙箱基线（仅参考）**：Claude Code 2.1.302 / mcode 0.6.9 / Python 3.13.11 / 89 passed in 280.30s（Linux）。

---

## 14. Claude Code 后续步骤

本机**未安装** Claude Code，本次未验证。按以下步骤补齐：

1. **安装**（Windows PowerShell，二选一）：

   ```powershell
   irm https://claude.ai/install.ps1 | iex
   # 或
   winget install Anthropic.ClaudeCode
   ```

2. **确认可用**：

   ```powershell
   claude --version
   ```

3. **交互登录**（`claude -p` 非交互模式需要已有登录态；首次必须真人交互完成）：

   ```powershell
   claude
   # 在交互界面完成登录 / 订阅确认
   ```

4. **与加锁配置的对照说明**：

   - Claude Code 侧的权限收紧靠 **`.claude/settings.json` 的 `permissions.deny`** + **`.claude/hooks/guard_hook.py` 的 `PreToolUse` 钩子**，不用 `config.locked.yaml`（那是 mcode 的机制）。
   - **默认不开 `--with-human-tool`**：`agent_gate init` 不加这个参数时，Agent 拿不到 `human_resume` 工具 → 人工关卡只能由人在交互终端 `resume`。
   - 实测过的事实（交付包沙箱）：`permissions.deny` 会让 `mcp__video-agent__human_resume` 从模型工具列表里**直接消失**；`PreToolUse` 钩子退出码 2 会让工具调用被拒并把 stderr 文本回给模型。
   - **MCP 启动竞态**：MCP 服务启动慢时 `-p` 首轮请求里 MCP 工具数会是 0 → 设 `CLAUDE_CODE_MCP_STARTUP_WAIT_MS=15000`（需 v2.1.274+），或用 `--mcp-config <.mcp.json> --strict-mcp-config` 显式指定。
   - **结构化输出**：`claude -p` 用 `--output-schema`；答案不合 schema 时 Claude Code 自己校验并把错误回给模型，连续 5 次失败 → `is_error=true`、`structured_output=null`。
   - **图片**：640 宽真实尺寸帧约 77 KB/张（base64 JSON 约 103 KB/张），一次 12 张能完整到达，不截断。

5. **验证**：把 `agent_gate drive --agent claude` 跑一遍，确认与 mcode 一样停在人工关卡、退出码 10。

---

## 15. 风险登记

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| `config.locked.yaml` 含明文密钥 | 凭据泄露 | 仓库外 + P9-2 `git status` 核验 + B-11 |
| `mcode` 无「关内置工具」硬开关 | 软护栏 | 加锁配置收紧 `permissionMode` + `guard_hook.py`；验收 B-9/B-10 |
| Postgres 路径未支持 | 不能上生产 | 显式拒绝启动；本文只声明 SQLite |
| ffmpeg 不在后台 shell 的 PATH | kit 测试 6 个用例误报失败 | 显式 `$env:PATH` 前置；已写入 §7 阶段前置 |
| **`mcode` 不在 PATH** | ① kit 测试里 10 条 `@needs_mcode` 用例被 **skip**（数字上看不出来）；② `drive` 直接 `WinError 2` → 退出码 4 `stalled` | 显式把 `C:\Users\wayyet\.minimax-code` 加进 PATH，或设 `AGENT_GATE_MCODE_BIN` 指到 `mcode.cmd`；见 附录 B |
| 素材链路缺陷（D5/D6）未修 | 主图跑不出成片 | ✅ **已修**（2026-10-19），见 §10.4 |
| `clip.kind` / prompt 模板问题扩大 | 后续节点拿空数据 | ✅ 同上（clip 契约 + 渲染器两层都修了） |
| **D9：生产端/契约端字段集不一致** | `node_05` 必抛 `ContractInvalid` → `draft_path=None` → 关卡 ① 后必崩 | ✅ **已修**（2026-10-20），见 §10.5 |
| **D10：qa_gate 重试回环后 fan-in 屏障死锁** | 图静默 END，worker 却报 `completed` | ✅ **已修**（2026-10-20），见 §10.5 |
| **D7/D8：`draft_path=None` 无防御** | `Path(None)` → `TypeError` → worker 崩 | ✅ **已修**（2026-10-20），下游统一降级跳过，见 §10.5 |
| MiniMax Token Plan 额度耗尽 | Agent 驱动的实跑跑不了（429） | ✅ 额度已恢复；2026-10-20 的 `v006` 是**模型自动作答**（4 个会话全部 succeeded） |

---

## 附录 A. `agent_gate` 模块职责速查

| 模块 | 职责 |
| --- | --- |
| `__main__.py` | `python -m agent_gate` 入口 |
| `cli.py` | 命令行：`init` / `start` / `status` / `events` / `tasks` / `submit` / `resume` / `drive` / `mcode-lock` |
| `drive.py` | 无人值守编排：拉起 worker、驱动 Agent 会话、遇人工关卡返回退出码 10 |
| `runner.py` | worker 生命周期：spawn / run / resume / continue_after_crash + 生产护栏 |
| `mailbox.py` | 文件信箱：task / result / error / attempts / frames 的原子读写 |
| `gate_client.py` | `GateLLMClient`：把项目里的 `chat()` 调用转成「出题 + 阻塞等答案」 |
| `mcp_server.py` | MCP 服务：暴露 `run_start` / `gate_next_tasks` / `gate_submit_result` / `human_resume` / `run_status` |
| `cli_client.py` | 备选适配器：把 `chat()` 转成 `claude -p` / `mcode exec` 一问一答 |
| `scaffold.py` | 生成操作员目录：`.mcp.json` / `AGENTS.md` / `CLAUDE.md` / `.claude/` + `mcode-lock` |
| `safety.py` | **唯一**的标识符与路径校验入口（F1/F2/F6） |
| `validate.py` | 答案校验：JSON 解析 + Draft-07 schema（F4） |
| `service.py` | 任务服务层：出题、收答案、状态汇总、拒绝事件 |
| `media.py` | 抽帧（ffmpeg） |
| `protocol.py` | 任务/结果数据结构与 `task_id` 计算 |
| `factory.py` | graph factory 解析（`graph:build_graph`） |
| `launcher.py` | `mcode` / `claude` 进程启动 |
| `guard_hook.py` | `PreToolUse` 钩子：拦截直接访问信箱文件与 `resume` |

## 附录 B. 验收命令速查

```powershell
# 环境前置(两样都必须):
#   ffmpeg —— 抽帧要它,否则 7 条用例会误报失败
#   mcode  —— kit 测试里 @needs_mcode 的 10 条要它,否则会被 skip(不是 pass!)
$env:PATH = "C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin;C:\Users\wayyet\.minimax-code;$env:PATH"
$env:PYTHONIOENCODING = "utf-8"
$env:STORYLINE_MODE = "auto"
$env:AGENT_GATE_MEDIA_ROOTS = "$REPO\inputs;$REPO\tests\fixtures"
# drive / resume 还要(Windows:CreateProcess 不做 PATHEXT 补全,裸名 mcode 必 WinError 2)
$env:AGENT_GATE_MCODE_BIN = "C:\Users\wayyet\.minimax-code\mcode.cmd"

# 状态
& $PY -m agent_gate status --thread-id v006 --outputs-root "$REPO\outputs"
& $PY -m agent_gate events --thread-id v006 --outputs-root "$REPO\outputs" --limit 50

# 单独跑 kit 测试(必须带 ffmpeg + mcode PATH)
& $PY -m pytest agent_gate/tests -q -p no:cacheprovider --junit-xml="$env:TEMP\kit_after.xml"

# 项目测试(与基线对比)
& $PY -m pytest tests -q --no-header -p no:cacheprovider --tb=no --junit-xml="$env:TEMP\after.xml"
$LASTEXITCODE
```

> ⚠️ **PATH 漏了 mcode 的后果**:kit 测试里 10 条 `@needs_mcode` 用例会**被 skip 而不是跑**,
> 数字上看不出来(mcode 不在 PATH → `shutil.which` 找不到 → 装饰器判定为「没装」)。
> 本机 `C:\Users\wayyet\.minimax-code` **默认不在 PATH** 里,必须显式加上。

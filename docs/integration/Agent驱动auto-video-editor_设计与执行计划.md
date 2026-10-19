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
| 接入层验证 | ✅ **通过**。真实主图上 8/8 任务由 Agent 作答、人工关卡①放行成功 |
| 端到端出片 | ❌ **未通过**。人工关卡放行后，项目主图在 `node_07_speed_fit` 因 `draft_path=None` 崩溃，最终剪映草稿未生成（详见 [§10 已知缺陷](#10-已知缺陷与后续项)） |

**一句话**：Agent 驱动接入层已经跑通；卡住的是项目自身的素材数据链路（`clip.kind` 为空、prompt 模板未渲染），
按 §1 Non-goals 不在本次修复范围，故整条流水线没有出片。

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
2. **不修**项目既有缺陷（见 [§10](#10-已知缺陷与后续项)），只记录、不混入本次接入。
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

### P8 — Agent 实跑 + 人工关卡 ⚠️ 部分通过

**目的**：让 MiniMax Code 真正接管答题，一路跑到剪映人工关卡，再由真人放行。

**P8-1 无人值守跑到关卡**

```powershell
& $PY -m agent_gate drive --thread-id v001 --agent mcode `
   --project-dir $OPS --mcode-config "$env:USERPROFILE\.minimax\config.locked.yaml" `
   --outputs-root "$REPO\outputs" --graph-factory graph:build_graph --video $VIDEO
$LASTEXITCODE

& $PY -m agent_gate status --thread-id v001 --outputs-root "$REPO\outputs"
& $PY -m agent_gate events --thread-id v001 --outputs-root "$REPO\outputs" --limit 50
```

**期望退出码 `10`** = 停在人工关卡，**不是失败**。

**P8-2 人工关卡放行**（必须真人在**交互终端**、repo 根目录内）

```powershell
& $PY -m agent_gate status --thread-id v001 --outputs-root "$REPO\outputs"

& $PY -m agent_gate resume --thread-id v001 --outputs-root "$REPO\outputs" `
   --graph-factory graph:build_graph --checkpoint "①" --value true
```

- 终端打不出 `①` 时用码点：`--checkpoint ([string][char]0x2460)`
- `resume` 在非交互环境会直接返回 `need_human_terminal` —— **这是设计的安全边界，不要用 `--force-no-tty` 绕过**

**实测结果**：

`events.jsonl` 事件统计（`v001`）：

| 事件 | 次数 | 含义 |
| --- | --- | --- |
| `task_created` | 8 | 出题 8 次 |
| `task_answered` | 8 | **Agent 作答 8 次，全部被收** |
| `agent_session` | 3 | mcode 起了 3 个会话，全部 `status=succeeded` |
| `waiting_human` | 1 | 到达人工关卡 `①` |
| `drive_finished` | 1 | `outcome=ask_human`, `sessions=3` → **drive 退出码 10** ✅ |
| `worker_started` | 2 | 首次启动 + 人工 resume 后重启 |
| `worker_failed` | 1 | ❌ 见下 |

`resume.json` = `{"①": true}` → **人工关卡放行成功** ✅。

**但 worker 在 resume 后崩溃** ❌：

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

**根因分析**（Agent 自己在会话 001 的输出里已经定位到）：

1. **上游素材描述全失败**：`clip_0001/0002/0003` 三个都返回 `Error: Clip kind not supported:`（冒号后为空 → `clip.kind` 取值为空或不在允许列表）。
2. **prompt 模板变量没渲染**：`{{user_request}}`、`{{clip_captions}}`、`{{clip_number}}`、`{{duration_budget}}`、`{{style}}` 全是原始占位符，没一个被替换 → 下游节点拿到的素材信息是空的。
3. Agent 因此产出**退化答案**（`scripts: []`、`themes`/`voiceover_hints` 留空，并如实写明阻塞原因）——这是**正确的行为**，Agent 没有编造。
4. `node_05_generate_draft` 的 canonical 映射因此抛 `ContractInvalid` → 走早退分支 → `draft_path = None`。
5. `node_07_speed_fit` 直接 `Path(state["draft_path"])`，**没有 None 防御** → `TypeError` → worker 失败。

**判定**：

- ✅ **接入层（agent_gate）全绿**：出题 / 收答案 / schema 校验 / 人工关卡 / resume / drive 退出码 10，全部符合设计。
- ❌ **端到端出片未完成**：项目主图在素材数据链路缺陷下拿不到合法 canonical plan，最终剪映草稿**未生成**。
- ⚠️ 这属于**项目侧数据链路缺陷**，不在本次接入范围（§1 Non-goals 2），本次**不修**。

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
| A1-3 | 主图至少一个 LLM 任务由 Agent 作答，不是 Stub | `tasks/*.task.json` 数量 > 0 且与 `*.result.json` 一一对应 | ✅ 8/8 |
| A1-4 | 画面帧真的传给了 Agent | `frames/<task_id>/*.jpg` 非空，任务视图里 `images.shown > 0` | ⚠️ v001 任务 `images.Count=0`，素材链路缺陷导致抽帧无内容 |
| A1-5 | `answered_tasks > 0` 且 `degraded_tasks == 0` | `agent_gate status` | ✅ 8 answered |
| A1-6 | 崩溃恢复不重复提问 | 手动 kill worker → `continue_after_crash` → 已答任务不再出现 | ✅ `worker_started` 出现 2 次，8 个任务未重复 |
| A1-7 | 人工关卡真的暂停 | `worker_state=waiting_human`、`interrupts` 非空；Agent 会话**未**调用 resume | ✅ `interrupts[0].checkpoint = "①"` |
| A1-8 | 最终产物合理 | 剪映草稿可打开；字幕/分镜/转场/BGM 人工过目 | ❌ 未生成草稿（见 P8 根因） |

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
| C-1 | P0 基线与 P7 之后的项目测试**逐项一致**（多 0 少 0） | ✅ **919 / 0 失败 / 0 错误 / 11 skip**，用例总数与基线完全一致，且比基线少 1 个失败（见 [§11](#11-回归对比)） |
| C-2 | `pytest agent_gate/tests` 全绿；claude 相关用例 skip 并如实记为 skip | ✅ 197 收集 / 180 通过 / 17 skip / 0 失败 |
| C-3 | 记录版本 | ✅ 见 [§13](#13-版本与环境) |
| C-4 | 声明「只支持 SQLite 本地开发路径」，不声称 Postgres 已验收 | ✅ |
| C-5 | 声明平台与覆盖范围 | ✅ 见下 |

> **C-5 声明（实测口径）**：
> - **Windows 已实测**（本机 Windows 11 + PowerShell 7）。
> - **Linux 未复测**：交付包在 Linux 沙箱跑过 89 测试（Claude Code 2.1.302 / mcode 0.6.9），该结论**不作为本机验收依据**，只作参考。
> - **接入层端到端跑通**：真实主图上 8/8 任务由 MiniMax Code 作答、人工关卡①放行成功、drive 退出码 10。
> - **完整流水线未跑通**：人工关卡放行后主图因 `draft_path=None` 崩溃，**最终剪映草稿未生成**。
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

### 10.1 项目既有缺陷（本次**不改**，只记录）

| # | 缺陷 | 位置 | 影响 | 本次实测表现 |
| --- | --- | --- | --- | --- |
| D1 | `aes_score` 在成功路径被丢弃 | `storyline_capabilities/understand_clips.py`（`clip_captions` 只含 `caption`/`clip_id`/`source_ref`） | `filter_clips` 按 `aes_score` 排序时全按 0 处理 | 交付包审计 P4 已确认；本项目现有单测不覆盖 |
| D2 | `storyline_local_asr` / `storyline_speech_rough_cut` 路由可能不可达 | `nodes/storyline/` | 部分节点不会被触发 | 本次未触发该路径 |
| D3 | 多个 JSON schema 只有 `properties` 没有 `required` | 项目内多处 | 字段可缺失，Agent 可能漏填 | 8 个任务中 7 个带 schema，其中 1 个（`t_676905527d010e7d`）完全无 schema（F5） |
| D4 | 镜头描述调用没传 schema | `storyline_capabilities/understand_clips.py:143` | 见 §6 F5 | Agent 只能给纯文本答案 |
| D5 | **素材链路缺陷：`clip.kind` 为空** | 上游 `clip` 构造 | `Error: Clip kind not supported:` → 镜头描述全失败 | ⚠️ **本次实跑命中** |
| D6 | **prompt 模板变量未渲染** | 上游 prompt 拼装 | `{{user_request}}` / `{{clip_captions}}` / `{{clip_number}}` / `{{duration_budget}}` / `{{style}}` 原样残留 → 下游拿空数据 | ⚠️ **本次实跑命中** |
| D7 | `node_07_speed_fit` 不防御 `draft_path=None` | `nodes/node_07_speed_fit.py:91`（及 `:163`、`:186`） | `TypeError` 直接崩 worker，无友好降级 | ⚠️ **本次实跑命中**（P8 失败点） |
| D8 | `node_05_generate_draft` 早退时把 `draft_path` 置 `None` 且下游未检查 | `nodes/node_05_generate_draft.py:71-76`、`:112-119` | 早退信息只进 `error_log`，下游看不到 | ⚠️ **本次实跑命中**（D7 的上游） |

> D5–D8 是本次 P8 实跑**新发现**的，之前不在审查版 §4.2 清单里。
> 按 §1 Non-goals 2「不修项目既有缺陷」处理：**只记录、不修**，但 P8 的失败结论必须如实写明。

### 10.2 后续项

| # | 项 | 具体改法 | 需要连带调整的测试 |
| --- | --- | --- | --- |
| F1 | 给镜头描述任务补 `json_schema`（解 F5） | 在 `understand_clips.py:143` 的调用传 `json_schema={"type":"object","properties":{"caption":{"type":"string"},"aes_score":{"type":"number"}},"required":["caption"]}` | ⚠️ 补上后 `answer_format` 从 `text` 变 `json`，**`stub` 后端行为会变**（`StubLLMClient` 改走 `_fake_obj_from_schema`，返回 `{"caption":"","aes_score":0.0}` 而非 `"no caption"`）→ 必须同步改 `tests/` 里依赖 `"no caption"` 的断言；还需新增 `required` 字段校验用例 |
| F2 | 任务哈希纳入推理参数 | `task_id` 计算时把 `temperature` / `top_p` / `max_tokens` 加进 `canonical_json` 的输入 | `agent_gate/tests/test_gate.py::test_task_id_stable_and_sensitive` 的期望值会变，需重算；还要补一条「参数不同 → ID 不同」的用例 |
| F3 | 修 D5：`clip.kind` 取值 | 追 `clip` 字典的构造点，确保 `kind` 落在 `understand_clips` 允许列表内 | 建议加一条单测：构造合法 `kind` 的 clip，断言不出现 `Clip kind not supported` |
| F4 | 修 D6：prompt 模板渲染 | 追 prompt 拼装点，确认 `{{user_request}}` 等占位符在调用前被替换 | 加一条单测断言渲染后 prompt 不含 `{{...}}` 残留 |
| F5 | 修 D7/D8：`draft_path=None` 的防御 | `node_07_speed_fit` 等下游节点改用 `state.get("draft_path")` + 提前 `raise`/降级；或 `node_05` 早退时写显式状态字段（如 `draft_failed_reason`）供下游判断 | 建议加一条单测：`draft_path=None` 时节点抛**可识别**的异常而非 `TypeError` |
| F6 | 补 `required`（D3） | 逐个 schema 补 `required` | 会让原本能通过的答案变成不合格 → 需同步放宽/调整 Agent 侧提示词 |

### 10.3 本次明确不覆盖的验证

- **Claude Code**：本机未安装 → 未验证。只给后续步骤（见 §14）。
- **Linux**：交付包在 Linux 沙箱跑过 89 测试；本机是 Windows，**不复用该结论**。
- **Postgres 生产**：未实现、未验证（worker 显式拒绝启动）。
- **真实模型答案质量**：无法自动化，需人工判断。
- **交付包 `3_脚本/`**：里面脚本指向原 Linux 沙箱路径，**不迁移、不执行**。
- **交付包 `4_验证记录/` 的 89-passed 结论**：只作参考，**不作为本机验收依据**。

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

### 11.2 实测结果

| 测量 | tests | failures | errors | skipped | 失败用例 |
| --- | --- | --- | --- | --- | --- |
| M1 `baseline_true` | 919 | 1 | 0 | 11 | `test_no_auto_import_video::test_button_click_unblocks_graph_and_import_succeeds` |
| M2 `baseline_rebuild` | 919 | 1 | 0 | 35 | 同上（另 +24 个素材相关 skip） |
| M3 `baseline_final` | 919 | 1 | 0 | 11 | `test_phase5_e2e::test_phase5_decoupling_no_runtime_dependencies` |
| **M4 `after_p9`** | **919** | **0** | **0** | **11** | **无** ✅ |

### 11.3 归属判定

**用例总数四次测量完全一致（919）** → P2 的接入补丁与 P3 的安全加固**没有增删、跳过或改变任何用例**。

两个基线失败**都不是 P2/P3 引入的**，而是测量环境的产物：

| 失败用例 | 出现于 | 根因 | 是否 P2/P3 引入 |
| --- | --- | --- | --- |
| `test_no_auto_import_video::test_button_click_unblocks_graph_and_import_succeeds` | M1 / M2 | 依赖主工作区的运行时状态（独立 worktree 里没有 `outputs/`、`checkpoints/` 等既有数据）。在 M3 / M4 的主工作区里**通过** | ❌ 否 |
| `test_phase5_e2e::test_phase5_decoupling_no_runtime_dependencies` | M3 | 原始 import 守卫禁止 `import mcp`，而 `agent_gate/` 正是 MCP 文件信箱实现 → 守卫命中。**P3 对 `scripts/assembly/phase5_e2e.py` 的改动（`agent_gate` 受控豁免）正好修掉这一条** | ❌ 否（反而是本次修掉的） |

### 11.4 结论

- **改动后 919 个用例 0 失败 / 0 错误**，比任一基线都严格更优。
- 计划的 C-1 判据是「与基线逐项一致（多 0 少 0）」→ 实测**多 0、少 1**，满足且优于该判据。
- 唯一的 skip 差异（11 vs 35）是 M2 缺本地素材导致，与代码无关。

---

## 12. 提交前核验结果（P9-2）

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
| 素材链路缺陷（D5/D6）未修 | 主图跑不出成片 | 记入 §10.1 D5–D8 + §10.2 F3/F4/F5 后续项 |
| `clip.kind` / prompt 模板问题扩大 | 后续节点拿空数据 | 同上 |

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
# 状态
& $PY -m agent_gate status --thread-id v001 --outputs-root "$REPO\outputs"
& $PY -m agent_gate events --thread-id v001 --outputs-root "$REPO\outputs" --limit 50

# 单独跑 kit 测试（必须带 ffmpeg PATH）
$env:PATH = "C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin;$env:PATH"
& $PY -m pytest agent_gate/tests -q -p no:cacheprovider --junit-xml="$env:TEMP\kit_after.xml"

# 项目测试（与基线对比）
& $PY -m pytest tests -q --no-header -p no:cacheprovider --tb=no --junit-xml="$env:TEMP\after.xml"
$LASTEXITCODE
```

# agent_gate

让 Agent 程序(MiniMax Code / Claude Code)直接驱动 `auto-video-editor` 的闸门层。

`auto-video-editor` 里 11 处 LLM 调用全部走 `storyline_capabilities.vlm_client` 的
`LLMClient` 协议。`agent_gate` 提供 `GateLLMClient` —— 一个把调用转成**文件信箱任务**的
client:写入 `tasks/*.task.json`,阻塞等 Agent 通过 MCP 提交 `*.result.json`。

默认 backend 仍是项目自带的 `StubLLMClient`,既有行为不变。

---

## 最短用法

```powershell
$REPO = "E:\Documents\kuaishou\auto-video-editor"
$PY   = "$REPO\.venv\Scripts\python.exe"
Set-Location $REPO

# 看可用命令
& $PY -m agent_gate --help

# 拉起一次后台 worker(backend=stub,先证明主图能跑到人工关卡)
& $PY -m agent_gate start --thread-id smoke-stub --outputs-root "$REPO\outputs" `
   --graph-factory graph:build_graph --backend stub --video "$REPO\tests\fixtures\30s.mp4"

& $PY -m agent_gate status --thread-id smoke-stub --outputs-root "$REPO\outputs"
```

完整设计、端到端时序、安全修复规格见 `docs/integration/`。

---

## ⚠️ 测试必须单独跑

**kit 自带测试只能这样跑:**

```powershell
& $PY -m pytest agent_gate/tests -q -p no:cacheprovider
```

**不能**把 `agent_gate/tests` 和仓库 `tests/` 混在一次 pytest 里跑。原因:

`agent_gate/tests/conftest.py` 会把 `agent_gate/fixtures/` 插到 `sys.path` 最前面,而
`agent_gate/fixtures/storyline_capabilities/` 是一个**影子包**(只含 `__init__.py` 与
`vlm_client.py`,用来喂玩具图 `fixtures/toy_graph.py`)。它一旦生效,就会遮蔽仓库根真实的
`storyline_capabilities/`,导致仓库自身的测试导入到残缺模块而集体报错。

仓库 `pyproject.toml` 里 `testpaths = ["tests"]`,所以**裸跑 `pytest` 不会碰到 kit 测试**,
两个测试套件天然隔离:

| 命令 | 跑什么 |
|---|---|
| `pytest` / `pytest tests` | 仓库原有测试 |
| `pytest agent_gate/tests` | kit 的 11 个测试文件(含 4 个安全/生命周期专项) |

环境要求:`ffmpeg` / `ffprobe` 在 `PATH` 里。没装 `claude` 或 `mcode` 的测试会自动 skip。

---

## 目录

| 路径 | 作用 |
|---|---|
| `gate_client.py` | `GateLLMClient` —— 阻塞式文件信箱 client |
| `mailbox.py` | 信箱读写(task / result / error / attempts) |
| `service.py` | MCP 工具背后的业务层 |
| `mcp_server.py` | 暴露给 Agent 的 MCP 服务(`video-agent`) |
| `runner.py` | worker 进程拉起 / 图执行 / 状态文件 |
| `drive.py` | 无人值守循环,停在人工关卡时退出码 **10** |
| `cli.py` / `cli_client.py` | 操作员侧命令行;`cli_client.py` 是 `claude-cli` / `mcode-cli` 一问一答适配器 |
| `scaffold.py` | 生成仓库外的「操作员目录」 |
| `media.py` | ffmpeg 抽帧 |
| `validate.py` / `protocol.py` | 结果校验 / 任务与结果的数据结构 |
| `guard_hook.py` | Agent 工具权限面收敛钩子 |
| `factory.py` / `launcher.py` | graph factory 解析 / 子进程启动 |
| `safety.py` | **唯一**的标识符与路径校验入口(线程/任务白名单、路径归属、素材白名单) |

---

## 安全模型(一句话版)

| 威胁 | 挡在哪 | 返回码 |
|---|---|---|
| Agent 传 `thread_id=../x` 想越权到别的运行 | `safety.safe_thread_id` 白名单 | `bad_thread_id` |
| Agent 传 `task_id=../../x` 想读别人的文件 | `safety.safe_task_id` + `mailbox` 路径方法重复校验 | `bad_task_id` |
| Agent 让服务读任意路径当初始 state | MCP `run_start` **已删除** `initial_state_file` 参数 | 参数不存在 |
| Agent 传任意 `video_path` | `safe_media_path` + `AGENT_GATE_MEDIA_ROOTS` 白名单 | `media_path_not_allowed` |
| 同一个 `thread_id` 被反复喂初始 state | `spawn_worker(mode="start")` 要求 status 和 checkpoint 都不存在 | `already_used` |
| 非 crashed 状态被当成崩溃恢复 | `spawn_worker(mode="continue_after_crash")` 要求状态 ∈ {crashed, failed} 且有检查点 | `not_restartable` / `no_checkpoint` |
| 不在挂起列表里的 checkpoint 被放行 | `spawn_worker(mode="resume")` 校验 checkpoint 匹配 | `bad_checkpoint` |
| 非法 `json_schema` 静默当成校验通过 | `validate.check_schema` 抛 `InvalidSchema` → 直接关任务 | `schema_invalid` |
| 生产 Postgres 环境被静默降级成 SQLite | `run_worker` 护栏 + 退出码 3 | `production_unsupported` |

**拒绝时返回给 Agent 的文案一律不含本机绝对路径**,只给一个稳定错误码;
绝对路径只写进本机 `<gate_root>/events.jsonl`。

---

## 后续项(本次刻意没做)

1. **`understand_clips.py:143` 的镜头描述任务没有 `json_schema`** —— 该调用当前不传 schema,
   所以它在 gate 里是**文本任务**,只校验非空,靠 `parse_json_loose` 兜底解析。
   补 schema 的具体改法:给该次 `chat()` 传 `json_schema={"type":"object","properties":{
   "caption":{"type":"string"},"aes_score":{"type":"number"}},"required":["caption","aes_score"]}`。
   **代价**:默认 `stub` 后端下 `StubLLMClient` 会改走 `_fake_obj_from_schema` 分支,
   返回 `{"caption": "", "aes_score": 0.0}` 而不是现在的 `"no caption"`,
   会改变项目既有行为 —— 需要连带调整 `tests/` 里依赖该返回值的用例。
   本次不做,是为了不把「接入故障」和「项目既有缺陷」混在一起。

2. **项目既有缺陷(审查版 §4.2),本次只记录不修**:
   - `understand_clips()` 成功路径丢弃 `aes_score`(`understand_clips.py:174-176`)
   - `storyline_local_asr` / `storyline_speech_rough_cut` 路由可能不可达
   - 多个 JSON schema 只有 `properties` 没有 `required`

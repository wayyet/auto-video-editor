# auto-video-editor 工作流创建台（/console）设计与分阶段执行计划

> **项目**：`wayyet/auto-video-editor`  
> **分支**：`main`  
> **设计基线**：2026-11-05  
> **文档状态**：待实施设计方案  
> **目标**：新增 `/console`，实现提示词输入、视频导入、工作流创建、启动和运行监控。

---

## 1. 项目目标与总体设计

### 1.1 建设目标

新增独立的工作流创建台 `/console`，实现：

1. 输入自然语言剪辑提示词。
2. 导入一个或多个视频文件。
3. 设置目标时长、画面比例和输出语言。
4. 创建唯一的 `thread_id`。
5. 自动生成 LangGraph 初始 State。
6. 启动现有 LangGraph 工作流。
7. 实时显示执行状态和节点事件。
8. 跳转现有监控页面查看完整执行轨迹。

### 1.2 页面职责划分

**不能直接把现有监控页面改造成工作流创建页面。**

当前主分支已经删除旧聊天 Web 运行时。因此建议新增独立页面和 API。

| 页面 | 职责 | 是否启动工作流 |
|---|---|---|
| `/console` | 工作流创建、提示词输入、视频上传 | 是 |
| `/` | 运行状态和实时节点监控 | 否 |
| `/node-map` | LangGraph 节点拓扑 | 否 |
| `/api/workflow/*` | 只读监控 API | 否 |
| `/api/console/*` | 上传、创建、启动工作流 | 是 |

### 1.3 当前项目架构核对结果

当前 `main` 分支的重要情况：

- 2026-11-04 已删除旧聊天运行时。
- 当前采用 `auto-only` 单流水线架构。
- 保留人工关卡 ①、②、③。
- `/` 是只读监控页面。
- `/node-map` 是节点拓扑页面。
- 当前生产 Checkpoint 使用 PostgreSQL，不再使用 SQLite 持久化后端。
- `scripts/run_workflow.py` 是现有正式执行入口。
- `monitoring.workflow_runner.run_with_events()` 是统一事件执行器。
- 默认 LLM 客户端仍然是 `StubLLMClient`。

因此，新功能应复用当前的 LangGraph、PostgreSQL 和监控体系，不应该重新实现工作流引擎。

---

## 2. 现有代码架构分析

### 2.1 现有系统结构

```text
浏览器
  |
  ├── /console              新增工作流创建台
  |
  ├── /                    现有只读监控
  |
  └── /node-map             现有节点拓扑
          |
          v
    monitor_fastapi.py
          |
          ├── GET /console
          |
          ├── /api/console/*       新增写入 API
          |
          └── /api/workflow/*      现有只读 API
                    |
                    v
             工作流独立子进程
             scripts/run_workflow.py
                    |
                    v
               graph.py
                    |
                    v
                LangGraph
                    |
                    v
             PostgreSQL Checkpoint
                    |
                    v
          workflow_run_events
                    |
                    v
             SSE 实时事件
                    |
                    v
                /console
```

### 2.2 主要代码文件

| 文件 | 当前职责 | 改造要求 |
|---|---|---|
| `monitor_fastapi.py` | FastAPI 主服务 | 新增 `/console` 路由 |
| `web/api_workflow.py` | 只读监控 API | 保持只读 |
| `web/monitor.html` | 监控界面 | 增加 Console 入口和 thread 自动订阅 |
| `web/static/workflow_monitor.mjs` | 监控事件处理 | 复用现有事件协议 |
| `graph.py` | LangGraph 主图 | 不重新构建一套图 |
| `state.py` | 工作流 State | 复用已有字段 |
| `nodes/storyline/_common.py` | 输入视频和提示词解析 | 复用现有辅助函数 |
| `nodes/storyline/node_generate_script.py` | 文案生成 | 接收用户提示词 |
| `scripts/run_workflow.py` | 正式执行入口 | 增加真实 LLM 后端注入 |
| `monitoring/workflow_runner.py` | 工作流事件执行 | 保持统一执行路径 |
| `monitoring/workflow_event_store.py` | PostgreSQL 事件持久化 | 继续作为事件唯一数据来源 |
| `config.py` | 项目配置 | 集中管理 Console 参数 |

### 2.3 视频输入字段分析

当前 `state.py` 已经具有：

```python
video_input_path: str
video_input_paths: NotRequired[list[str]]
```

`nodes/storyline/_common.py` 提供：

```python
resolve_video_input_paths(state)
```

其规则是：

1. 优先读取 `video_input_paths`。
2. 没有多视频列表时，读取 `video_input_path`。
3. 过滤空路径。
4. 对重复路径去重，同时保持首次出现的顺序。

因此，Console 不需要重新设计视频输入 State。

**单视频：**

```json
{
  "video_input_path": "/path/video.mp4"
}
```

**多视频：**

```json
{
  "video_input_path": "/path/video1.mp4",
  "video_input_paths": [
    "/path/video1.mp4",
    "/path/video2.mp4"
  ]
}
```

### 2.4 提示词输入字段分析

当前 `nodes/storyline/_common.py` 有：

```python
extract_user_request(state, *keys)
```

现有文案节点读取：

```python
extract_user_request(
    state,
    "script_request",
    "style_request",
    "user_request",
)
```

因此，Console 应该把提示词映射到：

```json
{
  "storyline_targets": {
    "script_request": "用户输入的提示词",
    "user_request": "用户输入的提示词"
  }
}
```

注意：现有代码只能确认文案生成节点读取这些字段。不能默认所有工作流节点都会读取用户提示词。后续应逐个审查镜头理解、配音、选曲、字幕和时间线节点。

### 2.5 LLM 后端问题

当前：

```text
storyline_capabilities/vlm_client.py
```

默认实现是：

```python
StubLLMClient()
```

`agent_gate/factory.py` 已有：

```text
stub
gate
claude-cli
mcode-cli
```

建议 Console 生产环境使用：

- `claude-cli`
- `mcode-cli`

开发和测试环境可以使用 `stub`。

不能让生产 Console 在没有配置真实模型时静默使用 Stub。

---

## 3. 工作流创建台 UI 设计

### 3.1 页面布局

建议使用双栏布局。

```text
┌─────────────────────────────────────────────────────────────┐
│ AI 视频剪辑工作流创建台                监控首页  节点拓扑    │
├──────────────────────────────┬──────────────────────────────┤
│                              │                              │
│  01. 输入剪辑要求             │  本次工作流                  │
│                              │                              │
│  ┌────────────────────────┐  │  运行状态：等待创建           │
│  │                        │  │                              │
│  │ 请输入剪辑提示词       │  │  Thread ID：                │
│  │                        │  │                              │
│  │                        │  │  执行状态：                  │
│  └────────────────────────┘  │  尚未启动                    │
│                              │                              │
│  02. 导入视频                │  已导入视频                  │
│                              │                              │
│  ┌────────────────────────┐  │  ┌────────────────────────┐  │
│  │ 拖入视频或选择文件     │  │  │ video1.mp4             │  │
│  │ 支持多视频             │  │  │ video2.mp4             │  │
│  └────────────────────────┘  │  └────────────────────────┘  │
│                              │                              │
│  03. 剪辑参数                │  执行事件                    │
│                              │                              │
│  目标时长：[35秒]            │  等待工作流启动              │
│  画面比例：[9:16]            │                              │
│  输出语言：[中文]            │                              │
│                              │                              │
│  [ 创建并运行工作流 ]        │  [ 查看完整监控 ]            │
│                              │                              │
└──────────────────────────────┴──────────────────────────────┘
```

### 3.2 页面功能

| 功能 | 说明 |
|---|---|
| 提示词输入 | 多行文本输入框 |
| 视频导入 | 支持拖放和文件选择 |
| 多视频管理 | 显示已导入视频 |
| 视频预览信息 | 名称、大小、格式、时长 |
| 删除视频 | 删除当前待提交列表中的文件 |
| 目标时长 | 默认 35 秒 |
| 画面比例 | 9:16、16:9、4:3 |
| 输出语言 | 中文、英文 |
| 创建工作流 | 上传视频并启动 |
| 运行监控 | 通过现有 SSE 显示事件 |
| 查看完整监控 | 打开 `/` |

### 3.3 前端状态

| 状态 | 含义 |
|---|---|
| `idle` | 等待输入 |
| `uploading` | 正在上传 |
| `starting` | 正在启动 |
| `running` | 正在执行 |
| `waiting_human` | 等待人工关卡 |
| `completed` | 执行完成 |
| `failed` | 执行失败 |
| `event_store_unavailable` | 监控事件存储不可用 |

特别注意：`waiting_human` 是正常工作流状态，不能显示成失败或完成。

---

## 4. 后端 API 设计

### 4.1 新增页面路由

```text
GET /console
```

由现有 `monitor_fastapi.py` 托管。

### 4.2 新增 API 路由

建议新增：

```text
web/api_console.py
```

路由：

```text
POST /api/console/runs
```

该 API 负责：

1. 接收提示词。
2. 接收视频文件。
3. 验证请求参数。
4. 保存上传文件。
5. 执行 `ffprobe` 视频校验。
6. 生成初始 State。
7. 启动工作流子进程。
8. 返回 `thread_id` 和 SSE 地址。

现有：

```text
/api/workflow/*
```

仍然只读，不应在里面加入启动工作流的 POST 接口。

### 4.3 API 请求格式

使用：

```text
multipart/form-data
```

字段：

| 字段 | 类型 | 必填 | 默认值 |
|---|---|---|---|
| `prompt` | string | 是 | 无 |
| `videos` | file[] | 是 | 无 |
| `target_duration_ms` | integer | 否 | 35000 |
| `ratio` | string | 否 | 9:16 |
| `lang` | string | 否 | zh |

LLM Backend 由服务器环境配置决定，不接受浏览器任意指定。

### 4.4 API 响应

成功返回 HTTP `202`：

```json
{
  "ok": true,
  "thread_id": "console-7f1a2b3c4d5e",
  "state": "starting",
  "monitor_url": "/?thread_id=console-7f1a2b3c4d5e",
  "snapshot_url": "/api/workflow/runs/console-7f1a2b3c4d5e",
  "events_url": "/api/workflow/runs/console-7f1a2b3c4d5e/events"
}
```

错误码：

| HTTP | 说明 |
|---|---|
| 400 | 参数错误 |
| 403 | Origin / CSRF 校验失败 |
| 413 | 文件过大 |
| 415 | 视频格式或内容不支持 |
| 409 | 重复请求或状态冲突 |
| 503 | PostgreSQL 或真实模型未就绪 |
| 500 | 服务端未预期错误 |

---

## 5. 工作流初始 State 设计

### 5.1 State 结构

建议创建：

```json
{
  "session_id": "console-7f1a2b3c4d5e",
  "video_input_path": "<outputs>/console-7f1a2b3c4d5e/inputs/0001.mp4",
  "video_input_paths": [
    "<outputs>/console-7f1a2b3c4d5e/inputs/0001.mp4",
    "<outputs>/console-7f1a2b3c4d5e/inputs/0002.mov"
  ],
  "storyline_outputs_root": "<outputs>/console-7f1a2b3c4d5e",
  "storyline_targets": {
    "user_request": "用户输入的剪辑要求",
    "script_request": "用户输入的剪辑要求",
    "target_duration_ms": 35000,
    "ratio": "9:16",
    "lang": "zh"
  },
  "error_log": []
}
```

### 5.2 字段说明

| 字段 | 说明 |
|---|---|
| `session_id` | 本次工作流标识 |
| `video_input_path` | 第一个视频路径 |
| `video_input_paths` | 所有视频路径 |
| `storyline_outputs_root` | 本次工作流的产物根目录 |
| `storyline_targets.user_request` | 用户剪辑要求 |
| `storyline_targets.script_request` | 文案生成要求 |
| `storyline_targets.target_duration_ms` | 目标时长 |
| `storyline_targets.ratio` | 画面比例 |
| `storyline_targets.lang` | 输出语言 |
| `error_log` | 工作流错误日志 |

注意：

- `<outputs>` 是项目配置的真实绝对路径。
- `thread_id` 由服务端生成。
- 不要把普通剪辑提示词默认写入 `storyline_targets.query`，避免意外触发网页话题搜索分支。
- 提示词不应写入监控事件 payload。

---

## 6. 文件存储设计

### 6.1 目录结构

```text
outputs/
└── console-7f1a2b3c4d5e/
    ├── inputs/
    │   ├── 0001.mp4
    │   └── 0002.mov
    ├── initial_state.json
    ├── console_run.json
    ├── console_run.log
    ├── drafts/
    ├── storyline/
    └── 其他工作流产物
```

### 6.2 文件处理规则

1. 每个任务单独建立目录。
2. 使用服务端生成的文件名，不直接使用用户上传文件名作为磁盘路径。
3. 使用分块方式保存大文件。
4. 保存后使用 `resolve()` 检查目录归属。
5. 校验文件扩展名和真实视频内容。
6. 使用 `ffprobe` 检查是否有 video stream。
7. 上传失败时只清理本次任务的临时文件。
8. 不允许文件路径逃逸到任务目录之外。
9. `outputs/` 中的内容不得提交到 Git。

### 6.3 视频限制

建议首期限制：

| 项目 | 建议 |
|---|---|
| 视频格式 | 首期验证 MP4、MOV |
| 单文件大小 | 默认不超过 2 GiB，可配置 |
| 视频数量 | 默认不超过 10 个，可配置 |
| 总上传大小 | 由服务端配置 |
| 视频有效性 | 必须通过 ffprobe |
| 上传方式 | 分块写入 |

这些数值属于初始建议，应结合机器磁盘和内存配置调整。

---

## 7. 工作流执行架构

### 7.1 执行入口

建议复用：

```text
scripts/run_workflow.py
```

通过独立子进程启动，不在 FastAPI 请求线程中直接运行 LangGraph。

建议执行参数：

```powershell
.\.venv\Scripts\python.exe scripts\run_workflow.py `
  --production `
  --skip-preflight `
  --thread-id console-7f1a2b3c4d5e `
  --initial-state-json outputs\console-7f1a2b3c4d5e\initial_state.json `
  --llm-backend mcode-cli
```

`--skip-preflight` 仅适用于已经经过正式启动流程完成数据库和环境预检的情况。独立启动监控服务时不能盲目跳过预检。

### 7.2 必须使用统一 Runner

执行必须沿用：

```python
from monitoring.workflow_runner import run_with_events
```

不要直接使用另一套执行循环，也不要绕开现有 PostgreSQL 事件写入。

### 7.3 独立进程管理

使用：

```python
subprocess.Popen()
```

要求：

- 请求返回时不等待整条工作流结束。
- `shell=False`。
- 命令参数使用数组。
- 提示词只写入 JSON State，不拼接到命令字符串。
- stdout 和 stderr 分别记录到任务日志。
- 保存 PID 和启动信息。
- 任务使用独立进程组。
- 不能因浏览器断开连接就终止工作流。

### 7.4 草稿目录隔离

这是必须实现的保护。

当前 `agent_gate/runner.py` 已经提供按 `thread_id` 隔离剪映草稿目录的逻辑。

Console 如果直接启动 `scripts/run_workflow.py`，也要为子进程设置：

```text
AUTO_VIDEO_EDITOR_DRAFT_DIR=<outputs>/<thread_id>/drafts
```

否则不同工作流可能回落到共享的 `drafts/default`，产生相互覆盖。

---

## 8. 真实 LLM 后端接入

### 8.1 当前问题

当前默认客户端：

```python
StubLLMClient()
```

因此，仅完成页面和工作流启动还不够，必须接通真实模型调用。

### 8.2 改造要求

为 `scripts/run_workflow.py` 增加：

```text
--llm-backend
```

支持：

```text
stub
claude-cli
mcode-cli
```

执行时：

```python
from agent_gate.factory import build_client
from storyline_capabilities.vlm_client import set_default_client

client = build_client(backend)
set_default_client(client)
```

在执行 LangGraph 前完成注入。

### 8.3 配置建议

```text
CONSOLE_LLM_BACKEND=mcode-cli
CONSOLE_ALLOW_STUB=false
```

生产环境必须使用真实模型。

开发和测试环境允许使用 Stub。

保持现有 CLI 调用兼容，不要未经验证就改变旧入口的默认行为。

---

## 9. SSE 实时监控

### 9.1 现有接口

复用：

```text
GET /api/workflow/runs/{thread_id}/events
```

### 9.2 关注的事件

```text
run.started
run.resumed
run.interrupted
run.completed
run.failed

node.started
node.resumed
node.retrying
node.waiting_input
node.succeeded
node.failed

branch.selected
```

### 9.3 监控页面联动

建议增强现有 `web/monitor.html`：

支持：

```text
/?thread_id=console-7f1a2b3c4d5e
```

打开后自动读取 thread_id 并订阅 SSE。

保留原有手工输入 thread_id 的操作方式。

### 9.4 状态语义

| 状态 | 含义 |
|---|---|
| running | 正在执行 |
| waiting_human | 人工关卡等待 |
| completed | 工作流完成 |
| failed | 工作流失败 |

SSE 事件存储不可用时必须显示错误，不能把没有事件解释为执行成功。

---

## 10. 安全与稳定性

### 10.1 API 安全

- POST 接口校验 Origin 和当前服务来源。
- 增加 CSRF Token。
- 不允许浏览器提交任意本机文件路径。
- 不允许浏览器指定数据库连接串。
- 不允许浏览器指定 Python 执行命令。
- 不允许浏览器覆盖服务端 LLM Backend。

### 10.2 文件安全

- 扩展名白名单。
- 文件大小限制。
- 文件数量限制。
- 文件内容校验。
- ffprobe 校验。
- 分块写入。
- 目录归属校验。
- 路径穿越防护。
- 上传失败清理。

### 10.3 提示词安全

- 提示词作为普通文本传递。
- 不拼接到 shell 命令。
- 视频中的文字和指令视为不可信媒体内容。
- 不允许媒体内容覆盖系统规则。
- 不向前端暴露模型密钥、完整内部 State 和敏感路径。

### 10.4 并发安全

每个工作流独占：

```text
outputs/<thread_id>/
inputs/
drafts/
console_run.log
initial_state.json
```

还需要：

- 最大并发任务数。
- 最大运行时间。
- 上传超时。
- ffprobe 超时。
- 子进程 PID 管理。
- Web 服务重启后的运行状态恢复。

---

## 11. 分阶段执行开发任务

### 阶段 0：架构基线与接口冻结

**目标：** 明确改造边界，不修改现有生产行为。

任务：

- [ ] 检查 main 分支最新代码和测试基线。
- [ ] 确认 `/`、`/node-map` 和监控 API。
- [ ] 确认 PostgreSQL 是生产 Checkpoint 后端。
- [ ] 冻结 `POST /api/console/runs` 契约。
- [ ] 冻结初始 State 结构。
- [ ] 冻结视频目录结构。
- [ ] 确认真实 LLM 后端。
- [ ] 确定视频格式、大小和并发限制。

交付：

- API 接口说明。
- 初始 State 示例。
- 上传安全规则。
- 执行架构设计。

验收：

- Console 与只读监控职责分离。
- 不修改主图拓扑。
- 不重新引入 SQLite。

### 阶段 1：上传 API 和 State 构建

**目标：** 完成视频上传和工作流初始数据生成。

新增文件：

```text
web/api_console.py
web/console_service.py
web/console_state.py
web/console_uploads.py
```

任务：

- [ ] 生成唯一 thread_id。
- [ ] 支持多视频上传。
- [ ] 分块保存视频。
- [ ] 校验扩展名。
- [ ] 校验文件大小。
- [ ] 校验视频数量。
- [ ] 执行 ffprobe。
- [ ] 构建 video_input_path。
- [ ] 构建 video_input_paths。
- [ ] 写入 storyline_targets。
- [ ] 写入 session_id。
- [ ] 写入 storyline_outputs_root。
- [ ] 进行路径归属校验。
- [ ] 添加单元测试和 API 测试。

验收：

- 单视频和多视频均能上传。
- State 字段正确。
- 非法文件被拒绝。
- 不启动工作流，不创建 Checkpoint。

### 阶段 2：执行入口和真实 LLM 集成

**目标：** 实现真实工作流启动。

修改：

```text
scripts/run_workflow.py
web/console_service.py
```

复用：

```text
agent_gate/factory.py
storyline_capabilities/vlm_client.py
monitoring/workflow_runner.py
```

任务：

- [ ] 增加 `--llm-backend`。
- [ ] 接入 `build_client()`。
- [ ] 注入 `set_default_client()`。
- [ ] 保留 CLI 旧接口兼容。
- [ ] 配置生产真实 LLM Backend。
- [ ] 加入 PostgreSQL 启动检查。
- [ ] 使用 Popen 启动工作流。
- [ ] 设置独立草稿目录。
- [ ] 继承受控数据库环境变量。
- [ ] 保存 PID。
- [ ] 保存运行日志。
- [ ] 处理进程启动失败。
- [ ] 保证执行使用统一 Runner。

验收：

- 工作流能够成功启动。
- 节点事件进入 PostgreSQL。
- 生产环境不会静默使用 Stub。
- 并发工作流不会覆盖草稿。
- API 不等待工作流执行完成。

### 阶段 3：Console 前端开发

**目标：** 实现可使用的 `/console` 界面。

新增：

```text
web/console.html
web/static/workflow_console.mjs
```

任务：

- [ ] 增加 GET `/console`。
- [ ] 实现提示词输入。
- [ ] 实现视频拖放和选择。
- [ ] 实现多视频管理。
- [ ] 显示视频基本信息。
- [ ] 支持移除视频。
- [ ] 实现目标时长选择。
- [ ] 实现比例选择。
- [ ] 实现语言选择。
- [ ] 显示上传进度。
- [ ] 防止重复提交。
- [ ] 调用创建 API。
- [ ] 显示 thread_id。
- [ ] 订阅现有 SSE。
- [ ] 显示节点事件和执行状态。
- [ ] 处理人工关卡状态。
- [ ] 增加监控页面链接。
- [ ] 增强 monitor.html 的 thread_id 自动订阅。

验收：

- 用户不需要手动编辑 JSON。
- 用户不需要手动执行 CLI。
- 能直接输入提示词和导入视频。
- 能创建并执行工作流。
- 能看到实时状态。
- 原监控页面功能不回归。

### 阶段 4：集成、安全和稳定性测试

**目标：** 验证完整工作流。

任务：

- [ ] 测试 Console 页面加载。
- [ ] 测试单视频上传。
- [ ] 测试多视频上传。
- [ ] 测试非法文件。
- [ ] 测试提示词 State 映射。
- [ ] 测试 Stub 模式。
- [ ] 测试真实模型模式。
- [ ] 测试 PostgreSQL 事件写入。
- [ ] 测试 SSE 断线补发。
- [ ] 测试人工关卡。
- [ ] 测试模型后端缺失。
- [ ] 测试子进程启动失败。
- [ ] 测试文件路径穿越。
- [ ] 测试重复提交。
- [ ] 测试大文件。
- [ ] 测试多个任务并发。
- [ ] 测试服务重启后的状态恢复。
- [ ] 更新 README。

验收：

- 关键测试全部通过。
- 至少完成一次真实模型端到端运行。
- 不发生跨任务文件覆盖。
- 不发生重复执行。
- 不发生敏感信息泄漏。

### 阶段 5：后续增强

**目标：** 扩展非首期功能。

任务：

- [ ] 工作流历史列表。
- [ ] 任务搜索。
- [ ] 视频分片上传。
- [ ] 断点续传。
- [ ] 人工关卡恢复界面。
- [ ] 任务取消。
- [ ] 运行超时管理。
- [ ] 输出产物浏览。
- [ ] 剪映草稿定位。
- [ ] 用户权限和审计。

这些能力不应阻塞首期上线。

---

## 12. 测试设计

### 12.1 单元测试

| 模块 | 测试内容 |
|---|---|
| thread_id | 唯一性和合法字符 |
| prompt | 空内容、超长内容 |
| 文件上传 | 文件格式和大小 |
| 路径安全 | 路径穿越和符号链接 |
| ffprobe | 无视频流和超时 |
| State | 单视频、多视频、提示词、参数 |
| 草稿目录 | 多任务互相隔离 |
| LLM | 后端创建和配置检查 |
| 幂等 | 重复请求 |

### 12.2 集成测试

至少验证：

1. 视频上传成功。
2. 初始 State 生成成功。
3. 提示词正确传到文案节点。
4. LangGraph 成功启动。
5. PostgreSQL Checkpoint 正常。
6. 工作流事件成功写入 PostgreSQL。
7. SSE 能收到节点事件。
8. 人工关卡状态正确。
9. 多工作流并发隔离。
10. 数据库断开时明确报错。

### 12.3 Windows PowerShell 测试命令

在项目根目录执行：

```powershell
# 单元测试
.\.venv\Scripts\python.exe -m pytest tests\unit -v

# 集成测试
.\.venv\Scripts\python.exe -m pytest tests\integration -v

# agent_gate 测试，必须单独执行
.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q -p no:cacheprovider
```

注意：

`agent_gate/tests` 有独立的 `conftest.py` 和影子测试包。不能与根目录 `tests/` 混合在一次 pytest 命令中执行。

测试结果必须分别记录：

- Passed
- Failed
- Skipped

不能把全部测试跳过的情况认定为完整通过。

---

## 13. 最终交付验收

### 13.1 页面

- [ ] `/console` 可以访问。
- [ ] 支持输入剪辑提示词。
- [ ] 支持导入一个视频。
- [ ] 支持导入多个视频。
- [ ] 能显示视频基本信息。
- [ ] 能设置目标时长。
- [ ] 能设置画面比例。
- [ ] 能设置输出语言。
- [ ] 可以创建并启动工作流。
- [ ] 可以显示运行状态。

### 13.2 执行

- [ ] 正确生成唯一 thread_id。
- [ ] 正确生成初始 State。
- [ ] 正确保存视频文件。
- [ ] 正确传递用户提示词。
- [ ] 正确使用真实 LLM 后端。
- [ ] 使用 PostgreSQL Checkpointer。
- [ ] 使用统一 Runner。
- [ ] 每个工作流拥有独立草稿目录。

### 13.3 监控

- [ ] Console 能看到节点事件。
- [ ] 能跳转现有监控首页。
- [ ] SSE 断线能够补发。
- [ ] 人工关卡显示为等待状态。
- [ ] 原有 `/` 和 `/node-map` 保持正常。
- [ ] 没有恢复旧聊天 Web 运行时。

### 13.4 输出产物边界

当前 `final_video_path` 仍然具有占位性质，因此首期不能直接承诺最终 MP4 导出功能已经可用。

首期主要验收：

- 工作流正确启动。
- 工作流节点正常执行。
- 正确等待人工关卡。
- 能够明确报告失败。
- 能定位工作流产物目录。

最终 MP4 导出应单独审查和验收。

---

## 14. 风险与技术决策

| 风险 | 解决方案 |
|---|---|
| 重新引入旧聊天运行时 | Console 使用独立表单，不恢复旧聊天功能 |
| 监控 API 被改为可写 | 新增 `/api/console/*` |
| SQLite 与当前架构不符 | 继续使用 PostgreSQL |
| Stub 被误当成真实 AI | 生产强制真实 LLM Backend |
| 多视频字段缺失 | 复用 `video_input_paths` |
| 提示词无法进入节点 | 映射 `script_request` 和 `user_request` |
| 普通提示词误触发搜索分支 | 不默认写入 `query` |
| 草稿覆盖 | 设置独立 `AUTO_VIDEO_EDITOR_DRAFT_DIR` |
| 大文件导致内存压力 | 分块上传和文件大小限制 |
| 路径穿越 | 固定目录与 resolve 校验 |
| 页面重复提交 | 唯一 thread_id 和幂等键 |
| 人工关卡被错误处理 | 保留 `waiting_human` 语义 |
| SSE 断线 | 使用 Last-Event-ID |
| 最终视频导出未完成 | 单独作为后续验收项 |

### 14.1 最终技术决策

1. 新增 `/console`。
2. 保留 `/` 只读监控。
3. 保留 `/node-map` 节点拓扑。
4. 扩展现有 `monitor_fastapi.py`。
5. 新增 `web/api_console.py`。
6. 复用已有 WorkflowState。
7. 复用 PostgreSQL Checkpointer。
8. 复用 `monitoring.workflow_runner.run_with_events()`。
9. 增加明确的 LLM Backend 注入。
10. 上传视频按照 thread_id 独立存储。
11. 每个工作流设置独立草稿目录。
12. 不恢复旧聊天运行时。
13. 首期不实现自动放行人工关卡。

---

## 15. 当前 main 分支参考源码

- [README.md](https://github.com/wayyet/auto-video-editor/blob/main/README.md)
- [monitor_fastapi.py](https://github.com/wayyet/auto-video-editor/blob/main/monitor_fastapi.py)
- [web/api_workflow.py](https://github.com/wayyet/auto-video-editor/blob/main/web/api_workflow.py)
- [graph.py](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)
- [state.py](https://github.com/wayyet/auto-video-editor/blob/main/state.py)
- [nodes/storyline/_common.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/_common.py)
- [nodes/storyline/node_generate_script.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_generate_script.py)
- [scripts/run_workflow.py](https://github.com/wayyet/auto-video-editor/blob/main/scripts/run_workflow.py)
- [monitoring/workflow_runner.py](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_runner.py)
- [monitoring/workflow_event_store.py](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_event_store.py)
- [config.py](https://github.com/wayyet/auto-video-editor/blob/main/config.py)
- [agent_gate/factory.py](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/factory.py)
- [agent_gate/runner.py](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/runner.py)
- [agent_gate/safety.py](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/safety.py)
- [web/monitor.html](https://github.com/wayyet/auto-video-editor/blob/main/web/monitor.html)

---

## 16. 历史对话参考

参考历史对话：

`生成设计执行计划`

本方案延续其中增加 `/console` 工作流创建入口的目标。

当前源码检查确认：

- `monitor_fastapi.py` 没有 `/console`。
- README 未将 `/console` 列为现有页面。
- 现有页面是 `/` 只读监控和 `/node-map` 节点拓扑。

因此，本方案将 `/console` 作为一个尚未实现的新功能。

历史对话中的完整附件和原始设计文件没有在本次请求中提供，因此不将未读取的材料视为已核实依据。

---

## 17. 首期开发顺序

```text
阶段 0：架构与接口契约冻结
       |
       v
阶段 1：上传 API + State 构建
       |
       v
阶段 2：子进程启动 + 真实 LLM 注入
       |
       v
阶段 3：/console 前端 + SSE 状态显示
       |
       v
阶段 4：端到端与安全测试
       |
       v
阶段 5：历史、断点续传、恢复界面等增强
```

**首期最小可交付目标：**

用户打开 `/console`，输入剪辑提示词，导入一个或多个视频，点击创建并运行。服务端自动生成任务目录和初始 State，使用真实 LLM 后端启动现有 LangGraph 主图，页面通过已有 SSE 机制显示执行状态，并可以跳转到原监控页面查看完整节点轨迹。

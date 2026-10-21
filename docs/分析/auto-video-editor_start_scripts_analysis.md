# auto-video-editor 启动脚本分析

> ⚠️ **历史分析底稿(2026-10-21 之前的状态描述)**
>
> 本文记录的是**启动脚本统一与修复之前**的现状,`scripts/start_local.ps1`
> 已于 2026-10-21 **删除**,启动入口统一为根目录的 `start_production.ps1`。
> 下文所有关于 `start_local.ps1` 的描述仅作历史参考,**不是现行操作指引**。
> 现行用法见 [`README.md`](../../README.md) 的「启动入口」章节。
>
> 分析基准：`wayyet/auto-video-editor` 仓库 `main` 分支。  
> 主要文件：[`scripts/start_local.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/start_local.ps1)(已删除)、[`start_production.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/start_production.ps1)。  
> 配套实现：[`scripts/preflight.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/preflight.ps1)、[`scripts/start_postgres.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/start_postgres.ps1)、[`scripts/run_workflow.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/run_workflow.py)、[`runtime/preflight.py`](https://github.com/wayyet/auto-video-editor/blob/main/runtime/preflight.py)、[`nodes/node_02_launch_openstoryline.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_02_launch_openstoryline.py)、[`graph.py`](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)。

## 1. 结论摘要

- `scripts/start_local.ps1` 是**本地开发启动入口**。默认要求项目内已有 `inputs\30s.mp4`，不要求管理员权限，默认 `STORYLINE_MODE=auto`，启动前会创建初始状态 JSON，并负责启动 Postgres。
- 根目录的 `start_production.ps1` 是**生产全栈启动入口**。默认从另一个目录复制视频，检查管理员权限和 Docker，检查 OpenStoryline 资源/模型，启动或复用 OpenStoryline Web，并在工作流运行后检查 checkpoint、心跳和输出目录。
- 两个脚本都会调用 `scripts\run_workflow.py --production`。这个参数会令 Python 入口使用生产模式的 Postgres checkpointer；但**它不会自动跳过 Python 入口内部的 preflight**。
- 当前实现存在几项值得修正的问题：preflight/Postgres 启动重复执行；生产脚本未使用 `-PostgresUri` 参数；生产验收查询缺少 `THREAD_ID` 环境变量；生产脚本启动的 OpenStoryline Web 可能被 LangGraph 的 `node_02` 再次启动。

## 2. 文件位置与启动方式

仓库中的实际路径不同：

```text
auto-video-editor/
├── scripts/
│   ├── start_local.ps1
│   ├── start_postgres.ps1
│   ├── preflight.ps1
│   └── run_workflow.py
├── start_production.ps1
├── stop_production.ps1
├── graph.py
├── config.py
└── runtime/
```

在项目根目录运行本地版：

```powershell
.\scripts\start_local.ps1
```

在项目根目录运行生产版：

```powershell
.\start_production.ps1
```

如果使用本地版，常见参数如下：

```powershell
.\scripts\start_local.ps1 -ThreadId video-002
.\scripts\start_local.ps1 -StorylineMode human
.\scripts\start_local.ps1 -SkipPreflight
.\scripts\start_local.ps1 -WhatIf
```

生产版常见参数：

```powershell
.\start_production.ps1 -ThreadId video-002
.\start_production.ps1 -SkipPhase3
.\start_production.ps1 -WhatIf
```

`ThreadId` 是 LangGraph 的线程标识，也用于初始状态文件名和 checkpoint 查询。不同任务应使用不同 ID，避免读取或恢复到另一条任务的状态。

## 3. `scripts/start_local.ps1` 运行流程

### 3.1 参数与路径

| 参数 | 默认值 | 作用 |
|---|---|---|
| `-ThreadId` | `video-001` | 工作流线程 ID |
| `-VideoPath` | 空字符串 | 输入视频；为空时使用项目内 `inputs\30s.mp4` |
| `-StorylineMode` | `auto` | `auto` 自动剧情线；`human` 走人工规划路径 |
| `-PostgresUri` | `postgresql://postgres:devpass@localhost:5432/langgraph` | 设置生产工作流数据库连接串 |
| `-SkipPreflight` | 关闭 | 跳过本脚本显式调用的 PowerShell preflight |
| `-WhatIf` | 关闭 | 打印计划并退出；仍会先创建日志目录/日志文件 |

脚本通过 `$PSScriptRoot` 计算仓库根目录，因此脚本可以从其他当前工作目录调用，不依赖用户当前所在目录。

日志写入：`<RepoRoot>\logs\start_local_<时间戳>.log`。日志追加失败会被忽略，但创建日志目录等初始化操作仍可能中断。

### 3.2 阶段说明

| 阶段 | 实际动作 | 失败行为 |
|---|---|---|
| 初始化 | 配置 UTF-8、创建 `logs`、生成带时间戳的日志名 | 日志写入失败一般被忽略 |
| `WhatIf` | 输出视频、Postgres、preflight、Python 工作流的计划 | 退出码 `0`；不执行实际启动阶段，但日志目录/文件已创建 |
| Phase 0 | 默认视频为 `<RepoRoot>\inputs\30s.mp4`；检查文件存在；生成 `runtime\initial_state_<ThreadId>.json` | 视频不存在时退出 `12` |
| Phase 1 | 调用 `scripts\start_postgres.ps1`，等待 `edb-postgres16` 健康 | 子脚本非零时退出 `14` |
| Phase 2 | 默认调用 `scripts\preflight.ps1 -SkipDockerService -SkipProxyCheck` | preflight 非零时退出 `15`；传 `-SkipPreflight` 时跳过这一阶段 |
| Phase 3 | 检查 `.venv\Scripts\python.exe`，设置环境变量，补充可能缺失的 `ffmpeg` PATH，然后运行 `run_workflow.py --production` | Python 环境缺失退出 `16`；工作流非零且不包含预期人工中断标志时按 Python 退出码失败 |
| 收尾 | 输出日志路径与停止命令提示 | 正常或识别为预期 `GraphInterrupt` 时退出 `0` |

Phase 0 写入的状态结构为：

```json
{
  "session_id": "video-001",
  "video_input_path": "绝对视频路径",
  "error_log": []
}
```

生成 JSON 时使用 UTF-8 无 BOM 编码。视频只做存在性检查，不会复制到项目目录，也不会在启动阶段验证视频能否被 FFmpeg 解码。

### 3.3 环境变量与实际执行

在调用 Python 前，脚本设置：

```powershell
$env:POSTGRES_URI = $PostgresUri
$env:STORYLINE_MODE = $StorylineMode
$env:AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE = '1'
$env:AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_PROXY_CHECK = '1'
```

脚本随后执行的核心命令等价于：

```powershell
.\.venv\Scripts\python.exe scripts\run_workflow.py `
    --production `
    --thread-id $ThreadId `
    --initial-state-json $StateJson
```

若 `PATH` 中找不到 `ffmpeg`，脚本只会尝试扫描 `C:\ffmpeg\*\bin\ffmpeg.exe`，找到后临时追加到当前脚本进程的 PATH；找不到则警告，工作流仍会启动。

### 3.4 `GraphInterrupt` 处理

脚本捕获 Python 输出，并查找 `GraphInterrupt`、`checkpoint ... ①` 等文本。如果 Python 退出码非零但输出命中这些模式，就按预期人工关卡处理，脚本最终仍返回 `0`。

这代表：**启动脚本成功退出，不等于整条视频工作流已产出最终视频**。人工关卡可能要求在剪映中完成操作，再通过 checkpoint 恢复工作流。

### 3.5 本地版流程图

```mermaid
flowchart TD
    A([启动 start_local.ps1]) --> B[解析参数、创建日志、设置 UTF-8]
    B --> C{WhatIf?}
    C -- 是 --> D[打印启动计划]
    D --> Z0([退出 0])
    C -- 否 --> E[解析 VideoPath，默认 inputs/30s.mp4]
    E --> F{视频存在?}
    F -- 否 --> F1[记录错误] --> X12([退出 12])
    F -- 是 --> G[生成 runtime/initial_state_<ThreadId>.json]
    G --> H[调用 start_postgres.ps1]
    H --> I{Postgres 启动成功?}
    I -- 否 --> X14([退出 14])
    I -- 是 --> J{SkipPreflight?}
    J -- 否 --> K[调用 preflight.ps1<br/>跳过 Docker 服务和代理检查]
    K --> L{Preflight 成功?}
    L -- 否 --> X15([退出 15])
    L -- 是 --> M[检查项目 venv Python]
    J -- 是 --> M
    M --> N{Python 存在?}
    N -- 否 --> X16([退出 16])
    N -- 是 --> O[设置 POSTGRES_URI / STORYLINE_MODE / preflight skip 环境变量]
    O --> P[检查并尝试补充 ffmpeg PATH]
    P --> Q[运行 run_workflow.py --production]
    Q --> Q1[Python 入口再次执行内部 preflight<br/>Docker 服务与代理检查按环境变量跳过]
    Q1 --> Q2{内部 preflight 成功?}
    Q2 -- 否 --> X([按 Python 退出码退出])
    Q2 -- 是 --> Q3[build_graph + ainvoke 执行 LangGraph]
    Q3 --> R{退出码为 0?}
    R -- 是 --> S([启动流程完成，退出 0])
    R -- 否 --> T{输出包含 GraphInterrupt?}
    T -- 是 --> S
    T -- 否 --> X
```

## 4. 根目录 `start_production.ps1` 运行流程

### 4.1 参数与路径

| 参数 | 默认值 | 作用 |
|---|---|---|
| `-ThreadId` | `video-001` | 工作流线程 ID |
| `-SourceVideo` | `E:\Documents\kuaishou\FireRed-OpenStoryline\.claude\skills\jianying-editor\assets\video.mp4` | 源视频位置 |
| `-InputsDir` | `E:\Documents\kuaishou\FireRed-OpenStoryline\inputs` | 输入视频目标目录 |
| `-InputsFileName` | `30s.mp4` | 目标文件名 |
| `-PostgresUri` | `postgresql://postgres:devpass@localhost:5432/langgraph` | 设计上用于覆盖数据库连接串，但当前脚本没有实际使用该参数 |
| `-SkipPhase3` | 关闭 | 跳过启动后的验收查询 |
| `-WhatIf` | 关闭 | 打印计划后退出；仍会先创建日志目录/日志文件 |

日志写入：`<RepoRoot>\logs\start_<时间戳>.log`。OpenStoryline 另有 `openstoryline_web_<时间戳>.log` 和 `openstoryline_pids_<时间戳>.json`。

### 4.2 阶段说明

| 阶段 | 实际动作 | 失败行为 |
|---|---|---|
| 初始化 | 设置 UTF-8、创建日志、计算仓库路径和 OpenStoryline 路径 | 日志文件相关异常可能中止脚本 |
| `WhatIf` | 打印视频复制、Docker、资源检查、Web、preflight、工作流及验收计划 | 退出码 `0`；日志目录/文件已创建 |
| 权限前置检查 | 检查当前 PowerShell 是否管理员 | 非管理员退出 `11` |
| Phase 0 | 检查源视频；创建目标目录；仅当目标文件不存在时才复制视频；生成初始状态 JSON | 源视频不存在退出 `12` |
| Phase 0.5 | 执行 `docker info`；若 daemon 不可达则启动 Docker Desktop 并最多等待 90 秒 | Docker Desktop 不存在或 daemon 超时退出 `17` |
| Phase 0.55 | 检查 `openstoryline\resource` 和 `openstoryline\.storyline\models` 非空 | 缺失时提示下载并退出 `16` |
| Phase 0.6 | 校验 OpenStoryline 独立 venv、`agent_fastapi.py` 和包目录；复用 7860 端口或清理旧 PID 后启动 Uvicorn；保存 PID 文件 | 路径不全时退出 `18` |
| Phase 0.7 | 最多 180 秒检查进程、7860 端口及启动日志 | 超时只警告，**仍继续后续流程**；进程已退出时退出 `21` |
| Phase 1 | 调用 `scripts\preflight.ps1` 执行完整 6 步检查 | 失败时按 preflight 退出码退出 |
| Phase 2 | 检查项目 venv 和 `scripts\run_workflow.py`，运行生产工作流 | 路径缺失分别退出 `14` / `15`；非预期 Python 错误退出 `16` |
| Phase 3 | 可选验收：查询 Postgres checkpoint、读取心跳文件、列出 `outputs\<ThreadId>` 和 draft 目录 | `-SkipPhase3` 时跳过；部分验收查询错误被捕获后仅打印，不一定使脚本失败 |
| 收尾 | 输出完整日志路径 | 退出 `0` |

### 4.3 视频文件处理

生产版默认源视频与输入目录位于 `FireRed-OpenStoryline` 路径，而不一定在 `auto-video-editor` 仓库内。

其复制策略是：

- 源文件不存在：退出 `12`。
- 目标目录不存在：创建目录。
- 目标 `30s.mp4` 不存在：复制源视频。
- 目标文件已经存在：**跳过复制，不覆盖旧文件**。

因此，若源视频已经更新但目标文件仍存在，当前脚本会继续使用目标目录中的旧视频。要更换素材，需要手动替换目标文件，或修改脚本的覆盖策略。

### 4.4 OpenStoryline 启动与健康检查

生产版会在调用 LangGraph 前自行启动 `openstoryline` Web：

- 工作目录：`<RepoRoot>\openstoryline`
- Python：`openstoryline\.venv\Scripts\python.exe`
- 命令：`-m uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`
- 监听地址：`127.0.0.1:7860`
- 健康条件：端口开放，日志出现 `Uvicorn running on` 或 `Application startup complete`，且脚本自己启动的 PID 仍存活（复用模式跳过 PID 检查）。

要注意：180 秒健康条件不满足时，脚本只是输出 warning 并继续，并非 fail-fast。下游可能在服务未就绪时继续运行。

### 4.5 生产版流程图

```mermaid
flowchart TD
    A([启动 start_production.ps1]) --> B[解析参数、创建日志、设置 UTF-8]
    B --> C{WhatIf?}
    C -- 是 --> D[打印计划] --> Z0([退出 0])
    C -- 否 --> E{当前是管理员?}
    E -- 否 --> X11([退出 11])
    E -- 是 --> F[检查 SourceVideo]
    F --> G{源视频存在?}
    G -- 否 --> X12([退出 12])
    G -- 是 --> H{目标 inputs 文件存在?}
    H -- 否 --> I[复制视频到 InputsDir/InputsFileName]
    H -- 是 --> J[复用现有目标文件]
    I --> K[生成 initial_state JSON]
    J --> K
    K --> L{Docker daemon 在线?}
    L -- 是 --> N[校验 OpenStoryline 资源和模型]
    L -- 否 --> M[启动 Docker Desktop并等待最多 90 秒]
    M --> M1{daemon 就绪?}
    M1 -- 否 --> X17([退出 17])
    M1 -- 是 --> N
    N --> O{resource 和 models 均非空?}
    O -- 否 --> X16([提示下载并退出 16])
    O -- 是 --> P[校验 OpenStoryline venv/入口/包]
    P --> Q{7860 已监听?}
    Q -- 是 --> R[复用现有 Web，写 reused PID 标记]
    Q -- 否 --> S[清理旧 PID 指向进程并启动 Uvicorn]
    R --> T[健康检查：PID / 7860 / startup 日志]
    S --> T
    T --> U{180 秒内健康?}
    U -- 是 --> V[调用完整 preflight.ps1]
    U -- 否 --> W[记录 warning，仍继续]
    W --> V
    V --> V1{preflight 成功?}
    V1 -- 否 --> X([按 preflight 退出码退出])
    V1 -- 是 --> Y[检查 venv 和 run_workflow.py]
    Y --> Y1{路径存在?}
    Y1 -- 否 --> X14([退出 14 或 15])
    Y1 -- 是 --> AA[运行 run_workflow.py --production]
    AA --> AA1[Python 入口再次执行完整 preflight]
    AA1 --> AA2{内部 preflight 成功?}
    AA2 -- 否 --> X16B([非零退出码，脚本按错误处理])
    AA2 -- 是 --> AA3[build_graph + ainvoke 执行 LangGraph]
    AA3 --> AB{退出码为 0?}
    AB -- 是 --> AC[按需执行 Phase 3 验收]
    AB -- 否 --> AD{输出包含 GraphInterrupt?}
    AD -- 是 --> AC
    AD -- 否 --> X16B
    AC --> AE{SkipPhase3?}
    AE -- 是 --> AF([退出 0])
    AE -- 否 --> AG[查询 checkpoint / heartbeat / outputs / drafts]
    AG --> AH([启动脚本结束，退出 0])
```

## 5. Python 入口与 LangGraph 的关系

两个 PowerShell 脚本最终调用同一个 Python 入口。其关键行为如下：

1. `--production` 会在 `_main_async()` 中设置 `WORKFLOW_ENV=production`。
2. Python 入口默认执行 `_maybe_preflight()`；除非传入 `--skip-preflight`，否则会再次启动 `runtime/preflight.py` 包装的 `scripts/preflight.ps1`。
3. 成功后调用 `graph.build_graph()`，建立生产 checkpointer 并通过 `ainvoke()` 执行图。
4. `ThreadId` 会作为 LangGraph `configurable.thread_id`，供 checkpoint 的隔离与恢复使用。
5. 图里会执行 `node_02_launch_openstoryline`。该节点在 `STORYLINE_MODE=auto` 时跳过 Web 服务启动；在 `human` 模式时自行启动 Uvicorn 并检查 Web 健康状态。

Python 侧的 preflight 通过下列环境变量读取跳过选项：

```text
AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_DOCKER_SERVICE
AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_PROXY_CHECK
AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START
AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_SCHEMA_SETUP
```

本地脚本只设置前两个变量。因此，即使本地脚本先执行了一次显式 preflight，Python 入口仍会再执行一次 preflight；第二次仍会检查 daemon、重新检查/启动 Postgres、验证连接并确保 schema。

## 6. 两个脚本的差异对照

| 对比项 | `scripts/start_local.ps1` | `start_production.ps1` |
|---|---|---|
| 主要目标 | 快速本地开发 | 完整启动和验收 |
| 脚本位置 | `scripts/` 子目录 | 仓库根目录 |
| 管理员要求 | 不做统一管理员自检 | 启动前强制要求管理员 |
| 视频默认来源 | `<RepoRoot>\inputs\30s.mp4` | 外部 `FireRed-OpenStoryline` 演示素材路径 |
| 视频复制 | 不复制；检查指定文件存在 | 目标文件不存在时复制，目标存在则跳过 |
| 初始状态 | 自动生成 `runtime\initial_state_<ThreadId>.json` | 同样自动生成 |
| Docker | 假设 Docker daemon 已可用；不做 Docker 服务自检/代理自检 | 检查 daemon；必要时启动 Docker Desktop，preflight 执行完整检查 |
| Postgres | 先单独调用 `start_postgres.ps1` | 通过完整 preflight 的 Step 4 启动/检查 Postgres |
| OpenStoryline | 不在脚本层显式启动；由图节点按模式处理 | 先检查资源与模型，再由脚本启动/复用 Web 7860 |
| 默认 `STORYLINE_MODE` | `auto` | 不显式设置；通常继承当前 shell 环境，若未设置则由 `config.py` 默认 `human` |
| 手动模式 | `-StorylineMode human` | 取决于当前环境中的 `STORYLINE_MODE`，脚本没有自己的模式参数 |
| preflight | 显式调用时跳过 Docker 服务和代理检查；Python 入口还会再跑一次 | 显式运行完整 preflight；Python 入口通常还会再跑一次 |
| FFmpeg PATH 处理 | 尝试查找 `C:\ffmpeg\*\bin` 并追加到 PATH | 无对应的脚本级自动补充步骤 |
| 工作流调用 | `run_workflow.py --production` | `run_workflow.py --production` |
| 启动后验收 | 不执行数据库/输出目录验收 | 默认检查 checkpoint、heartbeat、outputs、drafts；可用 `-SkipPhase3` 跳过 |
| 日志文件 | `start_local_<时间戳>.log` | `start_<时间戳>.log`，另有 OpenStoryline Web 日志和 PID 文件 |
| 数据库 URI 参数 | 在调用 Python 前写入 `$env:POSTGRES_URI`，参数有效 | 声明了 `-PostgresUri`，但当前文件中没有使用该参数，覆盖值不会按预期生效 |

## 7. 当前实现中值得修正的问题

### 7.1 preflight 和 Postgres 会重复执行

**本地版执行链**目前是：

1. 直接调用 `scripts\start_postgres.ps1`。
2. 默认再调用一次 `scripts\preflight.ps1`，其中 Step 4 又调用 `start_postgres.ps1`。
3. 随后调用 `run_workflow.py --production`，Python 入口默认还会调用 preflight；其中 Step 4 又会启动/检查 Postgres。

所以默认情况下，Postgres 启动脚本可能被调用三次，preflight 至少执行两次。传入 `-SkipPreflight` 也只跳过第 2 项；Python 入口内部的 preflight 仍会执行。

**生产版执行链**是：脚本显式执行一次完整 preflight，然后 `run_workflow.py` 又执行一次内部 preflight。

影响：启动时间增长；Docker 镜像 pull、容器 start、数据库连接和 schema 检查重复执行；`-SkipPreflight` 的字面含义容易让人误解。

建议：每个启动入口只保留一处完整环境准备。若由 PowerShell 先执行 preflight，则调用 Python 时加 `--skip-preflight`；若确实要跳过所有环境自检，也应同步向 Python 入口传 `--skip-preflight`，并明确调用者必须保证数据库和 schema 已就绪。

### 7.2 生产版 `-PostgresUri` 参数没有生效

`start_production.ps1` 声明了 `$PostgresUri`，但脚本中没有将它赋值给 `$env:POSTGRES_URI`。`preflight.ps1` 在环境变量为空时会设置默认 URI，因此默认值可能看似能工作；但传入自定义 `-PostgresUri` 时，该参数不会被使用。

建议在运行 preflight 和 Python 前，明确设置：

```powershell
$env:POSTGRES_URI = $PostgresUri
```

并确认启动 Postgres 的用户名、密码、数据库名与该 URI 一致。注意：`scripts\start_postgres.ps1` 当前容器配置固定为 `postgres/devpass/langgraph`，单纯修改连接串并不会自动修改容器凭据或数据库名。

### 7.3 生产版 checkpoint 验收缺少 `THREAD_ID`

生产版 Phase 3 的 Python 查询使用：

```python
os.environ['THREAD_ID']
```

但 `start_production.ps1` 中没有设置 `$env:THREAD_ID`。因此该查询会触发 `KeyError`，被内部 `except` 捕获后打印“查询失败”，而不是输出指定 thread 的 checkpoint 数量。当前异常被吞掉，脚本可能仍以退出码 `0` 收尾，造成验收信息不完整。

建议在启动前设置：

```powershell
$env:THREAD_ID = $ThreadId
```

或把 `$ThreadId` 直接安全地作为参数传给查询代码。修正后还应检查查询失败是否需要让验收阶段返回非零退出码。

### 7.4 生产版可能重复启动 OpenStoryline Web

生产脚本的 Phase 0.6 已启动或复用 `127.0.0.1:7860`。但默认 `STORYLINE_MODE` 通常是 `human`，随后 LangGraph 的 `node_02_launch_openstoryline` 仍会执行自己的 `subprocess.Popen()` 启动逻辑，没有先检查现有 Web 是否可复用。

可能结果：第二个 Uvicorn 进程因 7860 端口占用而退出；节点却可能因为第一个服务仍然返回 HTTP 200 而判定健康，从而保留一个已经退出的 PID。它未必立即让工作流失败，但会造成重复启动、错误 PID 状态和日志混淆。

建议明确唯一的进程管理责任：

- 方案 A：由 `start_production.ps1` 负责启动与健康检查，`node_02` 检测到现有 Web 后直接复用；或
- 方案 B：由 `node_02` 单独负责启动，生产脚本不再提前启动 Web。

不建议让两层都无条件启动同一服务。

### 7.5 `STORYLINE_MODE` 在两个入口间不一致

- 本地版默认将 `STORYLINE_MODE` 设置为 `auto`。该模式走 19 节点确定性剧情线，`node_02` 会跳过 OpenStoryline Web UI 启动。
- 生产版不声明或设置该变量。如果当前 PowerShell 环境没有设置它，`config.py` 默认值为 `human`；这与本地版默认行为不同。
- 本地版直接设置 `$env:STORYLINE_MODE`，没有恢复原值。若脚本从现有 PowerShell 会话执行，变量可能留在当前进程环境中，影响同一会话后续运行的生产脚本。

建议生产脚本也提供显式的 `-StorylineMode` 参数，并在运行 Python 前设置它；同时在脚本结束时恢复原来的环境变量，或在子进程环境中单独设置，避免跨次调用污染。

### 7.6 生产版健康检查超时不阻止执行

Phase 0.7 最多等待 180 秒。若 PID、端口和日志启动标记未同时满足，当前实现仅发出 warning，之后仍调用 preflight 和工作流。

建议根据工作模式决定是否 fail-fast。若 `human` 模式必须依赖 OpenStoryline UI，健康检查失败应退出非零；若确认该服务只在特定分支需要，也应把健康状态和模式条件关联起来。

### 7.7 `-WhatIf` 不完全无副作用

两个脚本都先创建 `logs` 目录并初始化日志，再判断 `-WhatIf`。因此 `-WhatIf` 不会启动 Docker、Postgres 或工作流，但可能创建日志文件。若需要严格只读的 dry-run，应把 `WhatIf` 判断移到日志目录创建之前，或允许 dry-run 输出只写控制台。

## 8. 建议的启动职责划分

建议将两条路径收敛为以下约定：

- **本地快速启动**：使用项目内素材；明确选择 `auto` 或 `human`；避免重复执行 Postgres 和 preflight；保留易查看的单份启动日志。
- **生产全栈启动**：明确模式、素材目录和 `POSTGRES_URI`；只执行一次环境预检；OpenStoryline 进程只由一层管理；验收查询必须有 `ThreadId`，且关键验收失败应反映为非零状态。
- **人工关卡语义**：日志中区分“启动脚本结束”“图在 interrupt 处挂起”“完整工作流产出最终文件”三种状态，不能只用统一的“启动完成”来表达。

## 9. 参考代码入口

- [`scripts/start_local.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/start_local.ps1)
- [`start_production.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/start_production.ps1)
- [`scripts/start_postgres.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/start_postgres.ps1)
- [`scripts/preflight.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/preflight.ps1)
- [`scripts/run_workflow.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/run_workflow.py)
- [`runtime/preflight.py`](https://github.com/wayyet/auto-video-editor/blob/main/runtime/preflight.py)
- [`nodes/node_02_launch_openstoryline.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_02_launch_openstoryline.py)
- [`graph.py`](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)

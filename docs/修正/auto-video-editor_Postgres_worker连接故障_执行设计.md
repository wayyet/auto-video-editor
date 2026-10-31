# auto-video-editor：Worker 无法连接 PostgreSQL Checkpointer 执行设计

> **项目**：`wayyet/auto-video-editor`  
> **分析分支**：`main`  
> **分析日期**：2026-10-31  
> **故障现象**：`worker` 连接 PostgreSQL Checkpointer 失败，错误为 `no password supplied`  
> **目标**：修复 PostgreSQL 连接参数在 OpenStoryline → MCode → MCP → Worker 进程链路中的传播问题，并增加同路径诊断与回归测试。

---

## 1. 结论摘要

这次故障不能简单归因为“Postgres 服务没启动”。截图显示 Postgres 服务正在运行，Worker 已被启动，但 Worker 建立检查点连接时没有拿到可用密码。

结合 `main` 分支代码，最重要的问题是：

1. `start_production.ps1` **先启动 OpenStoryline Web（Uvicorn），后设置 `POSTGRES_URI`**。子进程只继承创建时父进程的环境变量，父进程稍后设置的变量不会自动补到已运行的 Uvicorn 进程中。
2. MCode 及其 MCP 子进程继承 OpenStoryline 进程环境；`runner.spawn_worker()` 又从 MCP 进程环境构造 Worker 环境。因此，启动顺序错误会沿进程链传递。
3. `agent_gate.runner.postgres_uri()` 的优先级是 `AGENT_GATE_POSTGRES_URI` → `POSTGRES_URI` → `config.POSTGRES_URI`。其中 `AGENT_GATE_POSTGRES_URI` 可以覆盖正确的 `POSTGRES_URI`。
4. `config.py` 使用 `os.environ.get("POSTGRES_URI", 默认值)`。当变量**存在但为空字符串**时，空字符串会覆盖默认值；这会使“环境变量未正确注入”从预期的默认 DSN 变成空 DSN。
5. 截图里的精确错误 `no password supplied` 说明最终连接参数没有提供服务端要求的密码。它不等价于“环境变量一定不存在”。还必须检查空值、无密码 DSN 和高优先级覆盖变量。

**建议的修复顺序**：先修正 `start_production.ps1` 的环境变量注入时机；再修正空值处理及高优先级覆盖变量；随后增加 Worker 实际 DSN 解析诊断、MCP→Worker 环境继承测试；最后重启旧 Web 进程并按检查点状态恢复任务。

> 本文是基于代码审查形成的执行设计，不代表已修改仓库或已在用户机器完成修复。仓库外操作员目录中的 `.mcp.json` 未纳入该仓库，当前无法从 `main` 分支直接核实其 `env` 配置。

## 2. 截图与错误分析

截图中的可确认信息：

- OpenStoryline Web 地址为 `127.0.0.1:7860`，界面模型为 `mcode-operator`。
- Worker 已启动，显示 PID `8288`。
- Checkpointer 初始化失败，错误片段为 `no password supplied`。
- 错误定位指向 PostgreSQL 连接参数，而不是 LangGraph 节点执行或视频素材读取。

### 2.1 `no password supplied` 的含义

PostgreSQL 服务端要求密码认证，但客户端建立连接时没有提供密码。常见原因包括：

- DSN 本身没有密码，例如 `postgresql://postgres@localhost:5432/video_workflow`。
- 高优先级环境变量提供了无密码 DSN。
- `POSTGRES_URI` 存在但值为空，配置加载逻辑没有回退到默认值。
- 实际运行的服务进程继承了旧环境，使用的不是启动脚本当前显示的 DSN。

这与以下问题不同：

| 错误类型 | 常见含义 |
|---|---|
| `no password supplied` | 连接参数中未提供密码，但服务端要求密码 |
| `password authentication failed` | 提供了密码，但密码错误或认证不通过 |
| `connection refused` / timeout | TCP 端口不可达、服务未监听或网络超时 |
| `database does not exist` | 已连接到 PostgreSQL 服务，但目标数据库不存在 |

### 2.2 仓库默认连接串

`config.py` 的默认值是：

```text
postgresql://postgres:devpass@localhost:5432/video_workflow
```

`agent_gate/runner.py` 的 `DEFAULT_POSTGRES_URI` 也是该值，**默认串包含密码 `devpass`**。

因此，若 Worker 完全没有设置 `POSTGRES_URI`、也没有设置 `AGENT_GATE_POSTGRES_URI`，且能够正常导入仓库根目录的 `config.py`，按当前代码通常会使用包含密码的默认值。仅用“`POSTGRES_URI` 没继承下来”不足以解释所有 `no password supplied` 情况。

需要特别检查以下两种边界情况：

1. `AGENT_GATE_POSTGRES_URI` 被设置为不带密码的 URI。它的优先级高于 `POSTGRES_URI`。
2. `POSTGRES_URI` 在 Worker 环境中存在但值为空。`runner.postgres_uri()` 会跳过空的环境变量，随后读取 `config.POSTGRES_URI`；但 `config.py` 在模块导入时使用 `os.environ.get()`，空字符串会覆盖默认值，最终可能返回空连接串。

## 3. 代码调用链和故障传播路径

### 3.1 生产运行链路

```mermaid
flowchart TD
    A[启动 start_production.ps1] --> B[启动 OpenStoryline Uvicorn :7860]
    B --> C[浏览器发起 MCode turn]
    C --> D[MCodeChatModel 启动 mcode exec]
    D --> E[mcode 读取操作员目录 .mcp.json]
    E --> F[启动 agent_gate.mcp_server]
    F --> G[run_start 调 runner.spawn_worker]
    G --> H[启动 python -m agent_gate worker]
    H --> I[runner.postgres_uri 选择 DSN]
    I --> J[PostgresSaver 建立连接并 setup]
    J --> K[LangGraph graph.invoke]
```

### 3.2 环境变量继承关系

```mermaid
sequenceDiagram
    participant PS as PowerShell 启动脚本
    participant WEB as OpenStoryline/Uvicorn
    participant MC as MCode CLI
    participant MCP as agent_gate MCP
    participant WK as Worker
    participant PG as PostgreSQL

    PS->>WEB: Start-Process（创建时复制当前环境）
    Note over PS: 此时脚本尚未设置本次 -PostgresUri
    PS->>PS: 后续才设置 POSTGRES_URI
    WEB->>MC: 创建 mcode 子进程，默认继承 WEB 环境
    MC->>MCP: 根据 .mcp.json 启动 MCP 服务
    MCP->>WK: spawn_worker(env=dict(os.environ, ...))
    WK->>WK: 解析 AGENT_GATE_POSTGRES_URI / POSTGRES_URI / config
    WK->>PG: PostgresSaver 连接
    PG-->>WK: no password supplied
```

### 3.3 关键代码证据

| 文件 | 当前实现 | 影响 |
|---|---|---|
| `start_production.ps1` | Phase 0.6/0.7 启动 Web；之后才执行 `$env:POSTGRES_URI = $PostgresUri` | 新 Web 进程拿不到本次脚本稍后设置的变量 |
| `openstoryline/src/open_storyline/mcode_chat_model.py` | `subprocess.Popen()` / `asyncio.create_subprocess_exec()` 未显式传 `env` | MCode 默认继承 OpenStoryline 进程环境 |
| `agent_gate/mcp_server.py` | `run_start()` 调用 `runner.spawn_worker()` | MCP 是 Worker 的启动入口，但不自行加载 `.env` |
| `agent_gate/runner.py` | `env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")` | Worker 继承 MCP 当前环境；这里没有过滤 `POSTGRES_URI` |
| `agent_gate/runner.py` | `postgres_uri()` 优先读取 `AGENT_GATE_POSTGRES_URI` | 可能被旧测试变量或 MCP 配置中的变量覆盖 |
| `config.py` | `os.environ.get("POSTGRES_URI", 默认 DSN)` | 变量值为空字符串时，不会使用默认 DSN |

**判断**：仓库里能直接确认的是启动脚本的环境变量注入顺序存在缺陷。MCP 的 `.mcp.json` 是仓库外配置，无法仅凭当前仓库内容判断它是否另行注入了空值或无密码 URI。因此，最终 DSN 的来源需要由 Worker 自己报告，不能只看启动脚本的控制台输出。

## 4. 根因分级

### P0：启动脚本注入环境变量过晚

`start_production.ps1` 先调用 `Start-Process` 启动 Uvicorn，等 Web 健康检查通过后才设置 `POSTGRES_URI`。Windows 进程环境是创建时继承的快照，父进程后续修改环境变量不会反向更新子进程。

所以，即使启动脚本后面打印了正确的 URI，也不能证明早先启动的 Uvicorn、MCode、MCP 和 Worker 使用了它。

### P0：最终 DSN 可能被更高优先级配置覆盖

`runner.postgres_uri()` 的真实优先级如下：

```text
AGENT_GATE_POSTGRES_URI
        ↓（为空才继续）
POSTGRES_URI
        ↓（为空才继续）
config.POSTGRES_URI
        ↓（config 导入异常时）
DEFAULT_POSTGRES_URI
```

生产启动应避免遗留的 `AGENT_GATE_POSTGRES_URI` 意外指向测试库、旧库或不带密码的 URI。

### P1：配置空字符串会遮蔽默认值

当前 `config.py`：

```python
POSTGRES_URI: str = os.environ.get(
    "POSTGRES_URI",
    "postgresql://postgres:devpass@localhost:5432/video_workflow",
)
```

当 `POSTGRES_URI=""` 时，`os.environ.get()` 返回空字符串，而不是默认值。该值随后可能被 `runner.postgres_uri()` 当作 `config.POSTGRES_URI` 返回。

### P1：MCP 外部配置未纳入统一配置审计

MCode 从操作员目录的 `.mcp.json` 读取 MCP 服务配置。该文件在仓库外，需人工检查：

- 是否在 `env` 中显式设置 `POSTGRES_URI` 或 `AGENT_GATE_POSTGRES_URI`。
- 是否将变量设为空字符串。
- URI 是否缺少 `:password@` 部分。
- 服务 command 是否指向当前仓库的 `.venv\Scripts\python.exe`，而不是旧 checkout 或旧虚拟环境。
- MCP 的 `cwd` 是否使 Worker 能导入当前仓库根目录的 `config.py`。

不要在日志或截图中输出完整密码。

## 5. 修复目标和设计原则

### 5.1 修复目标

1. 在任何 OpenStoryline Web、MCode、MCP 或 Worker 子进程创建前，设置好本次启动的 `POSTGRES_URI`。
2. 生产启动时明确清理测试专用的 `AGENT_GATE_POSTGRES_URI`，避免其覆盖生产 DSN。
3. 所有 DSN 候选值统一处理空字符串。
4. 日志输出 DSN 来源和脱敏元数据，禁止输出密码。
5. 提供一个使用 Worker 同样解析逻辑的 PostgreSQL 检查脚本。
6. 覆盖“父进程启动后修改环境变量不会影响子进程”的测试场景。
7. 修复后只在确认旧 Web 进程已重启、配置已生效后恢复任务。

### 5.2 设计原则

- PostgreSQL 是生产检查点唯一持久化后端；不能将 `memory` 当作生产修复方案。
- `AGENT_GATE_POSTGRES_URI` 保留为测试隔离用途，但生产启动脚本必须避免遗留值污染。
- 配置诊断只输出来源、主机、端口、数据库、用户名、是否含密码等非秘密信息。
- 不删除数据库、不重建 Docker volume、不清空 checkpoint 表。
- 先修连接配置，再处理任务恢复；不能因为连接失败就盲目重新执行同一个视频任务。

## 6. 具体修改设计

### 6.1 修改 `start_production.ps1`：将环境变量注入移到 Web 启动之前

**修改位置**：当前环境变量备份段之后，且必须早于 Phase 0.6 中的 `Start-Process`。

建议处理步骤：

1. 先备份 `POSTGRES_URI`、`AGENT_GATE_POSTGRES_URI`、`STORYLINE_MODE`、`THREAD_ID`、`PYTHONUNBUFFERED`、`PYTHONIOENCODING` 等原值。
2. 在启动任何服务之前，设置 `$env:POSTGRES_URI = $PostgresUri`。
3. 清除当前 PowerShell 进程中的 `AGENT_GATE_POSTGRES_URI`，防止其优先级覆盖生产 URI。
4. 提前设置 Web/MCode 运行需要的其它变量。
5. 删除或简化原来 Phase 0.7 后的重复注入块，保留脱敏日志。
6. 继续在 `finally` 中恢复环境变量，避免污染调用者的 PowerShell 会话。

建议代码结构：

```powershell
# 1. envBackup 必须包含 AGENT_GATE_POSTGRES_URI
$envBackup = @{}
foreach ($name in @(
    'POSTGRES_URI',
    'AGENT_GATE_POSTGRES_URI',
    'STORYLINE_MODE',
    'THREAD_ID',
    'PYTHONUNBUFFERED',
    'PYTHONIOENCODING',
    'AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START'
)) {
    $envBackup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

# 2. 在启动 OpenStoryline Web / MCode / MCP 之前统一注入
$env:POSTGRES_URI = $PostgresUri
Remove-Item Env:AGENT_GATE_POSTGRES_URI -ErrorAction SilentlyContinue
$env:STORYLINE_MODE = $StorylineMode
$env:THREAD_ID = $ThreadId
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
```

注意：以上是修改结构示例，需合并到现有脚本，不要复制成第二份独立启动逻辑。脚本原有 `$envBackup` 的 `finally` 恢复行为应保留。

### 6.2 处理“已经运行的 Web 服务”

只移动环境变量设置位置，还不足以修复已经运行的 Uvicorn。`start_production.ps1` 支持复用 `127.0.0.1:7860` 的健康 Web 服务；复用进程仍保留旧环境。

修复部署时必须：

1. 读取最新 `logs\openstoryline_pids_*.json`。
2. 检查 `reused` 是否为 `false`，并确认 `web_pid` 是本仓库启动的进程。
3. 只停止该 PID 对应的自有 Web 进程。
4. 不要停止 PostgreSQL 容器，不要删除 Docker volume。
5. 用修正后的 `start_production.ps1` 重新启动 Web。

安全的手动检查示例：

```powershell
$pidFile = Get-ChildItem .\logs\openstoryline_pids_*.json |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $pidFile) {
    throw '没有找到 OpenStoryline PID 文件，不能自动判断进程所有权。'
}

$info = Get-Content $pidFile.FullName -Raw | ConvertFrom-Json

if ($info.reused -ne $false -or -not $info.web_pid) {
    throw '当前 Web 进程不是本启动脚本明确拥有的进程；请勿自动终止。'
}

Get-CimInstance Win32_Process -Filter "ProcessId=$($info.web_pid)" |
    Select-Object ProcessId, ParentProcessId, CommandLine

# 人工确认 CommandLine 属于本仓库的 agent_fastapi:app 后再执行：
Stop-Process -Id ([int]$info.web_pid) -Force
```

不要直接使用 `stop_production.ps1` 作为“只重启 Web”的命令：该脚本还会处理 PostgreSQL 容器停止，容易把数据库服务一并停掉。

### 6.3 修改 `agent_gate/runner.py`：统一空值与来源解析

建议将当前返回字符串的函数改成可追踪来源的解析函数，例如：

```python
from dataclasses import dataclass
from urllib.parse import urlsplit

@dataclass(frozen=True)
class PostgresTarget:
    uri: str
    source: str


def _non_empty_env(name: str) -> str | None:
    value = os.environ.get(name)
    value = value.strip() if value else ""
    return value or None


def resolve_postgres_target() -> PostgresTarget:
    for name in ("AGENT_GATE_POSTGRES_URI", "POSTGRES_URI"):
        value = _non_empty_env(name)
        if value:
            return PostgresTarget(uri=value, source=f"env:{name}")

    try:
        from config import POSTGRES_URI as config_uri
        value = str(config_uri or "").strip()
        if value:
            return PostgresTarget(uri=value, source="config.POSTGRES_URI")
    except Exception:
        pass

    return PostgresTarget(
        uri=DEFAULT_POSTGRES_URI,
        source="runner.DEFAULT_POSTGRES_URI",
    )


def postgres_uri() -> str:
    return resolve_postgres_target().uri
```

同时增加安全日志辅助方法，解析并记录：

- `source`：例如 `env:POSTGRES_URI`。
- `host`、`port`、`dbname`、`user`。
- `password_present`：布尔值。
- 脱敏 URI：密码显示为 `***`，不输出 query 中可能存在的敏感参数。

不要只用 `urlsplit().password` 判定所有部署是否可连接。PostgreSQL 也可能通过 `.pgpass`、证书或其它认证方式获得凭据；诊断字段是为了定位，不应在通用代码中强制拒绝所有无密码 DSN。当前本地 Docker 密码认证部署则应检查 URI 是否包含预期密码。

### 6.4 修改 `config.py`：空字符串不应覆盖默认值

将当前逻辑：

```python
POSTGRES_URI: str = os.environ.get(
    "POSTGRES_URI",
    "postgresql://postgres:devpass@localhost:5432/video_workflow",
)
```

改为：

```python
_DEFAULT_POSTGRES_URI = (
    "postgresql://postgres:devpass@localhost:5432/video_workflow"
)
POSTGRES_URI: str = (
    os.environ.get("POSTGRES_URI", "").strip()
    or _DEFAULT_POSTGRES_URI
)
```

这能修复 `POSTGRES_URI` 为空字符串时默认值失效的问题。不要在模块加载阶段打印 URI。

### 6.5 增加 Worker 同路径诊断脚本

建议新增：`scripts/verify_agent_gate_postgres.py`

此脚本必须复用 `agent_gate.runner.resolve_postgres_target()`，而不是自行只读 `POSTGRES_URI`。这样才能验证 `AGENT_GATE_POSTGRES_URI` 的优先级和配置回退是否与真实 Worker 一致。

脚本执行内容：

1. 解析实际 DSN 和来源。
2. 仅打印脱敏的主机、端口、数据库、用户名、密码是否存在、来源。
3. 通过 `psycopg.connect(uri, connect_timeout=5)` 连接。
4. 执行 `SELECT 1, current_database(), current_user`。
5. 失败时输出异常类型和错误信息，但绝不输出原始 URI。
6. 退出码：成功 `0`，配置或连接失败 `1`。

调用示例：

```powershell
.\.venv\Scripts\python.exe scripts\verify_agent_gate_postgres.py
```

### 6.6 MCP 配置审计

在操作员目录中找到实际被 MCode 使用的 `.mcp.json`，检查 `video-agent` server 配置。

重点确认：

- `command` 指向当前仓库的 `.venv\Scripts\python.exe`。
- `args` 中调用 `-m agent_gate.mcp_server`，而不是旧版本入口。
- `cwd` 或 MCP 启动目录能使 `agent_gate` 与仓库根 `config.py` 从当前 checkout 导入。
- 若有 `env` 字段，不要将 `POSTGRES_URI` 设为空，也不要放置无密码的旧 URI。
- 生产配置优先由 `start_production.ps1` 的父进程注入，不建议在多个配置文件中各自维护一份不同的生产 DSN。

由于该 `.mcp.json` 不在仓库中，本设计不臆造其具体内容。审计时应以本机实际文件为准。

## 7. 分阶段执行开发任务

> 本章把第 6 章的修改设计拆分为可独立执行、可独立验收、可独立回滚的开发阶段。阶段编号与第 8 章「执行步骤」保持对齐：阶段 A/B 对应该章阶段 A/B；阶段 C1–C3 细分该章阶段 C 的第 1–5 步；该章阶段 C 的第 6 步（重启 Web）移入本章阶段 E 执行；阶段 D 对应该章阶段 D，并作为进入阶段 E 的验收闸门。

### 7.1 阶段总览

| 阶段 | 名称 | 是否改代码 | 主要交付物 | 前置依赖 | 建议提交点 |
|---|---|---|---|---|---|
| A | 现场收集与基线确认 | 否 | 脱敏现场记录：容器状态、变量快照、`verify_postgres.py` 输出、`worker.log` 失败点、`.mcp.json` 审计结论 | 无 | 不提交，随文档归档 |
| B | 环境临时验证 | 否 | 「正确 DSN 可连通」的临时验证结论 | A | 不提交 |
| C1 | 启动脚本修复 | 是 | `start_production.ps1` 注入顺序修正 + `AGENT_GATE_POSTGRES_URI` 备份/清理 | B | commit ① |
| C2 | 配置解析修复 | 是 | `config.py` 空值回退；`agent_gate/runner.py` 的 `resolve_postgres_target()` 与脱敏日志 | B（与 C1 可并行开发） | commit ② |
| C3 | 诊断脚本与测试建设 | 是 | `scripts/verify_agent_gate_postgres.py`；`agent_gate/tests/` 新用例（T1–T7） | C2 | 与 commit ② 合并 |
| D | 测试与验收 | 否 | T1–T7 自动化验收记录；诊断脚本三场景输出 | C1–C3 | 不提交 |
| E | 部署重启与故障任务恢复 | 否（运维操作） | 新 Web/Worker 运行记录；T8–T10 验收回填；任务处置结论 | D | 不提交 |

### 7.2 阶段依赖关系

```mermaid
flowchart LR
    A["阶段 A 现场收集"] --> B["阶段 B 环境临时验证"]
    B --> C1["阶段 C1 启动脚本修复"]
    B --> C2["阶段 C2 配置解析修复"]
    C1 --> D["阶段 D 测试与验收"]
    C2 --> C3["阶段 C3 诊断脚本与测试"]
    C3 --> D
    D --> E["阶段 E 部署重启与任务恢复"]
```

- C1 与 C2 相互独立，可并行开发；建议按 commit ① → commit ② 顺序提交，每次提交后仓库既有测试都必须保持通过。
- 阶段 A/B/D/E 不产生代码提交，其现场与验收记录随本文档归档。

### 7.3 阶段 A：现场收集与基线确认

**目标**：不改任何代码与数据，固化修复前基线，确认 §4 各根因在本机的实际形态。

**进入条件**：无；建议在 Postgres 容器保持运行时执行。

**任务清单**：

- [ ] 按 §8 阶段 A 记录 `edb-postgres16` 容器状态与端口映射。
- [ ] 记录当前终端 `POSTGRES_URI`、`AGENT_GATE_POSTGRES_URI` 脱敏快照（host/port/db/user/password_present）。
- [ ] 运行 `.\.venv\Scripts\python.exe scripts\verify_postgres.py`，记录连接与 checkpoint 表检查结果。
- [ ] 读取 `outputs\<thread_id>\agent_gate\status.json` 与 `worker.log`，定位失败发生在 checkpointer 初始化还是更早。
- [ ] 按 §6.6 完成操作员目录 `.mcp.json` 审计（command/args/cwd/env），记录是否注入了空值或无密码 URI。

**交付物**：一份脱敏现场记录（可归档到 `docs/修正/` 或任务单）。

**验收标准**：能明确回答三个问题——数据库服务是否在运行、当前环境哪个变量携带什么形态的 DSN、故障 Worker 的 DSN 来源是否可复现。

**中止条件**：容器未运行或数据库本身不可达时，先恢复基础设施再重做本阶段；此时不得进入阶段 C。

**回滚方式**：无改动，无需回滚。

### 7.4 阶段 B：环境临时验证

**目标**：证明「正确 DSN + 干净变量环境」可连通目标库，为阶段 C 提供取值基准（对应 §8 阶段 B）。

**任务清单**：

- [ ] 在当前 PowerShell 设置 `$env:POSTGRES_URI`（取阶段 A 确认的正确 DSN），并 `Remove-Item Env:AGENT_GATE_POSTGRES_URI`。
- [ ] 重跑 `scripts\verify_postgres.py`，确认连接成功且目标库为 `video_workflow`。
- [ ] 记录该 DSN 的脱敏形态，作为后续 `start_production.ps1 -PostgresUri` 的取值依据。

**边界**：只验证当前终端进程；不重启任何服务、不改任何文件。

**回滚方式**：关闭该终端即可，无持久影响。

### 7.5 阶段 C1：启动脚本修复（commit ①）

**目标**：保证任何子进程创建之前环境已就绪（§6.1）。

**任务清单**：

- [ ] 把 `$env:POSTGRES_URI = $PostgresUri`、清除 `AGENT_GATE_POSTGRES_URI`、以及 `STORYLINE_MODE`/`THREAD_ID`/Python 编码变量的设置，移动到所有 `Start-Process`（含 Phase 0.6 的 Uvicorn）之前。
- [ ] `$envBackup` 列表补入 `AGENT_GATE_POSTGRES_URI`（当前备份列表缺该变量），保留 `finally` 恢复逻辑。
- [ ] 删除或简化 Phase 0.7 之后的重复注入块，保留脱敏日志输出。
- [ ] 人工走查：不产生第二份独立启动逻辑；「复用已运行 Web」分支（`reused=true`）保留明确提示。

**交付物**：修改后的 `start_production.ps1`。

**验收标准**：静态走查确认 Uvicorn 的 `Start-Process` 位于 `$env:POSTGRES_URI` 注入之后；脚本经 PowerShell 解析无语法错误；实际运行验证在阶段 E 一并完成。

**回滚方式**：`git checkout -- start_production.ps1`；脚本 `finally` 保证不遗留环境污染。

### 7.6 阶段 C2：配置解析修复（commit ②）

**目标**：统一 DSN 解析与空值处理，Worker 启动时输出脱敏诊断（§6.3、§6.4）。

**任务清单**：

- [ ] `config.py`：`POSTGRES_URI` 空字符串/纯空白回退默认 DSN（§6.4）。
- [ ] `agent_gate/runner.py`：新增 `PostgresTarget` 与 `resolve_postgres_target()`，`postgres_uri()` 改为薄封装，保持对外返回 `str` 的兼容性（§6.3）。
- [ ] Worker 启动路径记录 `source`、host/port/dbname/user、`password_present` 与脱敏 URI；不输出密码与 query。
- [ ] 不在通用路径强制拒绝无密码 DSN（保留 `.pgpass`/证书等认证可能）；诊断字段只作定位提示。

**交付物**：修改后的 `config.py`、`agent_gate/runner.py`。

**验收标准**：T1–T5 语义正确（见 §8 阶段 D 表）；既有调用 `postgres_uri()` 的代码点无需改动。

**回滚方式**：`git checkout -- config.py agent_gate/runner.py`。

### 7.7 阶段 C3：诊断脚本与测试建设（随 commit ②）

**目标**：让「Worker 眼中的 DSN」可被独立验证，并固化回归用例（§6.5）。

**任务清单**：

- [ ] 新增 `scripts/verify_agent_gate_postgres.py`：必须复用 `resolve_postgres_target()`（不得自行只读 `POSTGRES_URI`），实现 §6.5 的连接检查、脱敏输出与退出码 0/1。
- [ ] `agent_gate/tests/` 新增用例覆盖 T1–T7；T7（MCP→Worker 环境传播）可扩展自既有 `test_mcp.py` 的 stdio 进程测试。
- [ ] 测试一律使用专用测试 `thread_id`；需要真实库的用例通过 `AGENT_GATE_POSTGRES_URI` 指向测试库，避免污染 `video_workflow`。

**交付物**：新诊断脚本 + 新测试用例。

**验收标准**：`.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q` 全绿（默认 `testpaths` 只含 `tests`，必须显式指定 `agent_gate\tests`）；诊断脚本在「正常 / 无密码 / 空变量」三场景下输出符合 §6.5 预期。

**回滚方式**：删除新增文件即可，不影响既有功能。

### 7.8 阶段 D：测试与验收（闸门）

**目标**：完整执行 §8 阶段 D 的测试命令，形成 T1–T10 验收记录；本阶段是进入阶段 E 的唯一闸门。

**任务清单**：

- [ ] `.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q`
- [ ] `.\.venv\Scripts\python.exe -m pytest tests\integration\test_postgres_checkpointer.py -v`
- [ ] `.\.venv\Scripts\python.exe scripts\verify_agent_gate_postgres.py`
- [ ] 对照 §8 阶段 D 的 T1–T10 逐项记录：T1–T7 在本阶段闭环；T8–T9 依赖真实部署，在阶段 E 回填；T10 随阶段 E 的启动/结束观察 `finally` 恢复行为。
- [ ] 失败项先修复再重跑；不允许用 `skip`/`xfail` 让闸门变绿。

**验收标准**：T1–T7 全部通过，且 `verify_agent_gate_postgres.py` 输出来源正确（预期 `env:POSTGRES_URI` 或 `config.POSTGRES_URI`，`password_present=true`）。

**回滚方式**：按失败项回退对应阶段提交（C1/C2/C3）。

### 7.9 阶段 E：部署重启与故障任务恢复

**目标**：让修复作用于真实进程链，并按 §9 完成故障任务处置。

**任务清单**：

- [ ] 按 §6.2 读取最新 `logs\openstoryline_pids_*.json`：确认 `reused=false` 且 `web_pid` 命令行属于本仓库后，才停止旧 Web 进程；禁止用 `stop_production.ps1` 做「只重启 Web」。
- [ ] 不停止 Postgres 容器、不删除卷、不清理 checkpoint 表。
- [ ] 用修正后的 `start_production.ps1` 重启；确认 `POSTGRES_URI` 注入先于 Uvicorn 启动，启动结束时原环境变量被恢复（T10）。
- [ ] 运行 `scripts\verify_agent_gate_postgres.py`，确认 Worker 同路径 DSN 来源正确。
- [ ] 触发一次 MCode turn，确认 Worker 进入 `running`，`worker.log` 无 `no password supplied`（T8）。
- [ ] 按 §9 检查目标 `thread_id` 是否已有 checkpoint：无 → 用新 `thread_id` 重新开始；有且状态为 `crashed`/`failed` → `continue_after_crash`；`waiting_human` → 走人工 resume（T9）。

**验收标准**：§8 阶段 D 的验收通过条件全部满足；T8–T10 回填为通过。

**回滚方式**：按 §10 回滚方案执行；保留 `worker.log`、`status.json` 与事件日志作为诊断材料。

### 7.10 提交策略与记录归档

- commit ①（阶段 C1）：仅含 `start_production.ps1`，提交信息建议 `fix(startup): 提前注入 Postgres 环境变量并清理 AGENT_GATE_POSTGRES_URI`。
- commit ②（阶段 C2+C3）：`config.py`、`agent_gate/runner.py`、诊断脚本与测试，提交信息建议 `fix(agent-gate): 统一 Postgres DSN 解析与空值回退，新增同路径诊断与回归测试`。
- 阶段 A/B/D/E 的现场与验收记录，随本文档的更新一并入库；记录中不得出现真实密码或内部敏感信息。

### 7.11 总体风险与中止条件

| 风险 | 触发信号 | 处置 |
|---|---|---|
| Postgres 基础设施故障 | 容器未运行、`connection refused` | 停在阶段 A/B，先恢复容器；不得改代码"绕过" |
| 复用了旧 Web 进程 | `openstoryline_pids_*.json` 显示 `reused=true` 或 PID 归属不明 | 停在阶段 E 第 1 步，按 §6.2 人工确认所有权后再操作 |
| 测试污染生产库 | 用例使用生产 `thread_id` 或直连 `video_workflow` | 测试固定走 `AGENT_GATE_POSTGRES_URI` 指向的测试库与专用 `thread_id` |
| 破坏性数据操作 | 任何 `DROP`/`TRUNCATE`/`docker volume rm` 提议 | 一律拒绝；本计划所有阶段均无数据清理动作 |
| 闸门被绕过 | 测试被 skip/xfail，或验收未执行就重启 | 退回阶段 D 重跑；禁止带病部署 |

## 8. 执行步骤

> 本章给出各阶段的现场命令与判读要点；各阶段任务分解、交付物、验收闸门与回滚点见第 7 章「分阶段执行开发任务」。

### 阶段 A：先收集现场，不改数据

在仓库根目录运行：

```powershell
# 检查容器状态（只读）
docker ps --filter "name=^edb-postgres16$" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 检查当前 PowerShell 进程里的两个候选变量（不打印密码）
foreach ($name in @('POSTGRES_URI', 'AGENT_GATE_POSTGRES_URI')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ([string]::IsNullOrWhiteSpace($value)) {
        "${name}=<未设置或空值>"
    } else {
        try {
            $u = [System.Uri]$value
            $hasPassword = -not [string]::IsNullOrEmpty($u.UserInfo) -and $u.UserInfo.Contains(':')
            "${name}: host=$($u.Host); port=$($u.Port); path=$($u.AbsolutePath); password_present=$hasPassword"
        } catch {
            "${name}=<URI 格式异常>"
        }
    }
}

# 用当前终端 POSTGRES_URI 检查数据库和 LangGraph 表
.\.venv\Scripts\python.exe scripts\verify_postgres.py
```

说明：`verify_postgres.py` 只检查当前进程的 `POSTGRES_URI`；它**不能替代**新增的 Worker 同路径诊断脚本。两者用途不同。

### 阶段 B：修正当前 PowerShell 环境，先做临时验证

本地 Docker 配置的示例 URI（仅当你的 Postgres 账号确实是 `postgres/devpass` 时使用）：

```powershell
$env:POSTGRES_URI = 'postgresql://postgres:devpass@localhost:5432/video_workflow'
Remove-Item Env:AGENT_GATE_POSTGRES_URI -ErrorAction SilentlyContinue

.\.venv\Scripts\python.exe scripts\verify_postgres.py
```

这一步只验证当前 PowerShell 进程，不会自动修复已经运行的 Uvicorn / MCP 子进程。必须重启旧 Web 进程，才能让其后续 MCode、MCP、Worker 继承新环境。

### 阶段 C：实施代码修复

按以下顺序修改：

1. `start_production.ps1`：提前注入生产环境变量，并将 `AGENT_GATE_POSTGRES_URI` 纳入备份/恢复列表。
2. `config.py`：修复空字符串回退默认值。
3. `agent_gate/runner.py`：加入带来源信息的 DSN 解析函数，Worker 启动时记录脱敏后的目标信息。
4. 新增 `scripts/verify_agent_gate_postgres.py`。
5. `agent_gate/tests/`：增加空值优先级、覆盖变量、MCP→Worker 环境传播测试。
6. 如果 Web 服务已有进程，按 §6.2 只重启确认归本项目拥有的 Web PID。

### 阶段 D：测试和验收

必须覆盖以下用例：

| 编号 | 测试场景 | 预期结果 |
|---|---|---|
| T1 | 只设置 `POSTGRES_URI` 且含密码 | 选择 `env:POSTGRES_URI` |
| T2 | `AGENT_GATE_POSTGRES_URI` 与 `POSTGRES_URI` 同时设置 | 按既有优先级选择 `AGENT_GATE_POSTGRES_URI`；生产脚本应清除遗留覆盖值 |
| T3 | `AGENT_GATE_POSTGRES_URI` 为空，`POSTGRES_URI` 有效 | 选择 `POSTGRES_URI` |
| T4 | `POSTGRES_URI` 为空字符串 | 不返回空 DSN；使用有效配置值或默认值 |
| T5 | 两个环境变量都未设置 | 使用 `config.POSTGRES_URI` 或默认 DSN |
| T6 | URI 无密码且本地服务端要求密码 | 诊断明确显示 `password_present=false`，连接失败原因可定位 |
| T7 | MCP 进程创建 Worker | Worker 获得预期环境变量；不依赖父进程启动后的环境修改 |
| T8 | Worker 连接 Postgres 并执行 `saver.setup()` | 成功，无 `no password supplied` |
| T9 | 挂起并恢复工作流 | 相同 `thread_id` 的 checkpoint 可读、可继续 |
| T10 | 生产启动结束 | PowerShell 原有环境变量被 `finally` 正确恢复 |

建议分别执行：

```powershell
# agent_gate 测试需要显式指定目录（默认 testpaths 不包含此目录）
.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q

# PostgreSQL checkpointer 集成测试
.\.venv\Scripts\python.exe -m pytest tests\integration\test_postgres_checkpointer.py -v

# 当前 Worker 实际连接目标诊断（新增脚本）
.\.venv\Scripts\python.exe scripts\verify_agent_gate_postgres.py
```

验收通过条件：

- Worker 实际 DSN 来源正确，目标数据库为 `video_workflow`（或部署明确指定的目标库）。
- Postgres 连接成功，`PostgresSaver.setup()` 成功。
- `worker.log` 不再出现 `no password supplied`。
- Worker 状态从 `starting` 进入 `running`，之后到达 `waiting_human` 或 `completed`，而不是在 checkpointer 初始化阶段失败。
- 既有 checkpoint 表和数据未被删除或重置。

## 9. 故障任务恢复策略

修复连接配置后，不要立即对同一 `thread_id` 使用 `mode=start`，也不要假定失败任务可以从 checkpoint 继续。

`agent_gate.runner.check_mode()` 的状态约束是：

- `mode=start`：仅用于尚未使用且没有 checkpoint 的新 `thread_id`。
- `mode=continue_after_crash`：要求 Worker 状态为 `crashed` 或 `failed`，并且 PostgreSQL 中存在该 `thread_id` 的 checkpoint。
- `mode=resume`：只用于 `waiting_human` 状态，并提交当前挂起 checkpoint 的答案。

建议操作：

1. 查看 `outputs\<thread_id>\agent_gate\status.json` 和 `worker.log`。
2. 通过 PostgreSQL 查询或 Worker 状态接口确认该 `thread_id` 是否已有 checkpoint。
3. 如果 checkpoint 不存在，说明 Worker 很可能在创建/初始化 checkpointer 之前失败。为避免误用已有 thread，使用新的 `thread_id` 重新开始；如果必须复用原 ID，先按项目状态机确认并处理旧状态文件。
4. 如果 checkpoint 已存在，使用 `continue_after_crash`，不要用 `start` 覆盖或重复启动。
5. 如果状态为 `waiting_human`，走人工 resume 流程，不要用 crash continuation。

**禁止动作**：不要为了绕过数据库连接问题而切换到 `InMemorySaver`。它是进程内存储，Worker 退出后不能提供生产级 checkpoint 持久化和崩溃恢复。

## 10. 回滚方案

本次修复原则上只改配置解析、进程启动顺序、诊断和测试，不应涉及数据库结构或 checkpoint 数据迁移。

若修复后 Web 服务无法启动：

1. 恢复 `start_production.ps1`、`config.py`、`agent_gate/runner.py` 到修复前 Git 版本。
2. 恢复 PowerShell 原环境变量（启动脚本 `finally` 应负责此事）。
3. 保留 PostgreSQL 容器、命名卷和数据库数据，不执行 `docker volume rm`、`DROP DATABASE`、`TRUNCATE checkpoints`。
4. 在修复版本恢复前，不要让新旧两个 Web 服务同时占用 `7860`。
5. 保留 `outputs\<thread_id>\agent_gate\worker.log`、`status.json` 和事件日志，作为后续诊断材料。

## 11. 实施检查清单

- [ ] 核实 `start_production.ps1` 中 `POSTGRES_URI` 注入发生在 Uvicorn `Start-Process` 之前。
- [ ] 将 `AGENT_GATE_POSTGRES_URI` 加入环境变量备份/恢复，并在生产启动时清除遗留值。
- [ ] 检查操作员目录 `.mcp.json` 是否覆盖 Postgres 环境变量。
- [ ] 修复 `config.py` 对空字符串的处理。
- [ ] Worker 输出 DSN 来源和脱敏目标信息，不输出密码。
- [ ] 新增 Worker 同路径诊断脚本。
- [ ] 增加 DSN 优先级和 MCP→Worker 环境传播测试。
- [ ] 重启旧的、由本项目拥有的 OpenStoryline Web 进程。
- [ ] 验证 Worker 成功执行 `PostgresSaver.setup()`。
- [ ] 按 checkpoint 是否存在决定新建任务或从检查点继续。

## 12. 参考代码

以下链接均指向 `wayyet/auto-video-editor` 的 `main` 分支：

- [`start_production.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/start_production.ps1)：生产启动顺序、Uvicorn 启动和环境变量注入。
- [`agent_gate/runner.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/runner.py)：Worker 启动、Postgres URI 解析、同步 `PostgresSaver` 创建与恢复状态机。
- [`agent_gate/mcp_server.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/mcp_server.py)：MCP 工具入口及 `run_start` 到 `spawn_worker` 的调用。
- [`config.py`](https://github.com/wayyet/auto-video-editor/blob/main/config.py)：仓库默认 `POSTGRES_URI`。
- [`openstoryline/src/open_storyline/mcode_chat_model.py`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/src/open_storyline/mcode_chat_model.py)：MCode 子进程启动方式。
- [`scripts/verify_postgres.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/verify_postgres.py)：现有 PostgreSQL 连通性及 checkpoint 表检查脚本。
- [`scripts/start_postgres.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/start_postgres.ps1)：本地 Postgres 容器启动参数。
- [`agent_gate/tests/test_mcp.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/tests/test_mcp.py)：MCP stdio 进程测试，可扩展环境传播覆盖。

---

## 最终建议

先修复 `start_production.ps1` 的环境变量注入顺序，并清除生产进程中遗留的 `AGENT_GATE_POSTGRES_URI`；再修复 `config.py` 空字符串回退问题。随后重启已存在的 OpenStoryline Web 服务，通过 Worker 同路径诊断确认最终 DSN 来源与密码状态，最后根据 PostgreSQL 中是否已有该 `thread_id` 的 checkpoint 决定重新开始还是继续恢复。

整个执行过程按第 7 章「分阶段执行开发任务」的 A→E 阶段推进；阶段 D 的测试闸门通过后，才进行阶段 E 的部署重启与任务恢复。

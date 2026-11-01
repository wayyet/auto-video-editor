# auto-video-editor：环境变量传播链路优化执行设计

> **项目**：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> **分析分支**：`main`  
> **分析日期**：2026-10-31  
> **参考历史设计**：`auto-video-editor_Postgres_worker连接故障_执行设计.md`  
> **目标**：降低 Worker / MCP / MCode / OpenStoryline 链路的配置错误率，缩短故障发现与恢复时间，并减少不必要的 MCode 调用和重复任务。

---

## 1. 结论摘要

当前架构方向基本合理，但环境变量传播依赖多级子进程的隐式继承，配置来源不统一，存在明显的故障传播风险。

**需要优先处理的 4 个问题：**

1. **P0：OpenStoryline Web 启动顺序错误。** `start_production.ps1` 在 Web 进程启动或复用之后，才设置 `POSTGRES_URI`。新 Web 进程无法继承这次启动时才设置的数据库变量；已复用的 Web 进程更不可能自动更新环境。
2. **P0：两条 LangGraph 路径的数据库配置优先级不一致。** 主图 `graph.get_checkpointer()` 读取 `POSTGRES_URI`；`agent_gate.runner.postgres_uri()` 优先读取 `AGENT_GATE_POSTGRES_URI`。后者原本用于隔离测试库。如果测试变量进入生产 MCP / Worker 环境，主图和 Worker 可能连接不同数据库。
3. **P0：MCode 到 MCP 的环境传递尚未被仓库代码完整控制。** `MCodeChatModel` 显式创建 MCode 子进程时没有传 `env`；MCode 再根据操作员目录中的 `.mcp.json` 启动 MCP。仓库能确认 `.mcp.json` 的生成规则，但无法确认本机 MiniMax Code 对父进程环境与 MCP `env` 的最终合并行为。
4. **P1：缺少 Worker 有效数据库目标诊断。** 当前连接失败时，日志没有一条统一、脱敏、可审计的记录来说明 Worker 最终选择了哪个 DSN 来源、目标主机和数据库。

**总体建议：保留 Web、MCode、MCP、Worker 的进程隔离，不要为了减少链路长度而把 Worker 合并进 Web 或 MCP 进程。** 应改为“启动前统一配置、子进程显式传递、Worker 启动时二次校验、数据库连接快速失败、链路测试覆盖”。

本文件是执行设计，不代表已经修改 GitHub 仓库或本机配置。

## 2. 分析范围与证据边界

### 2.1 已检查的仓库文件

| 文件 | 本次检查重点 |
|---|---|
| `start_production.ps1` | 环境变量备份、OpenStoryline Web 启动/复用、环境变量注入时机 |
| `config.py` | `POSTGRES_URI` 默认值和空字符串行为 |
| `graph.py` | 主 LangGraph 的 `AsyncPostgresSaver` 连接入口 |
| `openstoryline/agent_fastapi.py` | Web 进程入口和 OpenStoryline 运行环境 |
| `openstoryline/src/open_storyline/agent.py` | MCode agent 的创建方式、操作员目录校验 |
| `openstoryline/src/open_storyline/mcode_chat_model.py` | 同步、异步、探测、流式执行时的子进程创建 |
| `openstoryline/src/open_storyline/mcode_runner.py` | 流式 MCode 子进程的环境变量传入能力 |
| `openstoryline/README.md` | 嵌入式 OpenStoryline 架构、MCode 和 `.mcp.json` 约定 |
| `agent_gate/scaffold.py` | `.mcp.json` 的生成规则 |
| `agent_gate/mcp_server.py` | MCP `run_start` 到 Worker 的调用入口 |
| `agent_gate/runner.py` | DSN 选择、Worker 环境构造、PostgresSaver 初始化、恢复状态机 |
| `db/connection.py` | 连接超时配置与 JSONB 编码处理 |
| `agent_gate/tests/test_mcp.py`、`agent_gate/tests/conftest.py` | MCP 跨进程测试、测试数据库隔离方式 |

### 2.2 无法从仓库确认的外部配置

本机 MCode 操作员目录中的 `.mcp.json` 不属于 `auto-video-editor` 仓库。项目 `openstoryline/README.md` 明确要求 MCode 从 `OPENSTORYLINE_MCODE_CWD` 对应的目录读取 `.mcp.json` 和 `AGENTS.md`。

因此，本设计不会假定本机 `.mcp.json` 当前具体设置了哪些变量，也不会假定 MiniMax Code 一定如何合并父进程环境与 MCP `env`。这两项必须在实施阶段用真实 MCode 进程验证。

---

## 3. 当前架构与链路判断

### 3.1 当前实际运行链路

用户提出的链路是：

`Worker → MCP → MCode → OpenStoryline`

这个方向可以描述**工具结果、任务状态和响应向上返回**，但不能用来描述环境变量传播。

环境变量传播方向应为：

`OpenStoryline Web → MCode → MCP Server → Worker`

完整链路如下：

```mermaid
flowchart TD
    PS[PowerShell / start_production.ps1]
    WEB[OpenStoryline Web / Uvicorn]
    MC[MCode CLI / mcode exec]
    CFG[操作员目录 .mcp.json]
    MCP[agent_gate.mcp_server / MCP stdio]
    RUN[agent_gate.runner.spawn_worker]
    WK[独立 Worker 进程]
    LG[LangGraph graph:build_graph]
    PG[(PostgreSQL / video_workflow)]

    PS -->|启动时复制环境快照| WEB
    WEB -->|子进程继承环境| MC
    MC -->|读取 MCP 配置并启动服务| CFG
    CFG --> MCP
    MCP -->|run_start / resume| RUN
    RUN -->|Popen(env=...)| WK
    WK --> LG
    LG --> PG
```

### 3.2 任务状态和结果返回方向

Worker 并不是把整个执行结果直接通过进程标准输出返回给 MCode。

当前项目采用后台 Worker + Mailbox 的模式：

- MCP `run_start` 启动 Worker 后立即返回 PID 和信箱目录。
- Worker 把状态写入 `outputs/<thread_id>/agent_gate/status.json`，日志写入 `worker.log`，事件写入事件文件。
- MCode 通过 MCP 的 `run_status`、`gate_next_tasks`、`gate_get_task`、`gate_submit_result` 等工具读取任务并提交答案。
- Worker 读取提交结果，继续执行 LangGraph；LangGraph checkpoint 写入 PostgreSQL。
- OpenStoryline 接收 MCode 最终输出并向网页会话展示。

因此，结果流可以理解为：

```mermaid
flowchart BT
    PG[(PostgreSQL checkpoint)] --> WK[Worker / LangGraph]
    WK --> MB[Mailbox / status / tasks / events]
    MB --> MCP[MCP tools]
    MCP --> MC[MCode CLI]
    MC --> WEB[OpenStoryline Web]
    WEB --> UI[浏览器会话]
```

### 3.3 另有一条独立的主工作流路径

当前仓库还存在另一条生产执行路径：

`start_production.ps1 → scripts/run_workflow.py → graph.get_checkpointer() → LangGraph`

这条路径不经过 MCode、MCP、`agent_gate` Worker。主图使用 `AsyncPostgresSaver`；Agent Gate Worker 使用同步 `PostgresSaver`。

两条路径可以共用同一个 PostgreSQL 数据库，但必须共用同一套连接配置解析规则。不能让它们因为变量优先级不同而连接不同数据库。

### 3.4 链路是否合理

| 维度 | 判断 | 说明 |
|---|---|---|
| Web 与 Worker 进程隔离 | 合理 | Worker 崩溃不应直接拖垮 Web；Worker 可独立重启 |
| MCode 作为 OpenStoryline 的 Agent / ChatModel | 合理 | 项目明确采用 `mcode exec`，工具由 MCode 自己的 MCP 配置提供 |
| MCP 作为 Worker 控制面 | 合理 | MCP 负责启动、查询状态、读取任务和提交结果 |
| Worker 独立进程运行 LangGraph | 合理 | 支持独立心跳、状态记录和 PostgreSQL checkpoint 恢复 |
| 多级环境变量隐式继承 | 不合理 | 变量是否到达 Worker 依赖每一层创建子进程的时机和环境合并规则 |
| `.mcp.json` 在仓库外独立维护 | 有风险 | 容易产生旧路径、旧 Python、空值或过期 DSN；且无法随代码审查 |
| 主图与 Worker 的 DSN 解析逻辑不同 | 不合理 | 有连接到不同数据库的可能 |

**结论：进程架构可以保留；环境配置传递方式必须改造。**

---

## 4. 当前代码问题与优先级

### 4.1 P0：`POSTGRES_URI` 注入时机过晚

`start_production.ps1` 当前执行顺序如下：

1. 备份部分环境变量。
2. Phase 0.6 / 0.7 启动或复用 OpenStoryline Web。
3. Web 健康检查通过。
4. 才执行 `$env:POSTGRES_URI = $PostgresUri`。
5. 再执行 preflight 和 `run_workflow.py`。

子进程创建时复制父进程当时的环境变量。父进程之后修改环境变量，不会反向修改已经创建的 Web 进程。

影响：

- 本次新启动的 Web 可能没有拿到脚本参数 `-PostgresUri` 对应的值。
- Web 后续启动的 MCode 可能继续继承旧值或缺省值。
- MCP / Worker 如果继承这份旧环境，Worker 会使用错误 DSN。
- 启动脚本后续打印正确 URI，不能证明 Web 子树拿到的是同一个值。

### 4.2 P0：复用健康 Web 不代表环境配置正确

当前 `start_production.ps1` 检测到 `127.0.0.1:7860` 已占用且 `GET /` 返回 HTTP 200，就会标记 `reused=true` 并复用现有进程。

健康检查只证明 HTTP 服务可响应，不证明：

- 这个进程由当前仓库启动；
- 它使用当前仓库的 `.venv` 和代码版本；
- 它启动时继承的是当前 `POSTGRES_URI`；
- 它的配置与本次 `-PostgresUri` 参数一致。

因此，单纯把环境变量设置语句前移，还不足以解决旧 Web 进程复用问题。

### 4.3 P0：主图与 Worker 的 PostgreSQL 优先级不一致

当前主图 `graph.get_checkpointer()`：

```python
uri = os.environ.get("POSTGRES_URI", POSTGRES_URI)
```

当前 Agent Gate `runner.postgres_uri()`：

```python
for env in ("AGENT_GATE_POSTGRES_URI", "POSTGRES_URI"):
    uri = os.environ.get(env, "").strip()
    if uri:
        return uri

try:
    from config import POSTGRES_URI as _cfg_uri
    return str(_cfg_uri)
except Exception:
    return DEFAULT_POSTGRES_URI
```

即：

| 路径 | 连接配置优先级 |
|---|---|
| 主图 | `POSTGRES_URI` → 导入时的 `config.POSTGRES_URI` |
| Agent Gate Worker | `AGENT_GATE_POSTGRES_URI` → `POSTGRES_URI` → `config.POSTGRES_URI` → `DEFAULT_POSTGRES_URI` |

`AGENT_GATE_POSTGRES_URI` 在 `agent_gate/tests/conftest.py` 中用于隔离测试数据库。这是合理的测试设计，但不能让它意外覆盖生产 Worker 的数据库连接。

**风险场景：**主工作流连接 `video_workflow`，而 Worker 因 `AGENT_GATE_POSTGRES_URI` 残留连接到测试库或旧库。此时可能出现“主图看不到 Worker checkpoint”“Worker 查询不到主图 checkpoint”等问题。

### 4.4 P1：空字符串可能覆盖默认 DSN

根目录 `config.py` 当前代码：

```python
POSTGRES_URI: str = os.environ.get(
    "POSTGRES_URI",
    "postgresql://postgres:devpass@localhost:5432/video_workflow",
)
```

`POSTGRES_URI` 如果存在但值为空字符串，`os.environ.get()` 返回空字符串，不会回退默认值。

Agent Gate 的 `runner.postgres_uri()` 会忽略空环境变量，但随后读取 `config.POSTGRES_URI`；如果该常量在模块导入时已经被空字符串覆盖，最终仍有可能返回空 DSN。

主图 `graph.get_checkpointer()` 也存在类似问题：环境变量存在但为空时，会得到空字符串，而不是配置常量。

### 4.5 P1：MCode 子进程没有显式传入环境

`openstoryline/src/open_storyline/mcode_chat_model.py` 中：

- 同步 `_generate()` 用 `subprocess.Popen()`；
- 异步 `_agenerate()` 用 `asyncio.create_subprocess_exec()`；
- `_probe_stream_json_support()` 也会启动子进程；
- 这些调用点都没有显式传 `env`。

当前行为是依赖 Python 子进程 API 默认继承当前进程环境。这种方式可以工作，但无法形成可测试、可审计的环境契约。

流式路径有一个可复用的基础：`openstoryline/src/open_storyline/mcode_runner.py` 的 `MCodeRunner` 已经支持 `env: Optional[Mapping[str, str]]`，并将其传给 `asyncio.create_subprocess_exec()`。目前 `MCodeChatModel._astream_stream_json()` 创建 `MCodeRunner` 时没有显式传入该参数。

### 4.6 P1：MCP 配置环境传递需要实机验证

仓库 `agent_gate/scaffold.py` 生成的 `.mcp.json` 中，默认 MCP `env` 包含：

- `PYTHONPATH`
- `AGENT_GATE_REPO_ROOT`
- `PYTHONUTF8`

额外变量由 `agent_gate init --env KEY=VALUE` 写入。

默认生成配置没有写入 `POSTGRES_URI` 或 `AGENT_GATE_POSTGRES_URI`。实际运行依赖 MCode 启动 MCP 时如何处理父进程环境，以及本机 `.mcp.json` 是否存在额外覆盖。

**仓库无法证明外部 MiniMax Code 的最终环境合并行为。**必须用真实 MCode CLI 做端到端验证；不能仅用 Python 单元测试模拟后就宣布传播链路通过。

### 4.7 P1：Worker 没有报告最终 DSN 来源

Worker 在 `runner.make_checkpointer()` 里调用 `postgres_uri()`，然后连接 PostgreSQL。当前没有统一输出以下非敏感信息：

- DSN 来源：`POSTGRES_URI`、`AGENT_GATE_POSTGRES_URI` 还是默认值；
- host / port / database / username；
- 是否存在 URI 密码字段；
- 当前运行环境是 production / test / development。

因此，只看到 `no password supplied` 时，无法直接判断是父进程没传、MCP 覆盖、环境变量空值还是 URI 本身缺少密码。

### 4.8 P1：Worker 连接缺少明确超时

`db/connection.py` 已定义 `DEFAULT_CONNECT_TIMEOUT_S = 10`，并通过 `POSTGRES_CONNECT_TIMEOUT` 支持覆盖。

但 `agent_gate.runner.make_checkpointer()` 使用 `Connection.connect(uri, ...)`，目前没有传 `connect_timeout`，没有复用 `db.connection.connect()` 的超时封装。

数据库主机不可达时，Worker 可能比预期更久才失败。建议统一超时策略，避免故障时任务长时间停在 `starting`。

### 4.9 P1：数据库不可达可能被误判为没有 checkpoint

`runner.checkpoint_exists()` 当前捕获所有异常并返回 `False`。数据库暂时不可达时，调用方可能收到 `no_checkpoint`，而不是 `database_unavailable`。

当前 fail-closed 行为可以防止盲目续跑，但错误语义不准确，增加人工排查时间。建议改为三态结果：有 checkpoint、无 checkpoint、查询失败。

---

## 5. 推荐目标架构

### 5.1 设计原则

1. **单一连接配置解析器**：主图和 Agent Gate Worker 使用同一个 DSN 解析函数。
2. **先配置、再启动子进程**：Web、MCode、MCP 和 Worker 创建之前，数据库配置必须已经完成校验。
3. **显式传递环境快照**：每一层进程启动时传入明确的环境字典，而不是依赖默认继承行为。
4. **生产与测试配置隔离**：生产运行时不允许 `AGENT_GATE_POSTGRES_URI` 覆盖生产库。
5. **Worker 自检**：Worker 启动后重新解析实际 DSN，并写入脱敏诊断信息。
6. **失败快速、恢复有状态**：数据库连接设置有限超时；恢复逻辑继续遵守 checkpoint 状态机。
7. **不破坏进程隔离**：Web 不直接执行长时间 LangGraph 任务，MCP 不在本进程运行完整视频工作流。

### 5.2 目标链路

```mermaid
flowchart TD
    A[PowerShell bootstrap]
    B[Postgres config resolver]
    C[Validate DSN / DB target]
    D[OpenStoryline Web]
    E[MCodeChatModel]
    F[MCode CLI]
    G[.mcp.json / MCP bootstrap]
    H[agent_gate MCP]
    I[Worker launcher]
    J[Worker preflight]
    K[LangGraph]
    L[(PostgreSQL)]

    A --> B --> C
    C -->|配置快照| D
    D -->|显式 env| E
    E -->|显式 env| F
    F --> G --> H
    H -->|校验并构造 env| I
    I --> J --> K --> L
```

主图路径也从同一个解析器获得 DSN：

```mermaid
flowchart LR
    A[start_production.ps1]
    B[db.postgres_config.resolve_postgres_target]
    C[scripts/run_workflow.py]
    D[graph.get_checkpointer]
    E[AsyncPostgresSaver]
    F[(同一个 PostgreSQL 数据库)]
    A --> B --> C --> D --> E --> F
    B -->|同一解析规则| G[agent_gate.runner]
    G --> H[PostgresSaver]
    H --> F
```

### 5.3 数据库变量优先级

建议明确区分环境用途：

| 环境 | 允许使用的连接变量 | 行为 |
|---|---|---|
| production | `POSTGRES_URI` | 唯一生产 DSN；拒绝 `AGENT_GATE_POSTGRES_URI` |
| test | `AGENT_GATE_POSTGRES_URI` | 允许测试库覆盖，不污染生产库 |
| development | `POSTGRES_URI`；必要时允许显式测试覆盖 | 优先使用显式配置，空值视为未设置 |

生产启动脚本新增并设置：

```powershell
$env:APP_ENV = 'production'
$env:POSTGRES_URI = $PostgresUri
$env:AGENT_GATE_CKPT_BACKEND = 'postgres'
Remove-Item Env:AGENT_GATE_POSTGRES_URI -ErrorAction SilentlyContinue
```

`APP_ENV` 是本设计建议新增的统一环境标识。不要把 `WORKFLOW_ENV` 和 `APP_ENV` 混用：当前 `WORKFLOW_ENV` 在项目文档中用于选择执行路径，不建议改变其既有语义。

测试环境则显式设置 `APP_ENV=test`，由 `agent_gate/tests/conftest.py` 设置唯一测试库 URI。

---

## 6. 代码修改设计

### 6.1 新增统一 DSN 解析模块

**建议文件：**`db/postgres_config.py`

该模块不依赖 `graph.py`、`agent_gate.runner` 或根目录 `config.py`，避免循环导入。建议对外提供：

- `PostgresTarget`：不可变的数据结构；
- `resolve_postgres_target()`：解析有效 URI 和来源；
- `describe_postgres_target()`：只返回脱敏元数据；
- `make_postgres_conninfo()`：合并 `connect_timeout` 等参数。

建议接口示例：

```python
from dataclasses import dataclass
import os
from urllib.parse import urlsplit

DEFAULT_POSTGRES_URI = (
    "postgresql://postgres:devpass@localhost:5432/video_workflow"
)

@dataclass(frozen=True)
class PostgresTarget:
    uri: str
    source: str
    host: str
    port: int
    database: str
    username: str
    password_present: bool


def _non_empty_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def resolve_postgres_target() -> PostgresTarget:
    app_env = _non_empty_env("APP_ENV") or "development"
    agent_gate_uri = _non_empty_env("AGENT_GATE_POSTGRES_URI")

    if app_env == "production" and agent_gate_uri:
        raise RuntimeError(
            "生产环境禁止设置 AGENT_GATE_POSTGRES_URI；"
            "请使用统一的 POSTGRES_URI。"
        )

    if app_env == "test" and agent_gate_uri:
        uri, source = agent_gate_uri, "env:AGENT_GATE_POSTGRES_URI"
    else:
        uri = _non_empty_env("POSTGRES_URI")
        source = "env:POSTGRES_URI"

    if not uri:
        uri, source = DEFAULT_POSTGRES_URI, "default"

    parsed = urlsplit(uri)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError("POSTGRES_URI scheme 必须是 postgres 或 postgresql")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("POSTGRES_URI 必须包含 host 和 database")

    return PostgresTarget(
        uri=uri,
        source=source,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        username=parsed.username or "",
        password_present=parsed.password is not None,
    )
```

注意：这是设计接口示例，不是可直接覆盖文件的完整实现。正式实现还需处理 URI 百分号编码、IPv6、query 参数、错误脱敏和连接选项合并。

**密码认证策略：**`password_present=false` 应记录为诊断信息，不应对所有部署一概拒绝。PostgreSQL 也可能使用 `.pgpass`、证书或其他认证机制。当前本机使用密码认证时，应额外验证实际连接成功。

### 6.2 修改根 `config.py`

将当前 `os.environ.get("POSTGRES_URI", default)` 改为统一解析器提供的兼容值：

```python
from db.postgres_config import resolve_postgres_target

POSTGRES_URI: str = resolve_postgres_target().uri
```

同时注意：`POSTGRES_URI` 是模块导入时的常量快照。运行时连接入口应调用 `resolve_postgres_target()`，不要只依赖已导入的常量。

### 6.3 修改 `graph.py`

`get_checkpointer()` 当前从 `os.environ.get("POSTGRES_URI", POSTGRES_URI)` 获取连接串。修改为：

```python
from db.postgres_config import resolve_postgres_target

@contextlib.asynccontextmanager
async def get_checkpointer():
    target = resolve_postgres_target()
    uri = target.uri
    # 保留当前 configure_json_dumps()
    # 保留 AsyncPostgresSaver.from_conn_string(uri)
    # 保留 await saver.setup()
    ...
```

这样主图不再自行解析 DSN。

### 6.4 修改 `agent_gate/runner.py`

将 `postgres_uri()` 改为调用统一解析器：

```python
from db.postgres_config import resolve_postgres_target


def postgres_uri() -> str:
    return resolve_postgres_target().uri
```

在 `make_checkpointer()` 启动连接前写入脱敏诊断：

```python
target = resolve_postgres_target()
logger.info(
    "worker postgres target: source=%s host=%s port=%s db=%s user=%s password_present=%s",
    target.source,
    target.host,
    target.port,
    target.database,
    target.username,
    target.password_present,
)
```

`logger` 需按当前项目日志标准引入；严禁打印 `target.uri` 原文。

连接时设置有限超时。推荐复用 `db/connection.py` 的连接策略，或使用 `psycopg.conninfo.make_conninfo()` 将 `connect_timeout` 合并到 URI，不能直接拼接字符串：

```python
from psycopg.conninfo import make_conninfo

conninfo = make_conninfo(target.uri, connect_timeout=10)
conn = Connection.connect(
    conninfo,
    autocommit=True,
    prepare_threshold=0,
    row_factory=dict_row,
    client_encoding="utf-8",
)
```

生产环境在 Worker 启动时必须拒绝 `AGENT_GATE_POSTGRES_URI`，而不是默默使用它。测试环境继续允许该变量隔离数据库。

### 6.5 修正 `checkpoint_exists()` 的错误语义

建议把返回值从 `bool` 改为三态，或使用显式结果类型：

| 结果 | 含义 | 恢复动作 |
|---|---|---|
| `True` | 数据库可连，存在 checkpoint | 允许继续检查恢复状态 |
| `False` | 数据库可连，不存在 checkpoint | 拒绝 crash continuation，提示新建 thread_id |
| `None` / `DatabaseUnavailable` | 查询本身失败 | 返回 `database_unavailable`，不误报 `no_checkpoint` |

仍然保持 fail-closed：数据库不可达时禁止盲目重跑。

### 6.6 修改 `start_production.ps1`

这是第一优先修改项。

**A. 扩充环境变量备份列表**

在现有 `$envBackup` 列表中增加：

```powershell
'AGENT_GATE_POSTGRES_URI',
'AGENT_GATE_CKPT_BACKEND',
'APP_ENV',
```

如后续增加 `AGENT_GATE_ALLOW_DB_OVERRIDE` 等变量，也必须纳入备份与恢复。

**B. 将生产环境注入移动到所有 Web / MCode 子进程启动之前**

放置位置：现有环境备份完成后、Phase 0.6 / 0.7 的 Web `Start-Process` 之前。

```powershell
$env:APP_ENV = 'production'
$env:POSTGRES_URI = $PostgresUri
$env:AGENT_GATE_CKPT_BACKEND = 'postgres'
Remove-Item Env:AGENT_GATE_POSTGRES_URI -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:STORYLINE_MODE = $StorylineMode
$env:THREAD_ID = $ThreadId
```

原来 Web 启动后的重复注入块应删除或改成只打印脱敏状态，避免维护两份配置逻辑。外部 Postgres 模式下 `AUTO_VIDEO_EDITOR_PREFLIGHT_SKIP_POSTGRES_START` 的设置仍保留，但也要在调用相关子进程前完成。

**C. 处理复用 Web 服务的配置指纹**

不能再仅凭 HTTP 200 就认为现有服务可复用。建议分两步实施：

1. **第一阶段安全策略：**本次指定的 `PostgresUri` 或运行模式与现有 Web 的配置无法证明一致时，不复用；只允许重启 PID 文件明确标记为本项目所有的进程。未知进程继续 fail-closed，不自动杀进程。
2. **第二阶段优化：**增加仅监听 `127.0.0.1` 的运行时诊断端点，返回 `APP_ENV`、数据库 host/port/database 的非秘密配置指纹和应用版本。启动脚本比较期望指纹与当前进程指纹；不一致时仅对本项目拥有的进程执行受控重启。

不要在健康端点中返回完整 DSN、用户名密码或其他秘密。

### 6.7 修改 `MCodeChatModel`：显式传递环境快照

`MCodeChatModel` 应在构造时接收或生成一个受控的子进程环境快照，例如 `process_env`。不要在每个 turn 中随意重新读取一份可能发生变化的全局环境。

需要覆盖以下创建点：

| 入口 | 当前调用 | 修改要求 |
|---|---|---|
| 同步 `_generate()` | `subprocess.Popen()` | 增加 `env=process_env` |
| 异步 `_agenerate()` | `asyncio.create_subprocess_exec()` | 增加 `env=process_env` |
| `_probe_stream_json_support()` | `asyncio.create_subprocess_exec()` | 使用同一 `process_env` |
| `_astream_stream_json()` | `MCodeRunner(...)` | 增加 `env=process_env` |

`MCodeRunner` 已支持 `env`，不需要重新发明一套流式启动机制。

环境快照至少保证 Windows 下可执行程序需要的 `PATH`、`SYSTEMROOT`、`TEMP`、`TMP`、`USERPROFILE` 等基础变量存在。生产配置必须在 Web 启动前已确定。

**安全注意：**如果 `POSTGRES_URI` 原样传给 MCode，MCode 的命令行工具可能通过 Shell 或其他内置能力读取该环境变量。项目 README 已明确提醒 `--permission` 不是安全边界。建议先完成传播可靠性修复，再实施凭证隔离：由 MCP 启动包装器从 Windows Credential Manager / DPAPI 或 ACL 保护的本地秘密文件读取 DSN，避免把数据库密码暴露给 MCode Agent 本身。

### 6.8 审计 `agent_gate/scaffold.py` 和本机 `.mcp.json`

仓库自动生成的 MCP 配置应保留这些基础变量：

- `PYTHONPATH`
- `AGENT_GATE_REPO_ROOT`
- `PYTHONUTF8`

同时保证：

- `command` 指向主仓库 `.venv\Scripts\python.exe` 的绝对路径；
- `args` 使用 `-m agent_gate.mcp_server`；
- `AGENT_GATE_REPO_ROOT` 指向当前 checkout 根目录；
- MCP 输出目录使用绝对路径；
- 不把 `POSTGRES_URI`、密码或 token 写入 Git 仓库；
- 生产环境不得额外设置 `AGENT_GATE_POSTGRES_URI`。

本机操作员目录审计步骤：

1. 从 `openstoryline/config.toml.local` 读取 `[mcode].cwd`，或核对 `OPENSTORYLINE_MCODE_CWD`。
2. 进入该目录，检查 `.mcp.json` 中 `video-agent` 的 `command`、`args`、`env`。
3. 核对 `command` 是否是当前项目主 `.venv` 的 Python，而不是 OpenStoryline venv 或旧 checkout 的 Python。
4. 检查 `env` 是否覆盖 `POSTGRES_URI` / `AGENT_GATE_POSTGRES_URI`。只输出变量是否存在和脱敏后的目标信息，不输出原文。
5. 使用真实 MCode CLI 启动 MCP，并在 MCP 进程和 Worker 进程中分别执行环境指纹诊断。

不要直接把完整数据库 URI 加进 `agent_gate init --env POSTGRES_URI=...`，因为 `scaffold.py` 会把额外环境值写入 `.mcp.json` 文件。

---

## 7. 诊断与可观测性设计

### 7.1 统一诊断字段

每个 Worker 启动时只记录以下字段：

| 字段 | 示例 | 是否允许写日志 |
|---|---|---|
| `app_env` | `production` | 允许 |
| `config_source` | `env:POSTGRES_URI` | 允许 |
| `db_host` | `127.0.0.1` | 允许 |
| `db_port` | `5432` | 允许 |
| `db_name` | `video_workflow` | 允许 |
| `db_user` | `postgres` | 按本机日志权限策略允许 |
| `password_present` | `true` | 允许 |
| `thread_id` | `video-001` | 允许 |
| `worker_pid` | `8288` | 允许 |
| `config_fingerprint` | `sha256:...` | 允许，但不能由原始密码直接暴露可逆信息 |
| 完整 `POSTGRES_URI` | 含用户名、密码、host、query | **禁止** |

建议将诊断记录写入 `worker.log` 开头，并将非秘密字段写入 `status.json` 或 `events.jsonl`。这样 `run_status` 或故障包可以直接定位 Worker 使用的数据库配置来源。

### 7.2 新增诊断脚本

建议新增：`scripts/verify_agent_gate_postgres.py`

脚本必须调用 `db.postgres_config.resolve_postgres_target()`，保证它与 Worker 使用同一解析逻辑。

执行内容：

1. 解析 DSN 和来源。
2. 打印脱敏的 host、port、database、username、password_present、source。
3. 使用 `connect_timeout=5` 建立连接。
4. 执行：

```sql
SELECT 1, current_database(), current_user,
       inet_server_addr(), inet_server_port();
```

5. 连接成功返回退出码 `0`；配置错误或连接失败返回退出码 `1`。
6. 异常输出必须脱敏，不打印 URI 原文。

调用：

```powershell
.\.venv\Scripts\python.exe scripts\verify_agent_gate_postgres.py
```

这个脚本只验证当前调用环境。最终验收仍需由真实 MCode → MCP → Worker 路径生成一条 Worker 诊断日志。

### 7.3 建议的故障分类

| 错误 | 分类 | 处理 |
|---|---|---|
| `no password supplied` | DSN 未提供密码或实际认证来源未生效 | 核对 Worker `config_source`、password_present 和 MCP env |
| `password authentication failed` | 密码错误或账号认证失败 | 核对凭证来源；不要盲目更换数据库角色 |
| `connection refused` | 主机可达但端口没有服务监听 | 检查容器状态、端口映射和服务监听 |
| `timeout expired` | 网络不可达或数据库响应超时 | 快速失败；区分连接超时与 SQL 执行超时 |
| `database does not exist` | host 可连接，但目标库不存在 | 核对 database 名称；不自动删除或重建数据库 |
| `database_unavailable` | checkpoint 查询连接失败 | 禁止 crash continuation，提示先恢复数据库连接 |
| `no_checkpoint` | 数据库可访问但确实没有该 thread checkpoint | 使用新 thread_id 开始，不伪造恢复点 |

---

## 8. 执行计划

### 阶段 A：现场审计（只读，不改数据）

在仓库根目录执行：

```powershell
# 1. 查看 Postgres 容器状态

docker ps --filter "name=^edb-postgres16$" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 2. 查看当前 PowerShell 进程中的变量是否存在；不显示密码
foreach ($name in @(
    'APP_ENV',
    'POSTGRES_URI',
    'AGENT_GATE_POSTGRES_URI',
    'AGENT_GATE_CKPT_BACKEND',
    'OPENSTORYLINE_MCODE_CWD'
)) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ([string]::IsNullOrWhiteSpace($value)) {
        "${name}=<未设置或空值>"
    } else {
        "${name}=<已设置>"
    }
}

# 3. 查看最新 OpenStoryline PID 记录
$pidFile = Get-ChildItem .\logs\openstoryline_pids_*.json -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($pidFile) {
    Get-Content $pidFile.FullName -Raw | ConvertFrom-Json |
        Select-Object timestamp, reused, os_dir, web_pid, web_cmd_pid, web_log
}

# 4. 查看 MCode 操作员目录（路径来自当前配置）
# 核对该目录中的 .mcp.json，不要把含凭证的完整文件内容粘贴进日志或工单。
```

这一阶段确认：当前 Web 是否复用、实际 MCode 操作员目录、MCP server 的 Python 路径、是否存在 `AGENT_GATE_POSTGRES_URI` 覆盖。

### 阶段 B：统一配置层

修改顺序：

1. 新增 `db/postgres_config.py`。
2. 根 `config.py` 改为使用统一解析器，并修复空字符串回退。
3. `graph.py` 的 `get_checkpointer()` 改用统一解析器。
4. `agent_gate/runner.py` 的 `postgres_uri()` 改用统一解析器。
5. 生产模式拒绝 `AGENT_GATE_POSTGRES_URI`。
6. Worker 增加脱敏 DSN 诊断和连接超时。
7. `checkpoint_exists()` 区分数据库不可达和 checkpoint 不存在。

### 阶段 C：修正进程启动顺序

1. `start_production.ps1` 备份完整变量集，包括 `AGENT_GATE_POSTGRES_URI`、`APP_ENV`、`AGENT_GATE_CKPT_BACKEND`。
2. 在任何 Web / MCode / MCP 子进程创建之前设置 `POSTGRES_URI` 和生产标记。
3. 清除生产进程的测试专用 `AGENT_GATE_POSTGRES_URI`。
4. 处理已有 Web：先判断 PID 所有权，再决定重启；未知进程不自动终止。
5. 删除原有启动 Web 后的重复环境注入逻辑。
6. 保留 `finally` 对所有被修改环境变量的恢复。

### 阶段 D：显式传递 MCode / MCP 环境

1. 为 `MCodeChatModel` 增加 `process_env` 快照。
2. 同步、异步、能力探测三种子进程路径都传 `env=process_env`。
3. 流式路径给 `MCodeRunner(..., env=process_env)`。
4. 检查 `agent_gate/scaffold.py` 生成的 MCP 配置。
5. 核对本机真实 `.mcp.json`，确认 Python、cwd、repo root、outputs root 都是当前项目路径。
6. 用真实 MiniMax Code 执行 MCP 启动测试，验证 MCP 能获得预期数据库配置来源。
7. 如 MCode 的 MCP 子进程不会继承所需变量，增加本机 MCP bootstrap wrapper；由 wrapper 从受保护凭证源注入 DSN，不能把明文密码写进 `.mcp.json`。

### 阶段 E：回归测试与真实数据库验收

1. DSN 解析器单元测试全部通过。
2. 主图和 Worker 对同一组环境变量解析出同一个数据库目标。
3. MCode 各种执行模式均使用相同的 env 快照。
4. MCP 进程到 Worker 的环境传递在 Windows 实机通过。
5. Worker 成功连接 PostgreSQL 并完成 `PostgresSaver.setup()`。
6. 主图通过 `AsyncPostgresSaver.setup()`。
7. 使用同一个 `thread_id` 验证 checkpoint 读写、interrupt、resume 和 crash continuation。
8. 检查所有日志没有 DSN 明文、密码或其他凭证。

---

## 9. 测试矩阵与验收条件

### 9.1 DSN 解析测试

| 编号 | 场景 | 预期结果 |
|---|---|---|
| D1 | production + 有效 `POSTGRES_URI` | 使用 `POSTGRES_URI` |
| D2 | production + 设置 `AGENT_GATE_POSTGRES_URI` | 明确报配置错误，不允许覆盖 |
| D3 | test + 两个 URI 都设置 | 使用测试专用 `AGENT_GATE_POSTGRES_URI` |
| D4 | `POSTGRES_URI` 为空字符串或纯空格 | 按环境回退有效默认值或报明确配置错误，不返回空 DSN |
| D5 | 两个变量都未设置 | 使用明确配置的默认 DSN |
| D6 | URI 缺 host / database / scheme 错误 | 启动前报配置错误 |
| D7 | URI 包含百分号编码密码 | 正确解析，日志不泄露密码 |
| D8 | DSN 不包含密码但使用其他认证方式 | 不因 `password_present=false` 直接拒绝；以实际连接结果为准 |

### 9.2 进程传播测试

| 编号 | 场景 | 预期结果 |
|---|---|---|
| P1 | PowerShell 注入发生在 Web `Start-Process` 之前 | 新 Web 获得预期配置 |
| P2 | Web 启动后父进程修改变量 | 已运行 Web 不发生环境变化；测试确认快照语义 |
| P3 | Web 创建 MCode 子进程 | MCode 使用明确传入的 env |
| P4 | MCode 启动 MCP stdio server | MCP 使用约定的 Python、cwd 和配置来源 |
| P5 | MCP 执行 `run_start` 创建 Worker | Worker 获得预期变量，且生产无 `AGENT_GATE_POSTGRES_URI` |
| P6 | Worker 重启 | 新 Worker 再次验证 DSN，不能沿用过期诊断结果 |
| P7 | 复用旧 Web | 配置指纹一致才允许复用；否则按 PID 所有权受控重启或 fail-closed |
| P8 | PowerShell 脚本结束 | 原始环境变量全部恢复，包括 `AGENT_GATE_POSTGRES_URI` |

### 9.3 PostgreSQL 与恢复测试

| 编号 | 场景 | 预期结果 |
|---|---|---|
| G1 | Agent Gate Worker 连接数据库 | `PostgresSaver.setup()` 成功 |
| G2 | 主工作流连接数据库 | `AsyncPostgresSaver.setup()` 成功 |
| G3 | 两条路径解析连接目标 | host / port / database / username 一致 |
| G4 | Postgres 暂时不可达 | 在连接超时后失败，状态明确为数据库不可达 |
| G5 | checkpoint 存在且 Worker crashed | `continue_after_crash` 恢复原 thread |
| G6 | checkpoint 不存在 | 拒绝 crash continuation，提示新 thread_id |
| G7 | Worker 状态为 `waiting_human` | 只能走 `resume`，不能用 crash continuation |
| G8 | 生产环境测试库变量泄漏 | Worker 在连接前拒绝启动 |

### 9.4 推荐测试命令

在仓库根目录运行，使用项目自己的虚拟环境：

```powershell
# 主仓单元与集成测试
.\.venv\Scripts\python.exe -m pytest tests -q

# Agent Gate 跨进程 / MCP 测试（需要本地 PostgreSQL）
.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q -p no:cacheprovider

# Postgres checkpointer 集成测试
.\.venv\Scripts\python.exe -m pytest tests\integration\test_postgres_checkpointer.py -v

# OpenStoryline 自己的测试，必须使用它自己的 venv
.\openstoryline\.venv\Scripts\python.exe -m pytest openstoryline\tests -q

# Worker 同路径数据库诊断（新增脚本）
.\.venv\Scripts\python.exe scripts\verify_agent_gate_postgres.py
```

注意：`agent_gate/tests/conftest.py` 会创建带随机后缀的独立测试数据库，并在测试结束后清理它。生产启动不能继承测试进程的 `AGENT_GATE_POSTGRES_URI`。

### 9.5 最终验收标准

- 主图和 Agent Gate Worker 指向同一个预期 PostgreSQL 目标。
- Worker 日志能显示 DSN 来源和脱敏目标信息。
- `no password supplied` 不再由环境变量传递错误引起。
- Postgres 不可达时，Worker 在有限时间内失败，状态可识别。
- 新启动和恢复的 Worker 都使用经过验证的连接配置。
- 同一个 `thread_id` 的 checkpoint 可以被主图 / Worker 预期路径正确读取。
- Web 复用逻辑不会把旧环境的健康服务误认为新配置服务。
- 不出现数据库密码、URI query 凭证或 API token 明文日志。

---

## 10. 执行速度优化

本次性能目标不是单纯减少进程数量，而是减少无效重试、重复 Agent 调用、无意义等待和重复数据库连接错误。

### 10.1 优先级最高：一次解析、全链路复用

- 在启动阶段解析并验证 DSN 一次。
- Web 启动时固定配置快照。
- MCode、MCP、Worker 使用同一套变量契约。
- Worker 启动时只做轻量二次验证，不再猜测 DSN 来源。

效果：减少由错库、空 DSN、旧进程环境导致的失败和重新执行。

### 10.2 MCP 调用批量化

项目现有 MCP 工具已经支持：

- `gate_next_tasks(thread_id, wait_s, limit)` 批量取任务；
- `gate_submit_batch(thread_id, results_file)` 批量提交结果；
- `MAX_IMAGES = 12` 限制单次返回图片数量。

建议默认按任务类型调整 batch 大小：

- 轻量文本任务：一次取 3–5 个；
- 需要图像判断的任务：按图片总量限制批次，避免一次响应过大；
- 对长耗时任务使用合理 `wait_s`，避免频繁短轮询。

不要在没有测试服务端负载和响应大小之前，直接把 batch 调到最大。

### 10.3 控制 MCode 输入成本

当前 `MCodeChatModel` 默认保留 `history_turns=10`，`max_prompt_chars=60000`。很多 Worker 任务有明确的 task prompt，不需要每次重复传入全部历史上下文。

建议分档：

| 场景 | 建议 |
|---|---|
| 简单任务问答、JSON 结构化输出 | `mcode-chat` / lightweight；历史轮次 2–4 |
| 需要连续工具调用的执行任务 | `mcode-operator` / standard；历史轮次 4–6 |
| 需要跨文件分析的复杂任务 | standard；保留必要上下文，不盲目压缩 |

项目 README 记录过本机一次测量：standard 输入约 19.5K token，lightweight 约 857 token。该数据只能说明两种模式在该环境下的成本差异，不能当作所有任务的固定性能比例。

建议增加 prompt 长度、输入 token、MCode 执行耗时、工具调用次数指标，再按真实任务调整，不要直接降低所有任务的上下文预算。

### 10.4 防止双消费者重复工作

`openstoryline/README.md` 已指出：网页端 MCode 与命令行 `agent_gate drive` 是两个平级消费者，目前没有原子认领机制。

这会造成：

- 同一个 task 被两个 Agent 同时看到；
- 重复调用模型，浪费 token 和时间；
- 两边都提交答案时出现冲突。

短期：同一个 `thread_id` 只允许一个 Agent 消费者工作。  
中期：在 Mailbox 增加原子 task lease / claim 状态，带 `owner_id`、`lease_until` 和提交幂等键。领取任务时用原子文件操作或数据库事务，不能只依靠 prompt 要求 Agent 不重复。

### 10.5 不建议的“提速”方式

- 不要把 Worker 合并进 OpenStoryline Web：会增加 Web 请求阻塞和进程崩溃耦合。
- 不要去掉 Worker 独立进程：会削弱现有心跳与 crash recovery 能力。
- 不要在生产切换 `InMemorySaver`：Worker 退出后 checkpoint 不持久化。
- 不要为了省一次 `setup()` 就删除 `PostgresSaver.setup()`，除非已证明 schema 初始化由受控迁移单独负责且版本检查完整。
- 不要在连接失败时自动无限重试；使用有限重试、指数退避和明确超时。
- 不要把所有 MCP 任务并行执行；先确认节点对同一草稿文件、素材和任务结果不存在写冲突。

---

## 11. 回滚方案

本次变更不应修改 PostgreSQL schema、删除 checkpoint 表或重建数据库卷。

如修复后 Web / MCode / MCP 无法启动：

1. 回滚 `start_production.ps1`、`config.py`、`graph.py`、`agent_gate/runner.py` 和 MCode 相关文件到修改前 Git 版本。
2. 保留 `db/postgres_config.py`，仅在代码引用完整时保留；否则一并回滚。
3. 确认没有新旧 Web 服务同时占用 `127.0.0.1:7860`。
4. 只停止 PID 文件明确归本项目所有的 Web 进程；不杀未知进程。
5. 不执行 `stop_production.ps1` 来做“只重启 Web”的操作，因为它还会处理 PostgreSQL 容器。
6. 不执行 `docker volume rm`、`DROP DATABASE`、`TRUNCATE checkpoints` 或 `stop_postgres.ps1 -Remove`。
7. 保留 `outputs/<thread_id>/agent_gate/status.json`、`worker.log`、事件日志和已有 checkpoint。
8. 恢复旧配置后，按 checkpoint 是否存在和 Worker 状态决定新建 thread、`continue_after_crash` 或 `resume`。

### 11.1 任务恢复规则

| 当前状态 | checkpoint | 正确动作 |
|---|---|---|
| `not_started` | 不存在 | `mode=start`，使用新 thread_id |
| `crashed` / `failed` | 存在 | `mode=continue_after_crash` |
| `crashed` / `failed` | 不存在 | 不允许伪恢复；检查状态后使用新 thread_id |
| `waiting_human` | 存在 | `mode=resume`，提供对应 checkpoint 值 |
| `starting` / `running` | 任意 | 不重复启动；先检查进程和心跳 |

---

## 12. 推荐实施顺序

| 顺序 | 修改内容 | 优先级 | 完成判定 |
|---|---|---|---|
| 1 | `start_production.ps1` 在 Web 启动前注入生产环境；清除测试覆盖变量 | P0 | 新 Web 子进程获得正确 env |
| 2 | 主图和 Worker 改用同一 DSN 解析器 | P0 | 两条路径连接目标一致 |
| 3 | 生产拒绝 `AGENT_GATE_POSTGRES_URI` | P0 | 测试库变量不能污染生产 |
| 4 | 处理已运行 Web 的复用与配置指纹 | P0 | 不再复用旧配置服务 |
| 5 | MCode 各子进程入口显式传 env；MCP 实机验证 | P1 | sync / async / probe / stream 全覆盖 |
| 6 | Worker 脱敏诊断与连接超时 | P1 | 可以快速定位 DSN 和连接故障 |
| 7 | `checkpoint_exists` 改为三态错误结果 | P1 | DB 不可达不再误报 no_checkpoint |
| 8 | 批量 MCP 任务、Prompt 历史压缩、单消费者约束 | P2 | 减少调用次数与重复计算 |
| 9 | task lease / 原子认领机制 | P2 | 两个消费者不会重复领取相同任务 |

**推荐先完成 1–6，再做性能调优。** 当前最明显的耗时来源是配置错误后反复启动、排查和重跑，不是进程数量本身。先降低失败率，比单纯缩短启动时间更有效。

---

## 13. 相关源码

以下链接指向 `main` 分支：

- [`start_production.ps1`](https://github.com/wayyet/auto-video-editor/blob/main/start_production.ps1)
- [`config.py`](https://github.com/wayyet/auto-video-editor/blob/main/config.py)
- [`graph.py`](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)
- [`agent_gate/runner.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/runner.py)
- [`agent_gate/mcp_server.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/mcp_server.py)
- [`agent_gate/scaffold.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/scaffold.py)
- [`agent_gate/tests/conftest.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/tests/conftest.py)
- [`agent_gate/tests/test_mcp.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/tests/test_mcp.py)
- [`db/connection.py`](https://github.com/wayyet/auto-video-editor/blob/main/db/connection.py)
- [`openstoryline/README.md`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/README.md)
- [`openstoryline/src/open_storyline/agent.py`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/src/open_storyline/agent.py)
- [`openstoryline/src/open_storyline/mcode_chat_model.py`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/src/open_storyline/mcode_chat_model.py)
- [`openstoryline/src/open_storyline/mcode_runner.py`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/src/open_storyline/mcode_runner.py)

---

## 最终结论

`Worker → MCP → MCode → OpenStoryline` 可以理解为状态与结果向上返回；环境变量必须按反方向，从启动脚本和 OpenStoryline Web 开始向 MCode、MCP、Worker 传递。

当前设计最大的风险不是某一个变量写错，而是**多级子进程继承、外部 `.mcp.json` 配置、主图与 Worker 的 DSN 优先级不一致、旧 Web 复用**四个因素叠加。

建议保留现有进程隔离和 PostgreSQL 持久化架构，先统一 DSN 解析器与启动时序，再显式传递环境快照、增加 Worker 诊断和真实 MCode 端到端测试。之后再通过 MCP 批处理、Prompt 历史压缩和任务原子认领优化执行速度。

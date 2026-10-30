# auto-video-editor：SQLite 数据库彻底移除执行设计

> **项目**：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> **目标分支**：`main`  
> **审查基线**：`d6aef8c7d87f6069f4302ec6e889bcd9bc4a5d49`  
> **基线提交时间**：2026-10-29 18:10:10 UTC（新加坡时间 2026-10-30 02:10:10）  
> **设计日期**：2026-10-30  
> **文档性质**：执行设计，不代表已经修改 GitHub 仓库或删除本机文件。

---

## 1. 目标与结论

### 1.1 改造目标

彻底移除项目运行环境和仓库代码中的 SQLite 数据库能力，统一使用现有 PostgreSQL 持久化架构，同时保持现有功能行为不变。

具体目标：

1. 不再使用 SQLite 保存 LangGraph checkpoint。
2. 不再引入 `SqliteSaver`、`sqlite3` 或 SQLite 专用 LangGraph 依赖。
3. 删除 SQLite 专用迁移、归档工具和已经失效的配置说明。
4. 清除已归档的旧 SQLite 数据库文件及其 `-wal`、`-shm` 侧文件。
5. 保留 PostgreSQL checkpoint、聊天会话、工作流台账及现有 API 行为。
6. 不删除与 SQLite 无关的 JSON 会话迁移、媒体文件、剪映草稿和工作流模块。

### 1.2 当前代码审查结论

**当前 `main` 分支已经完成 SQLite 到 PostgreSQL 的主要运行时迁移。本次不需要再次改造 LangGraph 或会话存储架构。**

本次工作的重点是清除历史数据和剩余的 SQLite 专用工具、旧环境配置及过时操作说明。

当前主要持久化职责如下：

| 功能 | 当前实现 | 本次处理 |
|---|---|---|
| LangGraph checkpoint | PostgreSQL `AsyncPostgresSaver` | 保留，不改写 |
| Agent Gate checkpoint | PostgreSQL `PostgresSaver` | 保留，不改写 |
| 聊天会话 | PostgreSQL `chat_sessions` | 保留，不改写 |
| 工作流运行台账 | PostgreSQL `workflow_runs` | 保留，不改写 |
| 数据库迁移 | `db/migrations.py` | 保留，不改写 |
| 会话 Repository | `db/session_repository.py` | 保留，不改写 |
| 单元测试 checkpoint | LangGraph `InMemorySaver` | 保留，仅用于进程内测试 |
| SQLite 旧数据归档脚本 | `scripts/retire_sqlite_checkpoints.py` | 完成数据处置后删除 |
| SQLite 旧归档文件 | `logs/retired_checkpoints/2026-10-30/` | 核验后永久删除数据库文件 |
| SQLite 旧配置注释 | `.env.example` | 删除并修正说明 |

### 1.3 最终状态定义

完成后应满足：

- 生产、开发及集成测试的持久化 checkpoint 均使用 PostgreSQL。
- 聊天会话和工作流台账继续使用同一个 PostgreSQL 数据库 `video_workflow`。
- 仓库不再包含 SQLite 数据库实现代码或 SQLite 专用依赖。
- 项目运行目录不再存在旧 SQLite 数据库、WAL、SHM 文件。
- 不再提供 SQLite 旧库归档脚本。
- 允许历史设计文档、测试注释中保留 SQLite 迁移记录；这些文字不是可执行后端，也不是数据库文件。

---

## 2. 审查依据与当前实现

### 2.1 LangGraph Checkpoint 已使用 PostgreSQL

文件：[`graph.py`](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)

当前生产路径：

```python
async with get_checkpointer() as saver:
    graph = build_graph(checkpointer=saver, thread_id="video-001")
    await graph.ainvoke(initial_state, config=config)
```

`get_checkpointer()` 使用 `AsyncPostgresSaver.from_conn_string(POSTGRES_URI)`，并执行 `await saver.setup()`。`build_graph()` 要求调用方显式注入 saver，未注入时会抛出 `RuntimeError`，不会自动回退到 SQLite 或内存存储。

文件：[`config.py`](https://github.com/wayyet/auto-video-editor/blob/main/config.py)

当前 `make_checkpointer()` 只保留 `memory` 分支供单元测试使用。`postgres` 必须通过 `graph.get_checkpointer()` 创建。旧的 SQLite 后端分支、`CHECKPOINTER_BACKEND` 和 `CHECKPOINTER_DB_DIR` 选路配置已从主配置中移除。

### 2.2 Agent Gate 已使用 PostgreSQL

文件：[`agent_gate/runner.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/runner.py)

Agent Gate 的同步 worker 使用 `langgraph.checkpoint.postgres.PostgresSaver`；异步主工作流使用 `AsyncPostgresSaver`。两者均连接 `POSTGRES_URI` 指向的 PostgreSQL 数据库。

`AGENT_GATE_CKPT_BACKEND` 只允许 `postgres` 或 `memory`。`memory` 仅适合沙箱和测试，不是持久化后端。

`production_blocker()` 仍会拒绝旧的 `CHECKPOINTER_BACKEND` 非法配置。这是防止旧环境变量被误认为仍然生效的保护逻辑，不是 SQLite 数据库实现。本次应保留该保护语义。

### 2.3 聊天会话已使用 PostgreSQL

主要文件：

- [`db/session_repository.py`](https://github.com/wayyet/auto-video-editor/blob/main/db/session_repository.py)
- [`db/migrations.py`](https://github.com/wayyet/auto-video-editor/blob/main/db/migrations.py)
- [`openstoryline/agent_fastapi.py`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/agent_fastapi.py)
- [`openstoryline/web/static/app.js`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/web/static/app.js)

会话状态保存在 `chat_sessions.state_json`（PostgreSQL `JSONB`）。前端会话列表从服务端 `/api/sessions` 获取；旧 `localStorage` 列表只用于一次性对账，不再作为权威数据源。

数据库故障时，会话接口采用 fail-closed 行为并返回 503，不应回退到本地 SQLite 或 JSON 文件写入。

### 2.4 运行台账已使用 PostgreSQL

`monitoring/runs_table.py` 通过 `POSTGRES_URI` 连接 PostgreSQL 的 `workflow_runs` 表。

该表是 PostgreSQL 中的一张业务表，不是独立数据库。不能因删除 SQLite 而删除 `workflow_runs`。

### 2.5 依赖声明已经是 PostgreSQL 方案

当前：

- `pyproject.toml` 声明 `langgraph-checkpoint-postgres`、`psycopg[binary,pool]`。
- `requirements.txt` 声明 `langgraph-checkpoint-postgres`、`psycopg[binary,pool]`。
- 未发现当前依赖清单仍声明 `langgraph-checkpoint-sqlite`。

**不要删除** `langgraph-checkpoint` 核心包、`langgraph-checkpoint-postgres` 或 `psycopg`。它们是当前 PostgreSQL saver 的依赖链。

---

## 3. SQLite 残留项清单

### 3.1 SQLite 专用脚本

文件：[`scripts/retire_sqlite_checkpoints.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/retire_sqlite_checkpoints.py)

这是当前已审查源码中明确保留 SQLite 操作的工具，包含：

- `import sqlite3`。
- 只读打开旧 `.sqlite` 文件。
- 查询 SQLite `checkpoints` 表及 `thread_id`。
- 扫描 `-wal`、`-shm` 侧文件。
- 将旧文件移动到 `logs/retired_checkpoints/<日期>/`。
- 对比 PostgreSQL `checkpoints` 表，识别仅存在于 SQLite 的 thread ID。
- 对符合条件的 `workflow_runs` 状态执行 `running/suspended -> failed` 标记。

它不是运行时组件，但属于 SQLite 专用迁移工具。**必须先确认历史数据已处置，再删除该脚本。**

注意：脚本的 `--apply` 不只是移动文件，也可能修改 `workflow_runs.status`。因此不得在未核对的情况下重复执行 `--apply`。

### 3.2 旧 SQLite 文件归档记录

文件：[`PostgreSQL 统一改造阶段 CDE 执行记录`](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E4%BF%AE%E6%AD%A3/auto-video-editor_PostgreSQL%E7%BB%9F%E4%B8%80%E6%94%B9%E9%80%A0_%E9%98%B6%E6%AE%B5CDE%E6%89%A7%E8%A1%8C%E8%AE%B0%E5%BD%95.md)

该执行记录记载了以下历史归档结果：

| 类型 | 数量 |
|---|---:|
| `.sqlite` 主文件 | 27 |
| `-wal` / `-shm` 侧文件 | 48 |
| 合计 | 75 |
| 总大小 | 8.34 MiB |
| 归档目录 | `logs/retired_checkpoints/2026-10-30/` |

执行记录还记载：旧 SQLite 中有 25 个 thread ID；当时这些 ID 与 PostgreSQL checkpoint thread ID 的交集为空。旧 SQLite checkpoint 按 D2 决策不迁移到 PostgreSQL，而是作为旧工作流中间态归档。

**数据影响：** 如果永久删除这批旧 SQLite 文件，将无法再从这些文件恢复对应的旧 checkpoint。依据现有迁移决策，旧任务需要重新运行，而不是从旧 SQLite checkpoint 继续恢复。执行永久删除前必须再次确认这些任务已经不需要恢复。

`logs/` 已被 `.gitignore` 忽略，因此 GitHub 仓库树不能证明本机归档目录当前仍然存在。必须在实际部署机器上执行文件清点。

### 3.3 `.env.example` 中的旧配置

文件：[`\.env.example`](https://github.com/wayyet/auto-video-editor/blob/main/.env.example)

当前仍有过时注释：

```dotenv
# Use Postgres explicitly. Leave unset to keep the SQLite default.
# CHECKPOINTER_BACKEND=postgres
```

这与当前代码冲突：当前生产路径已经固定使用 PostgreSQL，未设置该变量不会启用 SQLite。

处理方式：删除以上两行，不要重新引入 `CHECKPOINTER_BACKEND` 配置。`POSTGRES_URI` 是唯一数据库连接配置。

### 3.4 `.gitignore` 中的旧目录规则

当前 `.gitignore` 有：

```gitignore
# Week 3 runtime state
checkpoints/
```

旧 SQLite 归档完成后，根目录 `checkpoints/` 已不再由当前工作流代码写入。清理完成后建议把规则改为根路径限定：

```gitignore
# Legacy root-level checkpoint directory; no longer used by runtime
/checkpoints/
```

这样不会无意改变其它目录中同名 `checkpoints/` 的忽略行为。`outputs/` 及其它运行产物忽略规则保持不变。

### 3.5 必须保留的非 SQLite 模块

下列内容不是 SQLite，不得因本次清理而删除：

| 文件 / 模块 | 原因 | 处理 |
|---|---|---|
| `scripts/import_session_states.py` | 把旧 `session_state.json` 导入 PostgreSQL | 保留，属于 JSON → PostgreSQL 迁移工具 |
| `session_state.json` 只读兼容逻辑 | 旧会话文件兼容路径，与 SQLite 无关 | 本次不改；以后单独审查 |
| `openstoryline/web/static/app.js` 的一次性 localStorage 对账 | 迁移旧浏览器列表到服务端会话目录 | 保留 |
| `openstoryline/src/open_storyline/storage/session_manager.py` | 清理媒体 artifact/cache 目录 | 保留，不是数据库 |
| `db/session_repository.py` | PostgreSQL 会话读写 | 保留 |
| `db/migrations.py` | PostgreSQL 应用层 schema 迁移 | 保留 |
| `monitoring/runs_table.py` | PostgreSQL 工作流台账 | 保留 |
| `graph.py` / `agent_gate/runner.py` | PostgreSQL checkpoint saver | 保留 |
| PostgreSQL `checkpoints` 表 | LangGraph 的 PostgreSQL checkpoint 表 | 保留，不能删除 |
| `chat_sessions` / `workflow_runs` 表 | 业务数据表 | 保留 |
| `logs/retired_databases/langgraph_final_2026-10-30.dump` | 旧 PostgreSQL 数据库的备份，不是 SQLite | 本次不删除 |

---

## 4. 目标架构

```mermaid
flowchart TD
    UI[浏览器 Web UI] --> API[OpenStoryline FastAPI]
    API --> SESSION[SessionRepository]
    SESSION --> PG[(PostgreSQL video_workflow)]

    WORKFLOW[LangGraph 主工作流] --> ASYNC[AsyncPostgresSaver]
    AGENT[Agent Gate Worker] --> SYNC[PostgresSaver]
    ASYNC --> PG
    SYNC --> PG

    RUNS[workflow_runs] --> PG
    MIGRATION[db migrations] --> PG
    PG --> CP[checkpoints / checkpoint_blobs / checkpoint_writes]
    PG --> CHAT[chat_sessions]
    PG --> RUN[workflow_runs]

    API --> FS[媒体、缩略图、剪映草稿文件]
    WORKFLOW --> FS
```

### 4.1 数据库边界

唯一运行时数据库：`video_workflow`。

| PostgreSQL 对象 | 负责人 | 用途 |
|---|---|---|
| `checkpoints` | LangGraph saver | Checkpoint 主记录 |
| `checkpoint_blobs` | LangGraph saver | Checkpoint 大字段数据 |
| `checkpoint_writes` | LangGraph saver | 待提交写入记录 |
| `checkpoint_migrations` | LangGraph saver | saver 内部迁移记录 |
| `chat_sessions` | `SessionRepository` | 聊天会话及可恢复状态 |
| `workflow_runs` | 工作流运行台账 | 工作流运行状态 |
| `app_schema_migrations` | `db/migrations.py` | 应用层迁移版本 |

文件系统仍保存视频、图片、音频、缩略图和剪映草稿等媒体产物。这些文件不属于 SQLite 数据库，也不在本次删除范围内。

### 4.2 SQLite 删除后不允许的回退路径

不得新增以下逻辑：

- PostgreSQL 连接失败后自动创建 SQLite 文件。
- `build_graph()` 未注入 saver 时自动创建 SQLite saver。
- 会话写库失败后静默改写本地 SQLite 或 JSON 文件。
- Agent Gate worker 检测不到 PostgreSQL 时切换到 SQLite。

数据库不可用时，保持当前显式失败行为；不要通过隐藏的本地持久化路径伪装成功。

---

## 5. 执行计划

执行分为五个阶段。必须按顺序进行，特别是数据删除必须晚于数据核验和代码清理。

### 阶段 A：分支与备份准备

**目标：** 保证清理前可以回滚，确认执行对象正确。

1. 在本地更新 `main`，确认工作区没有未提交修改。
2. 从审查基线创建独立工作分支，例如：

```powershell
git switch main
git pull --ff-only origin main
git switch -c chore/remove-sqlite-artifacts
```

3. 记录当前提交：

```powershell
git rev-parse HEAD
```

4. 对 `video_workflow` 做 PostgreSQL 备份。以下命令要求本机已安装 PostgreSQL 客户端工具，并且 `POSTGRES_URI` 已正确设置：

```powershell
New-Item -ItemType Directory -Force -Path .\logs\sqlite-removal-backup | Out-Null
pg_dump --format=custom --file .\logs\sqlite-removal-backup\video_workflow_before_sqlite_cleanup.dump $env:POSTGRES_URI
```

5. 检查备份文件存在且大小大于 0：

```powershell
Get-Item .\logs\sqlite-removal-backup\video_workflow_before_sqlite_cleanup.dump |
    Format-List FullName,Length,LastWriteTime
```

6. 确认连接的是目标数据库：

```powershell
psql $env:POSTGRES_URI -v ON_ERROR_STOP=1 -c "SELECT current_database();"
```

期望结果：`video_workflow`。

**阶段 A 验收：** 有可用的 PostgreSQL 备份；工作分支和基线提交已记录；未执行任何 SQLite 文件删除。

### 阶段 B：盘点 SQLite 归档和运行状态

**目标：** 明确旧数据库文件位置、数量及其是否仍需要恢复。

1. 检查旧归档目录：

```powershell
$archive = '.\logs\retired_checkpoints\2026-10-30'

if (Test-Path -LiteralPath $archive) {
    Get-ChildItem -LiteralPath $archive -Force -File -Recurse |
        Select-Object FullName,Length,LastWriteTime
} else {
    Write-Host "归档目录不存在：$archive"
}
```

2. 统计 SQLite 主文件和 sidecar 文件：

```powershell
if (Test-Path -LiteralPath $archive) {
    $files = Get-ChildItem -LiteralPath $archive -Force -File -Recurse
    $sqliteFiles = @($files | Where-Object { $_.Name -match '\.sqlite$' })
    $sidecars = @($files | Where-Object { $_.Name.EndsWith('-wal') -or $_.Name.EndsWith('-shm') })

    [pscustomobject]@{
        SQLiteMainFiles = $sqliteFiles.Count
        WalShmFiles    = $sidecars.Count
        TotalBytes     = (($sqliteFiles + $sidecars) | Measure-Object -Property Length -Sum).Sum
    } | Format-List
}
```

3. 读取 `retirement_manifest.json`，检查 `sqlite_thread_ids`、`sqlite_only_thread_ids` 和 `postgres_lookup_error`。如果清单显示 PostgreSQL 查询失败，必须先修复 PostgreSQL 连通性，再进行状态核验。

4. 查看 PostgreSQL 当前数据：

```powershell
psql $env:POSTGRES_URI -v ON_ERROR_STOP=1 -c "SELECT current_database();"
psql $env:POSTGRES_URI -v ON_ERROR_STOP=1 -c "SELECT COUNT(DISTINCT thread_id) AS checkpoint_threads FROM checkpoints;"
psql $env:POSTGRES_URI -v ON_ERROR_STOP=1 -c "SELECT status, COUNT(*) FROM workflow_runs GROUP BY status ORDER BY status;"
psql $env:POSTGRES_URI -v ON_ERROR_STOP=1 -c "SELECT COUNT(*) AS sessions FROM chat_sessions;"
```

5. 对照 `workflow_runs` 中 `running` / `suspended` 任务，确认没有仍依赖旧 SQLite 文件的业务任务。

6. 检查项目目录内是否还存在其它数据库扩展名文件：

```powershell
Get-ChildItem -LiteralPath . -Force -File -Recurse -Include *.sqlite,*.sqlite3,*.db,*.db3 |
    Where-Object {
        $_.FullName -notmatch '[\\/](\.venv|venv|node_modules)[\\/]'
    } |
    Select-Object FullName,Length,LastWriteTime
```

该命令只用于盘点。不要对所有 `.db` 文件直接批量删除；必须检查实际用途和文件头，避免误删非 SQLite 文件或第三方工具数据。

**特别注意：** 现有阶段 CDE 记录显示 25 个旧 SQLite thread ID 当时没有出现在 PostgreSQL checkpoint 中。删除旧归档后，这些旧线程不能再从原 checkpoint 继续恢复。只有确认相关任务可以重新运行或已经废弃，才允许进入永久删除阶段。

**阶段 B 验收：** 已确认归档文件清单、数据库数量、PostgreSQL 状态和旧线程处置结论；不存在未确认的活动任务依赖。

### 阶段 C：清除仓库中的 SQLite 专用代码和旧配置

**目标：** 删除 SQLite 专用工具，但不修改业务持久化实现。

#### C.1 删除 SQLite 旧库归档工具

删除：

```text
scripts/retire_sqlite_checkpoints.py
```

前置条件：阶段 B 已完成；旧 SQLite 数据已经归档；需要保留的迁移清单已另行保存。

删除原因：旧 SQLite 文件处置完成后，该脚本不再是运行时或日常维护必需品；其 `sqlite3` 依赖和 SQLite 表结构读取代码应一起移除。

#### C.2 修正 `.env.example`

删除以下过时注释：

```dotenv
# Use Postgres explicitly. Leave unset to keep the SQLite default.
# CHECKPOINTER_BACKEND=postgres
```

保留：

```dotenv
POSTGRES_URI=postgresql://postgres:devpass@localhost:5432/video_workflow
```

不要新增其它数据库后端配置。

#### C.3 更新 `.gitignore`

把：

```gitignore
# Week 3 runtime state
checkpoints/
```

改为：

```gitignore
# Legacy root-level checkpoint directory; no longer used by runtime
/checkpoints/
```

本规则只调整忽略路径范围，不删除 `outputs/`、`logs/`、媒体产物等其它忽略规则。

#### C.4 更新 README 当前操作说明

文件：`README.md`

- 删除当前操作章节中调用 `scripts/retire_sqlite_checkpoints.py --apply` 的命令。
- 将当前数据库说明明确为 PostgreSQL 唯一持久化后端。
- 保留 Week 3 使用 SqliteSaver 的历史时间线，但明确标注它是历史实现，当前版本已不支持。
- 保留 PostgreSQL saver、数据库初始化、迁移和验收说明。

历史执行记录 `docs/修正/auto-video-editor_PostgreSQL统一改造_阶段CDE执行记录.md` 不建议删除或批量替换。该文档解释了迁移过程、75 个历史文件的处置、旧 thread ID 风险和回归结果，属于审计记录。

#### C.5 保护其它兼容逻辑

本阶段不修改以下文件的业务逻辑：

- `graph.py`
- `config.py` 的 `make_checkpointer()` 行为
- `agent_gate/runner.py` 的 PostgreSQL saver 创建和连接释放
- `db/connection.py`
- `db/migrations.py`
- `db/session_repository.py`
- `monitoring/runs_table.py`
- `openstoryline/agent_fastapi.py` 的会话保存与恢复逻辑
- `openstoryline/web/static/app.js` 的服务端会话列表逻辑
- `scripts/import_session_states.py`

`agent_gate/runner.py::production_blocker()` 对旧 `CHECKPOINTER_BACKEND` 环境变量的拒绝逻辑应保留。它是配置防护，不会创建 SQLite 数据库；不能为了减少字符串命中而移除保护。

**阶段 C 验收：** SQLite 专用脚本已经删除；`.env.example` 不再暗示 SQLite 是默认后端；README 的当前操作说明不再依赖已删除脚本；PostgreSQL 运行时模块没有被替换或重写。

### 阶段 D：永久删除旧 SQLite 数据文件

**目标：** 删除运行目录中的 SQLite 数据库主文件和 sidecar，不删除 PostgreSQL 数据。

1. 确认阶段 A 的 PostgreSQL 备份有效。
2. 再次检查阶段 B 的归档目录和清单。
3. 将 `retirement_manifest.json` 复制到独立审计路径，避免随 SQLite 文件一起删除：

```powershell
$archive = '.\logs\retired_checkpoints\2026-10-30'
$auditDir = '.\logs\sqlite-removal-backup'

New-Item -ItemType Directory -Force -Path $auditDir | Out-Null

if (Test-Path -LiteralPath (Join-Path $archive 'retirement_manifest.json')) {
    Copy-Item -LiteralPath (Join-Path $archive 'retirement_manifest.json') `
        -Destination (Join-Path $auditDir 'retirement_manifest_2026-10-30.json') -Force
}
```

4. 输出准备删除的文件清单，先人工核对：

```powershell
$legacyFiles = Get-ChildItem -LiteralPath $archive -Force -File -Recurse |
    Where-Object {
        $_.Name -match '\.sqlite$' -or
        $_.Name.EndsWith('-wal') -or
        $_.Name.EndsWith('-shm')
    }

$legacyFiles | Select-Object FullName,Length,LastWriteTime
```

5. 确认清单只包含本次已核验的旧 SQLite checkpoint 文件后，再执行删除：

```powershell
$legacyFiles | Remove-Item -Force
```

6. 验证旧 SQLite 数据文件已经清空：

```powershell
$remaining = Get-ChildItem -LiteralPath $archive -Force -File -Recurse |
    Where-Object {
        $_.Name -match '\.sqlite$' -or
        $_.Name.EndsWith('-wal') -or
        $_.Name.EndsWith('-shm')
    }

if ($remaining.Count -ne 0) {
    throw "仍有 SQLite 文件未清理：$($remaining.FullName -join ', ')"
}
```

7. `retirement_manifest.json` 是 JSON 审计元数据，不是数据库。保留在独立备份目录即可。归档目录为空时，可以删除空目录：

```powershell
if (Test-Path -LiteralPath $archive) {
    $remainingItems = @(Get-ChildItem -LiteralPath $archive -Force)
    if ($remainingItems.Count -eq 0) {
        Remove-Item -LiteralPath $archive -Force
    } else {
        Write-Host "目录中仍有非 SQLite 文件，暂不删除目录：$archive"
    }
}
```

8. 检查仓库根目录 `checkpoints/`。只有确认目录为空时才删除：

```powershell
$checkpointDir = '.\checkpoints'
if (Test-Path -LiteralPath $checkpointDir) {
    $items = @(Get-ChildItem -LiteralPath $checkpointDir -Force)
    if ($items.Count -eq 0) {
        Remove-Item -LiteralPath $checkpointDir -Force
    } else {
        Write-Host "checkpoints 目录仍有文件，禁止递归删除：$checkpointDir"
    }
}
```

**禁止执行：**

```powershell
Remove-Item .\logs -Recurse -Force
Remove-Item .\outputs -Recurse -Force
```

这两个目录包含其它运行日志、会话归档、媒体和工作流产物，不能作为 SQLite 清理目标。

**阶段 D 验收：** 已归档的旧 SQLite 主文件和 sidecar 数量为 0；PostgreSQL 数据库和业务数据未被删除；审计清单仍可读取。

### 阶段 E：静态扫描与回归测试

**目标：** 证明 SQLite 数据库能力被移除，同时现有功能没有回归。

#### E.1 静态扫描

在仓库根目录执行：

```powershell
rg -n -i --hidden `
  -g '!.git/**' `
  -g '!logs/**' `
  -g '!**/.venv/**' `
  -g '!**/venv/**' `
  -g '!**/node_modules/**' `
  'sqlite3|aiosqlite|SqliteSaver|langgraph-checkpoint-sqlite|sqlite://|CHECKPOINTER_DB_DIR' .
```

预期：

- 不再有 `sqlite3`、`aiosqlite`、`SqliteSaver`、`langgraph-checkpoint-sqlite`、SQLite URI 或 `CHECKPOINTER_DB_DIR` 的可执行代码引用。
- 对旧 `CHECKPOINTER_BACKEND` 的安全拒绝逻辑可以保留；它只用于阻止过时配置，不是 SQLite saver。
- 历史文档和测试注释可以保留迁移背景。不要为了让全仓 `rg sqlite` 返回 0 而删除历史审计信息。

同时检查过时脚本和配置引用：

```powershell
rg -n --hidden -g '!.git/**' -g '!logs/**' 'retire_sqlite_checkpoints|SQLite default|CHECKPOINTER_DB_DIR' .
```

README 当前操作说明和 `.env.example` 不应再引用已删除的工具或宣称默认使用 SQLite。

#### E.2 依赖检查

```powershell
python -m pip check
```

确认依赖解析正常。再检查依赖清单：

```powershell
rg -n -i 'sqlite|aiosqlite' pyproject.toml requirements.txt requirements-local-dev.txt
```

当前运行依赖不应包含 SQLite saver 包。保留 `langgraph-checkpoint`、`langgraph-checkpoint-postgres` 和 `psycopg`。

#### E.3 单元测试

```powershell
python -m pytest tests\unit -q
```

重点验证：

- `InMemorySaver` 单元测试路径仍可运行。
- graph 构建必须显式注入 checkpointer。
- SessionStore 的创建、读取、保存、恢复和异常映射保持不变。
- 会话保存失败仍然 fail-closed，不回退写本地文件。
- Agent Gate 对旧配置的防护测试仍然通过。

#### E.4 PostgreSQL 集成测试

先设置隔离测试库 URI：

```powershell
$env:TEST_POSTGRES_URI = 'postgresql://<user>:<password>@<host>:5432/<isolated_test_db>'
```

不要把生产 `video_workflow` 直接用于会创建、改名、软删除测试数据的用例。然后执行：

```powershell
python -m pytest tests\integration\test_postgres_checkpointer.py -q
python -m pytest tests\integration\test_postgres_resume.py -q
python -m pytest tests\integration\test_sessions_api_postgres.py -q
python -m pytest tests\integration\test_session_repository_postgres.py -q
```

测试重点：

- saver `setup()` 幂等。
- PostgreSQL checkpoint 的 `interrupt -> resume` 完整链路。
- 退出旧 saver 后，新连接、新 saver 仍能恢复同一 thread。
- resume 后不会产生 `\\x<hex>` 形式的分裂 thread ID。
- 会话跨连接、跨 SessionStore 实例恢复。
- 会话列表、自动/手动标题、游标分页、软删除、恢复和 503 错误契约保持不变。

#### E.5 Agent Gate 测试

Agent Gate 不一定包含在项目默认 `testpaths` 中，应单独执行：

```powershell
python -m pytest agent_gate\tests -q
```

若上次测试被中断，先检查并清理测试遗留 worker 进程，再重新运行，避免旧 worker 占用 thread ID 导致误报。

#### E.6 启动与前端冒烟测试

1. 启动 PostgreSQL，确认数据库为 `video_workflow`。
2. 启动项目工作流及 OpenStoryline 服务。
3. 创建一个新会话，发送一条消息，确认 `/api/sessions` 返回该会话。
4. 刷新页面或使用另一个浏览器配置文件，确认侧栏仍从服务端读取同一会话。
5. 重新启动 Web 服务，再次打开该会话，确认聊天内容能够恢复。
6. 启动 LangGraph 工作流，走到一个人工 interrupt 节点，确认 checkpoint 写入 PostgreSQL。
7. 结束进程后重新启动 worker，确认能够从 PostgreSQL checkpoint 恢复。
8. 验证媒体上传、缩略图、视频输出和剪映草稿路径没有变化。

**阶段 E 验收：** 单元测试、PostgreSQL 集成测试、Agent Gate 测试和前端冒烟测试均通过；SQLite 静态扫描没有可执行后端残留；业务数据和媒体产物正常。

---

## 6. 文件变更清单

| 路径 | 操作 | 说明 |
|---|---|---|
| `scripts/retire_sqlite_checkpoints.py` | 删除 | 先完成旧数据处置，再删除 SQLite 专用工具 |
| `.env.example` | 修改 | 删除“默认 SQLite”及旧 backend 配置注释 |
| `.gitignore` | 修改 | 将 `checkpoints/` 改为根路径限定 `/checkpoints/` |
| `README.md` | 修改 | 删除已废弃脚本的当前操作入口，澄清 PostgreSQL 唯一后端 |
| `logs/retired_checkpoints/2026-10-30/*.sqlite` | 删除 | 永久删除 27 个已核验主文件 |
| `logs/retired_checkpoints/2026-10-30/*-wal` | 删除 | 永久删除 WAL 侧文件 |
| `logs/retired_checkpoints/2026-10-30/*-shm` | 删除 | 永久删除 SHM 侧文件 |
| `logs/retired_checkpoints/2026-10-30/retirement_manifest.json` | 迁移/保留 | 复制到审计备份目录，不作为数据库保留 |
| 根目录 `checkpoints/` | 条件删除 | 只在目录为空时删除 |

以下文件不应因本次改造而产生功能性变更：

```text
graph.py
config.py
state.py
db/connection.py
db/migrations.py
db/session_repository.py
monitoring/runs_table.py
agent_gate/runner.py
openstoryline/agent_fastapi.py
openstoryline/web/static/app.js
scripts/import_session_states.py
```

如果执行期间发现上述文件存在真实 SQLite 读写调用，应先单独分析调用链和测试覆盖，再决定是否删除。不能基于文件名或注释直接删除模块。

---

## 7. 数据安全与回滚策略

### 7.1 主要风险

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 旧 SQLite 有 PostgreSQL 中不存在的 checkpoint | 删除后不能继续恢复旧线程 | 检查 manifest 中的 `sqlite_only_thread_ids`，确认任务可重跑 |
| `--apply` 重复执行 | 可能修改 `workflow_runs.status` | 先核验现有归档；只有活动目录仍有旧文件时才运行该脚本 |
| 误删 PostgreSQL checkpoint 表 | 所有持久化工作流状态不可恢复 | 不执行 `DROP TABLE checkpoints`；不修改 `video_workflow` schema |
| 误删会话 JSON 归档 | 旧会话回滚能力丢失 | 本次不删除 `retired_session_states` |
| 误删 PostgreSQL 备份 | 数据恢复窗口缩短 | 保留 `video_workflow` 的 `pg_dump` 备份 |
| 测试写入生产数据库 | 污染真实会话和 checkpoint | 集成测试使用隔离测试库 |

### 7.2 不允许的数据库删除操作

本次不执行以下操作：

```sql
DROP DATABASE video_workflow;
DROP TABLE checkpoints;
DROP TABLE checkpoint_blobs;
DROP TABLE checkpoint_writes;
DROP TABLE chat_sessions;
DROP TABLE workflow_runs;
```

以下文件也不属于 SQLite，不要随本次清理删除：

```text
logs/retired_databases/langgraph_final_2026-10-30.dump
logs/retired_session_states/
logs/stage_e_cleanup/
```

### 7.3 回滚边界

- **代码回滚：** 通过 Git 回退清理提交即可恢复删除的脚本和配置注释。
- **SQLite 文件回滚：** 一旦执行阶段 D 永久删除，旧 SQLite checkpoint 无法通过代码回滚恢复。若业务要求保留恢复能力，必须在阶段 D 前把归档文件复制到受控离线备份介质；若最终要求所有副本均删除，则必须接受旧 SQLite-only thread 不可恢复。
- **PostgreSQL 回滚：** 使用阶段 A 的 `video_workflow_before_sqlite_cleanup.dump` 恢复 PostgreSQL 数据。不得假设旧 SQLite 文件可以替代 PostgreSQL 备份。

---

## 8. 最终验收标准

全部满足以下条件，才算完成：

### 代码与配置

- [ ] 仓库不存在 SQLite saver 实现或 `sqlite3` 运行时代码。
- [ ] `scripts/retire_sqlite_checkpoints.py` 已删除。
- [ ] `.env.example` 不再说明 SQLite 是默认后端。
- [ ] `pyproject.toml`、`requirements.txt`、`requirements-local-dev.txt` 不包含 SQLite saver 依赖。
- [ ] README 当前操作章节不再调用已删除的 SQLite 归档脚本。
- [ ] `CHECKPOINTER_BACKEND` 旧配置保护仍能阻止错误配置；PostgreSQL saver 选择逻辑未被破坏。

### 数据文件

- [ ] 已核验旧 SQLite-only thread 的业务状态。
- [ ] `logs/retired_checkpoints/2026-10-30/` 中 `.sqlite` 主文件数量为 0。
- [ ] 对应 `-wal` 和 `-shm` 侧文件数量为 0。
- [ ] 项目运行目录内无其它经确认的 SQLite 数据文件。
- [ ] PostgreSQL `video_workflow` 中 `checkpoints`、`chat_sessions`、`workflow_runs` 等表仍存在且数据可读。

### 功能与测试

- [ ] `build_graph()` 未注入 saver 时仍明确失败。
- [ ] `AsyncPostgresSaver` 的 setup、interrupt、resume 通过。
- [ ] 新 saver / 新连接能够恢复旧 thread 的 PostgreSQL checkpoint。
- [ ] Agent Gate 使用 PostgreSQL `PostgresSaver`，无本地文件回退。
- [ ] 会话保存、读取、列表、重命名、删除和恢复行为不变。
- [ ] 浏览器刷新和服务重启后，侧栏历史仍从服务端读取。
- [ ] 媒体上传、输出路径、剪映草稿及现有节点执行逻辑不受影响。
- [ ] 单元测试、PostgreSQL 集成测试和 Agent Gate 测试通过。

---

## 9. 推荐执行顺序总结

```mermaid
flowchart TD
    A[建立分支与 PostgreSQL 备份] --> B[盘点 SQLite 归档与 thread ID]
    B --> C{旧 SQLite-only 任务是否可重跑?}
    C -->|否 / 未确认| X[停止，不删除旧数据库]
    C -->|是| D[删除 SQLite 专用脚本和过时配置]
    D --> E[核对并永久删除 SQLite 主文件与 sidecar]
    E --> F[静态扫描与依赖检查]
    F --> G[单元 / PostgreSQL / Agent Gate 回归]
    G --> H[前端与工作流冒烟测试]
    H --> I[提交清理变更并记录验收结果]
```

**推荐方案：** 保留已经完成的 PostgreSQL 架构，只清理 SQLite 归档文件、SQLite 专用工具和过时配置。不要重新设计 `SessionRepository`、`AsyncPostgresSaver` 或前端会话 API。

**不可跳过的前置条件：** 旧 SQLite 归档里记录的 25 个 SQLite-only thread ID 不在当时的 PostgreSQL checkpoint 集合中。永久删除它们意味着放弃这些旧任务的 checkpoint 续跑能力；只有确认任务可重跑或已废弃后才能执行。

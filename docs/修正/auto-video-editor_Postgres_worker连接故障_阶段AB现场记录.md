# auto-video-editor：Postgres worker 连接故障 —— 阶段 A/B 现场记录

> **项目**：`wayyet/auto-video-editor`
> **分支**：`main` @ `a88419c`
> **执行日期**：2026-10-31
> **执行范围**：仅《[auto-video-editor_Postgres_worker连接故障_执行设计.md](auto-video-editor_Postgres_worker连接故障_执行设计.md)》§7 的**阶段 A（现场收集与基线确认）**与**阶段 B（环境临时验证）**；阶段 C1–C3 / D / E 按指示**未执行**。
> **脱敏声明**：本记录不含任何真实密码；DSN 一律以 `***` 脱敏呈现。

---

## 0. 执行环境与基础设施恢复（中止条件处置）

- 执行终端为 ZCode 会话（Git Bash + PowerShell 子进程），非事故当天的生产终端。
- 执行开始时 **Docker daemon 未运行**、`127.0.0.1:5432` 主动拒绝连接，触发 §7.3 中止条件
  「容器未运行或数据库不可达时，先恢复基础设施再重做本阶段」。
- 处置（均只读/无数据操作）：
  1. 启动 Docker Desktop（daemon 版本 29.7.2）。
  2. 确认容器 `edb-postgres16` 存在、镜像已缓存、命名卷 `edb_pgdata` 已存在。
  3. 运行仓库自带 `scripts/start_postgres.ps1`（**未传 `-TargetUri`**，未执行角色/库对齐）：
     对既有容器仅执行 `docker start`，未删除/重建卷，未清理任何数据。
- 恢复后状态：`edb-postgres16  Up (healthy)   0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp`。

## 1. 阶段 A 现场收集记录

### A-1 容器状态与时间线

| 项目 | 值 |
|---|---|
| 容器 / 镜像 | `edb-postgres16` / `quay.io/enterprisedb/postgresql:16.11-3.5-postgis-multilang` |
| 本次执行前状态 | `Exited (0)`（约执行前 8 小时；重启策略 `unless-stopped`，未随 daemon 恢复自动拉起） |
| 容器时间线（UTC） | `2026-10-30 21:26:09` 启动 → `2026-10-30 22:12:15` 正常退出 |
| 恢复后状态 | `Up (healthy)`，端口映射 `0.0.0.0:5432->5432`、`[::]:5432->5432` |
| 版本 | PostgreSQL 16.11（x86_64-pc-linux-gnu） |

**关键推断**：故障 worker 失败时间为 `2026-10-30T21:35:09Z`，落在容器「启动后、退出前」区间内，且错误为
`fe_sendauth: no password supplied`（服务器在线并要求密码）而非 `connection refused`。
**容器退出发生在故障之后约 37 分钟，不是本次故障的原因。**

### A-2 环境变量脱敏快照

`POSTGRES_URI` 与 `AGENT_GATE_POSTGRES_URI` 在 **Process / User / Machine 三个作用域全部为 `<unset-or-empty>`**。

- User/Machine 作用域为空 ⇒ 任何新启动的进程（含 `start_production.ps1`）都不会从注册表继承这两个变量。
- 事故当天生产进程链（Uvicorn → MCode → MCP → Worker）的运行时环境无法事后重建；
  本快照只代表执行时刻状态。

### A-3 `verify_postgres.py` 基线（原样环境，未做任何修改）

```text
✗ 未设置 POSTGRES_URI
exit=2
```

当前终端没有任何 `POSTGRES_URI` ⇒ 脚本按设计以 exit 2 退出。该脚本只读当前进程的
`POSTGRES_URI`，不能替代 Worker 同路径诊断（§6.5，属阶段 C3）。

### A-4 故障 Worker 现场定位

现场目录：`outputs/914c74fc5bdf4d44b7e021a7d483a6e8-r2/agent_gate/`

| 证据 | 内容 |
|---|---|
| `status.json` | `state=failed`；`pid=14148`；`started_at=2026-10-30T21:35:08Z`；`heartbeat_at=2026-10-30T21:35:09Z`（启动后约 1 秒即失败） |
| `worker.log` | **0 字节** —— 失败早于任何日志写入 |
| `events.jsonl` | 单条 `worker_failed` 事件，与 status.json 错误一致 |
| 错误类型 | `RuntimeError`（由 `psycopg.OperationalError: fe_sendauth: no password supplied` 包装） |
| 连接目标 | `localhost (::1):5432`（libpq 默认地址族优先 IPv6） |
| 失败点 | `agent_gate/runner.py:216 make_checkpointer` → `Connection.connect(uri, ...)` ← `checkpointer_session`（runner.py:261）← `run_worker`（runner.py:440） |

**结论：失败发生在 checkpointer 初始化阶段，早于任何 LangGraph 节点执行**
（与 `worker.log` 为 0 字节相互印证）。输入素材（`inputs/914c74fc.../media_0001~0005.mov`）与此故障无关。

**关于「故障 Worker 的 DSN 来源是否可复现」**：

- HEAD 代码路径已固化（`runner.py:119` `postgres_uri()`：`AGENT_GATE_POSTGRES_URI` → `POSTGRES_URI` →
  `config.POSTGRES_URI` → 默认值；空环境变量被 `strip()` 跳过；`config.py:375` 的
  `os.environ.get(...)` 使「存在但为空」遮蔽默认值 —— 与执行设计 §4 的 P0/P1 分析一致）。
- 但**现有日志无法直接复现当天 worker 实际使用的 DSN 来源**：worker 无 DSN 来源日志、`worker.log` 为空。
  这正是 §6.3（Worker 输出脱敏来源诊断）要补的能力。
- 已排除项：`.mcp.json` 未注入（见 A-5）；User/Machine 作用域无残留变量；
  `start_production.ps1:42` 默认 `-PostgresUri` 为**带密码** DSN（`postgres:***@localhost:5432/video_workflow`）。
- 剩余可疑路径：生产进程链中「存在但为空 / 无密码」的运行时变量（注入时序缺陷 §4 P0 +
  复用旧 Web 进程的历史环境 §6.2），只能在阶段 C/D/E 通过诊断脚本与重启闭环确认。

### A-5 操作员目录 `.mcp.json` 审计（§6.6）

文件：`E:\Documents\kuaishou\video-ops\.mcp.json`（`video-agent` server）

| 检查项 | 实际值 | 结论 |
|---|---|---|
| `command` | `E:/Documents/kuaishou/auto-video-editor/.venv/Scripts/python.exe` | ✅ 当前仓库 venv |
| `args` | `-m agent_gate.mcp_server --outputs-root .../outputs --graph-factory graph:build_graph` | ✅ 当前入口 |
| `cwd` | 未设置（由 PYTHONPATH/REPO_ROOT 保证导入） | ✅ 可导入仓库根 `config.py` |
| `env.POSTGRES_URI` | **未设置** | ✅ 无空值/无密码 DSN 注入 |
| `env.AGENT_GATE_POSTGRES_URI` | **未设置** | ✅ 无测试覆盖变量泄漏 |
| `env` 其余 | `PYTHONPATH`/`AGENT_GATE_REPO_ROOT`/`PYTHONUTF8`/`STORYLINE_MODE`/`AGENT_GATE_MEDIA_ROOTS`/`PATH` | 与 Postgres 无关 |

**审计结论：`.mcp.json` 不是本次故障的 DSN 污染源。** MCP 进程的 Postgres 变量完全继承自父进程
（OpenStoryline Uvicorn），因此根因收敛到启动脚本注入时序（§6.1，阶段 C1 修复范围）。

## 2. 阶段 B 环境临时验证记录

按 §7.4 在**当前终端进程**（Git Bash `export`，等价于 PowerShell `$env:`，仅本进程生效）执行：

1. 设置 `POSTGRES_URI` 为阶段 A 确认的正确 DSN；
2. 移除 `AGENT_GATE_POSTGRES_URI`（干净变量环境）；
3. 复跑 `scripts/verify_postgres.py`。

结果：

```text
version : PostgreSQL 16.11 on x86_64-pc-linux-gnu ...
db      : video_workflow
tables  : ['app_schema_migrations', 'chat_sessions', 'checkpoint_blobs',
           'checkpoint_migrations', 'checkpoint_writes', 'checkpoints', 'workflow_runs']
✓ 全部 checkpoint 表存在
exit=0
```

**DSN 取值基准（脱敏，供阶段 C1 的 `start_production.ps1 -PostgresUri` 使用）**：

```text
postgresql://postgres:***@localhost:5432/video_workflow
host=localhost; port=5432; db=video_workflow; user=postgres; password_present=true
AGENT_GATE_POSTGRES_URI = <unset>
```

即：「正确 DSN + 干净变量环境」可连通目标库，四张 checkpoint 表齐全 —— 阶段 B 目标达成。
该验证只作用于本进程，**未**修复任何已运行服务（阶段 E 才处理重启）。

## 3. 阶段 A 验收标准对照（§7.3 三问）

| # | 问题 | 答案 |
|---|---|---|
| 1 | 数据库服务是否在运行 | 执行时刻**未运行**（daemon 未启动、容器 8 小时前退出）；已按中止条件恢复，现为 `Up (healthy)`。故障当天容器在线，退出与故障无关。 |
| 2 | 当前环境哪个变量携带什么形态的 DSN | 三作用域均未设置；临时验证中以 `POSTGRES_URI` 携带带密码 DSN（脱敏形态见上）通过。 |
| 3 | 故障 Worker 的 DSN 来源是否可复现 | 日志层面**不可直接复现**（无 DSN 来源日志、worker.log 为空）；代码解析路径已固化，待阶段 C3 诊断脚本补齐同路径可观测性。 |

## 4. 边界与未执行项

- **未做任何代码修改**（阶段 C1/C2/C3 未执行）；本记录为本次执行唯一新增文件。
- 未重启 OpenStoryline Web（7860）、未触碰任何运行中的服务。
- 未做任何数据操作：未删库、未删卷、未清空/重置 checkpoint 表；事故 thread
  `914c74fc5bdf4d44b7e021a7d483a6e8-r2` 是否已有 checkpoint 的判定属阶段 E（§9），本阶段未查询。
- 指示灯：在阶段 C 修复并按 §6.2 重启旧 Web 进程之前，生产链路重复该任务仍会复现同类失败。

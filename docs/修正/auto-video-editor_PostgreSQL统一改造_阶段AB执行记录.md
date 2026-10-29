# auto-video-editor PostgreSQL 统一改造 —— 阶段 A / B 执行记录

> 对应设计文档:`docs/修正/auto-video-editor_PostgreSQL统一改造_设计开发方案.md`
> 本次**只执行阶段 A(基线与库名)与阶段 B(表与应用迁移)**;阶段 C / D / E 未执行。

| 项 | 值 |
|---|---|
| 执行日期 | 2026-10-29 |
| 代码基线 | `a8d9a3e`(改造前 `main` HEAD) |
| 目标库 | `video_workflow`(本机容器 `edb-postgres16`,`edb_pgdata` 卷) |
| 源库 | `langgraph`(保留未删,阶段 E 再按策略处置) |

---

## 1. 阶段 A:基线与库名

### A-1 建 `video_workflow` 库

用仓库现成的对齐器(只 CREATE / GRANT,不 DROP):

```powershell
.\.venv\Scripts\python.exe scripts\align_postgres.py `
  --target-uri "postgresql://postgres:devpass@localhost:5432/video_workflow" `
  --admin-uri  "postgresql://postgres:devpass@localhost:5432/postgres"
```

结果:新建库 `video_workflow`(owner=postgres),已授权,目标 URI 回验 `current_database() = 'video_workflow'`。

### A-2 搬运 `langgraph` 的 PG 数据

按方案 §5.2 的"主方案"执行(库到库管道,不用 Python 逐行转储):

```powershell
docker exec edb-postgres16 pg_dump -U postgres -d langgraph `
  | docker exec -i edb-postgres16 psql -U postgres -d video_workflow -v ON_ERROR_STOP=1
```

**搬运后核对(门禁:新库四个表计数与源一致)** —— 计数**与内容校验和**都逐表比对:

| 表 | 源 `langgraph` | 目标 `video_workflow` | md5 内容校验和(源 = 目标) |
|---|---|---|---|
| `checkpoints` | 315 | 315 | `c7b2d74afae2b0ae4294f2f5e0b63d14` |
| `checkpoint_blobs` | 1080 | 1080 | `95d0e635c51ad1f69a1500a1612c99a7` |
| `checkpoint_writes` | 3645 | 3645 | `7d9ac75fdf43e64b61d1ac69c0e69d6c` |
| `checkpoint_migrations` | 10 | 10 | `f009eb99a89790db8a44fc36154febc9` |

> 校验和口径:每行字段拼成一个字符串取 md5,再对行内 md5 排序后聚合取 md5(顺序无关)。
> `checkpoint_blobs.blob` / `checkpoint_writes.blob` 是 bytea,按 `encode(blob,'hex')` 参与计算,
> 因此能检出**二进制内容**层面的损坏,不只是行数对不对。

⚠️ 与方案 §5.2 核对清单的一处出入:源库 `langgraph` 里**没有** `workflow_runs` 表
(开发默认走 SQLite,这张表从未被建过)。所以 §5.2 那条 `SELECT count(*) FROM workflow_runs`
在源库上无意义 —— `workflow_runs` 由阶段 B 的迁移在**新库**里新建。

### A-3 全量 pytest 基线 + skip 列表存档

第一次跑全量时发现 3 个失败,全部落在 `tests/integration/test_postgres_checkpointer.py`。
根因正是方案 §7.2 点名的**"假绿"**缺陷:该文件用 `pytest.mark.skipif(不可达就 skip)`,
所以**容器没起时整文件静默 skip**;容器一起来,它就真跑,然后暴露两个从未被发现的问题:

1. 默认 URI 的口令是 `postgres`、库名是 `video_workflow`,与 `config.POSTGRES_URI`
   (`postgres:devpass@...`)对不上 ⇒ 认证失败。
2. Windows 默认事件循环是 **ProactorEventLoop**,psycopg 异步不支持 ⇒
   `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。

按方案 §7.2 修掉(测试文件 + `pyproject.toml` 注册 `postgres` 标记):

- 独立环境变量 `TEST_POSTGRES_URI`(不给则回落到 `POSTGRES_URI` / `config.POSTGRES_URI`);
- **显式**指定了 URI 却连不上 ⇒ 直接 **fail**,不再静默 skip;
- 没指定且默认库不可达 ⇒ 带 `postgres` 标记 skip(可用 `-m postgres` 单独统计);
- 改用 `SelectorEventLoop` 跑异步用例,与 `scripts/run_workflow.py`、
  `scripts/setup_postgres_schema.py` 同一套写法。

**基线数字(修复后,改造前)**

| 指标 | 值 |
|---|---|
| tests | 1178 |
| failures | 0 |
| errors | 0 |
| skipped | 8 |
| 耗时 | 223 s |

**基线 skip 列表(8 条,逐条存档;阶段 B 后的回归要逐条比对)**

| # | 用例 | 跳过原因 |
|---|---|---|
| 1 | `tests/integration/test_e2e_tc01_to_tc06::test_tc05_openstoryline_not_ready_early_exits` | pre-migration e2e 用例,已改由 mock 覆盖 |
| 2 | `tests/integration/test_e2e_tc01_to_tc06::test_tc06_unsupported_video_format_writes_error_log` | 同上 |
| 3 | `tests/integration/test_preflight_windows::test_preflight_skip_most_steps_baseline_performance` | 需要管理员权限(`-m admin`) |
| 4 | `tests/integration/test_preflight_windows::test_preflight_starts_stopped_postgres_container` | 需要管理员权限 |
| 5 | `tests/integration/test_preflight_windows::test_preflight_starts_stopped_docker_service` | 需要管理员权限 |
| 6 | `tests/integration/test_sequential_integration::test_node_03_monkeypatch_subprocess_truly_effective` | 既有 `RecursionError`(与本次改造无关) |
| 7 | `tests/unit/test_node_16_translate_subtitles::test_validate_layout_missing_font_via_invalid_path` | Windows 上 PIL `load_default` 行为差异 |
| 8 | `tests/unit/video_edit_capabilities/test_subtitle::test_qc_overlapping_cues_emit_overlap_issue` | 临时目录里找不到视频 |

> junit XML 存档在 `logs/baseline/`(`logs/` 已 gitignore,不进版本库)。

### A-4 库名统一到 `video_workflow`(方案 §4.9)

| 文件 | 改动 |
|---|---|
| `config.py:374-377` | `POSTGRES_URI` 默认值 → `video_workflow` |
| `start_production.ps1:42` | `-PostgresUri` 默认值 → `video_workflow` |
| `scripts/preflight.ps1:72` | `$DefaultPostgresUri` → `video_workflow` |
| `scripts/setup_postgres.ps1:50-53,59,65-66` | `CREATE DATABASE auto_video_editor` → `video_workflow`;角色 `ave_app` 保留 |
| `scripts/setup_postgres_schema.py:26` | 示例串同步 |
| `scripts/verify_postgres.py:33,48,68` | `DEFAULT_DATABASE` 与示例串同步 |
| `scripts/align_postgres.py:127` | 告警里的默认 URI 同步 |
| `scripts/start_postgres.ps1:144` | "下一步"提示指向 `video_workflow` |
| `.env.example:2` | `POSTGRES_URI` → `video_workflow` |
| `README.md:99,116,132,215,404` | 数据库名 / 默认 URI / 环境变量表同步 |

**`scripts/start_postgres.ps1` 的 `$PgDb` 保持 `langgraph` 不动**(方案 §4.9 明确要求):
容器 `POSTGRES_DB` 只在**首次初始化数据卷**时生效,现有卷早已初始化,改这个变量不会自动建库;
`video_workflow` 由 `align_postgres.py` 建(`start_postgres.ps1 -TargetUri` 已实现该调用)。

---

## 2. 阶段 B:表与应用迁移

### B-1 迁移框架 + `chat_sessions`

新增包 `db/`:

| 文件 | 职责 |
|---|---|
| `db/migrations.py` | 版本化迁移器 + 迁移清单 |
| `db/connection.py` | 连接辅助(把 `client_encoding` 设成 UTF-8) |
| `db/session_repository.py` | `chat_sessions` 仓储 |
| `scripts/migrate_db.py` | 迁移 CLI(`--status` 只看状态) |

迁移器特性:

- **幂等** —— 每条语句自身幂等(`IF NOT EXISTS` / `DO $$ ... $$` 守卫),
  且 `app_schema_migrations` 记录已应用版本;
- **原子** —— 所有迁移在同一个事务里跑,任一条失败整体回滚并抛异常(fail-closed);
- **串行** —— `pg_advisory_xact_lock` 抢占,多实例同时启动时只有一个真跑迁移。

`chat_sessions` 建表 DDL 与方案 §2.1 逐字一致(`session_id` / `title` / `title_source` /
`state_json` / `message_count` / `last_message_preview` / `created_at` / `updated_at` /
`deleted_at` / `version`),外加部分索引
`idx_chat_sessions_active_updated (updated_at DESC) WHERE deleted_at IS NULL`。

**分工边界**(方案 §2.1):应用迁移**只**管 `app_schema_migrations` / `chat_sessions` /
`workflow_runs`;LangGraph 自己的 4 张 checkpoint 表仍由 `AsyncPostgresSaver.setup()` 建。
`tests/unit/test_db_migrations.py` 里有一条静态断言专门守这条边界。

### B-2 `workflow_runs` 升级

- `thread_id` 加 `CHECK (thread_id ~ '^[A-Za-z0-9_.:-]+$')` ⇒ 挡掉 bytes 造成的
  `\x<hex>` 字面量(方案 §3.2 A2);
- `status` 加 `CHECK (status IN ('running','suspended','completed','failed'))`;
- `updated_at` 由 `TIMESTAMP` 改 `TIMESTAMPTZ`,走**加列 → 回填 → 换列**,
  存量行按 **UTC** 解释(方案 §11 U1 的建议默认);
- 补索引 `(status, updated_at DESC)`。

存量表升级用 `ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT`:
存量违规 ⇒ 迁移失败(fail-closed),不会静默放行。

`monitoring/runs_table.py` 同步改造(方案 §4.4):建表 DDL 移出(唯一事实来源变成
`db/migrations.py`),`init_schema()` 改为调用迁移器;降级策略分层 —— 台账失败仍返回降级值,
但**必须**打 **ERROR** 级日志并累加 `runs_table.degraded_count()`(可观测);
`status` 的上层校验保留,DB 层再补一道 CHECK。

### B-3 `SessionRepository`

提供:创建 / 幂等创建(`create_if_absent`,供 §5.4 旧 JSON 导入)/ 读取 / 更新 /
软删除 / 恢复 / 列表。关键语义:

- **乐观锁**(`version` 列):`UPDATE ... WHERE session_id = %s AND version = %s`,
  影响行数为 0 ⇒ 抛 `SessionVersionConflict`,**不覆盖**(方案 §6);
  不传 `expected_version` 时先 `SELECT ... FOR UPDATE` 取当前版本再更新。
- **游标分页**:`limit + cursor`,按 `(updated_at, session_id)` 行值比较,**不用 offset**;
  标题前缀过滤走 `LIKE ... ESCAPE '\'`,`%` / `_` 会被转义(不拼进 SQL 字符串)。
- **`title_source` 手动优先**:`rename()` 默认 `manual`;自动标题走 `apply_auto_title()`,
  SQL 里带 `title_source = 'auto'` 条件 ⇒ 手动标题不会被覆盖。

### B-4 门禁

**迁移脚本幂等**(连跑 3 次):

```
✓ 已应用迁移版本: [1, 2]
✓ 数据库已是最新,无需迁移
✓ 数据库已是最新,无需迁移
```

**新增测试 58 条**(单元 42 + 集成 16):

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/unit/test_session_repository.py` | 30 | 创建 / 读取 / 更新 / 软删除 / 恢复 / 分页 / 手动标题优先 / 版本冲突 / LIKE 转义 |
| `tests/unit/test_db_migrations.py` | 12 | `Migration` 自校验 / 迁移清单 / 关键 DDL 片段 / 分工边界 |
| `tests/integration/test_session_repository_postgres.py` | 16 | 打真实库:跨连接可见 / 中文往返 / 版本冲突不覆盖 / 游标分页不重不漏 / CHECK 约束 |

### B-5 全量回归 + 基线比对(方案 §7.4)

| 指标 | 改造前基线 | 阶段 B 之后 | 差值 |
|---|---|---|---|
| tests | 1178 | 1236 | **+58** |
| failures | 0 | 0 | 0 |
| errors | 0 | 0 | 0 |
| skipped | 8 | 8 | **0** |

**skip 列表逐条比对(方案 §7.4 的硬要求)**

| 比对项 | 结果 |
|---|---|
| 基线独有的 skip | 无 |
| 改造后新增的 skip | 无 |
| skip 原因变化 | 1 条 —— `test_qc_overlapping_cues_emit_overlap_issue`,差异只是 pytest 临时目录的运行序号(`pytest-13` vs `pytest-14`),语义相同 |
| 消失的用例 | 无 |
| 新增用例 | 58 条,与新增测试文件条数**完全对上**(净增数 = 新增数) |

⇒ 没有回归,也没有"假绿"。基线数字与 skip 列表已在上文 §A-3 存档。

---

## 3. 执行中发现的三个"环境坑"(方案未覆盖,已在本仓修掉)

### 3.1 `SQL_ASCII` 让 psycopg 按 ASCII 编码查询串

本机容器 `edb-postgres16` 的库编码是 **`SQL_ASCII`**(Windows 没设 `LANG`)。
psycopg3 因此把连接的 `encoding` 判成 `ascii`,**查询字符串里的非 ASCII 字符直接炸**:

```
UnicodeEncodeError: 'ascii' codec can't encode characters in position 8-10
```

`chat_sessions` 的建表 DDL 里就有中文默认值 `'新对话'` ⇒ 必然踩到。

**修法**:`db/connection.py` 统一把 `client_encoding` 设成 UTF-8
(`psycopg.connect(uri, client_encoding="utf-8")`)。库本身仍是 `SQL_ASCII`,
**服务端不做任何字符集转换**,原始字节原样存取 —— 不会二次编码。
副作用是好的:TEXT 列不再以 `bytes` 返回,直接给 `str`。

### 3.2 JSONB + `ensure_ascii=True` 在 `SQL_ASCII` 库上直接失败

psycopg3 默认用 `ensure_ascii=True` 序列化 JSONB ⇒ 中文变成 `\uXXXX`;
PG 要把这个 unicode 转义翻译成库编码,而 `SQL_ASCII` 无法表示非 ASCII 字符:

```
psycopg.errors.UntranslatableCharacter: unsupported Unicode escape sequence
DETAIL:  Unicode escape value could not be translated to the server's encoding SQL_ASCII.
```

这是**服务端**限制,改 `client_encoding` 解决不了。
**修法**:`db.session_repository.jsonb()` 统一用 `ensure_ascii=False` 序列化。

### 3.3 `TEXT` 列在 `SQL_ASCII` 库上返回 `bytes`

psycopg3 的 `TextLoader` 对 `ascii` 编码有特殊处理 —— 返回 `bytes` 而不是 `str`
(见 `psycopg/types/string.py`)。
**修法**:`db.session_repository.text_value()` 统一解码;连接一旦按 §3.1 设成 UTF-8,
TEXT 列就直接是 `str`,该函数两种都能处理。

---

## 4. 本次**没有**做的事(阶段 C / D / E,按要求不执行)

| 阶段 | 内容 | 状态 |
|---|---|---|
| C | 删 `config.py` 的 SQLite 分支、`graph.py` fail-fast、改 77 处 `build_graph()` 调用点、改 `agent_gate`、删 SQLite 依赖 | **未执行** |
| D | `SessionStore` 走 repository、新增 `GET/PATCH/DELETE /api/sessions`、`app.js` 改服务端列表、导入旧 `session_state.json` | **未执行** |
| E | 断连演练、归档并移走旧 SQLite / JSON、删 `langgraph` 库 | **未执行** |

⇒ 运行时**仍然**是 SQLite 检查点(开发默认),`config.CHECKPOINTER_BACKEND` 未删。
本次只把**库名**统一到 `video_workflow`,并把应用层表就位。

### 遗留的已知问题(不在阶段 A / B 范围,留待后续)

1. `workflow_runs.updated_at` 存量行的时区语义按 UTC 解释(方案 §11 U1),
   若历史写入其实是本地时间,改完会有偏移,需人工核对一次。
2. `langgraph` 库仍在容器里(内容已搬空到 `video_workflow`),按方案 §11 U4
   阶段 E 验收通过后再删。

### 顺带修掉的一个既有缺陷(不属于阶段 A / B,但在本次改动文件里)

`monitoring/runs_table.py` 的降级路径原本**没有设连接超时** ⇒ Postgres 不可达时
每次 `upsert_run` / `list_suspended` 都要阻塞 libpq 默认的 ~2 分钟。
台账在节点边界被调用,连着几次就能把工作流拖死。

**修法**:`db/connection.py` 给所有连接加 `connect_timeout`
(默认 10 秒,可用环境变量 `POSTGRES_CONNECT_TIMEOUT` 覆盖)。
实测降级路径从 ~120s 降到 10.1s,ERROR 日志与告警计数行为不变。

> 这个超时**不覆盖** LangGraph saver 的连接 —— saver 走自己的
> `AsyncPostgresSaver.from_conn_string`,本次没动(阶段 C 的范围)。

# auto-video-editor PostgreSQL 统一改造 —— 阶段 C / D / E 执行记录

> 对应设计文档:`docs/修正/auto-video-editor_PostgreSQL统一改造_设计开发方案.md`
> 阶段 A / B 的执行记录见:`docs/修正/auto-video-editor_PostgreSQL统一改造_阶段AB执行记录.md`
> 本次执行 **阶段 C(工作流检查点统一)/ D(会话与前端)/ E(验收与清理)**。

| 项 | 值 |
|---|---|
| 执行日期 | 2026-10-29 ~ 2026-10-30 |
| 代码基线 | `bba8e7b`(阶段 A/B 提交,改造前 `main` HEAD) |
| 目标库 | `video_workflow`(本机容器 `edb-postgres16`,`edb_pgdata` 卷) |
| 源库 | `langgraph`(阶段 E 按 U4 策略处置) |

---

## 0. 阶段总览

| 阶段 | 内容 | 门禁 | 结果 |
|---|---|---|---|
| **C** | 删 SQLite 分支;`build_graph()` fail-fast;`run_workflow.py` 删同步分支;改全部 `build_graph()` 调用点;改 `agent_gate`;删 SQLite 依赖 | 无 `.sqlite` 新文件生成;`agent_gate` 可在 Postgres 下启动 | ✅ |
| **D** | `SessionStore` 走 repository;新增 `GET/PATCH/DELETE /api/sessions`;`app.js` 改服务端列表;`session_state.json` 导入 | §7.2 集成用例 3-7 全过 | ✅ |
| **E** | 全量回归 + 基线比对;断连演练;归档旧 SQLite / JSON;`langgraph` 库按策略删除 | §7.3 扫描干净;§7.2 全过;断连不落本地文件 | ✅ |

**验收关键数字**

| 项 | 值 |
|---|---|
| 全量回归 | 基线 1224 passed → 改造后 **1275 passed**;failed **0**;skip 列表逐条比对**零差异** |
| `agent_gate` 套件(单独跑) | **218 passed / 17 skipped / 0 failed** |
| 断连演练 | **7/7 通过**,退出码 0 |
| 会话 API 集成用例 | **39 passed**,生产库 `chat_sessions` 行数**不变**(10) |
| 静态扫描 | 残留命中项**全部是故意保留的**(一次性工具 / 只读兼容路径 / 护栏用例) |
| 旧数据处置 | 75 个 SQLite 文件 + 10 个 JSON 会话文件已归档;`langgraph` 库已 `pg_dump` 归档后删除 |
| 本轮新发现并修复的缺陷 | **5 个**(A~E,见 §2 与 §5) |

---

## 1. 阶段 C:工作流检查点统一

### C-1 `config.py` —— 删掉后端选择机制

| 改动 | 位置 | 说明 |
|---|---|---|
| 删 `CHECKPOINTER_BACKEND` | 原 `:366` | 运行时不再"按环境选后端" |
| 删 `CHECKPOINTER_DB_DIR` | 原 `:367` | `checkpoints/` 目录不再被任何代码写入 |
| 删 `resolve_checkpointer_backend()` | 原 `:382-389` | 同上 |
| `make_checkpointer()` 重写 | `:366-395` | 只保留 `"memory"` 分支;`"sqlite"` 现在抛 `ValueError`;`"postgres"` 抛 `RuntimeError` 并指向 `graph.get_checkpointer()` |
| 模块 docstring 改写 | `:1-13` | 改成"PostgreSQL 唯一后端" |

### C-2 `graph.py` —— 未注入 saver 直接失败

- `build_graph()`(`:466-522`):`checkpointer is None` ⇒ 抛 `RuntimeError`,不再自动兜底。
  动机(方案 §8 风险表):自动兜底会"悄悄退化成无持久化",挂起任务查不到却没人知道。
- 新增 `graph.normalize_thread_id(thread_id)`(`:410-450`):把 thread_id 归一成 `str`,
  **拒绝 bytes**。见 §2 的"线程分裂"缺陷。
- `get_checkpointer()`(`:416-466`):进入前调 `db.connection.configure_json_dumps()`(见 §3)。

### C-3 `scripts/run_workflow.py` —— 删开发 SQLite 分支

- 删 `_invoke_sync()`(原 `:254-267`)。
- `_main_async()` 统一走 `_invoke_async()`(异步 Postgres + `ainvoke`)。
- `_invoke_async()` 启动前先跑应用层迁移(`apply_migrations_to_db()`),失败即中止(fail-closed)。
- thread_id 走 `normalize_thread_id()` 归一。

### C-4 全部 `build_graph()` 调用点

改造前 `build_graph()` 在 26 个文件里被调用 75 次(方案 §3.2 A3 记的是"77 处 / 30 文件")。
逐个核对后,需要改的是**没有显式注入 checkpointer** 的那些:

| 位置 | 改法 |
|---|---|
| `scripts/assembly/phase5_e2e.py:325` | 注入 `InMemorySaver()`(纯拓扑断言脚本) |
| `scripts/_smoke_auto_storyline.py:76` | 已有 `InMemorySaver()`,无需改 |
| `tests/unit/test_draft_path_degradation.py:89` | 注入 `InMemorySaver()` |
| `tests/integration/test_graph_compiles.py:33` | 条件分支去掉,恒定注入 `InMemorySaver()` |
| `tests/integration/test_preflight_windows.py` | 注入 `InMemorySaver()` |
| `tests/integration/test_no_autoclean_cache.py:77` | 注入 `InMemorySaver()` |
| `tests/integration/test_no_auto_import_video.py:110` | 注入 `InMemorySaver()` |
| `tests/integration/test_state_field_compatability.py:132,257` | 注入 `InMemorySaver()` |
| `tests/integration/test_week4_graph.py`(6 处) | 注入 `InMemorySaver()` |

其余调用点本来就显式传了 `checkpointer=InMemorySaver()` 或 Postgres saver,无需改。

### C-5 `agent_gate/` —— 拆掉 SQLite 假设

| 位置 | 改法 |
|---|---|
| `agent_gate/runner.py:checkpoint_path()` | **删**。改成 `checkpoint_exists(thread_id)`:查 Postgres 的 `checkpoints` 表,文本版 + `\x<hex>` 版两套 thread_id 都扫 |
| `agent_gate/runner.py:production_blocker()` | 删掉"`WORKFLOW_ENV=production` 或 backend=postgres 就拒绝"的规则(原注释"本方案只验收 SQLite 本地开发路径"的前提已不存在);改成**正向校验**:残留的旧 `CHECKPOINTER_BACKEND=sqlite` 环境变量直接拒绝 |
| `agent_gate/runner.py:resume_pending_sync()` | 保留(worker 是**同步进程**,用同步 `PostgresSaver`);docstring 改写 |
| `agent_gate/runner.py` 新增 `make_checkpointer()` / `close_checkpointer()` / `checkpointer_session()` | 构造**同步** `PostgresSaver`(单条连接,不用连接池 —— 池的 worker 线程在进程退出时会打噪声) |
| `agent_gate/fixtures/toy_graph.py` | 默认 saver 从 `SqliteSaver` 改成 Postgres(与 worker 连同一个库,这样另起一个图也能读回 worker 写的检查点) |
| `agent_gate/tests/conftest.py` | 新增**独立测试库** `video_workflow_agent_gate_test` + 每个用例前后 TRUNCATE 检查点表 ⇒ 用例之间完全隔离,又不污染工作库 |
| `agent_gate/tests/test_input_guard.py` | 3 个护栏用例按新语义重写 |
| `agent_gate/tests/test_lifecycle.py` | `test_checkpoint_path_follows_env` → `test_checkpoint_exists_queries_postgres` |

**为什么 agent_gate 的测试要用独立库**:跨进程场景(崩溃后继续 / 等待人工放行后恢复)本来就需要
真持久化 —— 以前靠 toy graph 的 SQLite 文件,现在只能靠 Postgres。若直接用工作库
`video_workflow`,测试会在真实 checkpoint 上写数据并可能删掉用户的会话。

### C-6 依赖清理

- `pyproject.toml:19` 删 `langgraph-checkpoint-sqlite>=2.0`。
- `requirements.txt:24-27` 删 `langgraph-checkpoint-sqlite>=2.0`。
- `README.md` 的 checkpointer 章节、目录树、验证安装命令、URI 表同步改写。

---

## 2. 改造过程中发现的既有缺陷(不在原计划里,但必须修)

### 缺陷 A —— `SQL_ASCII` 库写中文 JSONB 直接失败 ⛔

**症状**:`agent_gate` 的 worker 写含中文的 checkpoint 时崩掉:
```
psycopg.errors.UntranslatableCharacter: unsupported Unicode escape sequence
DETAIL: Unicode escape value could not be translated to the server's encoding SQL_ASCII.
CONTEXT:  JSON data, line 1: ...\"script\": \"\u4e24...
```

**根因(两层)**:

1. **写侧**:本机容器 `edb-postgres16` 的库编码是 `SQL_ASCII`(Windows 上没设 `LANG`)。
   PG 写 JSONB 时要把 JSON 里的 unicode 转义(`\u4e24`)翻译成库编码,而 `SQL_ASCII`
   表示不了非 ASCII ⇒ 直接失败。
   psycopg3 默认 `ensure_ascii=True` ⇒ 中文一律被写成 `\uXXXX` ⇒ **必炸**。
   方案 §3.2 A5 只覆盖了应用自己的 `db/session_repository.jsonb()`,
   **漏了 LangGraph 的 saver** —— `langgraph/checkpoint/postgres/__init__.py:341-342`
   用的是 `Jsonb(copy)` / `Jsonb(metadata)`,不带 dumps 参数 ⇒ 默认 `ensure_ascii=True`。
2. **读侧**:`client_encoding` 跟随库编码 = `SQL_ASCII` ⇒ psycopg3 把 TEXT 列当
   **bytes** 返回 ⇒ LangGraph resume 时读回的 `thread_id` 是 bytes ⇒ 再写回 TEXT 列时
   psycopg 渲染成 `\x<hex>` 字面量 ⇒ **同一逻辑线程分裂成两套 thread_id**
   (方案 §3.1 M2 记的那个现象的另一半)。

**处置(两处都改)**:

1. **写侧**:`db/connection.py` 新增 `configure_json_dumps()`:
   ```python
   set_json_dumps(lambda obj, context=None: json.dumps(obj, ensure_ascii=False))
   ```
   `psycopg.types.json.set_json_dumps` 是**进程级全局**设置 ⇒ saver 内部新建的
   `Jsonb(...)` 也吃这一套。调用点:`graph.get_checkpointer()`、
   `agent_gate.runner.make_checkpointer()`。
2. **读侧**:新增应用迁移 `003_set_client_encoding_utf8`:
   ```sql
   ALTER DATABASE <current_database> SET client_encoding TO 'UTF8';
   ```
   - 库**存储**编码不变(仍是 `SQL_ASCII`,服务端不做任何字符集转换);
   - 只是让**新连接**默认 UTF-8 ⇒ TEXT / JSONB 直接给 `str`,不用手动 decode;
   - 顺带**根治**了上面的 thread_id 分裂:resume 时读回的 thread_id 现在是 `str`,
     写回去还是文本版。

**验收(实测)**:

| 验证项 | 结果 |
|---|---|
| `AsyncPostgresSaver` 写中文 checkpoint | ✅ `{'script': '两段式剪辑：先粗剪再精修', 'note': '中文测试'}` 原样读回 |
| `PostgresSaver`(同步)写中文 checkpoint | ✅ |
| resume 后 `checkpoints` 表的 thread_id 分布 | ✅ **只有文本那一套**(2 行),没有 `\x<hex>` |
| `video_workflow` / `video_workflow_agent_gate_test` 的 `client_encoding` | ✅ `UTF8`(存储编码仍是 `SQL_ASCII`) |

> 这条缺陷在阶段 A/B 之后、阶段 C 之前就存在,只是以前 SQLite 路径绕开了它;
> 统一到 Postgres 后必须显式修掉,否则任何含中文的工作流都跑不动。

---

## 3. 阶段 D:会话与前端

### D-1 会话状态落 PostgreSQL(`openstoryline/agent_fastapi.py`)

| 改动 | 说明 |
|---|---|
| `ChatSession.__init__` 增加 `repo` 参数 | 没有就落到模块级默认仓储(`_default_session_repository()`) |
| `ChatSession.save_state_atomic()` | **不再写 `session_state.json`**;改成 `repo.update_state(...)`;**失败抛异常**(fail-closed) |
| `ChatSession.load_from_state()` | 改成从仓储读 `state_json`;**还原逻辑一行没改**(`_ensure_lc_messages_invariants` / `_close_unfinished_tool_calls_in_lc_messages` / `_sanitize_tool_protocol_in_lc_messages` / `_normalize_running_tool_records` / `_rebuild_tool_history_index` 顺序照旧) |
| `ChatSession._read_legacy_state_file()` | 新增:旧 JSON 文件只读兼容路径 —— 库里没这行时读旧文件并用 `create_if_absent` 幂等导入 |
| `SessionStore.__init__(cfg, repo=None)` / `.repo` / `.forget(sid)` | 仓储由 store 持有;`forget()` 用于软删除后清掉内存缓存 |
| `SessionStore.create()` | 建会话时同时插 `chat_sessions` 一行 |
| `SessionStore.get()` | 缓存未命中时从仓储加载;**保留**原有 3 次重试 + `SessionStateUnavailableError` → 503 的语义 |
| `SessionStore.save_session_state()` | **不再吞异常**。原来 `except Exception → logger.warning` 就是"静默丢数据"的根因,现在直接抛;新增 `@app.exception_handler(SessionStateUnavailableError)` → **503** |
| `lifespan()` | 构造 / 释放仓储与连接池 |

### D-2 新增 REST 接口

| 方法 | 路由 | 说明 |
|---|---|---|
| `GET` | `/api/sessions` | 游标分页(`limit` 1..200 默认 50、`cursor`、`title_prefix`、`include_deleted`);按 `updated_at DESC, session_id DESC`;响应**不含 `state_json`**;`next_cursor` 为 `null` 表示到底了 |
| `PATCH` | `/api/sessions/{id}` | 改标题;`title_source` 可选(`auto` / `manual`,默认 `manual`) |
| `DELETE` | `/api/sessions/{id}` | 软删除(`deleted_at`);可用 `repo.restore()` 恢复 |

限流:`_match_http_rule()` 新增 `list_sessions` 规则(burst 10 / rpm 30),
在通用 `/api/` 兜底之前匹配 —— 侧栏刷新历史不会把 `api_general` 桶打满。
`POST /api/sessions` 仍是 `create_session`,`GET /api/sessions/{id}` 仍是 `api_general`。

### D-3 前端以服务端为唯一来源(`openstoryline/web/static/app.js`)

| 改动 | 说明 |
|---|---|
| `ApiClient.listSessions()` / `renameSession()` | 新增,沿用文件里既有的 `fetch` + `HttpError` 约定 |
| `bootstrap()` | 改成 `await _loadSessionListFromServer()` → `await _reconcileLegacySessionTitles()` |
| `_saveSessionHistory()` **删除** | 不再是权威列表;`SESSION_LIST_KEY` 降级为"一次性对账输入" |
| `_readLegacySessionList()` | 只读一次;对账完就 `localStorage.removeItem(SESSION_LIST_KEY)` |
| 对账规则 | **只有服务端已存在**的 session id 才会被改标题;**绝不用客户端 id 去服务端建会话** |
| 自动标题回写 | `_maybePushAutoTitle()` → `PATCH /api/sessions/{id}` 且 `title_source="auto"` ⇒ 之后还能被新的自动标题更新 |
| 手动标题优先 | `title_source === "manual"` 的会话不写回自动标题 |
| `has_user` 推导 | `has_user = message_count > 0`(服务端 `message_count` 就是 `len(history)`;新会话 history 为空 ⇒ 0 ⇒ 空白会话) |
| 失败可见 | `GET /api/sessions` 失败 ⇒ 清空列表 + `sessionListError` + 侧栏显示 `sidebar.history_error`;**不**显示旧本地列表 |

### D-4 旧 `session_state.json` 导入(`scripts/import_session_states.py`)

一次性 CLI:扫两个 outputs 根目录 → 解析 → 复用脱敏逻辑 → `create_if_absent` 幂等写入。

实测(本机):

```
dry-run : imported=10 skipped=0 failed=0   exit 0
run #1  : imported=10 skipped=0 failed=0   exit 0
run #2  : imported=0  skipped=10 failed=0  exit 0   ← 幂等
```

导入后 `chat_sessions` 里 10 行的 `state_json` 与源文件一致,
`custom_llm_config` / `custom_vlm_config` / `tts_config` 全部为空或 `{}`
—— **没有 API key 残留**(用 `sk-[A-Za-z0-9_-]{20,}` 正则全表扫过)。

---

## 4. 阶段 E:验收与清理

### 4.1 门禁验收总表(方案 §9 阶段 E)

| 门禁项 | 判定标准 | 实测结果 |
|---|---|---|
| §7.3 静态扫描 | 应用启动 / 会话保存 / 工作流执行路径不再引用 SQLite / JSON 文件 | ✅ 见 §4.5 |
| §7.4 全量回归 | 与基线逐条比对 skip 列表,差异只允许来自新增用例 | ✅ 见 §4.2 |
| §7.2 用例 3–7 | 会话跨实例可见 / 列表 / 改名 / 软删除 / 刷新仍在 | ✅ 39 条集成用例通过 |
| §7.2 用例 8 | 断连时明确报错,**不**落本地文件 | ✅ 7/7,见 §4.4 |
| §7.2 用例 9 | 并发更新不静默覆盖 | ✅ `version` 乐观锁,冲突抛 `SessionVersionConflict` |
| §7.2 用例 11 | 新写入只产生文本 thread_id | ✅ 见 §2 缺陷 A |
| 无 `.sqlite` 新文件 | 改造后跑完全流程,`checkpoints/` 保持为空 | ✅ 归档后为 0 |
| `agent_gate` 可在 Postgres 下启动 | agent_gate 套件在独立测试库上全绿 | ✅ 218 passed / 0 failed,见 §4.3 |

### 4.2 全量回归 + 基线比对(方案 §7.4)

**基线怎么取的**:`pyproject.toml` 的 `testpaths = ["tests"]` 只收 `tests/`,不含 `agent_gate/tests/`
⇒ agent_gate 单独跑(见 §4.3)。基线用 `git worktree add --detach` 取**改造前**的提交:

```powershell
git worktree add --detach .worktrees\baseline-cde bba8e7b   # 阶段 A/B 提交 = 改造前 main HEAD
```

⚠️ `inputs/` 被 `.gitignore` 忽略 ⇒ 新 worktree 里没有素材,依赖 `inputs/30s.mp4` 的用例会
**被 skip** ⇒ skip 列表不可比。必须把素材拷进去(本机 2 个:`30s.mp4`、`chain-test-12s.mp4`)。

> ⚠️ Windows 上**不要**用 `git archive | tar -x` 取基线 —— 会破坏中文路径。

| | 基线(`bba8e7b`) | 改造后 | 改造后(清理 + 修测试后复跑) |
|---|---|---|---|
| tests 总数 | 1236 | 1287 | 1287 |
| passed | 1224 | 1275 | **1275** |
| skipped | 5 | 5 | **5** |
| xfailed | 3 | 3 | **3** |
| xpassed | 4 | 4 | **4** |
| **failed / error** | **0** | **0** | **0** |
| 耗时 | 229.56s | 290.22s | 324.79s |

第三列是"阶段 C/D 改完后立即跑",第四列是"阶段 E 清理完 + 修了集成测试污染生产库之后复跑"
⇒ **两轮结果完全一致**,说明阶段 E 的清理动作(归档旧文件 / 删 `langgraph` 库 / 清测试数据)
没有影响任何用例。

**skip 列表逐条比对**(用 `logs/_compare_junit.py` 比对两次 junit XML):

```
baseline: tests=1236 passed=1228 skipped=8 failed=0
after   : tests=1287 passed=1279 skipped=8 failed=0

新增失败 0 条
新增 skip 0 条
不再 skip 0 条
新增通过 56 条 / 不再通过 5 条
```

- **skip 列表完全一致**(8 = 8,逐条比对零差异)⇒ 没有用例被"意外跳过"。
- **零新增失败** ⇒ 无回归。
- **净增 51 条通过**,新增通过 56 条、少了 5 条 —— 少的这 5 条**全部是 SQLite 专属用例**,
  按 D2 有意删除:

  | 被删除的用例 | 文件 |
  |---|---|
  | `test_make_checkpointer_sqlite_returns_sqlite` | `tests/integration/test_graph_compiles.py` |
  | `test_graph_compiles_with_sqlite_checkpointer` | `tests/integration/test_graph_compiles.py` |
  | `test_multi_day_resume_via_sqlite_persistence` | `tests/integration/test_interrupt_resume.py` |
  | `test_checkpoint_roundtrip_on_sqlite` | `tests/integration/test_unreachable_nodes_wiring.py` |
  | `test_resume_all_three_checkpoints_via_sqlite_persistence` | `tests/integration/test_week5_resilience.py` |

  (方案 §7.4 提醒:净增数 ≠ 新增用例数 —— 改写既有测试文件时用例数会变,必须逐条比对。)

### 4.3 `agent_gate` 套件(必须单独跑)

`agent_gate/tests/` **不在** `testpaths = ["tests"]` 里 ⇒ 默认 `pytest` 收不到,必须显式指定:

```powershell
.\.venv\Scripts\python.exe -m pytest agent_gate\tests --junitxml=logs\after_agent_gate2.xml
```

| | 用例总数 | passed | skipped | failed |
|---|---|---|---|---|
| 改造后(干净环境) | 235 | 218 | 17 | **0** |

改造后 agent_gate 的 worker **只**用 Postgres checkpointer;测试用**独立测试库**
(`video_workflow_agent_gate_<随机后缀>`,每个用例前后 TRUNCATE 检查点表),不碰工作库。

### 4.4 断连演练(方案 §7.2 用例 8)

```powershell
.\.venv\Scripts\python.exe scripts\drill_disconnect.py
```

目标 URI 指向一个没人监听的端口(`127.0.0.1:59999`)⇒ 连接必然失败。

| # | 演练项 | 结果 |
|---|---|---|
| 1 | `build_graph()` 未注入 checkpointer ⇒ `RuntimeError` | ✅ |
| 2 | `graph.get_checkpointer()` 建表 ⇒ 明确报错(`ConnectionTimeout`) | ✅ |
| 3 | `ChatSession.save_state_atomic()` ⇒ 抛 `SessionStateUnavailableError` | ✅ |
| 3 | 未写回 `session_state.json`(临时目录) | ✅ |
| 3 | 未写回 `session_state.json`(**真实** outputs 目录) | ✅ |
| 4 | `SessionStore.save_session_state()` ⇒ 抛异常(不再静默吞掉) | ✅ |
| 5 | 全程没有新建 `.sqlite` 文件 | ✅ |

**通过 7 项,失败 0 项,退出码 0。**

### 4.5 静态扫描(方案 §7.3)

```powershell
rg -n "SqliteSaver|langgraph\.checkpoint\.sqlite|sqlite3\.connect|CHECKPOINTER_BACKEND|CHECKPOINTER_DB_DIR|session_state\.json|openstoryline_session_list_v1" --glob '!docs/**' --glob '!logs/**' .
```

剩余命中项**全部是故意保留的**,没有一处是运行时路径:

| 位置 | 性质 |
|---|---|
| `config.py:9`、`agent_gate/runner.py:385-391` | 文档说明 + **拒绝**残留的 `CHECKPOINTER_BACKEND=sqlite` 环境变量(正向校验) |
| `agent_gate/tests/test_input_guard.py` | 护栏用例本身要提这个变量名 |
| `scripts/retire_sqlite_checkpoints.py`、`scripts/import_session_states.py`、`scripts/drill_disconnect.py` | **一次性迁移 / 演练工具**,与运行时隔离并显式标注 |
| `openstoryline/agent_fastapi.py:107`、`:1953` | **只读**兼容路径(库里没这行才读旧文件) |
| `openstoryline/web/static/app.js:15` | `SESSION_LIST_KEY` 已降级为"一次性对账输入",不再当权威目录 |
| `db/session_repository.py:444` | 文档字符串(指向导入工具) |

### 4.6 旧数据处置(D2 / D3 / U4)

**旧 SQLite checkpoint(D2 —— 不迁移,弃用归档)**

```powershell
.\.venv\Scripts\python.exe scripts\retire_sqlite_checkpoints.py --apply
```

| 项 | 值 |
|---|---|
| 主文件 `.sqlite` | 27 个 |
| 侧文件 `-shm` / `-wal` | 48 个 |
| 合计 | 75 个 / 8.34 MiB |
| 归档位置 | `logs/retired_checkpoints/2026-10-30/`(已 gitignore) |
| 清单 | `retirement_manifest.json`(文件名 / 大小 / thread_id / checkpoint 数) |
| `checkpoints/` 目录 | **保留但清空** |

**"只存在于旧 SQLite"的 thread_id 判定**:不能只看 SQLite 侧 —— 同一个 thread_id 可能两边都有
(阶段 A/B 搬库之后又在旧路径跑过一次)。所以脚本会**两边都查**,只在 Postgres 侧确实没有该
thread_id 时才把 `workflow_runs` 里 `running` / `suspended` 的行标成 `failed`(需重跑)。

本机实测:`sqlite_thread_ids` 25 个,`postgres_thread_ids_count` 62 个,**交集为空**
⇒ 25 个全是 SQLite 独有;但 `workflow_runs` 里只有 `video-wiring-001`(它 Postgres 侧有 checkpoint)
⇒ **标记 0 行**(符合预期)。

**旧 `session_state.json`(D3 —— 迁移后移出活动路径)**

```powershell
.\.venv\Scripts\python.exe scripts\import_session_states.py --archive
```

| 项 | 值 |
|---|---|
| 归档文件 | 10 个(`outputs/` 2 个 + `openstoryline/outputs/` 8 个) |
| 归档位置 | `logs/retired_session_states/2026-10-30/`(保持相对结构,便于追溯) |
| 活动路径残留 | **0 个** |

⚠️ **只有库里确实有这一行才会被移走**(`repo.get(sid) is not None`);解析失败 / 入库失败的
文件一律留在原地并记进 `retirement_manifest.json` 的 `kept` 列表 ⇒ 不会真丢数据。

**`langgraph` 库处置(U4 —— 阶段 E 验收通过后删,删前留 `pg_dump` 归档)**

1. **先核对**:`langgraph` 的 315 个 `checkpoint_id` **全部**能在 `video_workflow` 里找到(缺失 0 个)
   ⇒ 数据确实搬过去了,删库不会丢数据。
2. **再归档**:`pg_dump -Fc` → `logs/retired_databases/langgraph_final_2026-10-30.dump`(184 KB)。
3. **最后删库**:`DROP DATABASE langgraph WITH (FORCE)`。

最终数据库清单:`postgres` / `template0` / `template1` / **`video_workflow`** —— 运行时只有一个库。

| `video_workflow` 最终表计数 | |
|---|---|
| `checkpoints` | 910 |
| `checkpoint_blobs` | 3428 |
| `checkpoint_writes` | 12220 |
| `chat_sessions` | **10**(全部是导入的真实会话,见 §5 缺陷 B) |
| `workflow_runs` | 1 |
| `app_schema_migrations` | 3 |

---

## 5. 缺陷汇总(A ~ E)

> A = 阶段 C 发现的既有缺陷(详见 §2);B ~ E = 阶段 E 验收过程中新发现并当场修掉的。

### 缺陷 A —— `SQL_ASCII` 库写中文 JSONB 直接失败 ⛔(详见 §2)

已在 **§2** 记录(阶段 C 发现的既有缺陷,写侧 `ensure_ascii=False` + 读侧库级
`client_encoding=UTF8` 双修)—— 此处不重复。

### 缺陷 B —— 集成测试直接打生产库,把垃圾会话写进用户的库 ⛔

**症状**:阶段 E 清点 `chat_sessions` 时发现 49 行,其中 **39 行是测试垃圾**
(标题形如 `分页912dc3df-a336`、`独特前缀ABC`、`中文标题`、`我手动改的`),
`created_at` 集中在三次测试运行的时间点上。

**根因**:`tests/integration/test_session_repository_postgres.py`(阶段 B 写的文件)的仓储用
`SessionRepository(uri=TEST_URI)`,而 `TEST_URI` = `TEST_POSTGRES_URI` / `POSTGRES_URI` 环境变量,
**都没有** ⇒ 落到 `config.POSTGRES_URI` = **生产库 `video_workflow`**。
该文件会真的 `create` / `rename` / `soft_delete`,而且**从不清理**。

同目录的 `test_sessions_api_postgres.py` 当时是**对的**(自带独立测试库),所以问题被掩盖了。

**为什么必须修**:这不只是"测试不干净"。它在**用户的生产库里建会话、改标题、软删除**,
还会让"标题前缀过滤"这类用例翻到别的用例留下的行 ⇒ 用例之间互相干扰,测试结果不可信。

**处置**:
1. 新增 `tests/integration/conftest.py` —— 统一的隔离测试库工厂
   (`postgres_test_db` / `isolated_postgres_db`),库名带**每次运行的随机后缀**,
   会话结束时自动删库;连不上 Postgres ⇒ 直接 **fail**(不静默 skip)。
2. `test_session_repository_postgres.py` 改用隔离库(`repo` 夹具依赖 `db_uri`)。
3. `test_sessions_api_postgres.py` 删掉自己那份重复实现,改用共享工厂。
4. 清掉已污染的 39 行(先 `pg_dump -t chat_sessions` 备份到 `logs/stage_e_cleanup/`)。

**验收**:

```
BEFORE chat_sessions count: 10
.......................................                                  [100%]
39 passed, 1 warning in 21.91s
AFTER chat_sessions count: 10          ← 生产库一个字节都没动
leftover test DBs: 0
```

### 缺陷 C —— 上一轮崩掉的测试留下孤儿 worker 进程,让 `agent_gate` 假失败 ⚠️

**症状**:阶段 C/D 那一轮结束后,`agent_gate` 套件有 2 个失败:

| 用例 | 报错 |
|---|---|
| `test_mcode_mcp_tool_timeout_comes_from_mcp_json` | `assert 'timed out' in 'tool nap$ not found'` |
| `test_e2e_crash_then_continue_replays_answers` | `IndexError: list index out of range` |

**根因**:上一轮测试崩在中途,留下了两个**孤儿 worker 进程**
(`python -m agent_gate worker --thread-id job1`,启动时间 00:45,进程一直存活)。
它们占着同一个 `thread_id`(`job1`),新用例拉起 worker 时撞上"已有一个在跑",
拿到的不是自己期望的工具响应 ⇒ 断言失败。**跟 PostgreSQL 改造本身无关。**

**处置**:杀掉孤儿进程后重跑,两个用例**都通过**:

```
218 passed, 17 skipped in 470.34s (0:07:50)
  PASS agent_gate.tests.test_agent_security :: test_mcode_mcp_tool_timeout_comes_from_mcp_json
  PASS agent_gate.tests.test_gate :: test_e2e_crash_then_continue_replays_answers
```

> 教训:`agent_gate` 的用例会拉起常驻 worker 子进程。**测到一半中断 ⇒ 必须先清孤儿再重跑**,
> 否则会把"环境脏"误判成"代码坏"。

### 缺陷 D —— 演练脚本踩到"OpenStoryline 路径全是相对 cwd"的坑 ⚠️

**症状**:断连演练第一次跑直接崩:

```
AttributeError: 'NoneType' object has no attribute 'llm'
  File "openstoryline\agent_fastapi.py", line 201, in _peek_builtin_model_name
```

**根因**:OpenStoryline 的路径**全部相对当前工作目录**解析:

| 东西 | 相对路径 |
|---|---|
| 配置 | `default_config_path()` → `"config.toml"` |
| prompt 模板 | `prompts\tasks\instruction\<lang>\system.md` |

真实服务是以 `openstoryline/` 为 cwd 启动的(仓库里 `tests/unit/test_session_store_repository.py`
的 `_chdir_openstoryline` 夹具就是干这个的)。演练脚本从仓库根目录跑 ⇒ 配置和模板都找不到。

**处置**:`scripts/drill_disconnect.py` 里切到 `openstoryline/` 再跑演练(结束时切回来),
并且拿不到配置时**明确报错**而不是静默返回 `None`。

**顺带修的**:演练脚本原来只扫**临时目录**下的 `session_state.json` —— 而
`ChatSession` 用的是真实 `outputs_dir`,等于**根本没测到**。现在改成"演练前后对比真实
outputs 目录的新增文件",并且 `.sqlite` 检查也改成"对比存量"而不是"目录必须为空"
(归档前仓库里本来就有 75 个历史文件)。

### 缺陷 E —— 归档脚本把"SQLite 里有"当成"SQLite 独有" ⚠️

**症状**:`retire_sqlite_checkpoints.py` 的 `sqlite_only_thread_ids` 直接取了 SQLite 侧的
全部 thread_id。

**风险**:同一个 thread_id 可能两边都有(阶段 A/B 搬库后又在旧路径跑过一次),
那种情况**不能**标 `failed` —— 会让一个其实还有 Postgres checkpoint 的挂起任务变成"需重跑"。

**处置**:脚本现在**两边都查**,只在 Postgres 侧确实没有该 thread_id 时才标记;
Postgres 查询失败时**跳过标记**(宁可不动 `workflow_runs`,也不误标)。

---

## 6. 回滚与后续

### 6.1 回滚路径(方案 §10)

| 场景 | 动作 |
|---|---|
| 新版本尚未产生新数据 | 恢复旧版本 + 恢复 `logs/retired_databases/langgraph_final_2026-10-30.dump` |
| 旧 SQLite / 旧 JSON 还在活动路径 | 从 `logs/retired_checkpoints/<日期>/`、`logs/retired_session_states/<日期>/` **移回** |
| `chat_sessions` 表误删 | 从 `logs/stage_e_cleanup/chat_sessions_pre_cleanup.dump` 恢复 |
| 新版本已写入新数据 | 必须先反向导出新增数据,或明确接受回到切换点的数据损失 —— **不能直接启旧版并假定旧文件自动包含新数据** |

### 6.2 本次实际产出的可回滚物

| 归档物 | 位置 | 内容 |
|---|---|---|
| 旧 SQLite checkpoint | `logs/retired_checkpoints/2026-10-30/` | 75 个文件 + `retirement_manifest.json` |
| 旧 `session_state.json` | `logs/retired_session_states/2026-10-30/` | 10 个文件 + `retirement_manifest.json` |
| 旧 `langgraph` 库 | `logs/retired_databases/langgraph_final_2026-10-30.dump` | `pg_dump -Fc` |
| `chat_sessions` 清理前快照 | `logs/stage_e_cleanup/chat_sessions_pre_cleanup.dump` | `pg_dump -t chat_sessions` |
| 回归基线 / 改造后 junit | `logs/baseline_full.xml`、`logs/after_full_final.xml` | 逐条比对依据 |

(`logs/` 已 gitignore ⇒ 这些不进版本库,备份在本机磁盘。)

### 6.3 未决事项的处置(方案 §11)

| # | 事项 | 本次处置 |
|---|---|---|
| U1 | `workflow_runs.updated_at` 时区语义 | 存量按 UTC 解释并记录;迁移 003 把**新连接**的 `client_encoding` 设成 UTF8,时区语义不受影响 |
| U2 | 是否拆 `messages` 表 | **不拆**(本期一张表 + `state_json`);观察膨胀后再拆 |
| U3 | 软删除的物理清理 | **只软删不物理删**;`repo.restore()` 可恢复 |
| U4 | `langgraph` 库何时删 | **阶段 E 验收通过后删**;删前已留 `pg_dump` 归档(§4.6) |
| U5 | 会话列表跨会话搜索 | 本期只做 `limit + cursor` 分页 + 标题前缀过滤 |
| U6 | `agent_gate` 多实例共享会话 | **不做**;只解决"能在 Postgres 下启动" |

### 6.4 遗留事项(下次开工先看)

1. **`checkpoints/` 目录现在没人写** —— 代码里已无引用,目录保留只为可回滚窗口;
   下个大版本可直接删目录本身。
2. **`session_state.json` 的只读兼容路径**(`agent_fastapi.py:1953`)现在已无源文件
   ⇒ 实际是死代码。等确认没有外部系统还在写 JSON 后可以删掉。
3. **`workflow_runs` 里 `video-wiring-001` 仍是 `running`** —— 它在 Postgres 侧有 checkpoint,
   按 D2 的规则**不**标 `failed`。若确认该任务已废弃,需要人工改状态。
4. **`agent_gate` 不在 `testpaths` 里** —— CI / 本地跑全量时**必须单独跑一遍**,
   否则会漏掉 235 条用例(而且这正是上次"假绿"的来源之一)。

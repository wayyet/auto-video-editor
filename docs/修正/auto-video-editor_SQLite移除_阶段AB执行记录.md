# auto-video-editor SQLite 彻底移除 —— 阶段 A / B 执行记录

> 对应设计文档:`docs/修正/auto-video-editor_remove_sqlite_execution_design.md`(v1,SHA256 `C3412A77…`)
> 本次**只执行阶段 A(基线与备份,T-A1 ~ T-A5)与阶段 B(盘点与核验,T-B1 ~ T-B6)**;阶段 C / D / E 未执行。

| 项 | 值 |
|---|---|
| 执行日期 | 2026-10-30 |
| 代码基线 | `d6aef8c7d87f6069f4302ec6e889bcd9bc4a5d49`(分支 `main`,未建 feature 分支) |
| 目标库 | `video_workflow`(本机容器 `edb-postgres16`,PostgreSQL 16.11) |
| 备份产物 | `logs/sqlite-removal-backup/video_workflow_before_sqlite_cleanup.dump`(633,743 B) |
| 门禁结论 | **T-B6 通过** —— 阶段 D 的数据前置条件已具备,但需先处理 §3.1 / §3.2 两条遗留 |

---

## 0. 执行前的前置修复(不属于任务卡,但不修就跑不动)

| # | 现象 | 处置 |
|---|---|---|
| 0.1 | Docker Desktop 未运行(`dockerDesktopLinuxEngine` 命名管道不存在) | 启动 Docker Desktop,等待 daemon 就绪(29.7.2) |
| 0.2 | 容器 `edb-postgres16` 状态 `Exited (0) 20 hours ago` | `docker start edb-postgres16` → `Up (healthy)`,`5432/tcp` 已映射 |
| 0.3 | 本机**未安装** PostgreSQL 客户端(`pg_dump` / `psql` 均不在 PATH) | 改用容器内自带的同版本客户端(16.11),见 §1.3 |

> 0.3 的替代方案已核实:仓库 `scripts/align_postgres.py` 的历史执行记录也采用
> `docker exec edb-postgres16 pg_dump … | docker exec -i edb-postgres16 psql …` 的容器内管道写法,
> 与本机现状一致。**未**为此安装系统级 PostgreSQL 客户端。

---

## 1. 阶段 A:基线与备份

### T-A1 冻结基线(不建分支)

按本仓长期约定(wayyet 仓库默认在 `main` 直提,不建 feature 分支、不走 PR),
因此**不执行**设计文档 §5 阶段 A-2 的 `git switch -c chore/remove-sqlite-artifacts`。

```powershell
git -C $repo rev-parse HEAD        # d6aef8c7d87f6069f4302ec6e889bcd9bc4a5d49
git -C $repo rev-parse --abbrev-ref HEAD   # main
git -C $repo status --porcelain
git -C $repo worktree list
```

结果:HEAD 与设计文档声明的审查基线**一致**;被跟踪文件树干净,唯一未跟踪项是
`docs/修正/auto-video-editor_remove_sqlite_execution_design.md`(v1 设计文档本身,本次不提交)。

产出落盘:`logs/sqlite-removal-backup/baseline_git.txt`(含 HEAD / 分支 / status / worktree 列表)。

**门禁:** HEAD = `d6aef8c7…` ✅ · 未建分支 ✅

### T-A2 建备份目录

```powershell
New-Item -ItemType Directory -Force -Path 'E:\Documents\kuaishou\auto-video-editor\logs\sqlite-removal-backup'
```

`logs/` 已被 `.gitignore:42` 忽略(`git check-ignore -v` 确认),备份产物**不会污染仓库工作区**。

**门禁:** 目录存在 ✅

### T-A3 备份 `video_workflow`

```powershell
docker exec edb-postgres16 pg_dump -U postgres -d video_workflow --format=custom \
  --file=/tmp/video_workflow_before_sqlite_cleanup.dump
docker cp edb-postgres16:/tmp/video_workflow_before_sqlite_cleanup.dump `
  'E:\Documents\kuaishou\auto-video-editor\logs\sqlite-removal-backup\video_workflow_before_sqlite_cleanup.dump'
```

| 项 | 值 |
|---|---|
| 文件大小 | 633,743 B(> 0) |
| SHA256 | `B2E196ED3CA6D3990AE822F06216BBE44DA128AB4E206F90FCFC8C972B670825` |
| 格式 | CUSTOM(gzip),Dump Version 1.15-0 |
| 来源 | PostgreSQL 16.11 / pg_dump 16.11 |
| TOC Entries | 32 |

`pg_restore --list` 回读校验,7 张业务表全部在备份内:

```
4333; 0 25237 TABLE DATA public app_schema_migrations postgres
4334; 0 25245 TABLE DATA public chat_sessions postgres
4329; 0 24940 TABLE DATA public checkpoint_blobs postgres
4330; 0 24946 TABLE DATA public checkpoint_migrations postgres
4331; 0 24949 TABLE DATA public checkpoint_writes postgres
4332; 0 24956 TABLE DATA public checkpoints postgres
4335; 0 25265 TABLE DATA public workflow_runs postgres
```

**门禁:** `Length > 0` ✅ · 归档可读、含全部表 ✅

### T-A4 确认连的是目标库

```powershell
docker exec -i edb-postgres16 psql -U postgres -d video_workflow -v ON_ERROR_STOP=1
```

| 查询 | 结果 |
|---|---|
| `current_database()` | `video_workflow` ✅ |
| `client_encoding` | `UTF8` |
| `server_encoding` | `SQL_ASCII` |
| `server_version` | `16.11` |

**门禁:** 输出 `video_workflow` ✅ · `client_encoding` 已记录 ✅

> `client_encoding=UTF8` 而 `server_encoding=SQL_ASCII`,与本仓历史结论一致
> (见 `docs/修正/auto-video-editor_PostgreSQL统一改造_阶段AB执行记录.md` §3):
> 库**存储**编码仍是 SQL_ASCII,客户端按 UTF-8 收发,服务端不做字符集转换。

### T-A5 复制 manifest 进备份目录

```powershell
Copy-Item -LiteralPath '…\logs\retired_checkpoints\2026-10-30\retirement_manifest.json' `
          -Destination '…\logs\sqlite-removal-backup\retirement_manifest_2026-10-30.json' -Force
```

源/目标 SHA256 一致:`BF7BC9341D4A0966604C2845E1F2E43D3E8097C91681FE42EAC113A9951784C5`(9,247 B)。

**门禁:** 备份目录内存在 manifest 副本 ✅

**阶段 A 收尾状态** —— 备份目录内容:

```
baseline_git.txt                        762 B
retirement_manifest_2026-10-30.json   9,247 B
video_workflow_before_sqlite_cleanup.dump  633,743 B
```

---

## 2. 阶段 B:盘点与核验

### T-B1 复核归档目录计数

归档目录 `logs/retired_checkpoints/2026-10-30/`:

| 类别 | 数量 | 字节 | 换算 |
|---|---|---|---|
| SQLite 主文件(`*.sqlite`) | 27 | 8,744,960 B | 8.34 MiB |
| sidecar(`*-wal`) | 24 | — | — |
| sidecar(`*-shm`) | 24 | — | — |
| **DB 文件合计** | **75** | **10,141,280 B** | **9.67 MiB** |
| sidecar 合计 | 48 | 1,396,320 B | 1.33 MiB |
| manifest(非 DB) | 1 | 9,247 B | — |
| **目录内文件总计** | **76** | — | — |

**门禁:** 27 + 24 + 24 + 1 = 76 ✅ · DB 合计 10,141,280 B = 9.67 MiB ✅

### T-B2 读 manifest 三个字段

| 字段 | 实测值 | 计划预期 | 判定 |
|---|---|---|---|
| `sqlite_thread_ids` | **24** | 25 | ⚠️ **偏差**(见 §3.3) |
| `sqlite_only_thread_ids` | **24** | 25 | ⚠️ **偏差**(见 §3.3) |
| `postgres_lookup_error` | `null`(JSON 原生 null) | null | ✅ |
| `postgres_thread_ids_count` | 62 | 62 | ✅ |
| `files` / `sidecars` | 27 / 48 | 27 / 48 | ✅ |

### T-B3 PostgreSQL 侧现状快照

各表行数:

| 表 | 行数 |
|---|---|
| `checkpoints` | 946 |
| `checkpoint_writes` | 12,697 |
| `checkpoint_blobs` | 3,559 |
| `checkpoint_migrations` | 10 |
| `chat_sessions` | 10 |
| `workflow_runs` | 1 |
| `app_schema_migrations` | 3 |

`checkpoints` 的 **两套 thread_id** 都要统计(psycopg 把 `bytes` 写进 TEXT 列时落成 `\x<hex>` 字面量,
与正常文本 thread_id 是两套独立记录,必须分开数):

| 来源 | thread_id 形态 | distinct threads | 行数 | step 区间 |
|---|---|---|---|---|
| `checkpoints` | 文本 | 50 | 614 | −1 → 38 |
| `checkpoints` | `\x<hex>` | 15 | 332 | 1 → 60 |
| `checkpoints` | **合计** | **65** | 946 | — |
| `checkpoint_writes` | 文本 | 50 | 8,209 | — |
| `checkpoint_writes` | `\x<hex>` | 15 | 4,488 | — |
| `checkpoint_blobs` | 文本 | 50 | 2,316 | — |
| `checkpoint_blobs` | `\x<hex>` | 7 | 1,243 | — |

`workflow_runs` status 分组:**`running` × 1**(明细见 §3.2)。
`chat_sessions`:10 行(3 条真实用户会话 + 7 条空标题「新对话」/ 测试会话)。

> 实测 live distinct threads = **65**,manifest 记录 = **62**。差值 +3 是 manifest 快照
> (2026-10-30 01:57)之后新增的写入,属正常漂移,不影响核验结论。

### T-B4 全仓 db 扩展名扫描

```powershell
Get-ChildItem -LiteralPath . -Force -File -Recurse -Include *.sqlite,*.sqlite3,*.db,*.db3 |
  Where-Object { $_.FullName -notmatch '[\\/](\.venv|venv|node_modules)[\\/]' }
```

命中 **31** 个,逐条归类:

| 归类 | 数量 | 路径 | 处置(阶段 D) |
|---|---|---|---|
| 已归档的旧 SQLite 后端 | 27 | `logs/retired_checkpoints/2026-10-30/*.sqlite` | T-D3 删除目标 |
| worktree 本地副本 | 4 | `.worktrees/baseline-cde/checkpoints/{default,job1,phase5-gate-off,topo-probe}.sqlite` | T-D5(随 worktree 清理或显式删) |

- 4 个 worktree 文件被 `.gitignore:30` 的 `.worktrees/` 忽略,属本机开发副本。
- 根 `checkpoints/` 目录**存在但为空**(0 个条目),符合设计文档 §5 阶段 D-8 的前置描述。
- `git ls-files | Select-String '\.sqlite'` → **0 条**,仓库没有跟踪任何 SQLite 文件。

**门禁:** 命中 31 = 27 归档 + 4 worktree ✅ · 逐条归类完成 ✅

### T-B5 确认旧 thread 无在跑任务

对 manifest 的 24 个 `sqlite_only_thread_ids` 逐个反查(文本形态与 `\x<hex>` 形态**都查**):

| 检查项 | 结果 |
|---|---|
| 出现在 `checkpoints` | **0 / 24** |
| 出现在 `checkpoint_writes` / `checkpoint_blobs` | 0 / 24 |
| 出现在 `workflow_runs` | **0 / 24** |
| 出现在 `chat_sessions` | **1 / 24**(见 §3.4) |
| 命名形态 | 全部为测试 / 探针产物:`v001`–`v011`、`p8fix-{direct,direct2,stream,stream2,stub,stub2}`、`smoke-{gate,stub}`、`job1`、`multi-day`、`test-thread`、`dedup-check`、`914c74fc5bdf4d44b7e021a7d483a6e8` |

进程侧核对:**没有任何 python.exe / node.exe 在跑**,5432 端口只有 Postgres 自身监听,
无应用工作流进程存活。

**门禁:** 无对应 `running` / `suspended` 任务 ✅ · 无活动工作流进程 ✅

### T-B6 门禁判定

| 判定项 | 结论 |
|---|---|
| T-A1 ~ T-A5 | 5/5 通过 ✅ |
| T-B1 / T-B4 / T-B5 | 通过 ✅ |
| T-B2 | 通过,但计数与计划预期差 1(§3.3),**不影响结论** |
| T-B3 | 通过,发现 1 条遗留 running 记录(§3.2) |
| **结论** | **允许进入阶段 D**,但需先处理 §3.2 的 stale `running` 行;§3.4 的 1 个会话仅影响"可续跑性",不影响数据安全 |

---

## 3. 执行中发现的偏差与遗留

### 3.1 本机缺 PostgreSQL 客户端 + Docker 整体停机

见 §0。已通过容器内客户端绕过,**未安装任何系统级软件**。

### 3.2 `workflow_runs` 有 1 条 stale `running` 记录

```
thread_id = video-wiring-001 | video_name = x | status = running | updated_at = 2026-10-29 15:16:26+00
```

判定为**遗留的脏状态**,不是活动工作流:

- `updated_at` 停在 2026-10-29 15:16 UTC(距今约 23 小时),此后无更新;
- 该 thread 的 `checkpoints` 最大 step = 38,未再推进;
- 机器上无 python / node 进程存活。

参照:该 thread 在 `checkpoints` 里同样呈两套 thread_id(`video-wiring-001` 文本 40 条 step −1→38
+ `\x766964656f2d776172696e672d303031` 形态 88 条 step 39→60),属本仓 LangGraph 续跑已知现象,
与本次 SQLite 清理**无关**。

> **阶段 D 前建议**:确认该记录已废弃后再清理(或先把它置为非 `running`),
> 以免 `T-D3` 的"无活动工作流"前置条件被形式上违反。

### 3.3 manifest 实际是 24 个 thread,不是计划里写的 25

计划文档 §2.2 / §2.4(C7)写"25 个 SQLite-only thread",**实测为 24**。经核对,manifest 是对的,
计划是**计数笔误**:

- 归档目录有 **27** 个 `*.sqlite` 主文件,其中 **3 个是 0 字节且不对应任何 thread**:
  `default.sqlite`、`phase5-gate-off.sqlite`、`topo-probe.sqlite`;
- 27 − 3 = **24** 个 thread 库;
- 计划 §2.4(C7)自己列出的名字(`v001`–`v011` = 11、`p8fix-*` = 6、`smoke-*` = 2、
  `job1`、`multi-day`、`test-thread`、`dedup-check`、哈希 ID = 1)合计也正好是 **24**。

结论:**计划里的 25 是笔误,manifest 的 24 是准确的**,无需修数据。

### 3.4 `914c74fc5bdf4d44b7e021a7d483a6e8` 是真实用户会话,不是测试产物

计划 §2.4(C7)把 24 个 ID 全部归为"测试/探针产物",**这一条不准确**:

```
session_id  = 914c74fc5bdf4d44b7e021a7d483a6e8
title       = #1、附件上传的这些视频是关于:马来西亚吉隆坡武吉 马六甲的海上…
message_count = 1        created_at = 2026-10-29 15:52:09+00
```

- **对话历史在 PostgreSQL `chat_sessions` 里,不会因阶段 D 丢失**;
- 但它的 **graph checkpoint 只在 SQLite**(`checkpoints` 中文本与 hex 形态均查无此 thread),
  删除归档后**该 thread 无法再从 checkpoint 续跑**(只能重新发起)。

即:影响的是「续跑能力」,不是「数据安全」。若后续有用户要求恢复这条会话的图状态,
需回到 §1 的 dump 之外的离线 SQLite 副本 —— 而 dump 只含 PG,不含 SQLite。

> **阶段 D 前建议**:要么显式接受该 thread 不可续跑,要么在 T-D3 之前先把整个
> `logs/retired_checkpoints/2026-10-30/` 复制到受控离线介质(设计文档 §5.3 已写明该回滚边界)。

### 3.5 manifest 记录的 PG thread 数已漂移

manifest 记 62,实测 65(+3)。属快照时间之后的正常写入,非数据异常。

---

## 4. 本次**没有**做的事

按用户指示,只执行阶段 A / B,以下**均未执行**:

- **阶段 C**(代码与配置清理):未删 `scripts/retire_sqlite_checkpoints.py`,未改 `.env.example`、
  `.gitignore`、`README.md`、`docs/分析/WP8-真机验收清单.md`;
- **阶段 D**(永久删除):**75 个 SQLite DB 文件一个都没删**,根 `checkpoints/` 空目录未删,
  worktree 4 个副本未动;
- **阶段 E**(静态扫描与回归):未跑单测 / PG 集成测试 / `agent_gate` 套件 / 静态扫描;
- **未提交任何删除类改动**。本次唯一的仓库写入是本执行记录文档;
- 未动 `logs/retired_databases/langgraph_final_2026-10-30.dump`、`logs/retired_session_states/`、
  `logs/stage_e_cleanup/`,未递归删除 `logs/`、`outputs/`。

### 阶段 D 的前置条件现状

| 前置条件 | 状态 |
|---|---|
| PostgreSQL 可用备份 | ✅ 已备(633,743 B,SHA256 `B2E196ED…`) |
| 归档文件清单确认 | ✅ 75 个 DB + 1 manifest,逐条归类 |
| 旧 thread 无在跑任务 | ✅ 24/24 无 checkpoint、无 workflow_run |
| 无活动工作流进程 | ✅ 无 python / node 进程 |
| stale `running` 行处理 | ⬜ **未处理**(§3.2) |
| `914c74fc…` 续跑性确认 | ⬜ **未确认**(§3.4) |

---

## 5. 本次产生的文件

| 文件 | 说明 |
|---|---|
| `logs/sqlite-removal-backup/video_workflow_before_sqlite_cleanup.dump` | PG 备份(633,743 B) |
| `logs/sqlite-removal-backup/baseline_git.txt` | Git 基线快照 |
| `logs/sqlite-removal-backup/retirement_manifest_2026-10-30.json` | manifest 副本 |
| `docs/修正/auto-video-editor_SQLite移除_阶段AB执行记录.md` | 本文档 |

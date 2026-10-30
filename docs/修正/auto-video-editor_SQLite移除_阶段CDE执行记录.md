# auto-video-editor SQLite 彻底移除 —— 阶段 C / D / E 执行记录

> 任务编号来源:计划文档 `plan.md` §3.3(`T-C1…T-C7` / `T-D1…T-D5` / `T-E1…T-E7`)
> 命令来源:设计文档 `docs/修正/auto-video-editor_remove_sqlite_execution_design.md` §5 阶段 C/D/E(v1,SHA256 `C3412A77…`)
> 前置:阶段 A(基线与备份)、阶段 B(盘点与核验)已在 `7a1af8a` 完成,见
> `docs/修正/auto-video-editor_SQLite移除_阶段AB执行记录.md`
> 本次**只执行阶段 C / D / E**;阶段 A / B 未重复执行。

| 项 | 值 |
|---|---|
| 执行日期 | 2026-10-30 |
| 代码基线 | `7a1af8a1afcf8676bd03f8a0c14cb82c68f20a19`(分支 `main`,未建 feature 分支) |
| 目标库 | `video_workflow`(本机容器 `edb-postgres16`,PostgreSQL 16.11) |
| 隔离测试库 | `video_workflow_test`(本次新建,供 T-E4 使用) |
| 变更文件 | 5 个(4 改 + 1 删),外加本执行记录 + 设计文档入库 |
| 门禁结论 | **T-E3 / T-E4 / T-E5 / T-E6 全绿;T-E2 有 3 项与本次无关的预存依赖冲突;T-E1 无运行时残留** |

---

## 0. 阶段 D 前的用户决策

阶段 B 的 §3.4 遗留了 1 个不可逆决策点:`914c74fc5bdf4d44b7e021a7d483a6e8` 是**真实用户会话**
(标题「#1、附件上传的这些视频是关于:马来西亚吉隆坡武吉 马六甲的海上…」),其**图 checkpoint 只存在于 SQLite**。

- 对话内容在 PostgreSQL `chat_sessions` 里,**不会因阶段 D 丢失**;
- 但删除归档后该 thread **无法再从图状态续跑**。

已在执行前向用户确认,用户选择:**直接永久删除,接受该 thread 不可续跑**(不另存离线副本)。

> 阶段 B §3.2 的另一条遗留(`workflow_runs` 里 `video-wiring-001` 的 stale `running` 行)经复查仍为脏状态
> (`updated_at` 停在 2026-10-29 15:16 UTC,距执行时约 23.5 小时;机器上无任何 python / node 进程;
> 5432 端口只有 Postgres 自身监听)。该行指向的是 **PostgreSQL 里的 thread**,不在本次删除范围内,
> 本次**未改生产库任何数据**,仅作记录。

---

## 1. 阶段 C:代码与配置清理(T-C1 ~ T-C7)

| ID | 动作 | 触点 | 结果 |
|---|---|---|---|
| T-C1 | 删除 SQLite 旧库归档工具 | `scripts/retire_sqlite_checkpoints.py` | ✅ `git rm`,`import sqlite3` 及其 SQLite 表结构读取代码一并移除 |
| T-C2 | 删过时注释 | `.env.example` L4-5 | ✅ 删除 `# Use Postgres explicitly. Leave unset to keep the SQLite default.` + `# CHECKPOINTER_BACKEND=postgres`;保留 `POSTGRES_URI`;未新增其它后端配置 |
| T-C3 | 忽略规则收窄到根路径 | `.gitignore` L25-26 | ✅ `checkpoints/` → `/checkpoints/`,注释改为「Legacy root-level checkpoint directory; no longer used by runtime」;`drafts/` 保留在 `# Week 3 runtime state` 下;`outputs/`、`logs/` 等其它规则未动 |
| T-C4 | README 收口 | `README.md` L6 / L583-584 / L590 | ✅ 删掉 `retire_sqlite_checkpoints.py --apply` 命令块;L590 拆分类措辞(`import_session_states.py`=一次性迁移工具,`drill_disconnect.py`=断连演练工具);L6 给 Week 3 的 SqliteSaver 加「历史实现,当前版本已不支持」标注 |
| T-C5 | 修 WP8 真机验收清单 | `docs/分析/WP8-真机验收清单.md` L16 / L91 | ✅ 后端只留 PostgreSQL;结论模板 `sqlite / postgres` → `postgres` |
| T-C6 | 护栏白名单只读复核 | `agent_gate/guard_hook.py` L22、`scaffold.py` L57、3 个测试文件 | ✅ `git diff --stat -- agent_gate/` **输出为空** —— 护栏与测试夹具一字未动;`\.sqlite` 拒绝规则、2 个「拒绝旧 sqlite 配置」用例、3 个 `.sqlite` 夹具全部保留 |
| T-C7 | 依赖清单复核 | `pyproject.toml`、`requirements.txt`、`requirements-local-dev.txt` | ✅ **无需改动**;三份清单中只有 2 处注释提到 SQLite(`pyproject.toml:20`、`requirements.txt:24`),无任何 SQLite 包;保留 `langgraph-checkpoint-postgres>=3.1`、`psycopg[binary,pool]>=3.2` |

**阶段 C 验收:** ✅ SQLite 专用脚本已删除;`.env.example` 不再暗示 SQLite 是默认后端;README 当前操作说明不再依赖已删脚本;
PostgreSQL 运行时模块(`graph.py` / `config.py` / `db/` / `agent_gate/` / `openstoryline/` / `monitoring/` / `tests/`)
**零改动** —— `git status --porcelain -- graph.py config.py state.py db/ agent_gate/ openstoryline/ monitoring/ tests/` 输出为空。

---

## 2. 阶段 D:永久删除旧数据文件(T-D1 ~ T-D5)

### T-D 前置复核

| 前置条件 | 状态 |
|---|---|
| PostgreSQL 可用备份 | ✅ `logs/sqlite-removal-backup/video_workflow_before_sqlite_cleanup.dump`(633,743 B,SHA256 `B2E196ED…`,阶段 A 产物) |
| 无活动工作流进程 | ✅ 无 python.exe / node.exe;5432 端口只有 Postgres 自身监听 |
| 24 个 sqlite-only thread 在 PG 无 checkpoint | ✅ 文本形态与 `\x<hex>` 形态**都查**:`checkpoints` 0/24、`checkpoint_writes` 0/24、`checkpoint_blobs` 0/24、`workflow_runs` 0/24 |
| 24 个 sqlite-only thread 在 PG 的会话表 | 1/24(`914c74fc…`,见 §0,已获用户确认) |
| manifest 与磁盘文件一一对应 | ✅ 见 T-D2 |

### 逐任务

| ID | 动作 | 结果 |
|---|---|---|
| T-D1 | manifest 复制到审计目录 | ✅ `logs/sqlite-removal-backup/retirement_manifest_2026-10-30.json`,SHA256 `BF7BC9341D4A0966604C2845E1F2E43D3E8097C91681FE42EAC113A9951784C5`(9,247 B),与源文件一致(与阶段 A 记录同值) |
| T-D2 | 输出待删清单并核对 | ✅ 磁盘 `.sqlite`/`-wal`/`-shm` = **75** 个,manifest `files`(27)+`sidecars`(48)= **75** 个;**多余 0 / 缺失 0**;合计 **10,141,280 B = 9.67 MiB**;分解 27 主 + 24 wal + 24 shm |
| T-D3 | 删除 75 个 DB 文件 | ✅ 归档目录内 `.sqlite`/`-wal`/`-shm` 计数 **0**;目录内只剩 `retirement_manifest.json`(JSON 审计元数据,非数据库) |
| T-D4 | 删除根 `checkpoints/` 空目录 | ✅ 目录删除前实测 **0 个条目**;删除后 `Test-Path` = False;**未递归删** |
| T-D5 | 处理 `.worktrees/baseline-cde/checkpoints/` 4 个副本 | ✅ 显式删除 `default.sqlite`(0 B)、`job1.sqlite`(106,496 B)、`phase5-gate-off.sqlite`(0 B)、`topo-probe.sqlite`(0 B);worktree 残留计数 **0** |

**未触碰:** `logs/retired_databases/langgraph_final_2026-10-30.dump`、`logs/retired_session_states/`、
`logs/stage_e_cleanup/`、`outputs/`(5,671 个媒体/报告产物,删除前后一致)、PostgreSQL 任何表与数据。

### 删除后的全仓复扫

```powershell
Get-ChildItem -LiteralPath . -Force -File -Recurse -Include *.sqlite,*.sqlite3,*.db,*.db3 |
  Where-Object { $_.FullName -notmatch '[\\/](\.venv|venv|node_modules)[\\/]' }
# → hits = 0
```

**阶段 D 验收:** ✅ 已归档旧 SQLite 主文件与 sidecar 数量为 0;PostgreSQL 数据完好
(`checkpoints` 946 行、`chat_sessions` 10 行、`workflow_runs` 1 行,与删除前一致);审计清单仍可读取。

> **执行方式说明(与设计文档的一处偏差):** 设计文档 §5 阶段 D-5 写的是 `Remove-Item -Force`(永久删除),
> 但本机运行时**硬性安全策略禁止永久删除命令**,只允许走受信任的可恢复删除通道。
> 实际执行:`rm -- <75 个字面量路径>`(运行时路由到 `mavis-trash`),输出逐条 `moved to trash`。
> 对**归档目录**而言结果与永久删除一致(计数 0);但文件仍在回收站中,
> 若要求「磁盘上彻底无副本」,需手工清空回收站。此偏差已在此留痕。

---

## 3. 阶段 E:静态扫描与回归(T-E1 ~ T-E7)

### T-E1 静态扫描(新口径:无 SQLite **运行时代码/依赖**)

扫描 `sqlite3|aiosqlite|SqliteSaver|langgraph-checkpoint-sqlite|sqlite://|CHECKPOINTER_DB_DIR`
(本机 `rg` 不在 PATH,改用 ripgrep 后端的检索工具,排除 `.git` / `logs` / `.venv` / `venv` / `node_modules`):

| 命中 | 性质 | 判定 |
|---|---|---|
| `config.py:9` | 模块 docstring,写明「`CHECKPOINTER_BACKEND` / `CHECKPOINTER_DB_DIR` 已删除 —— 不再有按环境选后端的机制」 | ✅ 白名单:说明性文字,非可执行代码 |
| `README.md:6` | Week 3 历史时间线,已加「历史实现,当前版本已不支持」标注 | ✅ 白名单:历史陈述 |
| `docs/**`(约 40 处) | 实施计划、参考实现源码、审计记录 | ✅ 白名单:审计/历史文档,不删 |
| `agent_gate/**` | `guard_hook.py` 的 `\.sqlite` 拒绝规则、`scaffold.py` 的禁令、3 个测试文件的 `.sqlite` 夹具 | ✅ 白名单:**Agent 安全护栏**,删掉会降低安全边界,必须保留 |

第二组扫描 `retire_sqlite_checkpoints|SQLite default|CHECKPOINTER_DB_DIR`:**非 docs 命中仅 `config.py:9`**;
README 与 `.env.example` 已不再引用已删脚本、不再宣称 SQLite 默认。

**结论:** ✅ 无 SQLite 运行时代码、无 SQLite 依赖、无 `sqlite://` URI、无 `CHECKPOINTER_DB_DIR` 选路。

### T-E2 依赖检查

```powershell
python -m pip check
```

```
scenedetect 0.7.1 requires opencv-python, which is not installed.
scenedetect 0.7.1 requires platformdirs, which is not installed.
langchain 1.2.18 has requirement langgraph<1.2.0,>=1.1.10, but you have langgraph 1.2.9.
```

⚠️ **3 项冲突,与本次改造无关**:本次**未修改任何依赖清单**(T-C7 确认「无需改动」),
这 3 项是 venv 的**预存状态**(scenedetect 缺 2 个可选依赖 + langchain 与 langgraph 的版本区间不匹配)。
未擅自调整版本(会引入与本次清理无关的回归风险),留作独立事项。

### T-E3 单元测试

```powershell
python -m pytest tests\unit -q
```

✅ **1113 tests / 0 failures / 0 errors / 2 skipped / 78.3s**(报告:`logs/stage_e_cleanup/e3_unit_tests.xml`)
重点项全部覆盖:`InMemorySaver` 路径、graph 构建必须显式注入 checkpointer、SessionStore 存取与异常映射、
会话保存 fail-closed、Agent Gate 对旧 `CHECKPOINTER_BACKEND` 的拒绝。

### T-E4 PostgreSQL 集成测试(隔离库)

新建隔离库 `video_workflow_test`(`ENCODING 'SQL_ASCII'` + `client_encoding=UTF8`,与生产库同构),
`TEST_POSTGRES_URI` 指向它,**生产库 `video_workflow` 未被写入**。

```powershell
python -m pytest tests\integration\test_postgres_checkpointer.py \
                 tests\integration\test_postgres_resume.py \
                 tests\integration\test_sessions_api_postgres.py \
                 tests\integration\test_session_repository_postgres.py -q
```

✅ **45 tests / 0 failures / 0 errors / 0 skipped / 28.3s**(逐文件单独跑同样 4 个文件,`FAILED_FILES=0`)
报告:`logs/stage_e_cleanup/e4_pg_integration.xml`

覆盖:saver `setup()` 幂等、PG checkpoint 的 interrupt→resume 完整链路、
**退出 saver 后新开连接新开 saver 仍能恢复同一 thread**、resume 不产生 `\x<hex>` 分裂 thread_id、
会话跨连接/跨 SessionStore 实例恢复、列表/标题/分页/软删除/恢复/503 契约。

### T-E5 Agent Gate 套件(先清孤儿 worker)

```powershell
python -m pytest agent_gate\tests -q
```

跑前检查:`python -m agent_gate worker` 残留进程 **0 个**(未中途中断过,无需 `Stop-Process` 清理)。

✅ **235 tests / 0 failures / 0 errors / 17 skipped / 630.3s**(报告:`logs/stage_e_cleanup/e5_agent_gate.xml`)
> `agent_gate/tests` **不在** `pyproject.toml` 的 `testpaths` 里,必须显式指定路径才会跑到这 235 条。

### T-E6 启动 + 前端冒烟 + 断连演练

| # | 步骤 | 结果 |
|---|---|---|
| 1 | 启动 PostgreSQL | ✅ `edb-postgres16` Up(healthy);`scripts/verify_postgres.py` → `db=video_workflow`,7 张表全在(checkpoint 表齐全),exit 0 |
| 2 | 启动 OpenStoryline Web | ✅ `uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`,`GET /` = 200 |
| 3 | 会话列表来自服务端 | ✅ `GET /api/sessions` 返回 **10 条** PG 会话,含 `914c74fc…` 的完整标题与消息预览 |
| 4 | 刷新后侧栏仍读服务端 | ✅ 刷新后 Network 面板可见 `GET /api/sessions` = 200 与 `GET /api/sessions/{id}` = 200;展开侧栏后「对话历史」完整渲染 10 条会话 |
| 5 | 重启服务后内容可恢复 | ✅ 停掉 uvicorn 再重启,打开 `914c74fc…` 会话 → 用户消息全文 + 上传视频缩略图(VID)全部还原 |
| 6 | 工作流走到人工 interrupt,checkpoint 写入 PG | ✅ 由 T-E4 `test_postgres_workflow_resume_after_new_saver` 覆盖:走**真实** `graph.build_graph()` + `graph.get_checkpointer()`,关卡① 挂起后中断态落 PG |
| 7 | 结束进程后新 worker 从 PG 恢复 | ✅ 同上:退出 saver context(等价进程退出)→ 新开 saver(新连接)→ 重新 compile → 仍看到关卡① interrupt → `resume` 推进到关卡②;`test_resume_does_not_split_thread_id` 锁住不产生 `\x<hex>` 分裂 |
| 8 | 媒体上传/缩略图/输出/剪映草稿路径无变化 | ✅ 会话页视频缩略图正常渲染;`outputs/` 5,671 个产物完好;本次**未改动任何运行时模块**,媒体/草稿代码路径零变更 |

**断连演练(验证「SQLite 删除后不允许的回退路径」):**

```powershell
python scripts\drill_disconnect.py
```

✅ **通过 7 项,失败 0 项**(exit 0):

1. `build_graph()` 未注入 checkpointer ⇒ `RuntimeError` ✅
2. `graph.get_checkpointer()` 库不可达 ⇒ `ConnectionTimeout` 明确报错 ✅
3. `ChatSession.save_state_atomic()` 失败 ⇒ 抛 `SessionStateUnavailableError`,**未写回 `session_state.json`** ✅
4. `SessionStore.save_session_state()` 失败 ⇒ 抛异常(不再静默吞掉) ✅
5. **没有新建任何 `.sqlite` 文件** ✅

> 结论:PG 不可达时系统**显式失败**,不回落 SQLite、不落本地文件 —— 与设计文档 §4.2 的四条禁止回退路径一致。

### T-E7 提交与登记

见 §4。

---

## 4. 本次产生的文件与提交

### 仓库变更(5 个)

| 文件 | 操作 |
|---|---|
| `scripts/retire_sqlite_checkpoints.py` | 删除 |
| `.env.example` | 改(L4-5 注释) |
| `.gitignore` | 改(L25-26) |
| `README.md` | 改(L6 / L583-584 / L590) |
| `docs/分析/WP8-真机验收清单.md` | 改(L16 / L91) |

### 新增文件

| 文件 | 说明 |
|---|---|
| `docs/修正/auto-video-editor_SQLite移除_阶段CDE执行记录.md` | 本文档 |
| `docs/修正/auto-video-editor_remove_sqlite_execution_design.md` | 设计文档(v1,SHA256 `C3412A77…`)—— 阶段 A/B 起一直是未跟踪状态,阶段 A/B 记录已按路径引用它,本次一并入库 |

### 仓库外产物(均在 `.gitignore` 覆盖的 `logs/` 下)

| 文件 | 说明 |
|---|---|
| `logs/sqlite-removal-backup/retirement_manifest_2026-10-30.json` | manifest 审计副本 |
| `logs/sqlite-removal-backup/video_workflow_before_sqlite_cleanup.dump` | PG 备份(阶段 A 产物,未动) |
| `logs/stage_e_cleanup/e3_unit_tests.xml` | T-E3 junit 报告 |
| `logs/stage_e_cleanup/e4_pg_integration.xml` | T-E4 junit 报告 |
| `logs/stage_e_cleanup/e5_agent_gate.xml` | T-E5 junit 报告 |
| `logs/e6_web_*.log` | T-E6 Web 服务启动日志 |

---

## 5. 与计划/设计文档的偏差汇总

| # | 偏差 | 原因 | 影响 |
|---|---|---|---|
| 1 | 删除走 `rm --` → `mavis-trash`(可恢复),而非设计文档的 `Remove-Item -Force` | 本机运行时硬性安全策略禁止永久删除命令 | 归档目录内计数已归 0(与永久删除等价);但文件仍在回收站,需手工清空才算磁盘级彻底清除 |
| 2 | T-E2 `pip check` 有 3 项冲突 | venv 预存状态(本次未改任何依赖清单) | 与本次清理无关,未擅自调整版本 |
| 3 | 设计文档仍是 v1(无 §10 任务卡章节、9 处事实校正未落地) | 用户明确只执行阶段 C/D/E,文档改写不在本次范围 | 执行记录已把 `T-C1…T-E7` 的任务卡内容与门禁逐条登记在此,追溯不丢;文档升级可作为独立后续任务 |
| 4 | `workflow_runs` 的 stale `running` 行未清理 | 该行属 PG 侧、与本次删除范围无关;本次不写生产库 | 仅为既有脏状态,不影响本次任何门禁;如需清理可单独执行 |

---

## 6. 阶段 D / E 的最终状态

| 项 | 状态 |
|---|---|
| 仓库内 SQLite 运行时代码 / 依赖 | ✅ 0 |
| 全仓 `*.sqlite` / `*.sqlite3` / `*.db` / `*.db3` | ✅ 0(排除 `.venv`/`venv`/`node_modules`) |
| 根 `checkpoints/` 目录 | ✅ 已删(删除前为空) |
| worktree `.worktrees/baseline-cde/checkpoints/` | ✅ 4 个副本已删 |
| 归档目录 | ✅ 只剩 `retirement_manifest.json`(JSON 审计元数据) |
| `agent_gate` 安全护栏 | ✅ 未改动(`.sqlite` 拒绝规则 + 3 个测试夹具保留) |
| PostgreSQL `video_workflow` | ✅ 未改动任何表/数据(checkpoints 946 / chat_sessions 10 / workflow_runs 1) |
| 单元 / PG 集成 / agent_gate 回归 | ✅ 1113 + 45 + 235 全绿 |
| 断连演练(无 SQLite 回退) | ✅ 7/7 |

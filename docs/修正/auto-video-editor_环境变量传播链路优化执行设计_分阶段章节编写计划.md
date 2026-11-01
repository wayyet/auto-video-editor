# auto-video-editor:《环境变量传播链路优化执行设计》"分阶段执行开发任务"章节编写计划

> **目标文档**:`docs/修正/auto-video-editor_环境变量传播链路优化执行设计.md`
> **计划日期**:2026-11-01
> **性质**:纯文档修改计划 —— 本计划只描述对目标文档的章节增补,不包含任何代码改动;章节中的开发任务由后续执行会话按阶段推进。
> **已确认的两项决策**(2026-11-01):
> 1. 章节按**当前代码基线**编写:已被 2026-10-31《Postgres_worker连接故障_执行设计》落地的任务标 `[x]` 并保留"验收确认"勾选项;正文 §4 分析结论**不回改**,仅在新章节章首加基线说明。
> 2. 新章节插为 `## 9.`,位于"## 8. 执行计划"之后;原 §9–§13 顺延为 §10–§14。

---

## 1. 背景与目标

- 设计文档 §8"执行计划"只有粗粒度的阶段 A–E 说明,缺少可直接执行、逐项验收的任务拆解。
- 项目已有成熟惯例:《auto-video-editor_Postgres_worker连接故障_执行设计》§7"分阶段执行开发任务"(阶段总览表 → 依赖 mermaid → 每阶段"目标/任务清单/交付物/验收标准/回滚方式" → 提交策略 → 风险与中止条件),该格式已被其实际执行(T1–T10)验证。
- **基线差异是本次拆解的核心约束**:设计文档成文于 2026-10-31,同日执行完毕的《Postgres_worker连接故障_执行设计》已落地部分重叠改动。若按原文照搬拆任务,会指示重做已完成的工作。

## 2. 已核实的代码基线(2026-11-01,工作区现状)

| 设计文档断言 | 当前实际状态 | 证据(供编写时定位,行号不抄入章节) |
|---|---|---|
| §4.1 P0 `POSTGRES_URI` 注入时机过晚 | ✅ 已修复 | `start_production.ps1:266-306` 注入段前移到所有 `Start-Process` 之前,含根因注释;envBackup(L277-288)已含 `AGENT_GATE_POSTGRES_URI`;finally 还原(L833-834) |
| §4.4 P1 空字符串覆盖默认 DSN | ✅ 已修复 | 根 `config.py:379-385` 空值回退 `_DEFAULT_POSTGRES_URI`;`agent_gate/runner.py:148-160` 空值防御 |
| §4.7 P1 Worker 缺脱敏 DSN 诊断 | 🔶 部分完成 | `runner.py:132` `resolve_postgres_target()`、`:173` `describe_postgres_target()` 已存在;`runner.py:528` 已接线 `log_event("postgres_target", ...)`;`scripts/verify_agent_gate_postgres.py`、`agent_gate/tests/test_postgres_target.py` 已存在。缺口:`make_checkpointer()` 连接路径本身无诊断输出 |
| §4.3 P0 主图与 Worker DSN 优先级不一致 | ❌ 未完成 | `graph.py:447` 仍 `os.environ.get("POSTGRES_URI", POSTGRES_URI)`,未接入统一解析器;解析器目前位于 `agent_gate/runner.py`,非共享模块 |
| §4.2 P0 复用 Web 不校验配置 | ❌ 未完成 | `start_production.ps1:519-533` 复用分支仅凭端口占用 + HTTP 200(端口冲突分支已 fail-closed,exit 23) |
| §4.5 P1 MCode 子进程无显式 env | ❌ 未完成 | `mcode_chat_model.py` 四个创建点(同步 `_generate` L806、异步 `_agenerate` L918、`_probe_stream_json_support` L1068、流式 `MCodeRunner` L1216)均未传 `env`;`mcode_runner.py:93` 已支持 `env` 参数 |
| §4.8 P1 `make_checkpointer` 无连接超时 | ❌ 未完成 | `runner.py:302-308` `Connection.connect(...)` 未传 `connect_timeout`(`checkpoint_exists` 已用 5s) |
| §4.9 P1 `checkpoint_exists` 二态 | ❌ 未完成 | `runner.py:377-378` `except Exception: return False`,数据库不可达与无 checkpoint 不区分 |
| 生产拒绝 `AGENT_GATE_POSTGRES_URI` / `APP_ENV` | ❌ 未完成 | 全仓库无 `APP_ENV`;生产脚本仅 `Remove-Item`(L293)静默清除,resolver 层无生产防护 |
| 双消费者防重(§10.4 线程级) | ✅ 已完成 | commit `fb31148` 线程级操作员租约(task 级原子认领仍未做,维持 P2) |

补充:`start_production.ps1` 当前存在**未提交**修改(Phase 1 → Phase 0.58 preflight 前移 + 0.7 失败时探测 5432,2026-11-01 故障方案 B),与本链路任务无冲突,但执行前需先行处置(单独提交或随 commit ② 一并提交)。

## 3. 步骤一:既有章节重编号

| 原章节 | 新章节 |
|---|---|
| 9. 测试矩阵与验收条件(9.1–9.5) | 10.(10.1–10.5) |
| 10. 执行速度优化(10.1–10.5) | 11.(11.1–11.5) |
| 11. 回滚方案(11.1) | 12.(12.1) |
| 12. 推荐实施顺序 | 13. |
| 13. 相关源码 | 14. |

已核实正文无 `§N` 式交叉引用,重编号只需改标题行。§1–§8 与"最终结论"不动。

**注意**:新章节中引用测试矩阵时使用**重编号后**的章节号:D1–D8 → §10.1、P1–P8 → §10.2、G1–G8 → §10.3、测试命令 → §10.4、最终验收标准 → §10.5、回滚约束 → §12;§6.5/§6.6.C/§6.8/§8 位于插入点之前,编号不变。

## 4. 步骤二:插入 `## 9. 分阶段执行开发任务`

### 4.1 章首基线声明(blockquote,不带编号)

说明三点:本章把 §8 执行计划拆分为可逐项执行、逐项验收的开发任务;基线日期 2026-11-01,列出已由《Postgres_worker连接故障_执行设计》完成的项;正文 §4 的 P0/P1 结论成文早于该修复计划,执行时以本章任务状态为准。

### 4.2 9.1 阶段总览表

| 阶段 | 名称 | 是否改代码 | 主要交付物 | 前置依赖 | 建议提交点 | 当前状态 |
|---|---|---|---|---|---|---|
| A | 基线核查 | 否(只读) | 基线核查记录 | 无 | 不提交 | 部分完成 |
| B | 统一 DSN 解析层收尾 | 是 | `db/postgres_config.py` 等 | A | commit ① | 部分完成 |
| C | 启动脚本剩余加固 | 是 | `start_production.ps1` | B-T4 | commit ② | 部分完成 |
| D | MCode/MCP 显式环境 | 是 | `mcode_chat_model.py` 等 | B | commit ③ | 未开始 |
| E | 回归测试与验收 | 是(测试) | 测试用例 + 验收记录 | B/C/D | commit ④(或随 ③) | 未开始 |

### 4.3 9.2 阶段依赖关系(mermaid)

`A → B → C → D → E` 线性主链;注明 C-T2(复用指纹)可与阶段 B 并行。

### 4.4 9.3 阶段 A:基线核查(只读,不提交)

**目标**:确认已完成项的真实行为、摸清仓库外配置(操作员目录 `.mcp.json`),为 B–E 提供入口条件。

**任务清单**:
- [x] 复核 `start_production.ps1` 注入时序早于所有 `Start-Process`、envBackup 完整性(已完成,2026-10-31)
- [ ] 审计本机 MCode 操作员目录 `.mcp.json`:路径取自 `openstoryline/config.toml.local` 的 `[mcode].cwd` 或 `OPENSTORYLINE_MCODE_CWD`;核对 `video-agent` 的 `command` 是否主仓 `.venv` 的 python、`env` 是否覆盖 `POSTGRES_URI` / `AGENT_GATE_POSTGRES_URI`(只输出存在性与脱敏目标,不输出原文)
- [ ] 核对 `describe_postgres_target()` 接线覆盖面:`runner.py` 的 `log_event("postgres_target")` 覆盖哪些启动路径;`make_checkpointer()` 失败路径有无诊断
- [ ] 用真实 MCode CLI 启动 MCP,在 MCP 进程与 Worker 进程分别执行环境指纹诊断(§6.8 步骤 5)

**交付物**:基线核查记录(归档到执行记录文档)。**验收**:§8.A 四项确认全部有结论。**回滚**:不适用(只读)。

### 4.5 9.4 阶段 B:统一 DSN 解析层收尾(commit ①)

**目标**:主图与 Worker 共用同一解析器;生产拒绝测试库变量;连接快速失败(对应 §8.B 七条,已完成两条)。

**任务清单**:
- [x] `agent_gate/runner.py` `postgres_uri()` 统一解析 + 空值防御(已完成)
- [x] 根 `config.py` 空字符串回退(已完成)
- [ ] **B-T1** 新增共享模块 `db/postgres_config.py`:把 `PostgresTarget` / `resolve_postgres_target()` / `describe_postgres_target()` 从 `agent_gate/runner.py` 上移;`runner.py` 保留 re-export(`agent_gate/tests/test_postgres_target.py`、`scripts/verify_agent_gate_postgres.py` 的现有导入不受影响);`config.POSTGRES_URI` 兜底档保留延迟导入 + try/except(纯 kit 场景兜底,防 config↔db 循环导入)
- [ ] **B-T2** `graph.py` `get_checkpointer()` 改用 `resolve_postgres_target()`,删除自有 `os.environ.get` 解析
- [ ] **B-T3** 根 `config.py` 的 `POSTGRES_URI` 常量改为从统一解析器取值,消除双份回退逻辑
- [ ] **B-T4** 新增 `APP_ENV` 生产标记:`resolve_postgres_target()` 在 `APP_ENV=production` 且存在 `AGENT_GATE_POSTGRES_URI` 时抛明确配置错误;`start_production.ps1` 注入段设 `$env:APP_ENV='production'`
- [ ] **B-T5** `make_checkpointer()` 连接加超时:复用 `db/connection.py` 的 `DEFAULT_CONNECT_TIMEOUT_S`(10s)/ `_connect_timeout_s()`,用 `psycopg.conninfo.make_conninfo()` 合并 `connect_timeout`,不拼接字符串
- [ ] **B-T6** `make_checkpointer()` 连接前把 `describe_postgres_target()` 脱敏字段写入 worker.log / status.json(与 `runner.py:528` 的 log_event 对齐)
- [ ] **B-T7** `checkpoint_exists()` 三态化:存在 / 不存在 / 查询失败;调用方区分 `database_unavailable` 与 `no_checkpoint`(§6.5),保持 fail-closed

**交付物**:`db/postgres_config.py`(新)、`agent_gate/runner.py`、`graph.py`、`config.py`。
**验收**:§10.1 中 D2/D4 用例 + §10.3 中 G3/G4/G8 用例通过;`tests/unit`、`agent_gate/tests/test_postgres_target.py` 全绿。
**回滚**:`git checkout -- graph.py config.py agent_gate/runner.py`;删除 `db/postgres_config.py`。

### 4.6 9.5 阶段 C:启动脚本剩余加固(commit ②)

**目标**:补齐注入变量集;复用 Web 不再仅凭 HTTP 200(对应 §8.C 六条,已完成五条)。

**任务清单**:
- [x] 备份完整变量集 / 注入前移 / 清除 `AGENT_GATE_POSTGRES_URI` / 删除重复注入块 / finally 恢复(已完成,2026-10-31)
- [ ] **C-T1** envBackup 列表与注入段补 `APP_ENV`、`AGENT_GATE_CKPT_BACKEND`(注入 `$env:AGENT_GATE_CKPT_BACKEND='postgres'` 钉住后端)
- [ ] **C-T2** 复用 Web 配置指纹第一阶段(§6.6.C):复用分支不能证明本次 `-PostgresUri`/运行模式与现有 Web 配置一致时不复用;仅允许重启 PID 文件明确归本项目所有的进程;未知进程维持 fail-closed(exit 23 分支保留)。二期可选:仅监听 127.0.0.1 的诊断端点返回非秘密配置指纹比对

**交付物**:`start_production.ps1`。
**验收**:§10.2 P7/P8;pwsh 解析无语法错误 + `-WhatIf` 走查;新启动/复用两条路径行为符合预期。
**回滚**:`git checkout -- start_production.ps1`。

### 4.7 9.6 阶段 D:MCode/MCP 显式环境(commit ③)

**目标**:MCode 各子进程入口使用受控环境快照,并完成真实链路实测(对应 §8.D)。

**任务清单**:
- [ ] **D-T1** `MCodeChatModel` 构造/接收 `process_env` 受控快照(保证 Windows 基础变量 `PATH`/`SYSTEMROOT`/`TEMP`/`TMP`/`USERPROFILE` 存在)
- [ ] **D-T2** 四个创建点传 env:同步 `_generate()`、异步 `_agenerate()`、`_probe_stream_json_support()`、流式 `_astream_stream_json()` 的 `MCodeRunner(..., env=process_env)`(`mcode_runner.py` 已支持 env 参数,只差传入)
- [ ] **D-T3** `.mcp.json` 审计结论落地:若 MCode 的 MCP 子进程不继承所需变量,增加本机 MCP bootstrap wrapper;凭证从 Windows Credential Manager / DPAPI 或 ACL 保护的本地文件读取,不把明文写入 `.mcp.json`
- [ ] **D-T4** 真实 MCode → MCP → Worker 端到端实测,验证 MCP 与 Worker 获得预期 DSN 来源

**交付物**:`openstoryline/src/open_storyline/mcode_chat_model.py`、(可选)wrapper。
**验收**:§10.2 P3/P4/P5;`openstoryline\.venv` 的 pytest 全绿。
**回滚**:`git checkout -- openstoryline/src/open_storyline/mcode_chat_model.py`。

### 4.8 9.7 阶段 E:回归测试与验收(commit ④ 或随 ③)

**任务清单**:
- [ ] **E-T1** 解析器用例补齐,对齐 §10.1 D1–D8(production 拒绝、百分号编码、无密码 DSN 等;随 B-T1 上移放 `tests/unit/test_postgres_config.py`,或扩展现有 `agent_gate/tests/test_postgres_target.py`)
- [ ] **E-T2** 进程传播测试自动化(§10.2 P1–P8 中可自动化部分)
- [ ] **E-T3** 端到端验收:§10.3 G1–G8 + 日志脱敏走查;测试命令按 §10.4 执行

**验收**:§10.5 最终验收标准全部满足。**回滚**:测试文件单独回退,不影响功能代码。

### 4.9 9.8 提交策略与记录归档

- commit ①(阶段 B):`fix(db): 统一主图与 Worker 的 Postgres DSN 解析…`
- commit ②(阶段 C):`fix(startup): 补齐生产环境注入变量与 Web 复用校验…`
- commit ③(阶段 D):`feat(openstoryline): MCode 子进程显式环境快照…`
- commit ④(阶段 E):`test(agent-gate): 环境变量传播链路回归…`
- 每阶段独立成组提交,执行记录归档命名沿用惯例:`auto-video-editor_环境变量传播链路优化_阶段XX执行记录.md`
- 前置处置:工作区未提交的 `start_production.ps1`(Phase 0.58)改动先行单独提交,避免与本链路 commit ② 混杂

### 4.10 9.9 总体风险与中止条件

- `graph.py`/`config.py` 导入链改动引发启动失败 → 回滚 commit ①,章节任务暂停
- 复用逻辑收紧误伤正常启动 → 回滚 commit ②
- 生产拒绝逻辑误伤纯 kit 场景 → B-T1 必须保留 runner 现有 try/except 兜底(不依赖仓库 config 的场景)
- 全程不改 PostgreSQL schema、不动 checkpoint 表与数据卷(§12 回滚约束继续有效)

### 4.11 §8 章首指引

在"## 8. 执行计划"开头加一行:*任务级拆分与当前基线状态(2026-11-01)见第 9 章。*

## 5. 风格约束(编写章节时遵循)

- 引用代码只写文件路径 + 函数名,**不用 file:line**(仓库文档既有惯例;本计划中的行号仅供编写时定位,不抄入章节)
- 阶段标题全角冒号:`### 9.4 阶段 B:统一 DSN 解析层收尾(commit ①)`;只读阶段标注"不提交"
- 任务用 `- [ ] **B-T1**:` 可引用勾选格式;验收标准引用 D/G/P 用例编号
- 不在文档写任何 DSN 原文/密码;`.mcp.json` 相关只写"存在性与脱敏目标"
- 不改动 §1–§8 与"最终结论"的实质内容(仅 §8 加一行指引、§9–§13 重编号)

## 6. 验证清单(编写完成后)

1. Read 全文:章号 1–14 连续、小节号与父章一致、无残留旧编号(如未顺延的"9.1")
2. grep 检查:无失效交叉引用、无 DSN 明文;新章节 mermaid / 表格 / 代码块语法闭合
3. 向用户展示 §9 锚点确认格式;是否按 `docs(修正):` 惯例提交由用户决定(默认不提交)

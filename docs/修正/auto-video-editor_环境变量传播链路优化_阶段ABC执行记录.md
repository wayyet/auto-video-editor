# auto-video-editor 环境变量传播链路优化 —— 阶段 A / B / C 执行记录

> 对应设计文档:`docs/修正/auto-video-editor_环境变量传播链路优化执行设计.md`
> 任务拆分依据:`docs/修正/auto-video-editor_环境变量传播链路优化执行设计_分阶段章节编写计划.md`(§9.3–9.5)
> 本次**只执行阶段 A(基线核查,只读)、阶段 B(统一 DSN 解析层收尾,commit ①)、阶段 C(启动脚本剩余加固,commit ②)**;阶段 D(MCode/MCP 显式环境)与阶段 E(回归测试与验收)**未执行**。

| 项 | 值 |
|---|---|
| 执行日期 | 2026-11-01 |
| 代码基线 | `a1757d0`(执行前 `main` HEAD;工作区另带未提交的 Phase 0.58 改动,按计划前置处置单独提交) |
| 本次提交 | 前置 `698a8d1`(Phase 0.58)/ commit ① `361cc13`(阶段 B)/ commit ② `8a4cbff`(阶段 C) |
| 环境 | 本机容器 `edb-postgres16`(执行中途 Docker Desktop 守护进程曾退出一次,已按 Phase 0.5 同款方式拉起,见 §4 事件记录) |

---

## 1. 阶段 A:基线核查(只读,不提交)

### A-1 当前 Web 是否复用(§8.A 第 1 项)

- 最新 PID 记录 `logs/openstoryline_pids_20261101_020533.json`:`reused=true` 且 `web_pid=null` —— **复用路径不记录进程 PID**,无法回溯现有 Web 由谁启动、带着什么配置。这正是 §4.2 P0 的盲区,已由阶段 C(C-T2)收口。
- 现场探活:容器 healthy;`127.0.0.1:7860` 返回 HTTP 200(当前确有一个健康 Web 在运行,归属不明)。

### A-2 实际 MCode 操作员目录与 `.mcp.json` 审计(§6.8 / §8.A 第 2、3 项)

- 操作员目录:`openstoryline/config.toml.local` 的 `[mcode].cwd` = `E:/Documents/kuaishou/video-ops`(环境变量 `OPENSTORYLINE_MCODE_CWD` 未设置);mcode CLI 0.6.12,`C:/Users/wayyet/.minimax-code/mcode.cmd`。
- `video-ops/.mcp.json` 中 `video-agent` 条目审计(只记录存在性与路径,不含值原文):

| 审计点 | 结果 |
|---|---|
| `command` | 指向主仓 `.venv\Scripts\python.exe` ✅(不是 openstoryline venv / 旧 checkout) |
| `args` | `-m agent_gate.mcp_server --outputs-root <主仓>/outputs --graph-factory graph:build_graph` ✅ |
| `env` | 含 `PYTHONPATH` / `AGENT_GATE_REPO_ROOT` / `PYTHONUTF8` / `STORYLINE_MODE=auto` / `AGENT_GATE_MEDIA_ROOTS` / `PATH` |
| 是否覆盖 `POSTGRES_URI` / `AGENT_GATE_POSTGRES_URI` | **均未设置** ✅ —— MCP 侧没有 DSN 覆盖,传播只能靠父进程继承,与设计 §4.6 判断一致 |

### A-3 `describe_postgres_target()` 接线覆盖面(改造前)

- 已接线:`run_worker` 在 checkpointer 连接前落 `log_event("postgres_target", ...)`(仅 postgres 后端分支),T7 用例对该事件字段有断言。
- 缺口(与编写计划基线表一致):
  1. `make_checkpointer()` 连接路径本身无诊断输出 —— 脚本/单测等不经 `run_worker` 的调用无诊断;
  2. `checkpoint_exists()` 失败路径静默返回 `False`,数据库不可达与"无 checkpoint"不可区分;
  3. `make_checkpointer()` 连接无超时。
  → 三项分别由 B-T6、B-T7、B-T5 收口。

### A-4 真实 MCode CLI 环境指纹实测(§6.8 步骤 5)

用真实 `mcode exec`(在操作员目录内,会自动按 `.mcp.json` 拉起 MCP)做哨兵探测:父进程注入哨兵值后,要求 agent 只报告 SET/UNSET:

| 变量 | 父进程注入 | MCode 进程报告 |
|---|---|---|
| `POSTGRES_URI` | 哨兵值 | **SET** ✅(父 → MCode 继承成立) |
| `APP_ENV` | 哨兵值 | **SET** ✅ |
| `AGENT_GATE_POSTGRES_URI` | 不注入 | **UNSET** ✅ |
| video-agent MCP | — | **已连接**(工具列表可见,按指令未调用) ✅ |

**已证明**:父进程 → MCode 进程的环境继承成立;真实 mcode 0.6.12 能按 `.mcp.json` 启动 video-agent MCP。

**边界(留待阶段 D)**:本次探测未证明 **MCode → MCP 子进程** 的 env 合并行为(MCP 进程内指纹无从读取,仓库侧也无法控制 MCode 的合并规则),也未跑真实 Worker —— 这正是设计 §4.6 要求"实机验证"的部分,归 D-T3/D-T4;B-T6 落地的 `postgres_target` 事件届时就是观测手段。

---

## 2. 阶段 B:统一 DSN 解析层收尾(commit ① `361cc13`)

| 任务 | 结果 |
|---|---|
| B-T1 新增 `db/postgres_config.py` | `PostgresTarget` / `resolve_postgres_target()` / `describe_postgres_target()` 自 `agent_gate.runner` 上移;`runner.py` 顶部 re-export,`scripts/verify_agent_gate_postgres.py` 与既有测试导入零改动。`source` 字符串沿用历史取值(含 `runner.DEFAULT_POSTGRES_URI`),保持日志契约连续。解析器对 `config` 的引用保持延迟导入 + try/except(防 config↔db 循环导入,兼容纯 kit 场景) |
| B-T2 `graph.get_checkpointer()` 接入 | 删除自有 `os.environ.get("POSTGRES_URI", ...)` 与空值检查,改 `resolve_postgres_target().uri`;主图与 Worker 从此共用同一解析函数(已验证是同一函数对象) |
| B-T3 根 `config.py` | `POSTGRES_URI` 常量改由解析器取值,删除第二份空值回退逻辑。语义说明:常量仍是导入时快照,但快照现在走统一优先级 —— 若某进程在导入 `config` 时已设 `AGENT_GATE_POSTGRES_URI`,快照会取该值(与 worker 行为一致;生产脚本注入段会清除该变量,不受影响) |
| B-T4 `APP_ENV` 生产防护 | `resolve_postgres_target()` 在 `APP_ENV=production` 且存在非空 `AGENT_GATE_POSTGRES_URI` 时抛 `RuntimeError`(fail-fast)。脚本侧注入由阶段 C(C-T1)落地 |
| B-T5 连接超时 | `make_checkpointer()` 用 `psycopg.conninfo.make_conninfo(uri, connect_timeout=...)` 合并超时(不拼字符串);超时值复用 `db/connection.py` 的 `_connect_timeout_s()`(默认 10s,`POSTGRES_CONNECT_TIMEOUT` 可覆盖),纯 kit 场景回退同款默认值 |
| B-T6 连接前脱敏诊断 | `postgres_target` 事件挪进 `make_checkpointer()` 连接路径本身:签名增加可选 `mailbox`,有 mailbox 落 events.jsonl(事件名/字段与原 `run_worker` 内联版本完全一致,T7 断言不变),无 mailbox 走 `logging`。`run_worker` 经 `checkpointer_session(thread_id, mailbox=mb)` 传入,不再重复落事件 |
| B-T7 `checkpoint_exists()` 三态化 | 返回 `True` / `False` / `None`:查询失败返回 `None` 并落一条脱敏诊断日志(密码子串打码,新增 `_scrub_dsn_text`);`check_mode` 对 `None` 返回新拒绝码 **`database_unavailable`**,与 `no_checkpoint` 区分,保持 fail-closed(不许盲目续跑) |

### 验收(设计 §9.5/编写计划 §4.5)

- `agent_gate/tests/test_postgres_target.py` **15/15 通过**(T1–T5 优先级与空值回退、T6 脱敏、T7 真实 MCP→Worker 传播 + 2 条启动脚本静态回归);
- `agent_gate/tests/test_lifecycle.py` + `test_operator_lease.py` 通过(覆盖 `check_mode` / `checkpoint_exists` 改动的调用方兼容);
- `tests/unit` 全部通过(exit 0,2 skip 与本次改动无关);
- 内联验证:D2(production 拒绝)抛错符合预期;`make_conninfo` 合并 `connect_timeout` 生效;`scripts/verify_agent_gate_postgres.py` 成功路径退出码 0、输出全脱敏。

---

## 3. 阶段 C:启动脚本剩余加固(commit ② `8a4cbff`)

| 任务 | 结果 |
|---|---|
| C-T1 变量集补齐 | `envBackup` 与注入段补 `APP_ENV`(钉 `production`,与 B-T4 形成"脚本清除 + resolver 拒绝"双保险)与 `AGENT_GATE_CKPT_BACKEND`(钉 `postgres`);注入日志行同步更新;finally 恢复沿用 `envBackup.Keys` 循环,新变量自动纳入(P8) |
| C-T2 复用指纹第一阶段 | 新增 `Get-WebConfigFingerprint`:`sha256(PostgresUri + '|' + StorylineMode)` 前 16 位十六进制,单向哈希不泄密码,启动/复用/重启均写入 PID 文件;新增 `Find-OwningWebRecord`:归属口径 = PID 文件 `reused=false` 且 `web_pid` 存活,另加 PID 复用防御(命令行不含 `agent_fastapi` 的存活 PID 不算归属)。复用分支由"三分支"改"四分支",见下表 |

### 复用判定四分支(C-T2 后)

| 分支 | 条件 | 行为 |
|---|---|---|
| 1 复用 | 端口在听 + HTTP 200 + PID 文件证明归属 + 指纹一致 | `reused=true`,PID 文件记录指纹与来源文件 |
| 1b 受控重启 | 归本项目所有,但指纹不一致 / 旧格式无指纹 | 只杀 PID 文件归属的 `web_pid`/`web_cmd_pid`,等端口释放(15s)后按本次注入配置重起;释放失败 → exit 23 |
| 2 端口冲突 | 端口在听但 HTTP ≠ 200 | exit 23(不变) |
| 2b 归属不明 | 健康,但找不到任何 PID 文件能证明归属 | **exit 23 fail-closed**(新增;附 `Get-NetTCPConnection` 占用进程定位),不自动杀未知进程 |
| 3 端口空闲 | — | 起一次 uvicorn(不变),PID 文件补 `config_fingerprint` / `storyline_mode` 字段 |

迁移说明:现有运行中的 Web 由旧格式记录启动(有归属记录但无指纹),下次 `start_production.ps1` 会走 **1b 受控重启**——这正是"旧 Web 持有过期环境仍被复用"(§4.2 P0)的修复路径;`stop_production.ps1` 只读 `reused` / `web_pid` / `web_cmd_pid` / `mcp_pid`,新增字段兼容。

### 验收(编写计划 §4.6)

- pwsh 7 解析无语法错误(Parser::ParseFile,0 错误);
- `-WhatIf` 走查:计划输出正确反映四分支语义,零副作用;
- 静态回归:注入必须早于任何 `Start-Process`、`AGENT_GATE_POSTGRES_URI` 清除位置、envBackup 完整性、单一 `POSTGRES_URI` 注入 —— 全部通过(Postgres 恢复后随 15 项全量一起跑绿)。

---

## 4. 事件记录

- **Docker Desktop 守护进程中途退出**(容器状态由 Up 变为守护管道消失,连接表现为 `ConnectionTimeout`):与本链路改动无关 —— 此前 `test_postgres_target.py` 全量已连真实测试库跑绿。处置:按脚本 Phase 0.5 同款方式启动 Docker Desktop(120s 轮询就绪)→ `docker start edb-postgres16` → healthy → 重跑全量回归确认。
- 阶段 A 的真实 mcode 探测会拉起 video-agent MCP 子进程并消耗一次极小会话;未调用任何 MCP 工具、未启动 Worker。

## 5. 提交与回滚

| 提交 | 内容 | 回滚方式 |
|---|---|---|
| `698a8d1` | 前置处置:Phase 0.58 preflight 前移(2026-11-01 故障方案 B,此前未提交) | `git revert 698a8d1` |
| `361cc13` | 阶段 B:`db/postgres_config.py`(新)、`agent_gate/runner.py`、`graph.py`、`config.py` | `git revert 361cc13`(或按计划 `git checkout` 四文件后删新模块) |
| `8a4cbff` | 阶段 C:`start_production.ps1` | `git revert 8a4cbff` |

全程未改 PostgreSQL schema、未动 checkpoint 表与数据卷(§12 回滚约束)。

## 6. 未执行项与遗留

- **阶段 D**(MCode/MCP 显式环境):`mcode_chat_model.py` 四个创建点的 `process_env` 快照、MCode→MCP env 合并实机验证、bootstrap wrapper 决策;
- **阶段 E**(回归与验收):D1–D8 解析器用例补齐、P 系列传播测试自动化、G1–G8 端到端验收;
- **C-T2 第二阶段(可选)**:仅监听 127.0.0.1 的诊断端点返回非秘密配置指纹,替代"PID 文件指纹"比对;
- 本记录对应的设计文档与编写计划两份 `docs/修正/*.md` 目前仍未入库(untracked),是否提交由用户决定。

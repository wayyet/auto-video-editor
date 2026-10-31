# auto-video-editor：Postgres worker 连接故障 —— 阶段 C3 / D / E 验收记录

> **执行日期**：2026-10-31
> **依据**：`docs/修正/auto-video-editor_Postgres_worker连接故障_执行设计.md` 第 7 章
> **前置**：阶段 A/B（见《阶段AB现场记录》）、C1（commit `1e281ad`）、C2（commit `b25cf8a`）已完成
> **本次范围**：阶段 C3（诊断脚本与测试建设）、阶段 D（测试与验收闸门）、阶段 E（部署重启与故障任务恢复）
> **脱敏声明**：本记录所有 DSN 均为脱敏形态，不含真实密码。

---

## 1. 阶段 C3 交付物（commit `1f4388a`）

| 交付物 | 内容 |
|---|---|
| `scripts/verify_agent_gate_postgres.py` | Worker 同路径诊断脚本：**复用 `agent_gate.runner.resolve_postgres_target()`**（非自行读 `POSTGRES_URI`），输出脱敏诊断字段（source/host/port/dbname/user/password_present/uri_masked），`psycopg.connect(uri, connect_timeout=5)` 后执行 `SELECT 1, current_database(), current_user`；失败时只输出异常类型+消息（不输出原始 URI）；退出码成功 0 / 失败 1 |
| `agent_gate/tests/test_postgres_target.py` | 15 个用例，覆盖 T1–T7（见 §2.1）；含 `start_production.ps1` 静态回归（注入顺序、`AGENT_GATE_POSTGRES_URI` 备份+清除、无重复注入块、pwsh 7+ 语法解析） |

C3 验收标准达成：`pytest agent_gate\tests -q` 全绿（见 §2）；诊断脚本三场景输出符合 §6.5 预期（见 §2.2）。

## 2. 阶段 D：测试与验收（闸门）

### 2.1 T1–T7 自动化验收

执行命令：`.\.venv\Scripts\python.exe -m pytest agent_gate\tests\test_postgres_target.py -v` → **15 passed**。

| 编号 | 用例 | 结果 | 证据 |
|---|---|---|---|
| T1 | 只设 `POSTGRES_URI`（含密码）→ 选 `env:POSTGRES_URI` | ✅ | `test_t1_only_postgres_uri_with_password_is_chosen` |
| T2 | 两变量同设 → `AGENT_GATE_POSTGRES_URI` 按既有优先级胜出；生产脚本清除遗留覆盖值 | ✅ | `test_t2_agent_gate_var_keeps_priority` + `test_production_script_injects_postgres_uri_before_any_start_process`（静态断言 `Remove-Item Env:AGENT_GATE_POSTGRES_URI` 早于任何 `Start-Process`） |
| T3 | `AGENT_GATE_POSTGRES_URI` 为空白 → 落 `POSTGRES_URI` | ✅ | `test_t3_blank_agent_gate_var_falls_to_postgres_uri` |
| T4 | `POSTGRES_URI=""` → 不返回空 DSN，回退 `config.POSTGRES_URI`（默认 `video_workflow`） | ✅ | runner 侧 `test_t4_blank_postgres_uri_falls_to_config` + **子进程级** `test_t4_empty_env_var_does_not_shadow_config_default`（真 `config.py` 导入路径） |
| T5 | 两变量均未设 → `config.POSTGRES_URI` / 默认 DSN | ✅ | 单测 + 子进程双覆盖；另验 `postgres_uri()` 保留 `str` 契约 |
| T6 | 无密码 URI：诊断 `password_present=false`、连接失败可定位、脱敏不泄漏密码/query | ✅ | 单测（`uri_masked=postgresql://alice:***@…`、query 丢弃）+ 真连本机服务端：脚本退出码 1，错误 `fe_sendauth: no password supplied`（与故障现场一致），输出无其它 DSN 密码 |
| T7 | MCP 子进程 env → Worker 传播（启动瞬间快照） | ✅ | `test_t7_worker_env_snapshot_from_mcp_child`：stdio 拉起真实 `agent_gate.mcp_server`（env 只有 `POSTGRES_URI`，生产形态），Worker 落 `postgres_target` 事件 `source=env:POSTGRES_URI`、`dbname`=测试库、`password_present=true`，并到达 `running`（`STATE_RUNNING` 在 `checkpointer_session` 成功后才设置 ⇒ 继承的 DSN 真实可连）；pytest 进程事后改 env 不影响 |

### 2.2 诊断脚本三场景（§6.5 验收）

```text
S1 正常(POSTGRES_URI 带密码):
  source: env:POSTGRES_URI; dbname: video_workflow; password_present: True
  ✓ 连接成功:SELECT 1 → 1; current_database()=video_workflow; current_user=postgres   exit=0

S2 无密码(AGENT_GATE_POSTGRES_URI 不带密码):
  source: env:AGENT_GATE_POSTGRES_URI; password_present: False
  ✗ 连接失败:OperationalError: … fe_sendauth: no password supplied   exit=1
  （与生产故障同款错误;诊断块完整可读,原始 URI 未落输出）

S3 空变量(POSTGRES_URI=""):
  source: config.POSTGRES_URI; dbname: video_workflow; password_present: True
  ✓ 连接成功   exit=0
```

### 2.3 闸门命令与结果

| 命令 | 结果 |
|---|---|
| `pytest agent_gate\tests -q` | **0 failed**；17 skip 为既有基线（claude/mcode 可执行档缺失类，与本次改动无关） |
| `pytest tests\integration\test_postgres_checkpointer.py -v` | **3 passed**（3.83s） |
| `scripts\verify_agent_gate_postgres.py` 三场景 | 见 §2.2，均符合预期 |

已知偶发（如实记录）：首次全量运行时既有用例 `test_agent_security.py::test_mcode_mcp_tool_timeout_comes_from_mcp_json` 在套件高负载下失败一次（mcode 子进程工具列表竞态，`Tool nap$ not found`）；**单独重跑即通过**，复跑全量 0 failed。该用例与本批次新增文件无交集（本批次仅新增两个文件）。

**闸门结论：T1–T7 全部通过，`verify_agent_gate_postgres.py` 来源/密码判定正确 —— 允许进入阶段 E。**

## 3. 阶段 E：部署重启与故障任务恢复

### E-1 旧 Web 进程处置（§6.2）

- 最新 PID 文件 `logs/openstoryline_pids_20261031_052533.json`：`reused=false`、`web_pid=22476`；
- 实测 **PID 22476 已不存在、7860 端口无人监听** —— 旧 Web 已自行退出，无需（也无法）执行停止动作；亦排除新旧双实例抢占 7860 的风险。
- 未停止 Postgres 容器、未删除卷、未清理 checkpoint 表（全程只读）。

### E-2 用修正后的 `start_production.ps1` 重启（UAC 提权代跑）

- 提权进程 PID 22128，命令行 `start_production.ps1 -SkipPhase3`，日志 `logs/start_20261031_160550.log`；
- **注入时序（C1 修复的直接证据）**：`[16:05:50] 注入环境变量(所有子进程启动之前;脚本结束时还原): POSTGRES_URI = postgresql://postgres:***@localhost:5432/video_workflow, …` —— 早于 Docker 检查（16:05:52）、早于 Web `Start-Process`（16:07:06）；
- 新 PID 文件 `logs/openstoryline_pids_20261031_160550.json`：`reused=false`、`web_pid=15636`；脚本另清掉一个孤儿 web 进程（PID 6048）；
- preflight 全过（Step 5 Postgres 连通、Step 6 schema 就绪），Phase 2 `run_workflow.py --production` 退出码 0，`[16:08:45] 启动完成`。

### E-3 T10：环境变量恢复

- 结构性证据：静态回归测试断言 `finally` 按 `$envBackup`（含 `AGENT_GATE_POSTGRES_URI`）还原；
- 运行时证据：脚本跑至「一键启动完成」正常结束（`finally` 必然执行）；提权父进程已退出；
- 泄漏检查：`POSTGRES_URI` / `AGENT_GATE_POSTGRES_URI` 的 **User/Machine 作用域均为空**（与阶段 A 基线一致），无作用域残留。

### E-4 T8：真实 MCode turn（Web → MCode → MCP → Worker）

浏览器（127.0.0.1:7860，LLM/VLM 均为 `mcode-operator`）发起 turn：「请用演示素材 E:\Documents\kuaishou\auto-video-editor\inputs\30s.mp4 启动一个视频剪辑任务…」。

- 新线程 `e8bacbcee73e4de4a1988403e0dde3e8`（即全新 thread_id）；
- Worker 启动即落 `postgres_target` 事件（**Worker 眼中的最终 DSN**）：
  ```json
  {"event": "postgres_target", "source": "env:POSTGRES_URI", "host": "localhost", "port": 5432,
   "dbname": "video_workflow", "user": "postgres", "password_present": true,
   "uri_masked": "postgresql://postgres:***@localhost:5432/video_workflow"}
  ```
- Worker `state: starting → running`，心跳持续；`worker.log` / `status.json` / `events.jsonl` 中 **0 处 `no password supplied`**；
- Postgres 实测该线程 checkpoint 数 **9**（持续增长）——`PostgresSaver` 连接与写入均正常；
- 图执行到人工关卡任务创建（`storyline_recommend_text` / `storyline_select_bgm`）。
- 终态回填：见文末「终态回填」。

### E-5 T9：故障任务恢复策略（§9）

- 故障线程 `914c74fc5bdf4d44b7e021a7d483a6e8-r2`：`status.json` 显示 `state=failed`、错误为 checkpointer 连接失败（`no password supplied`）——**失败发生在创建/初始化 checkpointer 之前**；
- Postgres 只读查询：该 thread_id 的 checkpoint 数 = **0**；
- 按 §9 判定：**checkpoint 不存在 ⇒ 使用新 thread_id 重新开始**；禁止对旧 ID 用 `mode=start`，亦无需 `continue_after_crash`（其前置条件「状态为 crashed/failed 且存在 checkpoint」不满足后半）；
- E-4 的新 turn 即为「新 thread_id 重新开始」的实际执行并成功。如需重做原故障任务的内容，在 UI 上发起新会话即可（机制已被 E-4 验证）；旧线程目录与日志按 §10 保留作诊断材料。

## 4. §11 实施检查清单回填

- [x] 核实 `start_production.ps1` 中 `POSTGRES_URI` 注入发生在 Uvicorn `Start-Process` 之前（C1 + 静态回归测试 + 16:05:50 日志）
- [x] 将 `AGENT_GATE_POSTGRES_URI` 加入环境变量备份/恢复，并在生产启动时清除遗留值（C1）
- [x] 检查操作员目录 `.mcp.json` 是否覆盖 Postgres 环境变量（阶段 A：未注入，非污染源）
- [x] 修复 `config.py` 对空字符串的处理（C2）
- [x] Worker 输出 DSN 来源和脱敏目标信息，不输出密码（C2 `postgres_target` 事件 + E-4 实测）
- [x] 新增 Worker 同路径诊断脚本（C3 `scripts/verify_agent_gate_postgres.py`）
- [x] 增加 DSN 优先级和 MCP→Worker 环境传播测试（C3 T1–T7）
- [x] 重启旧的、由本项目拥有的 OpenStoryline Web 进程（E-1/E-2：旧进程已死，直接以修正后脚本重启）
- [x] 验证 Worker 成功执行 `PostgresSaver.setup()`（E-4：running + checkpoint 持续写入）
- [x] 按 checkpoint 是否存在决定新建任务或从检查点继续（E-5：无 checkpoint ⇒ 新 thread_id）

## 5. 终态回填

- T8 终态：**`waiting_human`**（`events.jsonl`：`{"ts": "2026-10-31T08:20:56.814982Z", "event": "waiting_human", "checkpoints": ["①"]}`）。
  Worker 状态链 `starting → running → waiting_human`（设计内的人工关卡①「请在剪映客户端手动调整分镜顺序」挂起，非故障），`error=None`；
  全过程 `worker.log`/`status.json`/`events.jsonl` **0 处 `no password supplied`**；Postgres 中该线程 checkpoint 数 **40**。
  —— §8 验收通过条件全部满足：DSN 来源正确（`env:POSTGRES_URI`）、`PostgresSaver.setup()` 成功、无 `no password supplied`、状态到达 `waiting_human` 而非在 checkpointer 初始化阶段失败、既有 checkpoint 数据未被删除或重置（总数只增未减，故障线程之外的 1050 条原样保留）。

---

*执行：ZCode（wayyet 授权，阶段 E 重启采用 UAC 提权代跑、T8 采用浏览器自动化触发）；记录中不含真实密码或内部敏感信息。*

# auto-video-editor 环境变量传播链路优化 —— 阶段 D / E 执行记录

> 对应设计文档:`docs/修正/auto-video-editor_环境变量传播链路优化执行设计.md`
> 任务拆分依据:`docs/修正/auto-video-editor_环境变量传播链路优化执行设计_分阶段章节编写计划.md`(§9.6–9.7)
> 本次**只执行阶段 D(MCode/MCP 显式环境,commit ③)与阶段 E(回归测试与验收,commit ④)**;阶段 A/B/C 已于同日早前执行完毕(见《阶段ABC执行记录》)。

| 项 | 值 |
|---|---|
| 执行日期 | 2026-11-01 |
| 代码基线 | `3faedd4`(执行前 `main` HEAD;工作区另带未提交的"移除 mcode-chat"改动,非本链路,全程未触碰、未入库) |
| 本次提交 | commit ③ `7d88f55`(阶段 D)/ commit ④ `d2f0748`(阶段 E) |
| 环境 | 本机容器 `edb-postgres16`;真实 mcode CLI 0.6.12(`C:/Users/wayyet/.minimax-code/mcode.cmd`),操作员目录 `E:/Documents/kuaishou/video-ops` |

---

## 1. 阶段 D:MCode/MCP 显式环境(commit ③ `7d88f55`)

| 任务 | 结果 |
|---|---|
| D-T1 `process_env` 受控快照 | `mcode_chat_model.py` 新增模块级 `build_process_env()`(缺省 = 对当前进程环境拍快照;显式传入时缺失的 Windows 基础变量 `PATH/SYSTEMROOT/TEMP/TMP/USERPROFILE` 从当前进程环境补齐)与 `BASE_ENV_KEYS`;`MCodeChatModel` 新增 `process_env` 字段(显式注入用,缺省 `None` = 构造时快照)与 `_env_snapshot` 私有属性,由 `model_validator(mode="after")` 在**构造时**定格 —— agent 按 session 缓存模型实例,构造点靠近 Web 进程启动(阶段 C 已保证此时生产配置已注入),逐 turn 重读全局环境的漂移路径被关闭 |
| D-T2 四个创建点显式传 env | 同步 `_generate()`(`subprocess.Popen`)、异步 `_agenerate()`(`asyncio.create_subprocess_exec`)、能力探测 `_probe_stream_json_support()`、流式 `_astream_stream_json()`(`MCodeRunner(..., env=...)`,该参数 `mcode_runner.py` 原已支持)全部改为 `env=self._env_snapshot`。四点共用同一份快照对象 |
| D-T3 `.mcp.json` 审计结论落地 | 审计(与阶段 A 结论一致):`video-ops/.mcp.json` 的 `command` 指向主仓 `.venv` Python、`args`/outputs 均为当前项目绝对路径、`env` **未**覆盖 `POSTGRES_URI` / `AGENT_GATE_POSTGRES_URI`。**决策:不需要 MCP bootstrap wrapper** —— D-T4 实测证明真实 MCode 会把父进程环境完整传给 MCP 子进程(`.mcp.json` 的 `env` 是叠加键,不替代继承)。若未来 MCode 升级改变合并行为,worker 的 `postgres_target` 事件(B-T6)会立即暴露 `source` 缺失,无需新增观测手段 |
| D-T4 真实链路端到端实测 | 两段式,见下表 |

### D-T4 实测设计与结果

**方法**:真实 `mcode exec`(操作员目录内,按 `.mcp.json` 自动拉起 video-agent MCP),令其调用 `run_start`。第一段用**金丝雀 DSN**(真实 host/port/凭证 + 不存在的库名),第二段建一次性探针库走通全链。`APP_ENV=production` 全程注入(同时实测 B-T4 防护无误伤)。

| 段 | 注入 | 结果 |
|---|---|---|
| 金丝雀 | `POSTGRES_URI=…@127.0.0.1:5432/envprobe_d4_20261101`(库不存在) | MCP 进程内租约后端 fail-closed 拒绝 `run_start`,错误为 `FATAL: database "envprobe_d4_20261101" does not exist` —— host/端口/凭证**全部正确送达**(不是 `no password supplied`),证明 **MCode → MCP 的 env 继承成立**(P4),`APP_ENV=production` 下生产防护正常 |
| 探针库 | `POSTGRES_URI=…@localhost:5432/video_workflow_envprobe`(临时建库) | `run_start` 成功拉起 Worker → `events.jsonl` 落 `postgres_target`:`source=env:POSTGRES_URI / host=localhost / port=5432 / dbname=video_workflow_envprobe / password_present=true / uri_masked=postgresql://postgres:***@…` → 1.2s 后 `worker_started`(到达该事件即证明连接 + `PostgresSaver.setup()` 完成,探针库出现 `checkpoints` 系表 + 6 行 checkpoint)→ 立即 `taskkill /F /T` 收树,`DROP DATABASE` 清理(P5 + G1 实测) |

实测消耗:mcode exec 两轮(MiniMax-M3.1-Flash,约 42K/47K input tokens);Worker 在图早期即被收树,未产生有效视频工作流负载;探针库已删除,`outputs/envprobe-d4-20261101/`、`outputs/envprobe-d5-real-20261101/` 保留作审计痕迹(已走查无凭据)。

### 验收(编写计划 §4.7:§10.2 P3/P4/P5 + openstoryline pytest 全绿)

- P3(自动化):`openstoryline/tests/test_mcode_process_env.py` **12/12**(见 §2 E-T2);
- P4/P5(实测):见上表金丝雀段与探针库段;
- openstoryline 全量 pytest:3 个失败,全部为**既有失败**(见 §3 事件记录归因),其余全绿,含本次新增 12 例。

---

## 2. 阶段 E:回归测试与验收(commit ④ `d2f0748`)

| 任务 | 结果 |
|---|---|
| E-T1 解析器用例补齐(D1–D8) | `db/postgres_config.py` 新增 `_validate_uri()`:选出的 DSN 缺 scheme/host/database 或 port 非法时**启动前**抛明确配置错误(§10.1 D6),错误信息只报来源标签与缺失项,**不回显 URI 原文**(无效 URI 里也可能夹着凭据)。新增 `tests/unit/test_postgres_config.py` 17 例直测共享模块,不依赖真实数据库:D1 production 正向 / D2 production 拒绝(含大小写变体)/ D3 test 优先 / D4 空白回退 / D5 默认兜底 / D6 五种非法形状 + 两个变量位 + config 兜底档 / D7 百分号编码(解析层原样保留、解码交 psycopg;脱敏诊断编码/解码形态均不泄漏)/ D8 无密码 DSN 不被解析层拒绝 |
| E-T2 进程传播测试自动化 | 新增 `openstoryline/tests/test_mcode_process_env.py` 12 例:自带假 CLI 把「子进程**实际收到**的环境」按白名单写进记录文件,断言(P3)四个创建点收到同一份快照、(P2)构造后全局环境变化(改值/新增键)不泄漏进子进程、显式 `process_env` 语义(完整替代 + 基础变量补齐)。`agent_gate/tests/test_postgres_target.py` 回填:G4(不可达 → ≤30s 快速失败,错误含 thread_id、不含密码)、G8(production + 测试库变量 → `make_checkpointer` 连接前抛 `RuntimeError`) |
| E-T3 端到端验收(G1–G8 + 脱敏走查) | 见下表证据矩阵 |

### G 系列证据矩阵(§10.3)

| 用例 | 结论 | 证据 |
|---|---|---|
| G1 Worker 连接 + `PostgresSaver.setup()` | ✅ | D-T4 探针库实测(真实链路 `worker_started` + checkpoints 表落成);T7 用例(toy graph 至 `running`) |
| G2 主图 `AsyncPostgresSaver.setup()` | ✅ | `tests/integration/test_postgres_checkpointer.py` 3 例通过;另以内联冒烟复核 `graph.get_checkpointer()` → `setup()` 成功(SelectorEventLoop) |
| G3 两条路径同一数据库目标 | ✅ | 同一组 env 下,worker 同路径 `verify_agent_gate_postgres.py` 与主图 `graph.get_checkpointer()` 均解析 `source=env:POSTGRES_URI / video_workflow`(共用 `db.postgres_config`,B 阶段已验同一函数对象) |
| G4 不可达快速失败 | ✅ | 新增 G4 用例(127.0.0.1:9,`POSTGRES_CONNECT_TIMEOUT=2`,<30s,诊断含 thread_id) |
| G5 crash continuation | ✅ | 既有 `test_lifecycle.py`(`continue_ok_when_crashed/failed`)+ `test_mcp.py::test_mcp_run_start_continue_after_crash`(本日全量绿) |
| G6 checkpoint 不存在拒绝 | ✅ | 既有 `test_lifecycle.py` 两条 `no_checkpoint` 用例 |
| G7 `waiting_human` 只能 resume | ✅ | 既有 `test_lifecycle.py` resume 系用例(`resume_ok / not_waiting_human / checkpoint_unknown`) |
| G8 生产测试库泄漏拒绝 | ✅ | 新增 G8 用例 + D-T4 金丝雀段实测(`APP_ENV=production` 全程) |
| 日志脱敏走查 | ✅ | 两个探针 thread 目录全文 `grep devpass` / 完整 DSN **零命中**;事件仅含 `uri_masked` 脱敏形态 |

### 测试命令与结果(§10.4)

| 套件 | 结果 |
|---|---|
| `tests/unit`(主仓) | exit 0,全绿(1 skip 与本次无关),含新增 17 例 |
| `tests/integration/test_postgres_checkpointer.py` | exit 0,3 例全过 |
| `agent_gate/tests` 全量 | 第 1 次运行 2 例 `ConnectionTimeout`(与容器被外部停止同时发生,见 §3);第 2 次运行 2 例负载型失败(worker 停 `starting` / 图片批次乱序);**第 3 次运行 exit 0 全绿**;且历次失败用例在隔离 / 模块内复跑均绿 —— 判定为顺序/负载敏感的既有抖动,失败集合与本次改动无关(改动不触及 mailbox/任务排序/DSN 有效路径) |
| `openstoryline/tests` 全量 | 3 个失败,**全部既有**(见 §3 归因),其余全绿(含新增 12 例) |

---

## 3. 事件记录

- **Postgres 容器再次被外部停止**:`docker events` 显示 `kill → stop → die`(干净退出),时间点恰在 agent_gate 第 1 次全量运行中段 —— 与《阶段ABC执行记录》§4 的 Docker 事件同型,非本链路改动所致(仓库测试代码无任何 stop 调用)。处置:`docker start edb-postgres16` → healthy → 第 3 次全量全绿。两次失败运行中受影响的 4 个用例均已在恢复后单独/模块内复绿。
- **openstoryline 3 个既有失败归因**(与本次改动无关,与记忆判据一致):根因均为 `config.toml.local` 的 `[zcode].cwd` 经 `load_settings` 自动合并泄漏 —— cwd 非空使模型下拉出现 `ZCode Agent` 选项,连带 `test_glm_preset.py::test_menu_label_is_glm_not_zcode`(菜单断言)、`test_web_mcode_wiring.py::test_default_model_stays_builtin_when_mcode_not_configured`(默认档位断言)、`test_zcode_config.py::test_committed_config_toml_stays_neutral`(本地配置中性断言)三例失败;本次改动文件不在其依赖路径上。
- 实测期间真实 mcode 会话消耗两次输入(约 42K/47K tokens);未触碰生产库数据,探针库已删除。

## 4. 提交与回滚

| 提交 | 内容 | 回滚方式 |
|---|---|---|
| `7d88f55` | 阶段 D:`openstoryline/src/open_storyline/mcode_chat_model.py` | `git revert 7d88f55`(或按计划 `git checkout -- openstoryline/src/open_storyline/mcode_chat_model.py`) |
| `d2f0748` | 阶段 E:`db/postgres_config.py`(D6 校验)、`tests/unit/test_postgres_config.py`(新)、`openstoryline/tests/test_mcode_process_env.py`(新)、`agent_gate/tests/test_postgres_target.py` | `git revert d2f0748`;测试文件单独回退不影响功能代码(仅 `_validate_uri` 属功能改动,随 commit 整体回退) |

全程未改 PostgreSQL schema、未动既有 checkpoint 表与数据卷(§12 回滚约束);探针库为一次性新建后删除。

## 5. 未执行项与遗留

- **D-T3 bootstrap wrapper**:判定不需要(依据 D-T4 实测);该决策的前提是 MCode 继续完整继承父进程 env,若升级后行为变化,`postgres_target` 事件的 `source` 字段是现成的回归观测点;
- **C-T2 第二阶段(可选,属阶段 C)**:仅监听 127.0.0.1 的诊断端点返回非秘密配置指纹 —— 未做;
- 设计文档与编写计划两份 `docs/修正/*.md` 仍为 untracked,是否入库由用户决定(与《阶段ABC执行记录》§6 相同)。

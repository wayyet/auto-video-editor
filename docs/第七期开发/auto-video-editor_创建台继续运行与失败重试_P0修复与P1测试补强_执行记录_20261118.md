# 创建台「继续运行」与失败节点重试 — P0 风险修复与 P1 测试补强 执行记录

> 执行日期:2026-11-18
> 依据文档:《auto-video-editor_创建台继续运行与失败节点重试_设计审查及执行计划_20261118.md》(§6 分阶段执行计划;下称"审查计划")
> 前置基线:2026-11-17 已交付的继续运行/失败重试功能(4 提交 15c0ad9→9d8f512,随 befbb1f 批次推送)
> 本次范围:P0-01~P0-06 全部修复 + P1 自动化测试补强;**P2 真机验收留操作员**(清单见 §5)

---

## 1. 执行摘要

按审查计划 §6.1/§6.2 完成 6 项 P0 修复与 11 项 P1 测试,全程"先补测试再改实现"。所有修复零新增回归:全量 unit 仅 1 失败(剪映运行中的已知环境性失败),integration 失败经 HEAD worktree 对照**逐字一致**(全部环境性/存量),前端 80/80 全绿,新增 checkpoint 恢复集成测试 2/2 全绿。

两条用户拍板决策(会话内 AskUserQuestion 确认):

1. **范围 = P0+P1**;P2 真机验收输出操作员清单,不实跑。
2. **无 draft_path 且物理确认无草稿、剪映开着 → 放行**(审查计划 §7.4 矩阵"按策略决定"格)。依据:没有草稿可保护,剪映开不开与续跑无关(resume_workflow.py 既有注释);工作流写草稿侧另有 `JY_WRITE_FAIL_ON_JIANYING_RUNNING` 护栏兜底(剪映运行时写入必失败)。

---

## 2. P0 修复明细

### P0-01/P0-02 重试原子互斥(含人工恢复与失败重试统一互斥)

- **根因**:`resume_run`/`start_run` 在 `_ACTIVE_LOCK` 内查完 `_ACTIVE_PROCS` 即放锁,PG 预检→Popen→注册在锁外,两个并发请求可在窗口内同时通过检查,同一 thread 双进程恢复。
- **修复**:`web/console_service.py` 新增 `_CLAIMED_TIDS` 占用登记表 + `_try_claim_thread()`/`_release_claim()`。状态门禁通过后、启动链路之前原子占用(占用失败立即 409,不排队);spawn 成功注册进 `_ACTIVE_PROCS` 转正或任一步失败 finally 释放。人工恢复与失败重试走同一函数,同一 thread 级占用天然互斥。`cancel_run`/`delete_run` 对占用中 thread 返回 409。
- **部署边界声明**(代码注释):生产为单 uvicorn 进程(start_production.ps1 无 `--workers`),进程内锁即跨请求互斥;**多 worker/多实例部署必须换共享存储租约**(审查计划 §4.1 条款 6)。
- **测试**(`tests/unit/test_console_resume.py::TestAtomicClaim`):双线程 Barrier 对齐并发同 thread → 恰一个受理一个 409、popen 恰一次;占用中 resume/start/cancel/delete 全 409;不同 thread 互不阻塞;释放后恢复放行。

### P0-03 无 draft_path 物理草稿检测

- **根因**:`web/gate_state.py resume_guard(allow_missing_draft=True)` 在 state 无 `draft_path` 时直接放行,连剪映进程检查都跳过——"checkpoint 无 draft_path"被当成"磁盘无草稿"。
- **修复**:
  - `draft_ops/draft_lock.py` 新增 `find_task_draft_artifacts(task_dir, draft_root, thread_id)`:只读定位 staging(`task_dir/drafts` 限深 2 的 `draft_content.json`,中英分支全覆盖)+ 正式库(`JIANYING_DRAFT_ROOT/AutoVideo_<thread_id>`,复用 `jianying_registry.official_draft_dir` 保持命名零漂移);目录遍历 OSError → `unreadable=True`。绝不删除/覆盖文件。
  - `web/gate_state.py resume_guard` 新分支:定位到产物 → 对**每一份**跑 `assert_resume_ready` 全量门禁(含剪映进程检查),结果附 `located`;确认无产物 → 放行附 `note`("已确认无本任务草稿产物…");`unreadable` → fail-closed 拒绝。
- **测试**(`tests/unit/test_console_resume_guard.py::TestFindTaskDraftArtifacts/TestPhysicalDraftArtifacts`):定位规则 3 例 + 审查计划 §7.4 矩阵(staging 有→门禁执行;official 有→门禁执行;产物+剪映开→拒绝;无产物→放行带 note;不可读→fail-closed;真文件+剪映关→全量通过且 hashes 落审计)。

### P0-04 PostgreSQL 初始化链路异常归一化

- **根因**:`scripts/resume_workflow.py` 的 `apply_migrations_to_db`/`get_checkpointer` 连接/`build_graph`/`g.aget_state` 均无异常边界,DB 挂掉以未捕获异常退出(退出码 1="图执行失败",语义误导且绕过退出码 7)。
- **修复**:初始化链路逐段 fail-closed——任一段异常打印阶段标签(应用层迁移/checkpointer 初始化/图构建/checkpoint 状态读取)后统一返回 `EXIT_NO_CHECKPOINT(7)`;`run_with_events` 不在保护块内(其异常→退出码 1 语义不变);checkpointer 上下文在异常/正常两路都正确 `__aexit__`。不新增退出码,模块 docstring 退出码 7 语义同步扩写。
- **测试**(`tests/unit/test_resume_workflow_script.py::TestInitFailureFailClosed`):迁移失败/checkpointer 连接失败/aget_state 失败三例 → 均退出码 7 且 spy 断言 `run_with_events` 未被调用。

### P0-05 子进程退出状态回收 + 服务重启对账

- **根因**:`_prune_active_procs` 对退出进程只做 pop——不记退出码、不收敛 `starting`;服务重启后遗留 `starting` 无对账,状态悬挂。
- **修复**(`web/console_service.py`):
  - 进程退出即收尾 `_finalize_attempt_on_exit`:退出码→末条 `resumes[]` 条目补 `finished_at/exit_code/result`(0→succeeded;1→failed_execution;2/4/5/6/7→failed_preflight;130→cancelled;其他→failed_unknown);exit≠0 且事件库无可用终态 → record 从 starting 收敛为 failed。**工作流状态与尝试状态分字段记录,绝不覆盖事件库真实终态**。
  - 落盘 `last_proc_exit` 收割标记,供对账区分"收割器已观察过本次退出"与"服务死亡导致从未收割"。
  - 新增 `reconcile_stale_starting()`(reap_now 调用,60s 节流):仅收敛"末条 attempt 无 finished_at 且无收割标记 + 不在注册表 + PID 已死"的 starting/running 记录——派生 waiting_human 不动(正常挂起稳态)、completed→completed、其余→failed 并附对账说明。PID 仍活(入口 A 直启/PID 复用)绝不误判。
- **测试**(`tests/unit/test_console_resume.py::TestAttemptLifecycle/TestStaleReconcile`):CLI 提前退出(exit=7 无事件)→ attempt=failed_preflight + record failed;exit=0 → attempt=succeeded + 状态交调和(对账不误翻);exit≠0+事件库已有 run.failed → 事件库为准;重启遗留(未收尾 attempt)→ failed+收尾;已收割 exit=0 记录不被对账翻面;waiting_human 稳态不动;活 PID 跳过。

### P0-06 attempt id 贯穿

- **修复**:`resume_run` 生成 `attempt_id`(uuid4 hex 12),写入 `resumes[]` 条目 + 经 env `AUTO_VIDEO_RESUME_ATTEMPT_ID` 注入子进程 + resume API 202 响应返回;`resume_workflow.py` 开头打印 `[resume] attempt=<id> thread=<tid>`(手动 CLI 无 env 打 "-" 不失败);前端重试受理提示与查看模式展示该标识。
- **测试**:env 注入值与审计条目一致;202 body 含 attempt_id;查看模式格式化函数用例 4 例(见 §3 前端)。

---

## 3. P1 测试补强

| 编号 | 内容 | 落点 | 结果 |
|---|---|---|---|
| P1-01 | 串行失败恢复(A 不重复、B 重试) | `tests/integration/test_checkpoint_recovery.py` | 过 |
| P1-02 | 并行 superstep pending writes(b_ok 不重放/b_fail 重试/join 一次) | 同上 | 过 |
| P1-03 | 无 checkpoint → EXIT 7 | 既有 `test_resume_workflow_script.py` 覆盖 + 本次扩展 | 过 |
| P1-04 | 初始化各阶段异常 → 7 且不跑图 | `TestInitFailureFailClosed` | 过 |
| P1-05/06 | 草稿物理存在性矩阵 | `TestPhysicalDraftArtifacts` | 过 |
| P1-07/08 | 并发同 thread 409 / 不同 thread 放行 | `TestAtomicClaim` | 过 |
| P1-09 | 子进程提前退出 attempt 收敛 | `TestAttemptLifecycle` | 过 |
| P1-10 | 前端门禁回归(canResume 不放行 failed) | `web/tests/workflow_console.test.mjs` 全量 | 80/80 |
| P1-11 | SSE/展示回归 + attempt 展示 | 同上(formatAttemptEntry 4 例新增) | 过 |

P1-01/02 的 checkpointer 用 `AsyncPostgresSaver`(与生产 `graph.get_checkpointer` 同类型)指向 `isolated_postgres_db` 隔离库,非内存 saver;Windows 下以同步测试 + `asyncio.run(loop_factory=SelectorEventLoop)` 驱动(async psycopg 纪律)。pending writes 语义实测:同一 superstep 内已成功节点的写入不重放执行(与 LangGraph fault-tolerance 文档一致),测试把该语义钉住。

---

## 4. 测试基线与对照证据

- **全量 unit**:2408 collected;唯一失败 `test_node_09_inject_fx.py::test_inject_fx_deterministic_across_pythonhashseed` —— 执行时剪映(JianyingPro.exe,PID 29604 等三个进程)正在运行,草稿写入互斥拒绝子进程写入 = 已知环境性,与 20261117 交付记录一致,勿当回归。
- **全量 integration**:8 失败 = 7 个剪映写入互斥(`JY_WRITE_FAIL_ON_JIANYING_RUNNING`,atomic_writer.py:267)+ 1 个 auto_e2e split_shots 空镜头存量。已在 worktree(HEAD=9d27e29,不含本次改动)对照重跑同批用例,**失败逐字一致** → 零新增失败。对照 worktree 已清理。
- **前端**:`node --test web/tests/*.test.mjs`(glob 展开)80/80 全过。
- **本次新增/修改测试**:unit 90 例(4 个目标文件)+ 集成 2 例,复跑全绿(92 passed)。
- 口径说明:2408 收集数含 fx 池线(并行会话)工作区未提交的测试用例;提交后数字以当次全量为准。

---

## 5. P2 真机验收清单(操作员执行,审查计划 §6.3)

| 编号 | 场景 | 操作 | 预期 |
|---|---|---|---|
| P2-01 | 人工关卡① | 新任务运行至分镜顺序关卡,点「继续运行」 | 恢复面板正确显示,人工确认后继续 |
| P2-02 | 人工关卡② | 运行至 BGM 确认关卡 | 恢复面板及人工输入正确,工作流继续 |
| P2-03 | 失败节点重试 | 可控失败节点,点「重试失败节点」并确认 | 202 受理(带 attempt=),失败节点重新执行,终态正确 |
| P2-04 | 取消重试确认 | failed 点按钮后取消 confirm | 不提交 API,不产生 attempt |
| P2-05 | 剪映开+草稿存在 | 保持剪映运行并重试 | 拒绝恢复(409),不写入草稿 |
| P2-06 | 剪映关+草稿有效 | 关闭剪映后重试 | 门禁通过,续跑 |
| P2-07 | 无 draft_path 但有草稿文件 | 模拟节点写草稿后异常 | 仍触发物理草稿门禁(拒绝或全量校验) |
| P2-08 | 确认无草稿 | 产草稿前失败 | 放行续跑(附"已确认无草稿产物"审计) |
| P2-09 | 服务重启对账 | 重启服务后查看遗留任务 | starting 收敛为明确终态,不重复启动 |
| P2-10 | 监控页回归 | monitor.html 查看 failed 任务 | 监控页恢复门禁不变(canResume 仍只认 waiting_human) |

验收要点:真实重试消耗 LLM 调用与时间;P2-05/07 建议在剪映打开状态下先验证拒绝路径。前端改动强刷即生效,无需重启服务。

---

## 6. 遗留与边界

- **多实例部署**:互斥依赖进程内 `_CLAIMED_TIDS`;单 uvicorn 进程部署下成立。若未来多 worker/多实例,必须先换共享存储(DB 锁/租约),不得以进程内表为唯一保障。
- **attempt 结果与终态的关系**:202=受理,attempt result=尝试审计;工作流终态永远以事件库调和为准,attempt 审计不反向覆盖。
- **推送**:本次 6 笔提交待推送,按惯例经 sync-github 技能单独确认后执行。
- **不改动项**:SCHEMA_VERSION、数据库 schema、事件表、工作流节点拓扑、监控页 `canResume` 语义均未动。
- 关联文档:《…设计审查及执行计划_20261118.md》(本次修复的需求与验收来源)、《…创建台继续运行按钮与失败节点重试_交付说明_20261118.md》(2026-11-17 功能交付记录)。

# 创建台「继续运行」按钮与失败节点重试 — 交付说明(2026-11-18)

> 交付完成时间:2026-11-17(本地 4 提交 `15c0ad9 → 9d8f512`)
> 详细执行记录(含分阶段代码细节):[docs/第六期开发/执行记录_创建台继续运行按钮与失败重试_20261117.md](../第六期开发/执行记录_创建台继续运行按钮与失败重试_20261117.md)

## 1. 需求背景

操作员在创建台页面(console.html)「创建并运行工作流」按钮旁的红圈空位处需要一个按钮,点击后继续运行当前工作流:

1. 工作流停在人工关卡(关卡①分镜顺序 / 关卡②BGM 确认,状态 `waiting_human`)→ 继续运行;
2. 某个节点报错(状态 `failed`)→ 重试该节点。

## 2. 现状核实结论(实施前探索)

- **关卡恢复链路前后端已全齐**:`POST /api/console/runs/{tid}/resume`(`web/api_console.py`)→ `console_service.resume_run`(`web/console_service.py`)→ 子进程 `scripts/resume_workflow.py` 注入 `Command(resume=...)`;创建台已有恢复面板(waiting_human 时自动弹出)。**此场景零后端改动**。
- **failed 重试此前明确不支持**(双重拦截):
  - API 层:非 `waiting_human` 一律 409(`console_service.resume_run` 状态闸门);
  - CLI 层:无挂起关卡即退出码 4(`scripts/resume_workflow.py`)。
- **LangGraph 能力已在仓库内验证**:`run_with_events(g, None, config)`(None 输入)从最后 checkpoint 续跑,失败 superstep 写入未提交 → 失败节点重新执行。先例 = `agent_gate/runner.py` 的 `continue_after_crash` 模式;`tests/integration/test_workflow_stream_events.py` 已锁定该语义。缺口只是入口 A 恢复管线未暴露它。

> **词汇说明**:checkpoint(检查点)= LangGraph 工作流每跑完一个节点就把状态存进 PostgreSQL;失败节点的写入不会落盘,所以"从 checkpoint 续跑"= 回到最后一个成功节点之后,把失败节点重新执行,不是从头重跑。

## 3. 交互决策(操作员拍板)

| 场景 | 行为 |
|---|---|
| `waiting_human`(关卡中断) | 点按钮 → **弹出现有恢复面板**(显示挂起关卡内容 / QC 警告 / 草稿入口),人工点「确认恢复」才放行 |
| `failed`(节点失败) | 点按钮 → **先弹 confirm 确认框**(真实重跑会消耗 LLM 调用与时间),确认后提交重试 |

## 4. 按钮行为表

| 任务状态 | 按钮表现 | 点击行为 |
|---|---|---|
| 停在人工关卡(waiting_human) | 显示「**继续运行**」 | 弹出现有恢复面板,人工确认后放行 |
| 节点失败(failed) | 显示「**重试失败节点**」 | confirm 确认后从最后 checkpoint 续跑,重跑失败节点 |
| 已完成 / 已取消 / 运行中 / 未启动 | 隐藏 | — |

补充语义:**cancelled(已取消)不放行**(终态语义,只能新建任务);prepared / completed / unknown 仍 409。查看模式(`?thread_id=`)下按钮同样可用。

## 5. 修改内容(4 提交明细)

### 阶段 1:CLI 续跑分支 — `15c0ad9`

`scripts/resume_workflow.py`:

- argparse 新增 `--continue`(dest=`continue_run`,避开 Python 关键字 `continue`;与 `--checkpoint` 互斥,同给 → 退出码 2);
- 新增退出码 `EXIT_NO_CHECKPOINT = 7`;
- 新增 `_continue_after_failure` 辅助函数(fail-closed):
  - `saver.aget_tuple(config)` 为 None(Postgres 确无 checkpoint)或查询抛异常(数据库不可达)→ 一律退出码 7,语义对齐 `agent_gate/runner.py` `check_mode` 的 `no_checkpoint` / `database_unavailable`(不 import agent_gate,用同一 saver 连接);
  - state 有 `draft_path` 时照常执行 `assert_resume_ready`(剪映关闭 + 草稿校验);失败发生在产草稿前则跳过(无草稿可保护);
  - `run_with_events(g, None, config)` 续跑 → 失败节点重执行;落点提示 / `_check_final_state` 复用既有逻辑;
- `_resume_async_impl`:无挂起关卡时不给 `--continue` 保持原行为(退出码 4,文案提示可加 `--continue`);给 `--continue` 走续跑分支。**有挂起关卡时 `--continue` 不改变原 resume 行为**;
- 事件流零改动(复用 `run.resumed` / `node.resumed` / 终态事件),SCHEMA_VERSION 不 bump。

### 阶段 2:API 层放行 failed — `0465fa0`

- `web/console_service.py` `resume_run`:
  - 状态闸门:`state != "waiting_human"` → `state not in ("waiting_human", "failed")`,409 文案改为「未处于可恢复状态(仅等待人工关卡可恢复、失败可重试)」;
  - `retry_mode = (state == "failed")` 分支:argv 不带 `--value` / `--checkpoint`,改带 `--continue`;`--llm-backend` 仍取 record 固化值;
  - 门禁:retry 模式且未注入 draft_guard 时透传 `allow_missing_draft=True`;注入门禁(单测)保持 `(thread_id)` 原签名;
  - `resumes[]` 审计新增 `mode` 键(`resume` / `retry`)。
- `web/gate_state.py` `resume_guard` 新增 `allow_missing_draft: bool = False`:state 无 `draft_path` 时直接放行(不碰 `assert_resume_ready`);有草稿时不生效。
- `web/api_console.py` resume 端点 docstring 更新(无 schema 变化,ResumeBody 复用)。关卡③墓碑检查对 failed(无挂起)自然放行。

### 阶段 3:前端按钮 — `fa5511f`

- `web/static/workflow_console.mjs`:新增导出纯函数 `canResumeOrRetry(state)`(`waiting_human ∪ failed`);**`canResume` 保持不动**(监控页 monitor.html 行内「恢复」仍只认 waiting_human,行为零变化)。
- `web/console.html`:
  - `btnCreate` 外包 flex 行容器,后插入 `<button id="btnResumeRun">`(CSS `#btnResumeRun{margin-top:12px}`);
  - `renderRunView()`:仿 `btnCancel` 显隐模式 —— `liveThreadId && canResumeOrRetry(st)` 才显示;文案 / title 随状态切换;查看模式不额外禁用;
  - 点击处理:waiting_human → `showResumePanel()`(现有面板自动拉 gates);failed → `window.confirm` → `postJson(resumeRun, {checkpoint:null, value:true})` → 成功后仅当 `!source || source.readyState === 2`(EventSource 已彻底关闭)才 `subscribeToThread(tid)` 整体重置,其余情况靠 EventSource 自动重连按 Last-Event-ID 游标补发(保住右栏历史事件表);
  - 说明区文案补充按钮用法。

### 阶段 4:测试 + 执行记录 — `9d8f512`(测试随 `fa5511f` 入库,文档单独提交)

- `tests/unit/test_console_resume.py`:新增 `TestRetryRunService`(failed 202 + `--continue` argv 且无 `--value`/`--checkpoint` + mode=retry 审计;completed 409;record.status=cancelled 409;默认门禁透传 `allow_missing_draft=True` 断言;retry 门禁不过 409 不启动进程;API 级 retry 202);存量断言适配契约变更(审计字典加 `mode` 键、409 文案语义扩宽)。
- `tests/unit/test_console_resume_guard.py`:补 `resume_guard(allow_missing_draft=True)` 无草稿短路放行 / 有草稿不生效两用例。
- `tests/unit/test_resume_workflow_script.py`:新增 `TestContinueAfterFailure`(`--continue` 解析;与 `--checkpoint` 互斥退出码 2;无 checkpoint 退出码 7;DB 查询失败 fail-closed 退出码 7;dry-run 确认 checkpoint 后退出码 0;有草稿门禁不过退出码 6)。
- `web/tests/workflow_console.test.mjs`:补 `canResumeOrRetry` 门禁用例(含 `canResume("failed")` 仍 false,锁定监控页零变化)。

## 6. 验证结果

- 相关后端单测(test_resume_workflow_script / test_console_resume / test_console_resume_guard / test_console_gates_api / test_resume_workflow_llm_backend):pytest **exit=0**。
- `node --test web/tests/*.test.mjs`(glob 形式,仓库根):**76 pass / 0 fail**。
- 全量 unit(不带 `-x` 跑满 100%):**2159 通过 / 1 失败 / 0 错误**;唯一失败 =
  `test_node_09_inject_fx.py::test_inject_fx_deterministic_across_pythonhashseed`,
  为**环境性失败**(执行时剪映客户端正在运行,`draft_ops/atomic_writer` 草稿写入互斥严格模式拒绝子进程写草稿)——失败链路(draft_ops/nodes)与本次改动零交集,关闭剪映后可复验通过。**后续跑全量基线时若剪映开着,此失败勿当回归**。
- **浏览器真机冒烟 6 项全过**(2026-11-17,服务在跑,前端改动强刷即生效):
  1. failed 任务 console-52373c6f9e11:按钮显示「重试失败节点」,title 提示正确;
  2. waiting_human 任务 console-606615102af3:按钮显示「继续运行」,恢复面板自动激活;
  3. completed 任务 console-31a8a8e9edd2:按钮隐藏;
  4. cancelled 任务 console-e075a42bf351:按钮隐藏(该任务页面 SSE 派生显示「运行中」为既有行为,与按钮门禁双保险均隐藏);
  5. running 任务 console-f3466a531b85:按钮隐藏;
  6. failed 任务点击按钮 → confirm 弹窗出现 → 取消 → 未提交任何请求(服务端核实 `resumes=None`,状态仍 failed)。

## 7. 生效条件与遗留事项

### ⚠️ 生效条件

- **前端改动已即时生效**(console.html 由 FileResponse 每次读盘返回,强刷浏览器即可)。
- **后端改动需重启服务才生效**(`console_service.py` / `gate_state.py` / `api_console.py` / `resume_workflow.py` 均为进程内加载):未重启前,点「重试失败节点」确认后会收到 409(旧代码只放行 waiting_human)。关卡恢复(waiting_human)不受影响,旧代码本就支持。重启 = 管理员 pwsh7 跑 stop_production.ps1 / start_production.ps1。

### 遗留(待操作员)

1. **真机实跑一次真实重试**(会真实消耗 LLM 调用与时间),验收清单:
   - 新任务跑到关卡① → 创建台「继续运行」→ 恢复面板 → 确认恢复 → SSE 续跑;
   - failed 任务 → 「重试失败节点」→ confirm → 202 → 事件流出现 `run.resumed` + 失败节点重新 `node.started` → 最终到终态;
   - failed 任务在剪映未关闭时重试 → 409「恢复前检查未通过」(有草稿场景);
   - 失败发生在产草稿前(无草稿)的重试 → 不要求关剪映,直接续跑。
2. **4 提交待 sync-github 推送**(`15c0ad9 / 0465fa0 / fa5511f / 9d8f512`)。
3. 本文档随下次 sync-github 一并入库(当前未提交)。

## 8. 边界与语义备忘

- **重试语义 = 重跑失败节点**:LangGraph 从最后提交的 checkpoint 续跑,失败 superstep 的写入未提交;不是"从任务最开始重跑"。
- **checkpoint 缺失 / PG 不可达 fail-closed**:服务层 202 后由子进程校验,失败以退出码 7 落 `console_run.log`;事件库不会有 `run.resumed`,前端停在 failed 态可再次重试。
- **监控页零变化**:monitor.html 行内「恢复」按钮仍只认 waiting_human(`canResume` 未动);创建台新按钮用独立的 `canResumeOrRetry`。
- **事件流零改动**:复用 `run.resumed` / `node.resumed` / 终态事件,SCHEMA_VERSION 未 bump。

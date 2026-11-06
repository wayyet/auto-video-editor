> ⚠️ **历史文档(2026-11-04 单流水线架构收敛后失效)**:本文档描述的旧网页聊天端驱动 mcode/zcode 链路(①对话回合循环)已删除;现行架构与 mcode 无人值守用法(`agent_gate drive`)见 `README.md` 与 `docs/integration/architecture_decision_record.md`。内容保留仅作历史记录。

# ZCode Agentic 接入 · 阶段 C / D / E 执行记录

> **执行日期**：2026-10-31
> **执行依据**：`docs/修正/auto-video-editor_ZCode_Agentic_接入执行设计.md`（96c5de2）§10
> **前置状态**：阶段 A/B 已执行归档（1ae429e，记录：`auto-video-editor_ZCode_Agentic_接入_阶段AB执行记录.md`）
> **本次范围**（按用户指令）：仅执行阶段 C（GLM API 预设）、阶段 D（操作员租约）、阶段 E（内嵌 Runtime，按门禁仅 E-T0）；阶段 A/B 不重复执行

---

## 1. 回归基线（动手前，§8.2 三套）

| 套件 | 命令 | 结果 |
|---|---|---|
| 主仓 tests | `.venv` `python -m pytest tests -q` | **exit 0** |
| agent_gate | `.venv` `python -m pytest agent_gate/tests -q -p no:cacheprovider` | 首轮 1 项失败 → 见下 |
| openstoryline | `openstoryline\.venv` `pytest openstoryline/tests -q` | **exit 0** |

**基线唯一失败项**：`agent_gate/tests/test_cli_client.py::test_worker_with_mcode_cli_backend`
（worker 心跳过期判 crashed，`error=None`，240s 超时）。
**复跑单独执行 exit 0 通过** —— 判定为既有环境抖动：该用例依赖真实 mcode CLI
子进程回话，全量套件高负载下心跳窗口偶发超标；非本次改动引入（改动前即失败）。
该用例在后续每次全量运行中均通过。

## 2. 阶段 C：GLM API 模型预设（对应 P1）

**提交**：`feat(openstoryline): 新增 GLM API(BigModel) 模型预设(阶段 C)`

| 编号 | 结论 | 证据 |
|---|---|---|
| C-T1 文本对话验证 | **阻塞：待验证**。本机无可用 BigModel API Key（环境变量仅 `ZCODE_OFFICIAL_ENDPOINT_TOKEN`，属 ZCode 自身凭据，按 §7 约束 1 不得读取复用）。用户确认"暂无 Key，先写代码" | 本记录 §2 阻塞说明 |
| C-T2 图像/结构化输出验证 | **阻塞：待验证**（同上）。实现侧按设计兜底：VLM 预设需显式配置 `vlm_model` 才出现，未验证前不默认放开（"不满足则只做 LLM 预设"） | 同上 + C-T3 实现 |
| C-T3 预设实现 | **完成**。`config.py` 新增 `GLMConfig`（`[glm]` 段，default_factory 兼容旧配置）；`agent_fastapi.py` 新增 `GLM_MODEL_KEY = "GLM API / BigModel"`——该 key 即前端下拉显示文本（app.js 直接渲染模型 key 作标签），**app.js 零改动即满足菜单标识要求**；凭证齐全才进 LLM 下拉；ensure_agent 走 ChatOpenAI 链路（与内置模型同构） | 提交 diff；`test_glm_preset.py` |
| C-T4 Key 不入 Git | **完成**。入库 `config.toml` 的 `[glm].api_key` 为空字符串（有专门测试盯住）；真 Key 只放 `config.toml.local` 或 `OPENSTORYLINE_GLM_API_KEY` 等环境变量 | `test_config_toml_glm_section_has_no_secret` |
| C-T5 测试与回归 | **完成**。`test_glm_preset.py` 14 项离线接线测试全过（含命名红线：任何模型选项不得出现 ZCode 字样；base_url 默认通用 endpoint 非 Coding Plan）；`test_glm_live.py` 4 项 live 用例（文本/结构化/图像/超时）在无 Key 时自动跳过，配 Key 后即可执行 C-T1/C-T2；openstoryline 全量回归 exit 0 | 见 §5 回归汇总 |

**设计要点**（偏离说明）：GLM 解析未复用 `_resolve_builtin_model_override`——
那个函数对 `[llm]/[vlm]` 是"任一缺失整组回落环境变量"，会把 GLM 的默认
model/base_url 一并冲掉；GLM 改为按字段逐项合并（config 优先、环境变量补洞）。

**补验指引**（拿到 Key 后执行）：

```powershell
$env:OPENSTORYLINE_GLM_API_KEY="sk-..."
.\openstoryline\.venv\Scripts\python.exe -m pytest openstoryline\tests\test_glm_live.py -q
```

## 3. 阶段 D：线程级操作员租约（P2 前置）

**提交**：`feat(agent-gate): 线程级操作员租约,防多 Agent 消费同一 thread(阶段 D)`

| 编号 | 结论 | 实现要点 |
|---|---|---|
| D-T1 数据结构 | **完成** | 新增 `agent_gate/lease.py`；表 `agent_gate_operator_leases`：`thread_id` 主键 + owner/host/pid/run_id/acquired_at/expires_at/renew_count；owner 身份三级解析：`AGENT_GATE_OPERATOR_ID` 环境变量 > 显式 default > 进程级自动 ID（进程内缓存，防自锁） |
| D-T2 原子性 | **完成** | 取得/续约/接管全部在单事务（`SELECT … FOR UPDATE` 行锁）内完成；PostgreSQL 主键唯一约束保证并发 INSERT 单赢家；并发竞争测试 8 线程恰 1 赢家通过。表由 lease 模块自建（IF NOT EXISTS）——与 LangGraph checkpoint 表同一分工先例，因 agent_gate DSN（`AGENT_GATE_POSTGRES_URI` 优先）可能与应用库不同 |
| D-T3 owner 校验 | **完成** | `run_start`/`continue_after_crash`（`spawn_worker`）**只检查**：其他活操作员持有时返回稳定错误码 `operator_already_claimed`（附 owner + expires_at）；`gate_next_tasks`（`wait_for_work`）/ `gate_submit_result` / `gate_submit_batch` 取得或续约；`mode=resume` 人工放行**永不**拦截；`run_status`/`gate_get_task`/`events` 只读不拦 |
| D-T4 释放路径 | **完成** | MCP server / CLI 进程退出 `atexit` 释放本进程租约；强杀（无 atexit）由 **PID 存活检测**（同主机持有人进程已死 → 立即接管，ctypes/os.kill 实现，零新依赖）与 TTL（默认 900s，`AGENT_GATE_LEASE_TTL_S` 可调）兜底 |
| D-T5 三类消费者回归 | **完成（协议级）** | 见下 |

**关键设计决策**（相对 §6.4 原文的落地细化）：

1. **PID 存活快速接管**：§6.4 只说"进程崩溃后由 TTL 回收"。若只有 TTL，
   MCode 每轮 `exec`（新进程）都会撞上前一轮遗留租约、干等最长 15 分钟，
   直接破坏既有单消费者流程（与 D-T5 目标冲突）。同主机 PID 已死即接管
   解决此问题；TTL 保留为 PID 复用等边界兜底。**阶段 A/B 验证过的
   `continue_after_crash` 恢复路径因此不受影响**（同 owner 续约，或持有人
   已死立即接管）——有专门测试锁定该行为。
2. **run_start 只检查不取得**：drive 模式里拉起 worker 的父进程（drive）与
   真正消费的 agent 会话（各自 `.mcp.json` 拉起的 MCP server 子进程）不是
   同一进程；若 run_start 取得租约会把消费者挡在门外。取得交给第一次真正的
   消费动作。
3. **CLI 共享 `cli` 身份**：CLI 一次性命令（start/tasks/submit）若各自生成
   随机 owner 会把自己人挡在门外；缺省共享 `cli`。已记录限制：两个并行
   CLI 终端之间不互斥（人工双手，可设 `AGENT_GATE_OPERATOR_ID` 区分）。

**D-T5 验证口径**：MCode / Claude Code / ZCode 三类消费者的入口是**同一个
二进制**——各自操作员目录的 `.mcp.json` 都拉起 `python -m agent_gate.mcp_server`，
每会话一个进程、一个自动 owner。因此用「两个真实 MCP server 子进程」做
跨进程实测即覆盖三类消费者的协议层路径：消费者1 取得 → 消费者2 被拒
（`operator_already_claimed` + 暴露 owner）→ 消费者1 退出 → 消费者2 接管成功。
该实测已固化为 `test_operator_lease.py::test_mcp_cross_process_lease_takeover`。
**未做**：三类 CLI 的实机会话级联跑（需真实 mcode/claude 配额与操作员目录），
其协议路径与上述实测同构，风险评估为低。

**测试**：`test_operator_lease.py` 22 项全过（原子取得/续约/TTL 接管/PID
接活/并发单赢家/消费触点守卫/恢复路径不被自锁/人工放行穿透/跨进程实测）；
`conftest` 每用例清租约表（防止遗留行污染 MCP 子进程类用例）。
agent_gate 全量回归 exit 0。

## 4. 阶段 E：内嵌 ZCode Agent Runtime（仅 E-T0，E-T1～E-T7 冻结）

**提交**：`docs(修正): E-T0 固化官方外部 Agent 调用接口核查到设计文档附录`

**门禁核查新证据**（详情见设计文档附录 A）：

- **接口形态存在**：官方 npm 发行版 `zcode` CLI 0.17.1 实测内置
  `-p/--prompt`（无头单次执行）、`app-server`（ZCode Protocol stdio）、
  `--resume`；冒烟验证 `zcode -p "只回复两个字:正常" --mode plan` 返回
  `正常`、exit 0。
- **但门禁未满足**：官方文档（docs 首页全部章节 / Remote Control / FAQ /
  Changelog 至 v3.16.3 / 站内与全网检索）对该接口**零记载**，无报文规范、
  无版本承诺、无第三方集成授权声明。按设计 §4.2 既定标准（未文档化集成
  不属受支持方式），判定门禁不满足。
- **处置**（用户 2026-10-31 确认"维持门禁"）：E-T1～E-T7 维持冻结，
  本阶段仅交付 E-T0（核查结论已固化为设计文档附录 A，含解冻条件与届时
  入口）。不得提前实现任何私有协议调用（§10.6 门禁原文）。

## 5. 最终回归（改动后，§8.2 三套）

| 套件 | 结果 |
|---|---|
| 主仓 tests（`.venv`） | exit 0 |
| agent_gate（`.venv`，`-p no:cacheprovider`） | exit 0（含 22 项新租约测试） |
| openstoryline（`openstoryline\.venv`） | exit 0（含 14 项新 GLM 预设测试 + 4 项 live 跳过） |

## 6. 遗留与后续

| 项 | 状态 | 说明 |
|---|---|---|
| C-T1/C-T2 live 验证 | **阻塞** | 等用户提供 BigModel API Key 后跑 `test_glm_live.py`（§2 补验指引） |
| E-T1～E-T7 | **冻结** | 等官方文档化调用契约 + 第三方授权（附录 A.4）；届时阶段 D 租约已就绪 |
| MCode/Claude 实机会话级租约联跑 | 未做（可选） | 协议路径已被跨进程实测覆盖；可在下次实机验收时顺带执行 |
| 租约 TTL 默认 900s | 可调 | `AGENT_GATE_LEASE_TTL_S`；如需更激进的崩溃回收，PID 检测已即时接管 |
| `test_worker_with_mcode_cli_backend` 偶发超时 | 既有抖动 | 全量高负载下心跳窗口偶发超标，单独复跑必过；与本次改动无关 |

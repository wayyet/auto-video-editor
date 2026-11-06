> ⚠️ **历史文档(2026-11-04 单流水线架构收敛后失效)**:本文档描述的旧网页聊天端驱动 mcode/zcode 链路(①对话回合循环)已删除;现行架构与 mcode 无人值守用法(`agent_gate drive`)见 `README.md` 与 `docs/integration/architecture_decision_record.md`。内容保留仅作历史记录。

# ZCode Agentic 接入 阶段 A/B 执行记录

> **文档状态**：执行记录（阶段 A、B 已完成；阶段 C/D/E 未在本轮范围内）
> **执行日期**：2026-10-31
> **执行环境**：ZCode 桌面端（会话内 Agent 驱动），Windows，仓库根 `E:\Documents\kuaishou\auto-video-editor`
> **代码基线**：`e196100`（`docs(修正): 归档 Postgres worker 连接故障执行设计与 A/B/C3/D/E 验收记录`）
> **执行计划**：[auto-video-editor_ZCode_Agentic_接入执行设计.md](./auto-video-editor_ZCode_Agentic_接入执行设计.md) 第 10.2 节（阶段 A）、第 10.3 节（阶段 B）
> **范围声明**：仅执行阶段 A（MCP 接入配置）与阶段 B（全链路验收与回归）；阶段 C（GLM 预设）、D（操作员租约）、E（内嵌 Runtime）未执行。阶段 A/B 未修改任何仓库代码（`git diff` 为空），仅新增工作区 MCP 配置与本记录文档。

---

## 1. 前置条件核查（计划 5.1 节）

| 检查项 | 结果 |
|---|---|
| 主仓虚拟环境 | `E:\Documents\kuaishou\auto-video-editor\.venv\Scripts\python.exe` 存在，可导入 `agent_gate` / `mcp` / `langgraph` |
| PostgreSQL | 本机 5432 库 `video_workflow` 连通（PostgreSQL 16.11）；worker DSN 按 `AGENT_GATE_POSTGRES_URI` → `POSTGRES_URI` env → 仓库 `config.py` 默认值顺序解析，无需在 MCP 配置中写入凭据 |
| 测试视频 | `inputs/30s.mp4`（2046×1080 @60fps，ffprobe 实测），位于白名单目录 |
| ZCode 桌面端 | 已安装并登录（本会话即运行于该工作区） |
| 工作区既有 MCP 配置 | 无（`<repo>/.zcode/`、`<repo>/.agents/` 均不存在；用户级 `~/.zcode/cli/config.json` 仅含 plugins，无 `mcp.servers`） |

## 2. 阶段 A：MCP 接入配置（计划 10.2 节）

| 编号 | 任务 | 结果 |
|---|---|---|
| A-T1 | 核查既有配置并按 3.3 优先级定方案 | **采用新建 `<repo>/.agents/mcp.json`**：工作区作用域内不存在 `.zcode/config.json`（原生配置），按官方规则 `.agents/mcp.json` 兼容配置在该作用域生效；用户级配置无同名 server，无遮蔽风险 |
| A-T2 | 按 5.3/5.4 模板写入配置 | 已写入，全文见下 |
| A-T3 | 核对白名单目录 | `AGENT_GATE_MEDIA_ROOTS` 按分号分隔两项：`inputs`、`tests/fixtures`，两者均真实存在（实测 `exists=True, is_dir=True`） |
| A-T4 | 验证 server 启动无错误、工具可见 | stdio 握手成功：`video-agent` v1.26.0，MCP 协议 `2025-11-25`，工具列表完整（见 2.2），启动仅有一条无害的 pydantic `IncompleteFieldDefinitionWarning`，无错误 |

### 2.1 实际写入的配置（`.agents/mcp.json` 全文）

```json
{
  "mcpServers": {
    "video-agent": {
      "type": "stdio",
      "command": "E:/Documents/kuaishou/auto-video-editor/.venv/Scripts/python.exe",
      "args": [
        "-m", "agent_gate.mcp_server",
        "--outputs-root", "E:/Documents/kuaishou/auto-video-editor/outputs",
        "--graph-factory", "graph:build_graph"
      ],
      "env": {
        "PYTHONPATH": "E:/Documents/kuaishou/auto-video-editor",
        "AGENT_GATE_REPO_ROOT": "E:/Documents/kuaishou/auto-video-editor",
        "PYTHONUTF8": "1",
        "AGENT_GATE_MEDIA_ROOTS": "E:/Documents/kuaishou/auto-video-editor/inputs;E:/Documents/kuaishou/auto-video-editor/tests/fixtures",
        "STORYLINE_MODE": "auto"
      },
      "enabled": true
    }
  }
}
```

与设计文档 5.3 模板的差异及理由：

1. **新增 `STORYLINE_MODE=auto`**：`config.py` 默认 `STORYLINE_MODE=human`，human 模式依赖 OpenStoryline Web UI 探测（`_route_after_launch` 在 Web 未就绪时直接 END），裸 MCP 链路无法推进；`auto` 模式为 19 节点确定性图，是上一次 Postgres worker 连接故障验收（`e8bacbcee73e4de4a1988403e0dde3e8` 线程）已验证的可独立运行路径。
2. **显式 `type: "stdio"` 与 `enabled: true`**：ZCode 配置 schema 严格（未知键丢弃），使用规范字段名（`env` 而非 `environment`）。
3. **未写入任何 Postgres 凭据**：worker 侧 DSN 由仓库 `config.py` 默认值兜底（与生产启动脚本默认 DSN 一致），避免密钥进入配置文件。

> **该文件含本机绝对路径，属于机器特定配置，本次未提交 Git**（与仓库既有约定一致：`.gitignore` 明确排除 `.env`/`config.toml.local` 等本机配置；仓库也未跟踪任何操作员目录的 `.mcp.json`）。内容已完整存档于本节。

### 2.2 工具清单（A-T4 实测）

实际暴露 **6 个工具**（设计文档 5.5 节所列 5 个核心工具之外，还有 `gate_submit_batch`；`human_resume` 按安全设计默认未注册，仅 `--with-human-tool` 时才有）：

`run_start`、`run_status`、`gate_next_tasks`、`gate_get_task`、`gate_submit_result`、`gate_submit_batch`

### 2.3 ZC-P0-01 的 ZCode 端确认方式

ZCode 客户端在**会话启动时**加载工作区 MCP 配置（官方诊断文档：编辑配置后需重启会话）。本记录执行时配置在会话中途写入，因此"ZCode 设置 → MCP 中 `video-agent` 状态为已启用、5（实际 6）个工具在会话内可见"需在**下一次会话启动后**于 Settings → MCP 目视确认。服务端等价验证（同一份配置文件拉起 server、stdio 握手、tools/list 全量返回）已在 A-T4 完成——这是 ZC-P0-01 的服务端证据；客户端目视核对留作会话重启后的补充确认项。

## 3. 阶段 B：全链路验收与回归（计划 10.3 节）

### 3.1 验收方法

由于 ZCode 会话中途写入的 MCP 配置不能热加载，本轮验收用独立 MCP stdio 客户端脚本（`mcp_drive.py` 与 `mcp_serve.py`，存放于 `C:\Users\wayyet\AppData\Local\Temp\zcp0_acceptance_tools\`，**刻意放在仓库外**——见 3.5 节回归发现，且不属于仓库代码）直接读取 `.agents/mcp.json` 中的真实 `command/args/env` 拉起 `agent_gate.mcp_server`，以 MCP 协议调用工具执行验收——与服务端配置完全一致。

**关键运行约束（执行中发现）**：mcp Python SDK 在 Windows 上把 MCP server 进程放进 Job Object（`mcp/os/win32/utilities.py`、`mcp/client/stdio/__init__.py`："Creates process in a Job Object for reliable child termination"），会话关闭时 `terminate_windows_process_tree` 会**连坐终止整棵进程树**，包括 `spawn_worker` 以 `DETACHED_PROCESS` 拉起的 LangGraph worker。因此：

- 验收驱动器必须在整个 worker 生命周期内保持**同一个长驻 stdio 会话**（`mcp_serve.py`），一次性调用式驱动会在会话关闭时误杀 worker（执行初期 3 个废弃线程 `zcp0-b1-20261031a/b/c` 即因此报废，已清理；现象为 status 停在 `starting`、`worker.log` 空、3 秒内进程消失）；
- 对真实 ZCode 使用方式的推论：**ZCode 会话保持打开期间 worker 存活；会话关闭会终止在跑的 worker**，之后可用 `run_start(continue_after_crash=true)` 从 Postgres checkpoint 续跑。此为 SDK 行为而非仓库缺陷，建议在阶段 D/E 中评估是否给 worker 增加 breakaway 或由 `agent_gate` 提供守护。

### 3.2 B-T1 全链路（ZC-P0-02～05、ZC-P0-11）

测试线程：**`zcp0-b1-20261031d`**（全新 thread_id，符合 `safe_thread_id` 规则）；测试视频：`inputs/30s.mp4`（白名单内）。

执行链（时间为 2026-10-31 本地时间，完整日志：`outputs/_zcp0_acceptance_evidence/zcp0-b1-20261031d_status_log.jsonl`）：

| 步骤 | 证据 |
|---|---|
| `run_status`（新线程） | 返回结构化状态：`worker_state=not_started`、`next_action=start_run`，无连接异常 → **ZC-P0-02 ✓** |
| `run_start(thread_id, video_path)` | `ok=true, pid=17528`；长驻会话下 worker 稳定进入 `running`（对照：一次性会话中 worker 被连坐终止）→ **ZC-P0-03 ✓** |
| 11 个 gate 任务领取 | 每批经 `gate_next_tasks` 领取（含画面帧图片块），`gate_get_task` 抽查全文，共 11 个任务全部领取 → **ZC-P0-04 ✓** |
| 11 个任务提交 | `gate_submit_result` 全部一次通过校验（`ok=true`，无 retries），worker 持续推进 → **ZC-P0-05 ✓** |
| 终态 | `waiting_human`（关卡①），`next_action=ask_human`；见 3.3 与 ZC-P0-11 说明 |

任务清单（与上一轮真实运行 `e8bacbcee73e4de4a1988403e0dde3e8` 的任务集一致，任务 ID 亦相同——同视频同切分下确定性生成）：

| # | task_id | node | 格式 | 回答方式 |
|---|---|---|---|---|
| 1 | t_df18eac45697dec8 | storyline_understand_clips | text | 看 6 帧图写 caption + aes_score JSON |
| 2 | t_005df5bf1c6561bf | storyline_understand_clips | text | 同上 |
| 3 | t_ea23e11c496dc4c3 | storyline_understand_clips | text | 同上 |
| 4 | t_36b158b16644685a | storyline_understand_clips(汇总) | text | 基于前三条 clip 描述写整体概述 |
| 5 | t_c3185ca3fcad2bef | storyline_filter_clips | json | 素材总长 3.8s<预算下限且片段数<5 → 全保留 |
| 6 | t_f7b5eb348cd6897e | storyline_group_clips | json | themes + voiceover_hints（能力函数确定性分组） |
| 7 | t_7ecb5ccefde7b5df | storyline_generate_script | json | 每组旁白 + 标题 |
| 8 | t_3fe366741044b0f7 | storyline_recommend_transition | json | 首尾帧转场 prompt（英文，防翻车结构） |
| 9 | t_ee6fe05995904a3c | storyline_recommend_text | json | 字体推荐（中文思源黑体/英文 Roboto） |
| 10 | t_ce84e36afbf4bf46 | storyline_select_bgm | json | bgm_story_warm（温暖叙事向） |
| 11 | t_732d938b50448ca7 | assembly_build_timeline | json | 看 8 张帧图做 Hook/Core/Vibe 三段排序 + canvas(2046×1080@60) |

产物核查（`outputs/zcp0-b1-20261031d/`）：

- `assembly/`：`preview.mp4`、`timeline.json`、`preview_qc_report.json`、`report.md` 等 10 个文件（assembly QC 通道完整跑完）
- `drafts/`：`draft_content.json`、`draft_info.json`（剪映草稿，关卡①的 draft_path 即此处）
- `storyline/`：understanding/groups/script/voiceover/ai_transition/timeline_plan 等全套产物
- `normalized_sources/`：`30s_608x1080.mp4` 规范化素材

PostgreSQL checkpoint 核查：`select count(*) from checkpoints where thread_id like 'zcp0-b1-20261031d%'` → **40 行**，checkpoint 可核查。

**ZC-P0-11 结论：部分达成（停在设计内人工关卡）**。`done` 状态在本工作流上不可由 Agent 自主达成：关卡①（`node_06_human_reorder`）与关卡②（`node_12_human_add_bgm`）是无条件 `interrupt()`，`human_resume` 按安全设计未在本 server 注册，设计文档 7.4 明确"人工关卡不可绕过"。链路已推进到关卡①，状态 `waiting_human`、`error=null`，全部产物与 checkpoint 可核查；**经用户确认，本轮停在 `waiting_human` 归档**（先例：Postgres worker 连接故障验收同样以 `waiting_human` 为设计内终态，见其验收记录 T8）。若后续人工放行（给人用的命令：`python -m agent_gate resume --thread-id zcp0-b1-20261031d --checkpoint ① --value true`），worker 可继续走向关卡②与 `done`。

### 3.3 B-T2 安全用例（ZC-P0-06～09）

| ID | 场景 | 操作 | 实测结果 |
|---|---|---|---|
| ZC-P0-06 | 非白名单媒体路径 | `run_start(zcp0-sec-outside, C:/Users/wayyet/AppData/Local/Temp/zcp0_outside/outside.mp4)` | 拒绝，`code=media_path_not_allowed`；`outputs/zcp0-sec-outside/agent_gate/events.jsonl` 留有 `input_rejected` 事件，且返回 message 不含本机路径 ✓ |
| ZC-P0-07 | 未配置媒体白名单 | 以剥离 `AGENT_GATE_MEDIA_ROOTS` 的环境拉起第二个 server 实例并 `run_start` | 拒绝，`code=media_roots_unset`（fail closed）；events.jsonl 留有拒绝事件 ✓ |
| ZC-P0-08 | 人工关卡 | 主线程走到关卡① | `next_action=ask_human`，验收驱动器**立即停止自动推进**并上报用户，未尝试任何 resume（工具亦未注册）✓ |
| ZC-P0-09 | 非法 thread_id | `run_status("../evil")`、`run_status("sub/way")`、`run_start("../evil", …)` | 三次均拒绝，`code=bad_thread_id`，无目录穿越 ✓ |

### 3.4 B-T3 单消费者验证（ZC-P0-10）

- **运行纪律**：验收期间 `zcp0-b1-20261031d` 线程全程只有一个消费者（`mcp_serve.py` 长驻会话）；唯一一次第二个消费者探测（下条）由验收者主动发起并记录。
- **机制实测**：在 serve 驱动器已领取 `t_005df5bf1c6561bf` 后，第二个消费者调用 `gate_next_tasks` **再次拿到同一任务**。对照 `agent_gate/service.py`：`wait_for_work`/`pending` 直接读 `mb.pending_ids()`，**不存在认领/租约标记**——任务对所有消费者广播，首个合法 `gate_submit_result` 落盘即完成。
- **结论**：ZC-P0-10 以"严格单消费者运行纪律"通过；`operator_already_claimed` 稳定错误码属阶段 D（操作员租约）交付，本轮未实现、未验证，与计划 10.3 节 B-T3 的备注一致。本次实测直接证明了该约束的现实必要性：若无纪律约束，双消费者会重复消费同一任务并可能互相覆盖提交。

### 3.5 B-T4 回归命令（计划 8.2 节）

三条命令均按 8.2 节原文在仓库根执行（主仓 venv 与 openstoryline venv 各就各位）。执行过程如实记录：

1. **第一轮**：`agent_gate\tests` 与 `openstoryline\tests` 通过；`tests` 套 1 个失败——`test_phase5_e2e.py::test_phase5_decoupling_no_runtime_dependencies`，违规项为验收驱动器脚本 `outputs/_zcp0_acceptance_evidence/mcp_drive.py`、`mcp_serve.py` 中的 `from mcp import`（该用例全仓 `rglob("*.py")` 扫描 MCP 运行时依赖残留，`outputs/` 不在跳过名单）。**属验收工具自身污染仓库扫描范围，非既有失败、非环境回归**——本轮 `git diff` 为空，未改任何仓库代码。
2. **处置**：把两个驱动器脚本移出仓库（`C:\Users\wayyet\AppData\Local\Temp\zcp0_acceptance_tools\`，证据数据 JSON/JPG 留在 `outputs/_zcp0_acceptance_evidence/`）。
3. **第二轮（全量重跑三套，完整日志存证于 `outputs/_zcp0_acceptance_evidence/regression_{1_tests,2_agent_gate,3_openstoryline}.log`）**：

| 套 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `.venv\Scripts\python.exe -m pytest tests -q` | **0** | 全部通过，日志内 0 个 FAILED/ERROR 行 |
| 2 | `.venv\Scripts\python.exe -m pytest agent_gate\tests -q -p no:cacheprovider` | **0** | 233 passed + 17 skipped = 250，0 个 FAILED/ERROR 行（由进度点解析） |
| 3 | `openstoryline\.venv\Scripts\python.exe -m pytest openstoryline\tests -q` | **0** | 全部通过，日志内 0 个 FAILED/ERROR 行 |

> 计数行说明：仓库 `pyproject.toml` 固定 `addopts = "-q --strict-markers"`，在 pytest 9.1.1 下 `-q` 模式不打印末尾统计行（套 1/3 无计数、套 2 计数由进度点解析得出）；通过判据采用 pytest 退出码 0 + 日志零 FAILED/ERROR 行，两者均已存证。第一轮出现失败时 `FAILED` 短摘要行确实出现，说明该判据有效。

结论：`全部通过，无新增失败` 达成（第一轮唯一失败由验收工具 placement 引发，移出仓库后全量复跑通过；仓库代码零改动）。

**衍生发现（供阶段 D/E 参考）**：`phase5_e2e` 解耦扫描器 `rglob("*.py")` 不跳过 `outputs/`——任何放进 `outputs/` 的 `.py` 工具都会被判违规；验收/运维工具应放仓库外或推动扫描器增加 `outputs` 跳过目录（后者属代码改动，不在本轮范围）。

### 3.6 B-T5 归档

本文件即执行记录；原始证据目录：`outputs/_zcp0_acceptance_evidence/`（status_log、req/ans 文件、帧图、tools 握手输出、驱动器脚本）。

## 4. ZC-P0-01～11 验收对照总表

| ID | 场景 | 结论 | 证据位置 |
|---|---|---|---|
| ZC-P0-01 | ZCode 加载 video-agent MCP | 服务端验证 ✓（stdio 握手 + 6 工具全量）；ZCode 客户端口待下次会话启动目视确认 | §2.2/§2.3；evidence/tools |
| ZC-P0-02 | run_status 查询新线程 | ✓ | §3.2 |
| ZC-P0-03 | 白名单视频启动 worker | ✓ | §3.2 |
| ZC-P0-04 | worker 产生模型任务并可领取 | ✓（11 个任务） | §3.2 |
| ZC-P0-05 | 任务结果提交后 worker 继续 | ✓（全部一次通过） | §3.2 |
| ZC-P0-06 | 非白名单媒体路径拒绝 | ✓ | §3.3 |
| ZC-P0-07 | 未配置白名单拒绝 | ✓ | §3.3 |
| ZC-P0-08 | 人工关卡停止推进 | ✓ | §3.2/§3.3 |
| ZC-P0-09 | 非法 thread_id 拒绝 | ✓ | §3.3 |
| ZC-P0-10 | 单线程单消费者 | ✓（纪律通过；租约属阶段 D；已实测无认领机制） | §3.4 |
| ZC-P0-11 | 工作流完成 | 部分：停在设计内关卡① `waiting_human`（用户确认归档）；产物与 40 行 PG checkpoint 均可核查；done 需人工放行后达成 | §3.2 |

## 5. 关键发现汇总（供阶段 D/E 参考）

1. **MCP SDK Job Object 连坐终止**（§3.1）：ZCode 会话关闭会杀掉在跑 worker；建议阶段 D 评估 worker 脱离 Job（breakaway）或独立守护进程方案，并考虑把"会话关闭→worker 终止→continue_after_crash 续跑"写进操作手册。
2. **任务无认领机制**（§3.4）：`gate_next_tasks` 对多消费者广播同一任务；阶段 D 的线程级操作员租约（`operator_already_claimed`）是并发安全的前置改造。
3. **`done` 必经人工关卡**：①② 为无条件 interrupt，任何"全自动到 done"的预期都与工作流设计冲突；对 ZCode 的正确预期是"到人工关卡停下并报告"。
4. **配置要点**：工作区接入必须带 `STORYLINE_MODE=auto`（否则 human 模式在 Web 未就绪时静默早退 END）；`AGENT_GATE_MEDIA_ROOTS` 必须显式配置（fail closed）；DSN 无需写入 MCP 配置。
5. **工具数量**：实际 6 个工具（设计文档写 5 个核心工具 + 未提 `gate_submit_batch`），后续修订设计文档时可同步。

## 6. 交付物清单

- 工作区 MCP 配置：`E:\Documents\kuaishou\auto-video-editor\.agents\mcp.json`（未入 Git，内容存档于 §2.1）
- 执行记录：本文件
- 验收驱动器脚本：`C:\Users\wayyet\AppData\Local\Temp\zcp0_acceptance_tools\{mcp_drive.py, mcp_serve.py}`（仓库外，理由见 3.5 节）
- 证据目录：`outputs/_zcp0_acceptance_evidence/`（status_log、任务请求/答案 JSON、帧图、握手输出）
- 保留的运行线程：`outputs/zcp0-b1-20261031d/`（主链路，waiting_human）、`outputs/zcp0-sec-outside/`、`outputs/zcp0-sec-unset/`（安全拒绝证据）；调查期废弃线程 a/b/c、dbg1/dbg2 已清理

# auto-video-editor 工作流创建台：阶段 0 基线确认与接口契约冻结

> **文档类型**：阶段 0 交付物（基线记录 + 数据契约 + 节点消费映射）
> **执行依据**：`docs/第二期开发/auto-video-editor_工作流监控与任务创建界面_设计及分阶段执行计划.md`（无日期后缀版）§11 阶段 0
> **基线日期**：2026-11-05
> **执行范围**：仅阶段 0-2（契约冻结 / 上传 API + State 构建 / 子进程启动 + 真实 LLM 注入）；阶段 3（前端）及以后不在本次范围

---

## 1. 基线记录

### 1.1 代码基线

| 项 | 值 |
|---|---|
| 基线 HEAD | `a9a0fa7d711a8f9388aaa9761f1173d258ccf733`（docs(第二期开发) 提交，代码与核对基准 951259b 一致） |
| Python | 3.13.11（`.venv`） |
| 工作区既有改动 | `web/monitor.html` 存在**与本任务无关的浅色主题 CSS 改动**（并行会话产物，阶段 0-2 不触碰、不纳入提交） |

### 1.2 监控现状确认（只读边界）

- `monitor_fastapi.py`：模块级 `app`；路由 `GET /`、`GET /node-map`；挂载 `/node_map`、`/static`；无独立健康端点（健康 = `GET /` 200）。
- `web/api_workflow.py`：**仅 4 个 GET**（`/graph`、`/runs/{thread_id}`、`/runs/{thread_id}/events`(SSE)、`/runs/{thread_id}/resume-status`），无任何 POST；thread_id 白名单正则 `^[A-Za-z0-9_.:\-]{1,128}$` + 同源校验 `_verify_same_origin`。
- `scripts/run_workflow.py`：唯一直接执行入口；`--thread-id`（默认 video-001）、`--production`、`--skip-preflight`、`--initial-state-json`（JSON **整体透传进 state，无字段白名单**）、`--video-input-paths`；执行链 `apply_migrations_to_db()`(fail-closed) → `get_checkpointer()` → `build_graph(run_preflight=False)` → `monitoring.workflow_runner.run_with_events`。
- `agent_gate/cli.py`：另一类既有入口（start/drive），本次不复制其内部逻辑、不同时启动两个执行器。

### 1.3 基线测试结果

执行命令（两套测试分跑，符合计划文档 §12.3 纪律）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q -p no:cacheprovider
```

**`pytest tests`（2026-11-05/06 两轮，失败集合完全一致）**：

- FAILED `tests/integration/test_workflow_stream_events.py` 7 例 —— 全部为
  `conftest.py:144` 的 fail-closed 报错："Postgres 不可达:
  postgresql://…@localhost:5432/video_workflow —— PostgreSQL 集成测试不允许静默
  skip(方案 §7.2);请先启动 Postgres 容器。"（环境性失败，非代码缺陷）
- FAILED `tests/integration/test_reorder_topology.py::test_tail_chain_applies_to_both_drafts`
  —— 断言失败："英文草稿 transitions 为空:尾链未对英文草稿生效"。
  **既有失败**：该测试不 import 本次任何改动文件（本次改动 = 新增 web/console_* +
  config.py 末尾追加常量 + 依赖声明），与本次无关；已如实记录，供阶段验收"不新增失败"对比。
- 其余全部通过/跳过（pytest 末尾汇总行在本机终端序列下未落盘，最终精确计数以
  阶段验收的 junitxml 运行为准；回归判据为**失败集合一致**而非绝对数字）。

**`pytest agent_gate/tests`**：全部 SKIPPED，原因一致——"Postgres 不可达(测试库
video_workflow_agent_gate_*)—— agent_gate 的 checkpointer 已统一为 Postgres
(方案 §4.7)，没有本地文件兜底;请先启动 Postgres 容器。"（环境性跳过）

（详细输出见仓库根 `baseline_test_results.txt` / `baseline_full_tests.log` /
`baseline_agent_gate.log`；均为临时记录，不入库。）

### 1.4 PostgreSQL 探测（2026-11-05）

- `db.postgres_config.resolve_postgres_target()` → 来源 `config.POSTGRES_URI`（localhost:5432）。
- **探测结果：连接超时，PostgreSQL 不可达**（Docker Desktop / WSL HCS 故障延续，见 2026-11-04 记录）。
- 结论：生产 checkpoint 后端仍为 PostgreSQL（无 SQLite 回退）；阶段 2 真机冒烟**记为待办**，待 PG 恢复后按 §5.5 步骤补做。postgres 标记测试在 PG 不可达时自动跳过（现有 skipif 机制，非假绿：显式给 URI 连不上会 fail）。

### 1.5 真实 LLM 后端确认

`agent_gate.factory.build_client()` 构造性检查（不执行真实调用）：

| 后端 | 构造 | 可执行文件解析 |
|---|---|---|
| `stub` | OK | 内置（`StubLLMClient`） |
| `claude-cli` | OK | `claude`（PATH） |
| `mcode-cli` | OK | `C:\Users\wayyet\.minimax-code\mcode.CMD`（约定目录解析 `agent_gate.agent_bin`） |

注入路径已存在：`build_client(backend)` → `storyline_capabilities.vlm_client.set_default_client(client)`（`vlm_client.py:155`；默认懒加载 Stub）。`agent_gate/factory.py:41 register_from_env()` 即该模式的现成实现体。

---

## 2. 冻结：`POST /api/console/runs` 契约

- 路由：`web/api_console.py`，`APIRouter(prefix="/api/console")`，挂载于现有 `monitor_fastapi.py` app（**单服务 7860**，不建独立 7861 服务）。
- `/api/workflow/*` 保持 GET-only，不新增任何写接口。

### 2.1 请求（multipart/form-data）

| 字段 | 类型 | 必填 | 默认 | 校验 |
|---|---|---|---|---|
| `prompt` | string | 是 | 无 | strip 后非空；长度 ≤ `CONSOLE_PROMPT_MAX_CHARS`(4000) |
| `videos` | file[] | 是 | 无 | 数量 1..`CONSOLE_MAX_VIDEOS`(10)；扩展名 ∈ {.mp4,.mov}；单文件 ≤ `CONSOLE_MAX_VIDEO_BYTES`(2 GiB)；ffprobe 校验视频流 |
| `target_duration_ms` | integer | 否 | 35000 | > 0 且 ≤ 600000 |
| `ratio` | string | 否 | `9:16` | ∈ {9:16, 16:9, 4:3} |
| `lang` | string | 否 | `zh` | ∈ {zh, en} |

LLM 后端由服务端环境决定，**不接受浏览器指定**。

### 2.2 响应

成功 `202 Accepted`（`state` 取值随阶段推进：阶段 1 恒为 `prepared`，阶段 2 起 Popen 成功后为 `starting`）：

```json
{
  "ok": true,
  "thread_id": "console-7f1a2b3c4d5e",
  "state": "prepared",
  "monitor_url": "/?thread_id=console-7f1a2b3c4d5e",
  "snapshot_url": "/api/workflow/runs/console-7f1a2b3c4d5e",
  "events_url": "/api/workflow/runs/console-7f1a2b3c4d5e/events"
}
```

### 2.3 错误码

| HTTP | 场景 |
|---|---|
| 400 | 参数缺失/非法（prompt 空、ratio/lang 非法、target_duration_ms 越界、文件数 0） |
| 403 | Origin 校验失败（同源校验，仿 `web/api_workflow.py:_verify_same_origin`） |
| 409 | 活跃运行数超过 `CONSOLE_MAX_CONCURRENT_RUNS`（阶段 2 起） |
| 413 | 单文件或总量超限 |
| 415 | 扩展名不允许 / ffprobe 校验失败（无视频流、损坏、超时） |
| 503 | PostgreSQL 不可达，或真实 LLM 后端未配置/未放行 stub（阶段 2 起） |
| 500 | 服务端未预期错误（含 Popen 失败） |

### 2.4 幂等与 CSRF 边界（阶段 3+）

- `idempotency_key` 不在首期契约（计划文档 §4.3 字段表无此项）；防重复提交由阶段 3 前端负责。
- CSRF token 机制待阶段 3（有浏览器客户端时）随 `GET /api/console/csrf` 一并设计；首期仅做 Origin 同源校验。

---

## 3. 冻结：初始 State 契约

写入 `outputs/<thread_id>/initial_state.json`（UTF-8 无 BOM，`ensure_ascii=False`）：

```json
{
  "session_id": "console-7f1a2b3c4d5e",
  "video_input_path": "C:/…/outputs/console-7f1a2b3c4d5e/inputs/0001.mp4",
  "video_input_paths": [
    "C:/…/outputs/console-7f1a2b3c4d5e/inputs/0001.mp4",
    "C:/…/outputs/console-7f1a2b3c4d5e/inputs/0002.mov"
  ],
  "storyline_outputs_root": "C:/…/outputs/console-7f1a2b3c4d5e",
  "storyline_targets": {
    "user_request": "用户输入的剪辑提示词",
    "script_request": "用户输入的剪辑提示词",
    "target_duration_ms": 35000,
    "ratio": "9:16",
    "lang": "zh"
  },
  "error_log": []
}
```

硬性规则：

1. `video_input_path` = 第一个视频（兼容单视频消费者）；`video_input_paths` = 全部视频。
2. 提示词只写入 `storyline_targets.user_request` / `script_request`，**绝不写入 `query`**（`graph.py:205 _web_topic_branch_enabled` 读 `targets.query`，误写会触发网页话题搜索分支）。
3. 提示词不写入监控事件 payload（`monitoring/workflow_runner.py:97 STATE_SUMMARY_KEYS` 白名单本就不含它）。
4. `run_workflow.py --initial-state-json` 对 JSON 无字段白名单，因此**白名单责任在创建台**：本文件的键集合即全部，不透传任何其他键。

## 4. 冻结：目录结构与 thread_id 规则

```text
outputs/                      # 已在 .gitignore（.gitignore:38），不入库
└── console-<12hex>/
    ├── inputs/0001.mp4 …     # 服务端生成文件名，原文件名不落盘
    ├── initial_state.json
    ├── console_run.json      # 运行元数据：thread_id/created_at/files/status/pid/backend/log
    ├── console_run.log       # 子进程 stdout+stderr（追加）
    ├── drafts/               # AUTO_VIDEO_EDITOR_DRAFT_DIR 指向此处
    └── storyline/            # 工作流产物（storyline_outputs_root）
```

- `thread_id` = `console-` + `uuid4().hex[:12]`，服务端生成；同时通过三处既有校验（`agent_gate/safety.py:27` `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`、`web/api_workflow.py:84`、`db/migrations.py:136` CHECK）。
- 文件写入：临时文件 + `os.replace` 原子落盘；任务目录由服务端按 thread_id 创建，`Path.resolve()` 归属校验防穿越。

## 5. 冻结：限制与配置（`config.py` 新增，env 可覆盖）

| 常量 | env | 默认 | 说明 |
|---|---|---|---|
| `CONSOLE_OUTPUTS_ROOT` | 同名 | `<repo>/outputs` | 任务根目录（resolve 后做归属校验） |
| `CONSOLE_MAX_VIDEOS` | 同名 | 10 | 单次上传文件数上限 |
| `CONSOLE_MAX_VIDEO_BYTES` | 同名 | 2 GiB | 单文件上限 |
| `CONSOLE_PROMPT_MAX_CHARS` | 同名 | 4000 | 提示词长度上限 |
| `CONSOLE_FFPROBE_TIMEOUT_SEC` | 同名 | 30 | ffprobe 超时 |
| `CONSOLE_LLM_BACKEND` | 同名 | （空） | 阶段 2 起：空 → 503 拒绝启动（不静默 Stub） |
| `CONSOLE_ALLOW_STUB` | 同名 | false | true 时才允许 `stub` 后端启动 |
| `CONSOLE_MAX_CONCURRENT_RUNS` | 同名 | 2 | 阶段 2 起：活跃子进程上限 |

## 6. 冻结：执行架构（阶段 2）

```text
POST /api/console/runs
  → 校验 + 落盘 + initial_state.json（阶段 1，无副作用于图）
  → PG 预检(psycopg 短超时)          失败 → 503
  → LLM 后端门禁(CONSOLE_*)          未配置/未放行 stub → 503
  → subprocess.Popen(argv 数组, shell=False, cwd=仓库根,
      env += {AUTO_VIDEO_EDITOR_DRAFT_DIR: outputs/<tid>/drafts,
              WORKFLOW_ENV: production},
      stdout/stderr → console_run.log, CREATE_NEW_PROCESS_GROUP)
      argv = [主.venv python, scripts/run_workflow.py,
              --production, --skip-preflight, --thread-id <tid>,
              --initial-state-json <abs>, --llm-backend <backend>]
  → 202 + thread_id（不等待工作流结束）
  → run_workflow.py: apply_migrations(fail-closed) → build_graph
     → monitoring.workflow_runner.run_with_events → PG checkpoint + workflow_run_events
  → 既有 /api/workflow/* SSE 只读消费
```

- `--skip-preflight` 的使用理由：创建台自身已完成 PG 预检；schema 由 `run_workflow.py` 内 `apply_migrations_to_db()`（fail-closed）兜底；preflight 的 Docker 服务拉起属管理员操作，不属每次请求路径。
- 子进程独立进程组 + 独立草稿目录：浏览器断开、多任务并发互不影响（草稿不回落共享 `drafts/default`）。
- 提示词只进 JSON State，不进命令行参数。

## 7. 节点消费映射表（提示词真实生效路径）

| `storyline_targets` 键 | 消费方 | 证据 |
|---|---|---|
| `user_request` / `script_request` | `storyline_generate_script`（文案生成，主消费） | `nodes/storyline/node_generate_script.py:50` → `extract_user_request` → `prompts/tasks/generate_script/<lang>/user.md` 的 `{{user_request}}` |
| `user_request` | `storyline_recommend_text` / `storyline_recommend_transition` | `node_recommend_text.py:49`、`node_recommend_transition.py:44` |
| `user_request`/`group_request` | `storyline_group_clips`（选料分组） | `node_group_clips.py:48` |
| `user_request`/`bgm_request` | `storyline_select_bgm` | `node_select_bgm.py:53` |
| `user_request`/`voiceover_request` | `storyline_generate_voiceover` | `node_generate_voiceover.py:48` |
| `target_duration_ms` | filter/group 预算文本 + `plan_timeline_pro` + `qa_gate` 硬约束 | `nodes/storyline/_common.py:219`、`qa_gate.py:55`（>target*1.05 触发重试） |
| `lang` | 全部 prompt 节点（`resolve_lang`，默认 zh） | `nodes/storyline/_common.py:187` |
| `ratio` | **当前无消费者**（state.py:247 注释预告，代码未读取）——如实标记"暂不开放" | 全仓 grep 无命中 |
| `query` | 网页话题搜索分支——**禁止由提示词写入** | `graph.py:205` |
| （不接） | `plan_timeline_pro`（确定性 `TimeLine().distribute`，无 prompt）、`qa_gate`（只读 target_duration_ms） | `storyline_capabilities/plan_timeline_pro.py:104` |

汇聚函数：`nodes/storyline/_common.py:201 extract_user_request(state, *keys)`（strip、回退序、空→中性默认）——阶段 3 前端提示词的可见效果即经由上述既有通道，无需改图。

## 8. 验收自检（阶段 0）

- [x] Console（`/api/console/*`）与只读监控（`/api/workflow/*`）职责分离——后者零改动。
- [x] 不修改主图拓扑（`graph.py` 零改动）。
- [x] 不重新引入 SQLite（生产 checkpoint 仍 PostgreSQL；本机暂不可达已如实记录）。
- [x] 所有新增字段/配置均有明确消费者或标记"暂不开放"（`ratio` 如实标记无消费者）。
- [x] 生产执行入口仍只有既有 CLI / `agent_gate` 两类 + 本次新增的第三类 Web 入口（经同一 `run_workflow.py`，非新 runner）。

## 9. 遗留清单（PG 恢复后补做）

1. 阶段 2 真机冒烟：`CONSOLE_LLM_BACKEND=stub CONSOLE_ALLOW_STUB=true` 启动监控服务 → multipart POST 真实小视频（`tests/fixtures/30s.mp4`）→ 断言 202 + 子进程存活 + `console_run.json/log` 落盘 → 轮询 `workflow_run_events` 确认事件写入。
2. 真实模型（mcode-cli）端到端运行（计划文档属阶段 4 门槛）。
3. `start_production.ps1` 服务托管与"只启动服务"模式（阶段 5 范围）。

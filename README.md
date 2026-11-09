# AI 视频剪辑自动化工作流 — 单流水线架构(auto-only)

> **2026-11-04 架构收敛**:旧的 Web 聊天回合运行时(`agent_fastapi.py` +
> `src/open_storyline/` + 聊天前端 + `.venv-openstoryline`)已整体删除,项目收敛为
> **唯一的 LangGraph 流水线循环**。人工关卡 ①/②/③(`interrupt`/`resume`)保留;
> LLM 关卡答题走 `agent_gate drive` 无人值守监督;监控独立为只读服务
> `monitor_fastapi.py`。历史架构见 `README-OpenStoryline.md`(已标注失效)。

## 1. 目录结构

```
auto-video-editor/
├── README.md                           本文件
├── monitor_fastapi.py                  [阶段1] 只读监控服务(/ + /node-map + /api/workflow/*)
├── pyproject.toml                      项目元数据 + 依赖(含 fastapi/uvicorn)
├── requirements.txt                    pip 兜底依赖清单
├── config.py                           集中配置(STORYLINE_MODE 恒 auto,非 auto 值 import 即报错)
├── state.py                            WorkflowState TypedDict
├── graph.py                            StateGraph 装配 + 编译(START 直连 storyline_load_media;
│                                       节点/边统计以编译图为准,见 docs/remove-chat-loop-20261104/schema-snapshots)
├── nodes/
│   ├── node_05_generate_draft.py       草稿生成(storyline_plan 优先,shot_plan 兜底)
│   ├── node_06_human_reorder.py        关卡① interrupt
│   ├── node_07_speed_fit.py            护栏节点 + 帧对齐分配公式
│   ├── node_08_add_subtitles.py        字幕主干兼容节点(样式注入;B1 ASR 移除后不再转写语音)
│   ├── node_08b_add_subtitles_en.py    英文字幕
│   ├── node_09_inject_fx.py            转场 + 视频特效
│   ├── node_10_inject_text_fx.py       花字(中/英)
│   ├── node_11_inject_sticker.py       贴纸
│   ├── node_12_human_add_bgm.py        关卡② interrupt
│   ├── node_13_adjust_volume.py        音量
│   ├── node_14..17 / node_16a / checkpoint3 / fork / join   英文线 + 尾链 + 交付 QA
│   ├── assembly/                       assembly QC 六节点(QC 通道,可开关)
│   └── storyline/                      auto 链 19 节点(load_media → … → join)
├── storyline_capabilities/             auto 链本地能力层(BGM/配音/转场/提示词)
├── assembly_capabilities/              assembly QC 能力层
├── agent_gate/                         Agent 调度:MCP server / worker / 信箱 / drive / resume
├── monitoring/                         事件存储 / 统一 runner / 图 schema / 心跳
├── db/                                 应用层迁移(migrations;chat_sessions/workflow_chat_links
│                                       为 legacy:定义保留、应用层不读写、存量不 DROP)
├── web/
│   ├── api_workflow.py                 只读监控 API(无 POST /runs、无 chat-session 查询)
│   ├── monitor.html                    [阶段1] 监控首页
│   ├── static/workflow_monitor.mjs     SSE 事件流 + 节点状态
│   └── node_map/                       节点图页
├── scripts/
│   ├── run_workflow.py                 入口 A:直接 CLI(--production)
│   ├── resume_workflow.py              [阶段3前置] 入口 A 的 checkpoint 恢复(--checkpoint ①/②/③)
│   ├── preflight.ps1                   6 步预检(Postgres 拉起 + schema)
│   └── migrate_db.py                   应用层迁移(幂等)
├── start_production.ps1                一键启动(工作流 + 监控服务;auto-only)
├── stop_production.ps1                 一键停止(只停本项目 PID 记录的进程)
├── draft_ops/ / jy_common/ / templates/ / mcp_clients/   剪映草稿底层库 + 共享模块
├── docs/
│   ├── remove-chat-loop-20261104/      本次收敛的基线/回归/快照证据
│   └── integration/architecture_decision_record.md        ADR
└── tests/
    ├── unit/                           单测(阶段4 后全绿)
    └── integration/                    集成测试(PG 可达环境)
```

## 2. 环境与安装

### 2.1 系统要求

- Python 3.10+(本项目在 Python 3.13.11 实测通过)
- Windows / macOS / Linux
- 依赖以 `requirements.txt`(公共 PyPI 钉版,含 `langgraph==1.2.9`)为准;无需本机
  langgraph 源仓库。如需开发 langgraph 本体,可另用本机源仓库可编辑安装替代 ——
  与钉版二选一,不要混装。

### 2.2 一键安装

```powershell
cd C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\auto-video-editor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2.3 验证安装

```powershell
python -c "from graph import build_graph; from config import make_checkpointer; g = build_graph(checkpointer=make_checkpointer('memory')); print(type(g).__name__)"
# 应输出:CompiledStateGraph
# 注意:build_graph() 必须显式注入 checkpointer,不再自动兜底(方案 §4.2)。
```

### 2.4 本地启动 EDB Postgres 16.11（Docker 版，推荐）

Week 5 切到 `AsyncPostgresSaver` 时，使用 Docker Desktop 启动本地 EDB Postgres。默认配置为：

- 镜像：`quay.io/enterprisedb/postgresql:16.11-3.5-postgis-multilang`
- 容器：`edb-postgres16`
- 数据库：`video_workflow`（运行时唯一库；容器默认维护库为 `langgraph`，由 `align_postgres.py` 增量建出 `video_workflow`）
- 端口：`localhost:5432`
- 命名卷：`edb_pgdata`

在项目根目录执行：

```powershell
# 启动容器并等待 healthcheck
.\scripts\start_postgres.ps1

# 停止容器；命名卷与数据保留
.\scripts\stop_postgres.ps1

# 停止并删除容器；命名卷与数据仍保留
.\scripts\stop_postgres.ps1 -Remove

# 首次或需要重置表时执行
$env:POSTGRES_URI = "postgresql://postgres:devpass@localhost:5432/video_workflow"
.\.venv\Scripts\python.exe scripts\setup_postgres_schema.py

# 验证版本、数据库和 checkpoint 表
.\.venv\Scripts\python.exe scripts\verify_postgres.py

# 跑 Postgres 路径集成测试
$env:WORKFLOW_ENV = "production"
.\.venv\Scripts\python.exe -m pytest tests\integration\test_postgres_checkpointer.py -v
```

如需永久设置当前 Windows 用户的 `POSTGRES_URI`，可执行：

```powershell
[Environment]::SetEnvironmentVariable(
  "POSTGRES_URI",
  "postgresql://postgres:devpass@localhost:5432/video_workflow",
  "User"
)
```

运行时**所有**持久化(checkpoint + 聊天会话)都在这个容器的 `video_workflow` 库里,没有第二个数据库、没有本地 `.sqlite` 落盘(方案 §4.1)。完全删除数据前需确认影响：checkpoint 与会话状态会随 `edb_pgdata` 卷一起删除。


拉镜像前若之前遇过 `127.0.0.1:3067 connect refused` 之类的代理报错，可先跑代理自检脚本（containerd 的代理层偶尔会被指向失效端口）：

```powershell
.\scripts\check_docker_proxy.ps1                 # 只检查
.\scripts\check_docker_proxy.ps1 -RestartDocker  # 检查失败自动 docker desktop restart
```

### 2.5 e2e 测试的额外环境依赖(⚠️ 单元测试全绿 ≠ 全部通过)

`tests/unit` **不需要**下面任何东西就能全绿。但 `tests/integration/test_phase5_e2e.py`
的 happy / qc_fail 两个慢用例会**真实抽帧 + 真实渲染**,缺依赖时会失败——而症状具有
迷惑性:`tests/unit` 依然全绿,只有这两个 e2e 用例挂。

| 依赖 | 缺了会怎样 | 怎么确认 |
|---|---|---|
| `numpy`(已在 `requirements.txt` 钉 `2.5.3`) | `video_ingest` 抽帧抛 `ModuleNotFoundError` → `assembly_visual_observe` 节点早退 → **`video_ingest.json` 永不落盘** | `python -c "import numpy; print(numpy.__version__)"` |
| `opencv-python-headless`(钉 `4.10.0.84`) | 抽帧 / 编码静默失败 | `python -c "import cv2; print(cv2.__version__)"` |
| `ffmpeg` + `ffprobe` 在 `PATH` | 慢用例直接 skip | `ffmpeg -version` |
| `inputs/30s.mp4` | 慢用例直接 skip | `Test-Path inputs\30s.mp4` |

一次性补齐:

```powershell
python -m pip install -r requirements.txt
# 若 ffmpeg 缺失(本机装在 C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin)
$env:PATH += ";C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin"
# 素材不入库,需自行准备(任意 >=10s 的测试视频即可)
New-Item -ItemType Directory -Force -Path inputs | Out-Null
```

**两个静默陷阱**:

1. `inputs/` 被 `.gitignore:38` 忽略,新 clone 不会带上素材。此时 happy / qc_fail 是
   **skip 而不是 fail**,`pytest` 退出码仍是 0 —— 很容易误判成"测试通过"。
   依赖齐全时基线是 `877 passed / 11 skipped`;素材或 ffmpeg 缺失会变成
   `875 passed / 13 skipped`(多的 2 个就是被跳过的 e2e 慢用例)。
2. `video_ingest.json` 缺失时,失败信息是
   `missing_artifacts=['video_ingest.json']`,但 `qc_status` 仍显示
   `pass_with_warnings` —— 容易误判成 QC 逻辑问题,实际是抽帧根本没跑起来。

**当前基线**(Python 3.13.11 + pytest 9.1.1 + numpy 2.5.3 + opencv 4.10.0 + ffmpeg 9.0.2):

```
877 通过 / 0 失败 / 0 错误 / 11 跳过   (183s)
```

### 2.6 一键启动(唯一入口)

> 2026-10-21 起,`scripts\start_local.ps1` **已删除**。根目录的
> `.\start_production.ps1` 是**唯一**完整工作流启动入口。
> 2026-11-04 阶段5 起:7860 端口承载**只读监控服务** `monitor_fastapi:app`(主 `.venv`);
> OpenStoryline Web 生命周期已随旧聊天运行时退役;`-StorylineMode` 参数删除
> (auto-only,`STORYLINE_MODE` 恒 auto)。

**启动**(需要「以管理员身份运行」的 Windows PowerShell):

```powershell
# 默认:thread-id=video-001,本地 Postgres 容器,顺带起只读监控(:7860)
.\start_production.ps1

# 换 thread-id
.\start_production.ps1 -ThreadId video-002

# 指定 Postgres(支持任意 URI,见下)
.\start_production.ps1 -PostgresUri postgresql://app:secret@localhost:5432/appdb

# 只打印计划,不产生任何副作用(不建目录、不建日志、不起进程)
.\start_production.ps1 -WhatIf
```

**参数表**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-ThreadId` | `video-001` | LangGraph `thread_id`,同时写入 `runtime\initial_state_<id>.json` 与 checkpoint 查询 |
| `-PostgresUri` | `postgresql://postgres:devpass@localhost:5432/video_workflow` | 连接的唯一真相。`localhost:5432` → **本地托管**模式(起 `edb-postgres16` 容器 + 自动对齐角色/库);其它 host/端口 → **外部**模式(完全不碰本地容器) |
| `-SourceVideo` | `<仓库根>\inputs\30s.mp4`(`$PSScriptRoot` 锚定) | 复制到 `-InputsDir` 的源视频 |
| `-InputsDir` | `<仓库根>\inputs`(`$PSScriptRoot` 锚定) | 素材目录 |
| `-InputsFileName` | `30s.mp4` | 素材文件名 |
| `-SkipPhase3` | 关 | 跳过验收(checkpoints / heartbeat / outputs) |
| `-WhatIf` | 关 | 只打印计划,**零副作用** |

**`-PostgresUri` 的两种模式**

| URI 形态 | 模式 | 行为 |
|---|---|---|
| `postgresql://u:p@localhost:5432/db` | 本地托管 | 起 `edb-postgres16` 容器 → 用管理员凭据**增量**对齐目标角色/库(`scripts\align_postgres.py`,只 CREATE/GRANT,不删)→ 用目标 URI 建表 |
| `postgresql://u:p@<其它 host 或端口>/db` | 外部 | **不碰任何本地容器**,直接用目标 URI 验连 + 建表 |

> ⚠️ 对齐时若目标角色 == 管理员角色(`postgres`)且密码不同,脚本会 `ALTER ROLE` 改密码
> 并打印醒目告警 —— 此时默认 URI `postgresql://postgres:devpass@…` 将**不再可用**。
> 改密码后请改用新的 URI。

**停止**:

```powershell
.\stop_production.ps1                        # 停工作流进程 + 监控服务 + Postgres 容器(卷永远保留)
.\stop_production.ps1 -RemoveContainers      # 顺带删 Postgres 容器(卷仍保留)
.\stop_production.ps1 -CleanState -CleanHeartbeat
.\stop_production.ps1 -WhatIf                # 只打印计划,零副作用
```

> **所有权口径**:启动时若 `:7860` 已被健康的监控服务占用,脚本会**复用**它
> (不启第二个进程),PID 文件(`logs\monitor_pids_*.json`)标记 `reused=true`。
> `stop_production.ps1` 遇到 `reused=true` 时**既不按 PID kill、也不走命令行兜底** ——
> 那个进程不是本项目拥有的,保留不动,需要停止请手动处理。命令行兜底只匹配
> `monitor_fastapi:app`,**绝不**按端口盲杀未知进程。

**退出码表**

| 码 | 含义 |
|---|---|
| 0 | 成功(工作流跑完,或停在人工关卡 ①/②/③ 等待放行 —— 均为正常终态) |
| 10-16 | preflight Step 1-6 失败(脚本透传) |
| 19 | `-PostgresUri` 非法(格式 / 缺 host / 缺库名) |
| 20 | Postgres 角色/库对齐失败 |
| 21 | 监控服务进程启动后提前退出 |
| 22 | 监控服务健康检查超时(60s) |
| 23 | `:7860` 端口冲突(被非本项目进程占用) |
| 25 | Phase 3 checkpoint 查询失败(验收未完成) |
| 27 | `run_workflow.py` 非预期失败 |
| 99 | preflight 未捕获异常 |

> 旧码 3/24(OpenStoryline 未就绪提前终止)与 26(resource/models 缺失)已随
> human 模式链与 Phase 0.55 删除(2026-11-04 阶段3/5)。

**失败与恢复**

| 症状 | 退出码 | 处理 |
|---|---|---|
| `当前 PowerShell 不是管理员` | 11 | 用「以管理员身份运行」重开 PowerShell,或按 README §2.4 手动起 `com.docker.service` |
| `Docker Desktop 启动后 90s 内 daemon 未就绪` | 17 | 手动打开 Docker Desktop GUI,等「Docker Desktop is running」后重跑 |
| `端口 7860 已被占用,但 GET 不返回 200` | 23 | 手动查出占用进程并处理;脚本**不会**杀未确认归属的进程 |
| `60s 内监控服务未就绪` | 22 | 看报错里的 `Postgres :` 行——容器未运行时先 `docker start edb-postgres16`;再看 `logs\monitor_*.log` 尾部。脚本**不会**带未就绪的服务跑工作流 |
| `preflight.ps1 退出码 15(Postgres 不可达)` | 15 | 确认 `-PostgresUri` 可连;外部库需先自行建库建角色 |
| `角色/库对齐失败` | 20 | 看 `logs\start_*.log` 里 `align_postgres.py` 的报错(密码已脱敏) |
| `验收未完成:checkpoint 查询失败` | 25 | 确认 Postgres 可连 + `checkpoints` 表已建(Phase 0.58 preflight 的 Step 6 会建) |

**日志**

每次运行写一份 `logs\start_<时间戳>.log`;监控服务另写
`logs\monitor_<时间戳>.log`,PID 信息写 `logs\monitor_pids_<时间戳>.json`。
日志里的密码一律脱敏成 `***`。

### 2.7 运行入口、恢复与监控(单流水线两路触发)

**入口 A —— 直接 CLI**(进程内,`VIDEO_LLM_BACKEND`/信箱选答题后端):

```powershell
# 启动(通常经 start_production.ps1;亦可直接跑)
.\.venv\Scripts\python.exe scripts\run_workflow.py --production --thread-id <THREAD_ID> `
    --initial-state-json runtime\initial_state_<THREAD_ID>.json

# 查看挂起的人工关卡(不执行恢复)
.\.venv\Scripts\python.exe scripts\resume_workflow.py --thread-id <THREAD_ID> --dry-run

# 放行指定关卡(操作员交互终端执行;--checkpoint 支持 ①/②/③ 或 legacy_id)
.\.venv\Scripts\python.exe scripts\resume_workflow.py --thread-id <THREAD_ID> --checkpoint ① --value true
```

**入口 B —— Agent / MCP / drive**(无人值守):

```powershell
# 前置:素材必须在白名单内($env:AGENT_GATE_MEDIA_ROOTS),否则 safe_media_path 拒绝
$env:AGENT_GATE_MEDIA_ROOTS = "E:\Documents\kuaishou\video-ops\inputs"

# 无人值守:自动拉起 worker + mcode 会话答题 + 崩溃自动重启;
# 到达人工关卡返回退出码 10,等待真人放行
python -m agent_gate drive `
  --thread-id drive-20261105-001 `
  --outputs-root outputs `
  --agent mcode `
  --project-dir "E:\Documents\kuaishou\video-ops" `
  --video "E:\Documents\kuaishou\video-ops\inputs\30s.mp4"

# 查看挂起关卡(先读 interrupts 的实际 checkpoint 标识,不要假定固定停在同一关)
python -m agent_gate status --thread-id drive-20261105-001 --outputs-root outputs

# 真人在交互终端放行(不要用 --force-no-tty 绕过真人关卡;该参数只用于受控 CI)
python -m agent_gate resume --thread-id drive-20261105-001 --outputs-root outputs `
  --checkpoint "<status 返回的 checkpoint>" --value true
```

> ⚠️ **恢复语义不混用**(ADR-2026-11-04):入口 A 启动的运行只能由
> `scripts\resume_workflow.py` 恢复;入口 B(agent_gate worker)启动的运行只能由
> `agent_gate resume` 恢复。两者共享 PostgreSQL checkpoint 与事件表,但不共享控制状态。

**监控(只读)**:

```powershell
# 独立启动(一键启动已包含,绑 0.0.0.0;手动启动:)
.\.venv\Scripts\python.exe -m uvicorn monitor_fastapi:app --host 0.0.0.0 --port 7860
```

- 首页 <http://127.0.0.1:7860/>(SSE 事件流 + 节点状态);节点图 </node-map>。
- **局域网访问**:一键启动默认绑 `0.0.0.0`,同一局域网内的设备(手机/其它电脑)
  直接访问 `http://<本机IP>:7860/`、`http://<本机IP>:7860/console` 即可,前端全部
  相对路径、无需任何配置。启动脚本会幂等创建防火墙入站放行规则
  `auto-video-editor monitor 7860`(TCP 7860);手工等价命令:
  `New-NetFirewallRule -DisplayName "auto-video-editor monitor 7860" -Direction Inbound -Protocol TCP -LocalPort 7860 -Action Allow -Profile Any`。
  注意:创建台无鉴权,局域网内任何设备可创建运行(消耗 LLM tokens)。
- 监控**只读**:查询运行快照、SSE 补发、图拓扑;刷新/重连绝不触发工作流。
  工作流触发只有入口 A/B 两条路径。

### 2.8 创建台 /console(第二期,浏览器创建运行)

浏览器里的第三类入口(仍经同一 `scripts/run_workflow.py`,非新 runner):

1. 打开 <http://127.0.0.1:7860/console>(局域网设备用 `http://<本机IP>:7860/console`);
2. 输入剪辑提示词,拖入/选择 1-10 个 MP4/MOV(单文件 ≤2 GiB,可配);
3. 设置目标时长(默认 35 秒)、画幅比例、输出语言;
4. 点「创建并运行工作流」—— 服务端生成 `console-<12hex>` thread_id,
   视频落盘 `outputs/<thread_id>/inputs/`,写 `initial_state.json`
   (提示词只进 `storyline_targets.user_request/script_request`,绝不写 `query`),
   以 `--llm-backend`(来自服务端 `CONSOLE_LLM_BACKEND`,不接受浏览器指定)启动子进程;
5. 页面自动订阅既有 SSE 显示节点事件;「查看完整监控」跳转 `/?thread_id=`(自动订阅)。

约束与安全:

- 写 API 仅 `POST /api/console/runs`(202 即返回,不等待执行);带 Origin 的浏览器
  请求必须先 `GET /api/console/csrf` 取 token 并随 `X-CSRF-Token` 提交(无 Origin
  的本机脚本豁免);`/api/workflow/*` 仍 GET-only。
- LLM 后端门禁:`CONSOLE_LLM_BACKEND` 未配置 → 503 拒绝启动(不静默 Stub);
  `stub` 需 `CONSOLE_ALLOW_STUB=true`。
- 每任务独立草稿目录(`AUTO_VIDEO_EDITOR_DRAFT_DIR=outputs/<thread_id>/drafts`),
  并发上限 `CONSOLE_MAX_CONCURRENT_RUNS`(默认 2)。
- 恢复语义:console 创建的运行(入口 A 同款)由 `scripts/resume_workflow.py` 恢复;
  监控页/console 的恢复按钮只提供指引,不由监控 API 执行。
- 清除缓存:「历史任务」区「清除缓存」按钮 → `POST /api/console/cache/clear`,
  清理可再生缓存(系统临时目录 / 源码 `__pycache__` / `logs/*.log` /
  `runtime/initial_state_*.json` / 剪映草稿回收站与备份;清单见 `config.py`
  缓存段,路径按仓库位置动态解析)。三道保护:CSRF 门禁、冷却限流
  (`CONSOLE_CACHE_CLEAR_COOLDOWN_SEC`,默认 30s,超频 429)、本实例有活跃
  运行任务时 409 拒绝。禁删:剪映 `User Data\Cache`、`User Data\Log`、
  `outputs/` 成片、`.storyline/models`(2026-11-04 R-10 删除旧 clean-cache
  端点后,清理能力以此手动入口形式回归)。

## 3. 运行测试

```powershell
cd C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\auto-video-editor
.\.venv\Scripts\Activate.ps1

# 跑全部测试
python -m pytest -v

# 只跑单元测试
python -m pytest tests/unit/ -v

# 只跑集成测试
python -m pytest tests/integration/ -v

# 只跑某一条测试
python -m pytest tests/integration/test_interrupt_resume.py::test_checkpoint1_interrupt_and_resume_basic -v
```

**当前测试结果**(Week 3 末):
- 单元测试 60 条 ✅
- 集成测试 24 条 ✅(TC-01..TC-06 + Week 3 sequential + interrupt_resume + graph_compiles)
- 总计 **84 条全部通过**

## 4. Week 3 新增功能

### 4.0 Week 3 末补全记录(2026-09-16)

按验证报告 `auto-video-editor_第三周实施计划_代码验证报告.md` §11 落地 6 项缺口:

| 优先级 | 项 | 实现位置 | 状态 |
|---|---|---|---|
| P0-1 | 节点 13 真实实现(volume/fade/audio_fades) | `nodes/node_13_adjust_volume.py` | ✅ 逻辑完整,字段名待剪映客户端逆向 |
| P0-2 | 占位 resource_id 标注升级 | `templates/*.json` + `jy_common/asset_resource_map.json` + `scripts/build_resource_library.py` | ✅ 已迁移自 pyJianYingDraft metadata(转场 303 / 视频特效 462 / 文字入场 78 / 循环 52 / 出场 46,均为 VIP);贴纸仍是占位,见 §4.3 |
| P1-1 | 节点 11 按 timerange 绑 segment | `nodes/node_11_inject_sticker.py` | ✅ |
| P1-2 | ~~FireRedASR2S client~~ | `jy_common/asr_client.py` | ⚠️ 已随 2026-11-08 B1 ASR 移除整体删除,本行作废 |
| P1-3 | 两级超时接入(默认关闭) | `monitoring/timeout_watchdog.py` + `config.ENABLE_TWO_LEVEL_TIMEOUT` | ✅ 接入完整,默认 false 避免破坏既有 24 条集成测试 |
| P2 | 原子写入入口统一 | `jy_common/draft_writer.py` | ✅ 薄包装 re-export |

**仍需用户行动项**:

1. **剪映 v5.9.0 客户端字段逆向(P0-1/P0-2)**:在剪映客户端打开 `drafts/default/draft_content.json`,
   手动加淡入/淡出/音量 + 替换云端资源,保存后 `diff` 出真实字段名与真实 `resource_id`,
   回填到:
   - `nodes/node_13_adjust_volume.py` 的 `_PLACEHOLDER_*_KEY` 常量
   - `templates/*.json` 的 `_reverse_engineering_pending` 改为 `false` 并填入真实 ID

2. **~~真实 FireRedASR2S 服务启端(P1-2)~~**:已作废 —— 2026-11-08 B1 ASR 移除后,
   工作流不再有任何 ASR 调用,无需配置 `ASR_BACKEND` / `FIRERED_ASR_ENDPOINT`。

### 4.1 持久化 checkpointer(PostgreSQL 唯一后端)

PostgreSQL 统一改造后,运行时**只有一个**持久化后端:PostgreSQL。
SQLite 后端与 `checkpoints/` 落盘目录已删除(方案 §4.1 / D2)。

```python
from config import make_checkpointer

# 内存(单测专用,进程内无持久化)
saver = make_checkpointer("memory")
```

生产路径**不要**用 `make_checkpointer`,走 `graph.get_checkpointer()`(见 4.4)。
`build_graph()` 现在要求**显式注入** checkpointer,未注入直接抛 `RuntimeError` ——
不再自动兜底,避免"悄悄退化成无持久化"导致挂起任务查不到。

### 4.2 interrupt / resume 模式

```python
from graph import build_graph, get_checkpointer
from langgraph.types import Command

async def main():
    async with get_checkpointer() as saver:
        g = build_graph(checkpointer=saver, thread_id="video-001")

        # 第一次 invoke:跑到关卡① 挂起
        try:
            await g.ainvoke(initial_state, config={"configurable": {"thread_id": "video-001"}})
        except GraphInterrupt:
            pass

        # 用户在剪映手动调整后,用 Command(resume=True) 续跑
        await g.ainvoke(Command(resume=True), config={"configurable": {"thread_id": "video-001"}})
```

> ⚠️ `thread_id` **必须是 `str`**。psycopg3 把 `bytes` 写进 `TEXT` 列会变成
> `\x<hex>` 字面量,导致同一逻辑线程分裂成两套 thread_id(方案 §3.1 M2)。
> 用 `graph.normalize_thread_id(thread_id)` 归一后再用。

### 4.3 心跳监控

编排进程启动时 `build_graph()` 自动调用 `start_heartbeat()`(后台 daemon 线程,默认 10s 写一次时间戳到 `C:\ProgramData\VideoWorkflow\heartbeat.txt`)。

注册外部监控:

```powershell
# 管理员 PowerShell
.\monitoring\register_heartbeat_task.ps1
# 默认每 2 分钟检查一次,心跳超时 120s 即写本地日志告警
```

### 4.4 Postgres checkpointer — 生产路径

`graph.get_checkpointer()` asynccontextmanager 包装管理 `setup()` 与连接池:

```python
from graph import build_graph, get_checkpointer

async def main():
    async with get_checkpointer() as saver:
        g = build_graph(checkpointer=saver, thread_id="video-001")
        await g.ainvoke(initial_state, config={"configurable": {"thread_id": "video-001"}})
        # ... Command(resume=...) / 挂起 / 跨进程恢复
```

环境变量:

| 变量 | 说明 | 默认 |
|---|---|---|
| `POSTGRES_URI` | `postgresql://user:pass@host:port/dbname` | `postgresql://postgres:devpass@localhost:5432/video_workflow` |
| `WORKFLOW_ENV` | 只用于选择同步/异步执行路径,**不**参与存储后端选择 | `development` |
| `TEST_POSTGRES_URI` | 集成测试用的库;显式指定后连不上直接 fail(不静默 skip) | 同 `POSTGRES_URI` |

依赖(`pyproject.toml` / `requirements.txt`):`langgraph-checkpoint-postgres>=3.1` + `psycopg[binary,pool]>=3.2`。

> 本机 Postgres 容器 `edb-postgres16` 的库编码是 **`SQL_ASCII`**:
> 查询脚本要自己解码 `TEXT` 列(`bytes` → `str`),
> 写 JSONB 必须 `ensure_ascii=False`。
> 仓库已封装为 `db/connection.py`(`client_encoding=utf-8`)与 `db/session_repository.py::jsonb()`。

### 4.5 Week 5 `resume_all_pending` — 统一 resume 入口

支持单 / 多 interrupt 并发 resume,按 ``checkpoint`` 标识(`"①"` / `"②"` / `"③"`)自动匹配:

```python
from resume_utils import resume_all_pending

# 单 interrupt 时按 id 模式传 dict;≥2 个挂起时 LangGraph 要求 dict 模式
result = await resume_all_pending(
    g, config,
    {"①": "ok_a", "②": "ok_b"},
)
```

- 0 个挂起 → `ValueError("无挂起可恢复")`
- 单 interrupt + payload 缺 `checkpoint` 字段 → 走 `fallback_key="default"` 单值兼容
- ≥2 个挂起 → 必须按 id 传 dict(否则 RuntimeError)

### 4.6 双分支拓扑 — 节点 16 拆分 + 2026-10 调序

Week 5 把 `node_16_translate_subtitles`(翻译 + interrupt + 写 SRT 三合一)拆成两个独立节点;
2026-10 调序后英文线又补了 `node_08b` / `node_10b`,关卡③ 的挂载点后移到 `node_10b` 之后:

```
bridge_snapshot2
  ├─→ [中文线] node_08_add_subtitles → node_10_inject_text_fx → node_14_make_covers
  │                                      → node_15_localize_covers_en ───────────┐
  └─→ [英文线] fork_draft_for_english_branch                                    │
               → node_16a_translate_and_check                                     │
               → node_08b_add_subtitles_en    (英文字幕写入英文分支草稿)          │
               → node_10b_inject_text_fx_en   (英文花字,先于人工校对)             │
               → 🔁 条件边(route_after_translate)                                 │
                    ├─→ node_checkpoint3_layout_review ⏸ interrupt("③")(条件触发)│
                    │      └─→ node_17_inject_english_tts ──────────────────────┤
                    └─→ node_17_inject_english_tts ─────────────────────────────┤
                                                                                ▼
     ⇒ node_09_inject_fx(汇合屏障:15 与 17 都完成才触发,只触发一次)
     → node_11_inject_sticker → node_12_human_add_bgm ⏸ interrupt("②")
     → node_13_adjust_volume → join_before_delivery(交付前 QA 闸门)→ END
```

- **`node_16a_translate_and_check`**:翻译 + 写 marker + 写 `state["subtitle_segments_en"]` + 写 `layout_issues` / `layout_issues_detected` + **不调 interrupt**(Week 5 计划 §2.1 "关键发现①")。完全幂等,resume 重放由 `_marker_exists` 守住。
- **`node_08b_add_subtitles_en`**:把英文字幕写进英文分支草稿。此前英文草稿里没有任何文本素材,关卡③ 无物可校对、resume 后 SRT 会被重写成空文件。整表替换 `materials.texts`,天然幂等。
- **`node_10b_inject_text_fx_en`**:英文花字。**排在关卡③ 之前**,人工校对时在剪映里看到的就是带花字的最终效果。
- **`node_checkpoint3_layout_review`**:只读 `layout_issues`,做 `interrupt({"checkpoint": "③", ...})`,resume 后重写 SRT(Week 5 修订:不写 `subtitle_segments_en`,避免与 16a 在同一 superstep 触发 LastValue 并发写)。
- **`route_after_translate`**:挂载点是 **`node_10b_inject_text_fx_en` 之后**(2026-10 从 08b 后移过来),根据 `state["layout_issues_detected"]` 二选一走向 ③ 或 17。
- **`node_17_inject_english_tts_stub`**:Week 5 升级为在 `draft_dir_en_branch` 下写 `en_dub.wav` 空 wav 占位(>5KB),产出 `en_audio_path` 字段,让阶段五 `acceptance_check.py` "英文配音音轨非空" 断言通过。真实 FireRedTTS2 留 Week 6+。

**尾链 09 / 11 / 12 / 13 同时作用于中英两份草稿**:这四个节点被推迟到 `node_17` 之后,此时两份草稿
都已定稿,因此每个节点内部各跑一遍中文主草稿(`state["draft_path"]`)与英文分支草稿
(`state["draft_dir_en_branch"]/draft_content.json`),公共定位逻辑收敛在 `nodes/_dual_draft.py`。

- 不新增 09b/11b/12b/13b 四个英文实例节点:同一超步内并排写两份整份 state 会撞 LangGraph 的 LastValue 通道(`InvalidUpdateError`),而且关卡② 会被迫挂起两次(人工选两次曲)。改成**同一节点内部顺序处理**后,既不新增并发写冲突,关卡② 也只需人工做一次。
- 关卡② 仍只 `interrupt()` 一次;`post_resume` 之后把中文草稿里的 BGM 轨(含被引用的 `materials.audios`)**整轨复制**进英文草稿,segment id 不 remap(两份草稿视频时间轴相同)。英文侧音量随后由 13 统一处理。
- **英文草稿缺失 / 目录不存在 / 文件不存在时一律降级只处理中文草稿**并写 `error_log`,不抛异常 —— 与尾链既有的"模板库缺失降级""无音轨降级"同一档。
- ⚠️ 已知边界(R2):`node_17` 目前只合成音频文件、不写草稿,所以英文草稿没有主音轨 `audio_main`。13 对英文草稿只能调到关卡② 镜像过去的 BGM 轨;英文主音轨音量要等 node_17 落地写草稿后才生效。

### 4.7 关卡 payload 字段统一 — "①" / "②" / "③"

Week 5 把三个关卡的 interrupt payload `checkpoint` 字段统一为 Unicode 圆圈数字:

| 关卡 | 节点 | `checkpoint` 字段值 | `legacy_id`(兼容旧调用) |
|---|---|---|---|
| ① | `node_06_human_reorder` | `"①"` | `"checkpoint1_reorder"` |
| ② | `node_12_human_add_bgm` | `"②"` | `"checkpoint2_add_bgm"` |
| ③ | `node_checkpoint3_layout_review` | `"③"` | `"checkpoint3_layout_review"` |

`resume_all_pending` 据此自动匹配多个挂起。Week 3 单测 `test_interrupt_resume.py` 已更新以匹配新字段。

### 4.8 Week 5 新 State 字段(全部 NotRequired)

```python
# state.py Week 5 新增 — 所有读它们的节点用 .get(key, default) 兜底
en_audio_path:        NotRequired[Optional[str]]  # 节点 17 写,验收脚本读
final_video_path:     NotRequired[Optional[str]]  # 占位,Week 5 暂不实写
heartbeat_id:         NotRequired[Optional[str]]  # build_graph() 启动时生成
layout_issues:        NotRequired[list[dict]]     # node_16a 写
layout_issues_detected: NotRequired[bool]         # node_16a 写
```

**团队约定**(Week 5 计划 §5.4):任何给 State 加字段的 PR,必须新增一条"旧 checkpoint 能否恢复"测试(`tests/integration/test_state_field_compatability.py` 模板)。任何 `state["key"]` 直接索引(无 `.get` 兜底)在节点函数中:code review 直接打回(由 `test_nodes_use_state_get_not_subscript` 静态扫描守护)。

### 4.9 `monitoring/runs_table.py` — workflow_runs 台账

Week 5 新增轻量 SQL 工具,在 Postgres 同一库里建独立表跟踪挂起线程(对齐"破坏性变更应急"流程):

```python
from monitoring import runs_table

runs_table.init_schema()  # 幂等:走 db.migrations 的迁移器
runs_table.upsert_run(thread_id, video_name, "①", "suspended")  # 关卡前
runs_table.mark_completed(thread_id)                              # 流程结束后
runs_table.list_suspended()  # 运维查询入口
```

建表 DDL 已收敛到 `db/migrations.py`(唯一事实来源)。Postgres 不可达时所有调用**降级**
为 no-op、不阻塞主流程,但会打 **ERROR** 级日志并累加 `runs_table.degraded_count()`
——静默降级会让人误以为"已保存"。

### 4.9b `db/` — 应用层迁移(`chat_sessions` 已转 legacy)

> ⚠️ **2026-11-04 阶段4**:`db/session_repository.py` 已删除,应用层停止读写
> `chat_sessions` / `workflow_chat_links`;两者的**已发布迁移定义(1/5)原样保留**
> (迁移台账不可改写),存量表与数据**不 DROP**。本节以下为历史记录。

PostgreSQL 统一改造方案(阶段 A / B)新增的应用层数据访问层。**运行时唯一库 = `video_workflow`**。

| 模块 | 职责 |
|---|---|
| `db/migrations.py` | 版本化 SQL 迁移器。管 `app_schema_migrations` / `workflow_runs` / `workflow_run_events`(1/5 为 legacy 定义) |
| `db/connection.py` | 连接辅助:把 `client_encoding` 设成 UTF-8(本机库是 `SQL_ASCII`) |

**分工边界**:LangGraph 自己的 4 张 checkpoint 表由 `AsyncPostgresSaver.setup()` 建
(`scripts/setup_postgres_schema.py`),应用迁移**不碰**它们。

```powershell
# 执行应用层迁移(幂等,可重复跑)
.\.venv\Scripts\python.exe scripts\migrate_db.py

# 只看已应用的迁移版本
.\.venv\Scripts\python.exe scripts\migrate_db.py --status
```

⚠️ **本机库是 `SQL_ASCII`**:写 JSONB 必须用 `ensure_ascii=False`
(见 `db.connection.configure_json_dumps`),否则 PG 报 `UntranslatableCharacter`。

### 4.9c 会话状态入 PostgreSQL(阶段 D)与会话 REST API —— **已随旧聊天运行时删除**

> ⚠️ **2026-11-04 阶段2**:聊天 Web(`agent_fastapi.py`)与会话 REST API 整体删除,
> 本节仅作历史记录。存量 `chat_sessions` 数据保留在库中(不清理)。

| 方法 | 路由 | 说明 |
|---|---|---|
| `POST` | `/api/sessions` | 新建会话(旧接口,不变) |
| `GET` | `/api/sessions` | **列表**:游标分页(`limit` 1..200 默认 50、`cursor`、`title_prefix`、`include_deleted`);按 `updated_at DESC, session_id DESC`;响应**不含 `state_json`**;`next_cursor` 为 `null` 表示到底 |
| `GET` | `/api/sessions/{id}` | 取单个会话(旧接口,不变) |
| `PATCH` | `/api/sessions/{id}` | 改标题:`{"title": "...", "title_source": "auto"\|"manual"}` |
| `DELETE` | `/api/sessions/{id}` | **软删除**(打 `deleted_at`);可用 `repo.restore()` 找回 |
| `POST` | `/api/sessions/{id}/clear` | 清空对话(旧接口,不变) |

- **fail-closed**:数据库不可用 ⇒ 会话接口返回 **503**,**不**静默降级、**不**回退写 JSON 文件。
- **乐观锁**:`state_json` 更新走 `version` 乐观锁;冲突不覆盖 ⇒ 抛 `SessionVersionConflict`。
- **手动标题优先**:`title_source='manual'` 的会话不会被自动标题覆盖。
- 限流:`GET /api/sessions` 走独立桶 `list_sessions`(burst 10 / rpm 30),不挤占通用 `api_general`。

```powershell
# (以下两个一次性工具已随旧聊天运行时删除 —— 2026-11-04 阶段2:
#  scripts/import_session_states.py、scripts/drill_disconnect.py)
```

### 4.10 2026-10 接入原"不可达节点"(三个开关)

原先注册了、但从 START 永远走不到的 6 个节点,其中 **5 个已接入主图**;`clean_cache` 当初**不接边**、只由 Web「清理缓存」按钮手动触发(现已整体删除,见下)。**每个节点各自挂在与它最相关的现有节点旁边,不集中放在同一处。**
每条旁支由一个环境开关控制,**默认全开**。三个开关都设为 `0` 时,图的执行轨迹与接入前完全一致
(`tests/integration/test_unreachable_nodes_wiring.py` 里有黄金轨迹锁定)。

| 新节点 | 挂在哪(相关的现有节点) | 触发条件 | 开关(默认 `1`) |
|---|---|---|---|
| ~~`clean_cache`~~ | **已删除**(2026-11-04 阶段3,R-10:唯一触发入口 clean-cache 端点随旧聊天运行时删除) | — | — |
| ~~`storyline_search_media`~~ | **已删除**(2026-11-10 检索素材移除:`storyline_load_media` 主干恒直连 `storyline_split_shots`,Pexels 旁路与 `STORYLINE_PEXELS_API_KEY` 开关整体删除) | — | — |
| `storyline_search_web_topic` | `storyline_load_media` 之后,与 `storyline_split_shots` 并行的旁支(走到 END) | `STORYLINE_MODE=auto` + `storyline_targets.query` 非空 | `STORYLINE_ENABLE_WEB_TOPIC` |
| ~~`storyline_local_asr`~~ / ~~`storyline_speech_rough_cut`~~ | **已删除**(2026-11-08 B1 ASR 移除:切镜后静态直连 `storyline_understand_clips`,开关删除) | — | — |
| `storyline_generate_ai_transition` | 与 `storyline_plan_timeline_pro` 并排(同一道 4 源屏障) | `STORYLINE_MODE=auto` | `STORYLINE_ENABLE_AI_TRANSITION` |
| `storyline_plan_timeline_ai_transition` | 在 `plan_timeline_pro` + `generate_ai_transition` 之后、`qa_gate` 之前(`qa_gate` 的屏障里用它替换 `plan_timeline_pro`) | 同上 | 同上 |

备注:

- `clean_cache` **已删除**(2026-11-04 阶段3):清理缓存节点与旧 clean-cache 端点不复存在;
  清理能力已于 2026-11-06 以创建台手动入口形式回归(「清除缓存」按钮 →
  `POST /api/console/cache/clear` → `web/cache_cleaner.py`,仍不接图)。
- 网页话题:标题作为"外部不可信参考"软注入 `storyline_generate_script` 的 `overall`;失败 / 无结果不影响主线。
- ~~ASR~~:`storyline_local_asr` / `storyline_speech_rough_cut` 与 `STORYLINE_ENABLE_ASR_BRANCH`
  开关已随 **2026-11-08 B1 ASR 移除**整体删除(切镜后静态直连 `storyline_understand_clips`);
  旧文档中"只回填 `Clip.transcript` / `STORYLINE_ASR_MODE=stub` / Pexels 覆盖 `has_audio` 导致 ASR 不触发"等描述均已作废。
- AI 转场:当前只有 stub;只挂 `ai_transitions` 清单,不改 clips。两个节点在 qa 重试回路**里面**,每轮各跑 1 次。
- **读取时机不同**:`STORYLINE_ENABLE_WEB_TOPIC` 在**运行时**读取;`STORYLINE_ENABLE_AI_TRANSITION` 在**建图时**读取(决定图里接不接这两个节点),修改后须重启进程。
- 开发约定:条件触发的节点(`web_topic`)**不能**放进 `add_edge([...], X)` 屏障,否则 qa 重试时会静默 END;
  qa 重试回路里的节点,状态标签必须是固定值(不能带条数),否则 qa 路由会悄悄跳过重试。

PowerShell 临时关闭(须在启动 Python 进程之前设置):

```powershell
$env:STORYLINE_ENABLE_WEB_TOPIC = "0"
$env:STORYLINE_ENABLE_AI_TRANSITION = "0"
```

## 5. Week 1 交付物前置依赖(⚠️ 重要)

- [ ] **剪映 v5.9.0 已安装**
- [ ] **hosts 文件已屏蔽剪映升级域**(Week 1 交付物)
- [ ] **OpenStoryline 实际启动命令、端口、缓存路径**已核实替换 `config.py` 占位值

## 6. Week 3 已识别约束

| 约束 | 当前处理 |
|---|---|
| VIP 资源模板库(transition / effect / sticker)为占位 | **Week 4 已迁移**:转场 + 视频特效 + 文字动画的真实 VIP resource_id 已从 `pyJianYingDraft` metadata 导入 `templates/fx_resource_library.json` / `templates/text_resource_library.json`,由 `scripts/build_resource_library.py` 一键生成。**贴纸仍是占位**(§4.3 — 技能库无公开 ID)。 |
| ~~ASR 真实接入未完成~~ | 已作废 —— 2026-11-08 B1 ASR 移除:`jy_common/asr_client.py` 与节点 8 内部 ASR 整体删除,字幕主干保留 |
| 步骤 13 真实音量/淡入淡出未实现 | 本周 `node_13_adjust_volume` 仅占位(写 status_log);Week 4 先做字段逆向工程再实现 |
| `capcut decrypt` 返回码语义未实测 | Week 2 PoC 报告 §2.5 占位约定;`detect_draft_encryption` 接受 `decrypt_runner` 注入 |
| 心跳告警渠道(企业微信/邮件)未对接 | 本周仅本地日志 `heartbeat_monitor.log`;Week 4 对接 |

## 7. 手动验收(Day5 PM 必要步骤)

完整跑通后(`pytest tests/integration/test_sequential_integration.py` 通过)的产物 `draft_content.json` 需在剪映 v5.9.0 客户端手动打开验证:
- 不弹"草稿已损坏"错误
- 时间轴显示视频轨
- 字幕、转场、花字、贴纸可见
- 总时长 ≤ 35s

## 8. 文档索引

- `docs/poc_report.md` — Week 2 阶段 E 第三方组件 PoC 评估报告
- 实施计划:`《AI视频剪辑自动化工作流第3周详细实施计划.md》(同级目录)`
- LangGraph 源码(可选,仅开发 langgraph 本体时使用):本机 `langgraph-main` 仓库;运行依赖以 `requirements.txt` 的 PyPI 钉版为准

## 9. 进一步阅读

- [LangGraph 官方文档](https://langchain-ai.github.io/langgraph/)
- [LangGraph 源码仓库](https://github.com/langchain-ai/langgraph)
- `E:\Documents\kuaishou\.claude\skills\jianying-editor` — Week 3 借鉴字段结构(字幕样式 / AVAILABLE_ASSETS 枚举)

## 10. 工作流流程图与实时节点追踪(2026-11)

主工作流拓扑与逐节点执行状态现在可以在浏览器里实时观察。设计与分阶段执行记录见
`docs/修改文档/auto-video-editor-工作流流程图与实时节点追踪.md` 与
`docs/修正/auto-video-editor_工作流流程图_阶段*.md`。

### 10.1 页面入口与能力

- 入口:<http://127.0.0.1:7860/>(监控首页,`monitor_fastapi.py` 托管,uvicorn 绑定
  127.0.0.1;阶段5 起由 `start_production.ps1` 管理生命周期);节点图
  </node-map>。
- **拓扑视图**:「完整 LangGraph 工作流」由编译图程序化提取(含 START/END、
  条件边、并行 fan-in、QC 开关状态;阶段3 起 auto-only,无 fusion)。
- **实时状态**:输入 `thread_id` 点「订阅」即通过 SSE 重放该线程全部事件并持续跟进。
  节点七态:执行中(蓝脉冲)/已完成(绿)/等待人工(琥珀)/重试中(橙)/
  失败(红)/未参与(虚化)/未连通(虚线);已走路径绿色高亮,未命中分支不预点亮。
- 布局缓存 key 纳入 `workflow_id + mode + schema_version`。

### 10.2 事件从哪来(双入口统一)

- CLI(`scripts/run_workflow.py`,经 `scripts/resume_workflow.py` 恢复)与
  `agent_gate` worker 都产出 §7.1 标准事件:CLI 经统一 runner
  (`monitoring/workflow_runner.py`,astream `tasks/updates/custom` + `version="v2"`)
  事件先落 PostgreSQL `workflow_run_events` 表再推送;断线重连按全局 `id` 游标补发;
  worker 补发运行级事件(`run.started/resumed/interrupted/completed/failed`)。
- LangGraph `interrupt` 人工关卡(①②③)以 `node.waiting_input(waiting_kind=interrupt)`
  + `run.interrupted` 呈现(2026-11-04 阶段3 起 human 模式 5 节点与网页文件握手已删除)。

### 10.3 监控 API 端点(宿主 `monitor_fastapi.py` → `web/api_workflow.py`,只读)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workflow/graph` | 主图拓扑 JSON(节点/边/元数据/flags;`?refresh=1` 重建缓存) |
| GET | `/api/workflow/runs/{thread_id}` | 运行快照(事件计数/游标/派生状态/attempt 汇总) |
| GET | `/api/workflow/runs/{thread_id}/events` | SSE 事件流;`Last-Event-ID` 补发 |
| GET | `/api/workflow/runs/{thread_id}/resume-status` | 挂起探测 + 外部恢复指引(只读,不执行 resume) |

访问控制:有 `Origin` 头时必须是本机来源(跨源 403);curl/同源导航不受影响。
所有端点只读,绝不触发图执行;刷新页面只重放事件。旧 `POST /api/workflow/runs`
(监控 API 启动工作流)已于阶段1 删除 —— 触发只走入口 A/B(§2.7)。

### 10.4 排错

- **页面打开但订阅无事件**:先 `curl http://127.0.0.1:7860/api/workflow/runs/<thread_id>`
  看 `available`。`false` = 事件存储不可用(Postgres 未就绪或 `workflow_run_events`
  表缺失 —— 跑一次 `python -c "from db.migrations import apply_migrations_to_db; apply_migrations_to_db()"`),
  `/events` 会显式报错而非静默。
- **CLI 跑完但页面没动**:CLI 的事件行打在 **stderr**(`[wf] #12 node.started ...`);
  确认走的是统一 runner(老版本调用 `ainvoke` 不产事件)。
- **`workflow_run_events` 查询**:`SELECT id, event_type, node_id, attempt, occurred_at,
  payload FROM workflow_run_events WHERE thread_id = '...' ORDER BY id;`(`id` 即 SSE 游标)。
- **事件表在库里但页面 503**:Web 进程连的库由 `POSTGRES_URI` 决定,确认与 CLI 一致。
- 已知限制:事件丢失(存储降级)时该段历史不可回放;Web 进程不执行
  `Command(resume=...)`(langgraph 版本与生产不同,恢复走 agent_gate/人工脚本)。
- `E:\Documents\kuaishou\.claude\skills\jianying-speed-fit-35s` — Week 3 节点 7 帧对齐公式金标准
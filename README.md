# AI 视频剪辑自动化工作流 — Week 2 + Week 3 交付物

本目录实现附件《第 2 周分阶段实施计划》与《第 3 周详细实施计划》的全部交付物:

- **Week 2**:LangGraph 5 节点骨架(节点 1-5)+ draft_content.json 加密检测/版本策略/原子写入底层库 + 端到端集成测试
- **Week 3**:扩展为 13+1 节点完整图(节点 1-13 + 1 升级节点)+ 关卡①/② interrupt/resume 联调 + 心跳监控 + 持久化 SqliteSaver
- **PostgreSQL 统一改造**:运行时唯一数据库 = PostgreSQL(`video_workflow`);SQLite 检查点后端与本地 `.sqlite` 落盘已删除,聊天会话从 JSON 文件迁入 `chat_sessions` 表,浏览器侧栏历史以服务端为唯一事实来源

## 1. 目录结构

```
auto-video-editor/
├── README.md                           本文件
├── pyproject.toml                      项目元数据 + 依赖
├── requirements.txt                    pip 兜底依赖清单
├── .gitignore
├── config.py                           集中配置(Week1/3 待核实项占位)
├── state.py                            WorkflowState TypedDict(Week 3 +5 字段)
├── graph.py                            StateGraph 装配 + 编译(Week 3 13+1 节点)
├── nodes/
│   ├── node_01_clean_cache.py          ─┐
│   ├── node_02_launch_openstoryline.py │
│   ├── node_03_open_preview.py         │ Week 2 已有
│   ├── node_04a_import_video.py        │ 手动导入关卡(等网页【📥 导入视频】按钮)
│   ├── node_04b_get_storyboard_plan.py │ 读 openstoryline 产物 → storyline_plan
│   ├── node_05_generate_draft.py       ─┘
│   ├── node_06_human_reorder.py        [新] 关卡① interrupt + 副作用挪后
│   ├── node_07_speed_fit.py            [新] 护栏节点 + 帧对齐分配公式
│   ├── node_08_add_subtitles.py        [新] ASR Mock + 字幕样式注入
│   ├── node_09_inject_fx.py            [新] 转场 + 视频特效注入
│   ├── node_10_inject_text_fx.py       [新] 花字样式追加
│   ├── node_11_inject_sticker.py       [新] 贴纸 resource_id 关联
│   ├── node_12_human_add_bgm.py        [新] 关卡② interrupt(同节点6结构)
│   └── node_13_adjust_volume.py        [新] 占位节点(Week 4 实现)
├── draft_ops/                          Week 2 底层库(无 LangGraph 依赖,Week 3 复用)
│   ├── atomic_writer.py
│   ├── encryption_detector.py
│   └── version_strategy.py
├── jy_common/                          [新] Week 3 共享模块
│   ├── template_library.py             模板加载 + 资源库 by_name 查询(Week 4 已迁移自 pyJianYingDraft)
│   ├── asr_client.py                   ASR Mock 接口(Week 4 接 FireRedASR2S)
│   └── sticker_resolver.py             关键词→resource_id 解析
├── templates/                          Week 4 已迁移自 pyJianYingDraft metadata
│   ├── fx_template.json                风格索引(name + style_tag),真实 ID 在 fx_resource_library.json
│   ├── fx_resource_library.json        转场 + 视频特效 VIP 资源库(由 scripts/build_resource_library.py 生成)
│   ├── text_style_template.json        文字样式模板,默认 entrance_animation_name 在 text_resource_library.json 解析
│   ├── text_resource_library.json      文字入场/循环/出场 VIP 资源库(由 scripts/build_resource_library.py 生成)
│   └── sticker_template.json
├── scripts/
│   └── build_resource_library.py       一次性生成器 — 从 pyJianYingDraft metadata 导出 VIP 资源库
├── monitoring/                         [新] Week 3 心跳监控
│   ├── heartbeat_writer.py             编排进程内 daemon 线程
│   ├── heartbeat_monitor.ps1           外部监控脚本(任务计划程序触发)
│   └── register_heartbeat_task.ps1     任务计划程序注册脚本
├── db/                                 [新] PostgreSQL 统一改造:迁移器 + 会话仓储
│   ├── migrations.py                   版本化应用层迁移(app_schema_migrations)
│   ├── connection.py                   连接辅助(client_encoding=utf-8)
│   └── session_repository.py           chat_sessions 仓储(乐观锁 + 游标分页)
├── mcp_clients/
│   └── openstoryline_client.py         Week 2 已交付
├── docs/
│   └── poc_report.md                   Week 2 阶段 E PoC 报告
└── tests/
    ├── conftest.py                     Week 2 共享 fixtures
    ├── unit/                           Week 2: 29 + Week 3: 31 = 60 单测
    └── integration/                    Week 2: 8 + Week 3: 16 = 24 集成测试
```

## 2. 环境与安装

### 2.1 系统要求

- Python 3.10+(本项目在 Python 3.13.11 实测通过)
- Windows / macOS / Linux
- 需可访问 `E:\Documents\kuaishou\langgraph-main` 源仓库(本机)

### 2.2 一键安装

```powershell
cd E:\Documents\kuaishou\auto-video-editor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e E:\Documents\kuaishou\langgraph-main\libs\langgraph `
                      -e E:\Documents\kuaishou\langgraph-main\libs\checkpoint `
                      -e E:\Documents\kuaishou\langgraph-main\libs\prebuilt `
                      httpx pytest pytest-mock pytest-asyncio langgraph-checkpoint-postgres
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
| `numpy`(已在 `requirements.txt` 钉 `2.5.3`) | `video_ingest` 抽帧抛 `ModuleNotFoundError` → `assembly_asr_and_visual_observe` 节点早退 → **`video_ingest.json` 永不落盘** | `python -c "import numpy; print(numpy.__version__)"` |
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

**启动**(需要「以管理员身份运行」的 Windows PowerShell):

```powershell
# 默认:thread-id=video-001,STORYLINE_MODE=human,本地 Postgres 容器
.\start_production.ps1

# 换 thread-id
.\start_production.ps1 -ThreadId video-002

# 明确走 auto 模式(19 节点确定性图,不启动 OpenStoryline Web)
.\start_production.ps1 -StorylineMode auto

# 指定 Postgres(支持任意 URI,见下)
.\start_production.ps1 -PostgresUri postgresql://app:secret@localhost:5432/appdb

# 只打印计划,不产生任何副作用(不建目录、不建日志、不起进程)
.\start_production.ps1 -WhatIf
```

**参数表**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-ThreadId` | `video-001` | LangGraph `thread_id`,同时写入 `runtime\initial_state_<id>.json` 与 checkpoint 查询 |
| `-StorylineMode` | `human` | `human` = 走人工关卡(启动 OpenStoryline Web);`auto` = 19 节点确定性图(**不**启动 Web)。非法值在参数绑定阶段被拒(`ValidateSet`) |
| `-PostgresUri` | `postgresql://postgres:devpass@localhost:5432/video_workflow` | 连接的唯一真相。`localhost:5432` → **本地托管**模式(起 `edb-postgres16` 容器 + 自动对齐角色/库);其它 host/端口 → **外部**模式(完全不碰本地容器) |
| `-SourceVideo` | jianying-editor 演示素材路径 | 复制到 `-InputsDir` 的源视频 |
| `-InputsDir` | `FireRed-OpenStoryline\inputs` | 素材目录 |
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
.\stop_production.ps1                        # 停工作流进程 + Web + Postgres 容器(卷永远保留)
.\stop_production.ps1 -RemoveContainers      # 顺带删容器(卷仍保留)
.\stop_production.ps1 -CleanState -CleanHeartbeat
.\stop_production.ps1 -WhatIf                # 只打印计划,零副作用
```

> **所有权口径**:启动时若 `:7860` 已被健康的 OpenStoryline Web 占用,脚本会**复用**它
> (不启第二个进程),PID 文件标记 `reused=true`。`stop_production.ps1` 遇到 `reused=true`
> 时**既不按 PID kill、也不走命令行兜底** —— 那个进程不是本项目拥有的,保留不动,
> 需要停止请手动处理。

**退出码表**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 3 | `run_workflow.py`:OpenStoryline 未就绪,工作流提前终止(脚本透传为 24) |
| 10-16 | preflight Step 1-6 失败(脚本透传) |
| 19 | `-PostgresUri` 非法(格式 / 缺 host / 缺库名) |
| 20 | Postgres 角色/库对齐失败 |
| 21 | Web 进程启动后提前退出 |
| 22 | Web 健康检查超时(180s) |
| 23 | `:7860` 端口冲突(被非本项目进程占用) |
| 24 | 工作流因 `node_02` 未就绪提前终止 |
| 25 | Phase 3 checkpoint 查询失败(验收未完成) |
| 26 | openstoryline 资源/模型缺失 |
| 27 | `run_workflow.py` 非预期失败 |
| 99 | preflight 未捕获异常 |

**失败与恢复**

| 症状 | 退出码 | 处理 |
|---|---|---|
| `当前 PowerShell 不是管理员` | 11 | 用「以管理员身份运行」重开 PowerShell,或按 README §2.4 手动起 `com.docker.service` |
| `Docker Desktop 启动后 90s 内 daemon 未就绪` | 17 | 手动打开 Docker Desktop GUI,等「Docker Desktop is running」后重跑 |
| `openstoryline/resource/ 缺失或为空` | 26 | 跑 `.\openstoryline\scripts\download_resources.ps1` + `download_models.ps1`(约 642 MB) |
| `端口 7860 已被占用,但 GET 不返回 200` | 23 | 手动查出占用进程并处理;脚本**不会**杀未确认归属的进程 |
| `180s 内 OpenStoryline Web 未就绪` | 22 | 看 `logs\openstoryline_web_*.log` 尾部的报错;脚本**不会**带未就绪的服务跑工作流 |
| `preflight.ps1 退出码 15(Postgres 不可达)` | 15 | 确认 `-PostgresUri` 可连;外部库需先自行建库建角色 |
| `角色/库对齐失败` | 20 | 看 `logs\start_*.log` 里 `align_postgres.py` 的报错(密码已脱敏) |
| `验收未完成:checkpoint 查询失败` | 25 | 确认 Postgres 可连 + `checkpoints` 表已建(Phase 1 的 Step 6 会建) |
| `OpenStoryline 未就绪,工作流提前终止` | 24 | Web 没起来;human 模式下先确认 `:7860` 的服务健康 |

**日志**

每次运行写一份 `logs\start_<时间戳>.log`;OpenStoryline Web 另写
`logs\openstoryline_web_<时间戳>.log`,PID 信息写 `logs\openstoryline_pids_<时间戳>.json`。
日志里的密码一律脱敏成 `***`。

## 3. 运行测试

```powershell
cd E:\Documents\kuaishou\auto-video-editor
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
| P1-2 | FireRedASR2S client | `jy_common/asr_client.py` | ✅ client 骨架完整,服务启端由用户做 |
| P1-3 | 两级超时接入(默认关闭) | `monitoring/timeout_watchdog.py` + `config.ENABLE_TWO_LEVEL_TIMEOUT` | ✅ 接入完整,默认 false 避免破坏既有 24 条集成测试 |
| P2 | 原子写入入口统一 | `jy_common/draft_writer.py` | ✅ 薄包装 re-export |

**仍需用户行动项**:

1. **剪映 v5.9.0 客户端字段逆向(P0-1/P0-2)**:在剪映客户端打开 `drafts/default/draft_content.json`,
   手动加淡入/淡出/音量 + 替换云端资源,保存后 `diff` 出真实字段名与真实 `resource_id`,
   回填到:
   - `nodes/node_13_adjust_volume.py` 的 `_PLACEHOLDER_*_KEY` 常量
   - `templates/*.json` 的 `_reverse_engineering_pending` 改为 `false` 并填入真实 ID

2. **真实 FireRedASR2S 服务启端(P1-2)**:启 `E:\Documents\kuaishou\FireRed-OpenStoryline` ASR 服务,
   设置:
   ```powershell
   $env:ASR_BACKEND = "firered"
   $env:FIRERED_ASR_ENDPOINT = "http://127.0.0.1:8009/transcribe"
   ```

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
  └─→ [英文线] fork_draft_for_english_branch(显式依赖 08 产出的 asr_segments_zh)  │
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

### 4.9b `db/` — 应用层迁移与 `chat_sessions` 仓储

PostgreSQL 统一改造方案(阶段 A / B)新增的应用层数据访问层。**运行时唯一库 = `video_workflow`**。

| 模块 | 职责 |
|---|---|
| `db/migrations.py` | 版本化 SQL 迁移器。管 `app_schema_migrations` / `chat_sessions` / `workflow_runs` |
| `db/session_repository.py` | `chat_sessions` 仓储:CRUD、乐观锁(`version`)、游标分页 |
| `db/connection.py` | 连接辅助:把 `client_encoding` 设成 UTF-8(本机库是 `SQL_ASCII`) |

**分工边界**:LangGraph 自己的 4 张 checkpoint 表由 `AsyncPostgresSaver.setup()` 建
(`scripts/setup_postgres_schema.py`),应用迁移**不碰**它们。

```powershell
# 执行应用层迁移(幂等,可重复跑)
.\.venv\Scripts\python.exe scripts\migrate_db.py

# 只看已应用的迁移版本
.\.venv\Scripts\python.exe scripts\migrate_db.py --status
```

```python
from db.session_repository import SessionRepository

repo = SessionRepository()                      # 用 POSTGRES_URI / config 默认值
repo.create(session_id, state_json=state)       # 新建
repo.update_state(session_id, state, expected_version=1)  # 乐观锁:版本对不上抛冲突
repo.list(limit=50, cursor=(updated_at, sid))   # 游标分页(不用 offset)
```

⚠️ **本机库是 `SQL_ASCII`**:写 JSONB 必须用 `db.session_repository.jsonb()`
(内部 `ensure_ascii=False`),否则 PG 报 `UntranslatableCharacter`;读 TEXT 列用
`text_value()` 解码。详见 `db/connection.py` 的模块文档。

### 4.9c 会话状态入 PostgreSQL(阶段 D)与会话 REST API

聊天会话的完整状态存 `chat_sessions.state_json`(JSONB),**不再**写 `session_state.json`。
浏览器侧栏历史以**服务端为唯一事实来源**。

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
# 一次性工具:旧 session_state.json 导入 chat_sessions(幂等;--archive 会把旧文件移出活动路径)
.\.venv\Scripts\python.exe scripts\import_session_states.py --dry-run
.\.venv\Scripts\python.exe scripts\import_session_states.py --archive

# 一次性工具:旧 SQLite checkpoint 弃用清单 + 归档(移动到 logs/retired_checkpoints/<日期>/)
.\.venv\Scripts\python.exe scripts\retire_sqlite_checkpoints.py --apply

# 断连演练:PostgreSQL 不可达时必须明确报错,且不落本地文件
.\.venv\Scripts\python.exe scripts\drill_disconnect.py
```

⚠️ 这三个脚本是**一次性迁移工具**,不是运行时组件;运行时代码已不引用任何 SQLite / JSON 文件。

### 4.10 2026-10 接入原"不可达节点"(三个开关)

原先注册了、但从 START 永远走不到的 6 个节点,其中 **5 个已接入主图**;`clean_cache` **不接边**,只由 Web「清理缓存」按钮手动触发。**每个节点各自挂在与它最相关的现有节点旁边,不集中放在同一处。**
每条旁支由一个环境开关控制,**默认全开**。三个开关都设为 `0` 时,图的执行轨迹与接入前完全一致
(`tests/integration/test_unreachable_nodes_wiring.py` 里有黄金轨迹锁定)。

| 新节点 | 挂在哪(相关的现有节点) | 触发条件 | 开关(默认 `1`) |
|---|---|---|---|
| `clean_cache` | **不接边**(保留 `add_node` 注册,零条入边/出边);仅 Web「清理缓存」按钮(`POST /api/system/clean-cache`)显式触发 | 只有人点 Web 按钮 | —(无开关) |
| `storyline_search_web_topic` | `storyline_load_media` 之后,与 `storyline_search_media` / `storyline_split_shots` 并行的旁支(走到 END) | `STORYLINE_MODE=auto` + `storyline_targets.query` 非空 | `STORYLINE_ENABLE_WEB_TOPIC` |
| `storyline_local_asr` | `storyline_split_shots` 之后,与 `storyline_understand_clips` 并行的旁支 | `STORYLINE_MODE=auto` + 素材有音轨 | `STORYLINE_ENABLE_ASR_BRANCH` |
| `storyline_speech_rough_cut` | 紧跟 `storyline_local_asr`(走到 END) | 随 `local_asr` | 同上 |
| `storyline_generate_ai_transition` | 与 `storyline_plan_timeline_pro` 并排(同一道 4 源屏障) | `STORYLINE_MODE=auto` | `STORYLINE_ENABLE_AI_TRANSITION` |
| `storyline_plan_timeline_ai_transition` | 在 `plan_timeline_pro` + `generate_ai_transition` 之后、`qa_gate` 之前(`qa_gate` 的屏障里用它替换 `plan_timeline_pro`) | 同上 | 同上 |

备注:

- `clean_cache` ⚠️ **不再自动执行**;需要清理请点 Web 按钮(有破坏性:会删剪映草稿 `.bak`/`.backup`、回收站、FireRed 缓存与日志、`__pycache__` 等)。
- 网页话题:标题作为"外部不可信参考"软注入 `storyline_generate_script` 的 `overall`;失败 / 无结果不影响主线。
- ASR:只回填 `Clip.transcript`,不改镜头和时长;默认 `STORYLINE_ASR_MODE=stub` 时转写为空,无可见变化。
- ASR 已知限制(接入前就存在,本次没改):配了 `STORYLINE_PEXELS_API_KEY` 时,`storyline_search_media` 会把 `storyline_media_artifact` 整个换成搜索结果清单,
  素材的 `has_audio` 随之丢失 → ASR 分支保守不触发(不会报错,只是不跑)。
- AI 转场:当前只有 stub;只挂 `ai_transitions` 清单,不改 clips。两个节点在 qa 重试回路**里面**,每轮各跑 1 次。
- **读取时机不同**:前两个开关在**运行时**读取;`STORYLINE_ENABLE_AI_TRANSITION` 在**建图时**读取(决定图里接不接这两个节点),修改后须重启进程。
- 开发约定:条件触发的节点(`web_topic` / `local_asr` / `speech_rough_cut`)**不能**放进 `add_edge([...], X)` 屏障,否则 qa 重试时会静默 END;
  qa 重试回路里的节点,状态标签必须是固定值(不能带条数),否则 qa 路由会悄悄跳过重试。

PowerShell 临时关闭(须在启动 Python 进程之前设置):

```powershell
$env:STORYLINE_ENABLE_WEB_TOPIC = "0"
$env:STORYLINE_ENABLE_ASR_BRANCH = "0"
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
| ASR 真实接入未完成 | 节点 8 用 `jy_common.asr_client.MockASRClient`;Week 4 替换为 `FireRedASR2S` |
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
- LangGraph 源码:`E:\Documents\kuaishou\langgraph-main`(本地 editable)

## 9. 进一步阅读

- [LangGraph 官方文档](https://langchain-ai.github.io/langgraph/)
- [LangGraph 源码仓库](https://github.com/langchain-ai/langgraph)
- `E:\Documents\kuaishou\.claude\skills\jianying-editor` — Week 3 借鉴字段结构(字幕样式 / AVAILABLE_ASSETS 枚举)
- `E:\Documents\kuaishou\.claude\skills\jianying-speed-fit-35s` — Week 3 节点 7 帧对齐公式金标准
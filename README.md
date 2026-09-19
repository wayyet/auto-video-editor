# AI 视频剪辑自动化工作流 — Week 2 + Week 3 交付物

本目录实现附件《第 2 周分阶段实施计划》与《第 3 周详细实施计划》的全部交付物:

- **Week 2**:LangGraph 5 节点骨架(节点 1-5)+ draft_content.json 加密检测/版本策略/原子写入底层库 + 端到端集成测试
- **Week 3**:扩展为 13+1 节点完整图(节点 1-13 + 1 升级节点)+ 关卡①/② interrupt/resume 联调 + 心跳监控 + 持久化 SqliteSaver

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
│   ├── node_04_import_and_plan.py      │
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
├── checkpoints/                        [新,gitignore] SqliteSaver 落盘目录
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
                      httpx pytest pytest-mock pytest-asyncio langgraph-checkpoint-sqlite
```

### 2.3 验证安装

```powershell
python -c "from graph import build_graph; g = build_graph(); print(type(g).__name__)"
# 应输出:CompiledStateGraph
```

### 2.4 本地启动 EDB Postgres 16.11（Docker 版，推荐）

Week 5 切到 `AsyncPostgresSaver` 时，使用 Docker Desktop 启动本地 EDB Postgres。默认配置为：

- 镜像：`quay.io/enterprisedb/postgresql:16.11-3.5-postgis-multilang`
- 容器：`edb-postgres16`
- 数据库：`langgraph`
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
$env:POSTGRES_URI = "postgresql://postgres:devpass@localhost:5432/langgraph"
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
  "postgresql://postgres:devpass@localhost:5432/langgraph",
  "User"
)
```

默认 `CHECKPOINTER_BACKEND` 仍是 SQLite；只有 `WORKFLOW_ENV=production` 或显式设置 `CHECKPOINTER_BACKEND=postgres` 时才使用该容器。完全删除数据前需确认影响：checkpoint 会随 `edb_pgdata` 卷一起删除。

拉镜像前若之前遇过 `127.0.0.1:3067 connect refused` 之类的代理报错，可先跑代理自检脚本（containerd 的代理层偶尔会被指向失效端口）：

```powershell
.\scripts\check_docker_proxy.ps1                 # 只检查
.\scripts\check_docker_proxy.ps1 -RestartDocker  # 检查失败自动 docker desktop restart
```

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

### 4.1 持久化 checkpointer

`config.make_checkpointer(backend, thread_id)` 支持两种后端:

```python
from config import make_checkpointer

# 内存(单测 / 临时运行)
saver = make_checkpointer("memory")

# SQLite(Week 3 默认,支持多日挂起恢复)
saver = make_checkpointer("sqlite", thread_id="video-001")
# 文件落盘: <workspace>/checkpoints/<thread_id>.sqlite
```

### 4.2 interrupt / resume 模式

```python
from graph import build_graph
from langgraph.types import Command

g = build_graph(thread_id="video-001")  # 默认 sqlite

# 第一次 invoke:跑到关卡① 挂起
try:
    g.invoke(initial_state, config={"configurable": {"thread_id": "video-001"}})
except GraphInterrupt:
    pass

# 用户在剪映手动调整后,用 Command(resume=True) 续跑
g.invoke(Command(resume=True), config={"configurable": {"thread_id": "video-001"}})
```

### 4.3 心跳监控

编排进程启动时 `build_graph()` 自动调用 `start_heartbeat()`(后台 daemon 线程,默认 10s 写一次时间戳到 `C:\ProgramData\VideoWorkflow\heartbeat.txt`)。

注册外部监控:

```powershell
# 管理员 PowerShell
.\monitoring\register_heartbeat_task.ps1
# 默认每 2 分钟检查一次,心跳超时 120s 即写本地日志告警
```

### 4.4 Week 5 持久化 checkpointer — Postgres 后端

`config.make_checkpointer` Week 5 起支持 `backend="postgres"`(生产路径),由
`graph.get_checkpointer()` asynccontextmanager 包装管理 ``setup()`` 与连接池:

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
| `WORKFLOW_ENV` | `production` 强制用 Postgres;其它值走 `CHECKPOINTER_BACKEND` | `development` |
| `POSTGRES_URI` | `postgresql://user:pass@host:port/dbname` | `postgresql://postgres:postgres@localhost:5432/video_workflow` |

依赖(`pyproject.toml` / `requirements.txt`):`langgraph-checkpoint-postgres>=3.1` + `psycopg[binary,pool]>=3.2`。本机未启 Postgres 时,`tests/integration/test_postgres_checkpointer.py` 自动 skip。

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

### 4.6 Week 5 关卡③ 拓扑变化 — 节点 16 拆分

Week 5 把 `node_16_translate_subtitles`(翻译 + interrupt + 写 SRT 三合一)拆成两个独立节点:

```
bridge_snapshot2 → fork → node_16a_translate_and_check
                                  ├─→ node_checkpoint3_layout_review ⏸ interrupt("③")(条件触发)
                                  └─→ node_17_inject_english_tts → join_before_delivery → END
```

- **`node_16a_translate_and_check`**:翻译 + 写 marker + 写 `state["subtitle_segments_en"]` + 写 `layout_issues` / `layout_issues_detected` + **不调 interrupt**(Week 5 计划 §2.1 "关键发现①")。完全幂等,resume 重放由 `_marker_exists` 守住。
- **`node_checkpoint3_layout_review`**:只读 `layout_issues`,做 `interrupt({"checkpoint": "③", ...})`,resume 后重写 SRT(Week 5 修订:不写 `subtitle_segments_en`,避免与 16a 在同一 superstep 触发 LastValue 并发写)。
- **`route_after_translate`**:根据 `state["layout_issues_detected"]` 二选一走向 c3 或 17。
- **`node_17_inject_english_tts_stub`**:Week 5 升级为在 `draft_dir_en_branch` 下写 `en_dub.wav` 空 wav 占位(>5KB),产出 `en_audio_path` 字段,让阶段五 `acceptance_check.py` "英文配音音轨非空" 断言通过。真实 FireRedTTS2 留 Week 6+。

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

runs_table.init_schema()  # 幂等建表
runs_table.upsert_run(thread_id, video_name, "①", "suspended")  # 关卡前
runs_table.mark_completed(thread_id)                              # 流程结束后
runs_table.list_suspended()  # 运维查询入口
```

Postgres 不可达时所有调用降级为 warning no-op,不阻塞主流程。

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
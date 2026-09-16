# video-agent-kit 0.4.3 vs wayyet/auto-video-editor 功能差异对比

> 对比日期：2026-09-16  
> 对比对象：
> - `video-agent-kit` 0.4.3
> - `wayyet/auto-video-editor` `main`
> 
> 对比目标：以 `video-agent-kit 0.4.3` 的 Skill 能力为基准，逐项判断 `auto-video-editor` 已有能力、实现方式、额外能力、缺失能力以及当前代码完成度。

---

## 1. 先给结论

这两个项目不是简单的“谁包含谁”，而是两个不同抽象层次的系统。

### 1.1 video-agent-kit 0.4.3 的定位

`video-agent-kit` 更接近：

```text
宿主 Agent
    ↓
Skill 路由
    ↓
确定性的 Video MCP Runtime
    ↓
FFmpeg / OpenCV / ASR / TTS / Timeline / Render / QC
    ↓
可验证的视频产物
```

它把“智能决策”和“媒体执行”分开：

- 主 Agent 负责理解视频、制定剪辑方案、写旁白、选择镜头和判断结果。
- MCP 工具负责确定性的媒体操作。
- Skill 负责把这些工具组织成通用工作流。
- Hook、文件契约和 QC 用于约束最终产物完整性。

公开的 0.4.3 源码拆解资料显示，它包含 5 个核心 Skill 和 37 个 MCP 注册工具，并有 SessionStart / UserPromptSubmit / Stop 三类 Hook。工具侧覆盖抽帧、转录、时间线、渲染、TTS、QC 等能力。

### 1.2 auto-video-editor 的定位

`auto-video-editor` 更接近：

```text
固定的视频生产业务流程
        ↓
LangGraph StateGraph
        ↓
WorkflowState
        ↓
节点 1 → 2 → 3 → ... → 17
        ↓
人工关卡 / interrupt / resume
        ↓
SQLite Checkpointer
        ↓
剪映 draft_content.json
        ↓
中文成片 + 英文分支 + 封面 + 字幕 + TTS
```

它的重点不是做一个通用的“视频工具箱”，而是建立一套面向剪映和 Windows 环境的、可断点恢复的自动化视频生产链。

### 1.3 最核心的差异

可以用一句话概括：

```text
video-agent-kit
= 通用视频 Agent 的 Skill + 确定性媒体执行层 + QC 证据层

auto-video-editor
= 面向剪映生产链的 LangGraph 业务编排层 + 持久化 + 人工关卡 + 多语言交付链
```

因此：

- `video-agent-kit` 的优势集中在“通用工具能力、可复用 Skill、媒体执行、QC/证据”。
- `auto-video-editor` 的新增能力集中在“固定业务流程、Windows/剪映生态、LangGraph 状态机、人工审批点、跨进程恢复、中文→英文生产分支”。

---

# 2. 对比依据与证据等级

## 2.1 video-agent-kit 0.4.3

当前环境不能直接从 GitHub 将用户指定的 `wayyet/video-agent-kit` 仓库完整 clone 到本地，因此本报告对 `video-agent-kit 0.4.3` 的 Skill、MCP 工具数量和内部实现，主要依据公开的 0.4.3 源码拆解资料，以及公开 Skill 市场条目。

公开资料确认的信息包括：

- 5 个核心 Skill：
  - `env-setup`
  - `video-edit-agent`
  - `video-edit-assembly`
  - `video-recap-workflows`
  - `video-speech-workflows`
- 37 个 MCP 工具。
- MCP 工具覆盖视频观察、转录、抽帧、剪辑、时间线、渲染、TTS、QC 等。
- 有 Hook：环境检查、视频输入检查、Stop 收尾检查。
- 有 timeline / transcript / video_observation Schema。
- 有文件契约、hash/fingerprint、QC evidence 等工程约束。

参考：

- https://bingqiangzhou.github.io/posts/zcode-video-agent-kit/
- https://llmbase.ai/skills/wwwzhouhui/jimeng-mcp-skill/

## 2.2 auto-video-editor

`auto-video-editor` 的判断依据为 2026-09-16 对 `main` 分支的静态代码验证结果和前期实施计划文档。

需要特别区分：

```text
“代码存在 / 图结构正确 / 测试文件存在”
≠
“当前环境已经真实跑通”
```

此前代码验证报告明确记录：当前执行环境无法解析 `github.com`，因此没有重新 clone 仓库，也没有重新执行完整 pytest、剪映客户端、FireRedASR2S、Windows Task Scheduler 等运行时验证。

因此本报告把：

- 代码已经落地的能力
- 计划设计中的能力
- 当前仍是 Mock / Stub / Placeholder 的能力

分开列出。

---

# 3. 两个项目的架构差异

| 维度 | video-agent-kit 0.4.3 | auto-video-editor | 差异说明 |
|---|---|---|---|
| 系统定位 | 通用视频编辑 Agent 工具箱 + Skill | 特定视频生产业务工作流 | 抽象层不同 |
| 总控 | 宿主 Agent + Skill 路由 | LangGraph `StateGraph` | auto-video-editor 更像业务编排器 |
| 状态 | 以文件契约、缓存、artifact 为核心 | `WorkflowState` + SQLite checkpoint | auto-video-editor 更强调显式工作流状态 |
| 工作流表达 | Skill / workflow 文档 + MCP tool | 节点 + 边 + 条件边 + interrupt | 两者编排方式不同 |
| 媒体执行 | MCP 工具 | OpenStoryline / 剪映 / Python client / 外部服务 | auto-video-editor 更强依赖具体生态 |
| 持久化 | 文件产物、缓存、hash/fingerprint | SQLite Checkpointer | auto-video-editor 原生支持 thread resume |
| 人工介入 | Hook / Agent 决策 / 文件契约 | 关卡①②③ `interrupt()` | auto-video-editor 的人工关卡更明确 |
| 恢复机制 | 依赖文件契约和工作流可重入设计 | `thread_id` + SQLite + `Command(resume=True)` | auto-video-editor 更明确地做了流程级恢复 |
| 环境目标 | 相对通用、本地插件式 | Windows + 剪映 + OpenStoryline | auto-video-editor 更专用 |
| 工具协议 | MCP 为主 | MCP + Python + 本地文件 + 外部 client | auto-video-editor 是混合执行模式 |
| QC | 强调 evidence、hash、fingerprint、接缝证据 | 主要是节点状态、字段检查、join QA、集成测试 | QC 抽象不同 |
| 产物契约 | 核心设计 | `draft_content.json` + workflow artifacts | auto-video-editor 对剪映草稿有更强耦合 |
| 扩展方式 | 新增 Skill / MCP Tool / Workflow | 新增 LangGraph Node / Client / Provider / State 字段 | 两者扩展边界不同 |

---

# 4. video-agent-kit 0.4.3 的 Skill 能力基线

## 4.1 `video-edit-agent`

负责总控和任务路由。

典型路由逻辑：

```text
用户任务
  │
  ├─ 多素材混剪
  │      └→ video-edit-assembly
  │
  ├─ 单源口播视频
  │      └→ video-speech-workflows
  │
  ├─ 电影 / 足球 / LOL / 篮球解说
  │      └→ video-recap-workflows
  │
  └─ 其他
         └→ 通用 timeline workflow
```

它不是单纯的工具目录，而是一个“任务分类 + workflow 路由器”。

## 4.2 `video-edit-assembly`

重点是多素材混剪：

- 素材发现
- 视频观察
- 选择镜头
- timeline 组装
- render
- validation / QC

## 4.3 `video-speech-workflows`

面向口播 / 单源视频：

- `speech-condense`
- `talking-head-subtitles`
- `video-pipeline`

典型能力：

- 去口头禅
- 去重复表达
- ASR 时间戳驱动剪切
- 口播字幕
- TTS / 旁白

## 4.4 `video-recap-workflows`

提供内容类型专用工作流：

- `movie-recap`
- `soccer-recap`
- `lol-recap`
- `basketball-recap`

这意味着其 Skill 不只是“调用 FFmpeg”，而是包含内容类型级 workflow 约束。

## 4.5 `env-setup`

负责环境体检：

- FFmpeg
- 字幕能力
- 语音能力
- 依赖
- 本地运行环境

---

# 5. 详细功能矩阵

图例：

- ✅ = 当前明确存在
- 🟡 = 部分存在 / 实现方式不同 / 当前仍有外部依赖
- ❌ = 当前项目没有对应能力，或当前版本未实现
- ⚠️ = 计划中存在，但不能视为已经完成

| 功能 | video-agent-kit 0.4.3 | auto-video-editor | 差异 |
|---|---:|---:|---|
| 通用视频剪辑 | ✅ | 🟡 | auto-video-editor 更偏固定业务流程 |
| 多素材混剪 | ✅ | 🟡 | auto 有分镜链，但不是通用 assembly Skill |
| 视频观察 / 抽帧 | ✅ | ✅/依赖 OpenStoryline | 实现位置不同 |
| Contact Sheet | ✅ | 非核心 | video-agent-kit 明确把观察结果作为工作流基础 |
| 镜头检测 / Shot Plan | ✅ | ✅ | auto 主要交给 OpenStoryline VLM |
| 内容理解 | ✅ 主 Agent 驱动 | ✅ OpenStoryline VLM + 节点 | AI 所在层次不同 |
| ASR | ✅ | 🟡 | auto Week3 默认 MockASRClient，真实 FireRedASR2S 未完全接入 |
| ASR 时间戳 | ✅ | ✅/🟡 | auto 的字幕数据链路有结构，但真实 ASR 尚未闭环 |
| 口播压缩 | ✅ | 非核心 | video-agent-kit 有专门 `speech-condense` |
| 去口头禅 | ✅ | 非核心 | auto 的固定剪映链未专门建模 |
| Talking-head 字幕 | ✅ | 🟡 | auto 有字幕节点，但更依赖剪映草稿 |
| TTS | ✅ | 🟡 | auto 有英文 TTS 分支，但第4周仅允许 stub，第5周仍有静音 WAV 占位 |
| 混音 | ✅ | 🟡 | auto 有节点13设计，但实际音量/淡入淡出尚未完成 |
| Timeline | ✅ | ✅ | 都是核心能力 |
| Render | ✅ | ✅ | 两者都有，但落地点不同 |
| Timeline Schema | ✅ 独立 Schema | 🟡 通过 `draft_content.json` / State | auto 更依赖剪映数据结构 |
| QC | ✅ 强 | 🟡 | auto 有字段验证、join QA、集成测试，但证据链不等价 |
| 接缝前后帧证据 | ✅ | ❌ | video-agent-kit 明确提供 |
| 接缝波形 / dB 证据 | ✅ | ❌ | video-agent-kit 明确提供 |
| Timeline hash | ✅ | 非核心 | video-agent-kit 用于 QC / artifact 闭环 |
| Video fingerprint | ✅ | 非核心 | auto 不是该项目的核心契约 |
| Stop Hook | ✅ | ❌ | auto 主要依赖 LangGraph 运行结束与节点验证 |
| 文件契约 | ✅ 核心 | ✅ | 但 auto 的契约集中在剪映草稿和业务 artifacts |
| MCP Runtime | ✅ 37 工具 | 🟡 | auto 使用 MCP/外部服务，但没有对等的通用 37-tool runtime |
| Skill 路由 | ✅ 5个核心 Skill | ❌/🟡 | auto 主要通过 LangGraph graph/node 组织 |
| 电影解说 | ✅ | 非核心 | video-agent-kit 有专用 workflow |
| 足球集锦 | ✅ | 非核心 | video-agent-kit 有专用 workflow |
| LOL 集锦 | ✅ | 非核心 | video-agent-kit 有专用 workflow |
| 篮球集锦 | ✅ | 非核心 | video-agent-kit 有专用 workflow |
| OpenStoryline VLM 分镜 | 非核心 | ✅ | auto 将 OpenStoryline 放在生产链前段 |
| 剪映 `draft_content.json` | 非核心 | ✅ 核心 | auto 的独有业务耦合 |
| 剪映草稿原子写入 | 非核心 | ✅ | auto 有 `atomic_writer.py` |
| 剪映加密检测 | 非核心 | ✅ 设计/代码链路 | auto 专门处理剪映草稿版本问题 |
| 剪映版本策略 | 非核心 | ✅ | 有 version-lock / one-way-write 设计 |
| 35 秒总时长护栏 | ❌ | ✅ | auto 的业务规则 |
| 图级重试 | ✅ 可通过 workflow | ✅ | auto 在节点7明确采用条件边重试 |
| 节点7最大3次重试 | 非特定能力 | ✅ | auto 的具体业务约束 |
| 人工关卡① | 非固定 | ✅ | 分镜顺序人工调整 |
| 人工关卡② | 非固定 | ✅ | 人工加 BGM |
| 人工关卡③ | 非固定 | ✅ | 英文字幕/布局检查 |
| `interrupt()` | ❌ 不是其核心编排模型 | ✅ | LangGraph 原生能力 |
| `Command(resume=True)` | 非核心 | ✅ | auto 明确实现 |
| SQLite Checkpointer | ❌ 不是其核心持久化方式 | ✅ | auto 核心基础设施之一 |
| `thread_id` 隔离 | 非核心 | ✅ | auto 有明确测试 |
| 跨进程恢复 | 文件工作流可实现 | ✅ | auto 有 SQLite 方案与测试 |
| 心跳文件 | 非核心 | ✅ | `C:\ProgramData\VideoWorkflow\heartbeat.txt` |
| PowerShell watchdog | ❌ | ✅ | auto Windows 专用能力 |
| Windows Task Scheduler | ❌ | ✅ | auto Windows 运维链能力 |
| 中文封面 9:16 | 可做普通媒体处理 | ✅/🟡 | auto 当前默认使用 MockJianyingCoverClient |
| 16:9 封面 | ✅ 可通过工具实现 | ✅ | auto Pillow 方案 |
| 4:3 封面 | ✅ 可通过工具实现 | ✅ | auto Pillow 方案 |
| `text_bbox` | 非核心 | ✅ | auto 为英文封面本地化提供位置依据 |
| 英文封面本地化 | 可通过宿主 Agent + 工具实现 | ✅/🟡 | auto 有专门节点，但 FireRed Image Edit 当前 Mock |
| 中英文字幕 | ✅ | ✅ | auto 有独立英文分支 |
| 英文 SRT | ✅ 可生成 | ✅ | auto 有显式文件产物 |
| 英文字幕布局人工复核 | 非固定 | ✅ | auto 有 checkpoint3 |
| 英文 TTS | ✅ | ✅/🟡 | auto 当前阶段仍有 stub / 静音占位限制 |
| 在线素材搜索 | 可扩展 | 非核心 | video-agent-kit 本身不是资源搜索平台；auto 同样非核心 |
| BGM 推荐 | Agent 可决策 | ❌ 当前主要人工 | auto 第12步明确保留人工判断 |
| AI 转场生成 | ❌ | ❌ | 两者都不是当前重点能力 |
| 字体智能推荐 | Agent 可决策 | ❌ | auto 当前没有独立字体推荐节点 |
| Web UI | 非核心 | 非核心/依赖 OpenStoryline | auto 可借 OpenStoryline UI |
| Docker 独立产品化 | 非核心 | 非核心 | auto 更依赖 Windows/剪映 |
| 通用跨平台 | 相对更强 | 较弱 | auto 使用大量 Windows/剪映能力 |
| Skill 复用 | ✅ | 🟡 | auto 更偏 graph/node 复用 |
| 生产链固定化 | 🟡 | ✅ | auto 的主要设计目标 |

---

# 6. auto-video-editor 相比 video-agent-kit 新增的能力

这一部分是两者最重要的“功能差异”。

## 6.1 固定的 17 步视频生产链

auto-video-editor 将工作流明确拆成 17 步：

```text
1  清理缓存
2  启动 OpenStoryline
3  打开预览 / Web UI
4  分镜规划
5  生成剪映初始草稿
6  人工调整
7  35 秒速度适配
8  字幕
9  转场 / 视频特效
10 花字 / 字幕动画
11 贴纸
12 人工添加 BGM
13 音量 / 淡入淡出
14 三比例封面
15 英文封面本地化
16 英文字幕翻译与布局检查
17 英文 TTS
```

这是一条“业务 SOP”，而不是通用工具链。

## 6.2 LangGraph 状态机

核心不是简单的 Python 函数串联，而是：

```text
WorkflowState
      ↓
StateGraph
      ↓
Node
      ↓
Conditional Edge
      ↓
Interrupt / Resume
      ↓
Checkpointer
```

这一层是 `video-agent-kit` 0.4.3 Skill 体系中没有直接对应的等价物。

## 6.3 SQLite 持久化

auto-video-editor 明确将：

```text
checkpoints/<thread_id>.sqlite
```

作为工作流状态持久化载体。

因此一个正在人工暂停的任务可以：

```text
进程退出
   ↓
重新启动
   ↓
相同 thread_id
   ↓
读取 SQLite checkpoint
   ↓
resume
   ↓
继续后续节点
```

它解决的是“长任务、人工介入、跨进程恢复”的业务问题。

## 6.4 三个明确的人机协作关卡

### 关卡①

人工调整分镜顺序。

### 关卡②

人工在剪映中选择 BGM。

### 关卡③

人工检查英文字幕布局；有问题才 interrupt，人工修改后 resume。

这说明 auto-video-editor 把 Human-in-the-loop 做成了流程节点，而不是只依赖 Agent 自主决定。

## 6.5 剪映草稿作为核心 Artifact

auto-video-editor 把：

```text
draft_content.json
```

当作整个生产链的核心中间产物。

所以它必须解决：

- 草稿加密
- 剪映版本兼容
- 原子写入
- 重试幂等
- snapshot
- 人工修改后的再读取

而 `video-agent-kit` 更偏向自己的 timeline / transcript / render / QC artifact contract，并不以剪映 `draft_content.json` 作为系统中心。

## 6.6 35 秒硬业务护栏

节点7定义：

```python
TARGET_DURATION_US = 35_000_000
NODE_07_MAX_RETRY = 3
```

并使用 LangGraph 条件边重试，而不是在节点内部通过 `while` 隐藏重试。

这属于很明确的业务规则。

## 6.7 中文 → 英文生产分支

auto-video-editor 不是“生成一个视频”就结束，而是：

```text
中文主线
   ↓
snapshot2
   ├──────────────→ 中文封面 3 比例
   │
   └→ fork English Branch
          ↓
       英文字幕
          ↓
       英文布局检查
          ↓
       英文 SRT
          ↓
       英文 TTS
```

这种“从同一快照派生语言分支”的业务模型，是 auto-video-editor 的明显特色。

## 6.8 Windows 进程监控链

仓库中还加入：

```text
heartbeat_writer.py
       ↓
heartbeat.txt
       ↓
heartbeat_monitor.ps1
       ↓
Windows Task Scheduler
```

这不是视频剪辑本身的能力，而是生产流水线的运行保障能力。

---

# 7. video-agent-kit 相比 auto-video-editor 新增或更完整的能力

## 7.1 更通用的 Skill 路由

`video-agent-kit` 有明确的：

```text
video-edit-agent
video-edit-assembly
video-speech-workflows
video-recap-workflows
env-setup
```

因此它可以按任务类型把请求路由到不同 workflow。

`auto-video-editor` 更接近“固定生产链”，而不是“通用视频任务路由器”。

## 7.2 37 个通用 MCP 工具

公开 0.4.3 源码拆解资料确认注册 37 个工具名。

工具集中包括：

- 视频观察
- 抽帧
- 转录
- 字幕相关
- Timeline
- Render
- TTS
- QC

因此它更适合直接作为一个“媒体执行 runtime”被不同 Agent 复用。

## 7.3 专业内容类型 Workflow

`video-agent-kit` 有：

```text
movie-recap
soccer-recap
lol-recap
basketball-recap
```

`auto-video-editor` 当前公开的工作流没有与之对应的专门“体育/电竞/电影解说 Skill”。

## 7.4 更完整的口播工作流抽象

`video-speech-workflows` 把：

```text
ASR
 ↓
文本清洗
 ↓
去口头禅
 ↓
时间轴裁剪
 ↓
字幕
 ↓
TTS / 旁白
```

作为独立 workflow 处理。

auto-video-editor 当前主要围绕剪映生产链节点推进，口播压缩不是第一层抽象。

## 7.5 更明确的 QC 证据链

`video-agent-kit` 的 QC 不只是：

```text
success = true
```

而是进一步产生：

- timeline hash
- transcript 绑定
- video fingerprint
- QC report
- 接缝前后 frame
- 接缝两侧波形
- dB 信息
- render 产物验证

所以 Agent 可以根据证据继续判断，而不是只读取一个布尔结果。

## 7.6 Stop Hook / 完成门禁

`check_closeout.py` 用于停止前检查：

```text
素材探针
↓
Transcript
↓
Observation
↓
Timeline
↓
Validation
↓
Render
↓
QC
↓
Final
```

如果文件契约没有闭环，则阻止流程直接结束。

这是 auto-video-editor 当前没有直接对等实现的设计。

## 7.7 更强的 Agent / Tool 解耦

video-agent-kit 的原则是：

```text
Agent 决策
    ↓
MCP Tool
    ↓
确定性媒体执行
```

因此媒体工具可以被不同宿主 Agent 复用。

auto-video-editor 则明显把业务流程、状态、剪映草稿和外部服务绑定在一起，复用边界更偏“业务工作流”。

---

# 8. 两者有能力，但实现方式不同

这一部分容易误判。两个项目有不少“表面功能相同、工程实现完全不同”的地方。

## 8.1 ASR

### video-agent-kit

ASR 是通用媒体工具能力，输出 transcript，供后续剪辑决策和字幕等 workflow 使用。

### auto-video-editor

节点8从 `snapshot2` 开始：

```text
snapshot2
   ↓
ASR
   ↓
materials.texts
```

但当前代码验证显示默认还是 `MockASRClient`，真实 `FireRedASR2S` 尚未完全闭环。

因此：

```text
接口设计：✅
真实外部能力：🟡
```

## 8.2 Timeline

### video-agent-kit

以独立 timeline schema、工具和 workflow 为中心。

### auto-video-editor

timeline 最终落在剪映 `draft_content.json` 的结构上。

所以：

```text
video-agent-kit = 通用 timeline

auto-video-editor = 剪映 timeline
```

## 8.3 TTS

### video-agent-kit

TTS 是通用 MCP Tool，并且公开资料显示默认可使用 Edge TTS。

### auto-video-editor

英文 TTS 是固定业务步骤17：

```text
英文字幕
  ↓
TTS
  ↓
英文音频
  ↓
最终视频
```

但第4周只要求 stub；当前后续代码还有静音 WAV 占位，因此不能把它理解为已经完全接入真实 TTS provider。

## 8.4 人工介入

两个项目都允许人工影响最终结果，但模型不同：

```text
video-agent-kit
= Agent + Skill + 文件契约 + Hook

auto-video-editor
= LangGraph interrupt + SQLite + resume
```

auto-video-editor 的人工暂停/恢复是工作流状态机的一等能力。

## 8.5 Retry

两个项目都可以实现失败重试。

但 auto-video-editor 对节点7做了非常明确的业务约束：

```text
≤ 35s  → 继续
> 35s   → 重新执行节点7
超过 3 次 → escalate
```

并且把 retry 放在图结构中，而不是节点内部 while。

---

# 9. auto-video-editor 当前代码中仍未完全完成的功能

这一部分非常重要。不能因为“架构设计有”就把它当作“当前版本已经完全有”。

## 9.1 第三周缺口

| 项目 | 当前状态 | 说明 |
|---|---|---|
| 真实 ASR | 🟡 | 默认 `MockASRClient` |
| 真实 VIP resource_id | 🟡 | 节点9、11仍存在 placeholder resource_id |
| 贴纸准确 segment 绑定 | 🟡 | 当前所有贴纸仍可能追加到第一个 segment |
| 花字入场动画 | 🟡 | `entrance_animation = None` |
| 两级超时 | ❌ | 只有 `TOTAL_EXECUTION_TIMEOUT_S`、`NODE_INACTIVITY_TIMEOUT_S` 配置，没有真正接入调度链 |
| 节点13音量 | ❌ | 当前函数是 placeholder |
| `materials.audio_fades` | ❌ | 当前没有真正写入 |

### 节点13是硬阻塞

当前代码类似：

```python
def adjust_volume(state: WorkflowState) -> dict:
    log = list(state.get("status_log", []) or []) + [
        "node_13_adjust_volume_placeholder_pass"
    ]
    return {
        **state,
        "status_log": log,
        "volume_adjusted": False,
    }
```

也就是说：

- 没有修改音量。
- 没有 `audio_fades`。
- 没有 fade-in。
- 没有 fade-out。
- 没有通过 `atomic_write_draft()` 回写。

因此不能把“第13步存在”理解成“第13步已经完成”。

## 9.2 第四周缺口

| 项目 | 当前状态 | 说明 |
|---|---|---|
| 9:16 真实剪映自动化 | 🟡 | 当前默认 `MockJianyingCoverClient` |
| 真实 FireRed-Image-Edit | 🟡 | 当前默认 `MockFireRedImageEditClient` |
| 5 个联调场景实际运行 | ⚠️ | 有图结构和单测源码，但本次没有实际执行记录 |
| 真实客户端 + UI 自动化 | ⚠️ | `HTTPJianyingCoverClient` 仍为 `NotImplementedError` |

## 9.3 第四周真实封面链路的实际状态

节点14虽然已经实现：

```text
9:16
16:9
4:3
```

并且使用 `asyncio.gather()` 做三路并发，但 9:16 默认客户端仍是 Mock。

节点15同样有：

```text
text_bbox
+
三比例并发
+
英文封面输出
```

但实际 `MockFireRedImageEditClient` 只是复制原图片，并没有真正执行：

```text
中文擦除
 ↓
英文重绘
```

所以完整生产能力仍未闭环。

---

# 10. auto-video-editor 当前代码已经完成的关键部分

虽然有缺口，但以下部分已经形成比较清晰的工程能力。

## 10.1 LangGraph 主图

第三周主链：

```text
START
  ↓
1 → 2 → 3 → 4 → 5
          ↓
          6 interrupt①
          ↓
          7
        ↙   ↘
     retry   8
              ↓
              9
              ↓
             10
              ↓
             11
              ↓
          12 interrupt②
              ↓
             13
```

第四周继续分叉：

```text
              ┌→ node14 → node15 ─────┐
node13 ──────┤                         ├→ join → END
              └→ fork English → 16a ──┤
                                │      │
                          checkpoint3  │
                                │      │
                                ↓      │
                               17 ─────┘
```

## 10.2 Fan-out / Fan-in

auto-video-editor 不只是线性 DAG。

它已经有：

```text
主分支
  ├─ 中文生产链
  └─ 英文生产链
```

最后再：

```text
fan-in
  ↓
join_before_delivery
```

这类模式在纯 Skill 文档中并不突出，但在 LangGraph 中非常自然。

## 10.3 Snapshot

节点7成功后生成 `snapshot2`。

这样后续节点可以基于稳定版本继续处理：

```text
原始草稿
   ↓
节点7
   ↓
snapshot2
   ├─ 中文继续
   └─ 英文 fork
```

它还承担“分支起点”的作用。

## 10.4 幂等

当前代码对这些场景已经明显考虑幂等：

- 已有英文分支目录则复用。
- 已有 marker 则避免重复翻译。
- resume 后重新读人工修改后的草稿。
- SQLite checkpoint 防止重复执行整条链。

这类设计很适合长时间运行的自动生产任务。

---

# 11. 最重要的能力边界

## 11.1 如果任务是“通用视频 Agent 能做什么”

`video-agent-kit` 更像：

```text
视频 Agent Runtime
```

可以给多个不同 Agent 使用。

例如：

```text
Claude Agent
   ↓
video-agent-kit MCP

GPT Agent
   ↓
video-agent-kit MCP

内部企业 Agent
   ↓
video-agent-kit MCP
```

工具层可以保持稳定。

## 11.2 如果任务是“固定生产企业视频”

auto-video-editor 更像：

```text
企业视频生产 Workflow
```

例如：

```text
输入视频
 ↓
OpenStoryline
 ↓
剪映草稿
 ↓
人工审核①
 ↓
自动字幕/特效
 ↓
人工审核②
 ↓
中文成片
 ↓
英文分支
 ↓
人工审核③
 ↓
英文成片
```

它更接近业务流程引擎，而不是通用媒体工具箱。

---

# 12. 两个项目之间不存在的“简单替换关系”

不能直接认为：

```text
auto-video-editor
    =
video-agent-kit + LangGraph
```

因为 auto-video-editor 有一些 `video-agent-kit` 没有作为核心抽象存在的东西：

- 剪映 `draft_content.json`
- 剪映版本 / 加密检测
- Windows Task Scheduler
- OpenStoryline MCP
- 9:16 剪映封面
- FireRed-Image-Edit
- 三个人工关卡
- 英文 fork
- Snapshot2
- 第17步固定业务链

反过来，也不能认为：

```text
video-agent-kit
    =
auto-video-editor 的节点数量更多版本
```

因为 `video-agent-kit` 有自己的通用能力集合：

- 通用 MCP runtime
- 5 个 Skill
- 37 个工具
- Movie/Soccer/LOL/Basketball recap
- speech condense
- talking-head subtitles
- QC evidence
- Stop Hook
- timeline/transcript/video_observation schema
- 宿主 Agent 解耦

所以两者是不同层次的产品。

---

# 13. 更合理的组合架构

如果目标不是二选一，而是把两个项目组合，比较自然的分层如下：

```text
                     用户自然语言
                           ↓
                    Business Agent
                           ↓
                    LangGraph Router
                           ↓
                  Workflow / Checkpoint
                           ↓
       ┌───────────────────┴───────────────────┐
       │                                       │
OpenStoryline / VLM                         video-agent-kit
负责“理解和规划”                         负责“媒体执行和 QC”
       │                                       │
       ├─ Shot Plan                            ├─ ASR
       ├─ Script                               ├─ Cut / Trim
       ├─ Clip Understanding                  ├─ Timeline
       └─ Visual Planning                     ├─ Render
                                               ├─ TTS
                                               └─ QC
       │                                       │
       └───────────────────┬───────────────────┘
                           ↓
                    Artifact Contract
                           ↓
                    Human Review
                           ↓
                  Final Delivery
```

而 `auto-video-editor` 当前的剪映分支可以放在最后的“企业交付适配层”：

```text
通用媒体 Runtime
        ↓
Jianying Adapter
        ↓
draft_content.json
        ↓
剪映
```

这样三个层次可以明确分开：

```text
AI Planning Layer
    = OpenStoryline / LLM / VLM

Workflow Layer
    = LangGraph / SQLite / Human Checkpoint

Media Runtime Layer
    = video-agent-kit / FFmpeg / ASR / TTS / QC

Delivery Adapter
    = JianYing / draft_content.json / Windows
```

这种分层比把所有能力继续堆在 `auto-video-editor` 的节点函数中更容易长期维护。

---

# 14. 从工程设计角度看，两者最值得借鉴的地方

## 14.1 auto-video-editor 值得保留的设计

### A. LangGraph 持久化

```text
State
+ thread_id
+ SQLite
+ interrupt
+ resume
```

适合长任务。

### B. 人工关卡

不是简单“暂停程序”，而是把人工审批建模成图节点。

### C. Snapshot + Fork

让中文和英文分支从稳定状态派生，而不是共享一个可变对象。

### D. 剪映适配层

把具体业务落地到 `draft_content.json`，适合固定生产链。

### E. Windows 运维监控

对桌面型视频软件尤其重要。

## 14.2 video-agent-kit 值得保留的设计

### A. Agent / Tool 解耦

Agent 决策与媒体执行严格分离。

### B. 文件契约

文件是流程状态，而不是只看聊天记录。

### C. QC Evidence

不是只输出 pass/fail，而是输出“为什么通过”的证据。

### D. Skill 路由

不同视频任务进入不同 workflow。

### E. 通用 MCP Runtime

工具可以脱离某一个业务 workflow 独立复用。

---

# 15. 最终对比总结表

| 项目 | video-agent-kit 0.4.3 | auto-video-editor |
|---|---|---|
| 核心目标 | 通用视频 Agent 能力 | 固定自动视频生产链 |
| 核心抽象 | Skill + MCP Tool | LangGraph Node + State |
| Agent | 宿主 Agent | 工作流内部编排 |
| 状态持久化 | Artifact / Cache / Contract | SQLite Checkpointer |
| Human-in-loop | Agent / Hook / 文件契约 | ①②③ interrupt/resume |
| 媒体执行 | 通用确定性工具 | 剪映 / OpenStoryline / FireRed / Python |
| 视频观察 | 强 | 主要依赖 OpenStoryline |
| ASR | 通用能力 | 有节点，但真实服务仍有缺口 |
| TTS | 通用能力 | 有英文业务链，当前仍有 stub/占位阶段 |
| Timeline | 通用 | 剪映 draft 为中心 |
| QC | 强证据链 | 业务校验 + join QA |
| Hash/Fingerprint | 核心 | 非核心 |
| Sports Recap | ✅ | 非核心 |
| Movie Recap | ✅ | 非核心 |
| LOL Recap | ✅ | 非核心 |
| Basketball Recap | ✅ | 非核心 |
| Speech Condense | ✅ | 非核心 |
| 35 秒业务护栏 | ❌ | ✅ |
| OpenStoryline VLM 分镜 | ❌/外部能力 | ✅ |
| 剪映草稿生成 | 非核心 | ✅ |
| 剪映草稿加密/版本处理 | ❌ | ✅ |
| Windows Watchdog | ❌ | ✅ |
| 多语言分支 | 非固定 | ✅ 中文→英文 |
| 3 比例封面 | 可做 | ✅ |
| 英文封面本地化 | 可组合 | ✅/🟡 |
| 真实剪映 9:16 | 非核心 | 🟡 当前 Mock |
| FireRed Image Edit | 非核心 | 🟡 当前 Mock |
| 节点13音量/淡入淡出 | 通用工具可支持 | ❌ 当前未实现 |
| 工程复用性 | 较高 | 更偏业务专用 |
| 对剪映耦合 | 低 | 高 |
| 对 Windows 耦合 | 低 | 高 |
| 对 LangGraph 耦合 | 无 | 高 |
| 对 MCP 耦合 | 高 | 中等 |
| 对生产 SOP 的表达力 | 中等 | 高 |
| 对通用媒体工具复用 | 高 | 中等 |

---

# 16. 最终结论

## 16.1 两者不是上下位关系

更准确的关系是：

```text
video-agent-kit
    ↓
通用媒体执行 / Skill / QC

        +

OpenStoryline
    ↓
AI 分镜 / 内容理解 / 创作规划

        +

LangGraph
    ↓
持久化业务编排 / Human-in-loop / Resume

        +

JianYing Adapter
    ↓
剪映业务交付
```

`auto-video-editor` 当前实际上已经走在这条组合路线的一部分上：

```text
OpenStoryline
      ↓
LangGraph
      ↓
JianYing
```

而它还没有完整拥有 `video-agent-kit` 的全部通用媒体 runtime 与 QC 体系。

## 16.2 如果只看“功能数量”，结论会失真

`video-agent-kit` 提供的是大量可复用的“底层能力”和“通用 workflow”。

`auto-video-editor` 提供的是大量“业务流程约束”和“生产环节”。

所以：

```text
video-agent-kit
偏“能力平台”

auto-video-editor
偏“业务生产线”
```

## 16.3 auto-video-editor 当前最值得补上的能力

从当前代码缺口看，优先级应首先放在“把已经设计好的生产链真正闭环”，而不是继续增加更多节点：

1. 完成节点13真实音量 / fade-in / fade-out。
2. 替换真实 ASR。
3. 替换真实 VIP resource_id。
4. 修正贴纸到真实 segment 的时间范围绑定。
5. 启用真正的两级 timeout。
6. 接入真实剪映 9:16 自动化。
7. 接入真实 FireRed-Image-Edit。
8. 在可运行环境实际执行 5 个 Week4 联调场景。
9. 再考虑把 `video-agent-kit` 的 QC / artifact evidence 体系引入。

## 16.4 最适合的长期架构

如果项目最终目标是“可持续扩展的企业级 AI 视频生产平台”，比较自然的分层是：

```text
                    ┌─────────────────────┐
                    │   用户自然语言请求   │
                    └──────────┬──────────┘
                               ↓
                    ┌─────────────────────┐
                    │   Agent / Router    │
                    └──────────┬──────────┘
                               ↓
                    ┌─────────────────────┐
                    │ LangGraph Workflow  │
                    │ State/Checkpoint    │
                    │ Human Review        │
                    └──────────┬──────────┘
                               ↓
              ┌────────────────┴────────────────┐
              ↓                                 ↓
     ┌──────────────────┐            ┌────────────────────┐
     │ AI Planning      │            │ Media Runtime      │
     │ OpenStoryline    │            │ video-agent-kit    │
     │ LLM/VLM          │            │ FFmpeg/ASR/TTS/QC  │
     └────────┬─────────┘            └─────────┬──────────┘
              └────────────────┬────────────────┘
                               ↓
                    ┌─────────────────────┐
                    │ Artifact Contract   │
                    │ Hash / Evidence / QC│
                    └──────────┬──────────┘
                               ↓
                    ┌─────────────────────┐
                    │ JianYing Adapter    │
                    │ draft_content.json  │
                    └──────────┬──────────┘
                               ↓
                    ┌─────────────────────┐
                    │   Final Delivery    │
                    └─────────────────────┘
```

这个架构可以同时保留：

- `video-agent-kit` 的通用媒体执行能力；
- `auto-video-editor` 的 LangGraph 持久化工作流；
- OpenStoryline 的 VLM 分镜和 AI 创作能力；
- 剪映作为现有生产环境的交付端；
- QC / Evidence 作为最终生产质量门禁。

---

# 17. 参考资料

## video-agent-kit

1. 0.4.3 公开源码拆解：
   https://bingqiangzhou.github.io/posts/zcode-video-agent-kit/
2. Skill 市场条目 / 0.4.3：
   https://llmbase.ai/skills/wwwzhouhui/jimeng-mcp-skill/
3. 用户指定的项目：
   https://github.com/wayyet/video-agent-kit

## auto-video-editor

1. GitHub：
   https://github.com/wayyet/auto-video-editor
2. 第三周实施计划：
   https://github.com/wayyet/auto-video-editor/blob/main/docs/AI%E8%A7%86%E9%A2%91%E5%89%AA%E8%BE%91%E8%87%AA%E5%8A%A8%E5%8C%96%E5%B7%A5%E4%BD%9C%E6%B5%81_%E7%AC%AC%E4%B8%89%E5%91%A8%E8%AF%A6%E7%BB%86%E5%AE%9E%E6%96%BD%E8%AE%A1%E5%88%92.md
3. 第四周实施计划：
   https://github.com/wayyet/auto-video-editor/blob/main/docs/AI%E8%A7%86%E9%A2%91%E5%89%AA%E8%BE%91%E8%87%AA%E5%8A%A8%E5%8C%96%E5%B7%A5%E4%BD%9C%E6%B5%81%E7%AC%AC4%E5%91%A8%E5%AE%9E%E6%96%BD%E8%AE%A1%E5%88%92.md

## 本次比较使用的代码验证资料

- `auto-video-editor_第三周实施计划_代码验证报告.md`
- `AI视频剪辑自动化工作流第4周代码验证报告.md`
- `AI视频剪辑自动化工作流第2周分阶段实施计划.md`
- `AI视频剪辑自动化流程执行计划`

> 说明：这些内部验证资料均以 2026-09-16 可核对的 `main` 分支静态代码情况为基准，并明确区分了“源码结构存在”和“本次环境实际运行通过”。

---

# 18. 一页版结论

```text
video-agent-kit 0.4.3
        │
        ├─ 5 Skills
        ├─ 37 MCP Tools
        ├─ 通用媒体 Runtime
        ├─ Speech Workflow
        ├─ Recap Workflow
        ├─ Timeline
        ├─ TTS
        ├─ Render
        ├─ QC Evidence
        ├─ Hash / Fingerprint
        └─ Stop Hook

                VS

auto-video-editor
        │
        ├─ LangGraph StateGraph
        ├─ SQLite Checkpointer
        ├─ interrupt / resume
        ├─ 3 个人工关卡
        ├─ 35 秒业务护栏
        ├─ OpenStoryline VLM 分镜
        ├─ 剪映 draft_content.json
        ├─ 剪映加密 / 版本处理
        ├─ 中文主线
        ├─ 英文 Fork
        ├─ 3 比例封面
        ├─ 英文封面本地化
        ├─ 英文字幕
        ├─ 英文 TTS
        └─ Windows Watchdog
```

最终可以把两者关系理解成：

```text
video-agent-kit
= “通用视频执行能力”

auto-video-editor
= “固定的视频生产业务编排”
```

而当前 `auto-video-editor` 还需要继续补齐真实 ASR、节点13、真实剪映 9:16、真实 FireRed Image Edit、超时和联调验收，才能把现有的工作流骨架真正闭环成完整生产能力。

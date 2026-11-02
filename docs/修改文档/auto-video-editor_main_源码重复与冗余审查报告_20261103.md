# auto-video-editor main 分支源码重复与冗余审查报告

> 项目：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 审查基准提交：`1164714f028e1f44c7c96d5cd04ee710cf2a12a9`  
> 审查日期：2026-11-03  
> 审查方式：GitHub 仓库树、文件内容、Git Blob SHA、关键调用链静态审查

---

## 1. 审查结论

当前项目确实存在重复源码和重复提示词，但不是所有相似模块都应该合并。建议按以下优先级处理：

1. **优先合并 OpenStoryline MCP 目录中的 6 组完全重复源码。** 生产代码主要使用 `open_storyline.mcp.*` 路径，建议以 `src/open_storyline/mcp/` 为唯一实现目录，旧路径暂时保留兼容转发。
2. **统一 48 组提示词文件。** `prompts/tasks/` 与 `storyline_capabilities/prompts/` 有 48 组内容完全相同的提示词文件。建议保留仓库根目录的 `prompts/tasks/` 为唯一文件源，保留两套 Loader 的兼容 API 和错误处理语义。
3. **保留已经完成的公共函数收敛。** `nodes/_draft_io.py`、`nodes/_checkpoint.py` 和 `nodes/storyline/_common.py` 已经承担公共逻辑，不应再次复制实现。
4. **不要合并业务节点和不同运行时的能力实现。** 很多模块只是处理相似任务，但数据契约、运行时、外部依赖和错误策略不同。应先抽象接口，再保留各自适配器。
5. **修订过期的架构文档。** 当前 `graph.py` 注册了 53 个节点，早期文档中 21 个、50 个等统计已经过时。

### 1.1 核心数据

| 审查项目 | 当前结果 | 说明 |
|---|---:|---|
| Git Tree 条目 | 918 | 包含文件和目录 |
| Git Blob 文件 | 697 | 不包含目录 |
| 内容 SHA 重复组 | 59 组 | 按 Git Blob SHA 完全相同判断 |
| 重复组涉及文件 | 126 个路径 | 包含提示词、文档、测试快照和空文件 |
| 提示词重复 | 48 组 / 96 个路径 | `prompts/tasks/` 与 `storyline_capabilities/prompts/` |
| OpenStoryline MCP 完全重复源码 | 6 组 / 12 个路径 | 两份源码逐字节相同 |
| `graph.py` 节点注册 | 53 个 | 正则统计 53 处 `g.add_node()` |
| `graph.py` 普通边声明 | 44 处 | `g.add_edge()` 调用数，含 fan-in 边 |
| `graph.py` 条件边声明 | 10 处 | `g.add_conditional_edges()` 调用数 |

以上数量固定对应审查基准提交，不代表后续提交的实时数据。

### 1.2 一个重要版本事实

仓库名称和历史设计中曾出现 `LangGraph + SqliteSaver`，但当前 `main` 分支已经不是 SQLite 持久化方案：

- `README.md` 说明运行时持久化已经统一为 PostgreSQL。
- `pyproject.toml` 声明 `langgraph-checkpoint-postgres` 和 `psycopg`。
- README 明确说明 SQLite checkpointer 和本地 `.sqlite` 落盘已经移除。

因此，本次审查以**当前 PostgreSQL 运行架构**为准，不能按照旧版 SqliteSaver 设计改造。

---

## 2. 审查范围与方法

### 2.1 主要目录

| 目录/文件 | 主要职责 |
|---|---|
| `graph.py`、`state.py` | LangGraph 主图和状态定义 |
| `nodes/` | 主工作流节点、Storyline 适配节点、装配质检节点 |
| `storyline_capabilities/` | Storyline 业务能力的本地化实现 |
| `assembly_capabilities/` | 素材探测、视觉观察、时间线、渲染和 QC |
| `video_edit_capabilities/` | 素材操作、字幕和语音合成等能力 |
| `src/open_storyline/` | OpenStoryline Agent、MCP Server、节点和工具 |
| `jy_common/` | 剪映、ASR、TTS 等共享客户端和工具 |
| `draft_ops/` | 草稿安全写入、版本和校验 |
| `monitoring/`、`db/` | 工作流事件、监控和 PostgreSQL 持久化 |
| `agent_fastapi.py`、`web/` | Web API、聊天、媒体上传和前端 |
| `prompts/` | OpenStoryline 提示词资产 |

### 2.2 判定标准

本报告把重复分成四类：

- **A：完全重复源码**——Git Blob SHA 相同，文件内容完全一致。
- **B：完全重复提示词/文档**——内容相同，但文件可能被不同 Loader 或文档流程引用。
- **C：功能相似或职责重叠**——输入输出、业务目的或算法相近，但接口、运行环境或副作用不同。
- **D：刻意复制/隔离**——文件相似，但源码注释或架构决策明确要求保留独立实现。

只有 A 类在调用链确认后适合直接收敛。B 类需要先统一资源加载路径。C、D 类不能仅凭文件名或函数名称相似就删除。

### 2.3 审查限制

本次通过 GitHub API 读取 `main` 分支的仓库树和源码，没有在本地成功 clone 仓库，因此：

- 没有在本地执行 `pytest`。
- 没有连接真实 PostgreSQL、OpenStoryline MCP Server、FireRed ASR/TTS 或剪映客户端。
- 没有向 GitHub `main` 分支提交源码修改。
- 本报告提供源码核查结论和可执行改造计划；实际合并必须按后续执行文档分阶段实施并运行测试。

---

## 3. A 类：完全重复的 OpenStoryline MCP 源码

### 3.1 6 组重复文件

| 编号 | 当前重复文件 A | 当前重复文件 B | 推荐唯一实现 |
|---|---|---|---|
| M1 | `src/open_storyline/hooks/chat_middleware.py` | `src/open_storyline/mcp/hooks/chat_middleware.py` | `src/open_storyline/mcp/hooks/chat_middleware.py` |
| M2 | `src/open_storyline/hooks/node_interceptors.py` | `src/open_storyline/mcp/hooks/node_interceptors.py` | `src/open_storyline/mcp/hooks/node_interceptors.py` |
| M3 | `src/open_storyline/register_tools.py` | `src/open_storyline/mcp/register_tools.py` | `src/open_storyline/mcp/register_tools.py` |
| M4 | `src/open_storyline/sampling_handler.py` | `src/open_storyline/mcp/sampling_handler.py` | `src/open_storyline/mcp/sampling_handler.py` |
| M5 | `src/open_storyline/sampling_requester.py` | `src/open_storyline/mcp/sampling_requester.py` | `src/open_storyline/mcp/sampling_requester.py` |
| M6 | `src/open_storyline/server.py` | `src/open_storyline/mcp/server.py` | `src/open_storyline/mcp/server.py` |

这 6 组文件的 Git Blob SHA 相同，因此属于**完全相同的源码副本**，不是根据名称推测出来的重复。

### 3.2 为什么选择 `src/open_storyline/mcp/` 作为唯一实现

当前生产调用路径已经倾向于嵌套 MCP 目录：

- `src/open_storyline/agent.py` 从 `open_storyline.mcp.hooks.chat_middleware` 导入中间件，并从 `open_storyline.mcp.sampling_handler` 导入 Sampling 回调。
- `agent_fastapi.py` 使用 `open_storyline.mcp.hooks.node_interceptors` 和 `open_storyline.mcp.hooks.chat_middleware`。
- `src/open_storyline/server.py` 通过 `open_storyline.mcp` 导入 `register_tools`。
- `docs/integration/architecture_decision_record.md` 的 ADR-001 明确给出了 `python -m open_storyline.mcp.server` 子进程启动路径。

因此，MCP 相关实现统一放在 `src/open_storyline/mcp/` 更符合当前导入和进程启动路径。

### 3.3 推荐处理方式

第一阶段不要直接删除旧路径。把 `src/open_storyline/` 下的旧文件改成兼容转发层：

- `src/open_storyline/hooks/*.py` 转发到 `src/open_storyline/mcp/hooks/*.py`。
- `src/open_storyline/register_tools.py` 转发到 `src/open_storyline/mcp/register_tools.py`。
- `src/open_storyline/sampling_handler.py` 转发到 `src/open_storyline/mcp/sampling_handler.py`。
- `src/open_storyline/sampling_requester.py` 转发到 `src/open_storyline/mcp/sampling_requester.py`。
- `src/open_storyline/server.py` 转发到 `src/open_storyline/mcp/server.py`，并保留直接执行入口。

兼容转发层应显式导出公开符号，不建议无条件依赖 `import *`。完成引用扫描和测试后，再决定是否删除旧路径。

---

## 4. B 类：提示词文件重复

### 4.1 当前目录

```text
prompts/
└── tasks/
    ├── elementrec_text/
    ├── filter_clips/
    ├── generate_ai_transition/
    ├── generate_script/
    ├── generate_title/
    ├── generate_voiceover/
    ├── group_clips/
    ├── instruction/
    ├── scripts/
    ├── select_bgm/
    ├── speech_rough_cut/
    └── understand_clips/

storyline_capabilities/
└── prompts/
    ├── elementrec_text/
    ├── filter_clips/
    ├── generate_ai_transition/
    ├── generate_script/
    ├── generate_title/
    ├── generate_voiceover/
    ├── group_clips/
    ├── instruction/
    ├── scripts/
    ├── select_bgm/
    ├── speech_rough_cut/
    └── understand_clips/
```

Git Blob SHA 比较结果：48 组提示词文件在两套目录下内容完全一致，共 96 个路径。

### 4.2 两套 Loader 的行为并不完全相同

| 能力 | `src/open_storyline/utils/prompts.py` | `storyline_capabilities/prompts/__init__.py` |
|---|---|---|
| 当前资源根目录 | `prompts/tasks/` | `storyline_capabilities/prompts/` |
| 文件不存在 | 抛 `FileNotFoundError` | 返回空字符串 |
| 变量格式 | `{{name}}` | `{{name}}`、`{name}`、`{name:?}` |
| 变量缺失 | 替换时会触发 KeyError | 保留未解析占位符 |
| 缓存 | 有模板缓存 | 直接读取文件 |
| API | `get_prompt()`、`build_prompts()` | `load_prompt()`、`render_prompt()`、`unresolved_placeholders()` |

所以不能只删除 `storyline_capabilities/prompts/` 文件，而不调整 Loader。否则会改变异常处理、占位符解析和默认降级行为。

### 4.3 推荐合并方式

- **唯一提示词文件源：** 仓库根目录 `prompts/tasks/`。
- **统一底层：** 增加一个共享 Prompt Loader，负责路径解析、读取、缓存和模板解析。
- **保留两套兼容 API：** OpenStoryline 与 Storyline Capability 通过适配层调用共享 Loader。
- **保留原有策略差异：** Loader 提供 `strict_missing_file`、`strict_variables`、`placeholder_syntax` 等策略，逐步迁移，不强迫所有调用方一次性采用相同行为。
- 经过测试后删除 `storyline_capabilities/prompts/` 下重复的 Markdown 文件，只留下 Python 兼容入口。

---

## 5. C 类：职责相近，但不应直接合并的模块

### 5.1 ASR（语音识别）相关实现

| 模块 | 当前职责 | 差异 |
|---|---|---|
| `jy_common/asr_client.py` | Mock 和 FireRed ASR HTTP 客户端 | 输入视频路径，输出字幕段列表 |
| `storyline_capabilities/asr_runner.py` | Storyline ASR Protocol、Stub、独立环境子进程调用 | 输入媒体和输出目录，返回 artifact 路径及 segments |
| `assembly_capabilities/speech_asr.py` | Assembly QC/素材流水线中的 ASR 能力 | 由 `RunContext` 和 ToolResult 体系管理 |
| `src/open_storyline/nodes/core_nodes/asr_node.py` | OpenStoryline MCP Node | 依赖 NodeState、MCP 会话和 Agent 运行环境 |

这些模块存在能力重叠，但调用协议、产物形式和运行环境不同。建议后续统一 Provider Contract，再保留 HTTP、子进程、MCP 三类适配器；不能直接把四个模块合成一个文件。

### 5.2 TTS（语音合成）相关实现

| 模块 | 当前职责 | 差异 |
|---|---|---|
| `jy_common/tts_client.py` | Mock 与 FireRed TTS2 客户端 | 面向 HTTP 服务，返回音频路径和时长 |
| `storyline_capabilities/tts_runner.py` | TTS Protocol、Stub WAV、供应商配置 | 面向 Storyline 能力层，调用方传入 WAV 产物路径 |
| `video_edit_capabilities/speech_synthesize.py` | 视频编辑工具的 `speech_synthesize` 入口 | 通过 `jy_common.tts_client` 实际调用 FireRed |
| `src/open_storyline/nodes/core_nodes/generate_voiceover.py` | OpenStoryline Agent 节点 | 依赖 NodeState 与 Agent LLM/MCP 运行上下文 |

建议把 TTS 服务调用和结果契约标准化，`video_edit_capabilities`、Storyline 和 OpenStoryline 保留各自适配层。

### 5.3 FFmpeg 与字体工具

`assembly_capabilities/ffproc.py` 与 `video_edit_capabilities/ffproc.py`、两个 `fonts.py` 的实现高度相似，但源码注释明确说明两套工具是从不同工具集移植，并且有意保留独立演进边界：

- `video_edit_capabilities/ffproc.py` 的注释说明其与 `assembly_capabilities.ffproc` 独立演进。
- `video_edit_capabilities/fonts.py` 保留历史 Linux 字体目录搜索顺序，以匹配字幕工具的行为。
- `assembly_capabilities` 与 `video_edit_capabilities` 提供的工具能力不同，虽共享 `ToolResult`、`RunContext`，但不是同一套业务工具。

**本次不合并这两套 `ffproc.py` 和 `fonts.py`。** 未来如要合并，应先通过跨平台行为测试，再抽公共底层和差异策略，不能直接用一个文件覆盖另一个。

### 5.4 OpenStoryline Core Node 与 LangGraph Storyline Capability

例如：

- `src/open_storyline/nodes/core_nodes/understand_clips.py`
- `storyline_capabilities/understand_clips.py`
- `nodes/storyline/node_understand_clips.py`

三者分别承担：

1. OpenStoryline MCP Node/Agent 运行时。
2. 可注入客户端的本地业务能力函数。
3. LangGraph State 输入输出、文件产物和日志适配。

它们是分层实现，不是三份重复功能。正确做法是保持业务算法尽量可复用，保留运行时适配器。

### 5.5 工作流监控和 Web API

以下文件职责不同，不应合并：

| 模块 | 单一职责 |
|---|---|
| `monitoring/workflow_runner.py` | 消费 LangGraph stream，生成并发布工作流事件 |
| `monitoring/workflow_events.py` | 事件类型、构造、校验、脱敏和 SSE 帧格式 |
| `monitoring/workflow_event_store.py` | PostgreSQL 事件持久化和游标查询 |
| `monitoring/runs_table.py` | 工作流运行台账/最新状态 |
| `monitoring/workflow_graph.py` | 从已编译 LangGraph 图提取拓扑 Schema |
| `web/api_workflow.py` | 工作流 API、运行快照和 SSE 接口 |
| `web/static/workflow_monitor.mjs` | 前端事件状态归约、节点状态和布局逻辑 |

这些模块构成分层链路，不是重复代码。把它们合并只会增加循环依赖和测试成本。

---

## 6. 已经完成的公共逻辑收敛

当前源码已经包含以下公共 helper，不应重复实现：

### 6.1 草稿读写：`nodes/_draft_io.py`

已经统一：

- `load_draft()`：读取 `draft_content.json`。
- `write_draft()`：统一调用 `safe_write_draft()`，并传入 `draft_path.parent`，保证目录级双写。
- `jianying_running_tags()`：统一生成剪映进程运行告警。
- `apply_and_write()`：统一节点写回和完成状态标签。

特别注意：`safe_write_draft()` 的参数是草稿目录，不是 `draft_content.json` 文件路径。该区别影响 `draft_info.json` 的同步生成。

### 6.2 人工关卡：`nodes/_checkpoint.py`

已经统一：

- `build_interrupt_payload()`。
- `post_resume()`。

这里有意没有用会改变节点名或单测 monkeypatch 路径的匿名闭包工厂。关卡 ⓪、①、② 仍然由各自 LangGraph 节点调用 `interrupt()`；关卡③还有额外 SRT 重写步骤，不应强行套用同一模板。

### 6.3 Storyline 公共函数：`nodes/storyline/_common.py`

已经统一：

- `_read_json_artifact()`。
- `_read_groups()`。
- `_read_media()`。
- `resolve_video_input_paths()`。
- `append_status_tag()`、`append_error()` 等。

此前 `node_plan_timeline_pro.py` 与 `node_plan_timeline_ai_transition.py` 内重复的产物读盘函数已移入 `_common.py`。

---

## 7. 当前 LangGraph 节点和旧文档差异

### 7.1 当前节点数量

当前 `graph.py` 有 53 个 `g.add_node()`：

- 基础入口、人工关卡、草稿和双语视频处理节点。
- 6 个 Assembly QC 节点。
- 21 个 Storyline 节点（自动模式和 Human Fusion 使用）。
- 编排桥接、分支、升级和汇合节点。

`graph.py` 当前有 44 个 `g.add_edge()` 调用、10 个 `g.add_conditional_edges()` 调用。条件边和 fan-in 边不能简单按文本行数解释为实际执行次数。

### 7.2 不要将“注册”误判为“可达”

- `clean_cache` 注册在图中，但有意保持零入边、零出边，由 Web 的 `/api/system/clean-cache` 显式调用。
- Assembly QC 的 6 个节点是否连通受 `ASSEMBLY_QC_GATE_ENABLED` 控制。
- Storyline 节点取决于 human/auto 模式、`STORYLINE_HUMAN_FUSION` 和 AI 转场开关。
- 当前 `monitoring/workflow_graph.py` 从编译图的 builder edges、waiting_edges 和 branches 生成拓扑，并计算 `reachable`；应以它作为可达性检查入口，而不是单纯 grep `add_node()`。

### 7.3 旧文档需修订

以下历史设计文档中的节点数量和拓扑描述不能直接作为当前基线：

- `docs/integration/auto-video-editor_节点冗余合并设计与执行计划.md`
- `docs/integration/auto-video-editor节点全量核查与冗余合并设计执行计划.md`
- 其他以 21、22 或 50 个节点为统计基准的早期文档

新文档应注明提交 SHA 和统计方法，防止未来再次把旧版本结果当成当前源码事实。

---

## 8. 最终分类和处理建议

| 编号 | 重复/重叠项 | 类型 | 优先级 | 处理建议 |
|---|---|---|---|---|
| M1-M6 | OpenStoryline MCP 6 组完全相同源码 | A | P0 | 嵌套 `mcp/` 保留唯一实现，旧路径兼容转发 |
| P1 | 48 组提示词文件 | B | P0 | `prompts/tasks/` 作为唯一资源目录，保留两种 Loader API |
| H1 | `nodes/_draft_io.py` | 已收敛 | — | 保留，不重新实现 |
| H2 | `nodes/_checkpoint.py` | 已收敛 | — | 保留，不重新实现 |
| H3 | `nodes/storyline/_common.py` | 已收敛 | — | 保留，不重新实现 |
| A1 | ASR 多种客户端/执行器 | C | P2 | 统一协议，不合并运行时适配器 |
| T1 | TTS 多种客户端/执行器 | C | P2 | 统一结果契约，保留 Provider 适配器 |
| F1 | `ffproc.py`、`fonts.py` 两套实现 | D | P3 | 本次不合并，先做行为一致性验证 |
| N1 | OpenStoryline Core Node / Storyline Capability / LangGraph Node | C | P3 | 保持分层，按接口复用能力 |
| W1 | Workflow Runner / Event Store / API / UI | 非重复 | — | 保持分层 |
| D1 | 两份同内容工作流设计文档 | B | P2 | 指定主文档，另一份改为索引或历史说明 |
| D2 | docs 内代码快照和测试夹具副本 | D | — | 保留，不进入生产代码去重范围 |

---

## 9. 参考源码

以下链接指向审查时的 `main` 分支。准确版本以本文开头的 commit SHA 为准。

- [README.md](https://github.com/wayyet/auto-video-editor/blob/main/README.md)
- [pyproject.toml](https://github.com/wayyet/auto-video-editor/blob/main/pyproject.toml)
- [graph.py](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)
- [nodes/_draft_io.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/_draft_io.py)
- [nodes/_checkpoint.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/_checkpoint.py)
- [nodes/storyline/_common.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/_common.py)
- [src/open_storyline/agent.py](https://github.com/wayyet/auto-video-editor/blob/main/src/open_storyline/agent.py)
- [src/open_storyline/mcp/server.py](https://github.com/wayyet/auto-video-editor/blob/main/src/open_storyline/mcp/server.py)
- [src/open_storyline/utils/prompts.py](https://github.com/wayyet/auto-video-editor/blob/main/src/open_storyline/utils/prompts.py)
- [storyline_capabilities/prompts/__init__.py](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/prompts/__init__.py)
- [architecture_decision_record.md](https://github.com/wayyet/auto-video-editor/blob/main/docs/integration/architecture_decision_record.md)
- [monitoring/workflow_runner.py](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_runner.py)
- [monitoring/workflow_events.py](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_events.py)
- [monitoring/workflow_event_store.py](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_event_store.py)
- [monitoring/workflow_graph.py](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_graph.py)
- [web/api_workflow.py](https://github.com/wayyet/auto-video-editor/blob/main/web/api_workflow.py)

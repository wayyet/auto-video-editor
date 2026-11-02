# auto-video-editor 节点冗余分析与合并设计、执行计划

> **本次修订：2026-11-03**  
> 仓库：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 本次审查基准：`a27298a11deb5b597474ac6b317edf7db6a12c0f`  
> 上次审查基准：`1164714f028e1f44c7c96d5cd04ee710cf2a12a9`  
> 本次比上次基准新增 3 个提交。  
> **目标文档路径：** `docs/integration/auto-video-editor_节点冗余合并设计与执行计划.md`

---

## 0. 本次修订结论

本次重新读取 GitHub `main` 的分支头、提交记录、递归文件树和关键源码。结论如下：

1. 当前 `main` 的图节点注册数为 **53**，不是旧文档中的 50 或更早版本的 22。图连线统计为 44 处普通 `add_edge()` 调用、10 处 `add_conditional_edges()` 调用。
2. 当前树有 926 个条目，705 个唯一 Blob；按 Blob SHA 统计，存在 59 组重复内容、涉及 126 个路径。不能把这 59 组全部当成应删除的业务重复。
3. 本次确认的可安全收敛对象：
   - **6 组 MCP Python 源码逐字重复**，旧路径与 `open_storyline/mcp/` 下的规范实现 SHA 完全相同。
   - **48 对提示词 Markdown 文件逐字重复**，路径一一对应、SHA 完全相同，可共用一份资源。
4. 新增的聊天会话—工作流关联和对话框节点指示器并未重复实现工作流状态机。`workflow_chat_indicator.mjs` 直接复用 `workflow_monitor.mjs` 的 `createMonitorState()`、`applyEvent()` 和 `activeNodeIds()`，不应合并成另一个状态机。
5. 不建议合并 `workflow_chat_link_store.py`、`workflow_event_store.py`、`runs_table.py` 等持久化模块。它们的数据粒度、写入语义和失败处理不同。
6. **本次未直接改写 GitHub `main`。** 已尝试创建重构分支和更新文件，但 GitHub 连接返回 403 `Resource not accessible by integration`。因此，本交付包含可运行的源码重构脚本和修订后的 MD 设计文档；需要在有仓库写权限的本地工作区执行并提交。没有把未执行或未推送的变更描述为已合并。

## 1. 仓库更新情况

### 1.1 最新提交

| 项目 | 值 |
|---|---|
| 分支 | `main` |
| 最新提交 | `a27298a11deb5b597474ac6b317edf7db6a12c0f` |
| 提交时间 | `2026-11-02T22:07:35Z`（新加坡时间 2026-11-03） |
| 提交说明 | 对话框显示 LangGraph 当前运行节点：阶段 6 回归验收收尾 |
| 上次基准 | `1164714f028e1f44c7c96d5cd04ee710cf2a12a9` |
| 比上次基准 | ahead 3 commits，behind 0 |

最近 3 个提交依次补齐：

- `f2187d8`：增加 `chat_session_id`、`workflow_chat_links` 表、统一 runner 关联维护和会话关联查询接口。
- `db9580f`：新增 `workflow_chat_indicator.mjs`，将节点状态显示组件挂接到对话框。
- `a27298a`：更新执行设计和验收记录。

### 1.2 最近提交变更范围

本次相对上次审查基准涉及 17 个文件，主要包括：

- 数据库迁移：`db/migrations.py`
- 会话关联：`monitoring/workflow_chat_link_store.py`、`monitoring/workflow_runner.py`
- API：`web/api_workflow.py`
- 状态字段：`state.py`
- CLI：`scripts/run_workflow.py`
- 前端：`web/static/workflow_chat_indicator.mjs`、`web/static/app.js`、`web/static/style.css`、`web/index.html`
- 测试：会话关联存储、API、runner、集成测试及前端测试。

这批新增代码需要纳入本次冗余审查，不能沿用旧版目录或节点统计。

### 1.3 当前图规模

按最新 `graph.py` 统计：

| 指标 | 数量 |
|---|---:|
| `g.add_node()` 注册调用 | **53** |
| `g.add_edge()` 普通边调用 | 44 |
| `g.add_conditional_edges()` 条件边调用 | 10 |

`add_node()` 调用数量是源码注册点统计，不等于所有配置下的可达节点数。部分节点受模式或开关控制，也有通过 Web API 显式调用的节点。判断可达性仍应使用编译图 Schema，并分别验证配置组合。

## 2. 重复内容统计

统计依据：最新 `main` 的递归 Git Tree，以 Blob SHA 判断文件内容是否完全相同。

| 指标 | 数量 |
|---|---:|
| Tree 条目（文件和目录） | 926 |
| 唯一 Blob | 705 |
| 重复内容组 | 59 |
| 涉及重复内容的路径 | 126 |
| 完全重复的提示词文件对 | **48 对** |
| 完全重复的 MCP 源码对 | **6 对** |

### 2.1 本次需要收敛的 MCP 源码

以下每一组的两个文件 SHA 完全相同：

| 旧路径（改为兼容入口） | 唯一实现路径 |
|---|---|
| `src/open_storyline/hooks/chat_middleware.py` | `src/open_storyline/mcp/hooks/chat_middleware.py` |
| `src/open_storyline/hooks/node_interceptors.py` | `src/open_storyline/mcp/hooks/node_interceptors.py` |
| `src/open_storyline/register_tools.py` | `src/open_storyline/mcp/register_tools.py` |
| `src/open_storyline/sampling_handler.py` | `src/open_storyline/mcp/sampling_handler.py` |
| `src/open_storyline/sampling_requester.py` | `src/open_storyline/mcp/sampling_requester.py` |
| `src/open_storyline/server.py` | `src/open_storyline/mcp/server.py` |

这 6 组旧副本合计约 67,387 字节。生产入口目前已经优先引用规范路径：

- `src/open_storyline/agent.py` 从 `open_storyline.mcp.hooks.*`、`open_storyline.mcp.sampling_handler` 导入。
- 根目录 `agent_fastapi.py` 从 `open_storyline.mcp.hooks.*` 导入。
- `src/open_storyline/mcp/server.py` 从 `open_storyline.mcp` 注册工具。
- ADR-001 指定的 MCP Server 启动路径为 `python -m open_storyline.mcp.server`。

**合并方式：**保留 `src/open_storyline/mcp/` 为唯一实现。旧路径不直接删除，而是替换为很薄的兼容模块别名。这样历史 import、外部插件或测试仍能解析旧路径，同时避免两份源码继续分叉。

兼容层采用 `sys.modules[__name__] = _implementation`，而不是简单的 `from module import *`。后者会漏掉以下划线开头的符号，并可能使 monkeypatch、模块级状态分别落在两个模块对象中。

`server.py` 需要特殊处理：旧的 `python -m open_storyline.server` 入口仍调用规范模块的 `main()`；普通 import 则映射到规范模块对象。

### 2.2 本次需要收敛的提示词文件

以下目录有 48 个 `.md` 文件，文件名和相对目录完全一致，且与 `prompts/tasks/` 下的对应文件 SHA 完全相同：

- 当前规范目录：`prompts/tasks/<kind>/<lang>/<part>.md`
- 重复目录：`storyline_capabilities/prompts/<kind>/<lang>/<part>.md`

两边各 48 个文件，各自总大小均为 67,930 字节。推荐只保留 `prompts/tasks/` 一份物理资源。

合并方式：

1. `storyline_capabilities/prompts/__init__.py` 的 `PROMPTS_ROOT` 改为仓库根 `prompts/tasks/`。
2. 保留 `load_prompt()`、`render_prompt()`、`unresolved_placeholders()` 的公开 API。
3. 删除 `storyline_capabilities/prompts/` 下 48 份重复 Markdown 文件。
4. 保留 `storyline_capabilities/prompts/__init__.py`，它是 Loader，不是重复资源。

#### 两套 Loader 不合并

两个 Loader 的行为不是完全相同，不能把 Python 实现也粗暴地合并：

| 行为 | `open_storyline.utils.prompts` | `storyline_capabilities.prompts` |
|---|---|---|
| 默认资源根 | `prompts/tasks/` | 修订后也指向 `prompts/tasks/` |
| API | `get_prompt()`、`build_prompts()` | `load_prompt()`、`render_prompt()`、`unresolved_placeholders()` |
| 文件缺失 | 抛 `FileNotFoundError` | 返回空字符串 |
| 占位符 | `{{name}}`，缺变量按严格策略处理 | `{{name}}`、`{name}`、`{name:?}`；缺变量保留原样 |
| 缓存 | `PromptBuilder` 有缓存 | 当前 Loader 不采用相同缓存策略 |

因此本次只合并**物理模板资源**，保留两套 Loader facade 的调用契约和错误策略。

## 3. 新增会话关联与节点指示器代码审查

这部分是上次审查之后新增的代码。本次逐层检查后，结论是职责互补，不应为了减少文件数而合并。

| 模块 | 当前职责 | 是否合并 | 原因 |
|---|---|---|---|
| `monitoring/workflow_chat_link_store.py` | 以 `chat_session_id` 为键，保存会话当前关联的 `thread_id`、`run_id` 和状态 | 保留 | 解决聊天会话 ID 与 LangGraph thread ID 的关联问题；每会话单行、最近运行覆盖语义 |
| `monitoring/runs_table.py` | 保存工作流运行台账/最新状态 | 保留 | 运行台账与会话关联关系不是同一实体 |
| `monitoring/workflow_event_store.py` | PostgreSQL 中追加式保存工作流事件，支持 Last-Event-ID 补发和历史回放 | 保留 | 事件序列是 append-only，不是会话关系表或运行台账 |
| `monitoring/workflow_events.py` | 工作流事件类型及进程内事件分发 | 保留 | 事件模型/广播机制与持久化存储职责不同 |
| `monitoring/workflow_runner.py` | 运行 LangGraph，并维护事件和 chat link 生命周期 | 保留 | 编排层，不是存储层 |
| `monitoring/workflow_graph.py` | 从编译图提取拓扑 Schema | 保留 | 图结构元数据，与运行状态无关 |
| `web/static/workflow_monitor.mjs` | 前端纯逻辑状态归约、事件去重和节点状态管理 | 保留 | 对话组件依赖的共享 reducer |
| `web/static/workflow_chat_indicator.mjs` | 查询 chat link、获取 Schema、订阅 SSE、渲染对话区节点指示器 | 保留 | 新 UI 组件复用现有 reducer，并未重新创建一套状态机 |

关键证据：`workflow_chat_indicator.mjs` 从 `workflow_monitor.mjs` 导入并使用：

- `API_PATHS`
- `activeNodeIds`
- `applyEvent`
- `createMonitorState`

所以应维持当前“共享 reducer + 独立 DOM 生命周期组件”的设计。把两者合成一个大文件会混合状态归约与 DOM/fetch/EventSource 副作用，不是有效去重。

`workflow_chat_link_store.py` 的写入失败采用“记 ERROR 并降级，不阻塞工作流”；查询失败则抛错，由 API 转成 503，不能伪装为“无关联”。事件存储的错误语义不同。即使它们有相似的 PostgreSQL 连接样板，也不应在这次改造中把两种失败契约强制统一。

## 4. 其他重复项与保留项

以下项目也有相同内容或相似结构，但当前不纳入自动删除。

| 项目 | 发现 | 处理建议 |
|---|---|---|
| `agent_gate/fixtures/storyline_capabilities/vlm_client.py` 与 `storyline_capabilities/vlm_client.py` | 文件内容完全相同 | 保留 fixture 副本。测试夹具可能需要独立、可控的模块快照；应先核对 agent_gate 的隔离加载方式，不能直接把 fixture import 指向生产文件 |
| `docs/分析/auto-video-editor节点调序-代码与脚本包/01_代码改动/nodes/_subtitle_texts.py` 与 `nodes/_subtitle_texts.py` | 文档交付包里的代码快照与生产源码相同 | 保留。它是历史交付包内容，不是 Python 运行时双份模块 |
| `docs/分析/auto-video-editor节点调序-代码与脚本包/01_代码改动/nodes/node_08b_add_subtitles_en.py` 与生产路径同名文件 | 文档交付包代码快照 | 保留。不要把交付包快照当作生产副本删除 |
| `docs/修改文档/auto-video-editor-实时工作流流程图功能设计与开发计划.md` 与 `docs/分析/` 下同名文档 | 文档内容 SHA 完全相同 | 可在后续文档治理时选择一个 canonical 文档并保留链接/归档说明；不属于源码模块合并 |
| 多个空 `__init__.py` | 内容为空导致 SHA 相同 | 保留。空包初始化文件不构成业务逻辑冗余 |

### 4.1 不合并的功能相似模块

- ASR：`jy_common/asr_client.py`、`storyline_capabilities/asr_runner.py`、`assembly_capabilities/speech_asr.py` 和 OpenStoryline 相关实现，协议、输入输出和运行时环境不同。未来可统一 Provider Protocol，不应直接删除 adapter。
- TTS：`jy_common/tts_client.py`、`storyline_capabilities/tts_runner.py`、`video_edit_capabilities/speech_synthesize.py` 和 OpenStoryline core 实现，调用协议与产物结构不同。只考虑抽象接口，不合并实现。
- FFmpeg / 字体：`assembly_capabilities/ffproc.py`、`video_edit_capabilities/ffproc.py` 以及不同 `fonts.py` 实现有平台搜索和演进边界，不强行共用。
- LangGraph 节点与 capability 函数：节点负责 State Adapter、interrupt 和状态更新；capability 负责本地业务计算。即便功能名称相似，也不是同一层。
- `node_09_inject_fx.py`、`node_10_inject_text_fx.py`、`node_11_inject_sticker.py` 操作不同的草稿字段，不能合并成一个节点。
- `node_06_human_reorder.py`、`node_12_human_add_bgm.py` 和其他 checkpoint 节点位于不同流程阶段，不能合并成一个会挂起多次的图节点。

## 5. 合并后的目标结构

```text
src/open_storyline/
├── mcp/                              # MCP 功能唯一实现
│   ├── hooks/
│   │   ├── chat_middleware.py
│   │   └── node_interceptors.py
│   ├── register_tools.py
│   ├── sampling_handler.py
│   ├── sampling_requester.py
│   └── server.py
├── hooks/                            # 旧路径兼容入口，不再保存实现
│   ├── chat_middleware.py            # sys.modules 别名
│   └── node_interceptors.py          # sys.modules 别名
├── register_tools.py                 # 旧路径兼容入口
├── sampling_handler.py               # 旧路径兼容入口
├── sampling_requester.py              # 旧路径兼容入口
└── server.py                         # 旧 import + `python -m` 入口兼容

prompts/
└── tasks/                            # 唯一物理提示词资源目录（48 个文件）

storyline_capabilities/
└── prompts/
    └── __init__.py                   # 保留原 Loader API，根目录指向 prompts/tasks
```

**图拓扑不变：**本计划不修改 `graph.py`、53 个节点、任何 `add_edge()` 或 `add_conditional_edges()` 调用。

## 6. 源码实施脚本

随本文档交付的脚本：`refactor_dedupe_openstoryline_mcp.py`。

该脚本将：

1. 校验 6 组 MCP 旧路径和规范路径当前仍逐字相同；只在相同的情况下改写旧路径。
2. 将旧 MCP 文件替换为兼容别名；唯一实现保留在 `src/open_storyline/mcp/`。
3. 校验两套提示词的相对路径集合完全相同、数量为 48、每个文件字节完全相同。
4. 更新 `storyline_capabilities/prompts/__init__.py` 的文档说明和 `PROMPTS_ROOT`。
5. 删除 48 份重复 Markdown 文件，但保留 Loader 文件。
6. 新增两个测试文件，分别验证旧 MCP import 的模块对象身份和共享提示词资源路径。
7. **不自动提交 Git，不运行生产服务，不修改图拓扑和数据库。**

### 6.1 应用步骤（Windows PowerShell）

先把下载的脚本复制到仓库的 `scripts/` 目录：

```powershell
Copy-Item .\refactor_dedupe_openstoryline_mcp.py .\scripts\refactor_dedupe_openstoryline_mcp.py
```

检查工作区状态，创建独立分支：

```powershell
git status --short
git switch -c refactor/dedupe-openstoryline-mcp-20261103
```

先运行预检，不修改文件：

```powershell
python .\scripts\refactor_dedupe_openstoryline_mcp.py
```

确认输出显示 6 组 MCP 和 48 份提示词预检通过后，再执行：

```powershell
python .\scripts\refactor_dedupe_openstoryline_mcp.py --apply
```

检查变更：

```powershell
git status --short
git diff --stat
git diff -- src/open_storyline storyline_capabilities/prompts tests_openstoryline tests/unit
```

脚本对 MCP 文件和提示词文件做了内容一致性检查。如果文件已被后续提交修改、提示词数量不再是 48、路径集合不一致或目标测试文件已存在，脚本会停止，不会按旧假设盲目删除。

### 6.2 新增文件

脚本会创建：

- `tests_openstoryline/test_mcp_legacy_compat.py`
- `tests/unit/test_shared_storyline_prompt_resources.py`

第一个测试保证 6 个旧路径和规范路径 `import` 后得到同一个模块对象。第二个测试保证 Storyline Loader 仍从 `prompts/tasks/` 加载资源，且旧目录不再保存重复 Markdown。

## 7. 回归测试和验收标准

### 7.1 专项测试

```powershell
python -m pytest tests_openstoryline/test_mcp_legacy_compat.py tests/unit/test_shared_storyline_prompt_resources.py tests/unit/test_storyline_capabilities_b_classes.py -q
```

### 7.2 MCP 相关测试

```powershell
python -m pytest tests_openstoryline/test_agent_mcode_branch.py tests_openstoryline/test_zcode_agent_branch.py -q
```

### 7.3 前端状态归约和指示器

```powershell
node --test web/tests/workflow_monitor.test.mjs web/tests/workflow_chat_indicator.test.mjs
```

### 7.4 完整回归

```powershell
python -m pytest tests/unit tests/integration tests_openstoryline -q
```

完整回归的结果应与当前基线对照，不能把已有失败误判成本次引入。最新提交说明记录：`tests/unit` 有 21 个失败，经 stash 基线对照确认为存量失败、无新增；特性相关 53 项、集成 10 项、wiring 29 项和前端 Node 77 项通过。这是仓库提交说明中的作者验收记录，不是本次环境独立执行的测试结果。

### 7.5 验收清单

- [ ] 6 个规范 MCP 实现只保留在 `src/open_storyline/mcp/`。
- [ ] 6 个旧路径均可 import，且与规范模块是同一模块对象。
- [ ] 旧的 `python -m open_storyline.server` 入口仍调用规范 `server.main()`。
- [ ] `storyline_capabilities/prompts/` 下不再有重复 Markdown。
- [ ] `prompts/tasks/` 的 48 个 Markdown 文件完整保留。
- [ ] 两套 Prompt Loader 的缺失文件、占位符、缓存等策略未被合并或改变。
- [ ] 专项测试、MCP 测试和前端测试通过。
- [ ] `graph.py` diff 为空，53 个节点注册数及边调用数不变。
- [ ] 数据库迁移、工作流事件和会话关联行为没有变化。

## 8. 风险和回滚

| 风险 | 控制措施 |
|---|---|
| 外部脚本仍从旧 MCP 路径导入 | 旧路径保留为兼容模块别名；新增模块身份测试 |
| `python -m open_storyline.server` 旧启动方式失效 | 旧 server shim 在 `__main__` 场景显式调用规范 `main()` |
| Prompt Loader 仍访问旧目录 | 先把其根目录切到 `prompts/tasks/`，再删重复副本；新增路径和加载测试 |
| 两种 Loader 渲染行为被误统一 | 本次只合并资源文件，不合并 Loader 实现 |
| 仓库文件已进一步更新，当前脚本前提不成立 | 脚本按字节、路径和数量预检；不匹配即停止 |

回滚建议按一个独立提交执行：

```powershell
git diff --check
git status --short
# 若重构已经提交：
git revert <dedupe-commit-sha>
```

如果只出现 MCP 导入回归，可以先回滚 MCP 兼容路径部分；如果只出现 Prompt 资源回归，恢复 48 份提示词并将 Loader 根目录还原。不要通过改图边或数据库迁移来规避本次问题。

## 9. 本次尚未执行的事项

- GitHub 连接对创建分支和 `update_file` 均返回 403，因此没有把任何源码提交到 GitHub。
- 当前执行环境未成功建立本地完整 clone，也没有在项目虚拟环境中运行 pytest 或启动服务。
- 交付脚本已通过 Python 语法编译，并在临时模拟仓库中验证了 dry-run、apply、兼容层生成、48 份资源迁移和新增测试文件生成；这不等于目标项目测试通过。
- 请在实际仓库分支应用脚本后，按第 7 节运行测试，再提交代码和更新后的本文档。

## 10. 参考源码

- [最新 main 提交](https://github.com/wayyet/auto-video-editor/commit/a27298a11deb5b597474ac6b317edf7db6a12c0f)
- [MCP 规范目录](https://github.com/wayyet/auto-video-editor/tree/main/src/open_storyline/mcp)
- [Storyline Prompt Loader](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/prompts/__init__.py)
- [OpenStoryline Prompt Loader](https://github.com/wayyet/auto-video-editor/blob/main/src/open_storyline/utils/prompts.py)
- [聊天节点指示器](https://github.com/wayyet/auto-video-editor/blob/main/web/static/workflow_chat_indicator.mjs)
- [工作流监控 reducer](https://github.com/wayyet/auto-video-editor/blob/main/web/static/workflow_monitor.mjs)
- [会话—工作流关联存储](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_chat_link_store.py)

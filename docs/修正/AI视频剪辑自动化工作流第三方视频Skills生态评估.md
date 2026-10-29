# auto-video-editor 数据库数量与职责分析

> 项目：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 核查日期：2026-10-26  
> 关联背景：历史对话“重设计对话历史模块”，参考文档《auto-video-editor_对话历史故障分析与重设计执行方案.md》

## 1. 结论摘要

**当前 `main` 分支还没有实现“聊天历史 SQLite 数据库”。** 当前代码的存储结构是：

1. **LangGraph 工作流检查点（Checkpoint）**：使用 SQLite 或 PostgreSQL 保存工作流执行状态。两者是可选后端，不代表每次运行都会同时使用两个后端。
2. **OpenStoryline 聊天会话**：服务端将会话状态保存到 `session_state.json`，不是数据库。
3. **侧栏“对话历史”列表**：前端使用浏览器 `localStorage` 保存，不是数据库，而且不同浏览器或不同站点来源之间不共享。
4. **PostgreSQL 运行台账表**：代码还定义了 `workflow_runs` 表，存放工作流运行状态。它使用 `POSTGRES_URI` 指向的 PostgreSQL 数据库，不是另一个独立数据库实例。

因此，必须区分两种统计口径：

- **按工作流检查点的数据库后端统计**：SQLite、PostgreSQL 两种可选方案，运行时通常选择其中一种。
- **按当前已实现的持久化职责统计**：工作流检查点有数据库后端；聊天会话主要由 JSON 文件和浏览器本地存储承载。聊天历史数据库仍属于重设计目标，而非当前已完成实现。

历史重设计方案建议新增一个独立的服务端 SQLite 会话库。完成该方案后，才形成清晰的“双数据库职责”架构：一个保存 LangGraph 工作流状态，另一个保存聊天会话和侧栏历史。

## 2. 当前存储结构

```mermaid
flowchart TD
    A[auto-video-editor] --> B[LangGraph 工作流]
    B --> C{检查点后端}
    C -->|开发/默认| D[(SQLite: checkpoints/<thread_id>.sqlite)]
    C -->|生产模式| E[(PostgreSQL: langgraph)]
    E --> F[checkpoints / checkpoint_blobs / checkpoint_writes]
    E --> G[workflow_runs 运行台账表]

    A --> H[OpenStoryline 聊天会话]
    H --> I[服务端 session_state.json]
    H --> J[浏览器 localStorage 历史列表]

    K[重设计目标] -.尚未实现.-> L[(独立 SQLite 会话库 sessions.sqlite3)]
```

图中的 SQLite 检查点库与 PostgreSQL 检查点后端是**同一职责的两种实现路径**。`sessions.sqlite3` 是历史重设计建议新增的存储，并非当前代码已经存在的数据库文件。

## 3. 数据存储一：LangGraph 工作流检查点

### 3.1 保存什么

这个存储由 LangGraph 的 `SqliteSaver` / `AsyncPostgresSaver` 管理，用来保存图执行过程中的状态快照及恢复信息，主要用途包括：

- 工作流执行到哪一个节点、当前图状态是什么。
- 在人工关卡或 `interrupt` 中断后，能够基于同一个 `thread_id` 继续执行。
- 服务重启或任务重新调度后，恢复尚未完成的工作流。
- 调试工作流执行过程。

它**不是聊天侧栏历史库**，不能自动提供聊天会话标题、预览、会话列表分页和会话重命名等功能。

### 3.2 SQLite 路径与默认行为

`config.py` 定义：

```python
CHECKPOINTER_BACKEND = "sqlite"
CHECKPOINTER_DB_DIR = Path(__file__).parent / "checkpoints"
```

`make_checkpointer()` 在 SQLite 模式下按 `thread_id` 创建文件：

```text
checkpoints/<thread_id>.sqlite
```

例如 `thread_id=video-001` 时，对应文件通常为：

```text
checkpoints/video-001.sqlite
```

它是本地开发和默认路径。SQLite 文件可能按不同 `thread_id` 分开创建，因此“一个 SQLite 数据库”不一定意味着磁盘上只有一个 `.sqlite` 文件。

### 3.3 PostgreSQL 路径

项目也支持生产模式通过 `graph.py::get_checkpointer()` 使用 `AsyncPostgresSaver`。连接地址由 `POSTGRES_URI` 配置，README 给出的本地示例为：

```text
postgresql://postgres:devpass@localhost:5432/langgraph
```

LangGraph 检查点相关表包括：

- `checkpoints`
- `checkpoint_blobs`
- `checkpoint_writes`

启动脚本和 `scripts/run_workflow.py` 将开发路径与生产路径区分开：开发路径走 SQLite / 内存检查点；生产路径走 PostgreSQL 检查点。应以实际启动参数和环境变量判断正在使用哪条路径，不要因为仓库里同时存在两套实现，就认定它们正在双写同一份检查点。

### 3.4 `workflow_runs` 是什么

`monitoring/runs_table.py` 定义了 PostgreSQL 表 `workflow_runs`，用途是记录运行台账，例如：

- `thread_id`
- 视频名称
- 当前检查点/关卡
- 运行状态和更新时间

这张表偏向**运维查询和运行状态管理**，与 LangGraph 的详细检查点数据不同。但它通过同一个 `POSTGRES_URI` 连接，因此通常属于同一个 PostgreSQL 数据库中的另一张表，不能算作第三个数据库实例。

## 4. 当前存储二：OpenStoryline 聊天会话

### 4.1 服务端用 JSON 文件保存会话

当前 `openstoryline/agent_fastapi.py` 中：

- `ChatSession.state_file_path_for()` 计算会话文件路径。
- `ChatSession.save_state_atomic()` 将状态写入临时文件，再通过 `os.replace()` 替换目标文件。
- `ChatSession.load_from_state()` 根据会话 ID 从文件恢复状态。
- `SessionStore` 先查进程内存；内存中没有会话时，再尝试从对应 JSON 文件恢复。

会话文件路径为：

```text
<cfg.project.outputs_dir>/<session_id>/session_state.json
```

文件内包含聊天回放 `history`、LangChain 消息序列化数据、模型设置和媒体登记等会话状态。它可以在同一个 `session_id` 和同一数据目录下支持会话恢复，但它不是 SQLite 数据库，也没有自动列举所有历史会话的能力。

### 4.2 浏览器 `localStorage` 保存侧栏历史列表

当前 `openstoryline/web/static/app.js` 使用以下键：

```javascript
const SESSION_ID_KEY = "openstoryline_session_id";
const SESSION_LIST_KEY = "openstoryline_session_list_v1";
```

其中：

- `openstoryline_session_id`：记录当前浏览器选中的会话 ID。
- `openstoryline_session_list_v1`：保存侧栏历史列表元数据；代码会将列表裁剪到最多 50 条。

这不是服务端持久化数据库。浏览器之间、浏览器配置文件之间，以及 `localhost`、`127.0.0.1`、局域网 IP 等不同站点来源之间，都可能读取到不同的 `localStorage`。

当前前端启动时会先加载本地列表，再尝试恢复当前会话；它没有先从服务端拉取完整历史会话目录的流程。历史重设计文档也指出，当前后端缺少 `GET /api/sessions` 列表接口，因此新浏览器无法只凭服务端自动枚举旧会话。

## 5. “重设计对话历史模块”与当前代码的差异

历史方案建议新增服务端 SQLite 数据库，例如：

```text
sessions.sqlite3
```

并通过明确的配置项（如 `OPENSTORYLINE_SESSION_DB`）固定数据库绝对路径。数据库保存聊天会话元数据和可恢复的完整会话状态，包括：

- 会话 ID、标题、标题来源。
- 创建时间、更新时间、最后一条消息预览、消息数量。
- 会话状态、软删除标记。
- 完整的 `state_json`，用来恢复聊天消息、工具轨迹、模型设置和媒体登记等。

该方案还要求新增服务端会话列表 API，并让前端从服务端加载历史；浏览器端只保留当前会话 ID 和界面偏好，不再把 `localStorage` 作为历史列表的权威来源。

**截至本次核查，当前 `main` 的代码仍能看到 `SESSION_LIST_KEY`、JSON 文件持久化和按 ID 读取会话的实现；未看到重设计所提议的独立 SQLite 会话库已取代它们。** 因此，重设计方案应视为待实施目标，不能描述成现状。

## 6. 两类存储的职责对比

| 比较项 | LangGraph 检查点存储 | 聊天会话存储（当前实现） | 聊天会话存储（重设计目标） |
|---|---|---|---|
| 主要数据 | 图状态、执行检查点、中断恢复信息 | 聊天状态 JSON 文件；侧栏列表保存在浏览器 | 会话元数据与完整会话状态统一存入 SQLite |
| 当前介质 | SQLite 或 PostgreSQL，按运行模式选择 | 服务端 `session_state.json` + 浏览器 `localStorage` | 独立服务端 SQLite 数据库 |
| 是否适合跨浏览器共享历史 | 不适合直接承担此职责 | 不可靠；侧栏列表是浏览器本地的 | 适合；同一个服务端提供统一历史 API |
| 是否负责工作流恢复 | **是** | 否 | 否 |
| 是否负责侧栏标题/排序/分页 | 否 | 主要由前端本地列表维护 | **是** |
| 能否互相替代 | 不能 | 不能 | 不能 |

## 7. 最终答案：究竟有几个数据库？

### 当前 `main`

严格来说，**当前实现不是“两个已落地的业务数据库”**：

- 工作流检查点是一个独立的数据职责，后端可选 SQLite 或 PostgreSQL。
- PostgreSQL 中的 `workflow_runs` 是运行台账表，不是独立数据库。
- 聊天历史当前存于 JSON 文件和浏览器 `localStorage`，还没有落地为独立 SQLite 会话数据库。

### 完成重设计之后

推荐采用两个独立的数据职责：

1. **工作流数据库**：保存 LangGraph checkpoint。开发模式可以使用 SQLite；生产模式可以使用 PostgreSQL。
2. **聊天会话数据库**：使用独立 SQLite 数据库存储会话列表、标题、消息预览、完整会话状态和删除标记。

生产模式下，物理数据库技术可以是 **PostgreSQL + SQLite**；若工作流也选择 SQLite，则可能是两个独立的 SQLite 数据库文件，各自拥有不同的数据和生命周期。不能把两种后端当成同一份数据库，也不应把聊天记录塞进 LangGraph checkpoint 里来绕过会话列表 API 缺失的问题。

## 8. 实施建议

1. **先实施聊天会话 SQLite 库和服务端列表 API**，把服务端作为历史列表唯一数据源。
2. 将前端 `localStorage` 缩减为当前会话 ID、语言等 UI 状态，不再保存权威历史列表。
3. 保持工作流 checkpoint 与聊天会话两个职责独立，分别备份、迁移和恢复。
4. 迁移旧数据前，备份原来的 `session_state.json` 文件；完成数据校验前不要删除旧文件。
5. 为“新建、加载、切换、重命名、软删除、刷新、重启、跨浏览器访问、保存失败”补齐集成测试。

## 9. 主要代码依据

以下链接指向 `main` 分支中的相关实现：

- [`config.py`：检查点后端和路径配置](https://github.com/wayyet/auto-video-editor/blob/main/config.py#L329-L355)
- [`config.py`：`make_checkpointer()` 的 SQLite / PostgreSQL 分支](https://github.com/wayyet/auto-video-editor/blob/main/config.py#L502-L543)
- [`graph.py`：PostgreSQL `AsyncPostgresSaver` 的初始化](https://github.com/wayyet/auto-video-editor/blob/main/graph.py#L372-L406)
- [`scripts/run_workflow.py`：开发与生产执行路径](https://github.com/wayyet/auto-video-editor/blob/main/scripts/run_workflow.py#L237-L279)
- [`monitoring/runs_table.py`：PostgreSQL `workflow_runs` 台账表](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/runs_table.py)
- [`openstoryline/agent_fastapi.py`：聊天会话状态的 JSON 文件保存和恢复](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/agent_fastapi.py#L1415-L1515)
- [`openstoryline/agent_fastapi.py`：原子写入与 JSON 恢复](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/agent_fastapi.py#L1740-L1865)
- [`openstoryline/agent_fastapi.py`：`SessionStore`](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/agent_fastapi.py#L2408-L2475)
- [`openstoryline/web/static/app.js`：侧栏历史键与 `localStorage` 逻辑](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/web/static/app.js#L10-L13)
- [`openstoryline/web/static/app.js`：本地列表加载与保存](https://github.com/wayyet/auto-video-editor/blob/main/openstoryline/web/static/app.js#L2856-L2894)

---

**一句话总结：** 当前 `main` 是“工作流检查点数据库后端（SQLite / PostgreSQL 二选一）+ JSON 会话文件 + 浏览器本地历史列表”；重设计完成后，才应成为“工作流检查点数据库 + 独立聊天会话 SQLite 数据库”的双数据库职责架构。

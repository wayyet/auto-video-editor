from pathlib import Path

content = r'''# 创建台「AI 内容明细」按钮与新页面
## 设计审查、修订方案及分阶段执行计划

- 文档日期：2026-11-19
- 审查对象：`docs/第七期开发/auto-video-editor_创建台AI内容明细按钮与新页面_设计分析及执行计划_20261119.md`
- 仓库：`https://github.com/wayyet/auto-video-editor`
- 分支：`main`
- 本次核对代码提交：`09c297d6cb36273f5f5a44b5a45ccb280b848dbc`
- 审查结论：**原方案方向正确，需按本文修订后实施**
- 实施状态：仅完成代码与文档审查；未修改仓库代码、未运行项目测试、未重启服务。

---

# 1. 审查范围与结论

本次审查围绕以下目标：

1. 在创建台顶栏增加「AI 内容明细」入口。
2. 点击后打开独立页面，不使用弹窗、抽屉或下拉面板。
3. 展示五类工作流产物：
   - 网页话题检索；
   - 文案模板推荐；
   - 生成文案；
   - 中文字幕；
   - 英文字幕。
4. 网页检索区域明确显示搜索引擎和来源网站。
5. 页面按 `thread_id` 读取对应任务的数据，不能混用不同任务的数据。
6. 数据缺失、节点跳过、节点失败、checkpoint 不可用时，页面应准确说明状态，不能将不同状态都显示成「未生成」。

## 1.1 总体判断

| 审查项 | 判断 | 说明 |
|---|---|---|
| 独立页面方案 | 正确 | 与创建台现有页面职责相符 |
| 五类内容划分 | 正确 | 与当前 artifact 和 checkpoint 数据来源基本匹配 |
| artifact + checkpoint 取数 | 正确 | 事件摘要不是这些内容的可靠数据来源 |
| 网页搜索引擎展示 | 基本正确 | 当前节点使用 DuckDuckGo HTML，但跳过和异常时可能没有 JSON artifact |
| 模板字段映射 | 需要修订 | `description`、`score` 属于候选/选中模板对象，不应假设为 artifact 顶层字段 |
| 英文字幕状态 | 需要修订 | `skipped` 可能代表正常无源字幕，不能一律视为异常 |
| 页面路由位置 | 错误 | 当前入口文件是仓库根目录 `monitor_fastapi.py`，不是 `web/monitor_fastapi.py` |
| Node 测试接入 | 错误 | 当前 pytest 包装器点名运行测试文件，不会自动发现新增 `.mjs` 文件 |
| 安全和异常处理 | 不完整 | 需要明确路径归属校验、输入校验、HTML 转义和 URL 协议限制 |
| 验收标准 | 不充分 | 需要增加跨任务隔离、状态矩阵、字段缺失、恶意内容等测试 |

**结论：保留原需求和页面形态，修订后端路径、数据归一化、状态语义、安全策略和测试接入方式。**

---

# 2. 已核实的项目现状

## 2.1 实际文件位置

当前 `main` 分支的相关文件：

| 文件 | 当前职责 |
|---|---|
| `monitor_fastapi.py` | FastAPI 应用入口，注册页面路由和 API Router |
| `web/api_console.py` | 创建台 API，Router 前缀为 `/api/console` |
| `web/console_service.py` | 创建台任务服务、任务目录解析、任务详情读取等业务逻辑 |
| `web/console_state.py` | 构建创建任务初始状态 |
| `web/gate_state.py` | 从 LangGraph checkpoint 读取状态 |
| `web/console_uploads.py` | 上传和 thread_id 等输入校验 |
| `web/console.html` | 创建台页面 |
| `web/static/workflow_console.mjs` | 创建台前端纯逻辑及 API 路径表 |
| `web/tests/workflow_console.test.mjs` | 创建台前端 Node 测试 |
| `web/tests/sse_frame_contract.test.mjs` | SSE 帧契约测试 |
| `tests/test_workflow_console_frontend.py` | 通过 pytest 调用指定 Node 测试文件 |

### 必须修正的路径错误

原方案写的是：

`web/monitor_fastapi.py`

当前仓库实际不存在该文件。页面路由必须添加到：

`monitor_fastapi.py`

不能照原文创建一个新的 `web/monitor_fastapi.py`，否则新路由不会自动注册到正在运行的 FastAPI 应用。

## 2.2 创建台顶栏

当前 `web/console.html` 顶栏包含：

- `工作流创建台`
- 状态徽章
- 弹性占位
- `监控首页 →`
- `节点拓扑 →`

新入口建议放在「监控首页 →」之前：

`AI 内容明细 →`

链接建议使用：

`/ai-content?thread_id=<URL 编码后的 thread_id>`

建议沿用相邻入口的新标签页行为：

- `target="_blank"`
- `rel="noopener noreferrer"`

必须在创建成功并取得有效 `thread_id` 后更新链接。
查看模式则从 URL 查询参数读取现有 `thread_id`。

注意：不能在没有有效 `thread_id` 的情况下跳转到明细页面。

## 2.3 当前后端 API 结构

`web/api_console.py`：

- `router = APIRouter(prefix="/api/console")`
- 已存在任务列表、任务详情、人工关卡等接口。
- 已有 `validate_thread_id`。
- 已导入 `run_in_threadpool`。
- 已复用 `web.api_workflow._verify_same_origin`。

新增 API 应放在该 Router 中：

`GET /api/console/runs/{thread_id}/content-details`

业务逻辑放在：

`web/console_service.py`

不要把文件读取、JSON 归一化、checkpoint 读取和大量字段处理全部写进 API 路由函数。

## 2.4 现有 checkpoint 读取链路

`web/gate_state.py` 已提供：

- `fetch_snapshot(thread_id)`
- `fetch_state_values(thread_id)`

当前实现针对 Windows uvicorn Proactor 环境使用独立线程和 SelectorEventLoop 处理 checkpoint 读取。

因此可以复用 `fetch_state_values`，不应再新建另一套 LangGraph checkpoint 连接实现。

需要注意：

- checkpoint 读取必须限定到请求对应的 `thread_id`。
- checkpoint 读取异常不能导致三个磁盘 artifact 区块也无法显示。
- 不得将其他任务的最新 checkpoint 当作当前任务数据。
- 不得为了展示页面而触发工作流执行、恢复或写入 checkpoint。

---

# 3. 五类数据的真实来源与字段映射

## 3.1 网页话题检索

主要文件：

- `nodes/storyline/node_search_web_topic.py`
- `storyline_capabilities/search_web_topic.py`

artifact：

`<CONSOLE_OUTPUTS_ROOT>/<thread_id>/storyline/search_web_topic.json`

当前节点调用：

`provider="duckduckgo_html"`

检索结果由能力层返回，当前可识别的结果状态包括：

- `ok`
- `no_results`
- `failed`

节点缓存复用时会将状态改写为：

- `cache_hit`

当前条目包含 URL、标题、摘要和来源等信息。

### 关键问题：节点可能不写 artifact

当前节点在以下情况下可能直接返回状态标签，而不写 `search_web_topic.json`：

1. `STORYLINE_ENABLE_WEB_TOPIC` 关闭；
2. 检索词为空；
3. 检索调用发生异常，节点记录错误和失败标签后返回。

因此：

**文件不存在不等于「该任务未生成网页检索内容」。**

应通过 checkpoint 状态标签和 artifact 是否存在综合判断。

建议状态：

| 状态 | 含义 | 页面文案 |
|---|---|---|
| `ok` | 检索成功且有结果 | 已完成 |
| `cache_hit` | 命中缓存 | 已完成（缓存） |
| `no_results` | 检索完成但没有结果 | 检索完成，未找到结果 |
| `failed` | 检索失败 | 检索失败 |
| `skipped` | 开关关闭或检索词为空 | 本次任务跳过网页检索 |
| `pending` | 尚无完成证据，且任务仍在运行 | 等待生成 |
| `missing` | 任务已结束但没有产物且无跳过/失败证据 | 产物缺失 |
| `invalid` | 文件存在但 JSON 或结构无效 | 产物格式异常 |
| `unavailable` | 读取过程不可用 | 暂时无法读取 |

如果节点异常时只留下 `status_log` / `error_log`，页面应从这些信息判断失败，不能单凭文件缺失判断。

### 搜索引擎与来源网站

当前搜索引擎由节点固定为 DuckDuckGo HTML。

建议展示：

- 搜索引擎：DuckDuckGo
- 检索词
- 检索时间
- 来源网站域名
- 结果标题
- 摘要
- 原始链接

域名从 URL 解析。必须只接受 `http:` 和 `https:` 协议；无效 URL 不生成可点击链接。

不要把来源网站域名误写成搜索引擎名称。

## 3.2 文案模板推荐

主要文件：

- `nodes/storyline/node_script_template_recommendation.py`
- `storyline_capabilities/script_template_rec.py`

artifact：

`<CONSOLE_OUTPUTS_ROOT>/<thread_id>/storyline/script_template_plan.json`

当前节点在异常时会写入 `status: unavailable` 的降级 artifact，因此该区块不应仅根据文件是否存在判断状态。

### 字段映射修正

原方案将 `description` 和 `score` 当作顶层字段展示，这一假设不准确。

能力层的候选视图 `_candidate_view` 会生成候选对象，字段包括：

- `description`
- `score`

因此应该从：

- `candidates[]`
- `selected_template`

中读取相应字段。

推荐的后端归一化结果：

```json
{
  "status": "ok",
  "template_id": "template-id",
  "template_version": "1.0",
  "selected_template": {
    "name": "模板名称",
    "description": "模板描述",
    "score": 0.91
  },
  "candidates": [
    {
      "template_id": "template-id",
      "description": "模板描述",
      "score": 0.91
    }
  ],
  "retrieval_method": "keyword_bm25",
  "selection_method": "deterministic_top1",
  "selected_template_content": "...",
  "content_truncated": false
}
```

上述为 API 归一化示例，不代表原始 artifact 必然具有完全相同的嵌套结构。实施时必须按实际 `selected_template` 和 `candidates` 对象的字段读取，不得虚构缺失值。

页面展示：

- 模板 ID 和版本；
- 选中模板名称；
- 模板描述；
- 评分；
- 检索方式；
- 选择方式；
- 候选模板；
- 模板正文（折叠展开）。

## 3.3 生成文案

主要文件：

- `nodes/storyline/node_generate_script.py`
- `storyline_capabilities/generate_script.py`

artifact：

`<CONSOLE_OUTPUTS_ROOT>/<thread_id>/storyline/script.json`

当前节点写入：

- `group_scripts`
- `lang`
- `voiceover_style`
- `references`
- `template_ref`

`group_scripts` 中包含分组文案字段，例如：

- `group_id`
- `narration`
- `summary`
- `lang`
- `voiceover_style`
- `captions`

页面至少展示：

1. 分组 ID；
2. 分组摘要；
3. 旁白全文；
4. 语言；
5. 配音风格；
6. 参考资料标题、域名和链接。

`references` 可能为空。不能将空数组解释为文案生成失败。

注意：当前节点会先将 `script.json` 写入磁盘，再进行部分旁白有效性裁决。因此，文件存在不代表节点一定成功完成。需要结合 `status_log`、`error_log` 和任务状态判断。

## 3.4 中文字幕

数据来源：

`LangGraph checkpoint state.subtitle_segments_zh`

结构定义位于 `state.py` 的 `SubtitleSegment`：

- `index`
- `start_ms`
- `end_ms`
- `text_zh`

写入者：

`nodes/node_08_add_subtitles.py`

该节点仅在有中文字幕段时写入 `subtitle_segments_zh`。没有该字段不一定代表异常，也可能是没有可用旁白字幕源。

页面展示：

| 序号 | 开始时间 | 结束时间 | 中文字幕 |
|---|---|---|---|

时间由毫秒转换为可读格式。建议统一显示 `mm:ss.s`，但原始毫秒必须保留在 API 数据中。

## 3.5 英文字幕

数据来源：

- `subtitle_segments_en`
- `subtitle_translation_status`

写入者：

`nodes/node_16a_translate_and_check.py`

当前翻译状态：

| 状态 | 含义 | 页面文案 |
|---|---|---|
| `real` | 使用真实翻译客户端 | 真实翻译 |
| `stub` | 使用 Mock/占位翻译 | 占位翻译，不能视为真实翻译 |
| `skipped` | 无源字幕或缺少英文草稿目录而跳过 | 本次未执行翻译 |

当前无源字幕是正常流程中的一种可能输入，因此 `skipped` 不能一律显示为错误。

当 checkpoint 中没有 `subtitle_translation_status` 时，应结合任务状态和 `status_log` 判断是等待生成、旧任务缺失还是读取异常。

页面不能将 `stub` 标记成「翻译成功」。

---

# 4. 修订后的整体架构

```mermaid
flowchart TD
    A[创建台 console.html] --> B[AI 内容明细入口]
    B --> C[GET /ai-content?thread_id=...]
    C --> D[web/ai_content.html]
    D --> E[ai_content.mjs]
    E --> F[GET /api/console/runs/{thread_id}/content-details]
    F --> G[web/api_console.py]
    G --> H[web/console_service.py]
    H --> I[校验 thread_id 和任务目录]
    I --> J[读取三个 storyline JSON artifact]
    I --> K[读取当前 thread_id 的 LangGraph checkpoint]
    J --> L[字段归一化与状态判定]
    K --> L
    L --> M[统一 JSON 响应]
    M --> N[前端渲染五个内容区块]
```

## 4.1 职责划分

| 层 | 职责 | 禁止事项 |
|---|---|---|
| `console.html` | 提供入口并传递 thread_id | 不读取 artifact |
| `monitor_fastapi.py` | 注册 `/ai-content` 页面路由 | 不写业务数据读取逻辑 |
| `api_console.py` | 参数、安全校验、调用 service、返回响应 | 不堆积复杂 JSON 解析 |
| `console_service.py` | 读取 artifact/checkpoint、归一化数据 | 不执行工作流 |
| `gate_state.py` | 复用 checkpoint 读取 | 不创建新的 checkpoint 访问实现 |
| `ai_content.mjs` | 纯逻辑归一化、格式化、状态文案 | 不直接访问磁盘 |
| `ai_content.html` | 页面布局与展示 | 不在 HTML 中复制复杂业务逻辑 |

## 4.2 只读原则

整个功能必须是只读查询：

- 不触发 LangGraph 节点。
- 不启动工作流。
- 不恢复人工关卡。
- 不修改 checkpoint。
- 不修改 artifact。
- 不修改剪映草稿。
- 不调用外部搜索服务。
- 不重新生成文案或字幕。

---

# 5. 修订后的 API 契约

## 5.1 路由

`GET /api/console/runs/{thread_id}/content-details`

在 `web/api_console.py` 的现有 Router 中注册。

## 5.2 参数与安全校验

必须：

1. 调用现有 `validate_thread_id` 校验 thread_id。
2. 通过 `console_service` 现有任务目录解析逻辑确定任务目录。
3. 复用 `_task_dir_for` 的 `resolve()` 和目录归属检查思想，确保最终路径仍位于 `CONSOLE_OUTPUTS_ROOT` 下。
4. 不允许前端传入任意磁盘路径。
5. 不允许使用未经校验的 `thread_id` 拼接路径。
6. 同源校验沿用当前只读 API 的既有安全纪律；不要为了 GET 接口无条件复制写接口 CSRF 要求。应按当前 `_verify_same_origin` 的实际调用约定实施。

## 5.3 推荐响应结构

```json
{
  "ok": true,
  "thread_id": "console-xxxx",
  "task_status": "completed",
  "generated_at": "2026-11-19T10:00:00Z",
  "web_topic": {
    "status": "ok",
    "engine": "DuckDuckGo",
    "query": "...",
    "searched_at": "...",
    "entries": [
      {
        "title": "...",
        "url": "https://example.com/article",
        "domain": "example.com",
        "snippet": "..."
      }
    ],
    "reason": null
  },
  "template": {
    "status": "ok",
    "template_id": "...",
    "template_version": "...",
    "selected_template": {},
    "candidates": [],
    "retrieval_method": "keyword_bm25",
    "selection_method": "deterministic_top1",
    "selected_template_content": "...",
    "content_truncated": false,
    "reason": null
  },
  "script": {
    "status": "ok",
    "lang": "zh",
    "voiceover_style": "...",
    "groups": [],
    "references": [],
    "reason": null
  },
  "subtitles_zh": {
    "status": "ok",
    "count": 0,
    "cues": [],
    "reason": null
  },
  "subtitles_en": {
    "status": "ok",
    "count": 0,
    "translation_status": "real",
    "cues": [],
    "reason": null
  }
}
```

这是目标归一化契约。实施时应对照真实 artifact 和 state 字段，不存在的字段应返回 `null` 或省略，不得伪造业务值。

## 5.4 区块状态统一枚举

建议统一使用：

- `ok`
- `cache_hit`
- `no_results`
- `skipped`
- `pending`
- `missing`
- `failed`
- `unavailable`
- `invalid`

不要将所有非 `ok` 状态统一转换成 `missing`。

状态判断优先级建议：

1. 已读取到有效产物：按产物状态展示。
2. 有明确跳过标签：`skipped`。
3. 有明确失败标签或错误记录：`failed`。
4. 任务仍在运行且节点尚未执行：`pending`。
5. 任务已结束但产物缺失：`missing`。
6. 文件存在但 JSON/结构无法解析：`invalid`。
7. checkpoint/存储服务不可访问：`unavailable`。

同一字段存在矛盾证据时，保留原始状态证据并记录服务端日志，不应静默将失败覆盖成成功。

## 5.5 部分失败策略

推荐：

- 三个 artifact 区块分别读取、分别降级。
- checkpoint 字幕读取失败时，只将两个字幕区块标记为 `unavailable`。
- 只要请求本身有效，且任务记录可定位，局部数据不可用仍返回 HTTP 200。
- thread_id 非法返回 400。
- 任务不存在返回 404。
- 仅在服务整体无法处理请求时返回 5xx。

不要把「任务不存在」与「任务存在但 checkpoint 不可用」混为一谈。

---

# 6. 前端页面设计

## 6.1 页面结构

新建：

- `web/ai_content.html`
- `web/static/ai_content.mjs`

页面标题：

`AI 内容明细`

页面顶部显示：

- 当前 `thread_id`
- 任务状态
- 返回创建台链接
- 刷新按钮

主体固定五个区块：

### 01 网页话题检索

展示：

- 搜索引擎：DuckDuckGo
- 检索词
- 检索时间
- 结果数量
- 来源网站域名
- 标题链接
- 摘要

### 02 文案模板推荐

展示：

- 模板名称、ID、版本
- 描述
- 评分
- 召回方式
- 选择方式
- 候选模板列表
- 选中模板正文（使用 `<details>` 折叠）

### 03 生成文案

逐组展示：

- group_id
- summary
- narration 全文
- 语言
- voiceover_style
- references

### 04 中文字幕

展示：

- 字幕数量
- 序号
- 起始时间
- 结束时间
- 中文文本

### 05 英文字幕

展示：

- 字幕数量
- 翻译状态
- 序号
- 起始时间
- 结束时间
- 英文文本

即使某个区块没有数据，也保留区块标题和状态说明，避免页面结构随数据变化。

## 6.2 状态视觉规范

| 状态 | 展示建议 |
|---|---|
| `ok` | 正常完成 |
| `cache_hit` | 完成（缓存） |
| `no_results` | 已检索，无结果 |
| `skipped` | 已跳过，并显示原因 |
| `pending` | 等待节点生成 |
| `missing` | 任务结束但产物缺失 |
| `failed` | 节点执行失败 |
| `unavailable` | 当前无法读取 |
| `invalid` | 产物格式异常 |

页面不能仅凭「空数组」推断失败。

## 6.3 安全要求

1. 所有动态文本使用 `textContent` 或等价安全 DOM API。
2. 不得将 artifact 文本直接拼接到 `innerHTML`。
3. 若使用 HTML 模板，必须对所有动态文本进行完整转义。
4. 外部 URL 仅允许 `http:`、`https:`。
5. 外链使用 `target="_blank"` 时必须加 `rel="noopener noreferrer"`。
6. URL 解析失败时显示纯文本，不生成链接。
7. 不允许 artifact 内容控制页面脚本、样式或事件处理器。
8. 页面 fetch 必须处理 400、404、5xx、网络失败、JSON 解析失败和超时。
9. 刷新页面只重新读取数据，不触发工作流。

## 6.4 刷新和运行中任务

建议提供手动刷新。

初版不建议引入轮询，避免新增不必要的请求和状态竞争。

如果任务仍在运行，页面可提示：

`任务正在执行，部分内容尚未生成。点击刷新获取最新内容。`

后续如确有实时刷新需求，再独立评估是否复用现有 SSE 机制。

---

# 7. 文件改动清单（修订版）

## 7.1 后端

| 文件 | 操作 | 内容 |
|---|---|---|
| `web/console_service.py` | 修改 | 增加 `get_ai_content_details(thread_id)` 及内部归一化辅助函数 |
| `web/api_console.py` | 修改 | 增加 `GET /runs/{thread_id}/content-details` |
| `monitor_fastapi.py` | 修改 | 增加 `/ai-content` 页面路由 |
| `web/gate_state.py` | 原则上不修改 | 复用 `fetch_state_values`；只有发现现有读取接口无法满足只读需求时才评估修改 |

## 7.2 前端

| 文件 | 操作 | 内容 |
|---|---|---|
| `web/console.html` | 修改 | 顶栏增加入口，按 thread_id 更新链接 |
| `web/ai_content.html` | 新建 | 五区块页面结构 |
| `web/static/ai_content.mjs` | 新建 | 数据归一化、时间格式化、状态映射及渲染辅助逻辑 |
| `web/static/workflow_console.mjs` | 修改 | `CONSOLE_API_PATHS` 增加 `contentDetails` |

## 7.3 测试

| 文件 | 操作 | 内容 |
|---|---|---|
| `tests/unit/test_console_content_details.py` | 新建 | 后端 API、artifact、checkpoint、状态、安全测试 |
| `web/tests/workflow_console.test.mjs` | 修改 | 断言新 API 路径 |
| `web/tests/ai_content.test.mjs` | 新建 | 前端纯逻辑测试 |
| `tests/test_workflow_console_frontend.py` | 修改 | 显式接入 `ai_content.test.mjs`，不能依赖 glob 假设 |

---

# 8. 分阶段执行计划

## P0：实现前基线与接口冻结

目标：避免在错误路径和错误字段假设上开发。

任务：

1. 确认工作区干净、当前分支和 HEAD。
2. 确认 `CONSOLE_OUTPUTS_ROOT` 的实际配置。
3. 确认 `_task_dir_for`、`validate_thread_id` 的现有调用方式。
4. 确认 `get_console_run` 的任务不存在、删除、历史状态行为。
5. 确认 checkpoint 获取函数对不存在的 thread_id 如何返回。
6. 为检索、模板、文案和字幕分别建立真实 JSON/state fixture。
7. 冻结本文件第 5 节的 API 契约和状态枚举。

验收：

- 文件路径全部对应 `main` 分支真实文件。
- 所有字段映射有真实 fixture 支持。
- 不存在通过猜测补造的字段。
- 明确任务运行中、完成、失败、旧任务和不存在任务的判断方式。

## P1：后端只读数据服务

目标：实现统一、隔离、可降级的数据读取层。

任务：

1. 在 `web/console_service.py` 增加业务读取函数。
2. 对 thread_id 执行现有格式校验。
3. 使用现有任务目录解析和归属检查。
4. 分别读取三个 JSON artifact。
5. 分别解析并校验 JSON 顶层类型和必要字段。
6. 读取当前 thread_id 的 checkpoint state。
7. 归一化中英文字幕。
8. 结合 artifact、status_log、error_log 和任务状态计算区块状态。
9. 局部异常单独降级，不能使其他区块丢失。
10. 限制返回数据规模，避免超大文案、异常长 URL 或超量字幕导致响应失控。

验收：

- 单个 artifact 损坏不影响其他 artifact。
- checkpoint 不可用不影响三个磁盘 artifact。
- 不会跨 thread_id 读取数据。
- 目录解析不能逃逸 `CONSOLE_OUTPUTS_ROOT`。
- 只读请求不修改任何文件和 checkpoint。

## P2：API 和页面路由

目标：让前端可以稳定读取后端数据。

任务：

1. 在 `web/api_console.py` 注册只读 API。
2. 使用现有 `validate_thread_id`。
3. 使用 `run_in_threadpool` 执行同步文件和 checkpoint 读取。
4. 正确区分 400、404、200 局部降级和 5xx。
5. 在仓库根目录 `monitor_fastapi.py` 注册 `/ai-content`。
6. 页面文件缺失时返回明确 404。
7. 不创建错误路径 `web/monitor_fastapi.py`。

验收：

- API 路径与前端路径表完全一致。
- 非法 thread_id 无法访问磁盘任意目录。
- 不存在的任务返回 404。
- 有效任务的局部数据读取失败时仍能返回其他可用区块。
- 页面路由由实际 FastAPI app 注册。

## P3：前端页面和创建台入口

目标：完成可用的独立展示页面。

任务：

1. 新建 `web/ai_content.html`。
2. 新建 `web/static/ai_content.mjs`。
3. 增加五个固定区块。
4. 增加统一状态文案和异常展示。
5. 增加时间格式化。
6. 增加来源域名提取和安全 URL 校验。
7. 动态内容使用安全 DOM API。
8. 在 `CONSOLE_API_PATHS` 增加 `contentDetails`。
9. 在 `console.html` 顶栏增加「AI 内容明细 →」。
10. 查看模式根据已有 thread_id 显示入口。
11. 创建成功后更新入口 href。
12. 无 thread_id 时隐藏或禁用入口。

验收：

- 新页面可以独立打开。
- 五个区块始终存在。
- 每个区块显示正确数据和状态。
- 页面刷新不触发工作流。
- 外部链接不能执行 `javascript:` 等非 HTTP(S) 协议。
- 动态文案不能造成 HTML/脚本注入。

## P4：测试与回归

### 后端测试矩阵

| 场景 | 预期 |
|---|---|
| 三份 artifact 全部有效 | 三个区块正确显示 |
| 网页检索 `cache_hit` | 显示缓存命中 |
| 网页检索 `no_results` | 显示无结果，不显示失败 |
| 网页检索开关关闭 | 显示 skipped |
| 网页检索节点异常且无 artifact | 根据状态证据显示 failed |
| 模板 artifact `unavailable` | 显示不可用原因 |
| 模板 description/score 在候选对象内 | 正确显示 |
| script references 为空 | 正常显示空引用 |
| 中文字幕不存在 | 按任务状态显示 pending/skipped/missing |
| 英文字幕 `real` | 显示真实翻译 |
| 英文字幕 `stub` | 明确标记占位翻译 |
| 英文字幕 `skipped` | 显示跳过原因 |
| 单个 JSON 损坏 | 仅对应区块 invalid |
| checkpoint 抛异常 | 两个字幕区块 unavailable，其他区块仍可用 |
| 非法 thread_id | 400 |
| 不存在的任务 | 404 |
| 两个不同 thread_id | 返回各自数据，不串数据 |
| 路径穿越输入 | 拒绝访问 |
| 特大 artifact | 按限制拒绝或截断并明确标记 |

### 前端测试矩阵

- `mm:ss.s` 时间格式化边界。
- 空数据和 null 字段。
- 所有区块状态文案。
- `cache_hit` 与 `no_results` 区分。
- `stub` 不得显示为真实翻译。
- 无效 URL 不生成链接。
- `javascript:` URL 被拒绝。
- 特殊字符不造成 HTML 注入。
- 五区块始终渲染。
- API 404、500、网络失败和 JSON 解析失败有明确提示。

### Node 测试接入

当前 `tests/test_workflow_console_frontend.py` 显式运行：

- `web/tests/workflow_console.test.mjs`
- `web/tests/sse_frame_contract.test.mjs`

因此新增的 `web/tests/ai_content.test.mjs` 必须显式增加 pytest 包装测试，或者调整为经过验证的统一测试发现机制。

推荐本次采用显式包装，保持现有项目测试纪律。

不能仅写：

`node --test web/tests/*.test.mjs`

然后假定 pytest 会自动运行新文件。

### 回归命令

在 Windows PowerShell 7 项目环境中执行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_console_content_details.py tests/test_workflow_console_frontend.py -q
```

单独运行新前端逻辑测试：

```powershell
node --test web/tests/ai_content.test.mjs
```

运行现有前端测试：

```powershell
node --test web/tests/workflow_console.test.mjs
node --test web/tests/sse_frame_contract.test.mjs
```

全量测试：

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

验收原则：

- 新增测试全部通过。
- 现有相关测试无新增失败。
- 全量测试与实施前基线比较。
- 环境性失败必须有可复现证据，不能直接标记为通过。

## P5：真实任务冒烟测试

使用真实任务验证：

`/console?thread_id=console-b0b2a64a78c6`

前提：该任务目录仍存在，且对应 checkpoint 可读取。原文所述的本机真实数据状态需要在实施机器上重新确认，不能把历史截图视为当前环境保证。

验证步骤：

1. 打开创建台历史任务查看模式。
2. 确认顶栏显示「AI 内容明细 →」。
3. 点击入口，确认打开新标签页。
4. 确认 URL 中 thread_id 正确。
5. 检查网页检索词、DuckDuckGo、来源域名和链接。
6. 检查模板 ID、候选、评分、正文。
7. 检查文案分组、旁白和 references。
8. 检查中文字幕条数和时间轴。
9. 检查英文字幕条数、内容和翻译状态。
10. 手动刷新，确认数据一致且没有触发工作流。
11. 使用另一个任务验证数据隔离。
12. 使用旧任务或失败任务验证缺失、跳过、失败状态。

验收：

- 五个区块与对应任务真实数据一致。
- 没有跨任务串数据。
- 没有将 stub 翻译冒充 real。
- 没有把 skipped 显示成 failed。
- 页面刷新不产生工作流副作用。

---

# 9. 原设计文档问题清单

| 编号 | 原文位置 | 问题 | 修订要求 |
|---|---|---|---|
| D01 | §4.1 | `web/monitor_fastapi.py` 路径不存在 | 改为根目录 `monitor_fastapi.py` |
| D02 | §3 | 模板 description、score 字段层级假设错误 | 从 `selected_template` / `candidates` 读取 |
| D03 | §3、§4 | 检索缺文件统一判 missing | 结合 skipped/failed 标签和任务状态 |
| D04 | §3 | 未区分 cache_hit | 增加缓存命中状态 |
| D05 | §3 | checkpoint 缺字段的语义不清 | 结合任务状态和 status_log |
| D06 | §3 | `subtitle_translation_status=skipped` 语义不完整 | 显示跳过原因，不当作翻译失败 |
| D07 | §4.3、§7 | 假定 Node 测试 glob 会被 pytest 包装器执行 | 显式增加新测试包装 |
| D08 | §5 | 未明确线程 ID 和任务目录双重校验 | 复用现有验证和路径归属检查 |
| D09 | §3、§6 | artifact 结构损坏处理不完整 | 增加 invalid 状态 |
| D10 | §6 | 动态内容安全渲染要求不足 | 禁止不安全 innerHTML，限制 URL 协议 |
| D11 | §7 | 仅验证整体 200，未充分区分任务不存在 | 任务不存在应返回 404 |
| D12 | §7 | 冒烟任务的历史数据状态未经当前环境复核 | 实施前确认目录和 checkpoint |
| D13 | §4.2 | 仅说明创建成功后显示入口，未明确查看模式和创建模式状态转换 | 明确 href 更新规则 |
| D14 | §7 | 缺少跨任务隔离测试 | 增加两个 thread_id 的数据隔离用例 |
| D15 | §7 | 缺少恶意 URL、HTML 注入和超大数据测试 | 增加安全与规模边界测试 |
| D16 | §6 | 未充分处理运行中任务 | 增加 pending 状态和手动刷新 |

---

# 10. 不应实施的改动

本功能不应：

1. 修改 LangGraph 节点执行顺序。
2. 修改文案生成算法。
3. 修改网页检索算法。
4. 修改模板召回或排序算法。
5. 修改中文字幕生成逻辑。
6. 修改英文翻译逻辑。
7. 修改 `STATE_SUMMARY_KEYS`，除非后续确实提出实时事件展示需求。
8. 新建第二套 checkpoint 访问层。
9. 新建另一套任务目录规则。
10. 为展示页面重新执行工作流。
11. 修改或重写历史任务 artifact。
12. 让前端传递任意本机文件路径。

本次是**只读展示功能**，不应借机重构现有工作流。

---

# 11. 建议提交顺序

建议按以下顺序拆分提交：

| 提交 | 内容 | 前置条件 |
|---|---|---|
| Commit 1 | 后端 service + API + 后端测试 | P0 契约冻结 |
| Commit 2 | `/ai-content` 页面路由 + 页面静态文件 | Commit 1 |
| Commit 3 | 创建台入口 + API 路径表 + 前端纯逻辑 | Commit 2 |
| Commit 4 | 前端测试接入 + 全量回归 + 冒烟验证 | Commit 3 |

每个提交必须：

- 保持改动范围单一。
- 执行对应测试。
- 检查 diff。
- 不混入无关重构。
- 保留可回滚边界。

---

# 12. 最终验收标准

全部满足才可标记完成：

- [ ] 创建台顶栏正确显示「AI 内容明细 →」。
- [ ] 无 thread_id 时不允许进入错误任务页面。
- [ ] 创建成功后入口指向新建任务。
- [ ] 历史任务查看模式入口正确。
- [ ] `/ai-content` 路由可正常访问。
- [ ] API 只读取当前 thread_id 的数据。
- [ ] 三个 artifact 区块读取正确。
- [ ] 两个字幕区块读取正确。
- [ ] 搜索引擎和来源网站展示正确。
- [ ] 模板字段层级映射正确。
- [ ] real/stub/skipped 翻译状态准确。
- [ ] pending/missing/failed/unavailable/invalid 区分正确。
- [ ] 单区块读取失败不影响其他区块。
- [ ] 不存在跨任务数据污染。
- [ ] 不存在路径穿越。
- [ ] 不存在 HTML 注入和不安全 URL。
- [ ] 新增 Node 测试已接入 pytest。
- [ ] 后端新增测试全部通过。
- [ ] 前端新增测试全部通过。
- [ ] 全量回归无新增失败。
- [ ] 真实任务冒烟测试通过。
- [ ] 页面刷新不触发工作流执行。
- [ ] 服务重启后新路由正常生效。

---

# 13. 本次审查的执行边界与后续步骤

本次只完成 GitHub `main` 分支代码和指定文档的静态核对，并形成修订方案。

**尚未执行：**

- 未修改 GitHub 仓库。
- 未创建或修改项目源代码。
- 未提交 Git commit。
- 未运行本地 pytest / Node 测试。
- 未访问用户本机任务目录。
- 未重启 FastAPI 服务。

下一步应按 P0 → P1 → P2 → P3 → P4 → P5 顺序执行。

涉及文件写入、删除、服务重启或 Git 提交时，应先列出具体文件、命令、影响范围和回滚方式，取得明确确认后再执行。
'''

path = Path("/mnt/data/auto-video-editor_AI内容明细_设计审查及修订执行计划_20261119.md")
path.write_text(content, encoding="utf-8")
print(f"已生成：{path}\n文件大小：{path.stat().st_size} bytes")

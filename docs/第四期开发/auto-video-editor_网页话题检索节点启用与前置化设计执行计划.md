# 网页话题检索节点启用与前置化设计、执行计划

> 项目：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 核对日期：2026-11-10  
> 目标节点：`storyline_search_web_topic`（网页话题检索）  
> 目标位置：`storyline_group_clips` 之后、`storyline_generate_script` 之前  
> 文档性质：源码核查、目标设计、分阶段执行计划与验收方案  
> 本次处理范围：生成设计文档；未修改仓库代码、未变更 GitHub 分支、未在用户机器上设置环境变量。

---

## 1. 目标与结论

本次需求不是单纯把节点开关设为 `true`，而是要把网页检索节点从现有的独立旁支改造成生成文案前的正式前置节点，形成完整的数据链路：

1. 视频先经过镜头切分、画面理解、片段筛选和分组。
2. 网页检索节点读取前序视频分析结果，自动提取搜索主题和关键词。
3. 节点访问互联网搜索相关资料，并把搜索摘要、标题、来源 URL 等结构化保存。
4. 网页检索节点完成后，将资料交给 `storyline_generate_script`。
5. 文案生成节点同时使用视频分析结果、片段分组和网页资料生成文案。
6. 搜索失败或无结果时，保留可观测错误信息，但不应导致整条视频剪辑主流程失败。

**当前 `main` 分支尚未满足以上要求。** 开关默认值虽然已经是开启状态，但实际调度条件、拓扑位置、检索输入和文案上下文都存在缺口。必须调整 LangGraph 图边和节点的数据契约，而不是只改一个环境变量。

---

## 2. 两张截图的现状与目标位置

### 2.1 第一张截图：生成文案节点

截图中 `生成文案`（`storyline_generate_script`）节点的当前依赖是：

- `storyline_group_clips`

其后续节点包括：

- `storyline_generate_ai_transition`
- `storyline_generate_voiceover`
- `storyline_plan_timeline_pro`
- `storyline_recommend_text`
- `storyline_script_template_recommendation`

这说明当前文案生成节点直接由片段分组节点触发。目标是把网页检索节点放到这两个节点之间：

```text
storyline_group_clips
        |
        v
storyline_search_web_topic
        |
        v
storyline_generate_script
        |
        +--> storyline_generate_voiceover
        +--> storyline_select_bgm
        +--> storyline_recommend_text
        +--> storyline_script_template_recommendation
        +--> 其他既有后续路径
```

调整后，`storyline_generate_script` 的直接依赖应从 `storyline_group_clips` 改为 `storyline_search_web_topic`（检索开关关闭时，允许通过条件路由直接跳过检索并进入文案生成）。

### 2.2 第二张截图：网页话题检索节点

截图中的 `网页话题检索`（`storyline_search_web_topic`）当前显示：

- `DEPENDENCIES`：`storyline_load_media`
- `RUN STATUS`：`—`
- `NEXT NODES`：`—`

这与当前源码一致：它目前是 `storyline_load_media` 后面的并行旁支，没有到 `storyline_generate_script` 的下游边。截图显示 `RUN STATUS —` 代表本次运行没有调度到该节点，不等同于节点代码不存在。

### 2.3 调整后的节点卡片预期

| 节点 | Dependencies 预期 | Next Nodes 预期 |
|---|---|---|
| 网页话题检索 `storyline_search_web_topic` | `storyline_group_clips` | `storyline_generate_script` |
| 生成文案 `storyline_generate_script` | `storyline_search_web_topic` | 保持现有文案后续节点 |

---

## 3. `main` 分支源码核查

本节结论以本次读取的 `main` 分支源码为依据。源码链接列在文档末尾。

### 3.1 开关已经存在，默认值为开启

`config.py` 中已有：

```python
STORYLINE_ENABLE_WEB_TOPIC: bool = (
    os.environ.get("STORYLINE_ENABLE_WEB_TOPIC", "true").lower().strip()
    in ("1", "true", "yes")
)
```

因此：

- 未设置环境变量时，默认是 `true`。
- 设置为 `1`、`true` 或 `yes` 时开启。
- 设置为其他值（例如 `0`、`false`）时关闭。
- `graph.py` 的路由函数读取 `config.STORYLINE_ENABLE_WEB_TOPIC`。

**结论：当前问题不能简单归因为开关默认关闭。** 即使开关为 `true`，还必须满足 `storyline_targets.query` 非空，节点才会被触发。

### 3.2 当前触发条件只接受手动 `query`

当前 `graph.py` 的条件函数逻辑等价于：

```python
return bool(
    config.STORYLINE_ENABLE_WEB_TOPIC
    and web_topic_query(state)
)
```

`web_topic_query(state)` 只读取：

```python
state["storyline_targets"]["query"]
```

它不读取以下前序视频分析信息：

- `storyline_understanding_artifact`
- `storyline_groups_artifact`
- 画面整体概述 `overall`
- 分组主题 `groups[].theme`

因此，当前节点不能根据视频画面分析自动生成搜索关键词。

### 3.3 `/console` 创建任务可能天然不满足当前触发条件

项目 README 对创建台的输入映射说明：

- 用户提示词写入 `storyline_targets.user_request/script_request`。
- 创建台不会把用户提示词写入 `storyline_targets.query`。

而当前网页检索路由只检查 `storyline_targets.query`。因此，用户通过 `/console` 输入普通剪辑提示词创建任务时，即使检索开关为 `true`，也可能因为 `query` 为空而跳过网页检索节点。

这是截图中该节点显示未执行状态的重要原因之一。

### 3.4 当前图拓扑把网页检索放在错误位置

当前 `graph.py` 的主图关系为：

```text
START
  |
  v
storyline_load_media
  |
  +-----------------------------+
  |                             |
  v                             v
storyline_split_shots   storyline_search_web_topic
  |                             |
  v                             v
storyline_understand_clips     END
  |
  v
storyline_filter_clips
  |
  v
storyline_group_clips
  |
  v
storyline_generate_script
```

当前的 `_storyline_route_after_load_media()` 会根据开关和 `query` 返回一个或两个分支：

- 主分支：`storyline_split_shots`
- 条件旁支：`storyline_search_web_topic`

检索节点没有连接到 `storyline_generate_script`，最终成为独立旁支。现有集成测试也明确把它定义为“死胡同分支”，并验证它的唯一出边是 `END`。

### 3.5 当前检索节点只搜索 `query`，没有使用视频分析结果

`nodes/storyline/node_search_web_topic.py` 当前读取：

```python
targets = state.get("storyline_targets") or {}
query = (
    (targets.get("query") if isinstance(targets, dict) else None) or ""
).strip()
```

随后调用能力层：

```python
cap_topic(
    query=query,
    max_results=10,
    provider="duckduckgo_html",
)
```

该节点没有读取 `storyline_understanding_artifact` 或 `storyline_groups_artifact`，所以它无法根据分析画面的主题检索。

### 3.6 当前检索能力层只提供标题和 URL，没有给出足够的资料摘要

`storyline_capabilities/search_web_topic.py` 当前采用 DuckDuckGo HTML 页面进行检索：

- 请求地址：`https://duckduckgo.com/html/`
- HTTP 超时：15 秒
- 默认最多返回 10 条结果
- 解析字段主要是 `title`、`url`、`source`
- 当前 HTML 解析器没有稳定提取搜索摘要（snippet）。
- 当前节点调用时没有启用网页正文下载。

因此，当前检索结果不能等同于可供文案模型使用的完整参考资料。

此外，底层请求异常或未匹配到搜索结果时，能力层会返回空结果结构。节点需要将这些状态明确写入产物与监控日志，不能只凭节点显示绿色或 `done` 就认为检索到了资料。

### 3.7 当前生成文案节点只注入网页标题

`nodes/storyline/node_generate_script.py` 当前读取：

- 视频分析产物 `storyline_understanding_artifact`
- 网页检索产物 `storyline_web_topic_artifact`

但 `format_web_topic_reference()` 仅提取最多 5 个标题，每个标题最多 80 个字符，不带 URL，也没有摘要正文。随后它把标题拼入 `overall`，交给 `generate_script()`。

这存在两个问题：

1. **资料不足**：模型拿到的主要是搜索标题，而不是检索摘要或可核对事实。
2. **上下文混淆**：`overall` 原本表示视频素材整体概述，当前又混入了网页标题。视频分析和外部研究资料没有独立字段。

建议新增独立的 `web_research` 输入，不要继续把网页资料全部塞进 `overall`。

### 3.8 当前状态字段可以复用

`state.py` 已定义：

```python
storyline_web_topic_artifact
```

该字段用于保存检索结果 JSON 的路径。第一阶段无需为了这次需求新增重复的 artifact 路径字段。

但检索产物的 JSON 数据结构需要升级，并为旧结构保留兼容读取能力。

### 3.9 现有测试会锁定旧行为，必须同步调整

`tests/integration/test_unreachable_nodes_wiring.py` 当前包含以下旧约束：

- 网页检索节点的入边邻居是 `storyline_load_media`。
- 网页检索节点没有业务下游节点。
- 网页检索与 `storyline_split_shots` 在同一超步并行执行。
- 没有 `storyline_targets.query` 时不执行检索。
- 检索节点在 QA 重试回路外，只执行一次。

这些断言与本次新需求冲突。实施时必须修改相应测试，不能只改 `graph.py` 而保留旧测试语义。

---

## 4. 目标设计

### 4.1 目标拓扑

网页话题检索应使用视频理解与片段分组的产物，放在生成文案之前：

```mermaid
flowchart TD
    A([START]) --> B[storyline_load_media]
    B --> C[storyline_split_shots]
    C --> D[storyline_understand_clips]
    D --> E[storyline_filter_clips]
    E --> F[storyline_group_clips]

    F --> G{STORYLINE_ENABLE_WEB_TOPIC?}
    G -->|开启且存在可检索内容| H[storyline_search_web_topic]
    G -->|关闭或无有效输入| I[storyline_generate_script]
    H --> I

    F --> J[storyline_recommend_transition]
    F --> K[storyline_plan_timeline_pro 的既有汇合路径]
    I --> L[生成配音、BGM、字幕样式等既有并行路径]
```

说明：

- `storyline_load_media` 后不再启动网页话题检索旁支。
- `storyline_group_clips` 后通过条件路由决定是否执行网页检索。
- 开关开启且有有效检索输入时：`group_clips → search_web_topic → generate_script`。
- 开关关闭或没有有效检索内容时：`group_clips → generate_script`。
- `storyline_group_clips → storyline_recommend_transition` 等原有边保持不变。
- `storyline_generate_script` 后面的配音、BGM、文本推荐、模板推荐等分支保持不变。

### 4.2 为什么建议放在 `storyline_group_clips` 之后

`storyline_group_clips` 之前已经完成：

1. `storyline_load_media`：加载视频素材及媒体元数据。
2. `storyline_split_shots`：将视频切分为镜头片段。
3. `storyline_understand_clips`：分析画面，生成视频整体概述和片段理解结果。
4. `storyline_filter_clips`：筛选有效片段。
5. `storyline_group_clips`：将筛选后的片段组织成主题分组。

因此，在分组完成后，检索节点可以同时得到：

- 视频画面整体概述；
- 片段的主题分组；
- 用户显式指定的搜索主题（可选）。

相比只在 `storyline_load_media` 后搜索，这个位置拥有更丰富的语义信息，搜索目标也更贴近视频实际内容。

### 4.3 检索输入的数据来源

建议按以下优先级构建搜索内容：

| 数据来源 | State 字段 / 产物 | 使用方式 |
|---|---|---|
| 视频分析整体概述 | `storyline_understanding_artifact` 中的 `overall` | 提取视频核心主题、对象、场景和关键词 |
| 视频主题分组 | `storyline_groups_artifact` 中 `groups[].theme` | 补充具体主题和分支关键词 |
| 用户显式检索词 | `storyline_targets.query` | 作为优先主题或关键词补充 |
| 用户文案要求 | `storyline_targets.user_request/script_request` | 只提取与选题相关的主题提示，不直接把全部长提示词拼入搜索请求 |

**必须满足：即使 `storyline_targets.query` 为空，只要前序视频分析结果有效，仍然能够自动构造检索词并运行网页检索。**

建议构造结果示例：

```text
用户检索提示：可选
视频整体主题：夏季户外徒步，强日照环境，人物进行长时间运动
片段主题：户外防晒、补水、运动装备
最终检索词：夏季户外徒步防晒措施、运动补水与高温环境注意事项
```

这只是数据流示例，不是固定写死的业务主题。

### 4.4 检索节点输出契约

建议继续写入：

```text
outputs/<thread_id>/storyline/search_web_topic.json
```

建议产物结构：

```json
{
  "version": 1,
  "provider": "duckduckgo_html",
  "status": "ok",
  "query": "由视频分析自动生成的检索词",
  "query_hash": "sha256:...",
  "searched_at": "2026-11-10T00:00:00+08:00",
  "search_web_topic": [
    {
      "title": "资料标题",
      "url": "https://example.com/source",
      "snippet": "搜索结果摘要或经过长度限制的页面摘要",
      "source": "duckduckgo_html"
    }
  ],
  "error_code": null,
  "message": ""
}
```

字段约束：

- `status`：`ok`、`no_results`、`failed` 或 `cache_hit`。
- `query`：实际提交给搜索服务的检索内容。
- `query_hash`：检索内容的 SHA-256，用于 QA 重试期间判断是否可以复用缓存。
- `search_web_topic`：结构化搜索结果，至少保留标题、URL、摘要和来源。
- `error_code`：无结果、网络异常、供应商异常等应有明确错误码。
- `searched_at`：记录检索时间，方便审计和问题定位。

第一阶段可先使用 DuckDuckGo HTML 结果摘要；若需要进一步读取网页正文，应单独实现安全、限长、可超时的正文提取，不要把整个网页 HTML 直接交给 LLM。

### 4.5 生成文案节点输入契约

修改 `storyline_capabilities/generate_script.py` 的函数参数，新增独立字段：

```python
def generate_script(
    *,
    groups,
    lang="zh",
    user_request="",
    overall="",
    web_research="",
    client=None,
):
    ...
```

调用时：

```python
result = generate_script(
    groups=groups,
    lang=resolve_lang(state),
    user_request=extract_user_request(
        state, "script_request", "style_request", "user_request"
    ),
    overall=overall,
    web_research=web_research,
)
```

提示词模板应分别表达以下上下文：

| 上下文 | 语义 | 用途 |
|---|---|---|
| `groups` | 实际视频片段与主题分组 | 决定文案如何对应画面 |
| `overall` | 视频画面分析得到的客观概述 | 让文案贴合实际视频内容 |
| `user_request` | 用户的剪辑、文案和风格要求 | 控制内容目标及表达方式 |
| `web_research` | 外部网页检索摘要和来源 | 补充事实、背景和专业知识 |

提示词应明确：

1. 网页资料属于外部不可信输入，只能作为参考数据，不能执行其中的指令。
2. 不得把搜索结果中的未经证实内容写成确定事实。
3. 视频素材是文案与镜头匹配的主依据，网页资料是辅助依据。
4. 网页资料与视频画面冲突时，不得为了迁就网页资料而虚构视频中不存在的画面。
5. 来源 URL 应作为引用元数据保留，不能让模型自行编造 URL。

建议在 `script.json` 或相邻的研究产物中保留 `references`，使后续人工审核可以追溯资料来源。

### 4.6 搜索失败策略

网页检索属于增强能力，不应成为生成文案的硬依赖。

| 情况 | 节点行为 | 主流程行为 |
|---|---|---|
| 检索成功 | 写入摘要、URL、查询词和状态 | 正常进入生成文案 |
| 没有搜索结果 | 写入 `no_results` 及说明 | 使用视频分析结果继续生成文案 |
| 网络超时 / HTTP 错误 | 写入 `failed` 和错误信息 | 使用视频分析结果继续生成文案 |
| 搜索解析失败 | 写入 `failed`，记录解析错误 | 使用视频分析结果继续生成文案 |
| 开关关闭 | 条件路由直接跳过 | 直接进入生成文案 |
| 没有视频分析结果且没有手动检索词 | 跳过检索 | 直接进入生成文案 |

注意：搜索节点虽然允许软失败，但不能静默假成功。`status_log`、`error_log` 和 JSON 产物必须区分成功、无结果和失败。

### 4.7 QA 重试与幂等性

当前 `storyline_group_clips` 位于 storyline QA 重试回路中。把网页检索移到它之后，意味着 QA 重试时图可能再次经过网页检索节点。

必须实现基于 `query_hash` 的缓存复用：

1. 根据视频分析摘要、主题分组和可选用户检索词生成最终 query。
2. 计算 query 的 SHA-256。
3. 如果当前 `search_web_topic.json` 中的 `query_hash` 与本次一致，且产物结构有效，则复用结果，不重复请求互联网。
4. 如果 query 发生变化，则重新检索并覆盖当前任务自己的产物文件。
5. 每次节点执行仍要写固定的状态标签，不能把搜索条数、主题数量等动态数值拼入 `status_log` 标签。

这样既能满足“检索发生在生成文案之前”，也能避免 QA 重试过程中重复访问搜索服务。

### 4.8 LangGraph fan-in 屏障约束

现有 `graph.py` 存在 `storyline_group_clips`、`storyline_generate_script`、`storyline_generate_voiceover`、`storyline_select_bgm` 汇入 `storyline_plan_timeline_pro` 的 fan-in 屏障。

本次修改应：

- 保留 `storyline_group_clips` 到 `storyline_plan_timeline_pro` 屏障的既有依赖。
- 保留文案、配音、BGM 等既有屏障关系。
- 不要把网页检索节点额外添加到任何 fan-in 屏障的源列表。
- 不要把网页检索节点加入 AI 转场的既有屏障。

原因：网页检索是条件触发节点。若把它放进必须等待所有来源完成的 fan-in 屏障，而某些执行路径又跳过它，屏障可能永远无法满足，造成图提前结束或节点不再触发。现有源码和 D10 测试已经明确保护这一约束。

---

## 5. 分阶段执行开发任务

| 阶段 | 任务 | 主要修改位置 | 交付物 / 完成标准 |
|---|---|---|---|
| 阶段 0：基线与契约冻结 | 确认当前开关、图边、State 字段、检索产物格式；记录当前拓扑与测试基线 | `config.py`、`graph.py`、`state.py`、`tests/integration/test_unreachable_nodes_wiring.py` | 基线记录；明确新旧节点依赖和输出字段 |
| 阶段 1：自动构造检索词 | 读取视频理解 `overall`、分组主题和可选 `storyline_targets.query`；新增统一 query builder | `nodes/storyline/_common.py` 或独立检索查询 helper；`node_search_web_topic.py` | `query` 为空时也能从视频分析构造检索词 |
| 阶段 2：增强检索结果 | 解析标题、URL、摘要；添加状态、错误码、时间和 query hash；增加超时、限长和异常留痕 | `storyline_capabilities/search_web_topic.py` | `search_web_topic.json` 有可供 LLM 使用的结构化摘要和来源 |
| 阶段 3：调整 LangGraph 拓扑 | 删除 `load_media` 后的网页检索并行旁支；在 `group_clips` 后使用条件路由；接通 `search_web_topic → generate_script` | `graph.py` | 开启时检索节点成为文案的直接前驱；关闭时可直接跳过 |
| 阶段 4：接入文案 Prompt | 新增独立 `web_research` 参数；修改模板变量；保留视频概述、用户要求和网页资料的语义边界 | `node_generate_script.py`、`storyline_capabilities/generate_script.py`、`generate_script` 对应提示词模板 | LLM 请求中能看到摘要、URL 和资料说明，不再只传标题 |
| 阶段 5：监控图与状态验证 | 更新节点说明、依赖、后续节点和图 schema 版本；检查自动布局缓存是否刷新 | 监控图 schema / 节点 metadata / 拓扑版本 | 监控页面显示检索节点位于生成文案之前 |
| 阶段 6：测试与回归 | 更新旧测试，增加自动 query、成功、无结果、失败、关闭开关、重试缓存和 Prompt 输入测试 | `tests/unit/`、`tests/integration/test_unreachable_nodes_wiring.py` | 新测试全部通过，原 QA、AI 转场、checkpoint 恢复不回归 |
| 阶段 7：真实环境验收 | 使用真实视频运行，观察检索请求、产物、文案和监控拓扑 | `outputs/<thread_id>/storyline/`、监控服务 | 证明视频分析结果实际进入搜索，搜索摘要实际进入 LLM 文案请求 |

### 阶段 1：自动构造检索词

具体任务：

- 从 `storyline_understanding_artifact` 读取 JSON，提取 `overall`。
- 从 `storyline_groups_artifact` 读取 `groups[].theme`。
- 将 `storyline_targets.query` 作为可选搜索提示，而不是运行前置条件。
- 对空值、类型异常、超长输入进行清洗。
- 控制搜索词长度，避免将完整用户提示词或无关原文直接发送给外部搜索服务。
- 不向搜索服务发送视频二进制、视频本地路径、任务目录路径或完整内部 state。

验收条件：

- 不提供 `storyline_targets.query`，但 `overall` 有内容时，检索节点仍可运行。
- `overall` 为空但 group theme 有内容时，可由主题构造 query。
- 手动 `query` 与视频分析结果同时存在时，最终检索词能够兼顾两者。
- 所有输入均为空时，才允许跳过检索。

### 阶段 2：增强检索结果

具体任务：

- 在 DuckDuckGo HTML 解析器中增加摘要提取。
- 保留 `title`、`url`、`snippet`、`source`。
- 检查 URL 仅允许 `http` / `https`。
- 控制最大结果数量、摘要长度和总上下文长度。
- 统一输出 `status`、`error_code`、`message`。
- 记录检索时间和 query hash。
- 网络访问异常要可观察，但不得破坏文案主流程。

建议默认限制：

| 项目 | 建议初始上限 |
|---|---:|
| 搜索结果数量 | 10 条 |
| 注入文案的资料数量 | 5 条 |
| 单条摘要长度 | 600 字符 |
| 网页资料总长度 | 4,000 字符 |
| 单次搜索超时 | 15 秒 |

上述数值是初始工程护栏，后续可根据实际 LLM 上下文长度及检索质量调整。

### 阶段 3：调整 LangGraph 拓扑

核心修改方向：

1. 移除 `storyline_load_media` → `_storyline_route_after_load_media` 中的网页检索 fan-out。
2. `storyline_load_media` 后固定进入 `storyline_split_shots`。
3. 保留 `split_shots → understand_clips → filter_clips → group_clips` 主链。
4. 将 `storyline_group_clips` 到 `storyline_generate_script` 的直接关系改为条件路由。
5. 开关开启且存在有效分析输入时，路由到 `storyline_search_web_topic`。
6. 开关关闭或无有效检索输入时，路由到 `storyline_generate_script`。
7. 增加 `storyline_search_web_topic → storyline_generate_script`。
8. 保留 group_clips 的其他输出边及原有 fan-in 屏障。

建议伪代码：

```python
def route_after_group_clips(state: WorkflowState) -> str:
    import config

    if not config.STORYLINE_ENABLE_WEB_TOPIC:
        return "storyline_generate_script"

    if not has_web_topic_input(state):
        return "storyline_generate_script"

    return "storyline_search_web_topic"
```

对应图边：

```python
g.add_conditional_edges(
    "storyline_group_clips",
    route_after_group_clips,
    {
        "storyline_search_web_topic": "storyline_search_web_topic",
        "storyline_generate_script": "storyline_generate_script",
    },
)

g.add_edge(
    "storyline_search_web_topic",
    "storyline_generate_script",
)
```

以上是拓扑设计示意，实际实施时应沿用仓库现有的节点注册方式、类型标注和测试习惯。

### 阶段 4：接入文案 Prompt

具体任务：

- 新增 `web_research` 参数，不再把网页标题合并进 `overall`。
- 在 `generate_script` 用户提示词模板中增加 `web_research`。
- 保留来源标题和 URL。
- 在 prompt 中明确外部网页内容是不可信参考文本。
- LLM 生成的文案不得把来源中没有的事实或 URL 当成真实依据。
- 搜索为空或失败时传入空字符串，保持文案生成能力可用。

### 阶段 5：监控图与状态验证

监控页面应能从编译后的 LangGraph 图中看到：

- 网页话题检索节点的依赖是 `storyline_group_clips`。
- 网页话题检索节点的下一节点是 `storyline_generate_script`。
- 生成文案节点的依赖是 `storyline_search_web_topic`（启用检索时）。
- 原有文案后续节点仍然存在。
- 搜索执行时有执行中状态，成功时为完成状态；失败或无结果时有明确状态与错误说明。

项目 README 说明图布局缓存 key 包含 `workflow_id + mode + schema_version`。因此，若拓扑 schema 有版本号，应同步提升版本，避免浏览器继续使用旧图布局缓存。

### 阶段 6：更新测试

必须修改：

`tests/integration/test_unreachable_nodes_wiring.py`

需要移除或改写以下旧断言：

- `storyline_search_web_topic` 入边必须是 `storyline_load_media`。
- `storyline_search_web_topic` 必须是死胡同。
- `storyline_search_web_topic` 必须与 `storyline_split_shots` 同一超步运行。
- `query` 为空时一定不运行网页检索。
- 检索节点必须位于 QA 重试回路上游且始终只运行一次。

必须新增以下测试：

| 测试场景 | 预期结果 |
|---|---|
| 默认开关开启、`query` 为空、视频 `overall` 有内容 | 自动构造 query，检索节点运行 |
| 默认开关开启、仅 group theme 有内容 | 自动构造 query，检索节点运行 |
| 手动 query 和视频分析内容同时存在 | 检索 query 同时参考两类信息 |
| 开关关闭 | 检索节点跳过，文案节点仍运行 |
| 检索成功 | 产物含摘要、URL、query hash |
| 检索无结果 | 状态为 `no_results`，文案仍运行 |
| 检索网络异常 | 状态为 `failed`，文案仍运行 |
| 文案节点执行 | `web_research` 参数包含实际摘要和来源 URL |
| 恶意网页文本 | 不会覆盖系统提示词指令，不会破坏模板变量 |
| QA 重试且 query 相同 | 使用缓存，不重复发起网络请求 |
| QA 重试且 query 变化 | 按新的 query hash 重新检索 |
| AI 转场开关开/关 | 原有 AI 转场 fan-in 关系不变 |
| Checkpoint 恢复 | 既有人工关卡和 PostgreSQL checkpoint 恢复不回归 |

同时继续验证以下不变量：

- 条件节点不能进入 fan-in 屏障。
- QA 重试仍能重新触发 `storyline_plan_timeline_pro`。
- AI 转场两个节点与原计划节点同频运行。
- 工作流最终仍能正常到达关卡①或既定结束节点。

---

## 6. 开关配置与运行方式

### 6.1 当前默认配置

目前 `STORYLINE_ENABLE_WEB_TOPIC` 默认就是 `true`。若本机或服务环境此前显式设置为 `0`，需要覆盖为开启状态。

Windows PowerShell：

```powershell
$env:STORYLINE_ENABLE_WEB_TOPIC = "1"
```

### 6.2 使用生产启动脚本

必须在启动 Python 工作流进程前设置环境变量：

```powershell
$env:STORYLINE_ENABLE_WEB_TOPIC = "1"
.\start_production.ps1 -ThreadId web-topic-test-001
```

### 6.3 使用 Agent Gate 入口

```powershell
$env:STORYLINE_ENABLE_WEB_TOPIC = "1"

python -m agent_gate drive `
  --thread-id web-topic-test-002 `
  --outputs-root outputs `
  --agent mcode `
  --project-dir "E:\Documents\kuaishou\video-ops" `
  --video "E:\Documents\kuaishou\video-ops\inputs\30s.mp4"
```

请按本机实际项目目录和素材路径替换示例路径；不要直接复制不存在的路径。

### 6.4 环境变量生效注意事项

`config.py` 在 Python 模块导入时读取环境变量。修改当前 PowerShell 的环境变量不会自动改变已经运行的 Python 进程中的配置对象。

因此：

- 修改环境变量后，需要启动新的工作流 Python 进程。
- 已经启动的工作流不会因为浏览器刷新而重新读取环境变量。
- `STORYLINE_ENABLE_WEB_TOPIC` 目前是运行时路由读取的配置常量，但其环境变量值仍是在模块初始化时解析的。

---

## 7. 验收方案

### 7.1 静态拓扑验收

查看监控图：

```text
http://127.0.0.1:7860/node-map
```

刷新图 schema 后，重点检查：

| 验收项 | 期望 |
|---|---|
| `storyline_search_web_topic` 入边 | `storyline_group_clips` |
| `storyline_search_web_topic` 出边 | `storyline_generate_script` |
| `storyline_generate_script` 入边 | 启用检索时为 `storyline_search_web_topic` |
| `storyline_generate_script` 后续节点 | 与原有拓扑一致 |
| `storyline_search_web_topic` 是否出现在 load_media 并行分支 | 不应出现 |

### 7.2 自动化测试

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_unreachable_nodes_wiring.py -v
```

再运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit -q
```

测试通过的标准不是只看退出码，还要确认：

- 没有因依赖缺失而把关键测试全部 skip。
- 日志没有出现测试路径、外部搜索请求或节点拓扑相关异常。
- QA 重试用例没有提前结束。
- AI 转场和原有 checkpoint 恢复用例没有回归。

### 7.3 真实视频运行验收

使用一个有明确主题、画面内容可识别的测试视频启动一次完整工作流。

检查产物：

```text
outputs/<thread_id>/storyline/understanding.json
outputs/<thread_id>/storyline/groups.json
outputs/<thread_id>/storyline/search_web_topic.json
outputs/<thread_id>/storyline/script.json
outputs/<thread_id>/storyline/script_llm_trace.json
```

需要验证：

1. `understanding.json` 中的 `overall` 是真实视频画面的分析结果。
2. `groups.json` 中的主题与视频片段相符。
3. `search_web_topic.json` 的 `query` 能体现视频分析主题，而不是固定关键词。
4. 检索结果包含可读摘要和来源 URL。
5. `script_llm_trace.json` 中的 LLM 请求上下文包含 `web_research`。
6. `script.json` 中生成的文案与视频分组匹配，并能合理吸收网页资料。
7. 搜索来源可以被人工打开核对，不能出现模型编造的来源 URL。

### 7.4 监控执行顺序验收

一次正常执行中，节点顺序应能观察到：

```text
storyline_load_media
storyline_split_shots
storyline_understand_clips
storyline_filter_clips
storyline_group_clips
storyline_search_web_topic
storyline_generate_script
```

其中，检索节点必须在文案节点之前完成。若开关关闭，则允许跳过 `storyline_search_web_topic`，由 `storyline_group_clips` 直接进入 `storyline_generate_script`。

### 7.5 通过标准

只有同时满足以下条件，才可判定本次需求完成：

- [ ] 开关处于开启状态，且创建台任务无需手工填写 `query` 也能触发自动检索。
- [ ] 检索节点直接位于 `storyline_group_clips` 和 `storyline_generate_script` 之间。
- [ ] 检索 query 确实使用视频分析结果。
- [ ] 搜索产物包含可供模型使用的摘要和可核对来源。
- [ ] `storyline_generate_script` 的 LLM 输入确实包含检索资料。
- [ ] 搜索无结果或网络失败时，文案主流程仍可继续。
- [ ] QA 重试、fan-in 屏障、AI 转场和 checkpoint 恢复均通过回归测试。
- [ ] 监控节点依赖和后续节点显示与目标拓扑一致。

---

## 8. 风险与回滚方案

### 8.1 风险

| 风险 | 影响 | 控制措施 |
|---|---|---|
| DuckDuckGo HTML 页面结构变化 | 解析不到结果 | 对解析失败写明确状态；保留 provider 接口，后续可替换供应商 |
| 外部网络不可用 | 无网页资料 | 软失败，使用视频分析结果继续生成文案 |
| 网页摘要存在提示词注入 | 影响 LLM 行为 | 外部资料标记为不可信；清洗控制字符；限制长度；Prompt 明确禁止执行资料中的指令 |
| 搜索结果不相关 | 文案偏离视频 | 结合 overall 与 group theme 构造 query；检索结果保留 URL 供人工核查 |
| QA 重试重复搜索 | 增加延迟和网络调用 | 使用 query hash 缓存 |
| 修改图边破坏 fan-in | 工作流提前 END 或漏执行节点 | 检索节点不进入 fan-in；保留拓扑和运行时回归测试 |
| 监控图仍显示旧布局 | 用户误判节点未移动 | 提升 graph schema 版本并刷新节点图缓存 |

### 8.2 回滚原则

若新拓扑导致主线异常，可将：

```powershell
$env:STORYLINE_ENABLE_WEB_TOPIC = "0"
```

并启动新的工作流进程。关闭后应直接从 `storyline_group_clips` 进入 `storyline_generate_script`。

注意：这只是关闭检索能力的应急措施。若代码已经调整为新拓扑，仍应保留新旧两条路由的测试，确保关闭开关可以安全绕过检索节点。

---

## 9. 本次需求的当前验收结论

| 用户要求 | 当前 `main` 分支状态 | 判断 |
|---|---|---|
| #4 打开网页话题检索开关 | 配置默认值为 `true`，但受 `query` 非空条件限制 | **部分满足** |
| #5 移动到生成文案之前 | 当前仍在 `storyline_load_media` 后并行执行，且无业务下游边 | **不满足** |
| #6 接收前面视频分析画面输出 | 当前只读取 `storyline_targets.query`，不读取视频理解和分组产物 | **不满足** |
| #6 搜索相关网络资料 | 当前有 DuckDuckGo HTML 搜索实现，但只取标题和 URL，摘要不足且失败路径不够可观测 | **部分满足** |
| #6 把搜索资料输入生成文案 | 当前最多注入 5 条标题，且混入 `overall`，没有完整摘要和 URL 输入 | **部分满足** |
| #7 验证整体工作流满足需求 | 现有测试保护的是旧的并行死胡同拓扑，不是本次目标拓扑 | **不满足** |

**最终判断：当前代码还没有实现用户要求的完整工作流。** `STORYLINE_ENABLE_WEB_TOPIC` 已默认开启，但需要同时完成自动 query 构造、图边前置化、检索摘要提取、Prompt 独立注入和测试更新，才能真正满足需求。

---

## 10. 源码依据

以下链接对应本次核查的 `main` 分支文件：

1. [README.md](https://github.com/wayyet/auto-video-editor/blob/main/README.md) — 当前架构、`/console` 输入映射、运行入口和监控说明。
2. [config.py](https://github.com/wayyet/auto-video-editor/blob/main/config.py) — `STORYLINE_ENABLE_WEB_TOPIC` 默认值及读取方式。
3. [graph.py](https://github.com/wayyet/auto-video-editor/blob/main/graph.py) — 当前网页检索旁支路由、节点注册和 LangGraph 边。
4. [state.py](https://github.com/wayyet/auto-video-editor/blob/main/state.py) — `storyline_web_topic_artifact`、视频理解和分组产物字段。
5. [nodes/storyline/node_search_web_topic.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_search_web_topic.py) — 当前检索节点输入与输出。
6. [storyline_capabilities/search_web_topic.py](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/search_web_topic.py) — DuckDuckGo HTML 请求和结果解析实现。
7. [nodes/storyline/node_generate_script.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_generate_script.py) — 当前读取网页检索 artifact 并注入 `overall` 的实现。
8. [storyline_capabilities/generate_script.py](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/generate_script.py) — 文案生成函数和 Prompt 参数。
9. [nodes/storyline/_common.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/_common.py) — 视频分析、主题查询和网页标题格式化 helper。
10. [tests/integration/test_unreachable_nodes_wiring.py](https://github.com/wayyet/auto-video-editor/blob/main/tests/integration/test_unreachable_nodes_wiring.py) — 当前旧拓扑、死胡同旁支、query 条件、QA 重试和 fan-in 屏障测试。
11. [nodes/storyline/node_understand_clips.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_understand_clips.py) — 视频画面理解产物。
12. [nodes/storyline/node_group_clips.py](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_group_clips.py) — 分组产物及主题字段。

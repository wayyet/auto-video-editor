# 用户剪辑要求逐项生效：设计审查与分阶段执行计划

- **审查日期**：2026-11-19
- **目标仓库**：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)
- **目标分支**：`main`
- **本次核对版本**：`e67376a8cee941f7b6914a76b4438aa383e8e2ab`（2026-11-18 18:16:26 UTC）
- **参考输入**：`auto-video-editor_输入剪辑要求逐条生效_链路分析与实施计划_20261119.md`
- **审查类型**：基于指定 commit 的源码静态审查；未修改仓库代码，未执行本机剪映真机验收
- **结论状态**：原文档核心诊断大体成立；原实施方案需修订后再执行

---

## 0. 执行摘要

### 0.1 总体结论

附件的核心判断基本正确：**创建台里的自由文本已经进入 LangGraph State，并被多个 Storyline 节点作为整体提示词使用；但系统没有把其中的编号指令解析成可验证的结构化动作。** 当前工作流主要根据固定节点能力运行，提示词对 LLM 的影响属于软提示，不等同于确定执行。

但附件中的实施方案不能原样执行。对 `main` 分支核对后，至少需要修订以下事项：

1. **开场转场的时间语义写得不准确**：`node_09` 的边界 0 是第 1 个镜头与第 2 个镜头之间的转场，不是视频时间轴上的 `0–3s` 转场。`node_09b` 才是在第一个视频段上挂载 `0–3s` 开场特效和大字标题的节点。
2. **不能把 `whip` 直接定义成“最炸”**：当前 `pool_whip` 是一个 109 项的甩镜/旋转/闪切/形变等动势转场池，源码没有强度评分或“全库最强”排序。可以把它定义为“强动势/甩镜风格池”，不能宣称它客观上是最强转场。
3. **开场加强逻辑不能依赖 `if semantics`**：当前 `node_09` 即使没有任何显式计划语义，也会对真实草稿边界使用默认池。附件示例在 `semantics=[]` 时不会覆盖边界 0，导致某些合法草稿漏执行用户要求。必须以实际草稿边界数为准。
4. **标题链路必须同时修复 LLM 输出和 State 写入**：中文/英文 Prompt 都要求模型输出 `title`，但 `generate_script()` 当前 JSON Schema 只声明 `group_scripts`，返回结果也只保留 `group_scripts` 等字段，`title` 没有进入返回值；`node_generate_script` 也没有写入 `state["hook_title"]`。
5. **标题长度处理不能改坏既有兼容行为**：`pick_hook_title()` 对显式 `state["hook_title"]` 只 `strip()`，不会截断；现有单测还专门验证“显式标题不截断”。新功能应在写入 State 之前规范化新解析的模板标题/LLM 标题，不应直接修改现有选择器的显式值语义。
6. **“先网上搜索”需要限定为“文案生成前搜索”**：当前网页检索在镜头分析、理解、筛选、分组之后，与文案模板推荐并行执行，再汇入文案生成；它不是工作流的第一个步骤。
7. **联网搜索当前仍是 DuckDuckGo HTML**：`node_search_web_topic` 将 `provider="duckduckgo_html"` 写死。附件关联文档中提到的 Google + SOCKS5(1088) 方案，不在本次附件内，也尚不能从当前 `main` 代码确认已经实施。
8. **附件提到的本地实跑产物不能独立复验**：本次只收到 MD 文档，没有收到 `outputs/console-b0b2a64a78c6/` 下的 JSON 产物。因此，161 字原文、文案第一句、搜索 `PARSE_ERROR` 等属于附件记载的运行证据，本次只能确认代码链路，不能重新读取这些实跑文件。

### 0.2 建议实施范围

本次建议按附件的四类标记实现最小闭环：

- `【主题】`：作为主题上下文，同时作为搜索词回退来源。
- `【检索】`：明确指定网页检索词，优先于 `【主题】`。
- `【开场】`：在第一个真实镜头边界强制使用 `whip` 语义，最终落到 `pool_whip`。
- `【标题】`：直接作为中文开场大字标题，优先于 LLM 自动标题，再优先于首条字幕切句。

**向后兼容规则建议收紧为：完全没有 `【】` 标记时，所有新增结构化动作都不触发，旧文本行为保持不变。** 因此，附件提出的“自由文本兜底识别开场加强”应从第一阶段移除；它会与“无标记行为完全一致”的承诺冲突。

---

## 1. 审查基线与证据边界

### 1.1 当前仓库版本

本次以 GitHub API 返回的 `main` 分支 commit 为核对基线：

`e67376a8cee941f7b6914a76b4438aa383e8e2ab`

对应提交时间为 `2026-11-18 18:16:26 UTC`。下文所说的“当前源码”均指该 commit，不以附件里可能早于该版本的行号作为唯一依据。

### 1.2 本次实际核对的源码范围

重点检查了以下模块：

| 层次 | 文件 | 本次核对内容 |
|---|---|---|
| 创建台 UI | `web/console.html` | `promptInput` 输入框、创建请求载荷 |
| 创建 API/服务 | `web/console_service.py` | `prepare_run()` 如何传递 `prompt` |
| 初始 State | `web/console_state.py` | `build_initial_state()` 的字段映射与冻结契约 |
| Storyline 公共方法 | `nodes/storyline/_common.py` | `extract_user_request()`、`build_web_topic_query()` |
| 文案链 | `nodes/storyline/node_generate_script.py`、`storyline_capabilities/generate_script.py`、Prompt 文件 | LLM 请求、JSON Schema、返回字段、State 更新 |
| 搜索链 | `nodes/storyline/node_search_web_topic.py`、`storyline_capabilities/search_web_topic.py` | 检索词、Provider、缓存与错误处理 |
| 转场链 | `nodes/node_09_inject_fx.py`、`templates/fx_template.json` | 边界语义、池映射、确定性资源选择 |
| 开场钩子 | `nodes/node_09b_inject_opening_hook.py`、`jy_common/hook_title.py` | 开场效果、标题来源、标题样式、长度语义 |
| 图拓扑/自动能力 | `graph.py`、`nodes/storyline/node_split_shots.py`、`storyline_capabilities/split_shots.py`、`storyline_capabilities/timeline_planner.py` | 节点顺序、切镜回退、网页检索所在阶段、卡点模式 |
| 回归测试 | `tests/unit/test_node_09_inject_fx.py`、`test_node_09b_inject_opening_hook.py`、`test_hook_title.py` 等 | 现有行为约束及新测试落点 |

### 1.3 不能从本次材料独立验证的事项

附件称以下文件来自任务 `console-b0b2a64a78c6`：

- `outputs/console-b0b2a64a78c6/initial_state.json`
- `outputs/console-b0b2a64a78c6/storyline/search_web_topic.json`
- `outputs/console-b0b2a64a78c6/storyline/script.json`

这些产物没有随本次对话上传。因此，本次审查不把“161 字逐字一致”“文案第一句具体内容”“本次搜索状态为 `PARSE_ERROR`”标成独立复验通过，而标为**附件提供的运行记录**。要完成运行层复验，需要将上述 JSON 产物一并纳入验证输入。

---

## 2. 当前实现：输入文本实际经过哪些环节

### 2.1 创建台到 WorkflowState：链路成立

当前代码链路如下：

```text
web/console.html
  textarea#promptInput
       │ readParams() 读取 prompt
       ▼
创建请求 /api/console/runs
       │
       ▼
web/console_service.py
  prepare_run(prompt=...)
       │
       ▼
web/console_state.py
  build_initial_state(prompt=...)
       │
       ├── storyline_targets.user_request = prompt
       └── storyline_targets.script_request = prompt
       │
       ▼
LangGraph WorkflowState
```

核对结论：

- UI 的 `promptInput` 确实被提交为 `prompt`。
- `prepare_run()` 将该值传入 `build_initial_state()`。
- `build_initial_state()` 将原始文本写入 `storyline_targets.user_request` 和 `storyline_targets.script_request`。
- 创建台明确不写 `storyline_targets.query`。`console_state.py` 的模块契约也说明了这一点：`query` 是手动检索词覆盖位，不应被提示词污染。

因此，**不能把当前问题归因于前端没提交或后端没保存**。主要问题是后续节点怎样消费这段文本。

### 2.2 当前确认读取用户文本的八个节点

当前源码中可确认以下八个节点从 `storyline_targets` 读取用户要求，并把它交给各自能力层：

| # | 节点 | 请求字段优先级/用途 | 实际作用边界 |
|---|---|---|---|
| 1 | `storyline_generate_script` | `script_request` → `style_request` → `user_request` | 把用户要求注入文案 LLM Prompt；用于主题、风格和文案约束 |
| 2 | `storyline_script_template_recommendation` | `script_request` → `style_request` → `user_request` | 参与文案模板检索 query；选中的模板作为文案风格/结构参考 |
| 3 | `storyline_filter_clips` | `filter_request` → `user_request` | 提示 LLM 如何筛选镜头；不是硬规则过滤器 |
| 4 | `storyline_group_clips` | `group_request` → `user_request` | 提示 LLM 如何分组；不是确定性分组约束 |
| 5 | `storyline_recommend_transition` | `transition_request` → `user_request` | 给转场语义推荐能力提供提示；最终草稿节点还会再按最终计划解析语义 |
| 6 | `storyline_recommend_text` | `text_request` → `font_request` → `user_request` | 给字幕/文字样式推荐提供提示 |
| 7 | `storyline_select_bgm` | `bgm_request` → `music_request` → `user_request` | 参与音乐情绪/风格选择；是否影响拍点取决于最终 BGM 数据和规划分支 |
| 8 | `storyline_generate_voiceover` | `voiceover_request` → `tts_request` → `user_request` | 参与配音生成要求；不等于能控制所有时间轴行为 |

`extract_user_request()` 是通用读取入口，但各节点传入的优先键不同。创建台目前只写 `user_request` 和 `script_request`，因此这些节点通常都会回退读取同一段完整输入文本。

**重要区别**：文本进入这些节点，只能证明它被当成 Prompt 上下文；不能证明 `#1`、`#2` 等编号已经被解析，也不能证明模型一定按每条要求产出可验收的结果。

### 2.3 网页检索：当前只从画面理解与分组构造 Query

当前 `build_web_topic_query(state)` 的组成顺序是：

1. `storyline_targets.query`（手动 Query，当前创建台不写）；
2. `storyline_understanding_artifact.overall` 的首句；
3. `storyline_groups_artifact.groups[].theme` 的前三个主题。

它**没有从 `user_request` 解析 `【检索】` 或 `【主题】`**。所以用户在输入框中写了旅游主题，并不意味着当前检索 Query 会自动包含该主题。

当前图拓扑中，`storyline_search_web_topic` 与文案模板推荐在 `group_clips` 之后并行执行，然后经双源屏障汇入 `generate_script`。因此准确描述是：

> 网页话题检索发生在素材分析、镜头理解、筛选和分组之后，在文案生成之前；不是整个工作流的第一个执行节点。

检索节点还有三个需要纳入设计的行为：

- `STORYLINE_ENABLE_WEB_TOPIC` 关闭时，节点软跳过，不发起网络请求。
- Query 为空时，同样软跳过，不写搜索产物。
- 同一 `query_hash` 的有效缓存会被复用；当前缓存路径也会复用 `failed` 结果并标记为 `cache_hit`。所以同一任务在 QA 重试时，不能假定搜索失败后会自动再次请求网络。

**Provider 状态**：`node_search_web_topic.py` 当前将 `provider="duckduckgo_html"` 写死；能力层也只实现 DuckDuckGo HTML 和 `stub`。附件提到的 Google + SOCKS5(1088) 属于关联计划中的决策描述，本次没有收到关联文档，且当前 `main` 尚不能证明已经切换到 Google。

### 2.4 开场转场：当前是“最终计划语义 → 真实资源池”

当前 `node_09_inject_fx.py` 不只是简单地把 `storyline_transition_plan` 直接映射到素材：

- `_boundary_semantics_plan()` 优先读取 `effective_canonical_plan(state)` 给出的最终批准计划；最终计划无效时再回退 `storyline_plan`；最后才会读取 `storyline_transition_plan`。
- 一个边界对应相邻两个视频段。边界 `i` 优先取前一个 clip 的 `transition_out`，否则取后一个 clip 的 `transition_in`。
- `templates/fx_template.json` 将语义映射到池 key，例如 `whip → pool_whip`。
- 资源池中具体的转场资源由 `TemplateLibrary` 解析；同一任务使用 `sha256(job_id|boundary_index|pool_key)` 计算稳定轮换位置，保证重放确定性和中英草稿的一致性。
- 转场资源挂在边界后的那个视频段，即 `segments[i+1]`。

当前 `pool_whip` 有 109 个资源名，包括旋转、拉伸、弹跳、快门、模糊、闪切、扭曲、冲屏、穿越等类型。源码没有为这些条目建立“视觉冲击强度”数值或排序，所以 `whip` 代表一个强动势风格类别，不能严格等价于“全库最炸”。

当前没有读取 `user_request` 并强制覆盖边界 0 的逻辑。因此，附件指出“用户文本不会直接控制首个边界转场”的方向正确；但其“只有两个层次”的描述不够完整，必须说明**最终批准计划优先于早期推荐计划**。

### 2.5 开场钩子：已有首段特效和大字标题能力

当前 `node_09b_inject_opening_hook.py` 已实现：

- 开场免费特效 `CCD闪光` 绑定到第一个视频段；
- 中文草稿生成 `track-text-hook` 标题轨；
- 标题字号 `12.0`、加粗、位于画面上半区；
- 标题显示时长为 `min(3 秒, 第一个视频段时长)`；
- 标题入场动画必须通过 GUI 实证和本地缓存双重检查；不满足时降级为纯标题，不伪装动画成功；
- 英文分支只做开场特效，不生成英文大字标题；
- `AUTO_VIDEO_EDITOR_OPENING_HOOK_ENABLED` 默认开启，关闭后整个节点只记 `noop:disabled`。

标题选择器 `pick_hook_title()` 的优先级是：

1. `state["hook_title"]` 显式值；
2. 第一条中文字幕，在第一个标点前切句并最多保留 12 个字符；
3. 无标题时返回 `None`，但开场特效仍可存在。

因此，附件把标题问题描述成“样式和能力都缺失”并不准确。**样式与标题轨能力已经存在，缺的是从用户模板/LLM 结果向 `state["hook_title"]` 提供标题值的生产链路。**

还有一个必须遵守的兼容点：当前 `pick_hook_title()` 对显式 `hook_title` 仅做 `strip()`，不截断；`tests/unit/test_hook_title.py` 已明确测试显式标题不截断。新功能不能在没有单独评审的情况下改变它。

### 2.6 自动切镜、画面理解和时间轴：已自动执行，但不是完全无输入

当前自动能力的准确边界如下：

- **切镜**：`storyline_split_shots` 默认优先使用 TransNetV2；Torch、模型代码或权重不可用，以及推理失败时，会回退帧差检测，并将回退原因写入日志，不应表述为“始终只用 TransNetV2”。
- **画面理解**：`storyline_understand_clips` 将镜头与媒体信息交给 `understand_clips` 能力，当前调用未把整段 `user_request` 作为显式参数传入。
- **时间轴**：`timeline_planner` 可按 TTS 时长对齐、BGM beat 对齐或目标时长等比规划；BGM beat 对齐依赖 `beat_period_ms` 等音乐数据，不是无条件保证每次都能卡点。
- **其他用户输入**：创建台的 `speed_multiplier`、目标时长、画幅比例、语言等结构化参数会进入 `storyline_targets`，并影响规划或后续处理。因此不能笼统说“时间轴纯数学且完全不受用户输入影响”。

结论：附件将 #4 定性为“已有自动能力，不需要新增自然语言指令解析”，总体方向合理；但验收不能把“节点运行过”当成“必然识别成功/必然精确卡点”。

---

## 3. 对附件原文逐项审查

| 附件原结论/方案 | 审查结论 | 修订意见 |
|---|---|---|
| 前端输入经后端进入 `storyline_targets.user_request/script_request` | **通过** | 当前源码仍符合；不需要修复传输链路 |
| 8 个节点将文本作为软提示 | **基本通过** | 八个节点均可从当前源码确认；应说明各节点的字段优先级和影响范围 |
| 主题文案第一句点题，因此 #1 生效 | **部分通过** | 源码确认文案 LLM 收到主题文本；但本次未取得原任务 `script.json`，不能独立复验该句文案 |
| 开头 3 秒“最炸转场”没有实现 | **方向通过，边界描述需改** | 首段已存在 `CCD闪光` 开场特效；缺的是首个镜头边界的转场加强。首个边界不保证位于视频 0–3 秒 |
| `pool_whip` 是五池中最炸的池 | **不通过/缺乏证据** | `pool_whip` 是强动势类别，当前有 109 个条目；没有强度量化、排序或“最强”依据 |
| 标题样式写死、`hook_title` 不可达 | **部分通过** | 标题样式确实固定；标准创建台初始 State 不写 `hook_title`，LLM `title` 又被能力层丢弃。但选择器已经能消费 `hook_title`，并非整个功能不可达 |
| LLM Prompt 已要求输出 `title`，只需接住它 | **部分通过** | Prompt 确有 `title`；但 JSON Schema 当前只声明 `group_scripts`，能力函数返回对象也不包含 `title`。需同时补 Schema、解析和返回值 |
| `hook_title` 已在 `WorkflowState` 声明，无需改 schema | **通过** | 该字段确已声明为 `NotRequired`；若只复用该字段，不增加新 State 键，通常无需拓扑/schema 版本升级 |
| `build_web_topic_query` 没用用户主题 | **通过** | 当前只取手动 query、画面概述首句、前三个分组主题 |
| 网页搜索在文案前完成 | **部分通过** | 确实在文案生成前，但发生在分组后，不是工作流最初阶段 |
| 本次 DDG 实跑失败、错误为 `PARSE_ERROR` | **附件记录，未独立复验** | 当前能力层确有 HTML 锚点解析失败时返回 `PARSE_ERROR` 的分支；原任务产物未提供 |
| 无 `【】` 标记时必须与旧行为完全一致 | **与附件后文矛盾** | 附件同时设计了自由文本开场识别，会改变无标记旧文本行为。建议第一阶段删除自由文本兜底 |
| `【标题】` 截断到 12 字 | **可实现，但要定义边界** | `HOOK_TITLE_MAX_LEN=12` 主要用于字幕切句；显式标题当前不截断。只在新功能写 State 前做规范化，并保留旧选择器行为 |
| `if opening_boost_requested(...) and semantics` | **实现不完整** | 当实际草稿有边界但 `_boundary_semantics_plan()` 返回空列表时，增强逻辑被跳过。应基于实际草稿边界数覆盖第一个边界 |
| 全量 unit 基线 `2427/1F` | **历史数据，不可直接作为当前基线** | 当前分支已有后续提交；应在目标 commit 的干净环境重新执行测试并保存日志 |

---

## 4. 原方案中必须修复的问题

### P1-01：第一处边界不等于视频开始后的 3 秒

`node_09` 的边界索引 0 表示视频段 `segments[0] → segments[1]` 的转场，素材绑定在 `segments[1]`。如果第一个视频段有 8 秒，那么这个转场发生在首段末尾，而不是 0–3 秒内。

`node_09b` 的职责不同：它在首段 `segments[0]` 上挂开场特效，并在中文标题轨显示大字标题，标题时长最多 3 秒。它不切分镜头，也不创建首段与第二段之间的转场。

**本次最小改造建议采用以下明确口径：**

> `【开场】` 控制第一处镜头切点（clip 1 → clip 2）的转场风格，强制 `whip` 语义。

这比“保证 0–3 秒内出现转场”更符合当前架构，不需要额外切分素材。如果产品要求严格保证转场落在视频 0–3 秒内，则必须增加首段时间边界约束或重新规划镜头切点，不能只改 `semantics[0]`。

### P1-02：`whip` 不应被标记成客观“最炸”

`pool_whip` 包含旋转、拉伸、闪切、模糊、扭曲、冲屏等多类动势效果。它是可用的强动势分类池，但：

- 池内成员不是按视觉强度排序；
- 当前稳定轮换算法只负责确定性选择，不负责强度评价；
- 资源可解析不代表每项在实际剪映版本中的视觉效果都一样强。

**修订命名**：界面说明和日志使用“强动势/甩镜风格（whip）”，不使用“最强转场”或“最炸转场已保证”等绝对表述。若未来确实要选全库最强转场，应另做人工评分/实机抽样/资源分级，不属于本次最小改造。

### P1-03：增强开场转场时，不能要求语义列表必须非空

附件示例：

```python
semantics = _boundary_semantics_plan(state)
if opening_boost_requested(extract_user_request(state)) and semantics:
    semantics = list(semantics)
    semantics[0] = "whip"
```

此处 `and semantics` 会导致语义列表为空时不覆盖。可是在当前实现中，实际草稿即使没有计划语义，`node_09` 仍会按默认池为真实边界注入转场。因此这段代码会产生“正常有转场，但用户要求加强却没有加强”的静默漏执行。

**修订原则**：将 `opening_boost` 作为独立参数传给 `_inject_fx_into()`；该函数取得真实草稿视频段后，先计算 `n_boundaries=max(len(segments)-1, 0)`，只有 `n_boundaries > 0` 时才把 `working_semantics[0]` 设置为 `whip`。当原语义列表为空时，应先扩充占位列表，不能直接跳过。

### P1-04：LLM 的 `title` 当前并没有从能力层返回

当前 `prompts/tasks/generate_script/zh/system.md`、`en/system.md` 都要求输出 `title`。但 `storyline_capabilities/generate_script.py`：

- JSON Schema 的 `properties` 只声明 `group_scripts`；
- `_extract_scripts()` 只抽取脚本数组；
- 最终返回对象只包含 `group_scripts`、`lang`、`voiceover_style`、`llm_trace`。

因此，仅修改 Prompt 或只在 `node_generate_script.py` 写 `result.get("title")` 都不够。必须形成完整链路：

`Prompt 输出 title → JSON Schema 声明 title → 能力层提取 title → 返回 title → 节点写入 hook_title`。

### P1-05：显式标题长度与现有选择器语义冲突

附件希望所有新增标题都限制为 12 字，但当前 `pick_hook_title()` 的既有规则是：显式 `state["hook_title"]` 仅去首尾空格，不截断；单测 `test_explicit_title_not_truncated` 明确锁定了这一行为。

**修订方案**：

- `resolve_template_title()` 只负责新模板标题的清洗、长度校验/截断；
- LLM `title` 在写入 `hook_title` 前使用同一套新标题规范化函数；
- 不修改 `pick_hook_title()` 对历史/外部显式 `hook_title` 的行为；
- 如果超过 12 个字符被截断，必须在产物或日志中记录 `title_truncated=true`，不能静默截断。

本次标题只验证中文主草稿。当前英文分支不生成大字标题，因此不能声称本次实现了中英文双语标题。

### P1-06：无标记向后兼容与自由文本兜底逻辑相互矛盾

附件一方面说“完全不用 `【】` 标记，行为与现状完全一致”，另一方面又设计了“无标记时自由文本兜底匹配开场转场加强”。两者不能同时成立。

**建议第一期采用严格向后兼容**：

- 含 `【开场】`：按该字段解析；
- 不含 `【开场】`：不开启新增开场转场加强；
- 完全没有四种标记：新增结构化动作全部不触发。

用户使用模板后，开场加强才生效。自由文本意图识别可作为独立二期功能，另设开关和兼容测试。

### P1-07：搜索 Query 注入不等于联网搜索成功

修改 `build_web_topic_query()` 可以保证 `search_web_topic.json` 中出现用户指定 Query，但不能证明网络请求成功。当前 Provider 仍是 DuckDuckGo HTML，且失败缓存可能在同一任务的 QA 重试中被复用。

因此验收应拆成两层：

1. **Query 构造验收**：产物记录的 `query` 必须包含用户指定检索词，优先级正确；
2. **搜索成功验收**：`status` 是 `ok` 且 `search_web_topic` 至少包含一个有效结果；若失败，必须记录 Provider、错误码和失败原因。

不能把“Query 字段正确”写成“网上资料已搜索成功”。当前 Google + SOCKS5 方案也必须在另一项 Provider 改造合并后才能做端到端成功验收。

### P2-01：模板解析需要定义重复、空值、未知标记和多行内容

原方案只说明“扫描到下一个 `【` 或文末”，但没有规定：

- 同一个标记出现两次如何处理；
- 标记存在但值为空时，是空值覆盖还是回退自动行为；
- 未知标记是否会被误识别为支持字段；
- 用户在正文中引用 `【】` 字符时如何处理；
- 标题或检索文本含换行时如何处理。

建议只识别**行首**的四个已知标记；字段值可跨多行，直到下一个已知行首标记；同名字段重复时标记解析错误并使该字段 fail-closed，不要静默采用第一项或最后一项。

### P2-02：“不要写平台名”不应成为代码级限制

附件建议检索词不要写“小红书/抖音”等平台名。这是经验性写作建议，不是稳定的搜索规则。用户可能恰恰希望搜索某个平台上的攻略或案例。

建议保留用户填写的原始检索意图，只做空白折叠、长度限制、去重和安全字符清洗；不要在解析器中禁止平台名。

### P2-03：时间轴自动能力不能等同于每次必然卡点

`timeline_planner` 存在 TTS 对齐、BGM 节拍对齐、目标时长分配等路径。只有选择到 beat 对齐路径并拿到有效 beat 数据时，才能验收“按 BGM 节拍对齐”。不能只看 `plan_timeline_pro` 节点成功就声称智能卡点成功。

---

## 5. 修订后的功能需求与模板契约

### 5.1 模板格式

建议创建台输入以下格式：

```text
【主题】马来西亚吉隆坡武吉马六甲海上清真寺旅行 Vlog
【开场】开头使用强动势甩镜风格转场，配大字标题
【检索】马六甲海上清真寺旅游攻略、开放时间和拍摄打卡信息
【标题】马六甲海上清真寺之旅
```

### 5.2 四种标记的确定性规则

| 标记 | 解析结果 | 生效位置 | 优先级/回退 |
|---|---|---|---|
| `【主题】` | `theme` | `build_web_topic_query()`；完整原文仍传给文案 LLM | 检索 Query 的第二优先来源；空值视为未提供 |
| `【开场】` | `opening_boost_requested` | `node_09_inject_fx` 的第一处真实镜头边界 | 仅在字段明确表达正向转场加强时触发；否定表达不触发 |
| `【检索】` | `search_query` | `build_web_topic_query()` | 高于 `【主题】`；空值时回退主题 |
| `【标题】` | `template_title` | `node_generate_script` 写入 `state["hook_title"]` | 高于 LLM 标题；空值时回退 LLM 标题 |

标题后备链：

`【标题】 > LLM title > 第一条中文字幕切句`

当三者均没有有效值时，不创建标题文字，但现有开场特效仍可以按 `node_09b` 的既有逻辑执行。

### 5.3 搜索 Query 最终优先级

保留当前手动 Query 机制和创建台冻结契约，完整顺序为：

1. `storyline_targets.query`（现有手动 Query，当前创建台仍不写）；
2. `【检索】`；
3. `【主题】`；
4. 画面概述 `overall` 首句；
5. 前三个分组主题。

每一部分继续遵守当前 `WEB_TOPIC_QUERY_PART_MAX_CHARS`、`WEB_TOPIC_QUERY_TOTAL_MAX_CHARS`、空白折叠、去重逻辑。不要为了这次功能把用户原始文本写入 `storyline_targets.query`，以免破坏创建台冻结契约。

### 5.4 开场转场定义

第一期严格定义为：

> 用户在 `【开场】` 中明确要求开头使用强动势转场时，把首个真实镜头边界（边界 0，clip 1 → clip 2）的语义覆盖为 `whip`，并由现有资源库映射到 `pool_whip`。

行为细节：

- 只改边界 0，不改变边界 1 及后续边界的原计划语义。
- zh/en 两份草稿使用同一语义和同一 job id，保持现有双稿确定性。
- 只有实际草稿存在至少两个视频段时才可应用。
- 只有 `pool_whip` 资源真实解析并写入草稿后，才记录“已应用”；资源缺失必须记录降级/失败，不能记录成功。
- 单镜头草稿没有镜头边界，应记录 `not_applicable`，不能声称转场已生效。
- 由于首个边界由第一段实际时长决定，本方案不保证其发生在视频 `0–3s` 内。

### 5.5 开场标题定义

- 本次只对中文主草稿的大字标题做定制；英文分支仍保持当前“开场特效、不生成大字标题”的行为。
- `【标题】` 和 LLM 标题在进入 `hook_title` 前统一规范化，最多 12 个 Unicode 字符；发生截断时记录 `title_truncated=true`。
- 不改变既有 `pick_hook_title()` 对其他显式 `hook_title` 的兼容行为。
- 标题样式、标题轨、3 秒显示窗口、动画缓存双闸均沿用 `node_09b` 现有实现，不重复造轮子。

### 5.6 向后兼容

| 输入形态 | 新增功能行为 |
|---|---|
| 无任何 `【】` 标记 | 检索词、开场转场和标题均按旧行为运行，不触发新的结构化动作 |
| 只有 `【主题】` | 搜索 Query 增加主题回退来源；其他功能不变 |
| 只有 `【检索】` | 搜索 Query 优先使用检索字段；其他功能不变 |
| 只有 `【开场】` | 首个实际镜头边界使用 `whip` 语义；不修改标题来源 |
| 只有 `【标题】` | 用户标题优先写入中文主草稿；不改变转场与搜索 |
| 某一字段为空 | 该字段视为未提供，按对应回退规则处理 |
| 同一字段重复 | 该字段解析失败并 fail-closed；记录解析警告，不猜测用户意图 |

---

## 6. 修订后的技术设计

### 6.1 新增纯函数模块 `jy_common/user_directives.py`

新增一个不访问网络、不读写文件、不依赖 LangGraph 的纯函数模块，统一解析四个已知字段，避免每个业务节点自行写正则。

建议接口：

```python
parse_user_directives(text: str) -> dict[str, object]
opening_boost_requested(directives: dict[str, object]) -> bool
resolve_template_title(directives: dict[str, object]) -> str | None
```

解析规则：

1. 只识别四个支持标记：`主题`、`开场`、`检索`、`标题`。
2. 标记必须位于一行的开头（允许前置空格），字段内容可跨多行，直到下一个已知标记或文本结束。
3. 保留原始输入，不修改 `storyline_targets.user_request/script_request`。
4. 值统一 `strip()`；空值转成未提供，不写空字符串覆盖旧行为。
5. 同名标记重复时，该字段无效，并返回解析警告；其他无冲突字段仍可正常使用。
6. 未知标记作为普通文本保留，不解释为新功能。
7. 不带任何已知标记时，解析结果为空；不执行自由文本兜底识别。

`opening_boost_requested()` 采用 fail-closed：

- 只检查 `【开场】` 字段；
- 字段必须同时包含开场范围词（如“开头/开场/片头”）、“转场”和强动势词（如“炸/震撼/强烈/冲击/甩镜/炫酷”）；
- 否定表达（如“不要/无需/不需要/别/禁止/不用”）优先于正向词，命中否定时不触发；
- 不把 `【标题】` 中的“大字标题”误认为转场要求；
- 不把其他字段中的“转场”词误认为开场指令。

建议返回解析诊断信息（字段冲突、字段为空、标题是否超长），但不要把原始提示词或解析结果写入监控事件 payload。创建台目前有“提示词不进入监控事件”的安全契约，应继续保留。

### 6.2 改造检索词构造 `nodes/storyline/_common.py`

在 `build_web_topic_query(state)` 中：

1. 读取当前用户原始文本：`extract_user_request(state)`；
2. 调用 `parse_user_directives()`；
3. 按 `手动 query → 【检索】 → 【主题】 → overall 首句 → 分组主题` 构造 parts；
4. 复用现有长度限制、去重、分隔符和空结果逻辑；
5. 更新 docstring 和现有单测。

不改 `web/console_state.py` 的 State 字段集合，不把 Query 写入 `storyline_targets.query`。

### 6.3 改造开场转场 `nodes/node_09_inject_fx.py`

不要在节点外部只根据语义计划是否为空决定是否增强。推荐将布尔开关传入草稿注入函数：

```python
# node level
user_request = extract_user_request(state)
directives = parse_user_directives(user_request)
opening_boost = opening_boost_requested(directives)

# per draft, inside _inject_fx_into(...)
segments = _sorted_video_segments(video_track)
n_boundaries = max(len(segments) - 1, 0)
working_semantics = list(semantics)

if opening_boost and n_boundaries > 0:
    if not working_semantics:
        working_semantics = [None] * n_boundaries
    elif len(working_semantics) < n_boundaries:
        working_semantics.extend([None] * (n_boundaries - len(working_semantics)))
    working_semantics[0] = "whip"
```

以上代码是实现意图示例，正式提交前应按当前 `_inject_fx_into()` 的实际参数、语义列表长度和 all-cut 检查顺序整合，不要直接复制后忽略现有流程。

实现要求：

- `opening_boost` 要在知道实际边界数后生效；`n_boundaries == 0` 不应用。
- `working_semantics[0] = "whip"` 必须发生在 `_all_boundaries_cut()` 判断之前，使边界 0 不再被判定为硬切。
- 其余边界保留原语义；语义缺失时继续走现有 `pool_gentle` 回退。
- 现有 all-cut 保底只在“所有边界仍是硬切且必需转场为真”时触发。开场覆盖成 `whip` 后通常不应再触发 all-cut 保底。
- zh/en 分支必须共享相同 `opening_boost`、`job_id` 和边界索引。
- 记录稳定状态：`requested`、`applied`、`not_applicable` 或 `degraded`。不能在资源解析失败时仍追加表示“已应用”的成功状态。

建议在 `_inject_fx_into()` 的摘要结构里增加：

```text
opening_boost_requested
opening_boost_applied
opening_boost_not_applicable
opening_transition_pool
opening_transition_name
```

如现有日志规范不允许动态状态标签，具体资源名写入任务产物/诊断 JSON，状态日志仍使用固定标签。

### 6.4 改造标题生成 `storyline_capabilities/generate_script.py`

必须接通完整返回链路：

1. JSON Schema `properties` 添加 `title: {"type": "string"}`；如调用客户端要求严格 Schema，还要检查并补齐 `required`、`additionalProperties` 等约束，不能只添加一个属性后假设模型一定返回。
2. 从 LLM 返回对象中提取 `title` 并 `strip()`。
3. 能力层返回值新增 `title` 字段，保留原 `group_scripts` 处理逻辑。
4. 模型未返回有效标题时返回空值，不抛异常、不影响原有 narration 逻辑。

### 6.5 改造 State 写入 `nodes/storyline/node_generate_script.py`

文案生成结束后，按以下优先级写入标题：

```text
template_title = resolve_template_title(parsed_directives)
llm_title = normalize_generated_title(result.get("title"))
resolved_title = template_title or llm_title
```

- 若 `resolved_title` 非空，返回 State 更新 `{"hook_title": resolved_title}`。
- 若二者都为空，不写 `hook_title`，保留 `node_09b` 的字幕切句回退。
- 将 `title`、`title_source`（`template` / `llm` / `subtitle_fallback`）及 `title_truncated` 写入 `script.json`，便于验收和回溯。
- 本次只对中文主草稿启用该标题来源链；不要把英文模型标题无条件写入中文标题轨。
- 不改变 `state.py` 中已存在的 `hook_title` 类型声明，不新增图节点，不改变 graph 拓扑。

### 6.6 Prompt 调整

- `prompts/tasks/generate_script/zh/system.md`：标题长度改为“建议 8–12 个汉字，必须不超过 12 个字符”，与中文开场大字标题约束统一。
- `prompts/tasks/generate_script/en/system.md`：不要直接机械改成“8–12 字”。英文标题按英文语言规则规定词数；当前英文草稿并不展示开场大字标题，若后续要展示英文大字标题，再单独建立 `hook_title_en` 和对应轨道行为。
- Prompt 修改只是降低模型输出超长标题的概率；最终长度仍由节点层确定性校验，不能依靠 LLM 自觉遵守。

### 6.7 创建台提示

将 `web/console.html` 中 `promptInput` 的 placeholder 更新为四字段模板示例，并注明字段可选。Placeholder 只是输入框提示，不应把它自动填充为实际文本，也不应覆盖用户已输入内容。

---

## 7. 分阶段执行计划

### 阶段 0：基线冻结与回归准备

| 项目 | 内容 |
|---|---|
| 目标 | 对本次目标 commit 建立可比较的测试基线，确认当前行为 |
| 工作项 | 确认工作区干净；记录 `git rev-parse HEAD`；运行定向单测与全量单测；保留测试输出；记录现有标题选择器、node_09、搜索 Query 的基线产物 |
| 重点文件 | `tests/unit/test_hook_title.py`、`tests/unit/test_node_09_inject_fx.py`、`tests/unit/test_node_09b_inject_opening_hook.py`、`tests/unit/test_node_generate_script.py`、`tests/unit/test_node_search_web_topic.py` |
| 验收 | 基线测试结果可复现；任何失败都记录完整 traceback 和环境信息，不得未经对照就标成“环境性失败” |
| 退出条件 | 完成测试基线报告后，才进入功能开发 |

### 阶段 1：实现指令解析器

| 项目 | 内容 |
|---|---|
| 目标 | 引入确定性解析，定义四字段契约 |
| 代码改动 | 新增 `jy_common/user_directives.py`；新增 `tests/unit/test_user_directives.py` |
| 测试 | 四字段全量解析、单字段解析、多行值、空值、重复字段、未知字段、标记不在行首、无标记返回空、否定词优先、标题长度规范化 |
| 验收 | 同一输入在不同进程/不同执行顺序下解析结果完全一致；无标记输入不触发任何新增动作 |
| 风险 | 模糊标记和否定词误判；通过行首标记规则和 fail-closed 降低风险 |

### 阶段 2：接入网页检索 Query

| 项目 | 内容 |
|---|---|
| 目标 | 让 `【检索】` / `【主题】` 真正进入搜索 Query |
| 代码改动 | 修改 `nodes/storyline/_common.py::build_web_topic_query()`；扩展 Query 构造单测 |
| 测试 | 手动 Query > `【检索】` > `【主题】` > overall > group themes；长度限制；去重；空值回退；不修改 State 中的 `query` 字段 |
| 验收 | `search_web_topic.json.query` 精确包含 `【检索】` 词；没有检索字段时优先使用 `【主题】`；无标记回归结果与旧版一致 |
| 附加验证 | 独立验证 Provider 成功状态；Query 正确但 Provider 失败时应判“Query 层通过、搜索层失败” |

### 阶段 3：接入开场转场加强

| 项目 | 内容 |
|---|---|
| 目标 | `【开场】` 正向要求可覆盖首个镜头边界 |
| 代码改动 | 修改 `nodes/node_09_inject_fx.py`，传入开场增强意图，并在真实边界数确定后覆盖边界 0 |
| 测试 | 正向触发、明确否定不触发、无标记不触发、语义列表为空但有边界、单镜头无边界、资源池缺失降级、all-cut 兼容、仅边界 0 变化、后续边界不变、zh/en 同名转场、重放幂等 |
| 验收 | 正向样例的边界 0 实际映射 `pool_whip`，草稿中出现真实转场资源并绑定到第二段；无边界不得标成功；资源解析失败不得假成功 |
| 说明 | 验收用词是“强动势 whip 风格”，不是“全库最炸”；第一处切点不保证在 0–3 秒内 |

### 阶段 4：接通标题模板与 LLM 标题

| 项目 | 内容 |
|---|---|
| 目标 | 标题来源形成 `模板 > LLM > 首条字幕` 的闭环 |
| 代码改动 | 修改 `storyline_capabilities/generate_script.py`、`nodes/storyline/node_generate_script.py`、中文 Prompt；必要时增加标题清洗 helper |
| 测试 | JSON Schema 声明 title；能力层返回 title；模板标题优先；LLM 标题次优先；无 title 时回退字幕；超长标题截断并留痕；空标题不写 State；英文标题不误写入中文标题轨 |
| 验收 | `script.json.title_source` 正确；剪映中文草稿 `hook-title-1` 文本与最终标题一致；标题轨时长仍不超过 3 秒；`node_09b` 原有资源缓存双闸仍有效 |
| 兼容 | 保留 `pick_hook_title()` 对历史显式 `hook_title` 不截断的现有测试语义 |

### 阶段 5：创建台模板提示与端到端验证

| 项目 | 内容 |
|---|---|
| 目标 | 用户可以直接照模板输入，并从产物中核对每条指令的执行结果 |
| 代码改动 | 修改 `web/console.html` placeholder；必要时补充提示语和字段说明 |
| 端到端输入 | 使用本文件 §5.1 的模板样例创建真实任务 |
| 检查产物 | `initial_state.json` 保留完整原始输入；`search_web_topic.json` 查询词正确；`script.json` 标题来源正确；中文草稿首个边界使用 whip 池资源；中文开场标题与 `【标题】` 一致；状态日志没有把降级误报成功 |
| 兼容回归 | 删除全部 `【】` 标记后重新运行，比较新增功能相关行为与阶段 0 基线；不得出现新的开场增强动作 |
| 最终验收 | 所有阶段测试通过，真实任务产物可追溯，人工在剪映中打开草稿检查转场与标题 |

---

## 8. 测试与验收矩阵

### 8.1 指令解析器

| 用例 | 输入 | 期望 |
|---|---|---|
| 全字段 | 四种标记都有值 | 四字段全部正确解析 |
| 部分字段 | 只有 `【检索】` | 只返回检索字段 |
| 空字段 | `【标题】` 后无内容 | 标题视为未提供 |
| 多行字段 | 字段内容跨多行 | 合并/保留文本后正确 `strip()` |
| 重复字段 | 两个 `【标题】` | 标题字段无效并产生解析警告，不静默选一条 |
| 无标记 | 原始自由文本 | 解析结果为空，不触发新增结构化动作 |
| 否定开场 | `【开场】开头不要使用强转场` | `opening_boost_requested=False` |
| 正向开场 | `【开场】开头使用最炸的转场` | `opening_boost_requested=True` |
| 非开场转场 | `【主题】研究转场技术；【标题】转场旅行记` | 不触发开场增强 |

### 8.2 搜索 Query

| 用例 | 期望 |
|---|---|
| 手动 query + `【检索】` + `【主题】` | 手动 query 在前，检索字段在后，主题按既定优先级参与回退/拼接 |
| 无手动 query、有检索字段 | Query 首部包含检索字段 |
| 无检索字段、有主题字段 | Query 首部包含主题字段 |
| 两字段都无 | 使用 overall 首句和分组主题，完全回归旧行为 |
| 字段超长 | 单段与总长均被现有常量限制 |
| 搜索 Provider 失败 | `status=failed` 与 Query 构造结果分开判定 |
| 重试命中失败缓存 | 明确识别 `cache_hit` + 原失败状态，不误判为新请求成功 |

### 8.3 转场注入

| 用例 | 输入/场景 | 期望 |
|---|---|---|
| 正向增强 | 有 2 个以上真实视频段，开场要求命中 | 边界 0 使用 `whip` → `pool_whip` |
| 无语义计划 | `_boundary_semantics_plan()` 返回空列表，但草稿有多个视频段 | 仍然增强边界 0，不漏执行 |
| 原语义全硬切 | 原边界全部 `cut`，开启增强 | 边界 0 变 `whip`；其余边界仍按原计划；all-cut 保底不应覆盖 whip |
| 后续边界 | 3 个镜头以上 | 边界 1 及以后不得被开场指令改写 |
| 单镜头 | 只有 1 个视频段 | 不写转场，状态为 `not_applicable`，不得记 `applied` |
| 资源缺失 | `pool_whip` 无有效资源 | 状态为 `degraded`/失败，不能静默回退后宣称已应用 |
| 双语草稿 | zh/en 两份草稿 | 同一 job id 下边界 0 资源名一致 |
| 重放 | 同一任务再次执行 | 无重复素材、引用完整、结果确定性一致 |
| 无标记 | 原始文本未含四种标记 | 新增增强逻辑不触发 |

### 8.4 标题

| 用例 | 期望 |
|---|---|
| 模板标题存在 | `【标题】` 覆盖 LLM 标题和字幕标题 |
| 模板标题为空、LLM 标题存在 | 使用 LLM 标题 |
| 两者都没有 | `pick_hook_title()` 从第一条字幕切句 |
| 新模板标题超长 | 规范化到 12 字并记录截断标记 |
| LLM 标题超长 | 节点层确定性规范化并记录截断标记 |
| 显式历史 State 标题超长 | 原有 `pick_hook_title()` 行为不变；保留其现有单测 |
| 无字幕且无标题 | 不创建文字标题，开场特效行为保持原状 |
| 开场钩子关闭 | `node_09b` 仍按现有 disabled 行为，不误报标题已写入 |
| 英文分支 | 不把英文 title 写到中文主草稿；英文标题功能本次不验收 |

### 8.5 端到端验收标准

一次使用四字段模板的任务，至少应能从产物中验证：

1. `initial_state.json`：完整保留原始用户输入；
2. `search_web_topic.json`：`query` 包含 `【检索】` 字段；如联网成功，`status=ok` 且有有效结果；
3. `script.json`：标题值和 `title_source` 明确；
4. 中文 `draft_content.json`：标题轨文本与 `【标题】` 一致；
5. 中文和英文草稿：首个真实边界使用 `pool_whip` 语义对应的转场资源；
6. `status_log` / 诊断文件：能区分 requested、applied、not_applicable、degraded；
7. 去掉全部标记重新运行：新增动作不触发，原有默认行为保持不变。

---

## 9. 文件改动清单与不改动清单

### 9.1 预计修改/新增文件

| 文件 | 操作 | 目的 |
|---|---|---|
| `jy_common/user_directives.py` | 新增 | 四种标记的确定性解析、开场判断、标题规范化 |
| `tests/unit/test_user_directives.py` | 新增 | 解析器完整单测 |
| `nodes/storyline/_common.py` | 修改 | 将 `【检索】`、`【主题】` 接入 Query 构造 |
| `nodes/node_09_inject_fx.py` | 修改 | 按实际草稿边界加强第一处转场 |
| `storyline_capabilities/generate_script.py` | 修改 | JSON Schema、解析并返回 LLM title |
| `nodes/storyline/node_generate_script.py` | 修改 | `【标题】 > LLM title`，写入 `hook_title` 和来源元数据 |
| `prompts/tasks/generate_script/zh/system.md` | 修改 | 明确中文标题长度 |
| `web/console.html` | 修改 | 显示四字段模板 placeholder |
| `tests/unit/test_node_09_inject_fx.py` | 修改 | 增加开场增强边界测试 |
| `tests/unit/test_node_generate_script.py` | 修改 | 增加 title 提取和 State 写入测试 |
| `tests/unit/test_storyline_capabilities_search_web_topic.py` / 相关 Query 测试 | 修改 | 增加 Query 优先级和截断测试 |
| `tests/unit/test_hook_title.py` | 必要时扩展 | 保证旧显式标题语义不被破坏 |

### 9.2 本次建议不改动

| 文件/项目 | 原因 |
|---|---|
| `web/console_state.py` | 继续遵守提示词只写 `user_request/script_request` 的冻结契约；不写 `query` |
| `state.py` | `hook_title` 已声明，无需新增同义字段 |
| `graph.py` / `workflow.json` | 不新增 LangGraph 节点或边；属于现有节点内部的增量能力 |
| `SCHEMA_VERSION` | 不改变图拓扑，不新增必需 State 字段；以当前 optional `hook_title` 为标题传递通道 |
| `nodes/node_09b_inject_opening_hook.py` | 现有标题轨、样式、窗口和缓存双闸已具备；优先复用，不改变其核心写稿逻辑 |
| `jy_common/hook_title.py::pick_hook_title()` | 现有显式标题不截断是被测试锁定的兼容行为；规范化应放在新数据写入端 |
| `storyline_targets.query` | 不改变创建台契约；用户指令由 Query builder 读取，不从 API 侧污染手动覆盖字段 |

---

## 10. 风险、回滚与实施纪律

### 10.1 主要风险

| 风险 | 影响 | 防护 |
|---|---|---|
| 标记解析误判 | 将普通文本当成指令 | 行首标记、白名单字段、重复字段 fail-closed |
| 开场强度词误判 | 无意改变用户转场 | 只读 `【开场】`；否定词优先；无标记不触发 |
| 第一边界不存在 | 无法应用转场 | 检查实际视频段数，记录 `not_applicable` |
| `pool_whip` 资源缺失 | 用户要求没有真实落盘 | 区分 degraded，交付 QA 按 required feature 规则处理 |
| 首段很长 | 转场不会在视频 0–3 秒 | 在产品文案中明确这是第一镜头边界语义，不冒充时间保证 |
| LLM 不返回 title | 大字标题没有 LLM 兜底 | 模板标题优先；LLM 缺失时回退首条字幕；全空则 no-title |
| 标题超长 | 屏显裁切或不符合模板 | 写入 State 前统一规范化，并记录截断 |
| 搜索 Provider 故障 | Query 正确但无检索结果 | Query 与网络成功分层验收；独立处理 Provider 切换 |
| 失败搜索缓存复用 | 重试时不重新联网 | 明确 `cache_hit` 与原状态；需要强制重搜时另设刷新语义 |
| 老任务恢复 | optional 字段缺失 | 缺少 `hook_title` 仍走既有字幕标题回退 |

### 10.2 回滚方式

- 代码层回滚本次功能提交即可；不需要数据迁移。
- 因为不增加节点/边，原则上无需重生成工作流拓扑文件。
- 不新增持久化 State 必需字段；旧 checkpoint 缺少 `hook_title` 时仍走 `.get()` 回退。
- 不修改 `OPENING_HOOK_ENABLED` 默认值，不在这次功能中改动搜索 Provider 环境变量。
- 如果解析器出现异常，最安全的降级是返回空解析结果；旧自由文本继续作为原始 Prompt 使用，但不触发新增动作。

### 10.3 执行边界

本文件是设计和执行计划，不代表代码已经修改或部署。涉及文件写入、删除、服务重启、环境变量变更、搜索服务切换、剪映真机操作或推送 GitHub 时，应先输出操作清单和影响范围，再由操作者明确批准后执行。

---

## 11. 当前未解决但不应混入本次改造的事项

1. **Google + SOCKS5(1088) Provider 切换**：附件提到该决策，但没有附关联设计文档；当前源码仍由 `node_search_web_topic` 指定 DuckDuckGo HTML。应另设 Provider 适配和代理连通性任务，不要把 Query 构造改造误报为搜索服务修复。
2. **自然语言通用意图解析**：当前方案只支持四种固定标记，不等于理解任意编号指令。若后续要求任意描述都能逐条执行，需要定义正式的结构化剪辑 Brief、字段 Schema、冲突优先级和节点消费契约，不能不断增加正则补丁。
3. **精确 0–3 秒转场**：当前建议只改首个镜头边界；若必须保证第一处转场真实发生在前三秒，需单独设计首段时长约束和镜头规划。
4. **转场视觉强度标定**：`pool_whip` 是动势风格池，不是强度排序。要实现“从所有可用转场中挑最炸的一个”，需人工样片评分、资源可用性验证和稳定的强度元数据。
5. **英文开场大字标题**：当前 `node_09b` 的产品行为只为中文草稿写大字标题；英文大字标题需要单独的 `hook_title_en`、英语标题规则和英文草稿轨道测试。
6. **智能卡点的可控参数化**：当前已有 TTS/BGM beat/目标时长分配；若用户要指定“每 2 拍切一次”“副歌处切镜”等精确规则，需要独立扩展时间轴 Brief，而不是只把全文继续塞进 LLM Prompt。

---

## 12. 源码证据索引（固定到本次审查 commit）

以下链接均指向本次审查基线 commit，避免 `main` 后续变化导致证据漂移：

| 主题 | 源码链接 |
|---|---|
| 当前审查 commit | [e67376a8 — main](https://github.com/wayyet/auto-video-editor/commit/e67376a8cee941f7b6914a76b4438aa383e8e2ab) |
| 创建台输入框 | [`web/console.html`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/web/console.html) |
| 创建任务服务 | [`web/console_service.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/web/console_service.py) |
| 初始 State 契约 | [`web/console_state.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/web/console_state.py) |
| 文本读取和检索 Query | [`nodes/storyline/_common.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/nodes/storyline/_common.py) |
| 搜索节点 | [`nodes/storyline/node_search_web_topic.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/nodes/storyline/node_search_web_topic.py) |
| 搜索 Provider 实现 | [`storyline_capabilities/search_web_topic.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/storyline_capabilities/search_web_topic.py) |
| 工作流拓扑 | [`graph.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/graph.py) |
| 转场节点 | [`nodes/node_09_inject_fx.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/nodes/node_09_inject_fx.py) |
| 转场语义及资源池 | [`templates/fx_template.json`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/templates/fx_template.json) |
| 开场钩子节点 | [`nodes/node_09b_inject_opening_hook.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/nodes/node_09b_inject_opening_hook.py) |
| 标题选择器 | [`jy_common/hook_title.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/jy_common/hook_title.py) |
| 文案节点 | [`nodes/storyline/node_generate_script.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/nodes/storyline/node_generate_script.py) |
| 文案能力层 | [`storyline_capabilities/generate_script.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/storyline_capabilities/generate_script.py) |
| 中文文案 Prompt | [`prompts/tasks/generate_script/zh/system.md`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/prompts/tasks/generate_script/zh/system.md) |
| 英文文案 Prompt | [`prompts/tasks/generate_script/en/system.md`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/prompts/tasks/generate_script/en/system.md) |
| 切镜节点 | [`nodes/storyline/node_split_shots.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/nodes/storyline/node_split_shots.py) |
| 切镜能力层 | [`storyline_capabilities/split_shots.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/storyline_capabilities/split_shots.py) |
| 转场节点测试 | [`tests/unit/test_node_09_inject_fx.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/tests/unit/test_node_09_inject_fx.py) |
| 标题选择器测试 | [`tests/unit/test_hook_title.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/tests/unit/test_hook_title.py) |
| 开场钩子测试 | [`tests/unit/test_node_09b_inject_opening_hook.py`](https://github.com/wayyet/auto-video-editor/blob/e67376a8cee941f7b6914a76b4438aa383e8e2ab/tests/unit/test_node_09b_inject_opening_hook.py) |

---

## 13. 最终审查结论

**附件的“输入链路畅通、消费方式不完整、需要结构化解析”的主判断成立。原实施计划的技术方向也基本合理，但不能原样落地。**

执行前必须先吸收本修订版中的 P1 项：

- 把开场要求定义为首个真实镜头边界的强动势转场，而不是承诺 0–3 秒内一定出现转场；
- 不把 `whip` 夸大为全库最强效果；
- 根据实际草稿边界数覆盖语义，不能受空语义列表影响；
- 补齐 LLM title 的 Schema、解析、返回和 State 写入链路；
- 只规范化新输入的模板/LLM 标题，保留现有 `pick_hook_title()` 的兼容语义；
- 明确无标记时严格不触发新增动作；
- 将 Query 构造成功与 Provider 联网成功分开验收。

本文件完成的是**源码审查和修订后执行设计**，不是代码实施报告。完成阶段 0 基线测试和阶段 1–5 的代码改造、自动测试及剪映真机验收后，才能把状态更新为“已实施并通过验收”。

# auto-video-editor noop 节点功能补齐、FireRed-OpenStoryline 复用评估与分阶段执行计划

> 日期：2026-11-11（FireRed 功能复用修订）  
> 仓库：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> auto-video-editor 当前 main HEAD：`262098953b7f51171e91b12d8c65667a31dc3b76`  
> 附件记录基线：`04019c847243a4266f3577ed89740d928d24d70e`  |  FireRed-OpenStoryline main：`b45011d1a7ca7273ccba585cbaef6f3e3b23accd`  
> 文档性质：源码核验、设计与执行计划；**本次未修改仓库代码，也未执行任何删除或重构操作**。

---

## 0. 执行摘要

### 0.1 总体结论

本次修订将治理策略从“发现空转就考虑删除”调整为：**先寻找 FireRed-OpenStoryline 中是否已有相近能力；能复用的先补齐功能与数据链，再决定是否清理。** 本次只修改设计文档，没有修改两个 GitHub 仓库的代码。

当前代码事实仍然成立：

1. `auto-video-editor/graph.py` 有 **47 个 `g.add_node()` 注册点**，分组为 `18 + 6 + 23`。
2. `storyline_script_template_recommendation` 当前确实是结构级 noop：只追加 `status_log`，位于 `generate_script` 之后，没有图内出边，也没有业务产物消费者。但**本次不再把删除节点作为推荐方案**，而是参考 FireRed 的模板检索节点，将其改造成真正的“模板检索 → 候选筛选 → 选择 → 文案生成应用”链路。
3. FireRed-OpenStoryline 中存在高度相近的 `script_template_rec.py`、`generate_script.py`、`generate_ai_transition.py`、`plan_timeline_ai_transition.py` 和 `render_video.py`。其中模板推荐、AI 转场和视频渲染都有实际实现，具备明确的代码参考价值。
4. FireRed 的模板推荐节点仅返回最多 3 个候选模板；其 `generate_script.py` 当前源码没有显式读取 `script_template_rec` 的候选输出。因此不能把 FireRed 的模板推荐节点直接搬过来就认为完成了“模板影响文案”。本项目需要进一步补上推荐结果到 `generate_script` prompt 的消费链。
5. FireRed 的 AI 转场不是只生成 JSON：它读取相邻片段首尾帧，检查宽高比，通过视觉模型生成过渡提示词，再调用外部视频生成服务，产出真实 MP4；随后时间线节点把生成文件作为独立视频片段插入时间线。`auto-video-editor` 当前只生成 Stub JSON，且 mapper 未物化，因此应按 FireRed 的真实链路补齐。
6. FireRed 的 `render_video.py` 是完整 MoviePy 渲染实现，但依赖较重；`auto-video-editor` 已有真正的 `node_18_export_video` + `draft_ops/draft_exporter.py` FFmpeg 导出链。不能再并行造一套重复的最终导出能力。
7. BGM 的人工关卡②是此前明确采用的产品设计。FireRed 的自动 BGM 检索/分析能力可作为未来增强参考，但本次不改变“人工加 BGM”的既定决策。

### 0.2 FireRed 复用评估结论

| auto-video-editor 当前节点/能力 | FireRed 对应代码 | 复用结论 | 本项目需要补齐的内容 |
|---|---|---|---|
| `storyline_script_template_recommendation` | `script_template_rec.py` + `generate_script.py` + `llm_script_template_label.py` | **应复用设计与检索逻辑** | 模板库、元数据、候选检索、确定性过滤、模板内容读取、prompt 注入、结果审计；节点移到 `generate_script` 上游 |
| `storyline_generate_ai_transition` | `generate_ai_transition.py` | **应复用端到端生成流程** | 首尾帧抽取、宽高比检查、视觉提示词、可配置视频生成 Provider、取消/进度、产物校验、缓存与费用确认 |
| `storyline_plan_timeline_ai_transition` | `plan_timeline_ai_transition.py` | **应复用时间线物化思想** | 把真实生成视频加入 `source_media` 与 `clips`，由现有 mapper 按普通视频片段写入剪映草稿，不能只写 `ai_transitions` 元数据 |
| `storyline_render_video` | `render_video.py` | **参考实现，不直接复制** | 先确认是否需要“剪映草稿前预览”；如需要，优先复用本项目 FFmpeg 导出能力，不复制完整 MoviePy 渲染栈 |
| `storyline_select_bgm` | `select_bgm.py` + `StorylineRecall` + `ElementFilter` | **仅作为未来自动 BGM 能力参考** | 当前 manifest 路径为空是资源未配置问题，且人工关卡②是既定设计；本次不绕过人工关卡自动落轨 |
| `node_11_inject_sticker` | FireRed 的 `ElementFilter` / 元素推荐模式、剪映 Skill 资产脚本 | **只有资源检索模式可参考** | FireRed 未提供与本节点完全等价的 LangGraph 贴纸原生注入节点；继续按本项目真实 19 位资源 ID + fail-closed 规则治理 |
| `assembly_repair_loop` | 未找到直接等价的 Repair Loop 节点 | **不应强行映射** | 本项目自行实现基于 QC issue 的结构化 patch；不能把 FireRed 时间线规划节点当成修复节点 |

### 0.3 重要的跨项目兼容边界

- `FireRed-OpenStoryline` 是 `BaseNode + NodeMeta + NodeState + MCP Tool/Agent` 模式；`auto-video-editor` 是 LangGraph `StateGraph`，节点函数采用 `WorkflowState -> dict`。**不要直接把 FireRed 的 `BaseNode`、`NodeState` 或 Node 类 import 到 `auto-video-editor`。**
- FireRed 的 `StorylineRecall` 依赖 HuggingFace Embeddings / FAISS 等组件；FireRed 的 AI 转场和渲染代码依赖 MoviePy、图像/视频生成 Provider 等运行环境。当前 `auto-video-editor/pyproject.toml` 明确将 `torch / torchaudio / moviepy` 列为禁止进入主环境的依赖。不能把 FireRed 的 requirements 整体合并进主项目。
- 复用方式应是**能力与数据契约适配**：优先抽取算法思想、输入输出字段、缓存和失败语义；重依赖能力通过独立 Provider/隔离服务适配；主项目继续只维护 LangGraph 节点、state、artifact 和剪映草稿映射。
- FireRed 当前仓库树没有提交实际的 `resource/script_templates` 模板文本和 `meta.json` 资源库；它的 `script_template_rec` 需要由部署配置提供 `script_template_dir` 与 `script_template_info_path`。因此本项目必须把模板库初始化/资源缺失降级写入执行计划，不能假设 clone 后即可检索出模板。
- 若直接复制 FireRed 源文件而非按其思路重写，需保留并遵守其仓库 Apache-2.0 许可证及第三方资源各自的授权要求。本方案优先采用接口适配和针对本项目的实现，不直接搬入整个依赖栈。

### 0.4 修订后的治理顺序

1. 先补图拓扑守卫和功能契约，禁止“能从 START 到达但零出边”的空节点长期存在。
2. **优先恢复文案模板推荐功能**：按 FireRed 代码实现模板目录、元数据标注、候选检索/过滤，并把选中模板内容传入本项目 `generate_script`。
3. **补齐真实 AI 转场链路**：以 FireRed 的首尾帧 → 视觉提示 → 视频生成 → 时间线片段化为参考，同时增加本项目的显式费用确认、缓存、时长预算、草稿 QA。
4. 处理 `storyline_render_video`：优先决定是否保留“草稿前预览”。若保留，用本项目现有 FFmpeg exporter 实现真实 MP4 预览并增加消费者；若无此产品需求，则移除旁支，避免再维护一个无用的渲染节点。
5. 再清理 `node_16_translate_subtitles` 旧 shim 和经证明无人使用的 state 字段。

---

## 1. 核验基线与方法

### 1.1 远端基线说明

GitHub 当前 `auto-video-editor` `main` HEAD：

```text
262098953b7f51171e91b12d8c65667a31dc3b76
```

当前 `FireRed-OpenStoryline` `main` HEAD：

```text
b45011d1a7ca7273ccba585cbaef6f3e3b23accd
```

`auto-video-editor` 相对上次核验 HEAD `41386c4` 仅新增了一份网页话题检索设计文档，没有修改本次讨论涉及的业务代码。FireRed 源码以本次读取到的 `b45011d` 为核验基线。

附件标注的 `04019c8` 在远端确实存在，完整 SHA 为：

```text
04019c847243a4266f3577ed89740d928d24d70e
```

GitHub compare 结果显示：当前 `main` 相对 `04019c8` **ahead 6 commits、behind 0**。这 6 个提交中，除新增分析文档外，还修改了媒体探测、时间轴速度护栏、封面生产、草稿完整性 QA、Timeline contract、mapper、时间轴规划等文件。

因此：

- 本文以当前远端 `main` HEAD 为最终核验对象。
- 附件中的旧行号不能直接作为当前 `main` 的行号使用。
- 附件声称 `04019c8` 工作区还包含未提交改动；远端 GitHub 只能读取已提交对象，不能证明本机未提交内容。此部分仍需在实际开发工作区通过 `git status` 和 `git diff` 核实。
- 当前 `main` 的源码核验不等于对用户本机未提交文件的核验。

### 1.2 核验方法

采用以下静态检查方法：

1. 获取 GitHub `main` 当前 HEAD、`04019c8` 提交及两者 compare 结果。
2. 检查 `graph.py` 的节点注册、普通边、条件边及节点分组。
3. 按附件列出的节点读取节点函数、配置开关、产物写入逻辑和下游映射代码。
4. 检查 `state.py`、相关节点和 contract 对 state/artifact 字段的生产与读取。
5. 对附件特别列出的潜在误判项，单独检查实际调用路径，而不只依赖文件注释。

**本次是远端源码静态核验，没有在容器中 checkout 仓库，也没有执行仓库 pytest 全量测试。** 对“全仓无读者”的判断，本文会区分已核实、抽样核实和需要后续完整 grep/测试的范围，不把静态抽查包装成动态验收。

### 1.3 主要源码入口

| 文件 | 用途 |
|---|---|
| `graph.py` | LangGraph 节点注册、普通边、条件边和功能开关接线 |
| `config.py` | 节点功能开关和默认值 |
| `monitoring/workflow_graph.py` | 工作流图展示元数据、节点名称及边标签 |
| `state.py` | WorkflowState 字段定义 |
| `storyline/contract.py` | CanonicalTimeline 的结构与字段约束 |
| `storyline/mapper.py` | CanonicalTimeline 到剪映草稿原生结构的映射 |
| `draft_ops/draft_integrity.py` | 草稿结构与交付 QA |

---

## 2. 附件关键结论核验矩阵

### 2.1 四档空转节点

| 分类 | 节点/字段 | 当前 main 结论 | 核验状态 |
|---|---|---|---|
| 第一档：结构级 noop（待功能补齐） | `storyline_script_template_recommendation` | 节点只追加 `storyline_script_template_recommendation_done`；注册后仅有 `storyline_generate_script → storyline_script_template_recommendation` 入边，没有以它为源的图边。FireRed 有相近模板检索节点，可复用其元数据检索/过滤能力。 | **当前 noop 已确认；治理方向改为补功能，不推荐直接删除** |
| 第二档：默认关闭 | `storyline_render_video` | `STORYLINE_ENABLE_RENDER_SMOKE_TEST` 默认 `0`；关闭时只记 skipped。开启时只写 JSON 占位产物，没有真实 MP4 渲染，也没有下游读取该产物的业务链。 | **确认并补充修正** |
| 第二档：默认关闭 | `export_final_video` / `node_18_export_video` | `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED` 默认 `False`；关闭时显式 skipped；开启后调用 `draft_ops.draft_exporter.run_export()`。这是有效的可选导出能力。 | **确认** |
| 第三档：旧节点 shim | `node_16_translate_subtitles` | 函数未注册到图；`node_16a_translate_and_check` 和 `node_checkpoint3_layout_review` 会 import 该文件中的纯函数。文件不能整文件删除。 | **确认** |
| 第四档：产物没有进入成片 | `storyline_generate_ai_transition` | 默认开；当前 `get_default_ai_transition_client()` 返回 `StubAITransitionClient`，每个衔接点生成 JSON 占位文件，`duration_ms=500`。 | **确认** |
| 第四档：产物没有进入成片 | `storyline_plan_timeline_ai_transition` | 读取 AI 转场 JSON，写入 Timeline 的 `ai_transitions` 和 `options.enable_ai_transition`，并更新 `storyline_timeline_plan` 路径；但 `storyline/mapper.py` 没有消费 `ai_transitions`，没有物化为剪映草稿素材/片段引用。 | **确认，需精确表述** |
| 已知半截，非 noop | `storyline_select_bgm` | BGM manifest 5 条资源路径全空；BGM 选择仍可输出资源 ID/匹配结果，但没有本地音频文件可自动落草稿。人工关卡②负责人工 BGM。 | **确认配置；人工设计决定以附件记录为依据** |
| 已知半截，非 noop | `node_11_inject_sticker` | 贴纸 ID 仍是 `PLACEHOLDER_*`；节点检查 19 位资源 ID，无效时 fail-closed，不向草稿写入无效素材。 | **确认** |

### 2.2 节点注册数量

当前 `graph.py` 的 `g.add_node(` 数量为 **47**，拆分如下：

| 分组 | 数量 | 统计口径 |
|---|---:|---|
| 主图主干（含草稿、人工关卡、英文分支、交付 QA、可选导出） | 23 | `graph.py` 中 499–539 行的非 assembly 节点 |
| Assembly QC | 6 | `assembly_discover_and_probe` 至 `assembly_write_report` |
| Storyline 自动链 | 18 | `storyline_load_media` 至 `storyline_join` |
| **合计** | **47** | 以 `g.add_node()` 注册次数为准 |

附件的 47 节点总数与分组数量一致。

### 2.3 图结构守卫的证明范围

当前 `tests/integration/test_unreachable_nodes_wiring.py` 的 `test_every_registered_node_is_reachable_from_start()`：

- 从编译图提取节点和边。
- 计算从 `__start__` 可达的节点集合。
- 断言所有注册节点都出现在可达集合中。

它能发现“注册了但从 START 永远走不到”的节点；**不能证明每个节点都存在出边**，也不能证明某条业务边没有被错误遗漏。

建议新增独立测试：

```python
registered = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

outgoing = {
    src
    for src, _dst in _edge_set(compiled)
    if src not in {"__start__", "__end__"}
}

missing_outgoing = registered - outgoing
assert not missing_outgoing, (
    f"这些注册节点没有出边，请显式连接下游节点或 END: {sorted(missing_outgoing)}"
)
```

说明：以上为设计示例，需根据当前 `_edge_set()` 返回的节点 ID 规范适配。终止节点应显式连接 `END`，而不是保持零出边。

### 2.4 `storyline_script_template_recommendation`：当前空节点，但应按 FireRed 实现恢复功能

#### auto-video-editor 当前实现

- `nodes/storyline/node_script_template_recommendation.py` 的函数没有读取业务 state 字段，也没有生成模板推荐 artifact。
- 当前 `graph.py` 将其挂在 `storyline_generate_script` 之后，作为并行 fan-out；因此，即使未来补上推荐逻辑，它也无法影响本轮已经生成的文案。
- 节点只有一条入边，没有任何图内出边。
- `monitoring/workflow_graph.py` 有对应 `_NODE_META` 展示项，后续拓扑变化必须同步更新。

#### FireRed 的相近实现

FireRed-OpenStoryline 提供以下源码：

| 文件 | 实际职责 |
|---|---|
| `src/open_storyline/nodes/core_nodes/script_template_rec.py` | `ScriptTemplateRecomendation`：初始化模板元数据过滤器与向量检索器；按 `user_request` 检索候选，再按 include/exclude 标签过滤，最多返回 3 个候选 |
| `src/open_storyline/utils/recall.py` | `StorylineRecall.build_vectorstore()` 以模板描述字段建立 FAISS 向量库；`query_top_n()` 做语义相似度检索，默认取 Top 32 |
| `src/open_storyline/utils/element_filter.py` | `ElementFilter` 支持标签包含/排除过滤；同一维度多个值按 OR，不同维度按 AND |
| `scripts/llm_script_template_label.py` | 遍历模板 `.txt` 文件，计算 MD5，以 LLM 生成 `description` 与 `tags`，写入 `meta.json`；文件内容变化后重新标注，未变化的模板复用原元数据 |
| `prompts/tasks/scripts/zh/script_template_label.md` | 规定元数据标签生成格式：一句话描述风格/场景，输出 1–3 个标签 |
| `src/open_storyline/nodes/core_nodes/generate_script.py` | 按分组、镜头描述、总览和用户需求生成文案；支持自定义文案输入 |
| `prompts/tasks/generate_script/zh/system.md` | 规定主题、风格、分组素材、字数预算、输出 JSON 和文案事实约束 |

FireRed 的 `RecommendScriptTemplateInput` 包含：

- `user_request`：用户希望采用的文案风格或要求；
- `filter_include`：正向标签过滤；
- `filter_exclude`：排除标签过滤；
- `mode`：`auto / skip / default`。

FireRed 的推荐算法过程：

```text
模板目录 + meta.json
       │
       ▼
按 description 构建向量索引
       │
       ▼
user_request 语义检索 Top 32
       │
       ▼
filter_include / filter_exclude
       │
       ▼
返回 Top 3 candidates
```

**重要限制：FireRed 的 `script_template_rec.py` 本身只返回候选模板。** 本次检查到的 `generate_script.py` 代码读取 `split_shots`、`understand_clips`、`group_clips`、`user_request` 和 `custom_script`，没有显式读取 `script_template_rec` 结果，也没有在代码中读取被推荐模板文件。因此 FireRed 的“模板推荐”和“生成文案”两个节点虽然在节点元数据上相邻，但不能仅凭节点名断言模板内容已经注入文案生成。本项目必须补上这段消费链，不能原样照搬其不足。

另一个资源边界：当前 FireRed 仓库代码中存在配置模型与标签脚本，但 Git tree 中没有找到实际 `resource/script_templates` 模板文本和 `meta.json`。部署配置需要提供 `script_template_dir`、`script_template_info_path`，否则节点无法得到可用模板库。

#### 本项目的目标行为

将该节点从“文案生成后的无用旁支”改造成真正的**文案生成前置能力**：

```text
storyline_group_clips
       │
       ├───────────────┐
       │               │
       ▼               ▼
script_template_rec   search_web_topic（按现有条件执行）
       │               │
       └───────┬───────┘
               ▼
        storyline_generate_script
               │
               ▼
       script.json + template_ref
```

模板推荐节点负责检索、标签过滤和选出模板；`generate_script` 必须读取选中模板的实际文本内容，将其作为 few-shot 风格参考或结构约束注入 user prompt / system prompt。最终 `script.json` 和 `script_llm_trace.json` 需要保留模板 ID、版本、内容 hash 和是否实际应用，便于追溯。

**推荐结论：保留节点并补齐功能，不按原先方案 A 删除。**

### 2.5 `storyline_render_video` 与最终导出不能混为一谈

当前实现：

- 默认关闭：只追加 `storyline_render_video_skipped`。
- 打开开关：创建 `outputs/<session>/storyline/render_smoke_<timestamp>.json`。
- JSON 内写入 timeline 路径、时间戳、`provider=stub` 和 placeholder 说明。
- 没有调用 FFmpeg，也没有生成 MP4。
- `storyline_render_smoke_test_path` 没有进入下游业务节点。

当前节点 docstring 一处写“才跑真实渲染”，另一处又写“写 stub mp4 占位”，实现实际写的是 JSON 文件。文案、扩展名与实现存在三方不一致。

与之不同，`node_18_export_video` 在开关开启后读取 `state.draft_path`，调用 `run_export()` 生成 MP4，并回写 `final_export_status`、`final_export_report` 和成功时的 `final_video_path`。该节点应保留为独立可选能力，不应因默认关闭而归入结构级 noop。

### 2.6 AI 转场：FireRed 已有真实生成与时间线插入实现，本项目应按此补齐

#### auto-video-editor 当前链路

当前项目已经有 `storyline_generate_ai_transition`、`storyline_plan_timeline_ai_transition`、`CanonicalTimeline.ai_transitions` 和 `options.enable_ai_transition`，但默认 client 是 `StubAITransitionClient`，每个衔接点只写 JSON 占位结果。PTAT 把结果写入 `ai_transitions` 列表，但 `storyline/mapper.py` 不消费该字段，因此没有转化为剪映原生草稿中的可播放视频片段。

#### FireRed 的真实实现

FireRed 的 `src/open_storyline/nodes/core_nodes/generate_ai_transition.py` 已实现真实转场视频生成：

1. 输入 `group_clips` 和 `split_shots`，按最终分组次序取得有效片段。
2. 在同一组内部对相邻片段生成转场，并对相邻分组的边界生成转场；对一串 N 个片段，目标是最多生成 N−1 个相邻转场。
3. 校验前后素材的尺寸与宽高比；FireRed 当前使用 `MAX_ASPECT_RATIO_FACTOR = 1.1`，比例差异过大时跳过该对。
4. 从前一个片段取最后一帧，从后一个片段取第一帧；对齐两帧尺寸后，连同用户的自然语言要求交给视觉 LLM 生成转场描述。
5. 调用 `VisionClientFactory` 对应的视频生成 Provider，传入提示词、首帧、尾帧、时长、分辨率和输出目录，生成真实视频文件。
6. 读取生成视频的 `fps`、宽高、时长和文件路径，放入 `transition_info`；生成结果插入 group 的 `clip_ids`，作为真实独立片段。
7. `plan_timeline_ai_transition.py` 遍历含 `transition_*` ID 的分组序列，把转场视频和原视频分别写入 `tracks.video`，累计真实时长，并继续构建 BGM 时间轨。

FireRed 的 `.storyline/skills/ai_transition_editing_skill/SKILL.md` 还明确要求：对用户说明 N 个片段最多需要生成 N−1 个转场、提示第三方 AIGC 成本相对较高，并在执行前征求用户确认。README 也提示 AI 转场成本较高、输出有不确定性，应按需启用。

#### 本项目需要补齐的差异

不能只把 FireRed 的 MP4 路径写进 `CanonicalTimeline.ai_transitions`。本项目应把每个通过校验的转场视频注册成一个真实 `SourceMedia`，并在时间轴 `clips` 中插入一个引用它的 `Clip`：

```text
source_media:
  media_id = transition_media_<stable_hash>
  file_uri = file:///.../transition.mp4
  duration_ms = ffprobe_duration_ms

clips:
  原片段 A
  合成转场片段 A_to_B（source_media_id 指向 transition_media）
  原片段 B
```

这样 `storyline/mapper.py` 就能按现有普通视频 material/segment 逻辑把转场文件写入 `draft_content.json`，而不是新增一套不兼容的剪映结构。`ai_transitions` 可继续保留为审计元数据，但不能再作为唯一的转场表现形式。

还需补齐：

- 从源视频的真实 `source_out_ms` / `source_in_ms` 抽取末帧/首帧，不得误取原视频的绝对结尾或开头；图像与视频素材要分别处理。
- 统一方向、画布比例、色彩空间和分辨率；生成文件必须经过 ffprobe 复检。
- 缓存键由 `from_clip_id + to_clip_id + source content hash + prompt + provider/model + duration + resolution` 组成；QA retry 命中缓存，不重复付费。
- 保留单个 pair 失败降级逻辑，但不能将 Stub 结果当成真实转场；缺失/失败时使用原有硬切或普通 `transition_in/out`，并在 QA/报告标明降级。
- **费用确认**：`STORYLINE_ENABLE_AI_TRANSITION=true` 只表示功能可用，不等于本次任务获得了第三方付费生成授权。每次运行必须有 `ai_transition_approved=true` 或明确的 UI/interrupt 确认；确认前只估算 N−1 数量并给出预计费用/风险，不调用 Provider。
- 时间预算：转场时长必须纳入 `target_duration_ms` 总预算；不得像 FireRed 示例一样只累加转场时长而让本项目 35 秒时长约束失效。Planner 要为转场预留预算，不能事后静默拉长成片。

**结论：FireRed 为第四档 AI 转场“跑了但白跑”提供了可直接对应的功能参考。本项目下一步不是删除 AI 转场，而是补齐真实 Provider、真实视频产物、时间线片段化和 mapper 物化。**

### 2.7 其它重要核验项

| 项目 | 核验结果 | 处理建议 |
|---|---|---|
| `node_16_translate_subtitles` | 函数没有图注册；共享 helper 被 `node_16a` 和 checkpoint3 生产代码 import。 | 仅计划删除旧 shim；保留文件中的 helper。 |
| Assembly QC | `ASSEMBLY_QC_GATE_ENABLED` 默认 true；图中按该开关接入 6 个 QC 节点。 | 保留，不作为空转清理对象。 |
| `assembly_repair_loop` | `_build_repair_patch()` 当前主要写 `repair_marker`、QC 路径等 metadata，真正按问题类型修改时间线的 patch 尚未实现。 | 作为“半自动/修复占位”独立功能项，不应标成 noop。 |
| 人工关卡①② | interrupt/resume 和草稿校验链真实；通知函数仍用 `print`。 | 保留关卡；通知渠道另立任务。 |
| 人工关卡③ | 根据 `layout_issues_detected` 条件触发；resume 后重读草稿并重写 SRT。 | 保留。 |
| `node_15_localize_covers_en` | 仍以 `[EN] {title_zh}` 拼接生成英文标题，不是实际翻译服务。 | 作为 mock/半自动功能单独排期。 |
| 正常推荐转场 | Timeline planner 会把 `storyline_transition_plan` 按 group 写入 clip 的 `transition_in/out`；与 AI 转场旁支是两套链路。 | 保留，不要与 AI 转场空转混为一谈。 |
| 正常文本样式推荐 | `storyline_text_style_plan` 被 Timeline planner、中文/英文字幕相关逻辑使用。 | 保留。 |
| 网页话题检索 | 作为文案生成输入链路存在，不属于空转节点。 | 保留。 |
| `storyline_generate_voiceover` | 结构化 `voiceovers` 带真实文件路径，mapper 可生成配音 audio material/track。 | 保留。 |

---

### 2.8 FireRed 相近节点与可复用范围总表

| FireRed 文件/能力 | 作用 | 对应 auto-video-editor | 迁移策略 | 不应直接照搬的部分 |
|---|---|---|---|---|
| `script_template_rec.py` | 模板语义检索、标签包含/排除过滤、Top 3 候选 | `storyline_script_template_recommendation` | 迁移元数据契约、Top-N 检索、标签过滤、候选结果 | 不照搬 `ElementFilter.filter()` 在无匹配时随机抽样的 fallback；本项目必须确定性降级 |
| `llm_script_template_label.py` + `script_template_label.md` | 从 `.txt` 文案模板生成 description/tags 元数据，按 MD5 增量更新 | 新增模板库构建脚本/离线管理工具 | 复用 `id/hash + path + description + tags` 元数据思路；建议 SHA-256 或内容 hash | FireRed 的脚本使用独立 DeepSeek OpenAI SDK；本项目优先用已配置 LLM，避免另加密钥和隐式外呼 |
| `generate_script.py` + `generate_script` prompt | 根据素材分组和用户需求生成分组文案，支持自定义文案 | `storyline_generate_script` / `storyline_capabilities.generate_script` | 在 prompt 中显式消费模板正文，保持本项目 `narration/group_scripts` 输出契约 | 不改本项目 narration、字幕、配音字段契约；不能把 FireRed `subtitle_units` 直接写入本项目 state |
| `generate_ai_transition.py` | 取相邻片段首尾帧、视觉提示词、视频 Provider 生成真实转场 MP4 | `storyline_generate_ai_transition` | 重写 Stub Client 为 Provider Adapter，产出可验证视频文件 | 不直接依赖 FireRed `BaseNode`、`NodeState`、`MoviePy`；主项目由独立服务或适配器隔离重依赖 |
| `plan_timeline_ai_transition.py` | 将生成转场作为独立视频片段插入视频轨 | `storyline_plan_timeline_ai_transition` + `storyline/mapper.py` | 转换为 `SourceMedia + Clip`，用现有 mapper 生成标准 video material/segment | 不直接照搬 FireRed 的秒制/字符串 duration；本项目使用整数毫秒 |
| `render_video.py` / `RenderVideoPipeline` | MoviePy 合成视频、音频、字幕和视觉元素，写出 MP4 | `storyline_render_video` / `node_18_export_video` | 仅参考素材适配、时间线组合与导出校验；优先复用本项目 `draft_exporter` | 不复制约 1000 行 MoviePy Pipeline 到主环境；避免与 `node_18` 重复生成最终 MP4 |
| `select_bgm.py` | FAISS 检索 BGM 元数据、标签过滤、LLM 选择、Librosa 音乐分析 | `storyline_select_bgm` | 将来自动化 BGM 时可参考；先补齐真实资源路径与 manifest | 本次保留已确认的人工关卡②，不自动绕过人工落轨 |
| `recommend_effects.py` | 推荐转场、字体等元素；根据文案和候选元素让 LLM 选择 | `storyline_recommend_transition` / `storyline_recommend_text` | 可作为候选筛选和元数据结构参考 | 本项目对应节点已有真实消费者，不能按 noop 删除 |

#### 兼容性决策

- FireRed 是 MCP/Agent Tool Node；本项目是 LangGraph Node。适配目标必须是本项目的 `WorkflowState`、artifact JSON、`CanonicalTimeline` 与剪映 `draft_content.json`，不得直接复用 `BaseNode.__call__()` 执行路径。
- FireRed 的 `StorylineRecall` 依赖向量模型和 FAISS；当前主项目依赖没有 `langchain-huggingface`、`faiss-cpu` 等完整检索栈。**推荐先实现零新增重量依赖的确定性检索接口（关键词/BM25 风格评分 + 标签过滤），并保留 `TemplateRetriever` 插件接口；后续确有需要再以单独 sidecar/可选 extra 接入 FireRed 向量检索。**
- FireRed 转场和渲染依赖外部视频生成 Provider、MoviePy/FFmpeg 等组件。主项目只定义 Provider Adapter 和媒体文件契约，不合并两个项目的完整依赖文件。

---

## 3. B 类死键与死文件附录的复核修订

### 3.1 清理原则

“没有下游节点读取”不能自动等同于“可以删除”。state 字段还可能承担：

- 下游节点自身的幂等/重放状态；
- 前端状态卡、工作流事件和 API 最终结果输出；
- 审计字段、运行诊断和人工追溯；
- 旧 checkpoint 的兼容性；
- 自动化测试、E2E 诊断脚本或运维脚本读取。

后续清理需区分：

1. **真正无用字段**：无生产、UI、API、脚本、测试和 checkpoint 兼容消费者。
2. **仅自消费字段**：生产节点会读取自己之前写入的值，可能用于幂等或重试。
3. **输出/审计字段**：没有后继工作流节点读取，但对 UI、人工或诊断有用。
4. **兼容字段**：旧字段仍被保留用于旧 checkpoint、测试或旧客户端。
5. **误判字段**：当前存在真实生产消费者，不得删除。

### 3.2 已确认的误判和注释不一致

| 字段/文件 | 当前源码证据 | 修订结论 |
|---|---|---|
| `audio.voiceover` JSON 映射串 | `nodes/node_08_add_subtitles.py::_voiceover_to_group()` 读取 `plan.audio.voiceover`，解析 `{group_id: voiceover_id}` 并建立反向映射；`_group_spans_from_draft()` 会调用它。 | **从死键清单移除。** 这是实际生产读取路径，不能删除。 |
| `subtitle_scout_report` | `state.py` 已声明该字段；node_08 写回该字段。node_08b 注释仍称该字段未在 state.py 声明，并调用 `_style_from_scout(None)`。 | 不能按附件旧注释推断 state 丢弃字段。修正注释；是否让英文字幕复用 scout 结果，另作行为评审。 |
| `fx_summary` | `draft_integrity.py` 的 `build_qa_categories()` docstring 提到读取 `fx_summary`，但实际函数实现没有对应 state 读取/QA 判断。 | “注释与实现不一致”成立；先修注释或补真正 QA 逻辑，再决定是否保留字段。 |
| `audio_fade_targets` | `node_13_adjust_volume.py` 读取旧 `audio_fade_targets` 后追加/回写。 | 不是纯写-only 字段；至少存在节点自身的读取链，不能与 `volume_adjusted` 合并后直接删除。 |
| `ai_transitions` | `CanonicalTimeline` 有明确字段，PTAT 写入、Pydantic contract 校验；mapper 不生成原生草稿转场。 | 不是“完全没人读取”的字段；应列入“已接收、未物化”缺陷。 |

### 3.3 需要继续全仓复核的字段

以下条目在附件中列为无生产下游消费者。当前检查已确认其写入位置或 state 声明，但本次没有对 868 个 tree entry 的所有代码、前端、脚本和运行时接口逐文件执行完整消费者证明，因此不在此直接批准删除。

| 字段/文件 | 当前已知生产者/行为 | 推荐下一步 |
|---|---|---|
| `storyline_render_smoke_test_path` / `render_smoke_*.json` | `node_render_video` 开关打开后写入 JSON 路径；目前不是真正视频。 | 优先决定删除或更名/实现，不要单独删 state 字段而保留节点。 |
| `script.json.references` | `node_generate_script` 将检索来源写进文案 JSON，定位为人工追溯。 | 确认前端/报告是否展示；若无人展示，作为人工审计内容保留或独立删减。 |
| `script_llm_trace.json` | 文案节点写入 LLM 调用/解析跟踪文件。 | 这是可审计产物；先检查日志保留策略和隐私/磁盘成本，不应只按“没有节点读者”删除。 |
| `storyline_error_code` | QA gate/join 在失败或契约异常时写入。 | 检查 API 状态映射和错误展示；若没有任何消费，统一进入错误报告契约。 |
| `storyline_bgm_selection.bgm_path` | BGM selector 可能返回空路径；当前资源 manifest 的 5 个 `path` 都为空。 | 保留选择结果字段，清理无用路径语义或为真实资源配置路径；不要误删 BGM 选择节点。 |
| `fx_summary` | node_09 写入；当前 draft integrity 只在 docstring 提及。 | 先解决文档/实现不一致，再决定是否下线。 |
| `text_fx_summary` | 中文文本特效节点写入摘要。 | 确认 UI 是否读取；若仅用于调试，改成统一 observability event 或移除。 |
| `volume_adjusted` | node_13 写入执行结果布尔值。 | 独立确认是否有状态卡/API 消费。 |
| `audio_fade_targets` | node_13 自读并回写目标集合。 | 先验证重放/幂等语义；不能和 `volume_adjusted` 一起删除。 |
| `subtitle_scout_report` | node_08 写入 state。 | 更新 node_08b 过时注释；确认是否给前端或诊断使用。 |
| `en_audio_path` / `en_dub_audio_path` / `tts_run_id` | node_17 双写音频路径及运行 ID；音频本体同时写入英文草稿。 | 核对前端播放/下载/重试/审计消费；不要仅因草稿已含音频而删除 state 路径。 |
| `bgm_manual_draft_sha` / `bgm_notified` | 关卡②写入状态/审计字段。 | 检查 checkpoint resume 和事件消费后再删。 |
| `reorder_notified` | 关卡①通知 helper 写入兼容状态。 | 检查 post-resume 幂等与旧 checkpoint。 |
| `checkpoint3_triggered` / `layout_issues_source` | 关卡③、翻译检查节点写入审计状态。 | 若前端/审计不消费，可归并为 checkpoint report；不与关卡逻辑一起改。 |
| `join_qa_issues` | 交付 QA 写入字符串问题列表，同时存在结构化 `join_qa_report`。 | 先检查旧 API/事件消费，再决定是否只保留结构化报告。 |
| `draft_source_normalized_path` / `draft_published_dir` / `draft_publish_error` | node_05 写入路径和发布状态；附件指出真实 draft override 使用 `_draft_dir_override`。 | 检查创建台、发布状态卡和失败提示是否依赖这些输出。 |
| `final_video_path` / `final_export_report` | node_18 成功/失败时写入导出结果。 | 这是导出结果的输出契约；即使没有下游节点，也可能被 UI/API 使用，不能以“没有后继节点”作为删除依据。 |
| `assembly_repair_evidence_path` | assembly repair loop 写入证据路径。 | 附件称 E2E/测试会读；需确认报告生成器是否应汇总该证据。 |

### 3.4 死键清理的最低证据标准

每个拟删除字段必须同时通过以下检查：

```text
[ ] 全仓代码读取检查（Python/TS/JS/PowerShell/脚本/配置）
[ ] 前端状态卡、工作流图、API 响应和 SSE/custom event 检查
[ ] 测试与 E2E 脚本读取检查
[ ] 旧 checkpoint / resume 兼容性检查
[ ] 写入点、TypedDict、文档和测试同步检查
[ ] 清理前后运行样例对照，确认无 UI/交付状态退化
```

没有通过以上检查的字段，只能标记为“候选清理”，不能标记为“已确认可删除”。

---

## 4. 治理目标与设计原则

### 4.1 目标

- 让工作流图中的每个注册节点都有明确业务职责、控制职责或显式终止职责。
- 不允许“只写 status 标签、没有功能、没有下游”的节点长期留在生产图中。
- 不允许功能开关开启后只生成占位 JSON，却在工作流图和 UI 中显示成真实渲染/AI 能力。
- 每个关键 artifact 必须有明确 schema、生产者、消费者和错误处理策略。
- 清理 state 字段时不破坏 LangGraph checkpoint、前端状态卡、工作流事件或旧任务恢复。

### 4.2 设计原则

1. **先找相似功能，再决定删除或补齐。** 对空节点先扫描 FireRed-OpenStoryline 等已引用项目中的相近 Node、Skill、prompt 和资源管理脚本；有可复用实现时，优先补齐功能而不是删除节点。
2. **节点必须有明确的数据契约。** `input → output → consumer` 可追踪；不允许节点只写标签伪装成功。
3. **区分运行时功能与观察性产物。** 供人查看的 trace/report 可以没有后继节点，但必须明确属于审计产物而不是业务产物。
4. **开关默认值要与实现能力和外部成本一致。** Stub 不得伪装成真实能力；第三方 AIGC 节点必须有单次任务授权、费用提示、缓存和取消机制。现有配置默认值与每次付费执行授权是两个不同层次。
5. **状态字段删除要考虑 checkpoint。** LangGraph 会持久化 state 和待执行任务，移除图节点可能影响旧任务恢复。
6. **图拓扑与 UI 元数据同步。** `graph.py`、`monitoring/workflow_graph.py`、schema 版本、测试快照必须同步更新；节点从 noop 改为业务节点时，同样需要补齐输入/输出状态和 UI 摘要。
7. **跨项目只复用能力，不耦合运行时。** 不把 FireRed `BaseNode`/MCP `NodeState` 或重型依赖直接导入 LangGraph 主环境；用适配器转换到本项目 state/artifact/整数毫秒时间线。

---

## 5. 目标架构

### 5.1 节点责任分类

建议在设计规范中采用以下节点类型：

| 类型 | 定义 | 必要条件 | 示例 |
|---|---|---|---|
| `BUSINESS` | 真实处理素材/内容/草稿 | 有业务输入、输出，并有明确消费者或最终交付用途 | `generate_script`、`node_08_add_subtitles` |
| `CONTROL` | 控制流程、等待人工或做路由 | 有 interrupt/条件边/失败终止等明确控制行为 | `node_06_human_reorder`、`delivery_qa_failed` |
| `OBSERVABILITY` | 只产生诊断、trace、报告 | 产物目的明确、保留周期明确，不伪装成业务输出 | `script_llm_trace.json` |
| `OPTIONAL` | 可选真实能力，开关关闭时显式 skipped | 开启后必须真实执行；状态和错误可观测 | `export_final_video` |
| `PLACEHOLDER` | 尚未完成的能力占位 | 不允许默认开启并伪装成功；必须明确 feature flag 和 TODO/issue | AI transition Stub |

建议禁止生产图长期存在没有下游边的 `BUSINESS` 或 `PLACEHOLDER` 节点。确需终止的控制节点必须显式连接 `END`。

### 5.2 Artifact 契约登记表

对关键 artifact 增加维护清单，至少包含：

| 字段 | 含义 |
|---|---|
| `artifact_key` | state key 或文件类型 |
| `producer_node` | 唯一或明确的生产节点 |
| `schema_version` | JSON/TypedDict/Pydantic schema 版本 |
| `consumer_nodes` | 真实读取节点；无节点消费者时写明 UI/API/人工用途 |
| `failure_policy` | 缺失、损坏、stub、过期时如何处理 |
| `retention` | 输出目录、保留时间和清理策略 |
| `status` | `active`、`audit_only`、`deprecated`、`placeholder` |

该表不需要成为新的运行时框架，可先用 Markdown/YAML 维护，并通过测试锁定核心字段关系。

---

## 6. 分阶段执行开发任务

> 本节已按 FireRed-OpenStoryline 相近源码重新设计。目标是**填补空转节点的业务功能**，而不是优先删除。本文档只给出开发计划，没有在 GitHub 执行代码修改。涉及第三方 AIGC 付费调用、资源下载、服务启动、文件写入或删除、图拓扑变更时，仍须按项目既定执行确认流程审批。

### 阶段 0：双仓基线、资源盘点与图结构守卫（P0）

**目标：** 固定本项目和参考项目代码版本，识别能复用的功能与不能直接复用的依赖。

任务：

1. 锁定 `auto-video-editor` 当前基线 `262098953b7f51171e91b12d8c65667a31dc3b76`，锁定 FireRed `b45011d1a7ca7273ccba585cbaef6f3e3b23accd`；执行环境中记录 `git status --short`、`git diff --stat` 和两个仓库的版本。
2. 导出 FireRed `script_template_rec` 的输入输出样例、模板 meta.json schema、AI transition Provider 配置、转场视频产物样例；不含 API Key。
3. 盘点本项目是否有可用模板目录、模板文本、真实音视频素材路径、生成 Provider 配置。FireRed 仓库源码提供模板标签脚本和配置字段，但当前仓库树中未发现实际 `resource/script_templates` 库；因此本项目不得把“代码存在”误认为“数据资源已就绪”。
4. 增加图结构守卫：注册节点均须从 START 可达，且必须具有出边；有意终止的节点需要显式连接 `END`。
5. 增加 artifact producer/consumer 清单测试，重点锁定 `storyline_script_template_artifact`、`storyline_script_artifact`、`storyline_ai_transition_artifact`、`storyline_timeline_plan` 的生产与消费关系。
6. 运行当前测试基线，不做功能删除。

建议测试：

```bash
pytest -q tests/integration/test_unreachable_nodes_wiring.py
pytest -q tests/unit/test_node_16_translate_subtitles.py
pytest -q tests/unit/test_node_09_inject_fx.py
pytest -q tests/unit/test_node_11_inject_sticker.py
pytest -q tests/unit/test_node_12_human_add_bgm.py
pytest -q tests/unit/test_draft_integrity_quality_guards.py
```

验收：双仓基线明确；模板资源缺口有记录；测试能检出零出边节点；本阶段不删节点、不改变 AI 转场默认开关。

### 阶段 1：恢复文案模板推荐功能（P0，核心任务）

**目标：** 把 `storyline_script_template_recommendation` 从结构级 noop 改造成真正影响文案生成的前置节点。

#### 1.1 模板资源和元数据契约

建议目录：

```text
storyline_capabilities/resource/script_templates/
├── templates/
│   ├── daily_vlog_zh.txt
│   ├── travel_story_zh.txt
│   ├── product_review_zh.txt
│   └── documentary_zh.txt
├── meta.json
└── README.md
```

建议 `meta.json` 每项包含：

```json
{
  "schema_version": "1.0",
  "templates": [
    {
      "template_id": "travel_story_zh_v1",
      "path": "templates/travel_story_zh.txt",
      "language": "zh",
      "description": "旅行记录、第一人称轻叙事、注重场景与情绪变化",
      "tags": ["Travel", "Life"],
      "content_sha256": "<sha256>",
      "version": 1,
      "enabled": true
    }
  ]
}
```

兼容 FireRed 的核心字段 `id/path/description/tags`，但本项目应新增 `schema_version/language/content_sha256/version/enabled`，便于缓存失效、版本审计和中英文模板隔离。

任务：

1. 建立模板资源目录与少量初始模板；模板必须是可合法使用的原创/获授权文本，不得未经授权批量收集他人作品。
2. 参考 FireRed `scripts/llm_script_template_label.py` 和 `prompts/tasks/scripts/zh/script_template_label.md` 建立离线标注脚本：扫描 `.txt`、计算内容 hash、生成 `description/tags`、增量更新 `meta.json`。
3. 标注脚本优先调用本项目已配置的 LLM client；不要再单独引入 FireRed 脚本里的 DeepSeek SDK 和第二套 API Key。
4. 文件路径读取必须限制在模板根目录内，防止 `../` 路径穿越；模板缺失、hash 不匹配和编码错误要产生明确 warning，不得伪造候选。

#### 1.2 检索与标签过滤

参考 FireRed `StorylineRecall` + `ElementFilter`，但主项目先实现轻量、确定性的适配层：

- 新增 `TemplateRetriever` protocol，定义 `retrieve(query, top_k)`。
- MVP 使用无重型依赖的关键词/BM25 风格评分；同时保留未来接入向量检索的接口。
- 查询内容由以下信息组合：`script_request/style_request/user_request`、素材 `overall`、group summary/theme、用户指定语气和题材。
- 先检索 Top 32，再执行 `filter_include` / `filter_exclude`，最终返回最多 3 个候选。
- 多维 include：不同维度 AND；同一维度多个值 OR；exclude 命中任意排除标签即剔除。
- **禁止复制 FireRed `ElementFilter` 的随机 fallback。** 如果筛选结果为空，必须返回空候选和 `no_match`，不能随机选一个不相关模板。
- 检索排序必须稳定；相同 query、相同模板库版本应得到相同排序，以便测试与缓存。

如果后续确实需要 FireRed 原版 FAISS 语义召回，应部署在独立 sidecar/可选 extra 中，不把 `torch/moviepy/langchain-huggingface/faiss` 直接合并进主 `.venv`。

#### 1.3 推荐与文案生成的真实接线

当前节点在 `generate_script` 之后，必须调整拓扑：

```text
storyline_group_clips
        │
        ▼
storyline_script_template_recommendation
        │
        ▼
storyline_generate_script
        │
        ├── storyline_generate_voiceover
        ├── storyline_select_bgm
        └── storyline_recommend_text
```

网页话题检索仍按当前条件路由执行，`generate_script` 等待模板推荐节点和现有网页检索条件分支完成。具体任务：

1. 将模板推荐节点的执行位置改为 `storyline_generate_script` 的前置节点：删除原 `generate_script → script_template_recommendation` 入边，新增 `group_clips → script_template_recommendation → generate_script`。
2. 新增 state 字段 `storyline_script_template_artifact`，保存推荐计划 JSON 路径；字段采用 `NotRequired`，旧 checkpoint 缺字段时按无模板模式兼容。
3. 节点输出 artifact 至 `outputs/<job_id>/storyline/script_template_plan.json`，至少包含 `status`、`query`、`candidates`、`selected_template`、`template_id`、`template_version`、`content_sha256`、`retrieval_method`、`fallback_reason`。
4. 候选 Top 3 后，优先按明确用户偏好选择；无明确偏好时用确定性排名选 Top 1，必要时可让当前 LLM 对 Top 3 rerank。LLM rerank 失败时退回排序第一项，并记录 `selection_method=fallback_rank`。
5. 读取所选模板正文，按 token/字符上限截取示例段落。输出 `selected_template_content` 给 `generate_script` 使用，不要只传 ID 或 description。
6. 修改 `storyline_capabilities/generate_script.py`，新增 `script_template` 输入参数，并把模板正文与标签/说明注入 `generate_script` prompt。
7. Prompt 明确模板是“风格/结构参考”，不是事实来源；画面事实必须来自 `overall/group summaries/captions`，不得复制模板中的人名、地点、品牌、数字或事件。
8. 用户明确提供 `custom_script` 时，custom script 优先级最高，跳过模板改写；记录 `template_applied=false, reason=custom_script_override`。
9. `script.json` 加入 `template_ref`（template_id/version/hash）；`script_llm_trace.json` 记录 prompt 是否注入模板以及模板标识，不记录模板库以外的敏感用户数据。
10. QA retry 对相同模板 query 和相同库版本使用缓存；素材分组或 user request 改变后重新推荐。

#### 1.4 推荐节点降级策略

| 场景 | 行为 | 可否继续生成文案 |
|---|---|---|
| 模板库不存在/为空 | `script_template_status=unavailable`，error/warning 留痕 | 可以，按原始 user_request 生成 |
| 没有匹配标签/候选 | `no_match`，不随机抽选 | 可以，不注入模板 |
| 模板文件缺失/hash 错误 | 丢弃该候选，选择下一有效候选 | 有有效候选则可以；否则无模板生成 |
| LLM rerank 失败 | 用确定性 Top 1 | 可以 |
| 模板正文过长 | 按预算截取，保留原始 hash | 可以，trace 标明截断 |
| custom_script 存在 | 跳过模板应用 | 按 custom_script 逻辑执行 |

#### 1.5 测试与验收

新增测试建议：

```text
tests/unit/test_script_template_catalog.py
tests/unit/test_script_template_retriever.py
tests/unit/test_node_script_template_recommendation.py
tests/unit/test_generate_script_template_injection.py
tests/integration/test_script_template_workflow_wiring.py
```

必须覆盖：标签 AND/OR、exclude、空库无随机回退、路径穿越、hash 失效、模板丢失降级、候选排序稳定、模板正文真实进入 prompt、custom_script 优先、qa retry 缓存、旧 checkpoint 无新字段。

**验收标准：** 在同一素材和 user request 下，模板推荐节点实际产出一个有效 `template_id`；`generate_script` 的 trace 能证明模板正文进入了 LLM 请求；生成的 `script.json` 能追溯模板 ID/hash；关闭模板功能或模板库为空时，文案生成仍按原逻辑工作。

### 阶段 2：把 AI 转场 Stub 改成 FireRed 风格的真实转场视频生成（P0/P1）

**目标：** 复用 FireRed `generate_ai_transition.py` 与 `plan_timeline_ai_transition.py` 的流程，把实际 MP4 文件真正写入本项目时间线和剪映草稿。

#### 2.1 Provider Adapter

主项目新增适配接口，例如 `storyline_capabilities/ai_transition_provider.py`：

```python
class AITransitionProvider(Protocol):
    def generate_transition(
        self,
        *,
        first_frame: Path,
        last_frame: Path,
        prompt: str,
        duration_ms: int,
        resolution: tuple[int, int],
        output_path: Path,
    ) -> AITransitionResult: ...
```

`AITransitionResult` 至少包括：`path`、`provider`、`model`、`duration_ms`、`width`、`height`、`fps`、`sha256`、`request_id`、`status`、`error`。API Key 只从服务器端安全配置读取，不通过普通 state、workflow event 或日志暴露。

#### 2.2 参考 FireRed 的生成流程

1. 按最终时间线次序拉平 group clips，找出所有相邻 pair（FireRed 的目标是 N 个 clips 生成最多 N−1 个 transitions）。本项目要明确是“所有相邻片段”还是“仅 group 边界”，不能像当前实现那样只覆盖相邻 group 的边界而产品端误以为所有转场都已生成。
2. 对每个 pair 根据源素材路径和 `source_in_ms/source_out_ms` 抽取上一片段末帧与下一片段首帧。
3. 使用统一显示方向、比例和画布尺寸；两侧宽高比差异超过阈值时跳过并记 `incompatible_aspect_ratio`。
4. 将首尾帧和用户要求发给视觉模型，生成转场自然语言 prompt。
5. 调用配置的真实视频生成 Provider，支持 duration、resolution、取消、超时、进度、重试与错误分类。
6. 输出文件按 job 隔离：`outputs/<job_id>/storyline/ai_transitions/<pair_hash>.mp4`。
7. 对输出执行 ffprobe，确认视频流存在、可解码、时长/宽高/fps 合理；文件 hash 写入 manifest。
8. 单 pair 失败允许降级为普通硬切，但必须记录 warning；不能生成一段假的 JSON 视频文件并返回成功。

#### 2.3 费用授权与缓存

- FireRed 的 AI transition Skill 明确要求提示用户当前片段数 N、潜在转场数 N−1、外部 AIGC 成本较高，并在生成前征求确认。本项目也必须添加本次任务授权字段，例如 `ai_transition_approved`。
- `STORYLINE_ENABLE_AI_TRANSITION` 只表示部署层允许启用该功能；单次任务的付费执行必须另有明确批准。未批准时返回 `awaiting_approval` 或 `skipped:not_approved`，不得调用 Provider。
- 生成缓存键：`from_clip_id/to_clip_id + 两侧素材 hash + 取帧时间 + prompt hash + provider/model + duration + resolution`。
- QA retry 命中相同缓存就复用文件，不重新生成、不重复计费；素材或参数变化才重新生成。
- 配置最大 pair 数、最大总生成时长和费用上限；超过上限必须阻断并要求重新确认。

#### 2.4 Timeline 与剪映草稿物化

1. PTAT 保留 `ai_transitions` 作为审计清单，但必须把通过校验的视频转场转成 `CanonicalTimeline.source_media` 中的新媒体项。
2. 在 `CanonicalTimeline.clips` 中按相邻原始 clip 之间插入合成转场 clip，使用唯一 `clip_id` 与 `source_media_id`。
3. 转场 clip 的 `source_in_ms=0`、`source_out_ms=duration_ms`；时间轴位置由 planner 根据源素材预算和目标总时长重排。
4. `storyline/mapper.py` 不需要再特殊理解“抽象 AI 转场”；它应像普通视频一样生成 `materials.videos[]` 和 `tracks.video.segments[]`。
5. 转场 clip 写入 `metadata` 或 `storyline_meta` 的 source pair 信息，方便后续 QA 与诊断。
6. 不允许同时把同一个 pair 作为独立生成视频片段又作为原生 `transition_in/out` 双重叠加，除非明确设计成复合效果。

#### 2.5 QA 与验收

新增/扩展：

```text
tests/unit/test_ai_transition_provider.py
tests/unit/test_ai_transition_frame_extraction.py
tests/unit/test_ai_transition_cache.py
tests/unit/test_plan_timeline_ai_transition_materialization.py
tests/unit/test_mapper_ai_transition_video_segments.py
tests/integration/test_ai_transition_to_jianying_draft.py
```

验收标准：真实 mp4 存在且可 ffprobe；manifest 与 timeline 的 pair ID 一致；mapper 生成的草稿含对应 video material/segment；剪映预览中能看到过渡视频；时长预算不超限；未批准时 0 次外部生成调用；retry 不重复扣费。

### 阶段 3：处理 `storyline_render_video`，避免重复建设渲染器（P1）

**目标：** 参考 FireRed 的真实渲染能力，但不把 MoviePy 渲染栈直接移入 auto-video-editor 主环境。

FireRed `src/open_storyline/nodes/core_nodes/render_video.py` 有 `RenderVideoPipeline` 与 `RenderVideoNode`，当前实现包含素材适配、图像处理、视频/音频/字幕合成、淡入淡出、音量控制和 `write_videofile()` MP4 导出，是真正的视频渲染节点，不是 JSON stub。但它使用 MoviePy、NumPy、Pillow 和独立字体/资源配置；文件规模超过 1000 行，不能直接替换本项目 75 行 noop 节点。

本项目已有 `node_18_export_video` + `draft_ops/draft_exporter.py` 的 FFmpeg 导出链。其职责是从权威剪映主草稿导出最终 MP4，并明确记录 fidelity warnings。因此建议：

**方案 A（推荐）：把 `storyline_render_video` 改为真实“时间线预览”，但复用现有 FFmpeg 能力。**

1. 在 QA 通过后，将 `CanonicalTimeline` 用 `canonical_to_draft()` 映射为临时 preview draft。
2. 调用现有 `draft_ops.draft_exporter.run_export()` 生成 `storyline_preview.mp4`，文件写入 `outputs/<job_id>/storyline/preview/`。
3. 预览报告明确标记 `renderer=ffmpeg_storyline_preview`、缺失素材、缺失 BGM、未支持特效/字幕等 fidelity warnings。
4. 新增 `storyline_render_preview_path` / `storyline_render_preview_report`，并让创建台预览区域或工作流最终摘要读取它；否则即使产出 MP4，仍然是“生成文件无人消费”。
5. 与 `node_18_export_video` 区分：前者是**初始故事线预览**，后者是**经过剪映编辑与 QA 后的最终 MP4**。

**方案 B：如果产品不需要剪映草稿前预览，删除该旁支。** 删除时同步删除 `STORYLINE_ENABLE_RENDER_SMOKE_TEST`、节点注册、条件边、`monitoring/workflow_graph.py` 元数据、state 字段与测试。最终 MP4 统一由 `node_18_export_video` 负责。

不推荐方案：直接把 FireRed 的整个 `render_video.py` 复制进主项目或为了它将 MoviePy 及其依赖加到主 `.venv`。如果未来明确要求复用 FireRed 原渲染器，应通过独立 Python 运行环境/服务隔离，并通过文件路径或 artifact ID 交换结果。

### 阶段 4：移除 `node_16_translate_subtitles` 旧 shim（P2）

**目标：** 删除过期节点函数，保留共享 helper。

任务：

1. 全仓搜索 `node_16_translate_subtitles(`，确认函数调用方只有旧单元测试。
2. 保留 `nodes/node_16_translate_subtitles.py` 中生产模块仍 import 的纯函数，例如 `_marker_exists`、`_read_segments_from_draft`、`_save_marker`、`_write_marker`、`validate_layout`、`_build_interrupt_payload`、`write_srt_from_segments`。
3. 删除文件底部的 `node_16_translate_subtitles()` 兼容 shim 函数。
4. 删除或改造只验证旧 shim 行为的测试；为 `node_16a_translate_and_check` 和 `node_checkpoint3_layout_review` 补足分工测试。
5. 全量运行字幕翻译、布局检测、人工关卡③和 resume 相关测试。

验收：生产代码仍可 import 所有共享 helper；图上仍只有 `node_16a` 与 `node_checkpoint3_layout_review`；英文字幕生成和关卡③流程没有回归。

### 阶段 5：state 死键与 artifact 清理（P2，分批执行）

**目标：** 减少无消费者字段，但不破坏 UI、API、审计和 checkpoint。

任务：

1. 先修正 `draft_integrity.py` 中 `fx_summary` 读取说明与实现不一致的问题。
2. 将死键分为真正无用、自消费/幂等、UI/API 输出、审计、兼容和误判字段。
3. 新增的 `storyline_script_template_artifact`、AI transition manifest、preview report 不允许成为无消费者文件；必须有对应节点、状态卡、QA 或报告消费者。
4. 每次只处理一组字段，不与业务能力改造混在同一个 commit。
5. 同步修改生产者、读者、`WorkflowState`、序列化 schema、前端类型、API、事件、测试、E2E 和文档。
6. 对旧 checkpoint 保留 `state.get()` 缺省处理或迁移逻辑。

验收：每批字段有生产者/消费者清单、测试覆盖和回滚方式；UI、API、任务恢复和日志审计不退化。

### 阶段 6：拓扑、创建台 UI 与端到端验收（P0/P1）

**目标：** 确保功能实现、LangGraph、Artifact、剪映草稿和 UI 使用同一套事实。

任务：

1. 更新 `monitoring/workflow_graph.py`：文案模板节点变为 `generate_script` 前置节点，显示“文案模板推荐”；如保留 preview，显示为“故事线预览”，不能仍显示成没有真实产物的“渲染视频”。
2. 按需递增 workflow graph `SCHEMA_VERSION`，清理旧拓扑布局缓存。
3. 更新 `graph.py` 邻接关系、节点出边守卫、条件边测试、节点数量测试。
4. 测试场景：
   - 模板库完整、模板库为空、无匹配模板、标签过滤、模板文件损坏；
   - 模板候选进入 `generate_script` prompt，custom_script 优先；
   - storyline QA retry 模板检索命中缓存；
   - AI 转场未授权（不调用 Provider）、授权后成功、pair 失败降级、取消、缓存命中；
   - AI 转场真实文件进入 `CanonicalTimeline`、mapper 和 `draft_content.json`；
   - 初始时间线预览开启/关闭，node18 最终导出开启/关闭；
   - 人工关卡①/②/③、英文分支、交付 QA 和 checkpoint resume。
5. 使用真实视频素材在剪映客户端验收模板文案、AI 转场片段、最终草稿、预览/导出时长和画面效果。
6. 比较实施前后的节点执行次数、LLM token、AI 视频调用次数、产物文件数量、checkpoint 大小和成片效果。

最终验收：没有未解释的零出边节点；模板推荐能影响生成文案；AI 转场真实文件进入剪映草稿；所有生成产物都有消费者；stub 和真实能力在 UI/QA 中明确区分。

---

## 7. 风险与回滚设计

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 删除图节点但旧 checkpoint 仍待执行该节点 | 老任务 resume 失败或拓扑不匹配 | 清理前排空/终止旧任务；必要时保留一版兼容图或迁移任务；在测试中恢复旧 checkpoint。 |
| 删除 `_NODE_META` 后 UI 节点名称回退为原始 ID | 创建台展示不一致 | 同步图注册与 UI 元数据，增加 graph schema snapshot 测试。 |
| 清理 state 字段但前端仍读取 | 状态卡/进度面板显示空值或报错 | 全仓检查 API、SSE/custom event、前端类型和渲染路径。 |
| 关闭 AI 转场默认开关 | 自动工作流行为变化 | 这是显式功能行为变更；必须在发布说明中记录，并提供环境变量回退。 |
| 启用 AI 转场但 mapper 未物化 | 运行耗时和产物增加，成片没有变化 | 在 mapper 完成前禁止将其作为可交付功能；stub 结果不得置 enable=true。 |
| 删除 render smoke 节点但遗漏条件边映射 | LangGraph build 或 QA 通过路由失败 | 同步修改 `graph.py`、`monitoring/workflow_graph.py`、测试路由表。 |
| 把 `node_16_translate_subtitles.py` 整文件删除 | 生产 import 失败 | 只删 shim 函数，保留 helper 文件。 |
| 批量清理死键 | 破坏幂等、审计和外部状态契约 | 按字段组单独 commit，执行状态兼容和 E2E 回归。 |

回滚原则：每个阶段独立 commit；拓扑清理、AI 转场、state 字段清理不能合并为一个大 commit。出现回归时可以按阶段 revert。

---

## 8. 本次不直接删除、优先补齐或保留的节点清单

| 节点/能力 | 当前状态 | FireRed 对照 | 本次治理决策 |
|---|---|---|---|
| `storyline_script_template_recommendation` | 结构级 noop | `script_template_rec.py`、`generate_script.py`、模板标注脚本 | **不删除；P0 恢复模板目录、元数据、检索、候选选择、prompt 注入和前置拓扑** |
| `storyline_generate_ai_transition` | Stub JSON，实际不生成转场视频 | `generate_ai_transition.py` | **不删除；实现真实 Provider、首尾帧、费用授权、缓存与 MP4 输出** |
| `storyline_plan_timeline_ai_transition` | 将清单写进 Timeline，但 mapper 不物化 | `plan_timeline_ai_transition.py` | **不删除；将转场视频转换为普通 SourceMedia/Clip，进入 mapper 与剪映草稿** |
| `storyline_render_video` | 默认跳过；开启后仅写 JSON stub | `render_video.py`、本项目 `draft_exporter.py` | 先选择预览方案 A 或删除方案 B；不复制完整 MoviePy 渲染栈 |
| `export_final_video` / node18 | 默认关闭，开启后调用 FFmpeg 导出 | FireRed `render_video.py` 仅为渲染参考 | 保留为权威最终 MP4 导出能力 |
| `storyline_select_bgm` | BGM metadata path 未配置，人工关卡②负责落轨 | FireRed `select_bgm.py` | 保留用户已确认的人工 BGM 设计；可另立自动音乐库项目 |
| `node_11_inject_sticker` | 有真实写入逻辑，模板 ID 无效时 fail-closed | FireRed ElementFilter/资产库思想 | 保留；解决真实剪映 resource_id 采集，不删除节点 |
| `node_16_translate_subtitles` | 不注册的兼容 shim，helper 被生产代码 import | 无需迁移 FireRed | 只删除 shim，保留 helper 文件 |
| `assembly_repair_loop` | QC 流程真实，patch 仅写 metadata marker | 未找到 FireRed 直接等价节点 | 后续实现按 issue 类型生成真实 timeline patch；不误标为 noop |
| `storyline_recommend_transition` / `storyline_recommend_text` | 已有真实消费者 | FireRed `recommend_effects.py` | 保留，不能因 AI 转场节点问题而清理正常转场/文本样式链 |

## 9. 执行顺序与优先级

| 顺序 | 阶段 | 优先级 | 关键依赖/决策 | 输出物 |
|---:|---|---|---|---|
| 1 | 双仓基线与图结构守卫 | P0 | 锁定 auto-video-editor 与 FireRed commit；不删节点 | 基线记录、拓扑守卫测试、资源盘点 |
| 2 | 文案模板推荐功能恢复 | P0 | 需要准备原创/获授权模板文本和元数据 | TemplateCatalog、Retriever、推荐节点、prompt 注入、script.json template_ref |
| 3 | AI 转场真实生成和 timeline 物化 | P0/P1 | Provider 配置、费用授权、素材帧输入 | Provider Adapter、真实 MP4、CanonicalTimeline clips、mapper/QA |
| 4 | Render smoke 旁支治理 | P1 | 决定保留初始时间线预览还是完全移除 | FFmpeg preview + UI consumer，或旁支/开关/字段同步删除 |
| 5 | node_16 shim 清理 | P2 | helper 依赖确认 | shim 删除、helper 保留、测试迁移 |
| 6 | state 死键分批治理 | P2 | 全仓/前端/API/checkpoint 读者证明 | 字段消费表、state/API/前端/测试同步 |
| 7 | 创建台与剪映真机验收 | P0/P1 | 真实素材、AI Provider 测试额度与授权 | pytest、E2E、剪映预览和导出验收报告 |

**实施顺序约束：** 阶段 1 模板功能和阶段 2 AI 转场可以分支并行开发，但都必须先完成阶段 0 的基线冻结。阶段 3 是否实施取决于预览产品需求；阶段 4–6 不与核心业务功能混合提交。

## 10. 当前结论与后续决策点

### 10.1 当前已可以确定

- 47 个节点数量和三组分类统计没有错误。
- `storyline_script_template_recommendation` 当前确实是结构级 noop，但 FireRed 已有相近模板检索和生成文案能力；本次修订后推荐方案是**补齐功能并前置到 `generate_script` 之前**，不再推荐优先删除。
- FireRed `script_template_rec` 只产出 Top 3 候选，FireRed 的 `generate_script.py` 源码没有显式消费该候选；本项目必须补齐“模板正文进入 prompt”的最后一段链路。
- FireRed `generate_ai_transition.py` 和 `plan_timeline_ai_transition.py` 提供真实 AIGC 转场生成与独立片段插入的实现参考；本项目应把生成的真实视频纳入 `CanonicalTimeline.source_media/clips`，再由现有 mapper 物化。
- FireRed AI 转场流程有高成本提示与用户确认要求；本项目必须增加单次任务授权和费用上限，不能让 `STORYLINE_ENABLE_AI_TRANSITION=true` 自动等同于用户已批准付费调用。
- FireRed `render_video.py` 是真实 MoviePy 渲染器，但本项目已有 FFmpeg `node_18` 最终导出；应避免重复渲染栈和主环境依赖冲突。
- `audio.voiceover` 是附件误判项，存在 node_08 的生产读取，不得作为死键删除。
- `node_16_translate_subtitles.py` 的共享 helper 必须保留，旧 shim 可单独清理。
- `subtitle_scout_report` 的“state 未声明”注释已过时。

### 10.2 实施前需要准备的内容

1. 准备一组合法、可授权的初始文案模板（例如日常 Vlog、旅行叙事、产品介绍、知识解说），并确认模板示例可以进入 LLM prompt。
2. 确认模板推荐的首选检索模式：先使用无重型依赖的本地确定性检索，后续是否另接 FireRed FAISS/embedding sidecar。
3. 确认可用的 AI 视频生成 Provider、模型、分辨率、时长、成本上限与取消机制；本项目先加入显式任务授权，不在未授权状态调用 Provider。
4. 决定是否需要“剪映草稿生成前的 storyline 预览 MP4”；若不需要，删除 `storyline_render_video` 旁支；若需要，采用现有 FFmpeg exporter，避免移植完整 MoviePy Pipeline。

### 10.3 最终建议

**第一优先级是恢复文案模板推荐功能。** 它不需要先引入重型视觉/视频模型依赖，能够以较小改造完成 FireRed 的模板元数据、检索过滤与 few-shot 文案风格迁移能力，并直接改善 `generate_script` 的输入质量。

**第二优先级是补齐 AI 转场端到端链路。** FireRed 已经证明了“首尾帧 → 视觉提示 → 视频生成 → 时间线插入”的技术路径，但主项目必须额外做好整数毫秒预算、幂等缓存、费用授权和剪映草稿 QA。

Render smoke、旧 shim 和 state 死键属于后续治理；不要再把“节点当前是 noop”简单等同于“节点应该被删除”。

---

## 11. 关键源码证据索引（当前 main）

> 下列链接固定到本次核验 HEAD，避免后续 `main` 更新造成行号漂移。

| 主题 | 源码证据 |
|---|---|
| 47 个节点注册 | [`graph.py` L499-L573](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/graph.py#L499-L573) |
| 文案模板节点入边 | [`graph.py` L721-L728](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/graph.py#L721-L728) |
| 文案模板节点只有 status 输出 | [`node_script_template_recommendation.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/storyline/node_script_template_recommendation.py) |
| 图可达性测试 | [`test_unreachable_nodes_wiring.py` L150-L170](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/tests/integration/test_unreachable_nodes_wiring.py#L150-L170) |
| `_NODE_META` 文案模板条目 | [`monitoring/workflow_graph.py` L278-L282](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/monitoring/workflow_graph.py#L278-L282) |
| AI 转场默认开关 | [`config.py` L55-L63](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/config.py#L55-L63) |
| Render smoke 默认关闭 | [`config.py` L95-L104](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/config.py#L95-L104) |
| Assembly QC 默认开启 | [`config.py` L280-L285](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/config.py#L280-L285) |
| 最终导出默认关闭 | [`config.py` L515-L519](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/config.py#L515-L519) |
| Render smoke 实际写 JSON stub | [`node_render_video.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/storyline/node_render_video.py) |
| Node 18 真实导出 | [`node_18_export_video.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/node_18_export_video.py) |
| AI 转场 stub client | [`ai_transition_client.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/storyline_capabilities/ai_transition_client.py) |
| AI 转场节点 | [`node_generate_ai_transition.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/storyline/node_generate_ai_transition.py) |
| AI Timeline 节点 | [`node_plan_timeline_ai_transition.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/storyline/node_plan_timeline_ai_transition.py) |
| AI 转场 contract 字段 | [`storyline/contract.py` L145-L215](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/storyline/contract.py#L145-L215) |
| Mapper 未消费 AI 转场 | [`storyline/mapper.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/storyline/mapper.py) |
| `audio.voiceover` 的生产读取 | [`node_08_add_subtitles.py` L186-L220](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/node_08_add_subtitles.py#L186-L220) |
| `subtitle_scout_report` state 声明 | [`state.py` L180-L190](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/state.py#L180-L190) |
| `fx_summary` 注释与实现 | [`draft_integrity.py` L790-L845](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/draft_ops/draft_integrity.py#L790-L845) |
| BGM manifest | [`bgm_meta_ref.yaml`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/storyline_capabilities/resource/bgm_meta_ref.yaml) |
| 贴纸模板占位值 | [`sticker_template.json`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/templates/sticker_template.json) |
| 贴纸 fail-closed 逻辑 | [`node_11_inject_sticker.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/node_11_inject_sticker.py) |
| node_16 shim 与 helper 保留说明 | [`node_16_translate_subtitles.py` L290-L320](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/node_16_translate_subtitles.py#L290-L320) |
| Assembly repair placeholder | [`node_repair_loop.py` L52-L74](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/assembly/node_repair_loop.py#L52-L74) |
| 关卡①通知仍是 print | [`node_06_human_reorder.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/node_06_human_reorder.py) |
| node_15 英译 mock | [`node_15_localize_covers_en.py`](https://github.com/wayyet/auto-video-editor/blob/262098953b7f51171e91b12d8c65667a31dc3b76/nodes/node_15_localize_covers_en.py) |

---

**核验结论：** 附件主结论基本可信，但不能原样作为删除执行清单。必须先修订 `audio.voiceover` 死键误判、render smoke 的 stub 实现描述，以及图结构测试的证明范围；之后再按阶段治理，避免误删真实生产链路、输出契约和 checkpoint 兼容逻辑。


## 12. FireRed-OpenStoryline 参考源码索引（main 固定提交）

> 以下链接固定到 FireRed `main` 核验提交 `b45011d1a7ca7273ccba585cbaef6f3e3b23accd`，避免后续分支更新造成证据漂移。

| 主题 | FireRed 源码 |
|---|---|
| 仓库 README / 功能说明（few-shot 文风迁移、AI 转场成本提示） | [README.md](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/README.md) |
| 默认编辑工作流 Skill | [default_editing_workflow_skill/SKILL.md](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/.storyline/skills/default_editing_workflow_skill/SKILL.md) |
| AI 转场 Skill（提示 N 个片段最多 N−1 次生成，并要求用户确认） | [ai_transition_editing_skill/SKILL.md](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/.storyline/skills/ai_transition_editing_skill/SKILL.md) |
| 文案模板推荐节点 | [script_template_rec.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/script_template_rec.py) |
| 模板元数据检索 | [recall.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/utils/recall.py) |
| 模板标签包含/排除过滤 | [element_filter.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/utils/element_filter.py) |
| 模板输入 schema | [node_schema.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/node_schema.py) |
| 文案模板离线标注脚本 | [llm_script_template_label.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/scripts/llm_script_template_label.py) |
| 文案模板标签 Prompt | [script_template_label.md](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/prompts/tasks/scripts/zh/script_template_label.md) |
| FireRed 文案生成节点 | [generate_script.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/generate_script.py) |
| 文案生成 Prompt | [generate_script system prompt](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/prompts/tasks/generate_script/zh/system.md) |
| AI 转场真实生成 | [generate_ai_transition.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/generate_ai_transition.py) |
| AI 转场 Timeline 插入 | [plan_timeline_ai_transition.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/plan_timeline_ai_transition.py) |
| 真实 MoviePy 渲染节点 | [render_video.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/render_video.py) |
| BGM 检索与分析 | [select_bgm.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/select_bgm.py) |
| 文本/转场元素推荐 | [recommend_effects.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/recommend_effects.py) |
| FireRed 的 BaseNode / NodeMeta / NodeState 执行模型 | [base_node.py](https://github.com/wayyet/FireRed-OpenStoryline/blob/b45011d1a7ca7273ccba585cbaef6f3e3b23accd/src/open_storyline/nodes/core_nodes/base_node.py) |

> 注：FireRed 的实际模板目录和 `meta.json` 由运行配置提供；仓库源码树中未发现可直接作为默认库使用的完整模板资源，不应假定 clone 后即可检索出候选。

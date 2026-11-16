# 转场/特效使用剪映资源库方案：核验报告与修订执行计划

> 核验日期：2026-11-17  
> 仓库：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 核验基线提交：`27d82170bf94c5b69e3791fc26a88a870f231ac3`  
> 被核验文档：[`docs/第六期开发/auto-video-editor_转场特效直接使用剪映官方资源库_分析与实施计划_20261117.md`](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/docs/%E7%AC%AC%E5%85%AD%E6%9C%9F%E5%BC%80%E5%8F%91/auto-video-editor_%E8%BD%AC%E5%9C%BA%E7%89%B9%E6%95%88%E7%9B%B4%E6%8E%A5%E4%BD%BF%E7%94%A8%E5%89%AA%E6%98%A0%E5%AE%98%E6%96%B9%E8%B5%84%E6%BA%90%E5%BA%93_%E5%88%86%E6%9E%90%E4%B8%8E%E5%AE%9E%E6%96%BD%E8%AE%A1%E5%88%92_20261117.md)

---

## 1. 核验结论

**总体判断：方案方向正确，但当前文档还不适合直接作为代码实施依据。**

文档正确识别了两项现象：现有转场推荐能力不能可靠地产生多样化语义；画面特效模板当前为空，导致 `node_09_inject_fx` 不注入视频特效。复用已有 `resource_id`、原生 material 构造器、`extra_material_refs` 绑定和幂等清理机制，也是合理方向。

但核验发现了几项会影响实施结果的实质问题：

1. **标准生产启动路径默认没有注入真实 LLM。** `start_production.ps1` 调用 `run_workflow.py` 时没有传入 `--llm-backend`，而该参数默认 `None`，入口使用 `StubLLMClient`。所以仅修复提示词，不会自动让标准启动路径调用真实模型。
2. **转场池和视频特效白名单存在资源类型错误。** `pool_zoom` 中的 `推近推远` 不在转场资源段；视频特效候选 `金色光斑`、`金沙` 也不在视频特效资源段，它们实际属于转场资源。
3. **视频特效原生材质结构尚未实机验证。** `native_fx.py` 明确将 `apply_target_type`、`enable_mask`、`render_index`、`value` 等字段标为待 GUI diff 复核。不能把“资源 ID 存在”当作“剪映一定能正确加载和渲染”。
4. **分组语义到时间轴边界的映射没有讲清楚。** `plan_timeline_pro` 把同一个 group 的转场入/出语义写到该组的每个 clip；`node_09` 又优先采用前一个 clip 的 `transition_out`。这会影响组内边界和组间边界的实际转场选择。
5. **硬切与交付 QA 的组合场景没有覆盖。** 当前 `transitions` 默认是必需功能；若多镜头任务的所有边界都被判为 `cut`，注入结果没有转场素材，交付 QA 可能判定失败。原文档只验证了 `node_09` 单节点不写硬切素材，没有验证整条 QA 路由。

因此，建议按本文第 6 节的 P0/P1/P2 顺序修订原方案：先确保真实模型确实被调用，再修复 prompt 和结构化输出，再校验资源池，最后分阶段恢复视频特效。

## 2. 核验范围与证据边界

### 2.1 本次已核验内容

- 读取 `main` 分支提交 `27d8217` 下的目标 MD。
- 检查 `recommend_transition.py`、`node_recommend_transition.py`、`plan_timeline_pro.py`、`node_09_inject_fx.py`。
- 检查 `fx_template.json`、`fx_resource_library.json`、`TemplateLibrary`、`native_fx.py` 和资源生成脚本。
- 检查 `run_workflow.py`、`start_production.ps1`、`_entry_a_llm.py` 的 LLM 后端注入逻辑。
- 检查交付 QA 中 `required_features`、资源 ID 校验、视频特效可选策略。
- 对资源库 JSON 做了名称精确匹配，核对文档中转场池和视频特效候选的资源类型。
- 核对当前仓库中已经存在的相关单测文件和测试断言。

### 2.2 本次没有完成的验证

- 没有在本地克隆仓库并运行 pytest；本报告不宣称任何测试已通过。
- GitHub 对该 HEAD 提交没有返回可用的 commit status，因此无法独立确认文档中的“2124 个 unit tests 全绿”数字。
- 没有在剪映 5.9.0.11632 GUI 中实际下载、保存、重开和渲染 VIP 转场/视频特效。
- 没有读取文档提到的本机未提交工作区状态；GitHub 远端分支无法证明操作者本地工作区有哪些未提交文件。
- 文档提到的截图面板名称，本次只核对了这些名称是否存在于资源 JSON，未独立复核原始截图本身。

以上边界不影响静态代码与资源 JSON 中已经确认的问题，但实机兼容性必须在实施阶段单独验收。

## 3. 已确认正确的内容

| 原文档结论 | 核验结果 | 说明 |
|---|---|---|
| 当前转场映射主要把 fade 系映射到“叠化” | 正确 | `fx_template.json` 中 `fade/fade_in/fade_out/dissolve/crossfade` 都映射到“叠化”。 |
| `recommend_transition` 使用了 `generate_ai_transition` 提示词任务 | 正确 | 当前代码确实调用 `render_prompt("generate_ai_transition", ...)`。 |
| 仓库没有 `recommend_transition` 专用 zh/en prompt 文件 | 正确 | `prompts/tasks/recommend_transition/zh/system.md`、`en/system.md` 当前不存在。 |
| 当前 `video_effects` 模板数组为空 | 正确 | `templates/fx_template.json` 中 `video_effects: []`。 |
| 转场资源总量为 433，其中免费 130、VIP 303 | 正确 | 当前资源 JSON 的 `_counts` 与条目数量一致。 |
| 视频特效资源有 462 条，当前文件中均标记 VIP | 正确 | 资源生成脚本 `extract_video_effects()` 明确过滤 `is_vip != True` 的条目。 |
| 视频特效资源库生成时间为 2026-09-19 | 正确 | 当前 JSON 的 `_generated_at` 为 `2026-09-19T12:48:38+00:00`。 |
| 截图列出的十个转场名称存在于转场资源段 | 名称匹配正确 | 叠化、三屏滑入、中心切开、前后对比、推近 II、穿越 III、横移模糊、水墨、相片切换、顺时针三角均可精确匹配。是否能在当前剪映账号/版本下载，仍须实机验证。 |
| `node_09` 把转场 material 绑定到后一段的 `extra_material_refs` | 正确 | 边界 `i → i+1` 的 material 挂载在 `segments[i+1]`。 |
| `node_09` 有我方 ID 清理与幂等逻辑 | 正确 | `transition-N`、`vfx-N` 是我方确定性 ID；重放时清理未续用的我方引用和 material。 |
| 19 位资源 ID 检查存在 | 正确 | `draft_integrity.check_resource_ids()` 对我方转场/特效 material 做资源 ID 格式校验。 |
| 视频特效缺失不进入 required-feature gate | 正确 | QA 注释和调用逻辑都把 `video_effects` 视为恒可选；但它仍会被做结构/孤儿/模糊类检查。 |

关键代码：

- [recommend_transition.py](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/storyline_capabilities/recommend_transition.py#L36-L100)
- [fx_template.json](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/templates/fx_template.json)
- [资源生成脚本](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/scripts/build_resource_library.py#L107-L158)
- [node_09_inject_fx.py](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/nodes/node_09_inject_fx.py#L200-L300)
- [draft_integrity.py](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/draft_ops/draft_integrity.py#L665-L767)

## 4. 原文档问题清单

### 4.1 P0：标准工作流启动时默认使用 Stub，文档没有处理这个前置条件

**证据：**

- `scripts/run_workflow.py` 的 `--llm-backend` 默认值为 `None`；帮助文本明确说明不传时沿用懒加载 `StubLLMClient`。
- `_entry_a_llm.py` 明确写明未指定 `--llm-backend` 时，LLM 关卡使用 stub 假答案。
- `start_production.ps1` 第 842-847 行调用 `run_workflow.py`，只传 `--production`、`--skip-preflight`、`--thread-id`、`--initial-state-json`，**没有传 `--llm-backend`**。
- `start_production.ps1` 设置的 `CONSOLE_LLM_BACKEND=mcode-cli` 是创建台/监控服务的配置，不能代替 `run_workflow.py` 的 `--llm-backend` 参数。

**影响：**

在这个标准启动路径下，`storyline_capabilities.recommend_transition()` 取得的默认 client 是 stub。Stub 根据 JSON schema 返回空结构，能力层再把缺失的每组值补成 `fade_in/fade_out`。因此，用户看到“全部叠化”并不一定是模型读错 prompt，更可能是这个运行入口压根没有调用真实模型。

即使补上新的 prompt，如果运行时仍然使用 stub，转场依旧不会按内容推荐。

**原文档需要修改：**

- 把“修复 prompt”从唯一根因修复改成第二步。
- 新增“LLM 后端确认”作为 P0 前置任务。
- 明确 `CONSOLE_LLM_BACKEND` 与 `run_workflow.py --llm-backend` 是两条不同配置链。
- 标准生产执行路径必须显式注入真实后端，或在生产模式下没有真实后端时 fail-closed，不能静默继续使用 stub。

**建议实施方式：**

为 `start_production.ps1` 增加独立的工作流 LLM 后端参数（例如 `WorkflowLlmBackend`），并将其传给 `run_workflow.py --llm-backend`。如果生产模式固定使用 MCode，则应显式传 `mcode-cli`，不能仅设置 `CONSOLE_LLM_BACKEND`。

代码证据：

- [run_workflow.py 参数与注入逻辑](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/scripts/run_workflow.py#L95-L138)
- [run_workflow.py 主流程](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/scripts/run_workflow.py#L318-L340)
- [入口 Stub 提示](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/scripts/_entry_a_llm.py#L25-L54)
- [start_production.ps1 工作流调用](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/start_production.ps1#L820-L855)

### 4.2 P0：原文档对 `chat_json` 失败路径的描述不准确

原文档描述：模型输出散文 → `chat_json` 解析失败 → 抛异常 → 进入 `except` → `obj={}`。

**实际代码并非如此：**

`chat_json()` 调用 `chat_json_with_trace()`；后者会捕获 client 调用异常，也会把 JSON 解析失败转换成空字典返回。因此，常见的模型输出格式错误不会穿透到 `recommend_transition.py` 外层 `except`。

正确链路应描述为：

1. 当前调用错误复用 `generate_ai_transition` prompt。
2. 该 prompt 要求模型直接输出英文图生视频 prompt，而不是转场 JSON。
3. `parse_json_loose()` 找不到 `{...}` JSON 时返回 `{}`。
4. `chat_json()` 把 `{}` 返回给 `recommend_transition()`；外层 `except` 通常不会执行。
5. 能力层逐组补齐缺失项为 `fade_in/fade_out`。

还有一个诊断问题：`method` 当前按 `bool(obj)` 判定。Stub 可返回 `{"items": [], "default": ""}`，这是一个 truthy dict，因此会被错误标记为 `method="llm"`。**`method="llm"` 目前不能证明真实模型完成了有效推荐。**

**修订要求：** 使用 `chat_json_with_trace()`，将 `client/backend`、调用是否成功、JSON 是否解析、字段是否通过校验分开记录。只有真实 client 成功调用且结果通过 schema/业务校验，才标记为 `method="llm"`。

代码证据：

- [vlm_client.py：chat_json / chat_json_with_trace](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/storyline_capabilities/vlm_client.py)
- [recommend_transition.py](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/storyline_capabilities/recommend_transition.py#L36-L100)

### 4.3 P0：视频特效候选名称属于错误资源类型

原文档在 `video_effects` 白名单中提出候选：`金色光斑`、`金沙`。

对当前 `fx_resource_library.json` 分段精确检查后的结果：

| 名称 | `transitions` | `video_effects` | 结论 |
|---|---:|---:|---|
| 金色光斑 | 存在 | 不存在 | 是转场，不是画面特效 |
| 金沙 | 存在 | 不存在 | 是转场，不是画面特效 |
| 柔和辉光 | 不存在 | 存在 | 可作为视频特效候选，仍需实机验证 |
| 梦幻辉光 | 不存在 | 存在 | 可作为视频特效候选，仍需实机验证 |
| 金色辉光 | 不存在 | 存在 | 可作为视频特效候选，仍需实机验证 |
| 精致辉光 | 不存在 | 存在 | 可作为视频特效候选，仍需实机验证 |

**影响：** `TemplateLibrary.pick_video_effect_by_name()` 只在 `fx_resource_library.json` 的 `video_effects` 数组查找。把转场名字放入视频特效白名单后，查询结果会是 `None`，`node_09` 会降级为不写入，不会因为名字在另一个资源数组中存在就自动找到。

**修订要求：** 删除 `金色光斑`、`金沙` 这两个视频特效候选。可以把它们留在转场池（如果视觉风格需要），但不能当作视频特效。候选必须先通过 `video_effects` 数组精确命中、19 位 ID 检查，再进入剪映实机验证。上表的四个辉光名称只代表资源类型匹配，不代表已确认其视觉效果安全。

### 4.4 P1：`pool_zoom` 中存在无效转场名称

原文档 `pool_zoom` 写入：`推近 II`、`推近推远`、`中心切开`。

资源 JSON 核验结果：

- `推近 II`：存在于 `transitions`，VIP。
- `中心切开`：存在于 `transitions`，VIP。
- `推近推远`：**不在 `transitions`；它出现在 `video_effects`**。
- `推远 II`：存在于 `transitions`，VIP，可替代 `推近推远`。

**建议修正为：**

```json
"pool_zoom": ["推近 II", "推远 II", "中心切开"]
```

实施时还应增加自动检查：每个转场池成员必须在 `fx_resource_library.transitions` 中精确匹配，且必须有 19 位 `resource_id`。不能只验证名字存在于整个资源文件，因为同名资源可能存在于错误的类型数组。

### 4.5 P1：per-group 转场语义的实际应用范围没有定义

`plan_timeline_pro.py` 第 121-133 行把一个 group 的 `transition_in` 和 `transition_out` 写入该 group 的**每一个 clip**。

随后 `node_09_inject_fx.py` 的 `_boundary_semantics_plan()` 对每个相邻 clip 边界执行：

```python
semantic = cur.get("transition_out") or nxt.get("transition_in")
```

也就是说：

- 只要前一个 clip 有 `transition_out`，下一 clip 的 `transition_in` 就不会被采用。
- 因为 group 的 `transition_out` 被复制到该组所有 clip，所以组内多个 clip 之间也可能反复使用同一组的 `transition_out` 语义。
- `transition_in` 只有在前一个 clip 没有 `transition_out` 时才作为后备值。

**原文档没有说明这是期望行为还是副作用。** 如果设计目标是“每个剪辑边界都根据内容选转场”，应在文档明确组内边界和组间边界都参与轮换，并明确 `transition_out` 优先策略。如果设计目标是“每个 group 之间选一个转场”，则不应把同一语义复制到该 group 的所有 clip；应该只在组末 clip 写 `transition_out`、组首 clip 写 `transition_in`，并按边界生成唯一结果。

**建议：** 采用显式的 boundary-level 设计：先用相邻 clip 的所属 group 计算边界，再为每条边界解析唯一语义。不要让“同一个 group 的两端属性”隐式决定整组内部所有边界。

### 4.6 P1：顶层 `default` 字段校验了也可能不起作用

`recommend_transition()` 返回 `items` 和顶层 `default`。但 `plan_timeline_pro()` 只建立 `transition_by_group`，随后根据 `items` 写入 clip；没有把顶层 `default` 应用到缺失的 group item。

如果新的 prompt 只返回 `default`、漏掉某些 `group_id`，或只返回部分 `items`，那么顶层 `default` 不会自动填充缺失 group。`node_09` 对缺失语义可能走旧 style_tag 兜底，而不是使用推荐计划的 default。

**修订要求：** 二选一并写清契约：

- 推荐方案：解析后保证每个输入 group 都有一个规范化 item；缺失/非法字段用经过校验的 `default` 补齐。
- 或者移除无下游消费者的顶层 `default`，把 fallback 统一放在能力层。

不应保留一个“看上去存在、实际不生效”的字段。

### 4.7 P1：新 prompt 的 `group_id` 输出必须按 ID 对齐，不能只按数组下标

当前代码按 `raw_items[idx]` 与 `groups[idx]` 对齐，并没有按模型返回的 `group_id` 进行匹配。新 prompt 明确要求返回 `group_id`，但原文档没有要求同步改变解析逻辑。

**风险：** 模型可能改变数组顺序、重复 group、遗漏 group 或返回未知 group。如果仅按下标取值，可能把 A 组的推荐套到 B 组；而输出时再强制写入当前 group 的 ID，会掩盖原始错配。

**修订要求：** 以 `group_id` 建立映射，并执行：

- 输入 group ID 必须非空且唯一。
- 输出 group ID 不得重复。
- 未知 ID 拒绝并记录；缺失 ID 用 default 补齐。
- 最终 `items` 顺序按输入 `groups` 排序，不依赖 LLM 返回顺序。
- 每个 `transition_in/out` 必须属于白名单枚举。

### 4.8 P1：视频特效 material 的剪映原生字段尚未实机验证

`jy_common/native_fx.py` 中 `build_native_video_effect_material()` 的注释明确标记以下字段待实机复核：

- `apply_target_type`
- `enable_mask`
- `category_id` / `category_name`
- `render_index`
- `value`

目前代码有构造器，但“能够构造 JSON”不等于“剪映 GUI 能识别并正常渲染”。`node_09` 还会把同一视频特效挂到所有 video segment；如果材质字段或特效类型解释错误，影响范围可能是全片。

**原文档的风险控制不足：** 它计划直接恢复 4-6 个视频特效，之后才做真机验收。建议反过来：先在副本草稿中只注入 1 个特效、1 个片段，通过“加载—下载—调整—保存—关闭—重开—预览/导出”完整验证，再开放多候选白名单。

代码证据：[native_fx.py](https://github.com/wayyet/auto-video-editor/blob/27d82170bf94c5b69e3791fc26a88a870f231ac3/jy_common/native_fx.py#L50-L106)

### 4.9 P1：转场池确定性算法需要避免中文/英文草稿使用不同 seed

原文档提出：`stable_seed = md5(draft_tag + 相邻 clip id)`。

但 `node_09` 分别对中文和英文草稿调用 `_inject_fx_into()`，传入的 `tag` 通常是 `node_09_zh` 与 `node_09_en`。如果把这个 tag 用作 seed 输入，同一边界在两份草稿中会得到不同的索引，导致中英文草稿虽然条数一致，但转场名称可能不一致。

**建议：** seed 不要包含仅用于日志区分的 `tag`。用相同的业务输入构造 seed，例如：

```text
seed_input = job_id + "|" + left_clip_id + "|" + right_clip_id + "|" + pool_key
seed = int(sha256(seed_input.encode("utf-8")).hexdigest()[:8], 16)
```

如果产品希望中英文草稿完全一致，两个草稿必须共享同一个 seed 输入。并且应明确是否要避免相邻边界连续选中同一个转场；仅做模运算并不能保证不重复。

另一个细节：当前 `TemplateLibrary._legacy_pick_by_hash()` 使用 Python `hash()`，它受进程 hash 随机化影响，不适合跨进程作为可重复选择依据。新池选择不能再依赖该旧 fallback。

### 4.10 P1：特效挂载规则中的 `k % 3 == 1` 有索引歧义

原文档写“首尾段 + 中间每第 3 段”，又给出 `k % 3 == 1`，但没有说明 `k` 是从 0 还是从 1 开始。

按 0-based 索引和 1-based 索引，命中的片段位置不同。实施时必须把规则写成唯一公式，并覆盖 0/1/2/3/4/5/6 段等边界情况。

**建议固定为 0-based 索引 `i`：**

```python
selected = {0, n - 1} if n > 0 else set()
selected.update(i for i in range(1, n - 1) if i % 3 == 0)
```

该规则表示首段、末段，以及从第 4 段开始每隔 3 段选择一个中间段（第 4、7、10……段）。若产品想要第 3、6、9……段，应改为 `i % 3 == 2`。无论选择哪一种，都必须在文档和测试中固定，不要只写自然语言。

### 4.11 P1：硬切与默认 required-feature gate 存在整链路边界问题

当前 `draft_ops/draft_integrity.py` 的 `default_required_features()` 把 `transitions` 默认设为 `True`；用户可以通过 `state.required_features` 显式关闭。`video_effects` 则恒不进入 required gate。

`node_09` 对语义 `cut/none/hard_cut` 返回 `cut`，不写入转场 material。这在单节点层面是正确行为，但原文档没有覆盖以下情况：

- 多镜头草稿所有边界都被推荐为 `cut`。
- `node_09` 最终没有产生任何 `materials.transitions`。
- 默认 `required_features.transitions=True`，交付 QA 会把转场缺失作为 required 功能失败。
- 进入 `delivery_qa_repair` 后若仍消费相同的全 cut 计划，重跑 node_09 仍不会产生转场，不能修复这个状态。

**修订要求：** 明确产品规则：

- 若 `required_features.transitions=True`，多镜头草稿必须至少产生一个真实转场；当推荐结果全为 cut 时，应按策略将一个边界回退为安全转场（推荐 `fade`），或明确失败并提示用户调整要求。
- 若用户允许全部硬切，则本次 `required_features.transitions` 应显式为 `False`，QA 将转场归为 `not_applicable`。
- 不允许把“所有边界都按预期硬切”误判成 node_09 注入失败后无限重试。

要新增 `node_09 → node_join_before_delivery → route_after_join_qa` 的整链测试，而不只是 `node_09` 单元测试。

### 4.12 P2：文档对“官方资源库”的描述应更精确

当前资源库由 `scripts/build_resource_library.py` 从仓库 vendor 的 `pyJianYingDraft/metadata` 抽取并生成，属于本地维护的剪映资源元数据镜像，不是工作流实时调用剪映官方公开 API。

因此：

- “资源 ID 在本地元数据中存在”不等于剪映当前客户端一定能下载。
- “剪映账号是 VIP”不等于每个历史 VIP resource ID 都在当前版本可用。
- 当前生成时间是 2026-09-19，与本次核验日期相差约两个月；需把版本兼容性列为实机验收项。
- `video_effects` 当前文件只抽取 VIP 条目；不能把 462 条描述成免费与 VIP 全量视频特效。

建议将文档标题或正文表述改为“使用剪映资源元数据中的官方资源 ID”，并明确 VIP 权限与客户端在线下载仍由剪映客户端校验。

### 4.13 P2：测试文件路径和测试基线描述不够精确

当前相关测试文件实际位置：

| 测试目标 | 当前仓库已有文件 |
|---|---|
| `node_09` 注入与幂等 | `tests/unit/test_node_09_inject_fx.py` |
| 原生 FX material 构造 | `tests/unit/test_native_fx.py` |
| `recommend_transition` capability | `tests/unit/test_storyline_capabilities_b_classes.py` |
| `storyline_recommend_transition` 节点包装 | `tests/unit/nodes/test_node_recommend_transition.py` |

原文档的 `<recommend_transition 测试文件>` 是未完成占位符，应替换成真实路径；建议同时纳入 capability 和 node wrapper 两个测试文件。

另外，文档声称当前 unit 基线为 2124 全绿，本次静态核验没有获得可验证的 CI 状态，也未在本地执行测试。请将它标成“实施前由执行者现场记录的基线”，不能作为本次已经核验的事实。

原文档列出的未提交工作区文件也属于操作者本机状态，无法通过远端 GitHub 证明。实施前必须由执行者运行 `git status --short` 并留存快照。

## 5. 资源库逐项核验结果

### 5.1 当前资源数量与生成信息

| 资源段 | 数量 | 当前数据范围 |
|---|---:|---|
| `transitions` | 433 | 免费 130 + VIP 303 |
| `video_effects` | 462 | 当前只含 VIP；生成脚本过滤免费视频特效 |
| `_generated_at` | — | `2026-09-19T12:48:38+00:00` |

### 5.2 原文档五个转场池成员核验

| 池 | 候选名称 | 当前类型匹配 | 处理建议 |
|---|---|---|---|
| `pool_gentle` | 叠化 | 转场，免费 | 保留 |
|  | 水墨 | 转场，VIP | 保留，实机验收下载 |
|  | 相片切换 | 转场，VIP | 保留，实机验收下载 |
|  | 前后对比 | 转场，VIP | 保留，实机验收下载 |
|  | 融化 | 转场，VIP | 保留，实机验收下载 |
| `pool_zoom` | 推近 II | 转场，VIP | 保留 |
|  | 推近推远 | **视频特效** | 从转场池删除 |
|  | 中心切开 | 转场，VIP | 保留 |
| `pool_slide` | 三屏滑入 | 转场，VIP | 保留 |
|  | 横向滑动 | 转场，VIP | 保留 |
|  | 上移 | 转场，免费 | 保留 |
|  | 下移 | 转场，免费 | 保留 |
| `pool_whip` | 穿越 III | 转场，VIP | 保留 |
|  | 横移模糊 | 转场，VIP | 保留 |
|  | 顺时针三角 | 转场，VIP | 保留 |
|  | 闪屏故障 | 转场，VIP | 保留；注意视觉强度，勿默认高频使用 |
| `pool_creative` | 翻转冲屏 | 转场，VIP | 保留 |
|  | 课本转场 | 转场，VIP | 保留 |
|  | 负片下滑 | 转场，VIP | 保留 |
|  | 赛博马赛克 | 转场，VIP | 保留 |

建议把 `pool_zoom` 修正为：`推近 II`、`推远 II`、`中心切开`。`推远 II` 已在当前 `transitions` 数组中精确匹配。

### 5.3 视频特效候选修正

原文档候选 `金色光斑`、`金沙` 属于转场。以下名称可在当前 `video_effects` 数组精确查到，可作为下一轮视觉评审候选：

- `柔和辉光`
- `梦幻辉光`
- `金色辉光`
- `精致辉光`
- `复古辉光`
- `失焦光斑`
- `幻动光斑`

这份清单只证明“资源类型和名称匹配”，不代表已经证明特效不会改变全片色调、不会产生模糊或不会覆盖主体。第一轮实机验证建议从 `柔和辉光` 或 `金色辉光` 中只选一个，在副本草稿的一条视频段上进行单效果测试。

## 6. 修订后的设计方案

### 6.1 目标与不变项

**目标：** 每条剪辑边界使用可解释、可控、可复现的原生剪映转场；在通过真机验证后，对少量指定片段使用原生画面特效。

**本次不改变：**

- LangGraph 主图结构和节点顺序。
- `node_09_inject_fx` 的中文/英文双草稿处理模式。
- `materials.transitions` / `materials.video_effects` 与 segment `extra_material_refs` 的原生绑定路径。
- `transition-N`、`vfx-N` 我方 ID 约定和幂等清理机制。
- AI 图生视频转场链（MiniMax）及其授权门禁。
- `fx_resource_library.json` 的资源元数据源文件生成方式；本次只更新消费配置，不在运行过程中反向修改资源库。

### 6.2 P0：先固定真实 LLM 运行链

1. 在 `start_production.ps1` 增加独立的 `WorkflowLlmBackend` 参数。
2. 标准生产启动时显式把参数传给 `scripts/run_workflow.py --llm-backend`。
3. 生产模式禁止静默使用 stub；若 backend 为空或为 `stub`，应在工作流启动前明确拒绝或要求显式开发模式。
4. 在启动日志记录实际 backend 名称，不能以 `CONSOLE_LLM_BACKEND` 代替工作流后端信息。
5. 增加测试：`start_production` 的命令参数构造必须包含工作流 LLM backend；`run_workflow` 确认在图执行前完成 `set_default_client()`。

**验收标准：** 真实运行一次时，日志能证明使用 `mcode-cli`（或明确选定的非 stub 后端）；Stub 模式下运行必须被标记为开发/冒烟，不得记录为有效的模型推荐结果。

### 6.3 P0：修复转场推荐 prompt 与输出校验

新增文件：

```text
prompts/tasks/recommend_transition/zh/system.md
prompts/tasks/recommend_transition/zh/user.md
prompts/tasks/recommend_transition/en/system.md
prompts/tasks/recommend_transition/en/user.md
```

要求：

- system prompt 的任务是剪辑转场选择，不是生成图生视频英文 prompt。
- 模型只允许返回 JSON，不允许返回解释性散文。
- 语义枚举固定为：`cut`、`fade`、`zoom`、`slide`、`whip`、`creative`。
- 明确 `cut` 表示不附加转场 material；其它语义分别映射到对应资源池。
- 输入包含完整且可识别的 `group_id` 与 `theme`；可附带用户转场要求和 BGM 节奏摘要。
- `groups` 与 `bgm` 参数必须真正渲染进 user prompt，不能只传给 `render_prompt()` 却没有在模板里使用。

修改 `storyline_capabilities/recommend_transition.py`：

1. 改为 `render_prompt("recommend_transition", ...)`。
2. 使用 `chat_json_with_trace()` 或等价的有诊断信息接口。
3. 对返回 JSON 进行业务 schema 校验；不能仅依赖 `chat_json()` 的宽松解析。
4. 按 `group_id` 对齐，不按数组索引对齐。
5. 检查空 ID、重复 ID、未知 ID、缺失 group 和非法枚举。
6. 用经过校验的顶层 `default` 填充缺失 group；非法 default 回退到 `fade`。
7. 对每个 group 输出规范化 item，输出顺序与输入 groups 一致。
8. `method` 应能区分 `llm`、`stub`、`fallback`、`no_input`，不能只依据返回 dict 是否为空。
9. 把调用异常、JSON 解析失败、schema 错误、枚举回退写入可诊断日志或结构化结果。

### 6.4 P1：修复资源池配置和资源完整性校验

`fx_template.json` 的结构建议：

```json
{
  "transition_semantic_map": {
    "fade": "pool_gentle",
    "fade_in": "pool_gentle",
    "fade_out": "pool_gentle",
    "dissolve": "pool_gentle",
    "crossfade": "pool_gentle",
    "zoom": "pool_zoom",
    "slide": "pool_slide",
    "whip": "pool_whip",
    "creative": "pool_creative",
    "cut": null,
    "none": null,
    "hard_cut": null
  },
  "transition_pools": {
    "pool_gentle": ["叠化", "水墨", "相片切换", "前后对比", "融化"],
    "pool_zoom": ["推近 II", "推远 II", "中心切开"],
    "pool_slide": ["三屏滑入", "横向滑动", "上移", "下移"],
    "pool_whip": ["穿越 III", "横移模糊", "顺时针三角", "闪屏故障"],
    "pool_creative": ["翻转冲屏", "课本转场", "负片下滑", "赛博马赛克"]
  }
}
```

上面是基于当前资源 JSON 的修正候选，不代表所有候选已通过剪映 GUI 兼容性测试。

新增 `TemplateLibrary` API：

- `transition_pool(pool_key) -> list[str]`
- `video_effect_candidates() -> list[dict]`

实现时必须：

- 转场池只查 `_fx_lib["transitions"]`。
- 视频特效候选只查 `_fx_lib["video_effects"]`。
- 每个名称必须精确匹配一个资源条目；重复名称应报配置错误或显式处理。
- 每个资源必须有合法的 19 位数字 `resource_id`。
- 不因 `is_vip=True` 而过滤资源；但把 VIP 下载/账号权限作为 GUI 验收项。
- 池为空、名称不存在、ID 不合法时，返回可诊断 `degraded`，不得静默选用错误类型资源。

### 6.5 P1：明确 boundary-level 语义和确定性选择

建议把“LLM 每个 group 给出 `transition_in/out`”规范化成“每条相邻 clip boundary 最终只有一个语义”。

每条边界：

1. 确定左 clip、右 clip 和各自所属 group。
2. 按固定规则确定语义来源（建议：左侧 group 的 `transition_out` 优先；仅当其缺失时，才使用右侧 group 的 `transition_in`）。
3. 将语义映射到 pool key。
4. 使用左右 clip ID、job ID 和 pool key 计算稳定 seed。
5. 从 pool 中选择真实资源名并核对 resource ID。
6. 生成 material 并绑定到后一段。

确定性算法建议：

```python
seed_input = f"{job_id}|{left_clip_id}|{right_clip_id}|{pool_key}"
seed = int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:8], 16)
index = (boundary_index + seed) % len(pool)
```

约束：

- 不使用 Python `hash()`。
- 不把 `node_09_zh` / `node_09_en` 这样的日志 tag 放入 seed。
- 如果要求中英文草稿完全一致，seed 输入必须完全相同。
- 如需避免相邻边界重复，应增加确定性去重规则，并测试池长度为 1、2 和大于 2 的情况。
- 未收录语义不应直接落到“闪屏故障”这种强视觉默认值；建议统一回退 `pool_gentle`，并记录 fallback 原因。

### 6.6 P1/P2：视频特效恢复必须分两步

**第一步：单资源 canary（小流量验证）**

- 从真实 `video_effects` 资源数组选择一个温和辉光候选，例如 `柔和辉光` 或 `金色辉光`。
- 暂时只绑定到一条测试 segment。
- 使用副本草稿，不修改用户原始草稿。
- 在剪映 GUI 验证：是否识别、是否下载成功、能否调整参数、保存后是否仍保留、关闭重开后是否仍可见、预览/导出是否生效。
- 保存后重新读取 `draft_content.json`，对比 material 字段、resource ID、segment refs 是否发生变化。
- 只有 canary 全部通过，才允许加入 4-6 个候选白名单。

**第二步：按片段规则点缀**

- 仅从 `video_effect_candidates()` 返回的白名单中选择资源。
- 首尾及中间片段的索引规则必须在代码和测试中完全一致。
- 特效按“挂载序号”轮换，不按资源文件的随机顺序选取。
- 对 `intensity` 做 `0.0 <= intensity <= 1.0` 校验或钳制。
- 白名单为空、resource ID 错误或 canary 失败时 fail-closed，不写视频特效 material。
- `video_effects` 仍然保持 QA 可选；不能因为用户没有启用视频特效就让交付失败。

### 6.7 P1：处理 all-cut 与交付 QA

在调用 `node_09` 前或推荐能力层后增加策略校验：

- `video_segments <= 1`：没有剪辑边界，转场 `not_applicable`。
- 多镜头且 `required_features.transitions=True`：至少要有一个真实转场 material；若所有边界均为 `cut`，按明确策略将一个边界回退为 `fade`，或拒绝生成并返回可理解的错误，不要让 QA repair 对同一个全 cut 计划无限重复。
- 多镜头且 `required_features.transitions=False`：允许全 cut，QA 将转场标记为 `not_applicable`。
- 部分边界为 cut、部分边界为真实转场：正常通过，QA 验证真实 material 与 segment 引用完整。

### 6.8 P2：资源元数据版本管理

- 在资源白名单配置中记录资源库生成时间和来源版本。
- 实施前运行名称/ID 完整性脚本。
- 实机验证时记录剪映客户端版本、账号 VIP 状态、网络状态、资源名称、resource ID、能否下载和能否渲染。
- 对不可用资源只从对应池中剔除，不要通过改成另一个类型的资源来“补齐数量”。

## 7. 分阶段执行计划

| 阶段 | 优先级 | 主要改动 | 验收条件 |
|---|---|---|---|
| 0. 基线与后端确认 | P0 | 记录 `git status --short`、当前 commit、运行入口；修复 `start_production.ps1` 工作流 LLM backend 传递 | 启动日志证明非 stub；生产未配置真实模型时明确拒绝 |
| 1. 转场 prompt 与解析 | P0 | 新增 zh/en prompt；修复 `recommend_transition.py`；JSON/schema/group_id/枚举/default 校验 | 真实模型输出能按 group ID 映射；异常和非法值可诊断；stub 不伪装为 llm |
| 2. 资源池与 TemplateLibrary | P1 | 修正 `pool_zoom`；添加 `transition_pool()`、视频特效白名单读取和资源完整性检查 | 每个池成员只存在于 transitions 且 ID 合法；候选特效只存在于 video_effects 且 ID 合法 |
| 3. boundary 语义与 node_09 | P1 | 明确组内/组间边界；池轮换稳定；修复 fallback；保证中英相同 seed | 多边界转场名称可预测、可复现；重复执行 byte-identical；双草稿语义一致 |
| 4. 视频特效 canary | P1 | 副本草稿单资源、单片段验证 native material schema | 剪映加载、下载、保存、重开、预览/导出全部通过 |
| 5. 视频特效点缀 | P2 | 扩展白名单到 4-6 个；实现首尾/中间索引规则；强度校验 | 只有规则段出现效果；无全片误挂载；清理和重放幂等 |
| 6. QA 与回归 | P1 | 增加 all-cut、mixed-cut、required false、双语草稿和 QA repair 整链测试 | 所有功能状态与 `required_features` 一致；不误报、不无限重试 |
| 7. 文档与提交 | P2 | 更新设计文档、变更清单、实机验收记录 | 本任务提交只包含本任务文件；工作区其它改动隔离 |

## 8. 必须新增/修改的测试

### 8.1 Prompt 与推荐能力

在 `tests/unit/test_storyline_capabilities_b_classes.py` 或新建专用测试文件中覆盖：

- zh/en 都加载 `recommend_transition` 专用 prompt。
- user prompt 中包含实际 groups/theme、user request 和 BGM 摘要。
- 模型按输入 group 顺序返回、乱序返回、重复 ID、未知 ID、漏 ID。
- 合法语义完整通过；非法 `transition_in/out` 回退默认值。
- 顶层 `default` 确实被应用，或明确移除不用字段。
- JSON 输出错误、模型异常、Stub 返回分别标记为不同状态。
- `method="llm"` 只能表示真实模型完成了有效输出。

### 8.2 TemplateLibrary 与资源池

新增测试建议：`tests/unit/test_template_library_fx_pools.py`。

- 转场池名称必须命中 `fx_resource_library.transitions`。
- 视频特效名称必须命中 `fx_resource_library.video_effects`。
- `推近推远` 不得进入转场池。
- `金色光斑`、`金沙` 不得进入视频特效白名单。
- 缺失名称、重复名称、空池、非 19 位 ID 均有明确行为。
- 对 5 个 pool 的每一个候选执行完整性扫描。

### 8.3 `node_09_inject_fx`

修改现有 `tests/unit/test_node_09_inject_fx.py`：

- 每条边界按最终语义选择正确 pool。
- 相同输入在不同进程 / 不同 `PYTHONHASHSEED` 下输出一致。
- 中文与英文草稿选出的转场名称一致（若产品要求双语同步）。
- 池中资源名无效时状态为 degraded，不错误绑定其他类型资源。
- cut 边界不写 transition material；mixed cut + transition 正常。
- 组内多 clip、组间边界分别验证预期策略。
- 视频特效只挂目标片段；首尾重叠时不重复生成；1 段、2 段和多段边界规则正确。
- 重放 byte-identical；段数缩减后无孤儿 material/ref；人工 UUID 素材保持不变。

### 8.4 交付 QA 整链

增加或扩展测试，覆盖：

| 场景 | 预期结果 |
|---|---|
| 单镜头 | transitions `not_applicable` |
| 多镜头，部分 cut、至少一个真实转场，required=true | 通过转场类别检查 |
| 多镜头，全 cut，required=true | 按策略回退一个安全转场或明确失败；不重复 repair 空转 |
| 多镜头，全 cut，required=false | transitions `not_applicable` |
| 视频特效白名单为空 | 不注入视频特效；不阻断交付 |
| 视频特效 material 存在但未绑定 | QA 报孤儿/未绑定问题 |
| resource ID 非 19 位 | QA 报资源 ID 错误 |
| 中英草稿 | material 条数、绑定、资源名称符合既定双语一致性规则 |

## 9. 建议测试命令

在 Windows PowerShell、仓库根目录运行当前相关单测：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_node_09_inject_fx.py tests\unit\test_native_fx.py tests\unit\test_storyline_capabilities_b_classes.py tests\unit\nodes\test_node_recommend_transition.py -q
```

新增 `test_template_library_fx_pools.py` 后，将其加入上述命令。

**注意：** 该命令只覆盖相关单测，不等于全量 unit、integration 或剪映 GUI 验收。全量测试基线必须在执行前现场记录，并对新增失败与原有失败做基线对照。

## 10. 实机验收清单

### 10.1 转场

- [ ] 选择每个 pool 至少一个免费和/或 VIP 转场做真实草稿注入。
- [ ] 在剪映中打开草稿，核对名称、时长、方向和视觉效果。
- [ ] VIP 资源能在当前登录账号与当前客户端版本下载并正常显示。
- [ ] 保存草稿、关闭剪映、重新打开，转场仍存在。
- [ ] 相邻边界的转场不出现明显冲突、异常闪烁或时间轴错位。
- [ ] 中英文草稿符合已定义的一致性要求。

### 10.2 视频特效

- [ ] 先只启用一个已验证特效，且只挂到一个测试片段。
- [ ] 剪映能识别素材并在 GUI 中显示正确效果。
- [ ] 调整强度后保存、关闭、重开，参数仍然保留。
- [ ] 预览及导出结果没有全片模糊、意外色偏、频闪或覆盖主体。
- [ ] 确认效果只出现在目标片段，未被错误挂到全片。
- [ ] 单资源 canary 通过后，才扩大到白名单 4-6 个效果。

### 10.3 QA 与交付

- [ ] `required_features.transitions=True` 时转场 gate 的行为符合 all-cut 处理策略。
- [ ] `required_features.transitions=False` 时允许用户选择全硬切。
- [ ] `video_effects` 缺失不会阻断交付；但一旦写入，资源 ID 和 segment refs 必须完整。
- [ ] `node_09` 重放无重复 material、无悬空引用。

## 11. 需要修订的原文档位置汇总

| 原文档章节 | 必须修订的内容 |
|---|---|
| §1.3 根因① | 加入真实 LLM 后端未注入的问题；修正 `chat_json` 解析失败并非外层抛异常的描述 |
| §1.4 资源库 | 保留数量统计；补充生成日期、提取范围和元数据镜像边界；校验池成员资源类型 |
| §1.5 已实证通道 | 区分转场已实证与视频特效字段尚待 GUI 验证；不要把两者成熟度写成相同 |
| §2 设计决策 | 明确 LLM backend；明确 group/boundary 语义；规定 all-cut 与 required gate 策略 |
| §3.1 Prompt | 新 prompt 文件路径正确；输出 JSON schema；加入 group_id 严格校验 |
| §3.2 recommend_transition | 使用 trace、按 group_id 对齐、default 真正生效、准确标记 stub/fallback |
| §3.3 fx_template | `pool_zoom` 将 `推近推远` 替换为 `推远 II`；视频特效候选删除 `金色光斑`、`金沙` |
| §3.4 TemplateLibrary | 写明按资源类型查找、精确名称和 19 位 ID 校验 |
| §3.5 node_09 | 共享 seed，不使用中文/英文 tag；明确定义 pool 空/错误处理；视频特效逐片挂载索引规则 |
| §3.6 测试 | 填入实际测试文件路径；新增资源类型、跨进程确定性、all-cut QA 和 GUI canary 测试 |
| §4 验证 | 把静态单测、整链 QA、剪映 GUI 验收分开；不得以 material 写入数量代替剪映实际渲染成功 |
| §5 风险 | 增加 stub backend、VIP entitlement、元数据时间、视频特效原生字段未验证、all-cut QA 风险 |

## 12. 最终建议

不要直接按原文档一次性恢复五个转场池和 4-6 个视频特效。推荐实施顺序为：

1. **先修正工作流 LLM backend 注入**，确保真实模型确实被调用。
2. **修复 recommend_transition 专用 prompt 和结构化输出**，解决全叠化的推荐能力根因。
3. **修正资源池名称和类型**，加入程序化完整性扫描。
4. **修正 group 到 boundary 的映射并实现稳定轮换**，保证双语草稿和重放行为可预测。
5. **先单资源验证原生视频特效字段**，通过剪映 GUI 后再开启点缀策略。
6. **补齐 required-feature 与 all-cut 整链测试**，最后再扩大资源池并执行全量回归。

在完成这些修订前，原文档可以作为方向性方案，但不应当作为直接照单执行的代码任务清单。

---

## 13. 执行记录（2026-11-17，本文档 §6/§7 方案已全量实施）

**实施基线**：`60a4e50`（英文分支任务已全部入库，工作区除本文档外干净）→ **终态 HEAD**：`c03e6fb`。共 5 个提交：

| 提交 | 阶段 | 内容 |
|---|---|---|
| `1ad3000` | P0 | 生产链 LLM 后端 fail-closed：start_production.ps1 Phase 2 显式传 `--llm-backend`（取 CONSOLE_LLM_BACKEND，兜底 mcode-cli）+ run_workflow.py `--production` 未显式传后端 → 退出码 2 拒跑（显式 stub 仍允许，创建台 E2E 不受影响）；ps1 纯文本断言测试防回退 |
| `a989fad` | P0 | 转场推荐专用提示词+结构化解析：新增 `prompts/tasks/recommend_transition/{zh,en}/{system,user}.md`（受控词表 cut/fade/zoom/slide/whip/creative；user.md 真正渲染 groups/bgm/user_request）；recommend_transition.py 重写（chat_json_with_trace 带诊断、group_id 对齐、空/重复/未知 ID 拒绝记录、default 补齐、method 区分 no_input/llm/stub/fallback、warnings 返回）；节点包装层 warnings 落 error_log + method 记状态标签 |
| `12af907` | P1 | 转场风格池轮换+特效克制点缀：fx_template.json 重构（语义映射→池 key、五池真实转场名、pool_zoom 按 §5.2 修正、video_effects 白名单全量启用 6 候选）；TemplateLibrary 新增 `transition_pool()`/`video_effect_candidates()`；node_09 池化重写（sha256(job\|boundary\|pool) 确定性轮换、不含 zh/en tag、禁 python hash()、未收录语义回退 pool_gentle、特效 0-based 位点 {0,n-1}∪{i∈[1,n-2]且i%3==0} 按挂载序号轮换、intensity 钳制 [0,1]、空白名单 fail-closed、all-cut+required→边界 0 保底 fade）；test_template_library_fx_pools.py 真实库分段完整性全量扫描；test_node_09_inject_fx.py 按 §8.3 重写 25 例 |
| `0d62ab0` | P1 | `scripts/canary_video_effect.py`：副本草稿（`<名>_canary`）单资源单片段特效注入 + 19 位校验 + 原子双写（剪映在跑拒写）+ GUI 验证清单 + canary_summary.json；冒烟已过（合成草稿 3 段，柔和辉光 resource_id 7249209626829263421 只挂第 1 段） |
| `c03e6fb` | P1 | 交付 QA 整链场景补齐（§8.4）：混合硬切+required → passed；all-cut 保底产物 + required → passed（不无限 repair） |

**操作员决策落地**：特效白名单**直接全量启用**（6 候选），canary 脚本作为事后复核工具（不阻塞生产）。

**测试结果**：
- 改动前基线：6 个相关测试文件全绿（exit 0）；
- 全量 unit：exit 0 零失败（约 2100+ 用例）；
- 全量 integration：3 失败 = 存量基线逐字同名（`test_sequential_produces_complete_draft` / `test_tail_chain_applies_to_both_drafts` / `test_load_media_then_split_shots_then_qa_gate_chain`，记忆基线在案），零新增。

**关键实现决策**（执行中的裁量）：
1. 池轮换 seed 用 `job_id|boundary_index|pool_key`（§4.9 建议公式中的 clip id 改为 boundary_index：zh/en 草稿 clip id 可能分叉，job_id+索引对双语更强健且同样跨视频变化）；
2. all-cut 判定只认"显式映射为 null 的语义"——语义缺失/未收录会走 pool_gentle 产出转场，不触发保底；
3. 空白名单特效 = status_log 说明（`*_vfx_whitelist_empty`），不再刷 error_log degraded 噪音；白名单非空但解析失败仍 degraded 报错；
4. all-cut 保底动作记 status_log（`*_all_cut_fallback`），不当错误处理；
5. fx_template.json 旧 `transitions` style_tag 数组已删除（兜底职责由 pool_gentle 接管）；`pick_transition`/`pick_video_effect` 旧 API 保留兼容但 node_09 不再使用。

**遗留（待操作员）**：
- §10.1/§10.2/§10.3 真机验收清单逐项执行（新建任务跑全链 → 打开草稿核对转场多样性/VIP 下载/特效只挂规则段/交付卡通过）；
- canary 可选复核：`.venv/Scripts/python.exe scripts/canary_video_effect.py --draft <AutoVideo_xxx>`；
- video_effect 原生字段（apply_target_type/enable_mask/render_index/value）以此轮真机验收定案，异常候选从白名单剔除；
- 推送 GitHub：待 /sync-github。


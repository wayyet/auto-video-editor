# auto-video-editor 交付 QA 故障分析与修复计划（审查修订版）

- **审查日期**：2026-11-23
- **仓库**：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)
- **审查分支**：`main`
- **审查基准 Commit**：`94c0a6e43bb1df65d19d0fb4b802897467be55a0`
- **被审查文档**：[`docs/第10期开发/交付QA失败根因分析与修复计划_console-3e866b87d5d6_20261122.md`](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/docs/%E7%AC%AC10%E6%9C%9F%E5%BC%80%E5%8F%91/%E4%BA%A4%E4%BB%98QA%E5%A4%B1%E8%B4%A5%E6%A0%B9%E5%9B%A0%E5%88%86%E6%9E%90%E4%B8%8E%E4%BF%AE%E5%A4%8D%E8%AE%A1%E5%88%92_console-3e866b87d5d6_20261122.md)
- **审查方式**：GitHub `main` 分支静态代码核对、调用链分析、测试用例覆盖审查。未连接用户本机，不读取本机 `outputs/`、剪映草稿、PostgreSQL checkpoint，也未在用户环境执行测试。

> 本次请求在“根据以下几点”之后没有附加独立的功能需求清单。因此，本报告以指定 QA 文档中的事故、根因假设和修复方案作为审查范围，并补充必要的工程化约束。

---

## 1. 总体结论

**结论：原文档的核心故障机制判断基本成立，但取证方法、异常边界、修复结果传播和回归验证设计存在缺陷。建议修订后实施，不建议直接按原步骤照搬。**

### 1.1 已由当前代码支持的判断

1. `graph.py` 中，`node_11_inject_sticker` 先于 `node_09b_inject_opening_hook` 执行；开场标题是在 SDK 首次编译之后写入草稿的。
2. `timeline_extractor.py` 会把不属于 spec 字幕计划、也不匹配我方字幕 material 前缀的文本段标记为 `manual_text_track`。
3. `compiler.py` 在 `spec.timeline.blocked_features` 非空时抛出 `UnsupportedManualFeatureError`，阻止重建草稿。
4. `node_delivery_qa_repair.py` 的重编译确实忽略 `compile_and_publish_draft()` 返回的状态记录。
5. `pipeline.py` 会把编译失败作为结果字典返回；因此调用方不能只依赖异常传播来判断失败。
6. 现有单元测试覆盖了通用人工文本阻断、开场钩子写入和 QA 路由，但没有覆盖“hook 文本段经过提取、QA repair 重编译、编译后恢复 hook”的完整闭环。

### 1.2 必须修正的结论

- 原文档建议的 `/api/workflow/runs/{thread_id}` 查询方式**不能取得完整 checkpoint state**，不能从响应的 `state` 字段直接读取 `status_log` / `error_log`。
- 原文档把 `pipeline.py:99-114` 描述为能捕获“提取/指纹阶段异常”，不准确。该 `try` 只包围 `compile_spec()`；提取和指纹计算发生在 `try` 之前。
- “没有写入审计记录”是首编没有完成正常审计发布流程的强线索，但单凭缺少记录不能证明 `write_draft()` 完全没有被调用。
- 原文档提出的 hook 重放方案没有定义 LangGraph state 更新如何返回和合并；按当前调用约定，新增的 `status_log` / `error_log` 很可能仍然丢失。
- 还需要处理 `skipped_fingerprint`。QA 修复场景不能把“构建输入指纹未变”直接等同于“草稿中的必需效果实际存在”。

### 1.3 建议实施方向

采用**窄范围识别我方 hook + QA repair 显式检查编译回执 + 必要时强制重建 + 编译后恢复 hook + 合并 state 更新 + 端到端回归**的闭环。

这比单独添加 `hook-title-seg-` 白名单更完整，也避免修复节点报告“已重试”但实际没有完成编译的情况。

---

## 2. 当前代码事实与调用链

### 2.1 节点执行顺序

`graph.py` 当前尾链为：

```text
node_09_inject_fx
    ↓
node_11_inject_sticker       # 发射贴纸 spec，并进行 SDK 汇合编译
    ↓
node_09b_inject_opening_hook # 原生写入开场标题 / 开场视频特效
    ↓
node_12_human_add_bgm
    ↓
node_13_adjust_volume
    ↓
join_before_delivery        # 交付 QA
    ↓
route_after_join_qa
    ├── QA 通过 → export_final_video
    └── QA 未通过 → delivery_qa_repair → join_before_delivery 复检
```

代码依据：[`graph.py` L678-L710](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/graph.py#L678-L710)。

因此，正常路径中的 node_11 首次编译发生在 hook 写入之前；但 QA repair 是在 hook 已经写入后运行。两个时点的输入草稿并不相同。

### 2.2 hook 被识别为人工文本的机制

当前 [`timeline_extractor.py` L470-L497](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/jianying_sdk/timeline_extractor.py#L470-L497) 对所有文本轨执行检查：

- 命中 spec 字幕 ID 或显示文本：认领为计划字幕；
- 命中 `text-zh-` / `text-en-` material ID 前缀：认领为工作流字幕；
- 其他文本段：归入 `manual_text_track`。

`node_09b_inject_opening_hook.py` 创建的标题段使用 `id_prefix="hook-title-seg"`，material ID 为 `hook-title-1`，轨道 ID 为 `track-text-hook`。这些标识没有进入提取器现有的工作流字幕识别规则。

代码依据：

- [`nodes/node_09b_inject_opening_hook.py` L210-L275](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/nodes/node_09b_inject_opening_hook.py#L210-L275)
- [`timeline_extractor.py` L470-L497](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/jianying_sdk/timeline_extractor.py#L470-L497)

### 2.3 编译器为什么阻断

[`compiler.py` L528-L551](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/jianying_sdk/compiler.py#L528-L551) 在生成 SDK 草稿之前检查 `spec.timeline.blocked_features`。列表非空即抛 `UnsupportedManualFeatureError`。

这属于保护机制：SDK 当前不能保证完整重建未知人工编辑时，不应覆盖现有草稿并静默丢失内容。就“未知人工内容不应被误删”这一目标而言，fail-closed 设计合理；问题在于工作流自产的 hook 没有被正确识别为我方内容。

### 2.4 SDK 编译回执的真实语义

[`pipeline.py` L81-L114](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/jianying_sdk/pipeline.py#L81-L114)：

- 第 81-89 行：提取当前时间线并更新 spec；
- 第 91-97 行：计算指纹并判断是否跳过；
- 第 99-114 行：仅对 `compile_spec(spec)` 做异常捕获，失败后返回 `failed` 或 `failed_blocked_features` 字典；
- 第 116-148 行：执行安全写入并返回 `compiled` 或 `failed` 等结果。

正常返回状态至少包括：

| 状态 | 含义 | 调用方处理要求 |
|---|---|---|
| `compiled` | 编译并发布流程完成，仍需检查 `write.ok` | 校验发布结果，再做 hook 恢复和 QA |
| `skipped_fingerprint` | 构建输入指纹相同，跳过重建 | 不能无条件视为草稿内容完整；需做输出后置校验 |
| `skipped_no_spec` | 没有可用 spec，按兼容策略跳过 | 不应伪装成修复成功 |
| `failed_blocked_features` | 检测到无法安全重建的特征 | 保留现有草稿，记录失败原因 |
| `failed` | 编译或发布失败 | 记录分支、阶段、异常和草稿是否保持不变 |

`node_11` 已经检查上述主要状态并记录到返回的 state；QA repair 中的 `_recompile_drafts()` 没有做到这一点。

代码依据：

- [`pipeline.py` L32-L57](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/jianying_sdk/pipeline.py#L32-L57)
- [`pipeline.py` L91-L148](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/jianying_sdk/pipeline.py#L91-L148)
- [`node_11_inject_sticker.py` L141-L173](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/nodes/node_11_inject_sticker.py#L141-L173)

### 2.5 QA repair 的当前行为

[`node_delivery_qa_repair.py` L41-L75](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/nodes/node_delivery_qa_repair.py#L41-L75) 对中英文草稿分别调用 `compile_and_publish_draft()`，但没有接收或检查返回字典。

[`node_delivery_qa_repair.py` L142-L158](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/nodes/node_delivery_qa_repair.py#L142-L158) 中，repair action 返回值也被丢弃，最终只返回 retry count 和少量 `status_log` 标签。

因此，当前 `delivery_qa_repaired:transitions` 代表“执行过该修复动作”，不代表 SDK 已成功编译、发布或通过 QA。原文档指出“回执未消费”的方向正确，但还需要把整个 state 传播路径一起修复。

---

## 3. 原文档问题清单

| 编号 | 严重度 | 原文档问题 | 代码核对结果 | 修订要求 |
|---|---|---|---|---|
| R-01 | 高 | 用 `/api/workflow/runs/{thread_id}` 获取 `state.status_log` / `state.error_log` | 该 API 返回事件快照；`state` 是从最近事件派生的运行状态，不是 checkpoint 的完整 state 对象 | 改为使用 LangGraph checkpointer 的 `aget_state(config).values` 读取 checkpoint；HTTP API 只用于核对事件快照 |
| R-02 | 高 | 称 pipeline 在 99-114 行捕获“compile_spec 或前置提取/指纹”异常 | `extract_timeline_snapshot()` 和 `compute_build_fingerprint()` 在 try 之前；该 try 只捕获 `compile_spec()` | 分开描述提取异常、指纹异常、compile 异常、发布异常四个阶段 |
| R-03 | 中高 | 以“没有 node_11_compile 审计记录”证明没有进入发布 | `write_draft()` 在 `safe_write_draft()` 返回后才追加审计；若写入过程异常或审计本身失败，也可能没有记录 | 把它表述为“没有观察到成功追加的发布审计”，不能单独作为排除发布阶段的充分证据 |
| R-04 | 高 | 提议 `_recompile_drafts()` 里追加 `error_log` / `status_log`，但没有定义 state 更新返回方式 | `_recompile_drafts()` 当前返回 `None`；`delivery_qa_repair()` 对 `action(state)` 的返回值不合并 | 明确返回 `dict` 更新并在 `delivery_qa_repair()` 合并，禁止依赖原地修改 state |
| R-05 | 高 | 仅以 `status == "compiled"` 决定后续动作，没有定义 `skipped_fingerprint` | pipeline 有 `skipped_fingerprint` 快捷返回；该判断只比较输入指纹，不证明草稿当前实际含有 QA 所需的转场/动画 | 对 QA repair 增加强制重建或输出完整性校验；跳过状态必须单独处理 |
| R-06 | 中高 | 只按 `hook-title-seg-` 前缀豁免人工文本 | 当前确实能解决本次标题段识别，但单独信任 segment ID 范围过宽 | 同时校验 hook 专用轨道 ID、segment ID 前缀、material ID 和素材引用关系，其他人工文本仍阻断 |
| R-07 | 中高 | 假设调用 `inject_opening_hook(state)` 后，其日志会自然进入 QA repair state | `inject_opening_hook()` 返回新 state；当前 repair action 返回值会被丢弃 | 显式合并 hook replay 的日志更新；避免重复重放两份草稿和重复追加日志 |
| R-08 | 中 | 以英文分支重编译成功证明“编译管线本身是好的、spec 内容可编译” | 只能证明英文分支当次输入成功；中英文 spec、素材、字幕样式和文本资源彼此独立 | 改为“英文分支提供了一个成功对照，但不能排除中文分支特有输入或资源问题” |
| R-09 | 中 | 首编失败后主要依赖 sidecar `last_result` 及最终状态定位原因 | `last_result` 是覆盖式字段，后续重编译会覆盖前次结果；当前并非完整编译尝试历史 | 增加 append-only 编译尝试日志，记录阶段、分支、状态、输入指纹、异常类型和时间 |
| R-10 | 中 | 测试重点是提取器单测和 mock repair，没有完整验收矩阵 | 现有 repair 测试未验证编译返回状态、hook 恢复及 state 合并；hook 测试也未验证 extractor 的识别行为 | 增加 extractor → compiler → repair → hook replay → QA 的集成测试和双语真机测试 |

### 3.1 R-01：运行查询接口必须更正

当前 [`web/api_workflow.py` L220-L254](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/web/api_workflow.py#L220-L254) 的 `/runs/{thread_id}` 返回的是事件存储快照。`_snapshot()` 只把 `latest_snapshot()` 结果补上 `state` 字段；事件存储 [`workflow_event_store.py` 的 `latest_snapshot()`](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/monitoring/workflow_event_store.py) 返回事件计数、游标、最近事件和节点 attempt 汇总。

所以原文档中的：

```powershell
curl.exe -s "http://127.0.0.1:7860/api/workflow/runs/console-3e866b87d5d6" -o run_state.json
```

可以保留为**查询运行事件快照**的命令，但不能再把响应解释为完整 checkpoint state。正确取证路径应当是：

1. 使用相同的 PostgreSQL checkpointer 配置和 `thread_id`；
2. 在项目运行环境中调用 LangGraph `aget_state({"configurable": {"thread_id": "console-3e866b87d5d6"}})`；
3. 检查返回 snapshot 的 `values` 中 `status_log`、`error_log`、`draft_path`、`draft_dir_en_branch` 等字段；
4. 同时用运行事件接口确认 `node_11_inject_sticker`、`delivery_qa_repair`、`delivery_qa_failed` 的节点事件顺序。

注意：事件 API 是监控投影，不是 checkpoint 原始数据接口；不得从事件摘要反推没有被投影的完整 state。

### 3.2 R-02：首次编译错误分类需要拆开

当前 pipeline 的异常边界如下：

```text
load_build_spec
    ↓
_load_draft_safe
    ↓
extract_timeline_snapshot       # try 外
    ↓
canvas_of / compute_build_fingerprint  # try 外
    ↓
指纹相同？ → skipped_fingerprint
    ↓ 否
compile_spec                    # try 内，异常返回 failed / failed_blocked_features
    ↓
write_draft                     # 独立 try，发布异常返回 failed
    ↓
write_build_spec                # 当前不在 compile_spec 的 try 内
```

因此第一次异常应记录**最后成功阶段**和**异常发生阶段**，不能全部统称为“compile_spec 失败”。

### 3.3 R-03：审计缺失的证据强度

[`nodes/_draft_io.py` L217-L264](https://github.com/wayyet/auto-video-editor/blob/94c0a6e43bb1df65d19d0fb4b802897467be55a0/nodes/_draft_io.py#L217-L264) 说明：只有 `safe_write_draft()` 返回后，才计算 after hash 并追加审计记录。正常返回 `ok=False` 会写 `status="failed"`；但如果 `safe_write_draft()` 或其前面的流程异常退出，审计记录可能不会产生。

因此，“没有 `node_11_compile_*` 审计记录”只能支持以下表述：

> 未观察到该次编译调用完成 `write_draft()` 后的正常审计追加。结合其他日志和快照，可以提高对“失败发生在发布之前”的判断置信度，但不能单独排除写入函数内部异常或审计通道失败。

### 3.4 R-04 / R-07：state 更新必须有明确契约

当前问题不只是 `_recompile_drafts()` 忽略编译回执，还包括 repair action 的 state 更新丢失：

- `_recompile_drafts()` 返回 `None`；
- `_transitions()`、`_text_animation()` 返回 `{}`；
- `delivery_qa_repair()` 调用 `action(state)` 后不合并返回值；
- `inject_sticker()` 和 `inject_opening_hook()` 返回的新 `status_log` / `error_log` 也不能自动进入外层 state。

修复后应当统一成纯 state-update 契约：action 返回增量字典，repair 节点负责合并；文件写入仍由节点自身负责，但状态日志不依赖对输入字典的原地修改。

---

## 4. 修订后的目标设计

### 4.1 设计目标

1. 正常 SDK 编译不被 hook 误阻断。
2. 真正由用户在剪映中手动创建、且 SDK 不支持重建的内容仍然触发 fail-closed。
3. QA repair 的每次编译都能明确知道中文、英文各自的编译结果。
4. 编译完成后，工作流自产的 hook 标题、动画和开场特效能够恢复。
5. 编译失败、跳过、发布失败不被标记成“已修复”。
6. QA 二次复检仍然是最终成功判据；repair 尝试本身不等于交付成功。

### 4.2 hook 识别规则：采用窄范围白名单

不建议只依据 `segment.id.startswith("hook-title-seg-")` 放行。建议新增 helper，例如 `_is_our_hook_title_segment(track, segment, materials)`，至少同时校验：

- `track.id == "track-text-hook"`；
- `track.type == "text"`；
- `segment.id` 命中 `hook-title-seg-` 前缀；
- `segment.material_id` 命中 `hook-title-` 前缀，当前实际 ID 为 `hook-title-1`；
- 对应 `materials.texts` 中存在该 material；
- 该 hook material 的轨道/名称等身份字段与本节点产物契约一致。

只有完整命中上述身份组合的段落才视为我方 hook。以下情况仍然阻断：

- 普通文本轨上的未知文本；
- `track-text-hook` 轨上 ID 或 material 不匹配的段；
- 缺少实际文本 material 的悬空段；
- 用户创建的其他文本轨、贴纸轨、效果轨和高级素材。

这样既解决本次误判，又不把人工内容保护机制整体放宽。

### 4.3 repair 编译必须消费回执

建议把 `_recompile_drafts()` 改成返回结构化结果，而不是 `None`。返回内容至少包括：

```python
{
    "status_log": [...],
    "error_log": [...],
    "compile_results": {
        "zh": {"status": "compiled", "fingerprint": "..."},
        "en": {"status": "compiled", "fingerprint": "..."},
    },
    "compiled_languages": ["zh", "en"],
    "failed_languages": [],
}
```

实现时应遵守：

1. 对 `zh` / `en` 分别保存原始 record；不能只汇总成一个布尔值。
2. 检查 `status`，并在 `status == "compiled"` 时继续检查 `record.write.ok`。
3. `failed`、`failed_blocked_features`、`skipped_no_spec` 分别记录不同原因。
4. `skipped_fingerprint` 不能直接算修复成功，必须执行输出完整性检查；若 QA 当前仍报告该类别失败，应强制重建或明确返回 repair failure。
5. 不因某个语言分支成功就把另一分支也标记为成功。
6. 返回的 `status_log` / `error_log` 必须在 `delivery_qa_repair()` 中合并到 LangGraph state。

### 4.4 QA repair 中需要显式支持强制重建

当前 pipeline 的指纹短路只判断“spec 输入是否变化”，不是“当前草稿是否仍完整”。例如：spec 指纹没有变化，但草稿中的转场素材被删除，QA 仍会失败；此时 repair 再次调用 pipeline 可能返回 `skipped_fingerprint`，不会重新写出转场。

建议增加显式参数，例如 `force_rebuild: bool = False`：

- 普通 node_11 首次编译保持默认 `False`，继续使用指纹跳过优化；
- QA repair 对已判定缺失的 SDK 构建块启用 `force_rebuild=True`，跳过指纹短路并把 `force=True` 传给 `compile_spec()`；
- 强制重建依旧必须经过 `blocked_features` 检查和安全写入，不能绕过人工内容保护；
- 强制重建结束后检查 `write.ok` 和草稿实际结构，再进入 hook replay。

注意：`force_rebuild` 只绕过“指纹相同”的优化，不绕过 `blocked_features` fail-closed。

### 4.5 编译后恢复 hook

对成功重建的草稿恢复 hook：

- 使用现有 `inject_opening_hook()` 复用确定性 ID、先清理旧产物再写入的幂等机制；
- 只对确实完成重建的分支执行恢复，或者提供按 `draft_file` 调用的底层 helper，避免每处理一个分支就无条件重写两份草稿；
- hook replay 的日志更新必须被返回并合并；
- 若 `OPENING_HOOK_ENABLED=false`，保持 noop，不应为关闭的 hook 生成产物；
- replay 之后再次检查引用完整性、标题轨、标题 material、动画引用以及视频特效引用。

### 4.6 repair 状态语义

当前 `delivery_qa_repaired:<keys>` 容易让监控端把“执行过修复动作”误解为“修复成功”。建议拆分为：

- `delivery_qa_repair_attempted:<keys>`：已尝试；
- `delivery_qa_repair_compiled:<languages>`：SDK 已编译并发布；
- `delivery_qa_repair_failed:<languages>`：至少一个分支编译/发布失败；
- `delivery_qa_repair_replayed_hook:<languages>`：对应分支 hook 已恢复；
- 最终通过与否仍由 `join_before_delivery` 的复检报告决定。

`delivery.qa_repair` 事件也应传递 attempted / compiled / failed / replayed 等分项，不能只用 `retried` 暗示成功。

---

## 5. 分阶段执行计划

### 阶段 0：补齐事故证据（先做，不改代码）

| 项目 | 操作 | 验收结果 |
|---|---|---|
| 0.1 checkpoint state | 使用项目当前 PostgreSQL checkpointer 和 thread_id 调用 `aget_state()` | 导出 `status_log`、`error_log`、各节点结果及最后 checkpoint 时间 |
| 0.2 运行事件 | 查询 `/api/workflow/runs/console-3e866b87d5d6` 与 SSE 历史 | 确认 node_11、repair、QA、failed 事件顺序；不把该接口当 checkpoint state |
| 0.3 编译 sidecar | 保存当前 zh/en `.autovideo_draft_build_spec.json`，尤其 `compile.last_result` 与 `timeline.blocked_features` | 保留当前可见回执，记录其可能已经被后续尝试覆盖 |
| 0.4 草稿备份 | 复制两份草稿目录、`draft_content.json`、spec、审计 JSONL 和 `.snapshots/` | 留下只读现场副本，后续实验不覆盖原始证据 |
| 0.5 首编异常分类 | 对照 checkpoint `status_log` / `error_log` 与 node_11 节点事件 | 把异常归类到 spec 发射、draft load、timeline extract、fingerprint、compile、publish 或 sidecar 回写阶段 |

**阶段 0 退出条件**：首编失败阶段和异常信息有明确证据；无法取得的项目明确标记为“未取证”，不通过缺少文件推断成确定事实。

### 阶段 1：修复 hook 的所有权识别

**改动文件**：

- `jianying_sdk/timeline_extractor.py`
- `tests/unit/test_jianying_sdk_phase2.py`
- 可选：把 hook 身份常量集中到一个小型共享模块，避免 extractor 与 node_09b 各自维护不同字符串。

**任务**：

1. 增加 hook 所有权识别 helper。
2. 使用轨道 ID、segment ID、material ID 和实际 material 引用的组合校验。
3. 对确认属于我方 hook 的文本段不加入 `manual_text_track`。
4. 对其他未知文本段保持原有 fail-closed 行为。
5. 不扩大对 sticker/effect/filter 轨及高级素材桶的放行范围。

**单测**：

- 标准 `track-text-hook` + `hook-title-seg-*` + `hook-title-*`：不阻断；
- 普通未知文本轨：仍阻断；
- hook 轨但 material ID 不匹配：仍阻断；
- hook segment 引用不存在 material：仍阻断；
- hook 段与普通人工段同时存在：只豁免 hook，人工段仍进入 blocked。

**退出条件**：所有新增用例通过，原有人工文本保护用例不回退。

### 阶段 2：修复编译回执与 state 传播

**改动文件**：

- `nodes/node_delivery_qa_repair.py`
- `jianying_sdk/pipeline.py`
- `tests/unit/test_node_delivery_qa_repair.py`
- `tests/unit/test_jianying_sdk_phase7.py`

**任务**：

1. `_recompile_drafts()` 返回分支级编译结果和 state 增量。
2. 检查 `status` 与 `write.ok`，保留完整失败 note。
3. 对 `failed_blocked_features`、`failed`、`skipped_no_spec`、`skipped_fingerprint` 分别处理。
4. 新增 `force_rebuild` 参数，供 QA repair 在 QA 明确检测到构建特征缺失时使用。
5. `delivery_qa_repair()` 合并 action 返回的 state update；禁止只调用后丢弃返回值。
6. repair event 和 status tag 区分 attempted、compiled、failed；“已尝试”不等于“已修复”。
7. 对每个语言分支独立记录结果，出现部分成功时不能整体标记成功。

**单测**：

- `compiled + write.ok=true`：记录成功；
- `compiled + write.ok=false`：按失败处理；
- `failed_blocked_features`：记录 blocked feature，原草稿不变；
- `failed`：保留异常 note；
- `skipped_no_spec`：不算修复成功；
- `skipped_fingerprint` 且 QA 仍失败：触发强制重建或返回明确失败；
- zh 成功 / en 失败：状态按分支准确记录；
- action 返回 `error_log` / `status_log`：最终 repair node state 中可见。

**退出条件**：repair 不再出现“编译返回失败，但节点仍记录已修复”的情况；所有结果进入 checkpoint state。

### 阶段 3：编译后恢复开场 hook

**改动文件**：

- `nodes/node_delivery_qa_repair.py`
- `nodes/node_09b_inject_opening_hook.py`
- `tests/unit/test_node_09b_inject_opening_hook.py`
- 新增或扩展 repair + SDK pipeline 集成测试。

**任务**：

1. 仅在分支编译及发布成功后恢复 hook。
2. 复用现有 hook 幂等清理逻辑，不复制另一套标题构建逻辑。
3. 避免按 zh/en 循环多次调用会同时处理双草稿的高层 state 函数；必要时抽取单草稿 helper。
4. 把 hook replay 的日志合并到 repair state。
5. replay 后检查 hook-title 文本 material、text track、animation ref、video effect ref 均可解析。
6. 若 `OPENING_HOOK_ENABLED=false`，测试确认不会重新写入 hook。

**集成测试**：

```text
带 hook 的 zh/en 原生草稿
    ↓
extract timeline（只豁免受信任 hook）
    ↓
QA repair 强制 SDK 编译
    ↓
SDK 重建（hook 暂时消失是预期行为）
    ↓
按分支 replay hook
    ↓
检查引用完整性和最终草稿内容
    ↓
重新执行 QA 检查
```

**退出条件**：成功重编译后 hook 标题、标题动画和开场视频特效重新存在；未知人工内容仍会阻断编译。

### 阶段 4：验证交付 QA 闭环

**改动文件**：

- `tests/unit/test_node_delivery_qa_repair.py`
- `tests/unit/test_jianying_sdk_phase7.py`
- `tests/unit/test_node_09b_inject_opening_hook.py`
- 如有必要新增 `tests/integration/` 下的 QA repair 集成测试。

**场景矩阵**：

| 场景 | 预期行为 |
|---|---|
| zh/en 均可编译，hook 存在 | 双分支 SDK 重建后 hook 均恢复，QA 再检通过 |
| zh 被 hook 以外的人工文本阻断 | 编译 fail-closed，草稿保留，不得静默删除人工文本 |
| zh 编译成功、en 编译失败 | zh/en 状态独立显示；只恢复成功分支，整体 QA 不得误报通过 |
| 编译结果为 `skipped_fingerprint`，QA 仍报告转场缺失 | 强制重建或明确 repair failed，不允许仅凭指纹跳过 |
| hook 开关关闭 | repair 不得重新注入 hook |
| repair 失败后再次复检 | `delivery_qa_failed` 仍按设计 raise，运行状态为 failed |
| repair 成功后再次复检 | required features 全部通过才允许进入 export |

### 阶段 5：全量测试与 Windows 剪映真机验收

**自动化测试**：

1. 执行第 10 期 SDK 相关单测：phase2–phase7。
2. 执行 `test_node_delivery_qa_repair.py` 和 `test_node_09b_inject_opening_hook.py`。
3. 执行全量 unit tests，与修复前基线对比，要求无新增失败。
4. 若项目现有测试命令有统一入口，以仓库当前 `pyproject.toml` / 测试脚本为准，不在本计划里臆造命令。

**真机验收**：

1. 使用隔离测试素材和新的测试草稿目录；先备份草稿和 sidecar。
2. 在 Windows + 剪映环境运行完整 zh/en 流程。
3. 制造或复现一次 QA repair 场景，验证转场或字幕动画进入 repair 分支。
4. 确认 SDK 重建后开场 hook 恢复；剪映关闭再打开测试草稿，确认标题、动画、开场特效和普通字幕/转场都存在。
5. 在剪映 GUI 中检查时间线实际效果，而不仅检查 JSON 中存在 material ID。
6. 保存 QA 报告、草稿 SHA-256、编译回执、审计 JSONL 和剪映复开截图，作为本次修复验收附件。

**最终退出条件**：

- unit tests 无新增失败；
- zh/en 的 compile record 与写入审计一致；
- QA repair 的 state 和事件记录能区分 attempted / compiled / failed；
- hook 恢复后草稿可由剪映正常打开；
- required QA 全部通过才允许导出；
- 未知人工编辑仍触发 fail-closed。

---

## 6. 修改文件建议汇总

| 文件 | 建议修改 | 优先级 |
|---|---|---|
| `jianying_sdk/timeline_extractor.py` | 窄范围识别工作流 hook，保留人工内容阻断 | P1 |
| `jianying_sdk/pipeline.py` | 增加 QA repair 可选强制重建能力；保留 blocked 检查和安全写入 | P1 |
| `nodes/node_delivery_qa_repair.py` | 检查编译回执、合并 action state update、恢复 hook、区分尝试与成功 | P1 |
| `nodes/node_09b_inject_opening_hook.py` | 如需要，抽取单草稿 hook replay helper | P1/P2 |
| `tests/unit/test_jianying_sdk_phase2.py` | hook 身份识别和未知人工文本保护测试 | P1 |
| `tests/unit/test_jianying_sdk_phase7.py` | pipeline 强制重建、回执状态测试 | P1 |
| `tests/unit/test_node_delivery_qa_repair.py` | repair 编译失败、跳过、双语部分失败和 state 合并测试 | P1 |
| `tests/unit/test_node_09b_inject_opening_hook.py` | hook replay 后幂等、双语和开关测试 | P1 |
| `docs/第10期开发/` | 追加首编真实异常和真机验收结果 | P2 |

**Schema 版本建议**：如果只增加 extractor 对现有 hook 产物的识别规则、增加函数参数和日志，不改变 `DraftBuildSpec` 的持久化字段结构，则不必仅因本次修复 bump `DraftBuildSpec.schema_version`。但如果新增了 spec 持久化字段、hook 构建段或编译输入契约，则需要重新评估 schema version 和指纹版本。

---

## 7. 风险和回滚

1. **禁止在唯一的用户原始草稿上首次验证**：SDK 是全量重建，先在复制的草稿目录验证。
2. **未知人工内容不得白名单放行**：测试中必须保留普通人工文本、未知引用、贴纸轨和高级素材桶阻断用例。
3. **强制重建仍须经过安全发布**：`force_rebuild` 不能绕过快照、原子写入、完整性校验和 blocked feature 检查。
4. **hook replay 失败不能伪装成功**：需要进入 `error_log` 和 repair result；QA 若把 hook 设为 required，还必须由 QA category 验证实际产物。
5. **回滚方式**：回滚代码后，使用阶段 0 备份恢复测试草稿目录；不要用旧 spec 覆盖新草稿或反向覆盖用户原始草稿。

---

## 8. 原文档保留与修订建议

建议保留原文档的以下内容：

- fail-closed 是保护性设计，不应为通过 QA 而删除；
- hook 在首次编译之后写入、在 QA repair 之前已经存在，构成两种不同编译输入；
- 编译回执必须被 repair 消费；
- hook 豁免必须与编译后恢复配套；
- 中英文草稿独立编译、独立检查；
- 首次编译异常需要先取证，不能直接猜测修法。

建议修订或删除以下内容：

- 删除“curl 运行快照 API 可以直接取得 `state.status_log/error_log`”的描述；
- 把“没有审计记录证明未进入发布”改成有限置信度推断；
- 把“pipeline catch 了提取/指纹异常”改成阶段化异常边界；
- 把“英文成功证明管线本身没有问题”收窄为英文分支单次成功对照；
- 补充 `skipped_fingerprint` / 强制重建 / state 合并 / 双语部分成功处理；
- 补充自动化集成测试和剪映复开验收要求。

---

## 9. 最终审查意见

原文档对 `manual_text_track` 阻断机制和“编译后 hook 丢失”风险的识别有代码依据，修复方向总体正确；但它还不是可直接交给执行代理逐项完成的闭环计划。

**建议执行顺序：先完成阶段 0 真实取证，再按阶段 1→2→3→4→5 实施。** 尤其不能跳过 state 更新合并和 `skipped_fingerprint` 处理，否则即使增加 hook 白名单，repair 仍可能出现“动作执行了、回执没检查、状态没记录、QA 仍失败”的问题。

本报告只完成仓库代码静态审查和计划修订；没有修改 GitHub `main`、没有提交代码，也没有声称已在 Windows/剪映环境复现或修复该事故。

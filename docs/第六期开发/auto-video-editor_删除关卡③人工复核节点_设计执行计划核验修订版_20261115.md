# auto-video-editor 删除关卡③“布局人工复核”节点：设计与执行计划核验修订版

> 核验日期：2026-11-15  
> 仓库：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 核验时 HEAD：`3a2672bb49ebc5e37ee55299737e0e1ea3eb1730`  
> 原计划：[`auto-video-editor_删除关卡③布局人工复核节点_执行设计_20261115.md`](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E4%BF%AE%E6%AD%A3/auto-video-editor_%E5%88%A0%E9%99%A4%E5%85%B3%E5%8D%A1%E2%91%A2%E5%B8%83%E5%B1%80%E4%BA%BA%E5%B7%A5%E复核节点_执行设计_20261115.md)  
> 原计划状态：计划稿、未执行  
> 本文性质：源码静态核验、原计划审查和修订执行设计；**本次没有修改 GitHub 仓库，也没有运行 pytest 或真实剪映工作流**。

---

## 1. 执行摘要

### 1.1 总体结论

原计划的核心目标在当前 `main` 分支上成立，技术路线可行：

1. 删除 `node_checkpoint3_layout_review` 节点及其 `interrupt()` 人工挂起行为。
2. 将 `node_10b_inject_text_fx_en` 到 `node_17_inject_english_tts` 的条件边替换成普通直连边。
3. 删除仅服务于关卡③的布局检测编排、状态字段、payload 和测试。
4. 将工作流 schema 版本由 8 提升到 9，使节点图布局缓存失效。
5. 保留中文线、英文线的汇合屏障，以及汇合后的尾链和关卡②。

但**原计划不宜原样直接执行**。核验发现一个明确的前端功能性风险、一个测试基线不一致问题，以及若干未清理的源码引用、CLI 说明、测试断言和测试脚本标识。应先按本文修订，再开始实施。

### 1.2 结论分级

| 审查项 | 结论 | 说明 |
|---|---|---|
| 删除关卡③节点 | 可行 | 当前图中该节点是英文线中的独立条件分支节点 |
| `10b → 17` 普通边直连 | 可行 | 不改变英文线的业务先后顺序，也不改变 15 与 17 的汇合关系 |
| 删除 16a QC 链 | 有条件可行 | 会彻底取消英文字幕布局自动检测，不只是取消人工复核；这是明确的功能范围变化 |
| 删除 4 个 State 字段 | 可行 | 前提是全仓引用扫描确认无剩余运行时消费者，并更新兼容性测试 |
| 前端和恢复入口调整 | 原计划不完整 | 必须修复 `draftKind` 变量、更新 CLI / API / 台账说明 |
| 测试计划 | 需要修订 | `6679e54` 不是原计划声明的 `4f48bd3` 基线 |
| 生产存量任务处理 | 需要补充 | 关卡③挂起中的旧任务无法按新图正常恢复，需要上线前盘点和处置 |

**推荐决策：保留“彻底清除”这一已确定的目标范围，但将本文第 5～9 章的补充要求纳入原计划后，再执行。**

### 1.3 本次没有做的事情

- 没有删除节点文件或修改 `graph.py`。
- 没有修改 `state.py`、前端、测试或 README。
- 没有在本地检出仓库，因此没有执行全仓 `rg`。
- 没有连接项目 PostgreSQL，也没有执行 pytest。
- 没有在剪映中验证草稿和人工操作流程。

因此，本文中的“正确”是指源码结构和设计逻辑经静态核验后成立；不等同于改造已经通过运行测试。

---

## 2. 仓库基线与原计划基线核对

### 2.1 当前 `main` 分支

| 项目 | 核验值 |
|---|---|
| 仓库 | `wayyet/auto-video-editor` |
| 分支 | `main` |
| HEAD | `3a2672bb49ebc5e37ee55299737e0e1ea3eb1730` |
| HEAD 提交时间 | `2026-11-14T17:12:55Z` |
| 当前是否包含关卡③ | 是 |
| 当前是否包含 16a 布局 QC 链 | 是 |
| 指定执行设计文档 | 已提交，文档仍标记为“计划稿，未执行” |

### 2.2 原计划使用的源码基线仍然有效

原计划声明基线为 `4f48bd3`。将 `4f48bd3` 与当前 `main` 比较后，结果为：

- `main` 相对 `4f48bd3` 前进 3 个提交，没有落后提交。
- 这 3 个提交改动了 `agent_gate/cli_client.py`、其测试，以及新增的两份文档。
- 本次改造涉及的 `graph.py`、`nodes/node_16a_translate_and_check.py`、`nodes/node_16_translate_subtitles.py`、`state.py`、`monitoring/workflow_graph.py`、`web/human_gate_payload.py` 等源码文件，不在这 3 个提交的改动清单内。

因此，原计划以 `4f48bd3` 为源码基线的主体分析没有被这 3 个后续提交推翻。执行时仍应按符号搜索定位，不要依赖绝对行号。

### 2.3 必须修正的测试基线错误

原计划 §5.3 使用：

```text
.worktrees/baseline-6679e54
```

但原计划声明的源码基线是 `4f48bd3`。核对 Git 提交历史可知：

- `6679e54` 是更早的提交，提交信息对应当时的“全量回归 1922 零失败”等历史阶段。
- `6679e54` 到 `4f48bd3` 之间相差 **31 个提交**。
- 这 31 个提交修改过 `graph.py`、`state.py`、`node_16a_translate_and_check.py`、工作流 schema、前端以及多份本次相关测试。

因此，不能将 `.worktrees/baseline-6679e54` 的结果直接用作 `4f48bd3` 的存量失败基线。否则测试差异可能来自这 31 个历史提交，而不是本次删除关卡③的改造。

**修正要求：**基线对照 worktree 必须检出 `4f48bd3`，并在执行日志中记录 `git rev-parse HEAD` 的完整 SHA。

建议命令：

```bash
git worktree add .worktrees/baseline-4f48bd3 4f48bd3
```

在基线 worktree 和改造 worktree 中使用相同 Python 环境、相同依赖锁文件、相同环境变量和相同 PostgreSQL 状态执行目标测试。不要把不同环境的结果直接比较。

---

## 3. 当前源码结构与原计划正确性核验

### 3.1 当前 LangGraph 拓扑

当前 `graph.py` 仍然存在以下注册和接线：

```python
from nodes.node_checkpoint3_layout_review import node_checkpoint3_layout_review

# 节点注册
g.add_node("node_checkpoint3_layout_review", node_checkpoint3_layout_review)

# 英文线接线
g.add_edge("node_08b_add_subtitles_en", "node_10b_inject_text_fx_en")
g.add_conditional_edges(
    "node_10b_inject_text_fx_en",
    route_after_translate,
    {
        "node_checkpoint3_layout_review": "node_checkpoint3_layout_review",
        "node_17_inject_english_tts": "node_17_inject_english_tts",
    },
)
g.add_edge("node_checkpoint3_layout_review", "node_17_inject_english_tts")
```

`route_after_translate()` 当前以 `layout_issues_detected` 为条件：

- 为真：进入 `node_checkpoint3_layout_review`。
- 为假：直接进入 `node_17_inject_english_tts`。

所以原计划提出删除条件路由，并把 10b 到 17 改为普通直连边，与当前代码一致。

### 3.2 删除后的目标拓扑

```text
英文线
  |
  v
node_16a_translate_and_check
  |
  v
node_08b_add_subtitles_en
  |
  v
node_10b_inject_text_fx_en
  |
  | 普通直连边
  v
node_17_inject_english_tts
  |
  +--------------------------+
                             |
中文线                       |
  |                          |
node_14_make_covers           |
  |                          |
node_15_localize_covers_en    |
  |                          |
  +------------+-------------+
               |
               v
       node_09_inject_fx
               |
       node_09b_inject_opening_hook
               |
       node_11_inject_sticker
               |
       node_12_human_add_bgm  interrupt("②")
               |
       node_13_adjust_volume
               |
       join_before_delivery
               |
       交付 QA / 可选导出
```

目标边：

```python
g.add_edge("node_10b_inject_text_fx_en", "node_17_inject_english_tts")
```

### 3.3 必须保持不变的依赖

本次只删除英文线中 10b 与 17 之间的关卡③分支。以下内容不得顺带改变：

1. `node_08_add_subtitles → fork_draft_for_english_branch → node_16a_translate_and_check`。
2. `node_16a_translate_and_check → node_08b_add_subtitles_en → node_10b_inject_text_fx_en`。
3. 中文线 `node_10_inject_text_fx → node_14_make_covers → node_15_localize_covers_en`。
4. `node_15_localize_covers_en` 和 `node_17_inject_english_tts` 必须继续通过列表型 fan-in 汇入 `node_09_inject_fx`。
5. 汇合后的 09、09b、11、12、13 和交付 QA 的顺序保持不变。
6. 关卡①和关卡②仍使用 `interrupt()`；PostgreSQL checkpointer 仍负责生产环境的挂起状态持久化。

尤其不能将 `node_17_inject_english_tts` 直接连接到尾链首节点而绕过 `node_15_localize_covers_en` 的 fan-in 等待语义。否则英文支路可能先进入尾链，造成中文封面本地化尚未完成就开始后续处理。

### 3.4 16a 当前确实包含 QC 编排

当前 `nodes/node_16a_translate_and_check.py` 包含以下过程：

1. 读取 `subtitle_segments_zh`，没有时兼容读取旧的 `asr_segments_zh`。
2. 根据 marker 判断是否跳过翻译 API 调用。
3. 将翻译结果写入英文分支草稿和 marker。
4. 根据源视频路径逐个执行 `_check_layout_via_subtitle_qc()`。
5. QC 链不可用时降级为 `validate_layout()`。
6. 将英文字幕写为 SRT。
7. 返回 `layout_issues`、`layout_issues_detected`、预览视频和证据帧等状态增量。

这确认了原计划对 QC 链位置的判断。

但要特别说明：**QC 链并非字幕生成的必要步骤。** 当前 SRT 写入由 `write_srt_from_segments()` 执行。只要保留翻译、marker、草稿读写和 SRT 写入逻辑，删除布局检测代码并不会从代码依赖关系上要求删除英文字幕生成。

### 3.5 删除 QC 链的功能影响

原计划的“彻底清除”不只是删除人工关卡，而是删除自动布局质量检查能力。改造后：

- 英文字幕仍然翻译并写入英文分支草稿。
- 英文 SRT 仍然生成。
- 英文花字仍然注入。
- 英文 TTS 仍然执行。
- 但英文字幕超宽、重叠、布局错误等问题不再由 16a 自动检测。
- 不再生成供人工复核使用的 QC 预览视频和证据帧。
- 不再有布局问题触发人工停机的机制。

这是明确的产品行为变化，应在 README、发布说明和验收标准中写清楚。不能把它描述成纯粹的“隐藏一个 UI 节点”。

如果未来仍要求自动化质量保障，应另行设计不阻断主流程的自动检测或交付 QA 规则；这不属于本次“彻底清除”方案，不应偷偷在本次混入新的 QC 行为。

### 3.6 前端节点图与静态子图

当前主工作流节点图由 `GET /api/workflow/graph` 和 `monitoring.workflow_graph.build_graph_schema()` 根据编译后的图构建，节点显示信息来自 `_NODE_META`。没有发现 `web/node_map/workflow.json` 包含 `node_checkpoint3_layout_review` 或布局 QC 字段。

当前 `tests/integration/test_unreachable_nodes_wiring.py` 的已读取内容也没有出现 `checkpoint3`、`node_10b` 或 `layout_issues` 命中。因此原计划认为该测试的 `GOLDEN_STEPS` / `NEIGHBORS` 不需要因本次关卡③删除而变更，静态核验结果支持这一点。

但前端并非完全没有关卡③相关代码：它没有硬编码节点 ID，不代表没有硬编码关卡编号、草稿类型和 QC 展示字段。见第 5 章。

---

## 4. 原计划中正确、建议保留的设计

### 4.1 保留节点 ID `node_16a_translate_and_check`

建议继续保留内部节点 ID，不在本次将其重命名为 `node_16_translate_subtitles` 或其他名称。

原因：

- 避免扩大图拓扑、日志、历史状态和测试的变更范围。
- 保留英文字幕翻译的稳定节点身份。
- 只将展示名称改为“翻译英文字幕”或“翻译英文字幕并写入 SRT”，不再使用“翻译与布局检测”。

### 4.2 保留英文字幕写入和 marker 幂等机制

必须保留：

- `_marker_exists`
- `_read_segments_from_draft`
- `_save_marker`
- `_write_marker`
- `write_srt_from_segments`
- `translate_segments`

marker 用于避免 LangGraph 重放时重复调用翻译 API。删除 QC 不应影响这项幂等保障。

### 4.3 保留字幕工具能力层

原计划明确不删除 `video_edit_capabilities` 中的 `subtitle_build`、`subtitle_render`、`subtitle_qc` 能力层，这是正确的边界处理。

本次删除的是 16a 对这些能力的编排，不是全仓删除工具能力。其他测试、字幕链或后续独立工作流仍可能调用这些能力。

### 4.4 保留 `web/node_map/workflow.json`

当前该文件属于 storyline 子流程静态快照，并非完整主工作流的实时节点图。当前文件没有关卡③节点引用，因此不应为了本次主图调整而重生成或修改它。

### 4.5 schema 版本由 8 提升至 9

当前 `monitoring/workflow_graph.py` 中 `SCHEMA_VERSION = 8`，节点图布局缓存 key 会包含 schemaVersion。删除节点和边属于拓扑变化，因此提升到 9 是正确的。

应同时更新版本历史注释，并在 schema 测试中增加新版本及新边的断言。

---

## 5. 原计划必须修正的问题及解决方案

### 5.1 P1：`console.html` 删除 `draftKind` 声明会破坏打开草稿目录功能

**原计划位置：** C4，删除 `web/console.html` 中 `g.checkpoint === "③" ? "en" : "zh"` 的表达式。

**当前代码依赖：**

```javascript
const draftKind = g.checkpoint === "③" ? "en" : "zh";
// 后续 HTML 模板仍使用 draftKind
`<button ... data-draft="${draftKind}" data-open-dir ...>`
```

如果只删除变量声明而不替换后续使用，点击关卡①或②的“打开草稿目录”按钮时会因为 `draftKind` 未定义而抛出 `ReferenceError`。

**修正方案（二选一，推荐第一种）：**

```javascript
const draftKind = "zh";
```

或者把模板中的 `data-draft="${draftKind}"` 改成固定的 `data-draft="zh"`，并删除变量声明。因为剩余的关卡①/②都操作中文主草稿。

**验收要求：**分别在关卡①和关卡②的恢复卡片上点击“打开草稿目录”，确认打开的是中文主草稿目录，且浏览器控制台没有 `ReferenceError`。

### 5.2 P1：测试存量对照使用了错误的基线提交

**原计划位置：** §5.3。

原计划声明 `4f48bd3`，却使用 `.worktrees/baseline-6679e54` 进行存量失败对照。两个提交之间相差 31 个提交，且涉及本次工作流、状态、前端和测试代码。

**修正方案：**

```bash
git worktree add .worktrees/baseline-4f48bd3 4f48bd3
```

在执行记录中至少记录：

```bash
git rev-parse HEAD
python --version
python -m pytest --version
```

然后在基线 worktree 和改造工作区分别执行同一测试集合。测试失败必须按照“同一提交、同一依赖、同一配置”的结果分类：

- 基线失败、改造后仍以相同原因失败：存量失败。
- 基线通过、改造后失败：新增回归。
- 基线和改造后失败原因不同：单独调查，不能简单归为存量。

原计划中“集成测试基线存量失败 3 例”“单测基线约 2046 例”等数字，在本次静态核验中没有实际运行复现，执行时必须重新采集，不应直接当作当前已证实的测试统计。

### 5.3 P1：必须明确处理生产中仍挂起在③的存量任务

原计划已指出：旧任务如果挂起在③，新图删除该节点后不能按原路径恢复。这个判断方向正确，但操作步骤还不够完整。

**上线前增加存量盘点：**查询运行台账中仍处于 suspended 且当前关卡为③的任务：

```sql
SELECT thread_id, status, current_checkpoint, updated_at
FROM workflow_runs
WHERE status = 'suspended'
  AND current_checkpoint = '③'
ORDER BY updated_at;
```

**处置纪律：**

1. 对查询出的任务逐个确认是否还需要保留。
2. 需要保留业务产物的，先备份草稿和任务日志。
3. 使用项目已有的任务取消或删除入口将这些任务作废，不要直接删除 PostgreSQL checkpointer 内部表的行。
4. 不要尝试用新图强行恢复旧③ interrupt。
5. 新建任务验证新拓扑。

对于旧任务挂起在①或②的情况，不能直接推断一定兼容。应增加至少一个基于真实 PostgreSQL checkpointer 的旧任务恢复验证：

- ①挂起后部署新图，再恢复并继续到②。
- ②挂起后部署新图，再恢复并走完尾链。

只有这两种路径都验证后，才能确认保留①/②的存量任务恢复兼容性。

### 5.4 P1：`node_16_translate_subtitles.py` 删除函数时必须同步删除其私有依赖

原计划 B2 提出删除 `validate_layout()` 和 `_build_interrupt_payload()`，但没有完整列出它们专属的辅助代码和 import。

当前文件中：

- `DEFAULT_FONT_CANDIDATES` 为布局校验提供字体候选。
- `_try_load_font()` 为 `validate_layout()` 加载字体。
- `validate_layout()` 使用 PIL 的 `Image`、`ImageDraw`、`ImageFont`。
- `_build_interrupt_payload()` 使用 `WorkflowState` 类型。

这些依赖在删除两个函数后应重新检查。建议一并删除只服务于上述函数的内容：

- `DEFAULT_FONT_CANDIDATES`
- `_try_load_font()`
- `validate_layout()`
- `_build_interrupt_payload()`
- `Image`、`ImageDraw`、`ImageFont` 导入
- 如果 `WorkflowState` 不再被其他保留代码使用，则从 import 中移除

必须保留 `SubtitleSegment`、SRT 序列化、marker、草稿读取和写入所需要的依赖。

不能整文件删除 `nodes/node_16_translate_subtitles.py`，因为它仍然承载英文字幕草稿读写和 SRT 辅助函数。

### 5.5 P2：16a 删除 QC 链时需要清理完整 import 和辅助代码

原计划 B1 已列出删除：

- `_check_layout_via_subtitle_qc()`
- `_segments_en_to_transcript_records()`
- `_QC_CHAIN_WORK_ROOT`
- `subtitle_build` / `subtitle_render` / `subtitle_qc`
- `RunContext`
- `resolve_video_input_paths`
- `validate_layout`

还应检查以下 import 是否只被被删 QC 代码使用：

- `json`：当前 QC 链会生成 transcript JSON；移除该链后应确认 16a 其余代码是否还需要。
- `shutil`：当前导入未在本次已核对的 16a 主逻辑中发现必要用途，执行时应通过静态检查确认并移除未使用导入。

建议改造后 16a 保持清晰的单一职责：

```text
检查是否存在源字幕
        |
检查英文分支草稿目录
        |
marker 存在？—— 是：从草稿读取英文字幕
        |
        否
        |
调用翻译客户端
        |
写入英文草稿和 marker
        |
读取/确认英文字幕段
        |
写入 SRT
        |
返回 subtitle_segments_en、subtitle_srt_path、status_log
```

目标返回值可保持原计划形式：

```python
out: dict[str, Any] = {
    "subtitle_segments_en": list(segments_en),
    "subtitle_srt_path": srt_path,
    "status_log": ["node_16a_translate_done"],
}
```

无源字幕 skip、英文草稿缺失的错误路径、翻译 marker 幂等逻辑均应保留。

### 5.6 P2：UI 对证据帧和 issues 的展示残留没有完整列入改动清单

当前 `web/human_gate_payload.py` 除 `preview_video_path` 和 `evidence_frames` 外，还读取 payload 中的 `issues` 并返回给前端。

当前 `web/console.html` 还包含：

- `issuesHtml` 的 issues 格式化代码。
- `g.issues` 布局异常列表展示。
- `g.preview_video_path` 预览视频展示。
- `g.evidence_frames` 证据帧列表展示。

原计划 C3 只明确删除 preview/evidence 两个状态字段；C4 只明确删除 preview 视频行，没有明确删除证据帧和 issues 展示。

**修正方案：**

如果本次坚持“关卡③专属的布局复核 UI 数据彻底清除”，则应同步：

1. 删除 `build_gate_view()` 对 `preview_video_path`、`subtitle_qc_evidence_frames` 的读取与输出。
2. 明确是否删除 `issues` 字段。当前它只服务于③布局问题展示；如果没有其他 gate 使用，应一起删除。
3. 若删除 `issues`，同时删除 `console.html` 的 `issuesHtml` 和 issues 列表拼接逻辑。
4. 删除 `console.html` 的 preview 视频展示和 evidence_frames 展示片段。
5. `GATE_META` 删除③，保留 `_FALLBACK_META`，避免未知/历史 payload 导致恢复面板直接报错。

如果希望保留通用人工关卡未来展示 issues 的能力，应把它定义成通用可选字段，并明确其不再由本次 16a QC 链生成；不能一边宣称彻底清除，一边留下无来源的专用展示链。

### 5.7 P2：恢复 CLI 和通用恢复模块的关卡编号说明不完整

原计划列出的 `web/console_service.py`、`web/api_console.py` 是必要更新项，但还漏了：

#### `scripts/resume_workflow.py`

当前帮助说明和 docstring 多处写有 `①/②/③`，包括：

- checkpoint 标识说明。
- `--checkpoint` CLI help。
- payload checkpoint 的匹配说明。

修改后应将“当前有效关卡”说明改为①/②。

#### `resume_utils.py`

该模块按 payload 的 `checkpoint` 值做通用匹配，算法本身不依赖固定的关卡数量，不建议删除它。

但当前说明仍提到：

- ②/③ 可以同时挂起。
- `values_by_checkpoint` 的示例包含③。

修正为当前工作流只存在①/②两个人工关卡；保留通用映射算法，不要把它硬编码成只接受两个字符串。

#### `monitoring/runs_table.py`

需要同时更新：

- `current_checkpoint` 允许值的说明：`①/②/③/NULL` 改成当前有效值 `①/②/NULL`。
- 调用约定中的节点清单：移除 `node_checkpoint3_layout_review`。

历史数据库中可能仍有 `current_checkpoint='③'` 的旧记录。不要为了更新注释而删除整个台账表或历史任务数据。

#### `web/api_console.py`

除了恢复请求模型中 `①/②/③` 的说明，还要更新“英文分支草稿尚未创建（关卡③之前）”这类错误提示。关卡③删除后，这个时间描述不再准确。建议使用“英文分支草稿尚未创建（英文分支尚未执行到草稿生成步骤）”等基于实际状态的描述。

#### `web/console_service.py`

更新 `resume_run()` 的参数文档，将当前有效关卡说明改为①/②；但保留通过 interrupt ID 匹配的能力。

### 5.8 P2：E2E 脚本的旧名称不止在函数名和注册表中

原计划 E1 将 `case_checkpoint3_and_tts` 改名为 `case_english_tts`，方向正确，但当前脚本内部还使用：

```python
_out_dir("checkpoint3_tts")
_case("checkpoint3_and_tts", ...)
```

这些字符串也应同步修改，否则脚本虽然能运行，却会继续生成带有已删除关卡③标识的目录和报告。

建议：

- 函数名：`case_english_tts`
- 输出目录：`english_tts`
- 报告 case 名：`english_tts`
- `CASES` 注册键：`english_tts`
- 移除 `_check_layout_via_subtitle_qc` 导入和所有 QC 断言。
- 保留 node17 TTS 的真实调用/优雅回退验证。
- 检查 `video_input` 参数在删除 QC 后是否仍有用途；若无用途，调整函数签名和 `CASES` 调用方式。

### 5.9 P2：schema 测试必须增加新边的正向断言

原计划 D4 提出将 `node_checkpoint3_layout_review` 加入 `REMOVED_NODES`、从 `CHECKPOINT_NODES` 删除③，以及删除旧条件边断言。这些是必要的负向断言，但还不够。

建议增加正向断言：

```python
pairs = _edge_pairs(schema)
kinds = {(e["from"], e["to"]): e["kind"] for e in schema["edges"]}

assert "node_checkpoint3_layout_review" not in by_id
assert ("node_10b_inject_text_fx_en", "node_17_inject_english_tts") in pairs
assert kinds[("node_10b_inject_text_fx_en", "node_17_inject_english_tts")] == "normal"
assert not any(
    "node_checkpoint3_layout_review" in edge
    for edge in pairs
)
```

具体 `kind` 的字符串应以 `build_graph_schema()` 的现有 schema 契约为准；如果当前使用的名称不是 `normal`，应使用测试文件现有定义的普通边枚举值。

同时检查：

- `SCHEMA_VERSION == 9`。
- `CHECKPOINT_MARKS == ("①", "②")`。
- `CHECKPOINT_NODES` 只有①和②。
- `node_15_localize_covers_en` 与 `node_17_inject_english_tts` 仍共同连接 `node_09_inject_fx`。

### 5.10 P2：韧性测试和测试名还需要同步

#### `tests/integration/test_week5_resilience.py`

原计划删除 `test_checkpoint3_interrupt_and_resume_path` 是正确的。

但保留的 `test_resume_all_three_checkpoints_via_shared_checkpointer` 名称和文件 docstring 仍描述“三个关卡”。改造后应改为“两个人工关卡”，并更新头部覆盖说明。

`checkpoint3_resumed == 0` 断言删除后会变成几乎恒真的历史状态标签断言。建议删除该断言，将“图中不再存在③”交由 `test_workflow_graph_schema.py` 明确验证；如果保留负向 status_log 断言，应在测试注释中说明它只用于防止旧状态标签意外重新出现。

#### `tests/integration/test_week4_graph.py`

将 `test_week4_checkpoint3_normal_path` 改为描述英文字幕正常路径的名称，例如 `test_week4_english_subtitle_path_reaches_checkpoint2`。更新注释，删除“正常路径不触发③”的过时业务含义。

#### `tests/integration/test_state_field_compatability.py`

删除 `layout_issues` / `layout_issues_detected` 断言和字段列表后，还应更新：

- 文件头部对 Week 5 字段的说明。
- `_legacy_week4_state()` 注释。
- `test_nodes_use_state_get_not_subscript()` 的扫描字段列表。
- `test_workflow_state_new_fields_are_not_required()` 的待检查字段列表。

不要把这个兼容性测试整体删除；它仍然是其他 State 字段的旧 checkpoint 兼容性保护。

### 5.11 P2：`GATE_META` 删除③后，不要误删通用恢复兜底

保留 `web/human_gate_payload.py` 的 `_FALLBACK_META` 是合理的。它用于未知 checkpoint 的通用展示兜底，不是③专用能力。

不过旧的③挂起线程不能因为 UI 有 fallback 文案就被认为可以恢复。UI 兜底只解决展示，不解决新 LangGraph 图中已经不存在的节点。

建议在恢复服务层对已知退休关卡③的存量挂起任务返回明确的 409 错误或给出作废提示，避免用户点击“恢复”后得到不清晰的 LangGraph 内部错误。

---

## 6. 修订后的文件改动清单

### A. LangGraph 核心拓扑

| 文件 | 修订后的操作 | 验收点 |
|---|---|---|
| `graph.py` | 删除 `node_checkpoint3_layout_review` import、注册、`route_after_translate()` 和条件边；新增 10b → 17 普通边 | 图可编译；新边存在；无旧节点引用 |
| `graph.py` 模块 docstring | 更新拓扑图、关卡说明、K3=B 历史描述、Week 5 拆分说明 | 当前拓扑描述准确；历史记录明确标为历史 |
| `nodes/node_checkpoint3_layout_review.py` | 整文件删除 | 全仓无生产 import |
| `nodes/node_16a_translate_and_check.py` | 删除 QC helper、循环、fallback layout validation 和 QC 状态输出；保留翻译、marker、草稿写入、SRT | 仍返回英文字幕与 SRT 路径 |
| `nodes/node_16_translate_subtitles.py` | 删除 `validate_layout`、`_build_interrupt_payload` 及专属字体辅助代码 | 保留 marker、草稿解析、SRT 相关能力 |
| `state.py` | 删除 `layout_issues`、`layout_issues_detected`、`preview_video_path`、`subtitle_qc_evidence_frames` | 全仓无残留运行时读取/写入 |

注意：当前 `checkpoint3_triggered` 已经是历史注释说明，不是需要再次删除的活跃 TypedDict 字段。只要把相关历史注释更新清楚即可。

### B. 工作流 schema / 监控

| 文件 | 修订后的操作 | 验收点 |
|---|---|---|
| `monitoring/workflow_graph.py` | `SCHEMA_VERSION: 8 → 9`；`CHECKPOINT_MARKS` 改为①/②；删除③节点元数据及两条旧边标签 | 动态 schema 只输出两个人工关卡 |
| `monitoring/runs_table.py` | 更新 `current_checkpoint` 说明、调用节点清单 | 当前有效关卡说明不再包含③ |
| `tests/integration/test_workflow_graph_schema.py` | 将③加入 `REMOVED_NODES`；`CHECKPOINT_NODES` 只保留①/②；删除旧③断言并增加 10b→17 普通边正向断言 | 拓扑回归得到明确保护 |

### C. 人工关卡 payload 与前端

| 文件 | 修订后的操作 | 验收点 |
|---|---|---|
| `web/human_gate_payload.py` | 从 `GATE_META` 删除③；删除不再使用的 preview/evidence 字段；按最终决定处理 issues；保留 `_FALLBACK_META` | ①/② payload 仍完整 |
| `web/console.html` | 更新 `draftKind` 为固定 `zh`；删除 QC preview/evidence 展示；若 issues 仅为③服务则一并删除 issues 显示代码；placeholder 改成①/② | ①/②打开草稿目录正常 |
| `web/api_console.py` | 更新恢复关卡说明及英文草稿未生成时的提示文案 | 不再把“关卡③之前”作为状态边界 |
| `web/console_service.py` | 更新 `resume_run()` 的 checkpoint 参数说明 | 当前有效值准确，interrupt ID 匹配仍保留 |
| `web/gate_state.py` | 检查是否需要专门拒绝退休③的历史挂起任务；保持通用 snapshot 读取 | 旧任务恢复错误清晰可操作 |

### D. 恢复入口与通用匹配

| 文件 | 修订后的操作 | 验收点 |
|---|---|---|
| `scripts/resume_workflow.py` | 更新有效关卡 help/docstring 为①/②；保留通过 interrupt ID / legacy ID 的通用匹配 | CLI 提示不再宣称③是当前有效关卡 |
| `resume_utils.py` | 保留通用 payload 匹配算法；更新“②/③可同时挂起”等过时描述和示例 | 不把匹配机制错误硬编码为固定两关 |
| `agent_gate/resume_match.py` | 仅做全仓引用扫描，不为本次删除增加不必要的分支 | 通用匹配保持稳定 |

### E. 测试、脚本和 README

| 文件 | 修订后的操作 |
|---|---|
| `tests/unit/test_node_checkpoint3_layout_review.py` | 整文件删除 |
| `tests/unit/test_node_16_translate_subtitles.py` | 删除 `_build_interrupt_payload` 和 `validate_layout` 相关用例；保留 SRT、marker、草稿读取测试 |
| `tests/unit/test_node_16a_translate_and_check.py` | 删除 QC 链与布局路由相关用例；保留翻译、marker、SRT、无源字幕 skip 等用例；补充翻译完成和 SRT 仍正常生成的回归测试 |
| `tests/integration/test_reorder_topology.py` | 删除专门依赖③的测试；按精确基线 worktree 对照其历史失败 |
| `tests/integration/test_week5_resilience.py` | 删除③ interrupt/resume 路径测试；保留并重命名①/②跨实例恢复测试 |
| `tests/integration/test_week4_graph.py` | 重命名正常英文字幕路径测试并更新说明 |
| `tests/integration/test_state_field_compatability.py` | 删除四个 retired field 中对应的测试断言；更新文档说明和字段列表 |
| `tests/unit/test_console_gates_api.py` | 删除只验证③ issues/preview 的测试；保留①/②和 fallback 测试 |
| `tests/integration/test_workflow_graph_schema.py` | 增加新普通边、无③节点、schema 版本 9 和剩余关卡映射断言 |
| `scripts/video_edit_phase56_e2e.py` | 将 `case_checkpoint3_and_tts` 改为 `case_english_tts`；同步输出目录、报告 case 名、CASES 注册键；删除 QC 检查，保留 node17 TTS 测试 |
| `README.md` | 更新主图、16a 描述、关卡列表、State 字段和说明；保留有明确历史意义的旧设计记录时标明“已删除/历史” |

### F. 明确不删除的能力

- `video_edit_capabilities/subtitle_build`、`subtitle_render`、`subtitle_qc`：保留能力层，只移除 16a 的调用。
- `jy_common/subtitle_style.py` 中 `broadcast_en` 预设：本次先保留，不能仅因为 16a 不再消费就全仓删除。
- `web/node_map/workflow.json`：当前不包含关卡③，不修改。
- `resume_utils.py` 的通用匹配实现：保留，仅更新说明。
- `web/human_gate_payload.py` 的 `_FALLBACK_META`：保留通用兜底。
- PostgreSQL checkpointer、runs_table 表结构：不因关卡③删除而整体迁移或删除。

---

## 7. 修订后的分阶段执行计划

### 阶段 0：执行前盘点与基线冻结

**目标：**确保执行前后结果可对照，避免误处理存量任务。

任务：

1. 确认工作区干净，记录 `main` 的当前 HEAD。
2. 建立基线 worktree：`4f48bd3`，不要使用 `6679e54`。
3. 在 PostgreSQL `workflow_runs` 台账中盘点仍挂起在③的任务。
4. 备份需要保留的剪映草稿、任务日志和运行记录。
5. 确认是否存在①/②挂起任务，并纳入新图恢复验证。
6. 捕获改造前工作流 schema 快照。

退出条件：基线 SHA、测试环境、存量③任务处置清单和 schema 快照均有记录。

### 阶段 1：先修改核心拓扑

**目标：**移除关卡③，使英文线直接通过 10b 到 17。

任务：

1. 删除 `graph.py` 对 `node_checkpoint3_layout_review` 的 import。
2. 删除节点注册。
3. 删除 `route_after_translate()`。
4. 删除 10b 到③的条件边以及③到17的边。
5. 添加 `g.add_edge("node_10b_inject_text_fx_en", "node_17_inject_english_tts")`。
6. 删除 `nodes/node_checkpoint3_layout_review.py`。
7. 更新 `graph.py` 顶部拓扑图和历史说明。

退出条件：图能编译；10b 到17为普通边；15和17的 fan-in 未改变；没有悬空节点。

### 阶段 2：清除 16a 布局 QC 与死状态

**目标：**让 16a 只负责英文字幕翻译、草稿写入和 SRT 生成。

任务：

1. 删除 `_check_layout_via_subtitle_qc()`、`_segments_en_to_transcript_records()` 和 `_QC_CHAIN_WORK_ROOT`。
2. 删除多视频 QC 循环和 `validate_layout()` fallback。
3. 清理 `RunContext`、QC 能力、视频路径解析和不再使用的标准库 import。
4. 删除 `layout_issues`、`layout_issues_detected`、`preview_video_path`、`subtitle_qc_evidence_frames` State 定义。
5. 在 `node_16_translate_subtitles.py` 删除 `validate_layout()`、`_build_interrupt_payload()` 及其私有字体辅助代码和专属 import。
6. 确认 `write_srt_from_segments()` 仍然在 16a 的正常路径中调用。

退出条件：16a 保留翻译、marker、草稿和 SRT；不再调用 QC；State 不再声明这四个字段。

### 阶段 3：关卡面板与前端修正

**目标：**让前端只展示①/②，并保证人工操作不回归。

任务：

1. `GATE_META` 删除③。
2. 删除 preview/evidence 专用字段；按最终设计决定是否删除 issues 展示字段。
3. 保留 `_FALLBACK_META`。
4. `console.html` 将 `draftKind` 处理为固定 `zh`，不能只删除变量声明。
5. 删除 QC 视频、证据帧展示；如 issues 不再是通用字段，也删除 issues HTML 生成逻辑。
6. 输入框 placeholder 只展示①/②。
7. 更新 `api_console.py`、`console_service.py` 中的关卡和草稿状态说明。

退出条件：①和②的卡片、草稿路径、打开目录、恢复按钮均工作；无任何 QC 展示残留。

### 阶段 4：schema 和监控数据一致性

**目标：**后端 schema、监控元数据和缓存版本统一。

任务：

1. `SCHEMA_VERSION` 从 8 改为 9。
2. `CHECKPOINT_MARKS` 改为 `("①", "②")`。
3. 删除 `_NODE_META` 中③节点条目。
4. 删除 10b→③ 和 10b→17 的旧条件边标签。
5. 更新 `runs_table.py` 的关卡取值和调用约定注释。
6. 不删除数据库表，不删除历史已完成任务记录。

退出条件：节点图 schema 只出现①/②；缓存 key 使用版本9；数据库说明与当前图一致。

### 阶段 5：恢复入口与兼容性整理

**目标：**当前有效关卡说明准确，旧③挂起任务可被明确识别和处置。

任务：

1. 更新 `scripts/resume_workflow.py` 的 CLI help 和文档说明。
2. 更新 `resume_utils.py` 中关于③并发挂起的历史描述，但保留通用匹配代码。
3. 对退休③的旧挂起任务返回明确作废/重新创建提示，避免模糊恢复错误。
4. 验证①/②历史挂起任务在新图下的恢复路径。
5. 更新旧 checkpoint State 兼容性测试。

退出条件：CLI 不宣传③为当前有效关卡；旧③任务有明确处置；①和②的恢复通过测试。

### 阶段 6：测试与 E2E 脚本

**目标：**删除过时测试，同时证明主业务链未受损。

任务：

1. 删除③节点单测和③ interrupt 集成测试。
2. 将剩余 schema 测试改为只校验①/②。
3. 为 10b→17 普通边增加正向断言。
4. 保留并调整英文字幕翻译、marker、SRT 生成测试。
5. 更新 Week4、Week5、State 兼容性、console API 测试。
6. `video_edit_phase56_e2e.py` 改为 `english_tts`，只验证英文 TTS，不再做 QC 链验证。
7. 用基线 worktree 对照所有原有存量失败。

退出条件：无新增单测/集成测试失败；保留的业务行为有明确正向测试。

### 阶段 7：文档、静态扫描与发布验收

**目标：**清除当前代码说明中的③引用，完成真实工作流验证。

任务：

1. 更新 README 的工作流图、16a 职责和 State 字段说明。
2. 对生产代码执行全仓引用扫描。
3. 执行 schema 快照冒烟。
4. 执行前端离线冒烟。
5. 使用 PostgreSQL 验证①/②恢复。
6. 新建任务跑完整 auto 工作流。
7. 检查 node map、日志、关卡面板、英文字幕、英文 TTS、尾链和交付 QA。
8. 汇报结果，提交和推送由仓库操作员另行决定。

退出条件：本计划第 9 章全部验收项通过。

---

## 8. 验证命令和检查清单

### 8.1 基线和工作区

```bash
git status --short
git rev-parse HEAD
git worktree add .worktrees/baseline-4f48bd3 4f48bd3
```

### 8.2 单元测试

```bash
python -m pytest tests/unit -q
```

判定依据：进程退出码、失败用例详情和最终统计。不要只依赖可能被 pytest 配置折叠的汇总行。

### 8.3 集成测试

```bash
python -m pytest tests/integration -q
```

执行前确认 PostgreSQL 容器、连接 URI、数据库迁移和测试 fixture 均可用。若项目本地使用容器 `edb-postgres16`，先确认容器状态再启动；容器启动失败不得将相关 fixture 失败简单归为本次代码回归。

### 8.4 工作流 schema 快照

Windows PowerShell 示例：

```powershell
python scripts/capture_graph_schema_snapshots.py "$env:TEMP\c3-removal-check"
```

检查 JSON：

- `schema_version` 为 9。
- 不包含 `node_checkpoint3_layout_review`。
- 存在普通边 `node_10b_inject_text_fx_en → node_17_inject_english_tts`。
- 不存在 10b 到③的条件边。
- 关卡集合只有①和②。
- 15与17到09的 fan-in 保持不变。

### 8.5 源码残留扫描

建议在仓库根目录执行：

```bash
rg -n "node_checkpoint3_layout_review|route_after_translate|layout_issues(_detected)?|preview_video_path|subtitle_qc_evidence_frames|_build_interrupt_payload|_check_layout_via_subtitle_qc|_segments_en_to_transcript_records|validate_layout|checkpoint3_and_tts|checkpoint3_tts" --glob '!.worktrees/**' --glob '!docs/**' --glob '!**/__pycache__/**'
```

对于当前代码中仍允许出现的历史注释、`REMOVED_NODES` 负向断言和归档设计文档，应逐条标记为“有意保留”。生产路径、活动测试和活动 UI 中不得出现退休节点的调用或展示。

还应独立扫描关卡标记：

```bash
rg -n "①|②|③" graph.py state.py monitoring web nodes scripts tests README.md
```

对命中逐条分类。不能机械地要求全仓零字符③，因为历史说明、负向测试和存量数据说明可以保留；要求的是当前有效流程、UI 和 CLI 不再把③描述为活动关卡。

### 8.6 前端冒烟

至少检查：

1. node map 不显示“布局人工复核③”。
2. 10b 与17之间显示普通直连边。
3. 点击①的“打开草稿目录”能打开中文主草稿目录。
4. 点击②的“打开草稿目录”能打开中文主草稿目录。
5. 恢复输入和卡片只显示①/②。
6. 浏览器控制台无 `ReferenceError`、无访问已删除字段导致的错误。
7. schemaVersion=9 导致旧布局缓存重新计算；TB/LR 两种布局都检查。

### 8.7 完整工作流验收

使用全新 auto 任务，不要拿挂起在旧③节点的任务作为新图验收任务。

验收顺序：

1. 任务正常生成主草稿和英文分支草稿。
2. 16a 正常翻译英文字幕并写入 SRT。
3. 08b 正常写入英文字幕。
4. 10b 正常写入英文花字。
5. 10b 直接进入17，不发生③ interrupt。
6. node17 正常生成英文 TTS，或在服务不可用时按既有规则优雅回退。
7. 15与17均完成后才触发09。
8. 尾链依次执行到关卡②。
9. ②恢复后继续执行13、交付 QA 和后续流程。
10. 检查 state 中不再生成四个 layout QC 字段。

---

## 9. 最终验收标准

以下项目全部通过，才算本次改造完成。

### 9.1 拓扑验收

- [ ] 编译后的主图不再注册 `node_checkpoint3_layout_review`。
- [ ] `route_after_translate()` 已删除。
- [ ] 10b 到17是普通直连边。
- [ ] 15和17仍通过列表型 fan-in 汇入09。
- [ ] 关卡①和②可达，且 interrupt / resume 功能未受损。
- [ ] `SCHEMA_VERSION == 9`。

### 9.2 字幕业务验收

- [ ] 16a 仍能读取源字幕。
- [ ] 翻译 API marker 幂等机制仍有效。
- [ ] 英文字幕仍写入英文分支草稿。
- [ ] 英文 SRT 仍能生成且路径写入 state。
- [ ] QC helper 不再被 16a 调用。
- [ ] 英文字幕布局检测与③人工复核已明确从本次产品流程移除。

### 9.3 UI 与恢复验收

- [ ] `GATE_META` 中不再有③。
- [ ] `_FALLBACK_META` 仍可兜底未知 checkpoint。
- [ ] 前端只显示①/②。
- [ ] ①和②“打开草稿目录”功能正常，不出现 `draftKind` 未定义。
- [ ] 旧③挂起任务能够被明确识别并作废，不误导用户继续恢复。
- [ ] 旧①/②挂起任务已通过 PostgreSQL 恢复验证，或已明确不承诺兼容并在发布前全部完成/作废。

### 9.4 测试和交付验收

- [ ] 单元测试零新增失败。
- [ ] 集成测试零新增失败。
- [ ] 存量失败对照基于 `4f48bd3`，不是 `6679e54`。
- [ ] schema 正向边断言通过。
- [ ] 前端离线冒烟通过。
- [ ] 完整 auto 新任务通过。
- [ ] README 和活动代码说明与新拓扑一致。
- [ ] 源码残留扫描结果已逐条归类。

---

## 10. 风险登记表

| 编号 | 风险 | 影响 | 处理措施 | 优先级 |
|---|---|---|---|---|
| R1 | 删除 `draftKind` 声明但保留模板引用 | ①/②打开草稿目录时报 ReferenceError | 固定为 `zh`，加前端测试 | P1 |
| R2 | 用 6679e54 做 4f48bd3 的测试基线 | 错误分类新增/存量失败 | 精确建立 4f48bd3 worktree | P1 |
| R3 | 生产仍有挂起在③的线程 | 旧线程无法按新图恢复 | 发布前盘点、备份并作废 | P1 |
| R4 | 删除布局 QC | 英文字幕问题不再自动检测 | 在发布说明中明示，纳入验收 | P1 |
| R5 | 漏删私有字体辅助代码和 import | 产生死代码或静态检查失败 | 同步删除字体 helper 和专属 import | P2 |
| R6 | CLI / resume 文档仍写③ | 运维按旧说明操作 | 更新 resume_workflow、resume_utils、API 和 service 注释 | P2 |
| R7 | 前端仍残留 evidence/issues 展示 | 死代码和语义不一致 | 同步清理 payload 与 HTML | P2 |
| R8 | E2E 名称残留 checkpoint3 | 测试报告和输出目录语义错误 | 同步修改函数、case、目录名 | P2 |
| R9 | schema 测试只删除旧断言，没有新边正向断言 | 可能漏掉 10b→17 接线错误 | 增加普通边和 fan-in 断言 | P2 |
| R10 | 旧①/② checkpoint 恢复行为未经验证 | 发布后存量任务恢复失败 | PostgreSQL 真实 saver 验证 | P1 |

---

## 11. 原计划最终评价

### 11.1 正确性

原计划的主要源码判断是成立的：当前主图中确有③节点和条件边；16a 中确有为③提供布局检测、预览和证据帧的 QC 编排；删除③后用 10b→17 普通边接续，保留15/17 fan-in，逻辑上不会改变两条业务线的汇合契约。

### 11.2 可行性

技术上可实施。删除范围虽然横跨 LangGraph、State、监控 schema、人工关卡 payload、前端、测试和 README，但依赖边界相对清楚。只要保留 16a 的翻译、marker、草稿写入和 SRT 生成逻辑，不需要重构整个英文字幕链。

### 11.3 完整性

原计划还不够完整，主要不足是：

1. 前端变量删除不完整，可能破坏打开草稿目录。
2. 测试基线提交不一致。
3. 恢复 CLI、通用恢复说明、runs_table 和英文草稿状态提示仍有③相关说明。
4. 前端证据帧和 issues 展示清理没有覆盖完整。
5. 测试脚本内部输出目录和报告 ID 没有同步重命名。
6. `validate_layout` 的私有字体 helper 及 import 未完整列入删除清单。
7. 对旧③线程以及旧①/②任务的 PostgreSQL 恢复策略不足。
8. schema 测试需要增加新普通边的正向断言。

### 11.4 最终结论

**结论：方案方向正确，技术可行，但原始计划需要按本文修订后执行。**

最重要的执行约束是：

- 不能把“删除关卡③”误解为“只删除一个节点文件”。
- 不能在删除 QC 后仍宣称布局检测能力保留。
- 不能只删除 `draftKind` 声明而不修复前端引用。
- 不能使用 6679e54 作为 4f48bd3 的同版本测试对照。
- 不能忽略生产中已挂起的 PostgreSQL checkpoint。
- 不能破坏 15/17 的 fan-in，也不能删除①/②的 interrupt / resume。

完成以上修订后，建议按第 7 章顺序分阶段实施，所有验收项通过后再决定是否提交和推送代码。

---

## 12. 核验参考

以下链接对应本次核验使用的当前 `main` 源码或文档：

- [原始关卡③删除计划](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E4%BF%AE%E6%AD%A3/auto-video-editor_%E5%88%A0%E9%99%A4%E5%85%B3%E5%8D%A1%E2%91%A2%E5%B8%83%E5%B1%80%E4%BA%BA%E5%B7%A5%E5%A4%8D%E6%A0%B8%E8%8A%82%E7%82%B9_%E6%89%A7%E8%A1%8C%E8%AE%BE%E8%AE%A1_20261115.md)
- [`graph.py`](https://github.com/wayyet/auto-video-editor/blob/main/graph.py)
- [`nodes/node_16a_translate_and_check.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_16a_translate_and_check.py)
- [`nodes/node_16_translate_subtitles.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_16_translate_subtitles.py)
- [`nodes/node_checkpoint3_layout_review.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_checkpoint3_layout_review.py)
- [`state.py`](https://github.com/wayyet/auto-video-editor/blob/main/state.py)
- [`monitoring/workflow_graph.py`](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/workflow_graph.py)
- [`monitoring/runs_table.py`](https://github.com/wayyet/auto-video-editor/blob/main/monitoring/runs_table.py)
- [`web/human_gate_payload.py`](https://github.com/wayyet/auto-video-editor/blob/main/web/human_gate_payload.py)
- [`web/console.html`](https://github.com/wayyet/auto-video-editor/blob/main/web/console.html)
- [`web/api_console.py`](https://github.com/wayyet/auto-video-editor/blob/main/web/api_console.py)
- [`web/console_service.py`](https://github.com/wayyet/auto-video-editor/blob/main/web/console_service.py)
- [`scripts/resume_workflow.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/resume_workflow.py)
- [`resume_utils.py`](https://github.com/wayyet/auto-video-editor/blob/main/resume_utils.py)
- [`scripts/video_edit_phase56_e2e.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/video_edit_phase56_e2e.py)
- [`tests/integration/test_workflow_graph_schema.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/integration/test_workflow_graph_schema.py)
- [`tests/integration/test_week5_resilience.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/integration/test_week5_resilience.py)
- [`tests/integration/test_state_field_compatability.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/integration/test_state_field_compatability.py)
- [`tests/integration/test_unreachable_nodes_wiring.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/integration/test_unreachable_nodes_wiring.py)
- [基线提交 4f48bd3 与当前 main 的比较](https://github.com/wayyet/auto-video-editor/compare/4f48bd3...main)
- [较早提交 6679e54 与计划基线 4f48bd3 的比较](https://github.com/wayyet/auto-video-editor/compare/6679e54...4f48bd3)

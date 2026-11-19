# auto-video-editor：关卡①改为仅 QC 降级时挂起
## 设计审查与修订后的分阶段执行计划

- **审查日期**：2026-11-20
- **仓库**：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)
- **分支**：`main`
- **审查基线 Commit**：`6f44a41941a2d9124b4844c4e69feaf5f56b3aeb`
- **被审查文档**：[auto-video-editor_关卡①分镜顺序调整改为仅QC降级挂起_分析与实施计划_20261120.md](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/docs/第八期开发/auto-video-editor_关卡①分镜顺序调整改为仅QC降级挂起_分析与实施计划_20261120.md)
- **本文档状态**：静态代码审查完成；修订建议待实施
- **实施状态**：未实施。本次仅读取 GitHub `main` 分支文件，没有修改仓库、启动服务或执行 pytest。

---

## 1. 审查结论

### 1.1 总体判断

原方案的核心方向成立：保留 `node_06_human_reorder`、图边、checkpoint 标识及恢复 API，只将关卡①从“每次都调用 `interrupt()`”改为“QC 降级时调用 `interrupt()`，其余情况自动通过”。这比删除节点或重排图拓扑的影响范围更小，也能保留已挂起任务的恢复入口。

但原文档目前**不建议直接照单执行**。它对当前 QC 路由、错误类型和部分测试行为的描述存在事实偏差；此外，状态缺失、旧 checkpoint 恢复、测试中的真实 interrupt 以及到达关卡②后的 BGM 处理尚未形成完整闭环。

### 1.2 关键审查结果

| 级别 | 发现 | 处理要求 |
|---|---|---|
| P0 | 原文档把所有 `escalated` 描述成重试耗尽后软降级到关卡①；当前代码对结构性错误走 `assembly_failed` → `END` | 修正文档中的 QC 路由说明，保留当前 fail-closed 逻辑 |
| P0 | 原文档把黑帧、静音、冻结帧列为结构性错误；当前代码把它们归为预览级问题 | 修正分类和挂起范围 |
| P0 | 仅按 `assembly_qc_status != "escalated"` 自动通过，会把 QC 状态缺失、未知状态也当作正常 | 明确 QC 关闭、状态缺失、未知状态和旧任务的处理规则 |
| P1 | `test_assembly_qc_graph.py` 使用 `interrupt_before`，并未执行关卡①节点本身 | 增加真正调用 `human_reorder()` / `interrupt()` 的行为测试 |
| P1 | `test_unreachable_nodes_wiring.py` 的恢复用例会从关卡①前移到关卡②；恢复前必须模拟添加 BGM | 更新恢复用例的检查点和草稿准备步骤 |
| P1 | `SCHEMA_VERSION` 计划由 10 改为 11，但现有测试仍断言等于 10 | 更新 `tests/integration/test_workflow_graph_schema.py:356` |
| P1 | 原文档把 `test_workflow_graph_schema.py:393` 描述为显示名断言；实际该行断言的是 checkpoint 编号 | 修正文档中的测试定位 |
| P1 | 对历史挂起任务的恢复兼容性只有文字推断，没有旧 checkpoint 回归测试 | 增加 PostgreSQL 挂起态跨版本恢复测试 |
| P2 | 节点模块 docstring、配置注释仍描述每轮必须人工调序或旧 ADR-3 软降级 | 纳入活代码文案清理 |
| P2 | 自动通过分支没有独立审计标记 | 建议增加自动通过状态标记，便于排查运行轨迹 |
| P2 | 图边标签拟写成“仅 QC 降级进入关卡①”，容易误解为边本身是条件边 | 文案应表达“节点始终经过，节点内部按 QC 状态决定自动通过或挂起” |

### 1.3 可以保留的原方案内容

以下设计与当前代码核对后没有发现需要推翻的理由：

1. `node_06_human_reorder` 节点 ID 保持不变。
2. `checkpoint="①"`、`legacy_id="checkpoint1_reorder"`、`step=6` 保持不变。
3. `generate_draft → node_06_human_reorder → node_07_speed_fit` 图边不变。
4. 关卡② `node_12_human_add_bgm` 和 `checkpoint2_add_bgm` 不重编号、不改变恢复契约。
5. `web/console.html` 的卡片标题、说明和 QC 产物入口由 API 数据驱动；更新后端元数据和 payload 后，原则上不需要改卡片渲染逻辑。
6. QC 关闭时，当前图确实由 `storyline_join` 直接进入 `generate_draft`，不会执行 assembly 六节点。
7. `SCHEMA_VERSION` 变更会改变浏览器主图布局缓存 key；仅显示名变化也有 bump 先例，因此 bump 11 可以保留，但必须同步测试断言并确认布局缓存重建的影响可接受。

---

## 2. 当前代码行为核验

### 2.1 关卡①当前无条件挂起

当前 `nodes/node_06_human_reorder.py` 的入口逻辑为：

```python
def human_reorder(state: WorkflowState) -> dict:
    """关卡①:挂起等待用户确认,resume 后发通知并通过(reject 显式拒绝)。"""
    resume_value = interrupt(_build_interrupt_payload(state))
    return _post_resume(state, resume_value=resume_value)
```

当前每次执行该节点都会调用 `interrupt()`，没有依据 `assembly_qc_status` 判断是否需要人工处理。

代码位置：
- [`nodes/node_06_human_reorder.py:144-147`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/nodes/node_06_human_reorder.py#L144-L147)

### 2.2 图拓扑与 QC 关闭路径

当前图的关键接线为：

```text
QC 开启：storyline_join → assembly QC 六节点 → generate_draft
QC 关闭：storyline_join → generate_draft

两种模式：generate_draft → node_06_human_reorder → node_07_speed_fit
```

因此，关卡①改造不需要删除节点或改图边。正常 QC 通过后，图仍会调度 `node_06_human_reorder`，只是节点内部不再等待用户，继续进入 `node_07_speed_fit`。

代码位置：
- [`graph.py:587-624`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/graph.py#L587-L624)

### 2.3 当前 QC 路由不是统一软降级

当前 `route_after_assembly_qc()` 逻辑：

| 状态 | 当前路由 |
|---|---|
| `pass` | `assembly_write_report` |
| `pass_with_warnings` | `assembly_write_report` |
| `escalated` 且未到最大重试次数 | `assembly_repair_loop` |
| `escalated` 且达到最大重试次数，`assembly_structural_error=True` | `assembly_failed`，最终到 `END` |
| `escalated` 且达到最大重试次数，`assembly_structural_error=False` | `assembly_write_report`，继续生成正式草稿 |

因此，关卡①只可能接收到已经通过当前 QC 路由、未被结构性错误 fail-closed 截断的任务。

代码位置：
- [`nodes/assembly/node_validate_render_qc.py:59-79`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/nodes/assembly/node_validate_render_qc.py#L59-L79)
- [`graph.py:606-618`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/graph.py#L606-L618)

### 2.4 当前结构性错误与预览级问题分类

`assembly_capabilities/repair_strategies.py` 将错误分成两组：

| 分类 | 错误代码 | 路由含义 |
|---|---|---|
| 结构性错误 | `broken_reference`、`missing_asset`、`invalid_range`、`missing_segment`、`extra_segment`、`track_desync` | 达重试上限后应 fail-closed，不应交给关卡①放行 |
| 预览级问题 | `black_frame`、`freeze_frame`、`silence` | 可以自动修复；无法修复且重试耗尽后，当前路由允许写报告并继续 |

原文档把黑帧、静音、冻结帧称为结构性错误，与代码相反。

代码位置：
- [`assembly_capabilities/repair_strategies.py:23-47`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/assembly_capabilities/repair_strategies.py#L23-L47)

### 2.5 当前配置注释已过时

`config.py:459-461` 仍然写着“重试到上限后不硬性中断，照常进入关卡①”。这与当前 `route_after_assembly_qc()` 的结构性错误 fail-closed 分支不一致。

修订时不能只把注释替换成“escalated 挂起①”，而应准确描述：结构性错误达到上限后终止；允许进入报告流程的非结构性降级，之后才由关卡①决定是否挂起。

代码位置：
- [`config.py:451-461`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/config.py#L451-L461)

### 2.6 创建台实际如何显示关卡内容

`web/human_gate_payload.py` 的 `build_gate_view()` 使用：

- `GATE_META` 提供标题、节点名和 `action_hint`；
- interrupt payload 提供动态 `instructions`；
- payload 或 checkpoint state 提供 QC 状态、报告路径、预览路径和警告。

`web/console.html` 将这些字段渲染为卡片，并在有路径时显示打开报告目录、预览目录的按钮。因此，原文档提出更新 `GATE_META`、payload instructions 和 `COMMON_RESUME_NOTE`、不改卡片渲染逻辑，方向正确。

代码位置：
- [`web/human_gate_payload.py:24-41`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/web/human_gate_payload.py#L24-L41)
- [`web/human_gate_payload.py:65-135`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/web/human_gate_payload.py#L65-L135)
- [`web/human_gate_payload.py:146-151`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/web/human_gate_payload.py#L146-L151)
- [`web/console.html:1079-1105`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/web/console.html#L1079-L1105)

---

## 3. 必须修订的设计决策

### 3.1 明确关卡①的触发边界

推荐规则如下：

| QC 执行情况 | QC 状态 | 结构性错误标记 | 关卡①行为 |
|---|---|---|---|
| QC 已执行 | `pass` | `False` | 自动通过，不调用 `interrupt()` |
| QC 已执行 | `pass_with_warnings` | `False` | 自动通过，不调用 `interrupt()`；警告保留在报告 |
| QC 已执行 | `escalated` | `False` | 调用 `interrupt()`，人工查看报告后继续或拒绝 |
| QC 已执行 | `escalated` | `True` | fail-closed；不允许通过关卡①的 `true` 放行 |
| QC 未启用 | 状态缺失 | 不适用 | 自动通过，并留下 QC 未启用的审计标记 |
| QC 已启用 | 状态缺失或未知值 | 未知 | 不允许默认为通过；报错并终止当前运行 |
| 旧 checkpoint | 状态字段或新标记缺失 | 未知 | 按明确的旧任务兼容策略恢复，不能仅凭“缺失”推断为 QC 正常 |

这里必须区分：**QC 未启用**与**QC 应当执行但没有产出状态**。两者的状态字段可能都为空，但业务含义完全不同。

推荐新增一个可持久化的运行来源标记，例如 `assembly_qc_gate_enabled`，记录本次任务是否执行了 assembly QC。它应在新任务进入工作流时写入 state，并纳入 `WorkflowState` 类型定义。这样恢复任务时不需要依赖恢复进程当前的环境变量来推断历史运行配置。

旧 checkpoint 没有该字段时，按兼容分支处理：

1. `assembly_qc_status == "escalated"`：按旧任务的人工复核语义恢复；如果 `assembly_structural_error=True`，则不得人工放行。
2. `assembly_qc_status` 为 `pass` 或 `pass_with_warnings`：按已确认的新策略自动通过，并验证历史 resume 值不会造成任务卡死。
3. 状态字段缺失：不能直接把旧任务等同于新任务的 QC 关闭模式；应通过旧 checkpoint 回归测试确认恢复行为，并记录兼容分支。

如果实施中不增加该标记，也必须写出等价、可测试的判定规则。不能继续使用“除 escalated 以外全部直接 `return {}`”作为最终规则。

### 3.2 推荐节点行为伪代码

下面是建议的控制结构。实际实现时应根据最终决定的 QC 来源标记和旧 checkpoint 兼容策略调整；不能直接复制后跳过测试。

```python
def human_reorder(state: WorkflowState) -> dict:
    qc_status = state.get("assembly_qc_status")
    qc_enabled = state.get("assembly_qc_gate_enabled")

    # 结构性错误不应由人工确认 true 绕过；正常路由本来就应在上游终止，
    # 此处的检查用于保护历史 checkpoint 或异常状态。
    if state.get("assembly_structural_error") is True:
        raise RuntimeError("[node_06] 检测到结构性 QC 错误，禁止人工放行")

    if qc_status in ("pass", "pass_with_warnings"):
        return _mark_auto_pass(state, qc_status)

    if qc_status == "escalated":
        resume_value = interrupt(_build_interrupt_payload(state))
        return _post_resume(state, resume_value=resume_value)

    if qc_enabled is False and qc_status is None:
        return _mark_auto_pass(state, "qc_disabled")

    # 旧 checkpoint / 状态异常必须有显式兼容分支；不得静默按正常处理。
    raise RuntimeError(f"[node_06] 无法确定 assembly QC 状态: {qc_status!r}")
```

注意：`_mark_auto_pass()` 是建议的审计收尾函数，不应调用 `_send_notification()`，也不应产生“用户已经人工确认”的假记录。可以追加独立的 `status_log` 标签，例如 `checkpoint1_auto_passed`，并将 QC 状态写入运行事件或日志。

### 3.3 人工拒绝值

`_post_resume()` 当前仅在 `assembly_qc_status == "escalated"` 且恢复值为 `reject` 时抛出 `RuntimeError`。该语义可以保留。

创建台和 CLI 对恢复值使用 JSON/字符串解析；前端 `parseResumeValue()` 和 CLI `parse_resume_value()` 都支持 JSON 解析失败后回退为原始字符串。因此输入 `reject` 可以作为字符串传入。新文案应明确：

- `true`：接受 QC 降级结果并继续；
- `reject`：拒绝 QC 降级结果，终止运行。

此操作只适用于 `escalated` 状态。结构性错误不应进入人工放行分支。

### 3.4 图边标签用语

原文档建议将 `generate_draft → node_06_human_reorder` 标签改为“初始草稿就绪；仅 QC 降级进入关卡①”。但当前这条边仍是普通无条件边，所有任务都会经过 `node_06_human_reorder`，只是节点内部可能自动通过。

建议改为：

> 初始草稿就绪；节点内按 QC 状态自动通过或挂起复核

这样不会让流程图误导用户，以为图边本身变成了条件路由。

---

## 4. 修订后的实施计划

### 阶段 0：冻结行为口径与兼容规则

| 任务 | 内容 | 验收标准 |
|---|---|---|
| 0.1 | 确认结构性错误与预览级问题的准确分类 | 文档与 `STRUCTURAL_CODES`、`PREVIEW_CODES` 完全一致 |
| 0.2 | 明确 QC 关闭、QC 状态缺失、未知状态的处理规则 | 有明确决策表和单测，不存在隐式默认放行 |
| 0.3 | 明确旧 checkpoint 兼容策略 | 至少覆盖旧任务 status=escalated、pass、缺失三种情况 |
| 0.4 | 明确结构性错误保护 | `assembly_structural_error=True` 时，不可能通过关卡①的 `true` 放行 |

**阶段出口**：状态判定矩阵、旧任务兼容矩阵经设计确认后，才进入代码修改。

### 阶段 1：节点控制逻辑

**主要文件**：
- `nodes/node_06_human_reorder.py`
- `state.py`（若新增 QC 来源标记）
- 写入新任务初始 state 的统一入口（按仓库实际启动入口确定）

实施任务：

1. 把 `human_reorder()` 从无条件 `interrupt()` 改为状态分支。
2. `pass` 与 `pass_with_warnings` 自动通过。
3. 只有非结构性 `escalated` 才进入人工复核。
4. 结构性错误在关卡①再次检查并 fail-closed，保护旧 checkpoint。
5. QC 未启用时允许状态缺失，但必须能证明本次 QC 确实未启用。
6. QC 启用但状态缺失或出现未知值时，不允许静默通过。
7. 保留 `_build_interrupt_payload()` 的三个契约键及 assembly 报告、预览、QC 字段。
8. 保留 `_post_resume()` 的 `reject` 终止语义。
9. 自动通过时增加独立审计标记；不发送人工恢复通知，不伪造 `checkpoint1_resumed`。
10. 更新模块顶部调用栈 docstring 和 `human_reorder()` 函数 docstring，不能再写“每轮在剪映手动调整”。

**验收标准**：
- 正常及警告 QC 不调用 `interrupt()`。
- 降级 QC 调用 `interrupt()`，payload 契约不变。
- `reject` 只在降级状态下终止。
- 结构性错误不能从关卡①放行。
- QC 关闭与 QC 状态缺失有可区分的行为。

### 阶段 2：UI 元数据、配置注释和用户文案

| 文件 | 修改内容 |
|---|---|
| `web/human_gate_payload.py` | 关卡① title 改为“① QC 降级复核”；`action_hint` 改为阅读 QC 报告、确认接受或 reject；更新 `COMMON_RESUME_NOTE` 为按当前关卡指引操作，而不是默认所有关卡都要编辑剪映 |
| `monitoring/workflow_graph.py` | 更新节点显示名、描述和边标签；保留节点 ID、checkpoint 元数据 |
| `config.py` | 修正 `ASSEMBLY_QC_MAX_RETRY` 注释，区分结构性错误 fail-closed 与非结构性降级 |
| `nodes/node_06_human_reorder.py` | 更新模块注释、函数 docstring 和操作文案 |
| `nodes/_checkpoint.py` | 仅在确有过时语义时更新共享 docstring；不能误写成关卡②也只在 QC 降级时挂起 |
| `nodes/node_12_human_add_bgm.py` | 共享结构注释更新为关卡①是条件性挂起；不改变关卡②业务逻辑 |
| `agent_gate/cli.py` | checkpoint help 文案清除已删除的“③” |
| `scripts/resume_workflow.py` | 更新关卡①语义说明，不改变 `--checkpoint` 匹配顺序和恢复值解析 |
| `web/monitor.html` | 恢复输入 placeholder 从“① / ② / ③”改为“① / ②” |
| `README.md` | 更新目录树描述、人工关卡说明和恢复行为 |

**前端范围**：`web/console.html` 原则上不改渲染逻辑。验收时必须确认标题、instructions、action_hint、common note、QC 状态、报告和预览目录入口均正确显示。

### 阶段 3：单元测试

**主要文件**：`tests/unit/test_node_06_human_reorder.py`

新增或重写以下行为测试：

| 用例 | 输入状态/恢复值 | 预期 |
|---|---|---|
| U1 | `pass` | 不调用 interrupt，自动通过 |
| U2 | `pass_with_warnings` | 不调用 interrupt，自动通过，警告不丢失 |
| U3 | `escalated` 且非结构性 | 调用 interrupt，payload 三键及 QC 产物字段完整 |
| U4 | `escalated` + `true` | 通过并写入 `checkpoint1_resumed` |
| U5 | `escalated` + `reject` | 抛出 RuntimeError，不进入 node_07 |
| U6 | 非降级 + `reject` | 不触发拒绝语义；正常状态按自动通过处理 |
| U7 | `assembly_structural_error=True` | fail-closed，不允许人工 true 放行 |
| U8 | QC 关闭且状态缺失 | 自动通过，并记录 QC 关闭的自动通过原因 |
| U9 | QC 开启但状态缺失 | fail-closed 或进入明确的兼容策略，不得隐式通过 |
| U10 | 未知 QC 状态字符串 | fail-closed |
| U11 | payload 中缺少旧版本可选字段 | 兼容构造 payload，不改变三个契约键 |
| U12 | 自动通过 | 不发送人工恢复通知，不追加 `checkpoint1_resumed`，写入独立自动通过标记 |

`human_reorder()` 的行为测试应通过 monkeypatch `nodes.node_06_human_reorder.interrupt` 或真实 LangGraph 图执行验证。不能只测试 `_build_interrupt_payload()` 和 `_post_resume()` 两个 helper，就声称节点控制分支已经覆盖。

同步更新：
- `tests/unit/test_console_gates_api.py`：关卡① title/action hint 断言。
- `tests/unit/test_api_workflow_readonly.py:87`：节点显示名断言；当前显示名断言确实位于该文件第 87 行。
- `tests/unit/test_node_08_add_subtitles.py`：若测试名仍表达“人工调序必经”，只更新测试名和注释，不改变字幕行为。

### 阶段 4：集成测试与 PostgreSQL 恢复

#### 4.1 先按测试目的分类

不能对所有测试统一执行“在初始 state 注入 `assembly_qc_status="escalated"`”。完整图在 QC 开启时会执行 assembly 节点并重新写入 QC 状态，初始注入值可能被覆盖。

建议分类：

- **只测试 gate/resume 契约的用例**：明确关闭 assembly QC，直接构造关卡节点所需状态，或从 gate 前 checkpoint 启动；测试自身不应依赖真实 assembly 运行。
- **测试 assembly + gate 集成的用例**：保留 QC 开启，使用 typed preview issue（例如 `black_frame`）构造真实的 `escalated` 非结构性降级，并走完修复上限、报告生成、正式草稿生成和关卡① interrupt。
- **QC 关闭用例**：不注入 `escalated`，明确断言状态缺失时自动通过。

特别注意：`tests/integration/test_interrupt_resume.py` 的 `_seed_state()` 没有 `assembly_qc_status`；该测试构建完整图。`tests/conftest.py` 没有统一关闭 `ASSEMBLY_QC_GATE_ENABLED`，而 `config.py` 默认值为 true。因此，不能只在 seed state 增加 `escalated` 就假定它一定能保留到 node06；必须明确该测试使用的 QC 模式。

#### 4.2 修正 assembly QC 行为测试

当前 `tests/integration/test_assembly_qc_graph.py` 的 `test_assembly_chain_qc_blocking_then_repair_soft_degrade` 使用：

```python
interrupt_before=["node_06_human_reorder"]
```

`interrupt_before` 是 LangGraph 编译配置中的节点前置停点。它在 `node_06_human_reorder` 执行前就停止，因此只能证明图到达了该节点，不能证明节点本身会调用 `interrupt()`。

必须新增真实执行节点的断言：

1. 对非结构性 `escalated`：图的 `snapshot.interrupts` 中出现 checkpoint `①`。
2. 对 `pass` / `pass_with_warnings`：没有 checkpoint `①`，并能继续进入 node_07。
3. 对结构性错误：图走 `assembly_failed` / `END`，不会进入关卡①。
4. `interrupt_before` 测试可以保留，用于单独验证拓扑到达，不得把它当作人工挂起测试。

#### 4.3 修正 `test_unreachable_nodes_wiring.py`

当前 `run_auto()` 明确执行：

```python
monkeypatch.setattr(config, "ASSEMBLY_QC_GATE_ENABLED", False)
```

并且测试预置草稿只包含 video track，没有 audio/BGM track。因此，改造后 node06 自动通过，流程会继续执行后续节点；当到达 node12 时，因为 BGM 缺失，关卡②将成为真实挂起点。

需要调整：

1. `_SUFFIX` 黄金轨迹不能再以 `node_06_human_reorder` 作为最后一个节点；应录制 node06 自动通过后到达关卡②的真实轨迹。
2. `run_auto()` 的 docstring 从“直到关卡①挂起”改成准确的新行为描述。
3. 更新所有 `next_nodes == ("node_06_human_reorder",)` 断言；但每个用例应先确认它最终是否因 BGM 缺失而停在 node12，不应机械替换字符串。
4. `test_checkpoint_roundtrip_on_shared_checkpointer` 当前断言恢复后先执行 node06、node07。改造后初次运行应已经自动通过 node06 并停在 node12；恢复前必须对测试草稿执行 `_add_manual_bgm(...)`，然后再发 `Command(resume=True)`，否则 node12 会因 BGM 仍缺失而再次挂起。
5. 同步更新原来“恢复关卡①后到 node07”的测试 docstring、debug stream 事件断言和恢复落点断言。

参考代码：
- [`tests/integration/test_unreachable_nodes_wiring.py:450-486`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/tests/integration/test_unreachable_nodes_wiring.py#L450-L486)
- [`tests/integration/test_unreachable_nodes_wiring.py:727-743`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/tests/integration/test_unreachable_nodes_wiring.py#L727-L743)
- [`tests/integration/test_unreachable_nodes_wiring.py:1328-1352`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/tests/integration/test_unreachable_nodes_wiring.py#L1328-L1352)
- [`nodes/node_12_human_add_bgm.py:89-95`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/nodes/node_12_human_add_bgm.py#L89-L95)

#### 4.4 PostgreSQL 跨连接恢复

`tests/integration/test_postgres_resume.py` 必须保留真实 PostgreSQL 新连接恢复测试，但应把“QC 路径如何得到 escalated”明确化：

- 若本测试只验证 checkpointer 恢复能力，隔离 QC 通道，并通过可控 state/fixture 明确构造关卡①的降级状态。
- 若本测试也要验证 QC 到关卡①的完整链路，则必须使用真实 typed preview issue，不能仅在最初输入 state 注入一个可能被上游节点覆盖的字段。

另加一组“旧版本 checkpoint 恢复”测试：先用旧逻辑构造并持久化关卡① interrupt，再使用新逻辑、新建 PostgreSQL saver 连接和新图实例恢复，检查：

1. `snapshot.interrupts` 可读取；
2. resume 不会因为新代码跳过 `interrupt()` 而卡死；
3. 旧任务是否调用 `_post_resume()`、是否写入 `checkpoint1_resumed`，符合预先定义的兼容语义；
4. node05 不会被重复执行；
5. 恢复后能到达 node07 或按新状态策略自动进入后续节点。

### 阶段 5：Schema、元数据和文档测试

1. `monitoring/workflow_graph.py`：`SCHEMA_VERSION = 10` 改为 `11`；更新版本注释，说明本次是节点展示元数据调整，为使浏览器布局缓存中的旧显示信息失效而 bump。
2. `tests/integration/test_workflow_graph_schema.py:356`：将 `assert SCHEMA_VERSION == 10` 改为 `11`。
3. `tests/integration/test_workflow_graph_schema.py:393` 当前断言的是 `node_06_human_reorder` 的 checkpoint 为 `①`，不是显示名断言；不得按原文档误改。
4. 节点显示名断言实际位于 `tests/unit/test_api_workflow_readonly.py:87`，应更新为新显示名。
5. 图边仍然是普通边，不应把它断言成新增条件边；只验证 edge label 文案和拓扑未变。
6. 保留 `CHECKPOINT_MARKS == ("①", "②")`、`CHECKPOINT_NODES`、checkpoint/legacy_id 匹配契约。
7. 更新 `web/monitor.html` 中遗留的“③” placeholder 和 `agent_gate/cli.py` help 文案。

代码位置：
- [`monitoring/workflow_graph.py:61-87`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/monitoring/workflow_graph.py#L61-L87)
- [`web/static/workflow_monitor.mjs:71-82`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/web/static/workflow_monitor.mjs#L71-L82)
- [`tests/integration/test_workflow_graph_schema.py:342-357`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/tests/integration/test_workflow_graph_schema.py#L342-L357)
- [`tests/unit/test_api_workflow_readonly.py:85-88`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/tests/unit/test_api_workflow_readonly.py#L85-L88)

### 阶段 6：全量验证和运行验收

按顺序执行：

1. 单测：`pytest tests/unit`
2. 集成测试：`pytest tests/integration`
3. 专项测试：
   - `tests/integration/test_interrupt_resume.py`
   - `tests/integration/test_postgres_resume.py`
   - `tests/integration/test_assembly_qc_graph.py`
   - `tests/integration/test_unreachable_nodes_wiring.py`
   - `tests/integration/test_workflow_graph_schema.py`
4. SSE 契约：
   - `python -m tests.unit.monitoring.test_sse_frame_contract`
   - `node --test web/tests/sse_frame_contract.test.mjs`
5. 检查文案：扫描活代码中的“每轮必须手动调整分镜”“每次都挂起”“③”等旧语义。排除历史归档文档时，应在审查记录中列清排除规则。
6. 重启服务后检查 `/console` 和监控页；确认 schema 为 11，节点显示名和布局缓存正常。
7. 真机冒烟：
   - QC `pass`：不出现关卡①，自动继续；
   - QC `pass_with_warnings`：不出现关卡①，报告警告仍可查；
   - QC 非结构性 `escalated`：出现“① QC 降级复核”，报告和预览入口可打开；
   - 降级状态选择 `true`：继续 node07；
   - 降级状态选择 `reject`：运行失败终止，不进入 node07；
   - 结构性错误达到上限：上游 `assembly_failed` 终止，不出现关卡①；
   - QC 关闭：无状态时不出现关卡①；
   - 旧任务：历史挂起的 `console-011419d9ceec` 可以按兼容策略恢复。

**说明**：以上是建议执行命令和验收步骤，本次审查没有实际运行这些命令。PostgreSQL 集成测试只有在测试数据库可用时才具备有效验证意义，不能以跳过测试作为通过。

---

## 5. 原文档逐项问题清单

### 5.1 §1.2、§2.7：把结构性问题写成黑帧/静音/冻结帧

**问题**：与 `repair_strategies.py` 的 `STRUCTURAL_CODES` / `PREVIEW_CODES` 定义相反。

**修正**：黑帧、静音、冻结帧是预览级问题；引用、素材缺失、区间非法、片段缺失/多余、轨道错位才是当前结构性错误集合。

### 5.2 §1.2、§2.7：把所有 escalated 描述为软降级进入关卡①

**问题**：当前 `route_after_assembly_qc()` 对结构性错误使用 `assembly_failed` → `END`。

**修正**：明确只有能通过当前 QC 路由的非结构性降级才可能进入关卡①。

### 5.3 §3.A：非 escalated 一律 `return {}`

**问题**：这会把 QC 开启但状态丢失、非法枚举值等异常当作自动通过；它们不等于 QC 正常或 QC 关闭。

**修正**：区分 pass、warnings、escalated、QC disabled、missing、unknown 和 legacy checkpoint；未知状态不得静默通过。

### 5.4 §3.C2：仅在初始 state 注入 escalated

**问题**：对完整图测试，assembly 节点可能在后续覆盖初始注入值。`test_interrupt_resume.py` 的 seed state 没有 QC 状态，且构建完整图；`test_postgres_resume.py` 也从完整工作流图运行。

**修正**：明确这些测试是 QC-off 的 gate 恢复测试，还是 QC-on 的 assembly 集成测试。前者显式控制 config 和测试 state；后者从 assembly 节点构造 typed `escalated` 输出。

### 5.5 §3.C2：称 `test_assembly_qc_graph.py` 的软降级测试与挂起天然兼容

**问题**：相关用例用 `interrupt_before=["node_06_human_reorder"]` 在节点执行前截停，不能证明 `human_reorder()` 自己调用了 `interrupt()`。

**修正**：保留前置停点测试作为拓扑测试，并增加真实节点运行到 `snapshot.interrupts` 的集成测试。

### 5.6 §3.C3：`test_unreachable_nodes_wiring.py` 的恢复链路不完整

**问题**：当前 `run_auto()` 关闭 QC 且预置草稿只有 video track；关卡①自动通过后会抵达关卡②，BGM 缺失会再次挂起。原恢复测试只发 `Command(resume=True)`，没有在恢复前向草稿写入 BGM。

**修正**：在恢复命令前调用 `_add_manual_bgm()`，并更新 `next_nodes`、黄金轨迹和 debug stream 断言。

### 5.7 §3.C4：Schema 测试更新范围不完整且行号描述错误

**问题 A**：`tests/integration/test_workflow_graph_schema.py:356` 仍有硬编码 `SCHEMA_VERSION == 10`，原文档未列入修改。

**问题 B**：该文件第 393 行断言的是 checkpoint 编号，不是显示名。

**修正**：第 356 行更新到 11；显示名断言更新 `tests/unit/test_api_workflow_readonly.py:87`。

### 5.8 §3.A、§3.B：没有明确覆盖节点模块 docstring

**问题**：`nodes/node_06_human_reorder.py:1-12` 和 `:144-147` 仍明确描述“节点6无条件 interrupt、用户每轮手动调序”。仅修改 payload instructions 不会更新这些活代码说明。

**修正**：纳入阶段2文案清理。

### 5.9 §3.B7：配置注释更新口径不准确

**问题**：当前 `config.py:459-461` 旧注释本身就过时。新注释不能简单写成“escalated 时挂起①”，因为结构性 escalated 在上游已被终止。

**修正**：按结构性/非结构性两条路线准确描述。

### 5.10 §3.C3：`next_nodes` 断言清单需要逐项映射核对

**问题**：原文档把若干行号统称为 `next_nodes` 断言，但 `test_unreachable_nodes_wiring.py:1352` 实际断言的是 debug stream 的 `names[:2]`。不能统一替换成 node12。

**修正**：逐个检查断言类型，分别更新 `next_nodes`、`names`、黄金轨迹和恢复事件序列。

### 5.11 §5 风险 3：旧任务兼容性没有测试支撑

**问题**：原文档断言存量任务会“interrupt 立即返回已存 resume 值 → `_post_resume` 正常放行”。但新分支对 pass/None 状态可能根本不再执行 `interrupt()`，因此 `_post_resume()` 是否执行、历史 resume 值是否消费、状态日志是否写入，都不能靠文字推断。

**修正**：使用旧版本逻辑保存的 checkpoint，在新版本代码和新 PostgreSQL 连接下做真实恢复测试。

---

## 6. 额外需要关注的 QC 边界风险

当前 `node_validate_render_qc.py` 有若干早退分支会写入 `assembly_qc_status="escalated"`，但没有在同一个返回值中写入 `assembly_structural_error=True`，例如：

- 候选草稿映射抛出 `CandidateDraftError`；
- 缺少 `assembly_timeline_path` 的早退分支。

当前路由主要依赖 `assembly_structural_error` 区分结构性错误。如果这些早退被定义为不可放行的契约/产物错误，就必须明确标记并 fail-closed；如果它们被定义为可降级的预览级问题，则应在 QC 分类文档中写明原因。不能仅因为最终状态是 `escalated`，就默认它们适合交给人工看报告后放行。

相关代码：
- [`nodes/assembly/node_validate_render_qc.py:115-128`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/nodes/assembly/node_validate_render_qc.py#L115-L128)
- [`nodes/assembly/node_validate_render_qc.py:334-349`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/nodes/assembly/node_validate_render_qc.py#L334-L349)
- [`nodes/assembly/node_validate_render_qc.py:59-79`](https://github.com/wayyet/auto-video-editor/blob/6f44a41941a2d9124b4844c4e69feaf5f56b3aeb/nodes/assembly/node_validate_render_qc.py#L59-L79)

建议把这项作为阶段0的错误分类核对项，不在没有确认业务定义的情况下扩大修改范围。

---

## 7. 最终验收标准

只有同时满足以下条件，才能将本次改造判定为完成：

1. 正常 QC 和警告 QC 不产生关卡① interrupt。
2. QC 非结构性降级且达到最大重试次数时，关卡① interrupt 存在且 payload 完整。
3. 结构性错误始终 fail-closed，不能由人工 `true` 绕过。
4. `reject` 能终止降级任务，且不会影响正常状态任务。
5. QC 关闭与 QC 状态异常不会混为一谈。
6. 原 checkpoint 三键和关卡②契约保持不变。
7. PostgreSQL 新连接恢复和旧 checkpoint 兼容测试通过。
8. QC-off 黄金轨迹正确延伸到关卡②；恢复关卡②前模拟已添加 BGM。
9. 创建台文案、QC 报告/预览入口和监控图元数据准确。
10. Schema 测试、SSE 契约、全量 unit/integration 测试通过；任何基线失败需与改造新增失败区分。
11. 真机测试覆盖 QC pass、warnings、escalated、reject、结构性 fail-closed、QC-off 和历史任务恢复。
12. 执行记录另行记录实际改动文件、commit SHA、测试命令与输出、真机验收结果；不能把计划中的验证写成已验证。

---

## 8. 审查范围与限制

本报告基于 `main` 分支 commit `6f44a41941a2d9124b4844c4e69feaf5f56b3aeb` 的 GitHub 文件内容进行静态核验，覆盖目标设计文档、关卡①实现、assembly QC 路由、repair 分类、图接线、state、UI payload、监控 schema、相关 unit/integration/PostgreSQL 测试。

本次没有：

- 在本地检出仓库或执行 pytest；
- 连接项目 PostgreSQL 数据库；
- 启动或重启工作流服务；
- 操作剪映客户端或验证真实 checkpoint `console-011419d9ceec`；
- 修改 `main` 分支或写入任何仓库文件。

因此，本文结论属于**基于当前源码的静态审查**，不是运行时测试报告，也不代表原计划已经实施。

# 关卡①「分镜顺序调整」改为仅 QC 降级时挂起 —— 分析与实施计划

- **日期**:2026-11-20
- **状态**:**未实施**(本文档 = 全仓引用排查结论 + 用户决策 + 实施计划;执行记录另行落盘)
- **触发**:用户在创建台(任务 console-011419d9ceec,挂起于关卡①)提出:把「① 分镜顺序调整 node_06_human_reorder」这个每轮强制人工挂起的节点移除。

---

## 1. 背景与问题

### 1.1 现状行为

工作流节点 `node_06_human_reorder`(界面名「① 分镜顺序调整」)位于 assembly 草稿生成之后、node_07 时长护栏之前,目前**每轮运行都无条件挂起**(LangGraph `interrupt`),要求用户:

1. 在剪映客户端打开中文主草稿,人工检查并调整镜头顺序;
2. 保存草稿、关闭剪映;
3. 回创建台恢复面板点击「确认恢复」(放行值 `true`),工作流续接 node_07。

创建台恢复面板(截图所示)整块内容即此关卡挂起时的 UI:「挂起人工关卡 ×1(自动选中)→ ① 分镜顺序调整 node_06_human_reorder → 操作指引 → Assembly QC 状态 → 打开草稿/QC 报告/预览目录 → 恢复值(JSON,默认 true)→ 确认恢复」。

### 1.2 该节点的隐藏职责:QC 降级终审

除「每轮人工调序」外,该节点还承担一个**条件性安全阀**:

- assembly QC 通道(`ASSEMBLY_QC_GATE_ENABLED`,默认开)会对候选编辑计划做自动质检,状态三档:`pass` / `pass_with_warnings` / `escalated`(`nodes/assembly/node_validate_render_qc.py:14,49`);
- `escalated` = 存在结构性错误(如黑帧 blackdetect / 静音 silencedetect / 冻结 freezedetect 的 blocking issues),修复重试(`ASSEMBLY_QC_MAX_RETRY`,默认 2)耗尽后**软降级**:问题如实写进 `report.md`,照常进入关卡①(`config.py:459-461` 的设计决策,ADR-3);
- 此时用户在关卡①看 QC 报告后可决定:放行 `true` 继续,或输入恢复值 `reject` → 节点 `raise RuntimeError` 终止流程(`nodes/node_06_human_reorder.py:117-141`)。

### 1.3 用户决策(2026-11-20 已拍板)

三选一(直接终止 fail-closed / 无条件继续 / 仅降级时才挂起),用户选择:

> **仅降级时才挂起** —— 节点保留但平时零打扰:QC 正常或有警告时自动通过(不再要求调整分镜),只有 `escalated` 才挂起,保留看报告后「继续 / reject 终止」的人工决策(保留 ADR-3 的软降级设计意图)。附带收益:今天已挂①的存量任务仍可恢复。

因此本方案**不删节点文件、不改图拓扑**;改动 = 节点运行时行为 + 全部「每轮必须人工调序」文案 + 测试。

---

## 2. 现状梳理(排查结论)

### 2.1 节点本体与挂起机制

| 项 | 位置 | 内容 |
|---|---|---|
| 节点入口 | `nodes/node_06_human_reorder.py:144-147` | `human_reorder(state)`:无条件 `interrupt(_build_interrupt_payload(state))`,resume 后走 `_post_resume` |
| interrupt payload | 同文件 `:87-114` | 契约三键 `checkpoint="①"` / `legacy_id="checkpoint1_reorder"` / `step=6`;extra_payload 带 `draft_path / assembly_report_path / assembly_preview_path / assembly_qc_status / assembly_qc_warnings`;`instructions` =「请在剪映客户端手动调整分镜顺序…」(`:110-113`) |
| reject 语义 | 同文件 `:117-141` | 仅当 `assembly_qc_status == "escalated"` 且 resume 值为 `"reject"` 时 `raise RuntimeError`;其余值走 `nodes/_checkpoint.post_resume`(追加 status_log 标签 `checkpoint1_resumed` + 发通知) |
| 挂起机制 | `nodes/_checkpoint.py:39-108` + `graph.py:403-474` | `langgraph.types.interrupt` + 强制注入 checkpointer;`resume_all_pending` 按 payload 的 `checkpoint`(①/②)自动配对(`resume_utils.py:34-92`) |
| 放行值约定 | `scripts/resume_workflow.py:32-33`、`web/human_gate_payload.py:148-151` | 正常放行 = `Command(resume=True)` 布尔语义;默认 JSON `true` |

### 2.2 图接线(本方案不改)

- `graph.py:131` import;`:506` `add_node("node_06_human_reorder", human_reorder)`;
- 前驱 `generate_draft`:`:622` `g.add_edge("generate_draft", "node_06_human_reorder")` —— **QC 开/关两态均直连**(QC 开:`assembly_write_report → generate_draft`,`:618`;QC 关:`storyline_join` 条件路由直连 `generate_draft`,`:852-857`);
- 后继 `node_07_speed_fit`:`:624` 普通边(非条件边);
- 拓扑注释:`graph.py:15-16,52,592`。

### 2.3 关卡元数据与编号

- `web/human_gate_payload.py:24-41` `GATE_META`:`"①"` 条目 = title「① 分镜顺序调整」+ node + action_hint(「在剪映中打开中文主草稿,检查镜头顺序…恢复后工作流继续 node_07 时长护栏」);`"②"` = node_12_human_add_bgm;`_FALLBACK_META`(`:44-48`)兜底未知 checkpoint;
- 全图当前共 **2 道人工关卡**:`CHECKPOINT_MARKS = ("①", "②")`(`monitoring/workflow_graph.py:87`;历史:⓪ 随单流水线收敛删、③ 于 2026-11-15 删);
- P1-7 扩展:`build_gate_view`(`web/human_gate_payload.py:104-134`)对关卡①透出 assembly 报告/预览/QC 状态/产物目录五组字段,**唯一消费方就是①的挂起面板**(本方案保留)。

### 2.4 恢复链路(本方案不改)

- CLI:`scripts/resume_workflow.py`(`--checkpoint` 匹配顺序 checkpoint → legacy_id → id,`:224-242`);
- Web:`web/api_console.py:568-591` `POST /runs/{tid}/resume`(`:586` 现有挂③ → 409 退休拦截,**保留**);`web/gate_state.py:64-150` `resume_guard`(通用 draft_path 门禁,无①特判);`web/console_service.py:1435+` `resume_run`;
- Agent 入口:`agent_gate/resume_match.py`(按 checkpoint 配对,关卡无关)、`agent_gate/cli.py:138`(help 文案遗留「③」待顺手清)。

### 2.5 前端渲染链(卡片内容 API 下发,本方案零改 console.html)

```
nodes/node_06_human_reorder.py:110-113  interrupt payload instructions
  → scripts/resume_workflow.describe_interrupts(归一)
  → web/gate_state.py:49-56  fetch_gate_data
  → web/human_gate_payload.py  GATE_META["①"] + build_gate_view
  → web/api_console.py:421-447  GET /runs/{tid}/gates(gates + common_note)
  → web/console.html:1049-1125  renderGateCards 渲染
```

- 前端硬编码项(与本方案无关或仅微调):`console.html:247` placeholder「① / ②」、`:248` 默认 true、`:249` 按钮文案、`:1070`「挂起人工关卡 ×N」、`:1119-1121` 自动选中提示;
- `web/monitor.html:153` placeholder 仍是「① / ② / ③」—— ③删除时漏清的遗留,本方案顺手清为「① / ②」;
- 节点显示名真源:`monitoring/workflow_graph.py:147-152`(`_NODE_META`,name「人工调整镜头顺序①」);边标签 `:369`;`SCHEMA_VERSION = 10`(`:84`);经 `/api/workflow/*`(`web/api_workflow.py:203-216`)下发,前端零硬编码。

### 2.6 状态与 schema

- 读:`session_id / draft_path / assembly_report_path / assembly_preview_path / assembly_qc_status / assembly_candidate_qc_report_path / assembly_qc_report_path`(`state.py:344-353`);
- 写:仅追加 status_log 标签 `checkpoint1_resumed`(`state.py:146`);
- `SCHEMA_VERSION = 10`(`monitoring/workflow_graph.py:84`;本方案仅元数据文案变更,按 bump4 先例 → **bump 11** 使浏览器布局缓存失效);
- 影响链登记:`nodes/node_05_generate_draft.py:473-480` influence trace `consumed_by=["node_06_human_reorder"]`(节点保留,**不动**)。

### 2.7 assembly QC 状态语义(决策落点)

- `nodes/assembly/node_validate_render_qc.py`:`pass` / `pass_with_warnings` / `escalated`(`:14,49`);escalated 且重试未到上限 → `assembly_repair_loop`;到上限 → `assembly_write_report` 软降级(`:63-71,248,338-341`),随后 `generate_draft → 关卡①` 人工终审;
- 本方案后:该软降级链路不变,仅「人工终审」从**每轮必挂**变为**仅 escalated 挂**。

---

## 3. 实施计划

### A. 核心行为:`nodes/node_06_human_reorder.py` 重写主体

1. `human_reorder(state)`:
   - `state.get("assembly_qc_status") == "escalated"` → `interrupt(_build_interrupt_payload(state))` + `_post_resume`(reject → RuntimeError 语义保留);
   - **否则直接 `return {}`**(零副作用自动通过,不写状态、不通知)。
2. `_build_interrupt_payload`:契约三键与 assembly 五组字段**全保留**;仅 `instructions` 文案改为:
   > 「assembly QC 修复重试后仍未达标;请打开 QC 报告与预览确认,可接受则放行(true),需要终止则输入恢复值 reject」
3. `_send_notification` / `_qc_issue_messages` / `_post_resume`(status_log `checkpoint1_resumed`)保留。
4. 节点 id(`node_06_human_reorder`)与 `legacy_id("checkpoint1_reorder")` **不改名**(契约稳定 + 存量任务兼容;显示名另行更新,先例 = bump4 的 render_video 展示名改法)。

### B. 文案与元数据(只改显示文字,不改键名/契约)

| # | 文件 | 改动 |
|---|---|---|
| B1 | `web/human_gate_payload.py:24-32` | `GATE_META["①"]` title → 「① QC 降级复核」;action_hint → 「assembly QC 修复重试后仍未达标;打开 QC 报告确认问题,可接受则放行(true),需要终止则输入 reject」 |
| B2 | `web/human_gate_payload.py:146-151` | `COMMON_RESUME_NOTE` 微调(不再默认所有关卡都要剪映编辑) |
| B3 | `monitoring/workflow_graph.py:84` | `SCHEMA_VERSION` 10→11 + bump 注释(仅元数据文案,先例 bump4) |
| B4 | `monitoring/workflow_graph.py:147-152` | `_NODE_META` 显示名 → 「QC降级复核①」、description → 「interrupt:关卡①,仅 assembly QC 降级时挂起复核,其余自动通过」 |
| B5 | `monitoring/workflow_graph.py:369` | 边标签 → 「初始草稿就绪;仅 QC 降级进入关卡①」 |
| B6 | `web/monitor.html:153` | placeholder 「① / ② / ③」→「① / ②」(③遗留清理) |
| B7 | `config.py:451-461` | `ASSEMBLY_QC_GATE_ENABLED` 注释:escalated 挂起①,其余自动通过 |
| B8 | `graph.py:587-599,620-624` | 注释改写(不再写「生成后直连关卡①等待人工调序」) |
| B9 | `nodes/node_12_human_add_bgm.py:3`、`nodes/_checkpoint.py` docstring | 「与 node_06 同构 / 当前有效关卡 ①②」表述更新为「①仅 QC 降级时挂起」 |
| B10 | `agent_gate/cli.py:138` | help 关卡枚举清掉遗留「③」 |
| B11 | `scripts/resume_workflow.py:12,26,32,104` | ① 语义描述微调(仅 QC 降级时挂起) |
| B12 | `README.md:23,566` | 目录树一行描述 + 关卡表①行更新 |

前端 `console.html` 恢复卡片为 API 下发渲染,内容自动跟随,**零改动**;Assembly QC 状态行(`:1086-1089`)与「打开 QC 报告目录 / 打开预览目录」按钮(`:1096-1104`)在降级挂起时正好是所需入口,保留。

### C. 测试

**C1. Unit(重写/更新)**

| 文件 | 改动 |
|---|---|
| `tests/unit/test_node_06_human_reorder.py`(整文件,12 用例) | 重写:escalated → interrupt(契约三键 / assembly 字段 / reject 三态 / post_resume 幂等);pass / pass_with_warnings / 缺省 → **不 interrupt 直接通过** |
| `tests/unit/test_console_gates_api.py:39-97,161-182` | ① title 断言更新(「① 分镜顺序调整」→ 新 title) |
| `tests/unit/test_api_workflow_readonly.py:87` | 显示名断言更新(「人工调整镜头顺序①」→ 新名) |
| `tests/unit/test_node_08_add_subtitles.py:788` | `test_zh_subtitles_survive_human_reorder` 用例名/注释微调(逻辑不动) |
| 其余 unit(test_resume_workflow_script / test_resume_all_pending / test_dedup_behavior_changes / test_console_resume / test_nodes_checkpoint_helper) | 契约键(①/legacy_id)未变,预计零改动;跑全量确认 |

**C2. Integration —— 需让①继续挂起的用例:初始 state 注入 `assembly_qc_status="escalated"`**(QC 通道关闭时该键缺省 → 自动过①,故注入后仍保持原 ①→② 测试结构):

- `test_interrupt_resume.py`(4 用例)、`test_week5_resilience.py:105-164`、`test_postgres_resume.py:293-305`、`test_postgres_checkpointer.py:178`、`test_sequential_integration.py:128-141`、`test_week4_graph.py:109-139`、`test_reorder_topology.py:180,208`;
- `test_assembly_qc_graph.py`:「软降级仍到 node_06」的断言与挂起行为天然兼容(escalated 时确实挂起),逐个核对;
- `test_phase5_e2e.py:141-205` + `scripts/assembly/phase5_e2e.py:408-460`(`case_checkpoint1_notification`)、`scripts/assembly/phase5_combined.py:64-65,150-151`:checkpoint1 case 注入 escalated;gate_off 场景断言改为「QC 关 → 自动过①不挂起」;
- `test_assembly_checkpoint_artifacts.py` / `test_assembly_generate_draft_handoff.py` / `test_assembly_draft_influence.py`:`_invoke_to_before_node_06` 用的是 `interrupt_before=["node_06_human_reorder"]`(config 级停点,与节点行为无关),预计零/极小改动。

**C3. 黄金轨迹重录(最大测试冲击点)**

- `tests/integration/test_unreachable_nodes_wiring.py`:①不再挂起 → flags 全关黄金轨迹从 `[storyline_join, generate_draft, node_06_human_reorder]` **延伸到 `node_12_human_add_bgm` 挂起**;`_SUFFIX`(:113-117)重录、15 处 `next_nodes` 断言(:806,825,910,921,1053,1079,1138,1156,1178,1262,1276,1294,1341,1345,1352)与 `:806` 的 `next_nodes == ("node_12_human_add_bgm",)` 同步改;`run_auto` docstring(:459「直到关卡①挂起」)更新;
- 风险:node_07-11 在该测试桩环境从未跑过,若缺桩(TTS 等)按文件既有「假节点」模式补齐(参照 `test_week4_graph` fixture)。

**C4. Schema 测试**

- `test_workflow_graph_schema.py`:`:69-72` CHECKPOINT_NODES 保留、`:357` CHECKPOINT_MARKS 断言不变、`:214-229,316-336` 边断言不变(拓扑零变更)、`:340-350` 不变、`:393` 显示名元数据断言更新。

### D. 文档

- 新增执行记录 `docs/第八期开发/auto-video-editor_关卡①改为仅QC降级挂起_执行记录_20261120.md`(实施后落盘):决策、改动清单、验证结果、真机验收清单;
- 历史期次文档(docs/ 各期、docs/修正 关卡③执行设计等)**一律不动**。

### E. 明确不做(防扩散)

- 不删节点文件、不改 `graph.py` 任何边;
- 不动 checkpoint 契约(`checkpoint="①"` / `legacy_id="checkpoint1_reorder"` / `step=6`)与 `CHECKPOINT_MARKS`;
- 关卡②不重编号(③删除先例;避免破坏 `checkpoint2_add_bgm` 匹配);
- 不加 `api_console` 新 409(①仍存在,存量挂①任务仍可恢复;现有③守卫 `:586` 保留);
- 不动 `console.html` 渲染逻辑、SSE fixture(`sse_frames.txt:19` 的 node_id 未变,跑契约测试确认);
- 不动 `nodes/node_05_generate_draft.py:478` 的 influence trace `consumed_by`(节点仍在)。

---

## 4. 验证方案

1. **PG 前置**:`docker start edb-postgres16`(重启后容器不自启,集成测试依赖);
2. **单测全量**:`pytest tests/unit`,exit 0、无新增失败(基线 1 环境性 F);
3. **集成全量**:`pytest tests/integration`,失败与 main 基线 worktree 对照(存量 3F 口径),零新增;
4. **黄金轨迹**:先跑一次取 actual grouped 输出 → 回填 `_SUFFIX`/`GOLDEN_STEPS` → 复跑确认绿;`next_nodes == ("node_12_human_add_bgm",)`;
5. **SSE 契约**:`python -m tests.unit.monitoring.test_sse_frame_contract` + node 侧 `web/tests/sse_frame_contract.test.mjs`(node_id 未变,fixture 预计不动);
6. **扫尾 grep**(live 代码,排除 docs/.venv/归档快照):「分镜顺序调整|手动调整分镜」应清零;`node_06_human_reorder` 仅剩节点 id 本体与中性引用;
7. **重启服务**(restart-video-editor 技能,管理员 pwsh7);
8. **真机冒烟**(操作员):
   - 新建小任务 → 全程无①挂起,直达关卡②(添加 BGM);
   - 旧任务 console-011419d9ceec(挂①中)仍可确认恢复;
   - 可选:临时注入 `assembly_qc_status="escalated"` 验证降级挂起 + `reject` 终止。

---

## 5. 风险与备注

| # | 风险/备注 | 处置 |
|---|---|---|
| 1 | golden 轨迹延长后 `run_auto` 桩环境需覆盖 node_07-11 | 缺桩按文件内假节点模式补齐(test_week4_graph 先例) |
| 2 | SCHEMA_VERSION bump 仅使浏览器节点图布局缓存失效 | 前端无节点文案硬编码,无额外动作 |
| 3 | 存量挂①任务恢复时节点按新代码重放 | interrupt 立即返回已存 resume 值 → `_post_resume` 正常放行,兼容 |
| 4 | 关卡②编号保持「②」不重编 | ③删除先例;重编会破坏 legacy_id 匹配 |
| 5 | 「分镜顺序调整」设计出处追溯 | 主计划 = `docs/第二期开发/auto-video-editor_人工关卡与剪映草稿生成导入_设计与分阶段执行计划.md`;③删除套路 = `docs/修正/auto-video-editor_删除关卡③布局人工复核节点_执行设计_20261115.md` |
| 6 | 关卡①自动化通过后,正常轮次用户不再经手草稿 | 关卡②(BGM)仍有人工触点;QC 警告/降级仍留痕于 report.md 与产物 |

---

## 附:全仓引用排查清单(2026-11-20 双代理核查)

**活代码(本方案涉及)**:`nodes/node_06_human_reorder.py`(重写)、`nodes/_checkpoint.py`(docstring)、`nodes/node_05_generate_draft.py:478`(不动)、`nodes/node_12_human_add_bgm.py:3`、`graph.py`(注释)、`config.py:451-461`、`web/human_gate_payload.py`、`web/api_console.py:421-447,568-591`(不动)、`web/gate_state.py`(不动)、`web/console_service.py:1463`、`monitoring/workflow_graph.py`、`monitoring/runs_table.py:13-18`(注释)、`agent_gate/cli.py:138`、`agent_gate/resume_match.py`(不动)、`agent_gate/fixtures/toy_graph.py:103-104`(toy 桩,不动)、`scripts/resume_workflow.py`、`scripts/assembly/phase5_e2e.py`、`scripts/assembly/phase5_combined.py`、`web/console.html`(零改动)、`web/monitor.html:153`、`web/static/workflow_console.mjs`(不动)、`web/tests/fixtures/sse_frames.txt:19`(预计不动)、`README.md:23,566`。

**测试(§3.C 全列)**:unit 5 文件必改 + integration 14 文件核对/更新 + `web/tests/sse_frame_contract.test.mjs`(Node 侧确认)。

**仅历史记录(不动)**:docs/ 各期计划/执行记录/验收记录、`docs/refactor-baseline-20261103/schema-snapshots*`、`docs/remove-chat-loop-20261104/schema-snapshots`(归档工件)、`docs/分析/*`。

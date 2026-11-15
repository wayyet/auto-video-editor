# auto-video-editor 删除关卡③"布局人工复核"节点 执行设计

> 日期:2026-11-15
> 状态:**已按《核验修订版》执行**(修订版见 docs/第六期开发/auto-video-editor_删除关卡③人工复核节点_设计执行计划核验修订版_20261115.md;执行记录见 docs/第六期开发/执行记录_删除关卡③节点_20261115.md;基线 HEAD = 4f48bd3)
> 决策记录:清理深度已由操作员拍板 = **彻底清除**(连同 16a 中只为关卡③服务的布局检测 qc 链一起删)
> 来源:操作员在节点图(node-map)中圈红"布局人工复核③"节点,要求移除并删除该节点,且不影响上下游节点的边

---

## 1. 背景与目标

### 1.1 要删什么

完整 LangGraph 工作流(mode=auto,schema v8)中的节点 **`node_checkpoint3_layout_review`**(展示名"布局人工复核③"):

- kind: task,group: subtitles_covers,关卡编号 ③,modes: auto
- 行为:一个 `interrupt()` 人工关卡(术语解释:`interrupt` 是 LangGraph 的挂起机制——图运行到该节点时暂停,把控制权交给人,人工在创建台点"恢复"后图从挂起点继续,前提是有 checkpointer 持久化状态)
- 触发条件:仅当英文字幕布局检测命中 **error 级**问题时才走到它(2026-11-14 提交 23200f5 收紧的语义),未命中走旁路直达下游

### 1.2 上下游关系(删除前)

```
node_10b_inject_text_fx_en(注入英文花字)
   │  条件边 route_after_translate:layout_issues_detected 为真?
   ├─→ node_checkpoint3_layout_review(③,interrupt)──→ node_17_inject_english_tts
   └─→ node_17_inject_english_tts(注入英文 TTS,旁路)
```

接线真源在 `graph.py:636-644`:

```python
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

### 1.3 删除后(目标拓扑)

条件边整体拆除,换成一条普通直连边,**上下游零影响**:

```python
g.add_edge("node_10b_inject_text_fx_en", "node_17_inject_english_tts")
```

### 1.4 为什么选"彻底清除"而不是"最小删除"

探索发现:上游节点 `node_16a_translate_and_check`(翻译与布局检测)里有一整块**布局检测 qc 链**——`subtitle_build → subtitle_render(mode=burn,烧录预览 MP4)→ subtitle_qc`,**每个源视频跑一遍**,产出 issues / 预览视频 / 证据帧。这三种产出的唯一消费方就是关卡③:

- `layout_issues` / `layout_issues_detected`:被 `route_after_translate`(路由判据)和 c3 节点读取
- `preview_video_path` / `subtitle_qc_evidence_frames`:被关卡恢复面板 `web/human_gate_payload.py` 读取展示

③删掉后,这块 qc 链每轮照样白跑(烧 MP4 无人看),4 个状态键变死键(只写不读)。彻底清除后 16a 变纯"翻译+写字幕"节点,每轮省一次 QC 算力。

---

## 2. 探索结论(关键事实)

1. **前端零硬编码**。主图节点图数据来自 `GET /api/workflow/graph`(`web/api_workflow.py:187-199`),运行时调 `monitoring.workflow_graph.build_graph_schema` 从**编译后的 builder** 实时取节点/边(`monitoring/workflow_graph.py:411-426`);中文名/分组/关卡编号由 `_NODE_META` 注册表下发。前端 `node_map.html` / `workflow_layout.mjs` / `console.html` / `monitor.html` 均无该节点 id 的硬编码。**改完 graph.py + bump SCHEMA_VERSION 后前端自动少一个点**;布局缓存 key 含 schemaVersion(`web/node_map/node_map.html:917-921`),bump 即自动失效 TB/LR 两份缓存。
2. **`web/node_map/workflow.json` 不用动**:它是 storyline 子流程静态快照(storyline_subflow,17 节点),grep 零命中 checkpoint3。
3. **wiring 权威测试不受冲击**:`tests/integration/test_unreachable_nodes_wiring.py` 的 GOLDEN_STEPS 与 NEIGHBORS 映射**完全不含** checkpoint3(③是条件旁路,不在 flags 全关默认轨迹里),删节点后该文件无需改动。
4. **SCHEMA_VERSION 惯例**(`monitoring/workflow_graph.py:62-78`):拓扑变更(节点/边增删)必 bump;当前值 8 → 本次 bump 到 **9**。
5. **删除节点+补直连的仓库先例**:render_video 节点删除(边治理阶段5)、bridge_snapshot2 桥接节点删除(依赖约简阶段4,`graph.py:282-286` 留有历史注释块)——本次照此风格留历史注释。
6. **16a 的检测块是纯观测性产出**:只产 issues/预览/证据帧给③,**不自动修正草稿**;删除不影响字幕写入路径。

---

## 3. 改动清单

### A. 核心拓扑

#### A1. `graph.py`

| 位置 | 改动 |
|---|---|
| `:135` | 删 `from nodes.node_checkpoint3_layout_review import node_checkpoint3_layout_review` |
| `:223-237` | 删 `route_after_translate` 函数,原地留历史注释块(仿 `:282-286` bridge_snapshot2 先例:记录 2026-11-15 关卡③整体删除、条件边改直连) |
| `:521` | 删 `g.add_node("node_checkpoint3_layout_review", node_checkpoint3_layout_review)` |
| `:636-644` | 条件边块 + `:644` 的 `g.add_edge("node_checkpoint3_layout_review", "node_17_inject_english_tts")`,替换为 `g.add_edge("node_10b_inject_text_fx_en", "node_17_inject_english_tts")`;K3=B 注释(`:634`)同步改写 |
| docstring `:29-32` | ASCII 图:③分支两行删除,改为 10b → node_17 直连 |
| docstring `:55` | "保留:关卡①②③" → "保留:关卡①②" |
| docstring `:62-65` | K3=B 历史说明标注"关卡③已于 2026-11-15 删除" |
| docstring `:88-92` | Week 5 段落改写:记录 16a + c3 拆分的历史,以及本次合并回"纯翻译节点" |

#### A2. 删除节点文件

```
nodes/node_checkpoint3_layout_review.py   ← 整文件删除
```

### B. 16a 检测链彻底清除

#### B1. `nodes/node_16a_translate_and_check.py`

- 删 `_check_layout_via_subtitle_qc`(`:80-183`)、`_segments_en_to_transcript_records`(`:54-77`)、常量 `_QC_CHAIN_WORK_ROOT`(`:51`)
- 删 imports:`subtitle_build / subtitle_qc / subtitle_render`(`:46`)、`RunContext`(`:35`)、`resolve_video_input_paths`(`:47`)、`validate_layout`(`:42`)
- 主体删 layout 校验段(`:244-299` 的 issues/preview/evidence 逻辑),返回值只剩:

```python
out: dict[str, Any] = {
    "subtitle_segments_en": list(segments_en),
    "subtitle_srt_path": srt_path,
    "status_log": ["node_16a_translate_done"],
}
```

- 无源字幕 skip 路径(`:207-213`)与 draft 缺失报错路径(`:215-220`)**不变**
- docstring 重写(去掉 qc 链/触发语义/broadcast_en 校准描述)
- **节点 id `node_16a_translate_and_check` 不变**(最小化爆炸半径,只有展示名更新)

#### B2. `nodes/node_16_translate_subtitles.py` 死代码清除

- 删 `_build_interrupt_payload`(`:248`,唯一调用方是被删的 c3)
- 删 `validate_layout`(`:72`,唯一生产调用方是 16a;qc 链的降级兜底也随之消失)
- **保留**:`_read_segments_from_draft` / `_write_marker` / `_save_marker` / `_marker_exists` / `write_srt_from_segments`(16a 与测试仍在用)
- docstring `:8-12`、`:20-23` 同步

#### B3. `state.py` 死键清除

| 键 | 位置 | 说明 |
|---|---|---|
| `layout_issues` | `:242` | 16a 写,唯一读者是被删的路由+c3 |
| `layout_issues_detected` | `:243-244` | 同上 |
| `preview_video_path` | `:250` | 唯一写入方 16a:301,唯一读者关卡恢复面板 |
| `subtitle_qc_evidence_frames` | `:252` | 唯一写入方 16a:303,唯一读者关卡恢复面板 |

另:`:186` checkpoint3_triggered 审计位历史注释顺带清理。

> 旧 PG checkpoint 里残留这些键**不影响加载**(LangGraph 状态是 TypedDict,容忍多余键);`test_state_field_compatability.py` 就是干这个的,见 D 组适配。

### C. 展示层 / 关卡面板

#### C1. `monitoring/workflow_graph.py`

- `SCHEMA_VERSION` 8→**9**(`:79`),`:62-78` bump 历史注释追加一行(拓扑变更:关卡③节点整体删除)
- `CHECKPOINT_MARKS` `("①", "②", "③")` → `("①", "②")`(`:81-82`,注释同步)
- `_NODE_META`:删 `node_checkpoint3_layout_review` 条目(`:198-203`);`node_16a_translate_and_check` 条目(`:193-197`)展示名改"翻译英文字幕"、description 去掉"命中问题才触发关卡③"
- `_EDGE_LABELS`:删 `:370`(10b→③"布局检测命中")与 `:371`(10b→17"布局无问题"——改直连后分支语义不再成立,参照中文线 08→10 无标签惯例)

#### C2. `monitoring/runs_table.py`

- `:17` 注释中关卡节点枚举 `node_06 / node_12 / node_checkpoint3_layout_review` 去掉 c3

#### C3. `web/human_gate_payload.py`

- `GATE_META` 删 `"③"` 条目(`:41-48`);模块 docstring `:8-9` ①/②/③→①/②
- `build_gate_view` 删 `preview_video_path`(`:112`)与 `evidence_frames`(`:113-116`)两字段(state 键已删,永远为空);`:90`、`:106-107` 的 ③ 注释微调
- **`_FALLBACK_META` 保留**:未知 checkpoint 兜底——存量旧线程若挂③,恢复面板退化为通用"人工关卡"文案(而不是报错)

#### C4. `web/console.html`

- `:1062` 删 `pathLine("预览视频", g.preview_video_path)` 显示行
- `:1046` 删 `g.checkpoint === "③" ? "en" : "zh"` 死分支
- `:241` 恢复输入框 placeholder `"① / ② / ③"` → `"① / ②"`

#### C5. 其余 ①②③ 枚举文案

`web/console_service.py:717`、`web/api_console.py:89,477`、`web/monitor.html:153` 等处逐处核对更新(实施时以 `rg "①②③|③"` 定位,只改枚举型文案,不改通用兜底逻辑)。

### D. 测试适配

| # | 文件 | 改动 |
|---|---|---|
| D1 | `tests/unit/test_node_checkpoint3_layout_review.py` | **整文件删除**(5 例) |
| D2 | `tests/unit/test_node_16_translate_subtitles.py` | 删 `test_build_interrupt_payload`(`:124`)+ 3 个 `validate_layout` 用例(`:58-91`)+ import 清理(`:17-21`) |
| D3 | `tests/unit/test_node_16a_translate_and_check.py` | 删检测类用例:layout 异常写 flag(`:142-159`)、warning 不触发(`:169-170`、`:217-219`)、error 触发(`:227-268`)、三段降级 validate_layout(`:272-460`)、qc 链契约(`:463-514`);保留翻译/marker/SRT/无源字幕 skip 等仍成立用例;import 与 patch 清理 |
| D4 | `tests/integration/test_workflow_graph_schema.py` | `REMOVED_NODES` **加入** `"node_checkpoint3_layout_review"`(`:53-64`,仓库防回归惯例);`CHECKPOINT_NODES` 删 ③ 行(`:70`);删 `:316` 条件边断言、`:338` checkpoint 断言 |
| D5 | `tests/integration/test_reorder_topology.py` | 删 `test_checkpoint3_path_no_parallel_conflict_and_human_edit_survives`(`:258+`);删 `:108` 的 validate_layout patch;`:215`、`:236` 等注释更新。⚠️ 此文件在基线有存量失败,改完须 worktree 对照(见 §5.3) |
| D6 | `tests/integration/test_week5_resilience.py` | 删 `test_checkpoint3_interrupt_and_resume_path`(`:235-310`);"三个关卡" docstring(`:106,113`)微调;`:166` `checkpoint3_resumed==0` 旁路断言**保留**(删节点后仍恒成立) |
| D7 | `tests/integration/test_week4_graph.py` | `test_week4_checkpoint3_normal_path`(`:110`)改名/注释微调(实施时确认其内部是否 patch validate_layout) |
| D8 | `tests/integration/test_state_field_compatability.py` | 删 `:162-163`(`layout_issues_detected` 应有值断言);`:191-192`、`:247` 键列表删对应键 |
| D9 | `tests/unit/test_console_gates_api.py` | 删 `test_gate3_issues_and_preview_from_state`(`:66-87`);其余 ①/②/兜底/API 用例不动 |

**不受影响的测试**(已核实零命中或仅旁路引用):`test_unreachable_nodes_wiring.py`(GOLDEN_STEPS/NEIGHBORS 无 c3)、`test_subtitle_texts_native.py`(只用 `_read_segments_from_draft`)、`test_interrupt_resume.py`、`test_postgres_resume.py`、`test_console_e2e.py`、`test_workflow_stream_events.py`、`test_graph_compiles.py`、`test_auto_mode_end_to_end.py`、`test_storyline_edge_governance_topology.py`、web/tests 4 个 .mjs 前端测试。

### E. 脚本与文档

#### E1. `scripts/video_edit_phase56_e2e.py`

- `case_checkpoint3_and_tts`(`:379-422`)改名 `case_english_tts`:删 qc 链段(`:381` import `_check_layout_via_subtitle_qc`、`:394-406` issues/preview/evidence 检查),保留 node_17 TTS 真调/优雅回退段(`:408-420`);`CASES` 注册表 `:434` 同步

#### E2. `README.md`

- `:537` ASCII 树删 ③ 分支行;`:546` node_16a 描述去检测;`:549` c3 段删;`:550` route_after_translate 段删;`:570` 关卡表删 ③ 行;`:581-582` state 键文档删 4 键

#### E3. 明确不动

- `docs/` 归档 schema 快照(`refactor-baseline-20261103/schema-snapshots*`、`remove-chat-loop-20261104/schema-snapshots`)与历史设计文档——工件性质,不参与断言
- `web/node_map/workflow.json`——storyline 子图快照,不含 c3
- `video_edit_capabilities/` 的 `subtitle_build / subtitle_render / subtitle_qc` 能力层——另有消费方(subtitle_chain e2e 等),只删 16a 对它们的编排
- `resume_utils.py` 的 `resume_all_pending`(①②③通用匹配机制,删③后自然无匹配)
- `jy_common/subtitle_style.py` 的 `broadcast_en` 预设本体(能力层;昨日 23200f5 校准的**消费方**随检测链删除,预设保留)

---

## 4. 实施顺序

1. A 组:graph.py 改线 + 删节点文件(此时图能编译,但测试会红——预期)
2. B 组:16a / 16 / state 死代码与死键清除
3. C 组:展示层与关卡面板
4. D 组:测试适配
5. E 组:脚本与 README
6. §5 全量验证

---

## 5. 验证

### 5.1 前置

```bash
docker start edb-postgres16
```

> PG 是 Docker 容器且不自启;不先启动会导致集成测试出现 8 例 fixture 失败 + 6 例转 skip,勿当回归。

### 5.2 单测

```bash
python -m pytest tests/unit -q
```

> 本仓 addopts=-q,汇总行可能被吞:以 **exit code + 结尾统计**为准。
> 目标:零失败;总数较基线 2046 下降约 15 例(D1 整删 5 例 + D2 删 4 例 + D3 删约 10 例 + D9 删 1 例)。

### 5.3 集成测试(存量对照纪律)

```bash
python -m pytest tests/integration -q
```

基线存量失败 = **3 例**(sequential / reorder / auto_e2e 的"字幕应被注入"),且 `test_reorder_topology.py` 本身在基线就有存量失败。**判定新失败用 worktree 对照法**:同一测试在基线 worktree(`.worktrees/baseline-6679e54`)逐字复跑,只在基线也失败才算存量。目标:零**新增**失败。

### 5.4 schema 快照冒烟

```bash
python scripts/capture_graph_schema_snapshots.py %TEMP%\c3-removal-check
```

检查输出 JSON:

- 不含 `node_checkpoint3_layout_review`
- 存在普通边 `node_10b_inject_text_fx_en → node_17_inject_english_tts`
- `schema_version` = 9

### 5.5 扫尾 grep

```bash
rg "node_checkpoint3_layout_review|layout_issues|validate_layout" --glob '!.worktrees/**' --glob '!docs/**' --glob '!**/__pycache__/**'
```

预期仅剩:`REMOVED_NODES` 条目、历史注释块。`rg "checkpoint3"` 额外允许命中 README 历史段落与 `test_week4_graph.py` 改名后的注释。

### 5.6 前端离线冒烟(可选)

静态服务器 + §5.4 快照伪装 `/api/workflow/graph`,打开 node-map 确认:"布局人工复核③"卡片消失、10b 直连 17、其余节点布局不乱(TB 缓存已因 schemaVersion=9 失效重建)。

### 5.7 真机验收(操作员执行)

1. 管理员 pwsh7 重启服务(一键脚本);
2. 新建 auto 任务跑全流程;
3. 核对:节点图无③、`node_10b_inject_text_fx_en` 直连 `node_17_inject_english_tts`、流程正常走到关卡②(BGM)挂起;
4. 恢复面板只出现 ①/② 两类关卡;
5. 观察一轮日志确认 16a 不再产 `layout_issues` / 不再烧预览 MP4。

---

## 6. 风险与注意事项

1. **存量挂起任务**:若有任务恰好挂起在③,改造后**无法恢复**(图里已无该节点)。处理:取消/删除该任务后新建——与仓库既有惯例一致(关卡①发布时同样"验证须新建任务")。
2. **昨日校准工作随检测链删除**:23200f5(2026-11-14)的 broadcast_en 校准其唯一消费方就是本检测链;`subtitle_style.py` 预设本体保留,不损失能力层资产。
3. **中文路径编码坑**:仓库路径含中文,生成的 ASCII/legacy 编码文件内**不嵌绝对路径**。
4. **pytest 汇总行被吞**:判定以 exit code + 进度标记为准。
5. **并行会话纪律**:若同期有其他会话在跑,禁用 `git add -A`。
6. **交付方式**:完成并全绿后停下汇报,不主动 commit/push;是否入库由操作员决定(走 sync-github 技能)。

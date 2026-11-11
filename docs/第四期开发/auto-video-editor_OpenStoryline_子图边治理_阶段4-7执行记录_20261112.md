# OpenStoryline 子图边治理:阶段 4-7 执行记录

> **执行日期:** 2026-11-12
> **依据文档:**《auto-video-editor_OpenStoryline_子图边有效性审计与精简设计执行计划_20261112.md》(同目录)
> **执行范围:** 仅阶段 4-7(模板前置差量收尾 / render_video 节点整体删除 / AI 转场物化差量收尾 / 回归与运行态观测);阶段 0-3 由并行会话交付(提交 `6f9c232` / `780dc28` / `608be51` / `b028d2e`),本批未触碰其范围。
> **用户指令:** #4 明确要求**彻底删除 storyline_render_video 节点**(不需要了)——该指令覆盖 noop 治理阶段3 方案A 的"保留为故事线预览"决策,为阶段5 的执行口径。

---

## 1. 基线

| 项 | 值 |
|---|---|
| 开工基线 HEAD | `b028d2e`(边治理·阶段3:GAT 屏障改为 [GC, RTR]) |
| 分支 | `main`,工作区干净(并行会话已收尾) |
| 单测基线 | `tests/unit` 全绿(EXIT=0;≈1850 passed / 3 skipped,与阶段0-3 记录一致) |
| 新基线 | 阶段5 删 render 专测 7 例、新增 12 例(拓扑 7 + 观测 5)→ **1855 passed / 3 skipped** |

**对依据文档过时前提的校准**(本批开工前核实):

1. **阶段4 主体已交付**:noop 治理阶段1a/1b(`853423a`/`9e2b3ab`)已把模板推荐做成真实现并前置 —— `group_clips → STR →(条件路由)→ search_web_topic | generate_script`,独立 artifact(`storyline_script_template_artifact` → `script_template_plan.json`)、BM25 检索、GS prompt 注入(zh/en)均已存在。本批只做防回归断言。
2. **阶段6 主体已交付**:noop 治理阶段2(`0a0bae98`/`c667182`)已删除 Stub 客户端、接入 MiniMax Hailuo-02 真实 Provider(ffmpeg 首尾帧 + ffprobe 产物复检 + pair 级缓存),PTAT 已把真实转场 MP4 物化为 SourceMedia+Clip 进 Canonical Timeline,mapper 零改动消费。本批只做残留清理。
3. **阶段5 前提变化**:依据文档写于 `2620989`(render 还是 JSON stub,"无产物、无消费者");noop 治理阶段3 方案A 把它升级为真实 FFmpeg 预览后,本批按用户指令整体删除(而非文档原文的"仅移出生产图保留函数")。删除理由:子图交付物是 Canonical Timeline,预览旁支无生产增益、状态卡语义易混淆;最终 MP4 统一由 `node_18 export_final_video` 负责。

---

## 2. 阶段4:模板前置能力(差量收尾)—— `8c4add8`

**核实结论(零生产代码改动):**

- 图接线:`group_clips → STR` 主链前置(graph.py),STR 出口条件路由二选一,检索节点出边 `→ generate_script`(唯一业务出边,非死胡同);
- 数据契约:独立 artifact 字段(`storyline_script_template_artifact`,state.py),GS 读取并注入 prompt(`node_generate_script.py` 的 `script_template_content`,`template_ref` 随 script.json 落盘留痕);
- 降级三态已有断言覆盖(`test_node_script_template_recommendation.py`):catalog 加载异常 → `unavailable`、空模板库 → `unavailable`、空 query → `no_match`,均不抛、不冒充 done。

**本批新增:** `tests/unit/test_storyline_edge_governance_topology.py`(单元级,读 builder 边真源,不执行图):

- 不存在反向边 `GS → STR`(§5.8 判定的无效方向),GS 的屏障/条件路由也不指向 STR;
- STR 入边恰为 group_clips、无普通出边、条件出口 = {search_web_topic, generate_script};
- search_web_topic 挂在 STR 出口(条件入边=STR、普通出边=GS),不作为任何屏障源(D10)。

---

## 3. 阶段5:彻底删除 storyline_render_video —— `7786713`

**删除清单(全部落实):**

| 域 | 内容 |
|---|---|
| 节点源码 | `nodes/storyline/node_render_video.py` 整文件 |
| 图接线 | graph.py:import、`add_node`、QA 条件出口映射(render→**join**)、`render_video → join` 边 |
| 路由 | `qa_gate.route_after_storyline_qa` 两处返回 `storyline_render_video` → `storyline_join`(含 docstring) |
| 配置 | `config.STORYLINE_ENABLE_RENDER_SMOKE_TEST` 整块删除 |
| 状态 | `storyline_render_preview_path` / `storyline_render_preview_report` 两字段删除(`_last_wins`,旧 checkpoint 残留键无读取方,无需迁移) |
| 监控 | `_NODE_META` render 行删除;QA 边标签改 `QA 通过/失败/空 plan(阶段5 直连)`;SCHEMA_VERSION **4→5**(拓扑变更,布局缓存失效) |
| 前端 | `console.html` 交付卡"故事线预览"行 + `preview.succeeded/failed/skipped` 三分支删除(其余行保留;强刷即生效) |

**顺带修正同文件的过时元数据**(阶段2/3 遗留):PTP 描述"4 源屏障"→ 6 源;GAT 描述"与 PTP 共用 4 源屏障"→ `[group_clips, recommend_transition]`;QA 描述"3 源屏障…通过走渲染"→ 最终时间线单源…通过进 join;RTR/TXT→QA 两条已不存在的边标签删除。

**测试同步:** 删 `test_node_render_video_preview.py`(7 例);`test_wiring_routers.py` / `test_draft_path_degradation.py` 路由落点改 join;`test_unreachable_nodes_wiring.py` GOLDEN 轨迹去 render 超步、NEIGHBORS QA 出边 {GC, join}、steps 断言改 `steps_of("storyline_join") == [qa[-1]+1]`;`test_auto_mode_end_to_end.py` 删两个 render 用例(其 QA 直连 join 行为由 wiring 断言+单元拓扑锁定)、初始 state 去 preview 字段、超限路由断言改 join;`test_artifact_producer_consumer_manifest.py` 产出消费清单去 render;`test_storyline_capabilities_c_classes.py` 导入清单去 render。

**未触碰(同名不同物):** `assembly_capabilities/render_preview.py` 及 assembly QC 链、`state.assembly_preview_path`、`web/human_gate_payload.py` 的 `preview_video_path`(关卡③载荷)、`storyline/firered_adapter.py` 的 FireRed ToolSpec `render_video`。

**新行为:** QA 非重试出口(通过 / 契约失败 / 空 plan / 重试耗尽)直连 `storyline_join`;子图完成 = Timeline 可交接,`jianying_draft_ready` / `video_export_ready` 语义分别由外层 `generate_draft` / `node_18` 承载。

---

## 4. 阶段6:AI 转场物化(差量收尾)—— `fde439b`

1. 删除 state.py 死字段 `storyline_timeline_plan_ai`(PTAT 已改为覆盖 `storyline_timeline_plan`,全仓零读写者);
2. `.env.example` 改写过时 stub 注释("当前只有 stub 客户端;不改 clips"已失真)为 MiniMax 真实现+PTAT 物化描述,并补 `STORYLINE_AI_TRANSITION_PROVIDER` / `MINIMAX_API_KEY` / `STORYLINE_AI_TRANSITION_APPROVED` 三变量示例(双门禁语义);
3. state TypedDict 补声明 `ai_transition_user_request`(GAT 原以 `.get` 容错读,补声明无行为变化)。

**既有验收回归(全部通过):** 默认双门禁(PROVIDER 空 + APPROVED false)0 次 Provider 外呼、不产占位 JSON;PTAT 物化 SourceMedia+Clip 且两侧相邻;mapper 零改动消费(`test_mapper_ai_transition_video_segments.py`);pair 级缓存与产物复核(`test_ai_transition_cache.py` 等 7 个 AI 转场测试文件全绿)。

---

## 5. 阶段7:回归与运行态观测 —— 本批提交

**1) 结构测试扩容**(`test_storyline_edge_governance_topology.py`,3→7 例):

- render 节点"完全不存在"守卫:注册表/普通边/屏障/条件路由/开关/状态字段六层断言;
- QA 单源 = 最终时间线节点(AI 开=PTAT,关=PTP),QA 无屏障入边、无普通出边,条件出口恰 {GC, join},join 无普通入边;
- 屏障形状锁定(仅 storyline 段):`[GS,GV]→SB`、`[GC,SB]→RTR`、六源→PTP、`[GC,RTR]→GAT`、`[PTP,GAT]→PTAT`;WT 与 STR 不作为屏障源。

**2) 顺序测试补强**(`test_unreachable_nodes_wiring.py` 重试回路用例):补 `storyline_script_template_recommendation == 4`、`storyline_generate_script == 4` —— 主链前置节点在 qa 重试回路内每轮恰 1 次(与 GOLDEN 单轮轨迹互补)。

**3) 路由决策观测**(`qa_gate.py` + `tests/unit/test_qa_route_observability.py` 5 例):

- 新增 `_emit_route_event`:QA 每次运行经 LangGraph custom stream 上报 `qa_route` 事件(`signal` + `actual_ms`/`target_ms`/`retry_count`/`reason`),经 `workflow_runner.handle_custom` 通用通道进创建台事件详情,**后端/前端零改动**;
- 观测异常(RuntimeError/任何异常)一律吞掉,绝不影响主流程;
- 覆盖四类返回点:done_empty / retry(带轮次与时长)/ done(带时长)/ failed(契约违例,JSON 语法损坏走 done_empty 分支——测试已辨析)。

**4) 运行态观测的边界说明(如实记录):**

- **barrier 等待不可直接观测**:LangGraph 不暴露"屏障等待中"的中间态,只能从执行轨迹(超步分组)反推;wiring 集成测试的 GOLDEN_STEPS + 屏障集合断言即为该事实的守卫,不做运行时假观测。
- **节点跳过原因**已由既有机制覆盖:状态标签(`*_skipped` / `*_failed` / `unavailable`)、GAT `provider_unconfigured` warning、qa_route 事件的 `reason` 字段。
- **三状态语义**(依据文档 §9.4):`timeline_ready` = QA 通过写入 storyline_plan(join);`jianying_draft_ready` = 外层 generate_draft;`video_export_ready` = 外层 node_18。render 删除后不存在会被误判成导出成功的旁支。

---

## 6. 终验结果与遗留

| 项 | 状态 |
|---|---|
| 单测 | ✅ 全绿 EXIT=0(基线 1850/3skip − render 专测 7 + 新增 12 = 1855 passed / 3 skipped 口径) |
| 集成测试 | ✅ 3 失败 = 存量,与基线 `b028d2e` worktree 对照**逐字一致**(test_load_media_then_split_shots_then_qa_gate_chain / test_tail_chain_applies_to_both_drafts / test_sequential_produces_complete_draft),零新增 |
| 前端测试 | ✅ `node --test` workflow_console + workflow_monitor 41 pass / 0 fail |
| console.html 语法 | ✅ 两段内联脚本 `node --check` 通过(交付卡行为强刷即生效,勿重启) |
| 创建台强刷(真机) | 待操作员:交付卡无"故事线预览"行、其余行正常;监控节点图无 render 节点(SCHEMA v5 强制布局重建) |
| qa_route 事件真机 | 待操作员:创建台跑一个任务,事件详情列可见 `qa_route.signal` |
| 推送 | 未自动推送;需要时走 /sync-github |

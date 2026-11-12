# OpenStoryline 子图边审计精简——阶段3-5 执行记录(2026-11-13)

> **对应计划:** 《auto-video-editor_OpenStoryline子图边审计与精简设计执行计划_20261113》(同目录)
> **前置记录:** 《…子图边审计精简_阶段0-2执行记录_20261113.md》(同目录;任务B 交付、AI 关档注册不接线现状冻结、遗留清单 §7)
> **执行范围:** 仅计划 §8 的阶段3(运行时回归)、阶段4(全量验收)、阶段5(文档同步);阶段0-2 不重做。
> **分支:** `feature/storyline-edge-audit-p0-2`(基于 main `97b27a3`;阶段0-2 四笔提交 `e310a7a`/`07795a8`/`0236807`/`fdc647d` 在前)
> **执行日期:** 2026-11-13(Asia/Singapore)

---

## 1. 口径差异(与计划文档默认项)

本阶段无新用户决策,沿用阶段0-2 的**任务B 结案**:`select_bgm` 真实消费配音时长,`[GS, GV]→SB` 双源屏障保留。计划文档以下两处按任务B / 已交付现状重述,不改动代码:

| 计划 §8 阶段3 场景 | 计划字面口径 | 实际执行口径(依据) |
|---|---|---|
| "BGM 调整后" | BGM 只等 GS,可与 GV 并行 | **任务B(0-2 记录 §7.1 已预重述)**:SB 每轮只运行一次、真实消费配音 artifact(缺失退化旧行为)、PTP 仍等待 GV;屏障保留 |
| "网页检索无 query" | query 空 → WT 不执行 | **2026-11-10 前置化已交付并真机验收的现状**:检索词 = 手动 query + 画面概述首句 + 分组主题自动构造,任一非空即检索;**全部为空才跳过**。既有 `test_web_topic_runs_on_auto_query_without_manual_query` / `test_web_topic_runs_on_theme_only_auto_query` / `test_web_topic_skipped_when_no_retrievable_input` 锁定,零改动 |

## 2. 阶段3 交付:运行时回归测试

**提交 `9dacb0b`** `test(边审计·阶段3): 运行时回归——SB真实消费配音时长/RTR读取BGM selection/重试回路四节点每轮次数锁定`

对照计划 §8 阶段3 十场景,既有覆盖为 a/b/g/h/i/j 六场景(见 §4 矩阵);本轮补齐三个缺口:

### 2.1 重试回路四节点次数断言(场景 d/e/f 的"每轮一次")

`tests/integration/test_unreachable_nodes_wiring.py::test_qa_retry_loop_still_retriggers_plan_timeline_pro` 补断言:`generate_voiceover` / `recommend_text` / `select_bgm` / `recommend_transition` 在 qa 重试 3 次的运行中**各恰 4 次**(每轮 1 次)——双源屏障 `[GS,GV]→SB`、`[GC,SB]→RTR` 不得退化为单源边重复触发,也不得漏跑;文件头变更记录追加 2026-11-13 条目(阶段5.1)。

### 2.2 新增运行时消费契约测试(场景 d/e 的"真实消费")

同文件新增 `test_select_bgm_consumes_voiceover_and_rtr_reads_bgm_selection`(默认档跑一轮过):spy 包装 `node_select_bgm.select_bgm` 与 `node_recommend_transition.recommend_transition`(仿既有 `spy_generate_script` 模式),断言:

- SB 每轮恰 1 次,spy 收到的 `voiceover_total_ms` == 从 `storyline_voiceover_artifact` 文件实算求和(跳 error 段、负数钳 0)——真实编译图上证明任务B 消费发生;
- `storyline_bgm_selection` 回写 `voiceover_total_ms`,且 RTR spy 收到的 `bgm_selection` == 该 dict(计划 §5.5 "RTR 读取 BGM selection" 的首个运行时证明);
- 屏障时序:SB 紧随 GV、RTR 紧随 SB。

### 2.3 新增节点级单测 2 文件(12 用例)

遵循 `tests/unit/nodes/` 既有约定(复用 `base_state` fixture、spy 注入):

| 文件 | 用例 |
|---|---|
| `tests/unit/nodes/test_node_select_bgm.py`(8) | `_read_voiceover_total_ms` 容缺省规则 5 例(缺 artifact→0 / 坏 JSON→0 / 非 dict→0 / error 段跳过+负数钳 0+缺 duration 按 0 / 空列表→0)+ 节点传聚合值(spy,含 groups/narration_text)+ 无配音传 0 + 能力层异常 stub 兜底不崩 |
| `tests/unit/nodes/test_node_recommend_transition.py`(4) | 节点传 `storyline_bgm_selection`(spy)+ 缺失/非 dict 传 None + 能力层异常→error_log+failed 标签 + node→capability→plan 全真实链路(LLM items 逐组映射、未给组按默认补齐) |

### 2.4 如实记录的一项观察(未修正)

`storyline_capabilities/recommend_transition.py` 把 `groups`/`bgm` 传给 `render_prompt`,但 `prompts/tasks/generate_ai_transition/{zh,en}/user.md` 模板只有 `{{user_request}}` 占位符——**bgm_ref 当前不进 LLM prompt**(能力层注释自述为"便于将来模板加字段"的预留参数)。本阶段不改模板(改 prompt 属生产行为变更,超出阶段3 范围);bgm_selection 的节点→能力层传递已由 §2.3 用例锁定。

## 3. 阶段4:全量测试与交付验收

计划 §8 阶段4 四条命令 + 0-2 记录 §7.2 遗留项,均在仓库根用 `.venv/Scripts/python.exe` 执行(不依赖 PG):

| 命令 | 结果 |
|---|---|
| `pytest tests/unit -q` | **收集 1922 例(0-2 基线 1910 + 本轮 12),退出码 0(零失败/零错误)**;skip 数未在本次输出留存(0-2 基线为 3 例存量 skip,非本次引入) |
| `pytest tests/integration/test_unreachable_nodes_wiring.py -q` | 38 例全绿(含本轮新增 1 例、扩展 1 例) |
| `pytest tests/integration/test_workflow_graph_schema.py -q` | 13 例全绿 |
| `pytest tests/integration/test_graph_compiles.py -q` | 14 例全绿 |
| 0-2 记录 §7.2 提到的 `test_workflow_console_frontend.py` | **该文件不存在**(0-2 记录笔误;前端契约面已含在 tests/unit 全量内) |

> 注:`addopts = "-q"` 吞汇总行(已知坑),计数以 `--collect-only` 累加、成败以退出码为准。

### 计划 §8 阶段4 七条验收点核对

| # | 验收点 | 结论 | 证据测试 |
|---|---|---|---|
| 1 | STR 只能在 GC 后、GS 前运行 | ✅ | `test_storyline_topology_baseline.py::test_ai_on_topology_baseline_frozen`(GC→STR 唯一入边)+ `test_template_rec_is_main_chain_node_before_generate_script` + 运行时 `test_defaults_run_all_wired_nodes_with_expected_shape`(STR==GC+1、GS==检索+1) |
| 2 | WT 只能由 STR 条件触发,不进 fan-in | ✅ | `test_search_web_topic_hangs_off_template_rec_not_group_clips` + `test_conditional_nodes_are_never_in_a_fan_in_barrier`(AI 开/关两态,基线快照同锁) |
| 3 | SB/RTR/PTP 不被多条独立普通边重复触发 | ✅ | `test_storyline_barrier_shapes_locked`(5 道屏障形状锁死)+ 基线屏障快照 + GOLDEN_STEPS 每轮 SB/RTR 各 1 次 + 本轮新增重试四节点各 4 次 |
| 4 | PTP 六源全部完成后才执行 | ✅ | `test_ai_pair_is_beside_plan_timeline_pro`(ins[PTP] 六源)+ 基线屏障快照;运行时 `gen == ptp == rtr+1` |
| 5 | QA 只等 PTP/PTAT 其一 | ✅ | `test_qa_in_is_final_timeline_only_and_out_goes_straight_to_join` + `test_ai_off_qa_waits_ptp_directly` |
| 6 | QA 重试不因 barrier 凑不齐而静默 END | ✅ | `test_qa_retry_loop_still_retriggers_plan_timeline_pro`(10 节点每轮次数 + `next_nodes` 断言)+ `test_ai_status_tags_are_fixed_so_retry_survives_changing_group_count`(分组数逐轮变化) |
| 7 | 产物被 join / generate_draft 正确消费 | ✅ | 黄金轨迹(SUFFIX 三步)+ `test_defaults_...`(`storyline_join` 后 `next=node_06_human_reorder`)+ `test_workflow_graph_schema.py` + `test_graph_compiles.py` |

## 4. 阶段3 场景 × 测试覆盖矩阵(计划 §8 阶段3 十场景终态)

| 场景 | 覆盖 | 测试 |
|---|---|---|
| a. 检索开+query 非空:STR→WT→GS、内容进 prompt | ✅(既有) | `test_defaults_run_all_wired_nodes_with_expected_shape` + `test_web_topic_reference_reaches_script_prompt_and_is_sanitized` |
| b. 检索关:STR 直达 GS、WT 不执行 | ✅(既有) | `test_web_topic_not_run_when_flag_off` + `test_all_flags_off_matches_pre_wiring_golden_trace` |
| c. 无有效检索输入:WT 不执行、GS 正常 | ✅(既有,口径见 §1) | `test_web_topic_skipped_when_no_retrievable_input`(+ 自动构造两例) |
| d. BGM(任务B):SB 每轮 1 次、消费配音时长、缺失退化、PTP 等 GV | ✅(本轮补齐) | §2.2 新用例 + §2.3 单测 8 例 + 重试断言 + manifest 契约(0-2) |
| e. RTR 每轮 1 次、读取 BGM selection | ✅(本轮补齐) | §2.2 + `test_node_recommend_transition.py` 4 例 |
| f. QA 重试 3 次:回路内节点每轮重跑、不静默 END | ✅(本轮补齐) | `test_qa_retry_loop_...`(扩展后含 GV/TXT/SB/RTR) |
| g. AI 关:GAT/PTAT 不执行、PTP 直进 QA | ✅(既有) | `test_ai_transition_flag_off_skips_both_nodes` + `test_ai_flag_off_graph_has_no_ai_nodes_wired_and_barriers_as_before` + `test_ai_off_qa_waits_ptp_directly` |
| h. AI 未批准/未配置:零外呼、基础时间线进 QA | ✅(既有) | `test_ai_transition_awaiting_approval_keeps_base_timeline_and_zero_calls` |
| i. AI 成功(fake):GAT 产物被 PTAT 物化 | ✅(既有) | `test_ai_transition_real_generation_materializes_into_timeline` |
| j. AI Provider 失败:基础时间线保留、重试照常 | ✅(既有) | `test_ai_provider_failure_degrades_and_keeps_base_timeline` |

## 5. 阶段5 交付:文档、拓扑图和运行态说明同步

| 项 | 内容 |
|---|---|
| `graph.py` BGM 屏障注释 | **复核零改动**:07795a8(阶段1)已改为任务B 真实消费口径(751-756 行区域);节点计数注释已在 e310a7a 纠偏 |
| `tests/integration/test_unreachable_nodes_wiring.py` 文件头 | 追加 2026-11-13 边审计阶段3 变更记录条目(轨迹形状零变更 + 新增用例指引) |
| `web/node_map/workflow.json` | **整体重生成**(0-2 记录 §7.3 建议口径):14 旧节点 → 当前 17 个 `storyline_*` 节点 + 外层 `generate_draft` 终点条目;删除已不存在的 `search_media`/`render_video`;连接改为 dependencies(屏障=全部源)/next_nodes 双向一致表达(select_bgm 双源含 voiceover、RTR 双源、PTP 六源、STR 条件二出口、QA 条件重试回边+join 出口);新增节点用简条目(`input_schema`/`output_schema` 空数组,仿既有 recommend_script_template 先例);select_bgm/voiceover 描述注明任务B 时长消费。校验:JSON 合法、18 id 无悬空引用、双向镜像一致(唯一不对称 = QA→GC 条件重试回边,按条件边语义只出现在 QA.next) |
| `web/node_map/node_map.html` | "旧 14 节点子流程(行为不变)"注释更新为"已同步当前 17 节点拓扑;仅为示意,权威以 graph.py 为准"(668-671 行区域) |
| 计划文档 | 头部加执行结果批注(任务B 结案,§7.2 的 GS→SB diff 不适用,指向本记录);原文其余不动 |
| 本执行记录 | 入库(同目录,命名对齐 0-2 记录) |

## 6. 提交清单

| 提交 | 阶段 | 内容 |
|---|---|---|
| `9dacb0b` | 阶段3 | 重试四节点次数断言 + 运行时消费契约用例 + 节点级单测 12 例(3 文件,+377/-1) |
| (本次) | 阶段4-5 | wiring 头变更记录 + workflow.json 重生成 + node_map.html 注释 + 计划文档批注 + 本记录 |

## 7. 遗留与操作员事项

- **计划 §11 可选优化**(AI 关档时 GAT/PTAT 的 `add_node` 移入开关分支)仍未执行——0-2 记录 §3.2 已按现状冻结基线,执行时需同步改 `test_ai_off_topology_baseline_frozen` 的注册断言;本阶段未获授权,不动。
- **真机观察项**(沿用 0-2 记录 §7.4):真机跑通 auto 链后查看 `storyline_bgm_selection.voiceover_total_ms`——FireRed TTS 正常时等于各段配音时长之和;stub 路径为静音 wav 估算时长(非 0)。该字段仅为选曲偏好输入,不影响交付。
- **真机 E2E** 与 AI 转场实机验收:非本计划阶段范围,按各自主计划执行。
- `web/node_map/workflow.json` 为纯静态示意快照(仅 node_map.html 演示视图 fetch,无代码依赖);后续接线变更需随手同步,权威始终以 `graph.py` + `tests/unit/test_storyline_topology_baseline.py` 为准。

# auto-video-editor noop 空转节点普查(2026-11-11)

> 基线:HEAD `04019c8`(main,含未提交的第四期修复改动,本文结论均基于当前工作区源码)。
> 性质:**仅记录分析,不改代码**——与 2026-11-11 文案模板推荐/选择BGM 两节点分析的用户决定一致。
> 方法:`graph.py` 接线全图审计 + 逐节点「读什么 / 写什么 / 谁消费」审计,产物键名全仓 grep(测试目录消费单独注明,不计真实消费者)。

---

## 0. 判定口径与总体面貌

- **图定义唯一文件**:`graph.py`(节点注册 47 个,`g.add_node(` 计数为准;另有 START/END)。图结构守卫:`tests/integration/test_unreachable_nodes_wiring.py:165-170` 断言所有注册节点从 START 可达,因此**不存在"漏接边的孤立节点"**——所有空转都是"节点本身空"或"产物没人要",不是"忘接线"。
- **A类 noop**:不读业务输入、不产业务输出,仅写 `status_log` 标签。
- **B类 死胡同**:有产出,但全项目无下游消费者(节点级或字段级)。
- **C类 半截**:产出被消费,但链路中途断掉(如止步于 JSON、进不了剪映草稿)。
- 状态传递机制:共享 state(LangGraph)+ artifact JSON 文件(路径进 state)+ 剪映草稿文件三层混合;`status_log` 标签兼做条件路由依据(`qa_gate.py:173-183` 读 `status_log[-1]`)。

---

## 1. 核心结论:空转节点四档分级

### 第一档 · 结构级 noop(任何配置下都空转,铁证)

| 节点 | 中文名 | 证据 |
|---|---|---|
| `storyline_script_template_recommendation` | 文案模板推荐 | ① 节点体不读任何业务 state 键,仅往 `status_log` 追加 `storyline_script_template_recommendation_done` 标签(`nodes/storyline/node_script_template_recommendation.py:24-33`);② 图中**只有入边、零出边**(`graph.py:725` 入边来自 generate_script;全文件无一条以它为源的边)→ 图内死胡同终点;③ 产物零消费者。系 vendored MCP 透传壳子阶段 7 本地化的遗留,代码自述留给未来 `storyline_capabilities.script_template_rec` hook(该模块不存在) |

**它是本次全图普查中唯一实锤的结构性 noop。** 图内另一死胡同终点是 `END` 前的正常出口(export/delivery_qa_failed/escalate),不算。

### 第二档 · 默认配置下空转(功能开关默认关,显式留痕跳过——设计如此)

| 节点 | 中文名 | 证据 |
|---|---|---|
| `storyline_render_video` | 渲染视频(剧情链) | `STORYLINE_ENABLE_RENDER_SMOKE_TEST` 默认 `"0"`(`config.py:101-102`)→ 直接返回,仅追加 `storyline_render_video_skipped` 标签(`nodes/storyline/node_render_video.py:39-47`,代码注释自述"默认 noop");即便开关打开,产物 `storyline_render_smoke_test_path` 全项目无读者,`config.py:100` 注释明说"不接入下游";`graph.py:806` 注释自证"默认 noop → join;不接入下游(plan §2.2 旁支 + ADR-006)" |
| `node_18_export_video` | 最终 MP4 导出 | `FINAL_EXPORT_ENABLED` 默认 **False**(`config.py:517-519`)→ 只写 `final_export_status="skipped"` + `node_18_export_skipped:disabled` 标签即过(`nodes/node_18_export_video.py:70-78`),不读任何业务输入;开关打开后是真渲染(`draft_ops/draft_exporter.py` run_export)。MP4 本体是人工交付物,系统内无下游属正常定位 |

两节点均**显式 skipped、非静默**,属"可选能力总开关默认关"的既定设计,与静默空转性质不同。

### 第三档 · 节点身份已死(图中零注册,文件是活函数库不能删)

| 节点 | 中文名 | 证据 |
|---|---|---|
| `node_16_translate_subtitles`(节点函数) | 翻译字幕(旧版整节点) | 节点函数**零 add_node/add_edge 引用**,graph.py 仅剩 docstring 提及(`graph.py:84`);文件头自述(2026-09-30 核查,只标记不删):"唯一调用方是 tests/unit/test_node_16_translate_subtitles.py"(`nodes/node_16_translate_subtitles.py:290-303`)。**文件本体必须保留**:6 个纯函数被生产代码真实复用——`node_16a_translate_and_check.py:32-39` import `_marker_exists`/`_read_segments_from_draft`/`_save_marker`/`_write_marker`/`validate_layout`/`write_srt_from_segments`;`node_checkpoint3_layout_review.py:31-34` import `_build_interrupt_payload`/`write_srt_from_segments` 等 |

即:这个"节点"已不存在于工作流中,现存的只是它留下的函数库 + 一个仅测试引用的 shim。

### 第四档 · 跑了但白跑(有产出、产出对成片零效果——严格说是 C类半截,不是 noop)

| 节点 | 中文名 | 证据 |
|---|---|---|
| `storyline_generate_ai_transition` | 生成 AI 转场 | 默认**开启**(`STORYLINE_ENABLE_AI_TRANSITION` 默认 true,`config.py:61-62`,建图时读取)→ 每轮实跑、进 4 源屏障;但当前唯一客户端是 `StubAITransitionClient`(占位,每衔接点 500ms 假数据,`nodes/storyline/node_generate_ai_transition.py:20-21` docstring 自述);产物 `storyline_ai_transition_artifact` 唯一消费者是 PTAT |
| `storyline_plan_timeline_ai_transition` | AI 转场时间线规划 | 把 `ai_transitions` 清单 + `options.enable_ai_transition` 写进 timeline(`node_plan_timeline_ai_transition.py:108-115`)后,**storyline/mapper.py 全文无 ai_transition 字样、node_05 亦不读 `enable_ai_transition`**(已 grep 验证)→ 草稿层零物化。每次 QA 重试两节点各重跑一次,净效果 = 多写两个 JSON 文件 + 固定 status 标签 |

对照:真正的转场进草稿走的是 `recommend_transition` → timeline clips 的 `transition_in/out` → `node_09_inject_fx.py:74-91` 原生绑定——那条链是通的。

### 另注:已知半截(既有结论,不重复展开)

- `storyline_select_bgm`(选择 BGM):选择逻辑真实(读 script/groups → 启发式+LLM 挑 1 个),但 `bgm_meta_ref.yaml` 5 条 `path` 全空 → 音频永不落草稿,只做节拍对齐时长分配;BGM 落轨靠人工关卡②。**用户已确认为设计如此**(详见 2026-11-11 两节点分析记录)。
- `node_11_inject_sticker`(注入贴纸):写路径真实,但 `templates/sticker_template.json` 全是 `PLACEHOLDER_STICKER_*` 且 `_reverse_engineering_pending: true` → 生产恒 fail-closed 降级、从不落草稿;回填真实 ID 即自动生效(join QA 有 stickers==0 兜底,链路不炸)。

---

## 2. 明确排除:看着像空转、实际真实干活的节点

| 节点 | 排除理由 |
|---|---|
| `storyline_recommend_transition` 推荐转场 | 产物 `storyline_transition_plan` 被 `node_plan_timeline_pro.py:56` 与 `node_09_inject_fx.py:79` 消费,语义转场真实注入剪映草稿 |
| `storyline_recommend_text` 推荐文本 | 产物 `storyline_text_style_plan` 被三处消费:PTP:57 回填 per-group `subtitle_style`、`node_08_add_subtitles.py:880-885`(zh)、`node_08b_add_subtitles_en.py:56`(en) |
| `storyline_search_web_topic` 网页话题检索 | 消费链完整:query 三源构造(`_common.py:381-419`)→ 检索 → `generate_script.py:53-55` 注入 LLM prompt;QA 重试时 cache_hit 自复用 |
| 人工关卡① `node_06_human_reorder` | 真实 interrupt 挂起 → 剪映人工调序 → resume 放行(`resume_utils.py` 按 "①" 匹配);消费 assembly 报告 |
| 人工关卡② `node_12_human_add_bgm` | 最实的关卡:BGM 整轨镜像到英文草稿 + 回读校验 + `bgm_manual_draft_sha` 审计 + interrupt 二次校验 |
| 人工关卡③ `node_checkpoint3_layout_review` | 条件触发(仅 layout_issues_detected 为真才进)真实 interrupt,resume 后从草稿重读重写 SRT |
| `node_fork_english_branch` / `node_join_before_delivery` / `node_delivery_qa_repair` / `delivery_qa_failed` | 分支复制+剪映注册 / 交付 QA 闸门(gate_failed 三路路由)/ 按类别幂等重跑修复 / raise 终止——全部真实控制流 |
| `bridge_snapshot2`、`escalate_guardrail_failure`(graph.py 内) | 前者因"条件边不能写 state"而存在,写 `snapshot2_path` 有 node_08/node_fork 两个真实消费者;后者是有真实文案与留痕的终止控制节点 |
| assembly 六 QC 节点(discover/observe/build_timeline/validate/repair/write_report) | 默认开启(`ASSEMBLY_QC_GATE_ENABLED` 默认 true,`config.py:283-286`)且产物消费链闭环;报告被关卡①通知消费 |
| `storyline_generate_voiceover` | 唯一从 ID 到真实音频文件全通的音频链(WAV → mapper ref=真实路径 → 落轨) |

### 排除名单里的三个已知占位小瑕疵(节点真实,仅局部半自动,如实记录)

1. **关卡①②的通知是 `print` 占位**:`node_06_human_reorder.py:44-47`、`node_12_human_add_bgm.py:40-42`(注释"替换为真实通知渠道"未兑现)——关卡本体(interrupt/resume/校验)真实。
2. **assembly_repair_loop 的"补丁"是占位**:仅写 metadata `repair_marker`(`nodes/assembly/node_repair_loop.py:58-74` 自述),重试与软降级链路真实,但不真正修复 QC 发现的问题。
3. **node_15 标题英译是 mock 拼接**:`[EN] {title_zh}`(`node_15_localize_covers_en.py:49`,"Week 5 接翻译服务"未接)——封面英文版产物本身有消费者(join QA 检查)。

---

## 3. 注册节点总览(47 个,按组)

**storyline 自动链(18)**:load_media ✅ / split_shots ✅ / understand_clips ✅ / filter_clips ✅ / group_clips ✅ / search_web_topic ✅ / generate_script ✅ / **script_template_recommendation ❌第一档** / generate_voiceover ✅ / select_bgm ⚠️已知半截 / recommend_transition ✅ / recommend_text ✅ / plan_timeline_pro ✅ / **generate_ai_transition ❌第四档** / **plan_timeline_ai_transition ❌第四档** / qa_gate ✅(控制流) / **render_video ❌第二档** / join_storyline ✅

**装配 QC(6,默认开)**:discover_and_probe ✅ / visual_observe ✅ / build_timeline ✅ / validate_render_qc ✅ / repair_loop ⚠️patch 占位 / write_report ✅

**主图主干(23)**:generate_draft(05) ✅ / node_06 关卡① ✅ / node_07 ✅ / escalate_guardrail_failure ✅ / bridge_snapshot2 ✅ / node_08 ✅ / node_10 ✅ / node_14 ✅ / node_15 ⚠️英译 mock / fork_english_branch ✅ / node_16a ✅ / node_08b ✅ / node_10b ✅ / checkpoint3 关卡③ ✅ / node_17 ✅ / node_09 ✅ / node_11 ⚠️模板占位恒降级 / node_12 关卡② ✅ / node_13 ✅ / join_before_delivery ✅ / delivery_qa_repair ✅ / delivery_qa_failed ✅ / export_final_video(18) ❌第二档

**未注册(死注册)**:node_16_translate_subtitles ❌第三档

图例:✅=真实干活;❌=本次普查认定的空转/死注册;⚠️=真实但含已知占位瑕疵。

---

## 4. 附录 A:B类死键/死文件清单(有产出、无生产读者)

> 这些不构成 noop 节点,但属于"写了没人读"的产物,与空转普查同源,一并留档。均不含测试读者。

### storyline 侧(6 项)

| 键/文件 | 写入处 | 备注 |
|---|---|---|
| `storyline_render_smoke_test_path` + render_smoke_*.json | node_render_video.py:55-71 | 仅 flag-on 分支产生;config.py:100 自注"不接入下游" |
| script.json 的 `references` 数组 | node_generate_script.py:101 | 自述"供人工审核追溯",无任何代码/UI 读者 |
| script_llm_trace.json | node_generate_script.py:81-96 | 设计意图为证据落盘;仅测试读 |
| `storyline_error_code` | qa_gate.py:122,147 + join_storyline.py:117 | 无生产读者(仅测试) |
| `storyline_bgm_selection.bgm_path` | storyline_capabilities/select_bgm.py:144 | **BGM 音频断链根因**:真实路径无读者,mapper 物化时用资源 ID |
| timeline `audio.voiceover` JSON 字符串 | plan_timeline_pro capability | mapper 不读;capability 自注"联调可查" |

### 主干侧(19 项)

| 键 | 写入处 | 备注 |
|---|---|---|
| `fx_summary` | node_09_inject_fx.py:338 | ⚠️ **`draft_integrity.py:801` docstring 声称会读它,实际 `build_qa_categories` 只读 voiceover_summary/sticker_summary/tts_issue——注释与实现不符,易误导** |
| `text_fx_summary` | node_10_inject_text_fx.py:188 | 10b 特意不写它只发标签(:195-197) |
| `volume_adjusted` / `audio_fade_targets` | node_13_adjust_volume.py:326-331 | 仅 node_13 自读旧值 + 测试 |
| `subtitle_scout_report` | node_08_add_subtitles.py:937 | scout 样式效果已在节点内落草稿;node_08b 传 None 的注释已过时 |
| `en_audio_path` / `en_dub_audio_path` / `tts_run_id` | node_17_inject_english_tts.py:237-243 | 音频本体已在英文草稿 |
| `bgm_manual_draft_sha` / `bgm_notified` | node_12_human_add_bgm.py:298-301 | state.py:142-143 明示"本设计不依赖" |
| `reorder_notified` | node_06_human_reorder.py | 同上 |
| `checkpoint3_triggered` / `layout_issues_source` | checkpoint3 / node_16a | 仅审计位,仅测试读 |
| `join_qa_issues` | node_join_before_delivery.py | 有效信息已并入 `join_qa_report` |
| `draft_source_normalized_path` / `draft_published_dir` / `draft_publish_error` | node_05_generate_draft.py:296-312 | 真实消费走 `_draft_dir_override`(config.py:235-236) |
| `final_video_path` / `final_export_report` | node_18 flag-on 分支 | `final_export_status` 仅 scripts/freeze_run_baseline.py:251 读 |
| `assembly_repair_evidence_path` | node_repair_loop.py:377 | write_report 未汇总;仅 e2e 脚本与测试读 |

---

## 5. 若未来要清理(仅留档,未批准、未执行)

1. **第一档** `script_template_recommendation`:最小改动 = 删 `graph.py:558-560` 注册 + `:725` 入边 + 节点文件,同时删 `monitoring/workflow_graph.py` _NODE_META 对应条目;但它是留给未来模板推荐 hook 的接线占位,删除前需产品侧确认放弃该功能。
2. **第三档** `node_16` shim:按文件头自述流程处理——grep 全仓确认零引用后,连 `tests/unit/test_node_16_translate_subtitles.py` 一起删 shim 函数,文件其余函数保留。
3. **第四档** AI 转场对:短期可设 `STORYLINE_ENABLE_AI_TRANSITION=0` 关闭(免每轮白跑两个 LLM/Stub 调用);长期需 mapper 物化 `ai_transitions` 或整体移除。
4. **B类死键**:删除属纯瘦身(不动行为),但需同步修订 `draft_integrity.py:801` 的失真注释;建议单独立项,勿与功能改动混提。

---

## 6. 关联记录

- 2026-11-11 两节点分析(文案模板推荐=noop、select_bgm 半截=设计如此):见会话 sess_0c0bb24b 分析记录,本文档为其全图扩展。
- 图接线守卫:tests/integration/test_unreachable_nodes_wiring.py(新增节点漏接边会在此报警)。
- 剪映草稿效果缺失修复(第三期/第四期)文档:同目录。

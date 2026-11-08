# B1 ASR 移除 —— 阶段 0 盘点记录

| 项 | 内容 |
|---|---|
| 执行日期 | 2026-11-08 |
| 基线提交 | `5d3ebe618b7242254739321f36a0294b7f55403e`(`55b1de5` + 两份计划文档入库,代码零差异) |
| 工作区状态 | 干净(提交前无未跟踪/未提交改动) |
| 执行范围 | `B1_ASR_移除设计与分阶段执行计划.md` 阶段 0~3;阶段 4~6 不执行 |
| 方法 | 只读静态盘点(两个并行检索 + 人工复核关键文件),图拓扑快照与 pytest 基线为运行产物 |

---

## 1. 图拓扑基线快照

- 生成方式:`config.make_checkpointer("memory")` + `graph.build_graph(...)`,枚举 `compiled.builder` 的 nodes/edges/branches。
- 脚本:`%TEMP%\b1-baseline\dump_topology.py`(一次性,不入库);产物:`%TEMP%\b1-baseline\graph-topology.json`。
- 摘要:**48 节点 / 37 条静态边 / 7 个条件分支源**。
- ASR 旁支证据(基线快照内):
  - 节点存在:`storyline_local_asr`、`storyline_speech_rough_cut`。
  - `storyline_split_shots` 分支 `_storyline_route_after_split_shots` → ends = `[storyline_local_asr, storyline_understand_clips]`。
  - 静态边:`storyline_local_asr → storyline_speech_rough_cut`。

## 2. 引用盘点结论(删除前提确认)

### 2.1 可整体删除(已确认无其他生产消费者)

| 对象 | 唯一生产消费者 | 结论 |
|---|---|---|
| `jy_common/asr_client.py` | `nodes/node_08_add_subtitles.py:27`(`call_asr2s`) | 可删;`assembly_capabilities/speech_asr.py` 不 import 它;`jy_common/__init__.py` 为空,无再导出 |
| `storyline_capabilities/asr_runner.py` | `nodes/storyline/node_local_asr.py:24-27` | 可删;`jy_common/provider_contracts.py:51` 仅 docstring 提及(不 import) |
| `storyline_capabilities/speech_rough_cut.py` | `nodes/storyline/node_speech_rough_cut.py:24` | 可删 |
| `storyline_capabilities/timeline_transcript.py`(`fill_clip_transcripts`) | `nodes/storyline/node_plan_timeline_pro.py:33,133-143` | 可删;无 agent_gate/web/db/scripts 消费者 |
| `prompts/tasks/speech_rough_cut/`(zh/system.md、zh/user.md) | `speech_rough_cut.py:62,64` 字面量 kind | 可删;无 prompt 注册表;`storyline_capabilities/prompts/speech_rough_cut/` 不存在 |
| `nodes/storyline/_common.py::media_has_audio`(240-254) | `graph.py:186,220`(仅 `_asr_branch_enabled`) | 可删连同 `__all__` 条目(328) |
| `nodes/storyline/node_local_asr.py`、`node_speech_rough_cut.py` | `graph.py:159-160` | 可删;`nodes/storyline/__init__.py` 无再导出 |

### 2.2 node 08 相关

- ASR 实现:`import call_asr2s`(L27),单视频 L368 / 多视频 L366,注入循环 L379-398,`sync_text_track(draft, texts)` **无条件调用**(L402)——ASR 移除后 texts 恒空,会清空 mapper 脚本字幕轨,必须连调用一起删。
- ASR 专属 helper:`_collect_source_spans`(214-270)、`_map_asr_to_target`(273-303)、`_multi_video_asr_segments`(306-342)。
- 必须保留(外部依赖):
  - `_style_from_scout` ← `nodes/node_08b_add_subtitles_en.py:25`;
  - `_JIANYING_DEFAULT_STYLE`、`_run_subtitle_scout` ← `scripts/video_edit_phase56_e2e.py:355`。
- `subtitle_scout` 调用(L374-375)属非 ASR 样式能力,保留。
- 返回键现状(L423-431):`{**state, asr_segments_zh, asr_segments_zh_written: True, status_log}` + 条件 `subtitle_scout_report`(该键未在 state.py 声明,图运行时丢弃——现状,不动)。

### 2.3 state 字段(阶段 0~3 只停写,不删声明)

`state.py`(TypedDict total=False):`asr_segments_zh`(159)、`asr_segments_zh_written`(160)、`storyline_asr_artifact`(247)、`storyline_rough_cut_artifact`(248)、`assembly_transcript_artifact`(267,assembly 不动故完全不动)。

- `asr_segments_zh` 生产写:`node_08:425-426`;生产读:`node_16a:195`。checkpoint/resume/web/db/agent_gate 无读写。
- `asr_segments_zh_written` 无生产读者(纯标记)。

### 2.4 配置与环境

- `config.py:59-61` `STORYLINE_ENABLE_ASR_BRANCH`(默认 `"true"`)+ 注释 36-40、56-58。
- `.env.example:18-21` ASR 旁支注释块 + `STORYLINE_ENABLE_ASR_BRANCH=1`。
- `ASR_BACKEND` / `FIRERED_ASR_ENDPOINT` 仅存在于 `jy_common/asr_client.py` 内部(env 直读),config.py 与 `.env.example` 均无条目。

### 2.5 graph.py 拓扑改动点

- import:159-160(两个节点)、186(`media_has_audio`,保留 `web_topic_query`)。
- 函数:`_asr_branch_enabled`(216-220)、`_storyline_route_after_split_shots`(243-257,唯一另一分支 = `storyline_understand_clips`)。
- 注册:546-548;边:693-704(条件边)→ 换静态边,705(`local_asr → rough_cut`)删除。
- docstring:66-68(ASR 旁支描述)、72-74(D10 屏障列表提及)。

### 2.6 展示层元数据(本轮不动,阶段 5 范围)

- `monitoring/workflow_graph.py:246-260,368`(_NODE_META/_EDGE_LABELS 含两个 ASR 节点)——真实拓扑由编译图派生,陈旧条目不影响显示与测试。
- `storyline/firered_adapter.py:102-113,276`(ToolSpec 字符串元数据,无 import)。
- `jy_common/provider_contracts.py:51`(docstring)。
- `web/node_map/workflow.json` 不含 storyline 节点(grep 零命中)。

## 3. 受影响测试清单(阶段 1~3 落点)

### 3.1 删除

| 文件 | 原因 |
|---|---|
| `tests/unit/test_timeline_transcript.py` | 整文件测 `fill_clip_transcripts` |
| `tests/unit/test_asr_client_firered.py` | 整文件测 jy_common asr client |
| `tests/unit/test_node_08_multi_video_subtitles.py` | 只测被删的三个 ASR 映射 helper(monkeypatch `mod.call_asr2s`) |

### 3.2 修改

| 文件 | 改动 |
|---|---|
| `tests/unit/test_node_08_add_subtitles.py` | 去 `set_default_client` ASR 依赖;8 个 ASR 用例改新语义;新增字幕保留用例;12 个 scout 样式用例保留 |
| `tests/unit/test_node_16a_translate_and_check.py`(111-120)、`test_node_16_translate_subtitles.py`(221-232) | 空段 → 正常 skip(新标签 `skipped_no_source_subtitles`),无 error_log;shim(`node_16_translate_subtitles.py:328-330`)转发条件同步 |
| 新建 `tests/unit/test_node_08b_add_subtitles_en.py` | node_08b 无专属单测(仅 `test_reorder_topology.py:125` 覆盖);补空列表 skip 用例 |
| `tests/unit/test_wiring_routers.py`(73-95、138-155) | 删 4 个 ASR 路由用例 |
| `tests/unit/test_flag_defaults_aligned.py`(29,40,65,76) | `FLAG_ENV_NAMES` 去 flag,子进程探测期望表同步 |
| `tests/unit/test_storyline_capabilities_c_classes.py`(254-310、479-495、518-529) | 删 speech_rough_cut/asr_runner 用例;import 测试去两个模块名 |
| `tests/unit/test_provider_contracts.py`(156,161,171-190) | 删 `MockASRClient`/`StubASRClient` 相关用例 |
| `tests/unit/test_web_topic_reference.py`(18,137-174) | 删 `media_has_audio` 用例与 import,保留 web_topic_query 部分 |
| `tests/unit/test_prompt_loader_semantics.py`(55-57) | 硬编码 48 → 46;重新生成 `tests/unit/golden/prompt_render_golden.json`(`scripts/capture_prompt_golden.py`) |
| `tests/integration/test_workflow_graph_schema.py`(153) | storyline 死端节点数 3→2;另加"两个 ASR 节点不再注册"负向断言 |
| `tests/integration/test_unreachable_nodes_wiring.py` | NEIGHBORS 表(107-118)、CONDITIONAL_NODES(54-59)、死端参数化(199-204)更新;删 6 个 ASR 运行时用例(644-709)与 monkeypatch(439、451);保留 web_topic/search_media 的 D10 守卫 |
| `tests/integration/test_reorder_topology.py`(97,108,115,125) | :125 断言 `subtitle_segments_en` 非空,ASR 移除后需改空输入语义 |
| `tests/integration/test_week4_graph.py`、`test_week5_resilience.py`、`test_state_field_compatability.py`、`test_postgres_resume.py` | 种子键仍有效(state 声明保留);逐个核对断言 |
| `tests/integration/test_auto_mode_end_to_end.py`(58-59) | 种子空字符串产物,不动 |

## 4. pytest 基线

- 命令:受影响 22 文件(见 §3)+ `--junitxml %TEMP%\b1-baseline\p0-affected.xml`。
- 结果:**total=308,failures=7,errors=0,skipped=1**,耗时 82.9s。
- 既有失败(非本次引入,验收口径"新增失败=0"):
  1. `test_unreachable_nodes_wiring::test_all_flags_off_matches_pre_wiring_golden_trace`
  2. `test_unreachable_nodes_wiring::test_qa_retry_loop_still_retriggers_plan_timeline_pro`
  3. `test_unreachable_nodes_wiring::test_ai_client_failure_keeps_base_timeline`
  4. `test_unreachable_nodes_wiring::test_ai_status_tags_are_fixed_so_retry_survives_changing_group_count`
  5. `test_unreachable_nodes_wiring::test_ai_pair_attaches_only_junctions_that_exist_in_the_final_timeline`
  6. `test_reorder_topology::test_tail_chain_applies_to_both_drafts`
  7. `test_auto_mode_end_to_end::test_load_media_then_split_shots_then_qa_gate_chain`
- 既有跳过(环境性):`test_node_16_translate_subtitles::test_validate_layout_missing_font_via_invalid_path`(Windows PIL 恒有默认字体)。

## 5. 阶段 0 验收结论

- [x] 基线提交固定:`5d3ebe6`(代码等价 `55b1de5`)。
- [x] 全部删除项的调用者已确认(§2.1),无"未确认调用者"的删除对象。
- [x] 图拓扑快照已存(§1)。
- [x] pytest 基线已存(§4)。
- 备注:`docs/第三期开发/` 两份计划文档已在基线提交内(单独 docs 提交,先于本记录)。

---

## 6. 阶段 0~3 执行结果(2026-11-08 追加)

| 阶段 | 提交 | 内容 |
|---|---|---|
| 1 | `c7e117d` | 新语义测试:node_08 无 ASR/字幕保留、16a/08b 空段正常跳过、schema ASR 旁支负向断言(实现前按计划允许红,7 例红在正确断言上) |
| 2 | `4a359b6` | 删 `storyline_local_asr` / `storyline_speech_rough_cut` 节点与 3 个能力模块(`asr_runner` / `speech_rough_cut` / `timeline_transcript`)与 prompt 目录;`split_shots` 改静态直连 `understand_clips`;config/.env 清开关;PTP 去 transcript 回填;测试同步(golden 48→46、schema 死端 3→2、wiring/routers/flag/c_classes/provider_contracts/web_topic 同步) |
| 3 | `d43d692` | node_08 去 ASR:`call_asr2s` import、`_collect_source_spans` / `_map_asr_to_target` / `_multi_video_asr_segments`、注入循环、**`sync_text_track` 空轨替换调用**全部删除;scout 样式链保留;`asr_segments_zh` / `asr_segments_zh_written` **不再写**(计划 §3.3"置空或缺省"取缺省,state 旧值经 `{**state}` 透传兼容旧线程);16a 空段改 `skipped_no_source_subtitles` 正常跳过、08b 空段改 `node_08b_skipped_no_en_segments`;shim 同步转发;删 `jy_common/asr_client.py` 及 2 个专属测试 |

**设计决策变更说明**:node_08 对 `asr_segments_zh` 由"显式写空列表"调整为"完全不写"(缺省)。原因:显式写空会清掉旧线程 state 里已有的段落,导致 9 个播种该键的存量集成测试(week4/week5/state_field)的"有源字幕"路径断链;"缺省"同样满足计划 §3.4.7"新任务为空/缺省",且与 §3.3"暂时保留为兼容字段"的定位一致——新任务 state 天然无此键(16a 正常跳过),旧线程残留值经 `{**state}` 原样透传,不新增伪造、不主动清写。

### 6.1 验证结果

- **图拓扑**:基线 48 节点 / 37 静态边 / 7 条件分支源 → 终态 **46 / 37 / 6**(-2 节点、-1 条件分支,静态边数不变:-2 旁支边 +1 静态主边)。
- **check_imports**:`未解析的导入: 0 处`。
- **全量单元套件**(`tests/unit`):**1474 例,0 失败,0 错误,3 环境跳过**。
- **受影响集成套件**(schema/compiles/wiring/reorder/week4/week5/state_field/postgres_resume/auto_mode):与阶段0基线**完全相同的 7 个存量失败**(wiring×5 + reorder tail_chain×1 + auto_mode split_shots×1,失败签名逐字一致,均为存量环境/LLM 问题,与 ASR 无关),**零新增失败**。
- **残留扫描**(计划 §7.1):生产代码无可执行 ASR import/注册/开关;剩余命中仅为——历史注释(config/.env.example/graph.py)、监控展示元数据(`monitoring/workflow_graph.py`,阶段5)、`firered_adapter` ToolSpec 与 docstring 交叉引用(阶段5)、测试负向断言与历史说明、assembly 侧(本轮不动)。
- **明确遗留**(阶段5~6 范围,本轮不动):`monitoring/workflow_graph.py` 两节点元数据与边标签、`storyline/firered_adapter.py` ToolSpec、`jy_common/provider_contracts.py:51` docstring、`tts_client.py`/`speech_synthesize.py` docstring 对照提及、state.py 四个 ASR 字段声明、README/节点图文档。
- **旧 checkpoint**:图拓扑已变更(删 2 节点 + 静态边),暂停在旧节点的存量线程不能跨拓扑恢复(计划 §5.3);发布前需由运维侧完成/终止存量任务。

---

## 7. 阶段 4~6 执行结果(2026-11-08 追加)

| 阶段 | 提交 | 内容 |
|---|---|---|
| 4 | `87df15e` | assembly 复合节点拆分:新建 `nodes/assembly/node_visual_observe.py`(`assembly_visual_observe_node`,纯视觉观察),删 `node_asr_and_visual_observe.py` / `speech_asr.py` / `test_speech_asr.py`;`graph.py` 注册与两条边改接 `assembly_discover_and_probe → assembly_visual_observe → assembly_build_timeline`;`video_ingest` 去 transcript 输入链(参数忽略、`observation_signature` 去 `transcript_sha256`、删 `transcript_for_inline`);`transcript.py` 只留 `subtitle_build` 复用 4 函数(`load_transcript_data` / `collect_segments` / `first_present` / `coerce_float`),删 6 个失去消费者的 ASR 解析函数;`build_timeline_from_paths` 可选 transcript 入参保留(evaluate_segment_selection 在用),`node_build_timeline` 不再读 `assembly_transcript_artifact`;`transcript_chars` 仅在显式传 transcript 时计入;监控注册表 :72/:99 同步改名;测试/脚本同步(`test_assembly_nodes` 3 例改名+负断言、conftest 去 ASR stub、`test_visual_observe` 换负向用例+签名断言、schema 测试 REMOVED_NODES+新链正断言、qc_graph 去桩改边断言、phase5_e2e/phase5_combined/video_edit_phase56_e2e 去 `--transcript`、产物清单 8→7) |
| 5 | `f16e963` | `monitoring/workflow_graph.py` 清陈旧条目(node_08 描述、split_shots 描述、local_asr/speech_rough_cut 两节点条目、失效边标签);`state.py` 5 字段(`asr_segments_zh` / `asr_segments_zh_written` / `storyline_asr_artifact` / `storyline_rough_cut_artifact` / `assembly_transcript_artifact`)标 **DEPRECATED 保留声明**(旧 checkpoint 兼容,§3.3 策略);`jy_common/provider_contracts.py` 删 `ASRResult` / `ASRProvider` 死契约(TTS 契约保留)+契约测试同步;`storyline/firered_adapter.py` 删 `local_asr` / `speech_rough_cut` ToolSpec 与 OPTIONAL_TOOLS 条目;docstring 交叉引用清理(`tts_client`×2 / `speech_synthesize` / `media_probe`);README 9 处现行描述更新(目录树、依赖表、P1-2 作废、§4.6 拓扑、§4.10 表+备注+开关示例、Week3 约束表);`agent_gate/README.md:123` 标注作废 |

**设计决策变更说明**:

1. **`tests/fixtures/assembly_phase5/transcript.json` 保留**(计划原列为"删除"):它不只服务 assembly e2e,`tests/unit/video_edit_capabilities/test_subtitle.py` 把它当作 `subtitle_build`(字幕构建,合法保留能力)的输入 fixture;误删导致 5 例静默 skip(skip 3→8),已回滚恢复。教训:删 fixture 前须全仓 grep 消费者。
2. **`assembly_capabilities/transcript.py` 不整删**:被 `video_edit_capabilities/subtitle_build.py:67-72` 复用 4 个纯函数;只删失去生产消费者的 6 个 ASR 解析函数(`read_transcript_text` / `transcript_data_to_text` / `format_segment` / `fmt_time` / `transcript_text_for_range` / `segment_overlaps_range`)。
3. **主图不传 transcript、能力层保留可选接口**(计划 §3.5):`build_timeline_from_paths(transcript_artifact=None)` 默认签名不变,评估脚本等非主图调用方不受影响;`transcript_chars` metadata 改为仅在显式传入时计入。
4. **`video_ingest` 缓存签名变化**:`observation_signature` 去掉 `transcript_sha256` 分量,改动后首次运行会重建一次 contact sheet 缓存(实测多视频第二次运行 0s 命中新签名缓存)。
5. **state.py 走"保留声明 + DEPRECATED 注释"**,不删字段:与阶段 0-3"asr_segments_zh 取缺省不写"的兼容口径一致,存量任务过期后再单独提交字段删除。

### 7.1 验证结果

- **check_imports**:`未解析的导入: 0 处`(阶段 4/5 后各跑一次)。
- **全量单元套件**(基线=51ca0d1 冻结 XML,1474 例 0 失败 3 跳过):
  - 阶段 4 后:**1451 例,1448 通过,0 失败,3 跳过**;`compare_junit` 新增失败 **0**;消失 28 例=预期(13 speech_asr + 11 transcript 死函数用例 + 3 节点改名 + 1 transcript 校验用例);新增 5 例(3 改名 + 2 负向);跳过数与基线持平。
  - 阶段 5 后:**1447 例,1444 通过,0 失败,3 跳过**;新增失败 **0**;消失 4 例=预期(ASR 契约用例);新增 0。
- **全量集成套件**(基线=51ca0d1 冻结 XML,127 例 8 存量失败):阶段 6 复跑 **128 例,117 通过,8 失败,3 跳过**;`compare_junit` **新增失败 0、消失用例 0**、新增 1 例(阶段 4 加的 `test_assembly_visual_observe_chain_wiring` 正断言);8 个失败与基线**逐字一致**(wiring×5 + reorder tail_chain + auto_mode split_shots + sequential,均为存量环境/LLM 问题,与 ASR 无关)。
- **环境插曲(如实记录)**:阶段 6 首轮集成出现 8 例 `Postgres 不可达` 失败——机器重启后 Docker 容器 `edb-postgres16`(唯一 PG 后端)未自启,属环境问题(fixture 阶段 `pytest.fail`,未触及任何项目代码;`postgres` 标记 6 例自检转 skip 佐证)。`docker start edb-postgres16` 拉起后复跑,结果如上,零新增失败。
- **DoD 运行验证(4 次,stub LLM 后端,assembly 走 fallback_to_placeholder)**:
  1. assembly 单视频:`scripts/assembly/phase5_e2e.py --case happy` → `all_ok=True`(5 节点 16.4s,7 产物齐、preview 可播放、report 可读、qc=pass)。
  2. assembly 多视频(media_0001.mov 69s + media_0002.mov 34s,`video_input_paths` 两路):`source_count=2`、`per_source=2`、9 sheet/206 帧合并、逐视频 `video_ingest_0/1.json` 齐、timeline/report 产出、`qc=pass_with_warnings`。
  3. storyline 带音轨(media_15s_audio.mp4,截自 media_0001):`storyline_split_shots → storyline_understand_clips`(静态主边),无任何 local_asr/speech_rough_cut,assembly 链推进至关卡① interrupt。
  4. storyline 无音轨(inputs/30s.mp4,无音轨):同上,执行顺序与带音轨完全一致(音轨不再影响图拓扑)。
- **旧 checkpoint 盘点**(只读查询 `video_workflow` 库):`workflow_runs` 仅 1 行非终态 `video-wiring-001`(2026-10-29 联调残留,video_name='x',0 事件,**非真实在途任务**);`checkpoints` 表 233 个历史 thread_id(多为已完成任务);无等待人工关卡的真实线程 → §6 所述"发布前完成/终止存量任务"运维项确认无待办,验证一律新建任务。
- **残留扫描**(计划 §7.1 两条):生产代码无可执行 ASR import/注册/开关;剩余命中全部在白名单——删除说明注释(config.py/.env.example/graph.py)、DEPRECATED state 注释、节点/docstring 的 B1 说明、测试负向断言与旧线程兼容播种(week4/week5/state_field/postgres_resume/reorder)、`docs/分析/` 历史快照。`web/node_map/workflow.json` 为旧 14 节点子流程图,无 ASR 内容,无需更新。
- **依赖确认**:`httpx` 仍被 `jy_common/tts_client.py` 使用,依赖声明未动。
- **执行环境**:Windows + `.venv`(Python 3.x)、ffmpeg/ffprobe 9.0.2、PG=Docker `edb-postgres16`、LLM=stub(未配置 mcode-cli 后端,storyline smoke 与 assembly fallback 路径均为设计内桩路径);基线与对比 XML/日志存档于 `outputs/b1-p46-baseline/`(git 忽略区)。

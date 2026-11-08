# auto-video-editor 剪映草稿效果缺失:阶段 0 运行基线与证据冻结记录

> 项目:`wayyet/auto-video-editor` 分支 `main`
> 冻结对象:thread `console-d103e894da2b`
> 代码基线:`ca885e4f216116b4ea38672c8bd7b800764f90f5`(2026-11-08,与根因分析文档一致)
> 冻结日期:2026-11-09
> 性质:只读取证,未恢复工作流、未修改任务目录与草稿
> 上游文档:《auto-video-editor_剪映草稿效果缺失根因分析与修复设计执行计划_20261108.md》§8 阶段 0

---

## 1. 冻结产物位置

| 产物 | 路径 | 说明 |
|---|---|---|
| 取证报告 | `outputs/baseline-console-d103e894da2b/report.md` / `report.json` | `scripts/freeze_run_baseline.py` 产出 |
| checkpoint state 全量 | `outputs/baseline-console-d103e894da2b/state_values.json` | 只读 `aget_state`,47 个 state 键 |
| 挂起关卡核查 | `outputs/baseline-console-d103e894da2b/resume_dry_run.txt` | `resume_workflow.py --dry-run`,退出码 4 |
| 测试基线 | `outputs/baseline-console-d103e894da2b/p0-unit.xml` / `p0-integration.xml`(+ `.log`) | 后续阶段对照"零新增失败" |
| 证据备份 | `outputs/baseline-console-d103e894da2b/backup/`(约 60MB) | console_run.*、manifest、initial_state、storyline/、assembly/、drafts/ 等;排除 `inputs/`、`normalized_sources/` 大媒体 |

新增脚本:`scripts/freeze_run_baseline.py`(只读;`--dump-state` 走 SelectorEventLoop,与 resume_workflow 同口径)。

## 2. 运行状态事实(修正根因分析文档的时点假设)

根因分析文档写作时(2026-11-08 晚)该 run 停在关卡①;**冻结时(2026-11-09)该 run 已于 2026-11-08 22:50 前后恢复并跑完全程**:

- `resume_workflow --dry-run` → `[resume] 当前无挂起人工关卡`(退出码 4),`interrupts: []`;
- `status_log` 尾链:`checkpoint1_resumed → node_08 → fork → node_10 → … → node_17 → node_09 → node_11 → checkpoint2_resumed → node_12_bgm_mirrored_en_done:1 → node_13_adjust_volume_no_audio_track → join_before_delivery_done → delivery_qa_issues:14 → node_18_export_skipped:disabled`;
- `final_export_status = skipped`(导出默认关闭,符合预期);
- 英文分支草稿已建:`en_branch_console-d103e894da2b_20261108_225047_811415`(已注册)。

**结论:本次 run 是"跑完但交付 QA 不通过"的完整样本,恰好实证了根因文档的核心判断——即使全流程跑完,草稿仍然缺效果。**

## 3. 上游断裂证据链(旁白为空)

| 证据 | 文件 | 事实 |
|---|---|---|
| 旁白为空 | `storyline/script.json` | `group_scripts` ×1,`narration` 为空(mcode-cli 当次调用失败被静默吞,见根因分析) |
| TTS 空转 | `storyline/voiceover.json` | `voiceover_0001` `duration=0` `error="empty narration text"`;`provider=minimax`(配置名)、`stub_count=0`、`method=default_params` |
| 时间线无字幕 | `storyline/timeline_plan.json` | `subtitles.zh = null`;`audio.voiceover = null`;`bgm_ref = bgm_upbeat_energetic`;`method = beat_align`(无 TTS → BGM 节拍对齐,`qa_done: actual_ms=29610 target_ms=30000`) |
| 草稿无文本/配音 | staging/官方 `draft_content.json` | `materials.texts = 0`;staging 无 audio 轨(见 §4) |

推论:**即使阶段 1/2 代码落地,本 thread 仍会正确走"空输入跳过"路径**(node_08 无旁白可写、node_17 无英文字幕可合成);真实效果验收必须用旁白正常的新 thread。

## 4. 双草稿结构证据(SHA-256 + 计数)

### 4.1 staging(`outputs/console-d103e894da2b/drafts/`)

- sha256(`draft_content.json`)= `d0f739002578a900b587544ee0a64afe6a1fad82779aba9ab841275a8aeb4688`,(`draft_info.json` 同值,双文件字节一致);
- 轨道:video ×1(3 段);无 audio/text 轨;`materials`:videos 3、texts 0、audios 0、transitions 0、video_effects 0、stickers 0;
- `duration_us = 29610000`;双文件引用完整性校验 0 问题——**初始毛坯(3 裸片段)与预期一致**。

### 4.2 官方草稿(`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\AutoVideo_console-d103e894da2b`)

- sha256(`draft_content.json`)= `513bd17a500daa69ce3782f08f1ded2247d2e57e8332a0df8d1f89903668a89e`,(`draft_info.json`)= `978c80241661a24b2c905ce06a4679759436534094a4f80ee3ae6d1b50eb1dc0`;
- 轨道:video ×1(3 段)+ **audio ×1(1 段)**;`materials`:videos 3、**audios 1、beats 1**、texts 0、transitions 0、video_effects 0、stickers 0;
- audio 素材 = **人工在剪映内添加的 VIP 音乐 `Island Sway 3`**(会员热榜,路径在剪映 Cache `...\Cache\music\d46457f8….mp3`),段铺满 0→29.633s;
- `duration_us = 29633333`(≠ staging 29610000,剪映保存重算);
- 完整性问题 ×1:`draft_content.json 与 draft_info.json 内容不一致`(剪映端人工保存所致——我方写入恒字节一致)。

### 4.3 交付 QA 实录(`state.join_qa_report`,`passed=false`)

1. `[中文双文件] draft_content.json 与 draft_info.json 内容不一致`(剪映人工保存);
2-5. `[英文素材引用] track#1(audio) segment#0 extra_material_ref=… 悬空` ×4(镜像把 zh 段的 companion 引用带到 en,但未镜像对应素材——**镜像完整性缺陷,属阶段 5 范围,本三阶段不修**);
另 error_log 含:中文/英文音量未写入、英文转场/视频特效/贴纸缺失。

## 5. 节点已执行/未执行事实清单

**已执行**(status_log 全量):storyline 全链(load_media→split_shots→understand→filter→group→generate_script→recommend_transition→generate_voiceover→recommend_text→script_template→select_bgm→generate_ai_transition→plan_timeline_pro→qa→join)、`draft.published`(官方草稿库)、assembly 全链(discover→visual_observe→build_timeline→validate_render_qc→write_report)、checkpoint1 恢复、node_07(隐含,snapshot2 已生成)、node_08、fork、node_10/10b、node_14/15、node_16a(skip)、node_08b(skip)、node_17、node_09/09_en、node_11/11_en、checkpoint2 恢复、node_12、node_13、join QA、node_18(skip)。

**关键"done 但空跑"事实**(与根因分析一一对应):

| 节点 | done 标签 | 实际草稿效果 |
|---|---|---|
| node_08 | done | 草稿 0 texts(样式只算未落,scout 建议仅记 status_log) |
| node_10/10b | done | texts=0 → 无可修改对象 |
| node_09/09_en | done | transitions=0、video_effects=0(资源/边界未命中;归因属阶段 3) |
| node_11/11_en | done | stickers=0(无字幕可匹配) |
| node_16a | skipped_no_source_subtitles | `asr_segments_zh` 恒空(B1)→ 英文链断 |
| node_08b | skipped_no_en_segments | 无英文段 |
| node_17 | done | `empty_text`,未写英文配音轨 |
| node_12 | mirrored_en_done:1 | 镜像了人工音乐轨(1 条),但带出 4 个悬空 companion 引用 |
| node_13 | no_audio_track | 无命名匹配段(`audio_main`/`audio_bgm`),音量未写 |

## 6. 测试基线

- `tests/unit`:**1488 例,0 失败,0 错误,3 跳过**(86.8s);
- `tests/integration`:**128 例,8 存量失败,0 错误,3 跳过**(317s)。存量失败清单:
  `test_auto_mode_end_to_end::test_load_media_then_split_shots_then_qa_gate_chain`、
  `test_reorder_topology::test_tail_chain_applies_to_both_drafts`、
  `test_sequential_integration::test_sequential_produces_complete_draft`、
  `test_unreachable_nodes_wiring` ×5(golden trace / qa 重试计数类)。
- **环境注意事项**:首次集成跑出 32 失败,其中 24 例是"剪映客户端正在运行"被
  `JY_WRITE_FAIL_ON_JIANYING_RUNNING` fail-closed 拒写(本机剪映开着)——按错误
  提示以 `JY_WRITE_FAIL_ON_JIANYING_RUNNING=0` 重跑得到真实基线 8 例。后续阶段
  1/2 集成验收也须带此标志(或关闭剪映),对照 `scripts/compare_junit.py` 零新增失败。
- PG 容器 `edb-postgres16` 运行中;checkpoints 走 Postgres。

## 7. 对阶段 1/2 的直接输入

1. **node_08 配音轨重写必须带删除守卫**:官方草稿已有人工 VIP 音乐轨(剪映 Cache 路径)——重写只能动"我方产物"轨(name=`Voiceover_zh` 或素材路径全部位于本次运行 storyline 输出目录内),绝不误删人工音乐。
2. **check_dual_files 对人工编辑稿过严**:剪映保存后 content/info 不再字节一致,join QA 报 error 属预期外噪音;阶段 1/2 的完整性校验需区分"我方写入后回读一致"与"人工编辑后放松"两种口径(本三阶段先不放宽,记录差异即可)。
3. **英文镜像悬空 companion 引用**是真实缺陷,登记为阶段 5 输入(不在本次范围)。
4. 本 thread 是"空输入"样本:阶段 1/2 落地后,对该 thread 复验只验证 skip 路径;正路径验收须新 thread(旁白正常 + FireRed :8010)。

## 8. 遗留与偏差

- 根因分析文档 §3 假设"停在关卡①"已被本次冻结修正(§2);不影响阶段 1/2 的结构性修复设计。
- `delivery_qa_issues:14`(status_log)与 `join_qa_report.issues ×5` 口径不同(前者含 checks 计数),无实质影响。
- 备份位于 `outputs/`(gitignore),原始证据不进 git;本记录文档 + report 摘要入库。

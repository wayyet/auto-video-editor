# 第六期 P1-0~P1-6 分阶段执行记录(assembly 前移为剪映草稿内容生产链)

> **文档类型**:执行记录 / 验收对照
> **日期**:2026-11-15
> **依据**:《auto-video-editor_assembly草稿影响重构设计与分阶段执行计划_20261115》(本目录)
> **代码基线**:main@bbf7306(与设计文档基线一致)
> **执行范围**:P1-0 ~ P1-6 共 7 个阶段(用户确认);P1-7 / P1-8 / P2 不在本次范围

---

## 0. 执行摘要

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1-0 | 基线与防回归(fixture/helper/influence_trace 契约 + 全量基线) | ✅ 完成 |
| P1-1 | 拓扑前移(storyline_join → assembly → generate_draft,SCHEMA 10) | ✅ 完成 |
| P1-2 | 素材 manifest 与多素材 sidecar(F-02/F-11/F-12 根治) | ✅ 完成 |
| P1-3 | 视觉证据进入编辑决策(F-04 根治 + 选段 prompt 空模板存量 bug 根治) | ✅ 完成 |
| P1-4 | DraftPatch 与候选 CanonicalTimeline(F-03/F-05 根治 + 多素材选段索引塌缩存量 bug 根治) | ✅ 完成 |
| P1-5 | QC 验证候选映射草稿(F-06 根治) | ✅ 完成 |
| P1-6 | 真实 Timeline Repair Patch(F-07 根治 + §4.4 fail-closed 路由) | ✅ 完成 |

**提交序列**(每阶段一笔;P1-3/P1-4 因深度共享 build_timeline.py / node_build_timeline.py / state.py 合并为一笔,阶段边界在提交说明中保留):

| 提交 | 阶段 |
|---|---|
| 185b437 docs | 设计文档入库 |
| f52d047 test | P1-0 基线与防回归 |
| 3992552 refactor | P1-1 拓扑前移 |
| 205f264 feat | P1-2 素材 manifest |
| (合并一笔) feat | P1-3 视觉证据 + P1-4 DraftPatch |
| (最后一笔) feat | P1-5 候选草稿 QC + P1-6 真实修复 |

---

## 1. 全量基线(P1-0 记录)

- **unit**:2048 collected,exit 0 全绿(汇总行被 `-q` 吞为已知现象,以退出码为准)
- **integration**:3 个失败,均为已知存量(`test_auto_mode_end_to_end.py::test_load_media_then_split_shots_then_qa_gate_chain`、`test_reorder_topology.py::test_tail_chain_applies_to_both_drafts`、`test_sequential_integration.py::test_sequential_produces_complete_draft`,同因"字幕应被注入"),与记忆基线 full-test-baseline-drift-20261113 一致
- **判定新失败的方法**:与上述基线对照;环境性失败用 worktree/stash 对照

## 2. 各阶段落点(文件级)

### P1-0(新增为主,产品代码零改动)
- `tests/fixtures/storyline/canonical_timeline_sample.json`:3 clip / 双源 / 两组配音 / zh+en 字幕 / 转场语义
- `tests/support/draft_influence.py`:视频/音轨摘要、逐段对比、引用完整性(缺失/悬空/重复)统一断言入口
- `assembly_capabilities/influence_trace.py`:canonical_json_sha256 唯一口径 + InfluenceTrace 契约 + load/save helper

### P1-1 拓扑前移
- `graph.py`:`_route_after_storyline_join` QC 开→assembly_discover、关→generate_draft、无 plan→END;接线块删 `generate_draft→assembly` 旧边,`assembly_write_report→generate_draft` 交棒,`generate_draft→node_06` 两态直连;repair 回环保留;`assembly_failed→END` 映射(P1-6 补入)
- `monitoring/workflow_graph.py`:SCHEMA_VERSION 9→10;_EDGE_LABELS 五条更新;六节点职责描述更新
- 测试:test_workflow_graph_schema / test_unreachable_nodes_wiring(NEIGHBORS 补六节点行)/ test_assembly_qc_graph / test_storyline_topology_baseline(JOIN 条件出口含 assembly 目标)

### P1-2 素材 manifest
- `assembly_capabilities/media_probe.py`:`compute_asset_id`(规范化路径+size+mtime sha256 前 16 位);analyze_media probe sidecar 按 asset_id 命名
- `nodes/assembly/node_discover_and_probe.py` 重写:plan 主导素材发现(file_uri_to_path);media.json 升级 manifest v1(sources[] 完整 candidate_segments/black/silence/切点 + base_plan_sha256);[ERROR]+必需字段校验;部分失败 degraded / 全失败节点失败
- 消费端适配:`build_timeline.manifest_to_media_reports` 归一;write_report 摘要兼容
- `state.py`:assembly_base_plan_sha256

### P1-3 视觉证据
- `assembly_capabilities/visual_observe.py`:新增 `build_candidate_observation`(每 candidate_id 独立抽帧,复用 build_video_observation 缓存签名;black_suspect 旗标 = manifest black_ranges 确定性重叠;degraded/unknown 显式)
- `nodes/assembly/node_visual_observe.py` 重写:候选级观察;`assembly_observation_artifact` 新字段(ingest_artifact 兼容同路径);旧 list manifest 显式拒绝
- `assembly_capabilities/build_timeline.py`:attach_visual_evidence;选段 prompt 注入候选自己的拼贴图;media 参数按候选 sheet(不再全片前 8 张);fallback 黑帧降权排序;clip 带 selection_basis/evidence_refs
- **存量 bug 根治**:选段模板在 `assembly_capabilities/prompts/` 而 PROMPTS_ROOT 指向 `prompts/tasks` → `render_prompt` 一直返回空串(选段 prompt 静默为空);现以 `prompts_root=_PROMPTS_ROOT` 修正
- 测试:tests/unit/assembly_capabilities/test_visual_observation_influence.py(9 例)

### P1-4 DraftPatch
- `assembly_capabilities/draft_patch.py`(新增):select/trim/reorder/remove/restore 五类 op;apply_patch(base sha 绑定+内容漂移拒绝+幂等+preserve_and_remap 重排);diff_plans(逐 clip 可解释,证据回填);strip_internal_marks
- `assembly_capabilities/build_timeline.py`:`build_candidate_plan_from_media`(确定性黑帧 trim/remove ops + LLM select ops + 硬门 + no_safe_candidate 语义,不再整段拼接);`candidate_projection_timeline`(timeline.json = 候选投影)
- `nodes/assembly/node_build_timeline.py` 重写:P1-4 主路径(candidate_plan/draft_patch/timeline.json/influence trace) + 无基线回落旧路径
- `state.py`:assembly_candidate_plan / candidate_plan_path / candidate_plan_sha256 / draft_patch
- **存量 bug 根治**:多素材候选 index 按 media 局部编号,`_normalize_selection` 的 by_index dict 跨素材塌缩(后者覆盖前者)→ 引入全候选空间唯一 `selection_index`
- 测试:test_draft_patch.py(16 例)+ test_canonical_timeline_patch.py(11 例)

### P1-5 候选草稿 QC
- `assembly_capabilities/draft_preview_adapter.py`(新增):候选 → canonical_to_draft + storyline_to_native(与正式链同一映射)→ qc_draft/ 隔离目录原子双写(不走 safe_write_draft,不触碰剪映库);draft_video_binding(按值核对 segment↔clip);audio binding(BGM 终点/配音组区间,跑 mapper 输出草稿——native 合法丢弃音乐库 BGM ref 不算错);validate_candidate_draft(typed issues:missing_segment/extra_segment/broken_reference/track_desync/missing_asset/invalid_range)
- `nodes/assembly/node_validate_render_qc.py` 重写:候选 QC 主路径 + 旧路径兜底;assembly_structural_error 标记
- 测试:test_candidate_draft_qc.py(12 例)

### P1-6 真实修复
- `assembly_capabilities/repair_strategies.py`(新增):typed issue → 策略(preview 坏窗 trim 避开/remove 坏片段;invalid_range 夹回;structural → restore 基线 clip / remove;无策略 → manual);STRUCTURAL_CODES;patch_fingerprint 去重
- `nodes/assembly/node_repair_loop.py` 重写:候选修复主路径(typed issues → patch → apply → 硬门 → candidate_plan/repair_patch/repair_history 落盘,同指纹不重放,repair_noop 显式)+ 旧 marker 路径兜底 + 视觉证据旁路保留
- `route_after_assembly_qc`:escalated+达上限+结构性 error → `assembly_failed` → END(§4.4 fail-closed,取代旧 ADR-3 软降级;warning-only 仍带警告继续)
- 测试:test_repair_strategies.py(15 例)

## 3. 验收对照(设计文档 P1-0~P1-6 复选框)

### P1-0
- [x] 原始 `storyline_plan → mapper → draft_content` 路径通过基线测试
- [x] 无改动 plan 复跑 mapper 结构摘要不变(逐字节一致)
- [x] 同一素材计划重复执行 mapper 结果确定
- [x] 引用完整性检查识别缺失素材 ID / 重复 ID / 悬空引用
- [x] 样本覆盖 zh/en 音轨字段(voiceovers[].provider/stub/path/duration_ms)

### P1-1
- [x] QC 开轨迹:storyline_join → discover → observe → build → validate → write_report → generate_draft → node_06
- [x] 修复轨迹:validate → repair_loop → validate → write_report → generate_draft
- [x] QC 关轨迹:storyline_join → generate_draft → node_06(assembly 不执行)
- [x] storyline_join 返回 END 不触发 assembly(golden trace + JOIN 条件出口断言)
- [x] 不存在 generate_draft→assembly_* 旧边 / write_report→node_06 旧边
- [x] 全部注册节点可达、均有出边(可达性守卫 + 零出边守卫,允许集为空)

### P1-2
- [x] 同名 clip.mp4 两个目录:asset_id 不同,产物互不覆盖
- [x] 三素材连续探测:每个素材候选明细都存在
- [x] media.json(manifest)可完整还原各素材候选区间
- [x] [ERROR]/损坏/缺 ffmpeg 不标成功(probe_status/analysis_status=failed + degraded_reason)
- [x] 单素材失败 → degraded tag 不静默丢失;全失败 → 节点失败
- [x] manifest 候选映射回 plan clip(plan_clip_ids)

### P1-3
- [x] 两种视觉 fixture → 不同候选排序(fallback 黑帧降权)
- [x] 每个选段 clip 有 evidence ref 或标 deterministic_fallback
- [x] 缓存命中/未命中同契约(cache_signature / cache_reused)
- [x] 签名变化(区间)重抽
- [x] build_timeline 实际读取观察(prompt 注入 + evidence_refs,非死产物)

### P1-4
- [x] patch 改变候选 clip 集合/顺序/入出点
- [x] diff 可追溯 candidate_id + evidence refs
- [x] 原始 storyline_plan 不被覆盖(apply 只动深拷贝,有测试钉死)
- [x] 无候选/模型失败/非法索引 → 保留基线 + no_safe_candidate,不虚构素材
- [x] 同输入同模型输出同规则 → 结果确定可重复(operation_id 确定性)
- [x] base_plan_sha256 不匹配拒绝应用(绑定+内容漂移双层)
- [x] BGM/voiceover/transition 字段保留(关联轨由 mapper 从 clips 重推导)

### P1-5
- [x] QC 检查候选计划映射后的临时 Jianying draft(同 mapper/native 链)
- [x] 临时草稿 video track 与候选 clip 顺序/时间范围一致(按值核对)
- [x] 删素材/破坏 ref/非法区间 → QC error
- [x] clip 时长变化 → 配音/BGM 关联轨道错位被发现
- [x] QC 不修改 draft_path/剪映草稿库/原始 plan(隔离目录 + 测试钉死)
- [x] issue 定位到 clip_id/segment_id/时间区间 + typed code

### P1-6
- [x] 黑帧 issue → 源区间调整/坏片段移除/操作撤销,非 metadata
- [x] 悬空引用 → restore/remove 修复或阻止
- [x] track_desync → 恢复基线 clip
- [x] 修复后复检按真实 QC 重算(repair 改写候选 → validate 复检新候选)
- [x] 前后 sha 不同 + diff 可解释;无变化 → repair_noop
- [x] 同 patch 指纹不重放
- [x] 结构性 error 达上限 fail-closed(assembly_failed → END)

## 4. 全量验证(P1-6 完成后统一跑)

- unit:`2048+新增(20 P1-0 + 15 P1-2 + 9 P1-3 + 16+11 P1-4 + 12 P1-5 + 15 P1-6 + observe/node 存量改写)≈ 2160+`,结果见文末补充
- integration:与基线相同的 3 个存量失败,零新增
- (最终数字以本轮运行输出为准,见 §6)

## 5. 中间态说明(重要)

P1-7 / P1-8 不在本次范围。当前系统状态:
- 六节点在 generate_draft **之前**运行,QC 对象是"候选计划映射后的临时草稿"(与正式链同一 mapper/native);
- `generate_draft` 仍消费**原始** storyline_plan(`assembly_final_plan` 的提交与消费属 P1-7);
- `assembly_structural_error` fail-closed 已生效:结构坏掉的候选会在达重试上限后终止任务,不进关卡①;
- influence_trace 已有 build 节点 contribution(P1-4 起),discover/observe/validate/repair 的 trace 写入与 generate_draft 的最终消费在 P1-7 接通。

## 6. 最终验证结果(P1-6 完成后统一实测)

- **unit**:2124 collected,exit 0 **全绿**(基线 2048 + 新增 76:20 P1-0 + 15 P1-2 + 9 P1-3 + 16+11 P1-4 + 12 P1-5 + 15 P1-6,余为存量改写产生的净增)
- **integration**:149 collected,**恰好 3 个失败 = 已知存量**(auto_e2e / reorder tail_chain / sequential,同因"字幕应被注入"),与 P1-0 基线逐字一致,**零新增失败**
- 执行中修复的两处自伤(均已回归绿):
  1. phase5_e2e 产物清单仍期望 `video_ingest.json` → 改为 `visual_observation.json`(P1-3 产物更名);
  2. 种子草稿流(plan 引用虚构素材路径)被 `check_material_paths` 的 missing_asset error 误杀成 fail-closed → 文件存在性降为 warning(discover 层已记录 degraded),结构完整性由引用闭合 + 契约硬门 + binding 把守。
- 执行中发现并根治的**三个存量 bug**(与本次重构无关的既有缺陷):
  1. 选段 prompt 空串:模板在 `assembly_capabilities/prompts/` 而 PROMPTS_ROOT 指向 `prompts/tasks`,render_prompt 一直返回空(P1-3 修正 prompts_root);
  2. 多素材选段索引塌缩:候选 index 按 media 局部编号,`_normalize_selection` 的 dict 跨素材覆盖(P1-4 引入全候选空间 selection_index);
  3. 上述两 bug 使多素材 LLM 选段从不确定/无提示,是 F-10"llm_used 语义宽松"观察的深层原因之一(P2-1 诊断改造时顺带收益)。

## 7. 真机验收建议(操作员)

1. 管理员 pwsh7 重启服务(新拓扑 + SCHEMA 10 生效,监控页节点图缓存自动失效);
2. 新建任务跑 QC-on 链路:监控页确认 storyline_join → 六节点 → generate_draft → 关卡① 顺序;
3. 检查 `outputs/<job>/assembly/` 下产物:media.json(manifest)、visual_observation.json、candidate_plan.json、draft_patch.json、qc_draft/(双文件)、timeline.json(投影)、candidate_qc_report.json、preview.mp4、report.md;
4. 打开 qc_draft/draft_content.json 用剪映"导入草稿"验证可加载(临时草稿与正式草稿同链生成);
5. `ASSEMBLY_QC_GATE_ENABLED=false` 跑一条验证应急路径(storyline_join 直连 generate_draft)。

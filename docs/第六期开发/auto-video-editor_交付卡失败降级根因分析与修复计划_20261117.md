# 交付卡失败与降级根因分析及修复计划(20261117)

> 分析对象:创建台任务 `console-553e3e7b34f7`(运行 `run-b99cb547e0a7`,2026-11-16 15:36 启动,已完成,42 节点成功/0 失败)。
> 触发场景:交付状态卡出现 **Assembly QC「降级(final:not_available)」**、**效果落盘「失败 1」**、**交付 QA「降级(2 个问题)」**。
> 结论:3 个真实缺陷 + 2 项预期行为(另有审查补充的第 4 缺陷,见 §3.4)。
>
> **文档状态(20261117 修订)**:本文初版经 `auto-video-editor_交付卡降级根因审查与修复执行计划_20261117.md`(下称《审查文档》)静态审查,三根因判定成立,但存在 D01(repair strategy speed 假设遗漏)等 12 项修订要求;本文已按审查 §5 修订,修复已按审查 §6 的 P0/P1/P2 分批实施,**执行记录见本文 §5.2**。§2 的任务运行证据来自本地任务目录 `outputs/console-553e3e7b34f7/` 的直接读取(《审查文档》基于 GitHub 远端源码,未读取这些运行产物)。

---

## 1. 结论总览

| 交付卡行 | 显示 | 定性 | 根因 |
|---|---|---|---|
| Assembly QC | 降级 chip + `final:not_available` | 缺陷① + 缺陷② | ①投影数据自相矛盾 → QC 校验误报;②final 显示时序 bug(纯显示) |
| 效果落盘 | 失败 1(红 chip) | 缺陷③ | 贴纸未启用属预期,却被无条件尾链检查报 error |
| 交付 QA | 降级·发现 2 个问题 | 缺陷③(同源) | 同上,zh/en 两份草稿各 1 条贴纸误报 |
| MP4 导出 | 跳过 | **预期行为** | `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=0` 默认关闭 |
| 草稿时长 12.467s ≠ 目标 35s | — | **预期行为** | `timeline_plan.json` `method="tts_align"`,槽位跟随配音时长(见 §4.2) |

「草稿生成/草稿发布」两行成功与上述降级**不矛盾**:QC 属观测层,final plan 实际正常提交;但缺陷①并非"无影响"——候选预览恒不生成,关卡①人工复核缺预览依据(详见 §3.1 末的 D03 修订口径)。

---

## 2. 任务数据快照(证据)

- 任务产物:`outputs/console-553e3e7b34f7/`
  - `assembly/candidate_qc_report.json`:`status=escalated`,`preview_render_ok=false`,`preview_qc_report=null`;issues = 6 warning + 2 error(全部 `code=preview_issue`)
  - `assembly/timeline.json`:`version="assembly-candidate-projection-1"`,`sequence.duration=12.467s`,4 clips
  - `assembly/repair_history.json`:2 轮,全部 noop,`noop_reason="no repairable issues(全部 manual / 无 error)"`
  - `assembly/report.md`:§7「✅ 已提交」final_plan.json(sha `9ed12c11…`,与候选计划相同,patch 0 ops)
  - `drafts/draft_info.json`:`duration=12467000`(µs,即 12.467s);`draft_content.json` 两份草稿 `"stickers": []`
  - `initial_state.json`:`"stickers": false`(required_features 未勾贴纸)
- timeline.json 的 clip_0001 实测字段(自相矛盾的关键证据):

```json
{
  "start": 0.0,          "end": 15.442,   "duration": 15.442,
  "timeline_start": 0.0, "timeline_end": 1.351,
  "speed": 1.0
}
```

  素材原长 15.442s 被压进 1.351s 槽位(隐含 ≈11.4 倍变速),但 `speed` 写死 1.0。

---

## 3. 缺陷分析

### 3.1 缺陷①:Assembly QC 恒降级 —— 投影数据自相矛盾导致校验全盘误报

**因果链(每步有实证):**

1. **投影写错**。候选计划 → `timeline.json` 投影发生在 `assembly_capabilities/build_timeline.py:1341-1360`:每个 clip 的 `start/end/duration` 写**素材原区间**、`timeline_start/timeline_end` 写真实目标槽位、`speed` **硬编码 1.0**。三个字段互相矛盾——隐含变速(本次 11.4×/11.4×/20.7×/2.03×)没有体现。
2. **校验误报**。投影校验 `assembly_capabilities/timeline_ops.py` 的 `check_asset_references_and_track_layout`(:180)用 `clip_render_duration`(:268)算渲染时长 = `(end−start)/speed` → 把素材原长(15.442/15.441/27.935/17.118s)当成时间线占位 → 同轨两两"重叠" → **6 条 warning**(与交付卡「警告 6」吻合);`content_end=30.637s > sequence.duration=12.467s` → **1 条 error**("sequence.duration 12.467s is shorter than timeline content ending at 30.637s")。
3. **预览被卡死**。`assembly_capabilities/render_preview.py:75` 渲染前先跑校验,不过 → 第 2 条 error `[ERROR] timeline validation failed before render` → **preview.mp4 未生成**(report.md §1「(未生成)」、§5 无 QC 报告)。这就是关卡①看不到候选预览的直接原因。
4. **QC 升级 + 修复空转**。有 error → `qc_status="escalated"`(`nodes/assembly/node_validate_render_qc.py:49`);修复循环把 `preview_issue` 归为 manual(不可自动修)→ 两轮全 noop(`repair_history.json` 实证)→ 仍 escalated。
5. **前端映射为降级**。`web/static/delivery_summary.mjs` 的 `reduceDeliveryCard`:`st === "escalated"` → `state="warn"` → 「降级」chip。

**实质影响(按审查 D03 修订):** 该缺陷**没有阻断** P1-7 最终计划提交,也没有直接阻断后续正式草稿生成(P1-7 提交判据只拦结构性 error,`preview_issue` 非结构性,不在 `STRUCTURAL_CODES`;final plan 正常提交,sha 与候选一致、patch 0 ops,generate_draft 正常消费 → 「草稿生成 成功」)。**但是**候选预览无法生成,导致关卡①缺少预览复核依据,Assembly QC 无法完成预期的视觉质量验证(qc_preview 的黑帧/静音/卡帧检查全部落空)——它不属于正式草稿生成阻断故障,但属于**影响人工复核和交付质量保障的实际缺陷**。

**关键放大效应(按审查 D02 收敛表述):** tts_align 链的槽位通常小于素材原长(变速压缩是常态)。对**源素材区间与目标时间轴槽位不一致**的候选 clip,固定 speed=1.0 会导致时长计算错误;当该错误进一步触发同轨重叠或 sequence 时长越界时,候选预览会被阻断。该问题具有**系统性复现风险**,但不能仅凭单次任务断言所有运行都会失败(候选恰为 1:1 投影时不触发)。

**修复要点:** 投影处补写隐含速度 `speed = (end−start)/(timeline_end−timeline_start)`。渲染工具链本就支持 speed(`render_preview.py:314` render_duration=source/speed、`:496/:552` atempo/setpts),修一处即全链自洽。

### 3.2 缺陷②:`final:not_available` 是恒定的显示 bug(与本次运行无关)

- `nodes/assembly/node_write_report.py:660` 在节点 **return 之前**调 `_emit_assembly_qc_summary(state)`;此时含 `assembly_final_plan_path` 的返回值 `out` 还没合入 state(LangGraph 在节点结束后才合并)。
- `assembly_final_status` 这个键**全代码库从未赋值**(grep 只有读取,无写入)。
- 于是 `_emit_assembly_qc_summary` 里的兜底 `state.get("assembly_final_status") or ("submitted" if state.get("assembly_final_plan_path") else "not_available")` **永远落到 `not_available`** ——交付卡永远显示 `final:not_available`,哪怕本次实际已提交(report.md §7「✅ 已提交」+ `final_plan.json` 存在)。
- 性质:纯显示 bug(P2-4 交付卡事件时序),不影响提交逻辑本身。

### 3.3 缺陷③:效果落盘「失败 1」+ 交付 QA「2 个问题」—— 贴纸误报

**预期行为侧:**
- 创建台贴纸复选框禁用(贴纸真实 ID 未按 `sticker_template.json` 回填,功能暂不可用)且未勾选 → `required_features.stickers=false` → node_11 不挂贴纸 → zh/en 两份主草稿 `materials.stickers=[]`。**这是预期,不是链路故障。**

**误报侧(两层口径冲突):**
1. 尾链检查 `nodes/node_join_before_delivery.py:88 _tail_chain_structured` **无条件**检查「贴纸数=0 → error 级 issue」,zh/en 各报一条:`贴纸缺失(尾链 node_11 未对…生效?)`(该检查为 2026-11-07 阶段4 引入,当时无 required 概念)。
2. `draft_ops/draft_integrity.py:796 build_qa_categories` 的第 9 步**本有正确兜底**:「非 required 且无贴纸 → 类别=不适用(未要求贴纸)」,但其前提是 `_status_of("stickers") == CAT_PASSED` —— 第 1 步归桶时那 2 条 error 已把 stickers 类打成 `CAT_FAILED`,**兜底永不生效**。
3. 结果:stickers 类 `status=failed` → 效果落盘行 `failed=1` → 红 chip;2 条 issue 非必需 → `gate_failed=false` → 交付 QA 行「降级(非必做,不阻断)」。

**本质:** 2026-11-07 阶段4 的无条件尾链检查 与 阶段6 的 required-aware 兜底之间的口径冲突;病灶在**上游无条件产 issue**,类别聚合逻辑本身是对的。

**分类口径(按审查 D08 修订):** 三个类别不能等同对待——转场、贴纸在 `REQUIRED_FEATURE_KEYS`(`draft_ops/draft_integrity.py:607`)里,缺失是否报告由 `resolve_required_features()` 决定;**视频特效不在其中**(源码注释明示「加分效果,缺资源时降级不阻断交付」),其缺失永远不报,而已有特效的孤儿绑定仍必须报。即:不 required 只豁免「缺失」,不豁免「存在但结构错误」。

### 3.4 缺陷④(审查 D01 新增,初版遗漏):repair/patch 层仍假设 speed=1.0

投影修复后,预览 QC 将真正跑起来,repair 循环会进入**此前从未实际执行**的分支(此前 preview 恒不生成,`PREVIEW_CODES` 修复无窗口可修)。此时两处 speed=1.0 假设成为活性缺陷:

1. **`assembly_capabilities/repair_strategies.py:269`**(注释「候选 clip 速度恒 1.0,源偏移 = 时间轴偏移」):局部修复把坏窗时间轴边界按 1:1 直映回源区间。speed=2 时正确源起点应为 `0 + 600×2 = 1200ms`,旧算法得 600ms——**裁错源素材区间**。
2. **`assembly_capabilities/draft_patch.py:174`**(注释「trim 恒速度 1.0:时间轴跨度 = 新源跨度」,审查文档亦未列):trim 应用后时间轴跨度=新源跨度。被 trim 的 11.4× 压缩片段(如 tts_align 槽位)时间轴会膨胀 ≈11 倍,破坏总时长与配音对齐;`_relayout` 的注释同样声称 trim 后恒 1.0。

**修复纪律(与投影修复同一批次,不可拆分):** 换算统一用「源偏移 = 时间轴偏移 × 隐含速度(源跨度/槽位跨度)」;trim 应用保留 trim 前隐含速度(新槽位 = 新源跨度 ÷ 隐含速度);干净区是否「过短」按**时间轴口径**(观众所见)判定;select 新建片段维持 1.0、restore 本就保基线速度,均不动。已有正确参考实现:`storyline/mapper.py:144-150`(speed=源跨度/目标跨度,1e-6 归一 1.0)。

### 3.5 候选 QC 的双验证路径与错误双计(审查 D04/D05)

`nodes/assembly/node_validate_render_qc.py` 的候选 QC 不是只检查 timeline.json,实际流程:

```text
candidate_plan → write_candidate_draft(临时剪映草稿,qc_draft/ 隔离目录)
  → validate_candidate_draft(正式草稿映射/轨道/素材引用/关联音轨)
  → validate_timeline(timeline.json 预览投影)
  → render_preview(渲染前会再次调 validate_timeline)
  → qc_preview(黑帧/静音/卡帧)
```

两条验证对象不同:**修复 timeline 投影只恢复预览投影链路的一致性,临时剪映草稿与候选计划的映射检查(validate_candidate_draft)必须继续独立通过**(P1-5 mapper parity 用例锁定)。此外「渲染前校验失败」是 validate_timeline 同一根因的传导结果,曾被重复计为独立 error(QC 报告里 2 条 error 中 1 条即此)——已按审查 D05 降级为 warning 级同根因记录,错误数≈根因数。

---

## 4. 非故障项说明

### 4.1 MP4 导出「跳过」

`AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=0` 默认关闭,设计如此。

### 4.2 草稿 12.467s ≠ 目标 35s

`storyline/timeline_plan.json` 的 `method="tts_align"`:时间槽位跟随配音时长——本次旁白仅两句,group_0001 配音 4053ms(clips 1-3 各 1.351s)、group_0002 配音 8414ms(clip_0004 8.414s),合计 12.467s;35s 是上限 `options.max_duration_ms`。装配链(final_plan 与 timeline_plan 逐字段一致)只是忠实投影上游计划,不是本次故障。

---

## 5. 修复执行记录(已实施,2026-11-17)

按《审查文档》§6 的 P0/P1/P2 分批实施,共 6 个提交(main 分支,未推送,推送走 sync-github 流程):

| 提交 | 批次 | 内容 |
|---|---|---|
| `be39d19` | P0 | 投影/repair/patch 统一 speed 语义:P0-01/02 投影 speed 推导+防御;P0-03 sequence.duration 取最大 timeline_end;P0-04 repair_strategies 坏窗→源区间乘隐含速度;**P0-04b(审查外补充)draft_patch trim 保留 trim 前隐含速度**(§3.4 第 2 点,审查文档未列) |
| `bc2ac45` | P0-06 | 集成渲染测试:ffmpeg 合成素材 → 投影 → 校验零 error → 实渲 → ffprobe 实测预览时长≈计划(±0.15s),不 mock render_preview |
| `9e67dfd` | P1-A | assembly.qc 事件 final_status 取 `_submit_final_plan` 真实结果;节点返回值携带 `assembly_final_status`;`state.py` 增声明(D09) |
| `b8a80d8` | P1-B | 尾链缺失检查加 required 闸门(`skip_missing` keyword-only 参,K2=B 文本契约不变);transitions n/a 扩展「未要求转场」;video_effects 恒可选不报缺失(D08) |
| `9aafa19` | P2 | 渲染前校验失败降级 warning 同根因记录(D05);report.md §3/§5 口径注明(D04);assembly.qc 事件增 error_count,交付卡「错误 N」单列 |
| (本次) | docs | 本文档按审查 §5 修订 + 审查文档入库 |

**测试落点(D11/D12):**
- `tests/unit/assembly_capabilities/test_projection_speed.py`(新):§7.1 speed 表(1000/1000→1.0、2000/1000→2.0、15442/1351≈11.43、非法区间防御)、最大 duration、压缩槽位投影零 error/零假重叠、**mapper parity**(投影 speed 与 `canonical_to_draft` 同输入同值);
- `test_repair_strategies.py::TestSpeedAwareRepair`(新):§7.2 保尾/保头/3× 舍入/时间轴口径过短→remove/窗外→manual/1× 旧语义不回退;
- `test_draft_patch.py`:trim 保速(11.4× 片段 trim 后槽位 262ms 而非 3000ms);
- `test_node_join_before_delivery.py`:§7.5 组合(stickers true→failed+gate / false→n/a、transitions false 多镜头→n/a「未要求转场」、未 required 但未挂载仍报);
- `test_assembly_nodes.py`:final_status 三态 + 事件捕获(writer 异常不影响节点);
- `web/tests/delivery_summary.test.mjs`:final:submitted 并存口径 + 错误数单列;
- `tests/integration/test_assembly_preview_render.py`(新):真实渲染链路(ffmpeg 缺失 skip)。

### 明确不做(与审查一致)
- 不 bump SCHEMA_VERSION(timeline.json 为产物内部格式,节点图不变;state 仅增 NotRequired 字段);
- 不改正式草稿时间范围与 `storyline/mapper.py` 既有推导;不动 generate_draft 消费链;
- 旧式 1:1 路径 `_build_clips`(L655)自洽,不动;select 新建片段仍 1.0;restore 仍保基线速度;
- 意图重叠自动标注(P2-02)留档不实现:投影本就顺序落位,现阶段无意图重叠场景,以「压缩槽位投影零假重叠」单测固化语义。

---

## 6. 验证方案与结果

1. **定向单测(已跑,全绿)**:repair/draft_patch/canonical_timeline/timeline_ops/projection_speed 共 116 例;assembly_nodes 35 例;join/delivery_qa_repair 42 例;draft_integrity 29 例。
2. **前端(已跑,全绿)**:`node --test web/tests/delivery_summary.test.mjs` 14 例。
3. **集成渲染(已跑,通过)**:`tests/integration/test_assembly_preview_render.py` 真实 ffmpeg 渲染,预览时长≈计划。
4. **全量回归**:见提交后执行的全量 unit `pytest tests/unit -q`(exit 码为准)与集成基线(3 存量 F 不新踩)。
5. **真机验收(操作员,按《审查文档》§8 12 步)**:重跑创建台任务 →
   - Assembly QC 行不再因投影 speed 误报,preview.mp4 生成且时长≈计划,preview_qc_report.json 产出;
   - qc_draft 与正式草稿的源/目标/speed 语义一致,剪映播放无黑帧/停帧/错误裁切/音画错位;
   - 交付卡 `final:submitted`;不勾贴纸无失败、必需缺失仍阻断;
   - checkpoint 恢复与人工关卡行为不退化。

---

## 附:分析涉及的关键代码位置索引(含修复后状态)

| 位置 | 角色 | 修复 |
|---|---|---|
| `assembly_capabilities/build_timeline.py:1298 candidate_projection_timeline` | 候选→timeline.json 投影(缺陷①病灶) | ✅ speed 推导+防御+max duration |
| `assembly_capabilities/build_timeline.py:657 _build_clips` | 旧式 1:1 投影(自洽) | 不改 |
| `assembly_capabilities/timeline_ops.py:180/268` | 投影校验 + clip_render_duration(误报触发点) | 消费端不变,数据源已修 |
| `assembly_capabilities/repair_strategies.py:269` | 坏窗→源区间换算(缺陷④) | ✅ 乘隐含速度+时间轴口径过短判定 |
| `assembly_capabilities/draft_patch.py:174` | trim 应用语义(缺陷④,审查外补充) | ✅ 保留 trim 前隐含速度 |
| `assembly_capabilities/render_preview.py:75/314/496/552` | 渲染前校验 + speed 消费 | 消费端不变,数据源已修 |
| `nodes/assembly/node_validate_render_qc.py` | qc_status 判定 + 渲染传导错误 | ✅ 同根因降级 warning |
| `nodes/assembly/node_write_report.py` | P2-4 事件 emit 时序(缺陷②病灶) | ✅ submitted 参 + final_status 入 state |
| `state.py` | WorkflowState | ✅ 增 assembly_final_status(D09) |
| `web/static/delivery_summary.mjs` | 交付卡归约 | ✅ 错误数单列;final 真实口径 |
| `nodes/node_join_before_delivery.py` | 无条件尾链检查(缺陷③病灶) | ✅ skip_missing required 闸门 |
| `draft_ops/draft_integrity.py:607/796` | required keys + 类别聚合 | ✅ transitions n/a 扩展;video_effects 口径明确 |

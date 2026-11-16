# 交付卡失败与降级根因分析及修复计划(20261117)

> 分析对象:创建台任务 `console-553e3e7b34f7`(运行 `run-b99cb547e0a7`,2026-11-16 15:36 启动,已完成,42 节点成功/0 失败)。
> 触发场景:交付状态卡出现 **Assembly QC「降级(final:not_available)」**、**效果落盘「失败 1」**、**交付 QA「降级(2 个问题)」**。
> 结论:3 个真实缺陷(1 个影响每次运行 + 2 个显示/口径问题)+ 2 项预期行为。修复计划已批准(本文 §5),**截至 2026-11-17 尚未实施**。

---

## 1. 结论总览

| 交付卡行 | 显示 | 定性 | 根因 |
|---|---|---|---|
| Assembly QC | 降级 chip + `final:not_available` | 缺陷① + 缺陷② | ①投影数据自相矛盾 → QC 校验全盘误报;②final 显示时序 bug(纯显示) |
| 效果落盘 | 失败 1(红 chip) | 缺陷③ | 贴纸未启用属预期,却被无条件尾链检查报 error |
| 交付 QA | 降级·发现 2 个问题 | 缺陷③(同源) | 同上,zh/en 两份草稿各 1 条贴纸误报 |
| MP4 导出 | 跳过 | **预期行为** | `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=0` 默认关闭 |
| 草稿时长 12.467s ≠ 目标 35s | — | **预期行为** | `timeline_plan.json` `method="tts_align"`,槽位跟随配音时长(见 §4.2) |

「草稿生成/草稿发布」两行成功与上述降级**不矛盾**:QC 属观测层,final plan 实际正常提交,主链无实质影响(§3.1 末)。

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

**实质影响:无。** P1-7 提交判据只拦结构性 error(`preview_issue` 非结构性,不在 `STRUCTURAL_CODES`),final plan 正常提交(sha 与候选一致、patch 0 ops),generate_draft 正常消费 → 「草稿生成 成功」。

**关键放大效应:** tts_align 链的槽位恒小于素材原长(变速压缩是常态),所以**这个误报会在每次运行都触发**,preview.mp4 会永远生不出来——不是本次任务的偶发问题。

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

---

## 4. 非故障项说明

### 4.1 MP4 导出「跳过」

`AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=0` 默认关闭,设计如此。

### 4.2 草稿 12.467s ≠ 目标 35s

`storyline/timeline_plan.json` 的 `method="tts_align"`:时间槽位跟随配音时长——本次旁白仅两句,group_0001 配音 4053ms(clips 1-3 各 1.351s)、group_0002 配音 8414ms(clip_0004 8.414s),合计 12.467s;35s 是上限 `options.max_duration_ms`。装配链(final_plan 与 timeline_plan 逐字段一致)只是忠实投影上游计划,不是本次故障。

---

## 5. 修复计划(已批准,待实施)

三个独立提交:

### 提交 1 —— 缺陷①:投影补写隐含速度
- 文件:`assembly_capabilities/build_timeline.py` 候选投影函数(约 L1356,`"speed": 1.0` 处)。
- 改法:`slot = t_out - t_in`;`speed = (end_s - start_s) / slot`(slot<=0 或源区间<=0 时防御性回退 1.0);与 1.0 误差 <1e-6 时归一写 1.0。
- **不动**旧式 1:1 路径 `_build_clips`(L655,槽位=素材时长,其 speed=1.0 自洽)。
- 生效链:`clip_render_duration = source/speed = 槽位长` → 6 warning + duration error 消失;render_preview 支持 speed → preview.mp4 可生成。
- 新增单测(`tests/unit/nodes/test_assembly_nodes.py`):压缩槽位下投影 clip 的 speed 断言 + validate_timeline 对投影产物零 error/零 overlap。

### 提交 2 —— 缺陷②:assembly.qc 事件 final_status 修正
- 文件:`nodes/assembly/node_write_report.py`。
- `_emit_assembly_qc_summary(state)` 增参 `submitted`(即 `_submit_final_plan` 返回值),`final_status` 优先取 `submitted["final_status"]`,原 fallback 保留;
- `out` 增写 `assembly_final_status`(供未来消费者),与 report.md §7 口径一致。
- 单测:emit detail 在 submitted/blocked/not_available 三态下的 final_status 断言。

### 提交 3 —— 缺陷③:尾链效果检查加 required 闸门
- 文件:`nodes/node_join_before_delivery.py`(调用侧 L316/328)。
- `_tail_chain_structured` 签名与 issue 文本**不变**(保阶段4 K2=B 单测契约);节点侧用已有的 `resolve_required_features(state)` 过滤:转场/视频特效/贴纸三类中未 required 的类别不收集其「缺失」issue(孤儿绑定检查不受影响,其前提是 materials 有条目)。
- 效果:未勾选类别不再产生 error → build_qa_categories 第 9 步兜底恢复 → stickers 类=「不适用(未要求贴纸)」,效果落盘不再红、交付 QA 不再报贴纸问题。
- 单测:未勾贴纸时 join 无贴纸 issue 且 `categories.stickers=n/a`;勾选时行为不变。

### 明确不做
- 不 bump SCHEMA_VERSION(timeline.json 为产物内部格式,节点图不变);
- 不改转场/特效在本次运行中的行为(它们已启用且通过);
- 不动 `draft_ops/draft_integrity.py` 的类别聚合(兜底逻辑本身是对的,病灶在上游无条件 issue)。

---

## 6. 验证方案

1. **单测**:`.venv/Scripts/python.exe -m pytest tests/unit -q`(注意:pytest 汇总行被吞,以 exit 码为准;重点 `tests/unit/nodes/test_assembly_nodes.py`、`tests/unit/assembly_capabilities/`、join/integrity 相关);若有失败,用 `.worktrees` 基线对照法判存量/新增。
2. **前端**:`node --test web/tests/delivery_summary.test.mjs`(确认提交 2 不破坏归约契约)。
3. **真机验收(操作员)**:重跑创建台任务 →
   - Assembly QC 行应为 成功/pass_with_warnings,report.md §1 有 preview.mp4 路径,§7 `final:submitted`;
   - 不勾贴纸时效果落盘无「失败」、交付 QA 无贴纸问题;
   - 勾选贴纸(ID 回填后)时贴纸缺失检查仍生效。

---

## 附:分析涉及的关键代码位置索引

| 位置 | 角色 |
|---|---|
| `assembly_capabilities/build_timeline.py:1341-1360` | 候选→timeline.json 投影(缺陷①病灶) |
| `assembly_capabilities/build_timeline.py:655` `_build_clips` | 旧式 1:1 投影(自洽,不改) |
| `assembly_capabilities/timeline_ops.py:180/268` | 投影校验 + clip_render_duration(误报触发点) |
| `assembly_capabilities/render_preview.py:75/314/496/552` | 渲染前校验 + speed 消费 |
| `nodes/assembly/node_validate_render_qc.py:49` | qc_status=escalated 判定 |
| `nodes/assembly/node_write_report.py:660/495` | P2-4 事件 emit 时序(缺陷②病灶) |
| `web/static/delivery_summary.mjs` | 交付卡归约(escalated→warn、final 显示) |
| `nodes/node_join_before_delivery.py:88/316/328` | 无条件尾链检查(缺陷③病灶) |
| `draft_ops/draft_integrity.py:796` `build_qa_categories` | 类别聚合与 n/a 兜底(被上游污染) |

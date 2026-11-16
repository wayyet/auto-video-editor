# auto-video-editor 交付卡失败与降级根因分析：正确性审查与修复执行计划

> 审查日期：2026-11-17\
> 仓库：`wayyet/auto-video-editor`\
> 分支：`main`\
> 被审查文档：`docs/第六期开发/auto-video-editor_交付卡失败降级根因分析与修复计划_20261117.md`\
> 审查方式：GitHub `main` 分支源码静态审查；未在本地检出仓库，也未执行
> pytest、Node 测试或 Windows 剪映真机测试。

------------------------------------------------------------------------

## 1. 审查结论

### 1.1 总体判断

原文档提出的三个根因方向基本成立：

1.  Assembly 时间线投影把 `speed` 固定为
    `1.0`，与源素材区间和目标时间轴区间不一致，造成时间线校验和预览渲染失败。
2.  `assembly_write_report` 在节点返回前发出 QC
    事件，事件读取不到当前节点即将返回的
    `assembly_final_plan_path`，导致 `final:not_available`。
3.  `join_before_delivery` 对转场、视频特效、贴纸进行无条件缺失检查，与
    `required_features` 的按需判定冲突。

但是，原文档**不建议原样作为最终开发执行依据**。它遗漏了一个与 speed
修复直接相关的修复策略缺陷，并且有若干结论表述过度确定。

### 1.2 结论分级

  -----------------------------------------------------------------------------------------------------------------
  审查项                      判断                            置信度 说明
  --------------------------- ---------------- --------------------- ----------------------------------------------
  缺陷①：投影 `speed=1.0`     确认                                高 `candidate_projection_timeline()` 确实硬编码
                                                                     1.0

  缺陷①：校验使用源时长除以   确认                                高 `clip_render_duration()` 使用
  speed                                                              `duration / speed`

  缺陷①：导致本次预览失败     代码链路成立                        高 候选 QC
                                                                     先校验投影，再调用预览渲染；渲染函数再次校验

  缺陷①：每次运行都会失败     证据不足                          中低 取决于本次各 clip 的源区间、目标区间和重叠关系

  缺陷②：事件时序错误         确认                                高 节点返回值在节点结束后才合并入 LangGraph state

  缺陷③：贴纸缺失检查未判断   确认                                高 `_tail_chain_structured()` 无条件生成贴纸缺失
  required                                                           error

  缺陷③：修复后可恢复 N/A     逻辑成立                            高 前提是只移除非必需效果的"缺失"issue

  本次任务具体 issue          尚未独立核验                       --- `outputs/console-...`
  数、SHA、产物内容                                                  是运行产物，当前源码读取无法证明其内容
  -----------------------------------------------------------------------------------------------------------------

### 1.3 最重要的新增发现

原文档没有处理 `assembly_capabilities/repair_strategies.py`
中一个关键假设：

``` python
# 当前代码注释中的假设
# 候选 clip 速度恒 1.0，源偏移 = 时间轴偏移
```

修复时间线投影后，预览 QC
将有机会真正生成并发现黑帧、静音、卡帧等问题。此时修复策略会根据时间轴区间计算源素材裁切区间，但当前算法仍假设
`speed=1.0`。

当 `source_duration != timeline_duration`
时，这个换算不正确，可能裁错源素材区间。

因此，**speed 投影修复必须与 repair strategy
的时间换算一起审查和测试**，不能只修改 `build_timeline.py`
就宣布全链路正确。

------------------------------------------------------------------------

## 2. 源码核验结果

## 2.1 缺陷①：候选时间线投影的 speed 不正确

### 代码位置

`assembly_capabilities/build_timeline.py`

函数：`candidate_projection_timeline()`

当前逻辑：

``` python
start_s = int(clip["source_in_ms"]) / 1000.0
end_s = int(clip["source_out_ms"]) / 1000.0
t_in = int(clip["timeline_in_ms"]) / 1000.0
t_out = int(clip["timeline_out_ms"]) / 1000.0

clips.append({
    "start": start_s,
    "end": end_s,
    "duration": end_s - start_s,
    "timeline_start": t_in,
    "timeline_end": t_out,
    "speed": 1.0,
})
```

这里的 `start/end/duration`
表示源素材区间，`timeline_start/timeline_end`
表示目标时间轴区间。两者不同，却将速度固定为 1.0，确实造成契约不一致。

### 项目已有正确实现

`storyline/mapper.py` 的 `canonical_to_draft()`
已按源区间与目标区间计算速度：

``` python
speed = (
    seg_source_duration_us / seg_duration_us
    if seg_duration_us > 0 else 1.0
)
if abs(speed - 1.0) < 1e-6:
    speed = 1.0
```

因此 Assembly 投影应采用同一数学规则：

\[ speed = `\frac{source\_out-source\_in}`{=tex}
{timeline_out-timeline_in} \]

预期渲染时长：

\[ render_duration = `\frac{source\_duration}{speed}`{=tex} =
timeline_duration \]

例如：

-   源素材时长：15.442 秒
-   目标时间轴槽位：1.351 秒
-   推导速度：约 11.43
-   预期渲染时长：约 1.351 秒

### 校验链路

`assembly_capabilities/timeline_ops.py`：

``` python
return duration / speed
```

`assembly_capabilities/render_preview.py`：

``` python
f"setpts=(PTS-STARTPTS)/{clip['speed']:.8f}+{clip['timeline_start']:.6f}/TB"
```

两处都消费
`speed`。所以问题不是简单的展示错误，而是投影数据会影响时间轴校验和
FFmpeg 预览渲染。

### 必须补充的边界条件

原文档提出的公式方向正确，但应增加以下约束：

1.  源区间和目标区间必须是有限数值。
2.  源区间长度必须大于 0。
3.  目标槽位长度必须大于 0。
4.  `speed` 必须是有限正数。
5.  `speed` 与 `storyline/mapper.py` 的计算规则必须一致。
6.  浮点比较使用明确容差，不能用精确相等判断。
7.  `sequence.duration` 应使用所有 clip
    的最大目标结束时间，而不是默认取最后一个 clip 的结束时间。

特别是第 7 点：当前代码：

``` python
total = clips[-1]["timeline_end"] if clips else 0.0
```

隐含假设 clips 按结束时间排序。`CanonicalTimeline` 当前只验证
`timeline_in_ms` 单调非降，并未保证 `timeline_out_ms`
单调非降。因此更稳妥的规则是：

``` python
total = max(
    (clip["timeline_end"] for clip in clips),
    default=0.0,
)
```

是否需要保留该改动，应结合候选计划的排序和重叠语义测试确认。

## 2.2 缺陷①：原文档对"每次运行必然失败"的结论过强

当前代码能够证明：

-   `speed` 固定为 1.0 是错误的。
-   当源时长与目标槽位不一致时，校验会按错误时长计算。
-   当错误计算导致同轨重叠或超出 `sequence.duration`
    时，会产生校验问题。
-   `render_preview()` 在渲染前再次调用
    `validate_timeline()`，校验不通过会直接返回错误。

但不能仅凭一个任务样本证明所有运行都必然失败。

此外，`CanonicalTimeline` 当前只约束 `timeline_in_ms`
单调非降，并没有禁止所有目标区间重叠。转场等场景可能存在有意重叠。

因此：

-   不能把"6 条 warning 必然全部消失"写成无条件保证。
-   不能简单把所有 overlap 都定义成非法。
-   应判断重叠是否符合候选计划中的预期转场/覆盖语义。
-   应将"未按计划发生的重叠"与"有意设计的重叠"区分。

建议将原文档中的：

> 这个误报会在每次运行都触发，preview.mp4 会永远生不出来。

改为：

> 对源素材区间与目标时间轴槽位不一致的候选 clip，当前固定 speed=1.0
> 会导致时长计算错误；当该错误进一步触发同轨重叠或 sequence
> 时长越界时，候选预览会被阻断。该问题具有系统性复现风险，但不能仅凭单次任务断言所有运行都会失败。

## 2.3 缺陷①：候选 QC 实际上有两条验证对象

`nodes/assembly/node_validate_render_qc.py` 的 `_candidate_qc_path()`
不是只检查 timeline.json。

实际流程：

``` text
candidate_plan
    |
    v
write_candidate_draft()
    |
    v
validate_candidate_draft()
    |
    v
validate_timeline()
    |
    v
render_preview()
    |
    v
qc_preview()
```

其中：

  -------------------------------------------------------------------------------------------------
  环节                         检查对象                目的
  ---------------------------- ----------------------- --------------------------------------------
  `validate_candidate_draft`   临时剪映草稿            检查正式草稿映射、轨道、素材引用、关联音轨

  `validate_timeline`          timeline.json           检查预览投影结构、源素材和时间轴布局

  `render_preview`             timeline.json           生成 FFmpeg 预览

  `qc_preview`                 preview.mp4             检查黑帧、静音、卡帧等质量问题
  -------------------------------------------------------------------------------------------------

这两条验证路径不能混为一谈。

原文档应明确：

> 修复 timeline
> 投影，只能证明预览投影链路恢复一致性；还必须证明临时剪映草稿与候选计划之间的映射检查继续通过。

### 重复错误问题

`_candidate_qc_path()` 先调用 `validate_timeline()`，随后调用
`render_preview()`；而 `render_preview()` 自己又调用
`validate_timeline()`。

同一校验根因可能被记录两次：

1.  `validate_timeline()` 返回的原始 issue。
2.  `render_preview()`
    返回的通用错误：`timeline validation failed before render`。

这可能解释 QC 报告中出现多个 error，但不能在没有本次
`candidate_qc_report.json` 的情况下断言具体两条 error 一定来自这一机制。

建议增加根因关联字段，例如：

``` json
{
  "code": "timeline_validation_failed",
  "check": "timeline 投影校验",
  "root_cause_id": "timeline-validation-<hash>",
  "message": "..."
}
```

渲染阶段只记录"由该校验失败导致渲染跳过"，不要把同一根因再次计为独立缺陷。

## 2.4 新增缺陷：repair strategy 仍假设 speed=1.0

### 代码位置

`assembly_capabilities/repair_strategies.py`

`build_repair_patch()` 对预览坏窗执行局部裁切时，当前使用：

``` python
head_span = max(
    0,
    start_ms - int(target["timeline_in_ms"])
)

tail_span = max(
    0,
    int(target["timeline_out_ms"]) - end_ms
)
```

随后直接将时间轴偏移作为源素材偏移：

``` python
keep_in = (
    int(target["source_in_ms"])
    + (end_ms - int(target["timeline_in_ms"]))
)
```

或者：

``` python
keep_out = (
    int(target["source_in_ms"])
    + head_span
)
```

当前实现注释明确假设候选 clip 速度恒为 1.0。

但 `CanonicalTimeline` 和 `storyline/mapper.py` 已允许、并且实际推导非
1.0 的速度。

### 正确换算

定义：

\[ speed = `\frac{source\_duration}{timeline\_duration}`{=tex} \]

时间轴偏移转换为源素材偏移：

\[ source_offset = timeline_offset `\times `{=tex}speed \]

例如：

-   源区间：0～2000ms
-   目标区间：0～1000ms
-   speed：2.0
-   需要从目标时间轴 600ms 开始保留尾部

正确源素材起点：

\[ 0 + 600 `\times 2`{=tex} = 1200ms \]

当前代码却会得到 600ms。

这会使局部修复裁切位置错误。

### 必须增加的修复

1.  根据 clip 的源区间和目标区间计算 speed。
2.  将坏窗时间轴边界换算为源素材边界。
3.  使用整数毫秒进行计算，明确舍入策略。
4.  对计算结果进行源区间边界裁剪。
5.  重新验证保留区间大于零。
6.  更新代码注释，不再声称所有候选 clip 都是 1.0 倍速。

**该问题是原文档最重要的遗漏，建议列入 P1，与 speed
投影修复同一阶段实施。**

------------------------------------------------------------------------

## 3. 缺陷②：final_status 事件时序

### 3.1 代码核验

`nodes/assembly/node_write_report.py`：

`_submit_final_plan()` 返回：

``` python
{
    "final_plan": ...,
    "final_plan_path": ...,
    "final_plan_sha256": ...,
    "final_status": "submitted",
    "final_notes": ...,
}
```

`assembly_write_report_node()` 随后将结果写入局部变量 `out`，但在返回
`out` 之前调用：

``` python
_emit_assembly_qc_summary(state)
```

而 `_emit_assembly_qc_summary()` 只读取旧的 `state`。

因此它读取不到当前节点即将返回的：

``` python
assembly_final_plan_path
```

原文档对这个问题的判断成立。

### 3.2 推荐实现

推荐显式传入本次提交结果：

``` python
_emit_assembly_qc_summary(
    state,
    submitted=submitted,
)
```

事件中的状态直接取：

``` python
final_status = submitted.get("final_status", "not_available")
```

同时将状态写入节点返回值：

``` python
out["assembly_final_status"] = final_status
```

需要注意：

-   `WorkflowState` 是 `TypedDict`，应同步增加对应的可选字段声明。
-   `submitted` 为空、提交阻断和没有候选计划三种状态必须分别测试。
-   事件发送失败仍然不能影响工作流主流程。
-   不能用 `final_plan.json` 文件存在与否代替本次提交状态。

### 3.3 测试要求

  场景                 节点返回值        QC 事件
  -------------------- ----------------- -----------------
  正常提交             `submitted`       `submitted`
  结构性错误阻断       `blocked`         `blocked`
  无候选计划           `not_available`   `not_available`
  事件 writer 不可用   正常返回          不影响主流程

------------------------------------------------------------------------

## 4. 缺陷③：required-feature 与尾链检查冲突

### 4.1 代码核验

`nodes/node_join_before_delivery.py` 的 `_tail_chain_structured()`：

``` python
if transitions == 0:
    _add("转场", "...", "transitions")

if video_effects == 0:
    _add("视频特效", "...", "video_effects")

if stickers == 0:
    _add("贴纸", "...", "stickers")
```

这些检查没有根据 required 状态判断是否应当报告缺失。

`draft_ops/draft_integrity.py` 已有：

``` python
resolve_required_features(state)
```

以及贴纸未要求时的 `CAT_NOT_APPLICABLE` 处理。

但 `build_qa_categories()` 第九步只有在该类别当前状态仍为 `CAT_PASSED`
时，才会将未要求的贴纸设置为 `CAT_NOT_APPLICABLE`。

所以，先前加入的无条件尾链 error 会污染类别状态，导致兜底无法生效。

### 4.2 需要特别明确的分类规则

当前源码中的 `REQUIRED_FEATURE_KEYS` 包含：

-   `transitions`
-   `stickers`

但**不包含 `video_effects`**。

源码注释也明确将视频特效定位为加分效果，不进入 required gate。

因此不能把三个类别简单视为完全相同的 required-feature。

建议定义：

  类别       是否属于 required-feature   缺失时行为
  ---------- --------------------------- ----------------------------------------------------
  转场       是                          由 `resolve_required_features()` 决定是否报告缺失
  视频特效   否                          不因没有特效而报告缺失；已有特效的错误绑定仍需报告
  贴纸       是                          由 `resolve_required_features()` 决定是否报告缺失

### 4.3 过滤范围必须精确

原文档提出过滤非必需类别的缺失 issue，方向正确，但必须限制过滤范围。

不能把某个类别的所有 issue 全部删除。

例如：

-   没有贴纸，且贴纸不 required：可以不报缺失。
-   有贴纸，但没有绑定到视频段：必须继续报错。
-   没有视频特效：作为可选效果不报缺失。
-   有视频特效，但素材没有被任何片段引用：必须继续报错。
-   没有转场，且转场 required：必须继续报错。
-   有转场，但引用悬空：必须继续报错。

建议只对以下三类"缺失检查"应用 required 判断：

``` text
转场缺失
视频特效缺失
贴纸缺失
```

不要过滤：

``` text
转场绑定
视频特效绑定
贴纸绑定
资源 ID 非法
素材引用悬空
```

这与原文档声称的"孤儿绑定检查不受影响"一致，但必须落实到代码和测试。

### 4.4 推荐实现原则

在 `join_before_delivery()` 中解析一次：

``` python
required = resolve_required_features(state)
```

将 required 信息传入尾链检查，或者在调用侧按明确的 `check` 类型过滤。

推荐优先采用显式参数方式，使 `_tail_chain_structured()`
的输入契约完整，避免在调用侧通过模糊字符串匹配过滤 issue。

如果为了保持既有测试契约而不修改函数签名，则必须对 `check`
字段进行精确匹配，并保留所有绑定类 issue。

------------------------------------------------------------------------

## 5. 原文档存在的问题汇总

  -------------------------------------------------------------------------------------------------------------
  编号              问题                                           严重程度          修订要求
  ----------------- ---------------------------------------------- ----------------- --------------------------
  D01               未处理 repair strategy 的 speed 换算错误       P1                必须增加修复

  D02               将单次任务推论扩大为每次运行必然失败           P2                限定适用条件

  D03               将 QC 降级描述为"实质影响无"                   P1                明确预览和人工复核受影响

  D04               没有区分候选草稿校验与 timeline 投影校验       P1                补充双路径架构

  D05               没有分析重复执行 timeline validation           P2                增加根因去重
                    造成的错误重复计数                                               

  D06               假设 speed 修复后全部 overlap warning 消失     P1                区分意图重叠与异常重叠

  D07               `sequence.duration` 取最后一个 clip 的结束时间 P2                考虑最大结束时间

  D08               视频特效不属于 required-feature 的规则没有说明 P1                明确分类

  D09               未说明 `WorkflowState` 类型声明需要同步修改    P2                增加字段声明

  D10               任务运行产物没有独立复核来源                   P2                附原始
                                                                                     JSON、运行日志及哈希

  D11               测试只强调单测，没有充分覆盖真实预览渲染       P1                增加集成测试

  D12               没有验证临时草稿与正式剪映草稿的速度语义一致   P1                增加 mapper parity 测试
  -------------------------------------------------------------------------------------------------------------

### 5.1 关于"实质影响无"的修订

原文档的表述不够准确。

建议改为：

> 该缺陷没有阻断 P1-7
> 最终计划提交，也没有直接阻断后续正式草稿生成。但是候选预览无法生成，导致关卡①缺少预览复核依据，Assembly
> QC
> 无法完成预期的视觉质量验证。因此它不属于正式草稿生成阻断故障，但属于影响人工复核和交付质量保障的实际缺陷。

### 5.2 关于运行证据的修订

原文档中的：

-   `console-553e3e7b34f7`
-   `run-b99cb547e0a7`
-   `42 节点成功/0 失败`
-   `6 warning + 2 error`
-   SHA `9ed12c11…`
-   草稿时长 `12.467s`

属于特定任务的运行证据。

本次审查读取了 GitHub `main` 分支源码，但没有读取该任务的完整
`outputs/console-553e3e7b34f7/`
运行目录。因此不能将这些数据标记为本次独立验证通过。

正式修订文档应为这些证据增加：

1.  `candidate_qc_report.json` 原始内容。
2.  `timeline.json` 对应 clip 的完整字段。
3.  `timeline_validation.json`。
4.  `repair_history.json`。
5.  `report.md`。
6.  `final_plan.json` SHA-256。
7.  `initial_state.json`。
8.  zh/en 两份 `draft_content.json` 中相关效果素材桶和轨道绑定数据。

------------------------------------------------------------------------

## 6. 修订后的分阶段开发执行计划

### P0：修复时间线投影与修复策略的速度语义

**目标：** Assembly
投影、预览渲染、候选计划和修复循环统一使用源区间/目标区间推导的 speed。

  --------------------------------------------------------------------------------------------------------------------------------
  任务              文件                                           实施内容                               验收标准
  ----------------- ---------------------------------------------- -------------------------------------- ------------------------
  P0-01             `assembly_capabilities/build_timeline.py`      将固定 `speed=1.0`                     speed 与 mapper 规则一致
                                                                   改为源区间/目标槽位推导                

  P0-02             `assembly_capabilities/build_timeline.py`      增加零长度、非法区间、非有限数值防御   非法输入不能生成伪合法
                                                                                                          speed

  P0-03             `assembly_capabilities/build_timeline.py`      检查 `sequence.duration` 的计算规则    覆盖所有 clip
                                                                                                          的最大目标结束时间

  P0-04             `assembly_capabilities/repair_strategies.py`   将 timeline 偏移正确换算为 source 偏移 非 1.0 speed
                                                                                                          下裁切位置正确

  P0-05             `assembly_capabilities/timeline_ops.py`        检查重叠判定与候选计划预期重叠语义     不误报合法转场重叠

  P0-06             `assembly_capabilities/render_preview.py`      验证 speed、音视频时长和目标时间轴一致 预览时长符合目标计划
  --------------------------------------------------------------------------------------------------------------------------------

**关键约束：**

-   不修改正式草稿的时间范围。
-   不改变 `storyline/mapper.py` 已有速度推导规则。
-   不将有意重叠的转场误判为非法。
-   不允许修复循环将时间轴偏移直接当成源素材偏移。

### P1：修复 Assembly QC 事件 final_status

  ----------------------------------------------------------------------------------------------------------------------
  任务              文件                                        实施内容                          验收标准
  ----------------- ------------------------------------------- --------------------------------- ----------------------
  P1-01             `nodes/assembly/node_write_report.py`       `_emit_assembly_qc_summary()`     事件读取实际提交状态
                                                                接收本次 submitted 结果           

  P1-02             `nodes/assembly/node_write_report.py`       将 `assembly_final_status`        state 可读取最终状态
                                                                写入节点返回值                    

  P1-03             `state.py`                                  增加 `assembly_final_status`      TypedDict 与运行时一致
                                                                可选字段                          

  P1-04             `tests/unit/nodes/test_assembly_nodes.py`   覆盖                              三态全部正确
                                                                submitted/blocked/not_available   

  P1-05             `web/tests/delivery_summary.test.mjs`       验证前端状态归约                  正确显示
                                                                                                  `final:submitted`
                                                                                                  等状态
  ----------------------------------------------------------------------------------------------------------------------

### P1：修复 required-feature 尾链检查

  ----------------------------------------------------------------------------------------------------------------------
  任务              文件                                   实施内容                     验收标准
  ----------------- -------------------------------------- ---------------------------- --------------------------------
  P1-06             `nodes/node_join_before_delivery.py`   将 required 判断引入缺失检查 非必需效果不报缺失

  P1-07             `nodes/node_join_before_delivery.py`   保留转场/特效/贴纸绑定检查   已存在但未绑定仍报错

  P1-08             `draft_ops/draft_integrity.py`         保持现有 N/A 分类语义        未要求贴纸为 `not_applicable`

  P1-09             `tests/unit/`                          增加 required true/false     必需缺失阻断，非必需缺失不阻断
                                                           组合测试                     
  ----------------------------------------------------------------------------------------------------------------------

### P2：完善 QC 诊断与报告口径

  -----------------------------------------------------------------------------------------------------------------------
  任务              文件                                          实施内容                     验收标准
  ----------------- --------------------------------------------- ---------------------------- --------------------------
  P2-01             `nodes/assembly/node_validate_render_qc.py`   关联重复校验错误             同根因不重复计为独立问题

  P2-02             `assembly_capabilities/timeline_ops.py`       增加预期重叠与异常重叠区分   诊断结果准确

  P2-03             `nodes/assembly/node_write_report.py`         报告中区分草稿结构 QC 与预览 报告能指出具体失败环节
                                                                  QC                           

  P2-04             `web/static/delivery_summary.mjs`             展示根因数量和原始 issue     不混淆错误数量与根因数量
                                                                  数量                         
  -----------------------------------------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 7. 测试与验收标准

### 7.1 Speed 投影单元测试

至少覆盖：

  场景              源区间   目标区间 预期
  -------------- --------- ---------- ----------------
  正常 1 倍速       1000ms     1000ms speed=1.0
  快速播放          2000ms     1000ms speed=2.0
  高速播放         15442ms     1351ms speed≈11.43
  非法源区间           0ms     1000ms 拒绝或明确错误
  非法目标区间      1000ms        0ms 拒绝或明确错误

对于 `CanonicalTimeline`
不允许的慢放输入，不应绕过其契约校验；单元测试可以单独验证公式函数的数值行为。

### 7.2 Repair Strategy 测试

必须新增非 1.0 倍速裁切测试。

示例：

``` text
source:   0 -------------------- 2000ms
timeline: 0 ---------- 1000ms
speed:    2.0

坏窗:     200ms -------- 600ms
保留尾部: 600ms -------- 1000ms
```

预期源素材保留范围：

``` text
1200ms -------- 2000ms
```

不能错误地计算为：

``` text
600ms -------- 2000ms
```

同时覆盖：

-   保留头部。
-   保留尾部。
-   保留区间不足 500ms。
-   坏窗超出 clip 区间。
-   speed 非法。
-   源素材边界裁剪。

### 7.3 Assembly 预览集成测试

不能只 mock `render_preview()`。

应使用可控的合成测试素材，验证：

1.  候选计划能生成 timeline.json。
2.  timeline.json 能通过校验。
3.  preview.mp4 实际生成。
4.  预览时长与 sequence.duration 一致。
5.  clip 实际显示区间与目标时间轴一致。
6.  音频 speed 处理不会造成异常时长。
7.  临时 QC 草稿和正式 mapper 生成的草稿使用一致的速度语义。

时间误差应按输出 FPS 设定容差，例如允许约 1～2
帧，而不是要求浮点时长完全相等。

如果 CI 没有
FFmpeg，应将此项标记为环境依赖的集成测试，不得用单元测试通过代替真实渲染验收。

### 7.4 Final Status 测试

必须覆盖：

``` text
submitted
blocked
not_available
writer unavailable
```

并验证：

-   节点返回值。
-   custom stream 事件 payload。
-   前端交付卡。
-   checkpoint resume 后最终状态没有被旧值覆盖。

### 7.5 Required Feature 测试

  required_features       草稿状态           预期类别         gate_failed
  ----------------------- ------------------ ---------------- -------------
  stickers=false          无贴纸             not_applicable   false
  stickers=true           无贴纸             failed           true
  stickers=false          有贴纸且正确绑定   passed           false
  stickers=false          有贴纸但未绑定     failed           false
  transitions=true        无转场             failed           true
  transitions=false       无转场             not_applicable   false
  video_effects（可选）   无特效             不因缺失报错     false
  video_effects（可选）   有特效但未绑定     failed           false

这里必须验证：**不 required 只豁免"缺失"，不豁免"已经存在但结构错误"。**

### 7.6 全量测试

建议执行：

``` powershell
.venv\Scripts\python.exe -m pytest tests/unit -q
```

前端测试：

``` powershell
node --test web/tests/delivery_summary.test.mjs
```

如果仓库实际虚拟环境目录不是
`.venv`，应使用当前项目实际环境路径，不要为了执行测试而新建或覆盖环境。

此外，应单独执行 Assembly、draft_integrity、join_before_delivery
相关测试，便于定位回归。

------------------------------------------------------------------------

## 8. 真机验收：剪映草稿与预览一致性

单测全部通过后，仍需在 Windows + 剪映环境完成验收。

### 验收步骤

1.  使用相同输入素材运行一次创建台任务。
2.  检查 `outputs/<job_id>/assembly/timeline.json`。
3.  检查 `candidate_qc_report.json`。
4.  确认 `preview.mp4` 实际生成。
5.  检查 `preview_qc_report.json`。
6.  检查临时 `qc_draft/draft_content.json`。
7.  检查正式剪映草稿的 `draft_content.json`。
8.  比较源区间、目标区间和 speed。
9.  在剪映中实际播放对应片段。
10. 确认没有黑帧、停帧、错误裁切、音画错位。
11. 检查未勾选贴纸时不再显示贴纸缺失错误。
12. 检查已勾选且必需的贴纸缺失时仍然正确阻断。

### 最终通过标准

-   Assembly QC 不再因为投影 speed 不一致而误报。
-   preview.mp4 能生成且播放内容符合候选计划。
-   临时草稿与正式草稿的速度和时间范围语义一致。
-   `final_status` 与最终计划真实提交结果一致。
-   非必需贴纸不再被误判为交付失败。
-   必需效果缺失仍然能被 QA 检测。
-   现有草稿生成、发布、人工关卡和 LangGraph checkpoint 恢复能力不退化。

------------------------------------------------------------------------

## 9. 建议的实施顺序

不建议把三个原始缺陷简单拆成三个互不相关的提交，因为 speed 修复会让预览
QC 和 repair loop 进入新的有效执行路径。

建议按以下顺序：

``` text
P0-01 统一 speed 推导
   |
   v
P0-02 修复 repair strategy 时间换算
   |
   v
P0-03 修正 sequence.duration 与 overlap 判定
   |
   v
P0-04 预览集成测试
   |
   v
P1-01 final_status 事件修复
   |
   v
P1-02 required-feature 尾链修复
   |
   v
P1-03 全量单测与前端测试
   |
   v
P2   QC 报告去重和诊断优化
   |
   v
Windows + 剪映真机验收
```

三个原始问题仍然可以独立提交，但 P0 的 speed 与 repair strategy
应作为一个完整的逻辑修复批次验收。

------------------------------------------------------------------------

## 10. 最终审查意见

原始 MD
文档的三个根因判断总体正确，代码定位也具有较高可信度；但它存在一个关键遗漏：**时间线
speed 修复后，预览坏窗修复策略仍按 1.0 倍速换算源素材区间。**

此外，文档对"每次运行必然失败""全部 warning
消失""实质影响无"的描述需要收敛，并补充候选草稿与时间线投影双路径、重叠语义、重复校验、视频特效
optional 规则以及运行证据来源。

因此，建议将原文档状态调整为：

> **根因分析基本成立，修复方案需要补充后再进入正式开发。**

优先级：

1.  P0：speed 投影与 repair strategy 统一速度语义。
2.  P1：final_status 事件时序修复。
3.  P1：required-feature 尾链检查修复。
4.  P2：重复错误归因、报告口径和诊断优化。
5.  最后通过 FFmpeg 集成测试及 Windows 剪映真机验收。

本报告仅提供代码审查和执行计划，不对 GitHub
仓库进行修改，也不代表相关测试已经执行通过。

# OpenStoryline 视频超时长（35 秒）问题分析

- 日期：2026-07-07
- 页面：Edge Tools 中打开的 `http://127.0.0.1:7860/`（OpenStoryline，PID 25012）
- 目标会话：`16afff52932943828851ead27abd8813`（吉隆坡 PAVILION 商场旅游 Vlog）

## 结论（TLDR）

实际定剪时长是 **170.73 秒**（约 2 分 51 秒），不是"大于 1 分钟"，超出用户要求的 35 秒约 4.9 倍。

根因：**「35 秒」这个约束只存在于聊天对话文本里，OpenStoryline 的工具管线中没有任何参数能承接它**；而定剪节点唯一能压缩时长的两个机制——配音时长对齐、音乐节拍卡点——恰好被用户自己提出的「不做 BGM、不做语音生成」全部关闭，导致定剪退化为 19 个片段按原长、原速首尾拼接，素材多长，成片就多长。

## 证据链（会话缓存实测数据）

| 环节 | 实测结果 |
|------|----------|
| `split_shots` 切镜 | 4 个素材切出 23 个片段（默认单片 1~10 秒） |
| `filter_clips` 筛选 | 保留 23 个片段，只挑观感差的剔除，无时长预算 |
| `group_clips` 分组 | 10 个叙事组、19 个片段，合计 **170.7 秒** |
| `select_bgm` | 输出 `"bgm": {}`（空，因用户要求不做 BGM） |
| `generate_voiceover` | 输出 `"voiceover": []`（空，因用户要求不做配音） |
| `plan_timeline_pro` 定剪 | 19 段全部 `source_window == timeline_window`、`playback_rate=1.0`，总长 **170.73 秒**，字幕轨同步到 170.73 秒 |

## 代码层根因

1. **`plan_timeline_pro.py` 的两把"尺子"都失效**
   - 有配音时：按 TTS 时长重排素材时长（[plan_timeline_pro.py:44-45](../FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/plan_timeline_pro.py#L44-L45)）
   - 有 BGM + 卡点时：按节拍对齐切点（[plan_timeline_pro.py:50-54](../FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/plan_timeline_pro.py#L50-L54)）
   - 本次会话 `use_beats=True` 但 `music` 为空字典，落入 else 分支（[plan_timeline_pro.py:56-58](../FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/plan_timeline_pro.py#L56-L58)）：`new_meterial_durations = meterial_durations`，素材时长原样透传，且**静默降级，不产生任何警告**。

2. **输入协议里从未有"目标总时长"参数**
   - `PlanTimelineInput` 只有 4 个字段：`use_beats`、`is_speech_rough_cut`、`is_ai_transition`、`image_duration_ms`（[node_schema.py:444-448](../FireRed-OpenStoryline/src/open_storyline/nodes/node_schema.py#L444-L448)），没有 `target_duration`。
   - `FilterClipsInput`、`GroupClipsInput` 只有 `mode` + 自由文本 `user_request`。
   - `group_clips` 的系统提示词（[system.md:40-43](../FireRed-OpenStoryline/prompts/tasks/group_clips/zh/system.md#L40-L43)）只约束"单组 3~20 秒"，对**总时长**完全没有约束——10 个组各自贴着上限走，170 秒反而是这套规则下的"标准产出"。

3. **定剪完成后无任何校验/告警**
   - 没有"实际时长 vs 用户要求时长"的比对步骤，超标 4.9 倍无声无息地通过。

## 三层根因总结

| 层级 | 问题 |
|------|------|
| 架构层（主因） | 全管线没有"目标总时长"的传递通道，35 秒只活在对话文本里 |
| 触发层 | 用户要求「不做 BGM、不做配音」，关闭了仅有的两个时长压缩机制 |
| 兜底缺失层 | 定剪后没有时长校验/告警环节 |

## 附：历史会话「接受 78 秒现状」的同源印证

该会话开启了配音，时长确实由 TTS 驱动，但 `generate_script` 生成文案时同样没有字数/时长预算——63 条字幕念完天然超过 35 秒，因此仍然超标。**两次超标是同一个病根的两种症状**：时长永远是别的东西（素材长度或文案长度）的因变量，从未被当作可控的自变量。

## 待选修复方向

1. ~~给 `PlanTimelineInput` 增加 `target_duration_ms` 参数，定剪时按比例裁剪素材时长。~~ ✅ 已执行（2026-07-07）
2. ~~在 `filter_clips` / `group_clips` 的提示词中注入总时长预算，从选料阶段就控量。~~ ✅ 已执行（2026-07-07）
3. 轻量方案：导入剪映后，用 `jianying-draft-edit` skill 对草稿做变速/删减压缩到 35 秒。（保留为兜底手段）

## 已执行修复（2026-07-07）

### 方案 1：定剪节点按目标时长等比裁剪

- [node_schema.py](../FireRed-OpenStoryline/src/open_storyline/nodes/node_schema.py)：`PlanTimelineInput` 新增可选参数 `target_duration_ms`（毫秒），字段描述要求 Agent 在用户指定成片时长时必须传入。
- [plan_timeline_pro.py](../FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/plan_timeline_pro.py)：
  - `TimeLine` 新增 `apply_target_duration()`：总时长超目标时等比缩放每段素材（夹底 `min_clip_duration=1000ms`），并重算变速；缩放发生在字幕/配音时间轴计算**之前**，保证同步。
  - 有配音时不裁剪（会与旁白脱节），改为告警提示走「缩短文案→重生成配音」路径；`speech_rough_cut` / `ai_transition` 模式下参数不生效并告警。
  - 裁剪后校验：实际时长仍超目标 10% 以上（受单片最短时长夹底限制）时告警，提示回到选料阶段减量。
  - 修掉静默降级：`use_beats=True` 但 BGM 为空时现在会输出明确警告。

### 方案 2：选料/分组阶段注入总时长预算

- `FilterClipsInput` / `GroupClipsInput` 同样新增 `target_duration_ms`。
- 新增共享工具 [duration_budget.py](../FireRed-OpenStoryline/src/open_storyline/utils/duration_budget.py)：生成中/英文预算提示文本。
- [filter_clips.py](../FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/filter_clips.py)：有预算时即使 `user_request` 为空也走 LLM 控量；保留片段总时长控制在预算的 1~2 倍（给定剪留裁剪余量），预算优先级高于「保留 80%」下限。
- [group_clips.py](../FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/group_clips.py)：所有分组时长累加不得超过预算 110%；素材超预算时「全量使用」失效，允许按叙事价值舍弃片段。
- 提示词模板（zh/en 各 4 个）：`filter_clips` 的 system.md 加「第零步：时长预算优先」，`group_clips` 的 system.md 加「总时长预算（最高优先级）」规则，两个 user.md 各加 `{{duration_budget}}` 占位符。

### 验证结果

编译、模板渲染、算法数值验证全部通过；复现场景 19 段共 170.73 秒 → 精确 35.00 秒（单段 1842ms，原速截取无慢放）。

### 使用提醒

- 修复只对**新会话/重跑节点**生效：需从 `filter_clips` 起重跑（或至少重跑 `plan_timeline_pro` 并传 `target_duration_ms=35000`）。
- MCP 服务需重启才能加载新工具参数。

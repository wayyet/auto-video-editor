# 剪映 VIP 花字草稿审计与流水线花字缺失根因分析（2026-11-18）

## 0. 背景与结论摘要

用户在剪映中打开草稿 `AutoVideo_console-52373c6f9e11`（截图显示右侧「文本 → 花字」素材库面板，满屏 VIP 徽章），提出两个问题：

1. 该草稿（以及英文分支草稿）有没有使用剪映里的 VIP 花字？
2. 为什么流水线/工作流从未把 VIP 花字效果写入中英文草稿？

**结论：**

- 中文主草稿确实挂了 **1 个 VIP 花字「互动立体」**（effect_id `7407340226189757733`），但它是 **2026-11-18 早上在剪映 UI 里手动添加**的，不是流水线写入；对流水线零影响，当前账号有 VIP，应用/导出不受阻。
- 英文分支草稿 `en_branch_console-52373c6f9e11_20261118_064813_970710` **完全干净**：零花字，纯流水线产物（含 node_10b 的入场动画）。
- 流水线不写花字**不是故障，是分层设计使然**：数据池、写入代码、vendor 库、导出兜底四层都没有剪映原生花字的通道；「花字」一词在代码库里被挪用为普通字幕样式/入场动画的代称。

---

## 1. 第一部分：草稿审计（截图 → JSON 实证）

### 1.1 截图解读

右侧面板只是打开了「文本 → 花字」**素材库浏览页**（第一格 ⊘「无」处于选中态）。满屏 VIP 徽章是**资源库本身**的标记，不代表草稿已应用——已应用与否必须看草稿 JSON 本体。

### 1.2 中文主草稿实测

草稿路径：

```
C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\AutoVideo_console-52373c6f9e11\draft_content.json
```

（最后保存 2026-11-18 07:13；流水线最后一次写入为审计日志末条 `node_13`，2026-11-17T22:50:11+00:00 = 本地 06:50。）

- 全文检索「花字」仅 2 处，指向**同一个**素材，位于 `materials.effects` 数组：
  - `category_id: "panel-text-flower"`、`category_name: "花字"`
  - `effect_id: "7407340226189757733"`、`name: "互动立体"`
  - `type: "text_effect"`、`source_platform: 1`
  - 条目重复出现 2 次（同 id `1D5E66AA-119A-416a-A828-3157E60EDDD3`），text 轨段的 `extra_material_refs` 也挂了 2 次——剪映 UI 保存的痕迹，同一资源重复引用，无害。
- 引用方：**`text-seg-4` / `text-zh-4`** —— node_08 生产的第 4 条中文字幕（start 18133333µs ≈ 18.13s，duration 5733333µs ≈ 5.73s，render_index 15000），对应时间轴第 4 条字幕「有人停下逗跳，灰绿海面平着…」。同一片段还挂着 node_10 的入场动画 ref `text-anim-text-zh-4`。
- 其余 5 条文本素材（含标题「嘣的一下」）**均无花字**。

### 1.3 VIP 属性判定（rp.db 面板缓存）

剪映本地面板响应缓存：

```
C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Cache\rp.db
```

其中 `flower_jianyingpro` 花字分类接口响应（2026-11-18 06:53 拉取）中该资源记录：

```json
"is_vip": true,
"paid_type": "subscribe",
"commercial_strategy": {"resource_export": {"paid_strategy_list": [{"paid_mode": "subscribe", "subscribe_package": "Pro"}]}}
```

→ **是 VIP 花字**，导出按 Pro 订阅套餐授权。资源包 md5 `c093f229a5b9529beab4d27c00bc0c4f` 与草稿引用的本地缓存目录完全一致（`Cache\artistEffect\7407340226189757733\c093f229…\`），确为同一资源。另有一条 `is_vip:false` 记录属商用素材通道（`publish_source: internal_zip`），与消费端面板无关。

### 1.4 手动操作的三重证据（排除流水线写入）

| 证据 | 结果 |
|---|---|
| 审计日志 `.autovideo_draft_mutations.jsonl`（node_05→13 全部写入记录） | **没有任何** materials.effects/花字痕迹，最后一条 06:50 node_13 |
| 代码库全库检索 `panel-text-flower`、`互动立体` | **零命中** |
| 时间线 | 花字资源包本地缓存创建于 2026-11-18 **07:07**（= UI 应用花字时下载），剪映 07:12 生成 .bak、07:13 保存草稿——都在流水线 06:50 最后一次写入之后 |

### 1.5 英文分支草稿实测

草稿路径：

```
C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\en_branch_console-52373c6f9e11_20261118_064813_970710\draft_content.json
```

- 「花字」「panel-text-flower」「互动立体」全部 **0 命中**。
- 含 15 处 `text-anim-text-en-*` 动画 ref —— node_10b 入场动画池正常写入，是**纯流水线产物**，未使用任何花字。

### 1.6 花字审计配方（可复用）

对任意剪映草稿检查花字使用：

1. `grep -o "花字" <草稿>/draft_content.json | wc -l` —— 计数；命中则用 `grep -o ".\{120\}花字.\{120\}"` 提取上下文（确认 category_id 是否 `panel-text-flower`）。
2. 追查引用方：`grep -o ".\{650\}<素材id前8位>" draft_content.json` 找到挂 ref 的 text 轨段（`material_id` → `materials.texts` 对应条目）。
3. VIP 判定：`grep -a -b -o "<effect_id>" rp.db` 取字节偏移，`tail -c +<offset> rp.db | head -c 1500` 提取面板记录，看 `extra.is_vip` 与 `business_info.json_str` 的 `paid_type`。

---

## 2. 第二部分：根因分析——为什么流水线从未写入 VIP 花字

### 2.1 术语澄清（关键前提）

代码库里「花字」一词被三种东西共用，只有第三种才是剪映面板里的原生 VIP 花字：

| 代码里的「花字」 | 实际含义 | 证据 |
|---|---|---|
| `default_text_style()`「默认花字样式」 | 普通 style dict（字体/字号/描边），Week 3 占位 | `jy_common/template_library.py:150-156` |
| node_10「花字/字幕动画」 | 只是**入场动画**（material_animations） | `nodes/node_10_inject_text_fx.py:1-7` docstring |
| 剪映原生花字（panel-text-flower） | **从未实现** | 全库 `panel-text-flower` 零命中 |

词汇混用的旁证：`graph.py:28/652`（node_10b 注释「英文花字/字幕动画」）、`state.py:225`（「花字动画/贴纸可观测字段」）、`monitoring/workflow_graph.py:180/185`（「注入中文/英文花字」）、`tests/integration/test_reorder_topology.py:237`（「10b 的花字已生效(阶段 4:原生 material_animations…)」）——均指入场动画而非原生花字。

### 2.2 五层根因

**① 数据层：没有花字池。**
资源库 `templates/fx_template.json` 与 `templates/text_resource_library.json` 中「花字/text_style/flower」零命中。对比：转场 395、特效白名单 18、字幕动画 78 都建了池并有收割通道，花字池从未立项。storyline 的 `recommend_text` 工具虽名叫「推荐字体/花字样式」（`storyline/firered_adapter.py:170`），实际产出只有 `font/size/color/position` 四个排版字段（`storyline_capabilities/recommend_text.py:45-58`，字体候选仅 SourceHanSansCN-Bold/Roboto-Bold 两个本地字体），喂给 node_08 的也只是普通字幕样式。

**② 写入层：`materials.effects` 无任何写入代码。**
文本唯一写入口 `make_text_material`（`nodes/_subtitle_texts.py:46-104`）只写 `materials.texts` 条目（content JSON + 描边 strokes + 私有键双形态）；node_10/10b 只写 `materials.material_animations` 并绑 text 轨段 ref（`nodes/node_10_inject_text_fx.py:98-151`）。而原生花字需要的 `materials.effects` 条目（effect_id/name/md5 path 等）+ 片段 refs 绑定，全项目无一行代码产出——第 1 部分审计已实证草稿里唯一的花字来自剪映 UI 手动操作。

**③ vendor 层：库能力缺失。**
`vendor/pyJianYingDraft/metadata/` 只有 `effect_meta`（视频特效）、`text_intro/text_loop/text_outro`（字幕动画）、`transition_meta`、`video_scene_effect`——**没有 text_style/花字元数据模块**，写入器无法表达花字素材形态（注：`video_scene_effect.py:6582` 的「烟花字」是视频场景特效，与文字花字无关）。

**④ 政策层：显式声明不承诺。**
`config.py:584-587`：最终 MP4 导出定为 FFmpeg，且「贴纸/视频特效/**花字样式不渲染**——差异作为 fidelity 明细写进导出报告与创建台事件，不是静默假成功」；`draft_ops/draft_exporter.py:20` 保真度表同口径。`config.py:645-646` 记录 2026-07-04 用户决定：已注入的 VIP 转场/特效/花字/贴纸「需重新联网下载才能渲染」故禁删剪映缓存——说明当时已明确花字属联网 VIP 资源，流水线选择了不主动写入这条路。

**⑤ zh/en 同因。**
node_10（中文）与 node_10b（英文）共用同一核心 `apply_text_fx`（`nodes/node_10_inject_text_fx.py:154-182`，docstring 第 164 行「与中文共用同一核心」），且英文草稿本身由中文草稿 fork 而来（`fork_draft_for_english_branch`）——英文草稿同样零花字，不是两条独立缺陷。

### 2.3 一句话总结

「花字」在项目词汇里长期是「字幕样式美化」的代称（占位样式 + 入场动画），剪映原生花字（VIP artistEffect 模板）从未进入任何一层的实现范围——数据无池、写入无通道、vendor 无模块、导出无支持，四层叠加导致流水线产物（zh/en 一致）永远不含原生花字。

---

## 3. 第三部分：若立项「花字池接入流水线」的落点（展望，未立项）

昨晚手动把「互动立体」加到 `text-zh-4` 上，剪映正常接受并渲染——证明草稿格式层面完全可挂花字。接入需要：

1. **建花字池**：元数据至少含 `effect_id / name / md5`。来源两种（可并用）：
   - UI 收割：对齐 2026-11-17 曲库池做法（computer-use 直连剪映 UI 逐个应用 → harvest 入库）；
   - `rp.db` 解析：`get_resources_by_category_id_*_flower_jianyingpro_*` 响应已在本地缓存，含全量 `effect_item_list`（id/title/md5/is_vip/分类），可直接提取建池。
2. **写入代码**：扩展 `jy_common/native_fx`，写 `materials.effects` 条目（字段对齐 UI 产出：`category_id=panel-text-flower`、`category_name=花字`、`effect_id`、`name`、`path`（本地缓存路径）、`resource_id`、`type=text_effect` 等）+ text 轨段 `extra_material_refs` 绑定；守卫对齐 node_10 哲学（只动我方 `text-zh-/text-en-` 素材，人工文本不动）。
3. **VIP 门禁与降级**：资源包本地缓存存在性检查（`Cache\artistEffect\<effect_id>\<md5>\`），缺失/VIP 未登录时 fail-closed 降级不写入，语义对齐 node_10 现有 degraded 模式（`node_10_inject_text_fx.py:115-116`），不伪装成功。
4. **FFmpeg 导出链不动**：维持 `draft_exporter.py` fidelity 明细口径（花字仍属「剪映客户端专属、导出不渲染」能力，报告中如实记录）。
5. **zh/en 一致性**：池轮换复用 `sha256(job_id)` 确定性机制，zh/en 传同一 job_id（对齐 node_09 转场池与 node_10 动画池的做法）。

---

## 4. 附录：证据文件路径清单

| 项 | 路径 |
|---|---|
| 中文草稿 | `C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\AutoVideo_console-52373c6f9e11\draft_content.json` |
| 写入审计日志 | 同目录 `.autovideo_draft_mutations.jsonl` |
| 英文草稿 | `C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\en_branch_console-52373c6f9e11_20261118_064813_970710\draft_content.json` |
| 花字资源包缓存 | `C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Cache\artistEffect\7407340226189757733\c093f229a5b9529beab4d27c00bc0c4f\` |
| 面板元数据缓存（VIP 判定） | `C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Cache\rp.db` |
| 文本素材构造 | `nodes/_subtitle_texts.py`（`make_text_material`） |
| 花字/动画注入节点 | `nodes/node_10_inject_text_fx.py`（node_10 中文 / node_10b 英文） |
| 样式占位 | `jy_common/template_library.py:150`（`default_text_style`） |
| 推荐工具 | `storyline_capabilities/recommend_text.py`、`storyline/firered_adapter.py:165-172` |
| 导出保真度 | `draft_ops/draft_exporter.py`、`config.py:584-587` |
| 缓存禁删政策 | `config.py:645-646` |
| vendor 元数据 | `vendor/pyJianYingDraft/metadata/`（无 text_style 模块） |

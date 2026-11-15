# assembly 三节点分析与剪映草稿影响面普查

> - **日期**：2026-11-15
> - **基线**：main @ `60a6ea4`（schema v9：关卡③已删除、render_video 已删除、B1 ASR 已移除）
> - **方法**：两路探索代理代码扫描交叉验证 + 真实运行产物实证（thread `console-606615102af3`，产物目录 `outputs/console-606615102af3/assembly/`）
> - **时效性**：本文全部结论绑定 schema v9 时点，行号基于 `60a6ea4`。后续治理（删节点/改边/改开关默认值）会使对应章节过时；引用前先核对当前拓扑（权威 = `graph.py` 建图代码 + `tests/integration/test_unreachable_nodes_wiring.py` 的 NEIGHBORS 表）。

## 0. 结论速览

1. **assembly_qc 组 6 个节点**（素材发现与探测 → 视觉观察 → 装配时间线 → 渲染与质量检查 →(修复回路)→ 写QC报告）是 `generate_draft` 之后的**质检旁路**：对原始素材独立产出证据链（`media.json` → 抽帧拼贴图 → `timeline.json` → `preview.mp4` → `report.md`），**对剪映草稿零写入**。
2. 三节点输出已在真机实证：本次运行 `timeline.json` 的 `llm_used=false / fallback_used=true`——模型输出未被采用，时间线由确定性兜底拼出（两段全片顺序拼接）。
3. assembly 产物影响草稿的**唯一通道是经关卡①的人工判断**（报告 + 预览视频作交叉参考），系刻意设计。
4. 全图 **45 个注册节点**（`graph.py` 的 `add_node` 计数核实）中，恒定对草稿零影响的共 **11 个**：assembly 6 + 封面 2（node_14/15）+ node_18 导出器 + 两个兜底终点；另有 GAT/PTAT 两个**显式门禁的条件式**节点默认零影响。
5. 20261111 旧普查（`docs/第四期开发/auto-video-editor_noop空转节点普查_20261111.md`）的四档结论**大半已过时**（模板推荐/render_video/node_16/GAT/PTAT/select_bgm 均已改变或删除），引用旧文档前先看本文第 5 章纠偏表。

---

## 1. assembly_qc 链定位：六个节点的质检旁路

### 1.1 链路与接线

```
generate_draft(node_05) → ①素材发现与探测 → ②视觉观察 → ③装配时间线
    → 渲染与质量检查 ──不达标──> 修复回路 ──> 回到渲染与质量检查复检
                └──达标/达重试上限──> 写QC报告 → 关卡①人工调整镜头顺序(node_06)
```

- 6 个节点注册于 `graph.py:508-513`；QC 开启时的接线在 `graph.py:585-601`（`generate_draft → assembly_discover_and_probe` :588，discover→observe :589，observe→build :590，build→validate :591，validate 的条件边 :592-599，repair_loop→validate 回环 :600，write_report→node_06 :601）。
- 路由函数 `route_after_assembly_qc`：`nodes/assembly/node_validate_render_qc.py:42-56`。
- 开关关闭（应急分支）：6 节点仅注册**不接边**（不可达），`generate_draft` 直连关卡①（`graph.py:604`）；接线正确性由集成测试断言（`tests/integration/test_workflow_graph_schema.py:204` QC 关=零 assembly 边、`:287` discover→observe→build 两条边）。

### 1.2 开关语义（ASSEMBLY_QC_GATE_ENABLED）

- 定义：`config.py:324-327`，env 可覆盖，默认 `true`（设计文档 ADR-3：每条视频强制过 QC 通道，但不通过时软降级不阻断）。
- **建图时**读取（`graph.py:584-585`）：改开关需重启进程重新编译图，不是运行时开关。
- 监控侧联动：`monitoring/workflow_graph.py:86` 用同一 flag 控制 6 节点的 `enabled`/`reachable` 展示语义。
- 重试上限：`ASSEMBLY_QC_MAX_RETRY`（`config.py`，默认 2），由 `route_after_assembly_qc` 用于达上限软降级判断（`node_validate_render_qc.py:54`）。

### 1.3 三个前置节点逐个拆解

#### ① 素材发现与探测 `assembly_discover_and_probe`

- 实现：`nodes/assembly/node_discover_and_probe.py:62`。
- **发现素材**：`_discover_source_files`（:26）单文件直取；目录只扫一级（非递归），扩展名白名单 `.mp4/.mov/.m4v/.mkv/.webm/.avi`；多输入去重合并（:44）。
- **ffprobe 探测**（ffprobe = FFmpeg 自带的媒体信息探测工具，只读元数据不解码画面）：`inspect_media`（`assembly_capabilities/media_probe.py:48-98`）取时长/码率/流信息/技术风险。
- **三类内容检测**：`analyze_media`（`media_probe.py:104-167`）——场景切换（`select=gt(scene,0.30)`）、黑帧（`blackdetect=d=0.2:pix_th=0.10`）、静音段（`silencedetect=n=-35dB:d=0.5`），按场景边界生成候选段。
- 产物：`outputs/<job_id>/assembly/media.json` → state 键 `assembly_media_artifact`（写入 :111-114）。
- 失败语义：无重试；探测异常被捕获记 `TOOL_EXECUTION_FAILED` 后直接返回（:102-109）。

#### ② 视觉观察 `assembly_visual_observe`

- 实现：`nodes/assembly/node_visual_observe.py:73`；上游产物缺失即契约错误早退（:88-95）。
- **关键帧抽帧**：按 **2fps 采样**（每秒抽 2 帧）、单视频上限 600 帧（默认参数 `assembly_capabilities/visual_observe.py:58-71`），OpenCV 逐帧解码，失败回退 ffmpeg 管道（:461-575）。
- **contact sheet**（缩略图网格拼贴图，把"画面长什么样"变成模型和人能一眼看的图）：`build_contact_sheets`（:604-699）——每张 4 列 × 最多 24 格、总宽 1568px、每格顶部画时间戳标签，文件名自带覆盖时间范围 `video_grid_<起>ms_<止>ms_p<序>.jpg`（:694-696）。
- **缓存**：`observation_signature`（视频路径/大小/mtime + 采样参数的哈希，:261-294）——同视频同参数直接复用，不重抽。
- 多视频：逐源写 `video_ingest_<i>.json` 再合并出 `video_ingest.json`（`node_visual_observe.py:46-70,104-141`）。
- 产物：`assembly/video_ingest.json` + 一组拼贴图 → state 键 `assembly_ingest_artifact`。
- 特点：**纯确定性、零模型**（产物 notes 明写 "No model API was called"，`visual_observe.py:174`）。

#### ③ 装配时间线 `assembly_build_timeline`

- 实现：`nodes/assembly/node_build_timeline.py:39`；核心装配逻辑 `build_timeline_from_paths`（`assembly_capabilities/build_timeline.py:618`）。
- **全链唯一调模型的节点**：
  1. 收集候选段（`_collect_candidate_segments` :110；无候选时整段占位 :133-149），截断到 `MAX_LLM_CANDIDATES=24`（:47）；
  2. `_call_llm_for_selection`（:247-277）经 `storyline_capabilities/vlm_client.chat_json` 调用（VLM = 视觉语言模型，能同时看图和读文本的多模态模型），输出 schema 见 `SELECTION_JSON_SCHEMA`（:75-104）；contact sheet 前 8 张图作 media 入参；
  3. system prompt 在 `assembly_capabilities/prompts/assembly_build_timeline/zh/system.md`，角色是"多素材组装导演"，按"分组评分选段 → 定版式结构 → 搭 timeline"输出 `selected_segments[{index, reason, beat, order}]`；
  4. 按时间线游标顺序拼成 timeline，画布默认 1080×1920@30fps（:50-53，竖屏短视频，LLM 可覆盖）。
- **模型客户端从哪来**：生产运行时由 `agent_gate/runner.py:427-429` 把 mcode 后端客户端 `set_default_client` 注入 `vlm_client` 全局槽；没有任何客户端注册时（如裸测试）`get_default_client()` 兜底返回 `StubLLMClient`（`storyline_capabilities/vlm_client.py:147-153`），`register_default_clients()` 是留空的阶段5 hook（:161-168）。
- 失败语义：**软降级**——模型失败/空输出不抛异常，落占位 timeline 保证下游总有合法输入（节点 docstring :18-21）；完全无候选时打 `empty` 标记（:611-613）让下游 QC 自然升级进修复回路。
- 产物：`outputs/<job_id>/assembly/timeline.json` → state 键 `assembly_timeline_path`（写入 :95-101）。

### 1.4 与上游 generate_draft 的区别

| 维度 | `generate_draft`（node_05） | assembly 三节点 |
|---|---|---|
| 输入对象 | storyline 链（17 节点）产出的**叙事计划** CanonicalTimeline（节点 :84-126 经 mapper 消费） | **原始视频文件本身**（绕过全部上游理解成果，重新独立探测） |
| 产出物 | **交付物本体**：剪映草稿 `draft_content.json`（含云音乐自动铺轨 :171-200），人工马上要在剪映里编辑 | **质检证据链**：media.json / 抽帧拼贴图 / timeline.json，最终只汇入 report.md 给人看 |
| 是否调模型 | 否（翻译计划为草稿） | ③调 VLM，①②纯工具 |
| 与草稿的关系 | 生产草稿 | **不读也不写草稿** |

设计文档对这个边界的原话判定（`docs/第三期开发/相似功能节点分析与设计执行计划.md:423-433`）：两条线的 timeline「功能相似、数据格式不同、互不读取」——storyline 线负责生成草稿，assembly 线负责**事后质检取证**，登记不合并。`docs/第三期开发/auto-video-editor_剪映草稿效果缺失根因分析与修复设计执行计划_20261108.md:24` 进一步明确：**Assembly QC 不是剪映特效合成节点，其产物不会自动合并进剪映草稿**（:348-352 逐节点列产物与"是否进草稿=否"）。

一句话：`generate_draft` =「照计划把草稿做出来」；assembly 链 =「做完之后，自己独立把素材看一遍，验证这份草稿赖以成立的素材判断站不站得住」。

### 1.5 与下游的区别：渲染与质量检查 / 修复回路 / 写QC报告

红圈三节点是「**生产证据**」，下游是「**消费证据做审判**」：

- **`assembly_validate_render_qc`**（`nodes/assembly/node_validate_render_qc.py:59`）三连动作：
  1. `validate_timeline` 结构校验 → `timeline_validation.json`；
  2. `render_preview` 用 ffmpeg 把 timeline 真渲染成 `preview.mp4`；
  3. `qc_preview` 扫描预览片（黑帧/卡帧/静音/音量/与时间线期望对齐，`assembly_capabilities/qc_preview.py:46` 起）→ `preview_qc_report.json`。
- **路由**（`node_validate_render_qc.py:42-56` + `_qc_status_from_data` :31-39）：有 error 级问题 → `escalated` → 进修复回路；只有 warning → `pass_with_warnings`；干净 → `pass`。validate/render 本身失败也直接 `escalated`（:92-123）。达重试上限（默认 2）软降级放行去写报告。
- **`assembly_repair_loop`**（`nodes/assembly/node_repair_loop.py:268`）：构造最小 patch（**目前仅 metadata marker 占位** :58-74）+ 可选视觉证据复核（开关 `ASSEMBLY_REPAIR_VISUAL_EVIDENCE`，从 QC 报告日志抽可疑时段重采样放大看图，:295-347）→ `timeline_diff(apply=True)` 写回（:349-359）→ 重试计数 +1（:362）→ **固定回到 validate_render_qc 复检**。注意：写回目标是 `assembly/timeline.json` 自己（`assembly_capabilities/timeline_ops.py:747`），**不是草稿**。
- **`assembly_write_report`**（`nodes/assembly/node_write_report.py:112`）：汇总全部产物写 `assembly/report.md`，绝不抛异常、失败降级空 section；报告路径被关卡①（node_06）的通知文案引用（`nodes/node_06_human_reorder.py:36-44`）。

本质区别：红圈三节点回答「素材里有什么、该拼什么样」，下游回答「拼出来的东西渲染出来到底有没有毛病」。一个生产，一个审判。

### 1.6 三个前置节点彼此的递进

层层收窄，前者产物是后者的输入：

1. **探测 = 元数据层**：素材有哪些、多长、技术参数、哪里有切点/黑帧/静音（不看画面内容）；
2. **观察 = 视觉内容层**：画面实际长什么样（有图有真相，但仍不做决策）；
3. **装配 = 决策层**：基于前两层，选哪些段、按什么顺序拼成什么结构（唯一动用模型判断的环节）。

---

## 2. 三节点输出内容（真实产物实证）

实证对象：thread `console-606615102af3`（2 个源视频：`inputs/0001.mov` 30.88 秒、`inputs/0002.mov` 27.94 秒）。

### 2.1 `media.json`（素材体检单）

顶层是一个 **JSON 数组，每个源视频一条**，每条 5 个字段：

| 字段 | 内容（本次真实值） |
|---|---|
| `source` | 源视频绝对路径 |
| `probe` | ffprobe 全量探测：时长 30.88s/27.94s、文件 28.7MB/30.9MB、总码率 7.44/8.85 Mbps、完整流列表（HEVC Main10 1920×1080 + AAC 立体声）、`summary`（分辨率/帧率/编码/色彩空间）、`technical_risks`（本次每源 2 条：旋转元数据 -90°（info）、HDR 传输特性 arib-std-b67 需 SDR 色调映射（warning）） |
| `analysis` | **只有计数**：场景切点 0、候选段 1、黑帧区间 0、静音区间 0（`scene_threshold=0.3`） |
| `probe_text` / `analysis_text` | 给日志/人看的文本摘要 |

**两个关键细节**：

1. **明细不在 media.json 里**。场景切点列表、候选段边界、黑帧/静音区间的全量明细写在**伴随产物**（sidecar，指主产物之外另落的配套文件）`media_analysis.json`（`media_probe.py:126-147`），落在 RunContext 工作目录——主图调用未传 `output_json`，所以 `media.json` 的 `analysis` 只保留 counts。
2. **连带缺陷**：装配节点的 `_collect_candidate_segments` 读的是 `analysis.candidate_segments`（`build_timeline.py:132`），主图上恒为空 → **恒走"整段占位"兜底 `placeholder_full`（:133-149）**——即使 LLM 调用成功，也没有细粒度候选段可选。这是提升 QC 链选段能力时的首要改进点（见附录 B）。

耗时构成：ffprobe 本身仅 0.05s/源，节点 44s 大头是每源 ~21.8s 的场景/黑帧/静音三路 ffmpeg 扫描 × 2 源。

### 2.2 `video_ingest.json` + `video_grid_*.jpg`（照片墙）

- `video_ingest.json`（多视频合并结构：`tool` / `sheet_paths` / `frame_paths` / `per_source` / `source_count`，`node_visual_observe.py:64-70`）。每源记录：采样参数（`sample_fps=2.0`、`sampled_frames`、解码器 `reader`）、**每帧时间戳列表**（`raw_frame_timestamps_sec`）、逐帧 jpg 路径、拼贴图路径、缓存签名、notes。
- 拼贴图真实文件名（实证）：`video_grid_0ms_11280ms_p00.jpg`、`video_grid_11800ms_23080ms_p01.jpg`……**文件名自带该张覆盖的时间范围**，每张约 11 秒、4 列 × 最多 24 格、宽 1568px、每格顶部印时间戳。抽帧图与拼贴图落在缓存目录 `.video_agent/video_frames/<源>_<哈希>_full/`。
- 节点 158s 耗时 = 对两源逐帧解码抽帧。

### 2.3 `timeline.json`（独立拼装方案）

真实文件自报家门（`project` 元数据）：

```json
"selected_segments_count": 2, "candidate_segments_count": 2,
"truncated_for_llm": false, "llm_used": false, "fallback_used": true,
"elapsed_seconds": 244.0
```

**修正记录**：分析过程中曾据 244s 耗时推测"真模型在分析 contact sheet"，真实产物的 `llm_used=false` **修正了该推测**——模型输出最终未被采用（调用尝试后失败/超时/返回不可用），时间线由**确定性兜底逻辑**生成。节点内其余步骤都是轻量 JSON 操作，244s 应主要耗在模型调用尝试本身（此为合理推断，产物未记录耗时分布）。

兜底生成的实际内容：

- `tracks[0].clips` 只有 2 个 clip，**两段全片首尾相接**：`clip_0000` = 0001.mov 整段铺在时间线 0→30.88s，`clip_0001` = 0002.mov 整段铺在 30.88→58.82s；每段 `reason` 诚实标注 `"placeholder full-segment fallback (start=0.000s)"`。
- `sequence`：1080×1920@30fps，总时长 58.82s。
- `assets`：a1/a2 两条源引用（`width/height` 是 16×16 **占位值**，非真实分辨率——已知占位字段）。
- `markers`：空（无 beat 结构标签）；`metadata.transcript_chars=0`（B1 ASR 移除后的正常状态）。

顶层结构（字段清单）：`version`（"assembly-build-timeline-1"）/ `task` / `project`（LLM 留痕：name、selected_strategy、editorial_structure、selected/candidate_segments_count、truncated_for_llm、llm_used、fallback_used、elapsed_seconds）/ `sequence`（fps、duration、canvas、output_canvas）/ `assets[]` / `tracks[].clips[]` / `markers[]` / `metadata`。每个 clip 字段：`id`（clip_0000…）、`asset_id`、`source`、`start`/`end`（源内入出点，秒）、`duration`、`timeline_start`/`timeline_end`、`speed`/`volume`/`opacity`（均 1.0）、`reason`、`candidate_index`、可选 `beat`。

### 2.4 汇入 `report.md`

本次真实报告：QC 状态 `pass_with_warnings`；修复循环重试 0 次；素材清单 2 条；timeline 校验 `pass`（0 error / 0 warning）；质检 `pass`，唯一 1 条警告 **「audio mean volume is very low」**（原始素材环境音很小）；尾注"建议把 `preview.mp4` 与剪映草稿一起过一遍"。

---

## 3. 对剪映草稿的影响

### 3.1 程序化影响 = 零（三层代码证据）

1. **写草稿的入口全部收敛在一处**：所有草稿落盘都走 `draft_ops/atomic_writer.py` 的 `safe_write_draft`（:159-241，底层 `atomic_write_draft` :37 / `atomic_write_draft_pair` :79 双写 `draft_content.json`+`draft_info.json`），经 `nodes/_draft_io.py`（`write_draft` :169-195、`apply_and_write` :232-243）。调用方：node_05（:28,:55）、node_07（:218）、node_08（:905）、node_08b、node_09、node_09b、node_10、node_11、node_12（:283）、node_13（:238）、node_16a（经 helper，:137）、node_17（:155）。对 6 个 assembly 节点及其下游全量检索：**零调用**，唯一命中是 `report.md` 里的提示文案字符串（`node_write_report.py:9,:129,:157`）。
2. **修复回路也只改自己的文件**：`assembly_repair_loop` 的 `timeline_diff(apply=True)` 写回目标是 `assembly/timeline.json`（`nodes/assembly/node_repair_loop.py:349-359` → `assembly_capabilities/timeline_ops.py:747`），apply 前校验失败会拒绝修改（timeline_ops.py:740-745）。
3. **assembly 产物在装配链外只有一个读取方**：`node_06_human_reorder.py:43-48` 读 `assembly_report_path`，且**只拼进通知文案**（print/日志），无文件 IO、无草稿读写。关卡①人工确认恢复后，node_07 起继续基于 node_05 生成的草稿（`draft_path`）做速度适配等修改，全程不读任何 `assembly_*` 状态键（assembly 键定义见 `state.py:319-339`）。

### 3.2 信息影响 = 唯一通道是"经关卡①的人工眼睛"

- 报告路径被写进关卡①（`node_06_human_reorder`，interrupt 提示"请在剪映客户端手动调整分镜顺序，完成后确认继续"）的通知文案；人工在剪映里调镜头顺序时，可以对照独立渲染的 `preview.mp4` 和 `report.md` 做**交叉参考**——影响的是"人工决定怎么改草稿"，不是程序改草稿。
- 本次运行就是活例：QC 检出"音量很低"，它的归宿只是写进报告；没有任何代码路径因此去动草稿的音量轨（后续 node_13 的音量处理是主链自己的逻辑，与 assembly 零读取关系）。

### 3.3 设计取舍与代价

- 设计文档 ADR（`docs/integration/video-agent-kit集成auto-video-editor设计执行计划.md`）：QC 默认强制开启但**软降级**——质检不通过重试到上限后不中断流水线，把未解决问题如实写进报告照常交人工（ADR-3）；接入点选在 `generate_draft` 与关卡①之间（ADR-2），动机正是给关卡①的人工提供独立预览+报告作交叉参考。
- **好处**：质检链永远不会污染交付物（草稿零风险）。
- **代价**：QC 发现的问题只能靠人工落回草稿，程序不会自动把修正写回去；且每条视频到关卡①前多出三节点开销——本次真机实测 44s + 158s + 244s ≈ **7.4 分钟**（其中 244s 含一次未产出结果的模型调用尝试）。

---

## 4. 全图 45 节点影响面普查

拓扑权威：`graph.py` 建图代码（`_build_state_graph`）+ `tests/integration/test_unreachable_nodes_wiring.py`（NEIGHBORS 表 :136-167，可达性/零出边守卫 :215-267）+ `web/node_map/workflow.json`。注册节点共 **45 个**（`add_node` 计数核实）。

### 4.1 判定"影响草稿"的标准（满足任一即算有影响）

- (a) 节点自身调用草稿写入口（`draft_ops/atomic_writer`、`nodes/_draft_io`、`jy_common/draft_writer`）；
- (b) 它写入 state/磁盘的产物，被某个草稿写入节点的逻辑直接或间接消费（含作为 prompt 内容注入下游 LLM 节点、被 mapper/plan 消费进 CanonicalTimeline）；
- (c) 它的输出改变路由/门控决策，而该决策会导致 (a)/(b) 发生或改变草稿的后续处理（如修复回路、门控 resume 后改草稿）。

只产出报告/QC/日志/前端展示产物、或产物无人消费的节点 = 无影响。

### 4.2 A 档：恒定无影响（产物有消费者，但只做观测/交付，永不触草稿）

| 节点 | 产物 | 为什么对草稿零影响 |
|---|---|---|
| assembly 6 节点 | 见第 1-3 章 | 零草稿写入口引用；repair_loop 只写回自己的 timeline.json |
| **node_14_make_covers** | `draft_dir/covers/*.png` | 只往草稿目录放**旁挂图片**，两个草稿 JSON 零变更（`nodes/node_14_make_covers.py:122-137`）；且 covers 不在交付 QA 的必需特性集（`draft_ops/draft_integrity.py:615-624`，REQUIRED_FEATURE_KEYS 不含 covers），缺失只降级展示、不阻断不修复 |
| **node_15_localize_covers_en** | 同上，加 `en_path` | 同上（`nodes/node_15_localize_covers_en.py:109-130`） |
| **export_final_video（node_18）** | MP4 或 skipped 标记 | 默认 `FINAL_EXPORT_ENABLED=False` 直接 skip（`config.py:570-571`；skip 路径 `nodes/node_18_export_video.py:70-77`）；即使开启也**只读**权威草稿去渲染 MP4，从不写草稿（:107-127）。已注册且可达（`graph.py:525`；join 后条件边 `graph.py:659-670`） |
| **escalate_guardrail_failure** | error_log/status_log | 纯兜底终点：写日志后直接 END（`graph.py:267-278`） |
| **delivery_qa_failed** | 无（只 raise） | 交付 QA 终局失败：抛异常终止运行，不静默、不写任何东西（`nodes/node_delivery_qa_repair.py:14-18`） |

### 4.3 条件档：默认无影响，开闸后有影响

**GAT（`storyline_generate_ai_transition`）+ PTAT（`storyline_plan_timeline_ai_transition`）**——AI 转场两节点：

- **默认生产姿态 = 零影响**：双门禁 fail-closed（Provider 未配置 + 未批准，`config.py:61-63,:73-80`）→ GAT 每轮只写一份**转场时长估算审计 JSON**，0 次外呼（`node_generate_ai_transition.py:181-187,:206-231`）；PTAT 读后判定"无可挂条目"纯透传，零 state 写（`node_plan_timeline_ai_transition.py:291-298`）。
- **开闸后（配 MiniMax Key + 批准）= 真实影响草稿**：GAT 用 ffmpeg 首/尾帧抽取 → LLM 运动提示词 → MiniMax Hailuo-02 HTTP 生成真实转场 MP4（`storyline_capabilities/ai_transition_provider.py:99-107`）→ PTAT 把它物化为 `source_media` 新条目（:217-235）+ 相邻片段间插入转场 Clip（:236-247）→ 回写 timeline plan（:337-350）→ 经 qa_gate 校验（`graph.py:802-808`）→ join → node_05 → mapper 落进剪映草稿（`storyline/mapper.py:77,:278,:303`）。
- 性质说明：20261111 普查判它们"Stub 假数据白跑"，2026-11-12 noop 阶段2（`0a0bae98`/`c667182`）真实现后已变成"显式门禁的条件式影响"，不再是假数据空转。

**storyline_search_web_topic**：仅在检索开关关闭/无检索词的轮次只落 skipped 标签（该轮零影响，`node_search_web_topic.py:45,:84-89`）；正常运行时把检索结果注入文案生成 prompt（`node_generate_script.py:53-56`），属间接影响。

### 4.4 边界澄清（易误判点）

1. **两个人工关卡节点自身都不改草稿**：node_06 interrupt 前零写、resume 后只写日志+通知（`nodes/node_06_human_reorder.py:12-44,:58-79`）；node_12 已有 BGM 时零 interrupt 放行、缺失才挂起，resume 后做中→英音轨镜像写草稿（那是它的直接写入，`nodes/node_12_human_add_bgm.py:17-21,:283`）。真正改草稿的永远是 resume 之后的后续节点 + 人工在剪映客户端里的外部操作。
2. **node_16a 的"直接草稿写"是内容净零**：英文分支草稿 fork 自 node_08 之前的快照，`materials.texts` 为空（`nodes/node_08b_add_subtitles_en.py:8-11` 明证），`_write_marker` 里 `0 <= idx < len(texts)` 的循环**永不命中**（`nodes/node_16_translate_subtitles.py:131-137`）——`write_draft` 会重写文件但内容一字不变。英文文本实际落盘全靠 node_08b 整表替换（:82）。所以 node_16a 对草稿的真实影响走的是间接通道（`subtitle_segments_en` → node_08b，`nodes/node_16a_translate_and_check.py:36-50,:74-110`）；无源字幕时 16a 正常 skip（`graph.py:630`）。
3. **join_before_delivery 与 delivery_qa_repair 算有影响**：join 自身零磁盘写、只出结构化 QA 报告（`nodes/node_join_before_delivery.py:39-46`"记录不抛"），但其 `gate_failed` 路由会触发 repair（`graph.py:659-668,:243-261`）；`delivery_qa_repair` 重跑尾链函数（inject_fx/inject_text_fx 中英/inject_sticker/adjust_volume），副作用真实落盘改草稿（`nodes/node_delivery_qa_repair.py:37-52`）。
4. **storyline_render_video（故事线预览）已不存在**：2026-11-12 边治理阶段5 整体删除（`graph.py:531-532` 注释自证），console 的故事线预览行同批移除（`web/console.html:884-885`）。其存在期间产物也只写独立预览目录，从不触主草稿。

### 4.5 B 档（节点级死产物）：已清零

- 2026-11-12 noop 治理阶段5 删掉 13 个死键：`fx_summary`（批0 `0048af8` 修注释 / 批2 `bb1fb26` 删键）、`reorder_notified`/`bgm_notified`/`checkpoint3_triggered`/`layout_issues_source`（批1 `20fbb18`）、`volume_adjusted`/`text_fx_summary`（批2 `bb1fb26`）、`join_qa_issues`/`draft_published_dir`/`draft_publish_error`/`draft_source_normalized_path`（批3 `66b0109`）、`en_dub_audio_path`/`tts_run_id`（批4 `7123662`）。
- storyline 17 节点链如今**每个节点都有在册消费方**，无 B 档死节点。
- 仍存在的 12 个死产物**键**是处置矩阵**有意保留的审计留痕**（如 `script_llm_trace.json`、`bgm_manual_draft_sha`、`assembly_repair_evidence_path` 等），属键级现象，不构成"无影响的节点"。

---

## 5. 对照 20261111 旧普查的纠偏表

旧文档：`docs/第四期开发/auto-video-editor_noop空转节点普查_20261111.md`（基线 `04019c8`）。**引用旧文档前先核对本表**。

| 旧结论 | 今日状态 |
|---|---|
| 模板推荐 = 结构性 noop（零出边） | ❌ 已过时：真实现 BM25 检索 + 注入文案生成 prompt，间接影响草稿（noop 阶段1a/1b `853423a`/`9e2b3ab` + 边治理阶段4 `8c4add8`；出边 `graph.py:706,:710-716` 双源屏障；注入链 `node_generate_script.py:82,:88-96,:112` → `storyline_capabilities/generate_script.py:63,:73-93`） |
| storyline_render_video 默认空转 | 节点已彻底删除（边治理阶段5 `7786713`；此前 noop 阶段3 `348442e` 曾短暂改造为真实 FFmpeg 预览，被用户指令推翻） |
| node_18 默认 skip | ✅ 仍成立（唯一原样保留项） |
| node_16 死注册 | ❌ 已过时：shim 已删（noop 阶段4 `cda4507`），文件降级纯函数库，经 node_16a 写英文分支草稿 |
| GAT/PTAT 白跑（Stub 假数据） | ⚠️ 性质已变：Stub 已删、MiniMax 真实现 + 双门禁（noop 阶段2 `0a0bae98`/`c667182` + 边治理阶段6 `fde439b`）——默认零影响、批准后物化进草稿 |
| select_bgm 只选 ID 不落轨 | ❌ 已过时：现有三条落草稿通道（① node_05 云音乐自动铺轨 `node_05_generate_draft.py:171-200`；② mapper bgm_ref `storyline_capabilities/plan_timeline_pro.py:163-171` → `storyline/mapper.py:105-113`；③ BGM 节拍对齐决定片段时长 `storyline_capabilities/timeline_planner.py:377`）；"GS/GV 完成各触发一次互相覆盖"的病已随 2026-11-14 单源边（GV→SB，`graph.py:722-737`）消除 |
| （关卡③布局人工复核，普查时为对照组） | 节点已于 2026-11-15 整体删除，10b→17 直连（`graph.py:634-637`） |

---

## 6. 全节点影响分类总表（45 节点）

影响分类：**直接**=自身写草稿；**间接**=产物被写草稿节点消费（含 prompt 注入）；**路由**=改变流程走向进而影响草稿处理；**条件**=默认无影响、开闸后有；**否**=对草稿零影响。

### storyline 链（17 个，产物汇入 storyline_plan → generate_draft）

| 节点 | 分类 | 依据（关键消费链） |
|---|---|---|
| storyline_load_media | 间接 | media_artifact → PTP source_media → mapper materials.videos（`node_load_media.py:61`；`mapper.py:77-101`） |
| storyline_split_shots | 间接 | shots_artifact → understand 链（`node_split_shots.py:117`） |
| storyline_understand_clips | 间接 | understanding_artifact → filter/STR/GS prompt（`node_understand_clips.py:69`） |
| storyline_filter_clips | 间接 | filtered_clips → group（`node_filter_clips.py:67`） |
| storyline_group_clips | 间接 | groups_artifact → STR/GS/SB/RTR/PTP/GAT（`node_group_clips.py:67`） |
| storyline_script_template_recommendation | 间接 | 模板正文注入 GS prompt（`node_generate_script.py:82-95`） |
| storyline_search_web_topic | 间接（条件） | 检索结果注入 GS prompt（:53-56）；关/无词轮次零影响 |
| storyline_generate_script | 间接 | narration → node_08 字幕文本（`node_generate_script.py:156-157`；`node_08_add_subtitles.py:171-172`） |
| storyline_generate_voiceover | 间接 | 真实 TTS WAV → mapper materials.audios + 配音段（`node_generate_voiceover.py:97-135`；`mapper.py:118-132,:197-219`） |
| storyline_recommend_text | 间接 | text_style_plan → node_08/08b 样式叠加（`node_08_add_subtitles.py:880-885`；`node_08b_add_subtitles_en.py:50-57`） |
| storyline_select_bgm | 间接 | 三通道：node_05 自动铺轨 / mapper bgm_ref / 节拍对齐定片长（见第 5 章） |
| storyline_recommend_transition | 间接 | transition_plan → node_09 逐边界转场（`node_recommend_transition.py:59-60`；`node_09_inject_fx.py:14,:80`） |
| storyline_plan_timeline_pro | 间接（核心） | timeline_plan → join → node_05 mapper（`node_plan_timeline_pro.py:133-143`；`join_storyline.py:122-124`） |
| storyline_generate_ai_transition（GAT） | 条件（默认否） | 默认只写估算审计 JSON；批准+Provider 后经 PTAT 物化进草稿 |
| storyline_plan_timeline_ai_transition（PTAT） | 条件 | 有可挂条目时物化 SourceMedia+Clip 重写 timeline_plan；无可挂=透传 |
| storyline_qa_gate | 路由 | `storyline_qa_route_signal` 驱动 retry → group_clips 重跑整链 → 重生成时间线 → node_05 写草稿（`nodes/storyline/qa_gate.py:139-156,:226-251`；`graph.py:813-820`） |
| storyline_join | 间接（核心） | 反序列化 timeline → state.storyline_plan 供 node_05（`join_storyline.py:122-124`） |

### 主链（18 个）

| 节点 | 分类 | 备注 |
|---|---|---|
| generate_draft（node_05） | 直接 | safe_write_draft + 云音乐自动铺轨 |
| node_06_human_reorder（关卡①） | 否（自身）/门控 | interrupt/resume 机制不改草稿，人工外部改 |
| node_07_speed_fit | 直接 | write_draft（:216-222） |
| node_08_add_subtitles | 直接 | apply_and_write（:905） |
| node_08b_add_subtitles_en | 直接 | 整表写英文字幕（:82） |
| node_09_inject_fx | 直接 | 中英两份（:314,:324,:334） |
| node_09b_inject_opening_hook | 直接 | apply_and_write（:271） |
| node_10_inject_text_fx | 直接 | 中文文本特效（:164） |
| node_10b_inject_text_fx_en | 直接 | 英文字幕入场动画（:192-216,:164）；10b→17 直连（`graph.py:636-637`） |
| node_11_inject_sticker | 直接 | apply_and_write（:236） |
| node_12_human_add_bgm（关卡②） | 直接（条件） | 已有 BGM 零 interrupt；resume 后镜像音轨（:283） |
| node_13_adjust_volume | 直接 | write_draft（:238） |
| fork_draft_for_english_branch | 直接 | 建英文分支草稿 copytree + 剪映草稿库注册（`node_fork_english_branch.py:63,:97,:100-110`） |
| node_16a_translate_and_check | 直接（条件）+间接 | 直写为内容净零（见 4.4）；真实影响经 subtitle_segments_en → node_08b；无源字幕 skip |
| node_17_inject_english_tts | 直接 | 英文配音写入 en 草稿（:121,:155） |
| node_14_make_covers | 否 | 封面旁挂文件 |
| node_15_localize_covers_en | 否 | 同上 |
| escalate_guardrail_failure | 否 | 兜底终点 |

### assembly 链（6 个）

| 节点 | 分类 |
|---|---|
| assembly_discover_and_probe / assembly_visual_observe / assembly_build_timeline / assembly_validate_render_qc / assembly_repair_loop / assembly_write_report | 否（全部，见第 1-3 章） |

### 交付尾链（4 个）

| 节点 | 分类 | 备注 |
|---|---|---|
| join_before_delivery | 路由（算有影响） | 自身只出 QA 报告；gate_failed → repair 重跑尾链改草稿 |
| delivery_qa_repair | 间接直接 | 重跑尾链函数，副作用落盘 |
| delivery_qa_failed | 否 | 只 raise 终止 |
| export_final_video（node_18） | 否 | 默认 skip；开启也只读草稿 |

---

## 附录 A：关键 file:line 索引（基线 60a6ea4）

- 建图/接线：`graph.py:508-513`（assembly 注册）、`:584-604`（QC 开关接线）、`:630`（16a skip）、`:634-637`（10b→17）、`:649-652`（11→12→13→join）、`:659-670`（join 条件边→repair/node_18）、`:706,:710-716`（STR 双源屏障）、`:722-737`（GV→SB 单源边）、`:793-808`（GAT/PTAT/qa_gate）、`:813-826`（qa 路由→join→node_05）
- assembly 实现：`nodes/assembly/node_discover_and_probe.py:62`、`node_visual_observe.py:73`、`node_build_timeline.py:39`、`node_validate_render_qc.py:42-56,:59`、`node_repair_loop.py:268,:349-359`、`node_write_report.py:112,:127-160`
- assembly 能力层：`assembly_capabilities/media_probe.py:48-167,:277-325`、`visual_observe.py:58-71,:125,:604-699`、`build_timeline.py:47,:110-149,:247-277,:345-358,:618`、`timeline_ops.py:740-747`、`qc_preview.py:46`
- 模型注入：`agent_gate/runner.py:427-429`、`storyline_capabilities/vlm_client.py:147-168`
- 草稿写入口：`draft_ops/atomic_writer.py:37,:79,:159-241`、`nodes/_draft_io.py:169-195,:232-243`
- 草稿影响证据：`nodes/node_06_human_reorder.py:36-48`、`state.py:319-339`、`draft_ops/draft_integrity.py:615-624`
- AI 转场：`nodes/storyline/node_generate_ai_transition.py:181-231`、`node_plan_timeline_ai_transition.py:217-247,:291-298,:337-350`、`storyline_capabilities/ai_transition_provider.py:99-107`、`config.py:61-80`
- 测试：`tests/integration/test_workflow_graph_schema.py:204,:287,:319`、`tests/integration/test_unreachable_nodes_wiring.py:136-167,:215-267`
- 监控：`monitoring/workflow_graph.py:81`（SCHEMA_VERSION=9）、`:84,:86,:112-141,:144`
- 设计文档：`docs/integration/video-agent-kit集成auto-video-editor设计执行计划.md`（ADR-1~5、§七 6 节点设计）、`docs/第三期开发/相似功能节点分析与设计执行计划.md:423-448`、`docs/第三期开发/auto-video-editor_剪映草稿效果缺失根因分析与修复设计执行计划_20261108.md:24,:348-352`、`docs/第四期开发/auto-video-editor_noop空转节点普查_20261111.md`（旧普查，引用前看第 5 章）

## 附录 B：改进点备忘（均为分析中发现，未立项）

1. **media.json 只存 counts 不存候选段明细**：明细在 sidecar `media_analysis.json`（主图调用未传 `output_json`），导致 `assembly_build_timeline` 主图上恒走"整段占位"兜底——即使模型调用成功也没有真选段空间。若要提升 QC 链选段能力，需让 media.json 携带 candidate_segments 或让 build_timeline 读 sidecar。
2. **GAT/PTAT 开闸评估**：AI 转场真实现已就绪，默认双门禁关闭；若评估开闸需配置 MiniMax Key 并走批准流，届时转场 MP4 会真实进入草稿。
3. **封面纳入 QA 必需集评估**：node_14/15 的 covers 目前缺失只降级展示，若产品上封面是必需交付物，可评估纳入 `REQUIRED_FEATURE_KEYS`。
4. **node_16a 内容净零直写可清理**：`_write_marker` 的直写在 en 分支上永不生效（texts 为空），可简化为纯间接通道（subtitle_segments_en → node_08b），减少一次无效草稿重写。

---

*本文由会话分析整理生成（2026-11-15）；三轮分析相互衔接：第 1 章起于节点图截图（thread console-606615102af3）中红圈的三个 assembly 节点，第 2-3 章实证其输出与草稿隔离，第 4-6 章扩展为全图普查。*

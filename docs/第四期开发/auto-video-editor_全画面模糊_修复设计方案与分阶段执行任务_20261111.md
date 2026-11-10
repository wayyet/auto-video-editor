# auto-video-editor 全画面模糊：修复设计方案与分阶段执行任务（重写版）

> 日期：2026-11-11
> 本文档状态：**阶段 0/1 与阶段 2-5 已全部执行（2026-11-11）**。阶段 0/1：两层根因定案并已修复——①模糊=fx_template"抖动模糊"0.3 全片挂载（video_effects 已清空,node_09 降级不注入）;②右侧条纹=native_draft `_CROP_TEMPLATE.lower_right_y=0.0` 退化裁切(已改 1.0);display 口径探测(`probe_video_dimensions`)已落地。阶段 2：canonical 归一化引用（R3）已修复——`source_media` 全量建立 normalized media map 并回写 file_uri,归一化/复检全程显示口径,复检不符降级并标 `MEDIA_NORMALIZATION_FAILED`,draft_integrity 新增 `check_video_material_aspect` 防线。阶段 3：逐片段速度护栏已落地——`STORYLINE_MIN_CLIP_SPEED=0.5`（config）,planner 逐段上限+溢出重分配+素材不足告警、contract 速度不变量硬失败（CLIP_SPEED_INVALID）、mapper 写真实 speed、node_07 拉长护栏+按源跨度变速、qa_gate 逐段复查、split_shots max-shot 平衡分段（不再二次切出 0.883s 残尾）。阶段 4：`cover_source_frame.jpg` 生产者（node_14 入口,ffmpeg 抽帧+非黑帧挑选+失败显式留痕）与双形态标题解析（`text_display_text`）已修复。阶段 5：画质交付报告 `draft_ops/quality_report.py`（三方尺寸对照/逐段 speed/renderer=jianying_manual|ffmpeg 标记）已接入 `build_join_qa_report.quality`。**测试基线：单测 1757 全绿;集成 134 例中 8 失败经 stash 对照逐字复现=存量,零新增。** 遗留：真机端到端验收（重启服务后新建任务+剪映手动导出 ffprobe 复核 1080×1920+抽帧目检）待操作员执行。证据主文档：`outputs/console-64da836e2f3f/baseline/ab_test_result.md`（§10-11）。
> 执行约定：后续修复开发**以本文档为唯一权威版本**；前置两份文档降级为参考证据。

**前置文档**（同目录）：

1. 《生成视频全画面模糊根因分析_console-64da836e2f3f_20261111.md》（下称"根因分析"）——现象记录 + 本机运行产物实证；
2. 《auto-video-editor_全画面模糊_源码核验与修复设计执行计划_20261111.md》（下称"核验计划"）——GitHub main 源码逐项核验 + 对根因分析的三项修正。

---

## 0. 本文档相对前置文档的主要变化

| # | 变化 | 依据 |
|---|---|---|
| 1 | 将"剪映按声明尺寸放大 1.78 倍"明确降级为**待验证假设 H1**，修复开发全部以阶段 0 的 A/B 实验为门槛 | 核验计划·修正 A |
| 2 | 并入本机运行产物实证，核验计划 §9 的待补材料**大部分已关闭**（见 §2.2/§2.3） | 根因分析全文 |
| 3 | 时间轴护栏从"丢弃 <2s 残尾"改为**平衡分段**（不再简单丢素材） | 核验计划 §3.4 |
| 4 | 速度语义统一为单一推导规则 `speed = 源跨度 / 目标跨度`，planner/contract/mapper/QA 四处共用一套口径，并设最低速度阈值策略 | 核验计划 §3.3/§5.4 |
| 5 | 明确区分**剪映手动导出（jianying_manual）与 node_18 FFmpeg 导出**两条验收链路 | 核验计划·修正 C |
| 6 | 新增第 6 章"分阶段执行开发任务"（阶段 0-5，含依赖关系与验收标准） | 本次需求 |
| 7 | 保留 QA 错误码、画质交付报告、评审禁止事项等机制设计 | 核验计划 §5.4/§5.5/§8 |

---

## 1. 问题定义

**现象**：工作流（Thread `console-64da836e2f3f`）生成剪映草稿，用户从剪映客户端手动导出 MP4（`outputs/AutoVideo_console-64da836e2f3f.mp4`，41,039,927 字节，约 34.78s），全片画面显著模糊；画面方向正常（竖屏、内容不歪斜），与原始素材清晰度差距大。

**影响**：该缺陷使成片不可交付；且归一化（预裁切）绕过机制在主链路失效，方向类问题对**所有含旋转元数据的素材**（iPhone 竖拍是最普遍场景）都会复现。

**本次不修**（明确排除，避免范围蔓延）：HDR→SDR 色调映射的色彩风格问题、FireRed TTS :8010 未部署的环境项、node_18 导出器自身的功能增强。

---

## 2. 证据基础（两路核验合流）

### 2.1 源码核验结论（引自核验计划，均已确认）

- `video_edit_capabilities/render.py:92-120` `probe_video_size()` 仅读 `stream=width,height`（coded 尺寸），不解析 `tags.rotate` / `side_data_list.rotation`；
- `storyline/native_draft.py:160-173` `_probe_size()` 复用该函数，`:189-242` `_convert_video_material()` 把 coded 尺寸写入 `materials.videos[].width/height`；
- `assembly_capabilities/media_probe.py:356-364` `stream_rotation()` 已具备旋转解析能力（现成可复用，需统一角度规范化）；
- `nodes/node_05_generate_draft.py:96-116,328-401`：canonical（storyline_plan）路径下归一化产物**没有回写**到 `source_media` 引用（R3 已知限制）；
- `storyline/mapper.py:161` 硬编码 `speed=1.0`；`storyline/contract.py` Clip 契约无 speed 字段、无源/目标时长一致性校验；
- `nodes/node_07_speed_fit.py:103-121` 早退只看目标总时长；`nodes/storyline/qa_gate.py:93-102` 只查总时长阈值；
- `nodes/node_14_make_covers.py:148-159`、`nodes/node_15_localize_covers_en.py:37-42` 把字幕原生 JSON 字符串直接当标题绘制；`jy_common/native_text.py:84-89` `text_display_text()` 现成未用；
- `node_18_export_video` + `draft_ops/draft_exporter.py` 是独立的 FFmpeg 导出链，`AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED` 默认关闭——**用户从剪映导出的成片与该链路无关**。

### 2.2 运行产物实证（本机核验，新并入；核验计划 §9 待补项就此关闭）

| 证据 | 数值/内容 | 来源 |
|---|---|---|
| R1 | 0001.mov：HEVC Main 10、coded 1920×1080、**Display Matrix rotation -90°**、DV Profile 8.4（rpu=1, bl=1, el=0）、HLG（arib-std-b67）、BT.2020、25fps、30.883s、视频流 ~7.2 Mbps | `assembly/media.json`（管线 ffprobe 全量输出） |
| R2 | 0002.mov：同上（30fps、27.935s、~8.6 Mbps）；两素材 `technical_risks` 均已含 rotation 提示与 HDR 警告 | 同上 |
| R3 | `load_media.json` 元数据记 1920×1080（PyAV 无旋转修正） | `storyline/load_media.json` |
| R4 | 草稿 3 个 material 全部声明 `width:1920, height:1080`、scale 1.0、crop 全幅、uniform_scale 1.0；引用路径均为 `inputs\0001.mov`（复用 2 次）+ `inputs\0002.mov`；画布 1080×1920 | `drafts/draft_content.json`（管线快照） |
| R5 | 三段 target 均 11.593s/11.593s/11.594s；source 窗口 `[0,30s]`/`[30s,30.883s]`/`[0,27.935s]`；segment 与 speeds 素材的 speed 全部 1.0 | 同上 tracks 段 |
| R6 | 剪映重存草稿（.bak，pretty JSON）后：material 宽高声明、scale 1.0、speed 1.0 **原样保留**，未被剪映纠正 | 剪映目录 `draft_content.json.bak` |
| R7 | 抽帧拼贴图：源像素**正立、清晰**（ffmpeg 解码自动应用旋转） | `.video_agent/video_frames/0001_ca98d8ee_full/sheets/` |
| R8 | 剪映自渲染草稿封面：**正立、满幅、重度模糊** | 剪映目录 `draft_cover.jpg` |
| R9 | `normalized_sources/0001_608x1080.mp4` 存在（crop 操作、耗时 10.777s）但**未被草稿引用**；608=round(1080×9/16) 即按 coded 尺寸误算的裁切框 | `normalized_sources/`、`*.basic_operation.json` |
| R10 | `timeline_plan.json`：`method=beat_align`；clips 无 speed 字段；BGM `bgm_upbeat_energetic`；TTS 失败（FireRed :8010 不可达）→ 走 beat_align | `storyline/timeline_plan.json` |
| R11 | 封面 6 张（9:16/16:9/4:3 × zh/en）全部为 #14141C 纯色底 + 巨大 JSON 文字；`cover_source_frame.jpg` **全仓库只有消费者无生产者**（本地 grep 确认：`node_14_make_covers.py:64`、`jianying_cover_client.py:110`、单测自造图 3 处） | 剪映目录 `covers/`；本机全仓库检索 |
| R12 | 成片容器总码率 ≈ 9.4 Mbps（文件大小/时长推算）；**成片分辨率/视频流码率/pix_fmt 未测**（分析会话中 ffprobe 被环境拦截） | `outputs/AutoVideo_console-64da836e2f3f.mp4` |

### 2.3 证据边界（仍待阶段 0 补齐）

1. **成片 ffprobe**：分辨率、视频流单独码率、pix_fmt、帧率、色彩元数据、rotation（R12 只有容器级推算，不足以排除导出配置问题——核验计划 §0.3 的保留意见成立）；
2. **剪映导出设置**：分辨率/帧率/码率档位/编码格式/HDR 与硬件编码选项截图；
3. **剪映环境**：版本是否 5.9.0（预期）、硬件解码开关、是否生成代理素材；
4. **"声明尺寸驱动剪映布局"未实证**：R6（剪映不纠正声明值）+ R7/R8 组合（方向正、源清晰、成片糊）与之高度一致，但需 A/B 实验定案（§5）。

---

## 3. 根因假设与判定路径

| 编号 | 假设 | 现有支持 | 反证/风险 |
|---|---|---|---|
| H1 | 草稿 material 按 coded 尺寸声明（1920×1080），剪映按声明尺寸布局，对旋转后的竖屏像素放大 ~1.78 倍再裁切 → 全画面模糊 | 源码缺陷实锤（§2.1）+ R4/R6/R7/R8 | 剪映内部布局机制未实测；若剪映自探测文件则 H1 不成立 |
| H2 | 便携版剪映 5.9 对 HEVC 10bit/DV 8.4/HLG 解码异常（或代理/硬解问题）导致模糊 | 暂无反证 | R7 显示 ffmpeg 解码正常；剪映解码器是独立实现 |
| H3 | 剪映导出配置（分辨率/码率/编码档位）导致模糊 | 暂无反证；成片分辨率未测 | R12 的 9.4 Mbps 与 1080p 导出相符，但不充分 |
| H4 | 草稿其他几何字段（transform/crop/segment 配置）异常 | 管线快照显示全为默认值 | 需与"剪映原生导入"草稿字段对比后排除 |

**判定路径**（先于一切代码修复）：

```text
阶段0 A/B 实验（§5）
  ├─ B 变清晰 ──────────→ H1 成立 → 执行阶段 1、2、3（§6）
  ├─ A、B 都糊 + 原片直导剪映也糊 → H2 → 硬解开关对照 + 同源 SDR H.264 素材对照 → 转预转码方案
  ├─ A、B 预览清晰但导出都糊 ──→ H3 → 核对导出设置/编码器 → 调导出参数
  ├─ A、B 都糊 + 原片直导清晰 ──→ H4 → 对比原生导入草稿与工作流草稿的 material/segment 字段
  └─ B 构图异常 ────────→ 交换宽高不可作为最终修复 → 检查剪映 material schema 语义
```

无论 A/B 结果如何，**阶段 3（逐片段时长/速度护栏）独立成立**：R5 的源/目标矛盾是已核实的缺陷，与模糊是否由 H1 引起无关。

### 3.1 实验定案（2026-11-11 回填，证据见 baseline/ab_test_result.md）

- **H1 证伪**：A/B 预览一样糊、B 方向正常 → 剪映渲染自探媒体文件，声明 width/height 不参与布局；阶段 1 代码保留（语义正确），但它不是模糊修复。
- **H3 排除**：成片 A 实测 1080×1920 / HEVC 10bit / 视频流 9.24 Mbps。
- **根因确认（H4 分支）**：实况草稿 `materials.video_effects` 含 3 条"抖动模糊"（effect_id=9780487，value=0.3），经 `extra_material_refs` 挂在**全部 3 个视频段**、覆盖全片 → 全片持续抖动模糊。来源：`templates/fx_template.json` `video_effects[0]`（`{"name":"抖动模糊","intensity":0.3}`）→ `nodes/node_09_inject_fx.py:121-135` 按 `_DEFAULT_STYLE_TAG` 选中 → `:251-266` 逐段挂载。
- **闭环验证**：`_AB_C` 副本（仅剥离该特效）待用户预览确认清晰。
- **阶段划分修订**：阶段 2 主项改为 **fx 注入修复**（模板替换/强度约束/局部挂载 + QA 检查"模糊类特效全片挂载=阻断"）；原"归一化 canonical 引用"降级 P2；阶段 1/3/4/5 不变。

---

## 4. 总体修复设计

### 4.1 统一媒体探测模型（P0）

新增结构化探测（建议落点 `video_edit_capabilities/render.py` 或独立 media probe 模块）：

```python
def probe_video_dimensions(path: Path) -> VideoDimensions:
    """返回 coded/display 双尺寸 + rotation + 基础信息。"""
```

返回字段：`coded_width/coded_height`、`display_width/display_height`、`rotation_degrees`、`duration_ms`、`fps`、`codec_name/profile/pix_fmt/bit_depth`、色彩三元组（primaries/transfer/space）、`has_audio`。

方向归一化规则：

- 角度先归一化到 `[0,360)`；90/270（含 -90）→ 交换宽高；0/180 → 不交换；
- 同时兼容 `tags.rotate` 与 `side_data_list.rotation` 两种来源；异常值（缺失/非整/超界）按无旋转处理并记 warning；
- coded、display、原始 rotation 三者都保留（审计用）；
- **兼容原则**：不改 `probe_video_size()` 的既有语义（其他调用方依赖 coded 口径）；新增函数独立提供 display 口径。

FFmpeg 滤镜口径（必须二选一，禁止混用）：

- **方案 A（采用）**：依赖 ffmpeg 自动旋转，所有 crop/scale 按显示尺寸计算，产物方向烘焙进像素、rotation 元数据清零；
- 方案 B（不采用）：`-noautorotate` + 显式 `transpose`，全程 coded 空间。

### 4.2 草稿素材声明修复（P0）

`storyline/native_draft.py`：`_probe_size()` 改调 display 口径探测 → `_convert_video_material()` 写入与解码后显示方向一致的 `width/height`；探测失败不得静默回退画布尺寸伪装成功，须记结构化 warning 并让 QA 可识别。

### 4.3 归一化与 canonical 素材引用修复（P0，范围随 A/B 结果微调）

`nodes/node_05_generate_draft.py`：

- 建立以 `source_media_id` 为键的 normalized media map（原始路径 → 归一化路径 → 显示尺寸 → 方向元数据）；
- canonical 分支遍历**全部** `source_media` 替换 file URI（不再只处理 `video_input_path`），clip 的 `source_media_id` 不变；legacy 分支同步对齐口径；
- 归一化 crop/scale 全程按显示尺寸（方案 A）；产物完成后重新 ffprobe 校验方向/尺寸/时长/音轨；失败可降级用原素材但必须记 warning 并让 QA 知道；
- 已是 9:16 的竖屏源不重复转码；同一素材多处引用复用同一归一化文件；保留原始路径供 manifest/幂等/审计。

### 4.4 逐片段时长/速度契约（P0）

单一速度语义（四处共用，禁止两套口径）：

```text
source_ms = source_out_ms - source_in_ms
target_ms = timeline_out_ms - timeline_in_ms
speed     = source_ms / target_ms          # 唯一推导规则，>0
```

- `storyline_capabilities/timeline_planner.py`：逐段分配上限 = 该段源跨度，超出预算重分配给源跨度富余的段；beat_align 保留节拍对齐但受逐段上限约束；
- 最低速度阈值（建议配置项 `MIN_CLIP_SPEED`，默认 0.5，评审定值）：低于阈值的分配视为非法 → planner 重新分配/替换素材/缩短成片，**禁止静默冻结末帧或虚标 speed**；目标时长短于源跨度时按真实 speed 加速或裁源；
- `storyline/mapper.py`：写入推导的真实 speed（删除硬编码 1.0）；
- `storyline/contract.py`：不新增存储字段（避免 schema churn），以只读校验实现不变量检查；新增可选字段才考虑 SCHEMA_VERSION 语义；
- `nodes/node_07_speed_fit.py`：以 `source_timerange` 为基准重写，处理"源<目标"段，去掉一刀切早退；
- `nodes/storyline/qa_gate.py`：新增逐段一致性校验，失败阻止交付。

QA 错误码：

| 错误码 | 触发条件 |
|---|---|
| `SOURCE_RANGE_INVALID` | 源区间超出媒体真实时长或范围无效 |
| `CLIP_SPEED_INVALID` | speed 非法或低于 `MIN_CLIP_SPEED` |
| `CLIP_DURATION_MISMATCH` | source/target/speed 不满足映射关系（帧级容差内不算） |
| `MEDIA_DISPLAY_SIZE_INVALID` | 显示尺寸缺失或与 rotation 计算不一致 |
| `MEDIA_NORMALIZATION_FAILED` | 需归一化但产物未生成/复检失败 |

### 4.5 画质诊断与交付报告（P1）

交付前报告包含：原始素材/草稿声明/成片三方 coded+display 尺寸、rotation 归一化状态、画布尺寸、成片编码/像素格式/帧率/视频流码率、HDR 与输出色彩、是否发生非预期放大、逐段 source/target/speed、降级与未验证项清单；并标记 `renderer=jianying_manual` 或 `renderer=ffmpeg`（两条链路参数不得混记）。

### 4.6 封面修复（P2）

- `node_14_make_covers.py:148-159`、`node_15_localize_covers_en.py:37-42` 改用 `text_display_text()` 取真实标题；
- node_14 入口补 `cover_source_frame.jpg` 生产者：ffmpeg 从 `materials.videos[0].path` 抽帧（自动应用旋转），优先选非黑帧/非转场帧；抽帧失败显式 warning，不静默回退纯色底。

---

## 5. 阶段 0 A/B 根因验证实验（修复前门槛）

**副本纪律（强制）**：剪映完全退出后**复制**整个草稿目录为独立测试草稿再改，禁止手改正式草稿（核验计划 §8.8）。

**变量控制**：A=原样基线；B=仅将 3 个 material 的 `width:1920→1080`、`height:1080→1920`（已知 R4 的声明值），同时改 `draft_content.json` 与 `draft_info.json`（内容相同）；**不**动 scale/crop/源文件/画布/导出参数。

**步骤**：复制草稿 → B 改宽高 → 同一剪映版本分别打开 A/B → 相同时间点 100% 比例对比细节 → 相同设置分别导出 → ffprobe 两成片并对齐同源时间点截图对比。

**元数据采集**（PowerShell，路径按实际替换）：

```powershell
$inputPath = "D:\path\to\source.mov"
ffprobe -v error -show_streams -show_format `
  -show_entries "stream=index,codec_type,codec_name,profile,width,height,coded_width,coded_height,pix_fmt,bits_per_raw_sample,r_frame_rate,avg_frame_rate,bit_rate,color_range,color_space,color_transfer,color_primaries:stream_tags=rotate:stream_side_data=rotation:format=duration,bit_rate" `
  -of json "$inputPath"

ffmpeg -hide_banner -loglevel error -i "$inputPath" -ss 10 `
  -map 0:v:0 -frames:v 1 ".\frame_10s.png"
```

注意：按草稿各段 `source_timerange` 抽取**对应源帧**比较，不做绝对秒数对齐；成片必查 width/height、pix_fmt、视频流 bit_rate、帧率、色彩与 rotation（9.4 Mbps 推算值不能替代）。

**结果判定**：见 §3 判定路径表。

---

## 6. 分阶段执行开发任务

**依赖关系与顺序**：

```text
阶段0(基线+A/B判定, 门槛) ──┬─→ 阶段1(旋转感知探测+声明修复) ─→ 阶段2(归一化canonical统一) ─┐
                            │                                                              ├─→ 阶段5(QA报告+真机回归)
                            └──────────────────────────→ 阶段3(逐片段速度护栏) ────────────┤
                                                 阶段4(封面, P2, 可与阶段3并行) ───────────┘
```

规模标记：S=半天内、M=1-2 天、L=2-4 天（含单测）。

### 阶段 0：基线固化与 A/B 根因判定（P0，门槛，规模 S）

- **目标**：定位模糊首次出现的环节；证实或否定 H1。
- **前置**：剪映关闭；无需改代码。
- **任务**：
  1. 归档原始 MOV、草稿目录（含 .bak）、成片 MP4 到 `baseline/`；
  2. ffprobe 原始素材与成片，存 `baseline/source_probe.json`、`baseline/export_probe.json`（补齐 §2.3-1）；
  3. 截取并记录剪映导出设置、版本号、硬解/代理配置（补齐 §2.3-2/3）；
  4. 按 §5 执行 A/B 实验，产出 `baseline/ab_test_result.md` + 两版导出 MP4 + 同源时间点截图；
  5. 按 §3 判定路径选定后续阶段范围。
- **验收标准**：能明确区分模糊出现在素材解码/草稿预览/导出哪个环节；H1 有证据支持或否定；未定案前不启动阶段 1-3 编码。

### 阶段 1：旋转感知媒体探测与草稿声明修复（P0，规模 M）

- **目标**：display/coded 双口径分离；草稿 material 声明与显示方向一致。
- **任务**：
  1. 新增 `VideoDimensions` 数据结构与 `probe_video_dimensions()`（含 `probe_display_size()` 轻量封装）；
  2. 角度规范化与双来源解析（复用/吸收 `media_probe.stream_rotation` 逻辑）；`probe_video_size()` 原语义不动；
  3. `native_draft._probe_size()` 切换 display 口径 + 失败结构化 warning；
  4. 更新 `tests/unit/test_node_05_source_normalize.py`（现有 1920×1080 横屏裁切用例保留语义、mock 迁移到新函数；**新增**旋转用例而非只改断言——核验计划 §8.9）。
- **测试要点**：横屏无旋转不交换；coded 1920×1080 + rot ±90° → 1080×1920；rot 来自 tags 与 side_data 两来源；0 尺寸/异常角度/探测失败回退告警。
- **验收标准**：竖拍素材 material 声明为显示尺寸；普通横屏不受影响；全量单测相对基线（当前 1646 绿）无新增失败。
- **回退**：新函数独立、调用点少，revert 单文件即可。

### 阶段 2：归一化与 canonical 素材引用统一（P0，规模 M）

- **目标**：消除 R3 失效——归一化产物真正进入草稿引用；crop 全程显示口径（方案 A）。
- **任务**：
  1. `_normalize_source_video()` 探测与 crop 坐标改显示口径；产物 rotation 清零并复检；
  2. normalized media map（按 `source_media_id`）；canonical 分支全量替换 file URI；legacy 分支对齐；
  3. 9:16 源跳过转码；同源多 clip 复用产物；
  4. 归一化失败降级 + warning + QA 可见（`MEDIA_NORMALIZATION_FAILED`）。
- **测试要点**：单竖屏/单横屏/混合方向多素材；带 rotation MOV 与无 rotation MP4；同源重复引用；成功/失败/开关关闭；最终 `materials.videos[].path` 与 map 一致。
- **验收标准**：两条路径素材方向/尺寸口径一致；不再出现"生成了归一化文件但草稿仍引用原始路径"；符合画布比例的源零转码。
- **范围微调**：若阶段 0 判定 H2/H3，本阶段降级为 P1 加固项（防未来横屏素材踩坑），优先级让位给对应根因修复。

### 阶段 3：逐片段时长/速度契约与护栏（P0，规模 L，独立于 A/B 结果）

- **目标**：源/目标/speed 三者一致成为被契约与 QA 强制的硬约束（R5 缺陷根治）。
- **任务**：
  1. `timeline_planner.distribute` 逐段上限=源跨度 + 超出重分配；beat_align 保留节拍对齐但受逐段约束；
  2. `MIN_CLIP_SPEED` 阈值策略与越限重规划（重分配/换素材/缩时长，禁止冻结末帧）；
  3. `mapper.py` 写真实 speed；`contract.py` 增不变量校验（不新增存储字段）；
  4. `node_07_speed_fit.py` 以 source/target 重写 + 去 early-return；`qa_gate.py` 逐段校验（§4.4 错误码）；
  5. `split_shots.py` max-shot 后置切分改**平衡分段**（按总时长/最大镜头时长均匀分配切点，避免二次切出 <min_shot 尾段；总长不足 min_shot 时允许短段并标记原因）。
- **测试要点**：883ms 源不得被分配 11.593s；目标<源时 speed 正确；允许/不允许慢动作两分支；总素材不足目标时长时报错而非注水；节拍对齐+逐段约束+音画同步共存。
- **验收标准**：每段 source/target/speed 在容差内一致；总时长达标不能绕过逐段 QA；无意外停帧/重复帧/超长慢动作。

### 阶段 4：封面模块修复（P2，规模 S，可与阶段 3 并行）

- **任务**：
  1. `node_14/node_15` 标题改 `text_display_text()`；
  2. node_14 入口新增 `cover_source_frame.jpg` 生产者（ffmpeg 抽帧，应用旋转，选非黑帧/非转场帧）；
  3. 抽帧失败显式 warning；检索确认全仓库单生产者单消费者关系。
- **验收标准**：封面显示真实视频画面 + 真实标题；缺帧时可观测，不静默伪装。

### 阶段 5：画质 QA 报告、真机回归与收尾（P0/P1，规模 M）

- **任务**：
  1. 落地 §4.5 交付报告（三方尺寸对照、逐段 speed、renderer 标记）；
  2. 全量单测 + 集成测试（集成基线 8 存量失败不作为新失败口径）；
  3. **新建** console 任务真机验收（同两素材、35s/9:16）：预览清晰 → 剪映手动导出 → ffprobe 复核 1080×1920 → 抽帧目检；
  4. HDR HEVC 原片 vs 同源 SDR H.264 对照、硬解开/关对照（若阶段 0 未排除 H2/H3 则必做）；
  5. 区分 `jianying_manual` 与 `ffmpeg` 两条导出链分别记录参数。
- **验收标准**：同显示比例下成片与原片无非预期细节损失；输出 1080×1920；无意外旋转/裁切/放大；逐段时间映射合法；报告字段齐全。
- **收尾**：回填根因分析文档验收结果；按仓库惯例提交（中文提交信息、分阶段提交）。

---

## 7. 修改文件清单

| 文件 | 修改内容 | 阶段 | 优先级 |
|---|---|---|---|
| `video_edit_capabilities/render.py` | 新增 `probe_video_dimensions()/probe_display_size()`，旧接口不动 | 1 | P0 |
| `assembly_capabilities/media_probe.py` | 角度解析规范化（tags.rotate + side_data 双来源） | 1 | P0 |
| `storyline/native_draft.py` | `_probe_size()` 切 display 口径；失败 warning | 1 | P0 |
| `nodes/node_05_generate_draft.py` | 归一化显示口径 + normalized media map + canonical 全量替换 | 2 | P0 |
| `storyline_capabilities/timeline_planner.py` | 逐段源跨度上限 + 重分配 + MIN_CLIP_SPEED | 3 | P0 |
| `storyline/mapper.py` | 真实 speed（删硬编码 1.0） | 3 | P0 |
| `storyline/contract.py` | 不变量校验（不新增存储字段） | 3 | P0 |
| `nodes/node_07_speed_fit.py` | 以 source/target 重写护栏 | 3 | P0 |
| `nodes/storyline/qa_gate.py` | 逐段校验 + 新错误码 | 3 | P0 |
| `storyline_capabilities/split_shots.py` | max-shot 平衡分段 | 3 | P1 |
| `nodes/node_14_make_covers.py` / `node_15_localize_covers_en.py` | text_display_text + 源帧生产者 | 4 | P2 |
| `tests/unit/**`、`tests/integration/**` | 旋转/归一化/速度契约/封面用例 | 1-5 | P0/P2 |

---

## 8. 评审注意事项与禁止事项（沿用核验计划 §8）

1. 禁止直接改 `probe_video_size()` 语义而不核查全部调用方；
2. 禁止只修 `native_draft.py`（node_05 的 crop 仍是 coded 口径则链路仍错）；
3. 禁止只修 legacy 路径（主链走 canonical，R3 不修等于没修主链）；
4. 禁止只查全片总时长（必须逐段查 source/target/speed）；
5. 禁止把短源段强行拉长还标 speed=1.0；慢动作必须写真实速度且过阈值策略；
6. 禁止把 HDR 色彩差异归因为空间模糊（色彩/解码/分辨率/压缩分开验证）；
7. 禁止把 node_18 FFmpeg 导出参数套到剪映手动导出（两链路分记）；
8. 禁止未备份手改正式剪映草稿（A/B 必须用副本）；
9. 禁止为通过测试固化错误假设（旋转用例要新增，不是改断言）。

---

## 9. 风险与回退

| 风险 | 缓解/回退 |
|---|---|
| A/B 判定 H1 不成立 | 阶段 1 仍保留（display 口径本身正确），阶段 2 降级 P1，主力转 H2/H3 对照实验（预转码 SDR / 导出参数） |
| 交换宽高后剪映构图异常 | 说明剪映 material 尺寸语义与假设不符，停用该修复方向，按 §3 分支排查 schema |
| 速度语义变更影响存量 checkpoint 恢复 | mapper 推导规则对新旧计划一致（旧计划无 speed 字段），resume 链路回归覆盖 |
| 单测 golden 大面积重录 | 仅 mock 点迁移 + 新增旋转用例；以 1646 绿基线逐阶段比对，无净损失 |
| 便携版剪映行为与预期版本漂移 | 阶段 0 固化版本号与配置；真机验收固定同一版本 |

---

## 10. 参考索引（本地 file:line，均已在分析/核验会话中读取确认）

- `video_edit_capabilities/render.py:92-120` — probe_video_size（coded 口径）
- `storyline/native_draft.py:160-173,189-242` — _probe_size / _convert_video_material；`:326` speed 透传
- `assembly_capabilities/media_probe.py:291,320-321,356-364` — stream_rotation
- `nodes/node_05_generate_draft.py:96-116,328-401,404-411,433-460` — 归一化分支
- `config.py:121-123` — 画布 1080×1920
- `storyline_capabilities/split_shots.py:34,229-253` — 30s 强切
- `storyline_capabilities/timeline_planner.py:94-96,123-131,146-160,209-241` — beat_align 均分
- `storyline/contract.py:67-100,213-235` — Clip 契约与校验
- `storyline/mapper.py:140-164` — timerange 映射与硬编码 speed（161 行）
- `nodes/node_07_speed_fit.py:103-121`；`nodes/storyline/qa_gate.py:93-102`
- `nodes/node_14_make_covers.py:64,72-73,76-102,148-159`；`node_15_localize_covers_en.py:37-42`；`mcp_clients/jianying_cover_client.py:62-92,102-131`
- `nodes/_subtitle_texts.py:55`；`jy_common/native_text.py:32-59,84-89`
- `tests/unit/test_node_05_source_normalize.py:124-137,223-243`

> 修复执行后：在本文件头部状态行与根因分析文档中回填验收结果。

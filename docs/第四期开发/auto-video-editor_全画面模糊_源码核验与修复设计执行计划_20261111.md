# auto-video-editor：全画面模糊问题源码核验与修复设计、分阶段执行计划

> 分析日期：2026-11-11  
> 分析仓库：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分析分支：`main`  
> 参考分析文档：[生成视频全画面模糊根因分析_console-64da836e2f3f_20261111.md](https://github.com/wayyet/auto-video-editor/blob/main/docs/第四期开发/%E7%94%9F%E6%88%90%E8%A7%86%E9%A2%91%E5%85%A8%E7%94%BB%E9%9D%A2%E6%A8%A1%E7%B3%8A%E6%A0%B9%E5%9B%A0%E5%88%86%E6%9E%90_console-64da836e2f3f_20261111.md)  
> 本文档状态：**源码核验与修复设计已完成；未修改仓库代码、未执行修复、未修改剪映草稿。**

---

## 0. 结论摘要

### 0.1 当前最值得优先验证的根因

项目存在一个明确的媒体尺寸探测缺陷：`video_edit_capabilities/render.py` 的 `probe_video_size()` 只读取视频流的 `width,height`，没有读取并应用旋转元数据；`storyline/native_draft.py` 的 `_probe_size()` 又直接复用该函数，将结果写入剪映草稿 `materials.videos[]`。

对于手机竖拍但以横向 coded dimensions（编码存储尺寸）保存、依靠 Display Matrix（显示矩阵）旋转显示的视频，文件可能是：

- 编码存储尺寸：1920×1080；
- 旋转元数据：-90° 或等价的 270°；
- 实际显示尺寸：1080×1920。

当前代码可能把它作为 1920×1080 写入 1080×1920 的竖屏草稿。**这项代码缺陷已经确认；它是否就是本次剪映成片全画面模糊的直接原因，还必须通过同素材、同导出参数的 A/B 实验确认。**“剪映一定按错误声明尺寸放大 1.78 倍”的内部布局机制没有在本仓库源码中得到证明，不能写成已证实事实。

### 0.2 同时确认的其他缺陷

1. `node_05_generate_draft.py` 的素材比例归一化没有真正覆盖 `storyline_plan`（canonical timeline，规范时间线）路径。该分支只对 `video_input_path` 做一次预处理，随后直接以原 `storyline_plan` 构建草稿；预处理产物没有回写到 canonical timeline 的 `source_media`，因此草稿素材可能仍引用原始路径。
2. 时间线规划器限制了**全时间线总时长**，却未限制**每一个片段的目标时长**不能超过其源素材跨度。`Clip` 契约也没有 `speed` 字段或源时长/目标时长一致性校验。`mapper.py` 固定输出 `speed: 1.0`，因此源区间与目标区间可能互相矛盾。
3. `node_07_speed_fit.py` 的“已达标”早退只检查时间轴目标时长累计值，不检查每段 `source_timerange`。全片时长未超预算，不代表每个片段的源区间与目标区间正确。
4. 封面模块把原生字幕 JSON 字符串直接用作标题；封面源帧缺失时回退为深色纯色背景。这是封面显示缺陷，不是视频主体模糊的直接根因。

### 0.3 不能据现有证据排除的因素

- 剪映导出分辨率、码率、编码器、质量档位及硬件编码设置；
- 剪映是否使用低清代理（Proxy，代理素材）；
- HEVC Main 10、杜比视界 Profile 8.4、HLG HDR 素材在当前剪映版本/解码器上的兼容性；
- 导出前预览清晰、导出后变模糊，还是预览阶段已经模糊。

参考报告提到成片约 34.78 秒、文件大小约 41,039,927 字节，推算总平均码率约 9.4 Mbps。**文件大小和平均总码率不足以证明导出质量正常**：还缺少成片分辨率、视频编码、视频流单独码率、像素格式、帧率、色彩元数据和剪映导出设置。

### 0.4 修复优先顺序

| 优先级 | 工作项 | 目的 |
|---|---|---|
| P0 | 旋转感知的显示尺寸探测 + 剪映草稿素材尺寸修正 | 先验证、修复全画面模糊最可疑的代码缺陷 |
| P0 | canonical/legacy 两条路径统一素材引用与方向归一化 | 避免一条链路修好、另一条链路仍引用错误素材 |
| P0 | 逐片段 source/target/speed 契约与 QA | 防止短素材被错误延长，造成停帧、重复帧或节奏异常 |
| P1 | 剪映导出参数与 HDR/HEVC 对照实验 | 排除导出设置和解码兼容性因素 |
| P2 | 封面源帧生产、标题文本解析修复 | 修复独立的封面问题 |

---

## 1. 问题范围与工作流链路

本次问题是：工作流生成剪映草稿后，从剪映客户端导出的 MP4 全片画面显著模糊，与原始视频清晰度差异很大；画面方向正常，仍保持竖屏。

与画质直接相关的代码链路为：

```text
原始视频文件
   │
   ├─ load_media / 媒体探测
   │     └─ 记录 coded width/height、rotation、codec、HDR 等信息
   │
   ├─ split_shots / group_clips
   │     └─ 产生镜头区间 source_in_ms / source_out_ms
   │
   ├─ plan_timeline_pro
   │     └─ TimeLine.distribute 分配 timeline_in_ms / timeline_out_ms
   │
   ├─ node_05_generate_draft
   │     ├─ 可能执行素材比例归一化
   │     └─ storyline_plan → canonical_to_draft
   │
   ├─ storyline.mapper
   │     └─ 产生 source_timerange / target_timerange / speed
   │
   ├─ storyline.native_draft
   │     └─ 探测素材宽高并写入 materials.videos[].width/height
   │
   ├─ node_07_speed_fit / 后续字幕、音频、特效节点
   │
   └─ 剪映客户端打开草稿、预览并由用户手动导出
```

补充：仓库还有 `node_18_export_video.py` 和 `draft_ops/draft_exporter.py`，它们构成单独的 FFmpeg 导出链路。配置 `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED` 默认是 `False`；用户从剪映客户端手动导出的文件，不应在没有执行记录的情况下归因给 `node_18`。

---

## 2. 对参考分析文档逐项核验

核验口径：

- **源码确认**：能从 `main` 分支当前源码直接验证；
- **运行产物依赖**：需要本次任务的 `media.json`、`draft_content.json`、`.bak`、原视频或成片才能复核；
- **高置信推断**：代码和现象支持，但剪映内部行为还未通过实验确认；
- **需修正**：参考文档中的解释不够准确、证据不足，或建议不够稳妥。

| 参考文档中的判断 | 核验结果 | 结论与修正 |
|---|---|---|
| `probe_video_size()` 只读取 `width,height`，不处理 rotation | 源码确认 | 正确。当前函数调用 ffprobe 的 `-show_entries stream=width,height`，没有解析 `tags.rotate` 或 `side_data_list.rotation`。 |
| `native_draft._probe_size()` 使用该尺寸写入视频 material | 源码确认 | 正确。`_convert_video_material()` 将探测结果写入 `height`、`width`。 |
| `media_probe.py` 已有 rotation 解析能力 | 源码确认 | 正确。`stream_rotation()` 能从 `tags.rotate` 或 `side_data_list.rotation` 取值，可作为实现参考；但应统一角度规范化和异常输入处理。 |
| 草稿画布固定为 1080×1920 | 源码确认 | 正确。`config.py` 的 `CANVAS_WIDTH=1080`、`CANVAS_HEIGHT=1920`。 |
| 剪映必定按 material 声明尺寸布局，放大约 1.78 倍后导致模糊 | 高置信推断，未证实 | 这是合理假设，但不是仓库代码能直接证明的剪映内部行为。应通过复制草稿、只交换 material 宽高的 A/B 测试确认。 |
| `node_05` 归一化对 canonical timeline 主链路不生效 | 源码确认 | 正确。`storyline_plan` 存在时仅把 `video_input_path` 放入 `normalize_targets`；随后 `_build_from_canonical(storyline_plan)` 不消费 `draft_video_paths`，所以归一化结果没有进入 canonical 的素材引用。 |
| 归一化逻辑按 coded dimensions 计算比例 | 源码确认 | 正确。`_normalize_source_video()` 使用 `probe_video_size()` 计算 `src_ratio` 和 crop 坐标，因此对带旋转元数据的视频有方向错误风险。 |
| 本次 `draft_content.json` 中三个 material 都引用原始 `inputs\*.mov` | 运行产物依赖 | 参考文档报告了这项实测，但这些任务输出文件不在本次可读取的 Git 仓库源码中，本次无法独立复核具体路径和三个 material 的值。 |
| 本次原始素材 coded=1920×1080、rotation=-90°、display=1080×1920、HDR/HLG | 运行产物依赖 | 参考文档引用 `assembly/media.json` 和 `load_media.json`。源码支持这类元数据被探测，但本次未获得原始 MOV 文件及该运行产物，不能独立重新跑 ffprobe。应在复现阶段重新采集。 |
| 30 秒强切会出现 883ms 尾段 | 需修正证据描述 | 当前 `split_shots.py` 先合并小于 `min_shot_ms` 的短片，之后又执行 max_shot 后置兜底切分；后置切分仍可能重新产生短尾。因此“短尾可能出现”成立，但不能只引用第一次强切作为完整原因。 |
| BGM beat-align 将三段均分为 11.593 秒 | 条件成立时可复现 | `TimeLine.distribute()` 在无 TTS、有有效 BGM 节拍时走 beat-align；其逻辑按片段数均分总预算。参考文档中的具体数值仍依赖本次任务的 BGM、目标时长、clip 数和运行 JSON。 |
| Planner 已限制时间轴总时长，因此每段不会超过素材时长 | 不成立 | 当前代码只把总时长封顶到 `min(target_ms, src_sum_ms)`，并未逐片段封顶。总素材时长充足时，仍可能把某个短片段分配到远长于其源区间的目标时长。 |
| `Clip` 契约无 speed 字段，mapper 固定写 `speed=1.0` | 源码确认 | 正确。`Clip` 仅校验源区间和目标区间各自为正，不校验两者比例；mapper 写死 `1.0`。 |
| `node_07` 只看目标时长，早退不检查 source range | 源码确认 | 正确。其 early return 依据 `_compute_total_duration_us(video_track)` 与 target 比较，后续变速计算也使用 segment 的 `target_timerange` 时长，而不是 `source_timerange`。 |
| `qa_gate` 只检查全片目标时长 | 源码确认 | 正确。当前主要判断 `canonical.clips[-1].timeline_out_ms` 是否超过目标时长 1.05 倍，不校验逐段 source/target/speed。 |
| 第二段 0.883 秒被铺成 11.593 秒，所以产生约 13 倍慢放 | 需修正 | 这说明 source/target 时长不一致，但 material/segment 的 `speed` 仍是 1.0。不能直接将实际视觉表现断言为“13 倍慢放”；可能表现为停帧、持续末帧、时长异常或剪映的其他兼容行为，需在剪映实测。 |
| 封面缺少 `cover_source_frame.jpg` 时回退深色背景 | 源码确认 | 正确。`node_14_make_covers._load_source_frame()` 和 `MockJianyingCoverClient` 都有纯色兜底。 |
| 封面标题将字幕 JSON 当文本绘制 | 源码确认 | 正确。`_get_title()` / `_get_titles()` 直接 `str(content)`；仓库已有 `text_display_text()` 可提取原生 JSON 的 `text` 字段，但这两个消费者没有使用它。 |
| 全仓库没有任何 `cover_source_frame.jpg` 生产者 | 尚未完全独立证实 | 消费端已确认；本次 GitHub 代码搜索没有返回可靠结果，不能仅凭搜索空结果证明全仓库不存在生产者。应在开发环境执行全仓库文本检索并确认工作流图没有隐藏生产路径。 |
| 成片平均码率约 9.4 Mbps，足以排除导出问题 | 证据不足 | 平均总码率无法说明输出分辨率、视频流码率、编码器、色彩位深、压缩质量和剪映导出配置。不能据此排除导出质量问题。 |
| HDR 色差与模糊是两个不同问题 | 原理上正确 | HLG/Dolby Vision 色彩映射主要影响亮度、对比度和颜色，不应直接等同于空间分辨率下降；但 HEVC 10-bit 解码、硬件加速和代理素材仍应作为单独变量验证。 |

### 2.1 需要特别修正的三个结论

**修正 A：将“旋转尺寸错误”从确定的剪映渲染根因改为首要待验证假设。**

源码确认了尺寸探测缺陷；但剪映是否严格使用 `materials.videos[].width/height` 决定解码后画面的缩放矩阵，必须实验确认。仅凭“方向正确、画面模糊”不能唯一推出 1.78 倍放大。

**修正 B：逐片段时长是比总时长更重要的 QA 条件。**

`sum(source_duration)` 大于目标总时长，只能证明整体素材量够用，不能证明每一个片段都分配合理。例如一个 0.883 秒源区间被分配 11.593 秒目标区间时，全片总时长仍可能完全达标。

**修正 C：区分剪映客户端导出与项目内置 FFmpeg 导出。**

`node_18_export_video` 的默认开关关闭。只有显式设置 `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=1` 时，才会执行 `draft_ops.draft_exporter`。用户描述的是从剪映导出，所以当前首先应核对剪映项目预览和剪映导出设置，不应把 FFmpeg 导出器参数当作本次文件的实际参数。

---

## 3. 全画面模糊的根因分析

### 3.1 P0：旋转元数据没有进入草稿素材尺寸声明

现状：

1. `probe_video_size()` 只读取 coded `width,height`。
2. `media_probe.stream_rotation()` 能读取旋转信息，但尺寸函数没有调用它。
3. `native_draft._probe_size()` 复用 `probe_video_size()`。
4. `_convert_video_material()` 把结果写入 `materials.videos[].width/height`。
5. 项目画布是 1080×1920。

影响：当源视频依靠 Display Matrix 从横向存储尺寸旋转为竖向显示时，草稿 material 尺寸可能与最终显示方向不匹配。若剪映以这组尺寸参与布局，就可能造成不必要的缩放或裁切。

设计要求：将“编码存储尺寸”和“显示尺寸”分开，避免把二者混成一个 `width/height`。

建议的媒体信息结构：

```python
{
    "coded_width": 1920,
    "coded_height": 1080,
    "rotation_degrees": 270,
    "display_width": 1080,
    "display_height": 1920,
    "duration_ms": 30883,
    "fps_num": 25,
    "fps_den": 1,
    "codec_name": "hevc",
    "pix_fmt": "yuv420p10le",
    "color_transfer": "arib-std-b67"
}
```

说明：上面的数值是参考分析文档中的示例形态，不是本次重新探测得到的运行结果。

方向归一化规则：

- 旋转角度为 90° 或 270°（包含 -90°）时，显示宽高交换；
- 旋转角度为 0° 或 180° 时，显示宽高不交换；
- 处理浮点角度、负角度和异常值时先归一化到 `[0, 360)`；
- 同时兼容 `tags.rotate` 和 `side_data_list.rotation`；
- coded dimensions、display dimensions、rotation 原始值都应保留，便于审计。

注意：`ffmpeg` 默认会根据输入旋转元数据自动旋转解码帧。若 crop 坐标按 coded dimensions 计算、实际滤镜却作用在已旋转帧上，就会出现坐标空间不一致。实现时必须明确选择一种方案：

- **方案 A（推荐）**：让 FFmpeg 自动旋转，所有 crop 宽高和坐标都按旋转后的 display dimensions 计算；输出文件写成正常方向并清除旧 rotation metadata；
- 方案 B：输入时禁用自动旋转，显式使用 `transpose` 等滤镜旋转，再在旋转后的尺寸空间执行 crop。

禁止出现“尺寸按未旋转帧计算、滤镜按已旋转帧执行”的混合口径。

### 3.2 P0：素材归一化没有覆盖 canonical timeline

`node_05_generate_draft.py` 当前逻辑：

- 无 `storyline_plan` 时，对 `source_paths` 逐个执行 `_normalize_source_video()`；
- 有 `storyline_plan` 时，`normalize_targets` 只包含 `video_path`；
- 进入 canonical 分支后，`_build_from_canonical(storyline_plan)` 直接转换原始 timeline。

因此，canonical 路径计算出的 `draft_video_paths` 没有参与 `source_media` 的文件引用替换。即使产出了归一化文件，草稿也可能继续引用原始 MOV。

另外，`_normalize_source_video()` 目前使用 coded 尺寸判断比例，并按该尺寸计算 crop 参数。对带旋转标记的素材而言，这个过程本身也需要方向感知，不能只修复草稿 material 的宽高声明而保留错误 crop。

设计要求：

1. 统一为每个 `source_media_id` 建立 `原始路径 → 归一化路径 → 显示尺寸 → 方向元数据` 的映射；
2. canonical timeline 中通过 `source_media_id` 关联并替换 file URI，不能只修改第一个输入视频；
3. 保留原始输入路径用于 manifest、幂等键和审计；
4. 归一化产物完成后重新 ffprobe，校验实际 display dimensions、rotation、时长、帧率、音频和色彩元数据；
5. 归一化失败时可以降级使用原素材，但必须记录 warning，并让 QA 知道该素材尚未归一化；
6. 对本来就是 9:16 的竖屏源素材，不应无意义地重新编码。

### 3.3 P0：逐片段时长、速度和源区间没有一致性约束

当前 `Clip` 有：

- `source_in_ms`、`source_out_ms`；
- `timeline_in_ms`、`timeline_out_ms`；
- 没有显式 `speed` 字段。

mapper 将时间范围映射到剪映 segment，并固定写入 `speed=1.0`。`node_07_speed_fit` 根据目标时间轴总时长判断是否需要变速，未读取 `source_timerange` 来判断每个片段的播放速度。

建议统一速度定义：

```text
source_duration_ms = source_out_ms - source_in_ms
target_duration_ms = timeline_out_ms - timeline_in_ms
speed = source_duration_ms / target_duration_ms
target_duration_ms = source_duration_ms / speed
```

例如，源区间 883ms、目标区间 11,593ms，则理论速度约为 0.0762 倍。它属于极慢速播放，可能引发明显重复帧/插帧观感；而当前 `speed=1.0` 与这两个区间不一致。不能把这种不一致当成合法时间线。

建议策略：

- 默认不允许通过延长片段来“填满”目标时长；
- 若业务允许慢动作，设置明确的最低速度阈值，并且必须在片段契约中写入真实 speed；
- 若低于最低速度阈值，优先重新分配时长、替换素材或缩短成片，不允许静默冻结最后一帧；
- 若目标时长短于源区间，则按明确 speed 规则加速或裁切源区间；
- 变速后重新计算所有 timeline start/end，保证轨道连续且音画同步。

契约校验建议：

```python
source_ms = clip.source_out_ms - clip.source_in_ms
target_ms = clip.timeline_out_ms - clip.timeline_in_ms
expected_speed = source_ms / target_ms

# speed 必须大于 0，且落在业务允许区间内；
# 对 source_ms / speed 与 target_ms 的差值设置帧级容差。
```

不要仅在 `node_07` 后置修补；应让 planner、Pydantic contract、mapper 和 QA 使用同一套速度语义。

### 3.4 P1：镜头强制切分可能重新产生极短尾段

`split_shots.py` 当前包含：

1. 相邻切点生成片段；
2. 小于 `min_shot_ms` 的片段合并；
3. 对超长片段执行 max-shot 后置兜底强切。

第三步可能重新切出低于 `min_shot_ms` 的尾段。例如 30.883 秒素材按最大 30 秒切分时，可能再次出现 0.883 秒片段。

不建议简单地把所有小于 2 秒的尾段一律丢弃，因为这可能丢掉有效画面。建议采用平衡分段：

- 先根据总时长和最大镜头时长计算需要切成几段；
- 在可行范围内均匀分配切点，保证每段不超过 max-shot，同时尽量不低于 min-shot；
- 若总时长本身不足 min-shot，允许单独短段并标记原因；
- 切分后重新验证 `source_ref.start/end/duration` 与 clip 的源区间完全一致。

### 3.5 P1：HDR/HEVC 解码和剪映导出设置仍需排查

参考文档称原始素材为 HEVC Main 10、杜比视界 Profile 8.4、HLG、BT.2020、10-bit。若这些参数确实存在：

- HDR → SDR 的色调映射可能造成偏灰、偏亮或饱和度差异；
- 解码器、GPU 硬件解码、代理素材或剪映版本兼容问题可能影响预览和输出；
- 单纯的 HDR 色调映射通常不能解释“空间细节整体明显丢失”，但不能只凭格式推断画面一定没问题。

必须分别记录：

1. 原始 MOV 在系统播放器中的清晰度；
2. 原始 MOV 直接导入空白剪映项目后的预览清晰度；
3. 工作流草稿在剪映中的预览清晰度；
4. 剪映导出 MP4 的清晰度；
5. 关闭硬件解码/编码后的对照结果；
6. 原始 HDR HEVC 与同源 SDR H.264 对照素材的结果。

### 3.6 P2：封面缺陷与视频主体模糊分开处理

已核实：

- `node_14_make_covers._load_source_frame()` 读取 `cover_source_frame.jpg`，文件缺失或读取失败时生成 1080×1920 深色背景；
- `node_14_make_covers._get_title()` 把 `materials.texts[0].content` 转成字符串；
- `node_15_localize_covers_en._get_titles()` 同样直接取字符串；
- `jy_common.native_text.text_display_text()` 已经提供原生 JSON 文本解析能力。

修复：

- 用 `text_display_text()` 取真正的字幕文字；
- 在封面节点执行前，明确生产 `cover_source_frame.jpg`；
- 抽帧时应用正确旋转方向，优先选取非黑帧、非转场帧；
- 若抽帧失败，封面应显式标记 warning，而非静默生成看似成功的纯色封面。

封面源帧“全仓库没有生产者”还需本地全仓库检索确认，不能仅依赖 GitHub 搜索为空。

---

## 4. 首轮 A/B 根因验证方案（修复前必须执行）

### 4.1 目的

先确认“material 宽高与 rotation 不一致”是否会让剪映渲染变模糊。此步骤只做实验，不改正式代码。

### 4.2 准备

1. 关闭剪映，备份本次任务的整个草稿目录；
2. 复制一份草稿到独立测试草稿目录，避免覆盖原始草稿；
3. 固定同一组 MOV 素材、轨道、片段、字幕、滤镜、画布、剪映版本和导出参数；
4. 保存当前 `draft_content.json`、`draft_info.json`、导出 MP4 和 ffprobe JSON。

### 4.3 A/B 变量

| 版本 | 唯一变量 | 预期用途 |
|---|---|---|
| A | 当前草稿 material 宽高声明，例如 1920×1080 | 基线 |
| B | 对 rotation 为 ±90°/270° 的素材，将 material 声明改为显示尺寸，例如 1080×1920；其他字段不变 | 验证方向感知尺寸是否改变清晰度 |

B 版本不要同时改 `scale`、`crop`、源文件、画布或导出码率，否则无法判断是哪项修改产生效果。

### 4.4 实验步骤

1. 剪映完全退出后复制草稿；
2. 仅在 B 草稿中交换目标素材的 `width` 和 `height`；
3. 分别用同一剪映版本打开 A、B 草稿；
4. 在相同时间点暂停，按 100% 显示比例检查细节；
5. 分别以相同导出设置导出 A、B；
6. 对比输出分辨率、视频码率、帧率、色彩信息，并对齐同一源时间点截图。

### 4.5 结果判定

| 结果 | 解释 | 下一步 |
|---|---|---|
| B 预览和导出明显变清晰 | 强力支持尺寸声明/旋转处理为主因 | 进入第 5 节修复 |
| A、B 都模糊，原始 MOV 直接导入剪映也模糊 | 更倾向剪映解码、代理素材或 HDR/HEVC 兼容问题 | 关闭硬件解码对照，并生成同源 SDR H.264 测试素材 |
| A、B 预览都清晰，但导出都模糊 | 更倾向剪映导出配置、编码器或硬件编码路径 | 检查分辨率、码率/质量档位及硬件编码 |
| A、B 都模糊，但原始 MOV 直接导入剪映清晰 | 更倾向工作流草稿的 transform/crop/scale/segment 配置 | 对比原生导入草稿与工作流草稿的素材和 segment 字段 |
| B 方向或画面构图异常 | 不能直接交换宽高作为最终修复 | 检查剪映原生 material schema、旋转和 crop 坐标语义 |

### 4.6 原始素材和成片元数据采集命令（Windows PowerShell）

将路径替换为实际文件路径。分别对原始 MOV、剪映导出 MP4 执行：

```powershell
$inputPath = "D:\path\to\source.mov"

ffprobe -v error -show_streams -show_format `
  -show_entries "stream=index,codec_type,codec_name,profile,width,height,coded_width,coded_height,pix_fmt,bits_per_raw_sample,r_frame_rate,avg_frame_rate,bit_rate,color_range,color_space,color_transfer,color_primaries:stream_tags=rotate:stream_side_data=rotation:format=duration,bit_rate" `
  -of json "$inputPath"
```

抽取 10 秒附近的帧用于 100% 对比：

```powershell
ffmpeg -hide_banner -loglevel error `
  -i "$inputPath" -ss 10 `
  -map 0:v:0 -frames:v 1 `
  ".\frame_10s.png"
```

说明：

- 对原始文件执行 ffmpeg 抽帧时，默认自动应用旋转元数据；
- 应根据草稿中每段 `source_timerange`，抽取与导出时间轴对应的原始帧，不要简单比较两个视频相同的绝对秒数；
- 输出 MP4 必须同时检查 `width/height`、`coded_width/coded_height`、`pix_fmt`、视频流 `bit_rate`、帧率、色彩元数据和 rotation；
- 9.4 Mbps 只是参考文档根据文件大小/时长估算的平均总码率，不可替代上述检查。

---

## 5. 目标修复设计

### 5.1 建立统一媒体探测模型

建议在 `video_edit_capabilities` 或共享 media probe 层新增明确的显示尺寸探测函数，例如：

```python
def probe_video_dimensions(path: Path) -> VideoDimensions:
    """返回 coded dimensions、display dimensions、rotation 和基础视频信息。"""
```

建议返回结构：

- `coded_width`、`coded_height`；
- `display_width`、`display_height`；
- `rotation_degrees`；
- `duration_ms`；
- `fps` / `avg_frame_rate`；
- `codec_name`、`profile`、`pix_fmt`、`bit_depth`；
- `color_primaries`、`color_transfer`、`color_space`；
- `has_audio`、`audio_codec`。

兼容原则：

- 不直接改变现有 `probe_video_size()` 的返回含义，以免未知调用方把 coded size 当 display size 的假设被静默改变；
- 新增 `probe_display_size()` 或统一的 `probe_video_dimensions()`；
- 所有用于剪映 material、crop、画布适配的调用点明确使用 display dimensions；
- 只用于读取码流原始存储信息的调用点继续使用 coded dimensions。

### 5.2 修复 native draft material 声明

修改 `storyline/native_draft.py`：

- `_probe_size()` 改为取得 display dimensions；
- `_convert_video_material()` 将剪映 material 的 `width/height` 写成与解码后显示方向一致的尺寸；
- 探测失败不能静默伪装成素材原本就是画布尺寸。至少记录结构化 warning，并让 QA 能识别 fallback；
- 增加旋转素材、普通横屏、普通竖屏、无 rotation tag、负角度和侧数据角度的单元测试。

### 5.3 修复 node_05 的归一化引用

修改 `nodes/node_05_generate_draft.py`：

- legacy 分支继续逐素材归一化；
- canonical 分支必须遍历全部 `source_media`，不能只归一化 `video_input_path`；
- 以 `media_id` 为键建立 normalized media map；
- 用 map 重建 canonical source media 的 URI/path，不改变 clip 的 `source_media_id`；
- 检查 mapper/native draft 最终生成的 `materials.videos[].path` 是否指向正确产物；
- 对每个归一化输出重新探测，确认 rotation 已经被烘焙进像素方向或元数据已清零，且 display dimensions 与编码尺寸一致；
- 同一素材多次出现时复用同一个归一化文件，避免重复编码和资源浪费。

### 5.4 建立逐片段时间映射契约

修改：

- `storyline/contract.py`：为 `Clip` 显式添加 speed/播放策略字段，或明确规定由 source/target 推导；
- `storyline_capabilities/timeline_planner.py`：分配每段时长时考虑源区间长度，不能只按 clip 数均分；
- `storyline/mapper.py`：不再无条件写 `speed=1.0`，应写入经过校验的真实 speed；
- `nodes/node_07_speed_fit.py`：检查 source range、target range 和 speed 的关系，不能只看 target 总时长；
- `nodes/storyline/qa_gate.py`：增加逐片段 source/target/speed 检查，失败时阻止生成可交付草稿。

建议新增 QA 错误码：

| 错误码 | 触发条件 |
|---|---|
| `SOURCE_RANGE_INVALID` | 源区间超出媒体真实时长或范围无效 |
| `CLIP_SPEED_INVALID` | speed 非法或超出业务配置边界 |
| `CLIP_DURATION_MISMATCH` | source duration、target duration 与 speed 不满足映射关系 |
| `MEDIA_DISPLAY_SIZE_INVALID` | display dimensions 缺失或与 rotation 计算不一致 |
| `MEDIA_NORMALIZATION_FAILED` | 需要归一化但产物未生成/校验失败 |

### 5.5 增加画质诊断和交付报告

交付前报告至少包含：

- 原始素材和最终成片的 coded/display dimensions；
- rotation 是否已归一化；
- 实际剪映画布尺寸；
- 成片视频编码、像素格式、帧率、视频流码率、总码率；
- 是否 HDR、输出色彩空间/传递函数；
- 是否出现非预期放大；
- 每段 source duration、target duration、speed；
- 代理素材使用情况（如能从剪映草稿或运行环境识别）；
- 导出前后关键帧对照路径；
- 所有降级和未验证项。

若采用项目内 FFmpeg exporter，必须在报告中明确标记 `renderer=ffmpeg`；若用户从剪映导出，标记 `renderer=jianying_manual`，不能混用两条路径的参数。

---

## 6. 分阶段执行开发任务

### 阶段 0：建立可复现基线（P0，先于代码修复）

**任务**

1. 归档本次原始 MOV、当前剪映草稿目录、导出 MP4；
2. 运行 ffprobe，保存原始素材与成片的 JSON 元数据；
3. 保存当前 A 草稿中的 `materials.videos[]`、video segments、canvas config；
4. 执行第 4 节 A/B 实验；
5. 确认剪映实际版本为项目配置所预期的 5.9.0，记录硬件解码/编码配置和导出选项；
6. 记录原始视频直接导入剪映、工作流草稿预览、最终导出三个环节的清晰度差异。

**交付物**

- `baseline/source_probe.json`；
- `baseline/export_probe.json`；
- `baseline/draft_materials.json`；
- `baseline/ab_test_result.md`；
- A/B 两个版本的导出 MP4 和同源时间点截图。

**验收标准**

- 可以明确区分模糊第一次出现于素材解码、工作流草稿预览还是最终导出；
- 有证据支持或否定“旋转尺寸声明错误”假设；
- 未获得证据前不把根因定为剪映导出码率或 rotation 单一因素。

### 阶段 1：旋转感知媒体探测（P0）

**任务**

1. 新增 coded/display dimensions 统一数据结构；
2. 解析 ffprobe 的 `tags.rotate` 和 `side_data_list.rotation`；
3. 统一处理 -90、90、180、270、缺失和异常旋转值；
4. 新增显示尺寸探测接口，保留旧接口兼容性；
5. `native_draft._probe_size()` 切换为 display dimensions；
6. 为错误 fallback 增加日志/结构化 warning。

**测试**

- 普通横屏 1920×1080，无 rotation；
- 普通竖屏 1080×1920，无 rotation；
- coded 1920×1080、rotation=-90°；
- coded 1920×1080、rotation=90°；
- coded 1080×1920、rotation=0°；
- rotation 来自 tags 与 side data 两种来源；
- 探测失败、0 尺寸、非标准角度。

**验收标准**

- 竖拍带 ±90° rotation 的源，material 声明为实际 display dimensions；
- 普通横屏素材不被错误交换宽高；
- 原有调用方测试通过，旧 `probe_video_size()` 不发生语义漂移。

### 阶段 2：归一化与 canonical 素材引用修复（P0）

**任务**

1. crop 比例、尺寸和坐标统一使用 display dimensions；
2. 明确 FFmpeg 自动旋转或显式旋转的唯一策略；
3. 归一化后将 rotation metadata 清零/确认已烘焙方向；
4. canonical `source_media` 的所有素材均可建立 normalized media map；
5. 通过 `media_id` 保持 timeline clip 引用稳定；
6. 修复原来只处理 `video_input_path`、产物未进入 canonical timeline 的问题；
7. 保留原始文件路径用于追溯，输出文件单独存放。

**测试**

- 单视频竖屏、单视频横屏、多视频混合方向；
- 带 rotation 的 MOV、无 rotation 的 MP4；
- 同源素材在多个 clip 中重复引用；
- 归一化成功、失败、开关关闭；
- 多个 `source_media` 的 path 均与最终 native draft material 对应。

**验收标准**

- `storyline_plan` 和 legacy 路径最终写入的素材方向/尺寸口径一致；
- 不再出现“生成了 normalized file，但 draft material 仍引用原始路径”的情况；
- 对本身已符合画布比例的源素材不发生无意义转码。

### 阶段 3：时间轴逐片段时长和速度护栏（P0）

**任务**

1. 为 Canonical `Clip` 明确 speed 语义；
2. Planner 采用素材跨度约束时长分配；
3. BGM beat-align 在逐片段分配时不能突破各 clip 的允许播放时长；
4. Mapper 生成真实 speed，不再无条件固定 1.0；
5. `node_07_speed_fit` 使用 source/target range 计算和校验速度；
6. `qa_gate` 增加逐片段时长一致性校验；
7. split_shots 的 max-shot 后置切分改为平衡分段，避免重新产生极短残尾。

**测试**

- 883ms 源片段不能静默分配 11.593s 目标时间；
- 目标时长短于源时长时速度和 source range 正确；
- 允许慢动作时 speed 与实际时长映射一致；
- 不允许慢动作时 planner 重新分配或缩短目标时长；
- 总素材不足目标时间时必须给出 warning/错误，不能延长素材冒充有效画面；
- BGM 节拍对齐仍满足总时长约束、逐片段约束和音画同步。

**验收标准**

- 每个 segment 的 source range、target range、speed 在约定容差内一致；
- 全片总时长达标不能绕过逐片段 QA；
- 不出现意外停帧、重复帧或超长慢动作片段。

### 阶段 4：封面模块修复（P2，可与阶段 3 并行）

**任务**

1. `node_14_make_covers.py` 与 `node_15_localize_covers_en.py` 使用 `text_display_text()`；
2. 增加封面源帧生产步骤；
3. 抽帧正确处理 rotation；
4. 抽帧失败时返回 warning 和占位原因；
5. 对全仓库检索 `cover_source_frame`，确定唯一生产者和消费者关系。

**验收标准**

- 封面不再将字幕原生 JSON 显示为大段文字；
- 正常任务可以生成真实视频画面封面；
- 缺失源帧时结果可观测，不静默伪装成功。

### 阶段 5：画质 QA、剪映导出验证和回归（P0/P1）

**任务**

1. 建立原始素材、草稿 material、成片的媒体信息对照报告；
2. 对关键片段抽帧，检查显示方向、分辨率和实际细节；
3. 记录是否发生非预期 upscale；
4. 对 HDR HEVC 原片与同源 SDR H.264 测试素材分别执行剪映导入/导出；
5. 比较硬件解码/编码开启与关闭时的效果；
6. 区分剪映手动导出和 `node_18` FFmpeg 导出；
7. 执行单元测试、集成测试和真实剪映 5.9.0 手工验收。

**验收标准**

- 原始素材在相同显示比例下与成片相比不存在非预期的大幅细节损失；
- 输出分辨率符合 1080×1920 竖屏画布预期；
- 无意外旋转、错误裁切和非预期放大；
- 每段时间映射合法；
- 成片报告包含输出编码、分辨率、视频流码率、帧率、像素格式和色彩信息；
- 若剪映导出仍模糊，能够通过直导原片、工作流草稿和导出文件三组对照定位环节。

---

## 7. 建议修改的文件范围

| 文件 | 修改目的 | 优先级 |
|---|---|---|
| `video_edit_capabilities/render.py` | 新增旋转感知的 display dimensions 探测接口，保留旧接口 | P0 |
| `assembly_capabilities/media_probe.py` | 统一旋转角度解析，补充显示尺寸工具/测试 | P0 |
| `storyline/native_draft.py` | 用显示尺寸写入 native video material | P0 |
| `nodes/node_05_generate_draft.py` | canonical 和 legacy 两条路径都正确引用归一化素材 | P0 |
| `storyline/contract.py` | 扩展 Clip speed/播放策略并增加不变量校验 | P0 |
| `storyline_capabilities/timeline_planner.py` | 按单片源素材跨度进行时长分配 | P0 |
| `storyline/mapper.py` | 写入真实 speed，禁止固定 1.0 掩盖区间不一致 | P0 |
| `nodes/node_07_speed_fit.py` | 以 source/target range 为依据进行变速和 QA | P0 |
| `nodes/storyline/qa_gate.py` | 加入逐段 source/target/speed 检查 | P0 |
| `storyline_capabilities/split_shots.py` | 平衡 max-shot 分段，避免短尾被二次切出 | P1 |
| `nodes/node_14_make_covers.py` | 解析原生字幕文字，生成封面源帧 | P2 |
| `nodes/node_15_localize_covers_en.py` | 解析真实中文标题而不是 JSON 字符串 | P2 |
| 对应 `tests/unit/**`、`tests/integration/**` | 覆盖旋转、归一化、速度契约、封面和端到端路径 | P0/P2 |

---

## 8. 代码评审注意事项与禁止事项

1. **禁止直接把现有 `probe_video_size()` 的含义改成 display dimensions 而不检查所有调用方。** coded dimensions 仍可能被其他处理逻辑需要。应新增清晰的函数或返回结构。
2. **禁止只修改 `native_draft.py`。** 若 `node_05` 的 crop 仍使用 coded dimensions，归一化链路仍可能错误。
3. **禁止只修 legacy 路径。** 当前真实工作流如果走 `storyline_plan`，归一化产物没有注入 canonical source media，修 legacy 不会修复主链路。
4. **禁止只检查全片总时长。** 必须检查每段源区间、目标区间和速度。
5. **禁止把 0.883 秒强行延长为 11.593 秒并继续标注 speed=1.0。** 若业务确实需要慢动作，必须写入真实速度并经过质量策略审核。
6. **禁止把 HDR 色彩差异直接归因为空间模糊。** 色彩映射、解码、分辨率和压缩应拆开验证。
7. **禁止把 node_18 的 FFmpeg 导出参数套用到剪映客户端导出。** 两条链路必须分别记录和验收。
8. **禁止在未备份草稿的情况下直接手改正式剪映草稿。** A/B 测试必须使用副本。
9. **禁止为了让测试通过而继续固化错误假设。** 现有 `test_node_05_source_normalize.py` 对 1920×1080 横屏尺寸的 mock 测试并未覆盖“coded landscape + rotation = portrait”的真实场景，应增加旋转用例，而不是仅修改断言。

---

## 9. 当前证据边界与待补材料

本次已经通过 GitHub `main` 分支源码核验代码逻辑，但没有直接读取本次运行目录中的原始媒体和草稿文件。因此，下列内容仍需从本机运行环境补充：

- 原始 `0001.mov`、`0002.mov`；
- `outputs/console-64da836e2f3f/assembly/media.json`；
- `storyline/load_media.json`；
- 本次实际 `draft_content.json` 及剪映保存后的 `.bak`；
- `normalized_sources/` 下的真实 MP4；
- 原始抽帧拼贴图；
- 剪映导出的 `AutoVideo_console-64da836e2f3f.mp4`；
- 剪映导出设置截图（分辨率、帧率、码率/质量、编码格式、HDR 和硬件编码选项）。

这些运行产物决定“本次任务的数据值是否与报告一致”。当前可以确认代码存在的通用缺陷，但不能声称已重新读取并验证上述所有本机文件。

---

## 10. 参考源码索引

以下链接均指向仓库 `main` 分支对应文件及相关行范围：

- [render.py：`probe_video_size()`](https://github.com/wayyet/auto-video-editor/blob/main/video_edit_capabilities/render.py#L92-L120)
- [native_draft.py：`_probe_size()` 与 video material 宽高写入](https://github.com/wayyet/auto-video-editor/blob/main/storyline/native_draft.py#L160-L241)
- [media_probe.py：旋转元数据解析](https://github.com/wayyet/auto-video-editor/blob/main/assembly_capabilities/media_probe.py#L291-L364)
- [node_05_generate_draft.py：canonical/legacy 归一化分支](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_05_generate_draft.py#L85-L125)
- [node_05_generate_draft.py：`_normalize_source_video()`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_05_generate_draft.py#L328-L401)
- [split_shots.py：强制切分与短片合并](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/split_shots.py#L215-L310)
- [timeline_planner.py：BGM/目标总时长分配](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/timeline_planner.py#L80-L170)
- [timeline_planner.py：beat-align 均分](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/timeline_planner.py#L195-L255)
- [contract.py：Clip 字段和时间线不变量](https://github.com/wayyet/auto-video-editor/blob/main/storyline/contract.py#L55-L110)
- [mapper.py：source/target timerange 和固定 speed](https://github.com/wayyet/auto-video-editor/blob/main/storyline/mapper.py#L130-L175)
- [node_07_speed_fit.py：总时长早退和 speed 计算](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_07_speed_fit.py#L85-L205)
- [qa_gate.py：当前时间线总时长 QA](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/qa_gate.py#L75-L115)
- [node_14_make_covers.py：封面帧、标题读取](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_14_make_covers.py#L50-L175)
- [node_15_localize_covers_en.py：标题读取](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_15_localize_covers_en.py#L25-L55)
- [native_text.py：原生字幕 JSON 文本解析](https://github.com/wayyet/auto-video-editor/blob/main/jy_common/native_text.py#L25-L100)
- [node_18_export_video.py：独立可选导出节点](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_18_export_video.py)
- [draft_exporter.py：FFmpeg 导出实现](https://github.com/wayyet/auto-video-editor/blob/main/draft_ops/draft_exporter.py)
- [config.py：1080×1920 画布与最终导出开关](https://github.com/wayyet/auto-video-editor/blob/main/config.py#L119-L123)
- [config.py：最终导出默认关闭及编码参数](https://github.com/wayyet/auto-video-editor/blob/main/config.py#L501-L529)
- [test_node_05_source_normalize.py：现有横屏裁切测试](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_node_05_source_normalize.py#L110-L150)

---

## 11. 最终判断

当前最合理的处理路径不是立即调高剪映导出码率，而是先完成 **阶段 0 的 A/B 实验**，明确模糊出现在哪个环节。

若 B 草稿（仅将带旋转元数据的视频 material 宽高改为 display dimensions）明显变清晰，就优先落地阶段 1 和阶段 2；同时必须补上阶段 3 的逐片段时长护栏，因为已核实的时间线问题会影响最终画面连续性和播放质量。

若 A/B 无差异，则不要继续围绕 material 宽高反复修改；应转向剪映直接导入原片、HEVC/Dolby Vision/HLG 解码、代理素材、硬件加速与导出质量设置的对照实验。

**本文件是设计和执行计划，不代表任何修复已经提交或生效。**

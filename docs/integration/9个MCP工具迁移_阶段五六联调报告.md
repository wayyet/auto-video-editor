# 9 个 MCP 工具迁移 —— 阶段五 + 阶段六 联调报告

- **计划**:`docs/integration/video-agent-kit九个MCP工具迁移至auto-video-editor设计执行计划.md`
- **本次执行范围**:仅 §6.5 阶段五(可选)+ §6.6 阶段六;§6.1~§6.4 未在本次改动
- **执行日期**:2026-09-28
- **机器**:Windows 11 / Python 3.13.11 / ffmpeg 9.0.2(essentials build)
- **真实素材**:`inputs/30s.mp4`(**实测 2046x1080 / 5.000s**,文件名与实际时长不符,见 §4 遗留项 3)

---

## 一、阶段五(§6.5)做了什么

| 计划条目 | 落地位置 | 状态 |
|---|---|---|
| §6.5-1 `assembly_repair_loop` 接入 `video_watch_segment` + `video_read_frames` | `nodes/assembly/node_repair_loop.py` | ✅ 完成 |
| §6.5-2 `node_05` 前置 `video_basic_operation` 做素材归一化 | `nodes/node_05_generate_draft.py` | ✅ 完成 |
| 配套开关 | `config.py` | ✅ 新增 5 个 |
| 配套 state 字段(全 `NotRequired`) | `state.py` | ✅ 新增 2 个 |

### 1.1 repair loop 视觉证据复核

QC 只说"第 12 秒黑了",人要看图才知道是不是真问题。修复循环在写 patch **之前**
先自己看一眼:

```
preview_qc_report.json
  ├─ blackdetect_log_tail   ─┐
  ├─ silencedetect_log_tail  ├→ parse_filter_ranges(复用 qc_preview 现成解析器)
  └─ freezedetect_log_tail  ─┘        ↓
                            重叠窗口合并 + 两侧各外扩 0.5s
                                       ↓
                    video_watch_segment(fps=8, force=True)  高 fps 重采样
                    video_read_frames(upscale=2, center)     局部放大
                                       ↓
              outputs/<job_id>/assembly/repair_visual_evidence.json
```

关键设计:

- **旁路**:视觉复核任何失败(ffmpeg 缺失 / 预览不存在 / 工具抛异常)只写 `error_log`,
  `timeline_diff` 与重试计数照常 —— 对应计划 §5.4 环境级失败降级纪律。
- **`force=True`**:修复循环最多跑 `ASSEMBLY_QC_MAX_RETRY` 轮,窗口通常一模一样;
  不 force 会被 `video_watch_segment` 的 ledger 判成"上次已看过",第 2 轮起拿不到新帧。
- **窗口上限对齐工具入参**:单段 ≤ `MAX_SEGMENT_SECONDS`(60s)、段数 ≤ `MAX_SEGMENTS`(8)、
  总时长 ≤ `MAX_TOTAL_SECONDS`(180s),超限会被工具直接 `[ERROR]`。
- **外扩 0.5s**:QC 只报黑帧起点,外扩才能同时拍到黑帧前后的正常画面。
- **开关**:`ASSEMBLY_REPAIR_VISUAL_EVIDENCE=false` → 完全退回阶段五之前的纯 `timeline_diff` 行为。

新增开关(`config.py`,均可用环境变量覆盖):

| 开关 | 默认 | 作用 |
|---|---|---|
| `ASSEMBLY_REPAIR_VISUAL_EVIDENCE` | `true` | 是否做视觉复核 |
| `ASSEMBLY_REPAIR_MAX_WINDOWS` | `3` | 一次最多复核几个窗口 |
| `ASSEMBLY_REPAIR_WATCH_FPS` | `8.0` | 重采样帧率 |
| `DRAFT_SOURCE_NORMALIZE_ENABLED` | `true` | 节点 5 是否做宽高比归一化 |
| `DRAFT_SOURCE_ASPECT_TOLERANCE` | `0.02` | 比例容差(2% 以内视为已对齐) |

新增 state 字段(`state.py`,均为 `NotRequired`,旧 checkpoint resume 不受影响):

| 字段 | 含义 |
|---|---|
| `assembly_repair_evidence_path` | 视觉证据汇总 json 路径;`None` = 本轮没跑 |
| `draft_source_normalized_path` | 归一化后的素材路径;`None` = 比例本就对齐 |

### 1.2 节点 5 素材宽高比归一化

横屏素材直进竖屏草稿,剪映会按画布拉伸,画面变形。节点 5 在写草稿前:

1. `probe_video_size` 读源素材分辨率(不碰 ffmpeg 裁切,只 ffprobe);
2. 比例在 2% 容差内 → **零开销直接放行**(绝大多数情况);
3. 比例不一致 → `video_basic_operation(operation="crop")` **居中裁切**到画布比例,
   宁可切掉两侧也不让主体被拉长;
4. 任何失败 → 退回原素材 + 写一条 `error_log`,**不阻断草稿生成**。

裁切框取"源框内能容纳的最大画布比例区域",并强制偶数边(yuv420p + libx264 要求)。
实测 2046x1080 → 裁成 608x1080(比例 0.5630 vs 画布 0.5625,偏差 0.09%)。

**契约保持**:`state["video_input_path"]` 不动,manifest 的 `input_sha256` 仍按**输入**素材计算
(否则幂等键会跟着归一化产物跑,同一素材重跑命中不了幂等);只有草稿 `materials[].path`
用归一化后的路径。

---

## 二、阶段六(§6.6)端到端联调

新增联调脚本 `scripts/video_edit_phase56_e2e.py`,6 个 case 全部用真实素材 + 真实 ffmpeg,
不 mock 工具层:

```powershell
python scripts\video_edit_phase56_e2e.py
# 或单跑一个 case
python scripts\video_edit_phase56_e2e.py --case repair_visual_evidence
```

产物:`outputs/phase56-e2e-<case>-<ts>/`,汇总 `outputs/phase56_e2e_report.json`。

### 2.1 结果:6/6 case 通过

| case | 验证内容 | 结果 |
|---|---|---|
| `assembly_chain` | 真实跑 assembly 6 节点,产物齐全 | ✅ `qc_status=pass_with_warnings` |
| `repair_visual_evidence` | 阶段五核心:24 张重采样帧 + 2 张放大帧落盘,`repair_marker` 照写 | ✅ |
| `source_normalize` | 阶段五核心:真裁切,比例对齐 | ✅ |
| `subtitle_chain` | scout → build → render → qc 全链路 | ✅ |
| `node08_style` | scout 建议真落进草稿样式(`size` / `border.width`) | ✅(原缺口见遗留项 1,已修) |
| `checkpoint3_and_tts` | 关卡③ 预览 MP4 + 节点 17 TTS | ✅(见 §4 遗留项 2) |

关键实测数据:

- `repair_visual_evidence`:从 QC 日志抽出 2 个窗口
  `[(0.5, 3.0, ['black']), (4.5, 5.0, ['black', 'freeze'])]` —— 注意第二个窗口
  **黑帧与卡帧时间重叠,被合并成一次复核**,不重复调 ffmpeg。
- `source_normalize`:2046x1080(比例 1.8944)→ 裁切 608x1080(比例 0.5630)。
- `checkpoint3_and_tts`:qc 链产出 8 条 issue,预览 MP4 落在
  `.video_agent/layout_qc/preview.mp4`,人工可直接打开。

---

## 三、§11 验收清单逐条打勾

| # | 验收项 | 结论 | 依据 |
|---|---|---|---|
| 1 | 3 个新依赖装好,`scenedetect --no-deps` 确认 | ✅ | 阶段一已装;`subtitle_scout` 本次真跑出 style,依赖可用 |
| 2 | `ffmpeg -filters` 确认带 `subtitles`/`ass` | ✅ | `subtitle_render` 真跑烧录出 `subtitled.mp4` |
| 3 | 三个工具在 `video_edit_capabilities/` 可独立调用并通过单测 | ✅ | 单测见 §四 |
| 4 | 真实视频跑通 scout→build→render→qc,产出预览 MP4 与 QC 问题列表 | ⚠️ 部分 | 预览 MP4 ✅、问题列表 ✅;**qc 证据帧为空**(见遗留项 4) |
| 5 | 节点 08 接入 scout 后,ffmpeg/scenedetect 缺失时退回默认样式,不中断主链 | ✅ | 降级路径 ✅;成功路径也已补上 `STYLE_KEY_MAP` 换算,建议真落进草稿(**阶段四补做,见 §4 遗留项 1**) |
| 6 | 关卡③ 走新链路后 `layout_issues` 字段契约不变,interrupt/resume 回归全绿 | ❌ 未通过 | `tests/unit` 全绿,但 22 个集成测试红,**归因阶段一~四**(见遗留项 5) |
| 7 | `node_17` 通过 `jy_common/tts_client.py` 产出音频,不再写静音占位 | ⚠️ 部分 | 走 `call_firered_tts()` ✅,产出 `tts_mock_*.wav`(**mock 后端**,真实 FireRedTTS2 未部署) |
| 8 | `tts_generate` 确认只是一行转发,未实现成第二份独立逻辑 | ✅ | 阶段三已落地 |

**合计:4 项完全通过,2 项部分通过,1 项未通过。**
(验收项 5 在阶段四补做 `STYLE_KEY_MAP` 后由"部分"升为"完全通过"。)

---

## 四、遗留项(按优先级)

### 遗留项 1 —— 节点 08 的 scout 建议没有落到草稿样式 ✅ 已修(2026-09-28 补做)

**原状**:`subtitle_scout` **没有顶层 `style` 键**,排版建议分散在:

```
font_recommendation.style      = {"font_size": 45}
contrast_recommendation.style  = {"readability": "box"}
```

而 `nodes/node_08_add_subtitles.py::_run_subtitle_scout` 返回的是**整份 scout 报告**,
只写进 `state["subtitle_scout_report"]`,**没有**把建议映射进
`_JIANYING_DEFAULT_STYLE`(草稿 `texts[].style` 仍是默认值)。
即:scout 跑通了、报告也拿到了,但**排版建议实际没生效**。

默认样式键名 `{'size','bold','color','align','border','transform_y'}`
vs scout 建议键名 `{'font_size','readability'}` —— 两边没有任何一个键名对得上,
不能 `{**default, **scout}` 直接展开,必须写显式映射表。

**已修**,见 §4.1。

### 4.1 `STYLE_KEY_MAP` —— 两个样式世界的换算表

两个世界键名、单位都不同,**不能直接展开**:

| | scout / `subtitle_style`(libass) | 剪映草稿 `texts[].style` |
|---|---|---|
| 字号 | `font_size` 绝对像素(竖屏 baseline 63px) | `size` 归一化浮点(默认 5.0) |
| 描边 | `outline` 像素 + `readability` 宏 | `border.width`(默认 40.0) |
| 颜色 | `primary_colour` ASS `&HBBGGRR` | `color` `[r,g,b]` 0..1 |
| 对齐 | `alignment=2` 底部居中 | `align=1` 居中 |

落地位置 `nodes/node_08_add_subtitles.py`:

```python
STYLE_KEY_MAP: dict[str, str] = {
    "size": "font_size",         # 字号
    "border.width": "outline",   # 描边粗细 = 对比度建议的落点
}
```

**只映射这两个键**,其余 scout 字段刻意不落草稿,理由写进了代码注释:

- `color`:scout 从不改 `primary_colour`(三个 preset 都是纯白),映射是恒等空操作。
- `align`:ASS `alignment=2` 是"底部居中",剪映 `align=1` 是"居中",照搬会让字幕整体上移。
- `transform_y`:scout 把 `caption_band` 当**诊断信息**报出(告诉人字幕带在哪),不给推荐值;动它等于替人做排版决定。
- `shadow`:node_08 调好的默认样式里没有这个键(= 不描阴影,这是 jianying-add-subtitles 实测约定),本链路从没校准过它。

**单位换算不编系数,用比值锚定。** 剪映 `size` / `border.width` 的单位与 ASS 像素
之间没有公开换算公式,写死一个系数就是拿真实字幕的观感赌一个猜出来的数。
改用:默认值 5.0 / 40.0 是**在真实剪映上调出来的**,对应的就是 scout 当前 preset
在本片分辨率下的 baseline(`sty.resolve_style(preset)`)。拿"建议值 ÷ baseline 值"
这个无量纲比值去缩放默认值 —— scout 判的是"大了还是小了",比值刚好就是差多少倍。
副作用是好的:baseline 没变化时草稿一个字段都不动。

夹取区间(防一句离谱建议把字幕推到不可用极端):字号 0.6x~2.0x,描边 0.5x~3.0x。

**降级路径全部保留**(scout 是旁路,不拖垮主链):

| 情况 | 行为 |
|---|---|
| scout 异常 / 返回 `[ERROR]` / 无 `video_input_path` | 默认样式,只写 error/status |
| 报告缺 `video` / 尺寸非法 / `resolve_style` 抛错 | 默认样式,静默 |
| `contrast level=low` + `font level=ok`(两处 `style` 都是空 dict) | 默认样式,**一个字段都不动** |
| `SUBTITLE_SCOUT_STYLE_ENABLED=false` | 退回阶段四之前的"只记录不改样式" |

**一处不等价的降级已显式声明**:scout 的 `readability: "box"` 在 ASS 里是**一整条
半透明底色带**,而本仓库已实测的剪映 style 字段里没有背景字段,只能降级成
"描边加粗到 2.5 倍"。描边 ≠ 底色带,`status_log` 会明写这句话,不装作两者一回事。

实测数值(竖屏 1080x1920 / shortform_zh,baseline 63px / 5.38px):

| scout 判定 | 建议 | 草稿结果 |
|---|---|---|
| `font level=too_small` | 96px | `size` 5.0 → **7.62** |
| `contrast level=medium` | `heavy_outline` | `border.width` 40.0 → **64.01** |
| `contrast level=high` | `box` | `border.width` 40.0 → **100.0**(降级) |
| 两处都判定合适 | `{}` | 全部不动 |

配套:`config.SUBTITLE_SCOUT_STYLE_ENABLED`(默认开)+ `STYLE_KEY_MAP` + 单测 28 条
(全量 `tests\unit` 退出码 0)。

### 遗留项 2 —— TTS 目前是 mock 后端

`jy_common/tts_client.py` 默认 `TTS_BACKEND=mock` → `MockTTSClient` 写
`%TEMP%\tts_mock_<hex>.wav`(非静音,>5KB,满足 acceptance_check 阈值)。
真实 `FireRedTTS2Client` 骨架已就位,设 `TTS_BACKEND=firered` 切换,
默认 endpoint `http://127.0.0.1:8010/synthesize`(占位端口,计划 §8.1 待核实项 2)。
**真实服务未部署,接口形状仍未核实** —— 与计划预期一致,推迟到 Week 6+。

### 遗留项 3 —— `inputs/30s.mp4` 实际只有 5 秒

ffprobe 实测:`2046x1080 / duration=5.000000`。阶段二用 9 秒转写文本打这个素材时,
`subtitle_qc` 正确报 `cue end 5.200s exceeds video duration 5.000s`。
**工具行为正确,是素材名与内容不符**。建议后续改名为 `5s_2046x1080.mp4` 或补真 30s 素材。

### 遗留项 4 —— `subtitle_qc` 证据帧采样失败(mjpeg 编码器报错)

qc 报告里稳定出现:

```
[mjpeg @ ...] Non full-range YUV is non-standard, set strict_std_compliance to at most unofficial
[vost#0:0/mjpeg @ ...] Error while opening encoder - maybe incorrect parameters
```

根因:采样帧走 `mjpeg` 编码器,而烧录产物是 `yuv420p` **limited range**,
ffmpeg 9.x 默认 `strict_std_compliance` 拒绝。后果:
`subtitle_qc.evidence_frames` 恒为空 → 关卡③ payload 的 `subtitle_qc_evidence_frames` 也是空。
**归属阶段二**(`video_edit_capabilities/subtitle_qc.py`),本次未修。
规避方向:采样时加 `-pix_fmt yuvj420p`,或给 mjpeg 传 `-strict -unofficial`。

### 遗留项 5 —— 22 个集成测试红,归因阶段一~四

`tests/unit/` **全绿(退出码 0,2 skip,0 失败)**;`tests/integration/` 22 个用例失败:

| 文件 | 失败数 |
|---|---|
| `test_interrupt_resume.py` | 5 |
| `test_week4_graph.py` | 5 |
| `test_week5_resilience.py` | 4 |
| `test_sequential_integration.py` | 3 |
| `test_state_field_compatability.py` | 2 |
| `test_phase5_e2e.py` | 2 |
| `test_no_autoclean_cache.py` | 1 |

**归因证据(已实测,不是推测)**:把本次阶段五改动的 5 个文件
(`config.py`、`state.py`、`nodes/assembly/node_repair_loop.py`、
`nodes/node_05_generate_draft.py`、`draft_ops/safe_write_guard.py`)
全部 `git stash` 后重跑全部 7 个失败文件 → **22 个失败一个不少,清单完全一致**
(分两批验证:14 + 8)。即**与阶段五改动无关**。

根因(抽查):
- `test_node_17_writes_empty_wav_placeholder`:仍断言旧 stub 的"空 wav 占位"行为,
  而阶段三已把 node_17 换成真实 TTS;配套的 `tests/unit/test_node_17_tts_stub.py`
  已删但集成用例没同步(`git status` 显示该文件为 `D`)。
- 其余用例多在 interrupt/resume 与分支并发路径上,疑似阶段四 `graph.py` 的
  `node_17_inject_english_tts_stub → node_17_inject_english_tts` 改名未同步到位。

**结论:阶段四需要补一轮"测试对齐 + 回归修复",不属于 §6.5/§6.6 范围。**

---

## 五、顺带修掉的一个真 bug(阶段六 §6.6 step 4 触发)

计划 §6.6 第 4 步要求中文路径乱码时执行:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
```

**这条命令会让 2 个既有单测变红。** 根因在
`draft_ops/safe_write_guard.py::_default_windows_proc_query`:
用 `subprocess.run(text=True)` 跑 `tasklist`,设了 `PYTHONUTF8=1` 后按 UTF-8 解码,
而中文 Windows 的 `tasklist` 输出是 GBK(首字节 `0xD0`)→ `UnicodeDecodeError`
把整个写入守卫炸掉。

已修:该子进程调用加 `errors="replace"`(只做进程名子串匹配,丢个别字节不影响判断),
并对 `result.stdout` 做 `or ""` 兜底。修完 `PYTHONUTF8=1` 下
`test_node_05_generate_draft.py` + `test_safe_write_guard.py` 共 22 条全绿。

---

## 六、怎么复跑

```powershell
# 阶段五单测(29 条)
python -m pytest tests\unit\nodes\test_repair_loop_visual_evidence.py -v   # 16 条
python -m pytest tests\unit\test_node_05_source_normalize.py -v             # 13 条

# 阶段四补做:node_08 STYLE_KEY_MAP(28 条)
python -m pytest tests\unit\test_node_08_add_subtitles.py -v

# 阶段五六端到端(6 case,真实素材 + 真实 ffmpeg)
python scripts\video_edit_phase56_e2e.py

# 全量回归(单测应退出码 0;集成测试的 22 个红是阶段一~四遗留)
python -m pytest tests\unit        -q   # → exit 0
python -m pytest tests\unit tests\integration -q
```

### 已知坑:按目录收集没事,手写跨目录文件顺序会炸

仓库顶层有 `nodes/` 包,测试目录里又有 `tests/unit/nodes/`(同名)。
pytest 用 `prepend` 导入模式时,若**手写命令行**把两个目录的文件交错排列,
可能出现 `tests/unit/nodes/conftest.py` 注册不出 fixture 的现象
(`fixture 'base_state' not found`,报在 `test_assembly_nodes.py` 上)。

已实测:同一组文件换个顺序结果就变(`A, B, C` 失败 / `C, B, A` 通过),
与被测代码无关。**按目录收集不受影响**,下面四种标准调用全部 exit 0:

```powershell
python -m pytest tests\unit                                              # exit 0
python -m pytest tests\unit\nodes                                        # exit 0
python -m pytest tests\unit\test_node_05_source_normalize.py             # exit 0
python -m pytest tests\unit\nodes tests\unit\test_node_05_source_normalize.py  # exit 0
```

要单跑阶段五用例,请**按上面的目录/文件形式**分开跑,别把
`tests/unit/nodes/` 里的文件和 `tests/unit/` 根下的文件手工交错。

**开关回退(需要临时关掉阶段五行为时)**:

```powershell
$env:ASSEMBLY_REPAIR_VISUAL_EVIDENCE = 'false'   # repair loop 不做视觉复核
$env:DRAFT_SOURCE_NORMALIZE_ENABLED = 'false'    # 节点 5 不做宽高比归一化
$env:SUBTITLE_SCOUT_STYLE_ENABLED  = 'false'     # 节点 8 不把 scout 建议落进草稿样式
```

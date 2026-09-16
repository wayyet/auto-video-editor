# RUNBOOK · D-B 桥接（OpenStoryline → 剪映卡点成片）

> 承接《路线D+OpenStoryline整合方案.md》第四节。本手册只讲**桥接这一段**：
> 把 OpenStoryline 出的「文案 / 字幕 / BGM / 卡点」翻译成 `edit_spec.json`，
> 再让剪映按**卡点**切镜出片。D-A（纯 Claude）流程见《RUNBOOK_剪映整合.md》，不受影响。

新增/改动的三个文件：
- `os_to_spec.py` ★新桥接：OpenStoryline 会话产物 → `output/edit_spec.json`（含 `beat_marks`）+ 裁好 `output/jy_clips/`
- `jy_build.py`   ★加了「卡点吸附」：spec 带 `beat_marks` 时，把切镜/字幕踩到节拍
- `config.yaml`   ★加了 `openstoryline:` 段（风格、是否启用卡点、吸附窗口等）

---

## 一、整条 D-B 怎么跑

### 第 1 段（Windows，OpenStoryline 大脑）
1. 双击 `FireRed-OpenStoryline\启动网页界面.bat` → 浏览器开 `http://127.0.0.1:7860`。
2. 把 `input/` 里的赤松宫素材丢进去，让它跑到**「生成时间轴 / 定剪完成」**
   （要的是文案 + 逐句字幕 + 选定 BGM + 卡点；**不必让它最终渲染 mp4**，渲染交剪映）。
   - API 已验证可用（MiniMax LLM/VLM 均 HTTP 200），跑得动。
3. 它的产物会落在 `FireRed-OpenStoryline\outputs\<会话id>\`（或 `.storyline\.server_cache\<会话id>\`）。

### 桥接（哪台机器都行，有 ffmpeg 即可）
```bash
python os_to_spec.py --theme "赤松宫"
# 默认自动取 outputs 下最新会话；也可指定：
python os_to_spec.py --session-dir FireRed-OpenStoryline/outputs/<会话id> --theme "赤松宫"
```
产物：`output/edit_spec.json` + `output/jy_clips/clip_*.mp4`。
脚本结尾会打印「镜头 N 段、字幕 M 条、卡点 K 个」。

### 第 2 段（Windows + 剪映 5.9）
```powershell
python jy_build.py --spec output/edit_spec.json          # 先建草稿，肉眼检查卡点
python jy_build.py --spec output/edit_spec.json --export  # 满意后自动导出（导出时别碰鼠标键盘）
```
`jy_build.py` 看到 `beat_marks` 会自动**卡点吸附**：把每个镜头切点移到最近的节拍，
并同步平移字幕时间（字幕不串位）。终端会打印 `🎯 卡点吸附：N 个切点已踩到节拍`。

---

## 二、卡点的调法（config.yaml → openstoryline.beats）

| 字段 | 作用 | 默认 |
|---|---|---|
| `enable` | 是否启用卡点（关掉=不踩点，退回顺铺） | true |
| `window_s` | 切点离卡点 ≤ 此秒数才吸附。**调大=更爱踩点**，但更可能改变原节奏 | 0.30 |
| `min_clip_s` | 单镜头吸附后不短于此秒 | 0.6 |
| `snap_last` | 成片末尾是否也对齐到卡点 | false |
| `headroom_s` | `os_to_spec` 裁镜头时多留的秒数，给「拉长吸附」留真实帧 | 0.5 |

> 经验：`window_s` 0.30 偏保守（节拍稀疏时可能一个都不吸）。想要明显的踩点节奏感，
> 先试 0.5~0.8。`os_to_spec` 会把这些写进 `edit_spec.json` 的 `snap` 段，剪映端直接读。

命令行可临时覆盖：`--no-beats`（这次不卡点）、`--no-cut`（不裁切直接引用镜头）、
`--copy-style "纪录片旁白"`、`--headroom 0.7`。

---

## 三、常见问题

| 现象 | 处理 |
|---|---|
| `❌ 没找到任何 OpenStoryline 会话产物` | 第 1 段还没跑/没跑完；或用 `--session-dir` 指定 outputs 下的会话目录 |
| `❌ 没找到 plan_timeline 的 tracks` | OpenStoryline 没跑到「排时间轴」；在 7860 里让它把时间轴生成出来再桥接 |
| 卡点 0 个 | 该 BGM 没解析出重拍，或 `enable=false`；换带鼓点的曲子，或调大 `window_s` |
| `⚠️ 没装 ffmpeg` | 桥接改为直接引用源镜头（`source_window.start≈0` 时 OK）；要精确裁切就装 ffmpeg |
| 字幕和画面对不上 | 卡点吸附已自动平移字幕；若仍偏，调小 `window_s` 或在剪映里微调 |
| `🎯 无切点落在吸附窗内` | 正常：这条片的切点本就离节拍较远；想强制踩点就调大 `window_s` |

---

## 四、桥接读了 OpenStoryline 的什么（对齐其产物结构）

`os_to_spec.py` 按「鸭子类型」从会话产物里取（抗版本漂移，best-effort）：
- `plan_timeline` → `tracks.{video,subtitles,bgm}`（毫秒）—— 镜头顺序/时长、字幕时间轴
- `split_shots`   → 镜头切片 `clip_id → 文件路径`（+ `source_window` 用来裁切）
- `select_bgm`    → `{path, bpm, beats:[ms,...]}` —— ★`beats` 就是卡点，转成秒写进 `beat_marks`
- `generate_voiceover`（可选）→ 旁白音频；没配 TTS 时为「纯字幕片」，正常

所有资源路径都存成**相对项目根**，Linux 跑桥接、Windows 跑剪映都能解析。

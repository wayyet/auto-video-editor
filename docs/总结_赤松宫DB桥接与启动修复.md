# 赤松宫 D-B 自动剪辑 · 桥接落地 + 启动修复 总结

> 日期：2026-06-14　
> 范围：本次会话把「路线 D-B」最后缺的工程补齐——写好并测通桥接 `os_to_spec.py`、给剪映端加「卡点吸附」、补 `config.yaml` 配置；并修好了 OpenStoryline 启动脚本双击报错的问题。
> 关联文档：《路线D+OpenStoryline整合方案.md》《RUNBOOK_剪映整合.md》《RUNBOOK_DB桥接_赤松宫.md》

---

## 一、一句话总结

OpenStoryline 当「第 1 段大脑」（出文案/字幕/BGM/卡点），剪映当「第 2 段装配工」（套字幕转场、按卡点切镜、导出），中间靠这次写的 **`os_to_spec.py`** 把两边接起来。**桥接代码已写好并测通**；OpenStoryline 的大模型 API 已验证可用；启动脚本的双击报错（LF 换行）也已修复。只差你在网页里跑一遍第 1 段，就能出第一条赤松宫 D-B 成片。

---

## 二、背景：路线 D-B 是什么

两段式，中间用共享文件夹 `kuaishou` 衔接：

```
你的素材(input/赤松宫) + 主题/提示词
        │
 第1段（Windows，OpenStoryline 网页 7860，纯 API 无需 GPU）
   切镜 → 看懂画面 → 写文案 → 选 BGM + 智能卡点 → 排时间轴
        │  产物落在 FireRed-OpenStoryline/outputs/<会话id>/
        ▼
 桥接（os_to_spec.py）：决策 → output/edit_spec.json（含 beat_marks）+ 裁好 jy_clips/
        │
 第2段（Windows + 剪映 5.9，jy_build.py）
   铺镜头 → 旁白 → BGM → 字幕 → 片头 → ★按卡点吸附切镜/转场 → 存草稿 → 自动导出
        ▼
   竖屏成片 mp4 → 快手发布
```

定位不变：OpenStoryline 只取「定剪决策」这一步，**它自带的 MoviePy 渲染不启用**，渲染统一交给剪映。

---

## 三、当前状态（2026-06-14）

| 项 | 状态 |
|---|---|
| OpenStoryline 环境（.venv 353 包、模型、100 BGM/18 字体/25 文案模板） | ✅ 上个会话已装好 |
| 大模型 API（MiniMax：LLM `MiniMax-M2.7-highspeed` + VLM `MiniMax-M3`） | ✅ 已验证可用（LLM/VLM 均 HTTP 200） |
| 桥接 `os_to_spec.py` | ✅ 本次写好并测通 |
| 剪映端「卡点吸附」 | ✅ 本次加进 `jy_build.py` 并通过 11 项单测 |
| `config.yaml` 的 `openstoryline:` 配置段 | ✅ 本次新增，桥接已读取 |
| 启动脚本双击报错（LF 换行） | ✅ 已修（全部 .bat 转 CRLF） |
| 第 1 段实跑（网页出片） | ⏳ 待你在 7860 里跑一遍 |
| 第 2 段（剪映装配+导出） | ⏳ 待第 1 段出了输出目录后进行 |

---

## 四、这次新增 / 改动的文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `os_to_spec.py` | ★新增 | 桥接：读 OpenStoryline 会话产物 → 生成 `output/edit_spec.json`（含 `beat_marks`）+ 用 ffmpeg 裁好 `output/jy_clips/`。自动发现最新会话；按「鸭子类型」取数，抗版本漂移。 |
| `jy_build.py` | ★改动 | 新增纯函数 `snap_timeline_to_beats()` 并接进装配流程：spec 带 `beat_marks` 时，把镜头切点吸附到最近卡点，并同步平移字幕时间（字幕不串位）。无 `beat_marks` 时行为不变（向后兼容）。 |
| `config.yaml` | ★改动 | 新增 `openstoryline:` 段：是否启用 D-B、文案风格、私有歌单、卡点开关与吸附参数（window_s/min_clip_s/headroom 等）。 |
| `RUNBOOK_DB桥接_赤松宫.md` | ★新增 | D-B 桥接这一段的操作手册（命令、调参、排错）。 |
| `提醒音.vbs` | ★新增 | 双击即播放提示音（无黑窗）。用于「任务完成 / 需要你操作」时提醒。 |

---

## 五、桥接怎么把两段接起来（数据流）

`os_to_spec.py` 从 OpenStoryline 的会话产物目录（`FireRed-OpenStoryline/outputs/<会话id>/`，或 `.storyline/.server_cache/<会话id>/`）里，按节点取这几样：

- `plan_timeline` → `tracks.{video, subtitles, bgm}`（毫秒）——镜头顺序/时长、逐句字幕时间轴。
- `split_shots` → 镜头切片 `clip_id → 文件路径`（+ `source_window`，用来裁切）。
- `select_bgm` → `{path, bpm, beats:[ms,...]}`——**`beats` 就是卡点**，转成秒写进 `edit_spec.json` 的 `beat_marks`。
- `generate_voiceover`（可选）→ 旁白音频；没配 TTS 时为「纯字幕片」，正常。

所有资源路径都存成**相对项目根**，所以 Linux 跑桥接、Windows 跑剪映都能解析。
随后 `jy_build.py` 读 `edit_spec.json`，看到 `beat_marks` 就自动做卡点吸附再装配。

---

## 六、验证做了什么（测试结果）

在沙箱里用「合成的 OpenStoryline 会话」+ ffmpeg 假片段，端到端跑了一遍，全部通过：

- **桥接**：正确发现会话、读出 3 段视频/3 条字幕/BGM/7 个卡点，裁出带 headroom 的 `jy_clips/`，写出结构正确的 `edit_spec.json`。
- **修了一个 bug**：旁白查找一度把 BGM 的 `.wav` 误当旁白——已改为只在 voiceover 节点里找并排除 BGM 路径；修后 `narration_audio` 正确为「无（纯字幕）」。
- **卡点吸附**：11 / 11 单测通过——含「无卡点不动（向后兼容）」「窗口内吸附」「不超过镜头可拉长上限 max_duration」「不短于 min_clip_s」「`snap_to_beat=false` 不吸」「字幕随切点平移且单调不越界」。
- **整链组合**：桥接产物喂进吸附函数——默认窗口 0.30s 较保守（卡点离得远就不动）；窗口放到 0.8s 时，2 个切点成功踩到节拍、总时长保持、字幕同步平移。
- `config.yaml` 的 `openstoryline` 段（copy_style / 吸附窗口 / headroom）确认被桥接读取生效。

> 说明：OpenStoryline 重栈是 Windows 专用（venv 在 Windows），所以「真实出片」必须在你机器上跑；沙箱里验证的是桥接与吸附的逻辑正确性。

---

## 七、完整出片步骤（D-B）

### 第 1 段 · OpenStoryline 出决策（你来，浏览器里，约 2 分钟）
1. 双击 `FireRed-OpenStoryline\启动网页界面.bat`，等终端出现 `Starting Web UI at http://127.0.0.1:7860` 并停住（别关）。
2. 浏览器手动打开 `http://127.0.0.1:7860`。
3. 把 `input\` 里 6 个赤松宫视频拖进去上传。
4. 给一句指令，例如：「把这些赤松宫素材剪成一条竖屏治愈系旅行 Vlog，写口语化文案、配逐句字幕、选合适 BGM 并卡点；**先生成时间轴，先别最终渲染**。」
5. 跑到「生成时间轴 / 定剪完成」（有文案+逐句字幕+选好 BGM+卡点）即可，不必最终导出 mp4。

### 桥接（我来；有 ffmpeg 即可）
```bash
python os_to_spec.py --theme "赤松宫"        # 自动抓 outputs 下最新会话
```
产物：`output/edit_spec.json` + `output/jy_clips/clip_*.mp4`。

### 第 2 段 · 剪映装配 + 导出（你的 Windows + 剪映 5.9）
```powershell
python jy_build.py --spec output/edit_spec.json           # 先建草稿，肉眼检查卡点
python jy_build.py --spec output/edit_spec.json --export  # 满意后自动导出（导出时别碰鼠标键盘）
```
`jy_build.py` 看到 `beat_marks` 会打印 `🎯 卡点吸附：N 个切点已踩到节拍`。

---

## 八、卡点调参（config.yaml → openstoryline.beats）

| 字段 | 作用 | 默认 |
|---|---|---|
| `enable` | 是否启用卡点（关掉=顺铺不踩点） | true |
| `window_s` | 切点离卡点 ≤ 此秒数才吸附。**调大=更爱踩点**，但更可能改变原节奏 | 0.30 |
| `min_clip_s` | 单镜头吸附后不短于此秒 | 0.6 |
| `snap_last` | 成片末尾是否也对齐到卡点 | false |
| `headroom_s` | 裁镜头时多留的秒数，给「拉长吸附」留真实帧 | 0.5 |

经验：想要明显的踩点节奏感，先把 `window_s` 试到 0.5~0.8。命令行也能临时覆盖：`--no-beats`、`--no-cut`、`--copy-style`、`--headroom`。

---

## 九、遇到的问题与修复

1. **双击 `启动网页界面.bat` 报错**（`'xist' / '~dp0"' / '01' / 'X]' is not recognized`）。
   - 根因：这些 `.bat` 是 **LF 换行**（Unix 风格），而 cmd 需要 **CRLF**；缺了 CR，行解析每行错位一个字符，于是 `exist→xist`、`%~dp0→~dp0`、`65001→01`、`[X]→X]`。含 `if (...)` 块的脚本对此最敏感，所以这条挂得最狠。
   - 修复：把项目里所有 `.bat`（OpenStoryline 4 个 + kuaishou 根目录 3 个）统一转成 CRLF；并确认 `.venv\Scripts\python.exe`、`activate.bat`、`agent_fastapi.py` 都在。重新双击即可正常起服务。
2. **API Key**：上个会话只差填 Key；本次确认 `config.toml` 的 `[llm]`/`[vlm]` 已填 MiniMax Key，且连通性测试 LLM/VLM 均 HTTP 200。
3. **旁白误判**（开发中发现）：桥接一度把 BGM 当旁白——已修（见第六节）。

---

## 十、下一步 / 待办

1. 你在 7860 跑第 1 段，出「文案+字幕+BGM+卡点」到生成时间轴，告诉我「跑完了」。
2. 我跑 `os_to_spec.py` 生成 `edit_spec.json` + `jy_clips/`。
3. 你在剪映端 `jy_build.py`（先建草稿检查卡点，满意再 `--export`），出第一条赤松宫 D-B 成片。
4. 按需调 `window_s` 等参数复跑，固化为你这套旅行 Vlog 的默认风格。

> 提示：第 2 段必须在装了剪映 5.9 的本机跑（要写剪映草稿目录并驱动导出）；剪映需禁用自动更新以保持 ≤5.9（见《RUNBOOK_剪映整合.md》）。

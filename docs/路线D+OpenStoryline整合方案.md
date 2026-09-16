# 路线 D + FireRed-OpenStoryline 整合方案

> 把附件原《路线 B:部署 FireRed-OpenStoryline》整合进现有《新路线 D(剪映整合)》。
> 定位:**OpenStoryline 只当「第 1 段大脑」**——负责文案仿写 / BGM 智能卡点 / 情绪理解 / 对话式定剪,
> 成片仍交给**剪映装配 + 自动导出**(第 2 段不变)。
> 日期:2026-06-14　承接《剪映整合分析_可否更自动化》与《旅游视频AI自动剪辑实施方案》

---

## 〇、为什么这样整合(一句话)

原附件把 OpenStoryline 当成一条**独立全链路路线(路线 B)**;但你现在的主力是**路线 D**——
「Claude 当导演 + 剪映当装配工」的两段式。两者重叠的部分恰恰是**第 1 段的「理解与决策」**。

所以最划算的做法不是再起一条 B,而是**把 OpenStoryline 拆开,只取它最强的「大脑」能力塞进路线 D 的第 1 段**,
让它和 Claude pipeline 一起把「剪什么、怎么讲、配什么乐、怎么卡点」想得更好,
然后照旧产出交接文件 `edit_spec.json`,交给第 2 段的剪映做特效、逐句字幕和导出。

> 一句话:**OpenStoryline 负责「想得更好」,剪映负责「做得更漂亮 + 点导出」,Claude 负责把两边串起来。**

OpenStoryline 自带的 MoviePy 渲染**在本方案里不启用**(它不产出剪映草稿,渲染层我们已经用剪映了)。
我们只用它到「定剪决策」这一步,把决策抽出来喂给剪映。

---

## 一、OpenStoryline 是什么(调研结论)

| 项 | 结论 |
|---|---|
| 出身 | 小红书 FireRedTeam 开源,Apache-2.0(**可商用**,比 short-video-factory 的 AGPL 宽松) |
| 本质 | 对话式 AI 剪辑 Agent:自然语言下指令,LLM 规划 + 工具编排,human-in-the-loop |
| 跑法 | Python ≥3.11;MCP Server + CLI(`cli.py`)/ Web(FastAPI:7860)/ Docker;模型走 API,**无需 GPU** |
| 与 Claude 的关系 | 自带 **Claude Code Skills**:`openstoryline-install`、`openstoryline-use`——可直接被 Claude Code 调用安装与使用 |
| 渲染层 | 基于 **MoviePy + FFmpeg** 程序化出片(产出成品 mp4,**不产出剪映草稿**)←本方案不用这一层 |
| 官方场景 | Demo 含**旅行 Vlog**、种草、好物分享、文艺风格等,正好对口 |

它真正值钱、也正好补路线 D 第 1 段的四块能力:

1. **智能文案生成 + few-shot 仿写**:给一段范文(种草测评 / 日常碎碎念 / 纪录片旁白……),
   它能复刻其语感、节奏、句式来写解说——这是路线 D 原来要靠提示词反复调的部分。
2. **BGM 智能卡点**:按画面内容与情绪推荐音乐,并**自动卡点**(支持导入私有歌单);
   产出「卡点时间戳」,正好能让剪映在节拍上切镜/套转场。
3. **情绪识别 + 画面理解**:逐片段拆分理解,构建带情绪曲线的故事线。
4. **对话式精修(决策层)**:"第三段删掉""换句更口语的旁白""这句字幕改成……"——
   在**出片前**就把剪辑决策聊顺,而不是等成片再返工。

> 还有一个 **ASR 口播粗剪 Skill**(自动去口头禅/语气词、按时间戳切分),旅游解说若改口播风可顺手用。

---

## 二、整合后的架构(路线 D · 增强版)

核心只动**第 1 段内部**:在 Claude pipeline 旁边并入一个「OpenStoryline 大脑」节点,
两者协作产出同一份 `edit_spec.json`。**第 2 段(剪映装配 + 导出)原封不动。**

```
你的输入:视频 + 主题 + 提示词(+ 可选:文案范文 / 私有歌单)
        │
 ┌──────┴──── 第 1 段:理解与决策(无头,云端 API,无需 GPU)─────────────────┐
 │  ① 预处理   ffmpeg + PySceneDetect 切分/抽帧                               │
 │  ② 画面理解 Claude 逐镜头打标评分          ← 保留(核心价值)              │
 │  ③ 脚本决策 ┌─ Claude:定镜头顺序/时长/高光                                │
 │            └─ ★OpenStoryline 大脑:文案仿写 + 情绪故事线 + BGM 智能卡点    │
 │  ④ 配音字幕 商用 TTS → 旁白 + 逐句时间轴   ← (可选,不必每次)            │
 │  ⑤ 选 BGM   OpenStoryline 推荐并产「卡点时间戳」 ← (可选,不必每次)       │
 │  ⑥a 裁镜头 + 写 edit_spec.json(经 os_to_spec 适配桥接合并两边决策)        │
 └───────────────────────────────┬───────────────────────────────────────────┘
                                  │ (共享文件夹 kuaishou 衔接,路径相对项目根)
 ┌──────────────── 第 2 段:装配与导出(Windows + 剪映 5.9)── 不变 ──────────┐
 │  ⑥b jy_build.py 读 edit_spec.json:                                         │
 │      铺镜头 → 旁白 → BGM → 片头 → 逐句字幕 → 按卡点套转场/滤镜 → 存草稿      │
 │  ⑦  (可选)人工进剪映微调                                                  │
 │  ⑧  auto_exporter 自动导出 1080×1920 mp4                                    │
 └─────────────────────────────────────────────────────────────────────────────┘
                                  │
                          成片 mp4 → 快手发布
```

**两个机器边界不变**:第 1 段(含 OpenStoryline)可在 Cowork/云端跑;第 2 段必须在装了剪映的 Windows 本机跑。
OpenStoryline 这一段是**纯 API + CPU**,放在第 1 段不破坏「前段无头」的特性。

> **④配音 与 ⑤选 BGM 是可选步骤(不必每次执行)**,默认可整步关掉;只跑 ①②③⑥ 也能产出可用草稿。跳过后的退化行为:
> - **跳过 ④配音**:成片不加 AI 旁白,出**纯画面 + 字幕**版本;`narration_audio` 留空,需要时再人工/后期单独补配音。
> - **跳过 ⑤选 BGM**:不预置背景乐与卡点,先出**静音/无 BGM** 版,或直接在剪映里用云端曲库手动挑;`beat_marks` 随之留空,`jy_build.py` 不做卡点吸附,成片节奏不受影响。

---

## 三、谁干什么(分工表,路线 D 增强版)

| 环节 | 负责方 | 说明 |
|---|---|---|
| 逐镜头看懂画面、打高光分 | **Claude**(pipeline) | 核心价值,保留 |
| 解说文案、语感/风格仿写 | **★OpenStoryline** | few-shot 范文仿写,比纯提示词更稳 |
| 情绪故事线、片段取舍排序 | **★OpenStoryline + Claude** | OpenStoryline 提情绪曲线,Claude 把关高光与主题 |
| BGM 选曲 + 智能卡点时间戳(**可选**) | **★OpenStoryline** | 支持私有歌单;产「卡点 marks」给剪映;**可整步跳过**,跳过则无 BGM/卡点 |
| TTS 配音(**可选**)、逐句字幕断句对时 | **Claude / pipeline** | 已跑通,保持可控;**配音可跳过**(出纯画面+字幕),字幕断句保留 |
| 决策层对话式微调(出片前) | **★OpenStoryline** | "删第三段""换口语旁白"即改即得 |
| 把两边决策并成 edit_spec.json | **os_to_spec.py(新桥接)** | 见第四节 |
| 套转场/滤镜/花字/逐句字幕动画 | **剪映 skill** | 主场,不变 |
| 竖屏构图、按卡点切镜、自动导出 | **剪映 skill** | `pyJianYingDraft` + GUI 自动化,不变 |
| 最后人工微调(可选) | **你** | 草稿可改,这是相比纯 ffmpeg 的最大灵活性 |

一句话升级:原路线 D 是「Claude 想 + 剪映做」;现在变成「**Claude 看画面 + OpenStoryline 写文案配乐卡点 + 剪映做漂亮**」。

---

## 四、桥接层:OpenStoryline 决策 → edit_spec.json(关键)

OpenStoryline 原生是「想完直接 MoviePy 出片」,**不会主动吐 `edit_spec.json`**。
所以整合的核心工程是一个**适配桥接 `os_to_spec.py`**:把 OpenStoryline 的决策产物**抽出来、翻译成路线 D 的交接格式**,
再交给剪映。这一步是 **best-effort**(OpenStoryline 内部表示随版本可能变,需按实际产物对齐)。

### 4.1 从哪里取 OpenStoryline 的决策

让 OpenStoryline 跑到「定剪/精修完成」但**不导出最终视频**,从它的产物里收集:

- **解说文案**:最终旁白文本(逐句)。
- **字幕时间轴**:逐句 start/end(它内部已对齐)。
- **BGM**:选中曲目文件 + **卡点时间戳**(beat / 切点)。
- **片段顺序与时长**:选用了哪些素材、各自时长与先后。
- **风格标签**:用了哪个范文风格 / 情绪基调(写进 spec 备查)。

> 取数优先走它对 Agent 暴露的接口/输出目录(`outputs/`、storage 记忆);拿不到结构化数据时,
> 退而解析它产出的中间文件(脚本文本、字幕 SRT、BGM 文件名 + 卡点列表)。

### 4.2 字段映射(沿用现有 spec,加 3 个可选扩展)

现有 `edit_spec.json` 不动,**新增 3 个可选字段**(剪映端可读可不读,向后兼容):

```jsonc
{
  "project_name": "旅游成片_大理治愈系",
  "resolution": {"width": 1080, "height": 1920}, "fps": 30,
  "narration_audio": "tmp/narration.mp3",
  "bgm": "bgm/xxx.mp3", "bgm_volume": 0.15,

  // ── ★ 新增:标记决策来源与风格,便于复盘 ──
  "source": "openstoryline+claude",
  "copy_style": "纪录片旁白",            // OpenStoryline 用的范文风格
  "beat_marks": [0.0, 1.8, 3.2, 5.0],    // ★ BGM 卡点时间戳(秒)

  "title": {"text": "", "anim_in": "复古打字机"},
  "subtitle_style": {"font_size": 12.0, "anim_in": "打字机", "transform_y": -0.78},
  "clips": [
    {"index":0, "source_clip":"output/jy_clips/clip_0000.mp4", "duration":3.2,
     "description":"洱海日落", "narration_segment":"看洱海的日落",
     "transition":"叠化", "effect":null, "filter":null,
     "snap_to_beat": true}          // ★ 该镜头边界对齐最近卡点
  ],
  "subtitles": [{"text":"看洱海的日落","start":0.0,"end":3.2}]
}
```

- `beat_marks`:全局卡点时间戳。`jy_build.py` 可据此**把镜头切点/转场点吸附到节拍上**。
- 每个 clip 的 `snap_to_beat`:标记该镜头边界要不要吸附卡点(默认按 `beat_marks` 就近吸附)。
- `copy_style` / `source`:仅记录用途,不影响渲染。

### 4.3 桥接落地方式(二选一)

- **A. 文案+卡点喂给 Claude pipeline(轻量,推荐起步)**
  只用 OpenStoryline 产**解说文案 + BGM + 卡点**,把这三样回填进 Claude pipeline,
  由 pipeline 既有的 `export_for_jianying()` 写出带 `beat_marks` 的 spec。改动最小。
- **B. 直接由 os_to_spec.py 出 spec(完整,后续做)**
  OpenStoryline 端跑完定剪 → `os_to_spec.py` 直接组装整份 `edit_spec.json` + 裁好 `jy_clips/`。
  适合让 OpenStoryline 主导整段决策。

> 这个桥接脚本目前**尚未落地**(它依赖 OpenStoryline 实际产物结构)。本方案先把接口和字段定清楚;
> 装好 OpenStoryline、确认它的输出目录后,我可以**直接把 `os_to_spec.py` 写出来并跑通**。

---

## 五、两种出片模式(按需选)

| 模式 | 第 1 段大脑 | 第 2 段 | 何时用 |
|---|---|---|---|
| **D-A(原路线 D)** | 纯 Claude pipeline | 剪映装配+导出 | 素材简单、文案好写,想最快出片 |
| **D-B(本整合,新)** | Claude 看画面 **+ OpenStoryline 写文案/配乐/卡点** | 剪映装配+导出 | 想要**风格化文案 + 卡点节奏**的精品旅行 Vlog |

两种共用同一套第 2 段(剪映)和同一份 `edit_spec.json` 格式,可随时切换,互不破坏。

> 注:OpenStoryline 的**全链路自渲染(MoviePy)**与**无人值守批量**能力本方案**不启用**——
> 你这次选的是「只当第 1 段大脑」。日后若想要无头批量量产,可再单开一条 OpenStoryline 全链路旁路,与本方案并存。

---

## 六、安装与配置(OpenStoryline,只装到「能产决策」)

> 目标:让 OpenStoryline 在本机能跑到「文案+卡点+定剪」,产物给桥接用。剪映那套照《RUNBOOK_剪映整合》不变。

### 6.1 环境(Python 3.11,无需 GPU)
```powershell
# 推荐 conda
conda create -n storyline python=3.11
conda activate storyline
git clone https://github.com/FireRedTeam/FireRed-OpenStoryline.git
cd FireRed-OpenStoryline
```

### 6.2 Windows 资源下载(注意:一键 build_env.sh 仅 Linux/Mac)
Windows 需**手动**下模型与资源:
- 新建 `resource` 目录;
- 下载 `models.zip` → 解压到 `.storyline` 目录;
- 下载 `resource.zip` → 解压到 `resource` 目录;
- `pip install -r requirements.txt`。

### 6.3 配 API Key
在 `config.toml` 填 LLM/相关 API-Key(参考官方 `docs/source/zh/api-key.md`)。模型全走 API,无独显也能跑。

### 6.4 用 Claude Code Skills 接入(与现有 .claude/skills 同构)
OpenStoryline 自带两个 Skill,和路线 D 已用的 `jianying-editor` 一样放 `.claude/skills`:
```powershell
# 在 OpenStoryline 仓库根目录用 Claude Code 可直接:
/openstoryline-install      # 安装、配置、首跑验证
/openstoryline-use          # 启动服务并执行剪辑/定剪流程

# 或装到全局,跨项目可用:
mkdir -p ~/.claude/skills
cp -R .claude/skills/openstoryline-install ~/.claude/skills/
cp -R .claude/skills/openstoryline-use     ~/.claude/skills/
```

### 6.5 启动(产决策时用)
```powershell
$env:PYTHONPATH="src"; python -m open_storyline.mcp.server   # MCP 服务
python cli.py                                                # 或 Web: uvicorn agent_fastapi:app --port 7860
```

---

## 七、诚实的限制与取舍

1. **桥接是 best-effort,且尚未落地**:OpenStoryline 不原生产 `edit_spec.json`,
   `os_to_spec.py` 需按它的实际产物结构对齐,版本变了可能要改。本方案先定接口,代码待装好后补。
2. **多一套环境**:OpenStoryline 自己要 Python 3.11 + conda + 下模型/资源,首次配置约半天;
   它与剪映、与 pipeline 的 venv 建议各用独立环境,避免依赖打架。
3. **Windows 没有一键脚本**:`build_env.sh` 仅 Linux/Mac,Windows 要手动下 `models.zip`/`resource.zip`。
4. **不启用它的渲染与实时 AI**:MoviePy 自渲染、GPU 渲染、它自家「一键成片」等本方案都不用——
   渲染统一交给剪映,避免两套渲染风格打架。
5. **卡点要落到剪映才生效**:`beat_marks` 只有在 `jy_build.py` 增加「吸附卡点」逻辑后才真正影响切镜/转场;
   未加之前,卡点信息只是记录,不改变成片节奏。这一步要改 `jy_build.py`(小改动,后续做)。
6. **版本锁死 5.9 不变**:第 2 段仍受剪映自动导出「≤5.9」限制,需禁用剪映自动更新(同原 RUNBOOK)。

---

## 八、成本(单条 45 秒,基本不变)

| 项目 | 估算 |
|---|---|
| 画面理解 + 文案(Claude / OpenStoryline 的 LLM API) | 约 ¥0.1~0.6(多一次 OpenStoryline 文案调用) |
| 商用 TTS 配音(约 150 字,**可选**) | ¥0.01~0.1(各家有免费额度);跳过则 0 |
| BGM(**可选**) | 免费(免版权曲库 / 私有歌单);跳过则无 |
| 剪映装配 + 导出 | 本地免费 |
| **合计** | **每条仍 < ¥1** |

OpenStoryline 只多一次文案/规划的 API 调用,渲染不用它(免),成本基本不变。

---

## 九、下一步(还差什么才能真出 D-B 的片)

1. **放素材**:`input/` 放 1~3 条视频(现在为空)。
2. **填 Key**:OpenStoryline 的 `config.toml` LLM Key(必需);商用 TTS(火山/阿里/MiniMax 任一)**仅在启用配音时才需要**。
3. **装 OpenStoryline**:按第六节装好,确认它能产出「文案 + 字幕时间轴 + BGM + 卡点」。
4. **落地桥接 + 改剪映端**(我可以来做):
   - 写 `os_to_spec.py`:把 OpenStoryline 决策 → `edit_spec.json`(含 `beat_marks`);
   - 给 `jy_build.py` 加「按 `beat_marks` 吸附切镜/转场」逻辑;
   - `config.yaml` 加 `openstoryline:` 段(范文风格、私有歌单、是否启用卡点)。
5. **跑两段**:
   - 第 1 段(可在 Cowork 跑):Claude 看画面 + OpenStoryline 出文案/卡点 → `edit_spec.json`;
   - 第 2 段(Windows + 剪映):`python jy_build.py --spec output/edit_spec.json`,满意后加 `--export`。

> 桥接脚本和剪映端的卡点逻辑都**尚未写**(本方案先把架构、字段、分工定清)。
> 你装好 OpenStoryline、确认它的输出目录后,我可以直接把 `os_to_spec.py` 与 `jy_build.py` 的卡点改动**落地并跑通第一条 D-B 成片**。

---

## 十、组件取舍:为什么这些组件不进默认流程

附件《旅游视频AI自动剪辑实施方案》里列的一批工具,大多是当初为**路线 A(纯 ffmpeg)补短板**用的。
路线 D 的两段式(剪映装配 + OpenStoryline 大脑)已经把其中几件事接管了,所以**不应一股脑都加进路线 D**。
逐个判断:

| 组件 | 原用途 | 在路线 D 里的处置 | 理由 |
|---|---|---|---|
| **PySceneDetect** | 按画面变化把长素材切成镜头 | ✅ **保留(地基)** | 切镜是 Claude 打分、裁 `jy_clips/` 的前置,剪映/OpenStoryline 都在切好的镜头上工作,不替代它 |
| **media-downloader** | 下免版权 BGM / 补空镜 | ⚠️ **降级为兜底** | BGM 已由剪映云端曲库 / OpenStoryline(私有歌单+卡点)接管;补空镜与 OpenStoryline 在线素材搜索重叠;仅 D-A 模式留作 fallback |
| **Remotion + GSAP/Lottie** | 花字片头、逐字字幕、转场 | ❌ **退出默认** | 剪映原生就能套花字/打字机字幕/转场且产可改草稿,直接顶替;仅做剪映表达不了的定制动态字幕时单独渲再导入 |
| **CutClaw(GVCLab)** | 多 Agent 按音乐卡点剪蒙太奇 | ❌ **退出默认** | 卡点已拆成 OpenStoryline 出 `beat_marks` + 剪映吸附切镜;且它是「无解说纯卡点 MV」片型,与带解说的旅游片不符 |
| **video-spec-builder + HyperFrames** | 逐镜头脚本 + HTML 渲染 | ❌ **退出默认** | 逐镜头脚本被 Claude+OpenStoryline 接管;HyperFrames 生成不了实拍画面,只能做纯文字片头卡,而片头卡剪映也能做 |

> **底线:media-downloader 当兜底,Remotion / CutClaw / video-spec-builder+HyperFrames 都降级为「特定片型才临时拉进来」的旁路,不写进主流程——否则就是用一堆为路线 A 补短板的工具,去重复剪映和 OpenStoryline 已经做好的事。**

路线 D 默认主流程因此只需:**PySceneDetect(切镜)+ Claude(看画面)+ OpenStoryline(文案/素材)+ 剪映(装配/字幕/转场/导出)**;
**配音(商用 TTS)与 BGM/卡点为可选增强,不必每次执行**——需要风格化旁白或卡点节奏时再开。

---

## 附:与原《路线 B》的对应关系

| 原附件「路线 B:部署 OpenStoryline」 | 本方案的处置 |
|---|---|
| 把 OpenStoryline 当独立全链路出片 | 拆解,只取「第 1 段大脑」并入路线 D;渲染交剪映 |
| 自动素材理解、脚本生成(范文定义风格) | 保留 → 喂进 `edit_spec.json` 的文案/分句 |
| BGM 智能卡点 | 保留 → 产 `beat_marks`,由剪映吸附 |
| 对话式微调 | 上移到「出片前的决策层」对话 |
| 满意流程存 Skill 复刻批量 | 保留;批量量产仍以剪映为渲染层(无头批量另议) |
| 自带 MoviePy 渲染出成品 mp4 | **本方案不启用**(渲染统一用剪映) |

---

**Sources(调研来源)**

- [FireRed-OpenStoryline · GitHub](https://github.com/FireRedTeam/FireRed-OpenStoryline)
- [README_zh.md(架构/特性/安装/Skills)](https://github.com/FireRedTeam/FireRed-OpenStoryline/blob/main/README_zh.md)
- [jianying-editor-skill](https://github.com/luoluoluo22/jianying-editor-skill)
- 本地文档:《剪映整合分析_可否更自动化.md》《RUNBOOK_剪映整合.md》《旅游视频AI自动剪辑实施方案.md》
- 历史会话:「Travel video auto-editing setup / implementation」

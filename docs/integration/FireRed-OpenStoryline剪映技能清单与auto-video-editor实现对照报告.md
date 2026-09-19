# FireRed-OpenStoryline 剪映技能清单与 auto-video-editor 实现对照报告

> 核查日期：2026-09-19
> 核查方式：克隆 `wayyet/FireRed-OpenStoryline` 与 `wayyet/auto-video-editor` 两个仓库最新源码（非网页浏览），逐个技能读取 `.claude/skills/*/SKILL.md` 全文（共16个），并对照读取 auto-video-editor 对应节点源码、`requirements.txt`、模板文件。
>
> **范围说明**：本报告只覆盖 `.claude/skills` 目录（剪映草稿"具体怎么改"的技能，共16个）。FireRed-OpenStoryline 的 storyline MCP 工具清单（`load_media`/`split_shots`/`generate_script` 等22个，管"看素材出分镜"）不在本报告范围内——那部分已有仓库内 `docs/integration/storyline_tools_inventory.md` 与 `auto-video-editor_FireRed集成_代码核查报告.md` 覆盖，本报告不重复分析。

---

## 0. 一句话结论

这16个技能不是可以直接"接进" auto-video-editor 的现成模块——它们是给人在 Claude Code 里交互式操作剪映客户端用的，很多步骤**故意**保留人工操作（因为试过全自动不稳）。auto-video-editor 走的是另一条路：LangGraph 全自动节点 + 本地 Pillow/JSON 处理，**没有引用**这16个技能背后的 `pyJianYingDraft` 库。这导致转场、花字动画、贴纸、音量4个节点里的关键参数（resource_id、字段名）目前都是自己标注的"占位符"，其中转场和花字动画的真实数据其实已经在技能库里现成放着，可以直接拿来用。

---

## 1. 这16个技能是什么

FireRed-OpenStoryline 仓库的 `.claude/skills/` 目录下有16个技能文件夹，可以分成4组：

| 分组 | 技能 | 一句话定位 |
|---|---|---|
| **草稿编辑基础** | `jianying-editor` | 底层SDK封装（`jianying-editor-skill`开源项目本体，含`pyJianYingDraft`），是其他技能的地基 |
| | `jianying-draft-edit` | "母技能"：变速/重排/剔瑕疵/加标题4种就地编辑的黄金法则 |
| **内容注入类** | `jianying-add-subtitles` | 加逐句口语化字幕 |
| | `jianying-speed-fit-35s` | 变速压缩到≤35秒（是draft-edit操作A的固化版） |
| | `jianying-inject-fx` | 注入VIP转场+特效 |
| | `jianying-inject-text-fx` | 注入VIP文字动画+花字 |
| | `jianying-inject-tts-sticker` | 注入TTS配音+VIP贴纸 |
| | `jianying-translate-subtitles` | 字幕译成英文并整轨替换 |
| | `jianying-inject-english-tts` | 注入英文AI配音 |
| **封面类** | `jianying-make-cover` | 9:16(剪映内人机协作)+16:9/4:3(本地渲染) |
| | `jianying-cover-localize-en` | 封面文字本地化成英文（纯本地，不进剪映） |
| **OpenStoryline运维类** | `openstoryline-install` | 从源码安装配置（假设macOS/Linux/WSL） |
| | `openstoryline-launcher` | 一键启动服务（Windows专属实测版） |
| | `openstoryline-to-jianying` | 把OpenStoryline时间线重建为剪映草稿 |
| | `openstoryline-use` | 实际剪辑流程（面向OpenClaw等Agent，非LangGraph） |
| | `kuaishou-clean-cache` | 清理项目缓存/临时文件 |

**需要注意的是**：`jianying-adjust-volume`（对应主计划文档步骤13）在这16个技能里**依然不存在**——和主计划文档里标注的"待补"缺口一致，说明这个缺口目前谁都没补上。

---

## 2. 16个技能功能清单（逐项详解）

| 技能 | 核心功能 | 关键实现方式 | 备注 |
|---|---|---|---|
| `jianying-add-subtitles` | 给已有草稿加逐句字幕 | 抽帧看画面→搜同类文案参考→写口语化文案→按分镜边界精确切分写入；同时写`draft_content.json`和`draft_info.json`两份 | 字幕必须卡在分镜边界，不能跨镜头 |
| `jianying-cover-localize-en` | 中文9:16封面转英文版 | 纯本地ffmpeg+Pillow，不进剪映；差分定位文字区域→翻译→字体映射→按原视觉配方重画 | 与剪映客户端无交互 |
| `jianying-draft-edit` | 4类就地编辑：A变速到目标时长/B删减重排/C挑瑕疵剔除/D加主副标题 | 直接操作`draft_content.json`；定义了后面很多技能继承的"黄金法则" | 是多个技能的公共基础 |
| `jianying-editor` | 底层SDK：`JyWrapper`高级API，录屏/素材导入/字幕生成/Web动效合成/项目导出 | 完整vendor了`jianying-editor-skill`开源项目（含`pyJianYingDraft`），支持macOS/Windows双平台、v5.9+/`draft_info.json`架构 | 是其余`jianying-*`技能的底层依赖，不是业务技能 |
| `jianying-inject-english-tts` | 注入英文AI配音（不动贴纸/字幕/转场） | SAMI接口；音色需批量试音+用户拍板；排版符号先改写成口语文本再合成 | |
| `jianying-inject-fx` | 注入VIP转场+VIP特效 | 从`pyJianYingDraft`元数据库直接取**真实**resource_id（转场共433个/303个VIP，特效共1097个/462个VIP）；维护历史黑名单防重复注入 | **这类资源有公开ID库** |
| `jianying-inject-text-fx` | 批量注入VIP文字动画(入场/循环/出场)+VIP花字 | 动画ID同样从元数据取（入场78VIP/循环52VIP/出场46VIP）；花字**没有**ID库，走"用户手挑样本→代码克隆"套路 | 动画有ID库，花字没有 |
| `jianying-inject-tts-sticker` | 注入TTS旁白配音+VIP贴纸 | 配音走SAMI离线合成；贴纸**没有**ID库，同样走"手挑样本→克隆重排"套路 | |
| `jianying-make-cover` | 三比例封面：9:16(剪映内)+16:9/4:3(本地) | 9:16走"人机协作"——用户手动进草稿点封面按钮、上传底图、切模板页签（这几步自动化明确不碰，实测不稳），代码接手选模板+换文案+推荐花字；16:9/4:3本地Pillow抄同一视觉配方 | 9:16是唯一必须人工介入的封面 |
| `jianying-speed-fit-35s` | 变速压缩到≤35秒（目标时长可参数化） | 是`jianying-draft-edit`操作A的固化专用版；含精确帧对齐算法防止客户端回弹超标 | |
| `jianying-translate-subtitles` | 中文字幕译成口语化英文并整轨替换 | 是`jianying-add-subtitles`的翻译替换变体；可选删除中文TTS配音轨、统一字号 | |
| `kuaishou-clean-cache` | 清理项目缓存/临时文件 | 清理OpenStoryline服务端缓存、`__pycache__`、Playwright调试产物、日志、临时帧、剪映草稿`.bak`备份 | 剪映AppData的Cache/Log目录明确禁止删除 |
| `openstoryline-install` | 从源码安装/配置/启动FireRed-OpenStoryline | 检测依赖→建venv→装依赖→下模型→填`config.toml`API key→启动服务→验证 | **假设macOS/Linux/WSL的POSIX shell**，见4.5节 |
| `openstoryline-launcher` | 一键启动MCP服务+Web界面(默认`127.0.0.1:7860`) | 等端口就绪后自动开浏览器 | 本机实测过的Windows专属精简版，与上一条内容有重叠但更具体 |
| `openstoryline-to-jianying` | 把OpenStoryline的`plan_timeline_pro`时间线重建为剪映草稿 | 尽力重建视频/字幕/配音/BGM四条轨道 | |
| `openstoryline-use` | 实际剪辑执行流程：建/续session、发指令、多轮编辑、验证渲染产物 | 主要面向OpenClaw等Agent执行策略（含飞书发送等） | **不是LangGraph相关内容**，是另一套Agent框架的用法说明 |

---

## 3. 逐项对照 auto-video-editor 实现现状

| 功能领域 | FireRed-OpenStoryline技能怎么做 | auto-video-editor现状 | 结论 |
|---|---|---|---|
| 清理缓存 | `kuaishou-clean-cache` | `node_01_clean_cache.py` | ✅ 有对应节点 |
| 启动OpenStoryline | `openstoryline-launcher`/`install` | `node_02_launch_openstoryline.py`，已验证做了真实MCP探活 | ✅ 有对应节点，且比技能文档描述的更完整 |
| 分镜转草稿 | `openstoryline-to-jianying` | `node_05_generate_draft.py` | ✅ 有对应节点 |
| 变速压缩 | `jianying-speed-fit-35s` | `node_07_speed_fit.py`（含帧对齐算法） | ✅ 有对应节点 |
| 加字幕 | `jianying-add-subtitles` | `node_08_add_subtitles.py`（76行） | ⚠️ 有节点，但用的是ASR Mock，未接入真实ASR |
| 转场+特效 | `jianying-inject-fx`：真实VIP resource_id | `node_09_inject_fx.py`：resource_id来自`templates/fx_template.json`，文件里明写`"_reverse_engineering_pending": true`、值是`"PLACEHOLDER_TRANSITION_FADE_001"`这类占位字符串 | ❌ 占位未实现，但**答案已现成存在**（见4.2节） |
| 花字动画 | `jianying-inject-text-fx`：动画有ID库，花字靠手选 | `node_10_inject_text_fx.py`（仅44行）：`entrance_animation`字段固定写`null` | ❌ 占位未实现，动画部分**答案已现成存在** |
| 贴纸 | `jianying-inject-tts-sticker`：无ID库，靠"手挑样本→克隆" | `node_11_inject_sticker.py`：纯关键词匹配`templates/sticker_template.json`，值同样是`"PLACEHOLDER_STICKER_THUMBS_UP"`这类占位符，且**没有**"用户手挑样本"这一步 | ❌ 占位未实现，且连技能库都没有公开答案 |
| 音量/淡入淡出 | 主计划文档标"待补"，16个技能里**确实没有**对应技能 | `node_13_adjust_volume.py`（234行，逻辑完整）：4个字段名（`fade_in_duration_us`等）代码里明确标注`# FIXME 字段名待逆向` | ❌ 双方都未解决，见4.3节 |
| 封面制作 | `jianying-make-cover`：9:16必须人工进剪映点选模板 | `node_14_make_covers.py`：三种比例**全部**走本地Pillow合成，9:16也只是Mock | 🔀 架构不同，见4.4节 |
| 封面英文化 | `jianying-cover-localize-en` | `node_15_localize_covers_en.py`（125行） | ✅ 有对应节点（未逐行核对细节） |
| 字幕翻译 | `jianying-translate-subtitles` | `node_16_translate_subtitles.py`（295行） | ✅ 有对应节点（未逐行核对细节） |
| 英文配音 | `jianying-inject-english-tts` | `node_17_inject_english_tts_stub.py`：文件名带`_stub`，只写静音占位；代码注释明确写"用户决策(2026-09-09)：Week 5继续stub" | ⚠️ 主动延后，不是遗漏 |
| 底层SDK复用 | `jianying-editor`（含`pyJianYingDraft`） | 全仓库`grep`未发现真实import；`requirements.txt`**没有**这个依赖；`mcp_clients/jianying_cover_client.py`注释自认"pyJianYingDraft路径留待接入" | ❌ 完全没有复用，是本报告最核心的发现 |
| `openstoryline-use`用法 | 面向OpenClaw等交互式Agent | 无对应节点 | 🔀 不适用——auto-video-editor是LangGraph全自动编排，不是交互式Agent调用模式，这个技能本身就不打算被搬过去 |

---

## 4. 关键发现

### 4.1 方法论差异：人机协作 vs 全自动

这16个技能里，好几个（`jianying-make-cover`的9:16部分、贴纸、花字）**故意**保留人工操作步骤，技能文档里写明了原因：类似"文件对话框/上传按钮偶发点不中，人工最快最稳"、"方向键微移和拖拽自动化都试过无效"这样的真实踩坑记录。

auto-video-editor 走的是相反的路：一切都要全自动节点完成，不设计人工介入点。这不是谁对谁错，而是两条不同的技术路线——但意味着**如果照抄auto-video-editor现在的做法，大概率会重新踩一遍技能库已经踩过的坑**。

### 4.2 可立即修复的缺口：转场/花字动画有真实ID库，但没被用上

转场、特效、文字动画这三类资源，`pyJianYingDraft`本身就有公开的元数据库（`jianying-editor`技能vendor进来的那份），里面是真实、验证过的resource_id：

- 转场：共433个，其中303个VIP
- 视频特效：共1097个，其中462个VIP
- 文字动画：入场78个VIP、循环52个VIP、出场46个VIP

auto-video-editor 的 `node_09_inject_fx.py`、`node_10_inject_text_fx.py` 现在用的却是自己模板文件里的占位符（`PLACEHOLDER_TRANSITION_FADE_001`这种假值），模板文件自己也写着`_reverse_engineering_pending: true`，说明团队原计划是"以后人工用剪映客户端逆向"。**这一步逆向工作其实不需要再做一遍**——把这份元数据库整理过去替换占位符即可。

### 4.3 尚无标准答案的缺口：贴纸ID、音量字段名

贴纸和音量这两处，不是"auto-video-editor没抄技能库的作业"，而是**技能库自己也没有现成答案**：

- 贴纸没有公开ID库，技能库的解法是运行时让用户在剪映里手挑一个样本，代码从生成的草稿里提取真实ID。auto-video-editor 目前只做关键词匹配到占位值，缺了"用户手挑"这一步，就算把占位符换掉也没有可用的真实ID来源。
- 音量/淡入淡出的字段名（`fade_in_duration_us`等）技能库里也没有对应技能可以参考，`node_13`自己的代码注释写的是"本机无法启动剪映做diff逆向"，属于两边都还没解决的真空地带。

### 4.4 封面：9:16人机协作流程未被复用，改成了纯本地渲染

技能库的`jianying-make-cover`对9:16封面有明确的"分工铁律"：用户必须手动进草稿点封面按钮、上传底图、切换模板页签，这几步自动化不碰，代码只负责后面选模板、换文案。

auto-video-editor 的 `node_14_make_covers.py` 对三种比例（9:16/16:9/4:3）**全部**用本地Pillow合成完成，9:16这部分代码注释里也承认"仍Pillow"（即Mock）。这意味着现在跑出来的9:16封面，用的是自己拼图渲染，而不是真正走剪映客户端里的模板系统——视觉效果和技能库文档描述的"剪映内生成"路径不是一回事。

### 4.5 其他值得注意的点

- `openstoryline-install`技能文档假设macOS/Linux/WSL环境，这和主计划文档"变更③：全流程仅Windows"存在不一致；实际启动流程应以`openstoryline-launcher`（Windows专属实测版）为准，`install`更像是上游项目带的通用安装说明，不代表本项目决策变化。
- `jianying-draft-edit`定义的4类就地编辑里，操作C（挑瑕疵镜头剔除）和操作D（加主副标题）在主计划文档17步流程里没有对应的独立步骤——这不算auto-video-editor的缺口，是最初17步规划范围本来就没纳入这两个动作。

---

## 5. 建议

关于第4.2节的转场/花字动画resource_id缺口，有两种处理方式：

| 方案 | 做法 | 推荐度 |
|---|---|---|
| **方案A（推荐）** | 把`jianying-editor`技能里`pyJianYingDraft`的元数据库（transition/effect/text动画三个文件）直接复制资源清单到`templates/fx_template.json`与`templates/text_style_template.json`，替换占位符 | ✅ 推荐——数据现成，工作量是"抄数据"而不是"重新逆向"，一两个小时内可完成 |
| 方案B | 保留现状，等Week计划里安排的"人工用剪映客户端逆向"步骤按原计划执行 | 不推荐——重复劳动，技能库已经把这份工作做完了 |

贴纸和音量两处（4.3节）不在这次二选一范围内，因为**技能库本身也没有现成答案**，仍需要按原计划走"人工介入"或"手动逆向"路径，无法靠抄现成数据跳过。

---

## 附录

### 附录A：技能 → auto-video-editor 节点映射表

| FireRed-OpenStoryline技能 | auto-video-editor节点/模块 |
|---|---|
| `kuaishou-clean-cache` | `nodes/node_01_clean_cache.py` |
| `openstoryline-launcher` / `openstoryline-install` | `nodes/node_02_launch_openstoryline.py` |
| `openstoryline-to-jianying` | `nodes/node_05_generate_draft.py` |
| `jianying-speed-fit-35s` | `nodes/node_07_speed_fit.py` |
| `jianying-add-subtitles` | `nodes/node_08_add_subtitles.py` |
| `jianying-inject-fx` | `nodes/node_09_inject_fx.py` |
| `jianying-inject-text-fx` | `nodes/node_10_inject_text_fx.py` |
| `jianying-inject-tts-sticker` | `nodes/node_11_inject_sticker.py` + `jy_common/sticker_resolver.py` |
| （16个技能中无对应） | `nodes/node_13_adjust_volume.py` |
| `jianying-make-cover` | `nodes/node_14_make_covers.py` + `mcp_clients/jianying_cover_client.py` |
| `jianying-cover-localize-en` | `nodes/node_15_localize_covers_en.py` |
| `jianying-translate-subtitles` | `nodes/node_16_translate_subtitles.py` |
| `jianying-inject-english-tts` | `nodes/node_17_inject_english_tts_stub.py`（stub） |
| `jianying-editor`（底层SDK） | 无引用（`requirements.txt`未声明依赖） |
| `jianying-draft-edit`（母技能） | 部分对应`node_06`(重排)/`node_07`(变速)，操作C/D无对应节点 |
| `openstoryline-use` | 无对应（架构不同，不适用） |

### 附录B：参考来源

- `https://github.com/wayyet/FireRed-OpenStoryline/tree/main/.claude/skills`（本次核查对象，16个技能全部读取SKILL.md原文）
- `https://github.com/wayyet/auto-video-editor`（`nodes/`、`jy_common/`、`templates/`、`mcp_clients/`、`requirements.txt`、`README.md`）
- 仓库内已有文档（本报告范围外，未重复分析）：`docs/integration/storyline_tools_inventory.md`、`docs/integration/auto-video-editor_FireRed集成_代码核查报告.md`

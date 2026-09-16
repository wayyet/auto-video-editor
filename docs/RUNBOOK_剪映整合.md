# 剪映整合操作手册（路线 D）

> 把「Claude 当导演（理解画面+写文案+定剪辑） + 剪映当装配工（套特效+逐句字幕+导出）」串起来。
> 你的剪映是 **5.9.0.11632**，正好满足 skill 自动导出对「≤5.9」的要求。

---

## 0. 一次性准备（约 20 分钟）

### 0.1 锁定剪映版本（重要）
skill 的自动导出只支持剪映 **5.9 及以下**（6.0+ 弹窗会干扰脚本）。所以要**禁止剪映自动更新**：
- 剪映设置里关掉自动更新；或把安装目录 `C:\Users\wayye\Downloads\媒体剪辑\JianyingPro\5.9.0.11632` 的更新程序改名/设为只读。

### 0.2 安装 jianying-editor skill
在项目根目录 `C:\Users\wayye\Downloads\媒体剪辑\kuaishou` 打开 PowerShell：

```powershell
# 方式一：官方一键脚本（自动下载代码+装 Python 库）
irm is.gd/rpb65M | iex

# 方式二：手动 clone 到 .claude/skills（与本项目 jy_build.py 的探测路径一致）
git clone https://github.com/luoluoluo22/jianying-editor-skill.git .claude/skills/jianying-editor
pip install -r .claude/skills/jianying-editor/requirements.txt
# 仅当你要用「网页动效转视频」才需要：
# playwright install chromium
```

### 0.3 自检环境
```powershell
python .claude/skills/jianying-editor/scripts/api_validator.py
```
看到剪映草稿目录被正确识别即可。默认目录：
`C:\Users\wayye\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft`
（已写进 `config.yaml` 的 `jianying.draft_dir`，若不对就改它。）

---

## 1. 两段式出片流程

### 第 1 段 · 理解与决策（无头，可在 Cowork/本机跑）
让 Claude（或你自己）跑 pipeline，产出**交接文件**而不是直接出 mp4：

```bash
python pipeline.py -i input/大理0613 -t "云南大理洱海 治愈系旅行" --render jianying
```
产物（都在 `output/`，相对路径，跨机器可用）：
- `output/edit_spec.json` —— 镜头顺序、时长、旁白分句、字幕时间轴、BGM、转场清单
- `output/jy_clips/clip_0000.mp4 …` —— 已按决策裁好的镜头片段
- `output/解说文案_*.txt`、`分镜分析_*.json`（沿用原流程，便于你确认）

> 想同时要一条 ffmpeg 快速预览片，用 `--render both`。

### 第 2 段 · 装配与导出（在 Windows + 剪映 的机器上跑）
```powershell
# 只生成草稿（推荐先这步，进剪映里肉眼检查/微调）
python jy_build.py --spec output/edit_spec.json

# 满意后自动导出（导出时不要碰鼠标键盘！）
python jy_build.py --spec output/edit_spec.json --export
```
`jy_build.py` 会在剪映里建一个草稿：主轨铺镜头 → 加旁白音轨 → 加 BGM → 加片头标题 → 逐句字幕（打字机动画）→ 镜头间套转场。

> 看不到新草稿？**重启剪映**，或随便点进一个旧草稿再退出来刷新即可。

---

## 2. 想要更强特效时怎么调

编辑 `config.yaml` 的 `jianying:` 段：
- `transitions`: 镜头间轮流套的转场中文名，如 `["叠化","闪白","拉远","向左滑动"]`
- `effects`: 画面特效（默认空），如 `["电影感","胶片颗粒"]`
- `filter`: 统一滤镜，如 `"京都"` / `"暖阳"`
- `title_text`: 片头大标题文字（留空＝不加片头）
- `subtitle_anim`: 字幕入场动画，默认 `"打字机"`

**查剪映里特效/转场的准确名字**（避免拼错套不上）：
```powershell
python .claude/skills/jianying-editor/scripts/asset_search.py "复古" -c transitions
python .claude/skills/jianying-editor/scripts/asset_search.py "电影" -c effects
python .claude/skills/jianying-editor/scripts/asset_search.py "暖"   -c filters
```

> `jy_build.py` 对转场/特效是 **best-effort** 套用：若你装的 skill 版本方法名不同，脚本不会报错，会在末尾打印「未套用」清单，你按上面查到的名字补即可。镜头/旁白/字幕/BGM/片头用的是 skill 已确认的稳定接口，不受影响。

---

## 3. 用剪映云端曲库当 BGM（可选，更省心）
skill 能直接用剪映自带的云端音乐：
1. 先在剪映里**播放一次**你想用的曲子（建立本地缓存）；
2. `python .claude/skills/jianying-editor/scripts/sync_jy_assets.py` 把它同步进 skill；
3. 在 `config.yaml` 里把 BGM 换成该曲名（或让 Claude 在生成 spec 时写入）。
这样就不必自己下免版权音乐了。

---

## 4. 常见问题
| 现象 | 处理 |
|---|---|
| 自动导出中途失败 | 导出时别动鼠标键盘；确认剪映是 5.9；重跑 `--export` |
| 看不到新草稿 | 重启剪映 / 点旧草稿再退出刷新 |
| 片段找不到 | 确认第 1 段产生的 `output/jy_clips/` 和 `edit_spec.json` 在同一项目根下 |
| 草稿是横屏 | 检查 `config.yaml` 的 `video.width/height` = 1080/1920；jy_build 会按此建竖屏 |
| 转场没套上 | 用 `asset_search.py` 查准确中文名后改 `config.yaml` |

---

## 5. 谁在哪台机器上跑（关键）
- **第 1 段**（理解/文案/裁剪）：纯云端 API + ffmpeg，**无需 GPU**，可在 Cowork 这边由 Claude 跑。
- **第 2 段**（装配/导出）：必须在**装了剪映的 Windows 机器**上跑，因为要写剪映草稿目录并驱动剪映导出。
- 两段通过共享文件夹 `kuaishou` 衔接（它在 Linux 沙箱和你的 Windows 上是同一个目录）。

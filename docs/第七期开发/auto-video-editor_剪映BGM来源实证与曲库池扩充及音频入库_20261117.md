# auto-video-editor:剪映 BGM 来源实证、曲库池 3→12 首扩充与音频入库

- **日期**:2026-11-17
- **性质**:真机分析与交付执行记录(零源码改动,新增 2 个脚本 + 12 个音频资产)
- **关联交付**:BGM 选曲与自动铺轨全 10 阶段(20261114,见记忆 `bgm-full-delivery-p0-9-20261114` / `jianying-music-bgm-plan-20261114`)
- **触发**:操作员提供剪映截图并提出三项任务——①分析草稿是否使用了剪映自带音频;②直接在剪映里搜索更多 VIP 曲目扩充曲库池;③把全部背景音乐保存到项目文件夹方便以后直接调用

---

## 一、任务①:草稿 BGM 来源实证(AutoVideo_console-553e3e7b34f7)

### 1.1 结论

**是。** 该草稿的 BGM 轨「Island Sway 3」是**剪映音乐库 VIP 曲目**(会员热榜分类),由管线自动写入;两条配音是本地 TTS 产物,不是库内音频。草稿共有 3 条音频素材:

| 素材 | 来源 | 证据 |
| --- | --- | --- |
| Island Sway 3(BGM 轨) | **剪映音乐库**(会员热榜 VIP 曲目,JYMusic) | 见 1.2 证据链 |
| voiceover_0001_1794843924271.wav | 本地文件(管线 TTS 产物) | path 指向 `outputs/console-553e3e7b34f7/storyline/` |
| voiceover_0002_1794843928283.wav | 本地文件(管线 TTS 产物) | 同上 |

### 1.2 证据链(三个独立来源交叉吻合)

**① 剪映草稿库的 `draft_content.json`**(路径 `C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\AutoVideo_console-553e3e7b34f7\`):

- BGM 素材带 `music_id: 7546563445158332454` —— 19 位纯数字,是剪映云端曲库曲目 ID 格式(本地文件素材此处是随机 UUID);
- `type: "music"`、`category_name: "会员热榜"`,与操作员截图左侧面板「会员热榜」区块、封面 VIP 徽标一致;
- `path` 指向剪映自缓存:`C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Cache\music\d46457f86d699d6598487f405480aaf1.mp3`;
- 时长 `252066666µs`(µs=微秒,即 4 分 12 秒),与截图面板 `04:12` 逐位一致。

**② 项目曲库索引 `runtime/jianying_music_library.local.jsonl`**(JSONL,每行一首):第三行登记的正是这首——`music_id`、`request_id(20261108225224D386B7F769BCDAF25E1F)`、缓存相对路径与草稿**逐字一致**;来源标注「回收站草稿收割」,`source_draft: AutoVideo_console-d103e894da2b`,`harvested_at: 2026-11-14T13:23:39+00:00`。

**③ 截图右栏参数**:音量 -9.1dB、淡入 2.0s、淡出 3.0s——与代码里 bgm 角色缺省值(音量 0.35 ≈ -9.1dB、淡入 2000000µs、淡出 3000000µs)完全吻合,且实测写在草稿 JSON 里。

### 1.3 写入链路(管线自动,非人工)

1. `nodes/node_05_generate_draft.py:213-257` 读选曲结果 `state["storyline_bgm_selection"]`(须含 music_id);
2. `nodes/_auto_bgm.py`(`apply_auto_bgm_track`)从曲库索引取登记条目,把**剪映原生 37 键曲库元数据原样搬进草稿**(故有 request_id、会员热榜分类等字段),并 fail-closed 校验缓存 mp3 真实存在且非空;
3. `nodes/node_13_adjust_volume.py:183-193` 按「bgm 角色」写音量 0.35 + 淡入 2s/淡出 3s(`materials.audio_fades` 原生 fade 条目 + 段引用)。

### 1.4 VIP 说明

- 代码全程**无任何 VIP 过滤/拦截**,该曲目本身就是 VIP 曲目,已正常进草稿;
- VIP 曲目授权跟账号绑定,草稿拷贝到非会员机器会失效(本项目单机使用,不涉及);
- **附带实证**:20261114 BGM 交付遗留的真机验收清单中「曲库 BGM 自动铺轨 + 音量/fade 终值」一项就此通过。

---

## 二、任务②:曲库池扩充 3 首 → 12 首

### 2.1 背景与机制

扩充前自动选曲池(`runtime/jianying_music_library.local.jsonl`,gitignored)仅 3 首(旅途的风景 / 舒缓背景音乐 Full Track / Island Sway 3),全部 2026-11-14 从回收站草稿收割。

关键机制(已核实 `scripts/harvest_jianying_music.py`):harvest 只认**出现在某草稿 `materials.audios` 里**的 `type=="music"` 素材——纯浏览/试听不产生草稿素材,进不了池子。因此扩充路径 = 通过剪映 UI 把曲目加进一个**专用草稿**,再收割入库。

### 2.2 执行过程

1. **进入首页**:剪映标题栏按钮吞合成事件,改用 `Ctrl+W` 关闭草稿编辑器回首页(任务草稿零改动);
2. **新建专用草稿**:点「开始创作」→ 默认名「11月17日」(收尾时重命名,见 2.5);
3. **搜索并添加 9 首 VIP 曲目**(搜索关键词:旅行、舒缓、大气、温馨;另 3 首从「会员热榜」面板直选,覆盖欢快/清新类):每首流程 = 点「⬇」下载(自动开始试听)→ 点「+」加入时间线;
4. **落盘预检**:`Ctrl+W` 关草稿促使写盘,`grep` 预检 `11月17日/draft_content.json` 命中 **9 条 `type:"music"` 素材**,名称与 music_id 一一对应;
5. **收割入库**(仓库根执行):

   ```bash
   # 预览不写盘
   .venv/Scripts/python.exe scripts/harvest_jianying_music.py --draft-dir "C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\11月17日" --dry-run
   # 确认后正式写入
   .venv/Scripts/python.exe scripts/harvest_jianying_music.py --draft-dir "C:\Users\wayyet\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\11月17日"
   ```

   输出:`[ok] 索引已写盘 ... 共 12 首(本次新增 9)`,9 首缓存 mp3 全部命中、无 skip;
6. **mood 标签手工校准**(3 首自动推导不准):准备出发 `[]→[travel,happy,energetic]`;心跳无数遍 `[]→[happy,energetic]`;轻松欢乐俏皮钢琴尤克里里 `[calm]→[happy,energetic]`(「轻松」含关键字「轻」被误推为 calm)。直接修 JSONL 行,harvest 重跑按 music_id 合并时人工标签幂等保留;
7. **加载验证**:`jy_common.jianying_music_library.load_jianying_music_library()` 12 条全部 `asset_available=OK`,mood 覆盖 travel/calm/soft/happy/energetic/fresh/epic/warm/storytelling。

### 2.3 新增 9 首曲目清单

| # | 曲目 | music_id | 来源 | mood | 时长 |
| --- | --- | --- | --- | --- | --- |
| 1 | 准备出发 | 7274050348452087819 | 搜索「旅行」 | travel / happy / energetic(校准) | 01:10 |
| 2 | 旅游背景 Travel Background Loop | 7573577269324892169 | 搜索「旅行」 | travel(自动) | 01:12 |
| 3 | 温柔钢琴 舒缓叙事 你的倒影 | 7491905758919362623 | 搜索「舒缓」 | calm / soft / storytelling(自动) | 02:41 |
| 4 | 轻松欢乐俏皮钢琴尤克里里 | 7378229393163175948 | 会员热榜直选 | happy / energetic(校准) | 01:13 |
| 5 | 心跳无数遍 | 7451625972677691430 | 会员热榜直选 | happy / energetic(校准) | 03:40 |
| 6 | 欢快清新旋律 | 7444180503244064805 | 会员热榜直选 | fresh / happy(自动) | 01:00 |
| 7 | 安静舒缓的钢琴曲19 | 7378278153687500841 | 搜索「舒缓」结果页 | calm / soft(自动) | 03:22 |
| 8 | 大气震撼 史诗宣传 励志 | 7377838878989813787 | 搜索「大气」 | epic(自动) | 05:26 |
| 9 | 暖心宣传 放松 温馨 | 7429110408387463178 | 搜索「温馨」 | warm(自动) | 02:44 |

### 2.4 剪映 UI 自动化配方与坑(computer-use 实测)

剪映(便携版 `kuaishou\JianyingPro\5.9.0.11632`)a11y 树全匿名(Qt 应用),必须走视觉+坐标路径:

- **所有按钮点击用 `strategy:"event"`**:默认策略对音乐卡的「+/⬇」按钮无效(静默无变化);内容区普通点击(面板 Tab、搜索框)默认策略即可;
- **标题栏(菜单/Logo)与首页右键完全吞事件**,不要尝试;关闭草稿用 `Ctrl+W`、退出应用用 `Alt+F4`(均实测可用);
- **预览条遮挡**:点「⬇」后自动开始试听,面板底部预览条会挡住下一行卡片的「+」——先点预览条「X」关掉,必要时滚轮下滚露出按钮,再点「+」;
- 下载与加轨两步之间插一次截图观察等待(等 UI 重渲染,连点过快会丢加轨);
- 进程识别:按名称绑会命中启动器存根(无窗口),须 `list_apps` 找真实 `JianyingPro.exe` 进程再按 pid 绑定。

### 2.5 收割载体草稿重命名

草稿从「11月17日」重命名为 **`AutoVideo_music-harvest-20261117`**,作为今后扩充池子的常备载体(沿用 `AutoVideo_bgm-smoke-20261114` 先例,保留不删):

- 首页右键同样吞事件,UI 重命名不可行 → 磁盘操作:退出剪映(**必须先退出**,否则应用退出时可能用内存态覆写登记文件)→ 文件夹 mv → 修补 `root_meta_info.json` 对应记录的 `draft_name` + `draft_fold_path`/`draft_json_file`/`draft_cover` 三路径 → 修补草稿内 `draft_meta_info.json` 的 `draft_name`/`draft_fold_path` → 重启剪映,首页正常显示新名,登记无损。

### 2.6 并行会话隔离

执行期间出现的两个新任务草稿(`AutoVideo_console-31a8a8e9edd2` 及其英文分支镜像)为并行会话产物,**全程未触碰**;任务草稿 `AutoVideo_console-553e3e7b34f7` 亦零改动(仅关闭其编辑器)。

---

## 三、任务③:12 首背景音乐备份入库(resource/bgms)

### 3.1 落点选择依据

- `resource/bgms/` 是仓库 2026-11-02 预留的空目录,本次首次启用;
- `resource/fonts/font_info.json` 已确立「资源目录 + info.json 清单(数组结构、一律仓库相对路径)」约定,音乐资产沿用同一约定 → 新增 `resource/bgms/bgm_info.json`;
- `resource/` 受 git 跟踪,下次 sync-github 会随仓库入库(约 30.1 MB,私有仓库可接受)。

### 3.2 产物清单

| 产物 | 说明 |
| --- | --- |
| `resource/bgms/*.mp3` ×12 | 命名 `{music_id}_{曲名}.mp3`(如 `7546563445158332454_Island_Sway_3.mp3`),字节数逐一校验与剪映缓存一致,`copy2` 保留原始时间戳;总量 30.1 MB |
| `resource/bgms/bgm_info.json` | 曲目清单:曲名 / music_id / mood / duration_ms / category_name / file_path(仓库相对)/ jianying_cache_relative_path / source_draft / harvested_at |
| `runtime/jianying_music_library.local.jsonl` | 索引每条新增 `project_audio_path` 字段(12/12 登记且文件实存);harvest 重跑按 music_id 合并时该字段原样保留 |
| `scripts/backup_bgm_audio.py` | 可复用同步工具(见 3.3) |

`bgm_info.json` 条目示例:

```json
{
    "name": "Island Sway 3",
    "music_id": "7546563445158332454",
    "mood": ["happy", "energetic"],
    "duration_ms": 252066,
    "category_name": "会员热榜",
    "file_path": "resource/bgms/7546563445158332454_Island_Sway_3.mp3",
    "jianying_cache_relative_path": "Cache/music/d46457f86d699d6598487f405480aaf1.mp3",
    "source_draft": "AutoVideo_console-d103e894da2b",
    "harvested_at": "2026-11-14T13:23:39+00:00"
}
```

### 3.3 同步工具 `scripts/backup_bgm_audio.py`

```bash
# 从仓库根执行:增量备份(已备份且字节数一致则跳过)
.venv/Scripts/python.exe scripts/backup_bgm_audio.py
# 无视幂等全部重拷
.venv/Scripts/python.exe scripts/backup_bgm_audio.py --force
```

特性:只读剪映缓存目录(不修改任何剪映数据);缓存缺失/为空 fail-closed skip 并打印原因;清单与索引原子写盘;本次输出 `[backup] 新拷 12 / 幂等跳过 0;清单 bgm_info.json 共 12 首;音频总量 30.1 MB`。

### 3.4 设计决策:管线行为零改动

写剪映草稿时 BGM 仍引用**剪映缓存路径**(`raw_material.path` 不动)——这是剪映曲库音轨的原生形态,改成项目路径可能让剪映判为异常素材。项目副本的角色:

1. **持久备份**:剪映清缓存/重装/换机都不丢曲库音频;
2. **未来直接调用的取材点**:通过索引 `project_audio_path` 字段或 `bgm_info.json` 定位。

### 3.5 验证结果

- loader 重新加载索引:12 条、全部 `asset_available=True`(剪映缓存侧不受影响);
- `project_audio_path`:12/12 登记、12/12 实际文件存在;
- 已知注意事项:索引文件位于 OneDrive 同步目录,连续写入后立即读取偶发解析失败(同步竞态),重读即正常,勿当数据损坏。

---

## 四、变更文件汇总

| 类别 | 文件 | 说明 |
| --- | --- | --- |
| 新增 | `resource/bgms/*.mp3` ×12 | BGM 音频持久副本,30.1 MB |
| 新增 | `resource/bgms/bgm_info.json` | 曲目清单(字体清单同款约定) |
| 新增 | `scripts/backup_bgm_audio.py` | 曲库音频备份同步工具 |
| 修改 | `runtime/jianying_music_library.local.jsonl` | 3→12 首、3 首 mood 校准、12 条 `project_audio_path`(gitignored 本机文件) |
| 修改(剪映侧) | 草稿 `AutoVideo_music-harvest-20261117` + 其 `root_meta_info.json` 登记 | 收割载体草稿(9 首 VIP 曲轨),保留不删 |
| 未动 | 任何源代码/测试 | `harvest_jianying_music.py`、`node_05`、`_auto_bgm`、`node_13`、`jianying_music_library.py` 零改动 |
| 未动 | 任务草稿 `AutoVideo_console-553e3e7b34f7`、并行会话草稿 | 零触碰 |

---

## 五、后续事项

1. **扩充曲库池的标准流程**(今后可重复执行):剪映打开 `AutoVideo_music-harvest-20261117` 草稿 → 音乐面板搜索/添加新 VIP 曲目 → `Ctrl+W` 关草稿落盘 → 跑 `harvest_jianying_music.py --draft-dir <该草稿目录>` → 跑 `backup_bgm_audio.py` 同步备份 → 需要时手工校准 mood 标签;
2. **sync-github**:下次同步将首次推送 12 个音频文件(30.1 MB)+ 2 个新脚本 + 本文档;
3. **真机 E2E 自然验证**:下一个真实任务的 node_05 自动选曲即从 12 首池选曲,无需专门造验收任务;
4. **待操作员**:无新增必办项(20261114 BGM 交付真机清单中的「曲库 BGM 自动铺轨 + 音量/fade 终值」项已由任务①实证通过)。

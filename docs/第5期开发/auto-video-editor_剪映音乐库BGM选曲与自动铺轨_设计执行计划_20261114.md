# 剪映音乐库 BGM 选曲与自动铺轨——设计执行计划

> 日期:2026-11-14
> 状态:**计划已成文,未执行**(关卡②行为已获用户拍板:直接放行,不挂起)
> 需求来源:用户 2026-11-14 三点指令(见 §1.1),附两张剪映截图
> 本文是实施的唯一权威依据;执行时按阶段推进,每阶段 DoD 达标再进下一阶段

---

## 1. 需求与截图解读

### 1.1 用户原始需求(3 点)

1. 参考第二张截图,把程序中原来的选择 BGM(`storyline_select_bgm`)节点修改成**直接使用剪映里面的音频中的音乐素材**。
2. 音乐素材所在的**菜单位置**参考第一张截图。
3. **在剪映草稿里面添加**以上的背景音乐。

### 1.2 两张截图说明了什么

- **截图 1(曲库菜单)**:剪映顶部菜单「音频」→「音乐素材」面板,当前展示"会员热榜"榜单,可见曲目:旅途的风景(03:11)、自由舒缓氛围音乐 Piano in Space(02:25)、舒缓背景音乐 Full Track(02:38)、Island Sway 3(04:12)、像风一样的自由、放松 Relax。全部带 VIP 标记。这就是选曲数据源的**语义来源**。
- **截图 2(目标效果)**:草稿 `Video console-50cd4c6af941` 的时间线底部已有一条蓝色音轨「旅途的风景」(波形铺满整条时间线)——这是操作员**手工**在剪映里从音乐素材面板添加的。本文目标就是让流水线自动产出同款音轨。

### 1.3 硬约束(必须先讲清楚)

- 程序**无法离线枚举**剪映云曲库的全量清单(剪映没有开放该面板的本地 API/数据库可查)。曲库 = **本机已收录曲目表**(种子 1 首「旅途的风景」)+ **收割脚本**(操作员在剪映里把想要的歌加进任意草稿→保存→跑脚本入库,见阶段 6)。表越收越多,选曲空间随之变大。
- VIP 曲目的播放/导出依赖操作员已登录 VIP 剪映账号(截图确认已满足);草稿生成本身不受影响。
- **拓扑零变更**(不增删节点、不动边)→ `SCHEMA_VERSION` 保持 8,NEIGHBORS/GOLDEN_STEPS 不动。

---

## 2. 现状与根因:BGM 为什么不落轨

### 2.1 现有选曲链路(全部带 文件:行号)

| 环节 | 位置 | 现状 |
|---|---|---|
| 节点壳子 | `nodes/storyline/node_select_bgm.py:83` `storyline_select_bgm_node` | 读分组/脚本/配音总时长,调能力层,输出 `storyline_bgm_selection` |
| 能力层 | `storyline_capabilities/select_bgm.py:49` `select_bgm` | 启发式召回 top N → LLM 挑 1 → fallback;输出 `bgm_ref/bgm_path/beats/duration_ms/tempo/default_volume/reason/method/stub/voiceover_total_ms` |
| 候选数据源 | `storyline_capabilities/bgm_selector.py:171` `load_bgm_library` → `resource/bgm_meta_ref.yaml` | **5 条占位 ID(`bgm_story_warm` 等),每条 `path: ""`,没有任何真实音频** |
| 图接线 | `graph.py:744`(GV→SB 入边)、`graph.py:752`(SB→RTR 出边) | 2026-11-14 依赖约简后 SB 单入单出 |
| 节拍对齐 | `storyline_capabilities/plan_timeline_pro.py:83,99,318-341`(`_bgm_to_internal`) | 用 selection 的 beats 做 clip 时长切分(`timeline_planner.py:228-260` beat_align)——**与落轨无关,保留** |
| 占位物化 | `storyline/mapper.py:103-113`(素材 `bgm-1`,ref=资源 ID 字符串)、`mapper.py:179-193`(段 `segment-bgm-1`,0→整条时间线,loop) | ref 不是本地文件路径 |
| 丢弃点 | `storyline/native_draft.py:495-511`(`Path(path).is_file()` 失败→`unresolved_audio`)、`:575-577`(段丢弃) | **BGM 永不进剪映草稿**,痕迹进 sidecar(随草稿目录存放的私有元数据 JSON) |
| 落轨唯一路径 | `nodes/node_12_human_add_bgm.py`(关卡②) | 人工在剪映拖 BGM;resume 后 `mirror_bgm_to_en_draft`(`:95-261`)整轨镜像进英文分支草稿 |
| 音量/淡入淡出 | `nodes/node_13_adjust_volume.py:63-71`(`_audio_tracks_by_role`:非配音 audio 轨=BGM 角色)、`:181` | 自动施加 `NODE13_VOLUME_BGM` + fade |
| QA 认账 | `draft_ops/draft_integrity.py:759-769`(`_bgm_segment_count`:非配音 audio 段数)、`:796/908-916`(bgm 类别) | 草稿里**只要有非配音音轨**即算 BGM——自动铺轨后关卡②/交付 QA 自动认账 |

### 2.2 根因一句话

选曲的候选是**没有真实音频的占位描述**(`path: ""`),mapper 只能写"资源 ID 引用",原生转换(把中间格式转成剪映认的原生 dict)因解析不到本地文件而丢弃——这是 2026-11-11 治理时的**有意设计**(`docs/第四期开发/auto-video-editor_noop空转节点_源码核验与治理设计执行计划_20261111.md:795` 明确记载"人工关卡②负责落轨"),本次把它翻转成"真曲库 + 自动落轨"。

---

## 3. 黄金样本:console-50cd4c6af941 的原生结构(逐字提取)

操作员手工加的「旅途的风景」是**剪映自己写盘的**结构,我们的生成器逐字段复刻它。草稿根:`C:/Users/wayyet/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/AutoVideo_console-50cd4c6af941/draft_content.json`(单行 minified,剪映 5.9.0,1080x1920,fps30,总时长 34800000us)。

### 3.1 云音乐素材(materials.audios 唯一一条,37 键)

```json
{"app_id":1775,"category_id":"7405872200315882277","category_name":"会员热榜","check_flag":1,"copyright_limit_type":"none","duration":190766666,"effect_id":"","formula_id":"","id":"464B08A6-0130-4e69-815A-4B59E58852B6","intensifies_path":"","is_ai_clone_tone":false,"is_text_edit_overdub":false,"is_ugc":false,"local_material_id":"","music_id":"7553871402870491155","name":"旅途的风景","path":"C:/Users/wayyet/AppData/Local/JianyingPro/User Data/Cache/music/db0778c27c2b63b997cb0d494ee0b64e.mp3","query":"","request_id":"202611141637071AFBCBFBF08506E9B10E","resource_id":"","search_id":"","source_from":"","source_platform":0,"team_id":"","text_id":"","tone_category_id":"","tone_category_name":"","tone_effect_id":"","tone_effect_name":"","tone_platform":"","tone_second_category_id":"","tone_second_category_name":"","tone_speaker":"","tone_type":"","type":"music","video_id":"","wave_points":[]}
```

关键字段语义:

| 字段 | 值 | 说明 |
|---|---|---|
| `music_id` | `7553871402870491155` | 云曲库权威 ID,选曲表的主键 |
| `category_id` / `category_name` | `7405872200315882277` / `会员热榜` | 来源榜单(截图 1 的面板) |
| `duration` | `190766666` | 微秒(190.77 秒) |
| `path` | `.../Cache/music/db0778c27c2b63b997cb0d494ee0b64e.mp3` | 剪映已下载到本机的缓存 mp3(正斜杠) |
| `request_id` | `202611141637071AFBCBFBF08506E9B10E` | 添加时的一次性请求号;**跨草稿复用已验证无害**(英文分支草稿是同一素材的逐字段拷贝) |
| `type` | `"music"` | 区别于本地音频的 `"extract_music"` |
| `app_id` / `check_flag` / `source_platform` / `copyright_limit_type` | `1775` / `1` / `0` / `"none"` | 固定值,生成器写死 |
| 其余 | 空串/false/[] | 照抄即可 |

**全草稿已验证不存在** `session_id`/`intervention_id`/`check_key`/`unique_id`/`collect_id`/`source_id`/`title`/`artist` 等字段——37 键即完备,不要多写。

### 3.2 音轨与轨段(tracks 里 type=="audio" 的轨)

```json
{"attribute":0,"flag":0,"id":"A2400B2C-7934-4b09-BF04-5299C0761607","is_default_name":true,"name":"","segments":[{"caption_info":null,"cartoon":false,"clip":null,"common_keyframes":[],"enable_adjust":false,"enable_color_correct_adjust":false,"enable_color_curves":true,"enable_color_match_adjust":false,"enable_color_wheels":true,"enable_lut":false,"enable_smart_color_adjust":false,"extra_material_refs":["96E023B8-A821-4e81-A515-3450008D20D7","audio-fade-3C272012-B173-425f-A2A5-922260563CBF","F7E3B403-FC64-4f30-B36B-D32E11C19601","A626EE39-CF15-424b-8C95-395528DF54A1","38E40AF0-FBBA-49f0-9D0E-C6E326AC0D66"],"group_id":"","hdr_settings":null,"id":"3C272012-B173-425f-A2A5-922260563CBF","intensifies_audio":false,"is_placeholder":false,"is_tone_modify":false,"keyframe_refs":[],"last_nonzero_volume":1.0,"material_id":"464B08A6-0130-4e69-815A-4B59E58852B6","render_index":0,"responsive_layout":{"enable":false,"horizontal_pos_layout":0,"size_layout":0,"target_follow":"","vertical_pos_layout":0},"reverse":false,"source_timerange":{"duration":34800000,"start":0},"speed":1.0,"target_timerange":{"duration":34800000,"start":0},"template_id":"","template_scene":"default","track_attribute":0,"track_render_index":0,"uniform_scale":null,"visible":true,"volume":0.35}],"type":"audio"}
```

要点:段 0→整条时间线(source/target 相同)、`volume: 0.35`、`speed: 1.0`、轨名空串(界面上显示的"旅途的风景"来自素材 `name`)。**我们的生成器不必逐字节照抄轨/段的全部键**——仓库现成 `build_native_audio_segment`(34 键)已被剪映接受;差异只在素材(§3.1 是必须逐键复刻的唯一对象)。

### 3.3 段的伴随素材(extra_material_refs 指向的 4+1 条)

```json
{"fade_in_duration":2000000,"fade_out_duration":3000000,"fade_type":0,"id":"audio-fade-3C272012-B173-425f-A2A5-922260563CBF","type":"audio_fade"}
{"curve_speed":null,"id":"96E023B8-A821-4e81-A515-3450008D20D7","mode":0,"speed":1.0,"type":"speed"}
{"audio_channel_mapping":0,"id":"A626EE39-CF15-424b-8C95-395528DF54A1","is_config_open":false,"type":"none"}
{"choice":0,"id":"38E40AF0-FBBA-49f0-9D0E-C6E326AC0D66","production_path":"","time_range":null,"type":"vocal_separation"}
```

- fade 规则:`audio-fade-` 前缀 + 段 id,2 秒淡入/3 秒淡出(与 node_13 既有口径一致)。
- 第 5 个引用是 `materials.beats`(节拍素材,含本机 .beat 缓存与云端 URL)。**省略**:英文分支草稿(node_12 镜像产物)就缺 beats 引用,且镜像时 `companion_buckets`(`node_12_human_add_bgm.py:189-195`)本就不复制 beats 桶;真机验收若发现异常再补(见 §8 风险表)。

---

## 4. 已确认的关键决策(用户拍板记录,实施时不再讨论)

| # | 决策 | 内容 |
|---|---|---|
| D1 | **关卡②直接放行** | 草稿已有 BGM 时关卡②**不挂起**直接过(镜像照跑);只在 BGM 缺失时挂起(自动铺入失败时人工补)。2026-11-14 用户从三选项中选定 |
| D2 | **自动铺轨无条件开启** | 不挂 `required_features.bgm` 勾选;`JY_AUTO_BGM=0` 作为应急/测试开关 |
| D3 | **prompt 零改动** | 归一化候选条目沿用现键名(`bgm_id/name/mood/duration_ms/tempo`),`prompts/tasks/select_bgm/{zh,en}/*.md` 一字不动 → prompt golden 测试零重录 |
| D4 | **SCHEMA_VERSION 保持 8** | 拓扑/展示名零变更(先例:2026-11-13 任务B 只补数据消费保持版本) |
| D5 | **beats 省略** | 依赖剪映自建节拍素材;真机验收兜底 |

---

## 5. 分阶段任务

### 阶段 0:基线冻结
- 跑 unit 全量记录基线数(记忆基线:unit 1965/0F;全量 2060/3F/6S 存量,判定新失败用 worktree/stash 对照法);记录 HEAD SHA 到本文档执行记录。
- DoD:基线数与 HEAD 写入 §9 执行记录。

### 阶段 1:音乐库表 + 加载器(复刻 TTS 音色表模式)
- 新增 `jy_common/jianying_music_library.csv`,头注释 `# Schema: music_id,name,category_id,category_name,duration_us,request_id,cache_path,mood_tags,notes`,`#` 开头为注释,逗号分隔。**种子行(唯一真实数据来源=§3.1 样本)**:
  ```
  7553871402870491155,旅途的风景,7405872200315882277,会员热榜,190766666,202611141637071AFBCBFBF08506E9B10E,C:/Users/wayyet/AppData/Local/JianyingPro/User Data/Cache/music/db0778c27c2b63b997cb0d494ee0b64e.mp3,舒缓,清新,旅行,种子:console-50cd4c6af941人工样本
  ```
  (mood_tags 含逗号需转义或改分号分隔——实现时定一种并写进 Schema 注释;notes 不允许逗号)
- 新增 `jy_common/jianying_music_library.py`:懒加载全局缓存(参照 `jy_common/jianying_tts.py:77-99` `_load_speakers` 纪律:**文件缺失/损坏返回空表不抛**);归一化条目输出 `{bgm_id:<music_id>, name, mood:[...], duration_ms, tempo:"medium", path:<cache_path>, music_id, category_id, category_name, request_id, cache_path, duration_us}`;env `JY_MUSIC_LIBRARY_CSV` 覆盖表路径(测试注入);**过滤 cache 文件不存在的条目**(fail-closed:宁可少选不可选进放不出的歌),全部不可用返回空表。
- DoD:单测覆盖 正常加载/文件缺失空表/坏行容缺/cache 过滤/注入覆盖。

### 阶段 2:select_bgm 换数据源 + 输出扩展
- `storyline_capabilities/bgm_selector.py`:`load_bgm_library`(`:171`)改为调新加载器;`_parse_bgm_meta_yaml`/`_DEFAULT_BGM_META_PATH` 保留(收割前兼容/审计用)或删除(实现时按引用面定);`rank_candidates`/`_score_library_entry` 不动。
- `storyline_capabilities/select_bgm.py`:输出 dict(`:159-170`)新增透传键 `music_id/music_name/category_id/category_name/request_id/cache_path`;`stub` 判定改为 `(not cache_path 可用) or method=="default_match"`;docstring 输出契约同步。
- `nodes/storyline/node_select_bgm.py`:壳子不动;`status_log` 消息附曲名;`:102-112` 异常兜底 dict 保持无新键(无 music_id → 下游自动走旧占位路径)。
- DoD:`tests/unit/nodes/test_node_select_bgm.py` 更新全绿;`git diff prompts/` 为空(D3)。

### 阶段 3:时间线占位抑制(清 sidecar 噪音)
- `storyline_capabilities/plan_timeline_pro.py:159-160`:selection 带 `music_id`(云模式)时 `audio.bgm_ref` 置 `None`(`contract.py:131-144` 的 `TimelineAudio.bgm_ref` 本就 Optional,无需改 contract;注意 `model_config extra="forbid"`,**不能**塞额外键进去)。
- 效果:`mapper.py:105/179` 的 `bgm-1` 占位不再生成 → `native_draft.py:495-511` 无可丢 → sidecar 不再出现 `unresolved_audio_refs` 噪音。
- 节拍对齐不受影响:`_bgm_to_internal`(`plan_timeline_pro.py:318-341`)直接读 selection,不走 canonical.audio。
- 兼容:旧 selection(无 music_id)行为逐字节不变。
- DoD:云模式/旧模式两条单测;stub 集成冒烟 sidecar 断言。

### 阶段 4:素材构造器 + 自动铺轨(node_05 内)
- `storyline/native_draft.py` 新增 `build_native_music_library_material(*, id_seed, music_id, name, category_id, category_name, request_id, cache_path, duration_us) -> dict`:**37 键逐字段照抄 §3.1**(id 用 uuid5 确定性生成,重放幂等;`id` 是素材自身 uuid,与 `music_id` 并存)。
- 新增 `nodes/_auto_bgm.py`:`apply_auto_bgm_track(draft: dict, selection: dict | None) -> list[str]`:
  1. 前置:selection 含 `music_id` 且 `cache_path` `is_file()`;env `JY_AUTO_BGM` 非 `"0"`;否则返回空 notes(不动 draft);
  2. 复用 `build_native_audio_segment`(`native_draft.py:394`,段+speed 伴随)、`build_native_audio_fade_material`(node_13 同款,2s/3s)、手写 scm/vocal_sep 伴随(值照 §3.3,scm 的 `type:"none"` 按样本而非转换路径的 `""`);段 `0 → min(draft.duration, 素材duration_us)`、`volume: 0.35`(样本值;node_13 后续会再统一施加 BGM 音量/fade);
  3. 轨:`{"type":"audio","id":"track-audio-bgm","name":"BGM","segments":[...]}`(固定 id,重放先删同 id 轨再 append,**幂等**;轨名不与任何 VOICEOVER_TRACK_NAMES 冲突 → node_12/node_13/QA 全部自动认账);
  4. materials:`nodes/_draft_io.upsert_by_id` 并入 `audios/speeds/sound_channel_mappings/vocal_separations/audio_fades`;
  5. 任何异常只记 notes(error_log),**绝不阻断**草稿生成。
- `nodes/node_05_generate_draft.py`:`storyline_to_native` 成功后(`:165` 附近)、writer 写盘前调用——单次写盘,staging 与发布稿同步携带。
- **已核实无需改动的联动**(实施时勿画蛇添足):
  - `node_12.mirror_bgm_to_en_draft` 按"替换全部非配音 audio 轨"镜像 → 自动轨随镜像进英文分支草稿;
  - `node_13` 对非配音轨统一施加 `NODE13_VOLUME_BGM`+fade;
  - 关卡② `_zh_bgm_missing`/交付 QA `build_qa_categories` 认账非配音音轨。
- DoD:构造器 37 键形状+幂等单测;`apply_auto_bgm_track` 四分支(落轨/无选择不动/cache 缺 skip+note/重放幂等)单测;node_05 stub 冒烟草稿含 `track-audio-bgm`。

### 阶段 5:关卡②"BGM 已有直接放行"(D1)
- `nodes/node_12_human_add_bgm.py:312-329` `human_add_bgm` 改为:
  1. 先 `_load_zh_draft_for_gate` + `_zh_bgm_missing`:**不缺 → 不 interrupt**,直接 `_post_resume`(`mirror_bgm_to_en_draft` 照跑);
  2. 缺 → 保持现有行为(先无条件 interrupt 一次;`bgm_required` 为真时带告警重挂,`:64-68` 文案不动);
  3. 草稿读取失败 → 保持现状(挂起确认,fail-closed:读不出就宁可人工点一下)。
- 注意:节点不 interrupt 也要执行 `_post_resume`(镜像/SHA 记录都在里面);LangGraph 顺序 interrupt 语义不变。
- DoD:三分支单测(已有 BGM 放行/缺失挂起/读取失败挂起);wiring 全绿(NEIGHBORS/GOLDEN_STEPS 未动)。

### 阶段 6:收割脚本 + 配套更新
- 新增 `scripts/harvest_jianying_music.py`:输入=一个或多个草稿目录(自动找 `draft_content.json`)+ `--csv`(默认 `jy_common/jianying_music_library.csv`);提取 `materials.audios` 中 `type=="music"` 且 `music_id` 非空、cache 文件存在的条目;按 `music_id` 去重(已存在则跳过)追加;UTF-8 写回;终端打印新增/跳过清单。**操作员工作流:剪映「音频→音乐素材」面板把想要的歌加进任意草稿→保存→跑脚本。**
- `web/node_map/workflow.json`:`storyline_select_bgm` 节点(`:363` 起)description/output_schema 手工更新为真实输出键(静态快照,权威仍以 graph.py/tests 为准);顺带核对 `node_names` 展示名。
- `state.py:295` 注释补新字段;本文档补执行记录。
- DoD:脚本对 `AutoVideo_console-50cd4c6af941` dogfood → 种子行与阶段 1 手写行一致、重复运行零新增(幂等);node_map 页面加载 workflow.json 无报错。

### 阶段 7:全量回归 + 真机验收
- unit 全量 + integration 全量;存量失败对照基线(worktree/stash 法),零新增才算过。
- 真机验收清单(操作员执行):
  1. 新建任务跑全链,关卡②**不再挂起**;
  2. 剪映打开中文草稿:底部音轨出现《旅途的风景》,可播放,VIP 正常;
  3. 音量/淡入淡出生效(node_13);
  4. 英文分支草稿同步带同款音轨;
  5. `autovideo_storyline_source.json` 无 `unresolved_audio_refs`;
  6. 创建台监控页节点图 `storyline_select_bgm` 描述为新语义。

---

## 6. 关键文件清单

**新增**(4):`jy_common/jianying_music_library.csv`、`jy_common/jianying_music_library.py`、`nodes/_auto_bgm.py`、`scripts/harvest_jianying_music.py`

**修改**(7 + 测试):

| 文件 | 改什么 |
|---|---|
| `storyline_capabilities/bgm_selector.py` | `load_bgm_library` 换源(§阶段2) |
| `storyline_capabilities/select_bgm.py` | 输出透传键 + stub 判定 |
| `storyline_capabilities/plan_timeline_pro.py` | `:159-160` 云模式 bgm_ref=None |
| `storyline/native_draft.py` | 新增云音乐素材构造器 |
| `nodes/node_05_generate_draft.py` | 调 `apply_auto_bgm_track` |
| `nodes/node_12_human_add_bgm.py` | `:312-329` 直接放行分支 |
| `nodes/storyline/node_select_bgm.py` | status_log 附曲名(最小改) |
| `web/node_map/workflow.json`、`state.py` | 快照/注释 |

**复用清单(已核实存在,勿重造)**:`build_native_audio_material/segment`(`storyline/native_draft.py:377/394`)、`build_native_audio_fade_material`(node_13 链路)、`upsert_by_id`(`nodes/_draft_io.py`)、`_zh_bgm_missing/_load_zh_draft_for_gate`(`node_12_human_add_bgm.py:72/81`)、TTS 表加载纪律(`jy_common/jianying_tts.py:77-116`)、`render_prompt`(`storyline_capabilities/prompts/__init__.py:62`)。

**明确不做**:不接剪映云曲库 API、不做音乐下载/缓存解析、不动 TTS 双后端、不做英文草稿单独选曲(镜像机制)、不 bump SCHEMA_VERSION、不动 NEIGHBORS/GOLDEN_STEPS。

---

## 7. 测试计划

| 类别 | 内容 |
|---|---|
| 更新 | `tests/unit/nodes/test_node_select_bgm.py`(新候选源/新输出键)、`tests/unit/test_storyline_capabilities_c_classes.py`(rank/load)、`tests/unit/test_storyline_contract_alignment.py`(含 `bgm_story_warm` 引用) |
| 新增 | music library 加载器(tmp fixture CSV;损坏容缺;cache 过滤;env 注入)、`build_native_music_library_material` 37 键形状+uuid 幂等、`apply_auto_bgm_track` 四分支、plan_timeline_pro 云模式 `bgm_ref=None`/旧模式不变、node_12 三分支、harvest 脚本(dogfood 草稿 fixture/去重/坏行) |
| 不受影响 | prompt golden(D3)、wiring/NEIGHBORS/GOLDEN_STEPS(D4)、`test_stage3_speed_guardrails.py:49` 的 `_BG` fixture(只用 bgm_ref) |
| 集成隔离 | 测试环境置 `JY_MUSIC_LIBRARY_CSV=<tmp>` 或 `JY_AUTO_BGM=0`,保持既有基线;新失败用 stash/worktree 对照法判定存量 |

---

## 8. 风险与兜底

| 风险 | 兜底 |
|---|---|
| 剪映 cache mp3 被清理(换机/清缓存) | 加载器过滤该曲 → 选不出;库空时退回旧行为(占位丢弃 + 关卡②人工补),fail-closed |
| 剪映对省略 beats 的复刻轨段不自动重建节拍 | D5 预案:真机验收异常时,收割脚本顺带收集 `materials.beats` 入库并在铺轨时补写 |
| 复用 request_id 跨草稿冲突 | 已有先例(en_branch 镜像轨同 request_id);若验收异常,改为按时间戳格式现造 `YYYYMMDDHHMMSS+随机` |
| node_05 重跑(resume)覆盖人工编辑 | 既有语义如此(草稿每次从 canonical 重建),非本次引入;自动轨以固定 id 幂等重建 |
| 轨名 "BGM" 与操作员手工轨混淆 | 手工轨轨名不可控但不会被删(node_08 删除守卫按轨名+素材路径双口径;我们的守卫只认 `track-audio-bgm` 固定 id) |
| VIP 失效导致导出失败 | 草稿生成/校验不受影响;导出是剪映侧行为,与现状一致 |

---

## 9. 执行记录(实施时追加)

- [ ] 阶段 0 基线:HEAD=____,unit=____,integration=____
- [ ] 阶段 1 音乐库表 + 加载器
- [ ] 阶段 2 select_bgm 换源
- [ ] 阶段 3 占位抑制
- [ ] 阶段 4 构造器 + 自动铺轨
- [ ] 阶段 5 关卡②直接放行
- [ ] 阶段 6 收割脚本 + 配套
- [ ] 阶段 7 全量回归 + 真机验收 6 项(操作员)

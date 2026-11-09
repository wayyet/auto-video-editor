# 交付 QA 阻断复盘:console-5aa2606e546b(2026-11-10)

任务:马来西亚海上清真寺旅行 Vlog(9:16 / 35s / 中文,mcode-cli,9 项必需功能全勾选)。
结果:失败 #2396,「交付 QA 终止」节点 raise。本文记录根因分析、证据链与三项修复。

## 1. 失败定性

「交付 QA 终止」(`delivery_qa_failed`)不是崩溃,是阶段 6 required-feature gate 的设计内阻断:
阀门复检(第 2 次)仍 gate_failed → 重试上限 1 用尽 → raise → `run.failed`。异常原文
(`console_run.log:232`):

```
[delivery_qa] 必需功能未通过,交付阻断(复检仍失败):
subtitles_zh=failed; voiceover_zh=failed; voiceover_en=failed; bgm=failed; stickers=failed
```

QA gate 正确兜住了本该静默交付的缺陷草稿 —— 本次是 gate 功能在正常工作。

## 2. 根因与证据链

### 2.1 最上游根因:generate_script 代码与 prompt 模板键名不一致(确定性 bug)

- `prompts/tasks/generate_script/zh/system.md` 输出格式要求
  `{"group_scripts": [{"group_id", "duration", "raw_text"}], "title"}`(L61-75);
- `storyline_capabilities/generate_script.py` 却读 `obj.get("scripts")` + `raw.get("narration")`;
- 模型 100% 照模板输出 → `raw_scripts=[]` → narration 全空。
- 三层静默吞叠加掩盖了它:①`generate_script` chat 异常 → `obj={}`;②`vlm_client.parse_json_loose`
  解析失败 → `{}`;③节点把能力层异常吞成 error_log 照常绿灯。**零痕迹**。
- 与 2026-11-08 d103e894da2b 会话"narration 空被静默吞"现象同根因,当时遗留"待查 mcode-cli 返回"。

**实锤证据**(修复后由 `chat_json_with_trace` 捕获,真实 mcode-cli 复跑):
`raw_head` 里模型返回 `{"group_scripts": [{"raw_text": "砰!我一转场,马六甲海上清真寺直接撞进镜头✨…"}],
"title": "我撞见了马六甲的海上清真寺"}` —— 旁白文案本身写得完整,只是键名对不上被丢掉。

### 2.2 连锁失败(空旁白 → 4 个 failed 类别)

| 类别 | 机制 |
| --- | --- |
| subtitles_zh | 字幕文本源 = 逐组旁白(node_08_add_subtitles.py:9),旁白空 → node_08:334 主动跳过 → 草稿 texts=[] |
| voiceover_zh | voiceover.json 两段全 `error:"empty narration text"`,0 段写入(minimax) |
| voiceover_en | 英文字幕段源于 subtitle_segments_zh(node_16a:199),zh 空 → en 空;另叠加 QA 误判(§2.3) |
| stickers | 贴纸挂字幕时间轴、按字幕 content 关键词解析 resource_id(node_11:168);字幕空 → 无注入来源;审计显示 node_11 两次运行均 `changed:false` |

### 2.3 QA 判定缺陷:voiceover_en 是误判(本应 n/a)

node_17 无英文文本返回 `tts_issue="empty_text: no text_en segments to synthesize"`(带后缀),
`draft_integrity.py` 曾用全等比较 `== "empty_text"` 不匹配 → 误判 failed。已改前缀匹配。

### 2.4 独立原因:bgm=failed(关卡②人工动作未实际完成)

必需 BGM(表单勾选)但最终草稿 0 条音轨(`materials.audios=[]`,tracks 仅 1 条视频轨)。
关卡②被确认放行但 BGM 未真正拖入草稿(②窗口内草稿 sha 有变化 `2fdf8deb→247cc430`,
疑为打开草稿的元数据改动;剪映草稿库另有 5 个历史 AutoVideo 草稿,不排除动错稿)。
node_12 此前不校验,确认即过。

### 2.5 未失败项(证明阶段 3-5 修复有效)

转场 passed(4 个叠化,resource_id 19 位合法)、video_effects 5 个已写入;字幕动画 n/a(无字幕)、
淡入淡出 n/a(无 BGM)。本次失败不是"效果写入坏了",是"内容源头断了 + 人工关卡未完成"。

## 3. 修复(三个提交,本文件随第二个入库)

1. **narration 静默吞链拆除**(storyline):
   - `vlm_client.chat_json_with_trace`:返回 `(obj, trace)`,trace 记 ok/error/raw_head(≤4000)/parsed/obj_keys;
     `chat_json` 改薄封装;`agent_gate/fixtures` 故意副本同步(守卫测试 AST 级比对)。
   - `generate_script`:走 traced 调用;坏返回(异常/拿不到脚本数组)重试 1 次;输出带 `llm_trace`;
     **键名归一对齐模板**:`_extract_scripts` 双键兼容(`group_scripts`/`scripts`),每项
     `raw_text`/`narration` 双键;schema 改 `group_scripts`。
   - `node_generate_script`:trace **先落盘** `<storyline>/script_llm_trace.json`(证据优先);
     **真实后端**(非 StubLLMClient)且 required(subtitles_zh/voiceover_zh 任一)而 narration 全空
     → 写完产物后 **raise**(源头早失败);**stub 联调不 fail-fast**(阶段 6 口径:stub 是
     "降级可见"不是"阻断",与交付 QA 对 stub 配音同口径 —— 首版漏了这条,整图集成 18 例
     被 raise 打爆,加 stub 豁免后回到基线);能力层异常不再吞,留痕后 re-raise。
2. **voiceover_en 前缀匹配**(`draft_integrity.py`):`startswith("empty_text")`,n/a 语义恢复。
3. **关卡② BGM 落轨校验**(`node_12_human_add_bgm.py`):`required_features.bgm` 为真时 resume 后
   重读磁盘草稿,`_zh_bgm_missing`(复用 `_bgm_segment_count`,配音轨按轨名豁免,与交付 QA 同口径)
   → 缺失则带告警 payload(`bgm_missing: true` + "未检测到 BGM 音轨"指引)再次 interrupt;
   草稿读取失败降级放行;未勾选 BGM 的任务保持原行为。

## 4. 验证

- unit:**1592 例全绿 exit 0**(基线 1582 + 新增 11:node_generate_script 5 / node_12 4 / draft_integrity 2);
- integration:**8 例失败 = 存量基线清单逐字一致**(阶段 6-7 文档 §3.3),新增 0 / 消失 0;
  过程记录:首版 fail-fast 漏了 stub 豁免,整图集成一度 18 例失败(均为 stub 空脚本被 raise 打爆),
  加 stub 豁免后回到基线 —— 这也验证了 fail-fast 对真实后端确实生效(集成证据:
  `raw_head='{"group_scripts": []}'` 触发 raise);
- mcode-cli 真实复跑两轮:修复前 narration 全空(键名错位实锤);修复后两段旁白完整提取;
  期间还捕获到 mcode 偶发 `CLIBackendFailed(exit=0 status=None stderr=)` —— 老代码同样会把它
  静默吞成空 narration,现由 trace 显式记录 + 能力层自动重试 1 次;
- 实机待办:新建任务复验(①generate_script 正常产旁白或早失败留 trace;②交付 QA blocked 不再
  虚列 voiceover_en;③关卡②未加 BGM 确认时重新挂起并显告警)。

## 5. 遗留

- 创建台六态卡在查看模式回放时未归约(草稿生成/草稿发布等显示"待发生",但 draft.generated/
  draft.published 事件存在)—— 展示缺陷,`handleDeliverySummary` 只挂在实时 SSE 路径,另案处理;
- "已导入视频"查看模式回填同类;
- mcode 偶发 exit=0 失败的底层原因(轻量会话不稳定?)未深究,现已有留痕可查。

## 6. 二轮实机验证(console-4bf596407c19,2026-11-10 凌晨)

首轮修复落地后的第一个真实任务(本地 02:47 启动,跑的是新代码)失败 #2589,QA blocked 只剩
`subtitles_zh; voiceover_zh`。

### 6.1 首轮修复的验证结论(全部生效)

- `script_llm_trace.json` 落盘,`obj_keys` 含 `group_scripts`,narration 完整提取 → **键名归一生效**;
- blocked 列表无 voiceover_en(英文字幕段空 → empty_text 前缀命中 → n/a)→ **误判修复生效**;
- 未勾选的 BGM/贴纸正确 not_applicable → **gate 语义正常**。

### 6.2 新根因一:字幕与配音设计耦合(已修复)

- FireRed TTS 全失败(HTTP 502)→ `timeline_planner.py:94` `has_tts=False` → `voiceover_id_map`
  保持空 dict → plan clips **无 `voiceover_id` 字段**(实机 plan 核对:4 clips 全带
  `subtitle_style.group_id`、全无 voiceover_id)→ `node_08_add_subtitles.py` 组映射全部
  continue → spans=None → **字幕静默跳过**(该 run 四份 .snapshots 快照 `text-zh` 均为 0,
  而 narration 完整);
- 修复:`_group_spans_from_draft` 组定位双通道 —— clip.voiceover_id(配音驱动,行为不变)
  优先,缺失时 fallback 读 `clip.subtitle_style.group_id`(plan clips 恒带,与 narration
  分组键同源);两者都无才跳过(不猜);
- 英文链自动受益(node_16a 翻译 zh 段 → node_08b 写英文字幕)。

### 6.3 新根因二:FireRed TTS :8010 未部署(基础设施,后议)

`TTS_BACKEND=firered`(start_production.ps1:378 生产门禁缺省)依赖的本地 FireRedTTS2 服务
**在本机从未部署** —— 检索实锤:文件系统(开发代码全树/剪艾/AI_dev_codes_setting/Tools/
Documents/Downloads,按名 `*fire*` `*tts*`)无命中、无相关 docker 镜像、开发代码树内无
`/synthesize` 服务端代码(仅客户端 `jy_common/tts_client.py:110`,注释明示"占位待核实")、
无 `.env`(无 `FIRERED_TTS_ENDPOINT` 覆盖)、历史 run 日志无任何成功连接(仅 FireRedASR
vendor 记录,B1 已移除)。生产门禁(node_generate_voiceover.py:40-58)要求 production 显式
firered,换 provider 需放宽门禁(设计决策,另议)。**决策(用户 2026-11-10):本轮不动配音侧,
部署后议;验收时表单不勾中文/英文配音**(required=False 时 QA failed 只展示不计 gate,
`draft_integrity.py` gate 语义)。

### 6.4 二轮验证

- unit:fd7ab77 纯净树实测收集 1589(与 11-09 记忆基线 1582 存在 -4 环境性漂移,与本轮改动
  无关 —— stash 对照:本轮 delta 精确 +2 = 1591);**1591 全绿 exit 0**(node_08 新增 2 例);
- integration:对照 8 例存量基线(见 §4);
- 实机待办:新建任务不勾配音 → 预期字幕落稿(texts>0)、QA 无 blocked、交付完成。

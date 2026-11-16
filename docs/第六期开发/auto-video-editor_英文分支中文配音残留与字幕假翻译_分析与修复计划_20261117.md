# 英文分支草稿中文配音残留与字幕假翻译 — 分析与修复计划

> 状态:**计划已确认(2026-11-17),实施未开始**
> 触发:用户对草稿 `en_branch_console-553e3e7b34f7_20261117_001556_972` 的真机反馈——英文草稿里中文配音没删掉、画面字幕还是中文。
> 结论:两个缺陷都是"从未实现"而非"实现了没触发",根因链见 §3/§4,修复方案见 §6。

---

## 1. 结论摘要

| 缺陷 | 现象 | 根因一句话 |
|---|---|---|
| ① 中文配音残留 | 英文草稿同时有 `Voiceover_zh`(中文配音)与 `en_voiceover`(英文配音)两条轨 | 删除逻辑从未存在过;且 fork 整目录复制、node_12 镜像回填、交付 QA 校验三处代码都在"加固"中文配音留在英文草稿里 |
| ② 字幕假翻译 | 画面字幕显示 `[EN] 中文`,没有真实英译 | `jy_common/translate_client.py` 的全局默认翻译客户端是 Week4 Mock(只加 `[EN] ` 前缀),生产代码从未替换;同一批假文本还被 node_17 拿去做英文 TTS(英语女王音色在读中文) |

---

## 2. 真机证据(草稿分析)

草稿位置:`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\en_branch_console-553e3e7b34f7_20261117_001556_972\`

### 2.1 draft_content.json 轨道结构

音频轨共 3 条,文本轨 1 条:

| 轨道名 | 类型 | 内容 |
|---|---|---|
| `Voiceover_zh` | audio | 中文配音 `voiceover_0001_1794843924271.wav`、`voiceover_0002_1794843928283.wav`(storyline 产物,应删除) |
| `en_voiceover` | audio | SAMI 英文配音 `Temp\jy_sami_7f0aa213b27c.wav`(剪映朗读合成) |
| `BGM` | audio | 音乐库 BGM(Cache\music\d46457f86d699d6598487f405480aaf1.mp3) |
| `Subtitles_en` | text | 2 条文本,内容均为 `[EN] ` + 中文原文 |

### 2.2 subtitle_en.srt / subtitle_en.json(英文翻译产物)

两条字幕全部是假翻译,`subtitle_en.json` 中 `text_en` 字段即 `[EN] 中文`:

```
1
00:00:00,000 --> 00:00:04,053
[EN] 镜头猛转,我从灰绿海面掠过白礁与远处邮轮🌊

2
00:00:04,053 --> 00:00:12,467
[EN] 我沿栈桥走向马来西亚马六甲的海上清真寺,仰看金蓝穹顶、彩色尖拱门与侧塔,游客驻足交谈。
```

### 2.3 .autovideo_draft_mutations.jsonl 时间线(draft_role=en)

| UTC 时间 | 节点 | 对英文草稿的动作 | changed_counts |
|---|---|---|---|
| 15:51:57 | node_05 | 在中文草稿铺轨(含 2 条中文配音素材) | audios 0→3 |
| 16:15:58 | node_16 | 复制中文草稿为英文草稿 | — |
| 16:16:00 | node_08b | 写入 2 条英文字幕(**假翻译**) | texts 0→2 |
| 16:16:00 | node_10b | 文字动画 | animations 0→2 |
| 16:16:08 | node_17 | **追加**英文配音,不删中文配音 | audios 3→4 |
| 16:16:08 | node_09 | 转场 | transitions 0→3 |
| 16:16:09 | node_09b | 开场钩子特效 | video_effects 0→1 |
| 16:16:10 | node_11 | 无变化 | — |
| 16:16:10 | node_12_mirror | **把中文音频轨整体回填**进英文草稿 | audios 4→6 |
| 16:16:12 | node_13 ×3 | 音量角色化 | audio_fades 0→1 |

---

## 3. 缺陷①根因链:中文配音残留(从未实现删除,且三处反向加固)

1. **fork 整目录复制带入**:`nodes/node_fork_english_branch.py:63-68` 用 `shutil.copytree` 复制快照②;而 node_08 在 fork 之前已把 `Voiceover_zh` 专用轨写入快照(`nodes/node_08_add_subtitles.py:554-559`,轨名常量 `:424`,素材 id_seed `zh-voiceover:<vid>` `:528-530`)。
2. **node_17 只加不减**:`nodes/node_17_inject_english_tts.py:118-152` 只 upsert `en-dub-1` 素材并追加 `en_voiceover` 轨;唯一的 `tracks[:]` 过滤只删自己同名旧轨(幂等),不碰 `Voiceover_zh`——与真机 `audios 3→4` 吻合。
3. **node_12 镜像回填**:`nodes/node_12_human_add_bgm.py:180-182` 取中文草稿全部 audio 轨,`:193-200` 整体替换回英文草稿,只豁免 `en_voiceover`。设计意图(关卡② 人工 BGM 轨名不可控 → 整轨复制)见 `:141-153` docstring。
4. **node_13 不静音反拉满**:`nodes/node_13_adjust_volume.py:54-60` 把 `Voiceover_zh` 归入 voiceover 角色,`:188` voiceover 音量 1.0,`:284,303` 对中英两份草稿都执行。
5. **交付 QA 把残留当正确**:`draft_ops/draft_integrity.py:540-599` `check_audio_mirror` 只豁免 `en_voiceover`(`:548-549`),英文侧缺中文音频段 → SEV_ERROR(`:578-587`)。若单方面加删除逻辑,QA 会直接阻断。
6. **文档侧从未立项**:P3b 设计(`docs/第三期开发/相似功能节点分析与设计执行计划.md:763-771`)验收项只有"英文配音轨经 node_12 后仍在";第六期文档(`docs/第六期开发/storyline_配音与BGM节点后移至node17之后_架构分析与执行计划.md:804`)把"英文分支里中文配音是否必须继续"留作未决问题。

---

## 4. 缺陷②根因链:字幕假翻译(Mock 被当真值)

1. 桩本体:`jy_common/translate_client.py:24-44` `MockTranslateClient.translate` 第 42 行拼 `f"{self._prefix}{text_zh}"`,默认前缀 `[EN] `。
2. **桩是全局默认**:`jy_common/translate_client.py:47` `_default_client: TranslateClient = MockTranslateClient()`;预留替换钩子 `set_default_client`(`:54-57`)全仓生产代码零调用,仅测试调用。
3. 翻译节点:`nodes/node_16a_translate_and_check.py:76` 调 `translate_segments(segments_zh)` → 走 Mock;`:72` marker 幂等(subtitle_en.json 存在即跳过翻译)。
4. 产物落盘:`nodes/node_16_translate_subtitles.py:116-137` `_write_marker` 把 text_en 写进草稿 `materials.texts[].text_en`,`:152-158` `_save_marker` 写 subtitle_en.json,`:165-170` 写 subtitle_en.srt。
5. **写进画面**:`nodes/node_08b_add_subtitles_en.py:64-80` 把假 `text_en` 同时写进 `content`(剪映文本轨显示内容)与 `text_en`,经 `sync_text_track` 铺上 `Subtitles_en` 轨——用户看到中文的直接环节。
6. **污染英文配音**:`nodes/node_17_inject_english_tts.py:64-75` `_join_segments_text` 用同一批 `text_en` 拼 TTS 文本 → "英语女王"音色读中文。
7. **封面标题同款**:`nodes/node_15_localize_covers_en.py:49` `title_en = f"[EN] {title_zh}"`。
8. **注入点只装了 storyline 的 LLM,没装翻译**:`scripts/run_workflow.py:114-129` `_apply_llm_backend` 与 `agent_gate/runner.py:425-429` `install_client` 都只调 `storyline_capabilities.vlm_client.set_default_client`。
9. 已有记录:该 mock 早在 `docs/第四期开发/剪映草稿缺失转场动画文案配音分析与设计执行计划.md:1340` 被点名,`:1658-1676`(§7.12/WP9)决策"英文线另立项"但一直未执行。

---

## 5. 已拍板决策(2026-11-17 用户确认)

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 英文草稿里的中文配音 | **无条件删除**(fork 建立英文分支时即删;英文 TTS 失败则英文草稿无配音,交付 QA 可能 fail-closed,知情接受) |
| D2 | 真实翻译实现 | **复用生产 LLM 后端**(mcode-cli/claude-cli,与脚本生成同链路);stub 模式保持 Mock 并显式标注,不冒充真实成功 |
| D3 | 封面标题同款 mock | **顺带修**(node_15) |
| D4 | 旧任务/旧草稿 | **只改代码,新建任务验证**;`en_branch_console-553e3e7b34f7` 留作根因证据不动 |

---

## 6. 修复方案

### A. 英文草稿无条件删除中文配音(三处 + 一处常量)

1. **`nodes/_dual_draft.py`**:新增权威常量 `ZH_VOICEOVER_TRACK_NAME = "Voiceover_zh"`(与 `EN_VOICEOVER_TRACK_NAME` 并列,`__all__` 同步);`nodes/node_08_add_subtitles.py` 的私有常量 `_ZH_VOICEOVER_TRACK_NAME` 改为从此处 import,消除双处漂移。
2. **`nodes/node_fork_english_branch.py`**(删除落点):copytree 成功后、目录幂等复用路径(`_fork` 两个 return 分支)都执行"清中文配音":`load_draft` / `write_draft(audit_node="node_fork_english_branch")` 读写 en_branch 目录的 draft_content.json——移除 `name == ZH_VOICEOVER_TRACK_NAME` 的 audio 轨,并从 `materials.audios` 删掉仅被该轨 segment 引用的素材(voiceover_000x.wav);记 status_log tag `node_fork_zh_voiceover_removed:<段数>`。删除失败按 fail-fast 处理(删不干净 = 缺陷回归,不降级)。
3. **`nodes/node_12_human_add_bgm.py` `mirror_bgm_to_en_draft`**:`audio_tracks` 选取(`:180-182`)排除 `ZH_VOICEOVER_TRACK_NAME` 轨(镜像只回填 BGM 等轨);合并前(`:195-200`)把英文草稿里可能残留的同名轨一并清掉(双保险);`referenced_ids` / companion refs 逻辑随之自然不覆盖配音素材。
4. **`draft_ops/draft_integrity.py` `check_audio_mirror`**:zh 侧比对集合(`zh_seg_ids`)、en 侧(`en_tracks`)、material 解析循环(`:592-598`)均排除 `ZH_VOICEOVER_TRACK_NAME` 轨(与 `en_voiceover` 豁免同款);docstring 同步更新。

node_13 无需改(`Voiceover_zh` 轨不存在后 `is_voiceover_track` 自然空转)。

### B. 真实翻译接入(字幕 + 配音文本 + 封面标题)

5. **`jy_common/translate_client.py`**:新增 `LLMTranslateClient`,实现既有 `TranslateClient` Protocol:
   - 内部经 `storyline_capabilities.vlm_client.chat_json`(`:222`;延迟 import 防循环依赖)批量翻译,输出严格 JSON `{"translations":[{"index":i,"text_en":"..."}]}`;
   - prompt 约束:口语化短句、适配字幕与 TTS 朗读、保留 emoji/数字/专有名词、禁止添加任何前缀、长度贴近原文;
   - 解析失败用 `chat_json_with_trace`(`:174`)留痕重试 1 次,仍失败则 raise(fail-closed,错误可见;marker 未写,resume 可重试);按 index 对齐回填,start_ms/end_ms 继承输入。
6. **注入点两处**:
   - `scripts/run_workflow.py` `_apply_llm_backend`(`:127-128` 后):backend ∈ {claude-cli, mcode-cli} 时追加 `translate_client.set_default_client(LLMTranslateClient())`;stub 保持 Mock(与 P2-1 "stub 显式标注不冒充真实成功"口径一致);
   - `agent_gate/runner.py` `install_client`(`:425-429`)同款处理(按需加 backend 参数)。
7. **marker 指纹防旧假翻译复用**(`nodes/node_16_translate_subtitles.py`):`_save_marker`(`:152`)改写 `{"translator": ..., "segments": [...]}`;`_marker_exists`(`:140`)加 `translator` 参数——兼容旧 list 格式(视为 legacy-mock),translator 不匹配返回 False 触发重翻;`node_16a`(`:72-85`)传入当前 translator 标识("mock"/"llm")。
8. **`nodes/node_15_localize_covers_en.py:49`**:封面标题 `[EN] ` 拼接改为走 `LLMTranslateClient` 翻译单条;失败降级保留中文标题 + error_log(封面非关键路径,不阻塞)。

### C. 文档

9. 本文档即执行记录;顺带回答第六期文档遗留开放问题(`storyline_配音与BGM节点后移至node17之后_架构分析与执行计划.md:804`):英文 TTS 失败时中文配音**不保留**,交付 QA fail-closed。

---

## 7. 测试计划

- **更新**:
  - `tests/unit/test_node_12_human_add_bgm.py`(镜像不回填 Voiceover_zh);
  - `tests/unit/test_draft_integrity.py`(QA 排除后无误报);
  - `tests/unit/test_node_16_translate_subtitles.py`(marker 新格式 + legacy 兼容);
  - `tests/unit/test_node_16a_translate_and_check.py`(指纹不匹配触发重翻);
  - `tests/unit/test_node_17_inject_english_tts.py`(如 fixture 预置 Voiceover_zh 需调整)。
- **新增**:`LLMTranslateClient` 单测(prompt 构造/解析/重试/失败 raise)、fork 删轨单测(含幂等复用路径)、node_15 封面翻译降级单测。
- **基线口径**:全量 unit 顺序跑零新增失败;integration 维持 3 存量 F 口径,新失败用 worktree 对照法判定。

---

## 8. 端到端验收清单(新建任务)

| # | 验收点 | 通过标准 |
|---|---|---|
| 1 | 新英文草稿轨道 | **无** `Voiceover_zh` 轨、无 voiceover_000x.wav 素材残留 |
| 2 | 英文配音轨 | `en_voiceover` 轨存在,TTS 读真英文文本 |
| 3 | 画面字幕 | `Subtitles_en` 轨为真英文;`subtitle_en.srt` 无 `[EN] ` 前缀 |
| 4 | 封面标题 | 英文封面标题为真译名,无 `[EN] ` 前缀 |
| 5 | 交付 QA | 通过(英文 TTS 成功的前提下) |

旧任务 `console-553e3e7b34f7` 与旧草稿保持不动。

---

## 9. 关键文件清单

| 文件 | 改动 |
|---|---|
| `nodes/_dual_draft.py` | 新增 `ZH_VOICEOVER_TRACK_NAME` 常量 |
| `nodes/node_fork_english_branch.py` | fork 后删中文配音轨+素材(含幂等复用路径) |
| `nodes/node_12_human_add_bgm.py` | 镜像跳过 Voiceover_zh + 清残留 |
| `draft_ops/draft_integrity.py` | check_audio_mirror 双侧排除 Voiceover_zh |
| `jy_common/translate_client.py` | 新增 `LLMTranslateClient` |
| `nodes/node_16_translate_subtitles.py` | marker 格式升级 + translator 指纹 |
| `nodes/node_16a_translate_and_check.py` | 传 translator 标识 |
| `nodes/node_15_localize_covers_en.py` | 封面标题真翻译(降级保留中文) |
| `nodes/node_08_add_subtitles.py` | 轨名常量改从 `_dual_draft` import |
| `scripts/run_workflow.py` / `agent_gate/runner.py` | 真实后端时注入 LLMTranslateClient |
| `tests/unit/test_*.py`(§7 所列) | 更新 + 新增 |

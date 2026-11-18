# 输入剪辑要求逐条生效：链路分析与实施计划

- **日期**: 2026-11-19
- **触发**: 用户在创建台截图圈出「01·输入剪辑要求」的 161 字自由文本（任务 console-b0b2a64a78c6），要求分析工作流执行节点有没有按这段文本执行任务；分析定案后追加目标——让文本指令真正逐条生效
- **状态**: 链路分析已定案（含实跑证据）；三项改造实施计划已定稿待执行
- **关联任务**: console-b0b2a64a78c6（2026-11-18 15:40 启动，已完成）
- **关联文档**: [网页话题检索失败分析与Bing切换计划](auto-video-editor_网页话题检索失败分析与Bing切换计划_20261119.md)（并行会话产出；后续已拍板引擎切 **Google+本地 SOCKS5(1088)**，Bing 方案作废——本计划的「检索词接入」管检索词从哪来，引擎切换计划管检索可用性，互补不冲突）

---

## 一、分析：这 161 字现在到底有没有被用上

### 1.1 结论摘要

**「整体提示」链路存在且部分生效；「逐条指令解析执行」的机制不存在。**

- 8 个节点把这段文本注入 LLM prompt 作整体风格/主题提示（软提示），其中文案主题确实生效（成片第一句就点题马来西亚马六甲海上清真寺）；
- 但文本里的 4 条编号指令没有一条被「逐条执行」——流水线按固定能力自动跑，不理解「#1、#2」这种指令结构；
- 「开场加强转场」「按需求定制标题」是明确的能力缺口；「网上搜索用你的主题词」是被设计排除的（检索词刻意由画面自动构造）。

### 1.2 链路核实：文本确实进了工作流（链路通）

| 环节 | 位置 | 说明 |
|---|---|---|
| 前端文本框 | `web/console.html:157` | `<textarea id="promptInput">`，即「01·输入剪辑要求」 |
| 提交载荷 | `web/console.html:756` | `readParams()` 里 `prompt: promptInput.value` |
| 后端接收 | `web/console_service.py:112` | 透传给 `build_initial_state` |
| 落入图状态 | `web/console_state.py:83-92` | 写进 `storyline_targets.user_request` + `script_request` 两个键 |

**实跑验证**: `outputs/console-b0b2a64a78c6/initial_state.json` 中 `"user_request"` 完整存着 161 字原文（含「#1、附件上传的这些视频是关于：马来西亚吉隆坡武吉 马六甲的海上清真寺……」逐字吻合）。

链路没有断点：前端发了 → 后端存了 → 进了 WorkflowState。问题不在链路，在**消费方式**。

### 1.3 真正读取这段文本的 8 个节点（软提示）

统一读取入口: `nodes/storyline/_common.py:274-289` `extract_user_request`（从 `storyline_targets` 按 `user_request` / `script_request` / `style_request` 顺序回退取值）。

| # | 节点 | 读取位置 | 用途 |
|---|---|---|---|
| 1 | generate_script（文案生成） | `node_generate_script.py:107` | 用户要求整体注入文案 LLM prompt（`{{user_request}}` 槽位） |
| 2 | script_template_recommendation（文案模板推荐） | `node_script_template_recommendation.py:77` | 模板推荐参考 |
| 3 | filter_clips（选段） | `node_filter_clips.py:48` | prompt 注入 |
| 4 | group_clips（分组） | `node_group_clips.py:48` | prompt 注入 |
| 5 | recommend_transition（转场语义推荐） | `node_recommend_transition.py:44` | prompt 模板「用户对转场的要求: {{user_request}}」，但 LLM 只能从受控词表 cut/fade/zoom/slide/whip/creative 选 |
| 6 | recommend_text（字幕文本推荐） | `node_recommend_text.py:49` | prompt 注入 |
| 7 | select_bgm（选曲） | `node_select_bgm.py:86` | 从文本抽 mood 关键词（旅行/清新/激进等）参与选曲 |
| 8 | generate_voiceover（配音） | `node_generate_voiceover.py:83` | prompt 注入 |

### 1.4 四条指令逐条核查（含实跑证据）

**指令 #1：马来西亚海上清真寺旅行 Vlog 主题**

✅ **生效（软提示）**。generate_script 的 LLM 拿到了完整文本。实跑证据——`outputs/console-b0b2a64a78c6/storyline/script.json` 文案第一句:

> 「刷地一转，海风扑面——马来西亚马六甲，海上清真寺，我来了！✨」

**指令 #2：「开头 3 秒用最炸的转场 + 大字标题」**

❌ **未逐条执行，两项均为能力缺口**:

- **开场加强转场**: 无对应逻辑。转场走两层: ①语义层 LLM 从受控词表选语义（`storyline_capabilities/recommend_transition.py:33-36`，user_request 仅作弱提示之一）→ ②落草稿层语义映射到 5 个池后**纯确定性 sha256 轮换**（`nodes/node_09_inject_fx.py:110-117`，seed 输入 = `job_id|boundary_index|pool_key`，与用户文本、素材语义均无关）。边界 0（开场切点）没有任何加强逻辑，与其它边界同权；唯一涉及边界 0 的是 all-cut 保底（`node_09_inject_fx.py:295-300`，全部边界硬切时把边界 0 回退 fade——是防 QA 误判的保底，不是加强）。
- **大字标题**: node_09b 开场钩子能力存在但样式写死（`node_09b_inject_opening_hook.py:70-72`，字号 12/加粗/上半屏；时长 min(3s, 首段)），标题文本 = 第 1 条字幕在第一个标点前切句、最多 12 字（`jy_common/hook_title.py:99-125`），不是按用户文本定制。`state["hook_title"]` 显式覆盖键虽在 `state.py:232` 声明，但全仓无任何写入方（不可达）。

**指令 #3：「先到网上搜索相关的主题和资料」**

❌ **双重未生效**:

- **检索词刻意不用用户文本**: web_topic 节点每轮必跑（开关 `config.py:52-53` `STORYLINE_ENABLE_WEB_TOPIC` 默认 true），但检索词由 `nodes/storyline/_common.py:470-508` `build_web_topic_query` 自动构造 = 画面概述（`understanding.overall`）首句 + 分组主题前 3 个；创建台侧 `web/console_state.py:11-15` 注释明写「绝不写 query」（冻结契约）。实跑证据——`storyline/search_web_topic.json` 的 query 是「素材整体呈现了一段海滨宗教建筑群的游览记录。」，与用户文本零关系。
- **本次实跑检索还失败了**: 同文件 `status: failed`、`error_code: PARSE_ERROR`（DuckDuckGo 页面改版导致解析不到结果锚点）——详见关联文档《网页话题检索失败分析与Bing切换计划》。且即使检索成功，结果也只作文案 LLM 的软参考（`node_generate_script.py:53-58` 注入 `web_research` 独立块），对视觉素材/转场/时间轴零影响。

**指令 #4：「自动切镜、看懂画面并做智能卡点然后生成时间轴」**

✅ **但这是全自动能力，本来就不读文本**（不算「生效」，算「无关」）:

- 切镜: `node_split_shots.py:60-62` TransNetV2 主推理，只读素材清单；
- 看懂画面: `node_understand_clips.py:48-52` 只传 shots+media+lang；
- 卡点/时间轴: `storyline_capabilities/timeline_planner.py` 三选一分配（TTS 驱动 / beat_align 节拍对齐 :345-377 / target 等比），纯数学；
- 唯一间接通道: select_bgm 从文本抽「旅行」等关键词影响选曲，进而影响节拍周期。

### 1.5 定性总结表

| 指令 | 现状定性 |
|---|---|
| #1 主题 | 软提示生效（文案点题实锤） |
| #2 开头3秒最炸转场 | **能力缺口**（转场池选择与文本无关，边界 0 无加强） |
| #2 大字标题 | **能力缺口**（样式写死、文本取自第 1 条字幕、显式键不可达） |
| #3 网上搜索 | **设计排除 + 本次实跑失败**（检索词刻意自动构造；DDG 改版解析失败） |
| #4 自动切镜/卡点/时间轴 | 全自动能力覆盖，与文本无关 |

---

## 二、模板提示词（实施后创建台这样填）

机制 = **结构化模板标记【】** + 确定性解析，fail-closed: 无标记时行为与现状完全一致。

### 2.1 模板骨架 + 填写示例

```
【主题】马来西亚吉隆坡武吉马六甲海上清真寺旅行Vlog
【开场】开头3秒用最炸的转场，配大字标题
【检索】海上清真寺旅游攻略和打卡玩法
【标题】马六甲海上清真寺之旅
```

### 2.2 填写规则

| 栏目 | 作用 | 留空/删行时 |
|---|---|---|
| 【主题】 | 检索词第二优先来源；同时随全文进文案 LLM | 该栏不参与检索词（其余照旧） |
| 【开场】 | 含「转场」+强度词（炸/最炸/震撼/猛/炫/酷/冲击）才触发开场转场加强；写「不要转场」等否定表述不触发 | 开场转场按原语义推荐，不加强 |
| 【检索】 | 检索词第一优先来源；**不要写「小红书/抖音」等平台名**（会干扰搜索引擎） | 回退用【主题】；再没有 → 维持现状（画面概述首句） |
| 【标题】 | 开场大字标题原文，**≤12 字** | AI 自动起（LLM title，兜底第 1 条字幕切句） |

- 整行删掉 = 该栏回退自动行为；
- 完全不用【】标记 = 与现状行为完全一致（向后兼容）；
- 大字标题能力恒开（现状即有），无需在【开场】里声明「大字标题」字样。

---

## 三、实施计划

### 3.1 设计总览

| 改造 | 触发条件 | 生效机制 | 不触发时 |
|---|---|---|---|
| ① 检索词接入 | 【检索】或【主题】存在 | 注入 `build_web_topic_query` 组件链首位 | 画面概述首句（现状） |
| ② 开场转场加强 | 【开场】含转场+强度词（或自由文本兜底命中） | 边界 0 语义强制 `whip`（pool_whip 甩镜类 = 五池中最炸） | 原语义推荐（现状） |
| ③ 标题定制 | 【标题】存在，或 LLM 输出 title | `state["hook_title"]` 显式键接通（三级优先链） | 第 1 条字幕切句（现状） |

**设计红利**（调研发现，大幅简化实施）:

- `generate_script` 的 system 模板（zh/en 两版，`prompts/tasks/generate_script/*/system.md`）**早就要求 LLM 输出 `title` 字段**（「创作原则 #5: title 8-15 字小红书风」），但 `storyline_capabilities/generate_script.py` 只取 `group_scripts`、把 title 丢了——标题定制只需「接住」而非「新增」；
- `WorkflowState` 已声明 `hook_title: NotRequired[Optional[str]]`（`state.py:232`），只是全仓无人写入——写上即通，无需改 schema、无需动 SCHEMA_VERSION；
- `jy_common/hook_title.py:99` `pick_hook_title` 优先读 `state["hook_title"]`——显式标题链路现成，node_09b 主逻辑可零改动。

### 3.2 改造 A: 新模块 `jy_common/user_directives.py`（纯函数，零依赖）

- `parse_user_directives(text) -> dict[str, str]`: 扫描【主题/开场/检索/标题】标记，取值到下一个【或文末，strip；无标记返回 `{}`;
- `opening_boost_requested(text) -> bool`:
  - ①【开场】栏: 含「转场」且含强度词 → True；否定词守卫（不要/无需/别/禁止/不用）→ False;
  - ②无标记时自由文本兜底: 按句号/分号/叹号/换行切句，同一句内同时出现（开头|开场|片头）+「转场」+强度词且无否定 → True;
  - 纯确定性、可单测、fail-closed（匹配不到恒 False）;
- `resolve_template_title(text) -> str | None`: 【标题】值 strip、截断到 `HOOK_TITLE_MAX_LEN`(12, `jy_common/hook_title.py:20`)。

### 3.3 改造 B: 检索词接入

- `nodes/storyline/_common.py` `build_web_topic_query`(:470-508): 解析指令后，在手动 query（`storyline_targets.query`，创建台恒空，冻结契约不动）之后插入【检索】（优先）或【主题】，沿用现有 `WEB_TOPIC_QUERY_PART_MAX_CHARS` 截断与去重拼接；画面概述首句、分组主题顺延后移；
- 同步更新函数 docstring（组件顺序变化）。

### 3.4 改造 C: 开场转场加强

- `nodes/node_09_inject_fx.py` :424 `semantics = _boundary_semantics_plan(state)` 之后:

```python
semantics = _boundary_semantics_plan(state)
if opening_boost_requested(extract_user_request(state)) and semantics:
    semantics = list(semantics)
    semantics[0] = "whip"
    status_log.append("node_09_inject_fx_opening_boost")
```

- `whip` → `pool_whip`（映射表 `templates/fx_template.json:21`），池内成员仍走既有 sha256 确定性轮换（重放幂等不破坏）；
- zh/en 双草稿共用同一 `semantics` 列表 → **双语一致**（既有约束 §4.9 天然满足）；
- 与 all-cut 保底（:295-300）天然兼容: 边界 0 = whip 后不再判「全切」，保底不触发、不冲突；
- `extract_user_request` 复用 `nodes/storyline/_common.py:274`（node_09 与 storyline 子图同层可导入）。

### 3.5 改造 D: 标题定制

- `storyline_capabilities/generate_script.py` :101-108: schema.properties 增 `"title": {"type": "string"}`；从 LLM 返回 obj 提取 title，随返回 dict 带出 `"hook_title": <原值>`（清洗放节点层）;
- `nodes/storyline/node_generate_script.py`: state 更新中 `ht = str(result.get("hook_title") or "").strip()[:HOOK_TITLE_MAX_LEN]`，**非空才写** `state["hook_title"]`（fail-closed: LLM 没给就不写，回退链不变）;
- `nodes/node_09b_inject_opening_hook.py` :310（仅 ZH 分支）: `title = resolve_template_title(user_request) or pick_hook_title(state, first_text)`——**【标题】 > LLM title > 第 1 条字幕**，三级优先链一行完成;
- `prompts/tasks/generate_script/zh/system.md` + `en/system.md`: title 字数「8-15 字」→「8-12 字」（与屏显上限对齐，减少截断尴尬）。

### 3.6 改造 E: 前端提示

- `web/console.html:157` textarea 的 placeholder 换成模板骨架（4 行示例），强刷即生效，无需重启。

### 3.7 兼容与守卫

- 无边增删 → `workflow.json` 不需重生成；SCHEMA_VERSION 不动（`hook_title` 早已声明 NotRequired，老 checkpoint resume 缺键走回退）;
- 存量任务/旧文本（无标记）行为逐字节不变；唯一例外:【开场】自由文本兜底命中时边界 0 换池——纯增量;
- 已知测试坑预案: node_09 确定性子进程测试在剪映开着时环境性失败（勿当回归）；node_09b 双闸测试、node_09 :157/:396 顺序敏感特效白名单不动。

### 3.8 测试与验收

**单测**:

- 新增 `test_user_directives.py`: 解析（四栏/部分栏/无标记）、boost（标记命中/否定词/自由文本兜底/无标记恒 False）、标题截断;
- `build_web_topic_query` 用例扩展: 【检索】优先于【主题】优先于概述首句;
- node_09: boost 开/关时边界 0 池断言（whip 池 vs 原语义）;
- generate_script: title 提取 + 节点非空才写;
- node_09b: 三级标题优先级（【标题】 > LLM title > 字幕）。

**基线对照**: 全量 unit 当前口径 2427/1F（环境性）；integration 失败用 stash 对照法逐字定性。

**真机验收**（操作员，重启服务后）:

1. 新建任务，按 §2.1 模板填写 →
   - `storyline/search_web_topic.json` 的 `query` 含【检索】词（注意: DDG 改版失败是环境性，验收点以 **query 字段**为准；引擎可用性由 Google+SOCKS5 切换计划解决，待其实施后可整链验收）;
   - `status_log` 含 `node_09_inject_fx_opening_boost`，草稿边界 0 转场来自 pool_whip;
   - 开场大字 = 【标题】原文;
2. 同文本去掉全部【】重跑 → 行为与旧版一致（回归确认）。

### 3.9 明确不做

- 卡点/时间轴指令化（#4 已是全自动能力，无用户可调语义）;
- web_topic 的 DDG 改版失败修复（Bing 切换计划另行执行）;
- 自由文本的 LLM 意图抽取（模板标记 + 少量正则已够，避免引入额外 LLM 调用与不确定性）。

---

## 四、关键代码证据索引

| 主题 | 位置 |
|---|---|
| 前端文本框 / 提交 | `web/console.html:157` / `:756` |
| 后端接收 / 落 state | `web/console_service.py:112` / `web/console_state.py:83-92` |
| 统一读取入口 | `nodes/storyline/_common.py:274-289` `extract_user_request` |
| 转场语义映射表 | `templates/fx_template.json:14-23`（fade/zoom/slide/whip/creative → 五池，cut→null） |
| 池内 sha256 轮换 | `nodes/node_09_inject_fx.py:110-117` |
| all-cut 保底 | `nodes/node_09_inject_fx.py:295-300` |
| node_09 调用点 | `nodes/node_09_inject_fx.py:424` |
| 开场钩子样式写死 | `nodes/node_09b_inject_opening_hook.py:70-72` |
| 标题三级来源 | `jy_common/hook_title.py:99-125` `pick_hook_title` |
| hook_title 键声明 | `state.py:232` |
| LLM title 字段（已被丢弃） | `prompts/tasks/generate_script/zh/system.md` 输出格式段 / `storyline_capabilities/generate_script.py:101-161` |
| 检索词自动构造 | `nodes/storyline/_common.py:470-508` `build_web_topic_query` |
| web_topic 开关 | `config.py:52-53` |
| 实跑产物 | `outputs/console-b0b2a64a78c6/`（initial_state.json / storyline/search_web_topic.json / storyline/script.json） |

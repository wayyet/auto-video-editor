# 转场/特效直接使用剪映官方资源库（含 VIP）—— 分析与实施计划

> 日期：2026-11-17
> 触发：操作员在剪映 5.9.0.11632 中打开 console-553e3e7b34f7 草稿，发现转场只有"叠化 0.5s"、画面特效为零；提出三点要求：①参考剪映转场/特效面板截图；②制作草稿时直接使用剪映里的转场和特效；③当前剪映已是 VIP 会员，VIP 转场/特效可直接使用；④据此直接修改工作流节点。
> 状态：**计划已定稿，实施未开始**。

---

## 1. 现状与全链路分析

### 1.1 现象

生成的剪映草稿每个剪辑边界都是"叠化 0.5s"（免费转场），视频特效（画面特效）为零。

### 1.2 转场/特效 → 草稿的完整链路（探明结论）

```
storyline_recommend_transition 节点
    │  写 state.storyline_transition_plan = {items:[{group_id, transition_in, transition_out}], default, method}
    │  （nodes/storyline/node_recommend_transition.py → storyline_capabilities/recommend_transition.py）
    ▼
storyline_plan_timeline_pro（storyline_capabilities/plan_timeline_pro.py:121-133）
    │  把 per-group 的 transition_in/out 原样盖到每个 clip 上（自由字符串，无白名单校验）
    ▼
node_05 generate_draft（mapper → storyline_to_native 原生 45 桶）
    ▼
node_09_inject_fx（nodes/node_09_inject_fx.py:293）
    │  _boundary_semantics_plan(:71-100)：逐边界取 clip.transition_out（缺省取下一 clip 的 transition_in）
    │  _resolve_transition_meta(:103-127)：语义 → fx_template.json transition_semantic_map
    │      fade/fade_in/fade_out/dissolve/crossfade → "叠化"；cut/none/hard_cut → null（硬切不写）
    │      未收录语义 → style_tag=default 兜底（闪屏故障/横向滑动/翻转冲屏）
    │  → TemplateLibrary.pick_transition_by_name（jy_common/template_library.py:170）
    │  → jy_common/native_fx.build_native_transition_material(:50)
    │  → materials.transitions[] + 后一段 extra_material_refs 绑定（id=transition-N）
    │  视频特效：fx_template.json video_effects=[] → 恒 degraded 不写入
    ▼
draft_content.json（zh + en 双草稿）
```

### 1.3 根因（两层）

**根因①：转场永远只有"叠化"——推荐节点渲染了错误的提示词任务。**

`storyline_capabilities/recommend_transition.py:43-44` 渲染的是 `generate_ai_transition` 提示词任务（`prompts/tasks/generate_ai_transition/zh/system.md`）——那是**给 MiniMax 首尾帧图生视频写英文 prompt 的模板**（"Smooth continuous single shot..."），输出散文、要求"直接输出英文 prompt 不附带解释"。而 `recommend_transition` 期望 LLM 返回 `{items, default}` JSON 并按索引对齐 groups。两者完全不匹配 → `chat_json` 解析失败 → 进 `except` 分支 `obj={}` → 全部降级为默认值 `transition_in="fade_in"` / `transition_out="fade_out"`（recommend_transition.py:79-83）→ node_09 语义映射把 fade 系全映射成"叠化"。

**这是一个存量 bug，不是 LLM 能力问题。**

**根因②：视频特效为零——2026-11-11"抖动模糊"事故后被主动清空。**

`templates/fx_template.json:40-41`：`video_effects: []`，注释明确记录——原候选"抖动模糊"(0.3)/"胶片暖棕"均为全片挂载型特效，"抖动模糊"实测把成片染成全画面模糊（outputs/console-64da836e2f3f/baseline/ab_test_result.md）；清空后 node_09 走降级路径不注入特效；**恢复前必须先在剪映实机验证候选特效（且禁止全片模糊类），再逐个加回**。

### 1.4 资源侧现状：无需任何新逆向，全量真实 ID 已在库

| 资源 | 位置 | 规模 |
|---|---|---|
| 转场库 | `templates/fx_resource_library.json` transitions 段 | **433 个 = 130 免费 + 303 VIP**，每行 `{name, is_vip, resource_id(19位), effect_id, md5, default_duration_s, is_overlap}` |
| 画面特效库 | 同文件 video_effects 段 | **462 个 VIP**（免费的 635 个场景特效未 dump，在 vendor 元数据中可补） |
| 元数据源头 | `vendor/pyJianYingDraft/metadata/transition_meta.py`（876 行）、`video_scene_effect.py`（8191 行） | 由 `scripts/build_resource_library.py` AST dump 生成 |
| 语义映射 | `templates/fx_template.json:12-22` | fade 系→叠化；cut 系→null |
| style_tag 兜底 | `templates/fx_template.json:23-39` | 闪屏故障/横向滑动/翻转冲屏 |

**截图面板里的 10 个转场名已逐一核对全部在库**（注意带空格）：叠化(:193)、三屏滑入(:1219)、中心切开(:1246)、前后对比(:1399)、推近 II(:2002)、穿越 III(:3154)、横移模糊(:2533)、水墨(:2551)、相片切换(:3019)、顺时针三角(:3793)。

### 1.5 已实证的落稿通道（本任务全部复用，不新造）

- 转场材质构造：`jy_common/native_fx.py:50` `build_native_transition_material`（截图已证明该通道在真实剪映中生效——草稿里的叠化正常显示、参数面板 0.5s 可调）。
- 特效材质构造：`jy_common/native_fx.py:76` `build_native_video_effect_material`（apply_target_type=0/enable_mask=True 等字段"待实机复核"）。
- 绑定通道：segment `extra_material_refs`（node_09 `_bind_ref`:183），转场挂后一段、特效挂所在段。
- 幂等守卫：我方 id 模式 `transition-\d+` / `vfx-\d+`，重放剥离旧 refs + 清理未续用材质；人工在剪映加的 uuid 材质不受影响。
- 原子写+审计：`nodes/_draft_io.apply_and_write` + `.autovideo_draft_mutations.jsonl`。
- QA 闸门：`draft_ops/draft_integrity.py` required_features（transitions 可勾选、video_effects 恒不 gate）+ 19 位兜底检查 + `nodes/node_join_before_delivery.py` 孤儿检测；修复路由 `nodes/node_delivery_qa_repair.py:58-63`（transitions→inject_fx）。
- AI 转场链（MiniMax 生视频 clip，GAT/PTAT）**默认关闭**（授权门禁+Provider 未配置），与本任务互不影响，不动。

---

## 2. 已确认的设计决策（操作员拍板）

| 决策点 | 结论 |
|---|---|
| 转场选取机制 | **内容感知 + 风格池轮换**：修复推荐提示词（根因①），LLM 按内容从受控词表选风格档，每档对应一个真实转场池（含 VIP），边界间确定性轮换；LLM 失败自动回退池轮换，不会跑飞 |
| 特效恢复策略 | **克制点缀**：白名单只收氛围光效类（禁模糊/抖动/频闪/故障/变焦类），只挂部分段落（首尾段+中间每第 3 段），避免全片特效重演 |
| VIP 资源 | 直接使用（剪映客户端已是 VIP 会员），不做 is_vip 过滤 |
| AI 转场链 | 不动（默认关闭，与原生转场通道互不干扰） |

---

## 3. 实施计划（改动清单）

### 3.1 新增提示词 `prompts/tasks/recommend_transition/{zh,en}/system.md + user.md`

修复根因①：给 recommend_transition 建自己的提示词任务（当前错误复用 `generate_ai_transition`）。

- `system.md`：角色=剪辑师；输入 groups 主题列表；输出 JSON `{"items":[{"group_id","transition_in","transition_out"}],"default"}`；**transition_in/out 只能取受控词表**：`cut / fade / zoom / slide / whip / creative`（cut=硬切），逐个给出风格定义（柔化/推近拉远/滑动位移/动感甩切/创意花式/硬切）。
- `user.md`：`{{user_request}}` + `{{groups}}` + `{{bgm}}` 占位符（与现调用参数对齐，见 recommend_transition.py:48-54）。
- zh/en 都建（render_prompt 缺文件返回空串，空提示词会导致 LLM 输出垃圾；见 `storyline_capabilities/prompts/__init__.py:45-51` missing_file="empty" 语义）。

### 3.2 `storyline_capabilities/recommend_transition.py`

- `render_prompt("recommend_transition", ...)` 替换错误的任务名（:43-44）。
- 解析后**校验**每个 transition_in/out ∈ 受控词表；未知值 → 回退 `fade`；`default` 同样校验。
- 降级路径（LLM 失败 → fade_in/fade_out 默认，:79-83）保留——旧值在语义映射中已有键，向后兼容存量 checkpoint/计划。

### 3.3 `templates/fx_template.json` 重构

- `transition_semantic_map`：值从"资源名"改为"池 key"：
  - `fade/fade_in/fade_out/dissolve/crossfade → pool_gentle`
  - 新增 `zoom→pool_zoom`、`slide→pool_slide`、`whip→pool_whip`、`creative→pool_creative`
  - `cut/none/hard_cut → null`（硬切）不变
- 新键 `transition_pools`（值=真实转场名数组，全部来自资源库 transitions 段，**实施时逐一核对存在性**）。种子：

| 池 | 成员（全部已在库核对，行号见 §1.4） |
|---|---|
| pool_gentle（柔化） | 叠化、水墨、相片切换、前后对比、融化 |
| pool_zoom（推近拉远） | 推近 II、推近推远、中心切开 |
| pool_slide（滑动位移） | 三屏滑入、横向滑动、上移、下移 |
| pool_whip（动感甩切） | 穿越 III、横移模糊、顺时针三角、闪屏故障 |
| pool_creative（创意花式） | 翻转冲屏、课本转场、负片下滑、赛博马赛克 |

- `video_effects` 恢复非空：从资源库 **video_effects 段**选 4-6 个氛围/光效/颗粒类白名单（种子候选：金色光斑、金沙；实施时用脚本扫段逐一核对并确认类别），**明确排除模糊/抖动/频闪/故障/变焦类**；每行 `{name, style_tag:"default", intensity}`；更新 `_video_effects_disabled` 注释为新政策+禁令（保留 2026-11-11 事故记录）。
- 现有 `transitions` style_tag 数组（闪屏故障/横向滑动/翻转冲屏）保留，作为未收录语义的最终兜底。

### 3.4 `jy_common/template_library.py` 扩展

- 新方法：`transition_pool(pool_key) -> list[str]`（读 transition_pools）、`video_effect_candidates() -> list[dict]`（读 video_effects 白名单行）。

### 3.5 `nodes/node_09_inject_fx.py`（核心节点改动）

- `_resolve_transition_meta`（:103-127）：语义 → semantic_map → 池 key → transition_pools → **确定性选取**：`pool[(boundary_index + stable_seed) % len(pool)]`，`stable_seed = int(md5(draft_tag + 相邻 clip id).hexdigest()[:8], 16)`。**禁用 python `hash()`**——str 盐化随机，跨进程不稳定，会破坏重放 byte-identical 幂等（现有测试基线）。选中名 → `pick_transition_by_name` → 19 位校验 → ok；池缺失/名解析失败 → degraded；映射 null → cut；未收录语义 → 现行 style_tag default 兜底不变。
- `_resolve_video_effect_meta`（:130-144）→ `_resolve_video_effect_candidates` 返回白名单列表；`_inject_fx_into`（:259-275）特效循环改为**克制点缀规则**：只对第 1 段、最后 1 段、中间每第 3 段（k%3==1 的中间段）挂特效；特效名按挂载序号在白名单内轮换；白名单为空 → 现行为（degraded 不写）。
- 幂等清理（`_strip_stale_owned_refs`:147 / `_remove_owned_materials`:167，kept-ids 机制）不变，天然兼容"部分段不挂"。中英双草稿双写（`dual_draft_targets`）、时长钳制（:243-249）、`extra_material_refs` 绑定通道全部不变。

### 3.6 测试

- `tests/unit/test_node_09_inject_fx.py`：更新 fade→叠化 断言为池轮换断言；新增：池轮换确定性（同输入两次运行 byte-identical 保持）、未知语义回退 default、cut 不写、特效只挂规则段+轮换、池名资源缺失 degraded。
- recommend_transition 对应单测（grep `recommend_transition` 定位现有测试文件）：词表校验、未知值回退、LLM 失败降级、新任务名渲染（不再读 generate_ai_transition）。
- `tests/fixtures/fx_template_stub.json`：加 transition_pools + video_effects 白名单行（名字与 `tests/fixtures/fx_resource_library_fixture.json` 对齐）。

### 3.7 文档与提交纪律

- 本文档入库。
- 提交**只含本任务文件**。⚠️ 工作区另有英文分支任务的未提交改动（`draft_ops/draft_integrity.py`、`nodes/_dual_draft.py`、`nodes/node_08_add_subtitles.py`、`nodes/node_12_human_add_bgm.py`、`nodes/node_13_adjust_volume.py`、`nodes/node_fork_english_branch.py`、`state.py`、`tests/unit/test_draft_integrity.py`）——**不得混入本次提交**。

---

## 4. 验证方案

1. **单测**：`.venv/Scripts/python.exe -m pytest tests/unit/test_node_09_inject_fx.py tests/unit/test_native_fx.py <recommend_transition 测试文件> -q`（以退出码为准）；末尾全量 unit 基线（当前 2124 全绿）零新增失败；integration 3 存量 F 用基线 worktree 对照确认非新增。
2. **真机验收（操作员）**：新建任务跑全链 → 打开草稿核对：
   - ① 多边界出现不同转场、含 VIP（如水墨/中心切开），在剪映转场面板可正常选中/改参数；
   - ② 特效只出现在规则段落（首/尾/中间每第 3 段），无全片模糊/抖动；
   - ③ 交付卡 transitions 关卡通过（勾选 require_transitions 的任务）；
   - ④ VIP 资源首次打开能联网下载（截图面板缩略图有下载角标属正常）。

---

## 5. 风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| 老 resource_id 下架 | pyJianYingDraft 元数据 dump 于 2026-09-19，个别 ID 可能在当前剪映版本下架 → 剪映不识别该条转场 | 只影响该边界显示，不崩链（材质构造/绑定通道已实证）；真机验收核对池内全部名字，不可见的剔除 |
| video_effect 材质字段待实机复核 | apply_target_type/enable_mask 等字段此前无实机样本 | 本次恢复注入后由真机验收定案；白名单克制（4-6 个）控制验证面 |
| 全片模糊事故重演 | 2026-11-11"抖动模糊"全片挂载 | 白名单禁模糊/抖动/频闪/故障/变焦类 + 克制点缀只挂部分段 |
| FFmpeg 导出路径转场为硬切近似 | draft_ops/draft_exporter.py 渲染链不含原生转场 | 不在本任务范围（操作员在剪映内查看/导出） |
| LLM 词表漂移 | 模型输出词表外值 | recommend_transition 硬校验 + 回退 fade；node_09 未收录语义仍有 default 兜底 |

---

## 附录：本次探明的关键文件索引

| 文件 | 角色 |
|---|---|
| `nodes/node_09_inject_fx.py` | 转场+特效注入主节点（本任务核心改动） |
| `storyline_capabilities/recommend_transition.py` | 转场语义推荐（根因①所在，:43-44 错误任务名） |
| `storyline_capabilities/plan_timeline_pro.py` | per-group 转场语义盖到 clip（:121-133） |
| `templates/fx_template.json` | 语义映射 + style_tag 索引 + video_effects 白名单（重构对象） |
| `templates/fx_resource_library.json` | 全量真实 ID 库（433 转场+462 VIP 特效，只读） |
| `jy_common/template_library.py` | 资源查询入口（扩展池方法） |
| `jy_common/native_fx.py` | 原生材质构造器（复用，:50/:76） |
| `prompts/tasks/generate_ai_transition/` | 被 recommend_transition 错误复用的 MiniMax 提示词（勿改） |
| `nodes/storyline/node_generate_ai_transition.py` | AI 转场链（MiniMax 生视频，默认关闭，不动） |
| `draft_ops/draft_integrity.py` / `nodes/node_join_before_delivery.py` | QA 闸门（transitions 可勾选、video_effects 恒可选，不改） |

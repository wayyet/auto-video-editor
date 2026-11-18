# 剪映 VIP 花字草稿审计与流水线花字缺失根因分析（修订版，2026-11-18）

> **修订性质：** 落实《auto-video-editor_剪映VIP花字文档正确性审查与分阶段执行计划_20261118.md》P0 阶段六项修正（P0-1~P0-6）。
> **原版文档：** [auto-video-editor_剪映VIP花字草稿审计与流水线花字缺失根因分析_20261118.md](auto-video-editor_剪映VIP花字草稿审计与流水线花字缺失根因分析_20261118.md)（保留作历史，其结论以本修订版为准）。
> **证据目录：** [花字审计证据_20261118/](花字审计证据_20261118/采集说明.md)（草稿副本 + SHA-256 清单 + rp.db 正规查询 + 缓存清单 + 时间线）。
> **代码基线：** `ea40d941b1824bf22b15b750f031841dad7ce485`（main；本轮 P4-1/P5 修复在工作区另行提交）。

---

## 0. 修订说明：对照审查六项修正

| 审查编号 | 原版问题 | 修订后表述 | 修订位置 |
|---|---|---|---|
| P0-1 | 本机草稿结论被写成已证实事实 | 改标「本机实测观察（操作者采集）」，附原始证据目录与 SHA-256 清单 | §3 |
| P0-2 | 审计日志「无花字痕迹」被当作排除证据 | 审计桶 `_AUDIT_BUCKETS` 原不含 `effects`，`changed_counts` 对花字增删**不可见**，日志只保留辅助证明力 | §3.5、§2.6 |
| P0-3 | 「账号有 VIP，应用/导出不受阻」 | `is_vip/paid_type` 只描述**资源授权类型**；截图 VIP 徽标是界面标识；当前账号订阅有效性与导出是否受阻**未验证** | §3.3、§3.6 |
| P0-4 | 「分层设计使然」暗示经过评审的主动决策 | 改为「**当前未实现**」：写入链无决策记录；仅 FFmpeg 导出链的保真度边界是 config.py 既有决策 | §4 |
| P0-5 | 「差异作为 fidelity 明细写进导出报告」被引为已实现 | 导出器原实现只统计转场/视频特效/贴纸，**不含** `materials.effects`/`material_animations`；该声明在本轮 P5 修复后才落地 | §2.5 |
| P0-6 | 「zh/en 传同一 job_id 即可一致」 | 当前 job_id 解析链有已定案缺陷（en 兜底恒为草稿库根名），须先实施公共 resolver（本轮已落地，见 §4.4） | §4.4 |

---

## 1. 结论摘要（修订版）

1. **当前 main 分支未实现剪映原生 VIP 花字**（`panel-text-flower` / `materials.effects` / `type=text_effect`）的资源收集、素材构造、片段绑定与 QA 校验——这是**实现范围缺失**，不是已验证的主动禁用策略。流水线默认不会向中英文草稿写入原生花字。
2. 原版记录的「中文草稿挂 1 个 VIP 花字『互动立体』、英文草稿零花字、资源授权类型为订阅制、UI 手动添加时间线」属于**本机实测观察**，本修订版已附原始证据（§3）；其中「当前账号可正常应用/导出」**降级为未验证项**。
3. FFmpeg 导出不渲染原生花字与剪映草稿能否挂载花字是**两个独立问题**；导出器在本轮 P5 修复后才把 `materials.effects` 纳入 fidelity 明细。
4. 花字池接入必须先通过 P1 Golden Sample（剪映 GUI 真实样本逆向），且 job_id 链路修复（本轮已落地）是其前置。

---

## 2. 仓库代码事实（本轮复核，含当日修复后状态）

### 2.1 文本写入层：普通字幕 ≠ 原生花字
`nodes/_subtitle_texts.py::make_text_material` 只写 `materials.texts`（content JSON + 排版 + 私有键），不产生任何 `text_effect` 引用。普通字幕样式与剪映「文本→花字」面板的原生花字素材是不同对象。

### 2.2 node_10 / node_10b：实现的是字幕入场动画
两入口共用 `apply_text_fx`，只写 `materials.material_animations`（`sticker_animation`）+ text 轨段 `extra_material_refs` 绑定。节点展示名与描述此前混称「花字」，**本轮已改**为「注入中文/英文字幕入场动画」（`monitoring/workflow_graph.py`）。

### 2.3 资源库：无花字池
`templates/fx_template.json`、`fx_resource_library.json`（转场/视频特效）、`text_resource_library.json`（intro/loop/outro 共 78/52/46 条 VIP 动画，见证据目录 `intro_vip_list.json`）均无花字段。storyline 的 `recommend_text` 只产出 font/size/color/position 排版字段。

### 2.4 vendor：无花字元数据模块（现象成立，因果修正）
`vendor/pyJianYingDraft/metadata/` 无 text_style/花字模块。但**这构不成写入阻塞**：项目已有 `jy_common/native_fx.py` 这类自定义原生素材构造器，花字构造器同样可以自写，vendor 模块缺失只是组织方式问题（审查 §1.4 因果修正，采纳）。

### 2.5 FFmpeg 导出器（本轮已补）
修复前 `build_render_plan` 只统计转场/视频特效/贴纸（`draft_ops/draft_exporter.py:212-221`）；本轮 P5 已加：`materials.effects` 中 `type=="text_effect"` 计数 → fidelity 行「N 个原生花字未渲染进 MP4(剪映客户端内可见)」。`material_animations` 仍未统计（动画随字幕 drawtext 保留文字本体，缺失仅损失入场动画，后续按需另补）。

### 2.6 审计桶（本轮已补）
`nodes/_draft_io.py::_AUDIT_BUCKETS` 原不含 `effects` → 审计日志对花字增删不可见；本轮已追加 `"effects"`。**注意**：修复只对**今后**的写入生效，历史审计日志（如 52373c6f9e11 的 9 条记录）不因此获得对花字的排除力——见 §3.5。

### 2.7 交付 QA：无 text_effect 功能类别
`draft_ops/draft_integrity.py::REQUIRED_FEATURE_KEYS` 含 `text_animation` 无 `text_effect`/`text_flower`（本轮不改；结构性花字校验按审查门禁留待 P1 之后）。通用悬空引用检查（`_material_ids` + `check_track_references`）对 `materials.effects` 条目**部分有效**（能查轨段引用悬空），但不校验花字字段结构/重复 ID/VIP 元数据。

---

## 3. 本机实测观察（操作者采集，附原始证据）

> 本节全部结论的原始依据在 [花字审计证据_20261118/](花字审计证据_20261118/采集说明.md)；SHA-256 清单见 `sha256_manifest.json`。采集时剪映运行中（3 进程），草稿为 08:06 快照。

### 3.1 中文草稿的花字观察
- `materials.effects` 含 **2 条** `type=text_effect` 条目（同 id `1D5E66AA-…`，`effect_id=7407340226189757733`、`name=互动立体`、`category_id=panel-text-flower`），对应 text 轨段 `extra_material_refs` 也挂 2 次——剪映 UI 保存形态（重复条目/重复引用的实际影响未验证，**不判无害也不判有害**，P1 样本须专门验证；流水线自身写入必须按 ID upsert + 引用去重）。
- 引用方：`text-seg-4`（`material_id=text-zh-4`，18.133333s 起 / 5.733333s 长）。其余 5 条我方文本（含标题）无花字。
- 证据：`zh_draft_flower_extract.json`（2 条完整条目 + 5 条轨段 refs）。

### 3.2 英文分支草稿
四种关键词（花字/panel-text-flower/type=text_effect/互动立体）**全部 0 命中**，`materials.effects` 桶为空，含 5 个 `text-anim-text-en-*` 动画 ref = 纯流水线产物。证据：`en_draft_zero_flower_check.json`。

### 3.3 花字资源的授权类型（语义限定版）
`rp.db` `http_cache` 表（Python sqlite3 只读正规查询）中，花字 item **自身**的 `business_info.json_str`：
```json
{"is_vip": true, "is_expired": false, "paid_type": "subscribe",
 "commercial_strategy": {"resource_export": {"paid_strategy_list": [{"paid_mode": "subscribe", "subscribe_package": "Pro"}]}}}
```
→ 该资源是**订阅制（Pro 套餐）授权类型**。⚠️ 三点限定（P0-3）：① `is_vip/paid_type` 描述**资源**授权策略，不证明**当前账号**订阅状态；② 截图右上角 VIP 徽标是界面标识，两者不互证；③「应用/导出实际不受阻」未验证（需真机导出证据，P1 手册含此项）。资源包 MD5 `c093f229a5b9529beab4d27c00bc0c4f` 与草稿引用缓存目录一致（`flower_cache_inventory.json`），确为同一资源。

### 3.4 归属判定：UI 手动添加（证据链修正版）
- **主证据**：代码库全库检索 `panel-text-flower`/`互动立体` 零命中（流水线无任何代码能产出该 effect_id）；花字资源包缓存目录创建于 07:07，晚于流水线最后写入 06:50:11（审计日志末条 mtime）；en 草稿 06:50:12 后 mtime/`tm_draft_modified` 均未再变。
- **辅助证据（证明力修正，P0-2）**：`.autovideo_draft_mutations.jsonl` 9 条记录的 `changed_counts` 只覆盖 `_AUDIT_BUCKETS` 原有 9 桶，**不含 effects**——「日志无花字痕迹」不构成排除证据，只能与 SHA-256 链一起证明「流水线各节点写入了哪些桶」。审计桶本轮已补 `effects`，今后恢复此证明力。
- 流水线侧排除仍然成立，故「UI 手动添加」判定维持。

### 3.5 未验证项（如实保留）
- 当前账号 Pro 订阅有效性与导出是否出现付费校验（P1 手册步骤 7 覆盖）。
- 重复 material/重复 refs 在剪映重开重存后的行为（P1 手册步骤 5）。
- 截图（选中字幕片段 + 花字面板显示已应用态）未采集，操作员可按 P1 手册补拍。

---

## 4. 根因（修订版）

### 4.1 核心表述（P0-4 修正版）
> 当前 main 分支**未实现**剪映原生 VIP 花字的资源收集、素材构造、片段绑定和 QA 校验，因此流水线默认不会自动向中英文草稿写入原生 VIP 花字。这是当前实现范围缺失，不表述成已验证的主动禁用策略。

**既有决策仅限导出侧**：`config.py:584-587`（2026-11-07 阶段 6）明确 FFmpeg 导出「贴纸/视频特效/花字样式不渲染——差异作为 fidelity 明细」；`config.py:645-646`（2026-07-04 用户决定）VIP 联网资源缓存禁删。写入侧无任何决策记录。

### 4.2 词汇混用（维持原结论）
代码中「花字」一词被三种对象共用：①`default_text_style()` 普通 style 占位（Week 3）；②node_10/10b 的**入场动画**（material_animations）；③剪映原生花字（未实现）。graph.py/state.py/monitoring 测试注释中的「花字」均指①②。显示名混称本轮已改（§2.2）。

### 4.3 zh/en 同因（维持）
node_10 与 node_10b 共用 `apply_text_fx` 核心，英文草稿由中文草稿 fork，故两边同样零花字——不是两条独立缺陷。

### 4.4 job_id 前置缺陷（P0-6，本轮已修复）
原版§3.5「zh/en 传同一 job_id 即一致」不成立：修复前 `thread_id` 是 LangGraph config 参数而非 state 键，`state.get("thread_id")` 恒空 → zh 兜底中文草稿目录名（`AutoVideo_console-…`）、en 兜底 `Path(draft_dir_en_branch).parent.name` = 草稿库根名 `com.lveditor.draft`（恒定，英文轮换失效），zh/en 动画因此不一致。**本轮已按《字幕VIP动画job_id链路_设计审查与修订执行计划_20261118》§4 落地**：`jy_common/job_identity.py::resolve_job_id`（config.thread_id 权威 → state.thread_id → state.session_id 旧 checkpoint 兼容 → 剥 `AutoVideo_` 前缀的中文草稿目录名兜底 → 显式 ValueError），node_09/node_10/node_10b 三入口统一接线。花字池接入直接复用该 resolver。

---

## 5. 接入落点（修订版）

### 5.1 硬门禁
P1 Golden Sample（剪映 GUI 真实样本逆向）通过之前，不接受按 `materials.effects + extra_material_refs` 直接写正式草稿的代码任务（审查 §6 门槛）。完整阶段规划见审查计划 §4（P1→P2→P3→P4-2→P5→P6）。

### 5.2 P1 操作员手册（Golden Sample 采集步骤）
1. **复制测试草稿**：在草稿库复制一份现有草稿（勿用生产草稿），重命名如 `AutoVideo_flower-golden-YYYYMMDD`。
2. **基线快照**：关闭剪映，备份其 `draft_content.json`/`draft_info.json` 并记 SHA-256。
3. **应用花字**：打开该测试草稿，选中**一个**测试字幕片段，在「文本→花字」面板应用**一个**已确认可用的花字（如「互动立体」），保存并**关闭剪映**。
4. **重开验证**：重新打开草稿，确认花字仍显示、动效正常；再次保存关闭。
5. **JSON diff**：对比步骤 2/4 两份 `draft_content.json`，记录：花字 material 落在哪个桶、`type` 值、`effect_id/resource_id/md5/path/source_platform` 等字段、绑定通道（是否 `extra_material_refs`）、重复条目/引用是否被剪映规范化。
6. **资源校验**：对照本地缓存目录文件 MD5（不只比较目录名里的 MD5 字符串）。
7. **降级行为采样**：分别记录资源缓存缺失、未登录、无订阅、断网时剪映的实际表现（弹窗/降级/静默）。
8. **导出验证**：导出一次，记录是否出现付费校验或资源缺失提示（同时验证 §3.3/§3.5 的未验证项）。
9. **交付物**：脱敏前后 JSON diff、重开实测结论、字段清单、缓存校验、降级行为记录、截图（选中字幕片段 + 花字面板已应用态）。

### 5.3 与审查计划的阶段映射
P0（本修订版+证据）✔ 已完成；P4-1（job_id）✔ 本轮落地；P5 可并行项（审计桶/导出 fidelity/显示名）✔ 本轮落地；P1 → 操作员按 §5.2 执行；P2/P3/P4-2/P5 余项/QA/事件展示 → P1 通过后立项。

---

## 6. 与审查结论的对照

| 审查判定（§0.2） | 修订版处理 |
|---|---|
| 代码已证实 3 项（字幕写入/动画非花字/无花字池） | 采纳，§2.1-2.3 |
| 高度支持但需全库复核（无专用写入逻辑） | 维持（本轮再次全库零命中） |
| 未独立验证 4 项（草稿观察/UI 手动/en 零花字/账号 VIP） | §3 附原始证据后升级为「操作者实测+证据」；账号订阅有效性仍为未验证项 |
| 因果错误（vendor 缺模块≠无法写入） | 采纳，§2.4 |
| 与当前代码不符（导出报告未统计花字） | 采纳，本轮 P5 已修复，§2.5 |
| job_id 现成缺陷 | 采纳，本轮 P4-1 已修复，§4.4 |

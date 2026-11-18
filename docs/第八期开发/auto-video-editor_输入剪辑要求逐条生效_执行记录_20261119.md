# 用户剪辑要求逐项生效：分阶段执行记录

- **执行日期**：2026-11-19
- **执行依据**：[设计审查与分阶段执行计划（修订版）](auto-video-editor_输入剪辑要求逐条生效_设计审查与分阶段执行计划_20261119.md)（吸收 P1-01~P1-07 / P2-01~P2-03 全部审查结论）
- **执行基线**：本地 `main` = `0f7c649`（比审查基线 `e67376a` 多 5 个 AI 内容明细提交，与本次目标文件无交集）；阶段 0 全量 unit 基线 **exit=0 零失败**（约 2520 例，`/tmp/baseline_unit_0f7c649.log`）
- **状态**：阶段 0-5 代码与自动测试全部完成；**剪映真机验收待操作员**（§6 清单）
- **关联**：分析文档《输入剪辑要求逐条生效_链路分析与实施计划_20261119》；并行会话《网页话题检索失败分析与Bing切换计划》（Google 切换，另案进行中）

---

## 0. 修订版审查结论落实对照

| 审查项 | 落实方式 |
|---|---|
| P1-01 边界0≠0-3秒 | 【开场】语义 = 第一处镜头切点（clip1→clip2）转场风格覆盖；文案与日志不承诺 0-3s |
| P1-02 whip≠最炸 | 全部日志/文档用词 =「强动势/甩镜风格（whip）」，无「最强」表述 |
| P1-03 不得依赖语义列表非空 | boost 传入 `_inject_fx_into()`，按真实草稿边界数生效；语义空/短先补 None 占位再覆盖边界 0；单镜头 not_applicable；资源解析失败不记 applied |
| P1-04 title 全链路 | Schema 声明 + 能力层提取 + 返回值携带 + 节点写入 `hook_title`，四处同时接通 |
| P1-05 显式标题不截断语义 | `pick_hook_title()` 一行未动；新标题在**写入端** `normalize_hook_title()` 规范化，截断记录 `title_truncated` |
| P1-06 删除自由文本兜底 | 解析器无兜底逻辑；无标记 = 严格旧行为（有专门测试锁定） |
| P1-07 Query 与联网分层 | 验收拆两层：query 字段正确为 Query 层通过；`status=ok` 才算搜索层通过 |
| P2-01 解析规则细化 | 行首标记、多行值、空值=未提供、重复字段 fail-closed+警告、未知标记不解释 |
| P2-02 平台名不代码级限制 | 解析器无平台名过滤；仅前端 placeholder 提示「勿写平台名」 |
| P2-03 卡点验收边界 | 本期不做卡点指令化（§7 未解决事项照录） |

---

## 1. 阶段 0：基线冻结

- 工作区：仅 4 个未入库文档（本会话+并行会话），代码零改动；`git rev-parse HEAD` = `0f7c649f9fc82426b04bc347ef97bf1ab29307ee`
- 定向基线（7 文件）：`test_hook_title / test_node_09_inject_fx / test_node_09b_inject_opening_hook / test_node_generate_script / tests/unit/nodes/test_node_search_web_topic / test_web_topic_reference / test_generate_script_template_injection` = **137 例全绿 exit=0**
- 全量基线：`tests/unit` **exit=0 零失败**（进度标记约 2520 例）
- 基线运行期间工作区只新增文件、不改既有文件，保证基线纯净

## 2. 阶段 1：指令解析器（新增）

**新增 `jy_common/user_directives.py`**（纯函数，零网络/文件/LangGraph 依赖）：

- `parse_user_directives(text)` → `{"theme","opening","search","title","warnings"}`；只识别四个**行首**标记【主题/开场/检索/标题】，值跨多行直到下一已知行首标记；空值=未提供（None）；同名重复 → 字段 fail-closed + `duplicate_directive:<field>` 警告；未知【xx】作普通文本；无标记 → 全空，**无自由文本兜底**（P1-06）
- `opening_boost_requested(directives)`：只看【开场】字段；须同时含范围词（开头/开场/片头/前3秒/前三秒）+「转场」+强动势词（炸/震撼/强烈/冲击/甩镜/炫酷）；否定词（不要/无需/不需要/别/禁止/不用）优先命中 → False
- `normalize_hook_title(text)` → `(标题, 是否截断)`：空白折叠+strip+截断 12 Unicode 字符（复用 `HOOK_TITLE_MAX_LEN`）；仅用于**新数据写入端**（P1-05）
- `resolve_template_title(directives)`：【标题】规范化值或 None

**新增 `tests/unit/test_user_directives.py`**：24 例全绿（全字段/部分/空值/多行/重复/无标记/未知标记/行首规则/否定优先/标题截断/纯函数确定性）。

## 3. 阶段 2：检索词接入

**修改 `nodes/storyline/_common.py::build_web_topic_query`**：组件顺序 = 手动 query（冻结契约位，仍不写）→ **【检索】→ 缺省回退【主题】** → 概述首句 → 分组主题前 3；沿用既有 `_collapse_ws`/PART_MAX 截断/去重/总长截断/空串跳过。`storyline_targets.query` 与 `console_state.py` 零改动。

**新增测试**（`tests/unit/nodes/test_node_search_web_topic.py` 追加 3 例）：【检索】优先于【主题】且有检索时不重复拼接；【检索】缺省回退【主题】、无标记时自由文本不进检索词；手动 query 仍最前。**3 例全绿**；wiring 回归（`test_wiring_routers` + `test_unreachable_nodes_wiring`）52 例全绿。

## 4. 阶段 3：开场转场加强

**修改 `nodes/node_09_inject_fx.py`**：

- 节点级：`opening_boost = opening_boost_requested(parse_user_directives(extract_user_request(state)))`；requested 记固定标签 `node_09_inject_fx_opening_boost_requested`（不含用户文本）
- `_inject_fx_into(..., opening_boost: bool = False)`：boost 在 `working_semantics` 初始化后、**all-cut 判定前**按真实边界数生效 —— 空列表/偏短先补 `None` 占位再 `working_semantics[0]="whip"`（P1-03）；单镜头 `opening_boost_not_applicable`；边界 0 实际写出才记 `opening_boost_applied`，池缺失 degraded 不假成功
- summary 新增诊断键：`opening_boost_requested/applied/not_applicable`、`opening_transition_pool/name`（进产物，不进监控事件）
- 状态标签三态（per draft）：`{zh|en}_opening_boost_applied` / `_not_applicable` / `_degraded`
- zh/en 共享同一 `opening_boost` 与 `job_id` → 双稿边界 0 转场名一致

**新增测试**（`test_node_09_inject_fx.py` 追加 9 例，全文件 36 例全绿）：真实库边界0∈POOL_WHIP+边界1保持原池；**空语义计划仍覆盖**（P1-03）；单镜头 not_applicable；否定不触发；无标记旧行为（含 161 字原文样张）；全硬切计划下覆盖后不触发 all-cut 保底；pool_whip 缺失 degraded 不假成功；zh/en 边界 0 同名；重放字节级幂等。

## 5. 阶段 4：标题链路

- `storyline_capabilities/generate_script.py`：schema.properties 增 `"title"`；`llm_title_raw = str(obj.get("title") or "").strip()`；返回值增 `"title"`（P1-04 四处接通之一~三）
- `nodes/storyline/node_generate_script.py`：标题链 `template_title = normalize(【标题】)` → `llm_title = normalize(result["title"])` → `resolved = template or llm`；**LLM 标题仅 `resolve_lang(state)=="zh"` 时采用**（英文标题不进中文标题轨）；`script.json` 落 `title / title_source(template|llm|subtitle_fallback) / title_truncated / user_directives_warnings`（诊断只进产物文件）；`resolved_title` 非空才写 `state["hook_title"]`（state.py:232 已声明，node_09b `pick_hook_title` 既有优先级自动消费）
- `prompts/tasks/generate_script/zh/system.md`：标题约束改「建议 8–12 个汉字，必须不超过 12 个字符（超长会被截断）」；en/system.md 不动（§6.6）
- `pick_hook_title()` 与 `node_09b` 零改动（§9.2 不改动清单）

**新增测试**（`test_node_generate_script.py` 追加 6 例，全文件 15 例 + 模板注入 6 例全绿）：LLM 标题写入 + 留痕；【标题】>LLM；超长截断+flag；两级全空不写键走字幕回退；英文运行跳过 LLM 标题但保留【标题】；重复【标题】fail-closed 回退 LLM + 诊断只进 script.json。

## 6. 阶段 5：前端提示与回归

- `web/console.html:157` placeholder 换为四栏模板示例（`&#10;` 换行；注明「四栏均可选、整行删掉=走自动、标题≤12字留空=AI自动起」）；不自动填充、不覆盖已输入内容；强刷即生效
- 旧 placeholder 无测试锁定；node_09b 显式标题已有 `test_explicit_state_hook_title_wins` 覆盖

## 7. 回归结果与并行会话冲突说明

- **定向回归**：本次全部改动/新增测试文件 + wiring = 全绿（阶段 1-5 各节已列，新增 42 例）
- **全量回归终值**：`tests/unit` **exit=0、0 失败、约 2587 例**（基线 2520 + 本次新增 42 + 并行会话同期新增；日志 `/tmp/final_unit2.log`）
- **golden 重录**：`zh/system.md` 标题字数修改触发 prompt golden 失效，已按惯例重录 `scripts/capture_prompt_golden.py`（50 份）；顺带收口一个**存量滞后**——`test_golden_manifest_shape` 断言 46 份而实库 50 份（此前新增模板未重录，旧 golden 只盖 46 份故未报），断言已改为与 golden 自描述一致（=50）
- **并行会话冲突（已消解）**：阶段 2 验证时同工作区存在并行会话的 Google 引擎切换半成品（provider 已切 `google_html`），曾致 3 个存量 duckduckgo 断言失败；其后续提交自行修复，最终全量回归零失败。本次全程未触碰其文件（`search_web_topic.py`/`config.py`/`requirements.txt` 等归属并行会话）

## 8. 改动文件清单

| 文件 | 操作 |
|---|---|
| `jy_common/user_directives.py` | 新增 |
| `tests/unit/test_user_directives.py` | 新增（24 例） |
| `nodes/storyline/_common.py` | 修改（query 组件接入） |
| `tests/unit/nodes/test_node_search_web_topic.py` | 追加 3 例 |
| `nodes/node_09_inject_fx.py` | 修改（boost 按真实边界数） |
| `tests/unit/test_node_09_inject_fx.py` | 追加 9 例 + POOL_WHIP 常量 |
| `storyline_capabilities/generate_script.py` | 修改（title Schema/提取/返回） |
| `nodes/storyline/node_generate_script.py` | 修改（标题链写入端） |
| `tests/unit/test_node_generate_script.py` | 追加 6 例 |
| `prompts/tasks/generate_script/zh/system.md` | 修改（标题字数） |
| `web/console.html` | 修改（placeholder） |

不改动清单（§9.2 全部遵守）：`console_state.py`、`state.py`、`graph.py`/`workflow.json`、`SCHEMA_VERSION`、`node_09b_inject_opening_hook.py`、`jy_common/hook_title.py::pick_hook_title()`、`storyline_targets.query` 契约。

## 9. 回滚方式

- 单一功能提交整体 revert 即可；无数据迁移、无拓扑重生成、无新必需 State 字段
- 解析器异常时最安全降级 = 返回空解析结果（旧行为），代码已按 fail-closed 实现

## 10. 真机验收清单（待操作员）

1. **重启服务**（restart-video-editor 一键脚本）使节点代码生效；前端强刷
2. 新建任务，输入框按模板填写（§5.1 修订版样例）→ 验收四产物：
   - `initial_state.json`：原始输入逐字保留
   - `storyline/search_web_topic.json`：`query` 以【检索】词开头（Query 层验收；`status=ok` 才算搜索层通过，失败须记录 provider/错误码——DDG→Google 切换另行验收）
   - `storyline/script.json`：`title`=【标题】原文、`title_source=template`
   - 中文草稿 `draft_content.json`：边界 0 转场资源 ∈ pool_whip 成员、绑定 `segments[1]`；`track-text-hook` 标题轨文本 = 【标题】原文、时长 ≤3s
   - `status_log`：`node_09_inject_fx_opening_boost_requested` + `..._zh_opening_boost_applied`；无 degraded 误报
3. 剪映打开草稿目检：开场转场为强动势风格、大字标题显示正常
4. **兼容回归**：同素材去掉全部【】标记重跑 → 边界 0 按原语义（无 requested/applied 标签）、标题走字幕切句、检索词走概述首句
5. （可选）英文输出语言任务：LLM 英文标题不得出现在中文草稿标题轨

## 11. 明确未混入本次的事项（照录修订版 §11）

Google+SOCKS5 Provider 切换（并行案）、自然语言通用意图解析、精确 0-3 秒转场、转场视觉强度标定、英文大字标题、卡点可控参数化。

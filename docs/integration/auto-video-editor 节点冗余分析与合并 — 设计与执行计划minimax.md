# auto-video-editor 节点冗余分析与合并 — 设计与执行计划

## Summary

**目标**：基于本地仓库 `E:\Documents\kuaishou\auto-video-editor` 的**实际代码**（非参考文档的二手结论），重新做一次节点冗余分析、合并真实存在的重复逻辑，并输出全部节点与工作流拓扑。

**范围**：只动 auto-video-editor 自身的 50 个 LangGraph 节点及其直接依赖的 `draft_ops/` / `nodes/storyline/` / `jy_common/` 工具层。不涉及 FireRed-OpenStoryline、video-agent-kit 等其他仓库。

**关键设计决策（4 条）**：

1. **参考文档的三条核心结论已被实际代码推翻**，本计划以代码为准重写结论，不沿用其"22 节点""删 `atomic_write_file`""join 被执行两次"的判断（详见第 1 节，含逐条证据行号）。
2. **先修 P0 阻塞 bug，再做合并**。核查发现 assembly QC 修复循环会无限自旋，导致 integration 基线是红的、整套跑要几十分钟。基线不绿就无法验证任何重构，所以循环修复独立成阶段 0、单独提交。
3. **零生产调用的遗留物只加注释标记，不删除**（用户决策）。4 处：`jy_common/draft_writer.py`、`node_16_translate_subtitles()` shim、`storyline_search_web_topic`、`storyline_generate_ai_transition`。删除有回归风险，标记零调用方是等价信息量下最安全的选择。
4. **不合并任何图节点**。逐节点核对后确认 50 个节点无一重复职责（每个操作草稿 JSON 的不同字段或不同产物）。可合并的只有工具函数层的 4 组重复，以及 4 个关卡节点的样板代码——后者用共享工厂收敛，节点数与图拓扑完全不变。

---

## 1. 核查结论：参考文档的三条结论已被推翻

参考文档 `docs/integration/auto-video-editor_节点冗余合并设计与执行计划.md`（git 未跟踪，本次核查前已存在于仓库）声明"本次未重新克隆仓库"，其结论与当前代码不符。

| 文档结论 | 实际情况 | 证据 |
|---|---|---|
| 全图 **22 个**图节点 | **50 个** `add_node`。文档遗漏 Phase 5 assembly QC 通道（6 个）与 Phase 4 auto-mode storyline 子图（21 个） | `graph.py` 共 50 处 `g.add_node(` |
| 冗余②：`join_before_delivery` 被接两条独立边导致**执行两次**，是待修 bug | **已修复**。已是 list 语法 fan-in，LangGraph 等齐两上游只触发一次；模块 docstring 已记录该修复 | `graph.py:617-620`；`graph.py:84-86` |
| 冗余①：`atomic_write_draft` 与 `atomic_write_file` "功能完全重复"，应删除后者 | **不成立**。两者数据类型不同：前者写 JSON 草稿（`content: dict`），后者写字节流（`data: bytes`），服务于 PNG 封面与 SRT 字幕。生产调用方是 `node_14`/`node_15`/`node_16`，删了会直接坏 | `draft_ops/atomic_writer.py:37`、`draft_ops/atomic_writer_file.py:18`；`nodes/node_14_make_covers.py:139`、`nodes/node_15_localize_covers_en.py:17`、`nodes/node_16_translate_subtitles.py:201,248` |

**文档中仍然成立的部分**：第 4 节"不建议合并的相似结构"两条判断正确——关卡节点不可合并、字幕三节点不可合并，只能抽共享 helper。本计划沿用该结论。

**文档中已过时的事实**：文档称"节点 5/7/8/9/10/11/13 七个节点调用 `atomic_write_draft`"，实际这 8 个节点（加节点 16）Week 5 已全部迁移到 `safe_write_draft` 双写入口（`nodes/node_08_add_subtitles.py:258`、`node_09_inject_fx.py:160`、`node_10_inject_text_fx.py:82`、`node_11_inject_sticker.py:127`、`node_13_adjust_volume.py:158`、`node_05_generate_draft.py:52`、`node_07_speed_fit.py:151`、`node_16_translate_subtitles.py:183`）。

---

## 2. 现状基线

### 2.1 图节点全清单（50 个）

| 组 | 节点名（`graph.py` 注册名） | 文件 | 接边状态 |
|---|---|---|---|
| 准备 | `clean_cache` | `nodes/node_01_clean_cache.py` | **孤立**（`graph.py:444` 注册，START 边在 `graph.py:518` 已剥离，改由 Web UI 端点触发，`tests/integration/test_no_autoclean_cache.py` 守护） |
| | `launch_openstoryline` | `node_02_launch_openstoryline.py` | START 入边 |
| | `open_preview` | `node_03_open_preview.py` | 线性 |
| | `checkpoint0_storyline_plan` | `node_checkpoint0_storyline_plan.py` | human 分支 |
| | `import_and_plan` | `node_04_import_and_plan.py` | 线性 |
| | `generate_draft` | `node_05_generate_draft.py` | 汇合点 |
| 关卡①+护栏 | `node_06_human_reorder` | `node_06_human_reorder.py` | `interrupt("①")` |
| | `node_07_speed_fit` | `node_07_speed_fit.py` | 条件边，3 次自重试 |
| | `escalate_guardrail_failure` | `graph.py:281` 内联 | → END |
| | `bridge_snapshot2` | `graph.py:293` 内联 | 唯一分叉点 |
| 中文主线 | `node_08_add_subtitles` / `node_09_inject_fx` / `node_10_inject_text_fx` / `node_11_inject_sticker` / `node_12_human_add_bgm` / `node_13_adjust_volume` | 对应文件 | 线性 8→13 |
| Phase 5 QC | `assembly_discover_and_probe` / `assembly_asr_and_visual_observe` / `assembly_build_timeline` / `assembly_validate_render_qc` / `assembly_repair_loop` / `assembly_write_report` | `nodes/assembly/` | 受 `ASSEMBLY_QC_GATE_ENABLED` 开关控制（默认 true） |
| 英文分支 | `fork_draft_for_english_branch` / `node_14_make_covers` / `node_15_localize_covers_en` / `node_16a_translate_and_check` / `node_checkpoint3_layout_review` / `node_17_inject_english_tts` / `join_before_delivery` | 对应文件 | fork/join 已正确 |
| Phase 4 auto | `storyline_load_media` … `storyline_join`（21 个） | `nodes/storyline/` | auto 分支；其中 `storyline_search_web_topic`（`graph.py:490`）、`storyline_generate_ai_transition`（`graph.py:494`）**零边**，`storyline_local_asr`（`graph.py:492`）因路由缺陷**不可达** |

### 2.2 测试基线（本次实测，非文档转述）

| 范围 | 结果 |
|---|---|
| `tests/unit` | **全绿**，exit=0，约 790 项，2 项 skip |
| `tests/integration` | **红**。抽样 3 文件 13 项 → 6 失败，其中 5 项 `GraphRecursionError: Recursion limit of 10007` |
| `tests/` 全套 | 跑到 ~96% 后 pytest 自身崩溃：`UnicodeDecodeError`（capture teardown 读到非 UTF-8 字节，来自 uvicorn 子进程的 GBK stderr） |

失败清单（抽样）：
```
test_no_autoclean_cache.py::test_graph_invoke_does_not_auto_trigger_clean_cache
test_week4_graph.py::test_week4_checkpoint3_normal_path
test_week4_graph.py::test_week4_zh_branch_no_rerun_after_en_resume
test_week4_graph.py::test_week4_replay_safety_translate_called_once
test_week4_graph.py::test_week4_branch_parallel_dispatch
test_week4_graph.py::test_week4_join_called_only_once
```

---

## 3. 问题清单

### 3.1 【P0 阻塞】assembly QC 修复循环无限自旋

**现象**：任何一次 `graph.invoke()` 只要走进 assembly QC 通道且 `assembly_timeline_path` 缺失，就会自旋到 LangGraph 递归上限（10007 superstep）才抛错。这就是 integration 测试慢到几十分钟的直接原因。

**根因（两处早退路径漏写计数器）**：

| 位置 | 问题 |
|---|---|
| `nodes/assembly/node_validate_render_qc.py:64-73` | `assembly_timeline_path` 缺失时早退，返回值只有 `error_log` + `status_log`。**既不设 `assembly_qc_status`，也不递增 `assembly_qc_retry_count`** |
| `nodes/assembly/node_repair_loop.py:272-280` | 同样条件下早退，返回值只有 `error_log` + `status_log`。**不递增 `assembly_qc_retry_count`** |

**死循环闭环**（`config.py:419` `ASSEMBLY_QC_MAX_RETRY` 默认 2）：

```
assembly_validate_render_qc 早退 → status=None, retry=N
  ↓ route_after_assembly_qc (node_validate_render_qc.py:42-56)
  status 不在 (pass, pass_with_warnings) 且 N < 2 → 返回 "assembly_repair_loop"
  ↓
assembly_repair_loop 早退 → retry 仍是 N
  ↓
assembly_validate_render_qc 早退 → 回到起点，retry 永远不增
```

`assembly_repair_loop_node:359` 的递增逻辑写在函数末尾，被 `:272` 的早退完全绕过。

**修复方向**：两处早退都必须消耗一次重试预算——`node_repair_loop_node:272` 早退时返回 `"assembly_qc_retry_count": retry + 1`；`assembly_validate_render_qc_node:64` 早退时同时设 `"assembly_qc_status": "escalated"` 并递增计数。这样最多 2 轮即落到 `assembly_write_report` 软降级，符合 `config.py:417-418` 注释声明的"重试到上限后不硬性中断"设计。

### 3.2 真实冗余（工具函数层，可合并）

| 编号 | 冗余 | 位置 | 重复内容 |
|---|---|---|---|
| R1 | 草稿读盘 helper | `node_07_speed_fit.py:34`、`node_08_add_subtitles.py:86`、`node_09_inject_fx.py:29`、`node_10_inject_text_fx.py:40`、`node_11_inject_sticker.py:25`、`node_13_adjust_volume.py:56` | 6 份逐字相同的 `def _load_draft(path): return json.loads(path.read_text(encoding="utf-8"))` |
| R2 | 写回样板 | `node_08:257-258`、`node_09:159-160`、`node_10:81-82`、`node_11:126-127` | 4 份相同的 `write_result = safe_write_draft(draft_path.parent, draft)` + `jianying_running` 告警 log 拼接。`node_13:157-158` 做了同样的写入但**漏掉告警 log**，是 5 处行为不一致 |
| R3 | storyline 读盘三函数 | `nodes/storyline/node_plan_timeline_pro.py:36,49,56` 与 `node_plan_timeline_ai_transition.py:37,50,57` | `_read_json_artifact` / `_read_groups` / `_read_media` 整块逐字复制 2 份。归属地 `nodes/storyline/_common.py` 已存在且已被 21 个 storyline 文件 import |
| R4 | 关卡节点样板 | `node_checkpoint0_storyline_plan.py:20,33,38`、`node_06_human_reorder.py:31,49,63,70`、`node_12_human_add_bgm.py:22,27,41,47` | 3 处同构的 `_build_interrupt_payload` → `interrupt()` → `_post_resume` 三段式。`node_checkpoint3_layout_review.py:30` 是第 4 个变体（多一步 SRT 重写） |

R1/R2/R3 均**无测试直接覆盖**（`grep _read_json_artifact|_read_groups|_read_media tests/` 无命中），抽取风险低。R4 有关卡行为的测试覆盖，需保证 payload 字段逐字不变。

### 3.3 零生产调用遗留物（按用户决策：只加注释标记，不删）

| 位置 | 事实 | 标记方式 |
|---|---|---|
| `jy_common/draft_writer.py:22,41` | 纯 re-export 壳（`atomic_write_draft = _atomic_write_draft`）。生产代码零调用；唯一消费者是 `tests/unit/test_draft_writer_compat.py`。而 `tests/integration/test_e2e_tc07_audio_fades.py:149` 反而**反向断言不再走它** | 模块 docstring 加"零生产调用方，仅为历史计划文档命名兼容保留" |
| `nodes/node_16_translate_subtitles.py:257` | `node_16_translate_subtitles()` 兼容 shim。`graph.py` 未注册该函数（只注册 `node_16a_translate_and_check` + `node_checkpoint3_layout_review`），仅 `tests/unit/test_node_16_translate_subtitles.py` 调用。**注意：本文件的纯函数被 16a 与 checkpoint3 复用，文件本身不可删** | 函数 docstring 加"未注册进图，仅单测使用" |
| `graph.py:490` `storyline_search_web_topic` | `add_node` 注册但**零条边**引用（`node_search_web_topic.py` 50 行） | `graph.py` 注释标注"已注册未连通" |
| `graph.py:494` `storyline_generate_ai_transition` | `add_node` 注册但**零条边**引用（`node_generate_ai_transition.py` 92 行） | 同上 |

另有一处**不可达的活代码**（非遗留物，是缺陷）：`graph.py:250-261` 的 `_storyline_route_after_split_shots` 两个分支返回同一值 `"storyline_understand_clips"`，导致 `storyline_local_asr` 永不可达、其下游 `storyline_speech_rough_cut`（`graph.py:655`）传递性不可达。docstring 写明"阶段 1 才完整检测"，属已知占位。本次只加注释说明当前不可达，**不改行为**（改行为属于功能变更，超出本计划范围）。

### 3.4 确认不合并的（沿用参考文档判断，仍然成立）

| 相似结构 | 不合并理由 | 本次可做 |
|---|---|---|
| 4 个关卡节点（⓪①②③） | 分别卡在流程第 4/6/12/16 步，LangGraph 无"一个节点挂起两次"用法 | 抽共享工厂（见 R4），图拓扑不变 |
| `node_09` / `node_10` / `node_11` | 分别改 `materials` 的三个不同数组（转场/字幕样式/贴纸），服务三个不同创意目的 | 抽共享"读-改-写"helper（见 R1/R2），节点数不变 |

---

## 4. 目标架构

**图拓扑零变化**。50 个 `add_node`、全部 `add_edge` / `add_conditional_edges` 在阶段 1-3 中保持不变。唯一的行为变更来自阶段 0 的循环修复（把"无限自旋"变成"2 轮后软降级"）。

```
graph.py (拓扑不动)
  ├─ human 模式：launch → preview → ⓪ → import → generate_draft
  │                                      └→ [assembly QC 6 节点] → ① → ⑦ ⇄ escalate
  │                                                                        ↓ 达标
  │                                        bridge_snapshot2 ─┬─ 中文主线 08→13→14→15 ─┐
  │                                                          └─ fork → 16a → ③?→17 ─────┤
  │                                                                                    ▼
  │                                                              join_before_delivery（fan-in 单次）
  └─ auto 模式：launch → preview → storyline_load_media → ... 21 节点 ... → storyline_join → generate_draft

工具层（本次收敛点）
  draft_ops/atomic_writer.py     atomic_write_draft(dict) / atomic_write_draft_pair / safe_write_draft
  draft_ops/atomic_writer_file.py atomic_write_file(bytes)   ← 保留，职责不同
  nodes/_draft_io.py（新增）      load_draft / apply_and_write   ← 收敛 R1 + R2
  nodes/_checkpoint.py（新增）    build_interrupt_node 工厂       ← 收敛 R4
  nodes/storyline/_common.py      追加 read_json_artifact / read_groups / read_media ← 收敛 R3
```

---

## 5. 分阶段执行步骤

每阶段一个独立 commit，阶段 1-3 之间无耦合，可单独回滚。

### 阶段 0：修 P0 无限循环（阻塞项，必须先做）

**目标**：让 `graph.invoke()` 在 QC 通道缺输入时 2 轮内软降级退出，恢复 integration 基线可跑。

- 步骤 0.1：`nodes/assembly/node_repair_loop.py:272-280`，早退返回值追加 `"assembly_qc_retry_count": int(state.get("assembly_qc_retry_count") or 0) + 1`，与 `:359` 的正常路径语义一致。
- 步骤 0.2：`nodes/assembly/node_validate_render_qc.py:64-73`，早退返回值追加 `"assembly_qc_status": "escalated"` 与 `"assembly_qc_retry_count": int(state.get("assembly_qc_retry_count") or 0) + 1`。设 `escalated` 而非 `pass`，保证走 repair→write_report 的软降级路径而非误判达标。
- 步骤 0.3：新增回归测试，断言"缺 `assembly_timeline_path` 时 `route_after_assembly_qc` 在 2 轮内返回 `assembly_write_report`"，不依赖真实 ffmpeg/ASR。
- 步骤 0.4：重跑 `tests/integration/test_no_autoclean_cache.py` 与 `test_week4_graph.py`，确认 6 个失败全部转绿且**单文件耗时从数分钟降到秒级**（自旋消失的直接证据）。
- 步骤 0.5：跑 `tests/` 全套，记录新的通过/失败数。若仍触发 pytest 的 `UnicodeDecodeError`，单独记为遗留项（uvicorn 子进程 GBK stderr 污染 capture），不阻塞本计划。

**回滚**：单文件单函数改动，`git revert` 即可，无数据迁移。

### 阶段 1：收敛 storyline 读盘三函数（R3，最低风险先行）

- 步骤 1.1：在 `nodes/storyline/_common.py` 新增 `_read_json_artifact(state, key)` / `_read_groups(state)` / `_read_media(state)`，实现从两个副本文件逐字搬入，加入 `__all__`。
- 步骤 1.2：`node_plan_timeline_pro.py` 与 `node_plan_timeline_ai_transition.py` 删除本地定义，改为 `from nodes.storyline._common import ...`。
- 步骤 1.3：跑 `tests/unit/test_storyline_capabilities_*.py` 全绿（该组 3 文件已存在）。

**为何先做这步**：这两文件已 import `_common`，改动面最小，且 `_read_json_artifact` 无测试覆盖 = 行为等价性容易验证。

### 阶段 2：收敛草稿读写样板（R1 + R2）

- 步骤 2.1：新增 `nodes/_draft_io.py`，提供两个函数：
  - `load_draft(draft_file: Path) -> dict` — 承接 6 份 `_load_draft`
  - `apply_and_write(state, draft_path, draft, node_tag, *, duration_us=None) -> list[str]` — 承接 4 份写回样板，统一返回追加进 `status_log` 的 tag 列表
- 步骤 2.2：6 个节点改用 `load_draft`（`node_07/08/09/10/11/13`），删除各自 `_load_draft` 定义。
- 步骤 2.3：4 个节点改用 `apply_and_write`（`node_08/09/10/11`）。
- 步骤 2.4：**修 R2 的不一致**——`node_13_adjust_volume.py:157-158` 补上 `jianying_running` 告警 log，与其余 4 个节点对齐。此为行为修正，需在 commit message 显式说明。
- 步骤 2.5：跑 `tests/unit/test_node_08/09/10/11/13*.py` 全绿；`test_node_10_inject_text_fx.py:118` 已断言 `safe_write_draft` 写到 `draft_dir/draft_content.json`，是抽取后的关键回归点。

**风险点**：`apply_and_write` 必须保持 `safe_write_draft` 的入参形态 `draft_path.parent`（不是 `draft_path`），Week 5 迁移时专门从文件级提升到目录级以启用双写。抽错会让 `draft_info.json` 缺失。

### 阶段 3：收敛关卡节点样板（R4）

- 步骤 3.1：新增 `nodes/_checkpoint.py`，提供 `build_interrupt_node(*, checkpoint, legacy_id, step, instructions, extra_payload, notifier, notified_field, log_tag)` 工厂，返回符合 LangGraph 节点签名的可调用对象。
- 步骤 3.2：`node_checkpoint0_storyline_plan.py`、`node_06_human_reorder.py`、`node_12_human_add_bgm.py` 改为调用工厂，保留各自的 `_build_interrupt_payload` / `_post_resume` 薄封装以维持 `patch("nodes.node_06_human_reorder.interrupt")` 这类测试打桩路径可用。
- 步骤 3.3：`node_checkpoint3_layout_review.py` **不改**——它在 resume 后多一步 SRT 重写，结构已偏离样板，强行套工厂会降低可读性。在 docstring 注明"有意不纳入工厂"。
- 步骤 3.4：跑 `tests/unit/test_node_06_human_reorder.py`、`test_node_12_human_add_bgm.py`、`test_node_checkpoint0_storyline_plan.py`、`test_node_checkpoint3_layout_review.py` 全绿；再跑 `tests/integration/test_interrupt_resume.py`（该文件显式测关卡行为，conftest 的 autouse fixture 对它不做旁路）。

**风险点**：payload 字段必须逐字不变——`checkpoint`（`⓪`/`①`/`②`）、`legacy_id`、`step` 三者被 `resume_utils.py::resume_all_pending` 用于自动匹配 resume，改字段会静默破坏多关卡恢复。

### 阶段 4：文档同步与遗留物标记

- 步骤 4.1：按第 1 节结论**重写** `docs/integration/auto-video-editor_节点冗余合并设计与执行计划.md` 的第 1、2、3.1、3.2 节，删除已失效的"22 节点""删 `atomic_write_file`""join 执行两次"三处表述，换成 50 节点清单与本次的 4 组真实冗余。
- 步骤 4.2：按 3.3 节表格给 4 处零调用物加注释标记（`jy_common/draft_writer.py`、`node_16_translate_subtitles.py:257`、`graph.py:490`、`graph.py:494`）。
- 步骤 4.3：给 `graph.py:250-261` 加注释说明 `storyline_local_asr` / `storyline_speech_rough_cut` 当前不可达及原因（路由两分支同值），明确"阶段 1 补检测"的后续归属。
- 步骤 4.4：用第 2.1 节的全清单重画工作流图（mermaid），替换文档中已过时的 22 节点图。

---

## 6. 测试与验收标准

### 6.1 验收项

| # | 验收标准 | 验证方式 |
|---|---|---|
| A1 | 缺 `assembly_timeline_path` 时 `route_after_assembly_qc` 在 `ASSEMBLY_QC_MAX_RETRY` 轮内返回 `assembly_write_report` | 新增回归测试，桩掉能力层，不依赖 ffmpeg |
| A2 | `test_no_autoclean_cache.py` + `test_week4_graph.py` 13 项全绿 | `pytest tests/integration/test_no_autoclean_cache.py tests/integration/test_week4_graph.py` |
| A3 | 上述两个文件单次运行耗时回到秒级 | 计时对比（阶段 0 前后各记一次） |
| A4 | `_read_json_artifact` / `_read_groups` / `_read_media` 在全仓库仅剩 1 份定义 | `grep` 计数 |
| A5 | `_load_draft` 在 `nodes/` 下仅剩 1 份定义 | `grep` 计数 |
| A6 | 6 个节点写回后 `draft_content.json` 与 `draft_info.json` 均存在且内容一致 | 复用 `test_node_10_inject_text_fx.py:118` 断言 + 新增双写一致性断言 |
| A7 | `node_13` 剪映进程告警 log 与其余 4 节点一致 | 新增/修改 `test_node_13_adjust_volume*.py` 断言 |
| A8 | 4 个关卡节点的 `interrupt` payload 逐字不变（`checkpoint`/`legacy_id`/`step`） | 现有 4 个关卡单测 + `test_interrupt_resume.py` + `test_resume_all_pending.py` |
| A9 | `tests/unit` 全绿，无新增失败 | `pytest tests/unit` exit=0（当前基线：全绿） |
| A10 | 50 个 `add_node` 与全部连线在阶段 0-3 后保持不变 | `git diff graph.py` 只含注释改动；`add_node` 计数仍为 50 |
| A11 | 4 处零调用物已加注释标记且未删除 | `git diff --stat` 显示这些文件只增注释行 |

### 6.2 每阶段必跑

```powershell
cd E:\Documents\kuaishou\auto-video-editor
.\.venv\Scripts\python.exe -m pytest tests/unit -q --no-header -p no:cacheprovider
```

阶段 0 额外跑（当前会失败，修复后应全绿）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_no_autoclean_cache.py tests/integration/test_week4_graph.py -q --no-header -p no:cacheprovider
```

**环境注意**：PowerShell 控制台非 UTF-8，跑 pytest 前设 `$env:PYTHONIOENCODING='utf-8'`，否则 capture teardown 会因 uvicorn 子进程的 GBK stderr 抛 `UnicodeDecodeError`（本次核查已实测复现）。

---

## 7. 风险与回滚

| 风险 | 影响 | 应对 | 回滚粒度 |
|---|---|---|---|
| 阶段 0 改错重试语义 | QC 不达标时不再重试，直接软降级，掩盖真实问题 | A1 回归测试锁死"2 轮内必达 write_report 且中间确实重试过" | 单 commit revert |
| 阶段 2 抽错 `safe_write_draft` 入参（`draft_path` vs `draft_path.parent`） | `draft_info.json` 缺失，剪映 5.9+ 打开异常 | A6 双写一致性断言 | 单 commit revert |
| 阶段 3 改 payload 字段 | `resume_all_pending` 静默匹配不到，多关卡自动恢复失效 | A8 逐字比对 + 保留薄封装维持测试打桩路径 | 单 commit revert |
| 阶段 1 触碰 storyline 能力链路 | auto 模式回归 | A9 + 现有 storyline 单测 | 单 commit revert |
| 阶段 4 改文档时误改代码 | 混入行为变更 | 步骤 4.1-4.3 限定只改 `.md` 与注释行，A10/A11 校验 | 单 commit revert |

阶段之间无依赖，任一阶段失败可单独回滚而不影响其他阶段成果。

---

## 8. 未决问题与已知遗留

| 项 | 状态 | 说明 |
|---|---|---|
| integration 全套能否全绿 | **未验证** | 本次只抽样跑了 3 个文件。阶段 0 修完循环后需跑一次全套确认剩余失败是否与本计划无关 |
| pytest `UnicodeDecodeError` 崩溃 | **已知遗留，不在本计划范围** | 根因是 uvicorn 子进程写 GBK 字节到 stderr，pytest capture 强制按 UTF-8 解码。规避方式是设 `$env:PYTHONIOENCODING='utf-8'`，根治需给子进程 stdout/stderr 显式指定 encoding |
| `storyline_local_asr` / `storyline_speech_rough_cut` 不可达 | **只标记不改** | 属功能补全（阶段 1 补 `has_audio` 检测），超出"冗余合并"范围，留给 storyline 后续计划 |
| `ASSEMBLY_QC_GATE_ENABLED` 默认 true | 需产品确认 | `config.py:409-415` 注释写"用户已确认接受默认开启"，但每条视频强制过 6 节点 QC 的启动成本较高。本次不改 |
| 参考文档是否保留 | 待定 | 该文件当前 git 未跟踪。重写后是覆盖它还是新建 v2 文件，需用户确认；本计划默认**原地重写** |

---

## 9. 参考证据

- 本地仓库核查：commit `3b33fa7`（`main` 分支，工作区仅 1 个未跟踪文件即参考文档本身）
- 参考文档：`docs/integration/auto-video-editor_节点冗余合并设计与执行计划.md`
- 交叉参考：`docs/integration/剪映核心技能集成auto-video-editor开发计划.md:57,183`（`safe_write_draft` 迁移计划，Week 5 已完成）、`docs/integration/architecture_decision_record.md`（ADR-3 QC 开关、ADR-1 进程隔离）

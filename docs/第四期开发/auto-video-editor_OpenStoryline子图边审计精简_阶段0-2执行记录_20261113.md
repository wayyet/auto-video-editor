# OpenStoryline 子图边审计精简——阶段0-2 执行记录(2026-11-13)

> **对应计划:** 《auto-video-editor_OpenStoryline子图边审计与精简设计执行计划_20261113》(同目录,已于当日随 main 入库)
> **执行范围:** 仅计划 §8 的阶段0、阶段1、阶段2;阶段3(运行时回归)、阶段4(全量验收)、阶段5(文档同步)**未执行**,遗留清单见 §6。
> **分支:** `feature/storyline-edge-audit-p0-2`(基于 main `97b27a3`)
> **执行日期:** 2026-11-13(Asia/Singapore)

---

## 1. 用户决策记录(与计划默认项的差异)

| 决策点 | 计划默认 | 用户实际选择 | 影响 |
|---|---|---|---|
| 阶段1 走任务A 还是任务B | 任务A(屏障精简为 `GS→SB`) | **任务B:补配音消费,保留 `[GS, GV]→SB` 双源屏障** | 阶段1 从"改边"变为"实现消费";图拓扑**零改动** |
| 执行分支 | — | 基于最新 main 拉新分支 | 见 §2 基线说明 |

## 2. 基线说明(与计划文档基线的差异)

计划文档核验基线为 `a41e45e`;实际执行时 main 已推进到 `97b27a3`(并发会话完成):

- 第四期《剪映草稿缺失内容补全》6 提交(`8db90a0→e8f0580`)已合并进 main;
- 计划文档本体已入库(`97b27a3`),本分支无需再入库。

已核验 `a41e45e→97b27a3` 的 diff:改动集中在草稿尾链(node_09b 开场钩子)、前端契约与 QA 口径;**storyline 接线段、BGM 屏障、`node_select_bgm.py`、`select_bgm.py`、两个拓扑测试文件均未被触及** —— 计划文档的全部结论在新基线上仍然成立。

## 3. 阶段0 交付:冻结当前拓扑基线

**提交 `e310a7a`** `test(边审计·阶段0): 冻结 Storyline 拓扑基线——17节点/AI开·关两态全量边清单快照单测`

### 3.1 新增 `tests/unit/test_storyline_topology_baseline.py`(3 用例)

builder 直读口径(不 compile,与 `test_storyline_edge_governance_topology.py` 同源):

| 用例 | 冻结内容 |
|---|---|
| `test_storyline_node_inventory_frozen` | 恰好 17 个 `storyline_*` 节点注册(审计 §2.1 清单) |
| `test_ai_on_topology_baseline_frozen` | 普通边全集(START→LM、主链4边、GC→STR、WT→GS、GS→GV、GS→TXT、PTAT→QA);屏障 5 道(`[GS,GV]→SB`、`[GC,SB]→RTR`、六源→PTP、`[GC,RTR]→GAT`、`[PTP,GAT]→PTAT`);条件路由恰三源(STR/QA/JOIN)及其出口集合;START 唯一入口;storyline 段与外层无普通边直连 |
| `test_ai_off_topology_baseline_frozen` | QA 直等 PTP;屏障少 GAT/PTAT 两道;GAT/PTAT 无任何普通边/屏障/条件路由 |

### 3.2 如实记录的一项现状(未修正)

**AI 关档时 GAT/PTAT 仍被注册**(不接任何边):`graph.py` 的两个 `add_node` 在开关分支外(原 559/577 行区域),仅接线在 `if is_ai_transition_enabled():` 内。这是计划文档 §6.4 的已知"可选清理项"(图卫生优化,非运行时边错误),**未授权执行且与"冻结现状"口径冲突**,基线快照按现状冻结并在 docstring 注明。

### 3.3 注释纠偏

`graph.py` 两处 `auto 链 18 节点` → `17 节点`(render_video 已随 2026-11-12 边治理阶段5 删除),并补删除出处注释。

## 4. 阶段1 交付:任务B——BGM 真实消费配音时长

**提交 `07795a8`** `feat(BGM配音对齐·阶段1): select_bgm 真实消费配音时长——任务B 补读取,保留[GS,GV]→SB双源屏障`

审计确认的事实:`node_select_bgm.py` 代码体不读 `storyline_voiceover_artifact`(仅 docstring 提及"可选"),双源屏障的 GV 源是"预留等待"。任务B 把它变成真实数据依赖:

| 文件 | 改动 |
|---|---|
| `nodes/storyline/node_select_bgm.py` | 新增 `_read_voiceover_total_ms(state)`:聚合 artifact 各段 `duration`(跳过带 `error` 的失败段);节点体传 `voiceover_total_ms=` 给 `select_bgm`;docstring 输入清单更新为真实输入 |
| `storyline_capabilities/select_bgm.py` | 签名加 `voiceover_total_ms: int = 0`;`>0` 时对候选做**稳定重排**(时长覆盖配音的候选排前,同组内保持启发式名次),影响启发式 fallback(`candidates[0]`)与 LLM 候选顺序;输出 dict 增 `voiceover_total_ms` 键(空库兜底 `_empty_bg_selection` 同步,形状一致);模块 docstring 输出示例同步 |
| `graph.py`(750-753 区域) | 屏障注释更新:删除"当前代码体未读该字段"旧说明,改述任务B 真实消费 |

**容缺省纪律**(与 2026-11-10 配音/字幕解耦同款):TTS 整体失败时 `storyline_generate_voiceover` 不写 artifact、单段失败时该段 `duration=0` —— `_read_voiceover_total_ms` 在任何缺失/解析失败/结构非预期路径都返回 0,BGM 行为退化为任务B 之前完全一致,不陪葬。

**明确不动的:**

- 图拓扑零改动(`[GS, GV]→SB` 屏障原样保留);`SCHEMA_VERSION` 不 bump(schema 节点/边集无变化);
- PTP 六源屏障继续等待 GV(时间线对配音的消费不受影响)。

## 5. 阶段2 交付:配音消费契约锁定

**提交 `0236807`** `test(BGM配音对齐·阶段2): 配音消费契约锁定——manifest AST级清单+覆盖偏好单测`

| 文件 | 改动 |
|---|---|
| `tests/unit/test_artifact_producer_consumer_manifest.py` | `ARTIFACT_MANIFEST` 增 `storyline_voiceover_artifact`(producer=`node_generate_voiceover.py`;consumer=`node_plan_timeline_pro.py` + `node_select_bgm.py`);`test_manifest_covers_expected_keys` 同步。该键此前仅 docstring 提及、代码未读,正是 manifest 的 AST 剥 docstring 口径要拦的"注释提及≠真实消费"——消费点被删/改名将立刻红 |
| `tests/unit/test_storyline_capabilities_c_classes.py` | 增 2 用例:`test_select_bgm_prefers_track_covering_voiceover`(同 mood 同分下,配音总时长把长轨顶到 fallback 首位;对照不传时仍选短轨)+ `test_select_bgm_voiceover_zero_matches_default_behavior`(=0 与不传输出逐字段一致,存量行为零漂移) |

**拓扑/集成测试零改动**(`NEIGHBORS`、`GOLDEN_STEPS`、barriers、`test_workflow_graph_schema.py:246` 全部维持)——这正是任务B"保留双源屏障"的回归证明。

## 6. 验证结果

| 验证项 | 命令 | 结果 |
|---|---|---|
| 基线+治理+能力层目标测试 | `pytest tests/unit/test_storyline_topology_baseline.py tests/unit/test_storyline_edge_governance_topology.py tests/unit/test_storyline_capabilities_c_classes.py -q` | 全绿 |
| manifest(含新条目参数化) | `pytest tests/unit/test_artifact_producer_consumer_manifest.py -q` | 全绿 |
| 集成三件 | `pytest tests/integration/test_unreachable_nodes_wiring.py tests/integration/test_workflow_graph_schema.py tests/integration/test_graph_compiles.py -q` | 64 例全绿 |
| **全量 unit** | `pytest tests/unit -q --junitxml=…` | **1910 例 / 0 失败 / 0 错误 / 3 存量 skip**(30s.mp4 fixture 缺失、Windows font 路径、E 盘旧路径 skip —— 均为已知存量,非本次引入) |
| PG 容器 | `docker start edb-postgres16` | healthy(集成前置) |

## 7. 遗留清单(未执行阶段,供后续会话接续)

### 7.1 阶段3:运行时回归(计划 §8 阶段3 场景表)

任务B 改变的是**选曲偏好**,不是调度;计划文档 §8 阶段3 的 10 个场景表仍适用,其中"BGM 调整后"一行应按任务B 口径改写为:`SB 每轮只运行一次;配音 artifact 缺失时退化为旧行为;PTP 仍等待 GV`。

### 7.2 阶段4:全量交付验收(计划 §8 阶段4 四条命令)

本轮已跑全量 unit + 集成三件;计划还列了 `test_workflow_console_frontend.py` 等前端契约面,留待阶段4 一并跑。

### 7.3 阶段5:文档与运行态说明同步

- `web/node_map/workflow.json` 静态快照**本就过时**(仍含已删除的 `render_video`;`select_bgm.dependencies` 只写 `generate_script` 未含 voiceover;`plan_timeline.dependencies` 只有四源)——本次不动,阶段5 处理时建议整体重生成而非局部补丁;
- 计划 §11"可选优化"(AI 关档时把 GAT/PTAT 的 `add_node` 移入开关分支)仍未执行,阶段0 基线快照已按现状冻结,执行时需同步改 `test_ai_off_topology_baseline_frozen` 的注册断言。

### 7.4 操作员可选观察项(非必验收)

真机跑通一次 auto 链后,可查看 `storyline_bgm_selection` 的 `voiceover_total_ms` 字段:FireRed TTS 正常时应等于各段配音时长之和;`TTS_BACKEND` 未配 firered 的 stub 路径下是静音 wav 的估算时长,同样非 0。该字段仅为选曲偏好输入,不影响交付流程。

## 8. 提交清单

| 提交 | 阶段 | 内容 |
|---|---|---|
| `e310a7a` | 阶段0 | 拓扑基线快照单测 + graph 节点计数注释纠偏 |
| `07795a8` | 阶段1 | 任务B 配音消费实现(节点 + 能力层 + 屏障注释) |
| `0236807` | 阶段2 | manifest 契约锁定 + 能力层 2 用例 |
| (本次) | docs | 本执行记录入库 |

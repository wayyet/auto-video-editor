# LangGraph 依赖约简与图重构——执行记录(2026-11-14)

> **对应计划:** 《auto-video-editor-langgraph-dependency-audit-and-plan.md》(同目录,第5期)
> **执行范围:** 计划 §10 阶段 0/1/2/4/5/6;**阶段3 跳过**(用户决策,见 §1);阶段顺序按 0→1→2→4→5→6 执行。
> **基线:** `main` @ `6f2d559`(2026-11-13 节点地图 TB 布局交付后)
> **执行日期:** 2026-11-14(Asia/Singapore)
> **交付提交:** `a9c0288`(阶段1)→ `0709a42`(阶段2)→ `ae5e92c`(阶段4)→ `156daad`(阶段5)→ 本记录(阶段6);未推送(推送走 sync-github)。

---

## 1. 用户决策记录(与计划默认项的差异)

| 决策点 | 计划默认 | 用户实际选择 | 影响 |
|---|---|---|---|
| 阶段1 四组删边 | —(计划推荐 P1) | **执行四组删边** | 明确推翻 2026-11-13 边审计「任务B 保留 `[GS,GV]→SB` 双源屏障」的结案(冲突已如实呈现)。本次约简保留的是 GV 源(配音时长消费不丢),删的是被 GV 传递保证的 GS 源 —— 方向与旧任务A(精简为 GS→SB)相反,与任务B 的配音消费成果兼容 |
| 执行范围 | 计划自述阶段4/5 可选 | **全量阶段 0-6** | 阶段4(bridge_snapshot2 重构)与阶段5(检索并行化)一并执行 |
| 阶段3 空narration排查 | 独立任务 | **跳过** | 根因(代码与 prompt 模板键名错位)已于 2026-11-10 修复;文档截图报错是 fail-fast 拦截机制正常工作的表现;进一步排查需新的真实失败 thread 数据 |

## 2. 拓扑对照(改造前 → 改造后)

| 关系 | 改造前(6f2d559) | 改造后(156daad) | 约简依据 |
|---|---|---|---|
| select_bgm 入边 | `[GS, GV]` 双源屏障 | `GV→SB` 单源普通边 | GV 已等 GS(计划 §4.1);任务B 配音消费保留 |
| recommend_transition 入边 | `[GC, SB]` 双源屏障 | `SB→RTR` 单源普通边 | SB 祖先链含 GC(§4.2) |
| plan_timeline_pro 入边 | 六源屏障 `[GC,GS,GV,SB,RTR,TXT]` | 两源屏障 `[RTR, TXT]` | RTR 链覆盖 GC/GS/GV/SB(§4.3);TXT 单独保证文字样式 |
| generate_ai_transition 入边 | `[GC, RTR]` 双源屏障(AI 开) | `RTR→GAT` 单源普通边 | RTR 传递保证 groups(§4.4) |
| PTAT 入边 | `[PTP, GAT]` 双源屏障 | **不变** | 两份输入来自独立分支,不能约简 |
| qa_gate 入边 | 最终时间线单源 | **不变** | — |
| generate_script 入边 | STR 条件路由(检索开+有词走 WT,否则直达) | `[STR, WT]` 双源屏障 | 阶段5 并行化(§9):GC fan-out 双出边,STR/WT 同超步 |
| search_web_topic 语义 | 条件触发节点(开关开且有检索词才执行) | **每轮必跑** + 内部软跳过(开关关/无检索词 → skipped 标签,零网络请求、零产物) | 每轮必跑才可进入必需屏障(不踩 D10) |
| bridge_snapshot2 桥接节点 | node_07 条件边达标 → bridge(写 snapshot2_path)→ node_08 | **已删除**:快照②生产移入 speed_fit 节点本体放行路径,直写 `state.snapshot2_path`,条件边直连 node_08 | 阶段4(§7.2):路由函数纯化 |

单元素列表屏障会被 LangGraph 折叠成普通边 —— 约简后的 SB/RTR/GAT 以普通边形式存在(唯一入边,每轮恰触发一次,无重复触发竞态)。

## 3. 各阶段交付

### 阶段0:改造前基线锁定(无代码改动)

- HEAD `6f2d559`;最小回归集 7 文件(baseline/governance/draft_path/wiring_routers/wiring/schema/compiles)90 例全绿;
- 全量 unit exit 0(1922 例 0F/3 存量 skip,junitxml 确认);
- AI 开/关、web topic 开/关、QA retry 轨迹由既有测试覆盖(wiring :316-1265),验证全绿,未新建。

### 阶段1:四组 fan-in 传递性约简(`a9c0288`)

- `graph.py`:4 组边替换 + 各屏障注释改写为传递性保证链(防未来加回冗余源)+ 模块 docstring/GC 出路注释同步;
- `monitoring/workflow_graph.py`:SCHEMA_VERSION 6→7(拓扑变更;无测试硬编码值);
- 测试重录(结构级集合断言全集,运行时轨迹断言零改动存活):
  - `test_storyline_topology_baseline.py`:普通边基线 +`(_GV,_SB)`/`(_SB,_RTR)`、屏障基线改 `[(RTR,TXT)→PTP]`、ai_on 补 `(RTR,GAT)` 普通边、屏障仅剩 PTP/PTAT;
  - `test_storyline_edge_governance_topology.py::test_storyline_barrier_shapes_locked`:同步;
  - `test_unreachable_nodes_wiring.py`:NEIGHBORS 4 行(SB/RTR/PTP/GAT 入边)、`test_ai_pair_…`(ins[PTP]={RTR,RT}、ins[GAT]={RTR})、AI off 屏障集、`test_ai_nodes_…`(with_ai 只剩 PTAT);
  - `test_draft_path_degradation.py::test_plan_timeline_pro_fan_in_excludes_split_shots`:required 源列表改 `[RTR,TXT]`(SS 排除断言保留);
- `web/node_map/workflow.json`:8 处同步(4 个屏障目标 deps + 上游 next_nodes 镜像 + meta 描述);
- 验证:最小回归集 + 全量 unit **1922/0F/0E/3S**;黄金轨迹形状零变更;`ptp==rtr+1` 实测存活(超步语义保证 RTR 晚于 TXT)。

### 阶段2:运行时契约补齐(`0709a42`)

- `test_unreachable_nodes_wiring.py` defaults 用例:新增 **TXT 逐轮先于 PTP** 时序断言(PTP 两源屏障不得在文字样式写入前触发)+ 约简后单源边措辞同步(次数/时序断言全数保留:SB==GV+1、RTR==SB+1、gen==ptp==rtr+1、各节点每轮恰 1 次)。

### 阶段3:跳过

见 §1 用户决策;`node_generate_script.py` 的 `script_llm_trace.json` 落盘 + 空 narration fail-fast 已在 2026-11-10 交付 QA 复盘修复中实现(本会话 grep 核实存在)。

### 阶段4:bridge_snapshot2 结构性重构(`ae5e92c`)

- `nodes/node_07_speed_fit.py`:
  - 新增 `_produce_snapshot2(draft_path)` helper(mkdir+copytree+返回快照路径);
  - 节点本体两个放行路径(未改动已达标 / 压缩后达标)产快照并写 `snapshot2_path`;
  - **写失败防御**:`write_draft` 返回 ok=False 时不产快照、落 `node_07_speed_fit_write_failed` 标签 → 路由按"草稿仍超时长"走 retry/escalate(原 route 重判草稿的防御语义等价保留);
  - `route_after_speed_fit` 纯路由化:删 copytree 副作用,保留无 draft 放行 + load_draft 达标判断(纯读);
  - 删 `get_snapshot2_path` helper(删桥接后无调用方);
- `graph.py`:删 bridge_snapshot2 函数/注册/边;node_07 条件边 mapping `"node_08_add_subtitles"` 直连;模块 docstring 流程图同步;
- 测试:`test_draft_path_degradation.py` 删 helper 用例;`test_node_07_speed_fit.py` 新增 3 用例(放行产快照×2 + 写失败不产快照走 retry);`test_sequential_integration.py` 注释同步;
- node_08/fork_english_branch 消费 `state["snapshot2_path"]` 的读法不变(带 draft_path fallback),零改动;
- 验证:全量 unit **1924/0F/3S**(净 +2:+3 新用例 -1 helper 用例);sequential/reorder 存量失败 stash 逐字对照一致。

### 阶段5:检索并行化(`156daad`)

- `nodes/storyline/node_search_web_topic.py`:节点改**每轮必跑** —— 入口判 `STORYLINE_ENABLE_WEB_TOPIC`(运行时读)与 `build_web_topic_query(state)`,关闭/为空 → `SKIPPED_STATUS_TAG="storyline_search_web_topic_skipped"` 标签直接返回,不发网络请求、不写产物(GS 消费软降级零改动,与既有硬异常降级同款纪律);
- `graph.py`:删 `_storyline_route_after_template_rec` 与 STR 条件边、WT→GS 独立边;加 `GC→WT` 普通边 + `[STR, WT]→GS` 双源屏障;模块 docstring/D10 注释/注册区注释同步;**SCHEMA_VERSION 7→8**;
- 测试重录:
  - `test_wiring_routers.py`:三态路由矩阵改写为节点级 skipped/执行矩阵(3 用例:flag off 零网络、query 空零网络、正常执行写产物)+ builder 一致性用例保留;
  - baseline:普通边基线 `(_WT,_GS)`→`(_GC,_WT)`、屏障基线 +`(frozenset({STR,WT}),GS)`、条件路由源 `{STR,QA,JOIN}`→`{QA,JOIN}`;
  - governance:STR 用例(无条件路由、屏障源身份)、WT 用例(入边 GC、屏障源、无普通出边)、barrier shapes +GS 屏障、D10 断言语义更新(STR/WT 合法为屏障源);
  - wiring:`WIRED_NODES` 移除 WT(语义=开关控制节点)、`CONDITIONAL_NODES` 置空(set(),⚠️ 空花括号是 dict 的坑)、NEIGHBORS STR/WT 行、**GOLDEN_STEPS 轨迹变更**(`[STR]`→`[STR, WT]` 同超步;web=False 时 WT 也在轨迹中)、defaults 相邻断言(并行同超步 + WT 每轮次数)、flag off/无检索词两用例改「执行但内部 skipped」;
- `web/node_map/workflow.json`:STR/WT/GS deps/next 镜像同步(GC.next=[STR,WT]、WT.deps=[GC]、STR/WT.next=[GS]、GS.deps=[STR,WT]);
- 验证:全量 unit **1924/0F/3S**;wiring 38 例全绿(新轨迹实测);schema/compiles/readonly/flag_defaults 78 例全绿。

## 4. 阶段6:完整回归与归档

| 验证项 | 命令/方式 | 结果 |
|---|---|---|
| PG 容器 | `docker start edb-postgres16` | healthy |
| 全量 unit(阶段5 终态) | `pytest tests/unit -q --junitxml` | **1924 / 0F / 0E / 3 存量 skip** |
| 全量 integration | `pytest tests/integration -q --junitxml` | **142 / 3F / 0E / 3 skip** —— 3F 逐一核实恰为记忆基线 3 存量(auto_e2e/reorder/sequential「字幕应被注入」类;reorder 经 stash 对照逐字一致),**零新增失败** |
| AI 开/关 | wiring `test_ai_*` 系列既有用例(AI 开=默认档,关=`test_ai_flag_off_…` 等) | 全绿 |
| assembly QC 开/关 | unit/integration 既有 flag 用例(assembly gate 默认开;`test_flag_defaults_aligned` 锁默认值) | 全绿 |
| checkpoint roundtrip | wiring `test_checkpoint_roundtrip_…` | 全绿 |
| 存量失败判定 | sequential :158「字幕应被注入」/ reorder :265「英文草稿 transitions 为空」等 —— stash 对照逐字一致 | 基线存量,非本次引入 |

> 存量失败基线(2026-11-13 记忆库):sequential+reorder+auto_e2e 的「字幕应被注入」类失败在基线 `6679e54` worktree 逐字一致 = 存量;判定新失败一律用 worktree/stash 对照法。

## 5. 遗留与操作员事项

- **真机 E2E**(非本次范围):auto 链全流程跑通后观察 —— ① `storyline_bgm_selection.voiceover_total_ms` 等于各段配音时长之和(FireRed TTS 正常时);② 节点地图(web/node_map)展示新拓扑(GV→SB→RTR→GAT 单源链 + PTP 两源 + GS 双源);③ 真机检索开关关闭时 web_topic 节点显示 skipped 标签、零网络调用。
- **服务重启**:SCHEMA_VERSION bump 后,监控页/创建台的图 schema 与布局缓存会在下次建图时自动失效重算,无需手工清理;但运行中的常驻服务需重启才会加载新 `graph.py`。
- **计划 §11.2 监控界面依赖语义展示优化**(区分必经前驱/条件前驱/屏障来源/传递上游)未执行 —— 本次交付的 workflow.json 已按 dependencies/next_nodes 双向表达;展示语义化属纯前端改进,留待后续计划单独决策。
- **计划 §2.2 提到的 README 测试基线数字(877 passed)严重过时** —— 本次未同步 README(超出范围);实际基线见 §4。
- 工作区中 `agent_gate/cli_client.py`、`agent_gate/tests/test_cli_client.py` 的既有改动非本次范围,未触碰、未提交。

## 6. 提交清单

| 提交 | 阶段 | 内容 |
|---|---|---|
| `a9c0288` | 阶段1 | 四组 fan-in 传递性约简 + SCHEMA_VERSION=7 + 拓扑基线重录(7 文件,+140/-127) |
| `0709a42` | 阶段2 | TXT→PTP 时序契约 + 单源边措辞同步(+19/-9) |
| `ae5e92c` | 阶段4 | bridge_snapshot2 删除 + 快照生产内聚 speed_fit + 写失败防御(+101/-46) |
| `156daad` | 阶段5 | web_topic 每轮必跑并行化 + [STR,WT]→GS 屏障 + SCHEMA_VERSION=8 + GOLDEN_STEPS 重录(8 文件,+199/-146) |
| (本次) | 阶段6 | 本执行记录 + 计划文档头部批注 |

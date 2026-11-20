# 关卡①改为仅 QC 降级时挂起 —— 执行记录

- **日期**:2026-11-20
- **状态**:✅ 已实施完毕并真机实证(本地已推送;遗留两项人工点验见 §7)
- **上游文档**:
  - 分析与计划:`auto-video-editor_关卡①分镜顺序调整改为仅QC降级挂起_分析与实施计划_20261120.md`
  - 审查修订版执行计划(权威):`auto-video-editor_关卡①仅QC降级挂起_设计审查与修订执行计划_20261120.md`(审查基线 6f44a41)

---

## 1. 实施概要

关卡①「分镜顺序调整 node_06_human_reorder」从**每轮无条件挂起**改为**条件挂起**:仅 assembly QC 降级(escalated,非结构性)时挂起人工复核(放行 true / reject 终止);QC 正常(pass / pass_with_warnings)与 QC 关闭自动通过;旧 checkpoint(无来源标记)完整保留旧的无条件挂起行为。

**不删节点、不改图拓扑**:接线保持 `generate_draft → node_06_human_reorder → node_07_speed_fit`;checkpoint 契约(`checkpoint="①"` / `legacy_id="checkpoint1_reorder"` / `step=6`)、`CHECKPOINT_MARKS=("①","②")`、关卡②、api_console 关卡③退休 409 守卫全部不动。

## 2. 阶段 0 定稿:状态判定矩阵(按序判定)

| # | 条件 | 行为 |
|---|---|---|
| 1 | `assembly_structural_error is True` | fail-closed raise(结构性错误绝不允许人工放行;上游正常路由本就 `assembly_failed → END`,此处兜底保护旧 checkpoint/异常状态) |
| 2 | `assembly_qc_gate_enabled is None`(旧 checkpoint) | 完整保留旧版行为:无条件 `interrupt()` + `_post_resume`(resume 重放时 `interrupt()` 直接返回已存恢复值;reject 仅 escalated 生效) |
| 3 | status ∈ {pass, pass_with_warnings} | 自动通过,`status_log += checkpoint1_auto_passed`,无通知、不冒充人工确认 |
| 4 | status == "escalated" | `interrupt()` 人工复核;放行 true → `checkpoint1_resumed`;reject → RuntimeError 终止 |
| 5 | status 缺失且标记 False(QC 关) | 自动通过,`status_log += checkpoint1_auto_passed_qc_disabled` |
| 6 | status 缺失且标记 True(QC 启用)/ 未知值 | fail-closed raise(异常状态不得静默放行) |

运行来源标记 `assembly_qc_gate_enabled`(state 新键):由 `storyline_load_media`(START 后第一个节点,QC 开/关两态必经)在建图进程内写 `bool(config.ASSEMBLY_QC_GATE_ENABLED)`,随 checkpoint 持久化 —— 恢复不依赖恢复进程当前 env。

**§6 分类核对记录(只记录不改行为)**:QC 节点两处早退(候选草稿映射 `CandidateDraftError` / 缺 `assembly_timeline_path`)写 escalated 但不带结构性标记,按现路由 = 非结构性软降级 → 修复重试后到①人工复核。收紧(改 fail-closed)属业务定义变更,另案。

## 3. 提交清单(3 笔)

| SHA | 内容 | 备注 |
|---|---|---|
| `bb18dd6` | 主改造:节点控制分支 / state 新键 / load_media 写标记 / GATE_META·显示名·边标签·SCHEMA 11 / monitor.html placeholder 清遗留③ / config·graph·docstring 注释 / README / 测试全量改造(30 文件,+1351/−140) | 已随 20261120 sync-github 推送 origin/main |
| `46f37da` | node_10 增量回传修复(见 §5) | **并行会话在共享工作区代为提交**(内容即本会话工作区改动,提交信息与实现一致);node_10 全套 28 用例全绿 |
| `98c99c` | 预热 required_features 全 False + 脚本头注释关卡语义更新(见 §5) | 本会话提交 |

## 4. 测试结果

| 项 | 结果 |
|---|---|
| unit 全量(`pytest tests/unit`) | **exit 0,0 失败**(比基线 1 环境性 F 还干净) |
| integration 全量 | 3 失败 = `test_reorder_topology::test_tail_chain_applies_to_both_drafts`、`test_sequential_integration::test_sequential_produces_complete_draft`、`test_auto_mode_end_to_end::test_load_media_then_split_shots_then_qa_gate_chain` —— **与 HEAD 基线 worktree(baseline-6f44a41,已清理)逐字一致 = 存量**,零新增 |
| node_06 单测重写 | 27 用例全绿:U1-U12 行为矩阵(monkeypatch `interrupt` 驱动真实控制流)+ 旧任务兼容 L1-L4 + payload/resume 纯函数 |
| 集成改造 | 6 个 gate 契约测试族统一「QC 关 + seed escalated」(`test_interrupt_resume`/`week4`/`reorder_topology`/`sequential`/`week5`/`postgres_resume`);`test_assembly_qc_graph` 补真实节点挂起/自动通过两用例 + 路由 fail-closed 断言;wiring 黄金轨迹重录(① 自动通过,轨迹延伸到关卡②挂起,`_SUFFIX` 14 步)+ roundtrip 恢复链补 `_add_manual_bgm`;PG 新增旧版本 checkpoint 跨连接恢复测试(审查 §4.4 五项) |
| SSE 契约 | pytest 侧 + node 侧(`node --test`,3 pass 0 fail)全过(node_id 未变,fixture 零改动) |
| grep 扫尾 | 「分镜顺序调整/手动调整分镜」活代码清零(仅剩 `agent_gate/fixtures/toy_graph.py` toy 桩文案与 node_05 历史注释,均已评估保留/改写) |

## 5. 真机三轮重启实证(video-001 预热线程)

| 轮次 | 日志 | 结果 |
|---|---|---|
| 第 1 轮 | start_20261120_044501 | ①自动通过后**首次暴露潜伏缺陷**:`fork_draft_for_english_branch` 与 `node_10_inject_text_fx` 同超步,线程复用旧 checkpoint 残留的 `draft_dir_en_branch` 通道值,被 node_10 的 `{**state}` 全量回传原样重发 → 同超步双写 → `InvalidUpdateError: Can receive only one value per step` 崩溃(旧代码停在①,该超步在预热线程从未执行过) |
| 第 2 轮 | start_20261120_045934 | node_10 改增量 patch 后穿过 fork 带,但交付 QA fail-closed:`subtitles_zh=failed; voiceover_zh=failed` —— 预热无人值守必然产不出配音(jianying SAMI 需剪映在线),既有 fail-closed 设计(50cd 复盘同款),每次启动被打成 run.failed |
| 第 3 轮 | start_20261120_050824 | **`一键启动完成`**:① auto-pass(`[notify] checkpoint1 auto-pass thread=video-001 qc_status='pass_with_warnings' tag=checkpoint1_auto_passed`)→ ② 直通(node_05 自动铺 BGM)→ 交付 QA 过(required_features 九类全 False,与整图测试同口径)→ export 成功 → **run.completed**;致命错误 0,设计内告警 3,timeline QC pass / clip_count 1,`/api/workflow/graph` schema_version=11,`node-names` 显示名=QC降级复核① |

> 说明:预热线程 required_features 全 False 后,交付 QA 照常检查留痕但不在预热里当交付闸门;真实任务的必需功能语义不变(创建台按用户勾选)。

## 6. 与审查修订版计划的差异说明

- 审查 §3.1 建议的 `assembly_qc_gate_enabled` 标记写入点未按「各启动入口」方案,改为 **`storyline_load_media` 单一收口点**(QC 两态必经、未来新入口自动覆盖;测试 fake load_media 不写标记,由 seed 显式携带);
- 审查 §3.2 伪代码对「旧 checkpoint + 状态缺失」未定案,本实施定案为**完整保留旧版无条件挂起**(最严格兼容:恢复值必被消费,不依赖"跳过 interrupt 后 resume 值闲置"的 langgraph 行为),并由 PG 跨版本恢复测试实证(§4);
- 审查 §4.1 分类落地:gate 契约测试族 6 文件统一 QC 关 + seed escalated(而非逐个判别);assembly 真实挂起/自动通过两用例落在 `test_assembly_qc_graph`(§4.2)。

## 7. 遗留(人工点验/观察项)

1. **真机 QC 降级挂起 + reject 场景**:需注入 escalated(或等真实降级任务出现)验证「① QC 降级复核」挂起面板、报告/预览入口可打开、reject 终止;
2. **旧任务恢复点验**:存量挂①任务 `console-011419d9ceec`(pass_with_warnings)在创建台点「确认恢复」应直接续跑(兼容分支已由 PG 跨版本测试覆盖,真机未点验);
3. **观察项(另案候选)**:`node_07/08/09/09b` 仍有 `{**state}` 全量回传 —— 同类隐患(与并发写者同带 + 线程复用残留通道值时触发),当前仅 node_10 与 fork 同带(已修);其余节点当前均独占超步,未触发路径;
4. SCHEMA 11 已使浏览器布局缓存失效,节点图旧名「人工调整镜头顺序①」强刷即更新。

## 8. 涉及文件(bb18dd6,30 文件)

生产:`nodes/node_06_human_reorder.py`(核心)、`nodes/storyline/node_load_media.py`(写标记)、`state.py`(新键)、`nodes/node_05_generate_draft.py`(空草稿守卫注释/文案)、`nodes/node_12_human_add_bgm.py`、`nodes/_checkpoint.py`、`graph.py`、`config.py`、`monitoring/workflow_graph.py`、`web/human_gate_payload.py`、`web/monitor.html`、`agent_gate/cli.py`、`scripts/resume_workflow.py`、`README.md`。
测试:unit 6 文件(node_06 重写 + gates_api/readonly/node_08/两个 docstring);integration 9 文件(6 契约族 + assembly_qc + wiring + schema)。
后补:`nodes/node_10_inject_text_fx.py` + 单测(46f37da)、`start_production.ps1`(98c99c)。

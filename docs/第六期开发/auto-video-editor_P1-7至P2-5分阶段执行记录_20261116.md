# auto-video-editor:assembly 草稿内容生产链 P1-7~P2-5 分阶段执行记录(2026-11-16)

> **文档类型**:分阶段执行记录 / 验收对照 / 真机交接
> **前置**:P1-0~P1-6 已由并行会话交付(f52d047→0256a02,2026-11-15,见《auto-video-editor_P1-0至P1-6分阶段执行记录_20261115.md》)
> **本记录覆盖**:P1-7、P1-8、P2-1、P2-2、P2-3、P2-4、P2-5 共七阶段
> **基线**:接续 0256a02;本会话提交 38929bd → adaf3db(+ P2-5 收尾提交)

---

## 1. 执行门禁与衔接(用户指令)

- 并行会话 `sess_40f70ed3-36ed-487a-9825-eb9b09101f8f`(执行 P1-0~P1-6)按用户要求每 20 分钟监控一次,14:00 UTC 确认 `idle` 后开工。
- 开工前 `git log` 核对:P1-0~P1-6 已全量交付,本会话范围收敛为最初指定的 **P1-7~P2-5 七阶段**。
- 等待期间工作区零改动、零提交(门禁纪律)。

## 2. 逐阶段改动与提交

### P1-7 最终计划提交与消费(38929bd)

| 改动点 | 内容 |
|---|---|
| `state.py` | `assembly_final_plan` 三件套 + `assembly_influence_trace_path` 正式进 state(补 P1-6 遗留断点:build 节点返回的路径键此前无人接) |
| `nodes/storyline/_common.py` | `effective_canonical_plan(state)` 统一三处 plan 消费口径:**文件权威**(路径+sha 双重校验),invalid → 主消费方 fail-closed、辅助消费方回落 |
| `nodes/assembly/node_write_report.py` | 最终计划提交(模型硬门 + 候选 sha + 基线 sha 五重阻断判定);影响链六节点贡献收尾 + `final_plan_sha256` 固化;报告 6/7/8 三节 |
| `nodes/node_05_generate_draft.py` | 优先消费 final plan;sha 漂移/基线漂移 → `ContractInvalid` fail-closed;`mapped_draft_sha256` 回写 trace + consumer contribution;幂等键快照记 final sha;`draft_from_assembly_final_plan / draft_from_storyline_plan` 来源 tag |
| `nodes/node_08/09` | 切换 `effective_canonical_plan`(assembly 改过 clips 后按旧计划匹配字幕/转场会静默失配) |
| `nodes/node_06_human_reorder.py` | interrupt payload 挂起前带报告/预览路径 + QC 状态 + 关键警告(契约三键逐字不动);resume 值 `reject` → escalated 时 fail-closed 终止 |
| `web/human_gate_payload.py` + `console.html` + `api_console.py` | gate view assembly 三字段(payload→state 兜底);关卡① 报告/预览打开按钮;`open-draft-dir` 端点扩 `assembly_report / assembly_preview`(路径只来自 checkpoint state,白名单不变) |
| 测试 | unit 新增 24 例;integration 新增 handoff + influence 初版;修复 `test_interrupt_resume` 替身签名(`**kw` 兼容新 resume_value 参数) |

**关键决策**:
1. **final plan 文件是权威**——state 内联 dict 仅为监控镜像;文件缺失/损坏/sha 漂移一律 invalid(use case E 的"删除 final plan 断言生成草稿失败关闭")。
2. **基线漂移守卫在 node_05**——`sha256(storyline_plan) == assembly_base_plan_sha256` 不匹配即 `ContractInvalid`(拒绝旧 patch 套新计划);检查发生在归一化 deepcopy **之前**。
3. **不新增 interrupt 关卡**(与关卡③删除的方向一致):结构性错误达上限走 `assembly_failed → END`(P1-6 已落);write_report 侧保留防御纵深(收到结构性 error 也不提交);仅警告降级放行进报告,人工可 reject。

**验收对照**(计划文档 P1-7 七条):全部通过——final plan 被 generate_draft 明确读取(unit 5 例 + integration handoff);改 selected/in/out/order → draft 视频轨对应变化;hash 不匹配 fail-closed;双文件仍走 safe_write_draft;挂起前可取报告/预览路径;结构性错误不进关卡①。

### P1-8 端到端六节点影响验证(5db7be9)

`tests/integration/test_assembly_draft_influence.py` 扩为六场景主文件:

| 场景 | 断言 |
|---|---|
| ① discover→草稿 | manifest 黑帧区间(dict `{start,end}` 格式)→ build 确定性 trim → 草稿 clip_0001 源区间 0-4000→2500-4000ms;patch 证据 `black:*` 可追溯;对照:干净 manifest 草稿与基线逐段一致 |
| ② visual evidence→草稿 | 同一 manifest、观察 fixture ok vs degraded → 脚本化 LLM 按证据行分支 → 草稿段数 4 vs 3(决策差异由视觉证据驱动) |
| ③ build patch→草稿 | 脚本化 LLM 选中未入编候选 → select op 追加 → 草稿 4 段逐段 == 候选计划,证据引用 candidate_id |
| ④ QC 阻断 | 结构性错误候选 → final plan 不提交(路由 fail-closed / 报告 ⛔) |
| ⑤ repair 生效 | 首轮注入 broken_reference(单数 `clip_id` 定位——**broken_reference 不读 clip_ids,只有 track_desync 读**)→ restore 基线 clip(applied_operations 非空 + 前后 sha 不同)→ 复验通过 → 草稿回基线 |
| ⑥ report/hash 守卫 | 文件缺失/sha 漂移/基线漂移 fail-closed 由 unit 覆盖;不提交→回退 storyline_plan 由本文件 fallback 场景覆盖 |

**排障记录**:① stub 链 join 产出 clips=0(node_05 空 clips fail-closed)→ 测试在 join 出口注入共享 fixture 计划;② `resolve_draft_dir` 需双 patch(config + graph 命名空间——graph 导入时绑定);③ 黑帧区间格式是 dict `{start,end}` 非数对;④ `resolve_draft_dir` 与 join 节点的 patch 目标分别是 graph 命名空间与 `nodes.storyline.join_storyline`。

### P2-1 LLM 调用诊断(49844d3)

- `_call_llm_for_selection` 改 `chat_json_with_trace` 留痕;`_normalize_selection` 增候选索引统计(returned/invalid/duplicate/adopted);
- `build_llm_diagnostics`:`llm_attempted / llm_client_type(stub|real|none)/ model_name / request_status(ok|empty_response|parse_error|schema_mismatch|exception|timeout|not_attempted)/ response_valid / selection_applied / fallback_used / failure_reason / elapsed_seconds`;
- 诊断入 state(`assembly_llm_diagnostics`)+ timeline.json metadata + status tag(`assembly_llm_request_status:*`)+ report 6.1 节;
- 三路注入核实:worker=agent_gate/runner.py:427、CLI=scripts/run_workflow.py:125 与 resume_workflow.py:147、测试=显式参数/set_default_client——全部经 `set_default_client` 汇聚;
- 单测 12 例。**llm_used 宽松语义保留**(空 list 也 True),准确刻画由诊断字段承担。

### P2-2 性能与资源(a5109da)

- `analyze_media` 增 `probe_data` 复用参数:同一素材 ffprobe 2 次→1 次;
- discover 并发化(`ASSEMBLY_PROBE_CONCURRENCY` 默认 2,顺序保持)+ 单素材超时(`ASSEMBLY_PROBE_TIMEOUT_S` 默认 300s,超时按 degraded 不静默丢失);
- `timing.py`(新增):`node_timing.json` 追加式六节点耗时 + `timed_assembly_node` 装饰器挂 5 节点(discover 自带分阶段不重复挂)+ report 6.2 节渲染;
- 观察缓存签名加 `tool_version`(实现升级旧缓存整体失效);单候选抽帧预算 `ASSEMBLY_MAX_FRAMES_PER_CANDIDATE`(默认 120)经采样参数 clamp 自动进签名;
- **决策记录**:preview 分辨率预算不单独实现——画布固定 1080x1920 且 scale+pad 链已有界;
- 单测 8 例。

### P2-3 幂等与回滚(76f9539)

- trace 增 `hash_chain`(base/候选/最终 + 逐修复轮 before/after/fingerprint)——四级 hash 单点对账;
- `test_assembly_checkpoint_artifacts.py` 4 例:同 patch 重放两次候选 sha 逐字一致 / 异 base hash PatchError / 同一 thread 重跑草稿逐段一致+候选 sha 一致(不重复加 clip/音轨)/ 写入中途失败旧草稿字节级保留+上游 artifact 不污染;
- 既有机制复用:patch 绑 base hash(P1-4)、repair 指纹去重(P1-6)、final plan 缺失回退(P1-7)、safe_write_draft 快照回滚(存量)。

### P2-4 前端可观测性(adaf3db)

- write_report 尾部上报 `assembly.qc` 摘要事件(qc_status/final_status/候选数/patch ops/修复轮次/retry/警告数;**detail 整体透传,summary.event 键必须保留**);
- `delivery_summary.mjs` reduceDeliveryCard 新增 assembly.qc 归并(pass→ok / warn / blocked→fail / not_available→skip);
- console.html DELIVERY_ROWS 增 assembly_qc 行;前端测试 +5 例(node --test 73 绿);
- 关卡① 三入口与 reject 已随 P1-7 交付;修复差异证据视图 = report.md 6/7/8 节 + 关卡① 打开 QC 报告目录入口。

### P2-5 测试数据、真实运行证据与质量基线(本提交)

- `scripts/make_assembly_fixtures.py`:真 ffmpeg 门控合成 8 组素材(黑帧段/静音音轨/无音轨/旋转 90°/色彩标记近似 HDR/VFR 混合/同名双目录/截断损坏),输出 `tests/fixtures/assembly/media/`(不入库按需再生);无 ffmpeg 时明确 exit 1;
- `scripts/archive_assembly_evidence.py`:证据包归档(commit SHA / 监控 schema 版本 / 配置开关 / VIDEO_LLM_BACKEND / 输入摘要 / 四级 hash 链 + 产物副本脱敏 + 可选最终计划↔正式草稿视频轨对账);**脱敏口径:用户目录/家目录前缀整体 `<REDACTED>`(JSON 转义 `\\` 双写需 `\\+` 正则;绝不回填用户名,basename 保留)**;
- `tests/fixtures/assembly/`:3 个契约样例 JSON(manifest 多素材含 degraded / structural QC report / repair_history)+ README;
- `tests/unit/test_assembly_evidence_archive.py` 4 例;
- 真机验收清单见《assembly重构真机验收清单_20261116.md》(**待操作员**)。

## 3. 测试基线

| 项 | 结果 |
|---|---|
| unit 全量 | exit 0 全绿(逐阶段核对;新增约 60 例) |
| integration 全量 | 恰 3 存量失败(auto_e2e / reorder tail_chain / sequential,与 P1-0~P1-6 执行记录逐字同因"字幕应被注入"),零新增 |
| 前端 node --test | 73 绿(68 + 5 新增) |
| assembly 专项回归 | influence/handoff/qc_graph/checkpoint_artifacts/schema/wiring/interrupt_resume 全绿 |

## 4. 关键设计决策汇总(P1-7~P2-5 新增)

1. final plan 文件权威 + 三重 hash 守卫(提交侧候选/基线漂移阻断;消费侧漂移/基线漂移 fail-closed);
2. 统一计划取用口径 `effective_canonical_plan`(node_05/08/09 三点共用);
3. 降级策略无新关卡:结构性错误→不提交→回退原计划;警告→放行;人工 reject 显式拒绝;
4. QC issue 统一 typed 形状;repair 策略按 code 分派(broken_reference 系走单数 clip_id;track_desync 走 clip_ids);
5. 四级 hash 链入 trace;重跑幂等由确定性 op_id + apply 纯函数 + stub 客户端确定性保证;
6. 计时/预算/缓存版本三件套为可观测与可控的资源边界;preview 分辨率预算不单做(画布固定有界)。

## 5. 遗留与交接

- **真机验收待操作员**:见《assembly重构真机验收清单_20261116.md》8 项(剪映客户端实际打开/播放核对是自动化 JSON 校验不能替代的部分);
- P2-5 的真素材自动用例走 `ASSEMBLY_TEST_REAL_FFMPEG=1` 门禁(本机未跑,素材脚本已就绪);
- 服务未重启:新拓扑(SCHEMA=10,随 P1-1)与本会话改动需管理员 pwsh7 重启 start_production.ps1 后生效。

# 阶段6 · 全量回归、E2E 与发布验收(remove-chat-loop-20261104)

> 日期:2026-11-05
> 执行计划:《auto-video-editor_单流水线架构设计与分阶段执行计划_评审修订版_20261104.md》§6 阶段6
> 分支工作线:`refactor/remove-chat-loop-20261104`(阶段0-2,由 sess_ddb715fe 交付)→ 阶段3-6(本会话,经用户拍板继续在 main 提交)
> 提交链:`ab7982b`(计划)→ `d2eb1d8`(阶段0)→ `6530837`(阶段1)→ `7c70d30`(阶段2)→ `8af762d`(阶段3前置:直接CLI resume)→ `5fd5d9d`(阶段3)→ `b7c3633`(阶段4)→ `10ef99b`(阶段5)→ 阶段6(本提交)
> 环境:Windows / Git Bash / Python 3.13.11(主 `.venv`)/ node v24.19.0 / PostgreSQL edb-postgres16(localhost:5432/video_workflow)—— 与阶段0 基线同环境

---

## 1. 测试套件回归(JUnit 坏用例 ID 集合差分,零新增 = 通过)

| 套件 | 阶段0 基线 | 阶段6 终态 | 差分结论 |
|---|---|---|---|
| `tests/unit` | 1296 条,18 坏(全部 `test_session_store_repository`) | **1142 条,0 坏(首次全绿)** | NEW=0;18 个基线坏 ID 属阶段4 删除对象,随文件删除消失(GONE 预期) |
| `tests/integration` | 230 条,26 坏(23 error=`test_sessions_api_postgres` openstoryline 死路径 + 3 failure 环境性) | **133 条,1 坏** | NEW=0;23 error 随阶段4 删除文件消失;`test_human_fusion_graph`(阶段3 删)与 `test_assembly_qc_graph e2e`(基线环境性失败,**阶段4 起转通过**)GONE;剩 1 坏 = `test_reorder_topology::test_tail_chain_applies_to_both_drafts`,与基线**同 ID** 环境性(产物存在性断言,ffmpeg/素材环境) |
| `agent_gate/tests` | 274 条,1 坏 | **274 条,1 坏** | NEW=0;坏 = `test_scaffold.py::test_init_writes_expected_files`,与基线**同 ID** 环境性(基线归因:断言重写缓存残留旧盘符 `E:\...` 路径) |
| `node --test web/tests/*.test.mjs`(glob 形式) | 77/77(旧文件集) | **14/14** | 聊天页专属前端测试已随阶段2 删除;monitor/node_map 前端测试全绿 |
| `tests_openstoryline` | 325 条,66 坏 | —(整目录随阶段2 删除) | 删除前共享测试已迁出(阶段1/2 交付) |

JUnit 证据:`baseline-*.xml`(阶段0)与本目录 `stage3-*` / `stage4-*` / `stage5-*` / `stage6-*` 各阶段 XML;每阶段提交前均做过 `NEW=∅` 集合差分。

## 2. 图拓扑验收(以编译图为真源)

- schema 快照重录:`docs/remove-chat-loop-20261104/schema-snapshots/`(default_baseline / assembly_qc_off / ai_transition_on 三组合;fusion 组合随 fusion 层删除)。
- `tests/integration/test_workflow_graph_schema.py`(阶段3 重写为 auto-only):schema 节点/边与 `compiled_graph.builder` 真源逐点一致;**START → storyline_load_media 直连**;关卡 **①②③** 节点可达;human 5 节点与 clean_cache 不再出现在 schema 中 —— 集成套件内通过。
- `monitoring/workflow_graph.py` 输出:`mode` 恒 "auto"、`flags` 仅 `ASSEMBLY_QC_GATE_ENABLED`;`CHECKPOINT_MARKS=("①","②","③")`;前端 `workflow_monitor.mjs` 布局缓存 key 兼容。

## 3. E2E(agent_gate stub 全链路,隔离测试 thread)

- thread_id:**`stage6-e2e-20261105-01`**(一次性测试线程;新写入 PG 的 checkpoints / workflow_runs / workflow_run_events 行为其运行产物,存量数据未动)。
- 命令:`AGENT_GATE_MEDIA_ROOTS=<repo>/inputs` 白名单下
  `python -m agent_gate start --thread-id stage6-e2e-20261105-01 --outputs-root outputs --backend stub --video inputs/media_0001.mov`
  → worker 启动,auto 链跑通(约 8 分钟,storyline 产物落 `outputs/stage6-e2e-20261105-01/`),**停在人工关卡①**:
  `status` → `worker_state=waiting_human`,`interrupts=[{checkpoint:"①", step:6, legacy_id:"checkpoint1_reorder", draft_path:...}]`,`next_action=ask_human`,并给出 `human_commands` resume 指引 —— 与计划验收条件一致。
- 受控 resume 验证(隔离 thread + `--force-no-tty`,受控 CI 用法;**真机 TTY resume 仍留给操作员**,生产路径不使用该旗标):
  **三道人工关卡全部触发并逐次放行,最终 `worker_state=completed`** ——
  关卡①(checkpoint1_reorder)→ resume → 英文线(翻译/排版检测命中)→ **关卡③**
  (checkpoint3_layout_review,条件触发属实)→ resume → **关卡②**(checkpoint2_add_bgm)
  → resume → completed。3 次 `agent_gate resume --checkpoint ①/③/② --value true
  --force-no-tty` 均成功(每次 worker 以 resume 模式重启,从 checkpoint 续跑),
  证明:①②③ 可挂起、可恢复、恢复后链路完整走到交付 QA 终点。

## 4. 监控只读冒烟(monitor_fastapi,主 .venv,127.0.0.1:7860)

- 独立启动 `python -m uvicorn monitor_fastapi:app --host 127.0.0.1 --port 7860`:
  `GET /`→200、`GET /node-map`→200、`GET /api/workflow/graph?refresh=1`→200。
- 用 E2E thread 实测:`GET /api/workflow/runs/<tid>` 快照 JSON 正常(`available:true`);
  SSE `/events` 先发 `snapshot` 再按全局 id 重放事件;`Last-Event-ID: 3` 补发生效
  (从 id>3 起续发);`GET /api/workflow/runs/<tid>/resume-status` 对未挂起线程返回
  409(未挂起属预期语义,只读不执行恢复)。
- 刷新页面只重新 GET;监控无任何启动/恢复端点(POST /runs 阶段1 已删)。
- PG 不可达 503:由 `web/api_workflow.py` 显式 503 分支保障(阶段1 已验收;本次未重演——不停生产库)。

## 5. 数据库与迁移

- 双路径迁移验证(阶段4 交付,复核于本阶段):existing 库幂等重跑「已是最新」,台账 1-5 与原时间戳一致;fresh 临时库全量迁移 1-5 成功后即弃。
- **存量表零 DROP**:`chat_sessions`(16 行)/`workflow_chat_links`(0 行)/`workflow_runs` / `workflow_run_events` / checkpoints / blobs / writes 全部原样;`app_schema_migrations` 1-5 未改写。
- 迁移 1/5 定义保留并加 legacy 注释(`db/migrations.py`),fresh 库仍可从零建满 schema。

## 6. 残留扫描与安全检查

- 生产代码零真实引用(全仓 grep,仅命中 db/migrations.py 的 legacy SQL 与各处「已删除/已退役」说明性注释):`agent_fastapi` / `open_storyline` / `mcode_chat_model` / `zcode_agent_adapter` / `workflow_chat_link` / `chat_session_id` / human 5 节点 / `STORYLINE_HUMAN_FUSION` / `clean_cache` / `IMPORT_VIDEO_*` / `OPENSTORYLINE_*` / `resolved_cache_paths`。
- 文档历史引用:`README-OpenStoryline.md` 与 `docs/修改文档/` 6 篇聊天UI/human融合/节点指示文档已加失效头注(阶段5)。
- `git diff --check`:干净;依赖清单:`fastapi==0.162.0` / `uvicorn[standard]==0.54.0` 在 `requirements.txt` 与 `pyproject.toml` 同步声明;secret 扫描:tracked 变更文件无密钥模式(`config.toml.local` 不入库,备份 `config.toml.local.bak-20261104` 受 `.gitignore` 保护)。
- 配置 diff:`config.toml` / `config.toml.local` 本阶段零改动。

## 7. 已知限制与遗留

1. `tests/integration/test_reorder_topology::test_tail_chain_applies_to_both_drafts` 与 `agent_gate/tests/test_scaffold.py::test_init_writes_expected_files`:与阶段0 基线**同 ID** 的环境性失败(非本次重构引入),留待后续环境治理,不阻断本发布。
2. `start_production.ps1` / `stop_production.ps1` 为 UTF-8 无 BOM,须用 **PowerShell 7(pwsh)** 运行;Windows PowerShell 5.1 按 ANSI 读会乱码 —— 与 HEAD 版一致的存量行为,非本次引入。
3. 一键启动的 Phase 3 验收、真机(剪映客户端)人工关卡操作与 `drive --agent mcode` 真实答题链路:留待操作员真机验收(计划 §7 验证 6)。
4. agent_gate worker 节点级事件仍为运行级(README §10.2 既定口径,非本次范围)。
5. `.venv-openstoryline/` 本地目录按计划保留,由操作员在回滚窗口结束后手动清理。

## 8. 评审收尾核对(2026-11-07)

按《docs/第二期开发/auto-video-editor_单流水线循环化_设计与执行计划_附件评审_20261105.md》逐项核对 main 当前态后的收尾修复(单 commit)。核对结论:该评审文档列出的阶段 3R/3G/3C/4/5/6 待修项**绝大多数已在此前提交落实**(阶段0-6 至 `aa1555a`;评审收尾 `eaf5c0b`/`ca37c18`;§4.6 两条残留扫描 83 条命中全部属豁免类别,零疑似残留)。本次补齐核对出的残留:

| 项 | 判定 | 处理 |
|---|---|---|
| README §2.6 参数表默认值漂移(`-SourceVideo`/`-InputsDir` 仍写 FireRed/jianying-editor 旧路径) | 未修 | 已对齐 `start_production.ps1` 实际默认(`$PSScriptRoot` 锚定 `inputs\`) |
| `scripts/migrate_db.py` docstring 缺 legacy 注记(评审阶段4任务6) | 未修 | 已补(chat_sessions/workflow_chat_links legacy 说明) |
| `pyproject.toml` fastapi/uvicorn 用范围钉法,与 requirements.txt `==` 不一致(评审 §8「依赖一致」;§6 声称的"同步"与实态不符) | 未修 | 已统一为 `fastapi==0.162.0` / `uvicorn[standard]==0.54.0` |
| docs/ 下 13 个 mcode/zcode 专属文档无历史头注(评审阶段5任务7) | 未修 | 已按 `README-OpenStoryline.md` 样式前置头注(docs/修正/ 8 篇 + docs/分析/ 5 篇) |
| `monitoring/workflow_graph.py` `_current_mode` 未删、`mode` 经 config 读取(评审阶段3R任务5明细) | 部分修 | 已删;`mode` 输出常量 `"auto"`(输出值不变,SCHEMA_VERSION 不升,前端布局缓存 key 不受影响) |
| 阶段3G 入口 A stub 提示(决策 D3-A,红线5) | 偏差·已补齐 | 新增 `scripts/_entry_a_llm.py` + `run_workflow.py`/`resume_workflow.py` 接入(stderr 提示;`VIDEO_LLM_BACKEND` 非 stub 且未显式 `--llm-backend` 时以退出码 2 拒跑)+ `tests/unit/test_entry_a_llm_notice.py` 4 用例;与既有的 Console `--llm-backend` 路线(`ee32878`)并存,显式传参时短路不打扰 |
| 阶段3C 独立清理脚本(决策 D2-A) | 偏差·判定达成 | 未建 `scripts/clean_cache.py`;清理缓存已由 `/console` Web 入口落地(`5379f86`,冷却限流+活跃任务 409+专项测试),README 已指向;记录方案变更,不另建 CLI |
| 评审文档本身未入库 | 未修 | 已入库并加 2026-11-07 核对注记 |

**门禁(2026-11-07 实测,HEAD=`9940806`)**:S1 收集 1864 条 0 错误;`tests/unit` 全量 0 失败(exit 0);`test_graph_compiles` / 既有 `test_run_workflow_llm_backend` / `test_resume_workflow_llm_backend` 通过;前端 `node --test web/tests/*.test.mjs` 36/36;`VIDEO_LLM_BACKEND=claude-cli` 跑 `run_workflow.py` 实测退出码 2(提示在预检前,不触 PG);`build_graph_schema` 实测 `mode="auto"`、`schema_version=1` 不变;tomllib 实测钉版一致;`git diff --check` 干净。

**已知存量失败(非本次引入,`git stash` 对照证实)**:`tests/integration/test_workflow_graph_schema.py::test_every_node_has_metadata[True/False]`(`export_final_video 缺描述`)——由并行进行中的未提交改动(未提交的 `graph.py` 接入 `export_final_video` 节点但 `_NODE_META` 尚未登记)造成,归该工作线收口,与本 commit 无关。

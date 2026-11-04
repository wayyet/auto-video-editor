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

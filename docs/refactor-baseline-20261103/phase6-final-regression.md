# 阶段6 全量回归与收尾记录(冗余模块合并 2026-11-03)

> 依据:`docs/修改文档/auto-video-editor_main_冗余模块合并设计与执行计划.md` §6.8(6.5)
> 执行分支:`refactor/dedupe-openstoryline-mcp-20261103`;执行时 HEAD:阶段 5 提交 `0bec650` 之后、阶段 6 提交之前
> 覆盖阶段:阶段 3(21a0e18)/ 阶段 4(a1298dc)/ 阶段 5(0bec650)的全部变更
> 对照基线:阶段 0 记录(`baseline-record.md`,基准 `a27298a`);另对照阶段 0-2B 收尾记录(cc8c02a)

## 全量回归结果(junit XML 逐 ID 对照)

| 套件 | 基线 tests | 本次 tests | 基线 失败/错误 | 本次 失败/错误 | 新增 | 明细 |
|---|---:|---:|---:|---:|---|---|
| `tests/unit`(failure) | 1231 | 1296 | 18 | 18 | **0** | `post-phase6-unit.xml`;失败 ID 与基线逐一相同(均为 `test_session_store_repository.py` 存量环境性失败) |
| `tests/integration`(error) | 230 | 230 | 23 | 23 | **0** | `post-phase6-integration.xml`;23 个 ERROR 全部为 Postgres 不可达类存量错误 |
| `tests_openstoryline`(failure) | 319 | 325 | 66 | 66 | **0** | `post-phase6-openstoryline.xml`;失败 ID 与基线逐一相同(+6 为阶段 1 新增 `test_mcp_legacy_compat.py`) |
| `node --test`(前端 monitor + chat indicator) | 37 | 37 | 0 | 0 | **0** | exit 0 |

tests/unit 增量 65 = 阶段 3 的 15(防重复静态测试)+ 阶段 4 的 11(Provider Contract 契约测试)+ 阶段 0-2B 已入库的 39(双 Loader 语义/golden 等)。

## 全局验收(G-A)核对

| 编号 | 结论 | 依据 |
|---|---|---|
| G-A1 | ✅ `graph.py` 53 节点 / 44 add_edge / 10 add_conditional_edges | grep 统计,阶段 3-6 各提交零改动 |
| G-A2 | ✅ 四配置 Schema 与基线一致 | 阶段 0-2B 收尾(cc8c02a)已对照;其后 graph.py 未再变动 |
| G-A3/A4/A5 | ✅ 三个 pytest 套件对照基线**零新增失败/错误** | 上表 junit 逐 ID 对照 |
| G-A6 | ⚠️ 环境受限:本机无运行中的 PostgreSQL,23 个集成 ERROR 与基线相同(存量);Checkpointer 行为未在本机新增验证 | 同基线口径 |
| G-A7 | ✅(阶段 1 已验)`.venv-openstoryline` 双入口启动由阶段 0-2B 收尾验证;其后 MCP 相关文件未再变动 | 54be813 执行记录 |
| G-A8 | ✅ 主项目未新增 torch/torchaudio/moviepy | `pyproject.toml` 零改动 + `test_provider_contracts.py` 守卫测试 |
| G-A9 | ✅ 6 个旧 MCP 路径 shim 无任何函数定义残留 | grep `^def`/缩进 def 计数 = 0 |
| G-A10 | ✅ docs 快照、`agent_gate/fixtures`、空 `__init__.py` 未被删除 | 阶段 3-6 提交 diff 仅含:新增测试/契约模块/文档与记录 |

## 阶段 6 文档同步清单(6.1–6.4)

- [x] 6.1 `README.md`:graph.py 结构行更新为当前统计口径(53/44/10,2026-11-03);PostgreSQL-only 描述原有
- [x] 6.2 `docs/integration/` 两份旧计划文档:补"2026-11-03 现状更新"横幅(53/44/10 勘误 + helper 收敛已完成标记),正文保留为历史记录
- [x] 6.3 `docs/integration/architecture_decision_record.md`:追加 ADR-015(MCP 唯一实现目录与兼容别名)、ADR-016(提示词单一物理资源目录 + 双 Loader Facade)
- [x] 6.4 `docs/修改文档/auto-video-editor_main_冗余模块合并设计与执行计划_20261103.md` 标记 superseded,指向主文档(canonical)

## 阶段纪律核对(6.9)

- 一阶段一提交:阶段 3 `21a0e18` / 阶段 4 `a1298dc` / 阶段 5 `0bec650` / 阶段 6(本提交)✅
- `git diff --check` 干净(仅 autocrlf 提示,非 whitespace 错误)✅
- 未修改 `graph.py` 拓扑、`db/migrations.py`、数据库 schema、开关默认值 ✅

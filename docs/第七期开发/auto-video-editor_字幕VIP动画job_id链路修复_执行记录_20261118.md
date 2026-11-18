# 字幕 VIP 动画 job_id 链路修复 —— 执行记录

> 执行日期:2026-11-18
> 执行依据:《[auto-video-editor_字幕VIP动画job_id链路_设计审查与修订执行计划_20261118.md](auto-video-editor_字幕VIP动画job_id链路_设计审查与修订执行计划_20261118.md)》(修订版审查计划,同目录)
> 来源审计:《[auto-video-editor_草稿52373c6f9e11字幕VIP动画核验与job_id链路修复计划_20261118.md](auto-video-editor_草稿52373c6f9e11字幕VIP动画核验与job_id链路修复计划_20261118.md)》
> 基线:`ea40d941b1824bf22b15b750f031841dad7ce485`(main,`7c0bbc7` 为其祖先,已验证)
> 定级:P1(功能一致性缺陷 —— zh/en 字幕动画不一致 + 英文轮换失效;非系统可用性故障)

---

## 1. 阶段 0:基线与证据固化

| 项 | 结果 |
|---|---|
| 基线完整 SHA | `ea40d941b1824bf22b15b750f031841dad7ce485` |
| `7c0bbc7` 祖先关系 | `git merge-base --is-ancestor` 通过 |
| 草稿备份 | `outputs/_draft_backups/20261118_jobid_fix/`(gitignore 内,不入库):中/英两稿 `draft_content.json`+`draft_info.json` 共 4 文件,sha256 见同目录 `MANIFEST.txt`(zh content `c5162379…c0d4` / zh info `5f520cd4…62fe` / en content=info `05ddd377…7d73`,en 稿两文件字节相同系该稿写入形态) |
| 资源库快照 | 同目录 `intro_vip_list.json`:intro 段 78 条、全 VIP,与 `_counts.intro_vip=78` 一致 |
| 测试基线 | 4 文件 67 用例(node_10=11 / node_09=25 / fx_pools=13 / console_state=18):**66 过 + 1 失败**;失败 = `test_node_09_inject_fx.py::test_inject_fx_deterministic_across_pythonhashseed`,原因 = 剪映客户端正在运行触发 `atomic_writer` 写入互斥(`JY_WRITE_FAIL_ON_JIANYING_RUNNING` 严格模式)——**已知环境性失败**(子进程写草稿被拒),与代码无关 |
| 取证脚本 | `outputs/_draft_backups/20261118_jobid_fix/_baseline_tools.py`(一次性,可复跑) |

对现有草稿零写操作 ✓。

## 2. 阶段 1:统一任务 ID resolver(已实施)

**新增 `jy_common/job_identity.py`** — `resolve_job_id(state, config=None)` 纯函数,解析优先级:

```text
config["configurable"]["thread_id"]   ← LangGraph 执行上下文(权威)
  → state["thread_id"]               ← 显式携带该字段的调用方
  → state["session_id"]              ← 旧 checkpoint 兼容(三入口均写 session_id=thread_id)
  → state["draft_path"] 父目录名      ← 末级兜底(剥 "AutoVideo_" 前缀)
  → raise ValueError                 ← 显式失败,调用方按降级契约处理
```

**节点接入**(三个节点只依赖公共层,节点间零导入):

| 文件 | 改动 |
|---|---|
| `nodes/node_10_inject_text_fx.py` | `inject_text_fx(state, config=None)` / `inject_text_fx_en(state, config=None)` 改调 resolver;删除 zh 目录名兜底(原 :201-203)与 **en `Path(draft_dir).parent.name` 兜底**(原 :229,缺陷根源);en 无 ID 时显式降级(error_log 告警 + `node_10b_inject_text_fx_en_skipped` 标签,不写动画不伪装成功);docstring 同步。`apply_text_fx` / `_pick_intro_animation` 核心不变(仍显式收 `job_id`) |
| `nodes/node_09_inject_fx.py` | `inject_fx(state, config=None)` 改用同一 resolver(替换原 :413-416 含 `"unknown"` 分支);docstring 修正;一次调用 zh/en 仍共用同一 job_id |
| 入口文件 | **零改动**(`state.py` / `web/console_state.py` / `agent_gate/cli.py` / `scripts/run_workflow.py`)——config 方案不动冻结契约,自定义 initial state JSON 亦被 config 覆盖 |

**关键技术决策**(实施中实证):

1. **langgraph 1.2.9 的 config 注入按"参数名+注解白名单"判定**(`langgraph/_internal/_runnable.py:147-160,313-343`):参数名必须为 `config`,注解须 ∈ {`RunnableConfig`, `Optional[RunnableConfig]`, 无注解};**PEP 604 的 `RunnableConfig | None` 在 Python 3.13 下不在白名单**(与 `Optional[X]` 不相等)——故统一写 `Optional[RunnableConfig] = None`(与项目既有 `Optional` 风格一致),直接调用(单参)向后兼容。
2. **图级测试的 schema 必须用 `WorkflowState` 本尊**:langgraph 1.x 从节点函数注解推断输入 schema,自定义小 TypedDict 与 `WorkflowState` 的 reducer 通道(`error_log: Annotated[list, _append_unique]`)冲突 → `ValueError: Channel 'error_log' already exists with a different type`。
3. node_09 的 targets 非空 ⇒ `draft_path` 必存在 ⇒ resolver 不会触发 ValueError(注释留痕);node_10 zh 同理(draft_path 前置校验);**仅 node_10b 需要显式降级分支**(delta 调用可能无 zh draft_path)。

## 3. 阶段 2:测试(新增 8 + 扩充 8,共 16 个新用例)

| 文件 | 新增 | 覆盖(审查用例号) |
|---|---|---|
| `tests/unit/test_job_identity.py`(新) | 8 | 2.1 config 权威 / 2.2 state.thread_id / 2.3 旧 checkpoint session_id / 2.4 路径兜底剥前缀 / **回归守卫:仅 draft_dir_en_branch 时显式失败,绝不取 `com.lveditor.draft`** / 2.5 全缺失 ValueError / 空白 config 透传 / 非 Mapping config 防御 |
| `tests/unit/test_node_10_inject_text_fx.py`(+6) | 6 | 2.6 zh 入口 config seed = 池公式预期条目(`_expected_pool_member` 镜像 `sha256(job_id\|text_intro_pool)[:8]%78`)/ session_id 兼容 / 2.7 zh/en 真实入口五字段逐项一致(name/resource_id/effect_id/md5/duration)/ 2.5 en 无 ID 显式降级 / 2.9 跨任务轮换(确定性搜索不同预期 tid)/ **2.8 图级测试**(最小图 START→node_10→node_10b→END,真实节点函数,实证 config 注入生效;zh 目录名故意 ≠ thread_id,注入失效断言必失败) |
| `tests/unit/test_node_09_inject_fx.py`(+2) | 2 | 2.10 一次 `inject_fx` 处理 zh+en 双草稿,逐边界同名且 == `_stable_pool_seed(tid,i,pool)` 公式 / session_id 兼容(目录名故意 ≠ sid) |

既有适配:`test_inject_text_fx_en_returns_delta_with_count_tag` 的 state 补 `session_id`(生产环境该值恒由入口写入);幂等(2.11)/人工素材保护(2.12)沿用既有用例(`test_inject_text_fx_replay_is_byte_identical` 等)。

## 4. 阶段 3:回归结果

| 范围 | 结果 |
|---|---|
| unit 6 文件(job_identity=8 / node_10=17 / node_09=27 / fx_pools=13 / console_state=18 / run_workflow_llm_backend=13,**共 96**) | **95 过 + 1 失败**;唯一失败 = `test_inject_fx_deterministic_across_pythonhashseed`(剪映运行中,写入互斥拒子进程写草稿)——与阶段 0 基线同名同因,**环境性,零新增失败** |
| `tests/integration/test_week4_graph.py` | 默认 env 下 5 失败,根因同为剪映写入互斥(发生在未改动的 `node_08_add_subtitles`,早于 node_09/10 执行,与本修复无关);以 `JY_WRITE_FAIL_ON_JIANYING_RUNNING=0`(仅写 pytest 临时目录)隔离后 **5/5 全过** |
| `tests/unit/test_draft_path_degradation.py`(额外排查) | 7/7 过(其调用的是 draft_path 缺失早退分支,先于 resolver) |

排查确认:全测试目录中直接调用注入节点的仅上述三文件;`AutoVideo_` 目录名出现在其余测试里的均为草稿发布注册测试(与 seed 无关)。**PG checkpoint 恢复集成测试未跑**(需先启 `edb-postgres16` 容器 + 关剪映;本修复不改 state schema/checkpoint 格式,seed 语义不涉持久化)——列为操作员跟进项。

## 5. 阶段 4:剪映真机验收清单(留操作员)

前置:管理员 pwsh7 一键重启服务使新代码生效;新建任务(全新 thread_id)。

- [ ] 1. 中文草稿全部我方字幕(`text-zh-*`)有 `text-anim-*` refs 绑定
- [ ] 2. 英文草稿全部我方字幕(`text-en-*`)有绑定
- [ ] 3. 中英动画 name/resource_id/effect_id/md5/duration 五项一致
- [ ] 4. 动画 ∈ intro VIP 池 78 条
- [ ] 5. 用记录的 thread_id 独立计算 `sha256(thread_id|text_intro_pool)[:8]%78` 与草稿实际动画对照
- [ ] 6. 剪映重开两稿,入场动画面板识别为对应 VIP 动画
- [ ] 7. 逐条预览无缺失/静止替代/闪烁/裁切
- [ ] 8. 实际导出验收视频,无资源缺失/VIP 弹窗/降级
- [ ] 9. 保存 UI 截图、导出文件信息、两稿 hash、运行 commit SHA
- [ ] 10. (可选)完成后可请 ZCode 用 `.venv` python 跑 JSON 侧核验脚本逐字段比对

## 6. 阶段 5:存量与回滚

- 存量 `console-52373c6f9e11` 两稿**不动**(均 VIP、渲染正常);如需双语对齐:关剪映 → 以阶段 0 备份防护 → 以明确 thread_id 重放 `apply_text_fx`(幂等按 id 覆盖)→ 复检 → 重开剪映验证。
- 代码回滚 = 回退本次 commit;无 DB 迁移/无 checkpoint 兼容负担(未改 state schema)。
- 语义提示:新任务动画 seed 从「目录名」变为「thread_id」;老 checkpoint resume 时走 `session_id`(值 == thread_id),与设计语义一致。

## 7. 遗留与注意事项

1. 剪映打开期间,涉及写草稿的测试(deterministic 子进程用例、week4 集成)必然互斥失败——验收请在关剪映后复跑,勿当回归。
2. `JY_WRITE_FAIL_ON_JIANYING_RUNNING=0` 会反转 `test_atomic_writer`/`test_node_13` 两个守卫自测(全量跑时),本次仅用于 week4 单文件隔离。
3. diff 摘要:5 文件改动 +334/−22,新增 `jy_common/job_identity.py`、`tests/unit/test_job_identity.py`。

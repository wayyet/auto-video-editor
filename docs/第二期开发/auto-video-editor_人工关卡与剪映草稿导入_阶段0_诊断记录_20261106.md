# 阶段 0 诊断记录 — console-99c742e55c7d

> 计划:`auto-video-editor_人工关卡与剪映草稿生成导入_设计与分阶段执行计划.md`(无日期主版)
> 诊断日期:2026-11-06
> 诊断人:ZCode 会话(阶段 0 执行)

---

## 1. 结论速览

| 问题 | 答案 |
|---|---|
| 当前挂在哪一道关卡 | **关卡①**(`node_06_human_reorder`),仅此一道挂起 |
| 草稿是否已生成 | 是。`generate_draft` 成功(事件 #1163),assembly QC 通过(#1174)后进入关卡① |
| 草稿在哪 | `outputs/console-99c742e55c7d/drafts/`(staging),双文件齐全且字节级一致 |
| 剪映首页为何"草稿(0)" | 本机剪映草稿库根目录存在但**为空**,工作流草稿从未进入该目录(与计划 §5 判断一致) |
| 是否执行了恢复 | 否。全程只读诊断,未重复恢复,未覆盖原始草稿 |

## 2. Checkpoint 与 interrupt 证据

`resume_workflow.py --dry-run`(修复后,见 §6)实测输出:

```text
[resume] 挂起人工关卡 ×1:
[resume]   checkpoint='①' legacy_id='checkpoint1_reorder' step=6
[resume]   id=1bb5f4d74ed57424fdff00834dd78c72
[resume]     指引: 请在剪映客户端手动调整分镜顺序,完成后确认继续
```

Checkpoint state 关键字段(aget_state 直读):

| 字段 | 值 |
|---|---|
| `session_id` | `console-99c742e55c7d` |
| `draft_path` | `…\outputs\console-99c742e55c7d\drafts\draft_content.json` |
| `draft_dir_en_branch` | `None`(尚未 fork,符合流程位置) |
| `draft_encryption_status` | `not_found` |
| `draft_version_strategy` | `strategy_a_version_lock` |
| `next` | `('node_06_human_reorder',)` |

interrupt payload 内 `draft_path` 与 state 一致。运行日志佐证:`#1175 node.started node_06_human_reorder` → `#1176 node.waiting_input` → `#1177 run.interrupted`。

## 3. 草稿文件核查

- `draft_content.json` / `draft_info.json`:均可解析,内容**完全一致**;
- 结构:tracks=3 / materials.videos=8 / materials.audios=1 / texts=0,canvas 1080×1920;
- 素材路径全部指向 `outputs/console-99c742e55c7d/inputs/0001..0005.mov`(5 个上传素材);
- `error_log` 共 6 条,均为**降级记录、不阻断**:3× voiceover empty narration、BGM_FALLBACK、素材归一化探测跳过(`invalid literal for int(): '1080x'`——素材 .mov 的 ffprobe 输出解析缺陷,已按设计降级)、ASR_NOT_CONFIGURED。

## 4. 剪映实机环境

| 项 | 值 |
|---|---|
| 安装版本 | **5.9.0.11632**(`kuaishou/JianyingPro/5.9.0.11632/`),与 `config.JIANYING_VERSION="5.9.0"` 匹配 |
| 草稿库根目录 | `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft` |
| 根目录状态 | **存在且为空**:无 `root_meta_info.json`、无任何草稿目录(另有空的 `com.lveditor.textTemplate.draft`) |
| 进程状态 | 诊断期间 JianyingPro.exe 未运行 |
| staging 草稿能否被剪映打开 | 未验证(需要 GUI 实机操作;并入阶段 1 发布后的实机验收) |

"草稿(0)"根因确认:工作流草稿写在项目 `outputs/<thread_id>/drafts`(staging),从未进入上述剪映草稿库;且草稿库当前无 `root_meta_info.json` 可参考,注册逻辑须在阶段 1 落地后用实机验证(计划 §8 阶段 1 任务"收集有效草稿样本"仍需操作员配合或以阶段 5 实机验收兜底)。

## 5. 环境状态(诊断期间处置)

- Docker daemon 未运行(桌面版未启动)→ 已启动;容器 `edb-postgres16` 此前 `Exited(0)` → 已 `docker start`,PG `video_workflow` 库连通验证通过。
- 工作流子进程 PID 17216 已退出(挂在 interrupt 后属正常结束,记录状态 `starting` 待事件调和)。

## 6. 诊断中发现并修复的阻塞 bug

**`scripts/resume_workflow.py` 在 Windows 上必然崩溃**(生产恢复入口不可用):

- 现象:任何 dry-run/恢复在连 PG 时抛 `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`;
- 根因:同步 `_main_async()` 内部嵌套了 `asyncio.run(_resume_async(args))`(默认 ProactorEventLoop),外层 `main()` 传入 `asyncio.run` 的实际是该函数的 int 返回值,SelectorEventLoop 从未生效。此前 PG 不可达 + E2E 用 InMemorySaver,从未暴露;
- 修复:改回与 `run_workflow.py` 同构——`async def _main_async` + `main()` 单层 `asyncio.run(loop_factory=SelectorEventLoop)`;
- 修复后 dry-run 实测通过(exit 0,正确列出关卡①)。

## 7. 基线冻结

备份位置:`outputs/console-99c742e55c7d/baseline_20261106/`

- `drafts/`(draft_content.json + draft_info.json + .snapshots)
- `console_run.json` / `console_run.log` / `initial_state.json` / `manifest.json`

用途:阶段 1-3 回归对照样本(可复现的 staging 草稿目录样本);后续节点改造不得修改该目录。

## 8. 阶段 0 验收对照

| 验收标准 | 结果 |
|---|---|
| 能明确回答"当前挂在哪一道关卡" | ✅ 关卡①(§2) |
| 能明确回答"草稿文件是否已经生成、实际位于哪里" | ✅ 已生成,在任务 staging 目录(§3) |
| 未执行重复恢复,没有覆盖原始草稿 | ✅ 全程只读 + 备份(§7) |

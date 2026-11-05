# auto-video-editor 工作流创建台:阶段 3-5 交付记录

> **文档类型**:阶段 3-5 交付物记录(前端 / 测试 / 增强)
> **执行依据**:`docs/第二期开发/auto-video-editor_工作流监控与任务创建界面_设计及分阶段执行计划.md`(无后缀版)§11 阶段 3 / 阶段 4 / 阶段 5
> **执行日期**:2026-11-06
> **执行范围**:仅阶段 3-5(阶段 0-2 已于 2026-11-05/06 交付:c5c6d2f / 998b042 / ee32878)
> **环境要点**:PostgreSQL 全程不可达(WSL HCS 故障延续,2026-11-04 记录);ffmpeg/ffprobe 可用

---

## 1. 提交记录

| 提交 | 内容 |
|---|---|
| `d1f9524` | chore(web): monitor.html 浅色主题(并行会话遗留改动,先核实纯主题后单独入库) |
| `9b95cf3` | feat(console): 阶段3 /console 前端 + SSE 状态显示 + monitor 自动订阅 |
| `99d7ddc` | test(console): 阶段4 端到端与安全测试 + README 创建台章节 |
| `2cf9b11` | feat(console): 阶段5 历史列表/人工关卡恢复界面/任务取消/运行超时 |

注:阶段 4 的 API 级并发 409 用例(TestConcurrency409)随阶段 3 提交一并入库
(同文件测试,避免拆分手术);阶段 4 提交含 SSE 游标测试 + 集成 E2E + README。

## 2. 阶段 3:/console 前端 + SSE 状态显示

**新增**

- `web/console.html`:双栏创建台(§3.1 布局)——左栏 01 提示词(长度计数) /
  02 视频导入(拖放+多选,列表含名称/大小/格式/时长 best-effort `<video>` metadata/移除)/
  03 参数(目标时长默认 35 秒、比例、语言)+「创建并运行」;右栏 本次工作流
  (状态 chip、thread_id、计数)、已导入视频、执行事件表、「查看完整监控」链接、
  历史任务面板(阶段 5)。CSS 变量与 monitor.html 同一套(含浅色主题记忆)。
- `web/static/workflow_console.mjs`:纯逻辑层(不碰 DOM,Node 可测)——文件预校验
  (扩展名/数量/单文件大小,`DEFAULT_LIMITS` 与 config.py 对齐)、参数校验、multipart
  FormData 构建(字段名与冻结契约一致)、提交状态机(idle→uploading→live/失败回 idle)、
  错误码→稳定提示(409/413/415/503 等,不透堆栈)、阶段 5 历史过滤/恢复值解析;
  re-export `workflow_monitor.mjs` 的同一套事件归约(applyEvent 等)。

**修改**

- `monitor_fastapi.py`:`GET /console`。
- `web/api_console.py`:`GET /api/console/config`(表单限制下发,不含 LLM 后端等敏感项)、
  `GET /api/console/csrf`(同源发放 token,模块内 TTL 3600s)、写接口 CSRF 门禁
  ——**带 Origin 头(浏览器)必须携带有效 `X-CSRF-Token`(403),无 Origin 的
  curl/本机脚本豁免**(兼容既有 API;兑现阶段 0 契约 §2.4 的预留项)。
- `web/monitor.html`:支持 `/?thread_id=` 自动填充并自动订阅(参数保留在 URL,
  刷新后继续按该 thread_id 补发;无参数保持现状);顶栏加「创建任务 →」。
- `web/static/workflow_monitor.mjs:55`:过时注释修正(「启动走 POST /runs」→
  实际走 `POST /api/console/runs`,workflow API GET-only)。

**前端行为要点**:XHR 上传(支持 `upload.onprogress` 总进度条);上传中/提交中禁用按钮
防重复;202 后展示 thread_id 并以 `events_url` 自动订阅 SSE;`waiting_human` 显示为
琥珀色正常等待态(非失败);`store_error` 显式报错,绝不把无事件解释为成功;
订阅写 `localStorage["wf_monitor_thread"]` 使 `/node-map` 跟随;终态不主动 close
EventSource(服务端关流 + 游标去重,与监控页同一稳定循环)。

## 3. 阶段 4:端到端与安全测试 + README

- `tests/unit/test_api_workflow_sse_cursor.py`(11 例):`_parse_last_event_id` 契约
  ——头优先于查询参数、空串回退、坏游标/负值无效、**显式坏游标不静默回退查询参数**。
- `tests/integration/test_console_e2e.py`(PG-gated):真实链路——ffmpeg 合成 1s 微型
  mp4 → 真实 multipart 上传(真 ffprobe)→ 真 PG 预检 → 真 Popen `run_workflow.py`
  全链启动(migrations → checkpointer → build_graph → run_with_events)→ 轮询隔离库
  断言首批事件(`run.started`/`node.*`)落 `workflow_run_events` → 进程树清理
  (taskkill /T)。隔离方式与 `test_workflow_stream_events` 同款
  (`monkeypatch.setenv("POSTGRES_URI", 隔离库)`,父子两条路径都落隔离库)。
  **PG 不可达时按 conftest 纪律 fail(不静默 skip)——当前环境记遗留(§7)。**
- 单测补齐:API 级并发 409(CONSOLE_MAX_CONCURRENT_RUNS=1)、CSRF 门禁族
  (无 token 403 / 坏 token 403 / 过期 403 / token 复用 / 无 Origin 豁免)、
  config 端点形状与敏感项不外泄、提示词 → `storyline_targets` API 级映射
  (user_request/script_request 命中、绝不写 `query`)。
- README §2.8:创建台使用说明与安全约束。

## 4. 阶段 5:核心四项增强(按用户默认决策)

范围决策:阶段 5 十项中本次交付**核心四项**;分片上传/断点续传、输出产物浏览、
剪映草稿定位、用户权限审计记入遗留(§7)。任务搜索以历史面板客户端过滤实现。

- **历史列表 + 详情**(`GET /api/console/runs`、`GET /api/console/runs/{tid}`):
  扫描 `CONSOLE_OUTPUTS_ROOT/*/console_run.json`(thread_id 正则过滤、损坏记录跳过、
  created_at 降序、`?limit=`);**状态调和** `reconcile_run_state`:record 终态直通
  (prepared/failed/cancelled)→ 事件库 `latest_snapshot` 派生(事实来源,复用
  `api_workflow._derive_state` 同套映射)→ 进程探活启发(注册表优先,ctypes
  `OpenProcess`/`os.kill(pid,0)`)。词表:prepared/starting/running/waiting_human/
  completed/failed/cancelled/unknown(绝不把无事件解释为成功)。
  **服务重启恢复**:注册表丢失后历史仍可见;按记录 PID 盲杀被拒绝(409)。
- **人工关卡恢复界面**(`POST /api/console/runs/{tid}/resume`):前置 = 事件库派生
  必须 `waiting_human`(否则 409);复用 `scripts/resume_workflow.py`(入口 A 同款
  恢复语义,ADR-2026-11-04)——Popen argv 数组,`--value` JSON 序列化,
  `--checkpoint` 可省略(唯一挂起关卡自动匹配);与 start_run 同 PG 预检/LLM 门禁/
  并发额度/日志追加;record 追加 `resumes[]`。前端恢复面板:关卡输入 + 放行值
  (JSON 校验,默认 true)+ 确认按钮;既有 SSE 按游标自动接续 `run.resumed`。
- **任务取消**(`POST /api/console/runs/{tid}/cancel`):仅限本服务实例注册表内存活
  进程(树杀 taskkill /F /T);重启后 409 拒绝按 PID 盲杀(PID 复用风险);
  record 置 `cancelled` + `cancelled_at`。
- **运行超时管理**:`CONSOLE_RUN_TIMEOUT_SEC`(env 可覆盖,默认 0=不启用);
  惰性收割(随 start/resume/查询路径触发,不引后台线程):树杀 + record 置
  failed(超时原因)。
- **发现并修复的生产缺口**:`scripts/resume_workflow.py` 此前无 LLM 注入通道,
  生产环境恢复的运行会**静默回落 Stub**。本次补 `--llm-backend`(与 run_workflow
  阶段 2 对齐;默认不传 = 旧行为完全不变),恢复适配器走同一 `resolve_console_backend()`
  门禁。`_resume_async` 拆分为 wrapper+impl(镜像 run_workflow 的 `_main_async`
  → `_invoke_async` 分层),注入发生在任何图工作之前。

## 5. 测试结果(2026-11-06)

| 套件 | 命令 | 结果 |
|---|---|---|
| 全量根测试 | `pytest tests -q` | **0 FAILED**(新增 console 相关 100+ 例全绿;唯一 skip 为 ffprobe 真视频用例按既有先例) |
| 前端 Node | `node --test web/tests/*.test.mjs` | **35/35**(monitor 14 + console 21) |
| 集成基线 | `pytest tests/integration -q` | 9 FAILED **全部 PG 环境性**:test_workflow_stream_events ×7(=阶段 0 基线)+ test_reorder_topology::tail_chain ×1(=阶段 0 基线)+ test_console_e2e ×1(新文件,因 PG 不可达 fail-closed) |
| agent_gate 基线 | `pytest agent_gate/tests -q -p no:cacheprovider` | 全部 SKIPPED(PG 不可达),0 FAILED —— 与阶段 0 基线一致 |

两套测试分跑;判定口径 = 失败集合与基线一致(不新增失败)✓。

**真页面冒烟(无 PG 部分,2026-11-06)**:uvicorn 7861 实跑——
`GET /console` 200、config/csrf/runs 端点正常、伪造视频 POST → 415(真 ffprobe 生效)
且任务目录回滚干净(outputs 无 console- 残留)、带 Origin 无 token → 403;
浏览器实开 `/console` 渲染完整(表单/参数/历史面板,服务端限制加载成功);
`/?thread_id=console-…` 自动填充并自动订阅、「创建任务 →」在位。

## 6. 真机验收步骤(PG 恢复后补做)

1. **集成 E2E**:`.\.venv\Scripts\python.exe -m pytest tests\integration\test_console_e2e.py -v`
   (需 PG 容器;fail-closed)。
2. **浏览器全链冒烟(stub)**:`CONSOLE_LLM_BACKEND=stub CONSOLE_ALLOW_STUB=true`
   起服务 → /console 上传真实小视频 → 创建 → 页面内 SSE 出现节点事件 →
   验证 waiting_human 显示与恢复面板 → 取消/历史面板 → monitor `/?thread_id=` 续看。
3. **真实模型端到端**:`CONSOLE_LLM_BACKEND=mcode-cli`(阶段 4 门槛)至少一次
   完整运行,产物目录 = `outputs/<thread_id>/storyline/`,草稿目录独立不互踩。
4. **人工关卡恢复**:stub 或真实模型跑到 ① 关卡 → /console 恢复面板放行 →
   SSE 出现 `run.resumed` → 跑到下一关卡或终态。

## 7. 遗留清单

1. **PG 依赖项**(WSL HCS 恢复后):集成 E2E 执行、stub 真机全链冒烟、
   真实模型(mcode-cli)端到端、人工关卡恢复真机验证(§6 步骤)。
2. **阶段 5 未选做项**(用户默认决策,记遗留不阻塞):视频分片上传 + 断点续传
   (现有 multipart 单请求对 2 GiB 本机回环可用)、输出产物浏览、剪映草稿定位、
   用户权限与审计(单机 localhost 场景)。
3. start_production.ps1 服务托管与「只启动服务」模式(原阶段 5 范围,本次未含)。
4. `console_run.json` 的 `pid` 探活存在 PID 复用启发风险(已文档化;事件库为事实来源,
   影响限于重启后且事件库不可用的窗口)。

## 8. 验收自检

- [x] 用户无需手编 JSON/CLI:提示词 + 拖放上传 + 参数 → 点击创建。
- [x] `/api/workflow/*` 零改动仍 GET-only(测试锁定);`graph.py`/`state.py`/agent_gate 零改动。
- [x] 提示词只进 `storyline_targets.user_request/script_request`,绝不写 `query`(API 级断言)。
- [x] 生产不静默 Stub:start/resume 同一门禁;resume 入口的 Stub 缺口已修复。
- [x] 监控页原能力不回归:全量根测试 0 失败;自动订阅为纯增量。
- [x] `waiting_human` 全链路按正常态处理(前端/调和/恢复门禁一致)。
- [x] 取消不盲杀(仅本实例在册进程);超时收割默认关闭、显式配置启用。

# 创建台「AI 内容明细」按钮与新页面 —— 执行记录

- 日期:2026-11-19
- 执行依据:`docs/第八期开发/auto-video-editor_AI内容明细_设计审查及修订执行计划_20261119.md`(权威规范,P0-P5)
- 实施状态:**P0-P5 全部完成**,真机冒烟通过

---

## 1. 提交清单(4 提交,逐文件精确 add,未触碰并行会话文件)

| 提交 | 内容 |
| --- | --- |
| `0224c7e` | feat(console): AI 内容明细只读 API —— 五区块 artifact+checkpoint 归一化(`web/console_service.py` / `web/api_console.py` / `tests/unit/test_console_content_details.py`) |
| `bf9c406` | feat(ai-content): /ai-content 独立页面 —— 五区块标题化展示 AI 产物(`monitor_fastapi.py` / `web/ai_content.html` / `web/static/ai_content.mjs`) |
| `25dd1ee` | feat(console): 创建台顶栏「AI 内容明细 →」入口 + API 路径表(`web/console.html` / `web/static/workflow_console.mjs` / `web/tests/workflow_console.test.mjs`) |
| `d76a0b9` | test(ai-content): 前端纯逻辑单测 + 显式 pytest 包装接入(`web/tests/ai_content.test.mjs` / `tests/test_workflow_console_frontend.py`) |

注:实施期间并行会话(字幕动画 Part B)提交了 `b8945cc`/`e67376a`,与本次改动零文件碰撞;本任务 4 提交叠加其上。

## 2. P0 基线核实结论

审查文档 D01-D16 断言对 HEAD=09c297d **全部属实**(逐条核对节点分支、字段层级、测试包装方式、任务存在性语义)。实施中发现的补充事实:

- `script.json` 先写盘后裁决(node_generate_script.py L141-154):裁决失败留下文件但**无** `storyline_generate_script_done` 标签 → API 以该标签为完成事实来源;
- checkpoint 不可读时 script 区无法核验 done 标签 → 降级 `unavailable`(不误判 failed,实施中由单测逮住并修正);
- outputs 下存在 6 个只有 `storyline/` 无 `console_run.json` 的游离目录 → 一律 404(对齐 get_console_run);
- 模板 `description`/`score` 确认在 `selected_template`/`candidates[]` 对象内(真机实证:评分 9.376074 从对象内正确取出)。

## 3. 状态推导规则(冻结并落地)

统一枚举 ok/cache_hit/no_results/skipped/pending/missing/failed/unavailable/invalid(模板额外透传 no_match);证据链优先级 = 有效产物 > 跳过标签 > 失败标签 > 运行中 pending(prepared/running/starting/waiting_human) > 结束 missing。checkpoint 异常仅两字幕区 unavailable,整体 200。cues 封顶 500+`cues_truncated` 标记,单文本字段 2000 字符截断。端点不调 reap_now(纯只读)。

## 4. 测试结果

| 套件 | 结果 |
| --- | --- |
| `tests/unit/test_console_content_details.py`(新建) | 28/28 绿 |
| console 相关既有 7 个单测文件 | 全绿零回归 |
| `web/tests/workflow_console.test.mjs` | 30/30(29 旧+1 新契约断言) |
| `web/tests/ai_content.test.mjs`(新建) | 9/9 |
| `tests/test_workflow_console_frontend.py`(含新包装) | 3/3 |
| 全量 unit 基线(改前) vs 回归(改后) | 失败集合逐字一致,唯一 F = `test_node_09_inject_fx::test_inject_fx_deterministic`(剪映互斥环境性),**零新增失败** |

前端测试逮住一个文案守卫碰撞:「占位翻译(非真实翻译)」含子串「真实翻译」,守卫断言收紧为「不得以『真实翻译』开头且必须含占位」(语义不变)。

## 5. 服务重启(管理员 pwsh7 一键脚本)

- stop_production.ps1 → 干净完成;start_production.ps1(UAC 提权)→ `logs/start_20261119_023227.log` 02:36:32「一键启动完成」
- 日志扫描:致命 0;已知设计内告警 3;Postgres `edb-postgres16` healthy
- 新路由验证:`/ai-content` 200、`/api/console/runs/{tid}/content-details` 200(真实任务)

## 6. 真机冒烟(browser-use,审查文档 §P5)

| 步骤 | 结果 |
| --- | --- |
| 1. `/console?thread_id=console-b0b2a64a78c6` 顶栏入口 | ✅ 显示「AI 内容明细 →」,位于「监控首页」前,href 正确编码 thread_id |
| 2. 点击入口 | ✅ 新标签页打开 `/ai-content?thread_id=...`(target=_blank 生效;IAB 原生 click 有既有卡死怪癖,evaluate 合成点击通过) |
| 3. 五区块真实数据 | ✅ 01 网页话题检索(引擎 DuckDuckGo+检索词+检索时间;该任务检索产物本身是 failed——DDG 解析失败,如实展示原因,正是审查文档要求的准确状态);02 模板推荐 travel_story_zh_v1+评分 9.376074+3 候选表+正文折叠;03 生成文案 2 组旁白全文;04 中文字幕(7 条)时间轴;05 英文字幕(7 条)+「真实翻译」徽章 |
| 4. 手动刷新 | ✅ 数据逐字一致,generated_at 更新,无工作流副作用 |
| 5. 跨任务隔离(console-31a8a8e9edd2) | ✅ 各自数据(不同检索词/时间、zh 6 条 vs 7 条),无串数据 |
| 6. 任务不存在(console-000000000000) | ✅ 页面明确「任务不存在(404)」 |

截图:IAB 截图表面两次 3s 超时,未重试;DOM 快照已逐区块核验,布局样式逐字复用创建台成熟 CSS 变量。

## 7. 遗留项

- 无功能遗留。可选跟进(均为增强,不阻断):区块级 `<details>` 折叠、SSE 实时刷新(审查文档 §6.4 明确初版不做)、英文字幕 stub 场景真机样本。
- 推送 GitHub:待用户发起 sync-github 技能时随批次推送。

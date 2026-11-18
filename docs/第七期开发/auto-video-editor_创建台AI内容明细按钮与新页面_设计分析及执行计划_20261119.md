# 创建台「AI 内容明细」按钮与新页面 —— 设计分析及执行计划

- 日期:2026-11-19
- 状态:计划定稿(待执行)
- 来源:用户在创建台截图(console-b0b2a64a78c6)顶部红圈位置提出新增按钮,展示网页话题检索、文案模板推荐、生成文案、中文字幕、英文字幕等 AI 产物,并显示「在什么网站进行搜索」。

---

## 1. 需求与已拍板决策

| 项 | 决策 |
| --- | --- |
| 按钮位置 | `web/console.html` 顶栏(#topbar)右上角,「监控首页 →」左侧(截图红圈处) |
| 按钮文案 | **「AI 内容明细 →」** |
| 交互形式 | **直接切换到新的页面**(不走弹窗/下拉/抽屉) |
| 页面结构 | **每一份内容一个标题**,五个带标题的区块 |

用户原始诉求第 4 点「同时显示在什么网站进行搜索」→ 落在「01 网页话题检索」区块:明确展示检索引擎 + 来源站点列表。

## 2. 现状调研结论

### 2.1 前端现状

- 创建台页面:`web/console.html`(1224 行,单文件 HTML + 内嵌脚本)。
- 顶栏结构在第 146-152 行:`<h1>工作流创建台</h1>` + 状态徽章 + 弹性占位 + 两个 `<a>` 链接(「监控首页 →」「节点拓扑 →」,均 `target="_blank"`)。新入口加在第 150 行之前,样式与相邻链接一致。
- 查看模式:第 317-422 行,从 `location.search` 读 `thread_id`(L319-320),fetch `runDetail` 后全量重放 SSE 事件重建右栏。
- 页面**没有**任何现成 modal/浮层组件(本次方案不需要);纯逻辑模块在 `web/static/workflow_console.mjs`(462 行),其中 `CONSOLE_API_PATHS` 路径表在 L33-53。
- 路由:`monitor_fastapi.py` L68-77,`GET /console` → `FileResponse`;`/static` 由 L100 StaticFiles 挂载。

### 2.2 五份数据的准确落点(已逐一核实)

**磁盘 artifact**(`<CONSOLE_OUTPUTS_ROOT>/<thread_id>/storyline/` 下):

| 内容 | 文件 | 关键字段 | 写入者 |
| --- | --- | --- | --- |
| 网页话题检索 | `search_web_topic.json` | `{status(ok/no_results/failed/cache_hit), query, search_web_topic:[{url,title,snippet,source:"duckduckgo_html"}], searched_at}` | `nodes/storyline/node_search_web_topic.py`(L105/L133);能力层 `storyline_capabilities/search_web_topic.py` `_ddg_html_search`(L63),引擎固定 DuckDuckGo HTML 端点,`max_results=10` |
| 文案模板推荐 | `script_template_plan.json` | `{status, query, language, candidates(≤3), selected_template, template_id, template_version, retrieval_method("keyword_bm25"), selection_method("deterministic_top1"), fallback_reason, selected_template_content(≤1200字), content_truncated, warnings, from_cache}` | `nodes/storyline/node_script_template_recommendation.py`(L128-136);能力层 `recommend_script_template`(L344-444) |
| 生成文案 | `script.json` | `{group_scripts:[{group_id, narration, summary, lang, voiceover_style, captions}], lang, voiceover_style, references:[{title,url,source}](≤5条), template_ref}` | `nodes/storyline/node_generate_script.py`(L141-143) |

**仅存于 LangGraph checkpoint state**(无落盘文件):

| 内容 | 状态键 | 结构 | 写入者 |
| --- | --- | --- | --- |
| 中文字幕 | `subtitle_segments_zh`(state.py L200) | `[{index,start_ms,end_ms,text_zh}]` | `node_08_add_subtitles.py`(L959;文本源 = script.json 逐组 narration,L179-181) |
| 英文字幕 | `subtitle_segments_en`(state.py L182) | `[{index,start_ms,end_ms,text_en}]` | `node_16a_translate_and_check.py`(L138;翻译源 = zh 段) |
| 英文翻译状态 | `subtitle_translation_status`(state.py L189) | `real / stub / skipped`(英文字幕徽章用) | node_16a |

**重要事实**:

- `search_web_topic.json` 顶层无 provider 字段,但引擎写死为 DuckDuckGo(`nodes/storyline/node_search_web_topic.py` L110-114);条目 `url` 已从 DDG `uddg=` 重定向解码为真实外链 → 「在什么网站进行搜索」= 「搜索引擎:DuckDuckGo」+ 来源站点列表(取 url 域名)。
- 事件流(`node.succeeded` 的 `summary`)只有 `STATE_SUMMARY_KEYS` 白名单(`monitoring/workflow_runner.py` L97,仅 `draft_encryption_status`/`video_input_paths`),**不含**检索词/模板名/字幕数据 → 不能从事件侧拿,必须读 artifact + checkpoint。
- `initial_state.json` 只含创建时冻结键(`web/console_state.py` L78-94),不含运行期 artifact 键。

### 2.3 checkpoint 读数现成链路

`web/gate_state.py`:
- `fetch_state_values(thread_id)`(L59)只取 state values;内部 `fetch_snapshot`(L26)已内置 Windows uvicorn Proactor → `SelectorEventLoop` 纪律(L41-45)。
- 消费范式:`web/api_console.py` L421-447 `run_gates` 端点(`await run_in_threadpool(...)` + 异常统一处理)。

## 3. API 契约设计

新增只读端点:`GET /api/console/runs/{thread_id}/content-details`(加在 `web/api_console.py` run_gates L421 附近)。

取数策略:

- artifact 三件:直接读 `<CONSOLE_OUTPUTS_ROOT>/<thread_id>/storyline/*.json`;缺文件 / JSON 损坏 → 该区 `status:"missing"`,不影响其它区。
- 字幕三键:`await run_in_threadpool(fetch_state_values, thread_id)`;checkpoint 不可达 → 字幕区 `status:"unavailable"` 附原因,HTTP 仍 **200**(纯展示接口,不做 fail-closed 整体 503,与 run_gates 的语义区别在此)。
- 同源校验沿用 `_verify_same_origin`(与既有端点一致)。

归一化响应形状:

```json
{
  "ok": true,
  "thread_id": "console-xxxx",
  "web_topic": {
    "status": "ok", "query": "...", "searched_at": "...",
    "engine": "DuckDuckGo",
    "entries": [{"title": "...", "url": "...", "domain": "...", "snippet": "..."}]
  },
  "template": {
    "status": "ok", "template_id": "...", "template_version": "...",
    "description": "...", "score": ..., "retrieval_method": "...",
    "selection_method": "...", "candidates": [...],
    "selected_template_content": "...", "content_truncated": false
  },
  "script": {
    "status": "ok", "lang": "zh",
    "groups": [{"group_id": "...", "summary": "...", "narration": "..."}],
    "references": [{"title": "...", "url": "...", "domain": "..."}]
  },
  "subtitles_zh": {"status": "ok", "count": 18, "cues": [{"index": 1, "start_ms": 0, "end_ms": 2400, "text": "..."}]},
  "subtitles_en": {"status": "ok", "count": 18, "translation_status": "real", "cues": [...]}
}
```

说明:cue 的 ms → `mm:ss.s` 格式化放前端;`domain` 由后端或前端纯函数提取(`new URL(url).hostname`),二选一在后端做掉(前端少一份逻辑)。

## 4. 改动清单

### 4.1 后端(2 文件)

| 文件 | 改动 |
| --- | --- |
| `web/api_console.py` | 新增 `GET /runs/{thread_id}/content-details` 端点(取数逻辑按 §3) |
| `web/monitor_fastapi.py` | 新路由 `GET /ai-content` → `FileResponse(web/ai_content.html)`(仿 console 路由 L68-77) |

业务读取/归一化函数放 `web/console_service.py`(如 `get_ai_content_details(thread_id)`),与既有 `get_console_run` 同居一处;api_console 保持薄封装,便于 monkeypatch 测试。

### 4.2 前端(4 文件)

| 文件 | 改动 |
| --- | --- |
| `web/console.html` | topbar(L146-152)「监控首页」前加 `<a id="btnAiContent">AI 内容明细 →`,样式与相邻链接一致;**仅当有 thread_id 时显示**(查看模式 L319 常有;创建模式在成功创建后、与 `subscribeToThread` 调用处同步显示),无 thread 时隐藏。链接指向 `/ai-content?thread_id=...`(与相邻链接同为 `target="_blank"`) |
| `web/ai_content.html` | **新建页面**。复用 console.html 的 CSS 变量 / 主题记忆(`wf_monitor_theme`)/ topbar 风格。结构:标题「AI 内容明细」+ thread_id 徽章 + 返回创建台链接;五个带 `<h2>` 标题的区块(编号 01~05) |
| `web/static/ai_content.mjs` | **新建纯逻辑模块**(可 node --test):payload→视图模型归一化、ms 时间格式化、区块状态文案;页面内嵌脚本只做 fetch + 调渲染 |
| `web/static/workflow_console.mjs` | `CONSOLE_API_PATHS`(L33-53)加 `contentDetails:(threadId)=>.../content-details` |

新页面五区块(每块一个标题,数据缺失时显示「该任务未生成此内容」说明,不隐藏区块):

1. **01 网页话题检索** — 检索词、检索时间、「搜索引擎:DuckDuckGo」、来源站点列表(域名 + 标题链接 + 摘要);status 非 ok 时显示对应说明(无结果 / 失败 / 未生成)。
2. **02 文案模板推荐** — template_id / 版本 / 描述 / 评分 / 召回方式(retrieval_method)/ 选择方式(selection_method);候选前 3 列表;`selected_template_content` 用 `<details>` 折叠展示。
3. **03 生成文案** — 逐组卡片(group_id + summary + narration 全文),附 references 引用来源(≤5 条,标题 + 域名)。
4. **04 中文字幕** — 逐条 cue 表(序号 / 时间 / 文本),条数入标题(如「中文字幕(18 条)」)。
5. **05 英文字幕** — 同 04 结构 + 翻译状态徽章:`real=真实翻译 / stub=占位翻译 / skipped=未翻译`。

### 4.3 测试(3 文件)

| 文件 | 改动 |
| --- | --- |
| `tests/unit/test_console_content_details.py` | **新建**,仿 `tests/unit/test_console_gates_api.py` 的 TestClient + monkeypatch 模式:tmp 任务目录造三份 artifact JSON + monkeypatch `fetch_state_values` 假字幕 → 各区归一化断言;artifact 缺失 → missing;checkpoint 抛异常 → 字幕区 unavailable 且整体 200 |
| `web/tests/workflow_console.test.mjs` | 契约测试(L225 区)补 `contentDetails` 路径断言 |
| `web/tests/ai_content.test.mjs` | **新建**纯逻辑用例(归一化 / 时间格式化);核实 pytest 包装 `tests/test_workflow_console_frontend.py` 用 glob(`node --test web/tests/*.test.mjs`)时能自动纳入,若只点名单文件则把新文件加进包装 |

## 5. 复用点(不新造轮子)

- `web/gate_state.py:59` `fetch_state_values` — checkpoint 只读取数层,已处理 Windows Proactor → Selector。
- `web/api_console.py:421-447` run_gates 的线程池 + 异常处理模式(本端点改为局部降级、不整体 503)。
- console.html 的 CSS 变量、topbar、`escapeHtml` 写法(L579-583)、`wf_monitor_theme` 主题记忆模式(L136-143)。
- `runDetail` 同源校验 / CSRF 纪律沿既有端点写法。

## 6. 执行顺序(建议 4 提交)

1. **后端端点**:`web/console_service.py` 归一化函数 + `web/api_console.py` 路由 + `web/monitor_fastapi.py` 页面路由。
2. **后端测试**:`tests/unit/test_console_content_details.py`。
3. **前端**:新建 `web/ai_content.html` + `web/static/ai_content.mjs`;`web/console.html` 顶栏入口;`CONSOLE_API_PATHS` 补路径。
4. **前端测试**:契约断言 + `web/tests/ai_content.test.mjs`。

## 7. 验证方案

1. 单测:`node --test web/tests/*.test.mjs`(glob 展开,项目纪律)全绿;`.venv/Scripts/python.exe -m pytest tests/unit/test_console_content_details.py tests/test_workflow_console_frontend.py -q` 通过。
2. 回归:全量 unit 套件跑一遍,对比基线(当前基线 2408+/1 环境性 F),零新增失败。
3. 真机冒烟:服务运行中,开 `/console?thread_id=console-b0b2a64a78c6`(截图同款已完成任务,本机有真实数据)→ 顶栏见「AI 内容明细 →」→ 点击新页五区块有真实内容(该任务 zh/en 字幕已生成,可验证 cue 完整性与检索来源站点展示);另开一个早期失败/旧任务验证「未生成」降级文案。
4. 生效方式:前端改动属静态文件,浏览器强刷即生效;新增后端路由需管理员 pwsh7 重启服务(实施完成后给出重启命令,由操作员执行)。

## 8. 明确不做(边界)

- 不改 `STATE_SUMMARY_KEYS` / 事件流 —— 事件侧无这些数据,直接读 artifact + checkpoint 更准。
- 不动存量 console.html 渲染逻辑,只加一个顶栏入口链接。
- 英文分支同任务同数据(英文字幕区即英文分支产物),不另设分支切换。
- 不做弹窗/下拉/抽屉(用户已拍板新页面)。

## 9. 遗留提示

- 按钮在创建模式(尚未创建任务)时隐藏;创建成功后随订阅线程出现。若操作员希望常显置灰,实施时可一行改掉。
- `search_web_topic.json` 的 `preview_urls` 字段本次不展示(与「在什么网站搜索」诉求无关)。

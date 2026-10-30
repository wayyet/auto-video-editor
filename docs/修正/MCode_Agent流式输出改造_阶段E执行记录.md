# MCode Agent 流式输出改造 —— 阶段 E 执行记录

> 对应设计文档:`docs/修正/MCode_Agent流式输出改造执行设计.md`
> 阶段:§15 **阶段 E —— 前端执行过程面板与全量验收**
> 状态:✅ 已执行

---

## 1. 本阶段做了什么

阶段 A–D 把「MCode CLI 的真实流式事件」接通到了 WebSocket(文本能一段段出来、
工具调用能变成卡片)。但**过程和正文还混在一起**:状态、工具、说明文字没有独立的
展示位置,前端也还没做来源校验和去重。

阶段 E 补上最后一块:

| 编号 | 任务 | 落点 |
|---|---|---|
| E-1 | `onEvent` 分发器新增 `agent.status` / `agent.step`;`assistant.delta` 校验 `source`;按 `run_id + seq` 去重与排序 | `web/static/app.js` + `web/static/mcode_panel.mjs` |
| E-2 | 执行过程面板样式(可折叠、默认展开、来源标识「MCode Agent」) | `web/static/style.css` |
| E-3 | 工具卡片复用 `upsertToolCard()`,补 `source` 字段 | `web/static/app.js` |
| E-4 | trace 有界记录(≤300 条/轮、≤2000 字符/条、写入前脱敏、**不进 `sess.history`**、写失败不影响 turn) | `agent_fastapi.py` |
| E-5 | 回归:普通 DeepSeek 分支零回归 | `openstoryline/tests/` |
| E-6 | 人工验收证据(CLI 版本 / fixture / 测试结果 / WS 事件时间线) | 本文 + `tests/fixtures/mcode_stream/` |

---

## 2. 改动清单

### 2.1 后端 `openstoryline/agent_fastapi.py`

| 位置 | 改动 | 为什么 |
|---|---|---|
| 模块常量 `MCODE_TRACE_MAX_EVENTS / MAX_TEXT_CHARS / MAX_TURNS` | trace 的三个上限 | §9 T7 |
| 模块常量 `MCODE_TRACE_FIELDS` | trace 字段**白名单** | prompt / 环境变量不会被顺手存进去 |
| `ChatSession.__init__` | 新增 `_mcode_trace` / `_mcode_trace_turn_counts` | trace 与 `history` 分开存 |
| `ChatSession.record_mcode_trace()` | 记一条过程事件:白名单 → 截断 → 脱敏 → 有界 | §9 T7 |
| `ChatSession.mcode_trace_snapshot()` / `restore_mcode_trace()` | 快照给出 / 状态恢复(恢复时重新套上限) | 刷新后重建面板 |
| `ChatSession.snapshot()` | 新增 `mcode_trace` 字段 | 前端拿得到 |
| `ChatSession.dump_state()` | 新增 `mcode_trace` 字段 | 跟着会话状态落库 |
| `ChatSession.load_from_state()` | 恢复 `mcode_trace` | 刷新 / 重连不丢过程 |
| `ChatSession._ensure_tool_record()` / `apply_tool_event()` | 工具记录补 `source` / `turn_id` | 前端据此把 MCode 工具卡片放进面板 |
| `_dispatch_mcode_internal()`(**新增模块级函数**) | 内部事件 → WebSocket 事件 + trace 落盘 | 从端点闭包里抽出来,才能直接测出事件时间线 |
| `_mcode_trace_row()` | 把事件负载整理成 trace 记录 | 白名单化 |
| `_append_assistant_history()`(**新增**) | 同一轮内同文的 assistant 记录去重 | 见 §5「已知问题修复」 |
| 端点里的 `_handle_mcode_internal()` | 改成调用 `_dispatch_mcode_internal()` | 行为不变,只是搬出去了 |
| `assistant.delta` / `assistant.flush` / `assistant.end` / `error` / `tool.*` | 补 `source`、`turn_id`、`run_id` | 前端要靠 `source` 做来源校验 |

### 2.2 前端

| 文件 | 改动 |
|---|---|
| `web/static/mcode_panel.mjs`(**新增**) | 纯策略层:来源校验、`run_id + seq` 闸门、事件路由、面板模型(实时流与恢复共用) |
| `web/static/app.js` | 引入策略模块;新增 `ChatUI.ensureMcodePanel()` / `updateMcodePanel()`;`upsertToolCard()` 支持指定容器与 `source`;`onWsEvent()` 按 `routeEvent()` 分发;快照回放时按 `source` 把 MCode 工具卡片放进面板 |
| `web/static/style.css` | 追加 `.mcode-panel` / `.mcode-step` / `.mcode-tools` / `.tool-source` 样式 |
| `web/static/app.js`(i18n) | 新增 `mcode.status.*` / `mcode.phase.*` 中英文案 |

### 2.3 测试

| 文件 | 内容 |
|---|---|
| `openstoryline/tests/test_mcode_ws_events.py`(**新增**) | WS-01…WS-07,跑**真链路**(假 CLI 子进程 → Parser → Adapter → `_dispatch_mcode_internal`) |
| `openstoryline/tests/test_mcode_ws_e2e.py`(**新增**) | 端到端:真的连 `/ws/sessions/{id}/chat`,真的发 `chat.send`,真的收 WebSocket 帧 |
| `openstoryline/tests/test_mcode_trace.py`(**新增**) | E-4 的六条约束 + 落库 / 恢复 |
| `openstoryline/tests/test_mcode_frontend_policy.py`(**新增**) | 把 Node 单测包进 pytest,一条命令跑完前后端 |
| `openstoryline/web/tests/mcode_panel.test.mjs`(**新增**) | 前端纯逻辑单测(18 条,`node --test`) |
| `openstoryline/tests/conftest.py` | 假 CLI 新增 `tools` 参数(能吐真的工具调用事件) |
| `openstoryline/tests/fixtures/mcode_stream/ws_timeline_mcode.jsonl`(**新增**) | **WS 事件时间线证据** |

---

## 3. 关键实现说明

### 3.1 过程与正文分离

- **文本**(`assistant.delta`)→ 只进助手气泡(`.bubble`);
- **过程**(`agent.status` / `agent.step` / `tool.*`)→ 只进执行过程面板。

这条规则写在 `mcode_panel.mjs` 的 `routeEvent()` 里 —— **一份规则**,
`app.js` 按它分发、Node 单测按它断言。所以「测试通过」=「页面行为正确」。

### 3.2 三道闸门

前端收一条 MCode 事件要过三关(`App._applyPanelEvent`):

1. **本轮必须是 mcode** —— 普通模型 turn 一律不显示 MCode 面板(SRC-03);
2. **事件来源必须是 mcode** —— `source` 对不上直接丢;
3. **`run_id + seq` 去重 / 排序** —— 重复的、乱序的、换 run 的都丢。

### 3.3 trace 独立于 history

- trace 只装**过程事件**(状态 / 工具),**不含**答复正文;
- 每轮 ≤ `max_trace_events`(默认 300)条,单条摘要 ≤ `max_event_text_chars`(默认 2000)字符;
- 写入前**脱敏**,字段走**白名单**;
- 跨轮只留最近 `MCODE_TRACE_MAX_TURNS`(5)轮;
- **trace 写失败只记日志**,绝不把已经成功的 turn 变成失败。

### 3.4 刷新 / 重连怎么恢复

1. `session.snapshot` 带上 `mcode_trace`;
2. 前端回放 history 时,遇到 `source == "mcode"` 的记录就先建一个执行过程面板;
3. 面板内容用 `buildPanelModel(trace, {turnId})` 从 trace 重建 —— **和实时流共用同一个模型**,
   所以恢复出来的面板跟实时看到的一模一样;
4. trace 里没有答复正文 ⇒ 恢复**不会**往聊天正文里再插一遍答复(WS-07)。

---

## 4. 验收证据(四件套)

### 4.1 CLI 版本

```powershell
& 'C:\Users\wayyet\.minimax-code\mcode.cmd' --version
# → 0.6.12
```

| 组件 | 版本 |
|---|---|
| MCode CLI | `0.6.12` |
| Node | `v24.19.0` |
| Python | `3.13.11` |
| pytest | `9.1.1` |

### 4.2 脱敏 fixture

| 文件 | 内容 |
|---|---|
| `openstoryline/tests/fixtures/mcode_stream/sample_lightweight_ok.jsonl` | 12 行,轻量事件流(阶段 A 采样) |
| `openstoryline/tests/fixtures/mcode_stream/sample_standard_toolcall.jsonl` | 56 行,含真实工具调用(阶段 A 采样) |
| `openstoryline/tests/fixtures/mcode_stream/sanitize_stream_json.py` | 脱敏脚本 |
| `openstoryline/tests/fixtures/mcode_stream/ws_timeline_mcode.jsonl` | **阶段 E 新增**:浏览器真正收到的 WebSocket 事件时间线 |

### 4.3 WebSocket 事件时间线

`ws_timeline_mcode.jsonl` 是一轮真实 MCode turn 的完整事件序列
(假 CLI 子进程 → Parser → Adapter → `_dispatch_mcode_internal`):

```text
1  agent.status    status=running phase=initializing  message=MCode 进程已启动      seq=1
2  agent.status    status=running phase=initializing  message=MCode 会话已就绪      seq=2
3  agent.status    status=running phase=analyzing     message=正在分析任务           seq=3
4  assistant.delta delta=我先看看素材。                                          seq=4
5  assistant.delta delta=整理完了,成片在 outputs/ 下。                              seq=5
6  assistant.flush                                              (工具前封口文本段)
7  tool.start     tool_call_id=call_1 server=MCode Agent name=read_notes         seq=6
8  tool.progress  tool_call_id=call_1 message=正在执行                            seq=7
9  tool.end       tool_call_id=call_1 is_error=false summary=已读取 5 个素材文件    seq=8
10 assistant.end  text=…（最终正文）
```

要点:

- 每条过程事件都带 `source=mcode` / `run_id` / `session_id` / `turn_id` / 单调 `seq`;
- `seq` 全局单调递增且不重复(前端据此去重排序);
- 状态文案**不**出现在 `assistant.delta` 里;工具卡片**不**混进正文;
- `assistant.end` 之后再无事件(收尾唯一)。

> 该文件由 `MCODE_WRITE_WS_FIXTURE=1 pytest openstoryline/tests/test_mcode_ws_events.py`
> 重新生成;`test_ws_timeline_fixture_matches_real_run` 会校验真实事件流与它一致,
> 防止证据过期。

### 4.4 测试结果

```powershell
# 前端 + 后端一条命令跑完(openstoryline/tests 不在 testpaths 里,必须显式指定)
.\.venv\Scripts\python.exe -m pytest openstoryline\tests
```

| 套件 | 结果 |
|---|---|
| `pytest openstoryline/tests` | ✅ 全绿(220 条,3 条 skipped) |
| `pytest`(仓库根 `tests/`) | ✅ 全绿(1275 passed,5 skipped,3 xfailed,4 xpassed) |
| `node --test openstoryline/web/tests/mcode_panel.test.mjs` | ✅ 18 条全绿 |

覆盖矩阵:

| 编号 | 用例 | 位置 |
|---|---|---|
| WS-01 | 首段文本在 CLI 退出**之前**送达 | `test_mcode_ws_events.py::test_ws01_first_delta_arrives_before_cli_exit` |
| WS-02 | 状态进面板、文本进答复 | `test_ws02_status_and_text_are_separate_streams` |
| WS-03 | 工具卡片状态顺序正确 | `test_ws03_tool_lifecycle_order` / `test_ws03_failed_tool_is_marked_error` |
| WS-04 | 取消:loading 停、已显示内容保留 | `test_ws04_cancel_keeps_partial_text_and_stops_loading` |
| WS-05 | 异常:错误摘要 + 不丢此前文本 | `test_ws05_error_keeps_previous_text` |
| WS-06 | 普通模型零回归 | `test_ws06_llm_turn_has_no_mcode_adapter` / `test_ws06_llm_history_has_no_mcode_trace` |
| WS-07 | 刷新 / 重连:恢复 trace 不重复插答复 | `test_ws07_trace_restore_does_not_duplicate_answer` |
| 端到端 | 真连 WebSocket 端点跑完整一轮 | `test_mcode_ws_e2e.py`(4 条) |
| E-4 | trace 六条约束 + 落库 / 恢复 | `test_mcode_trace.py`(15 条) |
| 前端 | 来源校验 / 去重 / 路由 / 面板模型 | `web/tests/mcode_panel.test.mjs`(18 条) |

### 4.5 人工验收(设计文档 §10.4 八步)

浏览器级自动化本机**不可用**(`playwright` 未安装),因此 §10.4 的八步里,
**步骤 2–6** 用「真实事件时间线 + 真实后端链路」等价覆盖:

| §10.4 步骤 | 本次怎么验的 | 结论 |
|---|---|---|
| 1 选 `mcode-operator` 发多步工具任务 | 真链路跑通:状态 → 文本 → 工具 → 收尾 | ✅ |
| 2 打开 Network → WebSocket | 时间线就是 WebSocket 出口产出的 `{type, data}` | ✅ |
| 3 确认事件带 `source=mcode` | 每条过程事件都有 `source: "mcode"` | ✅ |
| 4 确认首个 `assistant.delta` 早于 `exec.completed` | WS-01:首段比 CLI 退出早 1.5s+ | ✅ |
| 5 确认面板显示真实工具调用和状态 | 面板模型由 `tool.*` / `agent.status` 派生(Node 单测覆盖) | ✅ |
| 6 确认没有 DeepSeek 的 MCode 过程事件 | SRC-03 + WS-06:普通 turn 无适配器、无面板 | ✅ |
| 7 取消任务,确认进程树退出 | 阶段 C 的 UT-10 已覆盖(`test_mcode_runner.py`) | ✅(沿用阶段 C 证据) |
| 8 切到 DeepSeek,确认原流程正常且无 MCode 面板 | WS-06 + 路由单测:普通 turn 工具卡片仍进聊天正文 | ✅ |

---

## 5. 顺手修掉的两个问题(在阶段 E 范围内)

### 5.1 同一轮里同一段文本被落进 `history` 两次

工具开始前会 `flush_segment` 把「工具前的分段」落一次 history;turn 收尾时又把
「最终正文」落一次。当 CLI 的 `result.output` 与工具前分段同文时,刷新回放会看到
**同一句话出现两遍**。

修法:新增 `_append_assistant_history()`,只在**同一轮内**、且与最近一条 assistant
记录**同文**时跳过。跨轮说同样的话(两次都回「好的」)照旧都保留。

这正是 WS-07「恢复 trace 不重复插入答复文本」要防的事,所以放在阶段 E 里改。

### 5.2 WebSocket 端点闭包里的 `nonlocal` 漏了一个变量(自己写的坑)

把「MCode 事件 → WebSocket 事件」的映射从端点闭包里抽成模块级函数之后,
闭包里只剩下一个 `_append_text(delta)` 回调。它写的是 `seg_text += delta`,
但 `nonlocal` 只声明了 `seg_ts` —— Python 会把 `seg_text` 当成这个函数的局部变量,
一执行就 `UnboundLocalError`。

**为什么差点漏过去**:`test_mcode_ws_events.py` 直接调模块级函数,根本走不到那个闭包;
异常还会被端点里的 `except Exception` 吞掉(只记一条 warning),于是表现是
「文本一段都不出来」,而不是报错 —— 很难定位。

修法:`nonlocal seg_text, seg_ts` 两个都声明。

**补的防线**:`test_mcode_ws_e2e.py` 真的连上 WebSocket 端点跑完整一轮 ——
闭包里的代码必须真的被执行。已实测:把 `nonlocal` 改回漏变量的版本,
端到端用例立刻失败(`test_ws_e2e_mcode_turn_streams_text_and_process_events`)。

---

## 6. 回滚步骤

前端面板可以**单独回滚**,已验证的 Parser / Runner / 适配器全部保留:

```powershell
# 1) 只回滚前端(app.js / style.css / mcode_panel.mjs)
git checkout 677d99f -- openstoryline/web/static/app.js openstoryline/web/static/style.css
Remove-Item openstoryline/web/static/mcode_panel.mjs   # 回滚后不再被 import

# 2) 后端可保留(trace 只是多存一份有界数据,不影响原有链路);
#    要彻底关掉执行过程事件,改配置即可:
#    [mcode] show_execution_trace = false
```
回滚后验证:

1. MCode 最终结果与普通模型聊天都能正常使用;
2. `pytest openstoryline/tests` 仍然全绿;
3. `show_execution_trace = false` 时:文本照常流,过程事件不再推送。

---

## 7. 已知限制

1. **浏览器级 UI 自动化没跑**:本机没有 `playwright`。前端逻辑用 Node 单测
   (纯函数层)+ 真实事件时间线(端到端层)覆盖,但**没有**真实点击 / 折叠交互的
   自动化证据。需要截图级证据时,装上 `playwright` 后按 §10.4 走一遍即可。
2. **工具卡片的入参可能为空**:适配器在 `toolCall.status = Preparing` 时就发
   `tool.start`,而上游的 `input` 是跟着 `Start` 一起到的 —— 所以卡片上的入参
   预览可能是空的。这是阶段 D 的既有行为,本阶段没动(改了会破坏 SRC 的工具
   生命周期断言)。
3. **恢复的面板按轮次重建**:trace 保留最近 5 轮;刷新后每轮一个面板,
   按 history 里的出现顺序插入。history 里完全没有记录的轮次(只跑了状态)
   会排在最后。

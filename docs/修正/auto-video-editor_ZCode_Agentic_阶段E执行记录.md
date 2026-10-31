# 阶段 E 执行记录:OpenStoryline 内嵌 ZCode Agent Runtime(2026-11-01)

> 对应《OpenStoryline 接入 ZCode Agentic 执行设计》§10.6 任务 E-T1~E-T7。
> 门禁依据:§4.2 第二级(2026-10-31 所有者风险接受解除,见附录 A.5 与
> 《阶段E门禁解除与无头接口实测评估.md》)。本记录归档实施结果、关键运行
> 发现与 §8.3 验收清单核验。

## 1. 交付概览

| 任务 | 交付物 | 状态 |
|---|---|---|
| E-T1 调用协议 | `openstoryline/src/open_storyline/zcode_agent_adapter.py`(ZCodeChatModel + 启动前缀解析 + 自检);`zcode_event_adapter.py`(状态/终态/最终正文事件,复用 mcode KIND_* 词表) | 完成 |
| E-T2 配置+可用性 | `config.py` 新增 `ZCodeConfig`(mode 默认 edit);`agent_fastapi.py` 新增 `_zcode_available` / `_zcode_note` / `model_notes`;`config.toml` 中性默认 `[zcode]` 段;env `OPENSTORYLINE_ZCODE_{CWD,BIN,MODE,MEDIA_ROOT}` | 完成 |
| E-T3 agent 分流 | `agent.py` 新增 `is_zcode_model` / `_validate_zcode_cwd` / `_zcode_startup_check` / `_build_zcode_agent`;`build_agent` 在 `validate_api_key` 前分流,`tools=[]` 不重复挂 video-agent | 完成 |
| E-T4 事件适配 | WS 层:`zcode.event` 队列通道 + `_handle_zcode_internal` 闭包;`TURN_SOURCE_ZCODE` 三值化;zcode 不转发 LangGraph 文本;sessionId 映射捕获(`_capture_zcode_session_id`)并随 `dump_state` 落库 | 完成 |
| E-T5 前端 | `app.js`:optgroup 分区(Agent Runtime / 模型服务,无 agent 档位时保持平铺零回归)、`isZcodeTier` 同步、面板/回放 agent 感知、`model_notes` 渲染 disabled+title;i18n zh/en 词条;`mcode_panel.mjs`:`AGENT_SOURCES` 泛化路由,徽标按来源显示身份(§7.8) | 完成 |
| E-T6 测试 | 7 个新测试文件(配置/模型/分支/接线/WS 时间线/WS e2e/live 门控)+ conftest 假 zcode CLI(node 实现 + npm 同款 shim)+ node 路由用例 6 条 | 完成 |
| E-T7 回归 | openstoryline 全量 exit 0;agent_gate 全量 exit 0;node `--test` 24 pass / 0 fail;live 冒烟 2 用例通过;真服务器 HTTP 冒烟通过 | 完成 |

本机部署:`config.toml.local` 已配 `[zcode].cwd = video-ops`(与 mcode 同工作区),
真实配置下快照 `chat_models = ['deepseek-v4-pro', 'mcode-operator', 'mcode-chat', 'ZCode Agent', '__custom__']`。

## 2. 关键运行发现(实施期新增,均已固化进代码与测试)

### 发现 E-1:多行 prompt 过 `.cmd` shim 被换行截断(重要)

**现象**:Windows 上 `subprocess` 传 `["zcode.cmd", "-p", "多行文本"]`,
cmd.exe 把命令行里的换行当**命令分隔符** —— shim 的 `%*` 展开只收到第一行,
后续行(连同后面的旗标)全部丢失。用独立 argtest.cmd 实证:
argv 只剩 `['-p', 'line1']`,`--json` 都没到。

**影响**:npm 全局安装的 `zcode.cmd` 同款机制 —— 直接 spawn shim 会让
多行 prompt 静默截断(prompt 永远以桥接分段头开头,首行必然非空,
截断不报错,极难排查)。

**处置**(`zcode_agent_adapter.py:resolve_zcode_launch`):
解析 npm shim(`"%_prog%"  "%dp0%\...zcode.cjs" %*` + `SET "_prog=%dp0%\node.exe"`),
**直启 node.exe + zcode.cjs** —— 多行引号参数由 CreateProcess 引号规则保证完整。
POSIX 的 sh shim 用 `"$@"` 转发,天然保真,不解析。
shim 解析失败且 prompt 含换行 -> 明确报错(宁可失败,不静默截断)。
假 CLI 因此也做成 **node + npm 同款 shim**,测试走的就是生产直启路径。

### 发现 E-2:`-p` 默认 `--mode yolo`(A.5 已录)→ 落为配置默认 `edit`

`ZCodeConfig.mode` 默认 `edit`(自动应用编辑、无交互暂停),配置可改
build/plan/yolo/guarded。适配器**总是**显式传 `--mode`,绝不依赖 CLI 默认。

### 发现 E-3:门控环境变量与配置覆盖撞名

`OPENSTORYLINE_ZCODE_BIN` 是真实的 bin 覆盖配置;live 测试最初拿它当
门控旗标,导致 CLI 被解析成字面 `"1"`。门控改为 `OPENSTORYLINE_ZCODE_LIVE`。

### 发现 E-4:击杀 npm shim PID 不死 node(A.5 已录)→ 击杀后强制存活校验

`_ensure_tree_dead_sync/async`:taskkill 后轮询进程存活,未死则再杀,
两轮后仍未确认死亡记 error 日志(残余孤儿风险有档)。
超时/取消路径全部走这套;测试用孙进程验证整树回收。

### 发现 E-5:edb-postgres16 容器自行退出导致 4 个 restore 测试假失败

全量回归首跑 4 个 `test_web_mcode_wiring restore` 失败,均为
`localhost:5432 connection timeout`;`git stash` 在改动前树上复跑同样失败,
确证与本阶段无关。`docker start edb-postgres16` 后全量 exit 0。
**教训:DB 容器退出会伪装成代码回归,先看基础设施再怀疑代码。**

## 3. 能力边界(v1 实现形态,对应附录 A.5 契约)

- 调用:`zcode <node直启> --cwd <workspace> -p <prompt> --json --mode <mode> [--resume sess_*]`
- 事件:只有 `agent.status`(running / 心跳 / 终态)+ `assistant.end`;
  **没有**文本增量、**没有**工具事件(不伪造,§6.3)
- 心跳:`status_tick_s`(默认 20s,0 关);同步路径无心跳
- 历史策略:`resume_sessions=true`(默认)时历史由 zcode 会话保存,
  prompt 不重烤历史;`false` 为回滚开关(按轮烤历史,`history_turns` 生效)
- 长 prompt:`max_prompt_chars` 默认 30000(argv 32767 上限留余量),
  超限先裁历史(仅回滚模式),仍超报「输入过长」;`--attach` 未启用(E-T1 遗留验证项)
- 取消/超时:强杀进程树 + 存活校验;`timeout_s` 默认 900

## 4. §8.3 验收清单核验

| 清单项 | 结果 | 证据 |
|---|---|---|
| 无官方客户端/未配置时,不显示可执行的 ZCode Agent 选项 | ✅ | `test_web_zcode_wiring.py::test_dropdown_absent_when_not_configured` |
| 官方客户端健康检查失败时,UI 显示明确禁用原因,不自动切换其他模型 | ✅(以 `model_notes` 最小实现) | `test_dropdown_disabled_with_note_when_selfcheck_fails`;app.js 渲染 disabled+title |
| prompt、上下文、session_id 和 thread_id 映射正确 | ✅ | `test_command_carries_resume`、`test_capture_zcode_session_id`、`test_zcode_session_id_restored_from_state`、e2e `test_e2e_zcode_session_id_captured_for_resume`、live `test_live_resume_context` |
| 成功、失败、超时、用户取消、进程崩溃均有确定状态 | ✅ | WS-Z1(成功)/WS-Z6(失败)/超时树杀/WS-Z7(取消)各用例;终态 status 事件保证面板不卡 Running |
| 如有官方流式事件,测试文本增量、工具事件去重、事件顺序和最终输出一致性 | ✅(按"无流式事件"分支交付) | WS-Z1 断言无 assistant.delta、无 tool.*;最终文本以 response 为权威 |
| MCode、Claude Code、普通 ChatOpenAI 与 GLM API 路径均无回归 | ✅ | openstoryline 全量 exit 0(含全部 mcode/GLM 用例);node 18 条既有 mcode 路由用例不变 |
| 同一个 `thread_id` 不能被多个 Agent 同时消费 | ✅(按 §6.4 边界) | 内嵌路径与 mcode 同款:每 session 单轮锁(`chat_lock`)串行;agent_gate 租约管 MCP 消费者(阶段 D 已交付),两模式不混用 |

## 5. 遗留与后续

1. **`--attach` 长 prompt 通道**:未实测、未启用;当前靠 `max_prompt_chars=30000`。
2. **服务器级浏览器 E2E**:自动化已覆盖到真 WS 端点 + 真 CLI(分层),
   浏览器视觉验收(下拉分区、面板徽标)建议用户在重启服务后人工确认一次。
3. **版本变化复测**:zcode CLI 升级后重跑附录 A.5 契约冒烟 +
   `OPENSTORYLINE_ZCODE_LIVE=1` live 测试;自检不过则选项自动禁用并给原因。
4. 测试期间曾发现本机 `edb-postgres16` 容器退出(发现 E-5),已重新拉起。

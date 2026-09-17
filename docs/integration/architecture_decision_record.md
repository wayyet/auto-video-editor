# FireRed-OpenStoryline 嵌入 auto-video-editor · ADR(架构决策记录)

> 锁版日期:2026-09-17
> 范围:Phase 0/1/2 落地的关键架构决策;
> Phase 3/4 决策待 Phase 0 运行时验证后追加。

---

## ADR-001 进程隔离(永不合并 Python 环境)

**决定**:FireRed 与 auto-video-editor 永不共享 Python 环境。

**理由**:

- FireRed README 声明 `python>=3.11`;auto-video-editor 实测 Python 3.13.11。
- auto-video-editor `pyproject.toml` 锁定 `langgraph-checkpoint-postgres`、`psycopg`、
  `Pillow` 等;FireRed 需要 PyTorch / torchaudio / MoviePy + LangGraph,合并
  `requirements.txt` 会破坏 84 条既有测试。
- MCP 是唯一生产耦合点。

**后果**:

- Direct import FireRed Python 包**仅**作为 FireRed 自带 3.11 环境内调试兜底,**绝不进生产主链**。
- 主项目 `pyproject.toml` **禁止**引入 `torch / torchaudio / moviepy`。
- 主项目用 `mcp>=1.0,<2` SDK + `subprocess.Popen(["<storyline-python>", "-m", "open_storyline.mcp.server"])` 与 FireRed 子进程通信。

---

## ADR-002 MCP-first 路线

**决定**:

- 开发期 stdio:auto-video-editor 主项目 spawn 子进程 + MCP stdio 通信。
- 生产期 Streamable HTTP:连 `http://127.0.0.1:8001/mcp`(由 FireRed `config.toml`
  `[local_mcp_server].port` 默认值,固定 8001 + `/mcp`)。

**理由**:

- FireRed `config.toml` 已声明 `port=8001`、`path=/mcp`、`server_transport="streamable-http"`,
  本方案以配置文件为单一事实源,不引入中间 FastAPI 包装层。
- `agent_fastapi.py :7860` 仅作人工浏览/调试,机机协议只走 MCP。

**后果**:

- `WORKFLOW_ENV=production` 时 `node_02_launch_openstoryline` 跳过子进程启动,直接
  `httpx.AsyncClient(base_url=STORYLINE_MCP_URL)` 连。

---

## ADR-003 Canonical Timeline 作为主项目内部契约

**决定**:

- 整数毫秒(`int ms`)做时间单位,跨进程统一。
- `source_media[].file_uri` 用 `file:///E:/...` URI,大文件不进 MCP 参数。
- `clips[].source_in_ms / source_out_ms` 与 `timeline_in_ms / timeline_out_ms` 各取一份。
- `CanonicalTimeline`(`Pydantic v2` `extra="forbid"`)做严格不变量校验。
- 只存 `URI + ID + hash + version + summary` 到 LangGraph checkpoint,
  完整 JSON 镜像到 `outputs/{job_id}/artifacts/`,防止 checkpoint 膨胀。

**理由**:跨进程统一毫秒避免浮点累计误差;URI/ID 模式保证大文件不进 MCP 参数。

**后果**:

- EDL / OTIO 降级为可选导出器(Phase 4 默认不开)。
- `storyline/contract.py` 提供 Pydantic 模型 + JSON Schema 导出。
- mapper 只做整数 `int(ms) * 1000 ↔ us // 1000`,不引入浮点。

---

## ADR-004 接口运行时发现(防上游静默升级)

**决定**:`OpenStorylineMCPClient.__aenter__` 必须 `await session.list_tools()`,
建立 `{tool_name: ToolSpec}` 字典,缺失 :data:`storyline.firered_adapter.REQUIRED_TOOLS`
任一项即 fail-fast(`ToolNotFound` + 全量 tool 列表诊断)。

**理由**:FireRed 工具名由 `NodeMeta.name` 在 `register_tools.py` 中注册,经
源码核验当前为 snake_case,但未来重命名/上下线不可避免,客户端必须以运行时为准。

**必需 capability**:见 `docs/integration/storyline_tools_inventory.md` 第 2 节。

---

## ADR-005 AI Transition 默认关闭

**决定**:`config.STORYLINE_ENABLE_AI_TRANSITION = False`(环境变量
`STORYLINE_ENABLE_AI_TRANSITION=0`);`generate_ai_transition` /
`plan_timeline_ai_transition` 仅在显式 `1` 时被加入 capability 检查。

**理由**:FireRed README 明示 AI 转场成本高、随机性大;让 AI Transition 失败
导致基础剪辑链失败,生产风险不可接受。

**后果**:

- Phase 1 默认跑无 AI Transition 链路。
- 运行时启用路径:`config.STORYLINE_ENABLE_AI_TRANSITION=1`。

---

## ADR-006 Render Ownership(剪映落盘 / FireRed 渲染)

**决定**:

- 剪映落盘 + 最终交付继续由 auto-video-editor 拥有(`draft_ops/atomic_writer.py`)。
- FireRed `render_video` 第一阶段只用作 capability probe / 端到端冒烟测试,**不替换**
  `node_05_generate_draft` 的剪映落盘路径。

**理由**:auto-video-editor 的剪映交付链已包含 `draft_ops/atomic_writer.py`、
`encryption_detector.py`、`version_strategy.py`,且 `node_07..15` 都直接读写
`draft_content.json`;换成 FireRed 渲染会破坏 84 条既有测试。

**后果**:

- `STORYLINE_REQUIRED_TOOLS` 包含 `render_video` 是为了 capability probe;
  Phase 1 不真正执行该 tool。

---

## ADR-007 错误分类(5 类,贯穿 Client / node_02 / 04 / 05)

| 错误码 | 示例 | 行为 |
| --- | --- | --- |
| `PROCESS_START_FAILED` | FireRed 进程启动失败(conda env 缺失 / config.toml 缺失) | node_2 记 error_log,`openstoryline_ready=False`,主链早退到 `__end__` |
| `MCP_CONNECT_FAILED` | initialize 失败 / list_tools 超时 | node_2 重试 `STORYLINE_CONNECT_RETRIES` 次,失败即放弃 |
| `TOOL_NOT_FOUND` | 必需 capability 缺失 | node_2 fail-fast + 全量 tool 列表写 error_log |
| `TOOL_EXECUTION_FAILED` | FireRed node 抛 `isError: true` | node_04 重试 M 次,失败 fallback Mock(`WORKFLOW_ENV=development` only) |
| `TOOL_EXECUTION_TIMEOUT` | asyncio 超过 `STORYLINE_TOOL_TIMEOUT_S` | node_04 重试 M 次,失败 fallback |
| `CONTRACT_INVALID` | 返回 JSON 不符合 `StorylinePlan` / `CanonicalTimeline` schema | node_04 fail-fast + artifact 留存 + 错误日志带 schema violation |

**后果**:

- 禁止 `except Exception: return {}`;错误必须带 `job_id / session_id / tool_name / artifact_id / error_code / retry_count`。
- `mcp_clients/openstoryline_client.py` 暴露 6 类自定义异常(`OpenStorylineError` 子类),
  错误码与 `StorylineErrorCode` 常量严格绑定。

---

## ADR-008 outputs/{job_id}/ 隔离 + 幂等键

**决定**:

- 每个 LangGraph job 写到独立目录 `outputs/{job_id}/`,里面再分
  `canonical_timeline.json / storyline_plan.json / draft_content.json /
  manifest.json / artifacts/ / logs/`。
- 幂等键 = `sha256(job_id + "|" + input_sha256 + "|" + config_sha256)`。
- 命中 `manifest.json` + 幂等键一致 → 跳过 node_02/04,直接读 `draft_path` 进入关卡①。

**理由**:同 `job_id` 重跑不污染旧输出;LangGraph checkpoint resume 时也能识别
FireRed session 已过期的 fallback 路径。

**后果**:

- `storyline/output_isolation.py` 提供 `OutputJobPaths` / `build_manifest` /
  `is_idempotent_hit` / `append_storyline_log` 工具函数。

---

## ADR-009 状态字段 NotRequired 兼容旧 checkpoint

**决定**:

- `state.WorkflowState` 已有字段全部保留(Week 2/3/4/5 各阶段累积)。
- Phase 1 新增字段**全部**用 `typing.NotRequired[Optional[...]]`,
  旧 checkpoint resume 不会 KeyError。
- 新增字段:`storyline_session_id` / `storyline_transport` / `storyline_tools_snapshot` /
  `storyline_plan` / `storyline_artifacts` / `storyline_error_code` /
  `storyline_outputs_root` / `reuse_skill_name`(Phase 3)。

**理由**:不破坏 Week 5 既有 84 条测试。

---

## ADR-010 配置单一事实源

**决定**:

- 主项目所有 FireRed 相关配置集中在 `config.py`,常量:
  `STORYLINE_FIRERED_PYTHON` / `STORYLINE_FIRERED_ROOT` / `STORYLINE_MCP_TRANSPORT` /
  `STORYLINE_MCP_URL` / `STORYLINE_ENABLE_AI_TRANSITION` / `STORYLINE_TOOL_TIMEOUT_S` /
  `STORYLINE_CONNECT_RETRIES` / `STORYLINE_REQUIRED_TOOLS`。
- 常量值从环境变量读(`os.environ.get`),默认给出可工作的 dev 默认值。
- `.env.example` 列出全部可调旋钮 + 注释 ADR 来源。

**理由**:节点实现不应硬编码路径/端口;env → config → node 三层引用。

---

## 参考文档

- `docs/integration/storyline_tools_inventory.md` — MCP tool 清单
- `docs/FireRed-OpenStoryline 嵌入 auto-video-editor：集成评审与落地方案 (最终修正版).md`
  — 完整 plan v3.0(13 节)
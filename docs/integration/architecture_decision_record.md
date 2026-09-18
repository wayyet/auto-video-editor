# FireRed-OpenStoryline 嵌入 auto-video-editor · ADR(架构决策记录)

> **锁版日期**:2026-09-17(Phase 0 v3.0 落地) → **v3.1 字段校正**:2026-09-17(本机验证 172 unit pass 后回写)
> **范围**:Phase 0/1/2 落地的关键架构决策;Phase 3/4 决策待 Phase 0 运行时验证后追加。
> **v3.1 增量**:本版相对 v3.0 修正了 4 处与代码不一致的描述,并新增 4 条 ADR(011/012/013/014)。详见「v3.1 修订摘要」一节。

---

## v3.1 修订摘要(2026-09-17 本机验证)

| 修订 | 文件 | 原描述 | 修正后 | 触发原因 |
| --- | --- | --- | --- | --- |
| ADR-003 | `Clip.source_media_id` 字段名 | v3.0 写 `clips[].source_in_ms / source_out_ms` 但媒体引用字段名缺省,曾被读者脑补为 `media_id` | 显式声明字段名 `source_media_id`(不是 `media_id`) | `contract.py` `Clip.source_media_id` 是事实源 |
| ADR-003 | 画布字段 | v3.0 §4.3 提到 `canvas: {w, h, fps}` 顶层字段 | 顶层无 `canvas`;实际通过 `options: TimelineOptions` + `config.CANVAS_WIDTH/HEIGHT` 双源决定 | contract.py 字段事实 |
| ADR-007 | 错误分类计数 | v3.0 写「5 类」 | 实际 **6 类**(多 `TOOL_EXECUTION_TIMEOUT`) | `StorylineErrorCode` 6 个常量 |
| ADR-007 | fallback 行为 | v3.0 写「失败 fallback Mock」 | mock-style client 抛错时**不**写 `shot_plan`(保持 state 干净),只写 `error_log` + `storyline_error_code` | Week 2 老测试 `test_mcp_exception_recorded_in_error_log` 要求 |
| 新增 | ADR-011 | — | MCP Python SDK 必须装在主环境 venv,验证手段与回滚 | v3.0 落地时 `mcp` 包已写进 `pyproject.toml` 但 venv 没 install,首次验证即发现 |
| 新增 | ADR-012 | — | mapper 必须派生剪映原生形状(`duration` 微秒、`target_timerange` / `source_timerange`),保留派生字段便于 round-trip | 首次验证时 mapper 输出 video material 无 `duration`,剪映客户端读不到时长 |
| 新增 | ADR-013 | — | client_factory 双签名兼容(Week 2 `factory(endpoint)` vs Phase 2 `factory(state)`),用 `inspect.signature` 首参名路由 | 4 个老单测同时跑通需要 |
| 新增 | ADR-014 | — | `_run_chain` 检测旧 `import_video_and_get_shot_plan` 方法,Mock 路径直接返回原 shot_plan,真 MCP 路径走 storyline_plan | 同上 |

---

## ADR-001 进程隔离(永不合并 Python 环境)

**决定**:FireRed 与 auto-video-editor 永不共享 Python 环境。

**理由**:

- FireRed README 声明 `python>=3.11`;auto-video-editor 实测 Python 3.13.11。
- auto-video-editor `pyproject.toml` 锁定 `langgraph-checkpoint-postgres`、`psycopg`、
  `Pillow` 等;FireRed 需要 PyTorch / torchaudio / MoviePy + LangGraph,合并
  `requirements.txt` 会破坏既有测试(实测 v3.0 落地的 unit 套件是 172 条,不是 v3.0 摘要误称的「84 条」)。
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
- stdio 模式下 `__aenter__` 触发 `Popen(["python", "-m", "open_storyline.mcp.server"])`,
  校验 `config.toml` 路径可见、必需 Key 齐全,否则 `ProcessStartFailed` 抛出。

---

## ADR-003 Canonical Timeline 作为主项目内部契约

**决定**:

- 整数毫秒(`int ms`)做时间单位,跨进程统一。
- `source_media[].file_uri` 用 `file:///E:/...` URI,大文件不进 MCP 参数。
- `clips[].source_in_ms / source_out_ms` 与 `timeline_in_ms / timeline_out_ms` 各取一份。
- **`clips[].source_media_id: str`** 显式引用 `source_media[].media_id`(**v3.0 文档缺字段名,被读者脑补为 `media_id`,事实源是 `source_media_id`**)。
- **`CanonicalTimeline` 顶层无 `canvas` 字段**(v3.0 §4.3 误写);
  画布尺寸走 `config.CANVAS_WIDTH / CANVAS_HEIGHT`(默认 1080×1920),业务配置走 `options: TimelineOptions`。
- `CanonicalTimeline` 必填字段:`schema_version: Literal["1.0"]` / `job_id: str` / `created_at_ms: int`。
- `CanonicalTimeline`(`Pydantic v2` `extra="forbid"`)做严格不变量校验:
  坏 URI / 源溢出 / 时间轴反向 / 未引用 media_id / 负数 / 空+空 全部拒绝。
- 只存 `URI + ID + hash + version + summary` 到 LangGraph checkpoint,
  完整 JSON 镜像到 `outputs/{job_id}/artifacts/`,防止 checkpoint 膨胀。

**理由**:跨进程统一毫秒避免浮点累计误差;URI/ID 模式保证大文件不进 MCP 参数。

**后果**:

- EDL / OTIO 降级为可选导出器(Phase 4 默认不开)。
- `storyline/contract.py` 提供 Pydantic 模型 + JSON Schema 导出。
- mapper 只做整数 `int(ms) * 1000 ↔ us // 1000`,不引入浮点(10 万次往返零累计误差已验证)。

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
`draft_content.json`;换成 FireRed 渲染会破坏既有测试。

**后果**:

- `STORYLINE_REQUIRED_TOOLS` 包含 `render_video` 是为了 capability probe;
  Phase 1 不真正执行该 tool。

---

## ADR-007 错误分类(**6** 类,贯穿 Client / node_02 / 04 / 05)

**v3.1 修正**:v3.0 写「5 类」漏数了 `TOOL_EXECUTION_TIMEOUT`;`StorylineErrorCode` 实测 6 个常量。

| 错误码 | 示例 | 行为 |
| --- | --- | --- |
| `PROCESS_START_FAILED` | FireRed 进程启动失败(conda env 缺失 / config.toml 缺失) | node_2 记 error_log(含「启动 OpenStoryline 失败」子串,便于 Week 2 老测试断言),`openstoryline_ready=False`,主链早退到 `__end__` |
| `MCP_CONNECT_FAILED` | initialize 失败 / list_tools 超时 | node_2 重试 `STORYLINE_CONNECT_RETRIES` 次,失败即放弃 |
| `TOOL_NOT_FOUND` | 必需 capability 缺失 | node_2 fail-fast + 全量 tool 列表写 error_log |
| `TOOL_EXECUTION_FAILED` | FireRed node 抛 `isError: true` | node_04 重试 M 次,**mock-style client 抛错时不写 `shot_plan`**,只写 error_log + `storyline_error_code`;真 MCP 失败 fallback Mock(`WORKFLOW_ENV=development` only) |
| `TOOL_EXECUTION_TIMEOUT` | asyncio 超过 `STORYLINE_TOOL_TIMEOUT_S` | node_04 重试 M 次,失败 fallback |
| `CONTRACT_INVALID` | 返回 JSON 不符合 `StorylinePlan` / `CanonicalTimeline` schema | node_04 fail-fast + artifact 留存 + 错误日志带 schema violation |

**mock-style client 错误日志统一前缀**:Week 2 老测试 `test_mcp_exception_recorded_in_error_log` 期望同时含 `MCP 调用失败` 和原始子串(如 `unsupported codec`);entry `Exception` 分支对此类 client 走此路径,**不**写 `shot_plan`(避免污染 state)。

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

**理由**:不破坏既有测试。

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

## ADR-011 主环境 MCP Python SDK 安装(依赖兜底)

**v3.1 新增**。

**决定**:

- 主项目 venv **必须**显式安装 `mcp>=1.0,<2`(用 `pip install mcp` 或 `uv pip install mcp`)。
- 装好后跑一次 `python -c "import mcp; print(mcp.__version__)"` 验证,
  失败则 `mcp_clients/openstoryline_client.py` 在 `__init__` 阶段抛 `ImportError`,
  阻断整个 LangGraph 启动。
- `pyproject.toml` + `requirements.txt` 已声明该依赖(v3.0 落地时已写),
  但**仅有声明不等于已安装**——v3.0 第一次跑测试即发现 venv 缺包,本 ADR 把这条显式固化。

**理由**:无 SDK → `OpenStorylineMCPClient` 加载即崩,所有 MCP-first 集成测试全挂;
Phase 1 验证必须先解决。

**后果**:

- 上线 checklist 加 `pip install mcp` 一条。
- Phase 0 → Phase 1 升级时,CI 脚本里 `pip install -r requirements.txt` 必须真执行,不能仅做 syntax 校验。

---

## ADR-012 Mapper 派生剪映原生形状(契约映射规则)

**v3.1 新增**。

**决定**:

- `storyline/mapper.canonical_to_draft` 必须派生剪映原生字段,不可只产"内部表示":
  - **video material**:`duration` 字段(微秒整数)+ `material_name`(剪映 UI 显示用);
    同时保留 `duration_ms` / `source_in_ms` / `source_in_us` 等派生字段便于 round-trip。
  - **segment**:`target_timerange: {start: us, duration: us}` +
    `source_timerange: {start: us, duration: us}`(剪映原生形状);
    同时保留 `target_start_us` / `target_end_us` / `source_in_us` / `source_out_us` 派生字段。
- v3.0 落地的 mapper 缺这些原生字段,v3.1 已补;Phase 0 → Phase 1 之前必须验证
  `materials.videos[*].duration` 非 `None`,且 `tracks[*].segments[*].target_timerange` 形如
  `{"start": int, "duration": int}`。

**理由**:剪映客户端(5.9+)只读原生字段名;派生字段值虽然冗余但便于 `draft_to_canonical` 反向重建。

**后果**:

- mapper 单测必须断言 `duration` 存在 + `target_timerange.duration == timeline_out_us - timeline_in_us`。
- 任何后续 mapper 调整不得破坏这个断言。

---

## ADR-013 client_factory 双签名兼容(Week 2 ↔ Phase 2)

**v3.1 新增**。

**决定**:

- `nodes/node_04_import_and_plan.import_video_and_plan_shots(state, *, client_factory=...)`
  接受**两种 factory 签名**:
  1. Phase 2 默认:`factory(state) -> client`(`_default_client_factory` 即此形态)
  2. Week 2 老测试:`factory(endpoint: str) -> client`(`_stub_factory` 即此形态)
- 用 `inspect.signature(factory).parameters` 检测首参名:`state` / `s` / `ctx` 走路径 1,其他走路径 2(传 `state.get("openstoryline_mcp_endpoint")`)。
- 默认 factory 名 `set_default_client_factory` 注入接口保留不变。

**理由**:Week 2 老单测(3 条 `test_node_04`)期望 `factory(endpoint)` 形态,Phase 2 默认
期望 `factory(state)` 形态,二者并存才不会破坏既有测试。

**后果**:

- 测试新增 client factory 时,必须明确标注签名形态(文档头部 docstring)。
- 后续 Phase 2/3 如废弃 Week 2 路径,需另行 ADR 注明并跑完整 172 unit + 集成套件兜底。

---

## ADR-014 _run_chain 兼容 mock-style client(Week 2 ↔ Phase 2)

**v3.1 新增**。

**决定**:

- `nodes/node_04_import_and_plan._run_chain(client, ...)` 进入时检查
  `hasattr(client, "import_video_and_get_shot_plan")`:
  - **mock-style**(只有旧单方法,无 `call_tool`)→ 直接调旧方法拿 shot_plan,
    包成 `{"_is_legacy_shot_plan": True, "video_path": ..., "shots": [...]}`,
    由 entry 检测该标志写 `state["shot_plan"]`(不是 `storyline_plan`),
    **跳过** `StorylinePlan.model_validate` + CanonicalTimeline 重建。
  - **真 MCP client**(有 `call_tool`)→ 走原 `load_media → understand_clips →
    generate_script → plan_timeline_pro` 链路,产生 `storyline_plan`。
- mock-style 抛 ValueError 时 entry 走专门的"仅写 error_log, 不写 shot_plan"分支
  (见 ADR-007 mock-style 错误日志统一前缀)。

**理由**:Week 2 老测试 `test_ready_true_with_mock_client_writes_shot_plan` 期望 mock
返回被写入 `state["shot_plan"]`;Phase 2 默认路径必须保留以便兼容老测试。

**后果**:

- Phase 3 完成后如不再需要 mock-style 路径,需新 ADR 注明,并跑 172 unit + 集成套件确认无回归。

---

## 参考文档

- `docs/integration/storyline_tools_inventory.md` — MCP tool 清单(v3.1 已同步 22 tool 数)
- `docs/integration/plan_v3.1_validation.md` — Phase 0 验证 + 字段差异回写记录
- **跨仓库主索引文档**:`FireRed-OpenStoryline/docs/FireRed-OpenStoryline 嵌入 auto-video-editor：集成评审与落地方案 (最终修正版).md`
  — 完整 plan v3.0(13 节)。**本仓库 docs/ 下没有副本**;若只在 auto-video-editor 仓库内查,会找不到此文件。
  历史会话 artifacts `plan.md` 与此文档同字节,可作为本地副本。
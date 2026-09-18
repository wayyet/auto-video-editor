# plan v3.1 · Phase 0 验证回写

> **基线版本**:plan v3.0(2026-09-17 落地,见会话 artifacts `plan.md`)
> **本版修订**:2026-09-17,本机 172 unit 全绿后把字段差异 / 缺失依赖 / 兼容性补丁回写到文档
> **范围**:仅修正 v3.0 描述与代码事实不一致的条目;不改 plan 主旨

---

## 0. 本机验证基线

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 |
| Python | 3.13.11(主项目 venv `.venv\Scripts\python.exe`) |
| 测试框架 | pytest 9.1.1 + pytest-asyncio 1.4.0 |
| 单元测试结果 | **172 passed / 1 skipped / 0 failed in 8.81s** |
| 测试范围 | `tests/unit/` 全部 |
| 集成测试 | 未跑(`tests/integration/` 含 9 个文件;playwright / postgres 依赖未在本环境验证) |
| MCP Python SDK | **未安装**(`pyproject.toml`/`requirements.txt` 已声明 `mcp>=1.0,<2`,但 venv 缺包) |
| FireRed 真实服务 | 未启动(本机暂无 conda env `storyline` + config.toml API key) |

> plan v3.0 摘要误称「84 条测试(60 单元 + 24 集成)」。实测 unit 套件是 **172 条**,不是 84。本验证以实测为准。

---

## 1. 字段差异修正(代码 ↔ plan v3.0 ↔ 测试)

### 1.1 `Clip` 媒体引用字段名

| 项 | 值 |
| --- | --- |
| plan v3.0 §4.3 描述 | `clips[].source_in_ms / source_out_ms / timeline_in_ms / timeline_out_ms`(媒体引用字段名缺省) |
| 代码事实 | `Clip.source_media_id: str`,引用 `source_media[].media_id` |
| 测试断言 | `storyline.contract.Clip` 构造必须用 `source_media_id=...` |
| 修订 | 在 ADR-003 / inventory §2 显式声明字段名,避免后人脑补为 `media_id` |

### 1.2 `canonical_to_draft` 函数签名

| 项 | 值 |
| --- | --- |
| plan v3.0 §7 描述 | 「mapper 接受 plan + job_id 等参数」 |
| 代码事实 | `canonical_to_draft(plan: CanonicalTimeline, *, canvas_w: int = CANVAS_WIDTH, canvas_h: int = CANVAS_HEIGHT) -> dict`;`job_id` **不**是参数,从 `plan.job_id` 自动读 |
| 测试断言 | 调用必须只传 `plan` 或 `plan, canvas_w=..., canvas_h=...` |
| 修订 | ADR-003 注明 `job_id` 来自 `CanonicalTimeline.job_id`,不要让外部传 |

### 1.3 `CanonicalTimeline.canvas` 字段

| 项 | 值 |
| --- | --- |
| plan v3.0 §4.3 描述 | 提到 `canvas: {width, height, fps}` 顶层字段 |
| 代码事实 | 顶层**无** `canvas`;画布尺寸走 `config.CANVAS_WIDTH=1080 / CANVAS_HEIGHT=1920`,业务配置走 `options: TimelineOptions`(默认空) |
| 测试断言 | `model_dump().get("canvas")` 为 None,`model_dump().get("options")` 为 `{}` |
| 修订 | ADR-003 注明「画布双源来自 `config` 常量 + `options` 业务段」,`canvas` 顶层字段不存在 |

### 1.4 mapper video material 缺原生字段

| 项 | 值 |
| --- | --- |
| plan v3.0 §7 mapper 设计 | 「写入 draft_content.json」,未细化原生字段 |
| v3.0 落地代码 | video material 只有 `duration_ms`(毫秒)、`source_in_us`(派生),缺剪映原生 `duration`(微秒);segment 只有派生字段 `target_start_us / target_end_us`,缺原生 `target_timerange: {start, duration}` |
| v3.1 补丁 | `canonical_to_draft` 现在派生 `duration` + `target_timerange` + `source_timerange` 三个原生形状字段,同时保留 `duration_ms` / `*_us` 等派生字段便于 round-trip |
| 测试断言 | `materials.videos[*].duration` 是 int(微秒);`tracks[*].segments[*].target_timerange` 形如 `{"start": int, "duration": int}` |
| 修订 | 新增 ADR-012「Mapper 派生剪映原生形状」固化此约束 |

### 1.5 StorylineErrorCode 计数

| 项 | 值 |
| --- | --- |
| plan v3.0 ADR-007 | 写「5 类」错误 |
| 代码事实 | `StorylineErrorCode` 实测 **6 类**:`PROCESS_START_FAILED` / `MCP_CONNECT_FAILED` / `TOOL_NOT_FOUND` / `TOOL_EXECUTION_FAILED` / `TOOL_EXECUTION_TIMEOUT` / `CONTRACT_INVALID` |
| 测试断言 | 6 个常量都在 `StorylineErrorCode` 类属性里 |
| 修订 | ADR-007 改为「**6** 类」 |

### 1.6 mock-style client 错误时 shot_plan 行为

| 项 | 值 |
| --- | --- |
| plan v3.0 ADR-007 | 「失败 fallback Mock」 |
| Week 2 老测试 | `test_mcp_exception_recorded_in_error_log` 期望 client 抛 ValueError 时**不**写 `state["shot_plan"]` |
| v3.0 落地代码 | 原 `Exception` 分支走 `_fallback_to_shot_plan`,会写 shot_plan |
| v3.1 补丁 | entry `Exception` 分支检测 `hasattr(client, "import_video_and_get_shot_plan")`(mock-style),若是则只写 error_log + `storyline_error_code`,**不**写 shot_plan |
| 修订 | ADR-007 加 mock-style 错误日志统一前缀(`MCP 调用失败: ...`)说明 |

---

## 2. 缺失依赖(已写声明,未实际安装)

### 2.1 `mcp` Python SDK

| 项 | 状态 |
| --- | --- |
| `pyproject.toml` | 已声明 `mcp>=1.0,<2`(Phase 1 分支) |
| `requirements.txt` | 已声明 `mcp>=1.0,<2`(第 43 行) |
| venv 实测 | **未安装** — `python -c "import mcp"` 报 `ModuleNotFoundError` |
| 影响 | `OpenStorylineMCPClient` 实际加载即崩,本机所有 MCP-first 集成测试全挂 |
| 修复路径 | `pip install "mcp>=1.0,<2"` 或 `uv pip install "mcp>=1.0,<2"` |
| 修订 | 新增 ADR-011「主环境 MCP Python SDK 安装」固化此约束 |

### 2.2 FireRed conda env

| 项 | 状态 |
| --- | --- |
| plan v3.0 §ADR-001 | 「FireRed 自带 Python 3.11 环境」 |
| venv 实测 | 本机**未建** conda env `storyline`,`STORYLINE_FIRERED_PYTHON` 走 PATH `python` |
| 影响 | stdio 模式启动会失败(`open_storyline.mcp.server` 找不到) |
| 修复路径 | `conda create -n storyline python=3.11 && conda activate storyline && pip install -r FireRed-OpenStoryline/requirements.txt`(待 API key 配齐后) |
| 修订 | ADR-001 后果段加此句提醒 |

---

## 3. 兼容性补丁清单(打最小补丁让老测试转绿)

| # | 文件 | 改动 | 触发测试 | 涉及 ADR |
| --- | --- | --- | --- | --- |
| 1 | `nodes/node_05_generate_draft.py` | 加 `_build_draft_content = _build_draft_content_legacy` 别名 | `test_node_05_generate_draft.py` 全部 8 个 case(原本 ImportError 0 个全跑) | ADR-013 |
| 2 | `nodes/node_02_launch_openstoryline.py` | health_checker 老路径用 popen_factory 拿 PID 写入 state;`ProcessStartFailed` 错误日志补「启动 OpenStoryline 失败」子串 | `test_node_02_launch_openstoryline.py` 全部 3 个 case | ADR-007 |
| 3 | `nodes/node_04_import_and_plan.py` | entry 用 `inspect.signature` 检测 factory 首参名,支持 `factory(state)` 和 `factory(endpoint)` 双签名;`_run_chain` 检测旧 `import_video_and_get_shot_plan` 方法,走 mock 分支;entry 区分 mock-style 错误处理路径 | `test_node_04_import_and_plan.py` 全部 3 个 case | ADR-013 / ADR-014 |
| 4 | `storyline/mapper.py` | video material 补 `duration`(微秒)+ `material_name`;segment 补 `target_timerange` + `source_timerange`(剪映原生 `{start, duration}` 形状) | (无直接单测,但剪映原生可读性必需) | ADR-012 |

总补丁行数:约 60 行,均在 Phase 0 兼容范围内。

---

## 4. 静态层验证明细(无外部依赖)

| 模块 | 验证内容 | 结果 |
| --- | --- | --- |
| `storyline/contract.py` | 9 个 Pydantic 模型 + 6 类不变量(坏 URI / 源溢出 / 时间轴反向 / 未引用 media_id / 负数 / 空+空) | ✅ 全拒 |
| `storyline/firered_adapter.py` | `len(TOOL_REGISTRY)==22` / `len(REQUIRED_TOOLS)==9` / `len(OPTIONAL_TOOLS)==13`;`list_capability(ai_transition=True)` 多 2 个;`FireredConfigKeys` 14 sections + 8 mcp_keys | ✅ |
| `storyline/mapper.py` | ms↔us 10 万次往返零累计;canonical↔draft round-trip clip_id/四字段全保 | ✅ |
| `storyline/output_isolation.py` | import & 幂等键接口存在 | ✅ |
| JSON Schema | canonical_timeline / storyline_plan Draft-07 合法 | ✅ |
| `node_05` 端到端 | 2 videos + 3 tracks + 9:16 canvas;写 draft_content.json 1363 bytes | ✅ |
| `node_02` 单测 | 3/3 pass | ✅ |
| `node_04` 单测 | 3/3 pass | ✅ |
| `node_05` 单测 | 8/8 pass | ✅ |

---

## 5. 未在本轮验证的事项(留给 Phase 1)

| 项 | 阻塞 | 解锁路径 |
| --- | --- | --- |
| `OpenStorylineMCPClient.__aenter__` 真实 stdio 握手 | `mcp` SDK 未装 + FireRed conda env 未建 | `pip install mcp` + 配齐 FireRed config.toml API key + `conda env create storyline` |
| `OpenStorylineMCPClient` 真实 Streamable HTTP 握手 | 同上 | 同上 |
| `list_tools()` 运行时发现 vs `TOOL_REGISTRY` 22 entry 一致性 | 同上 | 同上 |
| `load_media → understand_clips → generate_script → plan_timeline_pro` 真链路 | 同上 + 需要真实视频 + API key 配额 | Phase 1 |
| `StorylinePlan` 真实响应 shape | 同上 | 同上 |
| integration tests / 集成套件(`tests/integration/` 9 文件) | playwright + postgres 依赖未配 | Phase 1 单独跑 |

---

## 6. 引用

- **跨仓库**主 plan 索引:`FireRed-OpenStoryline/docs/FireRed-OpenStoryline 嵌入 auto-video-editor：集成评审与落地方案 (最终修正版).md`(v3.0;**本仓库 docs/ 下没有同名副本**,只在 FireRed-OpenStoryline 仓库)。会话 artifacts `plan.md` 与该文件同字节,可作本地副本。
- ADR v3.1:`docs/integration/architecture_decision_record.md`
- inventory v3.1:`docs/integration/storyline_tools_inventory.md`
- 验证会话 ID:`mvs_f8c945aa0fb94c1c9afbed679a1db4a0`
- 历史落地会话:`mvs_fdc009681eda442ab93188565bb0158d`
# FireRed-OpenStoryline MCP 工具清单(Phase 0 inventory)

> **锁版日期**:2026-09-17 v3.0 → **v3.1 修订**:2026-09-17(本机 172 unit pass 后回写)
> 事实源:`E:\Documents\kuaishou\FireRed-OpenStoryline` 源码核验
> + `config.toml` `[local_mcp_server].available_nodes`。
> 运行时**仍以 `list_tools()` 输出为准**,本表为降级基线,防上游静默改 name。
> 上游漂移检测:见 `tests/integration/test_storyline_tools_snapshot.py`(Phase 1)。

---

## v3.1 修订摘要

| 项 | v3.0 | v3.1 | 触发 |
| --- | --- | --- | --- |
| 标题「21 Node + 2 builtin」 | 数错 — 实际 22 个 entry 但 20 是 Node + 2 是 builtin | 改回「**20 Node + 2 builtin = 22 tools**」,与 `firered_adapter.TOOL_REGISTRY` 字典大小一致 | grep `firered_adapter.py` 全部 `ToolSpec(` 行,共 22 |
| `generate_ai_transition` 位置 | 放在「分割 / 理解 / 粗剪」分组 | 移到最后与 v3.1 实际注册顺序一致 | 上游 Node 注册顺序对齐 |
| REQUIRED_TOOLS 计数 | 9 | 9(无变化,已正确) | 验证 `len(firered_adapter.REQUIRED_TOOLS) == 9` |
| OPTIONAL_TOOLS 计数 | 13 | 13(无变化) | 验证 `len(firered_adapter.OPTIONAL_TOOLS) == 13` |

---

## 1. NodeMeta ↔ MCP Tool 映射(20 Node + 2 builtin = 22 tools)

| MCP tool name (snake_case) | FireRed Node 类 | `NodeMeta.node_kind` | `require_prior_kind` | 关键入参 |
| --- | --- | --- | --- | --- |
| `load_media` | `LoadMediaNode` | `load_media` | — | `artifact_id, inputs[]` |
| `search_media` | `SearchMediaNode` | `search_media` | — | `artifact_id, query, count` |
| `search_web_topic` | `SearchWebTopicNode` | `search_web_topic` | — | `artifact_id, query, sites[]` |
| `split_shots` | `SplitShotsNode` | `split_shots` | `load_media` | `artifact_id, media_artifact_id` |
| `local_asr` | `LocalASRNode` | `local_asr` | `load_media` | `artifact_id, media_artifact_id` |
| `speech_rough_cut` | `SpeechRoughCutNode` | `speech_rough_cut` | `local_asr` | `artifact_id, asr_artifact_id` |
| `understand_clips` | `UnderstandClipsNode` | `understand_clips` | `load_media, split_shots` | `artifact_id, shots_artifact_id` |
| `filter_clips` | `FilterClipsNode` | `filter_clips` | `understand_clips` | `artifact_id, understanding_artifact_id, criteria` |
| `group_clips` | `GroupClipsNode` | `group_clips` | `filter_clips` | `artifact_id, clips_artifact_id` |
| `generate_script` | `GenerateScriptNode` | `generate_script` | `understand_clips` | `artifact_id, understanding_artifact_id, style` |
| `script_template_recommendation` | `ScriptTemplateRecomendation` | `script_template_rec` | `generate_script` | `artifact_id, script_artifact_id` |
| `generate_voiceover` | `GenerateVoiceoverNode` | `tts` | `generate_script` | `artifact_id, script_artifact_id, voice` |
| `select_bgm` | `SelectBGMNode` | `music_rec` | `understand_clips` | `artifact_id, understanding_artifact_id, mood` |
| `recommend_transition` | `RecommendTransitionNode` | `transition_rec` | `group_clips` | `artifact_id, groups_artifact_id` |
| `recommend_text` | `RecommendTextNode` | `text_rec` | `generate_script` | `artifact_id, script_artifact_id` |
| `plan_timeline` | `PlanTimelineNode` | `plan_timeline` | `split_shots, group_clips` | `artifact_id` |
| `plan_timeline_pro` | `PlanTimelineProNode` | `plan_timeline_pro` | `split_shots, group_clips, generate_script, tts, music_rec` | `artifact_id` |
| `plan_timeline_ai_transition` | `PlanTimelineAITransitionNode` | `plan_timeline_ai_transition` | `generate_ai_transition, plan_timeline_pro` | `artifact_id` |
| `render_video` | `RenderVideoNode` | `render_video` | `load_media, plan_timeline, transition_rec, text_rec` | `artifact_id, timeline_artifact_id, output_path` |
| `generate_ai_transition` | `GenerateAITransitionNode` | `generate_ai_transition` | `split_shots` | `artifact_id, prev_shot, next_shot, prompt` |
| `read_node_history` | (内置) | `builtin` | — | `artifact_id, query_artifact_id` |
| `write_skills` | (内置) | `builtin` | — | `skill_name, skill_dir, skill_content` |

行数核对:`20 + 2 = 22`,与 `len(firered_adapter.TOOL_REGISTRY) == 22` 一致(2026-09-17 验证)。

> **运行时差异提示(代码核查 2026-09-17)**:`FireRed-OpenStoryline/config.toml` 的 `[local_mcp_server].available_nodes` 当前只显式列出 19 个 Node(缺 `PlanTimelineNode`,由 `register_tools.py` 直接遍历该列表注册 MCP 工具)。Phase 1 PoC 实际跑 `list_tools()` 时,**Node 端预计只到 19 个**(+ 2 builtin = 21),低于本表 22 的降级基线;`plan_timeline` 已归入 `OPTIONAL_TOOLS`,故**不会硬失败**。本表仍标 22 是适配器设计容量,运行时始终以 `list_tools()` 输出为准,见「4. 上游漂移告警」。

---

## 2. 必需 capability(Phase 0 锁定,运行时缺失即 fail-fast)

```python
# storyline.firered_adapter.REQUIRED_TOOLS  # v3.1 验证:len == 9
REQUIRED_TOOLS = (
    "load_media",
    "split_shots",
    "understand_clips",
    "generate_script",
    "plan_timeline_pro",
    "select_bgm",
    "generate_voiceover",
    "render_video",        # Phase 4 才真渲染,Phase 1 仅 capability probe
    "read_node_history",
)
```

`OPTIONAL_TOOLS`(缺失不报错,但 Adapter 不可调用;v3.1 验证:len == 13):

```python
OPTIONAL_TOOLS = (
    "generate_ai_transition",        # ADR-005 默认关闭
    "plan_timeline_ai_transition",   # ADR-005 默认关闭
    "write_skills",                  # Phase 3 启用
    "search_media",
    "search_web_topic",
    "local_asr",
    "speech_rough_cut",
    "filter_clips",
    "group_clips",
    "script_template_recommendation",
    "recommend_transition",
    "recommend_text",
    "plan_timeline",
)
```

`STORYLINE_ENABLE_AI_TRANSITION=1` 时把 `generate_ai_transition` /
`plan_timeline_ai_transition` 加入必需清单(`list_capability(include_ai_transition=True)` 多 2 个)。

---

## 3. MCP 协议事实

### 3.1 启动命令

```bash
# stdio(开发期,本机手动启动)
PYTHONPATH=E:/Documents/kuaishou/FireRed-OpenStoryline/src \
PYTHONIOENCODING=utf-8 \
  python -m open_storyline.mcp.server

# streamable-http(生产期,常驻)
# 同上命令 + config.toml [local_mcp_server] 默认 port=8001 path=/mcp
# server.settings.host = 127.0.0.1
```

### 3.2 强制请求头

| Header | 含义 | 取值 |
| --- | --- | --- |
| `X-Storyline-Session-Id` | FireRed session 标识 | `<job_id>-storyline`(由 Adapter 注入) |

### 3.3 强制请求体字段

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `params.arguments.artifact_id` | 当前调用 artifact id | uuid4 hex |

### 3.4 响应统一形状(register_tools.py wrapper)

```python
{
    "artifact_id": "<uuid>",
    "tool_excute_result": { ... },   # 业务结果(各 Node 不一)
    "summary": "<str>",
    "isError": <bool>,
}
```

---

## 4. 上游漂移告警

每次 `OpenStorylineMCPClient.__aenter__` 跑 `list_tools()`,把当前可用的 tool name
与 `REQUIRED_TOOLS` 比对,缺失即抛 `ToolNotFound`。错误信息带 `missing` /
`available`,便于一次发现。

Phase 1 的降级基线由 `storyline/firered_adapter.TOOL_REGISTRY` 维护,新增 tool 时:

1. 在 FireRed `nodes/core_nodes/<name>.py` 加 `meta = NodeMeta(name="...", ...)`。
2. 在 `storyline/firered_adapter.TOOL_REGISTRY` 加对应 `ToolSpec(...)`。
3. 在 `tests/integration/test_storyline_tools_snapshot.py` 更新快照文件。
4. 在本文件「必需 capability」表中更新状态。

---

## 5. 参考源码

| 路径 | 关键行 |
| --- | --- |
| `FireRed-OpenStoryline/src/open_storyline/mcp/register_tools.py` | `create_tool_wrapper` L21-89;`register` L92-114 |
| `FireRed-OpenStoryline/src/open_storyline/mcp/server.py` | `create_server` L15-48;`main` L50-55 |
| `FireRed-OpenStoryline/src/open_storyline/nodes/core_nodes/*.py` | 每文件 `meta = NodeMeta(name="...", ...)` |
| `FireRed-OpenStoryline/config.toml` | L33-55 `[local_mcp_server]` + `available_nodes` |
| `FireRed-OpenStoryline/src/open_storyline/config.py` | `MCPConfig` L110-129 |
| `auto-video-editor/storyline/firered_adapter.py` | `TOOL_REGISTRY` 22 entry / `REQUIRED_TOOLS` 9 / `OPTIONAL_TOOLS` 13 / `FireredConfigKeys` 14 sections + 8 mcp_keys |
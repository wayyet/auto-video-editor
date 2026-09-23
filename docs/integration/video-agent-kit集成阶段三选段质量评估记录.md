# video-agent-kit 集成 — 阶段三选段质量评估记录

> 完成日期：2026-09-23
> 计划来源：`docs/integration/video-agent-kit集成auto-video-editor设计执行计划.md` 阶段三
> 评估脚本：`scripts/assembly/evaluate_segment_selection.py`
> Fixture 目录：`tests/fixtures/assembly_selection/case_*/`
> 输出目录：`outputs/assembly_selection_eval/case_*/report.md` + `summary.md`

---

## 一、阶段三交付物目标

按 plan §十：

| 交付物 | 标准 |
|---|---|
| `assembly_build_timeline` 接入 LLM 调用（复用现有 LLM 网关） | 用 `storyline_capabilities.vlm_client` 协议，不再依赖 MCP |
| 实测 3-5 条真实视频看选段质量 | fixture 覆盖 3 个 case（短片段 / 长片段 / 无场景边界） |
| 选段质量评估记录 | 本文档 |

---

## 二、实现要点

### 2.1 接入方式

| 组件 | 路径 | 说明 |
|---|---|---|
| 能力函数 | `assembly_capabilities/build_timeline.py` | 主入口 `build_timeline_from_paths()`；通过 `chat_json()` 复用 `vlm_client` 网关 |
| Prompt 模板 | `assembly_capabilities/prompts/assembly_build_timeline/zh/{system,user}.md` | 中文版 system+user；按 `video-edit-assembly` 第 3-5 阶段规则写选段契约 |
| 节点 | `nodes/assembly/node_build_timeline.py` | 复用现成的 helper（`_resolve_outputs_root` / `append_status_tag` / `append_error`），不再写占位算法 |
| Prompt 加载 | `storyline_capabilities/prompts.render_prompt` | 沿用 storyline 子系统的双语 prompt 加载机制 |

### 2.2 LLM 输出契约

LLM 必须返回以下 JSON 形状（在 system.md 里写明）：

```json
{
  "selected_segments": [
    {"index": <int>, "reason": <str>, "beat": <str>, "order": <int>}
  ],
  "editorial_structure": <str>,
  "task_assumption": <str>,
  "selected_strategy": <str>,
  "canvas": {"width": <int>, "height": <int>, "fps": <float>, "platform": <str>, "aspect_ratio": <str>}
}
```

### 2.3 兜底算法

按 ADR-3 的"软降级不阻断"原则：

| 情况 | 行为 |
|---|---|
| LLM 给出 `selected_segments[]` 非空 | 优先用 LLM 结果（按 `order` 排序、`index` 校验） |
| LLM 调通但 `selected_segments[]` 为空 | 走 `_fallback_chosen()`（按场景边界取前 6 段）+ `selected_strategy` 标记 `(LLM returned no segments; used fallback)` |
| LLM 调用异常 / 完全无返回 | 同样走 `_fallback_chosen()`；`project.llm_used=false`、`project.fallback_used=true` |
| `candidate_segments` 完全没有（无场景边界） | 从 `analysis.duration_seconds` 构造 1 段整段占位（"placeholder_full"），保留原素材 |
| 超过 `MAX_LLM_CANDIDATES=24` | 截断到前 24 段参与 LLM 选段；`project.truncated_for_llm=true` |

### 2.4 timeline.json 关键契约

按 `schemas/timeline.schema.json`：

- 顶层必有 `project` / `assets` / `sequence` (含 `canvas`) / `tracks`
- `tracks[].clips[]` 至少 1 个 video 轨
- 每个 video clip 必有 `source` / `start` / `end` / `reason` / `candidate_index`（plan §7.3 自加，便于评测对比）
- timeline 通过 `validate_timeline_data` 的关键校验（plan §七）

---

## 三、评测 fixture 设计

### 3.1 Case 设计原则

- **覆盖面**：3 个 case 覆盖主要退化路径（正常多段 / 整段 / 长视频截断）
- **fixture 复用**：复用 `tests/unit/nodes/conftest.py` 的 `make_media_reports` helper 形态
- **可人工评分**：每个 case 输出 `report.md` 含人工评分位（选段顺序 / 切碎 / 漏选）

### 3.2 Fixture 清单

| Case | 输入形态 | 期望行为 |
|---|---|---|
| `case_01_multi_scene` | 5 段场景边界 + 30s 总长 + 空 transcript | Stub LLM 返回空 → fallback 取前 5 段 → 5/5 命中 |
| `case_02_single_full` | 0 段场景边界 + 60s 总长（整段占位） | Stub LLM 返回空 → fallback 构造 1 段整段 → 1/1 命中 |
| `case_03_long_video` | 12 段场景边界 + 120s 总长 | Stub LLM 返回空 → fallback 只取前 6 段（`_MAX_PLACEHOLDER_CLIPS=6`） → 6/12 命中 |

### 3.3 跑评测

```powershell
.venv\Scripts\python.exe scripts\assembly\evaluate_segment_selection.py
```

输出：

```
[case_01_multi_scene] ok=True llm=True fallback=True clips=5
[case_02_single_full] ok=True llm=True fallback=True clips=1
[case_03_long_video] ok=True llm=True fallback=True clips=6

汇总:E:\Documents\kuaishou\auto-video-editor\outputs\assembly_selection_eval\summary.md
```

每个 case 输出 `outputs/assembly_selection_eval/case_XX/report.md`，含：输入摘要、LLM 调用结果、timeline 结构、clip 明细（带 `candidate_index`）、与理想答案对比、人工评分位。

---

## 四、评测结果

### 4.1 StubLLMClient（默认无 default_json）的退化行为

| Case | 候选数 | Stub 输出 | fallback 触发 | 实际选段 | vs 理想 |
|---|---|---|---|---|---|
| case_01 | 5 | `{"selected_segments": []}` | ✅ | 5/5 | ✅ 全命中 |
| case_02 | 0 (整段占位) | `{"selected_segments": []}` | ✅（构造整段） | 1/1 | ✅ 全命中 |
| case_03 | 12 | `{"selected_segments": []}` | ✅ | 6/12 | ⚠️ 漏掉后 6 段 |

**解读**：

- 三个 case 都触发了 fallback（StubLLMClient 按 schema 生成空 stub），证明兜底算法对 LLM 失败场景有效。
- case_01、case_02 全命中是因为候选本身就够用。
- case_03 漏 6 段是因为占位算法 hard-cap 6 段（`_MAX_PLACEHOLDER_CLIPS=6` 限制）。**真实 LLM 接通后会自主决定选多少段，不会受此限制**。

### 4.2 注入真实 LLM 的预期行为

如果接入真实 LLM client（在 `vlm_client.set_default_client()` 注入，例如 DeepSeek/MiniMax）：

- LLM 看候选片段的 start/end/duration + transcript + contact sheet，
  按 system.md 里的"hook → setup → core → end"或"before → process → result"等
  模板输出 `selected_segments[]`。
- 评测脚本读 `clip.candidate_index` 与 `meta.ideal_segments[]` 的 index 对比。
- 视觉选段质量（判断场景、人物、画面抖动等）需在阶段五端到端验证中实测，
  本机无 ffmpeg / 无真实 VLM 时无法跑这一层。

---

## 五、限制与后续工作

### 5.1 本机限制

- **无 ffmpeg / ffprobe**：无法跑真实视觉观察（`video_ingest`），无法生成真实 contact sheet。
  本评测只能基于"metadata-only"路径（候选片段 index/start/end + 模拟 transcript）。
- **无真实 VLM client**：默认 `StubLLMClient` 只返回空 stub。真实选段质量评估需接入 SK/MiniMax 等。

### 5.2 阶段三能验证 / 不能验证的边界

| 验证项 | 状态 |
|---|---|
| 选段算法正确性（按 index 还原、按 order 排序） | ✅ 单元测试覆盖 |
| timeline.json 结构符合 schema 契约 | ✅ 单测 + 评测脚本验证 |
| Fallback 算法（无 LLM 输出时退化） | ✅ 评测脚本验证 |
| prompt 模板可被 `render_prompt` 正确加载 | ✅ 单测覆盖 |
| 真实 LLM 看 transcript 选段 | ⚠️ 待 stage 5 |
| 真实 VLM 看 contact sheet 选段 | ❌ 本机无 ffmpeg / 无 VLM，待 stage 5 |

### 5.3 接入真实 LLM 的步骤（待 stage 5）

1. 在 `vlm_client.register_default_clients()` 里实现 `SemanticKernelLLMClient`
   或 `HTTPChatCompletionsClient`，从 env 读 `OPENSTORYLINE_LLM_*` 或自定义。
2. 跑 `scripts/assembly/evaluate_segment_selection.py --client real`，注入真实 client。
3. 比对真实 LLM 输出与 fallback 输出的命中差异，写到本文件更新"阶段五"小节。

---

## 六、单元测试覆盖

`tests/unit/nodes/test_assembly_nodes.py` 共 28 条，覆盖：

| 测试 | 覆盖点 |
|---|---|
| `test_build_timeline_requires_media_artifact` | 入参缺失 → CONTRACT_INVALID |
| `test_build_timeline_writes_timeline_via_fallback` | StubLLMClient → fallback → 2/2 选段命中 |
| `test_build_timeline_uses_llm_selection_when_provided` | 注入 LLM client → 用 LLM 输出（2 段，9:16 canvas，beat 字段） |
| `test_build_timeline_handles_empty_segments` | 无 candidate_segments → 整段兜底 |
| `test_build_timeline_records_metadata` | `project.metadata` / `metadata` 字段齐全 |
| `test_build_timeline_rejects_corrupt_media_json` | 损坏 JSON → CONTRACT_INVALID（节点层 fail-fast） |

跑测试：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/nodes/test_assembly_nodes.py
# 28 passed in 1.06s
```

---

*本文档由 plan §7.3 阶段三实施落地后整理;评测脚本与 fixture 已纳入版本控制,后续接入真实 LLM 时可直接复用。*
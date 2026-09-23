# video-agent-kit 核心能力集成 auto-video-editor 设计执行计划

> 制定日期：2026-09-22
> 核查方式：不是照抄用户上传的分析报告，而是用 `git clone` 把 `wayyet/video-agent-kit`（`0.4.3` 子目录）和 `wayyet/auto-video-editor` 两个仓库**当前最新代码**都拉到本地，逐文件读源码后再定方案。
> 前置说明：克隆下来发现，`jianying-editor 技能集成` 与 `OpenStoryline 迁移解耦` 这两项前置任务**已经落地**（仓库里已有 `vendor/pyJianYingDraft/`、`storyline_capabilities/`、`nodes/storyline/` 19 个节点）。本方案是在这个新基线上做设计，不是在旧版本上。

---

## 一、这次要做什么（一句话版）

把 `video-agent-kit` 里 `video-edit-assembly` 技能的**完整流程**（发现素材→探查→转写→看画面→拼 timeline→校验→渲染预览→质检→修复→写报告），加上 `video-edit-agent` 里除"路由到哪个子技能"之外的**强制质检闭环规则**，搬进 `auto-video-editor`。搬的时候把代码原样拷贝进来，不装 MCP 协议库、不跑网络请求去调 video-agent-kit——两个项目从此没有任何调用关系，`auto-video-editor` 单独就能跑完整个 17 步流程。

**MCP**：Model Context Protocol，一种让 AI 助手调用外部工具的标准协议。本方案要去掉它，改成普通 Python 函数调用。

---

## 二、核查依据

| 核查对象 | 方法 | 结论 |
|---|---|---|
| `video-edit-agent/SKILL.md`（183 行） | 完整读取真实文件 | "通用硬规则"共 10 条，第 8 条就是用户列的那 9 项功能（QC + 8 个流程步骤） |
| `video-edit-assembly/SKILL.md`（144 行） | 完整读取真实文件 | 7 个阶段的完整多素材组装流程，明确声明"遵循 video-edit-agent 的通用文件契约" |
| `mcp/ve_tools/*.py`（5 个文件，共 4934 行） | 用 `grep` 提取全部函数签名 | 8 个工具全部实现，签名统一为 `(args: dict, ctx: RunContext) -> ToolResult` |
| `mcp/ve_tools/result.py` | 完整读取（14 行，很短） | `ToolResult` 是纯数据类：`text/data/artifacts/image_paths/video_paths` |
| `requirements.txt` | 完整读取 | 无 `torch`/`moviepy`，与 `auto-video-editor` 现有"纯 Python 无重型依赖"的约束不冲突 |
| `schemas/timeline.schema.json`（231 行） | 完整读取 | video-agent-kit 有自己的一套 Timeline JSON 结构，跟 `auto-video-editor` 现有的 `CanonicalTimeline`、剪映 `draft_content.json` 都不是一回事，三者需要适配层 |
| `auto-video-editor/graph.py` | `grep` 提取全部 `add_node`/`add_edge` | 确认了真实的节点接线，找到本次新增节点应该插在哪 |
| `nodes/storyline/_common.py`、`node_understand_clips.py` | 完整读取 | 抄下现成的节点写法范式（见第七节），新代码风格要跟这个对齐 |

---

## 三、集成范围：纳入与排除

### 3.1 纳入

| 来源 | 纳入内容 |
|---|---|
| `video-edit-assembly` | 完整 7 阶段流程（发现物料与约束解析 → 探查源素材 → 分组评分选段 → 定版式与结构 → 搭建 timeline → 校验/渲染/质检/自查 → 报告） |
| `video-edit-agent` | 仅"通用硬规则"里第 8 条列的闭环步骤（媒体探查 / ASR / 视频视觉观察 / timeline 生成 / timeline 校验 / preview 渲染 / QC / timeline_diff 修复循环 / 最终 report.md），以及它规定的统一文件命名契约 |

### 3.2 明确排除

| 排除项 | 原因 |
|---|---|
| `env-setup`、`video-speech-workflows`、`video-recap-workflows` 三个 Skill | 用户明确要求"其他 skill 不用添加" |
| `video-edit-agent` 的路由规则（判断任务该走 assembly / speech / recap 哪条路） | 用户明确要求不添加；`auto-video-editor` 本身就是固定 17 步流程，不需要"选哪条路"这一层 |
| MCP 工具 `video_watch_segment`（局部重看片段） | 只属于 `video-edit-agent`，`video-edit-assembly` 自己的 8 个工具里没有它 |
| MCP 工具 `video_basic_operation`（裁剪/拼接/变速等底层操作） | 同上，assembly 不需要 |
| MCP 工具 `speech_synthesize`/`tts_generate`（配音合成） | 同上；且 `auto-video-editor` 已有自己的英文配音节点（第 17 步） |

### 3.3 一个要先说清楚的技术判断

`video-edit-assembly` 第 8 项"timeline 生成"，**不是纯代码算法**——原项目里是 Claude 自己看 `video_ingest` 返回的画面截图（contact sheet），判断选哪段、怎么排。这一步在本方案里要做成一个**会调用 LLM/VLM 的节点**（跟仓库里现成的 `storyline_generate_script` 节点做法一样），不是零 AI 调用的确定性代码。其余 7 项（探查/ASR/观察/校验/渲染/QC/修复）才是纯代码、不调模型。

---

## 四、架构决策记录（ADR）

### ADR-1：完全解耦——只搬代码，不搭协议

**决定**：把 `mcp/ve_tools/media.py`、`video_observe.py`、`timeline.py`、`render.py`、`qc.py` 五个文件里跟 8 个目标工具相关的纯函数**代码搬进来**，新建 `assembly_capabilities/` 目录承接，按普通 Python 函数调用；不引入 `mcp` 这个协议库，不启动 MCP Server，不连接 `video-agent-kit` 仓库。

**理由**：这跟 `OpenStoryline 迁移解耦` 已经用过、验证过的方式完全一样——那次也是把 OpenStoryline 的 19 个节点从"MCP 远程调用"改成"本地 `storyline_capabilities/*.py` 纯函数"。沿用同一套模式，风险最低。

**后果**：`video-agent-kit` 那边 `mcp/video_edit_server.py`（MCP 协议包装层，80 行）、`server_common.py`、`mcp` 这个 pip 包全部不需要搬。只搬 5 个纯逻辑文件。

### ADR-2：接入点——插在"生成初始草稿"和"人工调整分镜"之间

**决定**：新增 6 个节点，插入到现有的 `generate_draft`（第 5 步）→ `node_06_human_reorder`（第 6 步，关卡①）这条边中间。

```
现状：generate_draft ──────────────────────────► node_06_human_reorder

改后：generate_draft ─► assembly_discover_and_probe
                      ─► assembly_asr_and_visual_observe
                      ─► assembly_build_timeline
                      ─► assembly_validate_render_qc ─┬─► assembly_write_report ─► node_06_human_reorder
                                                        └─► assembly_repair_loop（不达标则回这里重试）
```

**理由**：
1. 这 6 个节点要处理的原始素材（`video_input_path`），第 4 步 `import_and_plan` 已经导入过一次了，此时素材已经就绪，不用重新等待。
2. 关卡①本来就是"人工在剪映里看草稿、调分镜顺序"——现在人工审核时，除了剪映草稿，还能同时拿到一份**独立渲染的预览视频 + 质检报告**，作为交叉参考。
3. 不管当前走的是 `STORYLINE_MODE=human` 还是 `auto`（仓库现有的两种模式），到 `generate_draft` 完成这一刻，草稿文件肯定已经生成，插入点对两种模式都成立，不用分别处理。

### ADR-3：默认强制开启，失败走软降级（用户已确认）

**决定**：新增开关 `ASSEMBLY_QC_GATE_ENABLED`，**默认值 `true`**——每条视频强制走一遍质检。质检不通过、重试到上限后，**不硬性中断整条流水线**，而是把未解决的问题如实写进 `report.md`，照常进入关卡①，交给人工判断。

**理由**：用户已明确选择"默认开启，每条视频强制过 QC"这一版。同时参照仓库里 `storyline_qa_gate` 现成的"软失败不阻断"设计惯例——质检本身是用来给人工提供更多信息的，不应该变成一个会让整条流水线随机失败的硬门槛。

**代价**（需要如实记录，不能回避）：这条链路加进临界路径后，每条视频在到达关卡①之前，会多出真实的 FFmpeg 渲染时间、ASR 转写时间和抽帧看图时间——这是"强制开启"必然要付出的等待成本。

### ADR-4：`RunContext` 改造为"每个节点各自新建一份"

**决定**：`video-agent-kit` 原本的 `RunContext`（在 `mcp/ve_tools/run_context.py`，类似一个"本次会话的记事本"，会记住"这个视频是不是已经转写过""是不是已经看过画面"，避免同一次对话里重复做同样的事）是为**长对话场景**设计的——一次会话里可能调用同一个工具很多次。搬进 LangGraph 后，改成**每个节点各自 `RunContext(session_kind="pipeline")` 新建一份**，不做跨节点的记忆复用。

**理由**：`auto-video-editor` 现有的 `storyline_capabilities` 也是这个模式——节点之间靠状态里存的文件路径（`state["storyline_xxx_artifact"]`）传递结果，不靠一个常驻内存对象。而且在本方案的设计里，每个工具在一次任务里本来就只会被调用一次，用不上"避免重复调用"这个记忆功能。

**待实现阶段核实项**：`RunContext.resolve()`/`virtualize()` 两个路径解析方法的具体行为（例如是否要求素材路径必须在某个"项目根目录"之内），需要在编码阶段对照 `run_context.py` 全文再核实一遍，本文档目前只确认了方法存在、签名是 `resolve(self, path: str | Path) -> Path`。

### ADR-5：新分类目录名沿用现有的命名习惯

**决定**：本地能力代码放在 `assembly_capabilities/`（跟现有 `storyline_capabilities/` 对称命名）；新节点放在 `nodes/assembly/`（跟现有 `nodes/storyline/` 对称）；节点内部的公共 helper **直接复用**现有的 `nodes/storyline/_common.py`（`_resolve_outputs_root`、`append_status_tag`、`append_error` 三个函数是纯通用逻辑，不含任何 OpenStoryline 专属内容，可以直接 import，不用抄一份）。

---

## 五、目录与文件清单

| 路径 | 类型 | 说明 |
|---|---|---|
| `assembly_capabilities/media_probe.py` | 新增 | 搬 `inspect_media`、`analyze_media`（源自 `video-agent-kit/mcp/ve_tools/media.py` 第 186、239 行） |
| `assembly_capabilities/speech_asr.py` | 新增 | 搬 `speech_transcribe`（源自 `media.py` 第 513 行；`transcribe` 只是它的兼容别名，不用重复搬） |
| `assembly_capabilities/visual_observe.py` | 新增 | 搬 `video_ingest`（源自 `video_observe.py` 第 56 行） |
| `assembly_capabilities/timeline_ops.py` | 新增 | 搬 `validate_timeline`（`timeline.py` 第 153 行）、`timeline_diff`（`timeline.py` 第 750 行） |
| `assembly_capabilities/render_preview.py` | 新增 | 搬 `render_preview`（`render.py` 第 29 行） |
| `assembly_capabilities/qc_preview.py` | 新增 | 搬 `qc_preview`（`qc.py` 第 17 行） |
| `assembly_capabilities/run_context.py` | 新增（改造版） | 精简过的 `RunContext`，去掉长对话记忆逻辑，只保留 `resolve`/`virtualize`/路径处理 |
| `assembly_capabilities/result.py` | 新增（原样搬） | `ToolResult` 数据类，14 行，原样拷贝即可 |
| `nodes/assembly/node_discover_and_probe.py` | 新增 | 素材发现 + `inspect_media`/`analyze_media` |
| `nodes/assembly/node_asr_and_visual_observe.py` | 新增 | `speech_transcribe` + `video_ingest` |
| `nodes/assembly/node_build_timeline.py` | 新增 | 选段/排序（调 LLM）+ 写出 `timeline.json` |
| `nodes/assembly/node_validate_render_qc.py` | 新增 | `validate_timeline` + `render_preview` + `qc_preview` 三连 |
| `nodes/assembly/node_repair_loop.py` | 新增 | `timeline_diff` 修复 + 回路由 |
| `nodes/assembly/node_write_report.py` | 新增 | 写 `report.md` |
| `state.py` | 修改 | 追加 `assembly_*` 系列新字段（见第六节） |
| `config.py` | 修改 | 追加 `ASSEMBLY_QC_GATE_ENABLED`、`ASSEMBLY_QC_MAX_RETRY` 两个开关 |
| `graph.py` | 修改 | 把 `generate_draft → node_06_human_reorder` 这一条边改成经过新节点（见第八节） |
| `requirements.txt` | 修改 | 新增 `opencv-python-headless`、`numpy` 两行（`pillow`、`requests` 已经在里面了） |

---

## 六、State 字段设计

跟现有 `storyline_*` 字段用一样的写法（`Annotated[Optional[str], _last_wins]` 表示"最后一次写入生效"，`NotRequired[int]` 表示"唯一写入者，不需要合并规则"）：

```python
# --- assembly QC 通道(video-agent-kit 移植,ADR-1~5) ---
assembly_media_artifact: Annotated[Optional[str], _last_wins]            # inspect_media/analyze_media 汇总结果路径
assembly_transcript_artifact: Annotated[Optional[str], _last_wins]       # speech_transcribe 结果路径
assembly_ingest_artifact: Annotated[Optional[str], _last_wins]           # video_ingest 结果(contact sheet 清单)路径
assembly_timeline_path: Annotated[Optional[str], _last_wins]             # 组装出的 timeline.json(video-agent-kit 自有 schema)
assembly_timeline_validation_path: Annotated[Optional[str], _last_wins]  # validate_timeline 输出路径
assembly_preview_path: Annotated[Optional[str], _last_wins]              # render_preview 输出的 mp4 路径
assembly_qc_report_path: Annotated[Optional[str], _last_wins]            # qc_preview 输出路径
assembly_report_path: Annotated[Optional[str], _last_wins]               # 最终 report.md 路径
assembly_qc_status: Annotated[Optional[str], _last_wins]                 # "pass" | "pass_with_warnings" | "escalated"
assembly_qc_retry_count: NotRequired[int]                                # 修复循环重试计数,唯一写入者,无需 reducer
```

产物统一落在 `outputs/<job_id>/assembly/` 目录下（跟现有 `outputs/<job_id>/storyline/` 平级），文件命名照抄 `video-edit-assembly` 的文件契约：`media.json`、`transcript.json`、`video_ingest.json`、`timeline.json`、`timeline_validation.json`、`preview.mp4`、`preview_qc_report.json`、`report.md`。

---

## 七、六个新节点详细设计

所有节点统一签名 `def xxx_node(state: WorkflowState) -> dict`，统一套用 `nodes/storyline/node_understand_clips.py` 现成的写法范式：先取必需的上游产物路径→缺失就提前返回错误→`try/except` 包住真正的能力调用→写产物 JSON→返回新增字段 + `status_log`。

### 7.1 `assembly_discover_and_probe`（素材发现 + 媒体探查）

对应 video-agent-kit 工具：`inspect_media`、`analyze_media`

```python
# nodes/assembly/node_discover_and_probe.py
from __future__ import annotations

import json
from pathlib import Path

from state import WorkflowState
from nodes.storyline._common import _resolve_outputs_root, append_status_tag, append_error
from assembly_capabilities.media_probe import inspect_media, analyze_media
from assembly_capabilities.result import ToolResult
from assembly_capabilities.run_context import RunContext


def assembly_discover_and_probe_node(state: WorkflowState) -> dict:
    outputs_root = _resolve_outputs_root(state)
    out_dir = outputs_root / "assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    video_input_path = state.get("video_input_path")
    if not video_input_path:
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="CONTRACT_INVALID",
                message="missing video_input_path",
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    # 素材发现:对 video_input_path 做目录/单文件扫描,产出候选素材清单
    # (video-agent-kit 原项目里这一步由 Skill 自己用普通文件系统操作完成,
    #  不是 MCP 工具,这里同样用普通 Python 实现,不调用任何外部服务)
    candidates = _discover_source_files(Path(str(video_input_path)))

    ctx = RunContext(session_kind="pipeline")
    media_reports: list[dict] = []
    try:
        for src in candidates:
            probe: ToolResult = inspect_media({"input_path": str(src)}, ctx)
            analysis: ToolResult = analyze_media({"input_path": str(src)}, ctx)
            media_reports.append({
                "source": str(src),
                "probe": probe.data,
                "analysis": analysis.data,
            })
    except Exception as e:  # noqa: BLE001
        return {
            "error_log": append_error(
                state, node_kind="assembly_discover", error_code="TOOL_EXECUTION_FAILED",
                message=repr(e),
            ),
            "status_log": append_status_tag(state, "assembly_discover_failed"),
        }

    out_path = out_dir / "media.json"
    out_path.write_text(json.dumps(media_reports, ensure_ascii=False), encoding="utf-8")
    return {
        "assembly_media_artifact": str(out_path),
        "status_log": append_status_tag(state, "assembly_discover_done"),
    }
```

### 7.2 `assembly_asr_and_visual_observe`（ASR + 视频视觉观察）

对应工具：`speech_transcribe`、`video_ingest`

```python
# nodes/assembly/node_asr_and_visual_observe.py（结构与 7.1 一致,核心调用片段如下）
transcript_result = speech_transcribe({"input_path": str(src), "output_json": str(transcript_out)}, ctx)
# video_ingest 官方说明:标准流程是先转写,再把转写结果路径传进去,
# 这样每张截图都能配上对应的语音文字
ingest_result = video_ingest(
    {"input_path": str(src), "transcript_path": str(transcript_out), "output_json": str(ingest_out)},
    ctx,
)
```

**要点**：`video_ingest` 是这条链路里最重的一步——它把整段视频按时间轴抽成一系列带时间戳的截图（contact sheet），返回给后续节点做视觉判断用。截图会写到磁盘（`outputs/<job_id>/assembly/frames/`），需要在实施阶段确认磁盘空间预算。

### 7.3 `assembly_build_timeline`（timeline 生成——需要调 LLM）

**这是唯一需要接入大模型的节点**。输入是前两步产出的 `media.json`、`transcript.json`、`video_ingest.json`（含截图路径），让模型看截图 + 读转写文字，参照 `video-edit-assembly` 第 3-5 阶段的规则（分组评分选段 → 定版式与结构 → 搭建 project 风格 timeline）做选段和排序判断，最终按 `schemas/timeline.schema.json` 的结构写出 `timeline.json`。

**建议实现方式**：复用仓库里已经跑通的 `storyline_generate_script` 节点调 LLM 的方式（同一个 LLM 网关、同一套重试与超时封装），不用另起一套模型调用链路。

### 7.4 `assembly_validate_render_qc`（校验 + 渲染 + 质检三连）

对应工具：`validate_timeline`、`render_preview`、`qc_preview`

```python
# nodes/assembly/node_validate_render_qc.py（核心片段）
validate_result = validate_timeline(
    {"timeline_path": timeline_path, "output_json": str(validation_out)}, ctx
)
render_result = render_preview(
    {"timeline_path": timeline_path, "output_path": str(preview_out)}, ctx
)
qc_result = qc_preview(
    {"video_path": str(preview_out), "timeline_path": timeline_path, "output_json": str(qc_out)}, ctx
)

# qc_preview 会做黑帧扫描、静音扫描、卡帧扫描、音量峰值/均值、
# timeline 与渲染产物的哈希绑定校验等,失败项区分"阻断级"和"警告级"
qc_data = qc_result.data
status = "pass" if not qc_data.get("blocking_issues") else "escalated"
if qc_data.get("warning_issues") and status == "pass":
    status = "pass_with_warnings"
```

### 7.5 `assembly_repair_loop`（timeline_diff 修复循环）

对应工具：`timeline_diff`。仿照仓库现有 `node_07_speed_fit` 的"护栏节点 + 条件边重试"写法（已验证的成熟模式）：

```python
# graph.py 里的路由函数
def route_after_assembly_qc(state: WorkflowState) -> str:
    status = state.get("assembly_qc_status")
    retry = int(state.get("assembly_qc_retry_count") or 0)
    if status == "pass" or status == "pass_with_warnings":
        return "assembly_write_report"
    if retry >= config.ASSEMBLY_QC_MAX_RETRY:
        # 达到重试上限:不阻断,如实记录进 report.md,交给人工在关卡①判断
        return "assembly_write_report"
    return "assembly_repair_loop"
```

`assembly_repair_loop` 节点内部：根据 `qc_preview`/`validate_timeline` 返回的问题列表，构造 `patch`（`timeline_diff` 支持的修复操作包括：调整片段时长、替换素材、增删轨道等结构化补丁），调用 `timeline_diff(args={"timeline_path": ..., "patch": patch, "apply": True}, ctx)`，写回 `timeline.json`，然后**回到 7.4** 重新走一遍校验/渲染/质检。

### 7.6 `assembly_write_report`（最终 report.md）

汇总前 5 步所有产物（素材清单、转写摘要、选段理由、质检结果、修复记录），写成一份 Markdown 报告，路径写入 `assembly_report_path`。**这份报告会在关卡①的人工通知文案里一并提示**（"本次已生成组装质检报告：`outputs/<job_id>/assembly/report.md`，建议对照剪映草稿一并查看"），但不会阻塞关卡①本身的中断/恢复逻辑。

---

## 八、graph.py 接线改动

现状（已核实的真实代码）：

```python
g.add_edge("generate_draft", "node_06_human_reorder")
```

改为：

```python
g.add_node("assembly_discover_and_probe", assembly_discover_and_probe_node)
g.add_node("assembly_asr_and_visual_observe", assembly_asr_and_visual_observe_node)
g.add_node("assembly_build_timeline", assembly_build_timeline_node)
g.add_node("assembly_validate_render_qc", assembly_validate_render_qc_node)
g.add_node("assembly_repair_loop", assembly_repair_loop_node)
g.add_node("assembly_write_report", assembly_write_report_node)

g.add_edge("generate_draft", "assembly_discover_and_probe")
g.add_edge("assembly_discover_and_probe", "assembly_asr_and_visual_observe")
g.add_edge("assembly_asr_and_visual_observe", "assembly_build_timeline")
g.add_edge("assembly_build_timeline", "assembly_validate_render_qc")
g.add_conditional_edges(
    "assembly_validate_render_qc",
    route_after_assembly_qc,
    {
        "assembly_repair_loop": "assembly_repair_loop",
        "assembly_write_report": "assembly_write_report",
    },
)
g.add_edge("assembly_repair_loop", "assembly_validate_render_qc")
g.add_edge("assembly_write_report", "node_06_human_reorder")
```

若 `ASSEMBLY_QC_GATE_ENABLED=false`（应急关闭场景），`generate_draft` 直接接回 `node_06_human_reorder`，等同于本次改动之前的状态——不需要额外的条件判断节点，接线时按 `config.ASSEMBLY_QC_GATE_ENABLED` 的值二选一即可。

---

## 九、依赖变更

`requirements.txt` 现状已确认包含 `pillow==12.3.0`、`requests==2.34.2`，无需重复添加。新增两行：

```
opencv-python-headless
numpy
```

**待实施阶段核实**：`video-agent-kit` 的 `requirements.txt` 未锁定这两个包的具体版本号，正式接入前需要用实际跑通的版本号替换（避免装到不兼容的新版本）。另外，`inspect_media`/`analyze_media`/`render_preview`/`qc_preview` 这几个工具都依赖 `ffmpeg`/`ffprobe` 命令行工具（不是 Python 包），需要在 Windows 部署清单里确认这两个可执行文件已在系统 `PATH` 里——仓库里已有类似先例（`docs/ffprobe找不到问题排查与修复.md` 记录过 OpenStoryline 那边踩过的同一类坑，可以直接参照那次的排查方法）。

---

## 十、分阶段实施计划

| 阶段 | 内容 | 交付物 |
|---|---|---|
| 阶段一 | 建 `assembly_capabilities/` 目录，搬 5 个工具文件 + `result.py` + 改造版 `run_context.py`；单独写单元测试跑通 8 个函数（不接入 graph） | 8 个工具函数的独立单测报告 |
| 阶段二 | 建 `nodes/assembly/` 6 个节点文件，state.py/config.py 加字段；节点各自单测（mock 掉 `assembly_capabilities` 层） | 6 个节点的单测 |
| 阶段三 | `assembly_build_timeline` 接入 LLM 调用（复用现有 LLM 网关），实测 3-5 条真实视频看选段质量 | 选段质量评估记录 |
| 阶段四 | graph.py 接线，跑通 `generate_draft → ... → node_06_human_reorder` 全链路；验证 `ASSEMBLY_QC_GATE_ENABLED=false` 时行为与改动前一致（回归测试） | 集成测试报告 |
| 阶段五 | 真实素材端到端验证：从 Windows 本机跑一条完整视频，确认 `preview.mp4` 可播放、`report.md` 内容可读、关卡①收到的通知文案正确带上报告路径 | 端到端验证记录 |

---

## 十一、测试与验收标准

| 验收项 | 标准 |
|---|---|
| 解耦验证 | 全仓库 `grep -r "video-agent-kit\|video_edit_server\|from mcp import"` 结果为空（除本文档自身引用说明外），证明真的没有残留依赖 |
| 独立启动 | 删除本地 `video-agent-kit` 仓库文件夹后，`auto-video-editor` 的现有测试套件（当前 202/210 通过）不受影响，新增的 `assembly_*` 测试同样全部通过 |
| 默认开启生效 | 不设置 `ASSEMBLY_QC_GATE_ENABLED` 环境变量时，流水线自动经过 6 个新节点 |
| 应急关闭生效 | 设置 `ASSEMBLY_QC_GATE_ENABLED=false` 时，`generate_draft` 直接到 `node_06_human_reorder`，与改动前行为一致 |
| 软降级验证 | 人为构造一个必然触发 QC 阻断项的素材（如全黑视频），确认重试到 `ASSEMBLY_QC_MAX_RETRY` 后流程仍能走到关卡①，而不是整条流水线崩溃退出 |
| 产物完整性 | `outputs/<job_id>/assembly/` 下 8 个文件（media.json / transcript.json / video_ingest.json / timeline.json / timeline_validation.json / preview.mp4 / preview_qc_report.json / report.md）全部生成 |

---

## 十二、风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| 默认开启后，每条视频在关卡①之前多等一段真实渲染+转写时间 | 用户已知晓并确认接受（ADR-3） | 阶段五端到端测试时实测记录具体耗时，供团队后续评估是否要改成并行分支 |
| `assembly_build_timeline` 的选段质量依赖 LLM，效果不稳定 | 可能选出不合理的片段组合 | `timeline_diff` 修复循环 + 人工在关卡①做最终把关，双重兜底 |
| 新增 `opencv-python-headless` 依赖，未在当前 Windows 部署清单验证过 | 可能出现安装/运行时兼容问题 | 阶段一单测阶段优先在目标 Windows 环境验证，不要等到阶段四才发现 |
| `RunContext` 精简版可能遗漏某个隐藏的路径校验逻辑（ADR-4 已标注待核实） | 可能导致个别边缘路径报错 | 阶段一单测覆盖真实路径样例（含中文路径、带空格路径），提前暴露问题 |
| ASR 转写用的云端服务商与现有第 8 步（`jy_common/asr_client.py`，用的是 FireRedASR2S）不是同一家 | 两套 ASR 结果可能存在文字差异，属预期设计（用途不同：一个给选段用，一个给最终字幕用），不建议合并成一套 | 本文档明确保持两者独立，不做任何复用尝试 |

---

## 十三、参考源码

| 内容 | 来源 |
|---|---|
| `video-edit-agent` SKILL.md | `https://github.com/wayyet/video-agent-kit/blob/main/0.4.3/skills/video-edit-agent/SKILL.md` |
| `video-edit-assembly` SKILL.md | `https://github.com/wayyet/video-agent-kit/blob/main/0.4.3/skills/video-edit-assembly/SKILL.md` |
| 8 个工具实现 | `https://github.com/wayyet/video-agent-kit/tree/main/0.4.3/mcp/ve_tools` |
| Timeline JSON Schema | `https://github.com/wayyet/video-agent-kit/blob/main/0.4.3/schemas/timeline.schema.json` |
| `auto-video-editor` 当前节点写法范式 | `https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_understand_clips.py` |
| `auto-video-editor` 现有开关写法范式 | `https://github.com/wayyet/auto-video-editor/blob/main/config.py`（`ENABLE_TWO_LEVEL_TIMEOUT` 一节） |

---

*本文档由 AI 辅助生成，基于对两个仓库当前最新代码的实际核查（非凭空推测）。第七节代码片段为设计级骨架，标注了"待实现阶段核实"的部分不代表设计有误，而是如实标出了需要在编码阶段对照源码逐行核实的边界，避免过度承诺。*

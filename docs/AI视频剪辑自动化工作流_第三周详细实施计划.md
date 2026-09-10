# 第3周详细分阶段实施计划：节点7-11/13实现、关卡①②联调、心跳监控部署

> 本文档是《AI视频剪辑自动化工作流开发执行计划.md》第12节"分阶段实施计划"中"第3周"一行的颗粒度细化，严格对齐原文档第2、3、4、5.2、6、7、8、11、14节的既有结论，不引入新的架构决策，目标是可直接据此进行编码开发。

**第3周原定范围**：节点7-11、13（含新增`/jianying-adjust-volume`）实现；关卡①/②的interrupt/resume联调；心跳监控脚本部署。
**交付物**：带字幕/特效/BGM的剪映项目、心跳监控脚本。

---

## 0. 范围与前提假设

| 项目 | 内容 |
|---|---|
| 本周输入前提 | 第1周环境已就绪（剪映v5.9.0+hosts屏蔽升级）；第2周节点1-5已实现、加密检测模块已就绪、JianYing MCP Server 与 jianying-editor-skill 的PoC评估报告已产出（14.5节待核实清单） |
| 本周产出范围 | 节点7-11、13实现；关卡①（节点6）/关卡②（节点12）的interrupt/resume联调；心跳监控脚本开发部署 |
| 交付物 | ① 带字幕/特效/BGM的剪映项目（draft_content.json）；② 心跳监控脚本（含Windows任务计划程序注册） |
| 人力假设 | 按2名工程师并行估算（1名侧重LangGraph编排/基础设施，1名侧重剪映领域细节）；若仅1人，建议整体工期按1.6-1.8倍估算，并参考第9节做取舍 |
| 不在本周范围 | 步骤14-17（第4周）、生产Checkpointer切换为PostgresSaver（第5周） |

---

## 1. 推荐排期总览

| 日期 | 工程师A（编排/基础设施方向） | 工程师B（剪映领域方向） |
|---|---|---|
| Day1 周一 | AM：阶段0技术路径确认（全员）<br>PM：阶段1公共基础设施（原子写入、状态schema、心跳写入器接口预留） | AM：阶段0技术路径确认（全员）<br>PM：协助阶段1，搭建步骤8-11共用的fixture测试草稿 |
| Day2 周二 | 节点6/关卡①收尾 + 步骤7护栏节点（含条件边重试、快照②产出） | 步骤8：添加字幕 |
| Day3 周三 | 心跳写入器+监控脚本开发（heartbeat_writer.py + heartbeat_monitor.ps1 + 任务计划程序注册） | 步骤9：转场/特效注入 + 步骤10：花字动画 |
| Day4 周四 | 节点12/关卡②收尾 + LangGraph两级超时配置 | 步骤11：贴纸关联（resource_id挖掘）+ 步骤13新Skill核心逻辑与schema逆向 |
| Day5 周五 | AM：全链路6→13顺序集成 + interrupt/resume联调测试（全员）<br>PM：端到端验收 + 心跳监控实测：kill进程验证告警（全员） | 同左 |

> 8/9/10/11在最终流程图中是**顺序**执行（8→9→10→11，见原文档第4节"中文主线"），但**开发阶段**可基于预置的fixture草稿并行编码，只在Day5做顺序集成——这是缩短工期的关键手段。

---

## 2. 阶段0：技术路径确认（Day1 上午，0.5人日）

前提：第2周应已产出"第三方组件PoC评估报告"（原文档12节）。本阶段是**执行决策**，不是重新做PoC。

| PoC结论 | 步骤8/9/10/11 实现路径 | 步骤7/13 实现路径 |
|---|---|---|
| 采纳 JianYing MCP Server | 通过 LangGraph 标准 MCP client node 调用其工具 | 仍直接调用 pyJianYingDraft（该组件按原文档14.2节仅覆盖5/8/9/10/11，不含7/13） |
| 采纳 jianying-editor-skill | 直接 `import` 其 `scripts/jy_wrapper.py` 中的 `JyProject` 类作为可vendor的Python库 | 同上 |
| 两者均未采纳（3.13兼容性未过 / 许可证不适用等） | 退回原方案，直接调用 pyJianYingDraft | 同上（本就如此） |

**关键结论（无论选哪条路径都成立）**：步骤13（音量/淡入淡出）在两个候选组件的现有描述中均**未被提及覆盖**，需按"待补齐"原样自研，与本阶段的技术路径决策无关——第4.8节给出独立方案。

**本阶段产出**：一份1页纸决策记录（团队自行归档），注明选定路径及理由。

---

## 3. 阶段1：公共基础设施（Day1 下午，0.5人日，先于各节点开发）

所有节点共用，避免7个节点各自重复造轮子。

### 3.1 原子写入工具（对应原文档6.3节）

```python
# jy_common/draft_writer.py
import json
import os
import tempfile
from pathlib import Path

def atomic_write_draft_json(draft_path: str, data: dict) -> None:
    """临时文件写入 → 校验JSON合法性 → 操作系统级重命名覆盖（6.3节写入安全规范）
    所有步骤7-13的节点必须通过本函数写回draft_content.json，禁止直接open().write()
    """
    draft_path = Path(draft_path)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=draft_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with open(tmp_path, "r", encoding="utf-8") as f:
            json.load(f)  # 二次读取校验JSON合法性
        os.replace(tmp_path, draft_path)  # Windows NTFS下os.replace是原子操作
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
```

**单元测试要求**：模拟写入中途被杀进程（写临时文件到一半时抛异常），验证原 `draft_content.json` 内容未被破坏。

### 3.2 状态schema扩展

```python
# graph/state.py（在第2周已有schema基础上新增本周字段）
class WorkflowState(TypedDict):
    # ...第2周已有字段（thread_id、draft_path、shot_plan等）
    snapshot2_path: str          # 步骤7产出的快照②路径（分叉点，供后续及第4周英文分支使用）
    reorder_notified: bool       # 关卡①前置通知幂等标记（5.2节坑③）
    bgm_notified: bool           # 关卡②前置通知幂等标记
    retry_counts: dict[str, int] # 各护栏节点重试计数，如 {"node07_speed_fit": 2}
    status_log: list[str]        # 各节点执行完成打点，用于联调测试校验是否重跑
```

### 3.3 建议项目结构

```
video_workflow/
├── graph/
│   ├── state.py
│   ├── build_graph.py              # 图组装：add_node + add_edge/add_conditional_edges
│   └── nodes/
│       ├── node06_human_reorder.py
│       ├── node07_speed_fit.py
│       ├── node08_add_subtitles.py
│       ├── node09_inject_fx.py
│       ├── node10_inject_text_fx.py
│       ├── node11_inject_sticker.py
│       ├── node12_human_add_bgm.py
│       └── node13_adjust_volume.py
├── jy_common/
│   ├── draft_writer.py             # 3.1
│   └── mcp_client.py                # 若阶段0选定MCP路径才需要
├── monitoring/
│   ├── heartbeat_writer.py
│   └── heartbeat_monitor.ps1
├── tests/
│   ├── test_node07_speed_fit.py
│   ├── test_interrupt_resume.py
│   └── fixtures/                    # 预置测试草稿，支撑8-11并行开发
└── config/settings.py                # 心跳阈值、重试上限等配置常量
```

---

## 4. 核心节点/技能详细设计

### 4.1 节点6 + 关卡①：人工调整分镜顺序（收尾/联调准备）

对应原文档7节关卡①。若第2周仅搭了骨架，本阶段补齐幂等处理与恢复逻辑。

```python
# graph/nodes/node06_human_reorder.py
from langgraph.types import interrupt

async def node06_human_reorder(state: WorkflowState) -> WorkflowState:
    # 前置副作用（发通知）必须幂等，防止interrupt()前逻辑被重放时重复发送（5.2节坑③）
    if not state.get("reorder_notified"):
        send_notification(
            thread_id=state["thread_id"],
            message="请在剪映客户端手动调整分镜顺序，完成后确认继续",
        )
        state["reorder_notified"] = True

    interrupt({
        "checkpoint": "关卡①-分镜顺序调整",
        "draft_path": state["draft_path"],
    })
    state["status_log"].append("checkpoint1_resumed")
    return state
```

**更稳妥的替代设计**（若测试发现幂等标记仍有漏洞）：把 `send_notification` 整体挪到本节点**之前**的一个独立"通知节点"，或者干脆挪到 `Command(resume=True)` **之后**才做任何有副作用的操作——原文档5.2节明确建议"优先把有副作用的动作挪到恢复之后执行，从根本上避免重放问题"，这是比幂等标记更彻底的方案，若本节点改造成本不高，建议优先采用这个方案而非幂等标记。

### 4.2 步骤7：`/jianying-speed-fit-35s`（护栏节点，分叉点）

写入字段：`materials.speeds`、`tracks[].segments`（6.1节）。

**设计要点**：原文档8节强调LangGraph的追踪优势是"每个决策点、分支、重试为何如此天然可审计"——因此重试逻辑用**图级条件边**表达，而非节点内部隐藏的while循环，这样每次重试在LangGraph的执行追踪里都是独立可见的一跳。

```python
# graph/nodes/node07_speed_fit.py
TARGET_DURATION_S = 35.0
MAX_RETRY = 3

async def node07_speed_fit(state: WorkflowState) -> WorkflowState:
    """幂等：每次进入都基于当前draft重新计算，不依赖内存中的历史调用状态"""
    draft = load_draft(state["draft_path"])
    speed_factor = compute_required_speed_factor(draft, TARGET_DURATION_S)
    apply_speed_to_segments(draft, speed_factor)  # 写 materials.speeds + tracks[].segments
    atomic_write_draft_json(state["draft_path"], draft)
    state["retry_counts"]["node07"] = state["retry_counts"].get("node07", 0) + 1
    return state

def route_after_speed_fit(state: WorkflowState) -> str:
    """条件边：校验硬约束，决定重试/放行/报警升级"""
    draft = load_draft(state["draft_path"])
    if compute_total_duration(draft) <= TARGET_DURATION_S:
        state["snapshot2_path"] = save_snapshot(state["draft_path"], tag="snapshot2")  # 分叉点产出（对应8节版本快照）
        state["status_log"].append("node07_speed_fit_done")
        return "node08_add_subtitles"
    if state["retry_counts"]["node07"] >= MAX_RETRY:
        return "escalate_guardrail_failure"
    return "node07_speed_fit"
```

```python
# graph/build_graph.py 片段
graph_builder.add_node("node07_speed_fit", node07_speed_fit)
graph_builder.add_conditional_edges(
    "node07_speed_fit",
    route_after_speed_fit,
    {
        "node07_speed_fit": "node07_speed_fit",           # 自循环重试
        "node08_add_subtitles": "node08_add_subtitles",
        "escalate_guardrail_failure": "escalate_guardrail_failure",
    },
)
```

**测试用例**：恰好35秒视频（不应变速）、60秒视频（需匀速或分段变速压缩）、极短视频边界情况、连续2次不达标后第3次达标（验证重试计数）。

### 4.3 步骤8：`/jianying-add-subtitles`

写入字段：`materials.texts`（6.1节）。输入依赖 FireRedASR2S 的带时间戳文本。

```python
# graph/nodes/node08_add_subtitles.py
async def node08_add_subtitles(state: WorkflowState) -> WorkflowState:
    draft = load_draft(state["snapshot2_path"])  # 基于步骤7产出的快照②操作
    asr_segments = call_asr2s(state["video_source_path"])  # 或读取已缓存的ASR结果
    for seg in asr_segments:
        add_subtitle_text(draft, content=seg["text"], target_timerange=seg["timerange"])
    atomic_write_draft_json(state["draft_path"], draft)
    state["status_log"].append("node08_add_subtitles_done")
    return state
```

**测试用例**：字幕时间轴与音频人工听感对齐抽检；长句自动换行是否符合剪映字幕显示规范。

### 4.4 步骤9：`/jianying-inject-fx`

写入字段：`materials.transitions`、`materials.video_effects`（6.1节）。核心技巧是原文档反复提及的"**模板复制注入绕过VIP限制**"，这里把机制说清楚：

> **VIP资源复用机制**（步骤9、步骤11共用）：预先用一个有VIP权限的剪映账号，对空白测试工程逐一手动添加目标转场/特效/贴纸，导出该"模板工程"的draft_content.json，从中摘取 `materials.transitions` / `materials.video_effects` / `materials.stickers` 里对应的完整对象（含云端resource_id）。运行时直接把这些对象追加/合并进目标草稿的同名数组，并在 `tracks[].segments` 里给目标片段挂上引用。由于该resource_id此前已在**同一台机器**上被这个VIP账号缓存过，本地渲染/预览时无需重新触发VIP购买校验。**前置任务**：本周开始前需先人工产出这份"模板库"，建议列入阶段0一并确认是否已具备。

```python
# graph/nodes/node09_inject_fx.py
async def node09_inject_fx(state: WorkflowState) -> WorkflowState:
    draft = load_draft(state["draft_path"])
    template_lib = load_template_library("templates/fx_template.json")  # 见上方VIP资源复用机制
    for shot in draft["shot_plan"]:
        fx_obj = template_lib.pick_transition(shot["style_tag"])
        inject_transition(draft, fx_obj, position=shot["boundary_index"])
    atomic_write_draft_json(state["draft_path"], draft)
    state["status_log"].append("node09_inject_fx_done")
    return state
```

参考原文档14.4节：可用 jianying-editor-skill 的 `references/AVAILABLE_ASSETS.md`（枚举全部合法转场/特效/动画名称）减少从零核对合法枚举值的工作量，无需等待其PoC结论即可直接借鉴。

### 4.5 步骤10：`/jianying-inject-text-fx`

延伸 `materials.texts`（在步骤8的基础上追加样式字段，不是新的顶层字段——6.1节 `materials.texts` 对应步骤"8,10,16"）。

参考14.4节：jianying-editor-skill 近期版本（v1.3.0/v1.3.1）新增了 `set_subtitle_style` 像素级参数与14种动效参数，与本步骤"描边/投影/入场动画"的需求高度吻合，若阶段0选定采纳该组件，可直接复用其参数设计，避免自行摸索剪映客户端对花字样式字段的私有编码规则。

```python
# graph/nodes/node10_inject_text_fx.py
async def node10_inject_text_fx(state: WorkflowState) -> WorkflowState:
    draft = load_draft(state["draft_path"])
    for text_obj in draft["materials"]["texts"]:
        apply_text_style(
            text_obj,
            outline=True, shadow=True,
            entrance_animation="淡入放大",  # 或对照AVAILABLE_ASSETS.md选取合法枚举值
        )
    atomic_write_draft_json(state["draft_path"], draft)
    state["status_log"].append("node10_inject_text_fx_done")
    return state
```

### 4.6 步骤11：`/jianying-inject-tts-sticker`

写入字段：`materials.stickers`（6.1节）。**难点提示**（原文档14.4节明确标注）：resource_id的来源是本步骤"容易卡住的环节"。

```python
# graph/nodes/node11_inject_sticker.py
async def node11_inject_sticker(state: WorkflowState) -> WorkflowState:
    draft = load_draft(state["draft_path"])
    for subtitle in draft["materials"]["texts"]:
        resource_id = resolve_sticker_resource_id(subtitle["content"])  # 关键难点，见下
        if resource_id:
            add_sticker_segment(draft, resource_id, target_timerange=subtitle["target_timerange"])
    atomic_write_draft_json(state["draft_path"], draft)
    state["status_log"].append("node11_inject_sticker_done")
    return state
```

`resolve_sticker_resource_id` 的实现建议：套用与步骤9相同的"VIP资源复用机制"；若阶段0采纳了jianying-editor-skill，其 `sync_jy_assets.py` / `build_cloud_music_library.py` / `build_cloud_text_styles_library.py` 提供了从本机剪映历史工程中挖掘云端资产resource_id的现成脚本思路，即使不整体采纳该组件，这几个脚本的实现方式也值得直接参考（14.4节，无需等PoC结论）。

**测试用例**：贴纸时间区间与对应字幕target_timerange的偏差应在容忍阈值内（建议≤1帧）。

### 4.7 节点12 + 关卡②：人工添加BGM

结构与节点6同构，仅替换通知文案与操作范围：

```python
# graph/nodes/node12_human_add_bgm.py
async def node12_human_add_bgm(state: WorkflowState) -> WorkflowState:
    if not state.get("bgm_notified"):
        beat_sync_candidates = recommend_bgm_by_beat(state["draft_path"])  # 可选：节拍同步推荐辅助
        send_notification(
            thread_id=state["thread_id"],
            message=f"请从剪映VIP音乐库选取BGM并拖入，推荐候选：{beat_sync_candidates}",
        )
        state["bgm_notified"] = True

    interrupt({"checkpoint": "关卡②-人工添加BGM", "draft_path": state["draft_path"]})
    state["status_log"].append("checkpoint2_resumed")
    return state
```

### 4.8 步骤13：`/jianying-adjust-volume`（新Skill，重点设计）

写入字段：`materials.audio_fades`，可能同时涉及 `tracks[].segments` 中音频片段的volume引用（6.1节该行同时对应步骤"7,13,16,17"）。**这是唯一从零设计的Skill，原文档两个候选第三方组件（JianYing MCP Server 建议覆盖范围是5/8/9/10/11、jianying-editor-skill 的重点资产也未提及音量/淡出能力）均未提及覆盖此功能，与阶段0的技术路径选择无关**，必须自研。

**第一步（必须先做，不是可选项）——字段结构逆向工程**：
1. 取一份现有测试草稿备份
2. 在剪映客户端内手动给某个音频片段添加淡入2秒/淡出3秒，音量调至-6dB，保存
3. diff 修改前后的 `draft_content.json`，定位 `materials.audio_fades` 新增条目的实际字段名与 `tracks[].segments` 中该片段volume引用的变化
4. 把逆向出的真实结构记录为代码注释/docstring，作为本Skill实现依据（下方代码中的字段名为**设计占位**，需按第1步逆向结果替换）

```python
# graph/nodes/node13_adjust_volume.py
def jianying_adjust_volume(
    draft_path: str,
    track_selector: str,       # 如 "audio_main" / "audio_bgm"
    volume_level: float,        # 口径（0-1线性 or dB）需按逆向结果确定
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> None:
    draft = load_draft(draft_path)
    track = find_track(draft, track_selector)
    apply_volume(track, volume_level)
    apply_fade(track, fade_in_seconds, fade_out_seconds)  # 写入 materials.audio_fades
    atomic_write_draft_json(draft_path, draft)

async def node13_adjust_volume(state: WorkflowState) -> WorkflowState:
    jianying_adjust_volume(state["draft_path"], "audio_main", volume_level=1.0)
    jianying_adjust_volume(state["draft_path"], "audio_bgm", volume_level=0.35,
                            fade_in_seconds=2.0, fade_out_seconds=3.0)
    state["status_log"].append("node13_adjust_volume_done")
    return state
```

**命名规范核对**：与其余12个Skill统一采用 `/jianying-<动词>-<对象>` 模式，`/jianying-adjust-volume` 已符合（原文档13.1节待补齐条目原名）。

---

## 5. 关卡①②interrupt/resume联调测试方案（Day5 上午）

对应第3周明确要求的"关卡①/②的interrupt/resume联调"，也是消解原文档5.2节坑③④的关键验证环节。

| 测试 | 目的 | 方法 | 验收标准 |
|---|---|---|---|
| 1. 基础中断-恢复 | 验证interrupt()正确挂起、Checkpointer完整持久化状态 | 运行至节点6，检查AsyncSqliteSaver落盘的checkpoint记录 | `Command(resume=True)`后从节点6之后继续，不重跑节点1-5 |
| 2. 多日挂起模拟（对应坑④场景） | 验证跨进程/跨天恢复的durability | 触发中断后**完全终止**编排进程，用**全新构建的graph实例**+同一thread_id调用resume（模拟服务器重启后数天再恢复） | status_log中各上游节点仅出现一次，无重复执行 |
| 3. 幂等性验证（对应坑③） | 验证interrupt()前置副作用不被重放 | 在通知发送后、interrupt()真正持久化前人为kill进程，重新调度同一checkpoint | 通知只发送一次（依赖reorder_notified/bgm_notified标记，或验证"挪至resume后执行"方案） |
| 4. thread_id会话隔离 | 验证多视频并发互不干扰 | 同时发起2个不同thread_id的工作流，都触发关卡①中断，乱序分别resume | 两条工作流的草稿文件、状态互不覆盖 |

```python
# tests/test_interrupt_resume.py（示意，需按实际LangGraph版本API核实）
async def test_checkpoint1_multi_day_resume():
    thread_id = "test-video-001"
    config = {"configurable": {"thread_id": thread_id}}

    graph = build_graph()
    result = await graph.ainvoke(initial_state, config=config)
    # 断言已挂起于节点6（具体断言方式按安装的LangGraph版本核实）

    graph2 = build_graph()  # 全新实例，模拟跨进程恢复
    resumed = await graph2.ainvoke(Command(resume=True), config=config)
    assert resumed["status_log"].count("node05_generate_draft_done") == 1
```

---

## 6. 心跳监控脚本开发与部署（Day3 开发 / Day5 下午实测）

对应原文档5.2节坑④缓解方案：心跳时间戳文件 + 外部监控脚本 + 双层超时兜底。

### 6.1 心跳写入器（编排进程内嵌）

```python
# monitoring/heartbeat_writer.py
import time, threading
from pathlib import Path

HEARTBEAT_FILE = Path(r"C:\ProgramData\VideoWorkflow\heartbeat.txt")
HEARTBEAT_INTERVAL_SECONDS = 10

def start_heartbeat(interval: int = HEARTBEAT_INTERVAL_SECONDS) -> threading.Thread:
    """编排进程启动时调用一次，后台线程持续写入心跳时间戳"""
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _loop():
        while True:
            HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="heartbeat-writer")
    t.start()
    return t
```

### 6.2 外部监控脚本（PowerShell，Windows任务计划程序定时触发）

```powershell
# monitoring/heartbeat_monitor.ps1
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$heartbeatFile = 'C:\ProgramData\VideoWorkflow\heartbeat.txt'
$thresholdSeconds = 120
$logFile = 'C:\ProgramData\VideoWorkflow\heartbeat_monitor.log'

function Write-Log {
    param([string]$Message)
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Message
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

if (-not (Test-Path -LiteralPath $heartbeatFile)) {
    Write-Log '心跳文件不存在，判定编排进程从未启动或已被清理'
    exit 1
}

$lastHeartbeat = [double](Get-Content -LiteralPath $heartbeatFile -Encoding utf8)
$nowEpoch = [double](Get-Date -UFormat '%s')
$elapsed = $nowEpoch - $lastHeartbeat

if ($elapsed -gt $thresholdSeconds) {
    Write-Log "心跳超时：距上次更新已 $([math]::Round($elapsed)) 秒，超过阈值 $thresholdSeconds 秒，判定编排进程可能已卡死或崩溃"
    # TODO：接入告警渠道（企业微信webhook/邮件/Windows事件日志），本周先落地本地日志
} else {
    Write-Log "心跳正常，距上次更新 $([math]::Round($elapsed)) 秒"
}
```

```powershell
# 注册为Windows任务计划程序（管理员身份运行一次）
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Scripts\heartbeat_monitor.ps1"'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName 'VideoWorkflow-HeartbeatMonitor' `
    -Action $action -Trigger $trigger -Description '视频剪辑自动化工作流编排进程心跳监控'
```

> 已按 `/windows-shell-commands` 要求：全程PowerShell、英文引号、UTF-8输出修复、`-LiteralPath`避免路径误解析、避免PS5.1不兼容语法。命令具体参数请在部署时对照本机PowerShell版本核实。

### 6.3 LangGraph两级超时（二级防护，原文档5.2节）

- 总时长超时：为图执行设置整体超时上限（覆盖"从启动到理论最长耗时"的场景）
- "是否还有动静"超时：为单个节点/单次工具调用设置无响应超时
- 需在开发时对照已安装的LangGraph版本文档核实具体配置参数（该库更新较快，本计划不假设固定的参数名）

---

## 7. 端到端集成测试与交付物验收标准（Day5 收尾）

| 检查项 | 验收标准 | 验证方法 |
|---|---|---|
| 剪映项目文件完整性 | draft_content.json可被剪映v5.9.0正常打开，无解析错误 | 手动在剪映客户端打开 |
| 字幕轨道 | materials.texts非空，时间轴与ASR结果对齐 | 剪映内预览+人工听感比对 |
| 转场/特效（含VIP） | materials.transitions/video_effects非空，可正常渲染 | 剪映内预览播放 |
| 花字动画 | 字幕含描边/投影/入场动画样式参数 | 剪映内预览播放 |
| 贴纸关联 | materials.stickers非空，时间区间与对应字幕target_timerange一致 | 人工比对时间轴 |
| BGM | 已通过关卡②手动添加，音轨存在 | 剪映内检查音轨 |
| 音量/淡入淡出 | materials.audio_fades按预期生效，主音轨与BGM音量平衡 | 试听 |
| 总时长约束 | ≤35秒 | 剪映内查看总时长 |
| 写入安全性 | 全部写入均走原子写入工具，模拟中断测试通过 | 单元测试 |
| 关卡①②interrupt/resume | 第5节测试1-4全部通过 | 测试记录 |
| 心跳监控脚本 | 已注册Windows任务计划程序，模拟kill进程后按阈值触发告警 | 手动kill编排进程实测 |

---

## 8. 本周风险清单（对照原文档第11节）

| 风险 | 本周应对 |
|---|---|
| interrupt()前置副作用重放 | 第5节测试3专项验证 |
| 单机心跳/离线检测缺失 | 本周核心交付物之一，第6节直接解决 |
| 步骤13缺少对应Skill | 第4.8节完成从零设计与实现 |
| 步骤11 resource_id挖掘耗时超预期 | 若阶段0未采纳jianying-editor-skill或其云端脚本不可用，需自行摸索VIP资源复用机制，建议预留缓冲 |
| 步骤13 audio_fades实际字段结构未知 | 已在4.8节安排"逆向工程"为第一步，而非直接假设字段名编码 |

---

## 9. 若进度紧张的优先级取舍建议

1. **不可延后**：关卡①②的interrupt/resume机制必须跑通——这是Temporal迁移LangGraph后最核心的风险点
2. **不可延后**：步骤7护栏节点——它是分叉点，阻塞后续所有中文主线步骤
3. **不建议延后**：步骤13新Skill的基础功能（哪怕只支持简单淡入淡出，不支持复杂音量曲线）——它是当前唯一"完全空白"的缺口，越拖集成风险越大
4. **可承受延后**：步骤10花字动画的精细样式参数，先用默认样式占位，视觉精修可顺延到下周
5. **可承受延后**：心跳监控的告警渠道对接（企业微信/邮件），本周先落地本地日志文件，渠道对接可下周补

---

*本文档由 AI 辅助生成，关键工程决策（尤其步骤13的字段逆向工程结果、步骤9/11的VIP资源模板库前置准备、阶段0的技术路径选择）建议在正式编码前与团队复核。*

# 第4周实施计划:节点14-17并行图、关卡③、封面双语输出

> 本文档是《AI视频剪辑自动化工作流开发执行计划.md》第12节"第4周"任务的展开版,聚焦节点14-17并行图设计、关卡③条件中断逻辑、封面中英文双语输出三部分,补充状态结构、图接线代码、四个节点的可运行代码框架、测试用例与Windows执行命令。目标:团队可直接据此编码。本文档为规划讨论新产出,尚未合并入主计划,合并时机由团队决定。

## 1. 范围与前提

**本周做什么**:节点14(封面生成)、节点15(封面英文本地化)、节点16(字幕翻译+关卡③)、节点17(骨架,不含真实配音质量)、汇合节点。

**本周不做什么**:节点17的真实TTS合成与音画对齐(见主计划第12节,属第5周)。

**开始前必须已具备**(依赖 Week2/Week3 产出):

| 前提 | 说明 |
|---|---|
| 节点1-13已跑通 | 尤其节点13输出的 `draft_dir_zh`(已调完音量/淡入淡出的中文草稿) |
| 关卡①②的 interrupt/resume 已联调通过 | 本周关卡③复用同一套机制 |
| 心跳监控已部署 | 本周新增节点若挂死,依赖此机制发现 |
| ASR时间戳文本(`asr_segments_zh`)已在状态中可用 | 见第11节"开放问题1",这是本周设计的关键前提 |

---

## 2. 状态结构(State Schema)

```python
"""
本周新增/扩展的字段。假设已有贯穿全流程的主State,此处仅为新增部分,
实际开发时合并进主State定义,不单独建类。
"""
from typing import TypedDict, Literal, Optional
from typing_extensions import NotRequired


class SubtitleSegment(TypedDict):
    index: int                            # 分段序号,从0开始
    start_ms: int                         # 起始时间(毫秒)
    end_ms: int                           # 结束时间(毫秒)
    text_zh: str                          # 中文原文(来自ASR)
    text_en: NotRequired[Optional[str]]   # 翻译后英文,未翻译时为None


class CoverAsset(TypedDict):
    ratio: Literal["9:16", "16:9", "4:3"]
    zh_path: str                          # 中文封面文件路径
    en_path: NotRequired[Optional[str]]   # 英文封面路径,节点15前为None
    text_bbox: NotRequired[dict]          # 标题文字包围盒,供节点15/节点16复用,避免重复定位


class Week4State(TypedDict):
    # ---- 依赖节点1-13,此处仅声明,不重复定义 ----
    draft_dir_zh: str
    asr_segments_zh: list[SubtitleSegment]

    # ---- 本周新增:英文分支独立草稿副本(见第11节"开放问题2") ----
    draft_dir_en_branch: NotRequired[Optional[str]]

    # ---- 节点14/15 产出 ----
    covers: NotRequired[list[CoverAsset]]

    # ---- 节点16 产出 ----
    subtitle_segments_en: NotRequired[list[SubtitleSegment]]
    subtitle_srt_path: NotRequired[Optional[str]]
    checkpoint3_triggered: NotRequired[bool]

    # ---- 节点17(本周骨架) ----
    en_dub_audio_path: NotRequired[Optional[str]]

    # ---- 汇合节点 ----
    join_qa_issues: NotRequired[list[str]]
```

`text_bbox` 格式:`{"x": int, "y": int, "w": int, "h": int, "font_path": str, "font_size": int}`。

---

## 3. 并行图结构设计

**关键结论**:LangGraph中,一个节点若有多条出边,这些目标节点会在下一个"超步"(superstep)中自动并行执行;多条入边汇入同一节点时,该节点会等所有前驱都产出后才触发。这是原生的 fan-out/fan-in(分叉/汇合)机制,结构固定的静态分支不需要额外配置。

**为什么节点14/15内部不用LangGraph级并行**:若两个并行节点写同一个状态字段(如都想往 `covers` 追加一项),LangGraph会报错"每一步只能收到一个值",除非额外配置合并逻辑(reducer)。三种封面比例互不依赖、无需人工中断,用 `asyncio.gather` 在节点内部并发更简单、更好测试,不需要引入这层复杂度。真正需要LangGraph级并行的,只有"中文主线尾段(14→15)"和"英文分支(16→17)"这两条**结构固定**的分支。

```mermaid
flowchart LR
    N13["节点13 音量调整<br/>(已完成)"] --> N14["节点14 制作封面<br/>三比例内部并发"]
    N14 --> N15["节点15 封面英文本地化<br/>三比例内部并发"]
    N15 --> JOIN["汇合节点"]

    SNAP2["快照②(节点7产出)"] --> FORK["fork_draft_for_english_branch<br/>〔本周新增〕"]
    FORK --> N16["节点16 字幕翻译为英文"]
    N16 -->|校验通过,全自动| N17["节点17 骨架<br/>本周仅打通结构"]
    N16 -.排版异常.->|interrupt挂起| CP3(("关卡③<br/>人工在剪映内校对"))
    CP3 -.Command resume.-> N16
    N17 --> JOIN

    style CP3 fill:#fff5e6,stroke:#d9822b,stroke-width:2px
```

```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(Week4State)  # 实际接入主State,此处仅示意

graph.add_node("fork_draft_for_english_branch", fork_draft_for_english_branch)
graph.add_node("node14_make_covers", node14_make_covers)
graph.add_node("node15_localize_covers_en", node15_localize_covers_en)
graph.add_node("node16_translate_subtitles", node16_translate_subtitles)
graph.add_node("node17_inject_english_tts", node17_inject_english_tts_stub)
graph.add_node("join_before_delivery", join_before_delivery)

# 中文主线尾段(接续已有的node13)
graph.add_edge("node13_adjust_volume", "node14_make_covers")
graph.add_edge("node14_make_covers", "node15_localize_covers_en")
graph.add_edge("node15_localize_covers_en", "join_before_delivery")

# 英文分支(接续已有的快照②节点)
graph.add_edge("snapshot2_node", "fork_draft_for_english_branch")
graph.add_edge("fork_draft_for_english_branch", "node16_translate_subtitles")
graph.add_edge("node16_translate_subtitles", "node17_inject_english_tts")
graph.add_edge("node17_inject_english_tts", "join_before_delivery")

# join_before_delivery 有两条入边(node15、node17),自动等两者都完成
```

---

## 4. 分阶段任务分解

| 阶段 | 目标 | 关键任务 | 验收标准 |
|---|---|---|---|
| A(约1天) | 英文分支基础设施 | 扩展State;实现`fork_draft_for_english_branch`;幂等性单测 | fork后的目录可被剪映独立打开,不影响中文主线草稿 |
| B(约1.5天) | 节点14 | 9:16剪映内路径打通(含uiautomation确认);16:9/4:3本地Pillow;三分支并发;原子写入 | 三种比例封面均生成,命名符合规范,重复运行不留半成品 |
| C(约1.5天) | 节点15 | 复用text_bbox定位;接入FireRed-Image-Edit;中英文件区分 | 英文封面文字清晰、位置/风格与中文版一致,无中文残留 |
| D(约1.5天) | 节点16+关卡③ | 翻译接入;排版校验函数;interrupt幂等设计;resume后重读;SRT导出 | 正常文本全自动通过;超长文本触发interrupt且不覆盖人工修正 |
| E(约1.5天) | 图接线与联调 | 节点17骨架;汇合节点;接线;5个集成测试场景(见第10节) | 5个场景全部通过 |

合计约7个工作日,对应主计划"第4周"。

---

## 5. 节点14:制作多维度封面

```python
import asyncio
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


async def node14_make_covers(state: Week4State) -> dict:
    """
    三种比例互不依赖,节点内并发(而非LangGraph级并行),
    原因见第3节"为什么节点14/15内部不用LangGraph级并行"。
    """
    draft_dir = state["draft_dir_zh"]
    results = await asyncio.gather(
        _render_cover_9x16_via_jianying(draft_dir),          # 剪映内生成,天然异步(涉及UI自动化)
        asyncio.to_thread(_render_cover_pillow, draft_dir, "16:9"),  # CPU密集,丢进线程池避免阻塞事件循环
        asyncio.to_thread(_render_cover_pillow, draft_dir, "4:3"),
    )
    return {"covers": list(results)}


def _render_cover_pillow(draft_dir: str, ratio: str, font_size: int = 72) -> CoverAsset:
    """16:9 / 4:3 封面:取一帧关键画面 + 叠加标题文字,本地渲染。"""
    target_sizes = {"16:9": (1920, 1080), "4:3": (1440, 1080)}
    w, h = target_sizes[ratio]

    base_frame = Path(draft_dir) / "cover_source_frame.jpg"  # 由上游步骤14准备的关键帧,具体取帧逻辑不在本周范围
    img = Image.open(base_frame).convert("RGB")
    img = _resize_and_crop(img, w, h)

    title_zh = _read_title_from_draft(draft_dir)
    draw = ImageDraw.Draw(img)
    font_path = "assets/fonts/SourceHanSansCN-Bold.otf"  # 需替换为团队实际字体路径
    font = ImageFont.truetype(font_path, font_size)
    bbox = draw.textbbox((0, 0), title_zh, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = (w - text_w) // 2, int(h * 0.78)

    stroke_width = max(2, font_size // 24)
    draw.text((x, y), title_zh, font=font, fill="white", stroke_width=stroke_width, stroke_fill="black")

    ratio_tag = ratio.replace(":", "x")
    out_path = str(Path(draft_dir) / "covers" / f"cover_{ratio_tag}_zh.png")
    _save_atomic(img, out_path)

    return {
        "ratio": ratio, "zh_path": out_path,
        "text_bbox": {"x": x, "y": y, "w": text_w, "h": text_h, "font_path": font_path, "font_size": font_size},
    }


def _resize_and_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """居中裁剪并缩放到目标尺寸,主体不变形。"""
    src_ratio, dst_ratio = img.width / img.height, target_w / target_h
    if src_ratio > dst_ratio:
        new_w = int(img.height * dst_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / dst_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((target_w, target_h), Image.LANCZOS)


def _save_atomic(img: Image.Image, out_path: str) -> None:
    """临时文件→保存→原子重命名,遵循主计划6.3节写入安全规范。"""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path + ".tmp"
    img.save(tmp_path, "PNG")
    os.replace(tmp_path, out_path)


async def _render_cover_9x16_via_jianying(draft_dir: str) -> CoverAsset:
    """
    9:16封面:通过pyJianYingDraft写cover_info字段,必要时配合uiautomation触发剪映导出。
    具体属性名与uiautomation控件ID待PoC核实,见第11节"开放问题4"。
    """
    raise NotImplementedError("阶段B落地,见开放问题4")
```

---

## 6. 节点15:封面英文本地化

**核心修正**:原计划设想"擦除+重绘"需要手工生成遮罩(mask)。经核实,FireRed-Image-Edit 是**指令驱动**的图像编辑模型,靠自然语言描述完成修改,不需要手工mask;官方CLI形式为 `python inference.py --input_image ... --prompt "..." --output_image ... --seed 43`,且明确支持按指令替换图片上的文字、同时保持原有字体风格——正好对应本场景,比原计划更简单。

```python
async def node15_localize_covers_en(state: Week4State) -> dict:
    """三种比例本地化互不依赖,节点内并发。"""
    covers = state["covers"]
    title_zh, title_en = _get_titles(state)

    en_paths = await asyncio.gather(*[
        _localize_one_cover(c, title_zh, title_en) for c in covers
    ])
    merged = [{**c, "en_path": p} for c, p in zip(covers, en_paths)]
    return {"covers": merged}


async def _localize_one_cover(cover: CoverAsset, title_zh: str, title_en: str) -> str:
    prompt = (
        f"将图片中的中文文字\"{title_zh}\"替换为英文文字\"{title_en}\","
        f"保持原有字体风格、描边效果、颜色与位置不变，不改变背景与其他元素。"
    )
    en_path = cover["zh_path"].replace("_zh.png", "_en.png")
    await fireRed_image_edit(input_image=cover["zh_path"], prompt=prompt, output_image=en_path, seed=43)
    return en_path


async def fireRed_image_edit(input_image: str, prompt: str, output_image: str, seed: int = 43) -> None:
    """
    对接FireRed-Image-Edit。生产环境建议包成常驻本地推理服务(参考OpenStoryline
    "MCP Server+Web前端"的常驻模式),避免每次调用重新加载扩散模型权重的开销——
    见第11节"开放问题3"。此处签名对齐官方CLI参数(input_image/prompt/output_image/seed)。
    """
    raise NotImplementedError("阶段C落地,对接本地推理服务后实现")
```

---

## 7. 节点16:字幕翻译为英文 + 关卡③条件中断

**关键设计依据**:LangGraph的`interrupt()`恢复时,整个节点函数会从头重新执行——这是`Command(resume=...)`的设计取舍:用"从检查点确定性重放"换取"精确恢复到中断那一行"的能力。因此中断之前的代码必须能安全重放(幂等),官方推荐把有副作用的写操作放在`interrupt()`之后,中断之前只做纯计算或幂等读写。这与主计划5.2节"坑③"的应对方案完全一致——本节点是该原则的具体落地。

```python
from langgraph.types import interrupt
from PIL import ImageFont

FONT_PATH_EN = "assets/fonts/Roboto-Bold.ttf"
FONT_SIZE_EN = 48
MAX_WIDTH_PX = 900  # 需按实际字幕安全框宽度调整


async def node16_translate_subtitles(state: Week4State) -> dict:
    draft_dir = state["draft_dir_en_branch"]

    # 1) 翻译(幂等只读调用,重放代价可接受,不做额外保护)
    segments_en = await translate_segments(state["asr_segments_zh"])

    # 2) 写入草稿前先做幂等检查,避免resume重放时用机器翻译覆盖人工修正(坑③)
    if not _subtitles_already_written(draft_dir, expected_count=len(segments_en)):
        _write_segments_to_draft(draft_dir, segments_en)

    # 3) 校验排版
    issues = validate_layout(segments_en, FONT_PATH_EN, FONT_SIZE_EN, MAX_WIDTH_PX)

    if issues:
        interrupt({
            "checkpoint": "③",
            "reason": "英文字幕排版异常",
            "issues": issues,
            "draft_dir": draft_dir,
            "instruction": "请在剪映客户端打开该草稿,手动修正列出的字幕分段后回复继续",
        })
        # 恢复后重新从草稿读取(可能已被人工修正),不使用interrupt的返回值直接覆盖
        segments_en = _read_segments_from_draft(draft_dir)

    # 4) 副作用写入放在interrupt()之后,只在真正需要产出交付物时执行一次
    srt_path = write_srt_atomic(segments_en, str(Path(draft_dir) / "subtitle_en.srt"))

    return {
        "subtitle_segments_en": segments_en,
        "subtitle_srt_path": srt_path,
        "checkpoint3_triggered": bool(issues),
    }


def validate_layout(segments: list[SubtitleSegment], font_path: str, font_size: int, max_width_px: int) -> list[dict]:
    """校验英文字幕是否溢出安全框、字体是否可用——关卡③的触发条件。"""
    try:
        font = ImageFont.truetype(font_path, font_size)
    except OSError:
        return [{"segment_index": s["index"], "issue": "missing_font", "detail": f"字体文件未找到: {font_path}"} for s in segments]

    issues = []
    for seg in segments:
        text = seg.get("text_en") or ""
        bbox = font.getbbox(text)
        text_width = bbox[2] - bbox[0]
        if text_width > max_width_px:
            issues.append({
                "segment_index": seg["index"], "issue": "overflow",
                "detail": f"渲染宽度{text_width}px超出可用宽度{max_width_px}px",
            })
    return issues


def write_srt_atomic(segments: list[SubtitleSegment], out_path: str) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines += [str(i), f"{_ms_to_srt_time(seg['start_ms'])} --> {_ms_to_srt_time(seg['end_ms'])}", seg.get("text_en", ""), ""]
    tmp_path = out_path + ".tmp"
    Path(tmp_path).write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp_path, out_path)
    return out_path


def _ms_to_srt_time(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
```

---

## 8. 节点17骨架 + 汇合节点

本周只保证图结构能跑通,不做真实配音质量(第5周完成)。

```python
async def node17_inject_english_tts_stub(state: Week4State) -> dict:
    """占位实现。TODO(第5周):替换为FireRedTTS2合成+动态变速补偿。"""
    return {"en_dub_audio_path": None}


def join_before_delivery(state: Week4State) -> dict:
    """轻量QA闸门:检查交付物是否齐全,不做深度校验(超出本周范围)。"""
    issues = []
    if len(state.get("covers", [])) < 3 or any("en_path" not in c for c in state.get("covers", [])):
        issues.append("封面缺失:需3种比例均含中英文两份")
    if not state.get("subtitle_srt_path"):
        issues.append("英文字幕文件缺失")
    return {"join_qa_issues": issues}
```

---

## 9. 交付物规范

| 交付物 | 数量 | 命名 | 目录 |
|---|---|---|---|
| 中文封面 | 3(三种比例) | `cover_9x16_zh.png` / `cover_16x9_zh.png` / `cover_4x3_zh.png` | `<draft_dir>/covers/` |
| 英文封面 | 3(三种比例) | 同上,`zh`→`en` | 同上 |
| 英文字幕 | 1 | `subtitle_en.srt` | `<draft_dir_en_branch>/` |

---

## 10. 联调测试场景

| 场景 | 验证内容 |
|---|---|
| 1. 并发实测 | 用时间戳打点确认14-15与16-17是真并行(总耗时≈max分支耗时),不是顺序执行 |
| 2. 关卡③触发路径 | 构造超长英文文本→确认interrupt挂起→模拟人工修正→resume→读到修正后文本 |
| 3. 关卡③正常路径 | 正常长度文本→确认全自动通过,不触发interrupt |
| 4. 中文分支已完成时英文分支挂起 | node14-15先跑完,node16触发interrupt;确认整体图正确暂停,resume后不重跑node14-15 |
| 5. 重放安全性 | 反复调用resume多次(模拟异常重试),确认`_subtitles_already_written`生效,不覆盖人工修正 |

```powershell
# 运行本周新增用例(PowerShell,需已激活虚拟环境)
python -m pytest tests\test_week4_nodes.py -v

# 检查产出文件是否齐全(6张封面+1个字幕文件)
Get-ChildItem -Path '.\covers' -Filter '*.png' | Measure-Object
Get-Item '.\subtitle_en.srt' | Select-Object Length
```

涉及中文路径若出现乱码,先执行UTF-8修复(见`/windows-shell-commands`):

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
```

---

## 11. 开放问题(需团队确认,每项给2个选项)

**1. 节点16的字幕翻译来源**
- **A(推荐)**:读 `asr_segments_zh`(ASR时间戳文本),不依赖节点8写入草稿的字幕
- B:等节点8把字幕写进`materials.texts`后再读
- 推荐A,理由:主计划写明英文分支"只依赖步骤7快照",若依赖B,英文分支就不再独立,且两条分支若同时写同一份`draft_content.json`会有写冲突风险

**2. 英文分支的独立草稿副本**
- **A(推荐)**:新增`fork_draft_for_english_branch`,用文件系统复制出独立副本
- B:两分支共用同一份草稿,靠文件锁排队访问
- 推荐A,理由:B会让"并行"名不副实,退化为排队等锁

**3. FireRed-Image-Edit调用方式**
- **A(推荐)**:包成本地常驻推理服务(仿照OpenStoryline的MCP Server模式)
- B:每次调用拉起子进程执行`inference.py`
- 推荐A,理由:该模型是扩散类基座模型,重复加载权重的开销在B方案下不可忽视

**4. 9:16封面剪映内生成的具体实现**
- **A(推荐)**:核实`cover_info`字段的实际属性名,配合uiautomation触发导出
- B:9:16也改本地Pillow渲染,放弃"剪映内生成"
- 推荐A(与14节既定选型一致);仅当PoC验证A行不通时才退回B。核实到的一点新信息:pyJianYingDraft官方文档指出自动导出依赖旧版剪映可见控件,新版(7及以上)剪映通常不再满足这一前提,比主计划14.4节的"5.9或V6"表述更精确,可作为核实边界时的参考起点。

---

## 12. 与第5周的衔接

第5周接手:节点17真实TTS合成+动态变速补偿、端到端联调、Postgres切换。本周产出的节点17骨架和图结构,第5周直接在其基础上补内容,不用重新接线。

---

## 13. 参考来源

- pyJianYingDraft(GuanYixuan):https://github.com/GuanYixuan/pyJianYingDraft
- LangGraph官方文档 - Interrupts:https://docs.langchain.com/oss/python/langgraph/interrupts
- LangGraph官方文档 - Graph API:https://docs.langchain.com/oss/python/langgraph/graph-api
- FireRedTeam GitHub组织:https://github.com/FireRedTeam
- FireRed-Image-Edit:https://github.com/FireRedTeam/FireRed-Image-Edit

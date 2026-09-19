# AI视频剪辑自动化工作流·第2周分阶段实施计划

> 本文档基于《AI视频剪辑自动化工作流开发执行计划.md》第2/3/4/5/6/9/10/11/12/13节，以及《AI视频剪辑自动化工作流第三方组件评估整合总结.md》《AI视频剪辑自动化工作流第三方视频Skills生态评估.md》交叉核对生成。目标：按阶段A→E顺序编码，每个阶段完成后代码可独立运行与测试，最终满足第12节Week2三项交付物（测试视频的故事板文件、剪映工程草稿文件、第三方组件PoC评估报告）。

## 0. 范围与阶段总览

第2周任务原文（第12节）：LangGraph节点1-5实现；pyJianYingDraft集成测试；draft_content.json加密检测与版本适配模块；JianYing MCP Server与jianying-editor-skill并列小规模PoC。

四项任务按依赖关系重新组织为5个阶段，**阶段B（加密检测与版本适配模块）插入在阶段A与阶段C之间**——因为节点1-5中只有节点5真正读写`draft_content.json`，若不先做好防护就实现节点5，等于让节点5直接暴露在"剪映6.0+悄悄加密"或"写入中断损坏草稿"两个已知风险下（第11节风险清单已列出）。

| 阶段 | 时间 | 任务 | 前置依赖 |
|---|---|---|---|
| A | Day1-2 | LangGraph骨架 + 步骤1-3节点 | 无（可立即开始） |
| B | Day2-3 | draft_content.json加密检测与版本适配模块 | 无（可与阶段A后半段并行开发，逻辑独立） |
| C | Day3-4 | 步骤4-5节点 | 依赖阶段A（复用节点2产出的MCP连接）+ 阶段B（节点5内部调用） |
| D | Day4-5 | pyJianYingDraft集成测试 | 依赖阶段A+B+C全部完成 |
| E | Day2-5（并行） | MCP Server / jianying-editor-skill并列PoC | 无（独立评估工作，建议由另一人或穿插时间完成，不阻塞A-D） |

---

## 1. 阶段A：LangGraph骨架 + 步骤1-3节点实现（Day1-2）

### 1.1 State Schema（v0.1）

覆盖节点1-5所需字段，后续周次再扩展。用`TypedDict`（LangGraph原生支持）：

```python
# state.py
from typing import TypedDict, Optional, Literal

class WorkflowState(TypedDict):
    # 全局
    session_id: str
    video_input_path: str

    # 步骤1
    cache_cleaned: bool
    cache_cleaned_paths: list[str]

    # 步骤2
    openstoryline_pid: Optional[int]
    openstoryline_mcp_endpoint: Optional[str]
    openstoryline_web_url: Optional[str]
    openstoryline_ready: bool

    # 步骤3
    preview_opened: bool

    # 步骤4
    shot_plan: Optional[dict]

    # 步骤5（依赖阶段B的加密/版本状态）
    draft_path: Optional[str]
    draft_encryption_status: Optional[Literal["plaintext", "encrypted", "not_found"]]
    draft_version_strategy: Optional[Literal["strategy_a_version_lock", "strategy_b_oneway_write"]]

    # 通用
    error_log: list[str]
```

字段命名与第6.1节`draft_content.json`字段路径（`canvas_config`、`materials.videos`、`tracks`）不冲突——那些是JSON内部字段，此处是LangGraph State字段，两者是不同层级，不要混用。

### 1.2 节点1：`clean_cache`

对应技能`/kuaishou-clean-cache`（第13.1节），清理剪映、OpenStoryline、FireRed-OpenStoryline、auto-video-editor 自身的「一类·常规再生缓存」。

```python
# config.py
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# 简单路径：环境变量展开后直接当 Path 用
CACHE_PATHS_TO_CLEAN: list[str] = [
    r"%LOCALAPPDATA%\Temp\OpenStoryline",
    r"%TEMP%\jianying_workflow_tmp",
    r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\.recycle_bin",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\.storyline\.server_cache",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\.playwright-cli",
    r"E:\Documents\kuaishou\.playwright-cli",
    r"E:\Documents\kuaishou\env_check_report.txt",
    r"E:\Documents\kuaishou\JianyingPro\5.9.0.11632\log",
    r"E:\Documents\kuaishou\剪艾（剪辑agent）\win-unpacked\boot.log",
    r"E:\Documents\kuaishou\auto-video-editor\.pytest_cache",
    r"E:\Documents\kuaishou\auto-video-editor\.docker-proxy\gost.out.log",
    r"E:\Documents\kuaishou\auto-video-editor\.docker-proxy\gost.err.log",
    # … FireRed web/mcp 日志、install_log.txt、test_result.txt、config.toml.bak.* 等
]

# 递归/通配 spec：调用 resolved_cache_paths 时才展开为 Path
@dataclass(frozen=True)
class CacheGlobSpec:
    root: str                              # 含环境变量
    kind: Literal["dir_recurse", "file_recurse", "dir_children", "file_pattern"]
    pattern: str                           # rglob/glob 参数
    exclude_substr: tuple[str, ...] = ()   # 例如 ("\\venv\\", "\\.venv\\")
    description: str = ""

CACHE_GLOB_SPECS: list[CacheGlobSpec] = [
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou",
        kind="dir_recurse",
        pattern="__pycache__",
        exclude_substr=("\\venv\\", "\\.venv\\"),
        description="源码 __pycache__ (排除 venv)",
    ),
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou\tmp",
        kind="dir_children",
        pattern="*",
        exclude_substr=(),
        description="tmp 子目录（不动根下 4 个模板）",
    ),
    # … .DS_Store、剪映草稿 *.bak / .backup、runtime/initial_state_*.json、logs/*.log、config.toml.bak.* 等
]


def resolved_cache_paths() -> list[Path]:
    """扁平化两类 spec 为 Path 列表。调用时才展开 glob/递归。"""
    out: list[Path] = []
    for raw in CACHE_PATHS_TO_CLEAN:
        p = Path(os.path.expandvars(raw))
        if p.exists():
            out.append(p)
    for spec in CACHE_GLOB_SPECS:
        out.extend(_expand_spec(spec))
    return out
```

> **重要禁删项**（技能三类）：剪映 `User Data\Cache` 与 `User Data\Log` **绝不**进入 `CACHE_PATHS_TO_CLEAN` —— 删除 VIP 素材/特效下载缓存会导致已注入的转场/特效/花字/贴纸下次渲染需重新联网下载（用户 2026-07-04 指定）。

```python
# nodes/node_01_clean_cache.py
from config import resolved_cache_paths
from state import WorkflowState

def clean_cache(state: WorkflowState) -> dict:
    cleaned: list[str] = []
    errors = list(state.get("error_log", []) or [])
    for path in resolved_cache_paths():
        try:
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                cleaned.append(str(path))
        except Exception as e:  # noqa: BLE001
            errors.append(f"[node_01] 清理失败 {path}: {e}")
    return {
        **state,
        "cache_cleaned": True,
        "cache_cleaned_paths": cleaned,
        "error_log": errors,
    }
```

**调用时机**：graph.invoke **不**自动触发本节点（START 边已剥离），仅由 FireRed-OpenStoryline Web UI 的「清理缓存」按钮通过 `POST /api/system/clean-cache` 端点显式调用。

**单元测试要点**：
- 目标目录不存在时不报错（正常跳过）
- 目标目录存在但被占用（模拟文件锁）时，异常被捕获并写入`error_log`，不中断流程
- 断言返回的`cache_cleaned_paths`只包含实际清理成功的路径
- `CACHE_PATHS_TO_CLEAN` 不含剪映 `User Data\Cache` / `User Data\Log`
- `__pycache__` 递归排除 `.venv/` 与 `venv/`
- `tmp/` 根下 4 个模板 JSON 不被删除（只动子目录）

### 1.3 节点2：`launch_openstoryline_service`

对应`/openstoryline-launcher`，拉起MCP Server + Web前端（FastAPI/uvicorn，第13.1节原文）。

```python
# nodes/node_02_launch_openstoryline.py
import subprocess
import time

import httpx

from state import WorkflowState

def launch_openstoryline_service(state: WorkflowState) -> WorkflowState:
    proc = subprocess.Popen(
        ["python", "-m", "openstoryline.server"],  # 实际入口命令需按OpenStoryline部署文档核实
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    mcp_endpoint = "http://127.0.0.1:8006/mcp"  # 端口需按实际部署确认
    web_url = "http://127.0.0.1:8005"           # 与第5.1节原方案默认地址一致

    ready = _wait_for_ready(web_url, timeout_s=30)
    return {
        **state,
        "openstoryline_pid": proc.pid,
        "openstoryline_mcp_endpoint": mcp_endpoint,
        "openstoryline_web_url": web_url,
        "openstoryline_ready": ready,
    }

def _wait_for_ready(url: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False
```

**单元测试要点**：
- Mock `subprocess.Popen`与健康检查HTTP调用，验证`openstoryline_ready=False`时节点不抛异常，而是把状态如实写回State（由下游或护栏节点决定是否中止，符合LangGraph"节点只管产出状态"的设计原则）
- 端口/入口命令目前是占位值，标注为**待第1周环境搭建产出核实**

### 1.4 节点3：`open_preview`

对应第5.1节：**无独立Skill**，编排引擎直接动作。需区分人工交互与无人值守两种场景：

```python
# nodes/node_03_open_preview.py
import subprocess

from state import WorkflowState

def open_preview(state: WorkflowState, unattended: bool = False) -> WorkflowState:
    web_url = state["openstoryline_web_url"]
    if not unattended:
        # 人工交互场景：直接打开独立Edge窗口（第5.1节：不再经VS Code插件）
        subprocess.Popen(["cmd", "/c", "start", "msedge", web_url])
    else:
        # 无人值守场景：Playwright以channel='msedge'驱动本机Edge内核（第5.1节原文）
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            page.goto(web_url)
            # 可在此处加渲染结果校验逻辑，供无人值守场景自动验证前端是否正常
            browser.close()
    return {**state, "preview_opened": True}
```

**单元测试要点**：`unattended`两个分支分别测试；`unattended=True`时mock Playwright，避免CI环境依赖真实浏览器。

### 1.5 阶段A验收标准

- [ ] 3个节点函数均可独立调用并返回符合`WorkflowState`结构的字典
- [ ] LangGraph图能以`StateGraph(WorkflowState)`正确编译，节点1→2→3线性连接（对照第4节"准备与初始化：步骤1-3，线性，无依赖"）
- [ ] 单元测试全部通过，覆盖核心分支
- [ ] 缓存清理路径、OpenStoryline启动命令/端口已按第1周环境搭建实际产出核实（若第1周尚未产出，本阶段需先花约0.5天核实，避免带着占位值进入阶段C）

---

## 2. 阶段B：draft_content.json加密检测与版本适配模块（Day2-3）

### 2.1 设计依据

第6.2节原文：剪映自6.0.0起对本地`draft_content.json`引入AES强加密，直接导致明文读写工具失效；第6.3节要求所有写入必须"临时文件写入→校验JSON合法性→操作系统级重命名覆盖"。本模块是节点5的前置依赖，必须先行开发完成。

### 2.2 加密检测函数

```python
# draft_ops/encryption_detector.py
import json
import subprocess
from enum import Enum
from pathlib import Path

class DraftStatus(Enum):
    PLAINTEXT = "plaintext"
    ENCRYPTED = "encrypted"
    NOT_FOUND = "not_found"

def detect_draft_encryption(draft_dir: Path) -> DraftStatus:
    draft_file = draft_dir / "draft_content.json"
    if not draft_file.exists():
        return DraftStatus.NOT_FOUND

    raw = draft_file.read_bytes()
    try:
        raw.decode("utf-8")
        json.loads(raw)
        return DraftStatus.PLAINTEXT
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    # 二次确认：调用capcut decrypt工具交叉验证（第6.2节原文命令）。
    # 仅在"明文JSON判定失败"后触发，避免把"JSON格式损坏"误判为"加密"。
    result = subprocess.run(
        ["capcut", "decrypt", str(draft_dir)],
        capture_output=True, text=True,
    )
    return DraftStatus.ENCRYPTED if result.returncode != 0 else DraftStatus.PLAINTEXT
```

**待核实项**：`capcut decrypt`工具的实际返回码语义（0/非0具体含义）需在阶段B开工时先花约1小时，用一份已知明文草稿和一份已知加密草稿各跑一次来确认，再固化进单元测试的mock行为——不建议凭空假设其返回码约定。

### 2.3 策略选择与执行

```python
# draft_ops/version_strategy.py
from enum import Enum

class VersionStrategy(Enum):
    STRATEGY_A_VERSION_LOCK = "strategy_a_version_lock"   # 策略甲：全局版本锁定
    STRATEGY_B_ONEWAY_WRITE = "strategy_b_oneway_write"   # 策略乙：单向明文写入渲染

def resolve_strategy(jianying_version: str) -> VersionStrategy:
    """
    策略甲（第6.2节）：要求本机剪映锁定在v5.9.0（明文UTF-8 JSON），
      且已在hosts文件屏蔽升级域名（第1周交付物已包含此项，见14.1节）。
    策略乙（第6.2节）：pyJianYingDraft全新创建未加密草稿；
      剪映一旦打开该草稿触发加密，工作流后续只读不写（单向使用）。
    第2周默认走策略甲（第1周已完成v5.9.0环境锁定），
    策略乙作为降级预案保留，供未来环境版本漂移时切换。
    """
    if jianying_version == "5.9.0":
        return VersionStrategy.STRATEGY_A_VERSION_LOCK
    return VersionStrategy.STRATEGY_B_ONEWAY_WRITE
```

### 2.4 原子写入封装

```python
# draft_ops/atomic_writer.py
import json
import os
import tempfile
from pathlib import Path

def atomic_write_draft(draft_file: Path, content: dict) -> None:
    """
    第6.3节要求：临时文件写入 → 校验JSON合法性 → 操作系统级重命名覆盖。
    """
    serialized = json.dumps(content, ensure_ascii=False, indent=2)
    json.loads(serialized)  # 前置校验：确保生成内容本身是合法JSON

    fd, tmp_path = tempfile.mkstemp(
        dir=draft_file.parent, prefix=".draft_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, draft_file)  # 操作系统级原子重命名
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
```

### 2.5 阶段B验收标准

- [ ] `detect_draft_encryption`：明文draft、损坏JSON、AES加密文件、文件不存在，四种输入验证四种返回状态
- [ ] `resolve_strategy`：v5.9.0输入返回策略甲；其他版本号返回策略乙
- [ ] `atomic_write_draft`：正常写入后文件内容正确；模拟写入中途抛异常（mock写一半后raise），验证目标文件未被污染（仍是旧内容或不存在），临时文件被清理
- [ ] 3个函数均不依赖阶段A/C的State结构，可独立于LangGraph图之外单测（符合"底层库函数"定位，供阶段E的PoC对比参考）

---

## 3. 阶段C：步骤4-5节点实现——分镜与初始草稿（Day3-4）

### 3.1 节点4：`import_video_and_plan_shots`

对应第3节步骤4："全自动（Agent+工具节点）| OpenStoryline内置VLM规划"。第10节调用堆栈图显示步骤4未对应独立技能标识，直接复用节点2建立的OpenStoryline MCP连接。

```python
# nodes/node_04_import_and_plan.py
from mcp_client import OpenStorylineMCPClient  # 封装MCP协议调用，见第2节"互操作协议MCP"

from state import WorkflowState

def import_video_and_plan_shots(state: WorkflowState) -> WorkflowState:
    if not state.get("openstoryline_ready"):
        return {
            **state,
            "error_log": state["error_log"] + ["[node_04] OpenStoryline服务未就绪，跳过分镜规划"],
        }

    client = OpenStorylineMCPClient(endpoint=state["openstoryline_mcp_endpoint"])
    shot_plan = client.import_video_and_get_shot_plan(video_path=state["video_input_path"])
    return {**state, "shot_plan": shot_plan}
```

**待明确项**：`OpenStorylineMCPClient`的具体方法名/协议细节属于OpenStoryline自身MCP接口定义，不在本执行计划范围内。建议阶段C开工前先花约0.5天翻一遍OpenStoryline的MCP工具清单（`tools/list`），确认实际方法名后替换此处占位调用。

### 3.2 节点5：`generate_initial_jianying_draft`

对应`/openstoryline-to-jianying`（第13.1节），是节点1-5中唯一读写`draft_content.json`的节点，**必须先调用阶段B模块**。

```python
# nodes/node_05_generate_draft.py
from pathlib import Path

from draft_ops.atomic_writer import atomic_write_draft
from draft_ops.encryption_detector import DraftStatus, detect_draft_encryption
from draft_ops.version_strategy import resolve_strategy
from state import WorkflowState

def generate_initial_jianying_draft(state: WorkflowState, draft_dir: Path) -> WorkflowState:
    # 前置：加密检测（阶段B）
    status = detect_draft_encryption(draft_dir)
    if status == DraftStatus.ENCRYPTED:
        return {
            **state,
            "draft_encryption_status": status.value,
            "error_log": state["error_log"] + ["[node_05] 检测到已加密草稿，中止写入，需人工核查剪映版本"],
        }

    strategy = resolve_strategy(jianying_version="5.9.0")  # 版本号来源：第1周环境探测结果

    # 按6.1节字段映射表，写入 canvas_config / materials.videos / tracks
    draft_content = _build_draft_content(
        shot_plan=state["shot_plan"],
        video_path=state["video_input_path"],
    )
    draft_file = draft_dir / "draft_content.json"
    atomic_write_draft(draft_file, draft_content)  # 使用阶段B的原子写入，而非直接open().write()

    return {
        **state,
        "draft_path": str(draft_file),
        "draft_encryption_status": status.value,
        "draft_version_strategy": strategy.value,
    }

def _build_draft_content(shot_plan: dict, video_path: str) -> dict:
    """
    按第6.1节字段映射表构造：
      canvas_config    —— 画布宽高比与分辨率
      materials.videos —— 影片/图片素材清单（步骤4/5共用字段，本节点负责落盘）
      tracks           —— 时间轴轨道（本节点负责初始化空轨道结构，
                           具体片段填充留给步骤7变速节点等后续周次的节点）
    """
    return {
        "canvas_config": {"width": 1080, "height": 1920},  # 竖屏，与步骤14"9:16剪映内生成"一致
        "materials": {"videos": _shot_plan_to_materials(shot_plan, video_path)},
        "tracks": _init_empty_tracks(),
    }
```

`_shot_plan_to_materials` / `_init_empty_tracks` 两个辅助函数的具体实现，取决于`shot_plan`的真实字段结构（由OpenStoryline输出）与`pyJianYingDraft`库对`materials.videos`/`tracks`的具体API封装形式，建议在阶段C开工时先跑一次OpenStoryline的示例输出、对照pyJianYingDraft的README或示例代码确定字段细节后再实现，此处不做预先假设。

### 3.3 阶段C验收标准

- [ ] 用一段测试视频跑通节点4，产出`shot_plan`，另存为JSON文件（对照第12节交付物"测试视频的故事板文件"）
- [ ] 用同一份`shot_plan`跑通节点5，产出`draft_content.json`（对照交付物"剪映工程草稿文件"），且写入过程验证走的是阶段B的`atomic_write_draft`而非裸写
- [ ] 手动在剪映v5.9.0客户端打开产出的草稿工程，确认可正常加载不报错（人工验收步骤，不可省略——单元测试无法完全替代"剪映真的认这个文件"这件事）

---

## 4. 阶段D：pyJianYingDraft集成测试（Day4-5）

### 4.1 测试范围

端到端跑通节点1→2→3→4→5全链路（对照第9节时序图前5步交互）。

### 4.2 正常路径测试用例

| 用例 | 输入 | 预期 |
|---|---|---|
| TC-01 端到端主流程 | 一段30秒测试视频 | 5个节点依次成功，最终`draft_path`指向合法可被剪映打开的草稿 |
| TC-02 State字段完整性 | 同上 | 每个节点执行后，State中新增字段均符合1.1节Schema类型定义 |

### 4.3 异常场景测试用例

| 用例 | 模拟条件 | 预期行为 |
|---|---|---|
| TC-03 加密草稿误判 | draft_dir下预置一份AES加密的draft_content.json | 节点5检测到加密后中止写入，`error_log`记录原因，不覆盖原文件 |
| TC-04 写入中途中断 | mock `atomic_write_draft`内部在`os.replace`前抛异常 | 目标`draft_content.json`保持写入前状态，临时文件被清理，不产生半成品 |
| TC-05 OpenStoryline未就绪 | 节点2健康检查超时（`openstoryline_ready=False`） | 节点4提前返回并记录错误，不崩溃，不产生空`shot_plan`误传给节点5 |
| TC-06 视频格式不支持 | 传入OpenStoryline不支持的编码格式 | 节点4捕获底层异常并写入`error_log`，State其余字段不受影响 |

### 4.4 测试报告模板

需包含：测试环境（剪映版本/Python版本/OS）、6个用例的通过/失败状态、失败用例根因分析、遗留问题清单（如"待第1周环境搭建核实的缓存路径/端口"若仍未核实，在此处升级为阻塞项）。

---

## 5. 阶段E：JianYing MCP Server 与 jianying-editor-skill 并列PoC（Day2-5，并行）

不阻塞阶段A-D，可穿插进行或由另一人并行推进。逐项对应14.5节5点：

| 序号 | 待核实项 | 验证方法 | 判定标准 |
|---|---|---|---|
| 1 | JianYing MCP Server：Python 3.13+兼容性 | 在Python 3.13虚拟环境中安装并运行其自带测试/示例 | 无导入错误、核心功能可跑通即视为兼容 |
| 2 | JianYing MCP Server：原子写入包装 | 走查其写`draft_content.json`的源码路径，对照本计划2.4节"临时文件→校验→replace"三步是否齐全 | 三步齐全→已具备；缺任一步→需在采纳后自行外层包装 |
| 3 | JianYing MCP Server：幂等设计 | 对照第5.2节"坑③缓解方案"（检查目标是否已存在/已发送再执行，或挪到resume后执行），核对其MCP工具调用是否符合该模式 | 符合→可直接用于`interrupt()`前置逻辑；不符合→PoC报告中标注需团队自行加一层幂等包装 |
| 4 | jianying-editor-skill：许可证条款 | 查阅仓库`LICENSE`文件，核实是否允许当前使用场景（内部/商用） | 按公司法务口径判断，PoC报告仅陈述条款内容，不下法律结论 |
| 5 | jianying-editor-skill："5.9/V6"版本表述边界 | 对照《第三方视频Skills生态评估》4.2节已识别的表述出入，向仓库issue或维护者直接提问确认 | 得到维护者明确答复，或至少在PoC环境（v5.9.0）实测确认可用 |

**PoC评估报告大纲**：环境信息 → 5项逐条结论（沿用上表"判定标准"列）→ 与阶段B自研模块的能力对比（若MCP Server/jianying-editor-skill已具备阶段B同等能力，是否值得替换自研部分）→ 采纳/不采纳建议 → 若采纳，对第10节调用堆栈图步骤5/8/9/10/11的影响范围。

---

## 6. 整体验收标准（对照第12节Week2交付物）

- [ ] 交付物1"测试视频的故事板文件"——阶段C节点4产出，落盘为独立JSON文件
- [ ] 交付物2"剪映工程草稿文件"——阶段C节点5产出，经阶段D人工在剪映v5.9.0打开验证
- [ ] 交付物3"第三方组件PoC评估报告"——阶段E产出
- [ ] 阶段A-D全部单元/集成测试通过
- [ ] 风险清单（第11节）中与本周任务相关的4项风险（draft写入中断损坏、剪映6.0+加密、MCP Server的Python 3.13兼容性、jianying-editor-skill版本/许可证）均已有明确结论或缓解措施落地

## 7. 风险与待办提醒

- 节点1/2中标注的缓存路径、OpenStoryline启动命令与端口目前是占位值，需在阶段A验收前用第1周环境搭建的实际产出替换（若第1周尚未产出，视为本周新增前置阻塞项，建议阶段A开工第一件事就是核实）
- `capcut decrypt`命令的返回码语义需要实测确认，不要凭空假设
- OpenStoryline MCP的具体工具方法名需查其自身文档/工具清单，本计划中的`OpenStorylineMCPClient`为概念占位，需在阶段C开工前替换为真实调用
- `_shot_plan_to_materials`/`_init_empty_tracks`两个辅助函数依赖`shot_plan`真实字段结构与pyJianYingDraft真实API，需阶段C开工时对照实际库文档实现

## 8. 与源文档的章节映射

| 本计划章节 | 对应执行计划原文档章节 |
|---|---|
| 阶段A | 第3节步骤1-3、第9/10节、13.1节S1/S2 |
| 阶段B | 第6.2/6.3节 |
| 阶段C | 第3节步骤4-5、第6.1节、第9/10节 |
| 阶段D | 第12节Week2交付物 |
| 阶段E | 第14.5节、《第三方视频Skills生态评估》4.5节、第5.2节（幂等设计对照） |

---

*本计划由AI辅助生成，代码为概念级实现示意（用于明确函数签名、State字段与调用顺序），实际编码时请对照OpenStoryline/pyJianYingDraft的真实API文档调整具体调用细节。标注"待核实""占位"的部分需在对应阶段开工前优先确认，避免在错误假设上继续搭建后续代码。*

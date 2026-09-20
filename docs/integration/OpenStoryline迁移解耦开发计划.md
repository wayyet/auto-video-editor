# OpenStoryline 迁移解耦开发计划

> 本计划基于对两个真实仓库源码的核实结果编写，不是凭文档推测：
> - `FireRed-OpenStoryline`：https://github.com/wayyet/FireRed-OpenStoryline （Apache-2.0 协议）
> - `auto-video-editor`：https://github.com/wayyet/auto-video-editor
>
> **采用方案**：方案B——OpenStoryline 的素材理解/分镜/文案/BGM/时间线规划，改为人工在（已迁移进 auto-video-editor 的）网页里与其对话完成，`auto-video-editor` 只负责启动这个页面、等人工做完、读取产物文件。全流程新增 1 个人工关卡（关卡⓪），原有关卡①②③（步骤6/12/16）不受影响。

---

## 1. 背景与目标

第2周落地的集成方式是"MCP-first"：`auto-video-editor` 通过 MCP（Model Context Protocol，一种工具调用协议）连接一个**外部、独立进程**的 OpenStoryline 服务，服务代码在 `E:\Documents\kuaishou\FireRed-OpenStoryline`，用 FireRed 自己的 Python 3.11 环境启动。

本次要做的改动，源于三条明确要求：

| 编号 | 要求 |
|---|---|
| 1 | FireRed-OpenStoryline 的工作流功能模块（`src/open_storyline/nodes/core_nodes/` 下实际 **19 个节点文件**，不含基类）全部迁移进 `auto-video-editor` |
| 2 | 两个项目完全解耦，不存在任何调用和依赖关系 |
| 3 | 前端页面（`web/` 目录）一并迁移；`auto-video-editor` 不再用 MCP 调用 FireRed-OpenStoryline，改为直接在自己内部启动这个页面 |

**范围说明**：这次只处理 OpenStoryline 本体（MCP 服务 8001 端口 + 网页 7860 端口）。FireRed 家族的另外两个独立模型服务——ASR2S（语音识别，步骤8用）、Image-Edit（步骤15用）——是单独的服务，不在本次范围内，保持现状不动。

---

## 2. 现状架构（迁移前）

```
auto-video-editor（Python 3.13.11，Windows）
    │
    ├─ node_02_launch_openstoryline.py
    │     └─ OpenStorylineMCPClient 启动子进程
    │          command = [STORYLINE_FIRERED_PYTHON, "-m", "open_storyline.mcp.server"]
    │          cwd     = STORYLINE_FIRERED_ROOT（默认 E:\Documents\kuaishou\FireRed-OpenStoryline）
    │          → 拉起 MCP(8001) + 网页/agent_fastapi(7860)
    │
    ├─ node_03_open_preview.py
    │     └─ 打开 Edge 指向 http://127.0.0.1:7860（人工可看，但不必须操作）
    │
    └─ node_04_import_and_plan.py
          └─ 用 MCP call_tool 依次调：
             load_media → understand_clips → generate_script → plan_timeline_pro
             → select_bgm → generate_voiceover
             （split_shots 当前被跳过，Phase 1 简化处理）
          → 组装成 CanonicalTimeline，写入 state["storyline_plan"]
```

**耦合点**（本次要清除的）：

1. `config.py` 里 `STORYLINE_FIRERED_ROOT` 指向外部仓库路径
2. `config.py` 里 `STORYLINE_FIRERED_PYTHON` 指向外部 conda 环境的解释器
3. `mcp_clients/openstoryline_client.py`（`OpenStorylineMCPClient` 类，MCP 协议客户端）
4. `node_04` 里对上述客户端的 `call_tool` 逐步调用

---

## 3. 目标架构（迁移后）

```
auto-video-editor（唯一仓库，clone 一次即可跑全部步骤）
    │
    ├─ openstoryline/                       ← 新增：完整搬进来的前端+后端
    │    ├─ agent_fastapi.py                ← 原样搬，FastAPI 应用（挂载 /static、/node_map、首页聊天界面）
    │    ├─ web/                            ← 前端页面本体，原样搬
    │    ├─ src/open_storyline/             ← 19个节点 + mcp/ + skills/ + storage/ + utils/ + agent.py + config.py，原样搬
    │    ├─ config.toml                     ← OpenStoryline 自己的 LLM/VLM/Pexels/TTS Key 配置
    │    ├─ prompts/                        ← 节点用的提示词模板
    │    ├─ LICENSE                         ← Apache-2.0 原文件，随代码一起保留（合规要求）
    │    └─ requirements.txt                ← 独立于 auto-video-editor 主环境（见第7节）
    │
    ├─ node_02_launch_openstoryline.py      ← 改写：本地启动 openstoryline/agent_fastapi.py，普通HTTP健康检查
    ├─ node_03_open_preview.py              ← 不变（URL 仍是 127.0.0.1:7860，只是背后换成本地服务）
    ├─ node_checkpoint0_storyline_plan.py   ← 新增：关卡⓪，等人工在网页里完成规划
    ├─ node_04_import_and_plan.py           ← 改写：读磁盘产物文件，不再用 MCP
    │
    └─ （删除）mcp_clients/openstoryline_client.py
```

变化对照：

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 代码归属 | 两个独立仓库 | 一个仓库 |
| 启动 node_02 | MCP SDK 握手 + `list_tools()` 能力探测 | 起本地进程 + `GET http://127.0.0.1:7860/` 返回200 |
| node_04 取结果 | MCP `call_tool` 自动调用6个工具 | 人工在网页完成规划 → 读 `openstoryline/outputs/<session_id>/` 下的文件 |
| 人工关卡数 | 3个（步骤6/12/16） | **4个**（新增关卡⓪，见第5节） |
| clone 步骤 | 2个仓库 | 1个仓库 |
| 主环境 `mcp` SDK 依赖 | 需要 | 可移除（openstoryline/ 子系统自己装） |

---

## 4. 代码搬运清单

| 源路径（FireRed-OpenStoryline） | 目标路径（auto-video-editor） | 说明 |
|---|---|---|
| `agent_fastapi.py` | `openstoryline/agent_fastapi.py` | 挂载 `/static`、`/node_map`，`GET /` 返回聊天页面 |
| `web/index.html`、`web/static/`、`web/node_map/` | `openstoryline/web/` | 前端页面全部，原样拷贝 |
| `src/open_storyline/` | `openstoryline/src/open_storyline/` | 19个核心节点（`nodes/core_nodes/*.py`）+ `mcp/`、`skills/`、`storage/`、`utils/`、`agent.py`、`config.py` |
| `config.toml` | `openstoryline/config.toml` | 迁移后仍需人工重新填 LLM/VLM/Pexels/TTS 的 API Key |
| `prompts/` | `openstoryline/prompts/` | 各节点用的提示词模板 |
| `LICENSE` | `openstoryline/LICENSE` | Apache-2.0，已核实，无需额外授权 |
| `cli.py` | `openstoryline/cli.py`（可选） | 非必须，留作调试用（不经网页、直接命令行聊天） |

**不搬的部分**（原仓库自己也没纳入 git，靠脚本单独下载，这次照抄同样做法）：

| 内容 | 体积 | 处理方式 |
|---|---|---|
| `resource/`（BGM/字体/文案模板） | 约526MB | `openstoryline/` 下放一份下载脚本，启动前手动拉取到 `openstoryline/resource/` |
| `.storyline/models/`（AI模型权重） | 约116MB | 同上，拉取到 `openstoryline/.storyline/` |
| `.storyline/skills/` | 视用量而定 | OpenStoryline 自己的"剪辑风格归档"目录，运行时自动生成，不用预先搬 |

---

## 5. 图结构变化：新增关卡⓪

参照现有关卡①②③的实现方式（`interrupt()` 只做暂停，不做实质工作；实质工作放在恢复之后的下一个节点——这是本项目已验证过的经验，能避免恢复时重复执行副作用）。

```
START → clean_cache → launch_openstoryline → open_preview
                                                    │
                                    ┌───────────────┘
                                    ▼
                          关卡⓪：等待人工完成OpenStoryline网页规划
                          （interrupt，触发方式：强制中断）
                                    │ Command(resume=True)
                                    ▼
                              import_and_plan（读文件）
                                    │
                                    ▼
                              generate_draft → 关卡①（步骤6，不变）→ ...
```

人机协同表新增一行（对应主计划第7节）：

| 关卡 | 步骤位置 | 触发方式 | 恢复方式 | 说明 |
|---|---|---|---|---|
| **⓪（新增）** | 3与4之间 | 强制中断 `interrupt()` | `Command(resume=True)` | 人工在本地网页（`http://127.0.0.1:7860`）上传素材、和 Agent 对话完成分镜/文案/BGM/时间线规划 |
| ①②③ | 步骤6/12/16 | 不变 | 不变 | 不受本次改动影响 |

---

## 6. 节点级改造方案

### 6.1 `node_02_launch_openstoryline.py` 改写

```python
# nodes/node_02_launch_openstoryline.py
"""改写要点：
1. 不再依赖 mcp_clients.openstoryline_client.OpenStorylineMCPClient
2. 启动命令指向仓库内的 openstoryline/，不再指向外部 FireRed 环境
3. 就绪判断从 MCP list_tools() 探测，退回成 node_02 里本来就有的
   "老路径" HTTP 健康检查（health_checker），不做能力校验——
   因为代码已经搬进同一个仓库、同一套测试覆盖，不再需要对"远程服务"
   做运行时能力探测
"""
import subprocess
import time
from pathlib import Path

import httpx

from state import WorkflowState

OPENSTORYLINE_DIR = Path(__file__).resolve().parent.parent / "openstoryline"
OPENSTORYLINE_PYTHON = OPENSTORYLINE_DIR / ".venv" / "Scripts" / "python.exe"  # Windows
OPENSTORYLINE_WEB_PORT = 7860
HEALTH_TIMEOUT_S = 30


def launch_openstoryline_service(state: WorkflowState) -> dict:
    proc = subprocess.Popen(
        [
            str(OPENSTORYLINE_PYTHON), "-m", "uvicorn",
            "agent_fastapi:app", "--host", "127.0.0.1",
            "--port", str(OPENSTORYLINE_WEB_PORT),
        ],
        cwd=str(OPENSTORYLINE_DIR),
    )
    ready = _wait_for_ready(f"http://127.0.0.1:{OPENSTORYLINE_WEB_PORT}/", HEALTH_TIMEOUT_S)
    return {
        **state,
        "openstoryline_pid": proc.pid,
        "openstoryline_web_url": f"http://127.0.0.1:{OPENSTORYLINE_WEB_PORT}",
        "openstoryline_ready": ready,
        "status_log": state.get("status_log", []) + ["node_02_launch_openstoryline_done"],
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

**需要人工核对的点**：`OPENSTORYLINE_PYTHON` 这个独立虚拟环境的具体路径命名，等第7节的环境搭建做完后回填。

### 6.2 新增 `node_checkpoint0_storyline_plan.py`（关卡⓪）

```python
# nodes/node_checkpoint0_storyline_plan.py
from langgraph.types import interrupt

from state import WorkflowState


def checkpoint0_wait_storyline_plan(state: WorkflowState) -> dict:
    """只做中断，不做实质工作——实质的文件读取放在 node_04（见6.3），
    避免恢复重放时重复执行（本项目已验证的经验，见decisions-and-learnings）。
    """
    interrupt({
        "checkpoint": "⓪",
        "instruction": (
            f"请打开 {state.get('openstoryline_web_url')} ，"
            "上传素材并与Agent对话完成分镜/文案/BGM/时间线规划，"
            "完成后回复继续"
        ),
    })
    return {
        **state,
        "status_log": state.get("status_log", []) + ["checkpoint0_resumed"],
    }
```

### 6.3 `node_04_import_and_plan.py` 改写

```python
# nodes/node_04_import_and_plan.py
"""改写要点：
1. 不再 import mcp_clients.openstoryline_client
2. 不再逐个 call_tool；改为在磁盘上找 openstoryline/outputs/ 下
   人工刚完成规划的那个 session 目录
3. 复用现有的 storyline.contract.CanonicalTimeline /
   storyline.mapper（这两个模块本来就是"数据契约"层，
   跟数据是从MCP来的还是从文件读来的无关，可以直接沿用）
"""
from pathlib import Path

from storyline.contract import CanonicalTimeline, ContractInvalid  # ContractInvalid 见6.6
from storyline.mapper import build_canonical_timeline_from_plan  # 已有函数，签名以实际代码为准
from state import WorkflowState

OPENSTORYLINE_OUTPUTS_ROOT = Path(__file__).resolve().parent.parent / "openstoryline" / "outputs"


def import_video_and_plan_shots(state: WorkflowState) -> dict:
    session_dir = _find_latest_session_dir(OPENSTORYLINE_OUTPUTS_ROOT)
    if session_dir is None:
        return {
            **state,
            "error_log": state.get("error_log", []) + [
                "[node_04] 未在 openstoryline/outputs/ 下找到任何会话产物，"
                "请确认关卡⓪的规划已在网页里真正完成"
            ],
        }

    plan_file = session_dir / "plan_timeline_pro" / "plan_timeline_pro_latest.json"
    if not plan_file.exists():
        return {
            **state,
            "error_log": state.get("error_log", []) + [f"[node_04] 缺少产物文件: {plan_file}"],
        }

    try:
        canonical = build_canonical_timeline_from_plan(plan_file)
    except ContractInvalid as e:
        return {
            **state,
            "error_log": state.get("error_log", []) + [f"[node_04] 产物校验失败: {e}"],
            "storyline_error_code": "CONTRACT_INVALID",
        }

    return {
        **state,
        "storyline_session_id": session_dir.name,
        "storyline_outputs_root": str(session_dir),
        "storyline_plan": canonical.model_dump(),
        "status_log": state.get("status_log", []) + ["node_04_import_and_plan_done"],
    }


def _find_latest_session_dir(root: Path) -> Path | None:
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
```

**需要人工核对的点**：`plan_timeline_pro_latest.json` 的确切文件名/路径规则，以 `openstoryline/src/open_storyline/storage/agent_memory.py` 里 `ArtifactStore` 的实际落盘规则为准（去年"3技能迁移"任务里 `openstoryline-to-jianying` 的 `build_draft.py` 已经读过同一类产物，可以直接对照它的读取逻辑）。

`storyline_session_id`、`storyline_outputs_root` 是已有 state 字段（ADR-009 加过），这里直接复用，不新增字段。

### 6.4 `node_05_generate_draft.py` 微调

只改一行 import：

```python
# 改之前
from mcp_clients.openstoryline_client import ContractInvalid

# 改之后
from storyline.contract import ContractInvalid
```

### 6.5 清理 `mcp_clients/`

- 删除 `mcp_clients/openstoryline_client.py`
- `mcp_clients/__init__.py` 去掉这两行：
  ```python
  from mcp_clients.openstoryline_client import (
      MockOpenStorylineMCPClient,
      OpenStorylineMCPClient,
  )
  ```
  `firered_image_edit_client.py`、`jianying_cover_client.py` 两个文件不动（属于 Image-Edit/剪映封面服务，不在本次范围）

### 6.6 `storyline/contract.py` 补充

把原来在 `openstoryline_client.py` 里定义、`node_05` 还在用的 `ContractInvalid` 异常类挪过来：

```python
# storyline/contract.py 追加
class ContractInvalid(Exception):
    """OpenStoryline 产出的数据不满足 CanonicalTimeline / StorylinePlan 约束时抛出。"""
```

`storyline/firered_adapter.py`（原本是给 MCP `list_tools()` 做"远程工具清单校验"用的）：**本次先不删**。原因：里面的节点元数据表（工具名、参数结构）仍可作为对照文档，帮助核对 `openstoryline/src/open_storyline/nodes/core_nodes/` 搬过来后接口没变。等第8节阶段4跑通回归测试后再决定是否清理。

---

## 7. `openstoryline/` 子系统的 Python 环境策略

FireRed-OpenStoryline 官方要求 `python>=3.11`，依赖里有 torch/torchaudio/langchain/funasr 等重量级包；`auto-video-editor` 主环境明确写着"禁止引入 torch/torchaudio/moviepy"（现有 ADR-001 的既定决策，本次不推翻）。

**结论：`openstoryline/` 用独立虚拟环境，但目录和代码都在同一个 git 仓库里**——满足"一个项目"的要求，同时不动现有的依赖隔离决策。

```powershell
# 在 auto-video-editor 仓库根目录下执行
cd openstoryline
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` 内容直接沿用 FireRed-OpenStoryline 原仓库的依赖清单，随代码一起搬过来。

---

## 8. 分阶段实施计划

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| 1 | 按第4节清单把代码搬进 `auto-video-editor/openstoryline/`；装好独立venv | `openstoryline/` 目录下能单独用 `uvicorn agent_fastapi:app` 起服务，浏览器打开 `127.0.0.1:7860` 看到聊天页面 |
| 2 | 改写 `node_02`（6.1）；删 `mcp_clients/openstoryline_client.py`（6.5）；补 `ContractInvalid`（6.6） | `node_02` 单测通过，起服务不再走 MCP 握手 |
| 3 | 新增 `node_checkpoint0`（6.2）；改写 `node_04`（6.3）；接入 `graph.py` | 手动跑一次：网页里完成一次规划 → 关卡⓪ resume → `node_04` 能正确读到产物 |
| 4 | 微调 `node_05`（6.4）；跑现有全部单测+集成测试，确认没有回归 | 已有测试套件（第三周报告记录172条）全部通过 |
| 5 | 端到端实测：只 `git clone auto-video-editor` 一个仓库，从零跑通步骤1-5 | 全程不出现任何指向 `FireRed-OpenStoryline` 外部路径的报错或访问 |

---

## 9. 验收标准（整体）

- [ ] `grep -r "FireRed-OpenStoryline" auto-video-editor/` 除文档注释外，代码里无任何硬编码外部路径
- [ ] `auto-video-editor` 主环境的 `requirements.txt` 不再含 `mcp` SDK
- [ ] 只 clone `auto-video-editor` 一个仓库，配好两套虚拟环境（主环境+`openstoryline/.venv`），能跑完步骤1-5
- [ ] 关卡⓪的 interrupt/resume 能跨进程恢复（参照关卡①②③已验证过的测试模式）

---

## 10. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| `openstoryline/` 独立venv体积大（torch等），仓库clone变慢 | 开发体验 | venv本身不进git（`.gitignore`排除），只提交`requirements.txt` |
| 关卡⓪的产物文件路径规则与实际代码不符（6.3节标注的待核对点） | node_04读取失败 | 阶段3verification时对照`agent_memory.py`实际落盘规则修正 |
| 新增关卡⓪后，人工操作步骤变多（原3个关卡→4个） | 使用体验 | 已在第5节说明，这是方案B的既定取舍，非缺陷 |
| `storyline/firered_adapter.py`暂不删，可能有人误以为仍在被调用 | 代码可读性 | 阶段4后在文件头加注释说明"仅作参考文档，无运行时调用" |

---

## 11. 参考来源

- FireRed-OpenStoryline：https://github.com/wayyet/FireRed-OpenStoryline
- auto-video-editor：https://github.com/wayyet/auto-video-editor
- 本次核实方式：直接 clone 两个仓库到本地容器，逐文件核对（非网页浏览/非文档推测）
- 沿用的既有经验：`decisions-and-learnings`（LangGraph interrupt/resume幂等模式）、`architecture_decision_record.md` ADR-001/ADR-006/ADR-009（进程隔离、状态字段兼容规则）

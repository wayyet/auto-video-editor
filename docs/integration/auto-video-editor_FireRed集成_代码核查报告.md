# auto-video-editor × FireRed-OpenStoryline 集成核查报告

> 核查日期：2026-09-17
> 核查方式：克隆两个仓库的最新代码（非静态网页浏览），逐条 grep/diff 源码，在隔离沙箱里重装依赖、重跑单元测试
> 仓库版本：
> - `auto-video-editor`：commit `cd5be36` "feat(phase-1): FireRed-OpenStoryline MCP 集成 + storyline 子系统"
> - `FireRed-OpenStoryline`：main 分支最新快照

---

## 0. 这次做了什么

任务要求三件事：①参考 FireRed-OpenStoryline 仓库 docs/ 下 6 份文档 + 您上传的 4 份文档；②把 FireRed-OpenStoryline 嵌入 auto-video-editor；③验证 auto-video-editor 当前实现是否满足要求、指出不合理之处。

做法：不只读文档，而是把两个仓库真实克隆下来，用 `grep`/`diff` 逐条对照文档里的技术断言和源码实际内容，并在沙箱里重新安装依赖、重跑了一遍单元测试。**结论先说：auto-video-editor 的实际代码进度比几份参考文档描述的更靠前**，但也确实查出几个需要修的问题，详见第 3 节。

---

## 1. 参考文档核实

### 1.1 FireRed-OpenStoryline/docs/ 下的 6 份文档

全部确认存在，文件名逐字核对如下（第 3 份您给的名字看起来被截断了，实际去仓库确认过，就是这个完整文件名，不是打字问题）：

| # | 文件名（原样，未做任何简化） |
|---|---|
| 1 | `FireRed-OpenStoryline-auto-video-editor-集成评审.md` |
| 2 | `FireRed-OpenStoryline-Integration-Design.md` |
| 3 | `FireRed-OpenStoryline 集成 auto-video-ed.md`（确认为真实完整文件名） |
| 4 | `FireRed-OpenStoryline 嵌入 auto-video-editor：集成评审与落地方案 v2.0.md` |
| 5 | `FireRed-OpenStoryline 嵌入 auto-video-editor：集成评审与落地方案 (最终修正版)doubao.md` |
| 6 | `FireRed-OpenStoryline 嵌入 auto-video-editor：集成评审与落地方案 (最终修正版).md` |

同一目录下还有一份您上传的《FireRed-OpenStoryline_嵌入_auto-video-editor_集成综合评审与落地方案.md》—— **这份文档已经合并进仓库，和您上传的版本逐字节完全一致，没有漂移**。本报告后文简称它为"综合评审文档"。

### 1.2 您上传的 4 份文档 vs 仓库里的实际版本

这 4 份文档里，有 3 份在 `auto-video-editor/docs/integration/` 下能找到同名文件，但**版本落后**：

| 文档 | 仓库里是否存在 | 对比结果 |
|---|---|---|
| `architecture_decision_record.md` | 存在 | **仓库版本是修正前的旧版（v3.0）**。旧版写"5 类错误"、"84 条测试"；您上传的新版（v3.1）已经改成"6 类错误"、"172 条测试"，还新增了 ADR-011～014。新版还没推上去。 |
| `storyline_tools_inventory.md` | 存在 | **仓库版本也是旧版**。旧版标题写"21 Node + 2 builtin"（这是错的），您上传的新版已经改成"20 Node + 2 builtin = 22 tools"并修正了 `generate_ai_transition` 的分组位置。新版还没推上去。 |
| `plan_v3.1_validation.md` | **不存在** | 这份记录"本机 172 条单测全绿后回写字段差异"过程的文档，仓库里完全没有，只存在于您本地/这次上传。 |
| `FireRed-OpenStoryline_嵌入..._集成综合评审与落地方案.md` | 存在（在 FireRed-OpenStoryline 仓库，不在 auto-video-editor） | 完全一致，无差异。 |

**这解释了一个现象**：综合评审文档里有几处"当前代码是什么样"的描述是错的（详见 3.2 节 P4）——因为它读到的正是仓库里那份**还没更新的旧版** `architecture_decision_record.md`，跟着旧版一起写错了。

---

## 2. FireRed-OpenStoryline 嵌入 auto-video-editor：集成现状

不需要"从零开始做集成"——集成已经在 `cd5be36` 这次提交里落地了。实际结构：

```
auto-video-editor/
├── storyline/                  # 契约层 + FireRed 能力清单（新增）
│   ├── contract.py             # Pydantic 数据模型 + 错误码
│   ├── mapper.py                # CanonicalTimeline ↔ draft_content.json 互转
│   ├── firered_adapter.py      # MCP 工具清单（22个）+ 必需/可选清单
│   └── output_isolation.py
├── mcp_clients/
│   └── openstoryline_client.py # 真实 MCP client（stdio + streamable-http）
├── nodes/
│   ├── node_02_launch_openstoryline.py   # 启动 + 真实 MCP 探活
│   ├── node_04_import_and_plan.py        # 调用 FireRed 拿分镜
│   ├── node_05_generate_draft.py         # 落地剪映草稿
│   └── ...（共 21 个节点文件，覆盖 17 步 + fork/join/checkpoint）
└── docs/integration/            # ADR + 验证记录（部分滞后，见第1.2节）
```

对照《AI视频剪辑自动化工作流开发执行计划.md》的 17 步表：**17 步全部有对应节点文件**，包括之前标记"缺口待补"的步骤 13（`node_13_adjust_volume.py` 已存在）。

---

## 3. 验证结果

### 3.1 已实现，且比参考文档描述的更新（这些事不用再做）

| 发现 | 证据 | 对应哪份文档写得过时 |
|---|---|---|
| `storyline/` 子系统是真代码，不是设计稿 | `contract.py`(268行)/`mapper.py`/`firered_adapter.py`(355行，22个`ToolSpec(`) | —— |
| `mcp_clients/openstoryline_client.py` 真的用了 `mcp` SDK 做 stdio 通信 | 647 行文件，第273-274/421-422行 `from mcp import ClientSession, StdioServerParameters` / `from mcp.client.stdio import stdio_client` | 综合评审文档 §17 说它"仍然是骨架和 Mock"——不准确，它是真实实现（同时保留 `MockOpenStorylineMCPClient` 供测试用，这是正常设计） |
| node_02 已做真正的 MCP 探活 | 该文件 `_probe_mcp()` 函数，docstring 写"initialize + list_tools + capability check" | 综合评审文档 §6.2 建议"应该做真实 MCP 探活而非网页健康检查"——其实已经做了，同时保留了向后兼容的 `health_checker` 老路径 |
| 端口/模块名已修正 | `config.py` 第32-41行：注释明写"Phase 1 修正：对齐 FireRed config.toml"，`OPENSTORYLINE_MCP_PORT=8001`、`OPENSTORYLINE_WEB_PORT=7860` | 综合评审文档 §6.1 说"当前代码是 8006/8005 端口 + `openstoryline.server` 模块名"——这是旧状态，已经修正 |
| `state.py` 新增字段全部用 `NotRequired` 包装 | 第173-185行：`storyline_session_id`/`storyline_transport`/`storyline_plan` 等 7 个字段 | 正好对应 ADR-009 的要求，已落地 |
| 步骤13缺口已补齐 | `nodes/node_13_adjust_volume.py` 存在 | 主计划文档标记的"待补"已解决 |

### 3.2 真问题（按严重程度排序，建议按顺序修）

#### 🔴 P0：`requirements.txt` 硬编码本机专属 Windows 路径，换机器直接装不上

第 17/19/21 行：

```
-e e:\documents\kuaishou\langgraph-main\libs\langgraph
-e e:\documents\kuaishou\langgraph-main\libs\checkpoint
-e e:\documents\kuaishou\langgraph-main\libs\prebuilt
```

**这不是猜测——我在全新的 Linux 沙箱里执行 `pip install -r requirements.txt` 直接复现了报错**：

```
ERROR: e:documentskuaishoulanggraph-mainlibslanggraph is not a valid editable requirement.
```

**影响**：任何人（新同事、CI、灾备环境）只要不是在您这台、这个目录结构完全一致的电脑上，装依赖这一步就会失败。这是这次核查里最严重的一条。

**建议修法**（二选一，推荐第一种）：
- **推荐**：把这 3 行从 `requirements.txt` 挪到单独的 `requirements-local-dev.txt`，主文件改成正常发布版本号 `langgraph==1.2.9` / `langgraph-checkpoint==4.1.1` / `langgraph-prebuilt==1.1.0`（用公共 PyPI 版本）。README 里加一句："如需本地修改版 LangGraph，额外执行 `pip install -e <你的本地路径>`。"
- 备选：如果这 3 个包确实有必须依赖的本地补丁（不是简单锁版本），那就把补丁提 PR 合并回 LangGraph 官方仓库，或者把这份本地 fork 也发布成一个私有包索引，而不是裸路径。

（附：我用公共版本替换这 3 行后重装依赖、重跑单测，173 条全部正常，见第4节——说明就算真有本地补丁，至少不影响现有测试覆盖到的行为。）

#### 🟠 P1：`docs/integration/` 里两份文档版本滞后，还没推送新版

见 1.2 节表格。建议：把您本地的 v3.1 版 `architecture_decision_record.md`、`storyline_tools_inventory.md`，以及全新的 `plan_v3.1_validation.md`，一起提交推送到仓库。这样任何人（包括下次做类似核查的人/AI）读到的都是准确信息。

#### 🟡 P2：`StorylineErrorCode` 类的注释没跟上实际代码

`storyline/contract.py` 第241行：

```python
class StorylineErrorCode:
    """5 类错误码,贯穿 OpenStorylineMCPClient / node_02 / node_04 / node_05。"""

    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    MCP_CONNECT_FAILED = "MCP_CONNECT_FAILED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_EXECUTION_TIMEOUT = "TOOL_EXECUTION_TIMEOUT"
    CONTRACT_INVALID = "CONTRACT_INVALID"
```

docstring 写"5 类"，往下数是 6 个常量——加 `TOOL_EXECUTION_TIMEOUT` 时忘了改注释。无害，但顺手改一下（一行的事）。

#### 🟡 P3：FireRed 实际启用的节点数，和 auto-video-editor 侧记的数对不上（差1个）

`FireRed-OpenStoryline/config.toml` 的 `available_nodes` 只列了 **19 个**具名 Node（不含 `PlanTimelineNode`）：

```toml
available_nodes = [
    "LoadMediaNode", "SearchMediaNode", "SearchWebTopicNode", "SplitShotsNode", "LocalASRNode", "SpeechRoughCutNode", "GenerateAITransitionNode",
    "UnderstandClipsNode", "FilterClipsNode", "GroupClipsNode", "GenerateScriptNode", "ScriptTemplateRecomendation",
    "GenerateVoiceoverNode", "SelectBGMNode", "RecommendTransitionNode", "RecommendTextNode",
    "PlanTimelineProNode", "PlanTimelineAITransitionNode", "RenderVideoNode"
]
```

而且 `register_tools.py` 的注册逻辑就是直接遍历这个列表（`for node_name in cfg.local_mcp_server.available_nodes`）——**没在这个列表里的 Node，不会被注册成 MCP 工具**。也就是说，若现在真去跑一次 `list_tools()`，大概率只会拿到 19 个 Node 工具（+ 2 个内置，共 21 个），而不是 `storyline_tools_inventory.md` 降级基线里记的 22 个。

**好消息**：`plan_timeline` 已经被 auto-video-editor 分类进 `OPTIONAL_TOOLS`（缺了不报错），所以这个差异不会导致硬失败。**需要做的**：只是把 inventory 文档里的数字加一条注释说明，并且在真正跑 Phase 1 PoC、拿到真实 `list_tools()` 结果时，确认这个 19/21 的判断（这份 inventory 文档自己也写了"运行时仍以 list_tools() 输出为准，本表为降级基线"——机制上已经有兜底，这里只是提醒具体数字）。

#### 🟢 P4：综合评审文档里几处"当前代码现状"的描述已经过时

综合评审文档自己也说明了"本次无法直接拉取仓库启动服务，只做了静态网页核验"——这次我实际克隆仓库核对后，发现它对"现状"的描述有几处不准（见3.1节表格里列的三条：mcp_clients 骨架说法、config.py 端口说法、node_02 探活说法）。**它的架构建议和 ADR 本身依然合理，只是"现状"部分需要用本报告替换**。

#### 🟢 P5（次要）："(最终修正版).md" 主索引文档只存在于另一个仓库

`architecture_decision_record.md` 和 `plan_v3.1_validation.md` 都用类似 `docs/FireRed-OpenStoryline 嵌入...md` 这样的相对路径引用"主索引文档"。但这份文件实际只存在于 `FireRed-OpenStoryline/docs/` 下，**`auto-video-editor/docs/` 里没有这份文件**（已用 `find -iname "*最终修正版*"` 确认零结果）。如果有人只在 auto-video-editor 仓库内找这份"主索引"，会找不到。建议：要么在 auto-video-editor 侧放一个软链接/副本，要么把引用路径改成完整的跨仓库说明。

### 3.3 已知、且已有明确决策的未完成项（不是 bug）

**node_17（英文 AI 配音注入）目前只写静音占位，不是真的配音。**

`nodes/node_17_inject_english_tts_stub.py` 的 docstring 原文写明：

> Week 5 继续 stub，只写空 wav 占位…真实 FireRedTTS2 合成 + 动态变速补偿留 Week 6+。用户决策(2026-09-09)：Week 5 继续 stub，只写空 wav 占位。

也就是说，这是您 9 月 9 日主动决定推迟的，**不是遗漏或疏忽**。之所以在这里点出来，是避免有人（或未来的我）把它当成一个"bug"去修——它只是排期还没到。

### 3.4 无法从仓库单独判断、需要真实环境跑一次才能确认的项

1. 本机 venv 是否真的装了 `mcp` Python SDK——这是您机器上的安装状态，不在 git 仓库里，代码层面看不出来。
2. FireRed 真实服务启动后，`list_tools()` 的真实返回值——这次只核对了"配置文件层面应该注册多少个工具"（第3.2节P3），没有真的启动 FireRed 跑一次握手。
3. 主环境 Python 3.13 与 FireRed 要求的 3.11 之间，通过 MCP 协议边界能否稳定协作——这个只能靠真跑一次验证，代码走读看不出来。

---

## 4. 单元测试独立复核

为了验证"172 条单测全绿"这个说法是否可信，我在全新的隔离沙箱里重新装了一遍依赖（跳过了 P0 提到的 3 行硬编码路径，改用对应的公开版本号）、重新跑了一遍：

| | 您本机（Windows，`plan_v3.1_validation.md` 记录） | 本次核查（Linux 沙箱） |
|---|---|---|
| Python | 3.13.11 | 3.12（容器自带） |
| LangGraph 来源 | 本地 editable 路径 | 公开 PyPI 版本（1.2.9 / 4.1.1 / 1.1.0，版本号相同） |
| 结果 | 172 passed / 1 skipped / 0 failed（8.81s） | **171 passed / 2 skipped / 0 failed（7.25s）** |

**总数一致（173），唯一差异是 1 个测试的跳过条件**。查了一下跳过原因：

```
SKIPPED tests/unit/test_node_16_translate_subtitles.py:78: Windows 上 PIL load_default 永远可用,无法触发 missing_font issue
SKIPPED tests/unit/test_node_16_translate_subtitles.py:91: 无可用字体文件
```

第一条本来就是"只在 Windows 上跳过"的测试；第二条是因为这个 Linux 沙箱里没装任何字体文件，属于环境差异，不是代码问题。

**结论："172 条单测全绿"这个说法可信，换一个完全不同的操作系统、不同的依赖安装方式重跑，结果高度吻合，0 failed。**

---

## 5. 修复建议清单（按优先级）

1. **`requirements.txt` 去掉硬编码本机路径**（P0，见3.2节修法）——这个不修，团队里其他人电脑上这个项目跑不起来。
2. **把本地已经改好的 v3.1 版 ADR / inventory / validation 三份文档推送到仓库**（P1）——避免下次评审又读到旧数字。
3. **`StorylineErrorCode` docstring 改成"6 类"**（P2，一行改动）。
4. **`storyline_tools_inventory.md` 给"22 个工具"的数字加一条脚注**，说明当前 FireRed 部署 `available_nodes` 只启用 19 个 Node，实际以 `list_tools()` 为准（P3）。
5. Phase 1 PoC 真正跑一次 FireRed 服务时，顺带验证 3.4 节列的 3 项。

---

## 6. 参考来源

- `FireRed-OpenStoryline/docs/` 下 6 份文档（见1.1节）+ 综合评审文档
- 您上传的 `architecture_decision_record.md`、`plan_v3_1_validation.md`、`storyline_tools_inventory.md`
- `auto-video-editor` 仓库源码：`storyline/`、`mcp_clients/`、`nodes/`、`config.py`、`state.py`、`pyproject.toml`、`requirements.txt`、`tests/`
- `FireRed-OpenStoryline` 仓库源码：`config.toml`、`src/open_storyline/mcp/register_tools.py`
- 项目主计划文档：`AI视频剪辑自动化工作流开发执行计划.md`

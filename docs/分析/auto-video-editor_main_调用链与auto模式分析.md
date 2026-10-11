# auto-video-editor `main` 分支静态调用链与 Auto 模式分析

> 分析对象：`wayyet/auto-video-editor`  
> 分支：`main`  
> 分析方式：以当前 `main` 分支源码为准，重点检查 `graph.py` 的 LangGraph 拓扑、`nodes/storyline/` 节点实现、`config.py` 配置以及相关能力层调用。  
> 说明：本次会话中“参考附件截图”没有被文件检索接口成功取得，因此“截图中的 auto 模式”部分以源码中的 `STORYLINE_MODE=auto`、`_route_mode()` 和实际图拓扑为主要依据；如果截图中的 UI 文案与源码不同，应以截图实际内容再做一次对照。

---

## 1. 结论先看

### 1.1 `storyline_load_media` 在哪里被调用？

它不是通过 MCP Client 间接调用。

当前 `main` 分支中，它是 **LangGraph `auto` 模式的直接入口节点**：

```text
START
  ↓
launch_openstoryline
  ↓
open_preview
  ↓
_route_mode()
  ├── human → checkpoint0_storyline_plan
  └── auto  → storyline_load_media
```

关键代码：

- `graph.py:230-242`：`_route_mode()` 根据 `STORYLINE_MODE` 返回 `"storyline_load_media"`。
- `graph.py:517`：`storyline_load_media` 被注册为 LangGraph 节点。
- `graph.py:562-570`：`open_preview` 后通过条件边进入 `storyline_load_media`。
- `graph.py:676-683`：`storyline_load_media` 完成后继续进入 `search_media` 或 `split_shots`。

---

### 1.2 `storyline_load_media` 自己又调用了什么？

节点实现：

```text
nodes/storyline/node_load_media.py
        │
        └── storyline_load_media_node()
                │
                └── storyline_capabilities.load_media.load_media()
```

即：

```python
from storyline_capabilities.load_media import load_media as cap_load_media

result = cap_load_media(inputs, include_hash=False)
```

对应 `nodes/storyline/node_load_media.py:30-35`。

所以调用关系是：

```text
LangGraph Node
    │
    ▼
storyline_load_media_node()
    │
    ▼
storyline_capabilities.load_media.load_media()
    │
    ▼
生成 load_media.json
    │
    ▼
state["storyline_media_artifact"]
```

它最终写：

```text
outputs/<job_id>/storyline/load_media.json
```

并把路径写入：

```python
state["storyline_media_artifact"]
```

---

## 2. `storyline_load_media` 完整调用链

### 2.1 上游

```text
START
  │
  ▼
launch_openstoryline
  │
  ▼
open_preview
  │
  ▼
_route_mode
  │
  └── STORYLINE_MODE == "auto"
          │
          ▼
  storyline_load_media
```

`graph.py:559-570`：

```python
g.add_edge(START, "launch_openstoryline")
g.add_edge("launch_openstoryline", "open_preview")

g.add_conditional_edges(
    "open_preview",
    _route_mode,
    {
        "checkpoint0_storyline_plan": "checkpoint0_storyline_plan",
        "storyline_load_media": "storyline_load_media",
    },
)
```

因此：

> **只有 `STORYLINE_MODE=auto` 时，正常图执行才会从 `open_preview` 进入 `storyline_load_media`。**

---

## 3. `auto` 模式完整拓扑

当前 `auto` 模式的核心图可以简化成：

```text
                    START
                      │
                      ▼
          launch_openstoryline
                      │
                      ▼
                open_preview
                      │
                 _route_mode
                      │
                  ┌───┴───┐
                  │       │
               human     auto
                  │       │
                  ▼       ▼
          checkpoint0   load_media
              │            │
              ▼            ├── search_media（可选）
          import_video     │
              │            ▼
              ▼        split_shots
       get_storyboard       │
              │             ▼
              └──►      understand_clips
                            │
                            ▼
                       filter_clips
                            │
                            ▼
                       group_clips
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
        generate_script  recommend_     ...
              │          transition
       ┌──────┼──────┬───────┐
       ▼      ▼      ▼       ▼
   template voiceover BGM recommend_text
       │      │       │
       └──────┴───────┘
              │
              ▼
        plan_timeline_pro
              │
              ▼
           qa_gate
          ┌───┴────┐
          │        │
        retry     pass
          │        │
          └──►group│
                   ▼
            render_video
                   │
                   ▼
             storyline_join
                   │
                   ▼
             generate_draft
```

注意：

`generate_draft` 是 `auto` Storyline 子图与原剪映草稿生成链之间的汇合点。

---

# 4. `storyline_load_media` 的作用

它属于 Storyline 自动化链的 **素材入口 / Media Ingest 节点**。

输入：

```python
[
    {
        "path": state.get("video_input_path") or ""
    }
]
```

来源：

```text
state["video_input_path"]
```

然后调用：

```python
cap_load_media(inputs, include_hash=False)
```

输出：

```text
outputs/<job_id>/storyline/load_media.json
```

并写入：

```text
state["storyline_media_artifact"]
```

下游 `storyline_split_shots` 会读取这个 artifact。

因此它的职责不是：

- 生成脚本
- 生成配音
- 选择 BGM
- 生成时间线
- 渲染视频

它只负责：

> **把输入视频转换成 Storyline 后续节点可以消费的媒体清单/媒体元数据。**

---

# 5. `storyline_load_media` 后面真正会走什么？

## 5.1 `STORYLINE_PEXELS_API_KEY` 未配置

这是当前默认主路径：

```text
storyline_load_media
        ↓
storyline_split_shots
        ↓
storyline_understand_clips
        ↓
storyline_filter_clips
        ↓
storyline_group_clips
```

源码：

```python
if os.environ.get("STORYLINE_PEXELS_API_KEY", "").strip():
    return "storyline_search_media"

return "storyline_split_shots"
```

对应 `graph.py:256-264`。

---

## 5.2 配置了 Pexels Key

路径变成：

```text
storyline_load_media
        ↓
storyline_search_media
        ↓
storyline_split_shots
```

注意：

当前 `search_media` **不会回到 `load_media`**。

代码：

```python
g.add_edge("storyline_search_media", "storyline_split_shots")
```

对应 `graph.py:684`。

---

# 6. 当前存在的不可达 / 不会正常调用的节点

这里必须区分 4 种情况。

| 类型 | 含义 |
|---|---|
| A：结构性死节点 | 注册了，但没有有效入边 |
| B：条件不可达 | 有边，但条件函数当前永远不会返回该分支 |
| C：模式隔离 | 不是死节点，只在另一种模式运行 |
| D：Feature Flag 禁用 | 节点可能被调用，但默认只执行 noop |

---

# 7. A 类：结构性死节点

## 7.1 `storyline_search_web_topic`

文件：

```text
nodes/storyline/node_search_web_topic.py
```

注册：

```python
g.add_node(
    "storyline_search_web_topic",
    storyline_search_web_topic_node
)
```

但是当前 `graph.py` 中：

```text
没有任何 add_edge()
没有任何 add_conditional_edges() 指向它
```

所以：

```text
storyline_search_web_topic
        X
      不可达
```

结论：

> **当前 LangGraph 正常执行路径永远不会调用 `storyline_search_web_topic`。**

它内部虽然实现了 DuckDuckGo 搜索能力，但当前图没有把它接进去。

---

# 8. A 类：另一个结构性死节点

## 8.1 `storyline_generate_ai_transition`

文件：

```text
nodes/storyline/node_generate_ai_transition.py
```

图中注册：

```python
g.add_node(
    "storyline_generate_ai_transition",
    storyline_generate_ai_transition_node
)
```

但没有任何入边。

因此：

```text
storyline_generate_ai_transition
        X
      不可达
```

即使：

```text
STORYLINE_ENABLE_AI_TRANSITION=1
```

当前也没有用。

这是一个很容易误判的地方：

> **Feature Flag 开启 ≠ 节点会被执行。**

因为当前图拓扑本身就没有把它接进去。

---

# 9. A 类：`storyline_plan_timeline_ai_transition`

文件：

```text
nodes/storyline/node_plan_timeline_ai_transition.py
```

注册：

```python
g.add_node(
    "storyline_plan_timeline_ai_transition",
    storyline_plan_timeline_ai_transition_node
)
```

但当前图：

```text
没有进入它的边
也没有从它出去的边
```

所以：

```text
storyline_plan_timeline_ai_transition
        X
      不可达
```

它属于：

> **已经实现的替代实现 / AI Transition 版本，但尚未接入当前主拓扑。**

---

# 10. B 类：条件逻辑导致不可达

这是当前最值得注意的问题。

## 10.1 `storyline_local_asr`

图定义了：

```python
g.add_conditional_edges(
    "storyline_split_shots",
    _storyline_route_after_split_shots,
    {
        "storyline_understand_clips": "storyline_understand_clips",
        "storyline_local_asr": "storyline_local_asr",
    },
)
```

表面上看：

```text
split_shots
   ├── understand_clips
   └── local_asr
```

但是实际路由函数：

```python
media_artifact = state.get("storyline_media_artifact")

if media_artifact:
    return "storyline_understand_clips"

return "storyline_understand_clips"
```

两个分支最终都是：

```text
storyline_understand_clips
```

所以：

```text
storyline_local_asr
```

**当前永远不会被路由到。**

---

# 11. B 类：`storyline_speech_rough_cut` 也随之不可达

它的唯一上游是：

```python
g.add_edge(
    "storyline_local_asr",
    "storyline_speech_rough_cut"
)
```

因此：

```text
storyline_local_asr
        ↓
storyline_speech_rough_cut
```

由于上游 `storyline_local_asr` 本身不可达：

```text
storyline_speech_rough_cut
```

也不可达。

这不是两个独立问题。

真正的问题是：

```text
_storyline_route_after_split_shots()
```

没有真正实现 `has_audio` 判断。

代码注释也已经明确标记：

```text
当前不可达
不是遗留物
是没写完的 has_audio 检测
```

---

# 12. 当前 auto 图实际少了一条设计中的 ASR 分支

设计意图应该类似：

```text
split_shots
     │
     ├── 无音频 ──► understand_clips
     │
     └── 有音频 ──► local_asr
                       │
                       ▼
                 speech_rough_cut
```

但是当前实际是：

```text
split_shots
     │
     └──────────────► understand_clips
```

所以 ASR 粗剪链目前没有进入实际执行路径。

---

# 13. C 类：human 模式专属节点

以下节点不是死代码。

它们属于旧的人工模式：

```text
checkpoint0_storyline_plan
import_video
get_storyboard_plan
```

human：

```text
open_preview
     ↓
checkpoint0_storyline_plan
     ↓
import_video
     ↓
get_storyboard_plan
     ↓
generate_draft
```

auto：

```text
open_preview
     ↓
storyline_load_media
```

所以：

> 在 `STORYLINE_MODE=auto` 下，这三个节点不会被调用；但在 `human` 模式下仍然有效。

不能把它们简单归类成“死代码”。

---

# 14. D 类：`launch_openstoryline` 在 auto 模式下会执行，但实际上是 noop

这是另一个容易误判的地方。

图始终有：

```text
START
 ↓
launch_openstoryline
 ↓
open_preview
```

但是：

```python
if STORYLINE_MODE == "auto":
```

时：

```text
不启动 OpenStoryline Web UI
不启动 uvicorn 子进程
不进行 HTTP health check
```

只返回：

```python
openstoryline_ready = True
```

以及：

```text
node_02_launch_openstoryline_auto_skipped
```

因此：

```text
launch_openstoryline
```

是：

> **被调用，但 auto 模式下执行的是快速 noop。**

---

# 15. 当前项目已经不再真正使用 OpenStoryline MCP 链路

这是本项目目前架构变化中最重要的一点。

`config.py` 已明确说明：

```text
OpenStoryline 现走本地 uvicorn + httpx 健康检查，
不再有 MCP 链路。
```

`node_02_launch_openstoryline.py` 也明确：

```text
不再有 MCP/stdio 链路
OpenStorylineMCPClient 已删除
```

所以当前：

```text
auto-video-editor
      │
      X
OpenStoryline MCP Client
```

已经不是运行时主链。

---

# 16. 那些“看起来像 MCP”的配置是什么？

`config.py` 仍保留：

```python
OPENSTORYLINE_MCP_PORT = 8001
```

以及：

```python
openstoryline_mcp_url()
```

还有：

```text
openstoryline_mcp_endpoint
```

但当前主要作用是：

> **向后兼容旧 checkpoint / 旧代码字段。**

不要把它理解成：

```text
当前 graph → MCP Server → Tool
```

当前真实架构已经变成：

```text
LangGraph
   │
   ▼
storyline node
   │
   ▼
storyline_capabilities
   │
   ▼
本地 Python 能力实现
```

---

# 17. `storyline_load_media` 当前架构已经从 MCP 迁移到本地 Capability

旧思路大致是：

```text
LangGraph
   ↓
MCP Client
   ↓
OpenStoryline MCP Server
   ↓
load_media Tool
```

当前代码是：

```text
LangGraph
   ↓
storyline_load_media_node
   ↓
storyline_capabilities.load_media.load_media
```

即：

```text
                当前架构

        ┌─────────────────────┐
        │      LangGraph      │
        └──────────┬──────────┘
                   │
                   ▼
        storyline_load_media
                   │
                   ▼
      storyline_capabilities
                   │
                   ▼
              load_media
                   │
                   ▼
          load_media.json
```

这也是当前项目“解耦”的核心之一。

---

# 18. `clean_cache` 也是一个特殊的“注册但不自动执行”节点

`graph.py`：

```python
g.add_node("clean_cache", clean_cache)
```

但：

```python
g.add_edge(START, "clean_cache")
```

已经被删除。

现在是：

```text
START
 ↓
launch_openstoryline
```

而不是：

```text
START
 ↓
clean_cache
 ↓
launch_openstoryline
```

原因是：

> 避免每次 `graph.invoke()` 都自动删除缓存。

因此：

```text
clean_cache
```

不是死模块。

它属于：

> **图中注册，但由外部 Web UI / API 显式触发的节点。**

---

# 19. `storyline_render_video` 的特殊情况

它有真实入边：

```text
qa_gate
   ↓
render_video
   ↓
storyline_join
```

因此它是：

> **可达节点。**

但默认：

```python
STORYLINE_ENABLE_RENDER_SMOKE_TEST = false
```

所以正常情况下：

```text
storyline_render_video
        ↓
noop
        ↓
storyline_join
```

它只写：

```text
storyline_render_video_skipped
```

不会真正渲染。

因此不要把它归类为“不可调用”。

准确说法是：

> **节点会被调用，但默认功能关闭。**

---

# 20. `storyline_generate_ai_transition` 与 Render 的区别

两者都与 Feature Flag 有关，但结构不同。

### Render

```text
qa_gate
  ↓
render_video
  ↓
join
```

有实际入边。

所以：

```text
会调用
但默认 noop
```

### AI Transition

```text
generate_ai_transition
```

没有任何入边。

所以：

```text
不会调用
即使打开 STORYLINE_ENABLE_AI_TRANSITION 也不会自动调用
```

这是两个完全不同的问题。

---

# 21. 当前不可达模块汇总

| 模块 | 是否注册 | 是否有入边 | 当前是否可达 | 原因 |
|---|---:|---:|---:|---|
| `storyline_search_web_topic` | 是 | 否 | ❌ | 完全没有图边 |
| `storyline_generate_ai_transition` | 是 | 否 | ❌ | 完全没有图边 |
| `storyline_plan_timeline_ai_transition` | 是 | 否 | ❌ | 完全没有图边 |
| `storyline_local_asr` | 是 | 有 | ❌ | 路由函数永远返回 `understand_clips` |
| `storyline_speech_rough_cut` | 是 | 有 | ❌ | 上游 `local_asr` 不可达 |
| `clean_cache` | 是 | 自动链无 | ⚠️ | 外部 API / UI 显式触发 |
| `checkpoint0_storyline_plan` | 是 | 有 | auto 下否 | human 模式专用 |
| `import_video` | 是 | 有 | auto 下否 | human 模式专用 |
| `get_storyboard_plan` | 是 | 有 | auto 下否 | human 模式专用 |
| `storyline_render_video` | 是 | 有 | ✅ | 默认执行 noop |
| `assembly_*` 6 节点 | 是 | 默认有 | ✅ | `ASSEMBLY_QC_GATE_ENABLED=true` |
| `node_14/15/16/17` | 是 | 有 | human 主链 | auto 子图之后仍汇入 `generate_draft` |

---

# 22. Auto 模式到底是什么意思？

源码定义：

```python
STORYLINE_MODE = os.environ.get(
    "STORYLINE_MODE",
    "human"
).lower().strip()
```

允许：

```text
human
auto
```

默认：

```text
human
```

---

## 22.1 human 模式

核心思想：

> **OpenStoryline Web UI + 人工参与。**

流程：

```text
open_preview
     ↓
checkpoint0_storyline_plan
     ↓
人工在 Web UI 完成：
  - 分镜
  - 文案
  - 时间线规划
  - BGM 等
     ↓
import_video
     ↓
get_storyboard_plan
     ↓
generate_draft
```

特点：

- 有人工关卡
- 有 interrupt / resume
- 依赖 Web UI
- 保留旧流程
- 适合人工干预

---

# 23. Auto 模式

核心思想：

> **不等待人工在 OpenStoryline Web UI 中规划，而是由本地 Storyline 节点图自动完成规划。**

流程：

```text
open_preview
     │
     ▼
storyline_load_media
     │
     ▼
split_shots
     │
     ▼
understand_clips
     │
     ▼
filter_clips
     │
     ▼
group_clips
     │
     ├── generate_script
     │       ├── template recommendation
     │       ├── voiceover
     │       ├── BGM
     │       └── text recommendation
     │
     ├── recommend_transition
     │
     ▼
plan_timeline_pro
     │
     ▼
qa_gate
     │
     ├── retry → group_clips
     │
     └── pass → render_video
                    │
                    ▼
               storyline_join
                    │
                    ▼
               generate_draft
```

---

# 24. Auto 模式真正解决了什么问题？

核心不是“多了一个开关”。

它解决的是：

> **把原来依赖 OpenStoryline Web UI + 人工操作的 Storyline 规划阶段，转换为 LangGraph 内部的确定性自动流水线。**

也就是：

```text
旧：

人
 │
 ▼
OpenStoryline Web UI
 │
 ▼
人工规划
 │
 ▼
产物
 │
 ▼
LangGraph
```

变成：

```text
新：

LangGraph
 │
 ▼
Storyline Nodes
 │
 ▼
Capabilities
 │
 ▼
Canonical Timeline
 │
 ▼
generate_draft
```

---

# 25. Auto 模式的一个重要误区

`auto` 并不意味着：

> 整个视频制作过程 100% 无人工节点。

因为 auto Storyline 子图完成后：

```text
generate_draft
   ↓
assembly QC
   ↓
node_06_human_reorder
   ↓
node_07_speed_fit
...
node_12_human_add_bgm
...
```

后续主流程仍然保留人工关卡。

因此：

> **auto 是 Storyline“剧情线/素材规划阶段”的自动化，不是整个项目所有节点都变成无人值守。**

---

# 26. Auto 模式和 MCP 的关系

当前代码实际上已经把两者解耦。

### 旧架构

```text
LangGraph
    ↓
MCP Client
    ↓
OpenStoryline MCP
    ↓
Tools
```

### 当前 auto 架构

```text
LangGraph
    ↓
Storyline Nodes
    ↓
storyline_capabilities
    ↓
本地 Python 能力
```

因此：

```text
Auto ≠ MCP Auto
```

更准确：

```text
Auto = LangGraph 内部自动执行 Storyline 节点图
```

---

# 27. 为什么还保留一些 MCP 相关字段？

主要原因是兼容与迁移。

例如：

```python
openstoryline_mcp_endpoint
```

以及：

```python
OPENSTORYLINE_MCP_PORT
```

当前代码注释已经明确：

```text
向后兼容
```

因此后续维护时建议把它们分成：

```text
Legacy / Compatibility
```

而不是继续理解为：

```text
Runtime MCP Dependency
```

---

# 28. 当前代码存在的主要结构问题

## P0：ASR 条件路由没有真正实现

当前：

```python
if media_artifact:
    return "storyline_understand_clips"

return "storyline_understand_clips"
```

结果：

```text
local_asr
speech_rough_cut
```

完全不可达。

建议修改为类似：

```text
读取 media artifact
      │
      ▼
检查 media.metadata.has_audio
      │
   ┌──┴──┐
   │     │
 true   false
   │     │
   ▼     ▼
local   understand
ASR
```

---

## P1：AI Transition 节点注册了，但没有接入

当前：

```text
storyline_generate_ai_transition
```

以及：

```text
storyline_plan_timeline_ai_transition
```

都没有进入主图。

如果后续要启用 AI Transition，至少需要：

```text
recommend_transition
        ↓
generate_ai_transition
        ↓
plan_timeline_ai_transition
```

或者根据最终设计放到合适的 fan-in 点。

---

## P1：Web Topic Search 也是孤立节点

当前：

```text
storyline_search_web_topic
```

没有任何调用入口。

如果产品需要：

```text
用户主题
  ↓
Web Search
  ↓
事实/素材补充
  ↓
Script
```

需要重新设计它应该插入：

```text
generate_script
```

之前还是：

```text
group_clips
```

之前。

目前不能仅仅“随便加一条边”，因为会影响 State 数据依赖和 fan-in。

---

# 29. 建议的最终模块状态划分

建议以后不要只用“有没有代码”判断模块是否存在。

建议维护以下状态：

```text
ACTIVE
ACTIVE-BUT-FEATURE-FLAG-OFF
MODE-SPECIFIC
EXTERNAL-TRIGGER
STRUCTURALLY-UNREACHABLE
CONDITIONALLY-UNREACHABLE
LEGACY-COMPATIBILITY
```

当前项目可以大致标记：

```text
storyline_load_media
    ACTIVE

storyline_search_media
    ACTIVE / PEXELS-CONDITIONAL

storyline_local_asr
    CONDITIONALLY-UNREACHABLE

storyline_speech_rough_cut
    CONDITIONALLY-UNREACHABLE

storyline_search_web_topic
    STRUCTURALLY-UNREACHABLE

storyline_generate_ai_transition
    STRUCTURALLY-UNREACHABLE

storyline_plan_timeline_ai_transition
    STRUCTURALLY-UNREACHABLE

storyline_render_video
    ACTIVE-BUT-FEATURE-FLAG-OFF

clean_cache
    EXTERNAL-TRIGGER

checkpoint0_storyline_plan
    MODE-SPECIFIC: HUMAN

import_video
    MODE-SPECIFIC: HUMAN

get_storyboard_plan
    MODE-SPECIFIC: HUMAN
```

---

# 30. 最终结论

### `storyline_load_media` 的位置

```text
open_preview
   ↓
_route_mode
   ↓
STORYLINE_MODE=auto
   ↓
storyline_load_media
```

它是：

> **Auto Storyline 图的第一实际业务节点。**

---

### `storyline_load_media` 调用的真正对象

不是 MCP。

而是：

```text
storyline_capabilities.load_media.load_media()
```

---

### 当前确定不可达的节点

至少有：

```text
storyline_search_web_topic
storyline_generate_ai_transition
storyline_plan_timeline_ai_transition
storyline_local_asr
storyline_speech_rough_cut
```

其中：

```text
search_web_topic
generate_ai_transition
plan_timeline_ai_transition
```

属于：

> **没有图边。**

而：

```text
local_asr
speech_rough_cut
```

属于：

> **有图边，但当前条件路由永远不会进入。**

---

### Auto 模式的本质

不是：

```text
打开一个 Auto 开关
```

而是：

```text
从：

Web UI + 人工 Storyline Planning

切换到：

LangGraph + 本地 Storyline Capability
```

即：

```text
Human Mode
Web UI
   ↓
人工规划
   ↓
产物
   ↓
LangGraph
```

切换为：

```text
Auto Mode
LangGraph
   ↓
19 节点 Storyline Pipeline
   ↓
Canonical Timeline
   ↓
generate_draft
```

---

## 31. 推荐后续检查顺序

如果继续修改这个项目，建议按这个顺序：

### 第一优先级

修复：

```text
split_shots
   ↓
has_audio 判断
   ↓
local_asr
   ↓
speech_rough_cut
```

因为这是一个已经设计好、但当前实际上跑不到的功能分支。

### 第二优先级

明确 AI Transition 是否仍然需要。

如果需要：

```text
generate_ai_transition
plan_timeline_ai_transition
```

应正式接入图。

如果不需要，应删除或明确标记为未来实验节点，避免造成“已经支持”的错觉。

### 第三优先级

处理：

```text
storyline_search_web_topic
```

确定它到底属于：

```text
主题研究能力
```

还是：

```text
素材检索能力
```

再决定放进 Storyline 图的哪个 fan-in 点。

---

## 32. 关键源码定位表

| 文件 | 关键位置 | 作用 |
|---|---|---|
| `graph.py` | 230-242 | `human/auto` 路由 |
| `graph.py` | 517-552 | Storyline 节点注册 |
| `graph.py` | 562-570 | `open_preview → auto/human` |
| `graph.py` | 676-684 | `load_media → search/split` |
| `graph.py` | 689-697 | `split_shots → understand/local_asr` |
| `graph.py` | 710-749 | Storyline fan-out / fan-in |
| `graph.py` | 753-769 | QA → render → join |
| `nodes/storyline/node_load_media.py` | 30-56 | `storyline_load_media` 实现 |
| `nodes/storyline/node_search_web_topic.py` | 全文件 | 当前孤立节点 |
| `nodes/storyline/node_local_asr.py` | 全文件 | 当前条件不可达 ASR |
| `nodes/storyline/node_speech_rough_cut.py` | 全文件 | 当前条件不可达粗剪 |
| `nodes/storyline/node_generate_ai_transition.py` | 全文件 | 当前孤立 AI Transition |
| `nodes/storyline/node_plan_timeline_ai_transition.py` | 全文件 | 当前孤立 Timeline 变体 |
| `nodes/node_02_launch_openstoryline.py` | 94-129 | auto 下不启动 Web UI |
| `nodes/node_05_generate_draft.py` | 81-107 | Storyline plan → 剪映草稿 |
| `nodes/storyline/join_storyline.py` | 47-123 | timeline → `storyline_plan` |
| `config.py` | 258-272 | `STORYLINE_MODE` 定义 |
| `config.py` | 238-268 | MCP 解耦说明 |

---

## 33. 一句话架构总结

```text
STORYLINE_MODE=auto
        ↓
open_preview
        ↓
storyline_load_media
        ↓
本地 Storyline Capability Pipeline
        ↓
CanonicalTimeline
        ↓
storyline_join
        ↓
generate_draft
        ↓
剪映草稿生成与后续 QC / 人工关卡
```

**因此，`storyline_load_media` 是 Auto 模式 Storyline 子图的真正入口，而不是一个由 MCP Server 隐式触发的工具节点。当前最明确的代码问题是 ASR 条件分支实际上不可达；另外三个节点则属于完全没有接入图拓扑的孤立模块。**

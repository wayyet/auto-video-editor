# auto-video-editor 字幕 VIP 动画与 job_id 链路设计审查及修订执行计划

> 审查日期：2026-11-18  
> 项目：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 审查基线：`ea40d941b1824bf22b15b750f031841dad7ce485`  
> 字幕动画池功能提交：`7c0bbc728dcd586eff300ddecdb79b7c9c0c638e`  
> 原始待审文档：[`auto-video-editor_草稿52373c6f9e11字幕VIP动画核验与job_id链路修复计划_20261118.md`](https://github.com/wayyet/auto-video-editor/blob/main/docs/第七期开发/auto-video-editor_%E8%8D%89%E7%A8%BF52373c6f9e11%E5%AD%97%E5%B9%95VIP%E5%8A%A8%E7%94%BB%E6%A0%B8%E9%AA%8C%E4%B8%8Ejob_id%E9%93%BE%E8%B7%AF%E4%BF%AE%E5%A4%8D%E8%AE%A1%E5%88%92_20261118.md)

---

## 1. 审查结论

### 1.1 总体结论

**原文档的核心故障判断基本正确，但修复方案存在遗漏和架构层面的改进空间，不建议原样直接实施。**

已经由 `main` 分支代码确认：

1. `WorkflowState` 声明了 `session_id`，没有声明 `thread_id`。
2. Console、Agent Gate CLI 和普通 CLI 的标准初始 state 均把任务 ID 写入 `session_id`，不是 `thread_id`。
3. LangGraph 执行时，调用方已经通过 `config["configurable"]["thread_id"]` 传入真实线程 ID；节点当前没有读取这个 config。
4. node_10 中文、英文入口分别实现了自己的 seed 解析逻辑。英文入口的路径兜底可能把固定的剪映草稿库目录名当成任务 ID。
5. node_09 在一个节点调用中依次处理中文、英文两份草稿，因此两份草稿使用同一个 seed；它没有 node_10 英文入口导致的双语 seed 分裂，但在缺少 `thread_id` 时仍未使用真正的逻辑任务 ID。
6. 原有跨语言一致性测试直接调用 `apply_text_fx(..., job_id=...)`，没有覆盖 node_10 两个图节点如何从执行上下文解析任务 ID。

### 1.2 建议的修复方向

推荐将**LangGraph `RunnableConfig` 中的 `configurable.thread_id` 作为首要、权威的任务 ID 来源**，并将 `session_id` 作为旧 checkpoint 的兼容来源。把解析逻辑放入公共工具模块，供 node_09、node_10 中文入口和 node_10 英文入口共同使用。

不建议把共享解析函数定义在 `nodes/node_10_inject_text_fx.py`，再由 node_09 反向导入。节点之间不应建立这种依赖关系。

本问题属于**P1 功能一致性缺陷**，不是当前证据能够证明的 P0 系统可用性故障：字幕动画仍能写入草稿，问题主要在同一任务的双语动画一致性和按任务轮换语义。

### 1.3 审查边界

本次审查读取了 GitHub `main` 分支代码、资源库、测试及目标 Markdown 文档。用户本机的两份 `draft_content.json`、剪映截图原始文件和缓存资源文件并未作为本次对话附件提供，也不属于 Git 仓库可读取内容。因此：

- 代码层面的 seed 解析缺陷、初始 state 键、资源库数量和哈希计算可以独立核验。
- 原文档所报告的两份本机草稿中 5/5 + 5/5 字幕的具体动画对象、缓存路径实际存在、剪映 UI VIP 徽标、最终渲染画面，不能仅凭仓库代码独立复现。
- 这些本机实测结果在本文中标记为“原文档报告”，不能误写成“本次审查已直接读取并验证”。

---

## 2. 代码与资源核验结果

### 2.1 `WorkflowState` 不含 `thread_id`

文件：[`state.py`](https://github.com/wayyet/auto-video-editor/blob/main/state.py)

当前 `WorkflowState` 使用 `TypedDict(total=False)`，包含：

```python
class WorkflowState(TypedDict, total=False):
    # 全局
    session_id: str
```

全文件没有 `thread_id` 字段声明。`total=False` 表示字段不是运行时强制必填；它不会自动把 LangGraph config 中的 `thread_id` 复制到 state。

**判断：原文档关于 `state.get("thread_id")` 在标准流程中取不到值的结论成立。**

### 2.2 标准入口已有 `session_id`

| 入口 | 当前代码行为 | 核验结果 |
|---|---|---|
| Console | `web/console_state.py::build_initial_state()` 写入 `"session_id": thread_id` | 已有逻辑任务 ID |
| Agent Gate CLI | `agent_gate/cli.py::_write_initial_state()` 写入 `"session_id": thread_id` | 已有逻辑任务 ID |
| 普通 CLI | `scripts/run_workflow.py::_default_initial_state()` 写入 `"session_id": thread_id` | 已有逻辑任务 ID |
| 自定义初始 state JSON | `_load_initial_state()` 读取 JSON，并覆盖 outputs 根路径；不保证 JSON 中有 `thread_id` 或 `session_id` | 需要依赖执行 config |

这里暴露出原修复计划的遗漏：它增加 `thread_id` 字段和入口写入，但没有充分利用当前已经存在且等于任务 ID 的 `session_id`。

### 2.3 LangGraph config 已经传递真实 `thread_id`

**Agent Gate：** `agent_gate/runner.py::run_worker()` 构造：

```python
cfg = {"configurable": {"thread_id": thread_id}}
```

随后首跑使用 `graph.invoke(initial_state, cfg)`，崩溃续跑也复用相同 config。人工恢复路径同样围绕相同 thread 执行。

**普通 CLI：** `scripts/run_workflow.py::_invoke_async()` 构造：

```python
config = {"configurable": {"thread_id": thread_id}}
```

随后将 state 和 config 一起交给 `run_with_events()`。

**设计含义：** 任务 ID 已经存在于 LangGraph 的执行上下文。节点可通过 LangGraph 支持的 `RunnableConfig` 参数读取它，无需仅依靠把同一个值复制到多个 state 构造入口。

### 2.4 node_10 中文与英文入口的 seed 解析不一致

文件：[`nodes/node_10_inject_text_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_10_inject_text_fx.py)

当前中文入口 `inject_text_fx()`：

```python
job_id = str(state.get("thread_id") or "").strip()
if not job_id:
    job_id = Path(str(raw_draft_path)).parent.name
```

当前英文入口 `inject_text_fx_en()`：

```python
job_id = (
    str(state.get("thread_id") or "").strip()
    or Path(str(draft_dir_raw)).parent.name
)
```

两处均未读取 LangGraph config，也未尝试读取 `state["session_id"]`。

在原文档所描述的目录结构下：

- 中文 `draft_path` 指向中文草稿目录内的 `draft_content.json`，其父目录名类似 `AutoVideo_console-...`。
- `draft_dir_en_branch` 本身是英文草稿目录；对这个目录再取 `.parent.name`，可能得到共享草稿库根目录名 `com.lveditor.draft`。

因此，英文入口会对不同任务反复使用相同的根目录名作为 seed，英文轮换失去按任务区分的能力。

注意：`Path(draft_dir_raw).parent.name` 的实际结果依赖真实目录结构。`com.lveditor.draft` 是原文档根据本机路径报告的结果，不应把它硬编码成所有部署环境的必然结果。

### 2.5 node_09 的问题性质与 node_10 不完全相同

文件：[`nodes/node_09_inject_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_09_inject_fx.py)

node_09 当前使用：

```python
job_id = str(state.get("thread_id") or "").strip()
if not job_id:
    draft_path = state.get("draft_path")
    job_id = Path(str(draft_path)).parent.name if draft_path else "unknown"
```

随后在同一个 `inject_fx()` 调用中遍历 `dual_draft_targets(state)`，用同一个 `job_id` 处理中文和英文草稿。

结论：

- node_09 的中文和英文转场不会因为两个入口分别解析 seed 而发生本次 node_10 这种分裂。
- 但在 `thread_id` 缺失时，它仍然以中文草稿目录名代替任务 ID，seed 语义不准确。
- 修复时应统一使用公共解析器，避免未来 node_09 与 node_10 的任务 ID 语义再次漂移。

### 2.6 资源库数量、顺序和动画条目核验

文件：[`templates/text_resource_library.json`](https://github.com/wayyet/auto-video-editor/blob/main/templates/text_resource_library.json)

`jy_common/template_library.py::list_text_animations()` 按 JSON 数组顺序遍历指定动画类型，过滤 `is_vip` 后依次返回名称，不会对列表重新排序。因此，原文档按 intro VIP 列表顺序从 0 开始编号的方式与当前实现一致。

本次解析 `intro` 数组得到：

- intro 条目数：78。
- `is_vip == true`：78 条，全部符合。
- idx 4：`兔子弹跳`，duration 0.5s，resource_id `7187785892382118461`，effect_id `8398145`，md5 `5d157129853f479ea15f75075c1ef070`。
- idx 58：`跳跳捣蛋鬼`，duration 1.8s，resource_id `7200340219109839419`，effect_id `9503089`，md5 `897a0c194e07ace6813a2eba797ac22f`。
- idx 63：`逐字旋入`，duration 1.5s，resource_id `7229520427196879421`，effect_id `13137035`，md5 `9838c6772ddbd48126f30baf74200f99`。

### 2.7 SHA-256 计算结果核验

当前 `_pick_intro_animation()` 的计算方式为：

```python
seed_input = f"{job_id}|text_intro_pool"
seed = int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:8], 16)
index = seed % len(intro_list)
```

本次使用 Python `hashlib.sha256` 独立计算，得到：

| 候选 job_id | SHA-256 前 8 位 | 十进制值 | `% 78` | 对应动画 |
|---|---:|---:|---:|---|
| `console-52373c6f9e11` | `a223a106` | 2720243974 | 58 | 跳跳捣蛋鬼 |
| `AutoVideo_console-52373c6f9e11` | `a7ce6360` | 2815320928 | 4 | 兔子弹跳 |
| `com.lveditor.draft` | `dde4718f` | 3722736015 | 63 | 逐字旋入 |

**判断：原文档给出的三组哈希和索引计算正确。** 这能证明三个候选字符串会映射到对应资源库索引，但不能单独证明本机草稿实际采用了哪个字符串；实际输入仍需由运行时日志或草稿证据确认。

---

## 3. 原文档问题清单

### 3.1 P1：旧 checkpoint 兼容策略没有利用 `session_id`

原计划的 `_resolve_job_id(state)` 只考虑：

```text
thread_id → 中文草稿目录名 → 空字符串
```

但当前项目的标准入口长期写入：

```python
{"session_id": thread_id}
```

旧 checkpoint 因此可能已经带有正确的逻辑任务 ID。忽略 `session_id`，会让旧 checkpoint 不必要地降级到路径解析，造成 seed 与逻辑任务 ID 不一致。

**修正：** 解析优先级至少应为：

```text
config.configurable.thread_id
    → state.thread_id
    → state.session_id
    → 中文 draft_path 推导出的兼容值
```

在项目标准目录 `AutoVideo_<thread_id>` 下，末级路径兜底可去掉已知的 `AutoVideo_` 前缀。不得使用英文分支目录的上级目录名作为任务 ID。

### 3.2 P1：共享 helper 放在 node_10 中会引入错误依赖方向

原计划把 `_resolve_job_id()` 定义在 `nodes/node_10_inject_text_fx.py`，然后让 `nodes/node_09_inject_fx.py` 使用它。

这会造成 node_09 依赖 node_10：

```text
node_09 → node_10
```

两个节点属于同层业务节点，不应相互依赖。以后 node_10 的重构可能无意影响 node_09，且不利于独立测试和代码复用。

**修正：** 将解析函数放在公共工具模块，例如：

```text
jy_common/job_identity.py
```

node_09 和 node_10 只依赖公共工具模块。

### 3.3 P1：原计划没有覆盖普通 CLI 和自定义初始 state

原计划修改：

- `state.py`
- `web/console_state.py`
- `agent_gate/cli.py`
- node_10 / node_09

但普通 CLI 还有 `scripts/run_workflow.py`：

- `_default_initial_state()` 目前也只写 `session_id`。
- `_load_initial_state()` 支持外部 JSON；外部 JSON 不保证含 `thread_id`。
- `_invoke_async()` 已经把 `thread_id` 放在 LangGraph config 中。

如果采用“仅在 state 中增加 `thread_id`”的方案，就必须同步覆盖所有入口、自定义 JSON 和恢复路径，否则会产生入口差异。

**修正：** 以 LangGraph config 为权威源，避免依靠各个入口分别写入 state。若最终仍决定新增 state 字段，必须把所有入口、恢复入口和 JSON 注入规则列成单独迁移项，不能只修改 Console 与 Agent Gate。

### 3.4 P1：新增 state 字段会破坏现有冻结键集合测试

文件：[`tests/unit/test_console_state.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_console_state.py)

`test_frozen_key_set` 明确断言顶层 state 键集合为：

```python
{
    "session_id",
    "video_input_path",
    "video_input_paths",
    "storyline_outputs_root",
    "storyline_targets",
    "error_log",
}
```

因此，若在 `build_initial_state()` 新增 `thread_id`，该测试必然需要同步修改，并且要更新创建台冻结契约文档及相关契约测试。

**修正：** 推荐 config 解析方案，不必为了读取任务 ID 而扩张初始 state 冻结契约。

### 3.5 P1：测试验证了核心算法，没有验证生产节点的 seed 提取

文件：[`tests/unit/test_node_10_inject_text_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_node_10_inject_text_fx.py)

现有 `test_inject_text_fx_auto_pool_same_job_zh_en_consistent` 直接调用：

```python
apply_text_fx(..., job_id="job-consistent")
```

这验证了“同一个 job_id 进入核心算法时，两个草稿会选出同一个动画”，但绕过了：

- `inject_text_fx()` 如何解析中文任务 ID；
- `inject_text_fx_en()` 如何解析英文任务 ID；
- 两个图节点是否从同一个 LangGraph config 获取 ID。

现有英文入口测试主要断言动画被写入、状态标签被记录，没有断言最终选择的动画与中文入口相同。

**修正：** 新增节点入口级和真实图调用级测试，不能只测试 `apply_text_fx()`。

### 3.6 P1：node_09 的“同病”描述需要更准确

原文档称 node_09“同病但未破一致”。建议改成：

- node_09 的 seed 来源也没有优先读取 LangGraph config，故任务 ID 语义不统一。
- 但 node_09 一次性处理双语草稿，使用同一个 seed，因此不会导致本次 node_10 的双语 seed 分裂。
- node_09 仍应纳入公共 resolver 重构和回归测试。

### 3.7 P1：P0 优先级偏高

从当前代码和原文档描述看：

- 工作流没有因该问题崩溃；
- 字幕动画仍然写入草稿；
- 动画资源仍然来自 VIP intro 资源库；
- 主要问题是中英文动画不一致，以及英文分支按任务轮换失效。

建议将本修复定为 **P1**。只有当产品验收把“同一视频中英动画一致”设为上线阻断条件时，才将其定义为发布门禁；不宜将其描述为已发生的数据损坏或系统不可用 P0。

### 3.8 P2：本机草稿证据和时间线证据的表述过于确定

原文档声称：

- 两份本机草稿全部字幕都使用 VIP 动画；
- 动画缓存资源已就位；
- 渲染正常；
- 根据英文目录时间戳和中文草稿 mtime，可以排除旧代码产物。

这些信息可能来自作者本机检查，但当前审查无法直接读取对应 `draft_content.json`、缓存文件和截图原件。

此外，目录名中的时间戳或文件 mtime 不能严格证明生成进程当时运行的是哪个 Git commit。文件可能被复制、覆盖或保留时间戳。

**修正：** 真正证明产物来自新代码，应记录任务启动时的 commit SHA、`thread_id`、节点日志和草稿文件 hash。缓存路径存在也不等同于最终渲染通过；仍需剪映重新打开、预览和导出验证。

### 3.9 P2：附录中的“零依赖核验配方”不具备跨平台性

原命令使用：

- `grep`
- `head`
- `sha256sum`
- `/c/Users/...` 路径格式

这些命令和路径格式适用于 Git Bash / MSYS 等环境，不是 Windows PowerShell 的原生命令，也不适用于所有 WSL/Linux 挂载方式。

另外，按 `name` 行号使用 `12 + 8N` 推导 JSON 条目位置，依赖 JSON 排版固定，文件格式变化后就可能失效。

**修正：** 用 Python 标准库 `json` 解析 JSON，避免按行号猜字段；并为 PowerShell、Git Bash 和 WSL 分别给出正确路径形式，或统一要求在项目 `.venv` 中运行核验脚本。

### 3.10 P2：node_10 的英文入口应明确称为 node_10b

原文档部分位置把中英文两个入口都概括成 node_10。代码实际函数名为：

- `inject_text_fx()`：中文主线 node_10。
- `inject_text_fx_en()`：英文分支 node_10b。

后续代码改动清单和验收日志应使用准确节点名，避免排查时把两个入口混为一谈。

---

## 4. 修订后的目标设计

### 4.1 核心不变量

修复后必须同时满足：

1. 一个 LangGraph `thread_id` 对应一个稳定的字幕动画 seed。
2. 同一任务的中文和英文草稿必须使用完全相同的 seed。
3. 同一草稿中所有我方字幕使用同一个 intro 动画。
4. 不同任务根据不同 `thread_id` 独立计算动画索引；不能保证任意两个任务必然选中不同动画，因为取模后允许自然碰撞。
5. node_09 转场轮换和 node_10 字幕动画轮换使用同一逻辑任务 ID 解析规则。
6. 不能把 `com.lveditor.draft` 这类共享根目录名当成任务 ID。
7. 重放同一任务时，确定性 ID 和动画选择保持稳定；不能因中英文入口路径不同而改变。
8. 不改变人工在剪映中创建的文本素材和人工动画。

### 4.2 任务 ID 权威来源

推荐解析顺序：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `config["configurable"]["thread_id"]` | LangGraph 执行上下文，首选权威值 |
| 2 | `state["thread_id"]` | 兼容显式携带该字段的调用方 |
| 3 | `state["session_id"]` | 兼容现有 Console、Agent Gate、普通 CLI 和旧 checkpoint |
| 4 | `state["draft_path"]` 对应的中文草稿目录名 | 仅在前三项都缺失时使用；按已确认的目录约定处理 `AutoVideo_` 前缀 |
| 5 | 无法解析 | 明确报错或标记降级，不允许静默退化为共享根目录名或固定 `unknown` |

禁止使用：

```python
Path(state["draft_dir_en_branch"]).parent.name
```

作为正式任务 ID 的兜底来源。

### 4.3 公共 resolver 放置位置

新增：

```text
jy_common/job_identity.py
```

建议接口：

```python
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def resolve_job_id(
    state: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> str:
    """解析当前任务的稳定 ID；优先使用 LangGraph config.thread_id。"""
    configurable = (config or {}).get("configurable") or {}
    if isinstance(configurable, Mapping):
        value = configurable.get("thread_id")
        if str(value or "").strip():
            return str(value).strip()

    for key in ("thread_id", "session_id"):
        value = state.get(key)
        if str(value or "").strip():
            return str(value).strip()

    draft_path = state.get("draft_path")
    if draft_path:
        folder_name = Path(str(draft_path)).parent.name
        # 当前项目正式草稿目录约定：AutoVideo_<thread_id>
        if folder_name.startswith("AutoVideo_"):
            folder_name = folder_name[len("AutoVideo_"):]
        if folder_name.strip():
            return folder_name.strip()

    raise ValueError(
        "无法解析当前任务 ID：configurable.thread_id、state.thread_id、"
        "state.session_id 和中文 draft_path 均不可用"
    )
```

说明：上面是设计接口示例。落地前应根据项目 Python 版本和静态类型规则调整类型注解。节点函数应按当前安装的 LangGraph 版本，以 `RunnableConfig` 类型参数接收 config，并通过图级测试确认运行时确实注入该参数。

### 4.4 节点调用方式

公共 resolver 只负责解析 ID，不负责读取、修改草稿：

```python
from langchain_core.runnables import RunnableConfig
from jy_common.job_identity import resolve_job_id


def inject_text_fx(state: WorkflowState, config: RunnableConfig) -> dict:
    job_id = resolve_job_id(state, config)
    # 后续仍调用 apply_text_fx(..., job_id=job_id)
    ...


def inject_text_fx_en(state: WorkflowState, config: RunnableConfig) -> dict:
    job_id = resolve_job_id(state, config)
    # 英文草稿仍走 apply_text_fx(..., job_id=job_id)
    ...


def inject_fx(state: WorkflowState, config: RunnableConfig) -> dict:
    job_id = resolve_job_id(state, config)
    # 中文、英文转场共用同一个 job_id
    ...
```

实施时需要兼容当前测试中直接调用节点函数的方式。可通过测试辅助函数显式传入 config，或为纯函数 resolver 单独编写单测；不要为了单测方便而让生产节点在缺少任务 ID 时静默使用固定 seed。

### 4.5 为什么不优先新增 `WorkflowState.thread_id`

本次缺陷不是因为系统没有任务 ID，而是 node 函数没有读取已经传入的 LangGraph config。

直接增加 state 字段会导致：

- `state.py` 变更；
- `web/console_state.py` 冻结契约变更；
- `agent_gate/cli.py` 初始 state 变更；
- `scripts/run_workflow.py` 初始 state 和自定义 JSON 入口变更；
- `tests/unit/test_console_state.py::test_frozen_key_set` 等契约测试变更；
- 旧 checkpoint 缺少该字段时仍要设计兼容逻辑。

而 config 已经包含 `thread_id`，并且首跑、人工恢复、崩溃续跑均通过 config 传递。**先从 config 读取可以减少改动面，也能覆盖不含 `session_id` 的自定义 JSON。**

如后续确有状态卡、事件审计或业务节点需要从 state 直接展示任务 ID，可以另行评估是否将 `thread_id` 持久化到 state；不应把这当成本次 seed 修复的必要前提。

---

## 5. 分阶段修复执行计划

### 阶段 0：基线和证据固化

**目标：** 在改代码前，确认当前缺陷、代码版本和测试基线，防止把本机草稿问题误判为代码回归。

| 任务 | 操作 | 验收标准 |
|---|---|---|
| 0.1 | 记录 `main` 当前 commit SHA | 记录完整 SHA，不只记录短 SHA |
| 0.2 | 记录 `7c0bbc7` 的字幕动画池实现 commit | 确认源代码与审查基线的父子关系 |
| 0.3 | 固化 `text_resource_library.json` 的 intro VIP 列表 | 78 条、顺序、name、resource_id、effect_id、md5 均可由 JSON 脚本复现 |
| 0.4 | 备份两份本机草稿的 `draft_content.json` 和 `draft_info.json` | 原始文件 hash、备份路径有记录；不直接覆盖原稿 |
| 0.5 | 记录本机两份草稿路径、目录结构和 `draft_path` / `draft_dir_en_branch` 实际值 | 确认路径 `.parent.name` 的真实结果 |
| 0.6 | 获取当前相关测试基线 | 记录用例总数、通过数、失败数、跳过数；不能仅引用历史 commit message |

**阶段 0 通过条件：** 代码基线、资源库基线和本机草稿证据可追溯；不对现有草稿执行写操作。

### 阶段 1：实现统一任务 ID resolver

**目标：** 消除 node_09、node_10 中文和英文入口之间的 ID 解析差异。

| 任务 | 涉及文件 | 实施要求 |
|---|---|---|
| 1.1 | `jy_common/job_identity.py` | 新增纯函数 `resolve_job_id(state, config)`；按第 4.2 节顺序解析 |
| 1.2 | `nodes/node_10_inject_text_fx.py` | `inject_text_fx()` 和 `inject_text_fx_en()` 接收 `RunnableConfig`，调用同一个 resolver |
| 1.3 | `nodes/node_09_inject_fx.py` | `inject_fx()` 使用同一个 resolver；同一次调用继续让 zh/en 共用同一个 job_id |
| 1.4 | `apply_text_fx()` | 保持核心动画写入逻辑不变，仍显式接收 `job_id`；不在核心函数内部重新解析 state |
| 1.5 | 类型和注释 | 明确 `job_id` 在本功能中的语义就是逻辑任务 `thread_id`，不是单次运行 `run_id` 或恢复尝试 `attempt_id` |

**重要约束：** 不允许 node_09 导入 node_10 的内部函数；resolver 必须属于公共工具层。

### 阶段 2：补齐单元测试和图级测试

**目标：** 验证真实节点入口收到的 ID，而不仅仅是核心动画选择算法。

| 用例 | 验证内容 | 通过标准 |
|---|---|---|
| 2.1 config 优先级 | config.thread_id 与 state.session_id 故意设置为不同值 | 选择 config.thread_id |
| 2.2 state 兼容 | config 缺失，state.thread_id 存在 | 选择 state.thread_id |
| 2.3 旧 checkpoint 兼容 | 无 state.thread_id，只有 session_id | 选择 session_id，不走草稿路径兜底 |
| 2.4 最末级路径兜底 | config 和两个 state ID 均缺失，提供中文 `draft_path` | 能按项目目录约定解析 ID；不能得到 `com.lveditor.draft` |
| 2.5 缺失 ID | config、state ID、中文 draft_path 均缺失 | 明确失败或按显式定义的降级契约处理；不静默使用固定值 |
| 2.6 中文 node_10 | 真实调用 `inject_text_fx(state, config)` | 动画索引等于 `sha256(thread_id + '|text_intro_pool')[:8] % 78` |
| 2.7 英文 node_10b | 真实调用 `inject_text_fx_en(state, config)` | 和中文入口动画 name、resource_id、effect_id、md5、duration 一致 |
| 2.8 双语图级测试 | 通过 LangGraph 编译图运行两个节点，configurable 中传入 thread_id | 两节点接收到相同 config thread_id；双语草稿动画一致 |
| 2.9 跨任务轮换 | 使用两个已知会映射到不同索引的 thread_id | 两任务按各自 hash 结果选择；不假设随机任务必然不同 |
| 2.10 node_09 回归 | 中文和英文草稿在一次 inject_fx 调用中处理 | 转场 seed 相同且来自 resolver |
| 2.11 幂等性 | 相同草稿、相同 thread_id 重放 | 两次结果字节级一致或满足现有幂等断言 |
| 2.12 人工素材保护 | 混合我方字幕和人工文本素材 | 只修改 `text-zh-*` / `text-en-*` 我方字幕，不修改人工文本和人工动画 |

建议新增 `tests/unit/test_job_identity.py`，并扩充：

- `tests/unit/test_node_10_inject_text_fx.py`
- `tests/unit/test_node_09_inject_fx.py`
- 必要时增加真实 LangGraph 图级测试，不能用直接调用 `apply_text_fx()` 替代。

**验收重点：** 至少一条测试必须走到真实图节点，并证明 LangGraph 传入的 `configurable.thread_id` 最终进入 SHA-256 输入。

### 阶段 3：回归测试

在项目根目录执行。Windows PowerShell 推荐：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\unit\test_job_identity.py `
  tests\unit\test_node_10_inject_text_fx.py `
  tests\unit\test_node_09_inject_fx.py `
  tests\unit\test_template_library_fx_pools.py -q
```

随后执行相关工作流和持久化恢复测试：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\unit\test_console_state.py `
  tests\unit\test_run_workflow_llm_backend.py `
  tests\integration\test_week4_graph.py -q
```

若项目的集成测试目录中有专门验证 PostgreSQL checkpoint resume 的用例，应将其加入回归范围；不要把纯内存 saver 测试当成持久化恢复的唯一证据。

**通过标准：**

- 新增测试全部通过。
- 原有 node_09、node_10、资源池测试没有新增失败。
- Console 初始 state 冻结契约未被无意修改。
- Agent Gate 和普通 CLI 首跑、人工恢复、崩溃续跑均可传入同一个 thread_id。
- 测试报告记录真实执行的用例数和结果。

### 阶段 4：剪映真机验收

**目标：** 证明代码写入结果在剪映中可正常识别和渲染。

1. 使用全新的任务 ID 启动一条新任务，并保存运行时 config 中的 `thread_id`。
2. 工作流完成后，分别打开中文和英文 `draft_content.json`。
3. 枚举 `materials.texts` 中 ID 前缀为 `text-zh-` / `text-en-` 的我方字幕。
4. 对每个字幕确认对应 text track segment 的 `extra_material_refs` 包含 `text-anim-{字幕 material id}`。
5. 检查 `materials.material_animations` 对象的 `animations[0]`：
   - `type == 'in'`；
   - `name` 相同；
   - `resource_id`、`effect_id`、`md5` 相同；
   - `duration` 相同；
   - 与资源库 VIP intro 条目一致。
6. 用记录的 thread_id 独立计算预期索引，与草稿实际 name 对照。
7. 在剪映中重新打开中文和英文草稿，确认入场动画面板识别为对应 VIP 动画。
8. 逐条预览字幕动画，确认无缺失、无静止替代、无异常闪烁或文字裁切。
9. 实际导出一个验收视频，确认渲染阶段没有资源缺失、VIP 限制弹窗或错误降级。
10. 保存 UI 截图、导出文件信息、两份草稿 hash 和运行 commit SHA。

**通过标准：** JSON 结构、资源库元数据、剪映 UI 和导出结果四类证据一致。只检查 JSON 中存在 `path` 不足以判定渲染成功。

### 阶段 5：存量任务处理与回滚

**原则：不自动修改已有草稿。**

对原任务 `52373c6f9e11`：

- 原文档报告其两份草稿均已写入 VIP 入场动画；如实际草稿预览和导出均正常，不需要为了代码修复自动重写它们。
- 若需要让双语动画保持一致，先关闭剪映，备份两个草稿目录中的 `draft_content.json` 和 `draft_info.json`。
- 使用同一个明确的 `thread_id` 调用修复后的动画注入逻辑，分别处理中文和英文草稿。
- 注入后重新检查 `material_animations`、`extra_material_refs` 和资源库字段，再打开剪映验证。
- 不得在剪映仍打开草稿时直接覆盖 JSON，以免剪映内存状态覆盖磁盘修改。

回滚方式：

1. 代码回滚到修复前 commit。
2. 对草稿回滚，使用阶段 0 备份恢复 `draft_content.json` 和 `draft_info.json`。
3. 本修复不涉及数据库 schema，不需要数据库迁移或 checkpoint 表回滚。
4. 若修复期间产生新任务，保留任务目录和日志供排查，不要以清空 outputs 或删除 checkpoint 作为默认回滚方式。

---

## 6. 验收清单

### 6.1 代码验收

- [ ] 公共 resolver 位于 `jy_common/job_identity.py` 或等价公共模块。
- [ ] resolver 首先读取 LangGraph config 的 `configurable.thread_id`。
- [ ] 旧 checkpoint 可从 `session_id` 正确取得逻辑任务 ID。
- [ ] node_09、node_10 中文、node_10b 英文使用同一 resolver。
- [ ] 不再通过英文草稿目录的 `.parent.name` 获取 job_id。
- [ ] 不存在 `com.lveditor.draft` 或 `unknown` 作为默认任务 seed 的静默路径。
- [ ] `apply_text_fx()` 仍然只消费明确传入的 `job_id`。
- [ ] 人工创建的文本和动画不会被清理或覆盖。

### 6.2 测试验收

- [ ] resolver 优先级测试通过。
- [ ] `session_id` 旧 checkpoint 兼容测试通过。
- [ ] node_10 中文入口真实节点测试通过。
- [ ] node_10b 英文入口真实节点测试通过。
- [ ] LangGraph config 注入图级测试通过。
- [ ] 双语动画 name 和完整资源元数据一致。
- [ ] SHA-256 索引断言通过。
- [ ] node_09 转场双语一致性回归通过。
- [ ] 幂等重放和人工素材保护测试通过。
- [ ] 相关测试套件无新增失败。

### 6.3 真机验收

- [ ] 新任务中文草稿全部我方字幕有动画绑定。
- [ ] 新任务英文草稿全部我方字幕有动画绑定。
- [ ] 中英文动画 name、resource_id、effect_id、md5、duration 一致。
- [ ] 动画都属于 intro VIP 资源池。
- [ ] 剪映重新打开后仍能识别动画。
- [ ] 播放预览和导出结果正常。
- [ ] 验收记录保存 commit SHA、thread_id、草稿 hash 和截图。

---

## 7. 对原始 Markdown 的具体修订意见

原始文件建议按以下内容修订，而不是只替换其中的代码片段：

| 原文档位置 | 修订要求 |
|---|---|
| 结论先行 | 将“100% 使用 VIP、资源就位、渲染正常”标注为本机实测报告，并列出证据文件；不要把它表述成代码仓库已验证事实 |
| §2 草稿实证 | 保留动画资源字段；补充两份 `draft_content.json` 的 SHA-256 和原始证据来源 |
| §3 缺陷 | 区分 node_10 英文入口 seed 分裂与 node_09 仅 seed 语义偏差 |
| §4 根因链 | 加入 `session_id` 已存在的事实；同时解释 config.thread_id 已在 worker 调用中传递 |
| §5 修复计划 | 把公共 resolver 移到 `jy_common`；让节点读取 `RunnableConfig`；加入 session_id 兼容 |
| §5 修复计划 | 不应只改 `web/console_state.py` 和 `agent_gate/cli.py`；普通 CLI 和自定义 initial state 也必须纳入设计 |
| §5 测试 | 增加真实节点入口和 LangGraph config 注入测试；扩展英文入口测试，不要仅验证 `apply_text_fx()` |
| §6 验证方案 | 增加双语资源元数据一致性、剪映重开、预览和实际导出验收 |
| §7 注意事项 | 明确旧 checkpoint 使用 session_id 的处理方式，以及重新执行 node_10 时动画可能变化的风险 |
| 附录 | 将“零依赖”改成“Git Bash 适用命令”或改为跨平台 Python JSON 核验脚本 |
| 时间线佐证 | 将目录时间戳和 mtime 降级为辅助证据；用运行日志中的 commit SHA 证明生成代码版本 |

---

## 8. 关键源文件索引

| 文件 | 审查用途 |
|---|---|
| [`state.py`](https://github.com/wayyet/auto-video-editor/blob/main/state.py) | WorkflowState 字段声明，确认存在 session_id、不存在 thread_id |
| [`web/console_state.py`](https://github.com/wayyet/auto-video-editor/blob/main/web/console_state.py) | Console 初始 state 构造及冻结键契约 |
| [`agent_gate/cli.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/cli.py) | Agent Gate 初始 state 写入 |
| [`agent_gate/runner.py`](https://github.com/wayyet/auto-video-editor/blob/main/agent_gate/runner.py) | worker 构造 configurable.thread_id 并调用 graph |
| [`scripts/run_workflow.py`](https://github.com/wayyet/auto-video-editor/blob/main/scripts/run_workflow.py) | 普通 CLI 初始 state 与 config 传递 |
| [`nodes/node_10_inject_text_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_10_inject_text_fx.py) | 中文 node_10、英文 node_10b seed 解析和字幕动画注入 |
| [`nodes/node_09_inject_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/node_09_inject_fx.py) | 转场 seed 解析及双草稿注入 |
| [`nodes/_dual_draft.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/_dual_draft.py) | 双草稿目标集合 |
| [`jy_common/template_library.py`](https://github.com/wayyet/auto-video-editor/blob/main/jy_common/template_library.py) | VIP 动画列表保序逻辑 |
| [`templates/text_resource_library.json`](https://github.com/wayyet/auto-video-editor/blob/main/templates/text_resource_library.json) | 78 条 intro VIP 资源定义 |
| [`tests/unit/test_console_state.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_console_state.py) | Console 冻结 key set 测试 |
| [`tests/unit/test_node_10_inject_text_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_node_10_inject_text_fx.py) | 字幕动画当前单测及覆盖缺口 |
| [`tests/unit/test_node_09_inject_fx.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_node_09_inject_fx.py) | 转场节点回归测试 |

---

## 9. 最终建议

**按本修订计划实施，不建议照原始文档直接增加 state 字段后就结束。**

本次最小且完整的修复闭环应为：

```text
LangGraph config.configurable.thread_id
                  │
                  ▼
       jy_common.job_identity
                  │
          ┌───────┼────────┐
          ▼       ▼        ▼
       node_09  node_10  node_10b
          │       │        │
          ▼       ▼        ▼
      转场 seed  字幕 seed  字幕 seed
                  │        │
                  └──同一 job_id──┘
                           │
                           ▼
           sha256(job_id + '|text_intro_pool')
                           │
                           ▼
                 intro VIP 池 78 条
                           │
                           ▼
              zh/en 两份草稿动画完全一致
```

先完成公共 ID 解析和入口级测试，再执行剪映真机验收。对原任务 `52373c6f9e11` 默认保持原稿不动，只有在备份完成且明确需要双语动画对齐时，才单独重放动画注入。

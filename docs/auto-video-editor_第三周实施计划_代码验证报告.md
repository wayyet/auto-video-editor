# auto-video-editor 第三周实施计划代码验证报告

> 验证日期：2026-09-16  
> 验证仓库：`wayyet/auto-video-editor`  
> 验证分支：`main`  
> 验证基准：`docs/AI视频剪辑自动化工作流_第三周详细实施计划.md`

---

## 1. 验证目的

本报告用于核对当前仓库代码是否满足《AI视频剪辑自动化工作流_第三周详细实施计划.md》提出的第三周要求。

验证范围重点包括：

1. 第 3 周新增节点 6～13。
2. LangGraph 图拓扑与节点 7 护栏重试。
3. 关卡① / ② 的 `interrupt/resume` 与 SQLite Checkpointer。
4. `draft_content.json` 原子写入。
5. 心跳写入、PowerShell 外部监控和 Windows 任务计划程序注册。
6. 第三周端到端验收标准。

> 本报告属于**代码与仓库静态验证**。当前环境无法直接启动该仓库、剪映客户端、FireRedASR2S 或 Windows 任务计划程序，因此“运行时实测”与“代码存在性/结构验证”分开记录，不能把 README 中的历史测试声明直接视为本次重新执行的测试结果。

---

## 2. 总体结论

### 2.1 最终结论

**当前 `main` 分支代码不能判定为“完全满足第三周实施计划”。**

更准确的结论是：

> **第三周核心编排能力基本完成，但第三周交付标准仍存在明确缺口，尤其是节点 13 音量/淡入淡出、真实 ASR、真实 VIP 资源、贴纸实际挂载逻辑以及两级超时尚未完整落地。**

### 2.2 关键结论一览

| 模块 | 状态 | 结论 |
|---|---|---|
| 节点 6 / 关卡① | ✅ 基本满足 | 已采用“副作用移到 `interrupt()` 之后”的更稳妥方案 |
| 节点 7 35 秒护栏 | ✅ 满足核心要求 | 图级条件边重试、最大 3 次、snapshot2 均已实现 |
| 节点 8 字幕 | 🟡 部分满足 | 字幕注入结构有了，但当前默认 ASR 是 Mock，不是真实 FireRedASR2S |
| 节点 9 转场/特效 | 🟡 部分满足 | 注入逻辑有了，但模板使用 `PLACEHOLDER_*` resource_id |
| 节点 10 花字动画 | 🟡 基本满足 | 描边/投影已实现，入场动画仍为 `None` |
| 节点 11 贴纸 | 🟡 部分满足 | 时间区间可对齐，但当前使用占位 resource_id，且固定挂到第一个视频片段 |
| 节点 12 / 关卡② | ✅ 基本满足 | `interrupt/resume` 和 SQLite 联调路径已实现 |
| 节点 13 音量/淡入淡出 | ❌ 不满足 | 当前只是占位节点，不修改 `draft_content.json` |
| 原子写入 | 🟡 功能满足、结构有偏差 | 实际放在 `draft_ops/atomic_writer.py`，并未按计划放在 `jy_common/draft_writer.py`；也未严格执行计划中的 read-back 校验 |
| SQLite Checkpointer | ✅ 满足 | 默认 `sqlite`，支持按 `thread_id` 落盘 |
| 关卡①②跨进程恢复 | ✅ 代码与测试覆盖充分 | 集成测试源码覆盖基础恢复、多日恢复、幂等、thread 隔离 |
| 心跳监控 | ✅ 基本满足 | writer、PowerShell monitor、任务计划注册脚本均存在 |
| LangGraph 两级超时 | ❌ 未实际启用 | 只有配置常量，代码明确标注 Week 3 不启用 |
| 第三周完整交付验收 | ❌ 不满足 | 节点 13 等缺口直接导致完整验收无法成立 |

---

## 3. 第三周计划要求与当前代码逐项核对

## 3.1 公共基础设施

### 3.1.1 原子写入

计划要求：

- 所有节点 7～13 均通过统一原子写入函数写回 `draft_content.json`。
- 临时文件写入。
- JSON 合法性校验。
- `os.replace()` 原子覆盖。
- 异常时清理临时文件。
- 做“写入中途进程异常”测试。

当前实现：

`draft_ops/atomic_writer.py` 实现了：

- `json.dumps()` 序列化。
- `json.loads()` 前置合法性校验。
- 同目录 `tempfile.mkstemp()`。
- `flush()` + `fsync()`。
- `os.replace()`。
- 异常清理临时文件。

因此，从**写入安全机制本身**看，满足核心要求。

但存在两个差异：

1. 计划指定路径为 `jy_common/draft_writer.py`，实际代码使用 `draft_ops/atomic_writer.py`。
2. 计划示例要求“写入后再次 read-back 校验 JSON”，当前实现认为序列化本身已能保证合法 JSON，没有做文件 read-back。

**结论：🟡 功能满足，但不是严格按计划原样实现。**

参考：`draft_ops/atomic_writer.py` 的实现包含临时文件、合法性校验、`fsync()` 和 `os.replace()`。 

---

## 3.2 WorkflowState 状态扩展

计划要求新增：

```text
snapshot2_path
reorder_notified
bgm_notified
retry_counts
status_log
```

当前 `state.py` 已存在上述字段，同时还存在 `volume_adjusted` 等后续周字段。

其中：

- `snapshot2_path`：存在。
- `reorder_notified`：存在。
- `bgm_notified`：存在。
- `retry_counts`：存在。
- `status_log`：存在，并且后续增加了 append-only reducer。

**结论：✅ 满足。**

---

# 4. 节点 6～13 核对

## 4.1 节点 6 / 关卡①：人工调整分镜顺序

计划核心要求：

- `interrupt()` 挂起。
- 支持 `Command(resume=True)`。
- 避免 `interrupt()` 前的副作用重复执行。
- 使用 `thread_id` 隔离状态。

当前 `node_06_human_reorder.py` 的实现采用了比原计划代码更稳妥的方案：

```python
interrupt(_build_interrupt_payload(state))
return _post_resume(state)
```

通知在 `interrupt()` 之后执行，而不是之前执行。

这正好对应计划文档提出的替代方案：将有副作用的动作移动到 resume 后，从根源避免重放。

另外，`tests/integration/test_interrupt_resume.py` 明确测试了第一次 invoke 不调用 `_post_resume`，resume 后才调用。

**结论：✅ 满足。**

---

## 4.2 节点 7：`/jianying-speed-fit-35s`

计划要求：

- 最大时长 35 秒。
- 使用图级条件边进行重试。
- 最多重试 3 次。
- 成功后产出 snapshot2。
- 失败后升级到 `escalate_guardrail_failure`。
- 不使用节点内部 `while` 隐藏重试。

当前代码已经完整采用该设计：

```text
node_07_speed_fit
      │
      ├── 达标 → bridge_snapshot2 → node_08
      │
      ├── 未达标 → node_07_speed_fit
      │
      └── 超过重试次数 → escalate_guardrail_failure → END
```

`config.py` 中：

```python
TARGET_DURATION_US = 35_000_000
NODE_07_MAX_RETRY = 3
NODE_07_DEFAULT_FPS = 30
```

`node_07_speed_fit.py` 采用帧预算方式重新分配 segment duration，并通过 `atomic_write_draft()` 写回。

`tests/unit/test_node_07_speed_fit.py` 至少覆盖了：

- 35 秒边界。
- 60 秒压缩到 35 秒以内。
- 单段视频。
- 帧对齐计算。

**结论：✅ 核心要求满足。**

---

## 4.3 节点 8：`/jianying-add-subtitles`

计划要求：

- 从节点 7 的 snapshot2 开始处理。
- 使用带时间戳 ASR。
- 写入 `materials.texts`。
- 使用原子写入。
- 字幕时间轴和 ASR 对齐。

当前代码满足结构：

```python
snapshot2 = state.get("snapshot2_path")
src = Path(snapshot2) if snapshot2 and Path(snapshot2).exists() else draft_path
```

然后调用：

```python
call_asr2s(state.get("video_input_path", ""))
```

并写入 `materials.texts`。

问题在于当前 ASR 实现仍是：

```text
MockASRClient
```

代码明确注明：Week 3 使用 Mock，Week 4 再替换真实 FireRedASR2S。

因此：

- **节点接口和数据结构：满足。**
- **真实 ASR 集成：未满足。**

**结论：🟡 部分满足。**

---

## 4.4 节点 9：`/jianying-inject-fx`

计划要求：

- 写入 `materials.transitions`。
- 写入 `materials.video_effects`。
- 使用模板复制机制复用 VIP resource_id。
- 目标草稿可以正常渲染。

当前节点确实实现了转场和视频特效注入，也通过原子写入保存。

但是模板文件中的资源仍然是：

```text
PLACEHOLDER_TRANSITION_FADE_001
PLACEHOLDER_TRANSITION_SLIDE_001
PLACEHOLDER_VFX_ZOOM_001
```

模板本身明确标记为：

```text
_week3_status: placeholder
```

因此代码完成的是“模板注入机制”，而不是“真实 VIP 资源可渲染交付”。

**结论：🟡 部分满足。**

---

## 4.5 节点 10：`/jianying-inject-text-fx`

计划要求：

- 描边。
- 投影。
- 入场动画。

当前代码确实对 `materials.texts[*].style` 写入：

```text
outline = true
shadow = true
entrance_animation = null
```

计划第 9 节明确允许花字动画精细样式延期，因此基础描边/投影部分已经具备。

但真实入场动画参数没有实现，当前为 `None`。

**结论：🟡 基本满足，精细动画延期。**

---

## 4.6 节点 11：`/jianying-inject-tts-sticker`

计划要求：

- 读取字幕。
- 根据字幕解析 `resource_id`。
- 写入 `materials.stickers`。
- 贴纸时间范围与字幕 `target_timerange` 偏差 ≤ 1 帧。

当前代码确实复制字幕的 `target_timerange`：

```python
"target_timerange": dict(text_obj.get("target_timerange", {}))
```

因此时间范围复制本身没有偏移。

但存在两个明显问题：

### 问题 1：resource_id 仍是占位值

例如：

```text
PLACEHOLDER_STICKER_THUMBS_UP
PLACEHOLDER_STICKER_FIGHT
PLACEHOLDER_STICKER_SURPRISE
```

### 问题 2：没有挂到真正匹配的 segment

当前代码：

```python
if segments:
    segments[0].setdefault("extra_material_refs", []).append(sticker_id)
```

也就是说，所有贴纸都追加到了**第一个视频 segment**。

而计划要求的是贴纸与对应字幕/视频片段关联。

代码自身也注明 Week 4 才会按 timerange 找最匹配 segment。

因此不能认为节点 11 已达到完整验收标准。

**结论：🟡 部分满足。**

---

## 4.7 节点 12 / 关卡②：人工添加 BGM

当前节点实现：

```python
interrupt(_build_interrupt_payload(state))
return _post_resume(state)
```

并支持：

```text
checkpoint = "②"
step = 12
Command(resume=True)
```

整体结构与节点 6 一致。

`tests/integration/test_interrupt_resume.py` 也验证了：

- 节点 12 能被第二次 interrupt。
- 第二次 resume 后继续节点 13。
- 上游节点不重复执行。

**结论：✅ 满足。**

---

## 4.8 节点 13：`/jianying-adjust-volume`

这是当前最大缺口。

计划要求明确指出：

- 本周完成新 Skill。
- 进行 `draft_content.json` 字段逆向。
- 支持音量。
- 支持淡入。
- 支持淡出。
- 写入 `materials.audio_fades`。
- 形成可交付的完整剪映项目。

但当前实际代码：

```python
def adjust_volume(state: WorkflowState) -> dict:
    log = list(state.get("status_log", []) or []) + [
        "node_13_adjust_volume_placeholder_pass"
    ]
    return {
        **state,
        "status_log": log,
        "volume_adjusted": False,
    }
```

它没有：

- 修改 `draft_content.json`。
- 写入 `materials.audio_fades`。
- 修改音量。
- 写入淡入/淡出。
- 调用 `atomic_write_draft()`。

因此这不是“实现不完整”，而是**功能尚未实现**。

**结论：❌ 不满足。**

这项缺口会直接导致第三周最终交付标准中的：

> “音量/淡入淡出：`materials.audio_fades` 按预期生效”

无法成立。

---

# 5. LangGraph 图拓扑核对

当前 `graph.py` 已建立：

```text
START
  ↓
1 → 2 → 3 → 4 → 5
              ↓
        6 interrupt①
              ↓
        7 speed_fit
          ↙       ↘
       retry      8
                   ↓
                   9
                   ↓
                  10
                   ↓
                  11
                   ↓
            12 interrupt②
                   ↓
                  13
```

计划中的第三周顺序：

```text
6 → 7 → 8 → 9 → 10 → 11 → 12 → 13
```

当前拓扑与此一致。

另外，仓库当前已经包含第 4、5 周节点，因此 `graph.py` 会继续连到节点 14～17；这属于后续工作，不应反向计算为第三周缺失。

**结论：✅ 第三周主线拓扑满足。**

---

# 6. SQLite Checkpointer / interrupt-resume 核对

这是当前实现完成度较高的部分。

`config.py` 默认：

```python
CHECKPOINTER_BACKEND = "sqlite"
```

SQLite 文件按：

```text
checkpoints/<thread_id>.sqlite
```

落盘。

`tests/integration/test_interrupt_resume.py` 当前明确覆盖：

### TC1：基础 interrupt / resume

节点 6 挂起 → resume → 节点 12 挂起 → resume → 完成。

同时验证：

```text
node_05_generate_draft_done == 1
checkpoint1_resumed == 1
checkpoint2_resumed == 1
```

### TC2：跨进程 / 多日恢复

通过：

```text
g1 + sqlite 文件
↓
销毁 g1
↓
重新 build g2
↓
同 thread_id + 同 sqlite 文件
↓
resume
```

验证 checkpoint 可以继续。

### TC3：副作用幂等

验证 `interrupt()` 前不会调用 `_post_resume()`。

### TC4：thread_id 隔离

同时运行两个 thread，乱序 resume，验证草稿与状态不串线。

**结论：✅ 满足计划中的四类联调要求。**

---

# 7. 心跳监控核对

第三周计划要求：

```text
编排进程内心跳线程
        ↓
heartbeat.txt
        ↓
PowerShell 外部监控
        ↓
Windows Task Scheduler
```

当前仓库已经具备：

```text
monitoring/heartbeat_writer.py
monitoring/heartbeat_monitor.ps1
monitoring/register_heartbeat_task.ps1
```

心跳写入器：

```text
间隔 = 10 秒
文件 = C:\ProgramData\VideoWorkflow\heartbeat.txt
```

外部监控：

```text
检查间隔 = 2 分钟
超时阈值 = 120 秒
```

超时目前只写本地日志，不接企业微信/邮件。这一点与第三周计划一致，因为计划允许告警渠道顺延。

Windows 任务注册脚本也已经存在。

**结论：✅ 代码与部署脚本基本满足。**

但由于当前验证环境没有 Windows 任务计划程序实际执行，因此：

> “kill 编排进程后 120 秒触发告警”的运行时结果，本报告不做已实测结论。

---

# 8. LangGraph 两级超时

计划要求：

1. 总执行时间超时。
2. 单节点 / 单工具调用无响应超时。

当前 `config.py` 只有：

```python
TOTAL_EXECUTION_TIMEOUT_S = 600
NODE_INACTIVITY_TIMEOUT_S = 120
```

但代码明确注明：

```text
Week 3 不启用
Week 4 接入前核实 API
```

也就是说：

> **存在配置占位，不存在已经接入 LangGraph 执行链的真实超时控制。**

**结论：❌ 第三周按“已实现功能”标准不满足。**

---

# 9. 测试现状与“README 通过”问题

README 当前声称：

```text
单元测试 60 条
集成测试 24 条
总计 84 条全部通过
```

同时给出了 `test_interrupt_resume.py` 的测试入口和 SQLite / interrupt-resume 验证说明。

但是，这不能直接证明本报告中的“第三周最终验收标准”全部通过，原因是：

1. 当前节点 13 明确是 placeholder。
2. 节点 8 默认是 Mock ASR。
3. 节点 9/11 使用 placeholder resource_id。
4. 节点 11 当前把贴纸挂到第一个 video segment。
5. 两级超时只有配置常量，没有启用。

因此：

> README 中的“84 条测试通过”更准确地理解为“当前仓库已有自动化测试通过”，而不是“第三周文档中的所有真实业务验收项均已完成”。

---

# 10. 与第三周最终验收标准的直接对照

计划第 7 节要求：

| 验收项 | 计划要求 | 当前代码 | 判定 |
|---|---|---|---|
| 剪映项目完整性 | `draft_content.json` 可在剪映打开 | 代码生成链存在，但当前静态验证未启动剪映 | 🟡 |
| 字幕轨道 | `materials.texts` 非空且与 ASR 对齐 | 有注入，但默认是 Mock ASR | 🟡 |
| 转场/特效 | 可正常渲染 | 有注入，但 resource_id 为 placeholder | 🟡 |
| 花字动画 | 描边/投影/入场动画 | 描边/投影有，入场动画为 `None` | 🟡 |
| 贴纸 | resource_id + 正确时间轴绑定 | 时间区间复制正确，但 resource_id 和 segment 绑定不完整 | 🟡 |
| BGM | 人工通过关卡②添加 | interrupt/resume 机制存在；真实客户端未实测 | 🟡 |
| 音量/淡入淡出 | `materials.audio_fades` 生效 | 完全未实现 | ❌ |
| 总时长 | ≤35 秒 | 节点 7 已实现 | ✅ |
| 写入安全 | 原子写入 + 中断测试 | 原子写入已实现；功能测试源码存在 | ✅ |
| 关卡①② | 测试 1～4 全通过 | 有对应集成测试源码 | ✅/运行结果未重新执行 |
| 心跳 | 注册任务并 kill 进程验证 | 脚本齐全；运行时 kill 未重新实测 | 🟡 |

---

# 11. 必须修复的缺口

按阻塞程度排序：

## P0：节点 13 必须实现

需要真正完成：

```text
jianying_adjust_volume()
    ├── 查找音轨
    ├── 修改 volume
    ├── 写 materials.audio_fades
    ├── 支持 fade-in
    ├── 支持 fade-out
    └── atomic_write_draft()
```

并补充至少：

- 主音轨 1.0。
- BGM 0.35。
- BGM fade-in 2 秒。
- BGM fade-out 3 秒。
- `draft_content.json` 字段逆向测试。

## P0：确认真实剪映资源

替换：

```text
PLACEHOLDER_TRANSITION_*
PLACEHOLDER_VFX_*
PLACEHOLDER_STICKER_*
```

为真实、经过剪映客户端验证的 `resource_id`。

## P1：节点 11 按时间范围查找实际视频 segment

当前：

```python
segments[0].setdefault("extra_material_refs", []).append(sticker_id)
```

应改为：

```text
字幕 target_timerange
        ↓
查找时间重叠 segment
        ↓
挂载 sticker_id
```

## P1：替换真实 ASR

当前默认：

```text
MockASRClient
```

第三周要求的实际视频验收应切换到真实 `FireRedASR2S` 客户端。

## P1：真正启用两级超时

不仅配置：

```python
TOTAL_EXECUTION_TIMEOUT_S
NODE_INACTIVITY_TIMEOUT_S
```

还需要将其接入实际 LangGraph 调度 / 节点执行机制，并补充超时测试。

## P2：统一原子写入模块位置

如果需要严格按计划文档验收，则把：

```text
jy_common / draft_writer.py
```

与当前：

```text
draft_ops / atomic_writer.py
```

进行统一。

从工程角度看，这不是功能阻塞问题；属于“实现结构与计划文档不一致”。

---

# 12. 推荐验收标准

第三周重新验收时，至少需要全部满足下面 6 条：

```text
[1] 节点 6 → 13 全链路可运行
[2] 关卡① / ② 可跨进程 resume
[3] 节点 7 在真实 draft 上稳定压到 ≤35s
[4] 节点 8 / 9 / 10 / 11 写入真实、可渲染数据
[5] 节点 13 真正产生 audio_fades / volume
[6] kill 编排进程后心跳监控能产生超时告警
```

其中 `[5] 节点 13` 是当前最明确的硬阻塞项。

---

# 13. 最终判定

## 判定结果：❌ 当前版本不满足“第三周实施计划全部完成”的标准

但可以进一步拆分为：

```text
LangGraph 编排骨架       ✅
SQLite 持久化            ✅
interrupt / resume       ✅
节点 7 护栏              ✅
心跳监控                  ✅

真实 ASR                 🟡
真实 VIP resource_id      🟡
节点 11 精确 segment 绑定 🟡
节点 10 入场动画          🟡
两级超时                  ❌
节点 13 音量/淡入淡出      ❌
```

因此，当前仓库更适合定义为：

> **“第三周工作流编排骨架 + 大部分节点逻辑已经落地，但第三周最终业务交付仍未闭环。”**

---

# 14. 主要代码证据

- 第三周计划：
  `docs/AI视频剪辑自动化工作流_第三周详细实施计划.md`
- 图装配：
  `graph.py`
- 状态：
  `state.py`
- 节点 6：
  `nodes/node_06_human_reorder.py`
- 节点 7：
  `nodes/node_07_speed_fit.py`
- 节点 8：
  `nodes/node_08_add_subtitles.py`
- 节点 9：
  `nodes/node_09_inject_fx.py`
- 节点 10：
  `nodes/node_10_inject_text_fx.py`
- 节点 11：
  `nodes/node_11_inject_sticker.py`
- 节点 12：
  `nodes/node_12_human_add_bgm.py`
- 节点 13：
  `nodes/node_13_adjust_volume.py`
- 原子写入：
  `draft_ops/atomic_writer.py`
- 心跳：
  `monitoring/heartbeat_writer.py`
  `monitoring/heartbeat_monitor.ps1`
  `monitoring/register_heartbeat_task.ps1`
- interrupt/resume 集成测试：
  `tests/integration/test_interrupt_resume.py`
- 节点 7 单元测试：
  `tests/unit/test_node_07_speed_fit.py`

---

# 15. 参考仓库

- GitHub 仓库：
  `https://github.com/wayyet/auto-video-editor`
- 第三周实施计划：
  `https://github.com/wayyet/auto-video-editor/blob/main/docs/AI%E8%A7%86%E9%A2%91%E5%89%AA%E8%BE%91%E8%87%AA%E5%8A%A8%E5%8C%96%E5%B7%A5%E4%BD%9C%E6%B5%81_%E7%AC%AC%E4%B8%89%E5%91%A8%E8%AF%A6%E7%BB%86%E5%AE%9E%E6%96%BD%E8%AE%A1%E5%88%92.md`


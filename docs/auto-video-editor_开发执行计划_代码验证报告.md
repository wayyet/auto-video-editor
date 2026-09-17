# auto-video-editor 代码验证报告

- **验证对象**：https://github.com/wayyet/auto-video-editor （main 分支）
- **对照文档**：《AI视频剪辑自动化工作流开发执行计划.md》（本次对话附件；已与仓库 `docs/AI视频剪辑自动化工作流开发执行计划.md` 逐段 diff，除本报告未展开的 Mermaid 图、14.3/14.4/14.6-14.7 章节外，其余章节逐字一致——即仓库使用的正是这份主计划）
- **入口参考**：`docs/第5周_全流程联调与生产上线实施计划.md`
- **验证日期**：2026-09-17

---

## 一、验证方法与局限

**做了什么**：

1. 克隆完整仓库到隔离环境，直接阅读源代码（不只读文档）。
2. 对 `graph.py`、`state.py`、`config.py`、`resume_utils.py`、`draft_ops/` 三件套、`monitoring/` 三件套、`node_13`、`node_17`、`node_fork_english_branch`、`node_join_before_delivery`、`jy_common/draft_writer.py`、`mcp_clients/openstoryline_client.py` 做了逐行代码审查。
3. 对其余 12 个节点（01/02/04/05/06/08/09/10/11/12/14/15），核实了文件存在、函数签名、以及在 `graph.py` 中的接线位置，未逐行审查内部实现——这部分结论的确信度低于上一条。
4. 实际运行了测试套件：171 个单元测试 + 37 个集成测试。
5. 针对一处存疑的 LangGraph 机制（两条不同长度的分支汇合到同一节点时的触发次数），单独写了最小复现脚本实测，而非凭经验判断（见 5.1）。

**局限**：

- 验证环境是 Linux 沙箱，没有真实 Windows、剪映客户端、Edge 浏览器、FireRed 系列服务、Postgres 数据库。
- 仓库 `pyproject.toml`/`requirements.txt` 指向开发者本机私有路径的 LangGraph 可编辑安装（注释标注版本 `1.2.9`），本次改用 PyPI 公开版 `1.2.11` 替代做测试——两者核心图语义预期一致，但并非同一份代码。
- 未对接真实第三方服务，所有"是否真的调得通 OpenStoryline/FireRed/剪映客户端"的问题都无法在本次验证中给出结论，只能确认代码里有没有对应的调用点。

---

## 二、总体结论

代码库整体成熟度较高。测试实跑 **202/208 通过（97%）**，失败的 3 条集成测试都能追溯到"验证环境没有真实 Windows/Edge"这一环境原因，不是代码逻辑错误（细节见六）。

拆开看，这个仓库其实是两层：

- **编排层**（LangGraph 图结构、状态管理、中断恢复、原子写入、心跳监控）：实现扎实，多处直接对应主文档 5.2 节列出的具体缓解方案，并有实测代码验证，质量好于预期。
- **集成层**（真实剪映 GUI 自动化、真实 pyJianYingDraft 库、真实 FireRed 模型服务、真实 MCP 协议）：目前以接口骨架 + Mock 为主，真实对接大多明确标注"待接入"。这是代码里写清楚的已知状态，不是被掩盖的缺口。

发现 **1 处需要修复的真实 bug**（汇合节点重复触发风险，见 5.1），其余是清晰可归类的"占位/待验证"项。

---

## 三、核心架构逐项对照（对照主文档第 2 节）

| 主文档要求 | 代码现状 | 结论 |
|---|---|---|
| 宏观编排引擎：LangGraph | `graph.py` 用 `StateGraph` 装配 17 节点，`checkpointer` 支持 memory/sqlite/postgres 三种后端 | ✅ |
| 剪映草稿操作：pyJianYingDraft | 全仓库搜索不到该库的实际 `import`；改为自建 `draft_ops/atomic_writer.py` 直接读写 JSON。`pyJianYingDraft` 仅在 3 处注释里作为"计划中方案"提及，`mcp_clients/jianying_cover_client.py` 明确写"真实 uiautomation+pyJianYingDraft 路径留待接入" | ⚠️ 未用指定库，但"直接读写 draft_content.json"这个**功能目标**已达成 |
| 前端预览：独立 Edge 窗口 | `node_03_open_preview.py`：人工场景用 `subprocess.Popen(["cmd","/c","start","","msedge",url])`；无人值守场景走 Playwright `channel="msedge"` | ✅ 逻辑对齐主文档变更①（测试 mock 方式有个问题，见 5.3） |
| GUI 自动化：uiautomation | 全仓库（含依赖声明）零处引用 | ❌ 未接入 |
| 运行环境：纯 Windows | 代码里多处 Windows 专属路径（`cmd`、`msedge`、`C:\ProgramData\...`），在 Linux 下实测确认跑不了 | ✅ 定位与主文档变更③一致 |
| AI 模型底座：FireRed 系列 | `mcp_clients/` 下是接口骨架 + Mock，注释标注"真实协议方法名待核实" | ⚠️ 骨架完成，真实对接未验证 |
| 状态持久化：开发 Sqlite / 生产 Postgres | `config.py::make_checkpointer` + `graph.py::get_checkpointer()` 均已实现，含 `AsyncPostgresSaver.setup()` 调用 | ✅ |
| 长期记忆：LangGraph Store | 全仓库零处引用 | ❌ 未实现 |
| 互操作协议：MCP | `mcp_clients/` 下是普通 `urllib.request` HTTP 骨架 + Mock，未见 MCP 协议/SDK 的实际调用 | ⚠️ 命名沿用"MCP"，实为待接入的 HTTP 骨架 |

---

## 四、17 步工作流逐项核对（对照主文档第 3 节）

| 步骤 | 名称 | 主文档要求 | 代码现状 | 结论 |
|---|---|---|---|---|
| 1 | 清理旧缓存 | `/kuaishou-clean-cache` | `node_01_clean_cache.py` 存在；清理路径在 `config.py` 标注 `[TODO: Week1]` 待核实真实安装位置 | ⚠️ 逻辑完成，路径未核实 |
| 2 | 启动 OpenStoryline | `/openstoryline-launcher` | `node_02_launch_openstoryline.py` 存在；启动命令/端口在 `config.py` 标注占位值 | ⚠️ 逻辑完成，参数未核实 |
| 3 | 打开前端预览 | 编排器直接动作，无独立 Skill | `node_03_open_preview.py`，人工/无人值守两分支都有 | ✅（测试 mock 问题见 5.3） |
| 4 | 导入视频输出分镜 | OpenStoryline 内置 VLM 规划 | `node_04_import_and_plan.py`；真实 client 是接口骨架 + Mock（见三） | ⚠️ 骨架完成，真实对接未验证 |
| 5 | 生成初始草稿 | `/openstoryline-to-jianying` | `node_05_generate_draft.py`（`graph.py` 内 `generate_draft_wrapped` 包装）；用自建 JSON 写入而非 pyJianYingDraft | ✅ 功能目标达成 |
| 6 | 人工调整分镜 | `interrupt()` 关卡① | `node_06_human_reorder.py` | ✅ |
| 7 | 变速适配 ≤35 秒 | 护栏+重试+分叉点 | `node_07_speed_fit.py` + `route_after_speed_fit` + `escalate_guardrail_failure`（超限终止）；`bridge_snapshot2` 产出快照② | ✅ |
| 8 | 添加字幕 | `/jianying-add-subtitles` | `node_08_add_subtitles.py`；据 `state.py` 注释与测试夹具，同步写 `asr_segments_zh` 供步骤 16 直接读取 | ✅ |
| 9 | 注入转场特效 | `/jianying-inject-fx` | `node_09_inject_fx.py` + `templates/fx_template.json` | ⚠️ 逻辑完成，转场/特效 resource_id 是占位符（`PLACEHOLDER_TRANSITION_FADE_001` 等） |
| 10 | 字幕花字动画 | `/jianying-inject-text-fx` | `node_10_inject_text_fx.py` 存在，已接线 | ✅ 存在且接线正确（未逐行核对具体样式参数，见一.3） |
| 11 | 字幕贴纸关联 | `/jianying-inject-tts-sticker` | `node_11_inject_sticker.py` + `jy_common/sticker_resolver.py` | ⚠️ 逻辑完成，贴纸 resource_id 是占位符 |
| 12 | 人工添加 BGM | `interrupt()` 关卡② | `node_12_human_add_bgm.py` | ✅ |
| 13 | 调整音量/淡入淡出 | `/jianying-adjust-volume`（主文档标记的原方案缺口） | `node_13_adjust_volume.py`：主音轨+BGM 各一次，逻辑完整，含降级处理 | ⚠️ 缺口已补齐，但写入的字段名（`fade_in_duration_us` 等）是设计占位符，待人工用真实剪映客户端逆向确认 |
| 14 | 制作多维度封面 | `/jianying-make-cover` | `node_14_make_covers.py` 存在，已接线 | ✅ 存在且接线正确（真实 uiautomation 导出待接入，见三） |
| 15 | 封面英文本地化 | `/jianying-cover-localize-en` | `node_15_localize_covers_en.py` 存在，已接线 | ✅ 存在且接线正确（FireRed-Image-Edit 真实对接未验证） |
| 16 | 字幕翻译为英文 | `/jianying-translate-subtitles`，混合模式 | 拆成两个节点：`node_16a_translate_and_check.py`（翻译+检测，无中断）+ `node_checkpoint3_layout_review.py`（只做 interrupt） | ✅ 正确实现了"翻译 API 不能和 interrupt 写在同一节点"这条关键设计 |
| 17 | 注入英文 AI 配音 | FireRedTTS2 合成 + 动态变速补偿 | `node_17_inject_english_tts_stub.py`：只写一段静音 wav 占位（>5KB），代码注释明确写"用户决策(2026-09-09)：Week5 继续 stub" | ❌ 仍是占位（团队已决策推迟到 Week 6+，不是疏漏） |

---

## 五、关键发现

### 5.1〔需要修复〕汇合节点存在被重复触发的风险

`graph.py` 里，中文主线和英文分支汇合到 `join_before_delivery` 节点，用了**两次独立**的 `add_edge()`：

```python
g.add_edge("node_15_localize_covers_en", "join_before_delivery")   # 第289行
...
g.add_edge("node_17_inject_english_tts_stub", "join_before_delivery")  # 第303行
```

`graph.py` 顶部注释认为这样就能"自动 fan-in、等两分支都到达才触发"。**这个假设是错的**——我写了个最小复现脚本，用真实 LangGraph 1.2.11 实测：两条不同长度的分支，各自用独立 `add_edge` 指向同一个 join 节点时，join **被调用了两次**：第一次是短分支先到达时触发（此时长分支的数据还没写进 state），第二次才是长分支到达后触发。

正确写法是官方文档推荐的"列表语法"，同样实测确认只触发一次：

```python
g.add_edge(["node_15_localize_covers_en", "node_17_inject_english_tts_stub"], "join_before_delivery")
```

值得一提的是，**第5周计划文档自己就点名了这个坑**（"`join_and_deliver`只被调用一次〔打日志确认,不是两次——两条分支各触发一次是常见bug〕"），文档里的示例代码用的正是列表写法。代码没有照文档的写法实现，等于踩了文档提前警示过的坑。

现有的集成测试 `test_week4_zh_branch_no_rerun_after_en_resume` 断言 `log.count("join_before_delivery_done") == 1` 且能通过，原因是 `status_log` 字段带了去重 reducer（`_append_unique`），会把两次重复的字符串合并成一条——**测试断言通过，不代表函数只被调用了一次**，这条测试没能测出这个问题。

**建议修复**：把两处独立 `add_edge` 合并成一次列表写法，如上所示。

### 5.2 依赖版本：文档写 1.2.11，代码实际锁定 1.2.9

- 第5周计划文档：`"本计划基于以下版本实测验证：langgraph==1.2.11、langgraph-checkpoint-sqlite==3.1.1、langgraph-checkpoint-postgres==3.1.2、psycopg==3.3.5"`
- 仓库 `requirements.txt` 注释：`langgraph==1.2.9`（可编辑安装，来自开发者本机路径）
- `tests/integration/test_week4_graph.py` 第292行注释：`"跳过以避免LangGraph 1.2.9 sync invoke不抛GraphInterrupt的限制"`——这不只是版本号写错，1.2.9 有个具体的行为限制导致一条计划中的测试场景被跳过。

建议团队确认实际在用哪个版本，统一更新文档和依赖文件。

### 5.3 节点3的测试 mock 没有真正生效

`node_03_open_preview.py` 的函数签名是：

```python
def open_preview(state, unattended=False, *, popen_factory=subprocess.Popen, ...):
```

`popen_factory=subprocess.Popen` 这个默认值在函数**定义时**就绑定好了，指向真实的 `subprocess.Popen`。而集成测试用的是 `monkeypatch.setattr(mod3, "subprocess", 假对象)`——这只是替换了模块里 `subprocess` 这个名字，不会影响已经绑定好的默认参数。我直接验证过：patch 之后，`popen_factory` 的默认值还是原来的真实 `subprocess.Popen`，不是假对象。

这也正是我这次验证里 3 条集成测试失败的根因：在没有 `cmd.exe`（Windows 命令行）的 Linux 环境下，这几个测试实际跑的是真的 `subprocess.Popen(["cmd","/c","start","","msedge",url])`，自然会失败。在开发者的真实 Windows 环境下，因为 `cmd.exe` 确实存在，这条命令能跑通、测试能"通过"——但通过的原因是"意外能跑通"，不是"mock 生效了"。换句话说，这几条测试在设计上没有真正隔离外部依赖。

**建议修复**：把 `popen_factory` 改成在函数体内部读 `subprocess.Popen`（而不是作为默认参数），或者调用处显式传入 mock。

### 5.4 Week1 环境前置条件疑似尚未核实

`README.md` 里"Week1 交付物前置依赖"这几项显示为未勾选状态：

- [ ] 剪映 v5.9.0 已安装
- [ ] hosts 文件已屏蔽剪映升级域
- [ ] OpenStoryline 实际启动命令/端口/缓存路径已核实

结合 `config.py` 里多处 `[TODO: Week1]` 标记（缓存路径、OpenStoryline 命令与端口都是占位值），这几项主文档第12节"第1周"的前置任务，看起来还没有在真实环境里逐一核实过。这不是代码问题，是环境准备的状态问题，建议开工前先确认。

### 5.5 VIP 资源 ID 与音量字段名均为占位符

- `templates/fx_template.json`、`jy_common/asset_resource_map.json` 都显式标注 `"_reverse_engineering_pending": true`，resource_id 是 `PLACEHOLDER_TRANSITION_FADE_001` 这类占位值。
- `node_13_adjust_volume.py` 里 `_PLACEHOLDER_FADE_IN_KEY` 等字段名同样待剪映客户端逆向确认。

两处都在代码里写清楚了具体的逆向步骤（备份草稿→客户端手工操作→diff→回填），不是隐藏缺口，但意味着**步骤9/11/13 现在写进 draft_content.json 的内容，剪映客户端未必认得**，真正联调前必须先做这几步逆向。

### 5.6 两级超时机制已实现但默认关闭

`monitoring/timeout_watchdog.py` 完整实现了主文档5.2节要求的"总时长超时+单节点无响应超时"，但 `config.ENABLE_TWO_LEVEL_TIMEOUT` 默认是 `False`（为了不影响现有测试）。也就是说，这项风险缓解措施代码层面存在，但**默认配置下没有生效**，需要显式设置环境变量才会启用。

---

## 六、测试实跑结果

| 测试套件 | 用例数 | 通过 | 失败 | 跳过 | 说明 |
|---|---|---|---|---|---|
| 单元测试 `tests/unit/` | 173 | 171 | 0 | 2 | 跳过原因：Linux 下 PIL 默认字体行为与 Windows 不同，测不出 `missing_font` 场景（与代码逻辑无关） |
| 集成测试 `tests/integration/` | 37 | 31 | 3 | 3 | 失败3条见5.3（Windows/Edge 环境缺失，非代码bug）；跳过3条为需要真实 Postgres 连接 |
| **合计** | **210** | **202** | **3** | **5** | 通过率 97% |

---

## 七、后续建议

1. **【优先】** 修复 5.1：`graph.py` 里两处指向 `join_before_delivery` 的 `add_edge` 合并成列表写法。
2. 确认 5.2：团队实际在用 LangGraph 1.2.9 还是 1.2.11，统一文档与依赖声明。
3. 修复 5.3：`node_03_open_preview.py` 的 `popen_factory` 改为运行时读取，让测试 mock 真正生效。
4. 按 5.4 清单，开工前逐项核实 Week1 环境前提（剪映安装、hosts 屏蔽、OpenStoryline 真实命令/端口）。
5. 按 5.5 的既定步骤，用真实剪映客户端完成音量字段名与 VIP 资源 ID 的逆向回填。
6. 团队确认是否需要接入真实 `uiautomation`/`pyJianYingDraft`/MCP 协议，或维持当前自建替代方案——两者都能达成主文档的功能目标，是否要换成"指定库"纯粹是选型层面的决定，需要团队拍板。

---

*本报告由 AI 辅助生成：结论基于实际克隆的仓库代码、真实运行的测试结果，以及针对性的最小复现脚本实测；未直接核实的部分已在一.3 与各表格备注中明确标出。*

# 阶段 E PoC 评估报告:JianYing MCP Server 与 jianying-editor-skill

> 评估日期:2026-08-31
> 评估人:Mavis(自动化生成,基于公开信息)
> 评估范围:对照《第 2 周分阶段实施计划》第 5 节(阶段 E)5 项待核实点

## 1. 评估方法与信息源

### 1.1 本地仓库情况

| 目标 | 本地是否可用 |
|---|---|
| JianYing MCP Server 源码 | ❌ **本地无此仓库** |
| jianying-editor-skill 源码 | ❌ **本地无此仓库** |
| 实际 `capcut` CLI 工具 | ❌ **本机未安装** |

**影响**:本报告**未做任何源码走查或运行时实测**。所有结论基于公开 README 摘要、第三方组件评估文档中已识别的表述,以及 pyJianYingDraft 已知 API 推断。**置信度低**,建议 Week 3+ 提供仓库与 `capcut` CLI 后重测。

### 1.2 已知文档来源

| 文档 | 用途 |
|---|---|
| 《AI 视频剪辑自动化工作流第三方组件评估整合总结.md》(附件) | 14.5 节 5 项 PoC 待办 |
| 《AI 视频剪辑自动化工作流第三方视频 Skills 生态评估.md》(附件) | 4.5 节"jianying-editor-skill 采纳前待核实清单" |
| `E:\Documents\kuaishou\langgraph-main` 仓库 | 仅用于确认本项目自身无冲突;非 PoC 目标 |

## 2. 5 项待核实点逐条结论

### 2.1 JianYing MCP Server:Python 3.13+ 兼容性

| 项 | 评估 |
|---|---|
| 信息源 | 仅有"待核实"标记,无 README/pyproject 实际证据 |
| 本地实测 | ❌ 未做 |
| **结论** | **无法判定**。langgraph 1.2.9 自身 pyproject 明确支持 Python 3.10-3.13,但 JianYing MCP Server 的兼容性需待用户提供仓库后实测。 |
| 行动 | Week 3+ 拿到仓库后第一件事:`pip install -e .` 在 Python 3.13 虚拟环境跑通 import + 自带示例/测试 |

### 2.2 JianYing MCP Server:原子写入包装

| 项 | 评估 |
|---|---|
| 信息源 | "待核实"标记 |
| 本地走查 | ❌ 未做 |
| **结论** | **无法判定**。需走查其写 `draft_content.json` 的源码路径,对照本计划 `draft_ops/atomic_writer.py` 的"临时文件 → fsync → os.replace"三步法是否齐全。 |
| 行动 | 若三步齐全 → 已具备,无需自行外层包装;若缺任一步 → PoC 报告中标注需团队自行加包装后再采纳。 |

### 2.3 JianYing MCP Server:幂等设计

| 项 | 评估 |
|---|---|
| 信息源 | "待核实"标记;参照《执行计划》第 5.2 节"坑③缓解方案"(检查目标是否已存在/已发送再执行,或挪到 resume 后执行) |
| 本地走查 | ❌ 未做 |
| **结论** | **无法判定**。需对照其 MCP 工具调用是否符合"检查已存在再写入"模式。 |
| 行动 | 若符合 → 可直接用于 `interrupt()` 前置逻辑;若不符合 → 标注需团队自行加一层幂等包装。 |

### 2.4 jianying-editor-skill:许可证条款

| 项 | 评估 |
|---|---|
| 信息源 | "待核实"标记 |
| 本地查证 | ❌ 未做(无仓库) |
| **结论** | **无法判定**。需查阅仓库 `LICENSE` 文件,核实是否允许内部/商用。 |
| 行动 | 按公司法务口径判断;**PoC 报告仅陈述条款内容,不下法律结论**。 |

### 2.5 jianying-editor-skill:"5.9/V6"版本表述边界

| 项 | 评估 |
|---|---|
| 信息源 | 《第三方视频 Skills 生态评估》4.2 节已识别表述出入 |
| 本地求证 | ❌ 未做(无仓库,无法向维护者提问) |
| **结论** | **存在已知歧义**。该 skill 在 README 中可能既提到"5.9"又提到"V6",但**未明确说明两者的适用场景差异**——是与剪映 6.0+ 加密机制兼容?还是只支持 5.9? |
| 行动 | Week 3+ 拿到仓库后,向维护者直接提问确认,或在 PoC 环境(v5.9.0 与 v6.0+)各实测一次。 |

## 3. 与阶段 B 自研模块的能力对比

| 能力维度 | 阶段 B 自研(`draft_ops/`) | JianYing MCP Server | jianying-editor-skill |
|---|---|---|---|
| 加密检测 | ✅ `DraftStatus` 枚举 + `detect_draft_encryption`(明文 JSON 解析 + capcut 二次确认) | ❓ 未核实 | ❓ 未核实 |
| 版本策略 | ✅ `VersionStrategy` 枚举 + `resolve_strategy`(v5.9.0 → A,其他 → B) | ❓ 未核实 | ❓ 未核实 |
| 原子写入 | ✅ `atomic_write_draft`(tempfile + fsync + os.replace) | ❓ 待走查是否含三步法 | ❓ 未核实 |
| 依赖 | 仅 stdlib + capcut CLI(可选注入) | 可能引入新依赖 | 可能引入新依赖 |
| 已知风险 | Week 2 自研,无运行时验证 | 未实测 | 未实测 |

## 4. 采纳/不采纳建议

### 4.1 Week 2 阶段 E 结论

> **Week 2 不采纳任一第三方组件**。

**理由**:
1. 本地无仓库,无法在 Week 2 时间窗内完成 5 项待核实点的实测验证;
2. 阶段 B 自研模块已通过 10/10 单测,具备满足 Week 2 交付的能力;
3. 强行"基于公开信息采纳"会引入无法量化的兼容性/许可证风险。

### 4.2 Week 3+ 重测建议

| 触发条件 | 行动 |
|---|---|
| 用户提供 JianYing MCP Server 仓库 + `capcut` CLI | 重跑 §2.1-2.3 三项,出独立 PoC 报告 |
| 用户提供 jianying-editor-skill 仓库 | 重跑 §2.4-2.5 两项,核许可证 + 版本表述 |
| 三项及以上"具备" → 评估是否替换阶段 B 自研 | 比对:维护活跃度 / 测试覆盖 / 性能 |
| 比对结果"第三方明显优于自研" → 采纳 | 在 graph.py 节点 5 内替换调用为第三方,自研模块降级为"防御性备份" |

## 5. 若采纳对调用堆栈图的影响范围(占位)

按附件第 10 节调用堆栈图,替换影响:

| 步骤 | 当前实现 | 若采纳 MCP Server | 若采纳 jianying-editor-skill |
|---|---|---|---|
| 步骤 5(openstoryline-to-jianying) | 节点 5 调 `draft_ops.atomic_write_draft` | 替换为调 MCP Server `write_draft` 工具 | skill 通常作为 Python 函数直接 import,不经过 MCP |
| 步骤 8-11(后续周次) | 待 Week 3+ 设计 | 若 MCP Server 覆盖,则全部走 MCP | 视 skill 暴露的 API 而定 |

**Week 2 结论**:本项目 `draft_ops/` 模块**继续保留**,作为节点 5 内部唯一写入路径,直至 Week 3+ 重测后给出明确替换方案。

## 6. 风险登记

| 风险 | 缓解 |
|---|---|
| 本报告基于公开信息,**未实测**,采纳建议置信度低 | 报告显式标注信息源;Week 3+ 提供仓库后重测 |
| Week 1 交付物(v5.9.0 安装 + hosts 屏蔽升级域)未在本项目内验证 | 阶段 D 自动化测试只校验 JSON 合法性,**剪映打开校验是手动步骤** |
| `capcut decrypt` 返回码语义未实测,默认 `returncode != 0 → ENCRYPTED` | `detect_draft_encryption` 接受 `decrypt_runner` 注入参数,实测后可换默认实现 |

---

*本报告由 Mavis 基于本地信息自动生成,未做任何第三方组件的源码走查或运行时实测。所有"未核实"项需 Week 3+ 提供仓库与 `capcut` CLI 后重测。*

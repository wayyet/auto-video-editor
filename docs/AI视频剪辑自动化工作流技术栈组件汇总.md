# AI视频剪辑自动化工作流技术栈组件汇总

本文档汇总五份参考文档中提及的全部技术栈、工具组件与协议标准，覆盖工作流编排、剪映草稿操作、多模态 AI 能力、前端调试、本地工具库、存储持久化等全链路环节。

## 一、工作流编排与 AI Agent 框架

### （一）核心推荐编排方案

#### 1\. LangGraph

- **定位**：全域工作流与 AI Agent 图编排引擎，LangChain 生态核心组件

- **核心特性**：

    - 原生支持人机协同（Human\-in\-the\-Loop）中断与恢复机制，可通过`interrupt()`触发暂停、`Command(resume=True)`恢复执行

    - 基于统一状态类（State Class）实现灵活的状态管理，支持执行回溯、线程级会话隔离

    - 双层持久化体系：Checkpointer（线程级短期记忆）\+ Store（跨线程长期记忆），支持 SQLite、PostgreSQL 作为持久化后端

    - 支持条件分支、并行路径、循环工作流，2\.0 版本新增护栏节点用于合规校验与硬约束控制

- **工作流作用**：承载 17 步视频剪辑流水线的全流程编排，管理分镜调整、BGM 选配两个人工暂停点，控制步骤间的数据流转与异常容错

#### 2\. Temporal

- **定位**：企业级宏观业务编排引擎，规模化生产级方案

- **核心特性**：

    - 支持工作流持久化挂起，通过外部 Signal 信号恢复执行，挂起期间不占用计算资源，无会话超时限制

    - 支持按 Task Queue 将任务路由到不同操作系统的 Worker 池，适配 Windows/Linux 混合执行环境

    - 工作流即代码，支持版本管理、回滚与可观测性，适合长周期、多依赖的业务流程

- **工作流作用**：适配剪映客户端 Windows 专属、AI 计算 Linux 部署的混合架构，承载等待时长不确定的人工关卡，支撑批量视频生产的规模化落地

#### 3\. Grok Build

- **定位**：xAI 推出的终端 AI 编程 Agent 框架，Rust 语言开发

- **核心特性**：

    - 原生支持 ACP 协议，可与 VS Code 深度无缝集成

    - 内置本地沙箱执行环境，保障本地文件操作的安全性

    - 支持 Skill 技能机制与 Hooks 事件钩子，可通过自定义脚本实现工作流精准暂停与恢复

    - 支持多模型接入，默认适配 Grok\-4\.5，可兼容 OpenAI 格式模型

- **工作流作用**：通过剪映专属 Skill 实现自动化剪辑操作，适配 VS Code 内嵌预览的开发工作流

### （二）备选对比框架

#### 1\. CrewAI

- **定位**：角色型多 Agent 编排框架，通过自然语言定义 Agent 角色与目标实现协作

- **核心特性**：原型开发速度快，支持双向 MCP 协议，内置统一记忆体系，配套可视化构建器

- **适配性说明**：适合团队类比的创意类工作流，对本项目确定性强的工具调用流水线拟合度低于 LangGraph

#### 2\. 微软 Agent Framework

- **定位**：微软官方统一 Agent 框架，整合原 AutoGen 与 Semantic Kernel 能力

- **核心特性**：深度集成微软生态，支持图式工作流、人在回路、检查点持久化，原生兼容 MCP 与 A2A 协议

- **适配性说明**：适合 Azure 与微软技术栈标准化的企业环境

#### 3\. OpenAI Agents SDK

- **定位**：OpenAI 官方轻量 Agent 开发工具包

- **核心特性**：学习曲线平缓，原生支持 MCP 协议与沙箱执行，内置追踪与可观测能力

- **适配性说明**：适合 OpenAI 模型优先的场景，长时持久化与复杂分支能力弱于 LangGraph

#### 4\. 其他对比框架

文档中提及的其他备选框架还包括：LlamaIndex Workflows /llama\-agents、Dify、n8n、AG2（AutoGen 社区分叉版）、smolagents、Haystack、AutoGPT、Rasa、Semantic Kernel、DeerFlow 2\.0、OpenClaw、Hermes Agent、LobsterAI、Pi、OpenSquilla 等，均从不同维度进行了适配性对比。

## 二、剪映草稿操作工具生态

### （一）核心底层操作库

#### 1\. pyJianYingDraft

- **定位**：Python 语言实现的剪映草稿底层读写工具，是本工作流的数据层核心

- **核心特性**：

    - 完美还原剪映草稿 JSON 架构，支持微秒级时间戳精确计算

    - 自动处理 UUID 生成、素材关联指向与轨道冲突规避，降低手动修改 JSON 的文件损坏风险

    - 支持「模板模式」，可复用含 VIP 素材的草稿资源 ID，绕过剪映闭源生态限制注入 VIP 特效、花字、贴纸

    - Windows 平台支持草稿生成、模板模式与自动导出全功能，Linux/macOS 仅支持草稿生成与模板模式

- **工作流作用**：支撑初始草稿生成、变速适配、字幕写入、特效转场注入、音频参数调整、封面配置等绝大多数底层草稿操作

### （二）命令行与 API 工具

#### 1\. capcut\-cli

- **定位**：零依赖命令行工具，直接读写`draft_content.json`文件

- **核心特性**：无服务器守护进程，无状态可移植，支持从终端、CI/CD 流水线调用

- **工作流作用**：可作为轻量替代方案，实现草稿的单向读写与导出

#### 2\. CapCutAPI / VectCutAPI

- **定位**：开源 HTTP \+ MCP 接口的剪映草稿操作服务

- **核心特性**：暴露 11 个标准化 MCP 工具，覆盖草稿创建、媒体管理、字幕特效、导出等全流程

- **工作流作用**：可作为标准化 MCP 服务端，对接上层编排框架

### （三）MCP 服务端工具

#### 1\. JianYing MCP Server（hey\-jian\-wei/jianying\-mcp）

- **定位**：基于 Python 的剪映 MCP 服务器，结合 UI 自动化与 pyJianYingDraft 能力

- **核心特性**：暴露草稿创建、轨道管理、特效动画、导出等 MCP 工具，支持环境变量配置路径

#### 2\. SmartCut MCP Server（capcut\-ai\-editor）

- **定位**：专注口播类视频的剪辑 MCP 服务

- **核心特性**：基于字幕自动检测并删除静音 / 重复片段、增强音频，适配访谈类短视频剪辑

### （四）专项剪辑 Agent

#### 1\. CutClaw

- **定位**：基于音乐节拍同步的多 Agent 长视频剪辑工具

- **核心特性**：采用编剧 \+ 剪辑师 \+ 审查员的多 Agent 循环架构，支持镜头规划、片段选取、质量校验全流程

### （五）辅助检测工具

#### 1\. capcut decrypt

- **定位**：剪映草稿加密检测工具

- **核心特性**：可识别`draft_content.json`是否被 AES 加密，适配剪映 6\.0 \+ 版本的加密防护

- **工作流作用**：工作流启动前检测草稿加密状态，匹配对应适配策略（版本降级或单向明文写入）

### （六）封装自动化技能集

基于上述工具封装的可调用 MCP 技能，共 12 项核心技能：

|技能标识|功能说明|
|---|---|
|`/kuaishou-clean-cache`|清理剪映、快手工具、OpenStoryline 与系统临时缓存|
|`/openstoryline-launcher`|一键启动 FireRed\-OpenStoryline 的 MCP 服务与 Web 前端|
|`/openstoryline-to-jianying`|将 OpenStoryline 分镜数据转换为剪映草稿 JSON 文件|
|`/jianying-speed-fit-35s`|通过变速算法将视频总时长压缩至 35 秒以内|
|`/jianying-add-subtitles`|基于 ASR 结果批量生成字幕轨道|
|`/jianying-inject-fx`|注入 VIP 转场与视频特效|
|`/jianying-inject-text-fx`|配置字幕花字样式与入场动画|
|`/jianying-inject-tts-sticker`|为字幕匹配并注入关联 VIP 贴纸|
|`/jianying-make-cover`|生成 9:16 竖屏封面，本地渲染 16:9 与 4:3 横版封面|
|`/jianying-cover-localize-en`|本地实现封面文字的英文本地化，无需进入剪映|
|`/jianying-translate-subtitles`|翻译剪映草稿中的中文字幕为英文|
|`/jianying-inject-english-tts`|注入英文 AI 配音，支持禁用贴纸关联|

## 三、多模态 AI 模型与算法矩阵

### （一）FireRed 模型家族（核心能力底座）

#### 1\. FireRed\-OpenStoryline

- **定位**：核心 AI 视频剪辑 Agent 框架，由 FireRed 团队开源

- **核心特性**：

    - 基于 LLM 规划与工具编排，将自然语言剪辑意图转化为分镜描述与时间轴指令

    - 内置 MCP Server，支持 Agent Skills 复用，提供 CLI、Web UI、Docker 多种部署方式

    - 支持智能分镜、脚本生成、节拍同步 BGM 推荐、AI 转场生成、ASR 粗剪等能力

- **工作流作用**：承担视频素材的智能分镜、场景理解与剪辑策略规划，是自动化创意环节的核心

#### 2\. FireRedASR2S

- **定位**：高精度语音识别模型

- **工作流作用**：识别原始视频的中英文旁白，输出带时间戳的文本，为字幕生成提供基础数据

#### 3\. FireRedTTS2

- **定位**：长文本多说话人语音合成系统

- **工作流作用**：合成高保真、带情感的英文 AI 配音，用于英文版本视频的旁白生成

#### 4\. FireRed\-Image\-Edit

- **定位**：指令遵循型图像编辑模型，支持局部重绘（Inpainting）

- **工作流作用**：实现封面中文文字无痕擦除与背景重建，支撑封面英文本地化

### （二）备选 AI 能力组件

- **edge\-tts**：微软神经语音合成引擎，可作为英文配音的备选方案

- **Whisper**：开源语音识别模型，可作为 ASR 能力的备选

- **GPT\-4 / DeepL**：通用大模型与专业翻译引擎，可作为字幕翻译的备选方案

## 四、前端预览与调试环境

1. **VS Code Microsoft Edge DevTools 插件**

    - 基于 headless Chromium 架构，在 VS Code 内部内嵌浏览器窗口

    - 支持实时 HTML/CSS 检查、前端 JS 逐行调试，自动清理缓存避免预览内容过期

    - 工作流作用：内嵌展示 OpenStoryline 的 Web 交互界面，实现开发环境与预览窗口一体化

2. **VS Code Live Preview 插件**

    - 轻量本地预览插件，可作为 Edge DevTools 的替代方案

3. **Playwright**

    - 浏览器自动化框架，支持无头 / 有头模式

    - 工作流作用：无人值守场景下的浏览器自动化替代方案，替代绑定 VS Code 的预览模式

## 五、本地处理与工具库

### （一）图像处理库

1. **Pillow \(PIL\)**

    - Python 主流图像处理库

    - 工作流作用：实现封面尺寸裁剪、背景模糊填充、文字绘制等本地化图像处理，支撑多比例封面生成与英文封面本地化

2. **OpenCV**

    - 计算机视觉库，可作为复杂图像处理的备选方案

### （二）系统与运维工具

1. **psutil**

    - Python 系统资源监控库

    - 工作流作用：监控 CPU、内存、磁盘占用，实现进程异常自动重启

2. **Git 版本管理**

    - 工作流作用：对关键节点的草稿目录做提交快照，实现版本回滚与差异对比

## 六、存储与持久化组件

1. **SQLite / AsyncSqliteSaver**

    - LangGraph 轻量检查点持久化方案，适合测试与小规模部署

2. **PostgreSQL / PostgresSaver**

    - 生产级强一致性持久化方案，支持跨重启状态保留，推荐生产环境使用

3. **ChromaDB**

    - 向量数据库，用于 CrewAI 等框架的语义记忆存储

4. **Kafka \+ Doris**

    - 埋点与可观测性链路，用于全流程指标聚合与运维监控

## 七、互操作协议标准

1. **MCP（模型上下文协议）**

    - 由 Anthropic 推出，标准化 Agent 调用工具与访问数据的接口规范

    - 工作流作用：统一 OpenStoryline、剪映工具、TTS / 翻译服务的调用接口，避免碎片化集成

2. **A2A（Agent\-to\-Agent 协议）**

    - 由 Google Cloud 主推，标准化 Agent 之间的任务委派与能力发现机制

    - 工作流作用：支撑多 Agent 协作场景的能力互通

3. **ACP 协议**

    - Grok Build 原生支持的编辑器集成协议，实现 Agent 与 IDE 的深度联动

> （注：部分内容可能由 AI 生成）

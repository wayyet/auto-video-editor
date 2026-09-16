# 主流开源 AI Agent 开发框架（2025–2026）与 AI 视频剪辑工作流编排设计

## 1. 概览与生态格局

截至 2025–2026 年，开源 AI Agent 框架生态已围绕少数几个具备生产级能力的主流工具收敛，外加若干专门化与无代码平台。这一收敛由两个开放互操作标准推动：**模型上下文协议（MCP）**——由 Anthropic 于 2024 年 11 月推出，用于标准化 Agent 调用工具与访问数据的方式（[Anthropic](https://www.anthropic.com/news/model-context-protocol)、[Wikipedia](https://en.wikipedia.org/wiki/Model_Context_Protocol)）；以及 **Agent-to-Agent（A2A）协议**——由 Google Cloud 主推，用于通过"agent card"实现 Agent 之间的任务委派与能力发现（[Auth0](https://auth0.com/blog/mcp-vs-a2a/)、[DigitalOcean](https://www.digitaloceancommunity.com/community/tutorials/a2a-vs-mcp-ai-agent-protocols)）。两者互补：MCP 标准化 Agent 到工具/数据的落地，A2A 标准化 Agent 到 Agent 的委派与协商（[Auth0](https://auth0.com/blog/mcp-vs-a2a/)）。

行业调研显示约 85% 的组织已在某种程度上使用 AI Agent，其中 LangChain/LangGraph 与 CrewAI 被列为两大主流开源编排玩家，微软则通过统一 Agent Framework 整合了微软栈生态（[ZenML](https://www.zenml.io/blog/langgraph-vs-crewai)）。

---

## 2. 各框架技术栈、优点与缺点

### 2.1 LangGraph（LangChain 出品）

**技术栈**
- **语言：** Python 与 JavaScript/TypeScript；MIT 许可，完全开源（[LangChain](https://www.langchain.com/langgraph)）。
- **核心模型：** 基于图的编排——Agent/工具是节点，边是条件转移，形成有向（常含环）图而非线性链（[dev.to](https://dev.to/richard_dillon_b9c238186e/langgraph-20-the-definitive-guide-to-building-production-grade-ai-agents-in-2026-4j2b)）。
- **LLM/供应商支持：** 模型无关，通过 LangChain 的模型封装可对接任意供应商。
- **记忆：** 两层持久化——**Checkpointer**（线程级短期记忆，支持对话连续性、人在回路暂停、时间旅行、容错）与 **Store**（跨线程长期记忆，存用户偏好、共享事实）（[LangChain Docs](https://docs.langchain.com/oss/python/langgraph/persistence)）。检查点可持久化到 Postgres 以跨重启保持（[LangGraph GitHub Discussions](https://github.com/langchain-ai/langgraph/discussions/4375)）。
- **工具调用：** 原生函数/工具调用节点；支持将 MCP 作为工具来源，以及新兴的 A2A 标准（[dev.to](https://dev.to/richard_dillon_b9c238186e/langgraph-20-the-definitive-guide-to-building-production-grade-ai-agents-in-2026-4j2b)）。
- **多 Agent 编排：** 显式图拓扑，单一框架内支持单 Agent、多 Agent、分层控制流；2.0 版（2026 年 2 月）新增一等公民"护栏节点（Guardrail Nodes）"，用于内容过滤、限流与合规日志（[dev.to](https://dev.to/richard_dillon_b9c238186e/langgraph-20-the-definitive-guide-to-building-production-grade-ai-agents-in-2026-4j2b)）。

**优点**
- 生产级、规模化验证：月下载量约 9000 万，部署于 Uber、JPMorgan、BlackRock、Cisco、LinkedIn、Klarna（[AlphaBold](https://www.alphabold.com/langgraph-agents-in-production/)）。
- 完全可观测——每个决策点、分支、重试都显式且可检查，区别于黑盒认知架构框架（[LangChain](https://www.langchain.com/langgraph)）。
- 原生支持循环工作流、条件分支、并行路径与可持久化暂停/恢复——对长时运行、容错管道至关重要。
- LangGraph 1.0（2025 年 10 月）标志 API 稳定，适合企业采用（[AlphaBold](https://www.alphabold.com/langgraph-agents-in-production/)）。

**缺点**
- 学习曲线较陡；被归为"进阶"难度，需理解图/状态机概念而非自然语言角色分配（[Codecademy](https://www.codecademy.com/article/top-ai-agent-frameworks-in-2025)）。
- 比角色型框架样板代码多——一个对比估算多 Agent 系统约需 60+ 行代码，而 CrewAI 约 20 行（[automely.ai](https://automely.ai/blogs/langchain-vs-langgraph-vs-crewai-ai-agent-framework)）。
- 搭建与达到生产就绪时间更长（约 1–3 周 vs CrewAI 的 1–2 周）（[agent-kits.com](https://www.agent-kits.com/2025/10/langchain-vs-crewai-vs-autogpt-comparison.html)）。

---

### 2.2 CrewAI

**技术栈**
- **语言：** Python；完全从头构建，独立于 LangChain（[agent-kits.com](https://www.agent-kits.com/2025/10/langchain-vs-crewai-vs-autogpt-comparison.html)）。
- **核心模型：** 角色型编排——Agent 以角色、目标、背景故事定义（如 Manager、Worker、Researcher），组装成"Crew"通过自然语言委派协作；或组装成确定性"Flow"做显式控制（[Latenode](https://latenode.com/blog/crewai-agent-framework)、[CrewAI](https://crewai.com/)）。
- **LLM/供应商支持：** 通过 LiteLLM 式集成支持广泛 LLM。
- **记忆：** v1.15 文档将短期、长期、实体、外部记忆统一为单一 `Memory` 类，采用 LLM 打分召回（语义相似度 + 时近度 + 重要性）；默认 ChromaDB 做向量记忆、SQLite 做结构化表（[CrewAI Docs](https://docs.crewai.com/v1.15.2/en/concepts/memory)）。原生记忆按 crew 执行作用域，非按终端用户对话，因此生产聊天场景常外挂 Mem0 或 Zep（[CrewAI Community](https://community.crewai.com/t/crewai-memories-multi-users-environment-conversational-history/4237)）。
- **工具调用：** CrewAI 企业版支持双向 MCP——crew/flow 可消费 MCP 服务器，也可自身暴露为 MCP 服务器；带 RBAC 的私有工具库（[CrewAI Blog](https://blog.crewai.com/how-crewai-is-evolving-beyond-orchestration-to-create-the-most-powerful-agentic-ai-platform/)）。
- **多 Agent 编排：** 原生角色型"Crew"模式（Agent 间自然语言委派）+ 独立"Flow"模式（确定性事件驱动编排）；无代码可视化构建器可导出 Python（[CrewAI](https://crewai.com/)）。

**优点**
- 主流框架中首个原型最快——约 2–4 小时 vs LangChain/LangGraph 的 4–8 小时（[agent-kits.com](https://www.agent-kits.com/2025/10/langchain-vs-crewai-vs-autogpt-comparison.html)）。
- 团队式工作流的心智模型直观：若问题天然映射到人类团队角色，数小时内即可高效产出（[automely.ai](https://automely.ai/blogs/langchain-vs-langgraph-vs-crewai-ai-agent-framework)）。
- 企业化轨迹强劲：GitHub 星数增长（一次对比中 33.4k），企业版控制面带实时追踪、护栏、双向 MCP（[ZenML](https://www.zenml.io/blog/langgraph-vs-crewai)、[CrewAI Blog](https://blog.crewai.com/how-crewai-is-evolving-beyond-orchestration-to-create-the-most-powerful-agentic-ai-platform/)）。

**缺点**
- 状态转移的确定性与显式控制不如 LangGraph——Agent 间自然语言委派在合规重场景下更难审计与调试。
- 原生记忆非为多用户对话历史设计，常需外挂记忆服务（[CrewAI Community](https://community.crewai.com/t/crewai-memories-multi-users-environment-conversational-history/4237)）。
- 一个生产基准引用其在复杂生产 Agent 评测中任务成功率低于 LangGraph（数值因来源而异，仅作方向性参考）（[automely.ai](https://automely.ai/blogs/langchain-vs-langgraph-vs-crewai-ai-agent-framework)）。

---

### 2.3 微软 Agent Framework（AutoGen + Semantic Kernel 的继任者；AutoGen/AG2 谱系）

**技术栈**
- **语言：** Python 与 .NET；开源（MIT）（[langchain.com 资源页](https://www.langchain.com/resources/ai-agent-frameworks)）。
- **历史：** AutoGen 源自微软研究院的对话式多 Agent 框架（[Microsoft Research](https://www.microsoft.com/en-us/research/project/autogen/)）。2024 年末，部分原始贡献者分叉为社区治理的 **AG2**；微软则于 2025 年 1 月将 AutoGen 重写为 **v0.4**（异步、事件驱动架构）（[orange-its.ch](https://www.orange-its.ch/en/insights/autogen-ag2-review)）。2025 年 10 月微软将 AutoGen 0.4 退役进入维护模式，与 Semantic Kernel 合并为统一的 **Microsoft Agent Framework**，于 2026 年 4 月 3 日 GA（v1.0）（[Microsoft DevBlogs](https://devblogs.microsoft.com/agent-framework/migrate-your-semantic-kernel-and-autogen-projects-to-microsoft-agent-framework-release-candidate/)、[agentscout.live](https://agentscout.live/tech/ai-agents/news/20260505-microsoft-agent-framework-1-0-ga-autogen-semantic-kernel/)）。
- **核心模型：** 结合 AutoGen 简单对话式 Agent 抽象与 Semantic Kernel 的企业特性（会话级状态管理、中间件、遥测、类型安全）；新增图式工作流，支持顺序、并发、移交、群聊模式，带流式、检查点、人在回路（[Microsoft DevBlogs](https://devblogs.microsoft.com/agent-framework/migrate-your-semantic-kernel-and-autogen-projects-to-microsoft-agent-framework-release-candidate/)）。
- **LLM/供应商支持：** Microsoft Foundry、Azure OpenAI、OpenAI、GitHub Copilot、Anthropic Claude、AWS Bedrock、Google Gemini、Ollama 等。
- **记忆：** 继承自 Semantic Kernel 的会话级状态管理，外加 AutoGen 式可插拔记忆组件。
- **工具调用：** 类型安全的"函数工具"调用任意代码；原生互操作 **A2A**、**AG-UI**、**MCP** 标准。
- **多 Agent 编排：** 图式工作流统一 AutoGen 对话群聊模式与 Semantic Kernel 的结构化编排模式。

**优点**
- 微软生态集成最深（Azure AI Foundry、Copilot Studio、企业身份/合规工具）——适合微软基础设施标准化的组织（[langchain.com](https://www.langchain.com/resources/ai-agent-frameworks)）。
- 单一 SDK 消除了历史 AutoGen vs Semantic Kernel 的选择困境；GA 承诺完全向后兼容（[agentscout.live](https://agentscout.live/tech/ai-agents/news/20260505-microsoft-agent-framework-1-0-ga-autogen-semantic-kernel/)）。
- 在企业背书框架中，开箱即用的一方多供应商支持最广（六大以上模型供应商）。

**缺点**
- 已投入 AutoGen 0.4 或 Semantic Kernel 的团队有迁移成本——两者前身现已进入维护/特性冻结（[orange-its.ch](https://www.orange-its.ch/en/insights/autogen-ag2-review)）。
- 生态碎片化风险：今天的"AutoGen"可能指社区 AG2 分叉、冻结的微软 0.4 版或新的统一框架——命名混乱影响招聘、文档与包选择。
- 作为统一产品生产履历较 LangGraph 更短（2026 年 4 月才 GA）。

---

### 2.4 AG2（AutoGen 的社区分叉）

**技术栈**
- **语言：** Python；包 `ag2`、`autogen`、`pyautogen` 保持互通（[LinkedIn/AG2 公告](https://www.linkedin.com/posts/bunyaminergen_github-ag2aiag2-ag2-formerly-autogen-activity-7264059431565320192--Hzg)）。
- **核心模型：** 对话式多 Agent 框架，可定制"conversable"Agent 交换消息完成任务，混合 LLM、人工输入与工具（[Microsoft Research 论文](https://www.microsoft.com/en-us/research/publication/autogen-enabling-next-gen-llm-applications-via-multi-agent-conversation-framework/)）。
- **治理：** 社区驱动，独立于单一公司路线图，由 Google、Meta、NVIDIA 及学术实验室（宾州州立、伯克利、MIT、华盛顿、剑桥、普林斯顿）贡献者支持。
- **记忆/工具调用/多 Agent 编排：** 与 AutoGen 0.4 同源——异步、事件驱动消息传递；可插拔自定义 Agent、工具、模型。

**优点**
- 保留 AutoGen 原始开放、社区治理精神，吸引警惕厂商锁定的团队。
- 过渡期对现有 `autogen`/`pyautogen` 用户无破坏性变更。

**缺点**
- 资源少于微软统一框架；长期路线图对等性不确定。
- 与微软现已分离的"AutoGen"谱系品牌重叠，对新用户造成真实困惑（[orange-its.ch](https://www.orange-its.ch/en/insights/autogen-ag2-review)）。

---

### 2.5 OpenAI Agents SDK

**技术栈**
- **语言：** Python 与 TypeScript（官方），外加第三方 Go 移植；宽松许可开源（[know.2nth.ai](https://know.2nth.ai/explainers/agents/openai-agents-sdk)）。
- **核心模型：** 五大原语——**Agents**（LLM + 指令 + 工具）、**Handoffs**（Agent 间委派）、**Tools**、**Guardrails**（输入/输出校验）、**Tracing**；刻意最小化抽象集（[OpenAI Agents SDK 文档](https://openai.github.io/openai-agents-python/)）。
- **LLM/供应商支持：** 为 OpenAI 的 GPT 与 o 系列模型优化；框架对 OpenAI"模型原生"，但开放原语可包装其他供应商。
- **记忆：** 2025 SDK 更新新增可配置记忆，外加对长时、文件/工具密集型任务的沙箱感知编排（[OpenAI](https://openai.com/index/the-next-evolution-of-the-agents-sdk/)）。
- **工具调用：** 原生函数调用；原生 MCP 集成；"通过技能渐进式披露"、`AGENTS.md` 自定义指令、shell/代码执行工具、"apply patch"文件编辑工具（[OpenAI](https://openai.com/index/the-next-evolution-of-the-agents-sdk/)）。
- **多 Agent 编排：**"Agent 即工具"/Handoffs 模式让 Agent 委派给专家；内置 Agent 循环自动处理工具调用/响应循环直至任务完成（[OpenAI Agents SDK 文档](https://openai.github.io/openai-agents-python/)）。
- **沙箱：** 原生沙箱执行，内置支持 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop、Vercel，或自带沙箱（[OpenAI](https://openai.com/index/the-next-evolution-of-the-agents-sdk/)）。

**优点**
- 学习曲线最小——"原语少到可快速上手"又足够表达复杂 Agent/工具关系（[OpenAI Agents SDK 文档](https://openai.github.io/openai-agents-python/)）。
- 对 OpenAI 模型标准化的团队是测试最充分的选项；该栈上原型到生产路径最干净（[know.2nth.ai](https://know.2nth.ai/explainers/agents/openai-agents-sdk)）。
- 内置追踪/可观测仪表盘，开箱即用的 MCP 优先工具集成。

**缺点**
- 第三方集成生态不如 LangChain 丰富成熟。
- 模型原生设计意味着对 OpenAI 模型优化最多——跨供应商对等性次要。
- 内置多 Agent 拓扑少于 LangGraph 或 CrewAI（无显式图/状态机可视化）。

---

### 2.6 LlamaIndex Workflows（及 llama-agents）

**技术栈**
- **语言：** Python（主），异步优先设计，便于与 FastAPI 等框架集成（[LlamaIndex](https://www.llamaindex.ai/workflows)）。
- **核心模型：** 事件驱动编排，多步 Agent 系统由有类型"step"构建；支持循环、并行路径、有状态的开始/暂停/恢复；2025 年 6 月达到"Workflows 1.0"（[LlamaIndex](https://www.llamaindex.ai/workflows)）。
- **LLM/供应商支持：** 广泛供应商支持，含 OpenAI、Mistral 函数调用指南；强 RAG/文档处理血统。
- **记忆：** 状态在有类型 step 间显式传递；可与人在回路审查节点组合。
- **工具调用：** 原生函数调用与工具使用抽象；上层有查询规划原语（路由、子问题、查询变换）（[LlamaIndex Developer Docs](https://developers.llamaindex.ai/python/framework/use_cases/agents/)）。
- **多 Agent 编排：** 独立的 `llama-agents` 项目提供**分布式、面向服务架构**——每个 Agent 作为独立微服务，由可定制的 LLM 驱动控制面协调，基于消息队列的 Agent 间通信，路由可为显式或"agentic"（LLM 决策）（[LlamaIndex Blog](https://www.llamaindex.ai/blog/introducing-llama-agents-a-powerful-framework-for-building-production-multi-agent-ai-systems)）。

**优点**
- 完全开源，无商用限制（[LlamaIndex](https://www.llamaindex.ai/workflows)）。
- 鉴于 LlamaIndex 的检索血统，RAG 密集或文档处理管道最佳。
- `llama-agents` 微服务模型可独立扩展部署每个 Agent，适合异构基础设施需求。

**缺点**
- 两个相关但不同的产品（`Workflows` 单应用编排 vs `llama-agents` 分布式多 Agent）带来架构决策开销。
- 通用 Agent 基准中多 Agent 影响力小于 LangGraph/CrewAI/AutoGen。

---

### 2.7 Semantic Kernel（遗留/维护轨道，已被继任）

**技术栈**
- **语言：** .NET（主）、Python、Java。
- **核心模型：** Semantic Kernel 内的 `Agent Framework` 层提供 `ChatCompletionAgent`、`OpenAIAssistantAgent`、`AzureAIAgent`、`OpenAIResponsesAgent`、`CopilotStudioAgent` 类型（[Microsoft Learn](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-architecture)）。
- **多 Agent 编排：** 多种 Agent 类型可在单一对话中协作，含人工输入；编排模式在 2025 过渡期仍标"experimental"（[Microsoft Learn](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-architecture)）。
- **状态：** 2025 Q1 达 GA（v1.0），随后于 2025 年 10 月正式并入微软 Agent Framework（[Microsoft DevBlogs](https://devblogs.microsoft.com/agent-framework/semantic-kernel-roadmap-h1-2025-accelerating-agents-processes-and-integration/)）。

**优点：** 企业就绪能力（类型安全、插件模型、遥测）比 AutoGen 更早成熟。**缺点：** 现为遗留入口——新项目被直接导向微软 Agent Framework。

---

### 2.8 Hugging Face smolagents

**技术栈**
- **语言：** Python；极轻量（核心逻辑约 1000 行）（[Hugging Face Docs](https://huggingface.co/docs/smolagents/en/index)、[GitHub](https://github.com/huggingface/smolagents)）。
- **核心模型：** 旗舰模式 `CodeAgent`（把动作写成可执行代码而非 JSON），外加传统 `ToolCallingAgent` 做 JSON/文本工具调用。
- **LLM/供应商支持：** 完全模型无关——HF Hub 推理、OpenAI、Anthropic 等经 LiteLLM，或本地 `transformers`/Ollama 模型（[Hugging Face Blog](https://huggingface.co/blog/smolagents)）。
- **记忆：** 内置记忆极简，依赖调用方应用或外部记忆服务。
- **工具调用：** 工具无关——可消费任意 MCP 服务器工具；也支持作为 Gradio Spaces 托管在 Hub 的工具（[Hugging Face Docs](https://huggingface.co/docs/smolagents/en/index)）。
- **多 Agent 编排：** 轻量；通过代码（函数嵌套、循环、条件）组合而非专用图/角色 DSL。
- **沙箱：** 在沙箱环境（Modal、Blaxel、E2B、Docker）执行 CodeAgent 动作。
- **模态：** 文本、视觉、视频、音频输入支持。

**优点**
- 代码库极小、可审计——易于精确理解框架做了什么。
- 代码执行 Agent 的沙箱故事强；真正模型无关。
- 模态无关（视频/音频）对媒体生成与编辑 Agent 直接有用。

**缺点**
- 内置记忆与编排原语少于 LangGraph/CrewAI——复杂多 Agent 工作流需自建更多脚手架。
- 企业工具少于 CrewAI Enterprise 或 LangGraph 2.0（无内置控制面、护栏节点或托管追踪仪表盘）。

---

### 2.9 Haystack（deepset）

**技术栈**
- **语言：** Python；由可组合"组件"与"管道"构建的开源 LLM 编排框架（[Haystack](https://haystack.deepset.ai/)）。
- **核心模型：** 内置 `Agent` 组件管理完整工具调用循环（调 LLM → 触发工具 → 更新状态 → 重复直至停止条件）（[Haystack Docs](https://docs.haystack.deepset.ai/docs/agents)）。
- **记忆：** `state_schema` 机制在工具间共享有类型数据并跨迭代累积结果。
- **工具调用：** 标准化工具调用，带分支/循环管道处理复杂决策流；也处理图像与音频（[Haystack](https://haystack.deepset.ai/)）。
- **多 Agent 编排：** 把 `Agent` 包装为 `ComponentTool` 构建协调者/专家多 Agent 架构；支持执行前拦截工具调用的人在回路（[Haystack Docs](https://docs.haystack.deepset.ai/docs/agents)）。
- **工具生态：** deepset Studio 提供可视化管道设计/测试后再部署（[YouTube — deepset 讲座](https://www.youtube.com/watch?v=tKyvkU69Ers)）。

**优点**
- 成熟 RAG 血统，生产就绪的检索 + 生成 + Agent 统一。
- 核心内置强状态管理与人在回路原语。

**缺点**
- 纯多 Agent 编排话题中社区/影响力小于 LangGraph/CrewAI。
- 可视化工具（deepset Studio）为独立产品层，增加整体栈面。

---

### 2.10 Dify

**技术栈**
- **语言：** 后端 Python；源码可见，Apache-2.0 衍生许可（"Dify 开源许可"）（[Dify GitHub](https://github.com/langgenius/dify)、[Dify](https://dify.ai/)）。
- **核心模型：** 统一可视化平台，整合工作流构建器、Agent 框架、RAG 管道/知识库与模型管理于一个工作区；社区版 GitHub 星数超 149K（[Dify](https://dify.ai/)）。
- **LLM/供应商支持：** 广泛多模型支持，带模型管理层。
- **记忆/可观测：** 经 Opik、Langfuse、Arize Phoenix 集成可观测（[Dify GitHub](https://github.com/langgenius/dify)）。
- **部署：** Docker/Kubernetes 自托管或 Dify Cloud（[Skywork 评测](https://skywork.ai/blog/dify-review-2025-workflows-agents-rag-ai-apps/)）。
- **工具调用/多 Agent 编排：** 可视化工作流画布串联工具与 Agent 步骤；比 LangGraph 低代码，但支持 agent + RAG + 工作流组合于单一低代码环境。

**优点**
- 对非工程师门槛最低——可视化、无代码优先设计（[Dify](https://dify.ai/)）。
- 单一平台覆盖 RAG、Agent、工作流自动化，减少常见生产场景的集成面。
- 非常大且活跃的开源社区（149K+ 星）。

**缺点**
- "源码可见"许可比纯 Apache-2.0 多附加条件——商业再分发场景需审阅许可条款。
- 低层控制不如代码优先框架（LangGraph、smolagents）。
- 托管云层无统一权威定价页，规划不便（[Skywork 评测](https://skywork.ai/blog/dify-review-2025-workflows-agents-rag-ai-apps/)）。

---

### 2.11 n8n

**技术栈**
- **语言：** Node.js/TypeScript 核心；fair-code 许可（Sustainable Use License）+ 企业许可（[n8n GitHub](https://github.com/n8n-io/n8n)）。
- **核心模型：** 可视化、基于节点的工作流自动化平台（"node everywhere, node everything"），400+ 集成，基于 LangChain 原语的原生 AI Agent 能力（[n8n GitHub](https://github.com/n8n-io/n8n)、[Medium 概览](https://medium.com/@Rahul_Samajpati/basic-overview-of-n8n-open-source-workflow-automation-with-docker-ai-agents-real-use-cases-0c34eb4cc35f)）。
- **工具调用：** 每个节点可为 API 调用、数据库查询、条件判断或 AI 推理步骤；支持在工作流任意处嵌入原始 JS/Python 代码（[n8n](https://n8n.io/)）。
- **多 Agent 编排：** AI Agent 节点可与常规自动化节点串联，使 n8n 成为 AI Agent 与传统 API/服务之上的编排层。
- **部署：** Docker 自托管或 n8n Cloud；完整源码在 GitHub。

**优点**
- 结合代码灵活与无代码速度——适合需要 AI 推理步骤与确定性系统集成（Slack、数据库、云服务）于一个画布的团队（[n8n](https://n8n.io/)）。
- 海量集成目录（400–500+ 预置连接器）大幅减少真实业务工作流的胶水代码（[Medium 概览](https://medium.com/@Rahul_Samajpati/basic-overview-of-n8n-open-source-workflow-automation-with-docker-ai-agents-real-use-cases-0c34eb4cc35f)）。
- 数据与基础设施完全自托管控制。

**缺点**
- "fair-code"（非纯开源）许可限制某些商业转售/SaaS 托管场景。
- 深层代码级 Agent 推理模式（如自定义有环图、细粒度状态机）不如 LangGraph——擅长编排/集成胶水而非复杂 Agent 认知。

---

### 2.12 AutoGPT

**技术栈**
- **语言：** Python 核心平台 + 构建 Agent 的"Forge"工具包；仓库主体 MIT 许可（[AutoGPT GitHub](https://github.com/significant-gravitas/autogpt)）。
- **核心模型：** 目标驱动自主 Agent 平台；遵循 AI Engineer Foundation 的 Agent Protocol 标准实现互操作。
- **多 Agent 编排：** 起初单 Agent/目标驱动；"AutoGPT Platform"后新增可视化工作流构建。

**优点**
- 历史上是使自主、目标驱动 Agent 普及的框架；社区庞大，极易上手（15–30 分钟搭建）（[agent-kits.com](https://www.agent-kits.com/2025/10/langchain-vs-crewai-vs-autogpt-comparison.html)）。
- 适合快速实验、原型与概念验证（[Codecademy](https://www.codecademy.com/article/top-ai-agent-frameworks-in-2025)）。

**缺点**
- "经典"自主循环模式在产品级部署中普遍认为不如图式或角色式编排稳健（[Codecademy](https://www.codecademy.com/article/top-ai-agent-frameworks-in-2025)）；新"AutoGPT Platform"更生产就绪但原始实验核心并非如此。
- 入门友好但无强护栏时长程规划不可靠。

---

### 2.13 Rasa

**技术栈**
- **语言：** Python；开源核心 + 企业"Rasa Platform"层（[Rasa GitHub](https://github.com/rasahq/rasa)）。
- **核心模型：** 为多轮、上下文对话助手（聊天与语音）跨渠道（Slack、Messenger、Alexa、Google Home、Telegram 等）量身定制的 NLU + 对话管理框架（[Rasa GitHub](https://github.com/rasahq/rasa)）。
- **多 Agent 编排：** 专利对话管理系统编排自主推理、引导式工作流与共享对话记忆（[Rasa](https://rasa.com/)）。
- **工具调用/LLM 支持：** 用确定性业务逻辑/策略约束 LLM，而非纯依赖 LLM 函数调用；提示词、策略、代码库开放访问避免厂商锁定（[Rasa](https://rasa.com/)）。

**优点**
- 为合规、高量对话助手（LLM 行为须受确定性业务逻辑约束的受监管行业、联络中心）量身打造。
- 生产用量每月 1000 次对话以下可免费许可（[Rasa GitHub org](https://github.com/rasahq)）。

**缺点**
- 焦点窄于通用 Agent 框架——非为开放式多步任务自动化（如视频剪辑或编码管道）设计。
- 最企业级能力（Rasa Studio、跨引导流/外部 Agent 全编排）在商业"Rasa Platform"之后。

---

## 3. 对比汇总表

| 框架 | 语言 | 许可 | 编排模型 | 原生记忆 | MCP/A2A 支持 | 最适合 | 学习曲线 |
|---|---|---|---|---|---|---|---|
| [LangGraph](https://www.langchain.com/langgraph) | Python/TS | MIT（OSS） | 有向图/状态机 | Checkpointer（短期）+ Store（长期） | 是（MCP + 新兴 A2A） | 复杂、有状态、可审计的生产管道 | 进阶 |
| [CrewAI](https://crewai.com/) | Python | OSS 核心 + 企业版 | 角色型"Crew" + 确定性"Flow" | 统一 `Memory` 类（语义+时近+重要性） | 双向 MCP（企业版） | 团队类比多 Agent 工作流、快速原型 | 中级 |
| [微软 Agent Framework](https://devblogs.microsoft.com/agent-framework/migrate-your-semantic-kernel-and-autogen-projects-to-microsoft-agent-framework-release-candidate/) | Python/.NET | MIT（OSS） | 图工作流：顺序/并发/移交/群聊 | 会话级状态（Semantic Kernel 血统） | MCP + A2A + AG-UI 原生 | 微软栈企业统一 AutoGen + SK | 中级 |
| [AG2](https://www.linkedin.com/posts/bunyaminergen_github-ag2aiag2-ag2-formerly-autogen-activity-7264059431565320192--Hzg) | Python | OSS（社区） | 对话式多 Agent | 可插拔 | 社区 MCP 集成 | 想要厂商中立 AutoGen 血统的团队 | 中级 |
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) | Python/TS/Go | OSS（宽松） | Agent 即工具/Handoffs | 可配置记忆 + 沙箱状态 | 原生 MCP | OpenAI 模型优先的生产 Agent | 初级–中级 |
| [LlamaIndex Workflows / llama-agents](https://www.llamaindex.ai/workflows) | Python | OSS 无商用限制 | 事件驱动 step；分布式微服务（llama-agents） | 有类型 step 状态 | MCP 兼容工具使用 | RAG 密集与文档中心 Agent 系统 | 中级 |
| [smolagents](https://huggingface.co/docs/smolagents/en/index) | Python | Apache-2.0 | 代码驱动 `CodeAgent`/`ToolCallingAgent` | 极简，应用管理 | 消费任意 MCP 服务器 | 轻量、可审计、沙箱化代码 Agent；多模态 | 初级 |
| [Haystack](https://haystack.deepset.ai/) | Python | Apache-2.0 | 组件/管道 `Agent` 循环 | `state_schema` 有类型状态 | 标准化工具调用 | RAG + Agent 统一、协调者/专家模式 | 中级 |
| [Dify](https://dify.ai/) | Python（后端） | 源码可见（Apache-2.0 衍生） | 可视化工作流画布 | 内置 + Opik/Langfuse/Arize | MCP 生态增长中 | 无代码/低代码 Agent + RAG 平台 | 初级 |
| [n8n](https://n8n.io/) | Node.js/TS | fair-code（Sustainable Use License） | 可视化节点图自动化 | 工作流级状态 | 社区 MCP 节点 | 业务流程自动化 + AI 步骤 | 初级 |
| [AutoGPT](https://github.com/significant-gravitas/autogpt) | Python | MIT（主体） | 目标驱动自主循环 | 基础 | Agent Protocol 标准 | 自主 Agent 快速原型 | 初级 |
| [Rasa](https://rasa.com/) | Python | OSS 核心 + 商业平台 | 对话管理策略 | 共享对话记忆 | N/A（渠道中心） | 受监管、高量对话助手 | 中级 |

**关键对比洞察：** 市场已有效分化为两类——(1) **代码优先、图/状态机框架**（LangGraph、微软 Agent Framework、Haystack、smolagents）以更陡学习曲线换取可审计性、持久性与细粒度控制；(2) **角色型或可视化优先平台**（CrewAI、Dify、n8n、AutoGPT）以部分控制换取开发速度与对非专家的可及性（[automely.ai](https://automely.ai/blogs/langchain-vs-langgraph-vs-crewai-ai-agent-framework)、[Alice Labs](https://alicelabs.ai/en/insights/open-source-ai-agent-frameworks-comparison-2026)）。一篇 2026 年对比总结"没有单一最佳框架"，但把 LangGraph 评为新开源生产项目最稳妥的通用选择（"8.9 生产评分"）（[Alice Labs](https://alicelabs.ai/en/insights/open-source-ai-agent-frameworks-comparison-2026)）。

---

## 4. 视频剪辑 MCP/工具生态（与下方工作流直接相关）

若干开源项目专门针对 AI 驱动视频剪辑，是目标管道的直接相关构件：

- **FireRed-OpenStoryline**：把"复杂视频创作变为自然、直观的对话"的 AI 视频剪辑 Agent，结合 LLM 驱动的规划与精确工具编排（[FireRed-OpenStoryline GitHub](https://github.com/FireRedTeam/FireRed-OpenStoryline)）。原生提供 **MCP Server**（`python -m open_storyline.mcp.server`）、可复用 **Agent Skills**（`openstoryline-install`、`openstoryline-use`，兼容 OpenClaw、Claude Code、Codex 类 Agent），支持 CLI、Web UI 与 Docker 部署。能力直接映射目标管道：自动媒体搜索/分段、脚本生成（few-shot 风格迁移）、BGM/配音/字体自动推荐（带节拍同步）、对话式重排/重剪、AI 转场生成（用尾帧+首帧+自然语言描述，2026-04-02 新增）、基于 ASR 的粗剪技能（去口头禅与重复镜头，2026-03-22 新增）。
- **JianYing MCP Server**（`hey-jian-wei/jianying-mcp`）：基于 Python 的 MCP 服务器，通过 UI 自动化与 `pyJianYingDraft` 库自动化剪映（CapCut 中文版），暴露草稿创建、媒体/轨道管理、特效/动画、导出等 MCP 工具，经 `SAVE_PATH`/`OUTPUT_PATH` 环境变量配置（[Playbooks](https://playbooks.com/mcp/hey-jian-wei/jianying-mcp)、[PulseMCP](https://www.pulsemcp.com/servers/hey-jian-wei-jianying)）。
- **CapCutAPI / VectCutAPI**（`sun-guannan/CapCutAPI`）：更完整的 CapCut/剪映草稿操作开源 HTTP + MCP API，经 JSON-RPC 2.0/stdio 暴露 11 个 MCP 工具——`create_draft`、`get_draft`、`update_draft`、`add_video`、`add_audio`、`add_image`、`add_text`、`add_subtitle`、`add_effect`、`add_sticker`、`export_draft`——与目标管道步骤 4–14 几乎一一对应（[LobeHub/CapCutAPI](https://lobehub.com/mcp/sun-guannan-capcutapi)、[DeepWiki MCP 工具参考](https://deepwiki.com/ashreo/CapCutAPI/13.2-mcp-tool-reference)）。
- **SmartCut MCP Server**（`mrbuslov/capcut-ai-editor`）：专注"talking head"剪辑——直接基于 CapCut 自动生成的字幕检测并删除静音/重复镜头、添加字幕、增强音频，导回 CapCut 项目（[LobeHub/SmartCut](https://lobehub.com/mcp/mrbuslov-capcut-ai-editor)）。
- **capcut-cli**：零依赖、无服务器 CLI 替代方案，直接读写 `draft_content.json`（JSON 进、JSON 出），显式为任意 LLM Agent（Claude、DeepSeek、GLM、Kimi）从 `bash`、`make`、GitHub Actions 或 cron 驱动而设计——无 MCP 服务器或 HTTP 守护进程在环，以牺牲 MCP 可发现性换取无状态与可移植性（[Libraries.io](https://libraries.io/npm/capcut-cli)、[Gist 演示](https://gist.github.com/renezander030/866bd85789c5902471f8f5fc86d09342)）。
- **CutClaw**（`GVCLab/CutClaw`）："基于音乐同步的 Agent 式长视频剪辑"——用 **编剧 + 剪辑师 + 审查员** 多 Agent 循环把原始素材/音频解构为结构化字幕、规划镜头（`shot_plan`）、选取片段时间戳（`shot_point`）、质量校验并渲染，带明确模型角色建议（视频模型做镜头理解、音频模型做 ASR + 节拍/能量分析、Agent 模型做规划循环），并声明路线图目标是支持 Claude Code MCP（[GVCLab/CutClaw GitHub](https://github.com/GVCLab/CutClaw)）。
- **MCP 标准本身**：提供通用"宿主 ↔ 客户端 ↔ 服务器"架构，让单一编排 Agent 通过一致的接口调用异构视频剪辑服务器（OpenStoryline、剪映/CapCut、TTS、翻译），而非为每个服务做定制集成（[Anthropic](https://www.anthropic.com/news/model-context-protocol)、[Anthropic Engineering — 用 MCP 执行代码](https://www.anthropic.com/engineering/code-execution-with-mcp)）。

---

## 5. 17 步 AI 视频剪辑编排工作流设计

### 5.1 管道（按你的需求）

1. 清理旧缓存
2. 启动 OpenStoryline MCP
3. VS Code Edge 插件（浏览器自动化桥）
4. 导入视频并输出分镜头
5. 导入最新草稿到剪映
6. 人工调整分镜顺序（人在回路）
7. 每个分镜变速以≤35 秒
8. 加字幕
9. 加转场/特效
10. 修改字幕花字与动画
11. 字幕加贴纸
12. 人工加 BGM（人在回路）
13. 调整音量
14. 制作封面（9:16 / 16:9 / 4:3）
15. 本地化封面为英文（本地，不进剪映）
16. 翻译字幕为英文
17. 注入英文 AI 配音

### 5.2 推荐框架：LangGraph

**LangGraph 最适合这一具体工作流**，理由基于上述技术栈对比：

1. **长时、有状态、可恢复的设计。** 管道跨本地缓存清理、外部 MCP 服务器启动、浏览器自动化、两轮人工干预（重排、BGM 选择）与多个下游本地化分支。LangGraph 的 checkpointer/store 持久化模型正是为此而生——在某节点暂停（如"人工重排"）、经 `interrupt()` 无限等待人工输入、再精确从原处恢复，无需重跑上游步骤（[LangChain Docs](https://docs.langchain.com/oss/python/langgraph/persistence)、[crewship.dev](https://www.crewship.dev/learn/langgraph-memory)）。
2. **人在回路是一等原语，非外挂。** 步骤 6（人工重排）与 12（人工 BGM）需管道中途人工决策。LangGraph 显式支持为人工审批/输入中断图运行并稍后恢复——是其设计的核心模式（[LangChain](https://www.langchain.com/langgraph)）。
3. **原生异构 MCP 工具编排。** 管道须调用至少三个不同 MCP/工具面——OpenStoryline MCP（分镜、风格技能）、剪映/CapCut MCP（草稿组装、字幕、转场、TTS、导出）、浏览器自动化面（VS Code + Edge 插件）——外加后续本地化的翻译/TTS 服务。LangGraph 把每个作为图中可调用工具节点，显式边让异构工具间依赖可审计（[dev.to LangGraph 2.0 指南](https://dev.to/richard_dillon_b9c238186e/langgraph-20-the-definitive-guide-to-building-production-grade-ai-agents-in-2026-4j2b)）。
4. **本地化扇出的分支。** 步骤 14–17（三个封面比例、封面本地化、字幕翻译、英文 TTS 注入）构成天然**并行分支后汇合**模式——三个封面格式分支可并发，本地化分支（15–17）依赖已完成的英文字幕翻译。LangGraph 有向图模型原生表达并行路径与条件汇合，纯线性链（如更简单框架）无法干净表达（[LangChain](https://www.langchain.com/langgraph)）。
5. **护栏与质量门。** LangGraph 2.0 内置护栏节点直接适用于"每个分镜变速≤35 秒"这类硬约束（违例应阻断推进）与最终导出前的质量校验（[dev.to LangGraph 2.0 指南](https://dev.to/richard_dillon_b9c238186e/langgraph-20-the-definitive-guide-to-building-production-grade-ai-agents-in-2026-4j2b)）。
6. **恰是这类工具密集编排的生产履历。** LangGraph 规模化采用（月下载 9000 万、企业部署）证明它能可靠管理此类复杂、易失败、多工具生产管道（[AlphaBold](https://www.alphabold.com/langgraph-agents-in-production/)）。

**为何其他框架对此任务稍弱：**
- **CrewAI** 可用，但角色型"Crew"抽象拟合度较低——本管道不是"专家团队辩论方案"，而是**严格、大体确定性、带两个人工检查点与并行本地化扇出的工具调用流水线**。这正是 LangGraph 优化的"步骤间数据如何流转"问题，而 CrewAI 是"谁来做工作"的框架（[automely.ai](https://automely.ai/blogs/langchain-vs-langgraph-vs-crewai-ai-agent-framework)）。
- **OpenAI Agents SDK** 简洁与原生沙箱诱人，但缺 LangGraph 显式多分支图可视化与 17 步、多小时、可恢复生产管道所需的成熟长期 Store/Checkpoint 分离。
- **smolagents/Haystack** 是单点 AI 推理子任务（如写驱动 FFmpeg 变速的 `CodeAgent`）的好构件，但不提供 LangGraph 跨含外部进程依赖（剪映桌面、浏览器自动化）整条多小时管道的原生持久执行保证。
- **CutClaw 的 编剧+剪辑师+审查员 模式**是镜头规划子问题（步骤 4）的好概念模型，但本身非涵盖含本地化与多比例导出全 17 步生命周期的通用编排框架（[GVCLab/CutClaw](https://github.com/GVCLab/CutClaw)）。

### 5.3 编排架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                     LangGraph 编排器（根图）                          │
│  持久化：Postgres 后端 Checkpointer（线程级）+                      │
│          Store（跨运行复用"风格技能"/预设）                           │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
[1] clean_cache_node ──► shell/工具：清理缓存目录，校验磁盘空间
        │
        ▼
[2] launch_mcp_servers_node ──► 启动 OpenStoryline MCP
        │                        (python -m open_storyline.mcp.server)
        │                        + 剪映/CapCutAPI MCP
        │                        (python mcp_server.py, stdio)
        ▼
[3] browser_bridge_node ──► VS Code + Edge DevTools 插件作为
        │                    MCP 客户端/自动化桥，用于任何需浏览器
        │                    驱动的素材拉取步骤
        ▼
[4] import_and_split_node ──► OpenStoryline MCP 工具：
        │                     媒体导入 → 片段分段 →
        │                     内容理解 → shot_plan
        ▼
[5] import_draft_to_jianying_node ──► CapCutAPI/剪映 MCP：
        │                             create_draft() → add_video()
        │                             （草稿现可在剪映编辑）
        ▼
[6] 人在回路：manual_reorder_node
        │   LangGraph interrupt() —— 管道暂停；编辑者在剪映 UI
        │   手动重排分镜；人工"继续"后恢复
        ▼
[7] speed_fit_node（护栏节点：硬约束≤35 秒/分镜）
        │   遍历分镜 → 经 update_draft()/add_effect() 调整播放速度
        │   → 校验时长≤35 秒 → 违例则重试/重裁
        ▼
[8] add_subtitles_node ──► ASR（OpenStoryline local_asr 或 Whisper）
        │                  → add_subtitle() MCP 工具
        ▼
[9] transitions_effects_node ──► OpenStoryline"AI 转场生成"
        │                        （尾帧+首帧+自然语言描述）→ add_effect()
        ▼
[10] style_subtitles_node ──► add_text()/风格参数：字体、颜色、
        │                     描边、位置、动画
        ▼
[11] tts_stickers_node ──► add_sticker() + TTS 配音片段生成
        │                  → 作为动画贴纸附加
        ▼
[12] 人在回路：manual_bgm_node
        │   interrupt() —— 人工选/导入 BGM 轨道
        │   （可由 OpenStoryline 节拍同步推荐辅助）
        ▼
[13] adjust_volume_node ──► 经 update_draft() 调整每轨音量/ducking
        │
        ▼
[14] ─────────── 并行扇出（三条并发分支）───────────
     ├─ cover_9x16_node  ──► export_draft(ratio=9:16)
     ├─ cover_16x9_node  ──► export_draft(ratio=16:9)
     └─ cover_4x3_node   ──► export_draft(ratio=4:3)
                │  （LangGraph 并行路径；下方汇合屏障）
        ▼（join）
[15] localize_cover_en_node ──► 本地图像编辑/修复模型把封面文字
        │                       换为英文（无外部 API——本地模型/工具）
        ▼
[16] translate_subtitles_en_node ──► LLM 翻译字幕轨
        │                            → 更新英文字幕轨
        ▼
[17] inject_en_tts_node ──► 英文 AI TTS 语音合成 →
        │                   add_audio() 绑定到翻译后字幕
        ▼
   export_final_node ──► export_draft() 按比例最终渲染
                          + 护栏 QA 校验 → 完成
```

### 5.4 步骤映射到具体工具/MCP 服务器

| 步骤 | LangGraph 节点类型 | 底层工具/MCP 服务器 |
|---|---|---|
| 1. 清理缓存 | 工具节点（shell） | 本地文件系统清理脚本 |
| 2. 启动 OpenStoryline MCP | 启动节点 | `open_storyline.mcp.server`（[GitHub](https://github.com/FireRedTeam/FireRed-OpenStoryline)） |
| 3. VS Code Edge 插件 | 桥/工具节点 | 浏览器自动化 MCP 客户端（Edge DevTools + VS Code 扩展）做素材搜索/下载 |
| 4. 导入并分镜 | Agent + 工具节点 | OpenStoryline 媒体导入、分段、内容理解（[GitHub](https://github.com/FireRedTeam/FireRed-OpenStoryline)） |
| 5. 导入草稿到剪映 | 工具节点 | `create_draft`、`add_video` 经 CapCutAPI/剪映 MCP（[DeepWiki](https://deepwiki.com/ashreo/CapCutAPI/13.2-mcp-tool-reference)） |
| 6. 人工重排 | **人在回路 interrupt** | 剪映桌面 UI（人工直接编辑）；LangGraph `interrupt()` |
| 7. 变速≤35 秒 | 护栏节点 + 工具节点 | `update_draft`/effect 速度参数；自定义约束检查 |
| 8. 加字幕 | 工具节点 | ASR（OpenStoryline `local_asr`/Whisper）+ `add_subtitle` |
| 9. 转场/特效 | Agent + 工具节点 | OpenStoryline AI 转场生成 + `add_effect` |
| 10. 字幕花字/动画 | 工具节点 | `add_text` 样式参数 |
| 11. TTS 贴纸 | 工具 + 生成节点 | TTS 模型 + `add_sticker` |
| 12. 人工 BGM | **人在回路 interrupt** | 人工音频选择，可由 OpenStoryline 节拍同步建议辅助 |
| 13. 调整音量 | 工具节点 | `update_draft` 音频轨音量/ducking |
| 14. 封面 9:16/16:9/4:3 | 并行工具节点 | 不同分辨率 `export_draft` |
| 15. 封面本地化英文（本地） | 本地模型节点 | 本地图像编辑/修复模型（无外部 API 调用） |
| 16. 翻译字幕为英文 | LLM 节点 | 对字幕文本轨的翻译 LLM 调用 |
| 17. 注入英文 AI 配音 | 生成 + 工具节点 | 英文 TTS 模型 + 绑定到翻译后时间轴的 `add_audio` |

### 5.5 为何此编排模式奏效

- **确定性主干，智能岛屿。** 多数节点（5、7–11、13–17）是对 MCP 服务器的确定性工具调用——适合 LangGraph 显式图边。真正"智能"的推理（分镜质量、转场创意、翻译流畅）委派给子 Agent 或单点 LLM 调用置于特定节点内，保持外层控制流可审计同时仍利用 LLM 创造力。
- **护栏节点强制硬约束。** 步骤 7 的每分镜≤35 秒正是 LangGraph 2.0 护栏节点为放行前强制的不变量（[dev.to](https://dev.to/richard_dillon_b9c238186e/langgraph-20-the-definitive-guide-to-building-production-grade-ai-agents-in-2026-4j2b)）。
- **两个干净的人工检查点。** 在创意判断重要的地方（镜头顺序、音乐选择）不强制全自动，图经 `interrupt()` 在步骤 6、12 干净暂停，经 checkpointer 持久化状态，再从原处恢复——避免上游 MCP 调用的昂贵重跑（[LangChain Docs](https://docs.langchain.com/oss/python/langgraph/persistence)）。
- **数据允许处的并行。** 步骤 14 的三个封面比例导出相互独立，可作为并行图分支在汇合屏障进入本地化阶段前并发——LangGraph 图模型原生表达，纯线性或纯对话框架则别扭。
- **MCP 作为通用工具总线。** 因 OpenStoryline、剪映/CapCut 与（可选）翻译/TTS 服务都暴露为 MCP 服务器，编排 LangGraph Agent 只需一致的工具调用接口，而非每个服务一个定制 SDK——直接实现 MCP"用单一协议替代碎片化集成"的目标（[Anthropic](https://www.anthropic.com/news/model-context-protocol)）。
- **经 LangGraph Store 跨运行复用。** OpenStoryline 的"剪辑技能"归档概念（把完整剪辑工作流存为可复用技能，再换媒体批量复制风格）（[GitHub](https://github.com/FireRedTeam/FireRed-OpenStoryline)）天然映射到 LangGraph 长期 **Store**——编排器可把验证过的 17 步配置（转场风格、字幕样式、BGM 流派偏好）作为可复用跨线程状态为未来视频持久化，而 **Checkpointer** 管理每个视频运行的瞬时状态。

---

## 6. 总结建议

对于你的 17 步 AI 视频剪辑管道，**推荐 LangGraph 作为编排框架**，理由是：

1. 它是少数原生支持**可持久化、可恢复长时管道**（17 步、跨小时、跨外部进程）的开源框架——Checkpointer + Store 双层记忆直接对应"技能/MCP 工具的跨视频复用"与"单条视频的瞬时进度"。
2. **人在回路 `interrupt()`** 是一等原语，干净对应你的两个明确人工检查点（步骤 6 重排、步骤 12 BGM）以及步骤 16"若需在剪映界面操作就停下通知我"。
3. **并行分支**原生表达步骤 14 的三比例封面扇出与步骤 15–17 的本地化串行依赖。
4. **护栏节点**强制步骤 7 的"≤35 秒"硬约束。
5. **MCP 作为统一工具总线**让你把 OpenStoryline MCP、剪映/CapCut MCP、浏览器自动化桥、TTS/翻译服务统一在一个工具调用接口下，与你 17 步中大量 `/skill` 调用一一对应。

落地路径建议：先用 **FireRed-OpenStoryline** 的原生 MCP Server + Agent Skills 做步骤 1–4 与 8–10 的"智能岛屿"；用 **CapCutAPI/VectCutAPI** 的 11 个 MCP 工具做步骤 5、7、11、13–17 的确定性草稿操作；用 **VS Code + Edge DevTools 插件**做步骤 3 的浏览器桥；外层用 **LangGraph** 编排这 17 个节点为一张可恢复、可审计、带两个人工中断与一个并行扇出的有向图。若团队更偏无代码可视化，可退而用 **Dify/n8n** 做外层画布，但会牺牲 LangGraph 的细粒度状态机与持久执行保证。

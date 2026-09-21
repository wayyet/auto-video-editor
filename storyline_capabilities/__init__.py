"""storyline_capabilities:LangGraph 19 节点本地化代码库(plan_v4 §5 阶段 1~6)。

本目录是 plan_v4 Phase 4 auto-mode 19 节点本地化的"纯函数层",与 vendored
OpenStoryline copy 字节对齐。**每个模块提供无 FireRed 依赖的纯函数**,接受
项目内已有的 Pydantic 契约 / Path / dict 输入,返回符合
``storyline/contract.py`` 不变量约束的 dict / SourceMedia-like 对象。

设计纪律(plan §5 阶段 1 决策):
1. **纯函数化**:本目录不依赖 FireRed NodeState / ArtifactStore / ``llm.complete()``。
2. **强制走主 venv 时不引 torch**:TransNetV2 / funasr / TTS 等重依赖仍走 vendored
   venv 子进程;本目录只装 ``av / Pillow / httpx / pydantic`` 等无 torch 的库。
3. **LLM 接口用 Protocol 抽象**:vlm_client / llm_script_gen 等接受 ``Callable``
   接口注入(默认是 stub;测试用 fake),阶段 5+ 接入 Semantic Kernel / Polly 熔断。
4. **不引入第三方二进制**:BGM / 字体 / 脚本模板资源只放 YAML 描述,二进制文件
   用户自备(plan §6.1 决策:B)。

子模块:
- ``load_media``:扫描 video/image 取 metadata(av + Pillow,**无 torch**)
- ``split_shots``:镜头切分,TransNetV2 走 vendored venv;**本地**提供简化版
  ``frame_difference_split_shots`` 用于本地 CI 烟测
- ``search_media`` / ``search_web_topic``:Pexels HTTP API + 简化的网络主题检索
- ``prompts/``:从 vendored prompts/tasks 复制而来的双语 Prompt
- ``vlm_client`` / ``llm_script_gen``:可注入的 LLM 接口
"""

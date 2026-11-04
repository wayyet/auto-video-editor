"""FireRed-OpenStoryline ↔ auto-video-editor 集成层。

模块清单:
- ``contract``:Canonical Timeline / StorylinePlan 的 Pydantic 模型 + 校验。
- ``schemas``:JSON Schema 导出文件(放 canonical_timeline / storyline_plan)。
- ``firered_adapter``:从 FireRed-OpenStoryline 源码移植过来的工具注册表 + 节点元数据
  + 配置 schema 映射,在主项目内独立可用(不依赖 FireRed Python 包)。
- ``mapper``:Canonical Timeline(整数毫秒) ↔ 剪映 ``draft_content.json``(整数微秒) 映射。
- ``output_isolation``:`outputs/{job_id}/` 隔离 + 幂等键(``job_id + input_sha256 + config_sha256``)。

集成模式(阶段1~7 本地化后的现状):storyline 链的能力已全部本地化到
``storyline_capabilities/``(不再依赖 vendored OpenStoryline MCP server);
本包保留 Canonical Timeline 契约层 —— 节点产物(时间线 JSON)经 ``contract``
校验后由 ``mapper`` 翻译成剪映草稿。

子模块不在包导入时 eager 加载;按需 import 即可。
"""

from __future__ import annotations

__all__ = ["contract", "mapper", "output_isolation", "firered_adapter"]
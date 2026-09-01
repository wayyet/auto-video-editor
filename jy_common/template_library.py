"""VIP 资源模板库 — Week 3 步骤 9/10/11 共用(对应原文档 4.4 节 VIP 资源复用机制)。

Week 3:从 ``templates/fx_template.json`` / ``templates/text_style_template.json`` /
``templates/sticker_template.json`` 加载占位内容;真实 resource_id 留 Week 4
由人工产出后替换。

模板缺失时降级为"空库",调用方选择"不注入"路径通过(Week 3 可接受,见附件 9 节
"可承受延后")。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TemplateLibrary:
    """从 JSON 文件加载的模板库;Week 3 仅作占位资源 ID 容器。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TemplateLibrary":
        """加载模板库;文件不存在时降级为空库(便于单测)。"""
        path = Path(path)
        if not path.exists():
            return cls({"_missing": True, "_path": str(path)})
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @property
    def is_empty(self) -> bool:
        """模板缺失时为 True,调用方据此降级。"""
        return bool(self._data.get("_missing"))

    def pick_transition(self, style_tag: str) -> dict[str, Any]:
        """根据 style_tag 选择一个转场对象;无匹配时返回第一个或空 dict。"""
        transitions = self._data.get("transitions", [])
        if not transitions:
            return {}
        # Week 3 占位:仅按索引顺序循环,Week 4 替换为按 style_tag 匹配
        idx = abs(hash(style_tag)) % len(transitions)
        return dict(transitions[idx])

    def pick_video_effect(self, style_tag: str) -> dict[str, Any]:
        """选择一个视频特效对象;无匹配时返回空 dict。"""
        effects = self._data.get("video_effects", [])
        if not effects:
            return {}
        idx = abs(hash(style_tag)) % len(effects)
        return dict(effects[idx])

    def default_text_style(self) -> dict[str, Any]:
        """返回默认花字样式(Week 3 占位 — entrance_animation=None)。"""
        return dict(self._data.get("default_style", {}))


def load_template_library(path: str | Path) -> TemplateLibrary:
    """便捷构造器 — Week 3 各节点的统一入口。"""
    return TemplateLibrary.from_json_file(path)
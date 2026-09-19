"""VIP 资源模板库 — Week 3 步骤 9/10/11 共用(对应原文档 4.4 节 VIP 资源复用机制)。

设计:
- 从 ``templates/fx_template.json`` / ``templates/text_style_template.json`` 加载"风格索引";
- 从 ``templates/fx_resource_library.json`` / ``templates/text_resource_library.json`` 加载
  真实 VIP resource_id 库(由 ``scripts/build_resource_library.py`` 从 pyJianYingDraft
  metadata 一次性 dump 出来,详见对照报告 §4.2 / §5);
- 暴露"按 name 查 resource_id"的精确查询入口,给节点 9/10/11/13 等共用;
- 模板缺失时降级为"空库",调用方选择"不注入"路径通过。

兼容:
- 旧的 ``TemplateLibrary(data)`` 构造器仍可用,只是没有资源库能力;
- ``load_template_library(path)`` 仍可用,行为不变;
- 新增 ``from_json_files(fx_template, fx_resource, text_resource)`` 一次性加载全套餐,
  用于节点 9/10。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TemplateLibrary:
    """从 JSON 文件加载的模板库。

    单文件模式(旧):只持有 ``_data``,无资源库。
    多文件模式(新):同时持有 ``_data`` + ``_fx_lib`` + ``_text_lib``,支持按 name 查询
    resource_id 的精确入口。
    """

    def __init__(
        self,
        data: dict[str, Any],
        fx_lib: dict[str, Any] | None = None,
        text_lib: dict[str, Any] | None = None,
    ) -> None:
        self._data = data
        self._fx_lib = fx_lib or {}
        self._text_lib = text_lib or {}

    # ---------- 单文件加载(兼容旧 API) ----------

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TemplateLibrary":
        """加载模板库;文件不存在时降级为空库(便于单测)。"""
        path = Path(path)
        if not path.exists():
            return cls({"_missing": True, "_path": str(path)})
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def from_json_files(
        cls,
        fx_template: str | Path,
        fx_resource: str | Path,
        text_resource: str | Path,
    ) -> "TemplateLibrary":
        """一次性加载三件套:风格索引 + fx 资源库 + text 资源库。

        任一文件不存在:对应那一块就是空 dict(``_data`` 的 _missing=True 时标记整个 lib
        为空)。仅 fx_resource / text_resource 不存在时,主体仍可用,只是查询返回 None。
        """
        fx_template_path = Path(fx_template)
        if not fx_template_path.exists():
            data: dict[str, Any] = {"_missing": True, "_path": str(fx_template_path)}
        else:
            with open(fx_template_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        fx_lib: dict[str, Any] = {}
        fx_resource_path = Path(fx_resource)
        if fx_resource_path.exists():
            with open(fx_resource_path, "r", encoding="utf-8") as f:
                fx_lib = json.load(f)

        text_lib: dict[str, Any] = {}
        text_resource_path = Path(text_resource)
        if text_resource_path.exists():
            with open(text_resource_path, "r", encoding="utf-8") as f:
                text_lib = json.load(f)

        return cls(data, fx_lib=fx_lib, text_lib=text_lib)

    # ---------- 属性 ----------

    @property
    def is_empty(self) -> bool:
        """模板缺失时为 True,调用方据此降级。仅指 fx_template/text_style_template
        不存在的情况;fx_resource / text_resource 缺失不算 empty(只是查不到)。"""
        return bool(self._data.get("_missing"))

    @property
    def has_fx_lib(self) -> bool:
        return bool(self._fx_lib.get("transitions") or self._fx_lib.get("video_effects"))

    @property
    def has_text_lib(self) -> bool:
        return any(
            self._text_lib.get(k) for k in ("intro", "loop", "outro")
        )

    # ---------- 旧 API(兼容) ----------

    def pick_transition(self, style_tag: str) -> dict[str, Any]:
        """根据 style_tag 选择一个转场对象;无匹配时返回第一个或空 dict。

        Week 3 占位:按索引顺序循环 + style_tag hash 取模;Week 4 真实 ID 在节点 9 里
        通过 ``pick_transition_by_name`` 二次补齐。本方法保留旧语义。
        """
        transitions = self._data.get("transitions", [])
        if not transitions:
            return {}
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
        """返回默认花字样式(Week 3 占位 — entrance_animation=None)。

        Week 4 升级后 ``entrance_animation`` 会被节点 10 用 ``pick_text_animation``
        二次补齐。
        """
        return dict(self._data.get("default_style", {}))

    # ---------- 新 API:按 name 精确查询 ----------

    def pick_transition_by_name(self, name: str) -> dict[str, Any] | None:
        """精确按 name 查 VIP 转场,返回完整字段
        (name/is_vip/resource_id/effect_id/md5/default_duration_s/is_overlap)。

        未命中或 fx_lib 缺失 → 返回 None。
        """
        if not self._fx_lib:
            return None
        for t in self._fx_lib.get("transitions", []):
            if t.get("name") == name:
                return dict(t)
        return None

    def pick_video_effect_by_name(self, name: str) -> dict[str, Any] | None:
        """精确按 name 查 VIP 视频特效。"""
        if not self._fx_lib:
            return None
        for v in self._fx_lib.get("video_effects", []):
            if v.get("name") == name:
                return dict(v)
        return None

    def pick_text_animation(
        self, kind: str, name: str
    ) -> dict[str, Any] | None:
        """精确按 name 查 VIP 文字动画。``kind`` ∈ {"intro","loop","outro"}。"""
        if kind not in ("intro", "loop", "outro"):
            return None
        if not self._text_lib:
            return None
        for row in self._text_lib.get(kind, []):
            if row.get("name") == name:
                return dict(row)
        return None

    def list_transitions(self, vip_only: bool = True) -> list[str]:
        """返回资源库里所有 VIP(或全部)转场 name 列表 — 给选型 UI / debug 用。"""
        if not self._fx_lib:
            return []
        out: list[str] = []
        for t in self._fx_lib.get("transitions", []):
            if vip_only and not t.get("is_vip", False):
                continue
            name = t.get("name")
            if name is not None:
                out.append(name)
        return out

    def list_text_animations(
        self, kind: str, vip_only: bool = True
    ) -> list[str]:
        """返回资源库里所有 VIP(或全部)文字动画 name 列表。"""
        if kind not in ("intro", "loop", "outro"):
            return []
        if not self._text_lib:
            return []
        out: list[str] = []
        for row in self._text_lib.get(kind, []):
            if vip_only and not row.get("is_vip", False):
                continue
            name = row.get("name")
            if name is not None:
                out.append(name)
        return out


def load_template_library(path: str | Path) -> TemplateLibrary:
    """便捷构造器 — Week 3 各节点的统一入口(单文件模式)。"""
    return TemplateLibrary.from_json_file(path)


def load_resource_libraries(
    fx_template: str | Path,
    fx_resource: str | Path,
    text_resource: str | Path,
) -> TemplateLibrary:
    """便捷构造器 — Week 4 升级后,节点 9/10 的统一入口(多文件模式)。"""
    return TemplateLibrary.from_json_files(
        fx_template=fx_template,
        fx_resource=fx_resource,
        text_resource=text_resource,
    )

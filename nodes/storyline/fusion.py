"""human 模式融合:把 auto 模式的 21 个 storyline 节点,以"侧支"并入 human 主图。

设计要点(完整设计见《剧情线自动模式节点并入人工模式主图设计与执行计划》):

1. **各挂各的锚点**:每个 storyline 节点独立挂在一个已有 human 节点之后
   (``anchor -> storyline_xxx``),不集中放在同一处。侧支节点**没有出边**,
   所以不会改变任何 human 节点的执行顺序(按 LangGraph 超步语义)。
2. **只写新键(白名单)**:侧支只能写自己声明的 ``storyline_*`` 产物键 + ``status_log`` /
   ``error_log``。不会覆盖 human 主图已有字段(``storyline_plan`` /
   ``storyline_outputs_root`` / ``storyline_session_id`` 等)。
3. **缺前置就跳过,出错不外溢**:前置产物没有 -> 记一条 ``*_fusion_skipped`` 并返回;
   节点抛任何异常 -> 记 ``error_log``,不拖垮人工主链。
4. **产物隔离**:全部写到 ``<outputs>/<session_id>/fusion/``,不写 OpenStoryline 网页会话目录。

只在 ``STORYLINE_MODE=human`` 且 ``STORYLINE_HUMAN_FUSION=1`` 时由 ``graph.py`` 启用。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import config
from nodes.storyline._common import resolve_video_input_paths

logger = logging.getLogger(__name__)

# 白名单里永远允许的两个日志键(都有 append-only reducer,可并发写)
_LOG_KEYS = ("status_log", "error_log")


# ---------------------------------------------------------------------------
# "缺什么前置产物"检查函数:返回缺失项列表,空列表 = 可以运行
# ---------------------------------------------------------------------------
def _needs_keys(*keys: str) -> Callable[[Mapping[str, Any]], list[str]]:
    def _check(state: Mapping[str, Any]) -> list[str]:
        return [k for k in keys if not state.get(k)]

    return _check


def _query_of(state: Mapping[str, Any]) -> str:
    targets = state.get("storyline_targets")
    if isinstance(targets, dict):
        return str(targets.get("query") or "").strip()
    return ""


def _needs_query(state: Mapping[str, Any]) -> list[str]:
    return [] if _query_of(state) else ["storyline_targets.query"]


def _needs_query_and_pexels(state: Mapping[str, Any]) -> list[str]:
    # 注意:能力层读的是 PEXELS_API_KEY(graph.py 的旧路由读 STORYLINE_PEXELS_API_KEY,
    # 两者不一致 —— 见审计文档 P0-1)。融合版以能力层为准。
    missing = _needs_query(state)
    if not os.environ.get("PEXELS_API_KEY", "").strip():
        missing.append("PEXELS_API_KEY")
    return missing


def _needs_imported_video(state: Mapping[str, Any]) -> list[str]:
    """load_media 只在网页【导入视频】成功后才跑(守护 test_no_auto_import_video)。"""
    missing: list[str] = []
    if state.get("import_video_status") != "imported":
        missing.append("import_video_status=imported")
    if not resolve_video_input_paths(dict(state)):
        missing.append("video_input_paths")
    return missing


def _final_timeline_path(state: Mapping[str, Any]) -> Optional[str]:
    """最终时间线 = AI 转场版(若有)优先,否则普通版。

    plan_timeline_ai_transition 在融合版写独立键 ``storyline_timeline_plan_ai``
    (不覆盖 ``storyline_timeline_plan``),避免与回写整份 state 的 human 节点竞争同一键。
    """
    return state.get("storyline_timeline_plan_ai") or state.get("storyline_timeline_plan")


def _needs_final_timeline(state: Mapping[str, Any]) -> list[str]:
    p = _final_timeline_path(state)
    if not p or not Path(str(p)).exists():
        return ["storyline_timeline_plan(file)"]
    return []


# ---------------------------------------------------------------------------
# 输出后处理
# ---------------------------------------------------------------------------
def _qa_post(out: dict) -> dict:
    """融合版没有 qa_gate 的"重试回 group_clips"循环,'retry' 标签改名避免误导。"""
    out["status_log"] = [
        "storyline_qa_over_target" if t == "storyline_qa_retry" else t
        for t in out.get("status_log", [])
    ]
    return out


# ---------------------------------------------------------------------------
# 规格表
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FusionSpec:
    node_name: str
    anchor: str                       # 挂在哪个已有 human 节点之后(质检开启时)
    writes: tuple[str, ...]           # 允许写出的产物键(重命名之前的原始键)
    needs: Callable[[Mapping[str, Any]], list[str]]
    qc_off_anchor: Optional[str] = None   # ASSEMBLY_QC_GATE_ENABLED=false 时的备用锚点
    rename: Mapping[str, str] = field(default_factory=dict)  # 原始键 -> 对外发布的键
    use_final_timeline: bool = False  # 运行前把 storyline_timeline_plan 换成"最终时间线"
    post: Optional[Callable[[dict], dict]] = None


# 顺序即数据流顺序;锚点选择理由见设计文档"相关性表"。
FUSION_SPECS: tuple[FusionSpec, ...] = (
    FusionSpec("storyline_search_web_topic", "checkpoint0_storyline_plan",
               ("storyline_web_topic_artifact",), _needs_query),
    FusionSpec("storyline_load_media", "import_video",
               ("storyline_media_artifact",), _needs_imported_video),
    FusionSpec("storyline_search_media", "import_video",
               ("storyline_media_artifact",), _needs_query_and_pexels,
               rename={"storyline_media_artifact": "storyline_search_media_artifact"}),
    FusionSpec("storyline_split_shots", "assembly_discover_and_probe",
               ("storyline_shots_artifact",), _needs_keys("storyline_media_artifact"),
               qc_off_anchor="get_storyboard_plan"),
    FusionSpec("storyline_local_asr", "assembly_discover_and_probe",
               ("storyline_asr_artifact",), _needs_keys("storyline_media_artifact"),
               qc_off_anchor="get_storyboard_plan"),
    FusionSpec("storyline_understand_clips", "assembly_asr_and_visual_observe",
               ("storyline_understanding_artifact",),
               _needs_keys("storyline_media_artifact", "storyline_shots_artifact"),
               qc_off_anchor="generate_draft"),
    FusionSpec("storyline_speech_rough_cut", "assembly_asr_and_visual_observe",
               ("storyline_rough_cut_artifact",), _needs_keys("storyline_asr_artifact"),
               qc_off_anchor="generate_draft"),
    FusionSpec("storyline_filter_clips", "assembly_build_timeline",
               ("storyline_filtered_clips",), _needs_keys("storyline_understanding_artifact"),
               qc_off_anchor="node_06_human_reorder"),
    FusionSpec("storyline_group_clips", "node_06_human_reorder",
               ("storyline_groups_artifact",), _needs_keys("storyline_filtered_clips"),
               qc_off_anchor="bridge_snapshot2"),
    FusionSpec("storyline_generate_script", "node_08_add_subtitles",
               ("storyline_script_artifact",), _needs_keys("storyline_groups_artifact")),
    FusionSpec("storyline_script_template_recommendation", "node_08_add_subtitles",
               (), _needs_keys("storyline_groups_artifact")),
    FusionSpec("storyline_recommend_text", "node_10_inject_text_fx",
               ("storyline_text_style_plan",), _needs_keys("storyline_groups_artifact")),
    FusionSpec("storyline_generate_voiceover", "node_17_inject_english_tts",
               ("storyline_voiceover_artifact",), _needs_keys("storyline_script_artifact")),
    FusionSpec("storyline_select_bgm", "node_17_inject_english_tts",
               ("storyline_bgm_selection",), _needs_keys("storyline_groups_artifact")),
    FusionSpec("storyline_recommend_transition", "node_09_inject_fx",
               ("storyline_transition_plan",), _needs_keys("storyline_groups_artifact")),
    FusionSpec("storyline_generate_ai_transition", "node_12_human_add_bgm",
               ("storyline_ai_transition_artifact",), _needs_keys("storyline_transition_plan")),
    FusionSpec("storyline_plan_timeline_pro", "node_12_human_add_bgm",
               ("storyline_timeline_plan",),
               _needs_keys("storyline_groups_artifact", "storyline_media_artifact")),
    FusionSpec("storyline_plan_timeline_ai_transition", "node_13_adjust_volume",
               ("storyline_timeline_plan",), _needs_keys("storyline_groups_artifact"),
               rename={"storyline_timeline_plan": "storyline_timeline_plan_ai"}),
    FusionSpec("storyline_render_video", "join_before_delivery",
               ("storyline_render_smoke_test_path",), _needs_final_timeline,
               use_final_timeline=True),
    FusionSpec("storyline_qa_gate", "join_before_delivery",
               (), _needs_final_timeline, use_final_timeline=True, post=_qa_post),
    FusionSpec("storyline_join", "join_before_delivery",
               ("storyline_plan",), _needs_final_timeline, use_final_timeline=True,
               rename={"storyline_plan": "storyline_auto_plan"}),
)

_SPEC_BY_NAME: dict[str, FusionSpec] = {s.node_name: s for s in FUSION_SPECS}
FUSION_NODE_NAMES: tuple[str, ...] = tuple(s.node_name for s in FUSION_SPECS)


def anchor_of(spec: FusionSpec, qc_enabled: bool) -> str:
    """质检开启用 ``anchor``;关闭(assembly_* 不接边)时优先用备用锚点。"""
    if not qc_enabled and spec.qc_off_anchor:
        return spec.qc_off_anchor
    return spec.anchor


def fusion_layout(qc_enabled: bool = True) -> dict[str, list[str]]:
    """``{锚点: [挂在它旁边的 storyline 节点,...]}``,保持规格表顺序。"""
    out: dict[str, list[str]] = {}
    for s in FUSION_SPECS:
        out.setdefault(anchor_of(s, qc_enabled), []).append(s.node_name)
    return out


# ---------------------------------------------------------------------------
# 包装
# ---------------------------------------------------------------------------
def fusion_outputs_root(state: Mapping[str, Any]) -> Path:
    """融合产物根目录:``<outputs>/<session_id>/fusion``。

    故意**不**用 ``state.storyline_outputs_root``:node_04b 会把它设成
    OpenStoryline 网页会话目录,往里写会污染网页读取的产物。
    """
    job_id = str(state.get("session_id") or "unknown")
    return Path(config.storyline_outputs_root()) / job_id / "fusion"


def _delta(base: list, new: Optional[list]) -> list:
    """把节点返回的"整份日志"(旧日志 + 新条目)还原成只含新条目的增量。"""
    base = list(base or [])
    new = list(new or [])
    if new[: len(base)] == base:
        return new[len(base):]
    return [x for x in new if x not in base]


def _publish(spec: FusionSpec, state: Mapping[str, Any], out: Mapping[str, Any]) -> dict:
    allowed = set(spec.writes)
    published: dict[str, Any] = {}
    for key, value in out.items():
        if key in _LOG_KEYS:
            continue
        if key in allowed:
            published[spec.rename.get(key, key)] = value
    published["status_log"] = _delta(state.get("status_log"), out.get("status_log"))
    errors = _delta(state.get("error_log"), out.get("error_log"))
    if errors:
        published["error_log"] = errors
    if spec.post is not None:
        published = spec.post(published)
    return published


def wrap_node(node_name: str, fn: Callable[[Any], Optional[dict]]) -> Callable[[Any], dict]:
    """给原 storyline 节点函数套上"白名单 + 跳过 + 异常隔离 + 产物隔离"。"""
    spec = _SPEC_BY_NAME[node_name]

    def _fusion_node(state):  # 不写类型注解:让 LangGraph 传入完整 state
        if node_name in config.STORYLINE_FUSION_DISABLE:
            return {"status_log": [f"{node_name}_fusion_disabled"]}
        try:
            missing = spec.needs(state)
            if missing:
                return {"status_log": [f"{node_name}_fusion_skipped:{'+'.join(missing)}"]}
            iso = dict(state)
            iso["storyline_outputs_root"] = str(fusion_outputs_root(state))
            if spec.use_final_timeline:
                iso["storyline_timeline_plan"] = _final_timeline_path(state)
            out = fn(iso) or {}
        except Exception as e:  # noqa: BLE001 — 侧支失败不得拖垮人工主链
            logger.warning("fusion node %s failed: %r", node_name, e)
            return {
                "status_log": [f"{node_name}_fusion_failed"],
                "error_log": [f"[fusion:{node_name}] {type(e).__name__}: {e}"],
            }
        return _publish(spec, state, out)

    _fusion_node.__name__ = f"{node_name}_fusion"
    _fusion_node.__qualname__ = f"{node_name}_fusion"
    return _fusion_node


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------
def wire_human_fusion(g, *, qc_enabled: bool) -> None:
    """给 ``StateGraph`` 加 21 条 ``锚点 -> storyline 节点`` 的边(节点须已注册)。"""
    for spec in FUSION_SPECS:
        anchor = anchor_of(spec, qc_enabled)
        for name in (anchor, spec.node_name):
            if name not in g.nodes:
                raise ValueError(f"融合接线失败:图里没有节点 {name!r}")
        g.add_edge(anchor, spec.node_name)


__all__ = [
    "FUSION_NODE_NAMES",
    "FUSION_SPECS",
    "FusionSpec",
    "anchor_of",
    "fusion_layout",
    "fusion_outputs_root",
    "wire_human_fusion",
    "wrap_node",
]

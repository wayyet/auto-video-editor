"""Assembly QC 通道节点(plan §7.1-7.6)。

按 nodes/storyline/node_understand_clips.py 范式:
- 签名 ``def xxx_node(state: WorkflowState) -> dict``
- 先取上游产物路径 → 缺失就 ``CONTRACT_INVALID`` 错误提前返回
- try/except 包住 ``assembly_capabilities`` 调用
- 写产物 JSON → 返回新字段 + status_log
- helper 直接复用 ``nodes/storyline/_common.py``(_resolve_outputs_root /
  append_status_tag / append_error)
"""
from .node_discover_and_probe import assembly_discover_and_probe_node
from .node_asr_and_visual_observe import assembly_asr_and_visual_observe_node
from .node_build_timeline import assembly_build_timeline_node
from .node_validate_render_qc import (
    assembly_validate_render_qc_node,
    route_after_assembly_qc,
)
from .node_repair_loop import assembly_repair_loop_node
from .node_write_report import assembly_write_report_node

__all__ = [
    "assembly_discover_and_probe_node",
    "assembly_asr_and_visual_observe_node",
    "assembly_build_timeline_node",
    "assembly_validate_render_qc_node",
    "assembly_repair_loop_node",
    "assembly_write_report_node",
    "route_after_assembly_qc",
]

"""P1 目标时长单一来源 — 锁定 config 唯一定义与各处引用一致(决定 1 选 B)。

对应《docs/第三期开发/相似功能节点分析与设计执行计划.md》§5.2 / §6-P1:
- 唯一权威定义:``config.DEFAULT_TARGET_DURATION_MS``(35000);
- ``TARGET_DURATION_US`` / 控制台默认值均由它派生;
- storyline 节点层统一走 ``nodes/storyline/_common.py``;
- ``qa_gate._target_ms`` 与 ``_common.target_duration_ms`` 解析规则一致
  (缺省/非正数/非法值回落默认 = 本阶段统一后的新行为);
- 能力层 ``storyline_capabilities/plan_timeline_pro.py`` 保留自己的 35_000
  兜底(能力层不 import 根目录 config 的既有纪律),由本文件锁住与 config 一致;
- node_07(决定 1 选 B)运行时读 ``state.storyline_targets.target_duration_ms``,
  其缺省(无 targets → 35s)行为由 test_node_07_speed_fit.py 既有用例锁定。
"""

from __future__ import annotations

from typing import Any

import config
from graph import escalate_guardrail_failure
from nodes.storyline import _common
from nodes.storyline._common import target_duration_ms
from nodes.storyline.qa_gate import _target_ms
from storyline_capabilities.plan_timeline_pro import plan_timeline_pro

DEFAULT = config.DEFAULT_TARGET_DURATION_MS


# ---------------------------------------------------------------------------
# 1. config 是唯一权威定义
# ---------------------------------------------------------------------------
def test_config_is_the_single_authority() -> None:
    assert config.DEFAULT_TARGET_DURATION_MS == 35000
    assert config.TARGET_DURATION_US == config.DEFAULT_TARGET_DURATION_MS * 1000
    assert config.CONSOLE_TARGET_DURATION_MS_DEFAULT == config.DEFAULT_TARGET_DURATION_MS


def test_common_reexports_config_constant() -> None:
    """/_common 的名字必须是 config 同一对象(re-export,不是第二份定义)。"""
    assert _common.DEFAULT_TARGET_DURATION_MS is config.DEFAULT_TARGET_DURATION_MS


# ---------------------------------------------------------------------------
# 2. _common.target_duration_ms 解析规则(节点层唯一解析入口)
# ---------------------------------------------------------------------------
def test_common_parse_falls_back_to_default() -> None:
    for state in (
        {},
        {"storyline_targets": None},
        {"storyline_targets": {}},
        {"storyline_targets": {"target_duration_ms": None}},
        {"storyline_targets": {"target_duration_ms": 0}},
        {"storyline_targets": {"target_duration_ms": -5}},
        {"storyline_targets": {"target_duration_ms": "abc"}},
    ):
        assert target_duration_ms(state) == DEFAULT, state


def test_common_parse_passes_valid_values_through() -> None:
    assert target_duration_ms({"storyline_targets": {"target_duration_ms": 60000}}) == 60000
    assert target_duration_ms({"storyline_targets": {"target_duration_ms": "65000"}}) == 65000
    # 显式 default 参数仍生效
    assert target_duration_ms({}, default=12345) == 12345


# ---------------------------------------------------------------------------
# 3. qa_gate 与 _common 解析规则一致(非正数回落默认 = P1 统一后的新行为)
# ---------------------------------------------------------------------------
def test_qa_gate_uses_common_rules() -> None:
    assert _target_ms({}) == DEFAULT
    # 旧实现:0 / -5 原样返回;统一后:回落默认。锁定新行为。
    assert _target_ms({"storyline_targets": {"target_duration_ms": 0}}) == DEFAULT
    assert _target_ms({"storyline_targets": {"target_duration_ms": -5}}) == DEFAULT
    assert _target_ms({"storyline_targets": {"target_duration_ms": "abc"}}) == DEFAULT
    assert _target_ms({"storyline_targets": {"target_duration_ms": 65000}}) == 65000


# ---------------------------------------------------------------------------
# 4. 能力层 35_000 兜底与 config 一致(刻意复制 + 防漂移测试)
# ---------------------------------------------------------------------------
def _minimal_group() -> list[dict[str, Any]]:
    return [
        {
            "group_id": "group_0001",
            "clips": [
                {
                    "clip_id": "clip_0001",
                    "media_id": "media_0001",
                    "source_in_ms": 0,
                    "source_out_ms": 3000,
                }
            ],
            "captions": ["画面一"],
        }
    ]


def test_capability_fallback_matches_config() -> None:
    # 能力层兜底写法 ``int(x or 0) or 35_000`` 只兜假值(缺省/0);
    # 文档 §5.2 只要求锁"空 targets 时与 config 一致"。负数透传是能力层
    # 既有行为,P1 不改能力层;非正数归一由 _common/qa_gate 负责(见上)。
    for targets in ({}, {"target_duration_ms": 0}):
        out = plan_timeline_pro(groups=_minimal_group(), targets=targets)
        assert out["options"]["max_duration_ms"] == DEFAULT, targets


def test_capability_passes_valid_target_through() -> None:
    out = plan_timeline_pro(groups=_minimal_group(), targets={"target_duration_ms": 65000})
    assert out["options"]["max_duration_ms"] == 65000


# ---------------------------------------------------------------------------
# 5. 升级文案随 state 目标动态化(决定 1 选 B)
# ---------------------------------------------------------------------------
def test_escalate_message_reflects_state_target() -> None:
    out = escalate_guardrail_failure(
        {"storyline_targets": {"target_duration_ms": 65000}, "error_log": [], "status_log": []}
    )
    assert "65s" in out["error_log"][-1]
    assert "guardrail_failed" in out["status_log"]


def test_escalate_message_default_is_35s() -> None:
    out = escalate_guardrail_failure({"error_log": [], "status_log": []})
    assert "35s" in out["error_log"][-1]

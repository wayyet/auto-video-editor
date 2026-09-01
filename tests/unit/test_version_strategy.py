"""draft_ops.version_strategy 单测(附件 2.5 节)。"""

from __future__ import annotations

from draft_ops.version_strategy import VersionStrategy, resolve_strategy


def test_strategy_a_for_v590() -> None:
    assert resolve_strategy("5.9.0") == VersionStrategy.STRATEGY_A_VERSION_LOCK


def test_strategy_b_for_other_versions() -> None:
    assert resolve_strategy("6.0.0") == VersionStrategy.STRATEGY_B_ONEWAY_WRITE
    assert resolve_strategy("6.5.1") == VersionStrategy.STRATEGY_B_ONEWAY_WRITE
    assert resolve_strategy("") == VersionStrategy.STRATEGY_B_ONEWAY_WRITE
    assert resolve_strategy("5.8.0") == VersionStrategy.STRATEGY_B_ONEWAY_WRITE

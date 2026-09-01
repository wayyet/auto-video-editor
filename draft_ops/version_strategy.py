"""剪映版本 → 写入策略 解析。

设计依据(附件 2.3 节):
- 策略甲(STRATEGY_A_VERSION_LOCK):全局版本锁定,要求本机剪映 v5.9.0(明文
  UTF-8 JSON)且 hosts 已屏蔽升级域名(Week 1 交付物)。pyJianYingDraft 可
  读写明文 draft_content.json。
- 策略乙(STRATEGY_B_ONEWAY_WRITE):单向明文写入渲染。pyJianYingDraft 全新
  创建未加密草稿;剪映一旦打开该草稿触发加密,工作流后续只读不写。

Week 2 默认走策略甲;策略乙作为降级预案保留。
"""

from __future__ import annotations

from enum import Enum


class VersionStrategy(Enum):
    STRATEGY_A_VERSION_LOCK = "strategy_a_version_lock"
    STRATEGY_B_ONEWAY_WRITE = "strategy_b_oneway_write"


def resolve_strategy(jianying_version: str) -> VersionStrategy:
    """根据剪映版本号选择写入策略。

    Args:
        jianying_version: 形如 "5.9.0"、"6.0.0"、"6.5.1"。

    Returns:
        VersionStrategy.STRATEGY_A_VERSION_LOCK 当 jianying_version == "5.9.0",
        否则 STRATEGY_B_ONEWAY_WRITE。
    """
    if jianying_version == "5.9.0":
        return VersionStrategy.STRATEGY_A_VERSION_LOCK
    return VersionStrategy.STRATEGY_B_ONEWAY_WRITE

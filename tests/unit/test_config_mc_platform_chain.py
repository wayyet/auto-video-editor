"""单测:MC 平台回退链配置解析(2026-11-21 审查修订版 P2/§6.2)。

直测 ``config._parse_mc_platform_chain`` 纯函数(导入期解析逻辑本体),
覆盖 §6.2 全部场景:空串关闭/小写归一/空项/未验证平台/重复/主平台重复。
"""
from __future__ import annotations

import pytest

import config as config_mod

_WL = ("xhs", "dy")


def test_chain_single_platform() -> None:
    assert config_mod._parse_mc_platform_chain("dy", _WL, "xhs") == ("dy",)


def test_chain_empty_string_disables_fallback() -> None:
    assert config_mod._parse_mc_platform_chain("", _WL, "xhs") == ()


def test_chain_whitespace_and_case_normalized() -> None:
    assert config_mod._parse_mc_platform_chain(" DY ", _WL, "xhs") == ("dy",)


def test_chain_order_preserved() -> None:
    wl = ("xhs", "dy", "ks")
    assert config_mod._parse_mc_platform_chain("dy,ks", wl, "xhs") == ("dy", "ks")
    assert config_mod._parse_mc_platform_chain("ks,dy", wl, "xhs") == ("ks", "dy")


def test_chain_empty_item_rejected() -> None:
    with pytest.raises(ValueError, match="空项"):
        config_mod._parse_mc_platform_chain("dy,,ks", _WL, "xhs")


def test_chain_unknown_platform_rejected() -> None:
    with pytest.raises(ValueError, match="未验证平台"):
        config_mod._parse_mc_platform_chain("unknown", _WL, "xhs")


def test_chain_duplicate_rejected() -> None:
    with pytest.raises(ValueError, match="重复"):
        config_mod._parse_mc_platform_chain("dy,dy", _WL, "xhs")


def test_chain_primary_platform_rejected() -> None:
    with pytest.raises(ValueError, match="主平台"):
        config_mod._parse_mc_platform_chain("xhs", _WL, "xhs")


def test_config_default_fallback_chain_is_dy() -> None:
    """默认配置(未设 env)= xhs → dy → Bing(审查修订版 §4.1)。"""
    assert config_mod.STORYLINE_WEB_TOPIC_MC_PLATFORM == "xhs"
    assert config_mod.STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS == ("dy",)


def test_config_chain_timeout_defaults_and_range() -> None:
    """整链总预算默认 600s,与单平台 TIMEOUT 独立(审查修订版 §3.5)。"""
    assert config_mod.STORYLINE_WEB_TOPIC_MC_CHAIN_TIMEOUT == 600
    assert 30 <= config_mod.STORYLINE_WEB_TOPIC_MC_CHAIN_TIMEOUT <= 1800


def test_config_whitelist_includes_dy() -> None:
    assert config_mod._MC_PLATFORM_WHITELIST == ("xhs", "dy")

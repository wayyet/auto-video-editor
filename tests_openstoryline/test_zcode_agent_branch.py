"""阶段 E:``build_agent()`` 的 zcode 分支行为测试。

与 ``test_agent_mcode_branch.py`` 同款策略(把 ``create_agent`` 换成假实现抓参数):
- zcode 分支**跳过** LLM / VLM 两处 ``validate_api_key``
- zcode 分支**不连 8001**(不构造 ``MultiServerMCPClient``)
- zcode 分支 ``tools=[]``、**不注册** LangChain skills 工具(不重复挂 video-agent)
- ``ZCode Agent`` 档位 -> ``ZCodeChatModel``,配置项要真的传到模型上
- 工作区没配 / 不存在 / bin 解析不到 / 自检不过 -> 明确的配置错误
- **普通模型分支一字未改**
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from open_storyline import agent as agent_mod
from open_storyline.agent import build_agent, is_zcode_model
from open_storyline.config import Settings, ZCodeConfig, load_settings
from open_storyline.storage.agent_memory import ArtifactStore
from open_storyline.zcode_agent_adapter import (
    ZCodeChatModel,
    ZCodeConfigError,
    zcode_selfcheck,
)

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _base_settings() -> Settings:
    return load_settings(OPENSTORYLINE_DIR / "config.toml")


def _cfg_with_zcode(cfg: Settings, **zcode_kwargs: Any) -> Settings:
    """把 ``[zcode]`` 段替换掉,其余段原样保留。"""
    return cfg.model_copy(update={"zcode": ZCodeConfig(**zcode_kwargs)})


@pytest.fixture
def zcode_workspace(tmp_path) -> Path:
    """zcode 工作区目录(zcode 不要求 .mcp.json / AGENTS.md)。"""
    d = tmp_path / "zcode-workspace"
    d.mkdir()
    return d


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "outputs", session_id="sess-zcode")


@pytest.fixture
def captured(monkeypatch) -> Dict[str, Any]:
    calls: Dict[str, Any] = {}

    def _fake_create_agent(**kwargs: Any):
        calls.update(kwargs)
        return SimpleNamespace(kind="fake-agent")

    monkeypatch.setattr(agent_mod, "create_agent", _fake_create_agent)
    return calls


@pytest.fixture
def forbid_api_key_check(monkeypatch) -> list:
    calls: list = []

    async def _boom(*args: Any, **kwargs: Any):
        calls.append(args)
        raise AssertionError("zcode 分支不应该做 API key 校验")

    monkeypatch.setattr(agent_mod, "validate_api_key", _boom)
    return calls


@pytest.fixture
def forbid_mcp_client(monkeypatch) -> list:
    calls: list = []

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any):
            calls.append(args)
            raise AssertionError("zcode 分支不应该连 8001 的 MCP server")

    monkeypatch.setattr(agent_mod, "MultiServerMCPClient", _Boom)
    return calls


@pytest.fixture
def forbid_load_skills(monkeypatch) -> list:
    calls: list = []

    async def _boom(*args: Any, **kwargs: Any):
        calls.append(args)
        raise AssertionError("zcode 分支不应该注册 LangChain skills 工具")

    monkeypatch.setattr(agent_mod, "load_skills", _boom)
    return calls


# --------------------------------------------------------------------------
# is_zcode_model
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("ZCode Agent", True),
        ("zcode-agent", True),
        ("zcode", True),
        ("  ZCode Agent  ", True),
        ("mcode-operator", False),
        ("GLM API / BigModel", False),
        ("deepseek-v4-pro", False),
        ("__custom__", False),
        ("", False),
        (None, False),
    ],
)
def test_is_zcode_model(name, expected):
    assert is_zcode_model(name) is expected


# --------------------------------------------------------------------------
# zcode 分支
# --------------------------------------------------------------------------

async def test_zcode_branch_skips_api_key_validation(
    zcode_workspace, store, captured, forbid_api_key_check, forbid_mcp_client,
    forbid_load_skills, fake_zcode_cli,
):
    """zcode 分支不调用 LLM / VLM 的 ``validate_api_key``(认证走 CLI 登录态)。"""
    cli = fake_zcode_cli()
    cfg = _cfg_with_zcode(
        _base_settings(), cwd=str(zcode_workspace), bin=cli.path
    )

    agent, node_manager = await build_agent(
        cfg=cfg,
        session_id="sess-zcode",
        store=store,
        llm_override={"model": "ZCode Agent"},
    )

    assert forbid_api_key_check == [], "zcode 分支竟然做了 API key 校验"
    assert forbid_mcp_client == [], "zcode 分支竟然构造了 MultiServerMCPClient"
    assert forbid_load_skills == [], "zcode 分支竟然调用了 load_skills()"
    assert agent is not None
    assert node_manager is not None
    assert captured["tools"] == [], f"tools 应该是空列表,实际 {captured['tools']!r}"


async def test_zcode_branch_uses_zcode_model(
    zcode_workspace, store, captured, fake_zcode_cli
):
    """``ZCode Agent`` 档位 -> ``ZCodeChatModel``,cwd/session 落对。"""
    cli = fake_zcode_cli()
    cfg = _cfg_with_zcode(
        _base_settings(), cwd=str(zcode_workspace), bin=cli.path
    )

    await build_agent(
        cfg=cfg,
        session_id="sess-zcode",
        store=store,
        llm_override={"model": "ZCode Agent"},
    )

    model = captured["model"]
    assert isinstance(model, ZCodeChatModel), f"模型类型不对: {type(model)!r}"
    assert model.cwd == str(zcode_workspace.resolve())
    assert model.session_id == "sess-zcode"
    assert model.mode == cfg.zcode.mode


async def test_zcode_branch_passes_config_to_model(
    zcode_workspace, store, captured, fake_zcode_cli
):
    """配置项要真的传到模型上(不是写死的默认值)。"""
    cli = fake_zcode_cli()
    cfg = _cfg_with_zcode(
        _base_settings(),
        cwd=str(zcode_workspace),
        bin=cli.path,
        mode="plan",
        timeout_s=42,
        max_prompt_chars=1234,
        status_tick_s=0.0,
        resume_sessions=False,
        history_turns=3,
    )

    await build_agent(
        cfg=cfg,
        session_id="sess-zcode",
        store=store,
        llm_override={"model": "ZCode Agent"},
    )

    model = captured["model"]
    assert isinstance(model, ZCodeChatModel)
    assert model.mode == "plan"
    assert model.timeout_s == 42
    assert model.max_prompt_chars == 1234
    assert model.status_tick_s == 0.0
    assert model.resume_sessions is False
    assert model.history_turns == 3


async def test_zcode_branch_providers_reach_model(
    zcode_workspace, store, captured, fake_zcode_cli
):
    """sink provider / resume id provider 要真的传到模型上。"""
    cli = fake_zcode_cli()
    cfg = _cfg_with_zcode(
        _base_settings(), cwd=str(zcode_workspace), bin=cli.path
    )
    sink_marker = object()

    await build_agent(
        cfg=cfg,
        session_id="sess-zcode",
        store=store,
        llm_override={"model": "ZCode Agent"},
        zcode_event_sink_provider=lambda: sink_marker,
        zcode_resume_id_provider=lambda: "sess_prev",
    )

    model = captured["model"]
    assert callable(model.event_sink_provider)
    assert model.event_sink_provider() is sink_marker
    assert model.resume_id_provider() == "sess_prev"


async def test_zcode_branch_requires_configured_cwd(store, captured, fake_zcode_cli):
    """``cwd`` 没配 -> 明确的配置错误(不静默回退)。"""
    cli = fake_zcode_cli()
    cfg = _cfg_with_zcode(_base_settings(), cwd="", bin=cli.path)

    with pytest.raises(ZCodeConfigError):
        await build_agent(
            cfg=cfg,
            session_id="sess-zcode",
            store=store,
            llm_override={"model": "ZCode Agent"},
        )

    assert captured == {}, "配置错误时不该调用 create_agent"


async def test_zcode_branch_requires_existing_cwd(
    tmp_path, store, captured, fake_zcode_cli
):
    """``cwd`` 指向不存在的目录 -> 明确的配置错误。"""
    cli = fake_zcode_cli()
    cfg = _cfg_with_zcode(
        _base_settings(), cwd=str(tmp_path / "nope"), bin=cli.path
    )

    with pytest.raises(ZCodeConfigError, match="不存在"):
        await build_agent(
            cfg=cfg,
            session_id="sess-zcode",
            store=store,
            llm_override={"model": "ZCode Agent"},
        )


async def test_zcode_branch_requires_resolvable_bin(
    zcode_workspace, store, captured
):
    """bin 解析不到 -> 明确的配置错误。"""
    cfg = _cfg_with_zcode(
        _base_settings(), cwd=str(zcode_workspace), bin="definitely-not-zcode-xyz"
    )

    with pytest.raises(ZCodeConfigError, match="zcode"):
        await build_agent(
            cfg=cfg,
            session_id="sess-zcode",
            store=store,
            llm_override={"model": "ZCode Agent"},
        )

    assert captured == {}, "自检失败时不该调用 create_agent"


async def test_zcode_branch_fails_when_help_flag_missing(
    zcode_workspace, store, captured, fake_zcode_cli
):
    """``--help`` 缺 ``-p, --prompt`` 旗标 -> 自检不过(§4.2 第二级前提 2)。"""
    cli = fake_zcode_cli(help_text="Usage:\n  --something-else\n")
    cfg = _cfg_with_zcode(
        _base_settings(), cwd=str(zcode_workspace), bin=cli.path
    )

    with pytest.raises(ZCodeConfigError, match="-p"):
        await build_agent(
            cfg=cfg,
            session_id="sess-zcode",
            store=store,
            llm_override={"model": "ZCode Agent"},
        )


# --------------------------------------------------------------------------
# 启动自检(§4.2 第二级前提 2)
# --------------------------------------------------------------------------

def test_selfcheck_ok_with_fake_cli(fake_zcode_cli):
    cli = fake_zcode_cli()
    ok, reason = zcode_selfcheck(cli.path, force=True)
    assert ok, f"自检应该通过: {reason}"


def test_selfcheck_fails_when_flag_missing(fake_zcode_cli):
    cli = fake_zcode_cli(help_text="Usage:\n  --no-prompt-here\n")
    ok, reason = zcode_selfcheck(cli.path, force=True)
    assert not ok
    assert "-p" in reason


def test_selfcheck_fails_on_broken_cli(fake_zcode_cli):
    cli = fake_zcode_cli(version="")  # --version 输出为空 -> 视为损坏安装
    ok, reason = zcode_selfcheck(cli.path, force=True)
    assert not ok
    assert "--version" in reason


def test_selfcheck_caches_success(fake_zcode_cli):
    cli = fake_zcode_cli()
    ok1, _ = zcode_selfcheck(cli.path, force=True)
    ok2, _ = zcode_selfcheck(cli.path)  # 走缓存
    assert ok1 and ok2

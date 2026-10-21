"""阶段 C:``build_agent()`` 的 mcode 分支行为测试。

关键断言(与计划 §5.3 一一对应):
- mcode 分支**跳过** LLM / VLM 两处 ``validate_api_key``
- mcode 分支**不连 8001**(不构造 ``MultiServerMCPClient``)
- mcode 分支``tools=[]``、**不注册** LangChain skills 工具
- ``mcode-operator`` -> standard 模式;``mcode-chat`` -> lightweight 模式
- 操作员目录没配 / 缺 ``.mcp.json`` / 缺 ``AGENTS.md`` -> 明确的配置错误
- **普通模型分支一字未改**:MCP 连不上时仍然照旧失败,不静默降级

策略:把 ``create_agent`` 换成假实现抓参数。这样断言的是「我们传给
LangChain 的东西」,而不是 LangChain 内部怎么存 —— 更稳,也不会误伤。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from open_storyline import agent as agent_mod
from open_storyline.agent import MCodeConfigError, build_agent
from open_storyline.config import MCodeConfig, Settings, load_settings
from open_storyline.mcode_chat_model import MCodeChatModel
from open_storyline.storage.agent_memory import ArtifactStore

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _base_settings() -> Settings:
    """从真实 config.toml 载入一份合法 Settings(测试只改 mcode 段)。"""
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    return cfg


def _cfg_with_mcode(cfg: Settings, **mcode_kwargs: Any) -> Settings:
    """把 ``[mcode]`` 段替换掉,其余段原样保留。"""
    return cfg.model_copy(update={"mcode": MCodeConfig(**mcode_kwargs)})


@pytest.fixture
def operator_dir(tmp_path) -> Path:
    """一个合法的操作员目录:有 ``.mcp.json`` 和 ``AGENTS.md``。"""
    d = tmp_path / "video-ops"
    d.mkdir()
    (d / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (d / "AGENTS.md").write_text("# operator\n", encoding="utf-8")
    return d


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "outputs", session_id="sess-mcode")


@pytest.fixture
def captured(monkeypatch) -> Dict[str, Any]:
    """替换 ``create_agent``,把收到的参数抓下来。"""
    calls: Dict[str, Any] = {}

    def _fake_create_agent(**kwargs: Any):
        calls.update(kwargs)
        return SimpleNamespace(kind="fake-agent")

    monkeypatch.setattr(agent_mod, "create_agent", _fake_create_agent)
    return calls


@pytest.fixture
def forbid_api_key_check(monkeypatch) -> list:
    """把 ``validate_api_key`` 换成「一调用就炸」。"""
    calls: list = []

    async def _boom(*args: Any, **kwargs: Any):
        calls.append(args)
        raise AssertionError("mcode 分支不应该做 API key 校验")

    monkeypatch.setattr(agent_mod, "validate_api_key", _boom)
    return calls


@pytest.fixture
def forbid_mcp_client(monkeypatch) -> list:
    """把 ``MultiServerMCPClient`` 换成「一构造就炸」。"""
    calls: list = []

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any):
            calls.append(args)
            raise AssertionError("mcode 分支不应该连 8001 的 MCP server")

    monkeypatch.setattr(agent_mod, "MultiServerMCPClient", _Boom)
    return calls


@pytest.fixture
def forbid_load_skills(monkeypatch) -> list:
    """把 ``load_skills`` 换成「一调用就炸」。"""
    calls: list = []

    async def _boom(*args: Any, **kwargs: Any):
        calls.append(args)
        raise AssertionError("mcode 分支不应该注册 LangChain skills 工具")

    monkeypatch.setattr(agent_mod, "load_skills", _boom)
    return calls


# --------------------------------------------------------------------------
# mcode 分支
# --------------------------------------------------------------------------

async def test_mcode_branch_skips_api_key_validation(
    operator_dir, store, captured, forbid_api_key_check, forbid_mcp_client, forbid_load_skills
):
    """mcode 分支不调用 LLM / VLM 的 ``validate_api_key``。"""
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(operator_dir))

    agent, node_manager = await build_agent(
        cfg=cfg,
        session_id="sess-mcode",
        store=store,
        llm_override={"model": "mcode-operator"},
    )

    assert forbid_api_key_check == [], "mcode 分支竟然做了 API key 校验"
    assert agent is not None
    assert node_manager is not None


async def test_mcode_branch_does_not_connect_8001(
    operator_dir, store, captured, forbid_mcp_client, forbid_api_key_check
):
    """mcode 分支不构造 ``MultiServerMCPClient``(不连 8001)。"""
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(operator_dir))

    await build_agent(
        cfg=cfg,
        session_id="sess-mcode",
        store=store,
        llm_override={"model": "mcode-operator"},
    )

    assert forbid_mcp_client == [], "mcode 分支竟然构造了 MultiServerMCPClient"


async def test_mcode_branch_does_not_register_langchain_skills(
    operator_dir, store, captured, forbid_load_skills
):
    """mcode 分支不调 ``load_skills()``,``create_agent`` 收到 ``tools=[]``。"""
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(operator_dir))

    await build_agent(
        cfg=cfg,
        session_id="sess-mcode",
        store=store,
        llm_override={"model": "mcode-operator"},
    )

    assert forbid_load_skills == [], "mcode 分支竟然调用了 load_skills()"
    assert captured["tools"] == [], f"tools 应该是空列表,实际 {captured['tools']!r}"


@pytest.mark.parametrize(
    "model_name,expected_mode",
    [
        ("mcode-operator", "standard"),
        ("mcode-chat", "lightweight"),
    ],
)
async def test_mcode_branch_uses_mcode_model(
    operator_dir, store, captured, model_name, expected_mode
):
    """``mcode-operator`` -> standard;``mcode-chat`` -> lightweight。"""
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(operator_dir))

    await build_agent(
        cfg=cfg,
        session_id="sess-mcode",
        store=store,
        llm_override={"model": model_name},
    )

    model = captured["model"]
    assert isinstance(model, MCodeChatModel), f"模型类型不对: {type(model)!r}"
    assert model.mode == expected_mode
    assert model.cwd == str(operator_dir.resolve())
    assert model.session_id == "sess-mcode"


async def test_mcode_branch_passes_config_to_model(operator_dir, store, captured):
    """配置项要真的传到模型上(不是写死的默认值)。"""
    cfg = _cfg_with_mcode(
        _base_settings(),
        cwd=str(operator_dir),
        mode="lightweight",
        permission="off",
        max_steps=7,
        timeout_s=42,
        history_turns=3,
        max_prompt_chars=1234,
        bin="",
    )

    await build_agent(
        cfg=cfg,
        session_id="sess-mcode",
        store=store,
        llm_override={"model": "mcode-other"},
    )

    model = captured["model"]
    assert isinstance(model, MCodeChatModel)
    # 未识别的 mcode* 档位 -> 走 cfg.mcode.mode
    assert model.mode == "lightweight"
    assert model.permission == "off"
    assert model.max_steps == 7
    assert model.timeout_s == 42
    assert model.history_turns == 3
    assert model.max_prompt_chars == 1234


async def test_mcode_branch_requires_configured_cwd(store, captured):
    """``cwd`` 没配 -> 明确的配置错误(不静默回退)。"""
    cfg = _cfg_with_mcode(_base_settings(), cwd="")

    with pytest.raises(MCodeConfigError) as exc:
        await build_agent(
            cfg=cfg,
            session_id="sess-mcode",
            store=store,
            llm_override={"model": "mcode-operator"},
        )

    assert "cwd" in str(exc.value) or "操作员目录" in str(exc.value)


async def test_mcode_branch_requires_existing_cwd(tmp_path, store, captured):
    """``cwd`` 指向不存在的目录 -> 明确的配置错误。"""
    missing = tmp_path / "nope"
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(missing))

    with pytest.raises(MCodeConfigError) as exc:
        await build_agent(
            cfg=cfg,
            session_id="sess-mcode",
            store=store,
            llm_override={"model": "mcode-operator"},
        )

    assert "不存在" in str(exc.value)


async def test_mcode_branch_requires_mcp_json(tmp_path, store, captured):
    """操作员目录缺 ``.mcp.json`` -> 明确的配置错误。"""
    d = tmp_path / "ops-no-mcp"
    d.mkdir()
    (d / "AGENTS.md").write_text("# operator\n", encoding="utf-8")
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(d))

    with pytest.raises(MCodeConfigError) as exc:
        await build_agent(
            cfg=cfg,
            session_id="sess-mcode",
            store=store,
            llm_override={"model": "mcode-operator"},
        )

    assert ".mcp.json" in str(exc.value)


async def test_mcode_branch_requires_agents_md(tmp_path, store, captured):
    """操作员目录缺 ``AGENTS.md`` -> 明确的配置错误。"""
    d = tmp_path / "ops-no-agents"
    d.mkdir()
    (d / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(d))

    with pytest.raises(MCodeConfigError) as exc:
        await build_agent(
            cfg=cfg,
            session_id="sess-mcode",
            store=store,
            llm_override={"model": "mcode-operator"},
        )

    assert "AGENTS.md" in str(exc.value)


async def test_mcode_branch_config_error_raises_before_agent_built(
    tmp_path, store, captured
):
    """配置错误要在**建 agent 之前**抛 —— 不能建出半个 agent。"""
    cfg = _cfg_with_mcode(_base_settings(), cwd=str(tmp_path / "nope"))

    with pytest.raises(MCodeConfigError):
        await build_agent(
            cfg=cfg,
            session_id="sess-mcode",
            store=store,
            llm_override={"model": "mcode-operator"},
        )

    assert captured == {}, "配置错误时不该调用 create_agent"


# --------------------------------------------------------------------------
# 普通模型分支:一字未改
# --------------------------------------------------------------------------

async def test_non_mcode_mcp_failure_semantics_unchanged(store, monkeypatch, captured):
    """普通模型路径:MCP 加载失败照旧失败,不静默降级成零工具。"""
    cfg = _base_settings()

    async def _ok_key(*args: Any, **kwargs: Any):
        return True

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any):
            self.kwargs = kwargs

        async def get_tools(self):
            raise RuntimeError("8001 连不上")

    monkeypatch.setattr(agent_mod, "validate_api_key", _ok_key)
    monkeypatch.setattr(agent_mod, "MultiServerMCPClient", _FailingClient)

    with pytest.raises(RuntimeError, match="8001 连不上"):
        await build_agent(
            cfg=cfg,
            session_id="sess-normal",
            store=store,
            llm_override={"model": "deepseek-v4-pro"},
        )

    assert captured == {}, "MCP 失败时不该建 agent"


async def test_non_mcode_branch_still_validates_api_key(store, monkeypatch, captured):
    """普通模型路径**照旧**做 API key 校验 —— mcode 分支的改动没渗过来。"""
    cfg = _base_settings()
    calls: list = []

    async def _record(*args: Any, **kwargs: Any):
        calls.append(args)
        return True

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any):
            pass

        async def get_tools(self):
            raise RuntimeError("stop-here")

    monkeypatch.setattr(agent_mod, "validate_api_key", _record)
    monkeypatch.setattr(agent_mod, "MultiServerMCPClient", _FailingClient)

    with pytest.raises(RuntimeError, match="stop-here"):
        await build_agent(
            cfg=cfg,
            session_id="sess-normal",
            store=store,
            llm_override={"model": "deepseek-v4-pro"},
        )

    # LLM + VLM 各一次
    assert len(calls) == 2, f"普通分支应该校验两次 key(LLM+VLM),实际 {len(calls)} 次"


async def test_non_mcode_branch_uses_chat_openai(store, monkeypatch, captured):
    """普通模型分支仍然走 ``ChatOpenAI`` + 正常工具列表,行为没变。"""
    cfg = _base_settings()

    async def _ok_key(*args: Any, **kwargs: Any):
        return True

    class _FakeTool:
        """够 ``NodeManager`` 用的假工具(它只看 ``.metadata``)。"""

        def __init__(self, name: str):
            self.name = name
            self.metadata = None

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any):
            self.kwargs = kwargs

        async def get_tools(self):
            return [_FakeTool("tool-a"), _FakeTool("tool-b")]

    async def _fake_skills(*args: Any, **kwargs: Any):
        return [_FakeTool("skill-a")]

    monkeypatch.setattr(agent_mod, "validate_api_key", _ok_key)
    monkeypatch.setattr(agent_mod, "MultiServerMCPClient", _FakeClient)
    monkeypatch.setattr(agent_mod, "load_skills", _fake_skills)

    await build_agent(
        cfg=cfg,
        session_id="sess-normal",
        store=store,
        llm_override={"model": "deepseek-v4-pro"},
    )

    assert [t.name for t in captured["tools"]] == ["tool-a", "tool-b", "skill-a"]
    assert not isinstance(captured["model"], MCodeChatModel)

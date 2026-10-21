"""网页端 -> mcode 的**接线**测试。

被测的就是上一版 plan 明确排除、导致「网页端连不上 mcode」的那一跳::

    模型下拉(含 mcode 档位)
      -> ChatSession.chat_model_key
        -> ensure_agent() 解析成 llm_override = {"model": "mcode-xxx"}
          -> build_agent() 命中 is_mcode_model() -> 走 mcode 分支

策略:把 ``create_agent`` 换成假实现抓参数 —— 断言的是「我们传给 LangChain 的东西」,
而不是 LangChain 内部怎么存。跟 ``test_agent_mcode_branch.py`` 同一套路。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

import agent_fastapi as web
from open_storyline import agent as agent_mod
from open_storyline.config import MCodeConfig, ProjectConfig, Settings, load_settings
from open_storyline.mcode_chat_model import MCodeChatModel

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent

#: 内置模型的「占位符」key —— 入库的 config.toml 里就是它,不是真凭证。
PLACEHOLDER_KEY = "<YOUR_DEEPSEEK_API_KEY>"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    """prompt 模板用的是**相对**路径(``prompts/tasks/...``)。

    生产环境以 ``openstoryline/`` 为 cwd 启动,这里对齐,否则
    ``ChatSession.__init__`` 里的 ``get_prompt("instruction.system")`` 会找不到模板。
    """
    monkeypatch.chdir(OPENSTORYLINE_DIR)


@pytest.fixture
def operator_dir(tmp_path) -> Path:
    """一个合法的操作员目录:有 ``.mcp.json`` 和 ``AGENTS.md``。"""
    d = tmp_path / "video-ops"
    d.mkdir()
    (d / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (d / "AGENTS.md").write_text("# operator\n", encoding="utf-8")
    return d


def _settings(
    tmp_path: Path,
    *,
    mcode_cwd: str = "",
    api_key: str = PLACEHOLDER_KEY,
    model: str = "deepseek-v4-pro",
) -> Settings:
    """一份 Settings:目录全部落在 tmp_path,凭证可调。"""
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    llm = cfg.llm.model_copy(update={"model": model, "api_key": api_key})
    vlm = cfg.vlm.model_copy(update={"model": model, "api_key": api_key})
    return cfg.model_copy(
        update={
            "project": project,
            "llm": llm,
            "vlm": vlm,
            "mcode": MCodeConfig(cwd=mcode_cwd),
        }
    )


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
def stub_builtin_backend(monkeypatch):
    """把内置模型路径的三样外部依赖桩掉。

    - ``validate_api_key``:真调网关会拿占位符 key 换回 401(测试不该出网);
    - ``MultiServerMCPClient``:不该真连 8001;
    - ``load_skills``:不该真读技能目录。
    """

    async def _ok(*args: Any, **kwargs: Any):
        return True

    class _FakeClient:
        async def get_tools(self):
            return []

    async def _no_skills(*args: Any, **kwargs: Any):
        return []

    monkeypatch.setattr(agent_mod, "validate_api_key", _ok)
    monkeypatch.setattr(agent_mod, "MultiServerMCPClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(agent_mod, "load_skills", _no_skills)


# --------------------------------------------------------------------------
# 模型下拉
# --------------------------------------------------------------------------

def test_mcode_tiers_appear_in_model_dropdown(tmp_path, operator_dir):
    """操作员目录配好了 -> 模型下拉里出现两个 mcode 档位。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir))
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)

    assert "mcode-operator" in sess.chat_models
    assert "mcode-chat" in sess.chat_models
    assert "mcode-operator" in sess.vlm_models
    # 内置模型仍然留在下拉里(只是可能不是默认项)
    assert "deepseek-v4-pro" in sess.chat_models
    # 自定义模型选项保留
    assert web.CUSTOM_MODEL_KEY in sess.chat_models


def test_mcode_tiers_hidden_when_not_configured(tmp_path):
    """没配操作员目录 -> 下拉里**不出现** mcode 档位(不给点了就报错的选项)。"""
    cfg = _settings(tmp_path, mcode_cwd="")
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)

    assert "mcode-operator" not in sess.chat_models
    assert "mcode-chat" not in sess.chat_models


def test_mcode_tiers_hidden_when_cwd_missing(tmp_path):
    """操作员目录不存在 -> 同样不出现 mcode 档位。"""
    cfg = _settings(tmp_path, mcode_cwd=str(tmp_path / "nope"))
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)

    assert "mcode-operator" not in sess.chat_models


def test_default_model_is_mcode_when_builtin_key_is_placeholder(tmp_path, operator_dir):
    """内置模型的 key 是占位符 -> 默认档位直接落到 mcode(别让用户先撞一次 401)。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key=PLACEHOLDER_KEY)
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)

    assert sess.chat_model_key == "mcode-operator"
    assert sess.vlm_model_key == "mcode-operator"


def test_default_model_stays_builtin_when_credential_usable(tmp_path, operator_dir):
    """内置模型有真凭证 -> 默认档位不变(不能抢用户的模型)。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key="test-credential-not-a-real-key")
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)

    assert sess.chat_model_key == "deepseek-v4-pro"
    assert sess.vlm_model_key == "deepseek-v4-pro"


def test_default_model_stays_builtin_when_mcode_not_configured(tmp_path):
    """mcode 没配 + 内置模型没凭证 -> 保持内置(报原本的错,不静默换模型)。"""
    cfg = _settings(tmp_path, mcode_cwd="", api_key=PLACEHOLDER_KEY)
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)

    assert sess.chat_model_key == "deepseek-v4-pro"


# --------------------------------------------------------------------------
# ensure_agent(): 网页端选择 -> mcode 分支
# --------------------------------------------------------------------------

async def test_ensure_agent_routes_mcode_tier_to_mcode_branch(
    tmp_path, operator_dir, captured, forbid_api_key_check, forbid_mcp_client
):
    """网页端选了 mcode 档位 -> 真的走 mcode 分支(跳过 key 校验、不连 8001、tools=[])。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir))
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)
    sess.chat_model_key = "mcode-chat"
    sess.vlm_model_key = "mcode-chat"

    await sess.ensure_agent()

    assert forbid_api_key_check == [], "mcode 分支竟然做了 API key 校验"
    assert forbid_mcp_client == [], "mcode 分支竟然连了 8001"
    model = captured.get("model")
    assert isinstance(model, MCodeChatModel), f"模型不是 MCodeChatModel: {type(model)}"
    assert model.mode == "lightweight", "mcode-chat 应该是 lightweight 模式"
    assert captured.get("tools") == [], "mcode 分支不该有 LangChain 工具"


async def test_ensure_agent_mcode_operator_uses_standard_mode(
    tmp_path, operator_dir, captured
):
    """``mcode-operator`` -> standard 模式。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir))
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)
    sess.chat_model_key = "mcode-operator"

    await sess.ensure_agent()

    model = captured.get("model")
    assert isinstance(model, MCodeChatModel)
    assert model.mode == "standard"


async def test_ensure_agent_ignores_builtin_credentials_in_mcode_mode(
    tmp_path, operator_dir, captured
):
    """mcode 模式下**完全不查**内置模型配置 —— 连「配置不完整」的错都不该报。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key="")
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)
    sess.chat_model_key = "mcode-operator"

    await sess.ensure_agent()  # 不抛错 = 没去解析内置模型
    assert isinstance(captured.get("model"), MCodeChatModel)


async def test_ensure_agent_switches_from_builtin_to_mcode(
    tmp_path, operator_dir, captured, stub_builtin_backend
):
    """从内置模型切到 mcode -> agent 重建(build key 变了)。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key="test-credential-not-a-real-key")
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)
    sess.chat_model_key = "deepseek-v4-pro"

    await sess.ensure_agent()
    first = sess.agent
    first_key = sess._agent_build_key

    sess.chat_model_key = "mcode-operator"
    await sess.ensure_agent()

    assert sess.agent is not first, "切到 mcode 后应该重建 agent"
    assert sess._agent_build_key != first_key
    assert isinstance(captured.get("model"), MCodeChatModel)


async def test_ensure_agent_builtin_path_still_uses_chat_openai(
    tmp_path, operator_dir, captured, stub_builtin_backend
):
    """内置模型路径**没被改坏**:仍然走 ChatOpenAI + 正常工具列表。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key="test-credential-not-a-real-key")
    sess = web.ChatSession(session_id="sess-web", cfg=cfg)
    sess.chat_model_key = "deepseek-v4-pro"

    await sess.ensure_agent()

    model = captured.get("model")
    assert not isinstance(model, MCodeChatModel), "内置模型不该被换成 MCodeChatModel"
    assert type(model).__name__ == "ChatOpenAI"


# --------------------------------------------------------------------------
# 会话恢复
# --------------------------------------------------------------------------

def _write_state(cfg: Settings, session_id: str, payload: Dict[str, Any]) -> None:
    path = web.ChatSession.state_file_path_for(session_id, cfg)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_restore_keeps_mcode_tier(tmp_path, operator_dir):
    """恢复出来的 mcode 档位要保住。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir))
    _write_state(cfg, "sess-restore", {
        "version": 1,
        "session_id": "sess-restore",
        "chat_model_key": "mcode-chat",
        "vlm_model_key": "mcode-chat",
    })

    sess = web.ChatSession.load_from_state("sess-restore", cfg)
    assert sess is not None
    assert sess.chat_model_key == "mcode-chat"
    assert sess.vlm_model_key == "mcode-chat"


def test_restore_falls_back_when_builtin_credential_missing(tmp_path, operator_dir):
    """旧会话记着内置模型,但那把 key 是占位符 -> 回落到 mcode(别一直 401)。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key=PLACEHOLDER_KEY)
    _write_state(cfg, "sess-restore", {
        "version": 1,
        "session_id": "sess-restore",
        "chat_model_key": "deepseek-v4-pro",
        "vlm_model_key": "deepseek-v4-pro",
    })

    sess = web.ChatSession.load_from_state("sess-restore", cfg)
    assert sess is not None
    assert sess.chat_model_key == "mcode-operator"
    assert sess.vlm_model_key == "mcode-operator"


def test_restore_keeps_builtin_when_credential_usable(tmp_path, operator_dir):
    """内置模型有真凭证 -> 恢复时保持原样。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key="test-credential-not-a-real-key")
    _write_state(cfg, "sess-restore", {
        "version": 1,
        "session_id": "sess-restore",
        "chat_model_key": "deepseek-v4-pro",
        "vlm_model_key": "deepseek-v4-pro",
    })

    sess = web.ChatSession.load_from_state("sess-restore", cfg)
    assert sess is not None
    assert sess.chat_model_key == "deepseek-v4-pro"


def test_restore_drops_unknown_model_key(tmp_path, operator_dir):
    """恢复出来的 key 不在可选列表里(配置变了)-> 落回默认。"""
    cfg = _settings(tmp_path, mcode_cwd=str(operator_dir), api_key="test-credential-not-a-real-key")
    _write_state(cfg, "sess-restore", {
        "version": 1,
        "session_id": "sess-restore",
        "chat_model_key": "gpt-9-turbo-ultra",
        "vlm_model_key": "gpt-9-turbo-ultra",
    })

    sess = web.ChatSession.load_from_state("sess-restore", cfg)
    assert sess is not None
    assert sess.chat_model_key == "deepseek-v4-pro"


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------

def test_resolve_mcode_tier_prefers_llm():
    """LLM 是 mcode 档位时,以 LLM 为准。"""
    assert web._resolve_mcode_tier("mcode-chat", "mcode-operator") == "mcode-chat"
    assert web._resolve_mcode_tier("deepseek-v4-pro", "mcode-operator") == "mcode-operator"
    assert web._resolve_mcode_tier("deepseek-v4-pro", "deepseek-v4-pro") == ""
    assert web._resolve_mcode_tier("", None) == ""


def test_looks_like_placeholder_secret():
    """占位符 / 空 / 掩码都算「不可用凭证」。"""
    assert web._looks_like_placeholder_secret("<YOUR_DEEPSEEK_API_KEY>")
    assert web._looks_like_placeholder_secret("")
    assert web._looks_like_placeholder_secret(None)
    assert web._looks_like_placeholder_secret("***")
    assert not web._looks_like_placeholder_secret("sk-abcdef123456")

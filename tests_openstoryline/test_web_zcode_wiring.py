"""阶段 E:网页端 -> ZCode Agent 的**接线**测试。

被测的跳转链(与 ``test_web_mcode_wiring.py`` 同构)::

    模型下拉(含 ZCode Agent)
      -> ChatSession.chat_model_key
        -> ensure_agent() 解析成 llm_override = {"model": "ZCode Agent"}
          -> build_agent() 命中 is_zcode_model() -> 走 zcode 分支

外加阶段 E 特有的三块:
- 可用性判定(_zcode_available / _zcode_note / model_notes)
- turn_source 三值化(zcode 不转发 LangGraph 文本)
- zcode sessionId 映射的捕获与持久化(dump_state / load_from_state)
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from langchain_core.messages import AIMessage

import agent_fastapi as web
from open_storyline import agent as agent_mod
from open_storyline.config import ProjectConfig, Settings, ZCodeConfig, load_settings
from open_storyline.zcode_agent_adapter import ZCodeChatModel

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent

ZKEY = web.ZCODE_MODEL_KEY


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    monkeypatch.chdir(OPENSTORYLINE_DIR)


def _settings(tmp_path: Path, *, zcode: ZCodeConfig) -> Settings:
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    # 本机 config.toml.local 可能配了 [mcode].cwd —— 显式清空,隔离 mcode 分支。
    mcode = cfg.mcode.model_copy(update={"cwd": "", "bin": ""})
    return cfg.model_copy(update={"project": project, "zcode": zcode, "mcode": mcode})


@pytest.fixture
def zcode_ok_cfg(tmp_path, fake_zcode_cli) -> Settings:
    """可用配置:工作区存在 + 假 CLI 自检通过。"""
    cli = fake_zcode_cli()
    ws = tmp_path / "zcode-ws"
    ws.mkdir()
    return _settings(
        tmp_path, zcode=ZCodeConfig(cwd=str(ws), bin=cli.path)
    )


@pytest.fixture
def captured(monkeypatch) -> Dict[str, Any]:
    calls: Dict[str, Any] = {}

    def _fake_create_agent(**kwargs: Any):
        calls.update(kwargs)
        return SimpleNamespace(kind="fake-agent")

    monkeypatch.setattr(agent_mod, "create_agent", _fake_create_agent)
    return calls


# --------------------------------------------------------------------------
# 可用性判定与下拉
# --------------------------------------------------------------------------

def test_dropdown_contains_zcode_when_available(tmp_path, zcode_ok_cfg):
    sess = web.ChatSession(session_id="s1", cfg=zcode_ok_cfg)
    assert ZKEY in sess.chat_models
    assert ZKEY in sess.vlm_models
    assert sess.model_notes() == {}


def test_dropdown_absent_when_not_configured(tmp_path):
    cfg = _settings(tmp_path, zcode=ZCodeConfig(cwd=""))
    sess = web.ChatSession(session_id="s1", cfg=cfg)
    assert ZKEY not in sess.chat_models
    assert ZKEY not in sess.vlm_models
    assert sess.model_notes() == {}


def test_dropdown_disabled_with_note_when_selfcheck_fails(
    tmp_path, fake_zcode_cli
):
    """配了 cwd 但自检不过:选项仍在下拉里,带明确禁用原因(§8.3)。"""
    cli = fake_zcode_cli(help_text="Usage:\n  --no-headless-flag\n")
    ws = tmp_path / "zcode-ws"
    ws.mkdir()
    cfg = _settings(
        tmp_path, zcode=ZCodeConfig(cwd=str(ws), bin=cli.path)
    )
    sess = web.ChatSession(session_id="s1", cfg=cfg)
    assert ZKEY in sess.chat_models
    notes = sess.model_notes()
    assert ZKEY in notes
    assert "-p" in notes[ZKEY]


def test_default_falls_back_to_zcode_when_builtin_creds_unusable(
    tmp_path, zcode_ok_cfg
):
    """内置模型凭证不可用而 zcode 可用 -> 默认档位直接落 zcode(与 mcode 同款)。"""
    sess = web.ChatSession(session_id="s1", cfg=zcode_ok_cfg)
    if not web._builtin_credentials_usable(zcode_ok_cfg, "llm"):
        assert sess.chat_model_key == ZKEY
        assert sess.vlm_model_key == ZKEY


# --------------------------------------------------------------------------
# turn_source 三值化
# --------------------------------------------------------------------------

def test_turn_source_zcode():
    assert web._resolve_turn_source(ZKEY, ZKEY) == web.TURN_SOURCE_ZCODE
    assert web._resolve_turn_source("mcode-operator", "mcode-operator") == web.TURN_SOURCE_MCODE
    assert web._resolve_turn_source("deepseek-v4-pro", "deepseek-v4-pro") == web.TURN_SOURCE_LLM
    # LLM 优先
    assert web._resolve_turn_source(ZKEY, "mcode-operator") == web.TURN_SOURCE_ZCODE


def test_langgraph_text_not_forwarded_for_zcode():
    assert web._should_forward_langgraph_text(web.TURN_SOURCE_MCODE) is False
    assert web._should_forward_langgraph_text(web.TURN_SOURCE_ZCODE) is False
    assert web._should_forward_langgraph_text(web.TURN_SOURCE_LLM) is True


def test_make_zcode_adapter_gating(tmp_path, zcode_ok_cfg):
    """普通模型链路不建 zcode 适配器(SRC-03 同款)。"""
    assert web._make_zcode_adapter(
        web.TURN_SOURCE_LLM, zcode_ok_cfg, session_id="s1", turn_id="t1"
    ) is None
    adapter = web._make_zcode_adapter(
        web.TURN_SOURCE_ZCODE, zcode_ok_cfg, session_id="s1", turn_id="t1"
    )
    assert adapter is not None
    assert adapter.source == "zcode"


# --------------------------------------------------------------------------
# ensure_agent 走 zcode 分支
# --------------------------------------------------------------------------

async def test_ensure_agent_builds_zcode_model(tmp_path, zcode_ok_cfg, captured):
    sess = web.ChatSession(session_id="s1", cfg=zcode_ok_cfg)
    sess.chat_model_key = ZKEY
    sess.vlm_model_key = ZKEY

    await sess.ensure_agent()

    model = captured["model"]
    assert isinstance(model, ZCodeChatModel)
    assert model.session_id == "s1"
    # provider 机制:取当前 sink / 当前 resume 映射
    assert sess.zcode_sink_provider()() is None
    marker = object()
    sess._zcode_event_sink = marker
    assert sess.zcode_sink_provider()() is marker
    sess._zcode_event_sink = None

    sess.zcode_session_id = "sess_prev"
    assert sess.zcode_resume_id_provider()() == "sess_prev"


# --------------------------------------------------------------------------
# sessionId 映射捕获与持久化
# --------------------------------------------------------------------------

def test_capture_zcode_session_id(tmp_path, zcode_ok_cfg):
    sess = web.ChatSession(session_id="s1", cfg=zcode_ok_cfg)

    # 没有消息 -> False
    assert web._capture_zcode_session_id(sess, []) is False

    # 有新映射 -> True 且落会话
    msg = AIMessage(
        content="ok",
        response_metadata={"zcode_session_id": "sess_new"},
    )
    assert web._capture_zcode_session_id(sess, [msg]) is True
    assert sess.zcode_session_id == "sess_new"

    # 同值重复 -> False(不重复落库)
    assert web._capture_zcode_session_id(sess, [msg]) is False


def test_zcode_session_id_persists_in_dump_state(tmp_path, zcode_ok_cfg):
    sess = web.ChatSession(session_id="s1", cfg=zcode_ok_cfg)
    sess.zcode_session_id = "sess_persist"

    data = sess.dump_state()
    assert data["zcode_session_id"] == "sess_persist"

    # 默认会话是空映射
    fresh = web.ChatSession(session_id="s2", cfg=zcode_ok_cfg)
    assert fresh.dump_state()["zcode_session_id"] == ""


def test_zcode_session_id_restored_from_state(tmp_path, zcode_ok_cfg):
    """load_from_state 能把映射接回来(重启后 --resume 仍续得上)。"""
    sess = web.ChatSession(session_id="s1", cfg=zcode_ok_cfg)
    sess.zcode_session_id = "sess_keep"
    data = sess.dump_state()

    class _Record:
        state_json = data

    class _Repo:
        def get(self, session_id):
            return _Record()

        def create_if_absent(self, *a, **k):
            return (_Record(), True)

    restored = web.ChatSession.load_from_state("s1", zcode_ok_cfg, repo=_Repo())
    assert restored is not None
    assert restored.zcode_session_id == "sess_keep"

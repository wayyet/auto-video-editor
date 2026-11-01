"""GLM API(BigModel)预设的接线测试(ZCode 接入设计 §10.4 阶段 C)。

被测链路::

    [glm] 配置(config.toml.local / 环境变量)
      -> ChatSession.chat_models 里出现「GLM API / BigModel」
        -> ensure_agent() 解析成 llm_override = {model, base_url, api_key, timeout, ...}
          -> build_agent() 走 ChatOpenAI(和内置模型同构)

跟 ``test_web_mcode_wiring.py`` 同一套路:把外部依赖桩掉,断言「我们传给
LangChain 的东西」。所有用例都不出网。

命名红线(设计 §7 约束 8):GLM API 是模型服务,**禁止**与 ZCode Agent 混用
任何来源标识 —— 有专门用例盯住这条红线。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

import agent_fastapi as web
from open_storyline import agent as agent_mod
from open_storyline.config import GLMConfig, ProjectConfig, Settings, load_settings

TESTS_DIR = Path(__file__).resolve().parent
OPENSTORYLINE_DIR = TESTS_DIR.parent

PLACEHOLDER_KEY = "<YOUR_DEEPSEEK_API_KEY>"
GLM_KEY = web.GLM_MODEL_KEY


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_openstoryline_dir(monkeypatch):
    """生产 Web 以仓库根为 cwd(2026-11-01 扁平化后 OpenStoryline 根 == 仓库根),这里对齐。"""
    monkeypatch.chdir(OPENSTORYLINE_DIR)


@pytest.fixture(autouse=True)
def _clean_glm_env(monkeypatch):
    """隔离 GLM 环境变量:用例自己决定设什么,不吃宿主机环境的残留。"""
    for name in (
        "OPENSTORYLINE_GLM_MODEL",
        "OPENSTORYLINE_GLM_BASE_URL",
        "OPENSTORYLINE_GLM_API_KEY",
        "OPENSTORYLINE_GLM_VLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(
    tmp_path: Path,
    *,
    glm: "GLMConfig | None" = None,
    llm_api_key: str = "test-credential-not-a-real-key",
) -> Settings:
    """一份 Settings:目录全部落在 tmp_path,GLM 段可调。"""
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")
    project = ProjectConfig(
        media_dir=tmp_path / "media",
        bgm_dir=tmp_path / "bgm",
        outputs_dir=tmp_path / "outputs",
    )
    llm = cfg.llm.model_copy(update={"api_key": llm_api_key})
    vlm = cfg.vlm.model_copy(update={"api_key": llm_api_key})
    return cfg.model_copy(
        update={
            "project": project,
            "llm": llm,
            "vlm": vlm,
            "glm": glm if glm is not None else GLMConfig(api_key=""),
        }
    )


@pytest.fixture
def captured(monkeypatch) -> Dict[str, Any]:
    """替换 ``create_agent`` 抓参数;同时包一层 ``ChatOpenAI`` 记录构造参数。

    ``ChatOpenAI`` 的属性名在 langchain_openai 各版本间有差异(base_url /
    openai_api_base),抓**构造 kwargs** 最稳,不受版本影响。
    """
    calls: Dict[str, Any] = {"chat_openai_kwargs": []}

    def _fake_create_agent(**kwargs: Any):
        calls.update(kwargs)
        return SimpleNamespaceAgent()

    _RealChatOpenAI = agent_mod.ChatOpenAI

    class _RecordingChatOpenAI(_RealChatOpenAI):
        def __init__(self, **kwargs: Any):
            calls["chat_openai_kwargs"].append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(agent_mod, "create_agent", _fake_create_agent)
    monkeypatch.setattr(agent_mod, "ChatOpenAI", _RecordingChatOpenAI)
    return calls


class SimpleNamespaceAgent:
    """create_agent 的假返回值(够 ChatSession 存引用即可)。"""


@pytest.fixture
def stub_builtin_backend(monkeypatch):
    """桩掉内置/GLM 模型路径的外部依赖:不出网、不连 8001、不读技能目录。"""

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
# 下拉可见性
# --------------------------------------------------------------------------

def test_glm_preset_hidden_without_api_key(tmp_path):
    """没配 Key -> 下拉里不出现 GLM 预设(不给「点了就 401」的选项)。"""
    sess = web.ChatSession(session_id="sess-glm", cfg=_settings(tmp_path))

    assert GLM_KEY not in sess.chat_models
    assert GLM_KEY not in sess.vlm_models
    # 既有选项不受影响
    assert "deepseek-v4-pro" in sess.chat_models
    assert web.CUSTOM_MODEL_KEY in sess.chat_models


def test_glm_preset_appears_in_llm_dropdown_with_key(tmp_path):
    """``[glm]`` 三要素齐 -> LLM 下拉出现 GLM 预设。"""
    cfg = _settings(tmp_path, glm=GLMConfig(api_key="glm-key-for-test"))
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)

    assert GLM_KEY in sess.chat_models
    # vlm_model 没配 -> VLM 下拉不出现(设计 C-T2:不满足则只做 LLM 预设)
    assert GLM_KEY not in sess.vlm_models


def test_glm_preset_vlm_needs_explicit_vlm_model(tmp_path):
    """``vlm_model`` 配了 -> VLM 下拉也出现 GLM 预设。"""
    cfg = _settings(
        tmp_path,
        glm=GLMConfig(api_key="glm-key-for-test", vlm_model="glm-4.6v-test"),
    )
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)

    assert GLM_KEY in sess.chat_models
    assert GLM_KEY in sess.vlm_models


def test_glm_preset_hidden_when_key_is_placeholder(tmp_path):
    """Key 是占位符 -> 同样不出现(占位符不能当凭证用)。"""
    cfg = _settings(tmp_path, glm=GLMConfig(api_key="<YOUR_GLM_API_KEY>"))
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)

    assert GLM_KEY not in sess.chat_models


def test_menu_label_is_glm_not_zcode(tmp_path):
    """命名红线(设计 §7 约束 8):菜单标识必须是「GLM API / BigModel」,
    任何模型选项都不得出现「zcode」字样 —— GLM API 是模型调用,
    ZCode Agent 是外部 Agent Runtime,两者不可共用来源标识。"""
    cfg = _settings(
        tmp_path,
        glm=GLMConfig(api_key="glm-key-for-test", vlm_model="glm-4.6v-test"),
    )
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)

    assert GLM_KEY == "GLM API / BigModel"
    for key in (*sess.chat_models, *sess.vlm_models):
        assert "zcode" not in key.lower(), f"模型选项混入了 ZCode 标识: {key!r}"


def test_default_endpoint_is_general_not_coding_plan(tmp_path):
    """base_url 默认用通用 OpenAI 兼容 endpoint,不是 Coding Plan 专属
    endpoint(设计 §3.5:视频任务不应默认套用 /api/coding/paas/v4)。"""
    g = GLMConfig()
    assert g.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert "coding" not in g.base_url


# --------------------------------------------------------------------------
# ensure_agent():选中 GLM -> ChatOpenAI 链路
# --------------------------------------------------------------------------

async def test_ensure_agent_glm_llm_uses_glm_config(tmp_path, captured, stub_builtin_backend):
    """LLM 选 GLM -> ChatOpenAI 拿到 [glm] 的 model/base_url/api_key/timeout。"""
    cfg = _settings(
        tmp_path,
        glm=GLMConfig(
            api_key="glm-key-for-test",
            timeout=66.0,
            temperature=0.2,
            max_retries=3,
        ),
    )
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)
    assert GLM_KEY in sess.chat_models
    sess.chat_model_key = GLM_KEY
    sess.vlm_model_key = "deepseek-v4-pro"

    await sess.ensure_agent()

    kw = calls_llm_kwargs(captured)
    assert kw["model"] == "glm-4.6"
    assert kw["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert kw["api_key"] == "glm-key-for-test"
    assert kw["timeout"] == 66.0
    assert kw["temperature"] == 0.2
    assert kw["max_retries"] == 3


async def test_ensure_agent_glm_vlm_uses_vlm_model_name(
    tmp_path, captured, stub_builtin_backend
):
    """VLM 选 GLM -> 模型名用 vlm_model,凭证仍来自 [glm]。"""
    cfg = _settings(
        tmp_path,
        glm=GLMConfig(api_key="glm-key-for-test", vlm_model="glm-4.6v-test"),
    )
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)
    sess.chat_model_key = "deepseek-v4-pro"
    sess.vlm_model_key = GLM_KEY

    await sess.ensure_agent()

    kw = calls_vlm_kwargs(captured)
    assert kw["model"] == "glm-4.6v-test"
    assert kw["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert kw["api_key"] == "glm-key-for-test"


async def test_ensure_agent_glm_without_key_raises_reconfig(tmp_path, captured, stub_builtin_backend):
    """会话恢复时 config 里的 Key 没了 -> 报 requires_reconfig,不静默回落。"""
    cfg = _settings(tmp_path, glm=GLMConfig(api_key="glm-key-for-test"))
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)
    sess.chat_model_key = GLM_KEY
    sess.vlm_model_key = "deepseek-v4-pro"
    # 模拟「Key 被清掉」:直接改 cfg(不走下拉可见性 —— 恢复路径可能绕过它)
    sess.cfg = _settings(tmp_path, glm=GLMConfig(api_key=""))

    with pytest.raises(RuntimeError, match="requires_reconfig"):
        await sess.ensure_agent()


async def test_ensure_agent_glm_vlm_without_vlm_model_raises(
    tmp_path, captured, stub_builtin_backend
):
    """VLM 选了 GLM 但 vlm_model 没配 -> 明确报错,不拿文本模型硬看图。"""
    cfg = _settings(tmp_path, glm=GLMConfig(api_key="glm-key-for-test"))
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)
    sess.chat_model_key = "deepseek-v4-pro"
    sess.vlm_model_key = GLM_KEY  # 恢复路径可能带着旧选择回来

    with pytest.raises(RuntimeError, match="requires_reconfig"):
        await sess.ensure_agent()


def calls_llm_kwargs(captured: Dict[str, Any]) -> Dict[str, Any]:
    """从捕获记录里挑出 LLM(第一个 ChatOpenAI)的构造 kwargs。"""
    kws = captured["chat_openai_kwargs"]
    assert kws, "没有构造任何 ChatOpenAI"
    return kws[0]


def calls_vlm_kwargs(captured: Dict[str, Any]) -> Dict[str, Any]:
    kws = captured["chat_openai_kwargs"]
    assert len(kws) >= 2, f"应有 LLM + VLM 两次构造,实际 {len(kws)} 次"
    return kws[1]


# --------------------------------------------------------------------------
# 秘密配置缺失上报(会话恢复路径)
# --------------------------------------------------------------------------

def test_missing_secret_config_fields_reports_glm(tmp_path):
    """选中 GLM 后配置被清 -> _missing_secret_config_fields 报 glm_llm。"""
    cfg = _settings(tmp_path, glm=GLMConfig(api_key="glm-key-for-test", vlm_model="glm-4.6v-test"))
    sess = web.ChatSession(session_id="sess-glm", cfg=cfg)
    sess.chat_model_key = GLM_KEY
    sess.vlm_model_key = GLM_KEY

    assert sess._missing_secret_config_fields() == []

    sess.cfg = _settings(tmp_path, glm=GLMConfig(api_key=""))
    missing = sess._missing_secret_config_fields()
    assert "glm_llm" in missing
    assert "glm_vlm" in missing


# --------------------------------------------------------------------------
# config 层:环境变量覆盖 + 旧配置兼容
# --------------------------------------------------------------------------

def test_load_settings_env_overrides_glm(tmp_path, monkeypatch):
    """环境变量逐字段补洞:只设 OPENSTORYLINE_GLM_API_KEY 也能用,
    model/base_url 沿用 config 默认值。"""
    monkeypatch.setenv("OPENSTORYLINE_GLM_API_KEY", "glm-key-from-env")
    monkeypatch.setenv("OPENSTORYLINE_GLM_VLM_MODEL", "glm-4.6v-from-env")
    cfg = load_settings(OPENSTORYLINE_DIR / "config.toml")

    assert cfg.glm.api_key == "glm-key-from-env"
    assert cfg.glm.vlm_model == "glm-4.6v-from-env"
    assert cfg.glm.model == "glm-4.6"
    assert cfg.glm.base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_settings_without_glm_section_still_loads(tmp_path):
    """旧 config.toml 没有 [glm] 段 -> default_factory 兜底,加载不炸。"""
    text = (OPENSTORYLINE_DIR / "config.toml").read_text(encoding="utf-8")
    assert "[glm]" in text  # 当前入库配置已带 [glm];剥掉它再加载
    before, rest = text.split("[glm]", 1)
    after = rest.split("\n[", 1)[1]
    stripped = before + "\n[" + after
    p = tmp_path / "config.toml"
    p.write_text(stripped, encoding="utf-8")

    cfg = load_settings(p)
    assert cfg.glm == GLMConfig()


def test_config_toml_glm_section_has_no_secret():
    """入库的 config.toml 里 [glm].api_key 必须为空(C-T4:密钥不入 Git,
    真实 Key 只放 config.toml.local 或环境变量)。"""
    import re

    text = (OPENSTORYLINE_DIR / "config.toml").read_text(encoding="utf-8")
    assert "[glm]" in text
    block = text.split("[glm]", 1)[1].split("\n[", 1)[0]
    m = re.search(r'api_key\s*=\s*"([^"]*)"', block)
    assert m, "[glm] 段缺少 api_key 字段"
    assert m.group(1) == "", "入库 config.toml 的 [glm].api_key 必须为空字符串"

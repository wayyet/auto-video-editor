"""阶段 E:``[zcode]`` 配置段的加载行为。

只测**配置层**。模式与 ``test_mcode_config.py`` 一致:从真实 config.toml
剥段 / 换段,不手写最小片段(Settings 其他段是必填的)。

- 旧 config.toml(没有 [zcode] 段)经 ``default_factory`` 照常加载
- ``[zcode]`` 段的值正确落进 ``Settings.zcode``
- 环境变量覆盖优先级:env > config.toml.local > config.toml
- 入库的 config.toml 不携带本机路径(中性默认值)
"""
from __future__ import annotations

from pathlib import Path

from open_storyline.config import ZCodeConfig, load_settings

OPENSTORYLINE_DIR = Path(__file__).resolve().parents[1]
REPO_CONFIG = OPENSTORYLINE_DIR / "config.toml"

_ZCODE_ENV_VARS = (
    "OPENSTORYLINE_ZCODE_CWD",
    "OPENSTORYLINE_ZCODE_BIN",
    "OPENSTORYLINE_ZCODE_MODE",
    "OPENSTORYLINE_ZCODE_MEDIA_ROOT",
)


# --------------------------------------------------------------------------
# 辅助(镜像 test_mcode_config)
# --------------------------------------------------------------------------

def _strip_zcode_section(text: str) -> str:
    """把 ``[zcode]`` 段整段删掉(连同它后面的行)。"""
    out = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped == "[zcode]"
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def _with_zcode_section(base_text: str, zcode_body: str) -> str:
    """**替换**(而不是追加)``[zcode]`` 段 —— 追加会造成 TOML 重复表报错。"""
    text = _strip_zcode_section(base_text).rstrip("\n")
    return f"{text}\n\n[zcode]\n{zcode_body}"


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _autouse_clean_zcode_env(monkeypatch) -> None:
    for var in _ZCODE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def test_settings_loads_without_zcode_section(tmp_path, monkeypatch):
    """没有 ``[zcode]`` 段的既有 config.toml 仍能加载(用默认值)。"""
    _autouse_clean_zcode_env(monkeypatch)
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _strip_zcode_section(base))

    cfg = load_settings(cfg_path)

    assert isinstance(cfg.zcode, ZCodeConfig)
    assert cfg.zcode.cwd == ""
    assert cfg.zcode.bin == ""
    assert cfg.zcode.media_root == ""
    assert cfg.zcode.mode == "edit"
    assert cfg.zcode.timeout_s == 900
    assert cfg.zcode.max_prompt_chars == 30000
    assert cfg.zcode.status_tick_s == 20.0
    assert cfg.zcode.resume_sessions is True
    assert cfg.zcode.history_turns == 10


def test_settings_loads_zcode_section(tmp_path, monkeypatch):
    _autouse_clean_zcode_env(monkeypatch)
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(
        tmp_path,
        _with_zcode_section(base, 'cwd = "E:/ws/base"\nmode = "plan"\n'),
    )

    cfg = load_settings(cfg_path)

    assert cfg.zcode.cwd == "E:/ws/base"
    assert cfg.zcode.mode == "plan"


def test_env_overrides_beat_file(tmp_path, monkeypatch):
    _autouse_clean_zcode_env(monkeypatch)
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(
        tmp_path,
        _with_zcode_section(base, 'cwd = "E:/ws/base"\nmode = "plan"\n'),
    )
    monkeypatch.setenv("OPENSTORYLINE_ZCODE_CWD", "E:/ws/from-env")
    monkeypatch.setenv("OPENSTORYLINE_ZCODE_MODE", "build")

    cfg = load_settings(cfg_path)

    assert cfg.zcode.cwd == "E:/ws/from-env"
    assert cfg.zcode.mode == "build"


def test_env_beats_local_file(tmp_path, monkeypatch):
    """优先级阶梯最高一级:env > config.toml.local > config.toml。"""
    _autouse_clean_zcode_env(monkeypatch)
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _strip_zcode_section(base))
    (cfg_path.parent / "config.toml.local").write_text(
        '[zcode]\ncwd = "E:/ws/local"\n', encoding="utf-8"
    )

    cfg_no_env = load_settings(cfg_path)
    assert cfg_no_env.zcode.cwd == "E:/ws/local"

    monkeypatch.setenv("OPENSTORYLINE_ZCODE_CWD", "E:/ws/env")
    cfg_env = load_settings(cfg_path)
    assert cfg_env.zcode.cwd == "E:/ws/env"


def test_committed_config_toml_stays_neutral():
    """入库的 config.toml 的 [zcode] 段只允许中性默认值(无本机路径)。"""
    cfg = load_settings(REPO_CONFIG)
    assert cfg.zcode.cwd == ""
    assert cfg.zcode.bin == ""
    assert cfg.zcode.media_root == ""

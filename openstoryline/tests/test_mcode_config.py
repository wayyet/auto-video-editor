"""阶段 A:``MCodeConfig`` / ``Settings.mcode`` / env 覆盖的加载行为。

只测**配置层**。``build_agent()`` 的 mcode 分支属于阶段 C,不在本轮范围。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from open_storyline.config import MCodeConfig, Settings, load_settings

OPENSTORYLINE_DIR = Path(__file__).resolve().parents[1]
REPO_CONFIG = OPENSTORYLINE_DIR / "config.toml"
REPO_GITIGNORE = OPENSTORYLINE_DIR / ".gitignore"


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _strip_mcode_section(text: str) -> str:
    """把 ``[mcode]`` 段整段删掉(连同它后面的行)。"""
    out = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped == "[mcode]"
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def _with_mcode_section(base_text: str, mcode_body: str) -> str:
    """**替换**(而不是追加)``[mcode]`` 段 —— 追加会造成 TOML 重复表报错。"""
    text = _strip_mcode_section(base_text).rstrip("\n")
    return f"{text}\n\n[mcode]\n{mcode_body}"


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def test_settings_loads_without_mcode_section(tmp_path):
    """没有 ``[mcode]`` 段的既有 config.toml 仍能加载(用默认值)。"""
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _strip_mcode_section(base))

    cfg = load_settings(cfg_path)

    assert isinstance(cfg.mcode, MCodeConfig)
    assert cfg.mcode.cwd == ""
    assert cfg.mcode.bin == ""
    assert cfg.mcode.media_root == ""
    assert cfg.mcode.mode == "standard"
    assert cfg.mcode.permission == "smart"
    assert cfg.mcode.max_steps == 120
    assert cfg.mcode.timeout_s == 900
    assert cfg.mcode.history_turns == 10
    assert cfg.mcode.max_prompt_chars == 60000


def test_mcode_section_values_are_loaded(tmp_path):
    """``[mcode]`` 段里的值能读出来。"""
    base = REPO_CONFIG.read_text(encoding="utf-8")
    body = (
        'cwd = "E:/ops/video-ops"\n'
        'bin = "C:/tools/mcode.cmd"\n'
        'mode = "lightweight"\n'
        'permission = "off"\n'
        "max_steps = 42\n"
        "timeout_s = 60\n"
        "history_turns = 3\n"
        "max_prompt_chars = 1234\n"
        'media_root = "E:/inputs"\n'
    )
    cfg_path = _write_config(tmp_path, _with_mcode_section(base, body))

    cfg = load_settings(cfg_path)

    assert cfg.mcode.cwd == "E:/ops/video-ops"
    assert cfg.mcode.bin == "C:/tools/mcode.cmd"
    assert cfg.mcode.mode == "lightweight"
    assert cfg.mcode.permission == "off"
    assert cfg.mcode.max_steps == 42
    assert cfg.mcode.timeout_s == 60
    assert cfg.mcode.history_turns == 3
    assert cfg.mcode.max_prompt_chars == 1234
    assert cfg.mcode.media_root == "E:/inputs"


def test_config_toml_local_overrides_mcode(tmp_path):
    """``config.toml.local`` 覆盖 ``config.toml`` 的 mcode 段。"""
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _with_mcode_section(base, 'cwd = "E:/from-toml"\n'))
    (tmp_path / "config.toml.local").write_text(
        '[mcode]\ncwd = "E:/from-local"\n', encoding="utf-8"
    )

    cfg = load_settings(cfg_path)

    assert cfg.mcode.cwd == "E:/from-local"


def test_env_overrides_mcode_paths(tmp_path, monkeypatch):
    """环境变量覆盖 mcode 的三个路径字段。"""
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _with_mcode_section(base, 'cwd = "E:/from-toml"\n'))

    monkeypatch.setenv("OPENSTORYLINE_MCODE_CWD", "E:/from-env-cwd")
    monkeypatch.setenv("OPENSTORYLINE_MCODE_BIN", "E:/from-env-bin")
    monkeypatch.setenv("OPENSTORYLINE_MCODE_MEDIA_ROOT", "E:/from-env-media")

    cfg = load_settings(cfg_path)

    assert cfg.mcode.cwd == "E:/from-env-cwd"
    assert cfg.mcode.bin == "E:/from-env-bin"
    assert cfg.mcode.media_root == "E:/from-env-media"


def test_env_override_beats_local_file(tmp_path, monkeypatch):
    """env 优先级最高:压过 config.toml.local。"""
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _with_mcode_section(base, 'cwd = "E:/from-toml"\n'))
    (tmp_path / "config.toml.local").write_text(
        '[mcode]\ncwd = "E:/from-local"\n', encoding="utf-8"
    )

    monkeypatch.setenv("OPENSTORYLINE_MCODE_CWD", "E:/from-env")

    cfg = load_settings(cfg_path)

    assert cfg.mcode.cwd == "E:/from-env"


def test_mcode_path_fields_stay_plain_strings(tmp_path):
    """路径字段必须是 ``str`` 而不是 ``Path``。

    ``ConfigBaseModel`` 会把 ``Path`` 字段按 config 目录解析相对路径,
    ``""`` 会被解析成 config 目录 —— 静默指向错误目录的坑。
    """
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _strip_mcode_section(base))

    cfg = load_settings(cfg_path)

    assert type(cfg.mcode.cwd) is str
    assert type(cfg.mcode.bin) is str
    assert type(cfg.mcode.media_root) is str
    # 空串必须还是空串,不能变成 config 所在目录
    assert cfg.mcode.cwd == ""
    assert cfg.mcode.bin == ""
    assert cfg.mcode.media_root == ""


def test_unknown_key_in_mcode_section_is_rejected(tmp_path):
    """``ConfigBaseModel`` 是 ``extra="forbid"``:段里写错字段名要响亮报错。"""
    base = REPO_CONFIG.read_text(encoding="utf-8")
    cfg_path = _write_config(tmp_path, _with_mcode_section(base, "cwd_typo = \"x\"\n"))

    with pytest.raises(ValidationError):
        load_settings(cfg_path)


def test_public_config_toml_has_no_local_paths_or_secrets(tmp_path):
    """入库的 ``config.toml`` 只能是中性默认值:不含本机绝对路径、不含真实密钥。

    要从**副本**加载:仓库里同目录还有 ``config.toml.local``(本机覆盖),
    直接加载真实文件会把本机路径合并进来。
    """
    text = REPO_CONFIG.read_text(encoding="utf-8")

    cfg = load_settings(_write_config(tmp_path, text))
    assert cfg.mcode.cwd == ""
    assert cfg.mcode.bin == ""
    assert cfg.mcode.media_root == ""

    # 真实 key 的典型形态不能出现
    assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", text)
    # 本机绝对路径不能出现(注释里提到目录名是可以的,值里不能有)
    for machine_path in ("E:/Documents", "E:\\Documents", "C:/Users", "C:\\Users"):
        assert machine_path not in text, f"config.toml 里不该出现本机绝对路径 {machine_path}"


def test_config_toml_local_is_gitignored():
    """本机覆盖文件必须在 .gitignore 里,否则真 key / 本机路径会被提交上去。"""
    gitignore = REPO_GITIGNORE.read_text(encoding="utf-8")
    assert "config.toml.local" in gitignore

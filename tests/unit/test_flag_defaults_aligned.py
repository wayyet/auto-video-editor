"""单测:三个接入开关的默认值,以及"同一个开关两份拷贝"必须一致(2026-10)。

背景:AI 转场开关在代码里有**两份**读取点:
- ``config.STORYLINE_ENABLE_AI_TRANSITION``(配置层 / .env.example 文档化的那份)
- ``storyline_capabilities.ai_transition_client.ENABLE_AI_TRANSITION``(节点和路由真正读的那份)
只改一份会出现"文档说开、实际是关"。这里锁死两份默认值相同。

⚠️ 必须在**干净子进程**里读默认值:
- 开发机上可能设了环境变量。
子进程里先把这三个变量从环境里摘掉,读到的才是代码里写死的默认值。

注:``clean_cache`` 不接图(只由 Web「清理缓存」按钮触发),因此没有开关。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FLAG_ENV_NAMES = (
    "STORYLINE_ENABLE_WEB_TOPIC",
    "STORYLINE_ENABLE_ASR_BRANCH",
    "STORYLINE_ENABLE_AI_TRANSITION",
)

_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import config
from storyline_capabilities import ai_transition_client as c
print(json.dumps({
    "STORYLINE_ENABLE_WEB_TOPIC": config.STORYLINE_ENABLE_WEB_TOPIC,
    "STORYLINE_ENABLE_ASR_BRANCH": config.STORYLINE_ENABLE_ASR_BRANCH,
    "STORYLINE_ENABLE_AI_TRANSITION": config.STORYLINE_ENABLE_AI_TRANSITION,
    "client.ENABLE_AI_TRANSITION": c.ENABLE_AI_TRANSITION,
}))
"""


def _probe(env_overrides: dict[str, str] | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in FLAG_ENV_NAMES}
    env.update(env_overrides or {})
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(ROOT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=120,
        check=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_all_three_flags_default_to_on_and_ai_copies_agree() -> None:
    got = _probe()
    assert got["STORYLINE_ENABLE_WEB_TOPIC"] is True
    assert got["STORYLINE_ENABLE_ASR_BRANCH"] is True
    assert got["STORYLINE_ENABLE_AI_TRANSITION"] is True
    assert got["client.ENABLE_AI_TRANSITION"] is True
    assert got["STORYLINE_ENABLE_AI_TRANSITION"] == got["client.ENABLE_AI_TRANSITION"]


@pytest.mark.parametrize("off_value", ["0", "false", "no", "FALSE", " 0 "])
def test_zero_turns_each_flag_off_including_both_ai_copies(off_value: str) -> None:
    got = _probe({name: off_value for name in FLAG_ENV_NAMES})
    assert got == {
        "STORYLINE_ENABLE_WEB_TOPIC": False,
        "STORYLINE_ENABLE_ASR_BRANCH": False,
        "STORYLINE_ENABLE_AI_TRANSITION": False,
        "client.ENABLE_AI_TRANSITION": False,
    }


@pytest.mark.parametrize("name", FLAG_ENV_NAMES)
def test_each_flag_is_independent(name: str) -> None:
    """只关一个,其余两个保持开(开关互不牵连)。"""
    got = _probe({name: "0"})
    key = "STORYLINE_ENABLE_AI_TRANSITION" if name == "STORYLINE_ENABLE_AI_TRANSITION" else name
    assert got[key] is False
    others = {k: v for k, v in got.items() if k not in (key, "client.ENABLE_AI_TRANSITION")}
    assert all(v is True for v in others.values())

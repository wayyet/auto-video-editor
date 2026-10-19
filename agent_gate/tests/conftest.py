import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "fixtures"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 子进程(worker / mcp 服务)也要能 import 到这两个目录
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(ROOT), str(ROOT / "fixtures")] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
)
os.environ.setdefault("AGENT_GATE_HEARTBEAT_S", "0.3")
os.environ.setdefault("AGENT_GATE_STALE_AFTER_S", "1.5")


import pytest  # noqa: E402


@pytest.fixture
def mock(tmp_path):
    """用法:srv = mock({...脚本...});测试结束自动关掉。"""
    from support import MockServer

    made = []

    def make(script: dict):
        m = MockServer(tmp_path, script)
        made.append(m)
        return m

    yield make
    for m in made:
        m.close()

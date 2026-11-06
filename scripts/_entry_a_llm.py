"""入口 A(直接 CLI)的 LLM 后端提示 —— 附件评审 20261105 阶段3G / 决策 D3-A。

入口 A(``scripts/run_workflow.py`` / ``scripts/resume_workflow.py``)定位为
开发 / 冒烟:未显式指定 ``--llm-backend`` 时,LLM 关卡答题使用
StubLLMClient(假答案)。红线5「生产不得静默使用 stub」要求至少打印提示;
若调用方还设置了 ``VIDEO_LLM_BACKEND``(该变量只有入口 B 的 ``agent_gate``
读取,入口 A 不读)且值不是 ``stub``,继续跑只会得到假答案,按评审要求
以退出码 2 拒跑。

run_workflow.py 与 resume_workflow.py 都以
``from scripts._entry_a_llm import llm_backend_notice`` 导入本模块
(scripts/ 无 __init__.py,靠仓库根进 sys.path 的 namespace package 解析;
scripts 直接运行时 sys.path[0] 是 scripts/,故两脚本顶部已先插仓库根),
保证脚本与测试拿到同一个模块实例。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

VIDEO_LLM_BACKEND_ENV = "VIDEO_LLM_BACKEND"

#: 未指定 --llm-backend 时的提示(打 stderr,不污染 stdout 的机器可读输出)
_NOTICE_NO_BACKEND = (
    "[llm] 未指定 --llm-backend,LLM 关卡答题将使用 stub 假答案"
    "(仅用于开发/冒烟);真实运行请用 agent_gate drive 或工作流创建台。"
)

#: 设了 VIDEO_LLM_BACKEND 且非 stub、又未显式指定 --llm-backend 时的拒跑说明
_REJECT_TEMPLATE = (
    "[llm] 检测到环境变量 {env}={value!r},但入口 A 不读取该变量"
    "(只有 agent_gate 读取);继续跑只会得到 stub 假答案,已拒绝执行。"
    "如需真实模型,请显式传 --llm-backend <stub|claude-cli|mcode-cli>。"
)


def llm_backend_notice(explicit_backend: Optional[str]) -> int:
    """打印入口 A 的 LLM 后端提示。

    Args:
        explicit_backend: ``--llm-backend`` 的显式取值;None/空 = 未指定。

    Returns:
        ``0`` = 继续;``2`` = 拒跑(设了非 stub 的 ``VIDEO_LLM_BACKEND``
        且未显式指定 ``--llm-backend``)。
    """
    if explicit_backend:
        return 0  # 显式指定(含 stub),尊重调用方,不打扰
    print(_NOTICE_NO_BACKEND, file=sys.stderr)
    env_backend = os.environ.get(VIDEO_LLM_BACKEND_ENV, "").strip()
    if env_backend and env_backend != "stub":
        print(
            _REJECT_TEMPLATE.format(env=VIDEO_LLM_BACKEND_ENV, value=env_backend),
            file=sys.stderr,
        )
        return 2
    return 0

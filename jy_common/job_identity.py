"""统一任务 ID(job_id)解析 —— node_09 / node_10 / node_10b 共用的公共工具层。

2026-11-18 池化轮换缺陷修复(审查文档:《字幕VIP动画job_id链路_设计审查与修订执行计划
_20261118》§4):三个节点的轮换 seed 语义 = 逻辑任务 thread_id,但此前各自解析:

- thread_id 是 LangGraph **config 参数**(``config["configurable"]["thread_id"]``),
  不是 state 键;``state.get("thread_id")`` 恒空 → zh 兜底成中文草稿目录名
  (``AutoVideo_console-xxx``)、en 兜底成 ``Path(draft_dir_en_branch).parent.name``
  = 草稿库根名 ``com.lveditor.draft``(恒定 → 英文轮换失效),zh/en 动画不一致。

本模块收敛解析优先级,三节点共用;节点之间不得互相导入,只依赖本公共层::

    config["configurable"]["thread_id"]      ← LangGraph 执行上下文(权威;
                                                run_worker / run_workflow 均已传入)
      → state["thread_id"]                   ← 兼容显式携带该字段的调用方
      → state["session_id"]                  ← 旧 checkpoint 兼容(Console /
                                                Agent Gate / 普通 CLI 三入口
                                                都写 session_id = thread_id)
      → state["draft_path"] 父目录名          ← 末级兜底(剥 "AutoVideo_" 前缀,
                                                项目正式目录约定 AutoVideo_<tid>)
      → raise ValueError                     ← 显式失败;调用方按各自降级契约处理,
                                                禁止静默退化为共享根目录名或固定
                                                "unknown" 之类伪任务 ID

job_id 语义:逻辑任务 thread_id(同一任务重放、人工恢复、崩溃续跑、zh/en 分支
必须得到同一值),不是单次 run_id 或恢复 attempt_id。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: 正式草稿目录名约定前缀(``AutoVideo_<thread_id>``);路径兜底时剥掉,
#: 使目录名兜底与 thread_id 语义对齐(如 ``AutoVideo_console-abc`` → ``console-abc``)。
_DRAFT_DIR_PREFIX = "AutoVideo_"


def _norm(value: Any) -> str:
    """把任意取值归一成 strip 后的字符串;None/空 返回 ``""``。"""
    if value is None:
        return ""
    return str(value).strip()


def resolve_job_id(
    state: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """解析当前任务的稳定 job_id(= 逻辑任务 thread_id),优先 LangGraph config。

    Args:
        state: 当前工作流 state(节点入参;允许 ``None`` 便于纯函数测试)。
        config: LangGraph ``RunnableConfig``(节点第二参数;缺省 ``None``)。

    Returns:
        非空的任务 ID 字符串。

    Raises:
        ValueError: 四级来源全部不可用时抛出 —— 调用方(node_09/10/10b)按各自
            降级契约显式处理(记 error_log 并跳过注入),不允许静默使用固定 seed。
    """
    merged_state: Mapping[str, Any] = state or {}

    # 1) LangGraph 执行上下文:config["configurable"]["thread_id"](权威源)
    if isinstance(config, Mapping):
        configurable = config.get("configurable")
        if isinstance(configurable, Mapping):
            value = _norm(configurable.get("thread_id"))
            if value:
                return value

    # 2/3) state 兼容键:显式 thread_id → 旧入口统一写入的 session_id
    for key in ("thread_id", "session_id"):
        value = _norm(merged_state.get(key))
        if value:
            return value

    # 4) 末级兜底:中文草稿目录名(约定 AutoVideo_<thread_id>,剥前缀对齐语义)
    draft_path = merged_state.get("draft_path")
    if draft_path:
        folder = Path(str(draft_path)).parent.name
        if folder.startswith(_DRAFT_DIR_PREFIX):
            folder = folder[len(_DRAFT_DIR_PREFIX) :]
        value = folder.strip()
        if value:
            return value

    raise ValueError(
        "无法解析当前任务 ID:configurable.thread_id / state.thread_id / "
        "state.session_id / state.draft_path 均不可用"
    )

"""关卡(人工 interrupt)节点的公共样板(冗余收敛 R4)。

关卡 ⓪ / ① / ② 三处原本是同构的三段式::

    interrupt(_build_interrupt_payload(state))
    return _post_resume(state)

差异只有 4 个参数:``checkpoint`` 标识(⓪/①/②)、``legacy_id``、``step``、
以及 resume 后要写的通知/标记。本模块把前两段里真正重复的部分抽成两个纯函数,
节点侧只保留"自己的一行 interrupt 调用 + 各自的薄封装"。

⚠️ **payload 字段不能改** — ``resume_utils.py::resume_all_pending`` 用
``i.value["checkpoint"]`` 做自动匹配(``values_by_checkpoint`` 表的键是
"①"/"②"/"③"),``legacy_id`` 是 Week 5 之前的旧名(向后兼容人工脚本),
``step`` 是关卡在流程里的序号。任何一个字段改名/改值,多关卡自动恢复会
**静默失配**:不报错,只是永远匹配不上,人只能看着图挂死。

⚠️ **为什么没有"返回节点可调用对象的工厂"** — 工厂闭包会在 ``_checkpoint``
模块里调 ``interrupt``,于是单测的 ``patch("nodes.node_06_human_reorder.interrupt")``
会打空(打桩的是节点模块的名字,不是工厂的),而且 ``graph.py`` 注册的将是一批
匿名闭包,LangGraph 追踪里看不到节点真名。所以本模块只收敛**纯函数**,
``interrupt()`` 仍在各节点模块里调用 —— 测试打桩路径与图可读性都不受影响。

``node_checkpoint3_layout_review`` **有意不纳入**:它 resume 后多一步 SRT 重写,
返回值是 delta 字典(不是 ``{**state}`` 全量),结构已偏离样板,硬套工厂会降低
可读性。
"""
from __future__ import annotations

from typing import Any, Callable

from state import WorkflowState


# ---------------------------------------------------------------------------
# 第一段:构造 interrupt payload
# ---------------------------------------------------------------------------
def build_interrupt_payload(
    *,
    checkpoint: str,
    legacy_id: str,
    step: int,
    instructions: str,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造关卡 interrupt payload(纯函数,无副作用)。

    Args:
        checkpoint: 关卡标识,如 ``"⓪"`` / ``"①"`` / ``"②"``。
            **必须逐字保持** —— ``resume_all_pending`` 按它匹配 resume 值。
        legacy_id: 旧版标识(Week 5 之前命名),保留给存量人工脚本。
        step: 关卡在流程中的序号(⓪=4 / ①=6 / ②=12)。
        instructions: 展示给人看的操作指引。
        extra_payload: 关卡特有的附加字段(如 ``draft_path`` /
            ``openstoryline_web_url``)。渲染位置在 ``step`` 与
            ``instructions`` 之间,与收敛前各节点的字面顺序一致。
            **不允许覆盖** ``checkpoint`` / ``legacy_id`` / ``step`` 三个契约键。

    Returns:
        payload dict,键序为
        ``checkpoint → legacy_id → step → <extra> → instructions``。
    """
    payload: dict[str, Any] = {
        "checkpoint": checkpoint,
        "legacy_id": legacy_id,
        "step": step,
        **(extra_payload or {}),
        "instructions": instructions,
    }
    # 契约键回写:上面展开 extra 时可能把它们顶掉,而这三个键一旦被改值就会
    # 静默破坏 resume_all_pending 的自动匹配。给已存在的键重新赋值**不改变
    # 字典键序**,所以既守住取值,又保住与收敛前一致的字面顺序。
    payload["checkpoint"] = checkpoint
    payload["legacy_id"] = legacy_id
    payload["step"] = step
    return payload


# ---------------------------------------------------------------------------
# 第二段:resume 后收尾
# ---------------------------------------------------------------------------
def post_resume(
    state: WorkflowState,
    *,
    log_tag: str,
    notifier: Callable[[WorkflowState, str], None] | None = None,
    notified_field: str | None = None,
) -> dict[str, Any]:
    """resume 后的收尾(纯函数):可选手动通知 + 追加 status_log + 写通知标记。

    Args:
        state: LangGraph 传入的 state。
        log_tag: 追加到 ``status_log`` 的 tag,如 ``"checkpoint1_resumed"``。
        notifier: 通知函数 ``(state, checkpoint_tag) -> None``;``None`` 表示
            本关卡不发通知(如关卡 ⓪ 只需写日志)。
        notified_field: 非空时把该 state 字段置 ``True``,作为"已通知"审计位
            (Week 2 测试依赖 ``reorder_notified`` / ``bgm_notified``)。

    Returns:
        ``{**state, notified_field: True, status_log: [...]}``;``notified_field``
        为 ``None`` 时不写该键。只追加 ``status_log``,不覆盖既有条目,也不动
        其他字段 —— 保证重放幂等。
    """
    out: dict[str, Any] = {**state}
    if notifier is not None:
        # 通知 tag = log_tag 去掉 "_resumed" 后缀("checkpoint1_resumed" → "checkpoint1")
        notifier(state, log_tag.removesuffix("_resumed"))
    out["status_log"] = list(state.get("status_log") or []) + [log_tag]
    if notified_field:
        out[notified_field] = True
    return out


__all__ = [
    "build_interrupt_payload",
    "post_resume",
]

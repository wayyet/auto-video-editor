"""tests/unit 全局密封:真实写草稿路径默认注入"剪映不在跑"。

节点单测(node_05/07/08/09/10/11/12/13/16/16a、publish wiring)大量经
``safe_write_draft`` / ``publish_draft`` 真实写盘(快照/双写/verify 都是真
执行),唯一的环境依赖是两者的 ``_default_proc_checker`` 默认值 —— 剪映
客户端开着时写入互斥严格模式直接 raise,全量回归结果随剪映开关状态抖动
(2026-11-08 实测:剪映开着 → 45 例环境性失败;关着 → 全绿)。

这里把两个默认 proc_checker 桩成"不在跑":

- 显式传 ``proc_checker`` 的用例(test_atomic_writer 的互斥正反例)不受
  影响 —— 用例级 monkeypatch 在本 fixture 之后生效,用例级优先;
- 写入互斥/进程检测行为本身由 test_safe_write_guard / test_atomic_writer
  单独覆盖,这里不背验证职责。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_jianying_mutex_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import draft_ops.atomic_writer as _aw
    import draft_ops.draft_publisher as _pub

    monkeypatch.setattr(_aw, "_default_proc_checker", lambda: False)
    monkeypatch.setattr(_pub, "_default_proc_checker", lambda: False)

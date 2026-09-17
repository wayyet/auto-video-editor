"""节点 3:open_preview — 无独立 Skill,编排引擎直接动作(附件 1.4 节)。

两种打开方式:
- 人工交互(unattended=False):subprocess.Popen 启动独立 Edge 窗口
- 无人值守(unattended=True):Playwright 以 channel='msedge' 驱动本机
  Edge 内核,headless=True
"""

from __future__ import annotations

import subprocess
from typing import Any

from state import WorkflowState

# 唯一哨兵:标记 ``popen_factory`` 未显式传入。函数体执行时才解析
# ``subprocess.Popen``,这样 ``monkeypatch.setattr(m3, "subprocess", fake)``
# 才会真正被拾取(对照验证报告 §5.3)。哨兵不能与任何真实可调用对象冲突。
_DEFAULT_POPEN = object()


def open_preview(
    state: WorkflowState,
    unattended: bool = False,
    *,
    popen_factory=_DEFAULT_POPEN,
    playwright_factory: callable | None = None,
    edge_browser: str = "msedge",
) -> dict:
    """打开 OpenStoryline Web 预览。

    Args:
        state: 当前工作流状态。
        unattended: True 走 Playwright 无人值守分支;False 走人工交互分支。
        popen_factory: 人工交互分支可注入的 subprocess.Popen 工厂。
            默认在函数体内**运行时**读取模块级 ``subprocess.Popen``,
            便于测试通过 ``monkeypatch.setattr(m3, "subprocess", fake)``
            真正替换底层调用(对照验证报告 §5.3)。若调用方显式传入,
            则直接使用传入的工厂。
        playwright_factory: 无人值守分支可注入的 Playwright 工厂,
            接受 None 参数返回 context manager;None 时尝试懒加载 playwright。
        edge_browser: 浏览器可执行文件名(默认 msedge)。
    """
    errors = list(state.get("error_log", []) or [])
    web_url = state.get("openstoryline_web_url")
    if not web_url:
        errors.append("[node_03] openstoryline_web_url 为空,无法打开预览")
        return {**state, "preview_opened": False, "error_log": errors}

    if not unattended:
        # 人工交互场景:直接打开独立 Edge 窗口
        # 运行时解析 subprocess.Popen —— 让 monkeypatch 真正生效
        factory = subprocess.Popen if popen_factory is _DEFAULT_POPEN else popen_factory
        try:
            factory(["cmd", "/c", "start", "", edge_browser, web_url])
        except Exception as e:  # noqa: BLE001
            errors.append(f"[node_03] 启动 Edge 失败: {e}")
            return {**state, "preview_opened": False, "error_log": errors}
    else:
        # 无人值守场景:Playwright 驱动本机 Edge 内核
        try:
            factory = playwright_factory or _default_playwright
            with factory() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page()
                    page.goto(web_url)
                finally:
                    browser.close()
        except Exception as e:  # noqa: BLE001
            errors.append(f"[node_03] Playwright 打开预览失败: {e}")
            return {**state, "preview_opened": False, "error_log": errors}

    return {**state, "preview_opened": True, "error_log": errors}


def _default_playwright() -> Any:
    """懒加载 playwright.sync_api,失败时抛出便于上层捕获。"""
    from playwright.sync_api import sync_playwright

    return sync_playwright()

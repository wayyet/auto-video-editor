"""节点 3 单测(附件 1.4 节要点):人工交互与无人值守两条分支均 mock。"""

from __future__ import annotations

import pytest

from nodes.node_03_open_preview import open_preview


def test_manual_branch_calls_edge_with_url() -> None:
    """unattended=False → subprocess.Popen 收到 cmd /c start msedge <url>。"""
    state = {"openstoryline_web_url": "http://127.0.0.1:8005", "error_log": []}
    calls: list = []

    def fake_popen(argv, *args, **kwargs):
        calls.append(argv)
        return None

    out = open_preview(state, unattended=False, popen_factory=fake_popen)
    assert out["preview_opened"] is True
    assert len(calls) == 1
    argv = calls[0]
    # cmd /c start "" msedge <url> 形式(start 后空标题避免被当成 URL)
    assert argv[0:2] == ["cmd", "/c"]
    assert "msedge" in argv
    assert "http://127.0.0.1:8005" in argv


def test_unattended_branch_uses_playwright() -> None:
    """unattended=True → playwright.chromium.launch(channel='msedge', headless=True) 且 page.goto(url)。"""
    state = {"openstoryline_web_url": "http://127.0.0.1:8005", "error_log": []}

    class _StubPage:
        def __init__(self):
            self.goto_url: str | None = None

        def goto(self, url):
            self.goto_url = url

    class _StubBrowser:
        def __init__(self):
            self.closed = False
            self.page = _StubPage()

        def new_page(self):
            return self.page

        def close(self):
            self.closed = True

    class _StubChromium:
        def __init__(self):
            self.browser: _StubBrowser | None = None
            self.launch_kwargs = None

        def launch(self, **kwargs):
            self.launch_kwargs = kwargs
            self.browser = _StubBrowser()
            return self.browser

    class _StubPW:
        def __init__(self):
            self.chromium = _StubChromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    pw_instance = _StubPW()

    def pw_factory():
        return pw_instance

    out = open_preview(
        state,
        unattended=True,
        playwright_factory=pw_factory,
    )
    assert out["preview_opened"] is True
    assert pw_instance.chromium.launch_kwargs == {"channel": "msedge", "headless": True}
    assert pw_instance.chromium.browser.page.goto_url == "http://127.0.0.1:8005"
    assert pw_instance.chromium.browser.closed is True


def test_missing_web_url_returns_false() -> None:
    """openstoryline_web_url 缺失 → preview_opened=False,error_log 有记录。"""
    state = {"error_log": []}
    out = open_preview(state, unattended=False, popen_factory=lambda *a, **k: None)
    assert out["preview_opened"] is False
    assert any("openstoryline_web_url 为空" in e for e in out["error_log"])

"""集成守护测试:验证图**自己**绝不会自动导入视频(2026-10 拆分 node_04)。

背景:需求要求"只有人点击网页【📥 导入视频】按钮才会执行导入视频,项目自身
(LangGraph 图)绝不自动导入"。本测试把这条要求从"约定"变成**可执行的断言**。

与 ``test_no_autoclean_cache.py`` 同构(clean_cache 按钮化改造的守护测试)。

断言三件事:
1. 跑一次 human 模式图,``load_media`` / ``cap_load_media`` 调用计数 == 0
   ——图没有任何自动导入动作;
2. ``import_video_result.json`` **不存在** ——图没有自己伪造"导入成功";
3. ``write_import_request`` 被调用过 ——节点确实发出了等待人工的信号;
   且超时后请求文件已被清掉(不留陈旧请求骗下一次点击)。
另外校验图里已无 ``import_and_plan``,且新两节点已注册。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import config
from graph import build_graph
from nodes.node_04a_import_video import (
    find_pending_request,
    import_video,
    import_video_request_filename,
    import_video_result_filename,
)
from storyline.output_isolation import OutputJobPaths


# ---------------------------------------------------------------------------
# 计数桩
# ---------------------------------------------------------------------------
@pytest.fixture
def load_media_spy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把两处 ``load_media`` 打成计数桩,返回计数容器。"""
    calls = {"n": 0}

    def _spy(*_args, **_kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        raise AssertionError("图不应自动调用 load_media —— 导入只能由网页按钮触发")

    # auto-mode 19 节点图侧
    import storyline_capabilities.load_media as cap_lm

    monkeypatch.setattr(cap_lm, "load_media", _spy)
    # human-mode 节点侧(nodes/storyline/node_load_media.py 里是 from ... import)
    try:
        import nodes.storyline.node_load_media as node_lm
    except ImportError:
        node_lm = None  # type: ignore[assignment]
    if node_lm is not None and hasattr(node_lm, "cap_load_media"):
        monkeypatch.setattr(node_lm, "cap_load_media", _spy)
    return calls


# ---------------------------------------------------------------------------
# 守护断言
# ---------------------------------------------------------------------------
def test_graph_never_auto_imports_video(
    load_media_spy: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑完 human 主链:图零自动导入,且只发请求、绝不自己写"结果"。"""
    # 隔离 outputs 根,避免污染真实 outputs/
    outputs_root = tmp_path / "outputs"
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: outputs_root)

    # 计数桩:证明节点确实走到了"发请求"这一步。
    # (超时后请求文件会被清掉,所以只能靠计数证明,不能靠文件存在)
    import nodes.node_04a_import_video as n4a

    real_write_request = n4a.write_import_request
    req_calls = {"n": 0}

    def spy_write_request(job_id, root=None):  # noqa: ANN001
        req_calls["n"] += 1
        return real_write_request(job_id, root=root)

    monkeypatch.setattr(n4a, "write_import_request", spy_write_request)

    # 关卡⓪ 旁路:本测试要的是"图自动往下走",不测人工 resume
    import nodes.node_checkpoint0_storyline_plan as cp0
    import graph as _graph

    passthrough = lambda state: cp0._post_resume(state)  # noqa: E731
    monkeypatch.setattr(cp0, "checkpoint0_wait_storyline_plan", passthrough)
    if hasattr(_graph, "checkpoint0_wait_storyline_plan"):
        monkeypatch.setattr(_graph, "checkpoint0_wait_storyline_plan", passthrough)

    job_id = "no-auto-import"
    g = build_graph(
        checkpointer=InMemorySaver(),
        start_heartbeat_thread=False,
    )
    cfg = {"configurable": {"thread_id": job_id}}
    out = g.invoke(
        {
            "session_id": job_id,
            "video_input_path": str(tmp_path / "fake.mp4"),
            "error_log": [],
        },
        config=cfg,
    )

    # 1. 图零自动导入
    assert load_media_spy["n"] == 0, (
        f"图不应自动导入视频,但 load_media 被调用了 {load_media_spy['n']} 次"
    )

    paths = OutputJobPaths.for_job(job_id=job_id, root=outputs_root)
    # 2. 图没有自己伪造导入结果(只有网页端点能写这个文件)
    assert not (paths.root / import_video_result_filename).exists(), (
        "图不应自己写 import_video_result.json —— 那等于伪造了导入成功"
    )
    # 3. 节点确实发出了等待人工的信号
    assert req_calls["n"] == 1, (
        f"import_video 节点应发 1 次请求,实际 {req_calls['n']} 次"
    )
    # 3b. 超时后请求被清掉 —— 不给下一次误点留"陈旧 job"
    assert not (paths.root / import_video_request_filename).exists(), (
        "超时后应清掉 import_video_request.json,否则陈旧请求会骗到下一次点击"
    )
    # 4. 没有回滚开关时,人一直不点 → timeout 并继续走降级(不抛异常)
    assert out.get("import_video_status") == "timeout", (
        f"未点按钮时状态应是 timeout,实际 {out.get('import_video_status')!r}"
    )
    assert out.get("import_video_media_path") is None


def test_graph_has_new_nodes_not_old():
    """图里 ``import_and_plan`` 已消失,新两节点已注册。"""
    g = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    node_names = set(g.get_graph().nodes)

    assert "import_video" in node_names
    assert "get_storyboard_plan" in node_names
    assert "import_and_plan" not in node_names, (
        "旧 import_and_plan 节点应已被 04a/04b 拆掉"
    )


def test_manual_required_default_is_true(monkeypatch: pytest.MonkeyPatch):
    """默认必须"必须手动"——回滚开关不得被改成默认开启。"""
    # 直接看 config 声明的默认值(env 未设置时的字面量)
    import importlib

    assert config.IMPORT_VIDEO_MANUAL_REQUIRED is True
    # 顺带确认 reload 时不会因 env 缺省而翻成 False
    reloaded = importlib.reload(config)
    assert reloaded.IMPORT_VIDEO_MANUAL_REQUIRED is True


# ---------------------------------------------------------------------------
# 端到端握手:模拟"人点了一次按钮"
# ---------------------------------------------------------------------------
def test_button_click_unblocks_graph_and_import_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图在 ``import_video`` 阻塞 → 网页端回写结果 → 图自动继续到 ``imported``。

    这里**不启动浏览器**,而是直接调用端点内部用的那两个函数
    (``find_pending_request`` + ``write_import_result``),也就是
    ``POST /api/system/import-video`` 的全部实质逻辑。这样 CI 就能守住
    "点一次按钮 ⇒ 导入完成 + 图自动继续"(验收 A5)这条端到端契约。
    """
    import threading

    outputs_root = tmp_path / "outputs"
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: outputs_root)
    # 这次要真的等一会儿(而不是被 conftest 压成 0)
    monkeypatch.setattr(config, "IMPORT_VIDEO_WAIT_TIMEOUT_S", 30)
    monkeypatch.setattr(config, "IMPORT_VIDEO_POLL_INTERVAL_S", 0.05)

    import nodes.node_checkpoint0_storyline_plan as cp0
    import graph as _graph

    passthrough = lambda state: cp0._post_resume(state)  # noqa: E731
    monkeypatch.setattr(cp0, "checkpoint0_wait_storyline_plan", passthrough)
    if hasattr(_graph, "checkpoint0_wait_storyline_plan"):
        monkeypatch.setattr(_graph, "checkpoint0_wait_storyline_plan", passthrough)

    job_id = "e2e-import"
    g = build_graph(checkpointer=InMemorySaver(), start_heartbeat_thread=False)
    cfg = {"configurable": {"thread_id": job_id}}
    result: dict = {}

    def run_graph() -> None:
        result["out"] = g.invoke(
            {
                "session_id": job_id,
                "video_input_path": str(tmp_path / "demo.mp4"),
                "error_log": [],
            },
            config=cfg,
        )

    t = threading.Thread(target=run_graph, daemon=True)
    t.start()

    # 等图的 import_video 节点发出请求(这正是网页按钮被点后的第一步)
    import nodes.node_04a_import_video as n4a

    deadline = time.monotonic() + 20
    pending = None
    while time.monotonic() < deadline:
        pending = n4a.find_pending_request(root=outputs_root)
        if pending is not None:
            break
        time.sleep(0.05)
    assert pending is not None, "图没有在 20s 内发出 import_video_request.json"

    # === 模拟 POST /api/system/import-video 的实质逻辑 ===
    answered_job, _req = pending
    assert answered_job == job_id
    n4a.write_import_result(
        answered_job,
        {
            "ok": True,
            "web_session_id": "websid-e2e",
            "media_id": "media_0001",
            "filename": "demo.mp4",
            "stored_path": str(tmp_path / "media" / "media_0001.mp4"),
            "triggered_by": "import_video_button",
        },
        root=outputs_root,
    )

    t.join(timeout=30)
    assert not t.is_alive(), "回写结果后图应自动继续,不该继续阻塞"

    out = result["out"]
    assert out.get("import_video_status") == "imported"
    assert out.get("import_video_web_session_id") == "websid-e2e"
    assert out.get("import_video_media_path") == str(tmp_path / "media" / "media_0001.mp4")
    assert out.get("import_video_filename") == "demo.mp4"
    assert "import_video_done" in out.get("status_log", [])
    # 图继续走完了后面的节点(不是卡在 import_video)
    assert "get_storyboard_plan" in str(out) or out.get("storyline_session_id") is not None
    # 已应答的 job 不再是"待处理",重复点不会误命中
    assert n4a.find_pending_request(root=outputs_root) is None


def test_rollback_switch_restores_auto_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``IMPORT_VIDEO_MANUAL_REQUIRED=False`` → 不等按钮,自己导入(验收 A6)。"""
    outputs_root = tmp_path / "outputs"
    monkeypatch.setattr(config, "storyline_outputs_root", lambda: outputs_root)
    monkeypatch.setattr(config, "IMPORT_VIDEO_MANUAL_REQUIRED", False)

    out = import_video(
        {
            "session_id": "rollback",
            "video_input_path": "E:/clips/a.mp4",
            "error_log": [],
        },
        outputs_root=outputs_root,
    )
    assert out["import_video_status"] == "imported"
    assert out["import_video_media_path"] == "E:/clips/a.mp4"
    # 回滚路径没发请求,所以网页端不该有待应答 job
    assert find_pending_request(root=outputs_root) is None

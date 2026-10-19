"""F1:thread_id / task_id 白名单 + 路径归属。

覆盖两件事:
1. 一批具体的恶意输入,在**每个**必须重复校验的调用点上都进不来;
2. 合法输入照常工作(不因为加固把正常路径也堵死)。
"""
from __future__ import annotations

import pytest

from agent_gate import runner, service
from agent_gate.mailbox import Mailbox
from agent_gate.safety import (
    UnsafeIdentifier,
    UnsafePath,
    safe_gate_root,
    safe_task_id,
    safe_thread_id,
)

BAD_THREAD_IDS = [
    "../x",              # POSIX 上跳
    "..\\x",             # Windows 上跳
    "/tmp/x",            # 绝对路径冒充
    "C:\\Windows\\x",     # 绝对路径冒充
    "%2e%2e/x",          # URL 编码的上跳(不 decode 就不是路径分隔符,更该拒)
    "",                  # 空串
    "a" * 65,            # 超长
    "has space",         # 含空格
    "中文id",             # 非 ASCII
    ".hidden",           # 以点开头
    "a/b",               # 含分隔符
    "a\\b",
    "-lead",             # 只能字母数字开头
]
BAD_TASK_IDS = [
    "t_" + "0" * 15,          # 少一位
    "t_" + "0" * 17,          # 多一位
    "t_" + "A" * 16,          # 大写 hex
    "t_seed0",                # 不符合形状
    "task_" + "0" * 16,
    "../t_" + "0" * 16,       # 上跳
    "..\\t_" + "0" * 16,
    "",
    "t_" + "0" * 16 + "/x",
]


# ------------------------------------------------------------------ 白名单本身
@pytest.mark.parametrize("bad", BAD_THREAD_IDS)
def test_thread_id_whitelist_rejects(bad):
    with pytest.raises(UnsafeIdentifier):
        safe_thread_id(bad)


@pytest.mark.parametrize("bad", BAD_TASK_IDS)
def test_task_id_whitelist_rejects(bad):
    with pytest.raises(UnsafeIdentifier):
        safe_task_id(bad)


@pytest.mark.parametrize("good", ["a", "A1", "smoke-stub", "job1", "t_1",
                                  "0", "a" * 64, "My-Thread_2026"])
def test_thread_id_whitelist_accepts(good):
    assert safe_thread_id(good) == good


def test_task_id_whitelist_accepts_shape_from_protocol():
    """真实 task_id(task_id_for 的产出)必须过白名单 —— 这是它能工作的前提。"""
    from agent_gate.protocol import task_id_for

    tid = task_id_for({"system_prompt": "s", "user_prompt": "u"})
    assert safe_task_id(tid) == tid


# ------------------------------------------------------------------ 路径归属
def test_gate_root_stays_under_outputs(tmp_path):
    root = safe_gate_root(tmp_path, "job1")
    assert root == (tmp_path.resolve() / "job1" / "agent_gate")
    assert root.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("bad", BAD_THREAD_IDS)
def test_gate_root_rejects_bad_thread_id(tmp_path, bad):
    with pytest.raises((UnsafeIdentifier, UnsafePath)):
        safe_gate_root(tmp_path, bad)


def test_gate_root_is_idempotent_under_symlink(tmp_path):
    """outputs_root 指向一个符号链接时,归属判断要按 resolve 后的真实位置算。"""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机不支持建符号链接")
    assert safe_gate_root(link, "job1").is_relative_to(real.resolve())


# ------------------------------------------------------------------ 重复校验的调用点
def test_mailbox_path_methods_reject(tmp_path):
    mb = Mailbox(runner.gate_root(tmp_path, "job1"))
    good = "t_" + "0" * 16
    assert mb.task_path(good).parent == mb.tasks_dir
    assert mb.frames_dir(good).parent == mb.frames_root

    for bad in BAD_TASK_IDS:
        for fn in (mb.task_path, mb.result_path, mb.error_path, mb.attempts_path, mb.frames_dir):
            with pytest.raises((UnsafeIdentifier, UnsafePath)):
                fn(bad)


def test_service_submit_result_rejects_bad_task_id(tmp_path):
    mb = Mailbox(runner.gate_root(tmp_path, "job1"))
    res = service.submit_result(mb, "../evil", "anything")
    assert res["ok"] is False and res["code"] == "bad_task_id"
    assert mb.read_events(10)[-1]["event"] == "input_rejected"


def test_service_get_task_rejects_bad_task_id(tmp_path):
    mb = Mailbox(runner.gate_root(tmp_path, "job1"))
    assert service.get_task(mb, "../../etc/passwd") is None
    assert service.get_task(mb, "") is None


def test_spawn_worker_rejects_bad_thread_id(tmp_path):
    for bad in ("../evil", "..\\evil", "", "a b"):
        res = runner.spawn_worker(thread_id=bad, outputs_root=tmp_path,
                                  graph_factory="toy_graph:build_graph",
                                  mode="start", initial_state_file=tmp_path / "x.json")
        assert res["ok"] is False, bad
        assert res["code"] == "bad_thread_id", (bad, res)


def test_pending_ids_skips_stray_files(tmp_path):
    """目录被手工塞进一个不合法文件名的任务文件时,不能炸,也不能当成待办。"""
    mb = Mailbox(runner.gate_root(tmp_path, "job1"))
    (mb.tasks_dir / "not-a-task.task.json").write_text("{}", encoding="utf-8")
    (mb.tasks_dir / "not-a-task.error.json").write_text("{}", encoding="utf-8")
    assert mb.pending_ids() == []
    assert mb.reset_errors() == 0


def test_rejection_message_has_no_local_path(tmp_path):
    """拒绝返回体里不能出现本机绝对路径(F2 的对外契约)。"""
    from agent_gate.safety import UnsafePath, rejection

    res = rejection(UnsafePath("media_path_not_allowed"))
    blob = repr(res)
    assert str(tmp_path) not in blob
    assert "C:\\" not in blob and "/Users/" not in blob
    assert res["code"] == "media_path_not_allowed"

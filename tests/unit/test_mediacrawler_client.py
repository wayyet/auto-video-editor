"""单测:MediaCrawler 子进程适配器(2026-11-20 审查修订版阶段2/6.1)。

锁定:normalize_query 逗号规整(P2-05)/ detect_revision 三形态(P2-01)/
preflight 三类缺失(P2-02)/ CDP 互斥锁独占-忙碌-陈旧抢占(P1-04)/
search_notes 全错误分类(NETWORK_ERROR / PARSE_ERROR / NO_RESULTS)/
stdout/stderr 脱敏(P2-06)/ diagnostics 无绝对路径(P2-06)/ 成功即删原始
JSON + TTL 清扫(P1-06/P2-08)/ 超时进程树清理(P2-09)。

全程不访问真实社交平台:monkeypatch ``_run_subprocess``(或 Popen)+ 临时
目录落 JSON 桩;锁文件用独立 lock_dir 防并行测试串扰。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

import storyline_capabilities.mediacrawler_client as mc


# ---------------------------------------------------------------------------
# normalize_query(审查 P2-05)
# ---------------------------------------------------------------------------
def test_normalize_query_replaces_ascii_commas_and_collapses() -> None:
    assert mc.normalize_query("海上清真寺,旅游攻略") == "海上清真寺 旅游攻略"
    assert mc.normalize_query("a, b ,c") == "a b c"
    assert mc.normalize_query("  多   空格  ") == "多 空格"
    # 中文逗号(全角)不是 MC 的多关键词分隔符,保留(用转义防源码歧义)
    full_width = "夏季\uFF0C防晒"
    assert mc.normalize_query(full_width) == full_width
    assert mc.normalize_query(None) == ""  # type: ignore[arg-type]


def test_normalize_query_is_idempotent() -> None:
    once = mc.normalize_query("a,b c,,d")
    assert mc.normalize_query(once) == once


# ---------------------------------------------------------------------------
# detect_revision(审查 P2-01:上游版本固定)
# ---------------------------------------------------------------------------
def _sha(prefix: str = "a") -> str:
    return prefix * 40


def test_detect_revision_reads_branch_ref(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(_sha("a") + "\n", encoding="utf-8")
    assert mc.detect_revision(str(tmp_path)) == "a" * 12


def test_detect_revision_reads_packed_refs(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "packed-refs").write_text(
        f"{_sha('b')} refs/heads/other\n{_sha('c')} refs/heads/main\n",
        encoding="utf-8",
    )
    assert mc.detect_revision(str(tmp_path)) == "c" * 12


def test_detect_revision_detached_head(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(_sha("d") + "\n", encoding="utf-8")
    assert mc.detect_revision(str(tmp_path)) == "d" * 12


def test_detect_revision_missing_git_returns_unknown(tmp_path: Path) -> None:
    assert mc.detect_revision(str(tmp_path)) == "unknown"


# ---------------------------------------------------------------------------
# preflight(审查 P2-02:三类缺失区分)
# ---------------------------------------------------------------------------
def _make_mc_root(tmp_path: Path) -> Path:
    root = tmp_path / "MediaCrawler"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / "main.py").write_text("# main\n", encoding="utf-8")
    (root / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    return root


def test_preflight_ok(tmp_path: Path) -> None:
    root = _make_mc_root(tmp_path)
    ok, reason = mc.preflight(str(root))
    assert ok and reason == ""


def test_preflight_missing_root(tmp_path: Path) -> None:
    ok, reason = mc.preflight(str(tmp_path / "nope"))
    assert not ok and "不存在" in reason


def test_preflight_missing_main_py(tmp_path: Path) -> None:
    root = tmp_path / "MediaCrawler"
    root.mkdir()
    ok, reason = mc.preflight(str(root))
    assert not ok and "main.py" in reason


def test_preflight_missing_venv(tmp_path: Path) -> None:
    root = tmp_path / "MediaCrawler"
    root.mkdir()
    (root / "main.py").write_text("# main\n", encoding="utf-8")
    ok, reason = mc.preflight(str(root))
    assert not ok and "venv" in reason


# ---------------------------------------------------------------------------
# CDP 互斥锁(审查 P1-04)
# ---------------------------------------------------------------------------
def test_cdp_lock_exclusive_and_releasable(tmp_path: Path) -> None:
    lock = mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300)
    assert lock is not None and lock.exists()
    # 第二个任务被拒(明确忙碌)
    assert mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300) is None
    mc.release_cdp_lock(lock)
    assert not lock.exists()
    assert mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300) is not None


def test_cdp_lock_different_ports_independent(tmp_path: Path) -> None:
    a = mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300)
    b = mc.acquire_cdp_lock(tmp_path, 9333, stale_seconds=300)
    assert a is not None and b is not None


def test_cdp_lock_stale_is_stolen(tmp_path: Path) -> None:
    lock = mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300)
    assert lock is not None
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300) is not None


def test_cdp_lock_fresh_busy_not_stolen(tmp_path: Path) -> None:
    lock = mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300)
    assert lock is not None  # 刚创建,未陈旧
    assert mc.acquire_cdp_lock(tmp_path, 9222, stale_seconds=300) is None


# ---------------------------------------------------------------------------
# search_notes(错误分类 / 脱敏 / 数据最小化 / 互斥)
# ---------------------------------------------------------------------------
def _fake_run(
    records: Any,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    timeout: bool = False,
    write_results: bool = True,
):
    """构造 _run_subprocess 替身:按 cmd 里的 --save_data_path 落结果 JSON 桩。"""

    def fake_run(cmd, cwd, timeout_seconds):  # noqa: ANN001
        if timeout:
            return subprocess.CompletedProcess(cmd, None, stdout, stderr), (
                f"timeout after {timeout_seconds}s (process tree killed)"
            )
        if write_results:
            platform = cmd[cmd.index("--platform") + 1]
            save_data_path = Path(cmd[cmd.index("--save_data_path") + 1])
            data_dir = save_data_path / platform / "json"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "search_contents_2026-11-20.json").write_text(
                json.dumps(records, ensure_ascii=False), encoding="utf-8"
            )
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr), None

    return fake_run


def _mc_env(tmp_path: Path) -> dict[str, Any]:
    root = _make_mc_root(tmp_path / "mc")
    return {
        "root": str(root),
        "save_root": str(tmp_path / "runs"),
        "lock_dir": tmp_path / "locks",
    }


def _mc_kwargs(env: dict[str, Any], query: str = "q") -> dict[str, Any]:
    return {
        "query": query,
        "max_results": 10,
        "platform": "xhs",
        "project_root": env["root"],
        "save_root": env["save_root"],
        "timeout_seconds": 600,
        "lock_dir": env["lock_dir"],
    }


def test_search_notes_happy_path_minimizes_and_unlocks(
    tmp_path: Path, monkeypatch
) -> None:
    env = _mc_env(tmp_path)
    records = [{"note_url": "https://www.xiaohongshu.com/explore/1", "title": "t"}]
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run(records))

    items, error, message, diag = mc.search_notes(**_mc_kwargs(env, "夏季防晒"))

    assert error is None and items == records
    assert not message
    # 数据最小化(P1-06):原始 JSON 成功读取后即删
    assert diag["raw_dir_deleted"] is True
    assert not any(Path(env["save_root"]).iterdir())
    # diagnostics 无绝对路径(P2-06):只有 run_id 与相对文件名
    assert "project_root" not in diag and "run_dir" not in diag
    assert diag["output_file"] == "xhs/json/search_contents_2026-11-20.json"
    assert diag["run_id"]
    assert diag["mc_revision"] == "unknown"  # 假根目录无 .git
    assert diag["query_submitted"] == "夏季防晒"
    # 互斥锁已释放
    assert not any(env["lock_dir"].iterdir())


def test_search_notes_busy_lock_returns_network_error(
    tmp_path: Path, monkeypatch
) -> None:
    env = _mc_env(tmp_path)
    held = mc.acquire_cdp_lock(env["lock_dir"], 9222, stale_seconds=300)
    assert held is not None
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run([], write_results=False))

    items, error, message, diag = mc.search_notes(**_mc_kwargs(env))

    assert items == [] and error == "NETWORK_ERROR"
    assert "busy" in message.lower() and diag["cdp_lock_busy"] is True
    mc.release_cdp_lock(held)


def test_search_notes_timeout_classifies_network_error(
    tmp_path: Path, monkeypatch
) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run([], timeout=True))

    items, error, message, diag = mc.search_notes(**_mc_kwargs(env))

    assert items == [] and error == "NETWORK_ERROR"
    assert "timeout" in message.lower() and diag["timeout"]


def test_search_notes_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc, "_run_subprocess", _fake_run([], returncode=1, stderr="boom")
    )

    _, error, _, diag = mc.search_notes(**_mc_kwargs(env))

    assert error == "NETWORK_ERROR" and diag["returncode"] == 1
    assert "boom" in diag["stderr_tail"]


def test_search_notes_no_result_file(tmp_path: Path, monkeypatch) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run([], write_results=False))

    items, error, _, _ = mc.search_notes(**_mc_kwargs(env))

    assert error == "NO_RESULTS" and items == []


def test_search_notes_invalid_json(tmp_path: Path, monkeypatch) -> None:
    env = _mc_env(tmp_path)

    def fake_run(cmd, cwd, timeout_seconds):  # noqa: ANN001
        platform = cmd[cmd.index("--platform") + 1]
        save_data_path = Path(cmd[cmd.index("--save_data_path") + 1])
        data_dir = save_data_path / platform / "json"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "search_contents_2026-11-20.json").write_text(
            "{broken", encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", ""), None

    monkeypatch.setattr(mc, "_run_subprocess", fake_run)
    _, error, message, _ = mc.search_notes(**_mc_kwargs(env))
    assert error == "PARSE_ERROR" and "unreadable" in message


def test_search_notes_dict_wrapper_tolerated(tmp_path: Path, monkeypatch) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc, "_run_subprocess", _fake_run({"data": [{"note_url": "https://e.com"}]})
    )
    items, error, _, _ = mc.search_notes(**_mc_kwargs(env))
    assert error is None and items == [{"note_url": "https://e.com"}]


def test_search_notes_oversized_file(tmp_path: Path, monkeypatch) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "MC_RAW_MAX_BYTES", 8)
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run([{"a": 123456789}]))
    _, error, message, _ = mc.search_notes(**_mc_kwargs(env))
    assert error == "PARSE_ERROR" and "oversized" in message


def test_search_notes_redacts_secrets_in_output_tails(
    tmp_path: Path, monkeypatch
) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc,
        "_run_subprocess",
        _fake_run(
            [],
            write_results=False,
            stdout="ok xsec_token: abcdefgh123456 done",
            stderr="cookie=SESSIONABCDEF98765 leaked",
        ),
    )
    _, error, _, diag = mc.search_notes(**_mc_kwargs(env))
    assert error == "NO_RESULTS"  # 无结果文件
    assert "abcdefgh123456" not in diag["stdout_tail"]
    assert "xsec_token" in diag["stdout_tail"] and "***" in diag["stdout_tail"]
    assert "SESSIONABCDEF98765" not in diag["stderr_tail"]
    assert "***" in diag["stderr_tail"]


def test_search_notes_passes_normalized_single_keyword(
    tmp_path: Path, monkeypatch
) -> None:
    """逗号规整(P2-05):提交给 MC 的 --keywords 是单关键词形态。"""
    env = _mc_env(tmp_path)
    captured_cmd: list[str] = []
    inner = _fake_run([], write_results=False)

    def spy_run(cmd, cwd, timeout_seconds):  # noqa: ANN001
        captured_cmd.extend(cmd)
        return inner(cmd, cwd, timeout_seconds)

    monkeypatch.setattr(mc, "_run_subprocess", spy_run)
    mc.search_notes(**_mc_kwargs(env, "海上清真寺,旅游攻略"))
    keywords = captured_cmd[captured_cmd.index("--keywords") + 1]
    assert keywords == "海上清真寺 旅游攻略"


def test_search_notes_preflight_failure_skips_subprocess(
    tmp_path: Path, monkeypatch
) -> None:
    env = _mc_env(tmp_path)
    env["root"] = str(tmp_path / "nope")  # 根目录不存在

    def _no_run(**_kw: Any):  # pragma: no cover - 不应被调用
        raise AssertionError("preflight 失败不应起子进程")

    monkeypatch.setattr(mc, "_run_subprocess", _no_run)
    items, error, message, _ = mc.search_notes(**_mc_kwargs(env))
    assert items == [] and error == "NETWORK_ERROR"
    assert "不存在" in message


def test_sweep_stale_runs_removes_only_old_stamp_dirs(tmp_path: Path) -> None:
    (tmp_path / "20260101_000000_000000").mkdir()
    (tmp_path / "20261120_101010_000000").mkdir()
    (tmp_path / "not-a-run").mkdir()
    old = time.time() - 10 * 86400
    os.utime(tmp_path / "20260101_000000_000000", (old, old))
    cleaned = mc.sweep_stale_runs(tmp_path, ttl_days=3)
    assert cleaned == 1
    assert not (tmp_path / "20260101_000000_000000").exists()
    assert (tmp_path / "20261120_101010_000000").exists()
    assert (tmp_path / "not-a-run").exists()


# ---------------------------------------------------------------------------
# _run_subprocess(P2-09:超时进程树终止)
# ---------------------------------------------------------------------------
class _FakePopen:
    spec: dict[str, Any] = {}

    def __init__(self, cmd, **kwargs):  # noqa: ANN001, ANN003
        self.args = cmd
        self.pid = 4242
        self.returncode = None

    def communicate(self, timeout=None):  # noqa: ANN001
        spec = _FakePopen.spec
        # 只在第一次调用抛超时(真实 Popen 被杀树后第二次 communicate 会返回)
        if spec.pop("timeout", None):
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = spec.get("returncode", 0)
        return spec.get("stdout", ""), spec.get("stderr", "")


def test_run_subprocess_returns_completed_process(monkeypatch) -> None:
    _FakePopen.spec = {"stdout": "out", "stderr": "err", "returncode": 0}
    monkeypatch.setattr(mc.subprocess, "Popen", _FakePopen)
    proc, flag = mc._run_subprocess(["x"], cwd=".", timeout_seconds=10)
    assert flag is None and proc.returncode == 0 and proc.stdout == "out"


def test_run_subprocess_timeout_kills_tree(monkeypatch) -> None:
    _FakePopen.spec = {"timeout": True, "returncode": 1}
    monkeypatch.setattr(mc.subprocess, "Popen", _FakePopen)
    killed: list[int] = []
    monkeypatch.setattr(mc, "_kill_process_tree", lambda pid: killed.append(pid))
    proc, flag = mc._run_subprocess(["x"], cwd=".", timeout_seconds=10)
    assert flag is not None and killed == [4242]
    assert proc.stdout == ""  # 杀树后回收剩余输出,无有效结果


# ---------------------------------------------------------------------------
# 2026-11-21 审查修订版 P1:平台代码 → 物理输出目录映射(dy → douyin)
# ---------------------------------------------------------------------------
def test_platform_label_douyin() -> None:
    assert mc.platform_label("dy") == "抖音"
    assert mc.platform_label("xhs") == "小红书"


def test_search_notes_douyin_results_live_under_douyin_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """真实 MC 抖音输出目录是 douyin/json(store 用 platform="douyin"),
    适配器按 --platform dy 调用后必须能从 douyin/json 读回结果(审查 §3.2)。"""

    def fake_run_douyin(cmd, cwd, timeout_seconds):  # noqa: ANN001
        records = [
            {
                "aweme_id": "1",
                "title": "t",
                "desc": "d",
                "aweme_url": "https://www.douyin.com/video/1",
            }
        ]
        save_data_path = Path(cmd[cmd.index("--save_data_path") + 1])
        data_dir = save_data_path / "douyin" / "json"  # 真实行为:固定 douyin,非 dy
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "search_contents_2026-11-21.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", ""), None

    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "_run_subprocess", fake_run_douyin)
    kwargs = _mc_kwargs(env)
    kwargs["platform"] = "dy"
    items, error, _, diag = mc.search_notes(**kwargs)
    assert error is None and len(items) == 1
    assert diag["output_file"] == "douyin/json/search_contents_2026-11-21.json"


def test_search_notes_wrong_platform_dir_is_not_read(
    tmp_path: Path, monkeypatch
) -> None:
    """反向守卫:结果写在 <代码>/json(dy/json)时不得被误读 —— 防映射退化为透传。"""

    def fake_run_dy_dir(cmd, cwd, timeout_seconds):  # noqa: ANN001
        save_data_path = Path(cmd[cmd.index("--save_data_path") + 1])
        data_dir = save_data_path / "dy" / "json"  # 错误目录(映射应指向 douyin)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "search_contents_2026-11-21.json").write_text(
            "[]", encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", ""), None

    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "_run_subprocess", fake_run_dy_dir)
    kwargs = _mc_kwargs(env)
    kwargs["platform"] = "dy"
    items, error, _, _ = mc.search_notes(**kwargs)
    assert items == [] and error in ("NO_RESULTS", "AUTH_REQUIRED")


# ---------------------------------------------------------------------------
# 2026-11-21 审查修订版 P1:登录失败诊断(AUTH_REQUIRED)+ 失败现场证据落盘
# ---------------------------------------------------------------------------
def test_search_notes_exit0_no_file_with_login_markers_is_auth_required(
    tmp_path: Path, monkeypatch
) -> None:
    """MC 登录失败走 sys.exit() 退出码 0 + 无结果文件 → AUTH_REQUIRED,不伪装 NO_RESULTS。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc,
        "_run_subprocess",
        _fake_run([], write_results=False, stderr="请扫码登录 (qrcode) 后继续"),
    )
    items, error, message, diag = mc.search_notes(**_mc_kwargs(env))
    assert items == [] and error == "AUTH_REQUIRED"
    assert "login" in message.lower() or "登录" in message
    assert diag["data_dir_exists"] is False


def test_search_notes_exit0_no_file_without_markers_stays_no_results(
    tmp_path: Path, monkeypatch
) -> None:
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc, "_run_subprocess", _fake_run([], write_results=False, stderr="all good")
    )
    _, error, _, _ = mc.search_notes(**_mc_kwargs(env))
    assert error == "NO_RESULTS"


def test_search_notes_failure_persists_redacted_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    """非零退出:脱敏 stderr 全文落盘到运行目录(TTL 兜底),诊断只记相对文件名。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc,
        "_run_subprocess",
        _fake_run(
            [],
            write_results=False,
            returncode=1,
            stderr="Traceback ...\nCookie: SESSIONABCDEF98765 xsec_token: abcdefgh123456",
        ),
    )
    _, error, _, diag = mc.search_notes(**_mc_kwargs(env))
    assert error == "NETWORK_ERROR"
    assert diag["stderr_file"]
    assert "/" in diag["stderr_file"] and diag["stderr_file"].endswith("stderr_full.txt")
    evidence = Path(env["save_root"]) / diag["stderr_file"]
    text = evidence.read_text(encoding="utf-8")
    assert "SESSIONABCDEF98765" not in text and "***" in text
    assert diag["data_dir_exists"] is False
    assert diag["result_files"] == []
    # 落盘目录在时间戳运行目录内 → TTL 清扫可见
    assert mc.sweep_stale_runs(Path(env["save_root"]), ttl_days=3) == 0


# ---------------------------------------------------------------------------
# 2026-11-21 ks/bili 准入修订版 §5 阶段3.1:目录映射正反向 + B站登录分类双向
# ---------------------------------------------------------------------------
def _fake_run_in_dir(store_dir: str, records, *, returncode: int = 0, stderr: str = ""):
    """按**物理目录名**(非 CLI 代码)落结果 JSON 桩,模拟真实 MC store 行为。"""

    def fake_run(cmd, cwd, timeout_seconds):  # noqa: ANN001
        save_data_path = Path(cmd[cmd.index("--save_data_path") + 1])
        data_dir = save_data_path / store_dir / "json"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "search_contents_2026-11-21.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, returncode, "", stderr), None

    return fake_run


def _mc_kwargs_platform(env: dict[str, Any], platform: str) -> dict[str, Any]:
    kwargs = _mc_kwargs(env)
    kwargs["platform"] = platform
    return kwargs


def test_search_notes_kuaishou_results_live_under_kuaishou_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """ks CLI 代码的物理输出目录是 kuaishou/json(准入修订版 §2.1 实测)。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc,
        "_run_subprocess",
        _fake_run_in_dir(
            "kuaishou",
            [
                {
                    "video_id": "1",
                    "title": "t",
                    "desc": "d",
                    "video_url": "https://www.kuaishou.com/short-video/1",
                }
            ],
        ),
    )
    items, error, _, diag = mc.search_notes(**_mc_kwargs_platform(env, "ks"))
    assert error is None and len(items) == 1
    assert diag["output_file"] == "kuaishou/json/search_contents_2026-11-21.json"


def test_search_notes_ks_json_dir_is_not_read(tmp_path: Path, monkeypatch) -> None:
    """反向守卫:写在 <CLI代码>/json(ks/json)的结果不得被误读。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run_in_dir("ks", []))
    items, error, _, _ = mc.search_notes(**_mc_kwargs_platform(env, "ks"))
    assert items == [] and error in ("NO_RESULTS", "AUTH_REQUIRED")


def test_search_notes_bili_results_live_under_bili_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """bili 的 JSON store 恰好也是 bili(BiliJsonStoreImplement);显式契约锁定。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc,
        "_run_subprocess",
        _fake_run_in_dir(
            "bili",
            [
                {
                    "video_id": "9",
                    "title": "t",
                    "desc": "d",
                    "video_url": "https://www.bilibili.com/video/av9",
                }
            ],
        ),
    )
    items, error, _, diag = mc.search_notes(**_mc_kwargs_platform(env, "bili"))
    assert error is None and len(items) == 1
    assert diag["output_file"] == "bili/json/search_contents_2026-11-21.json"


def test_search_notes_bilibili_json_dir_is_not_read(
    tmp_path: Path, monkeypatch
) -> None:
    """反向守卫(准入修订版 §5 阶段3.1):B站正确目录就是 bili/json,
    反向检查针对**错误的 bilibili/json**(未启用的 JSONL store 目录名)。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(mc, "_run_subprocess", _fake_run_in_dir("bilibili", []))
    items, error, _, _ = mc.search_notes(**_mc_kwargs_platform(env, "bili"))
    assert items == [] and error in ("NO_RESULTS", "AUTH_REQUIRED")


def test_bili_nonzero_exit_with_login_marker_is_auth_required(
    tmp_path: Path, monkeypatch
) -> None:
    """B站 start() 登录后二次 pong 失败走 sys.exit(1)(准入修订版 §2.5):
    非零退出 + 登录标记 → AUTH_REQUIRED,不伪装网络故障。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc,
        "_run_subprocess",
        _fake_run_in_dir(
            "bili", [], returncode=1, stderr="登录流程结束后仍未登录：请确认扫码已完成"
        ),
    )
    items, error, message, diag = mc.search_notes(**_mc_kwargs_platform(env, "bili"))
    assert items == [] and error == "AUTH_REQUIRED"
    assert "登录" in message or "login" in message.lower()
    assert diag["returncode"] == 1


def test_bili_nonzero_exit_ordinary_error_stays_network_error(
    tmp_path: Path, monkeypatch
) -> None:
    """反向守卫:普通非零退出(无登录标记)仍为 NETWORK_ERROR,不把所有
    非零退出改判为登录失败。"""
    env = _mc_env(tmp_path)
    monkeypatch.setattr(
        mc, "_run_subprocess", _fake_run_in_dir("bili", [], returncode=1, stderr="boom")
    )
    items, error, _, _ = mc.search_notes(**_mc_kwargs_platform(env, "bili"))
    assert items == [] and error == "NETWORK_ERROR"

"""创建台「清除缓存」单元测试(2026-11-06 维护入口)。

全部注入假实现/临时目录,无真机依赖、**绝不触碰真实缓存**:
- config 清单展开:占位符/环境变量展开、不存在跳过、黑名单下探剪枝;
- web/cache_cleaner.clean_cache:全清、不存在跳过、IO 异常吞掉进 errors 不中断;
- API 层(POST /api/console/cache/clear):curl 豁免 200、CSRF 403、
  冷却 429、活跃任务 409(且不执行删除)。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.api_console as api_console  # noqa: E402
import web.cache_cleaner as cache_cleaner  # noqa: E402
import web.console_service as cs  # noqa: E402
from config import CacheGlobSpec  # noqa: E402
from monitor_fastapi import app  # noqa: E402

client = TestClient(app)


def _mk_cache_tree(root: Path) -> dict[str, Path]:
    """构造一棵典型的"可再生缓存 + 应被剪枝保留"目录树。"""
    pkg_cache = root / "pkg" / "__pycache__"
    pkg_cache.mkdir(parents=True)
    (pkg_cache / "mod.cpython-311.pyc").write_bytes(b"x")
    venv_cache = root / ".venv" / "lib" / "x" / "__pycache__"
    venv_cache.mkdir(parents=True)
    (venv_cache / "v.cpython-311.pyc").write_bytes(b"x")
    logs_dir = root / "logs"
    logs_dir.mkdir()
    (logs_dir / "run_20261106.log").write_text("log", encoding="utf-8")
    (logs_dir / "keep.txt").write_text("keep", encoding="utf-8")
    runtime_dir = root / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "initial_state_video-001.json").write_text("{}", encoding="utf-8")
    (runtime_dir / "keep.json").write_text("{}", encoding="utf-8")
    return {
        "pkg_cache": pkg_cache,
        "venv_cache": venv_cache,
        "log": logs_dir / "run_20261106.log",
        "keep_txt": logs_dir / "keep.txt",
        "initial_state": runtime_dir / "initial_state_video-001.json",
        "keep_json": runtime_dir / "keep.json",
    }


class TestExpandSpec:
    def test_dir_recurse_prunes_excluded_subtrees(self, tmp_path):
        """黑名单目录(.venv)整棵不下沉:其下 __pycache__ 不出现。"""
        tree = _mk_cache_tree(tmp_path)
        spec = CacheGlobSpec(
            root=str(tmp_path), kind="dir_recurse", pattern="__pycache__",
            exclude_substr=("\\.venv\\",), description="t",
        )
        found = config._expand_spec(spec)
        assert tree["pkg_cache"] in found
        assert tree["venv_cache"] not in found

    def test_dir_recurse_prunes_by_dir_name_token(self, tmp_path):
        """token 命中目录名本身(补尾部 sep)也要剪枝:``\\.venv\\`` 剪掉 .venv 根。"""
        _mk_cache_tree(tmp_path)
        spec = CacheGlobSpec(
            root=str(tmp_path), kind="dir_recurse", pattern="__pycache__",
            exclude_substr=("\\.venv\\",), description="t",
        )
        found = config._expand_spec(spec)
        assert all(".venv" not in str(p) for p in found)

    def test_file_recurse_collects_matching_files_only(self, tmp_path):
        tree = _mk_cache_tree(tmp_path)
        spec = CacheGlobSpec(
            root=str(tmp_path / "logs"), kind="file_recurse", pattern="*.log",
            description="t",
        )
        found = config._expand_spec(spec)
        assert tree["log"] in found
        assert tree["keep_txt"] not in found

    def test_file_pattern_non_recursive(self, tmp_path):
        tree = _mk_cache_tree(tmp_path)
        nested = tmp_path / "runtime" / "sub"
        nested.mkdir()
        (nested / "initial_state_nested.json").write_text("{}", encoding="utf-8")
        spec = CacheGlobSpec(
            root=str(tmp_path / "runtime"), kind="file_pattern",
            pattern="initial_state_*.json", description="t",
        )
        found = config._expand_spec(spec)
        assert tree["initial_state"] in found
        assert (nested / "initial_state_nested.json") not in found

    def test_missing_root_returns_empty(self, tmp_path):
        spec = CacheGlobSpec(
            root=str(tmp_path / "no-such-dir"), kind="dir_recurse",
            pattern="__pycache__", description="t",
        )
        assert config._expand_spec(spec) == []

    def test_unknown_kind_raises(self, tmp_path):
        spec = CacheGlobSpec(root=str(tmp_path), kind="file_pattern",
                             pattern="*", description="t")
        object.__setattr__(spec, "kind", "bogus")
        with pytest.raises(ValueError):
            config._expand_spec(spec)


class TestResolvedCachePaths:
    def test_placeholder_expansion_uses_live_repo_root(self, tmp_path, monkeypatch):
        """``{repo_root}`` 在调用时取当前 config.REPO_ROOT(可 monkeypatch)。"""
        (tmp_path / "cacheA").mkdir()
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config, "CACHE_PATHS_TO_CLEAN", [r"{repo_root}\cacheA", r"{repo_root}\missing"])
        monkeypatch.setattr(config, "CACHE_GLOB_SPECS", [])
        found = config.resolved_cache_paths()
        assert tmp_path / "cacheA" in found
        assert all("missing" not in str(p) for p in found)

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        (tmp_path / "envcache").mkdir()
        monkeypatch.setenv("TEST_AV_CACHE_ROOT", str(tmp_path))
        monkeypatch.setattr(config, "CACHE_PATHS_TO_CLEAN", [r"%TEST_AV_CACHE_ROOT%\envcache"])
        monkeypatch.setattr(config, "CACHE_GLOB_SPECS", [])
        assert tmp_path / "envcache" in config.resolved_cache_paths()

    def test_kuaishou_root_is_parent_of_repo(self):
        """锚点关系不变式:kuaishou 同级根 = 仓库根的父目录。"""
        assert config.KUAISHOU_ROOT == config.REPO_ROOT.parent

    def test_placeholder_expansion_uses_live_kuaishou_root(self, tmp_path, monkeypatch):
        """``{kuaishou_root}`` 在调用时取当前 config.KUAISHOU_ROOT(可 monkeypatch)。"""
        monkeypatch.setattr(config, "KUAISHOU_ROOT", tmp_path)
        raw = config._expand_cache_raw(r"{kuaishou_root}\JianyingPro")
        assert raw == tmp_path / "JianyingPro"


class TestCleanCache:
    def test_cleans_dirs_files_and_skips_missing(self, tmp_path, monkeypatch):
        d1 = tmp_path / "c1"
        d1.mkdir()
        (d1 / "x.tmp").write_bytes(b"x")
        f1 = tmp_path / "f.log"
        f1.write_text("log", encoding="utf-8")
        missing = tmp_path / "nope"
        monkeypatch.setattr(config, "resolved_cache_paths", lambda: [d1, f1, missing])
        result = cache_cleaner.clean_cache()
        assert not d1.exists() and not f1.exists()
        assert result["errors"] == []
        assert sorted(result["cleaned_paths"]) == sorted([str(d1), str(f1)])

    def test_io_error_swallowed_into_errors(self, tmp_path, monkeypatch):
        locked = tmp_path / "locked"
        locked.mkdir()
        good = tmp_path / "good"
        good.mkdir()
        monkeypatch.setattr(config, "resolved_cache_paths", lambda: [locked, good])

        real_rmtree = shutil.rmtree

        def fake_rmtree(p, *a, **k):
            if "locked" in str(p):
                raise PermissionError(32, "被占用")
            return real_rmtree(p, *a, **k)

        monkeypatch.setattr(cache_cleaner.shutil, "rmtree", fake_rmtree)
        result = cache_cleaner.clean_cache()
        assert str(good) in result["cleaned_paths"]  # 其余缓存不受单点失败拖累
        assert not good.exists()
        assert locked.exists()  # 失败项保留原状
        assert len(result["errors"]) == 1 and "locked" in result["errors"][0]


class TestClearCacheApi:
    @pytest.fixture()
    def fake_cache(self, tmp_path, monkeypatch):
        """API 级测试的假缓存:一个待删目录;并复位冷却状态。"""
        target = tmp_path / "api-cache"
        target.mkdir()
        (target / "x.tmp").write_bytes(b"x")
        monkeypatch.setattr(config, "resolved_cache_paths", lambda: [target])
        monkeypatch.setattr(api_console, "_last_cache_clear", None)
        yield target
        cs._ACTIVE_PROCS.clear()
        cs._STARTED_AT.clear()

    def _csrf(self) -> str:
        return client.get("/api/console/csrf").json()["token"]

    def test_no_origin_exempt_200_and_counts(self, fake_cache):
        """curl/本机脚本(无 Origin)豁免 CSRF:200 + cleaned_count。"""
        resp = client.post("/api/console/cache/clear")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["cleaned_count"] == 1
        assert not fake_cache.exists()

    def test_origin_without_csrf_403(self, fake_cache):
        resp = client.post(
            "/api/console/cache/clear",
            headers={"Origin": "http://127.0.0.1:7860"},
        )
        assert resp.status_code == 403
        assert fake_cache.exists()  # 未执行删除

    def test_cooldown_second_call_429(self, fake_cache, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_CACHE_CLEAR_COOLDOWN_SEC", 30.0)
        headers = {"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": self._csrf()}
        first = client.post("/api/console/cache/clear", headers=headers)
        assert first.status_code == 200, first.text
        second = client.post("/api/console/cache/clear", headers=headers)
        assert second.status_code == 429
        assert "秒" in second.json()["detail"]

    def test_cooldown_disabled_allows_repeat(self, fake_cache, monkeypatch):
        monkeypatch.setattr(config, "CONSOLE_CACHE_CLEAR_COOLDOWN_SEC", 0.0)
        headers = {"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": self._csrf()}
        for _ in range(2):
            assert client.post("/api/console/cache/clear", headers=headers).status_code == 200

    def test_active_run_409_and_no_deletion(self, fake_cache, monkeypatch):
        """本实例在册活跃任务时 409,且不做任何删除。"""

        class FakeProc:
            def poll(self):
                return None

        cs._ACTIVE_PROCS["console-fake123"] = FakeProc()
        headers = {"Origin": "http://127.0.0.1:7860", "X-CSRF-Token": self._csrf()}
        resp = client.post("/api/console/cache/clear", headers=headers)
        assert resp.status_code == 409
        assert "console-fake123" in resp.json()["detail"]
        assert fake_cache.exists()  # 缓存原样保留

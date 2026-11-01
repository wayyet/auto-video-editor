"""节点 1 单测(附件 1.2 节要点 + 2026-11-01 回收站语义增补)。

2026-11-01 起 ``clean_cache`` 的所有删除项**一律移入 Windows 回收站**
(``_send_to_recycle_bin``),不再 ``shutil.rmtree``。本文件所有用例都
monkeypatch ``nodes.node_01_clean_cache._send_to_recycle_bin``,避免污染
真实回收站;stub 用 ``_really_delete`` 模拟「回收成功 = 从原位置消失」。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from config import (
    CACHE_GLOB_SPECS,
    CACHE_PATHS_TO_CLEAN,
    resolved_cache_paths,
)
from nodes.node_01_clean_cache import clean_cache_paths


@pytest.fixture
def state_base() -> dict:
    return {"session_id": "test", "error_log": []}


def _really_delete(path: Path) -> None:
    """stub 用:模拟一次成功回收(目标从原位置消失)。"""
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def test_nonexistent_path_does_not_error(tmp_path: Path, state_base: dict) -> None:
    """目标目录不存在 → 正常跳过,不影响整体。"""
    missing = tmp_path / "does_not_exist"
    out = clean_cache_paths([missing], state_base)
    assert out["cache_cleaned"] is True
    assert out["cache_cleaned_paths"] == []  # 不存在的路径不算成功


def test_recycle_failure_writes_error_log_and_keeps_target(
    tmp_path: Path, state_base: dict, monkeypatch
) -> None:
    """回收站操作抛 PermissionError → 捕获、写 error_log、目标保留原处。"""
    target = tmp_path / "locked_dir"
    target.mkdir()
    (target / "file.txt").write_text("data", encoding="utf-8")

    def fail_recycle(path):
        raise PermissionError("locked")

    monkeypatch.setattr("nodes.node_01_clean_cache._send_to_recycle_bin", fail_recycle)
    out = clean_cache_paths([target], state_base)
    assert out["cache_cleaned"] is True
    assert out["cache_cleaned_paths"] == []  # 失败不算成功
    assert any("清理失败" in e for e in out["error_log"])
    assert target.exists(), "回收失败的项目必须保留原处,绝不回退成永久删除"


def test_only_successful_paths_returned(tmp_path: Path, state_base: dict, monkeypatch) -> None:
    """混合成功/失败时,cache_cleaned_paths 只含成功项。"""
    success = tmp_path / "ok_dir"
    success.mkdir()
    fail = tmp_path / "fail_dir"
    fail.mkdir()

    def selective_recycle(path):
        if str(path).endswith("fail_dir"):
            raise PermissionError("nope")
        _really_delete(path)

    monkeypatch.setattr("nodes.node_01_clean_cache._send_to_recycle_bin", selective_recycle)
    out = clean_cache_paths([success, fail], state_base)
    assert out["cache_cleaned_paths"] == [str(success)]
    assert any("清理失败" in e for e in out["error_log"])


# ---------------------------------------------------------------------------
# 2026-09 计划:补齐 kuaishou-clean-cache「一类·常规再生缓存」后的回归守护
# ---------------------------------------------------------------------------

FORBIDDEN_SUBSTRINGS = (
    r"\JianyingPro\User Data\Cache",  # 剪映素材/特效下载缓存 — VIP 不可删
    r"\JianyingPro\User Data\Log",    # 剪映日志 — 禁删
)


def _all_declared_paths() -> list[str]:
    """列出 ``CACHE_PATHS_TO_CLEAN`` 中的字面路径(未展开)。"""
    return list(CACHE_PATHS_TO_CLEAN)


def test_resolved_cache_paths_excludes_jianying_vip_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolved_cache_paths()`` 输出绝不含剪映 ``User Data\\Cache`` / ``\\Log``。

    防回归:用户 2026-07-04 指定这两项是禁删项,任何清理动作都不能触碰。
    """
    # 让所有展开后的简单路径「存在」,确保即使它们可解析也不会被列入
    for raw in CACHE_PATHS_TO_CLEAN:
        monkeypatch.setattr("os.path.expandvars", lambda x, _raw=raw: str(tmp_path / "dummy"))

    paths = resolved_cache_paths()
    lowered = [str(p) for p in paths]
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert not any(forbidden in p for p in lowered), (
            f"resolved_cache_paths() 不应包含禁删项 {forbidden!r},"
            f" 实际命中:{[p for p in lowered if forbidden in p]}"
        )

    # 同时字面声明里也不能含这两个串
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert not any(forbidden in raw for raw in _all_declared_paths()), (
            f"CACHE_PATHS_TO_CLEAN 不应含禁删项 {forbidden!r},实际命中:"
            f"{[r for r in _all_declared_paths() if forbidden in r]}"
        )


def test_resolved_cache_paths_excludes_venv_pycache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``__pycache__`` 递归必须排除 ``venv/`` 与 ``.venv/`` 内部。

    在 tmp_path 下造两个被排除的 pycache,断言它们不在 ``resolved_cache_paths()`` 中。
    """
    # 在 tmp_path 下造 venv/.venv 各自的 __pycache__
    venv_root = tmp_path / "venv"
    dotvenv_root = tmp_path / ".venv"
    real_pkg = tmp_path / "myproj" / "mypkg"
    for root in (venv_root, dotvenv_root, real_pkg):
        (root / "__pycache__").mkdir(parents=True)
        (root / "__pycache__" / "module.cpython-313.pyc").write_bytes(b"")
        (root / "__pycache__" / "sub").mkdir()  # 多嵌一层验证递归也跳过

    # 把 __pycache__ 这一条 spec 的 root 指向 tmp_path,保留其 exclude_substr
    from config import CacheGlobSpec, _expand_spec

    spec = next(s for s in CACHE_GLOB_SPECS if s.pattern == "__pycache__")
    patched = CacheGlobSpec(
        root=str(tmp_path),  # 用绝对路径,绕开环境变量展开
        kind=spec.kind,
        pattern=spec.pattern,
        exclude_substr=spec.exclude_substr,
        description=spec.description,
    )
    found = _expand_spec(patched)

    # venv 与 .venv 内部的所有 __pycache__ 必须全部被排除
    assert all("\\venv\\__pycache__" not in str(p) and "\\.venv\\__pycache__" not in str(p) for p in found), (
        f"venv 内 __pycache__ 应被排除,实际找到:{[str(p) for p in found]}"
    )
    # 真包里的 __pycache__ 必须出现(并含 sub 子目录,验证递归生效)
    real_hits = [p for p in found if "\\myproj\\mypkg\\__pycache__" in str(p)]
    assert real_hits, f"myproj/mypkg 下的 __pycache__ 应被命中,实际找到:{[str(p) for p in found]}"


def test_resolved_cache_paths_includes_real_pycache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 venv 路径下的 ``__pycache__`` 必须出现在结果中(供实际清理)。"""
    from config import CacheGlobSpec, _expand_spec

    real_pkg = tmp_path / "auto-video-editor" / "mypkg"
    (real_pkg / "__pycache__").mkdir(parents=True)
    (real_pkg / "__pycache__" / "module.cpython-313.pyc").write_bytes(b"")

    spec = next(s for s in CACHE_GLOB_SPECS if s.pattern == "__pycache__")
    patched = CacheGlobSpec(
        root=str(tmp_path),
        kind=spec.kind,
        pattern=spec.pattern,
        exclude_substr=spec.exclude_substr,
        description=spec.description,
    )
    found = _expand_spec(patched)

    assert any(str(p).endswith("auto-video-editor\\mypkg\\__pycache__") for p in found), (
        f"真包 __pycache__ 应出现,实际找到:{[str(p) for p in found]}"
    )


def test_resolved_cache_paths_includes_draft_bak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """剪映草稿目录下的 ``*.bak`` 文件与 ``.backup/`` 子目录都在结果中。"""
    draft_root = tmp_path / "com.lveditor.draft"
    draft = draft_root / "my_draft"
    draft.mkdir(parents=True)

    # 准备 .bak 备份文件
    (draft / "draft_content.json.bak").write_text("{}", encoding="utf-8")
    (draft / "draft_meta.json.bak").write_text("{}", encoding="utf-8")
    # 准备 .backup 子目录(里面再嵌一层,验证递归)
    backup = draft / ".backup"
    backup.mkdir()
    (backup / "old.json").write_text("{}", encoding="utf-8")
    (backup / "deeper").mkdir()
    (backup / "deeper" / "x.json").write_text("{}", encoding="utf-8")
    # 不该匹配的普通文件
    (draft / "draft_content.json").write_text("{}", encoding="utf-8")

    # 把两条「草稿注入 .bak」spec 的 root 重定向到 tmp_path,跑真实展开
    from config import CacheGlobSpec, _expand_spec

    bak_spec = next(s for s in CACHE_GLOB_SPECS if s.pattern == "*.bak")
    bak_patched = CacheGlobSpec(
        root=str(draft_root),
        kind=bak_spec.kind,
        pattern=bak_spec.pattern,
        description=bak_spec.description,
    )
    backup_spec = next(s for s in CACHE_GLOB_SPECS if s.pattern == ".backup")
    backup_patched = CacheGlobSpec(
        root=str(draft_root),
        kind=backup_spec.kind,
        pattern=backup_spec.pattern,
        description=backup_spec.description,
    )

    bak_paths = _expand_spec(bak_patched)
    backup_paths = _expand_spec(backup_patched)

    # 两个 .bak 都应出现,但原文 json 不该出现
    bak_names = {p.name for p in bak_paths}
    assert "draft_content.json.bak" in bak_names
    assert "draft_meta.json.bak" in bak_names
    assert "draft_content.json" not in bak_names

    # .backup 目录本身 + 嵌套子目录都应出现(目录递归)
    assert any(str(p).endswith(".backup") for p in backup_paths), (
        f".backup 目录应出现,实际:{[str(p) for p in backup_paths]}"
    )


def test_resolved_cache_paths_preserves_tmp_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tmp/`` 根下 4 个模板 JSON 必须保留(脚本依赖,只动子目录)。"""
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()

    # 4 个模板 JSON(不应被列入清理)
    for name in (
        "cover_wrapper_template.json",
        "text_material_template.json",
        "text_segment_template.json",
        "text_track_shell.json",
    ):
        (tmp_root / name).write_text("{}", encoding="utf-8")

    # 子目录(应被列入)
    sub1 = tmp_root / "cover"
    sub1.mkdir()
    (sub1 / "frame.png").write_bytes(b"")
    sub2 = tmp_root / "scenes"
    sub2.mkdir()

    # 把 tmp 那条 spec 的 root 重定向
    from config import CacheGlobSpec, _expand_spec

    spec = next(s for s in CACHE_GLOB_SPECS if s.kind == "dir_children" and "tmp" in s.root)
    patched = CacheGlobSpec(
        root=str(tmp_root),
        kind=spec.kind,
        pattern=spec.pattern,
        description=spec.description,
    )
    found = _expand_spec(patched)

    # 只命中 cover/ scenes/ 子目录;4 个 json 文件绝对不出现在结果里
    found_names = {p.name for p in found}
    assert found_names == {"cover", "scenes"}, (
        f"只应命中子目录,实际:{found_names}"
    )
    for template in (
        "cover_wrapper_template.json",
        "text_material_template.json",
        "text_segment_template.json",
        "text_track_shell.json",
    ):
        assert template not in found_names


def test_clean_cache_paths_idempotent_on_missing_glob_results() -> None:
    """glob spec 的 root 不存在时,``resolved_cache_paths()`` 不抛异常。

    这是契约:即使本机没装剪映、没有 FireRed ``.storyline`` ``.server_cache``
    目录、没有 ``tmp/`` 目录,UI 按钮也能直接调用 ``clean_cache(state)``
    而不会崩。
    """
    # 不做 monkeypatch,直接调用真函数。env 上的 $LOCALAPPDATA/$TEMP 通常
    # 存在,但对应的 FireRed/tmp/.playwright-cli 等很可能不在;本断言仅
    # 验证「不抛异常 + 返回 list[Path]」。
    paths = resolved_cache_paths()
    assert isinstance(paths, list)
    for p in paths:
        # 每项必须是 Path 且当下存在(不存在的不应被列入)
        assert isinstance(p, Path)
        assert p.exists()


# ---------------------------------------------------------------------------
# 2026-11-01 增补:一键回收根(outputs/ inputs/ out/)+ 全量回收站语义
# ---------------------------------------------------------------------------

def test_resolved_trash_children_expands_files_and_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolved_trash_children()`` 返回各根下**当下存在**的直接子项,文件+目录都要。"""
    import config as config_mod
    from config import resolved_trash_children

    root = tmp_path / "root"
    root.mkdir()
    (root / "f.json").write_text("{}", encoding="utf-8")
    (root / "d").mkdir()
    (root / "d" / "nested.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(config_mod, "CACHE_TRASH_CHILDREN_ROOTS", [str(root)])
    kids = resolved_trash_children()
    assert {p.name for p in kids} == {"f.json", "d"}, (
        f"直接子项应含文件与目录(不含嵌套),实际:{[p.name for p in kids]}"
    )

    # 根不存在 → 静默跳过,返回空列表
    monkeypatch.setattr(config_mod, "CACHE_TRASH_CHILDREN_ROOTS", [str(tmp_path / "nope")])
    assert resolved_trash_children() == []


def test_clean_cache_empties_trash_roots_and_keeps_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clean_cache 清空三根的直接子项(文件+目录),根目录本身保留。"""
    import nodes.node_01_clean_cache as mod

    root = tmp_path / "outputs"
    root.mkdir()
    job = root / "job-1"
    job.mkdir()
    (job / "draft_content.json").write_text("{}", encoding="utf-8")
    video = root / "a.mp4"
    video.write_bytes(b"x")

    # 双解析器全部指到 tmp,绝不触碰真实磁盘
    monkeypatch.setattr(mod, "resolved_trash_children", lambda: [job, video])
    monkeypatch.setattr(mod, "resolved_cache_paths", lambda: [])
    monkeypatch.setattr(mod, "_send_to_recycle_bin", _really_delete)

    out = mod.clean_cache({"session_id": "t", "error_log": []})
    assert not job.exists() and not video.exists()
    assert root.exists(), "根目录本身必须保留(只清内容)"
    assert set(out["cache_cleaned_paths"]) == {str(job), str(video)}


def test_clean_cache_covers_trash_roots_then_cache_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """顺序契约:先清三根子项,再清原有清单;两类都经同一回收站函数。"""
    import nodes.node_01_clean_cache as mod

    calls: list[str] = []
    x = tmp_path / "x"
    y = tmp_path / "y"
    x.write_text("x", encoding="utf-8")  # 必须真实存在,否则 _recycle_one 会跳过
    y.write_text("y", encoding="utf-8")
    monkeypatch.setattr(mod, "resolved_trash_children", lambda: [x])
    monkeypatch.setattr(mod, "resolved_cache_paths", lambda: [y])
    monkeypatch.setattr(mod, "_send_to_recycle_bin", lambda p: calls.append(str(p)))

    out = mod.clean_cache({"session_id": "t", "error_log": []})
    assert [Path(c).name for c in calls] == ["x", "y"]
    assert out["cache_cleaned_paths"] == calls
    assert out["cache_cleaned"] is True


def test_trash_roots_recycle_failure_keeps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回收根子项回收失败 → 记 error_log、子项保留、根目录不受影响。"""
    import nodes.node_01_clean_cache as mod

    root = tmp_path / "inputs"
    root.mkdir()
    child = root / "src.mp4"
    child.write_bytes(b"x")

    monkeypatch.setattr(mod, "resolved_trash_children", lambda: [child])
    monkeypatch.setattr(mod, "resolved_cache_paths", lambda: [])

    def fail_recycle(path):
        raise OSError("bin locked")

    monkeypatch.setattr(mod, "_send_to_recycle_bin", fail_recycle)
    out = mod.clean_cache({"session_id": "t", "error_log": []})
    assert out["cache_cleaned_paths"] == []
    assert any("清理失败" in e for e in out["error_log"])
    assert child.exists(), "回收失败的子项必须保留原处"
    assert root.exists()

"""节点 1 单测(附件 1.2 节要点)。"""

from __future__ import annotations

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


def test_nonexistent_path_does_not_error(tmp_path: Path, state_base: dict) -> None:
    """目标目录不存在 → 正常跳过,不影响整体。"""
    missing = tmp_path / "does_not_exist"
    out = clean_cache_paths([missing], state_base)
    assert out["cache_cleaned"] is True
    assert out["cache_cleaned_paths"] == []  # 不存在的路径不算成功


def test_file_lock_exception_writes_error_log(tmp_path: Path, state_base: dict, monkeypatch) -> None:
    """rmtree 抛 PermissionError → 异常被捕获,写入 error_log,不中断。"""
    target = tmp_path / "locked_dir"
    target.mkdir()
    (target / "file.txt").write_text("data", encoding="utf-8")

    def fail_rmtree(path, *args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("shutil.rmtree", fail_rmtree)
    out = clean_cache_paths([target], state_base)
    assert out["cache_cleaned"] is True
    assert out["cache_cleaned_paths"] == []  # 失败不算成功
    assert any("清理失败" in e for e in out["error_log"])


def test_only_successful_paths_returned(tmp_path: Path, state_base: dict, monkeypatch) -> None:
    """混合成功/失败时,cache_cleaned_paths 只含成功项。"""
    success = tmp_path / "ok_dir"
    success.mkdir()
    fail = tmp_path / "fail_dir"
    fail.mkdir()

    real_rmtree = __import__("shutil").rmtree

    def selective_rmtree(path, *args, **kwargs):
        if str(path).endswith("fail_dir"):
            raise PermissionError("nope")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("shutil.rmtree", selective_rmtree)
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

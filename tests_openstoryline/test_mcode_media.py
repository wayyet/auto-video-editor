"""阶段 D:素材准备的行为测试。

覆盖:
- 复制是**原子**的:先写 ``.part``,校验大小 + SHA-256 后才改名落地
- 复制失败 -> 抛错 + **不留残留 ``.part``**
- 源文件必须在**本会话**的 media 目录内(客户端伪造的 ``stored_path`` 被拒)
- 幂等:同一个 ``media_id`` 重复复制 -> 复用同一份,不产生第二份
- 同名不同内容 -> 换唯一名,**不覆盖**既有文件
- 源文件**不被移动 / 删除**
- 清单里的路径是**服务端生成的绝对路径**,且过了白名单
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from open_storyline.mcode_media import (
    MCodeMediaError,
    SESSION_ID_RE,
    allowed_media_roots,
    build_media_manifest,
    prepare_media_copies,
    resolve_auto_editor_dir,
    resolve_media_root,
)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture
def session_media_dir(tmp_path) -> Path:
    """本会话的上传目录(网页端产物形状)。"""
    d = tmp_path / "outputs" / "sess-1" / "media"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def media_root(tmp_path) -> Path:
    """素材白名单根(临时,不碰仓库真实的 inputs/)。"""
    root = tmp_path / "inputs"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def allowed_roots(media_root) -> List[Path]:
    return [media_root.resolve()]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _make_source(session_media_dir: Path, name: str, content: bytes = b"fake-video-bytes") -> Path:
    src = session_media_dir / name
    src.write_bytes(content)
    return src


def _files(media_id: str, src: Path, filename: str = "") -> List[Dict[str, Any]]:
    return [{
        "media_id": media_id,
        "filename": filename or src.name,
        "stored_path": str(src),
    }]


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------

def test_resolve_auto_editor_dir_uses_env(monkeypatch, tmp_path):
    """``AUTO_VIDEO_EDITOR_DIR`` 优先。"""
    fake = tmp_path / "fake-repo"
    fake.mkdir()
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DIR", str(fake))
    assert resolve_auto_editor_dir() == fake.resolve()


def test_resolve_media_root_prefers_config(tmp_path):
    """``cfg.mcode.media_root`` 非空就用它。"""

    class _Cfg:
        media_root = str(tmp_path / "custom-root")

    assert resolve_media_root(_Cfg()) == (tmp_path / "custom-root").resolve()


def test_resolve_media_root_falls_back_to_inputs(monkeypatch, tmp_path):
    """没配 ``media_root`` -> ``<auto-video-editor>/inputs``。"""
    fake = tmp_path / "fake-repo"
    fake.mkdir()
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DIR", str(fake))

    class _Cfg:
        media_root = ""

    assert resolve_media_root(_Cfg()) == (fake / "inputs").resolve()


def test_allowed_media_roots_reads_env(monkeypatch, tmp_path):
    """``AGENT_GATE_MEDIA_ROOTS`` 非空就按 ``os.pathsep`` 切分。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("AGENT_GATE_MEDIA_ROOTS", f"{a}{os.pathsep}{b}")
    assert allowed_media_roots() == [a.resolve(), b.resolve()]


def test_allowed_media_roots_default(monkeypatch, tmp_path):
    """没设环境变量 -> ``<repo>/inputs`` + ``<repo>/tests/fixtures``。"""
    fake = tmp_path / "fake-repo"
    fake.mkdir()
    monkeypatch.delenv("AGENT_GATE_MEDIA_ROOTS", raising=False)
    monkeypatch.setenv("AUTO_VIDEO_EDITOR_DIR", str(fake))
    roots = allowed_media_roots()
    assert roots == [
        (fake / "inputs").resolve(),
        (fake / "tests" / "fixtures").resolve(),
    ]


# --------------------------------------------------------------------------
# 复制:原子性 / 完整性
# --------------------------------------------------------------------------

def test_media_copy_is_atomic_and_verified(session_media_dir, media_root, allowed_roots):
    """复制经大小 + SHA-256 校验后才进入最终路径。"""
    src = _make_source(session_media_dir, "clip.mov", b"0123456789" * 100)

    out = prepare_media_copies(
        "sess-1",
        _files("m1", src),
        media_root=media_root,
        allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )

    assert len(out) == 1
    item = out[0]
    target = Path(item["path"])
    assert target.exists()
    assert target.parent == (media_root / "sess-1").resolve()
    assert target.name.startswith("m1_")
    assert target.name.endswith(".mov")
    assert item["size"] == src.stat().st_size
    assert item["sha256"] == _sha256(src)
    assert _sha256(target) == _sha256(src)

    # 没有残留 .part
    assert list(target.parent.glob("*.part")) == []


def test_media_copy_does_not_move_or_delete_source(session_media_dir, media_root, allowed_roots):
    """源文件**不被移动 / 删除 / 覆盖**。"""
    src = _make_source(session_media_dir, "clip.mov", b"original-bytes")
    before = src.read_bytes()

    prepare_media_copies(
        "sess-1",
        _files("m1", src),
        media_root=media_root,
        allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )

    assert src.exists(), "源文件被删了"
    assert src.read_bytes() == before, "源文件被改了"


def test_media_copy_failure_leaves_no_part_file(session_media_dir, media_root, allowed_roots):
    """复制中途失败 -> 抛错 + 无残留 ``.part``。"""
    src = _make_source(session_media_dir, "clip.mov")

    import shutil as _shutil
    real_copyfile = _shutil.copyfile

    def _boom(src_path, dst_path, *a, **kw):
        # 先写一半再炸,模拟「磁盘写一半失败」
        Path(dst_path).write_bytes(b"partial")
        raise OSError("disk full")

    _shutil.copyfile = _boom
    try:
        with pytest.raises(MCodeMediaError):
            prepare_media_copies(
                "sess-1",
                _files("m1", src),
                media_root=media_root,
                allowed_roots=allowed_roots,
                session_media_dir=session_media_dir,
            )
    finally:
        _shutil.copyfile = real_copyfile

    target_dir = media_root / "sess-1"
    assert list(target_dir.glob("*.part")) == [], "残留了 .part 临时文件"
    assert list(target_dir.glob("*")) == [], "失败后不该留下正式文件"


def test_media_copy_detects_size_mismatch(session_media_dir, media_root, allowed_roots, monkeypatch):
    """副本大小对不上 -> 报错 + 清理。"""
    src = _make_source(session_media_dir, "clip.mov", b"0123456789")

    import shutil as _shutil
    real_copyfile = _shutil.copyfile

    def _short_copy(src_path, dst_path, *a, **kw):
        real_copyfile(src_path, dst_path, *a, **kw)
        Path(dst_path).write_bytes(b"short")  # 截断

    _shutil.copyfile = _short_copy
    try:
        with pytest.raises(MCodeMediaError):
            prepare_media_copies(
                "sess-1",
                _files("m1", src),
                media_root=media_root,
                allowed_roots=allowed_roots,
                session_media_dir=session_media_dir,
            )
    finally:
        _shutil.copyfile = real_copyfile

    assert list((media_root / "sess-1").glob("*.part")) == []


# --------------------------------------------------------------------------
# 信任边界
# --------------------------------------------------------------------------

def test_media_source_must_be_in_session_media_dir(tmp_path, media_root, allowed_roots):
    """客户端伪造的 ``stored_path``(会话目录之外)被拒绝。"""
    session_media_dir = tmp_path / "outputs" / "sess-1" / "media"
    session_media_dir.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")

    with pytest.raises(MCodeMediaError) as exc:
        prepare_media_copies(
            "sess-1",
            _files("m1", outside),
            media_root=media_root,
            allowed_roots=allowed_roots,
            session_media_dir=session_media_dir,
        )

    assert "不在本会话素材目录内" in str(exc.value)
    assert list(media_root.glob("**/*")) == []


def test_media_path_must_be_under_allowed_root(tmp_path, session_media_dir):
    """目标目录越过白名单 -> 拒绝(不扩大白名单绕过)。"""
    outside_root = tmp_path / "not-allowed"
    outside_root.mkdir()

    src = _make_source(session_media_dir, "clip.mov")

    with pytest.raises(MCodeMediaError) as exc:
        prepare_media_copies(
            "sess-1",
            _files("m1", src),
            media_root=outside_root,
            allowed_roots=[outside_root.parent / "allowed-elsewhere"],
            session_media_dir=session_media_dir,
        )

    assert "白名单" in str(exc.value)


def test_media_rejects_bad_session_id(session_media_dir, media_root, allowed_roots):
    """session_id 非法 -> 直接拒绝(防止目录穿越)。"""
    src = _make_source(session_media_dir, "clip.mov")

    for bad in ["../escape", "sess/../x", "", "a" * 65, "bad id"]:
        with pytest.raises(MCodeMediaError):
            prepare_media_copies(
                bad,
                _files("m1", src),
                media_root=media_root,
                allowed_roots=allowed_roots,
                session_media_dir=session_media_dir,
            )


def test_media_rejects_missing_source(session_media_dir, media_root, allowed_roots):
    """源文件不存在 -> 明确报错。"""
    missing = session_media_dir / "nope.mov"
    with pytest.raises(MCodeMediaError) as exc:
        prepare_media_copies(
            "sess-1",
            _files("m1", missing),
            media_root=media_root,
            allowed_roots=allowed_roots,
            session_media_dir=session_media_dir,
        )
    assert "不存在" in str(exc.value) or "不是普通文件" in str(exc.value)


# --------------------------------------------------------------------------
# 幂等 / 冲突
# --------------------------------------------------------------------------

def test_media_copy_is_idempotent(session_media_dir, media_root, allowed_roots):
    """同一个 ``media_id`` 重复复制 -> 同名同内容,复用,不产生第二份。"""
    src = _make_source(session_media_dir, "clip.mov", b"same-bytes")

    first = prepare_media_copies(
        "sess-1", _files("m1", src),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )
    second = prepare_media_copies(
        "sess-1", _files("m1", src),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )

    assert first[0]["path"] == second[0]["path"], "重复复制产生了不同路径"
    assert second[0]["reused"] is True
    files = [p for p in (media_root / "sess-1").iterdir() if p.is_file()]
    assert len(files) == 1, f"应该只有一份,实际 {len(files)} 份"


def test_media_copy_avoids_overwrite_on_conflict(session_media_dir, media_root, allowed_roots):
    """同名不同内容 -> 换唯一名,**不覆盖**既有文件。"""
    src_a = _make_source(session_media_dir, "clip.mov", b"content-A")
    first = prepare_media_copies(
        "sess-1", _files("m1", src_a),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )
    original = Path(first[0]["path"])
    original_bytes = original.read_bytes()

    # 换一个 media_id 但同名的主干 -> 名字前缀不同,这里直接用同名文件模拟冲突
    conflict_dir = media_root / "sess-2"
    conflict_dir.mkdir(parents=True, exist_ok=True)
    target = conflict_dir / original.name
    target.write_bytes(original_bytes)

    src_b = _make_source(session_media_dir, "clip.mov", b"content-B")
    out = prepare_media_copies(
        "sess-2", _files("m1", src_b),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )

    new_path = Path(out[0]["path"])
    assert new_path != target, "同名不同内容竟然覆盖了既有文件"
    assert target.read_bytes() == original_bytes, "既有文件被改动了"
    assert new_path.exists()
    assert _sha256(new_path) == _sha256(src_b)
    assert " (1)" in new_path.name, f"应该换唯一名,实际 {new_path.name}"


def test_media_copy_is_idempotent_in_conflict_path(session_media_dir, media_root, allowed_roots):
    """名字冲突路径下也要幂等:同内容重复复制 -> 复用同一个文件。"""
    target_dir = media_root / "sess-1"
    target_dir.mkdir(parents=True, exist_ok=True)
    # 预置一个同名但内容不同的文件,逼出冲突路径
    (target_dir / "m1_clip.mp4").write_bytes(b"preexisting-different")

    src = _make_source(session_media_dir, "clip.mov", b"payload-bytes")

    first = prepare_media_copies(
        "sess-1", _files("m1", src),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )
    second = prepare_media_copies(
        "sess-1", _files("m1", src),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )

    assert first[0]["path"] == second[0]["path"], "冲突路径下重复复制产生了不同路径"
    assert second[0]["reused"] is True
    assert (target_dir / "m1_clip.mp4").read_bytes() == b"preexisting-different", "既有文件被覆盖了"
    files = [p for p in target_dir.iterdir() if p.is_file()]
    assert len(files) == 2, f"应该只有 2 份(原名 + 冲突名),实际 {len(files)} 份"


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------

def test_media_manifest_lists_verified_absolute_paths(session_media_dir, media_root, allowed_roots):
    """清单里的路径是服务端生成的绝对路径,且过了白名单。"""
    src = _make_source(session_media_dir, "clip.mov", b"abc")
    prepare_media_copies(
        "sess-1", _files("m1", src),
        media_root=media_root, allowed_roots=allowed_roots,
        session_media_dir=session_media_dir,
    )

    target_dir = media_root / "sess-1"
    manifest = build_media_manifest(target_dir, allowed_roots)

    assert len(manifest) == 1
    item = manifest[0]
    assert os.path.isabs(item["path"])
    assert Path(item["path"]) == (target_dir / item["name"]).resolve()
    assert item["size"] > 0
    assert set(item.keys()) == {"name", "path", "size"}


def test_media_manifest_excludes_files_outside_whitelist(session_media_dir, media_root, allowed_roots):
    """白名单外的文件直接排除(不扩大白名单)。"""
    target_dir = media_root / "sess-1"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "good.mp4").write_bytes(b"good")
    # 白名单外的文件:放到别的根
    other = media_root.parent / "other-root"
    other.mkdir(exist_ok=True)
    (other / "bad.mp4").write_bytes(b"bad")

    manifest = build_media_manifest(target_dir, allowed_roots)
    names = [m["name"] for m in manifest]
    assert names == ["good.mp4"]

    # 把 target_dir 整个放到白名单外 -> 清单必须为空
    outside = media_root.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "x.mp4").write_bytes(b"x")
    assert build_media_manifest(outside, allowed_roots) == []


def test_media_manifest_missing_dir_returns_empty(tmp_path):
    """目录不存在 -> 空清单,不抛异常。"""
    assert build_media_manifest(tmp_path / "nope", [tmp_path]) == []


# --------------------------------------------------------------------------
# 端点级:``POST /api/system/import-video``
# --------------------------------------------------------------------------

@pytest.fixture
def endpoint_env(tmp_path, monkeypatch):
    """起一个只挂 ``import-video`` 端点的迷你 app,避开真服务的 lifespan。

    返回 ``(client, state)``;``state["written"]`` 收集被写出的导入结果。
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import agent_fastapi
    import nodes.node_04a_import_video as node_mod

    sid = "sess-ep"
    media_root = tmp_path / "inputs"
    media_root.mkdir()
    project_media_dir = tmp_path / "outputs" / "media"
    project_media_dir.mkdir(parents=True)
    session_media_dir = tmp_path / "outputs" / sid / "media"
    session_media_dir.mkdir(parents=True)

    src = session_media_dir / "clip.mov"
    src.write_bytes(b"video-bytes")

    # 速率限制:测试里不限流(真桶 burst=1,连着调会被 429)
    class _AlwaysAllow:
        async def allow(self, *a, **kw):
            return (True, None, None)

    monkeypatch.setattr(agent_fastapi, "RATE_LIMITER", _AlwaysAllow())

    written: List[Any] = []
    monkeypatch.setattr(node_mod, "find_pending_request", lambda: ("job-1", {}))
    monkeypatch.setattr(
        node_mod,
        "write_import_result",
        lambda job_id, payload: written.append({"job_id": job_id, "payload": payload}),
    )

    app = FastAPI()
    app.state.cfg = SimpleNamespace(
        project=SimpleNamespace(media_dir=str(project_media_dir)),
        mcode=SimpleNamespace(media_root=str(media_root)),
    )
    app.post("/api/system/import-video")(agent_fastapi.import_video_endpoint)

    state = {
        "session_id": sid,
        "src": src,
        "media_root": media_root,
        "written": written,
        "agent_fastapi": agent_fastapi,
    }
    with TestClient(app) as client:
        state["client"] = client
        yield state


def _post_import(state, monkeypatch, allowed):
    """POST 一次导入视频,白名单根由 ``allowed`` 指定。"""
    monkeypatch.setattr(state["agent_fastapi"], "allowed_media_roots", lambda: allowed)
    return state["client"].post(
        "/api/system/import-video",
        json={
            "session_id": state["session_id"],
            "files": [{
                "media_id": "m1",
                "filename": "clip.mov",
                "stored_path": str(state["src"]),
            }],
        },
    )


def test_media_copy_failure_prevents_result_write(endpoint_env, monkeypatch):
    """复制失败 -> 端点 500 + **不写导入结果** + 无残留 ``.part``。"""
    state = endpoint_env
    resp = _post_import(state, monkeypatch, [state["media_root"].parent / "elsewhere"])

    assert resp.status_code == 500, resp.text
    assert "素材准备失败" in resp.json()["detail"]
    assert state["written"] == [], "复制失败时不该写导入结果"
    assert list(state["media_root"].glob("**/*.part")) == [], "残留了 .part"
    assert list(state["media_root"].glob("**/*")) == [], "失败后不该留下正式文件"


def test_media_copy_success_writes_result_and_returns_prepared(endpoint_env, monkeypatch):
    """复制成功 -> 写导入结果(载荷不变)+ 响应带 ``prepared_media``。"""
    state = endpoint_env
    resp = _post_import(state, monkeypatch, [state["media_root"]])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["media_id"] == "m1"
    assert body["filename"] == "clip.mov"

    prepared = body["prepared_media"]
    assert len(prepared) == 1
    copy_path = Path(prepared[0]["path"])
    assert copy_path.exists()
    assert copy_path.parent == (state["media_root"] / state["session_id"]).resolve()
    assert prepared[0]["size"] == state["src"].stat().st_size
    assert prepared[0]["sha256"] == _sha256(state["src"])

    # 导入结果载荷**不变**:仍指向原始 stored_path(图侧契约不动)
    assert len(state["written"]) == 1
    payload = state["written"][0]["payload"]
    assert payload["media_paths"] == [str(state["src"])]
    assert payload["stored_path"] == str(state["src"])
    assert payload["media_id"] == "m1"


def test_media_copy_source_outside_session_dir_is_rejected(endpoint_env, monkeypatch):
    """客户端伪造 ``stored_path`` -> 500 + 不写导入结果。"""
    state = endpoint_env
    outside = state["media_root"].parent / "outside.mov"
    outside.write_bytes(b"outside")

    monkeypatch.setattr(state["agent_fastapi"], "allowed_media_roots", lambda: [state["media_root"]])
    resp = state["client"].post(
        "/api/system/import-video",
        json={
            "session_id": state["session_id"],
            "files": [{
                "media_id": "m1",
                "filename": "outside.mov",
                "stored_path": str(outside),
            }],
        },
    )

    assert resp.status_code == 500, resp.text
    assert "素材准备失败" in resp.json()["detail"]
    assert state["written"] == []


def test_media_copy_skips_files_without_media_id(endpoint_env, monkeypatch):
    """旧版单文件载荷(没有 ``media_id``)-> **导入照样成功**,只是不复制。

    目标名是 ``<media_id>_<净化stem><ext>``,没有 media_id 就没有安全命名依据。
    这种文件跳过复制,但导入结果载荷保持不变 —— 不能因为加了个副本功能
    就把旧客户端的导入搞挂。
    """
    state = endpoint_env
    monkeypatch.setattr(state["agent_fastapi"], "allowed_media_roots", lambda: [state["media_root"]])

    resp = state["client"].post(
        "/api/system/import-video",
        json={
            "session_id": state["session_id"],
            "filename": "clip.mov",
            "stored_path": str(state["src"]),
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["prepared_media"] == [], "没有 media_id 就不该产生副本"

    # 导入结果照旧写出来了,载荷不变
    assert len(state["written"]) == 1
    assert state["written"][0]["payload"]["stored_path"] == str(state["src"])
    assert list(state["media_root"].glob("**/*")) == [], "不该留下副本"


def test_media_copy_resolves_path_server_side_when_client_sends_empty(endpoint_env, monkeypatch):
    """网页端 ``stored_path`` 是空串时 -> **服务端**按 media_id 解析路径。

    这就是真实前端的形状:``public_media`` 不暴露 ``path``,
    ``app.js`` 送来的 ``stored_path`` 恒为 ``""``。
    只信客户端路径的话,「导入视频」会直接 500。
    """
    from types import SimpleNamespace

    state = endpoint_env
    monkeypatch.setattr(state["agent_fastapi"], "allowed_media_roots", lambda: [state["media_root"]])

    # 造一个会话媒体登记表:media_id -> 落盘路径
    class _Sess:
        load_media = {
            "m1": SimpleNamespace(path=str(state["src"])),
        }

    class _Store:
        async def get_or_404(self, session_id):
            return _Sess()

    state["client"].app.state.sessions = _Store()

    resp = state["client"].post(
        "/api/system/import-video",
        json={
            "session_id": state["session_id"],
            "files": [{
                "media_id": "m1",
                "filename": "clip.mov",
                "stored_path": "",          # <- 网页端的真实形状
            }],
        },
    )

    assert resp.status_code == 200, resp.text
    prepared = resp.json()["prepared_media"]
    assert len(prepared) == 1
    copy_path = Path(prepared[0]["path"])
    assert copy_path.exists()
    assert copy_path.parent == (state["media_root"] / state["session_id"]).resolve()
    assert _sha256(copy_path) == _sha256(state["src"])

    # 导入结果载荷仍然用**客户端**给的原始值(契约不变):
    # 空串在 build_result_payload 里会归一成 None
    payload = state["written"][0]["payload"]
    assert payload["stored_path"] is None
    assert payload["media_paths"] == [""]




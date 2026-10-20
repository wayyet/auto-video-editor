"""握手文件 ``import_video_request.json`` / ``import_video_result.json`` 单测。

只测**纯文件 IO 层**(路径、读写语义、find_pending_request 的边界),
节点层的轮询/终态逻辑在 ``test_node_04a_import_video.py``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.node_04a_import_video import (
    build_result_payload,
    extract_media_paths,
    find_pending_request,
    import_video_request_filename,
    import_video_result_filename,
    parse_import_payload,
    read_import_result,
    write_import_request,
    write_import_result,
)


def test_write_import_request_creates_job_dir_and_contract(tmp_path: Path) -> None:
    """写请求 → 自动建 ``<root>/<job_id>/``,payload 契约齐全。"""
    out_root = tmp_path / "outputs"
    target = write_import_request("job-contract", root=out_root)

    assert target == out_root / "job-contract" / import_video_request_filename
    assert target.exists()

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["job_id"] == "job-contract"
    assert payload["requested_at"].endswith("Z")
    assert payload["accept_exts"] == [".mp4", ".mov", ".mkv", ".avi", ".webm"]
    assert payload["accept_mime_prefixes"] == ["video/"]


def test_write_import_result_injects_job_id_and_leaves_no_tmp(tmp_path: Path) -> None:
    """写结果 → 自动补 ``job_id``;临时文件已被 ``os.replace`` 消费掉。"""
    out_root = tmp_path / "outputs"
    target = write_import_result(
        "job-res", {"ok": True, "filename": "x.mp4"}, root=out_root
    )

    assert target == out_root / "job-res" / import_video_result_filename
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["job_id"] == "job-res"
    assert payload["ok"] is True
    # 不能留下 .tmp 残留
    assert not list(target.parent.glob("*.tmp"))


def test_read_import_result_returns_none_when_absent_or_corrupt(tmp_path: Path) -> None:
    """不存在 / 半写损坏 → 返回 ``None``(不是抛异常),让轮询继续。"""
    out_root = tmp_path / "outputs"

    # 1. 完全没有
    assert read_import_result("job-none", root=out_root) is None

    # 2. 写了一半的 JSON
    write_import_result("job-half", {"ok": True}, root=out_root)
    (out_root / "job-half" / import_video_result_filename).write_text(
        '{"ok": tru', encoding="utf-8"
    )
    assert read_import_result("job-half", root=out_root) is None

    # 3. 合法 JSON 但不是 dict
    (out_root / "job-half" / import_video_result_filename).write_text(
        "[1,2,3]", encoding="utf-8"
    )
    assert read_import_result("job-half", root=out_root) is None

    # 4. 恢复合法后能读出来
    write_import_result("job-half", {"ok": False, "error": "x"}, root=out_root)
    assert read_import_result("job-half", root=out_root) == {
        "job_id": "job-half",
        "ok": False,
        "error": "x",
    }


def test_clear_import_request_is_idempotent(tmp_path: Path) -> None:
    """删请求:删得掉返回 True,已不存在返回 False(不抛异常)。"""
    from nodes.node_04a_import_video import clear_import_request

    out_root = tmp_path / "outputs"
    write_import_request("job-clr", root=out_root)
    assert (out_root / "job-clr" / import_video_request_filename).exists()

    assert clear_import_request("job-clr", root=out_root) is True
    assert not (out_root / "job-clr" / import_video_request_filename).exists()
    # 重复删不报错
    assert clear_import_request("job-clr", root=out_root) is False
    # 从没存在过的 job 同理
    assert clear_import_request("job-never", root=out_root) is False

    # 清掉后该 job 不再是"待应答",find_pending_request 不会命中它
    assert find_pending_request(root=out_root) is None


def test_find_pending_request_none_when_all_answered_or_empty(tmp_path: Path) -> None:
    """全都有 result / 根目录不存在 / 请求文件损坏 → 均为 ``None``。"""
    out_root = tmp_path / "outputs"

    # 1. 根目录压根不存在
    assert find_pending_request(root=out_root) is None

    # 2. 每个 job 都已应答
    write_import_request("j1", root=out_root)
    write_import_result("j1", {"ok": True}, root=out_root)
    assert find_pending_request(root=out_root) is None

    # 3. 请求文件是坏 JSON → 跳过,不当成待处理
    bad_dir = out_root / "j2"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / import_video_request_filename).write_text("{not json", encoding="utf-8")
    assert find_pending_request(root=out_root) is None


def test_find_pending_request_falls_back_to_dirname_when_job_id_missing(
    tmp_path: Path,
) -> None:
    """请求文件里没有 ``job_id`` → 用目录名兜底(端点仍能写对目录)。"""
    out_root = tmp_path / "outputs"
    job_dir = out_root / "job-from-dirname"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / import_video_request_filename).write_text(
        json.dumps({"requested_at": "2026-10-01T00:00:00Z"}), encoding="utf-8"
    )

    pending = find_pending_request(root=out_root)
    assert pending is not None
    job_id, payload = pending
    assert job_id == "job-from-dirname"
    assert payload["requested_at"] == "2026-10-01T00:00:00Z"


# ---------------------------------------------------------------------------
# 多文件协议:parse_import_payload / build_result_payload / extract_media_paths
# ---------------------------------------------------------------------------
def test_parse_import_payload_accepts_new_files_list() -> None:
    """新格式:``files`` 列表 → 归一成文件列表(顺序保持)。"""
    files = parse_import_payload({
        "session_id": "s1",
        "files": [
            {"media_id": "m1", "filename": "a.mov", "stored_path": "E:/a.mov"},
            {"media_id": "m2", "filename": "b.mov", "stored_path": "E:/b.mov"},
        ],
    })
    assert files == [
        {"media_id": "m1", "filename": "a.mov", "stored_path": "E:/a.mov"},
        {"media_id": "m2", "filename": "b.mov", "stored_path": "E:/b.mov"},
    ]


def test_parse_import_payload_accepts_legacy_single_file() -> None:
    """旧格式:单值字段 → 归一成 1 个文件(向后兼容)。"""
    files = parse_import_payload({
        "session_id": "s1",
        "media_id": "m1",
        "filename": "a.mov",
        "stored_path": "E:/a.mov",
    })
    assert files == [
        {"media_id": "m1", "filename": "a.mov", "stored_path": "E:/a.mov"}
    ]


def test_parse_import_payload_empty_and_dirty() -> None:
    """空 body / 无有效文件 / 脏数据 → 空列表(端点据此 400)。"""
    assert parse_import_payload({}) == []
    assert parse_import_payload({"session_id": "s1"}) == []
    assert parse_import_payload({"session_id": "s1", "files": []}) == []
    # files 里的脏条目(既没 media_id 也没 stored_path)被丢掉
    assert parse_import_payload({
        "session_id": "s1",
        "files": [{"filename": "x.mov"}, {"media_id": "m1", "stored_path": "E:/x.mov"}],
    }) == [{"media_id": "m1", "filename": "x.mov", "stored_path": "E:/x.mov"}]
    # 非 dict body
    assert parse_import_payload(["not", "a", "dict"]) == []  # type: ignore[arg-type]


def test_parse_import_payload_falls_back_to_legacy_when_files_absent() -> None:
    """``files`` 缺失 → 回退旧的单值字段(老前端仍能握手)。"""
    files = parse_import_payload({
        "session_id": "s1",
        "media_id": "m9",
        "filename": "z.mp4",
        "stored_path": "E:/z.mp4",
    })
    assert len(files) == 1
    assert files[0]["media_id"] == "m9"


def test_build_result_payload_has_list_and_single_fields() -> None:
    """列表字段(新)齐全 + 单值字段(旧)= 第一个,两套都在。"""
    payload = build_result_payload("websid", [
        {"media_id": "m1", "filename": "a.mov", "stored_path": "E:/a.mov"},
        {"media_id": "m2", "filename": "b.mov", "stored_path": "E:/b.mov"},
    ])
    assert payload["ok"] is True
    assert payload["web_session_id"] == "websid"
    # 列表字段
    assert payload["media_paths"] == ["E:/a.mov", "E:/b.mov"]
    assert payload["filenames"] == ["a.mov", "b.mov"]
    assert payload["media_ids"] == ["m1", "m2"]
    # 单值字段 = 第一个(兼容老节点 / 老 checkpoint)
    assert payload["stored_path"] == "E:/a.mov"
    assert payload["filename"] == "a.mov"
    assert payload["media_id"] == "m1"


def test_build_result_payload_empty_files_is_safe() -> None:
    """空文件列表 → 单值字段为 None,列表为空数组(不抛异常)。"""
    payload = build_result_payload("websid", [])
    assert payload["media_paths"] == []
    assert payload["stored_path"] is None
    assert payload["filename"] is None
    assert payload["media_id"] is None


def test_extract_media_paths_prefers_list_and_falls_back() -> None:
    """读 result:优先 ``media_paths``;没有就回退 ``stored_path``(兼容老文件)。"""
    assert extract_media_paths({"media_paths": ["E:/a.mov", "E:/b.mov"]}) == [
        "E:/a.mov", "E:/b.mov"
    ]
    # 老 result 只有单值
    assert extract_media_paths({"stored_path": "E:/a.mov"}) == ["E:/a.mov"]
    # 空列表 → 回退单值
    assert extract_media_paths({"media_paths": [], "stored_path": "E:/a.mov"}) == [
        "E:/a.mov"
    ]
    # 去重 + 去空
    assert extract_media_paths(
        {"media_paths": ["E:/a.mov", "", "E:/a.mov", "E:/b.mov"]}
    ) == ["E:/a.mov", "E:/b.mov"]
    assert extract_media_paths({}) == []

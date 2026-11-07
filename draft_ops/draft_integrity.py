"""draft_integrity — 双语草稿交付 QA 的结构化检查(2026-11-07 阶段 4)。

计划 §8 阶段 4(人工关卡与剪映草稿导入)的检查落地:

- 检查双草稿(中文主草稿 + 英文分支)双文件、元数据、素材路径、轨道和必要索引;
- 检查 ``fork_draft_for_english_branch`` 复制出的英文分支是否符合剪映项目目录
  结构(双文件 + 注册元数据,与 :mod:`draft_ops.draft_publisher` 的发布产物同口径);
- 校验转场 / 视频特效 / 贴纸等 ``extra_material_refs`` 引用不悬空;
- 校验关卡② 后中文音频轨完整镜像到英文分支(整轨复制 + audio materials 无丢失);
- QA 结果结构化为 issue dict 列表,供 ``join_before_delivery`` 写
  ``join_qa_report`` 并经 ``delivery.qa_*`` 事件在创建台展示。

纪律(沿用 join_before_delivery 既有口径):
- **英文分支从未生成**(``draft_dir_en_branch`` 为空,例如 fork 早退)→ 整段跳过,
  不当成问题 —— 那是上游降级,不是交付缺陷;
- 检查结果只记录不阻断:``join_before_delivery`` 仍走到 END,由创建台展示问题;
- ``severity="error"`` 才算 QA 失败;``warning`` 是提示(如 cover 路径存疑),
  不影响 passed 判定。

与既有 ``join_before_delivery`` 检查的分工:
- 节点内的封面 / SRT / 尾链四项检查保留原样(单测契约不变);
- 本模块是**新增的深度检查**(双文件一致性 / 索引 / 素材引用与路径 / 音频镜像),
  结果合并进同一份 ``join_qa_report``。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 双文件契约文件名(与 safe_write_draft 写入产物一致)
DUAL_FILES: tuple[str, ...] = ("draft_content.json", "draft_info.json")

#: 单草稿元数据文件(fork / 发布产物都应带)
META_INFO_NAME = "draft_meta_info.json"

#: 根索引文件名(剪映首页列表依据)
ROOT_META_NAME = "root_meta_info.json"

#: 发布开启时,正式草稿库下的目录才要求已注册(见 _registered_expected)
SEV_ERROR = "error"
SEV_WARNING = "warning"


def _issue(check: str, severity: str, draft: str, detail: str) -> dict[str, str]:
    """构造一条结构化 issue。``draft`` 取 ``zh`` / ``en`` / ``-``(跨草稿)。"""
    return {"check": check, "severity": severity, "draft": draft, "detail": detail}


def issue_text(issue: dict[str, str]) -> str:
    """issue → 单行文本(并入 ``join_qa_issues`` 字符串列表用)。"""
    draft_tag = {"zh": "中文", "en": "英文"}.get(issue.get("draft", "-"), "")
    return f"[{draft_tag}{issue['check']}] {issue['detail']}" if draft_tag else (
        f"[{issue['check']}] {issue['detail']}"
    )


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    """读 JSON 文件;返回 ``(obj, None)`` 或 ``(None, 错误说明)``;不存在返回 ``(None, None)``。"""
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _is_checkable_local_path(value: Any) -> bool:
    """值是否为"应该存在于本机"的路径(URI / 空串 / 相对占位不算)。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or "://" in v:  # file:// / http(s):// / assets:// 等 URI 不查盘
        return False
    if v.startswith("<") and v.endswith(">"):  # 测试占位
        return False
    return Path(v).is_absolute() or v.startswith("\\\\")


# ---------------------------------------------------------------------------
# 单草稿目录检查
# ---------------------------------------------------------------------------
def check_dual_files(draft_dir: Path, tag: str) -> tuple[list[dict[str, str]], dict | None]:
    """双文件存在、可解析、内容一致;返回 ``(issues, content_obj|None)``。"""
    issues: list[dict[str, str]] = []
    objs: list[Any] = []
    for name in DUAL_FILES:
        path = draft_dir / name
        obj, err = _read_json(path)
        if not path.is_file():
            issues.append(_issue("双文件", SEV_ERROR, tag, f"缺少 {name}: {draft_dir}"))
            continue
        if err is not None:
            issues.append(_issue("双文件", SEV_ERROR, tag, f"{name} 不可解析: {err}"))
            continue
        objs.append(obj)
    content: dict | None = None
    if len(objs) == len(DUAL_FILES):
        content_obj, info_obj = objs
        if content_obj != info_obj:
            issues.append(_issue("双文件", SEV_ERROR, tag, "draft_content.json 与 draft_info.json 内容不一致"))
        if isinstance(content_obj, dict):
            content = content_obj
        else:
            issues.append(_issue("双文件", SEV_ERROR, tag, "draft_content.json 顶层不是 dict"))
    return issues, content


def check_meta_info(draft_dir: Path, tag: str) -> list[dict[str, str]]:
    """``draft_meta_info.json`` 存在且可解析(仅注册场景调用)。"""
    issues: list[dict[str, str]] = []
    path = draft_dir / META_INFO_NAME
    obj, err = _read_json(path)
    if obj is None:
        detail = f"缺少 {META_INFO_NAME}(草稿未发布注册?)" if err is None else f"{META_INFO_NAME} 不可解析: {err}"
        issues.append(_issue("草稿元数据", SEV_ERROR, tag, detail))
    elif not isinstance(obj, dict) or not obj.get("draft_id"):
        issues.append(_issue("草稿元数据", SEV_ERROR, tag, f"{META_INFO_NAME} 缺 draft_id(注册元数据不完整)"))
    return issues


def check_root_index(draft_dir: Path, root: Path, tag: str) -> list[dict[str, str]]:
    """草稿已注册进 ``<root>/root_meta_info.json`` 的 ``all_draft_store[]``。"""
    issues: list[dict[str, str]] = []
    meta_path = root / ROOT_META_NAME
    obj, err = _read_json(meta_path)
    if obj is None:
        detail = f"根索引 {meta_path} 不存在(剪映首页不会列出该草稿)"
        if err is not None:
            detail = f"根索引 {meta_path} 不可读: {err}"
        issues.append(_issue("根索引", SEV_ERROR, tag, detail))
        return issues
    store = obj.get("all_draft_store")
    if not isinstance(store, list):
        issues.append(_issue("根索引", SEV_ERROR, tag, f"根索引缺 all_draft_store 列表: {meta_path}"))
        return issues
    draft_name = draft_dir.name
    if not any(isinstance(e, dict) and e.get("draft_name") == draft_name for e in store):
        issues.append(_issue("根索引", SEV_ERROR, tag, f"draft_name={draft_name} 未注册进根索引(剪映首页不会列出)"))
    return issues


def _material_ids(draft: dict) -> set[str]:
    """收集 ``materials`` 下所有 bucket 里出现过的 id(版本无关,防悬空引用)。"""
    ids: set[str] = set()
    materials = draft.get("materials") or {}
    for bucket in materials.values():
        if isinstance(bucket, list):
            for item in bucket:
                if isinstance(item, dict) and item.get("id"):
                    ids.add(str(item["id"]))
    return ids


def check_track_references(draft: dict, tag: str) -> list[dict[str, str]]:
    """所有轨道 segment 的 ``material_id`` 与 ``extra_material_refs`` 都能解析到素材。"""
    issues: list[dict[str, str]] = []
    known = _material_ids(draft)
    for ti, track in enumerate(draft.get("tracks") or []):
        if not isinstance(track, dict):
            continue
        track_type = str(track.get("type") or "?")
        for si, seg in enumerate(track.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            where = f"track#{ti}({track_type}) segment#{si}"
            mid = seg.get("material_id")
            if mid and str(mid) not in known:
                issues.append(_issue("素材引用", SEV_ERROR, tag, f"{where} material_id={mid} 悬空(materials 中不存在)"))
            for ref in seg.get("extra_material_refs") or []:
                if ref and str(ref) not in known:
                    issues.append(_issue("素材引用", SEV_ERROR, tag, f"{where} extra_material_ref={ref} 悬空(materials 中不存在)"))
    return issues


def check_material_paths(draft: dict, tag: str) -> list[dict[str, str]]:
    """``materials.videos[].path`` 与 ``materials.audios[].path/ref`` 指向的本机文件存在。

    URI / 空串 / 占位一律跳过(远程素材、剪映 VIP 资源不在本机路径体系内)。
    """
    issues: list[dict[str, str]] = []
    materials = draft.get("materials") or {}
    for m in materials.get("videos") or []:
        if not isinstance(m, dict):
            continue
        p = m.get("path")
        if _is_checkable_local_path(p) and not Path(p).is_file():
            issues.append(_issue("素材路径", SEV_ERROR, tag, f"video material {m.get('id', '?')} 的文件不存在: {p}"))
    for m in materials.get("audios") or []:
        if not isinstance(m, dict):
            continue
        for key in ("path", "ref"):
            p = m.get(key)
            if _is_checkable_local_path(p) and not Path(p).is_file():
                issues.append(
                    _issue("素材路径", SEV_ERROR, tag, f"audio material {m.get('id', '?')} 的 {key} 文件不存在: {p}")
                )
    return issues


def check_video_track(draft: dict, tag: str) -> list[dict[str, str]]:
    """至少一条 video 轨且带 ≥1 个 segment(空时间轴的草稿进剪映没有可编辑内容)。"""
    video_tracks = [t for t in (draft.get("tracks") or []) if isinstance(t, dict) and t.get("type") == "video"]
    total_segments = sum(len(t.get("segments") or []) for t in video_tracks)
    issues: list[dict[str, str]] = []
    if not video_tracks or total_segments == 0:
        issues.append(_issue("视频轨", SEV_ERROR, tag, f"无视频轨或视频轨为空(video_tracks={len(video_tracks)}, segments={total_segments})"))
    return issues


def check_texts(draft: dict, tag: str) -> list[dict[str, str]]:
    """``materials.texts[]`` 条目 content 非空、时间 range 合法(字幕/花字引用完整性)。

    双形态(2026-11-07 文本链原生升级):人工在剪映保存后 material 的私有
    ``target_timerange`` 被丢弃,计时权威移到 text 轨道段 —— 按 material_id
    兜底;两处都缺才告警(字幕不可见)。
    """
    issues: list[dict[str, str]] = []
    texts = (draft.get("materials") or {}).get("texts") or []
    text_track_tr_by_mat: dict[str, dict] = {}
    for tr in draft.get("tracks") or []:
        if not isinstance(tr, dict) or tr.get("type") != "text":
            continue
        for s in tr.get("segments") or []:
            if isinstance(s, dict) and s.get("material_id"):
                text_track_tr_by_mat.setdefault(str(s["material_id"]), s.get("target_timerange") or {})
    for i, t in enumerate(texts):
        if not isinstance(t, dict):
            issues.append(_issue("字幕文本", SEV_ERROR, tag, f"texts#{i} 不是 dict"))
            continue
        content = t.get("content")
        if not isinstance(content, str) or not content.strip():
            issues.append(_issue("字幕文本", SEV_ERROR, tag, f"texts#{i}({t.get('id', '?')}) content 为空"))
        tr = t.get("target_timerange")
        if not isinstance(tr, dict):
            tr = text_track_tr_by_mat.get(str(t.get("id") or ""))
        if isinstance(tr, dict):
            try:
                if int(tr.get("duration") or 0) <= 0:
                    issues.append(_issue("字幕文本", SEV_WARNING, tag, f"texts#{i}({t.get('id', '?')}) duration<=0"))
            except (TypeError, ValueError):
                issues.append(_issue("字幕文本", SEV_WARNING, tag, f"texts#{i}({t.get('id', '?')}) target_timerange 非法"))
        else:
            issues.append(_issue(
                "字幕文本", SEV_WARNING, tag,
                f"texts#{i}({t.get('id', '?')}) 无计时(既无 target_timerange 也无 text 轨段,字幕不可见)",
            ))
    return issues


def check_draft_dir(
    draft_dir: Path,
    *,
    tag: str,
    registered_expected: bool,
    root: Path | None = None,
) -> dict[str, Any]:
    """单草稿目录全套结构检查。

    Args:
        draft_dir: 草稿目录(含 draft_content.json)。
        tag: 报告里的草稿标识(``zh`` / ``en``)。
        registered_expected: 是否要求已注册(见 :func:`registered_expected_for`)。
        root: 剪映草稿库根(检查根索引用;``registered_expected=True`` 时必须给)。

    Returns:
        ``{"draft_dir": str, "present": bool, "draft": dict|None,
        "issues": [...], "checks": [...]}``。
    """
    result: dict[str, Any] = {
        "draft_dir": str(draft_dir),
        "present": False,
        "draft": None,
        "issues": [],
        "checks": [],
    }
    issues = result["issues"]
    if not draft_dir.is_dir():
        issues.append(_issue("目录", SEV_ERROR, tag, f"草稿目录不存在: {draft_dir}"))
        return result
    result["present"] = True

    dual_issues, content = check_dual_files(draft_dir, tag)
    issues.extend(dual_issues)
    result["checks"].append("dual_files")
    result["draft"] = content

    if registered_expected:
        issues.extend(check_meta_info(draft_dir, tag))
        result["checks"].append("meta_info")
        if root is not None:
            issues.extend(check_root_index(draft_dir, root, tag))
            result["checks"].append("root_index")

    if content is not None:
        issues.extend(check_video_track(content, tag))
        result["checks"].append("video_track")
        issues.extend(check_track_references(content, tag))
        result["checks"].append("track_references")
        issues.extend(check_material_paths(content, tag))
        result["checks"].append("material_paths")
        issues.extend(check_texts(content, tag))
        result["checks"].append("texts")
    return result


def registered_expected_for(draft_dir: Path) -> bool:
    """该草稿目录是否应视为"已发布注册":发布开启且目录在剪映草稿库根下。"""
    import config

    if not config.JY_DRAFT_PUBLISH_ENABLED:
        return False
    try:
        root = Path(config.JIANYING_DRAFT_ROOT).resolve()
        return Path(draft_dir).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 关卡② 音频镜像完整性
# ---------------------------------------------------------------------------
def check_audio_mirror(zh_draft: dict, en_draft: dict) -> dict[str, Any]:
    """校验中文音频轨(含引用的 audio materials)完整镜像到英文草稿。

    与 :func:`nodes.node_12_human_add_bgm.mirror_bgm_to_en_draft` 的"整轨替换"
    语义对齐:镜像成功 = 英文侧的 audio 轨段集合与中文侧一致,且这些段引用的
    audio materials 在英文侧都能解析到。

    Returns:
        ``{"issues": [...], "summary": {...}}``;中文无音频轨时无事可查(镜像
        本来就不产生内容),不算问题。
    """
    issues: list[dict[str, str]] = []

    def _audio_tracks(draft: dict) -> list[dict]:
        return [t for t in (draft.get("tracks") or []) if isinstance(t, dict) and t.get("type") == "audio"]

    zh_tracks = _audio_tracks(zh_draft)
    en_tracks = _audio_tracks(en_draft)
    summary: dict[str, Any] = {
        "zh_audio_tracks": len(zh_tracks),
        "en_audio_tracks": len(en_tracks),
        "zh_segments": sum(len(t.get("segments") or []) for t in zh_tracks),
        "en_segments": sum(len(t.get("segments") or []) for t in en_tracks),
        "matched": False,
    }
    if not zh_tracks:
        if en_tracks:
            issues.append(_issue("BGM镜像", SEV_WARNING, "-", "英文草稿有音频轨但中文主草稿没有(镜像来源缺失?)"))
            summary["matched"] = False
        else:
            summary["matched"] = True  # 两侧都没有:关卡② 未加 BGM,无事可查
        return {"issues": issues, "summary": summary}

    if not en_tracks:
        issues.append(_issue("BGM镜像", SEV_ERROR, "-", f"中文音频轨未镜像到英文草稿(中文 {summary['zh_segments']} 段)"))
        return {"issues": issues, "summary": summary}

    zh_seg_ids: list[str] = [
        str(s["id"]) for t in zh_tracks for s in (t.get("segments") or []) if isinstance(s, dict) and s.get("id")
    ]
    en_seg_ids: list[str] = [
        str(s["id"]) for t in en_tracks for s in (t.get("segments") or []) if isinstance(s, dict) and s.get("id")
    ]
    summary["matched"] = set(zh_seg_ids) == set(en_seg_ids)
    if not summary["matched"]:
        missing = sorted(set(zh_seg_ids) - set(en_seg_ids))
        extra = sorted(set(en_seg_ids) - set(zh_seg_ids))
        detail = []
        if missing:
            detail.append(f"英文缺段 {missing}")
        if extra:
            detail.append(f"英文多段 {extra}")
        issues.append(_issue("BGM镜像", SEV_ERROR, "-", f"英文音频轨与中文不一致:{'; '.join(detail)}"))

    # 引用的 audio materials 必须能在英文侧解析(mirror 复制过)
    en_known = _material_ids(en_draft)
    zh_known = _material_ids(zh_draft)
    for t in zh_tracks:
        for s in (t.get("segments") or []):
            if not isinstance(s, dict):
                continue
            mid = s.get("material_id")
            if mid and str(mid) in zh_known and str(mid) not in en_known:
                issues.append(_issue("BGM镜像", SEV_ERROR, "-", f"英文草稿缺 audio material {mid}(镜像丢失)"))
    return {"issues": issues, "summary": summary}


# ---------------------------------------------------------------------------
# join 节点用的总报告
# ---------------------------------------------------------------------------
def build_join_qa_report(state: dict[str, Any]) -> dict[str, Any]:
    """把双语草稿深度 QA 打包成结构化报告(``join_qa_report`` 字段)。

    口径:``draft_dir_en_branch`` 为空 → 整段跳过(上游降级,不是交付缺陷),
    返回 ``{"skipped": True}``;否则检查 zh / en 两份草稿 + 音频镜像。

    Returns:
        ``{"skipped": bool, "passed": bool, "checked_at": iso8601,
        "zh": {...}, "en": {...}|None, "mirror": {...},
        "issues": [合并后的结构化 issue 列表]}``。
    """
    en_dir_raw = state.get("draft_dir_en_branch")
    if not en_dir_raw:
        return {"skipped": True, "passed": True, "issues": []}

    zh_raw = state.get("draft_path")
    zh_dir = Path(zh_raw).parent if zh_raw else None
    en_dir = Path(en_dir_raw)

    zh_root = registered_expected_for(zh_dir) if zh_dir else False
    en_root = registered_expected_for(en_dir)
    root = None
    if zh_root or en_root:
        import config

        root = Path(config.JIANYING_DRAFT_ROOT)

    zh_report = (
        check_draft_dir(zh_dir, tag="zh", registered_expected=zh_root, root=root)
        if zh_dir is not None
        else {
            "draft_dir": None,
            "present": False,
            "draft": None,
            "issues": [_issue("目录", SEV_ERROR, "zh", "state.draft_path 缺失,中文主草稿无从检查")],
            "checks": [],
        }
    )
    en_report = check_draft_dir(en_dir, tag="en", registered_expected=en_root, root=root)

    zh_draft = zh_report.get("draft") or {}
    en_draft = en_report.get("draft") or {}
    mirror = check_audio_mirror(zh_draft, en_draft) if (zh_report["present"] and en_report["present"]) else {
        "issues": [],
        "summary": {"matched": None, "note": "草稿不齐,镜像检查跳过"},
    }

    issues: list[dict[str, str]] = [
        *zh_report["issues"],
        *en_report["issues"],
        *mirror["issues"],
    ]
    return {
        "skipped": False,
        "passed": not any(i["severity"] == SEV_ERROR for i in issues),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zh": {k: v for k, v in zh_report.items() if k != "draft"},
        "en": {k: v for k, v in en_report.items() if k != "draft"},
        "mirror": mirror["summary"],
        "issues": issues,
    }


__all__ = [
    "DUAL_FILES",
    "META_INFO_NAME",
    "ROOT_META_NAME",
    "SEV_ERROR",
    "SEV_WARNING",
    "build_join_qa_report",
    "check_audio_mirror",
    "check_draft_dir",
    "check_dual_files",
    "check_material_paths",
    "check_meta_info",
    "check_root_index",
    "check_texts",
    "check_track_references",
    "check_video_track",
    "issue_text",
    "registered_expected_for",
]

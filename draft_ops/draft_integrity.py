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
import os
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
    """issue → 单行文本(并入 ``join_qa_report.error_texts`` 字符串视图用)。"""
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
        # 阶段 2(2026-11-09 效果缺失修复):时长<=0 的音频素材不可播放 ——
        # 有素材、有路径但时长为 0 是"stub 冒充成功"的典型形态,warning 提示。
        duration = m.get("duration")
        duration_ms = m.get("duration_ms")
        try:
            us = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            us = None
        try:
            ms = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            ms = None
        known = us is not None or ms is not None
        if known and (us or 0) <= 0 and (ms or 0) <= 0:
            issues.append(
                _issue("素材路径", SEV_WARNING, tag, f"audio material {m.get('id', '?')} 时长<=0(不可播放,疑为占位)")
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


def check_texts_track_alignment(draft: dict, tag: str) -> list[dict[str, str]]:
    """text 轨道段 ↔ ``materials.texts`` 双向一致性(阶段 1,2026-11-09)。

    背景(效果缺失根因分析 §5.1):mapper 私有形状 text 轨曾被原生转换整轨
    丢弃而 QA 不报 —— "有字幕数据"与"时间轴可见"是两回事。本检查对每条
    text 轨段验证 material_id 可解析且显示文本非空;反向对未被任何段引用的
    texts 素材给 warning(躺在桶里、剪映时间轴不可见)。无 text 轨 + 无 texts
    是合法态(空输入),不报。
    """
    from jy_common.native_text import text_display_text

    issues: list[dict[str, str]] = []
    texts = {
        str(t.get("id") or ""): t
        for t in (draft.get("materials") or {}).get("texts") or []
        if isinstance(t, dict) and t.get("id")
    }
    referenced: set[str] = set()
    for tr in draft.get("tracks") or []:
        if not isinstance(tr, dict) or tr.get("type") != "text":
            continue
        for i, s in enumerate(tr.get("segments") or []):
            if not isinstance(s, dict):
                continue
            mid = str(s.get("material_id") or "")
            mat = texts.get(mid)
            if mat is None:
                issues.append(_issue(
                    "字幕一致", SEV_ERROR, tag,
                    f"text 轨段#{i}({s.get('id', '?')}) 引用的字幕素材 {mid!r} 不存在",
                ))
                continue
            referenced.add(mid)
            if not text_display_text(mat).strip():
                issues.append(_issue(
                    "字幕一致", SEV_ERROR, tag,
                    f"text 轨段#{i} 引用的字幕素材 {mid} 显示文本为空",
                ))
    for tid, mat in texts.items():
        if tid not in referenced:
            issues.append(_issue(
                "字幕一致", SEV_WARNING, tag,
                f"字幕素材 {tid}({text_display_text(mat)[:20]}) 未被任何 text 轨道段引用(时间轴不可见)",
            ))
    return issues


# ---------------------------------------------------------------------------
# 2026-11-11 模糊修复防线:crop 全幅 + 模糊类特效全片挂载
# ---------------------------------------------------------------------------
#: 全幅单位正方形(与 storyline/native_draft._CROP_TEMPLATE、剪映原生工程一致)
_FULL_FRAME_CROP: dict[str, float] = {
    "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
    "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
    "upper_right_x": 1.0, "upper_right_y": 0.0,
}


def check_video_crops(draft: dict, tag: str) -> list[dict[str, str]]:
    """video material 的 crop 必须是全幅单位正方形。

    退化裁切(如 lower_right_y=0.0 使四角变成三角形)会被剪映拉伸铺满画布,
    成片出现大面积定向拉丝——该 bug 曾随 ``_CROP_TEMPLATE`` 存活数日,字段级
    验收测不出,必须像素语义级校验。缺失 crop 的素材跳过(非我方写入形态)。
    """
    issues: list[dict[str, str]] = []
    for m in (draft.get("materials") or {}).get("videos") or []:
        if not isinstance(m, dict):
            continue
        crop = m.get("crop")
        if not isinstance(crop, dict) or not crop:
            continue
        mismatched = {
            k: crop.get(k)
            for k, v in _FULL_FRAME_CROP.items()
            if not isinstance(crop.get(k), (int, float)) or abs(float(crop[k]) - v) > 1e-6
        }
        if mismatched:
            issues.append(_issue(
                "素材裁切", SEV_ERROR, tag,
                f"video material {m.get('material_name') or m.get('id', '?')} crop 非全幅"
                f"(退化裁切会被剪映拉伸铺满画面): {mismatched}",
            ))
    return issues


def check_blur_video_effects(draft: dict, tag: str) -> list[dict[str, str]]:
    """我方 vfx 特效名字含"模糊"且**每个视频段都挂了至少一个**→ 告警。

    "抖动模糊"(0.3) 逐段全时长挂载(node_09 每段一个 vfx-N 条目)曾把成片
    染成全画面模糊(2026-11-11 定案)——注意各段引用的是不同 id,判定必须
    按"段覆盖率"而不是单 id 出现次数。只检查 id 前缀 ``vfx-`` 的我方特效,
    人工在剪映里添加的 uuid 特效不误伤。
    """
    issues: list[dict[str, str]] = []
    blur = {
        str(m.get("id")): str(m.get("name") or "")
        for m in (draft.get("materials") or {}).get("video_effects") or []
        if isinstance(m, dict) and str(m.get("id", "")).startswith("vfx-")
        and "模糊" in str(m.get("name") or "")
    }
    if not blur:
        return issues
    video_segments = [
        s
        for t in (draft.get("tracks") or [])
        if isinstance(t, dict) and t.get("type") == "video"
        for s in (t.get("segments") or []) if isinstance(s, dict)
    ]
    if not video_segments:
        return issues
    covered = sum(
        1 for s in video_segments
        if any(r in blur for r in (s.get("extra_material_refs") or []))
    )
    if covered == len(video_segments):
        names = sorted(set(blur.values()))
        issues.append(_issue(
            "模糊特效", SEV_WARNING, tag,
            f"模糊类特效 {names} 覆盖全部 {len(video_segments)} 个视频段(全片挂载):"
            "模糊类特效全片使用会把成片染糊,请确认是否有意为之",
        ))
    return issues


def check_video_material_aspect(draft: dict, tag: str) -> list[dict[str, str]]:
    """video material 的显示方向/比例必须与画布一致(阶段2 R3 修复的 QA 防线)。

    归一化(预裁切)失败被降级放行、或绕过归一化进入草稿的横屏素材,比例与
    1080×1920 画布不一致时剪映会拉伸/裁切变形 —— 字段级验收测不出,必须按
    **显示口径**(coded + 旋转元数据)实测。探测失败(合成路径 / 缺 ffprobe)
    静默跳过;归一化产物应已把方向烘焙进像素,仍带旋转元数据的条目一并告警。
    只报 warning:比例不一致是可交付但有观感缺陷的降级态,不是结构损坏。
    """
    import config
    from video_edit_capabilities.render import probe_video_dimensions

    issues: list[dict[str, str]] = []
    canvas_ratio = config.CANVAS_WIDTH / config.CANVAS_HEIGHT
    tolerance = config.DRAFT_SOURCE_ASPECT_TOLERANCE
    for m in (draft.get("materials") or {}).get("videos") or []:
        if not isinstance(m, dict):
            continue
        p = m.get("path")
        if not _is_checkable_local_path(p) or not Path(p).is_file():
            continue
        try:
            dims = probe_video_dimensions(Path(p))
        except Exception:  # noqa: BLE001 — QA 探测绝不阻断交付检查
            continue
        label = str(m.get("material_name") or m.get("id") or "?")
        if dims.rotation_degrees != 0:
            issues.append(_issue(
                "素材比例", SEV_WARNING, tag,
                f"video material {label} 带旋转元数据 rot={dims.rotation_degrees}"
                "(归一化产物应已烘焙方向,声明与显示方向可能不一致)",
            ))
        if dims.display_height <= 0 or dims.display_width <= 0:
            continue
        ratio = dims.display_width / dims.display_height
        if abs(ratio / canvas_ratio - 1.0) > tolerance:
            issues.append(_issue(
                "素材比例", SEV_WARNING, tag,
                f"video material {label} 显示尺寸 {dims.display_width}x{dims.display_height}"
                f" 与画布比例 {canvas_ratio:.4f} 不一致(未归一化或归一化降级,"
                "剪映将拉伸/裁切该素材)",
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
        issues.extend(check_texts_track_alignment(content, tag))
        result["checks"].append("texts_track_alignment")
        issues.extend(check_resource_ids(content, tag))
        result["checks"].append("resource_ids")
        issues.extend(check_video_crops(content, tag))
        result["checks"].append("video_crops")
        issues.extend(check_blur_video_effects(content, tag))
        result["checks"].append("blur_video_effects")
        issues.extend(check_video_material_aspect(content, tag))
        result["checks"].append("video_material_aspect")
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

    与 :func:`nodes.node_12_human_add_bgm.mirror_bgm_to_en_draft` 的"按角色
    合并"语义对齐(P3b,2026-11-08):镜像成功 = 英文侧**镜像轨**(排除
    ``node_17`` 写入的英文配音轨,即 ``name=EN_VOICEOVER_TRACK_NAME`` 标记轨)
    的 audio 段集合与中文侧一致,且这些段引用的 audio materials 在英文侧都能
    解析到。英文配音轨是英文版合法的特有内容,不参与比对、不触发告警。

    Returns:
        ``{"issues": [...], "summary": {...}}``;中文无音频轨时无事可查(镜像
        本来就不产生内容),不算问题。
    """
    issues: list[dict[str, str]] = []
    # P3b:英文配音轨标记(权威定义 nodes/_dual_draft.py)。延迟 import:
    # draft_ops 不做包级反向依赖 nodes(nodes 侧 node_join_before_delivery
    # 正向 import 本模块),函数内取值无初始化顺序风险。
    from nodes._dual_draft import EN_VOICEOVER_TRACK_NAME

    def _audio_tracks(draft: dict) -> list[dict]:
        return [t for t in (draft.get("tracks") or []) if isinstance(t, dict) and t.get("type") == "audio"]

    zh_tracks = _audio_tracks(zh_draft)
    en_tracks_all = _audio_tracks(en_draft)
    # P3b:英文配音轨不参与镜像比对
    en_voiceover_tracks = [t for t in en_tracks_all if t.get("name") == EN_VOICEOVER_TRACK_NAME]
    en_tracks = [t for t in en_tracks_all if t.get("name") != EN_VOICEOVER_TRACK_NAME]
    summary: dict[str, Any] = {
        "zh_audio_tracks": len(zh_tracks),
        "en_audio_tracks": len(en_tracks),
        "en_voiceover_tracks": len(en_voiceover_tracks),
        "zh_segments": sum(len(t.get("segments") or []) for t in zh_tracks),
        "en_segments": sum(len(t.get("segments") or []) for t in en_tracks),
        "matched": False,
    }
    if not zh_tracks:
        if en_tracks:
            issues.append(_issue("BGM镜像", SEV_WARNING, "-", "英文草稿有音频轨但中文主草稿没有(镜像来源缺失?)"))
            summary["matched"] = False
        else:
            # 两侧镜像轨都没有:关卡② 未加 BGM,无事可查
            # (英文侧只有配音轨时,P3b 起同样不算问题)
            summary["matched"] = True
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
# 阶段 6(2026-11-09 效果缺失修复):required-feature gate
# ---------------------------------------------------------------------------
#: 必需功能 9 类(计划 §7.3.G)。创建台表单显式声明,缺省由
#: :func:`default_required_features` 按运行时能力推导。
REQUIRED_FEATURE_KEYS: tuple[str, ...] = (
    "subtitles_zh",
    "subtitles_en",
    "voiceover_zh",
    "voiceover_en",
    "bgm",
    "transitions",
    "text_animation",
    "stickers",
    "volume_fades",
)

#: QA 分类全集 = 9 类必需功能 + 恒检查但不 gate 的交付类别
#: (video_effects 不进 required:特效属"加分效果",缺资源时降级不阻断交付)。
CATEGORY_KEYS: tuple[str, ...] = REQUIRED_FEATURE_KEYS + (
    "video_effects",
    "covers",
    "srt",
    "draft_integrity",
)

#: 类别 → 创建台展示名(与 DELIVERY QA 明细的 check 字段中文一致)
CATEGORY_LABELS: dict[str, str] = {
    "subtitles_zh": "中文字幕",
    "subtitles_en": "英文字幕",
    "voiceover_zh": "中文配音",
    "voiceover_en": "英文配音",
    "bgm": "BGM",
    "transitions": "转场",
    "text_animation": "字幕动画",
    "stickers": "贴纸",
    "volume_fades": "音量淡入淡出",
    "video_effects": "视频特效",
    "covers": "封面",
    "srt": "英文字幕文件",
    "draft_integrity": "草稿结构",
}

#: 深度 QA issue 的 check 文本 → category(尾链/交付物 issue 在节点侧自带 category)
DEEP_CHECK_CATEGORY: dict[str, str] = {
    "字幕文本": "subtitles",  # 按issue draft 标签再细分 zh/en
    "字幕一致": "subtitles",
    "BGM镜像": "bgm",
    "双文件": "draft_integrity",
    "草稿元数据": "draft_integrity",
    "根索引": "draft_integrity",
    "目录": "draft_integrity",
    "视频轨": "draft_integrity",
    "素材引用": "draft_integrity",
    "素材路径": "draft_integrity",
    "素材裁切": "draft_integrity",
    "素材比例": "draft_integrity",
    "模糊特效": "video_effects",
}

#: 类别状态(passed=通过;failed=阻断级;degraded=降级,required 时阻断;
#: not_applicable=该类别本次运行不适用,永不阻断)
CAT_PASSED = "passed"
CAT_FAILED = "failed"
CAT_DEGRADED = "degraded"
CAT_NOT_APPLICABLE = "not_applicable"


def default_required_features() -> dict[str, bool]:
    """按运行时能力推导的必需功能默认集(无显式 ``required_features`` 时用)。

    推导原则(计划 §7.1 原则 6"按用户配置区分必需能力"):
    - 字幕/转场/动画/淡入淡出 = 阶段 1-5 已承诺能力,默认必选
      (单镜头转场、无旁白字幕等由 QA 判 not_applicable,不会误伤);
    - 配音 = 仅 ``TTS_BACKEND=firered``(与 ``nodes/storyline/_tts_adapter.py``
      同口径)时默认必选 —— stub 环境不默认阻断联调,但表单显式勾选后
      stub 即 degraded 阻断(「stub 不冒充成功」);
    - BGM = 关卡② 人工可选,默认不阻断;贴纸 = 资源 ID 未实机回填
      (fail-closed 降级),默认不阻断。
    """
    tts_backend = str(os.environ.get("TTS_BACKEND", "") or "").strip().lower()
    return {
        "subtitles_zh": True,
        "subtitles_en": True,
        "voiceover_zh": tts_backend == "firered",
        "voiceover_en": tts_backend == "firered",
        "bgm": False,
        "transitions": True,
        "text_animation": True,
        "stickers": False,
        "volume_fades": True,
    }


def resolve_required_features(state: dict[str, Any]) -> dict[str, bool]:
    """解析本次运行的必需功能集:显式声明覆盖推导默认,键之外忽略。"""
    resolved = default_required_features()
    raw = state.get("required_features")
    if isinstance(raw, dict):
        for key in REQUIRED_FEATURE_KEYS:
            value = raw.get(key)
            if isinstance(value, bool):
                resolved[key] = value
    return resolved


def _is_our_fx_material(material_id: str) -> bool:
    """是否我方确定性 id 的效果素材(人工/剪映原生条目是 uuid,不受影响)。"""
    return material_id.startswith(("transition-", "vfx-", "sticker-", "text-anim-"))


def check_resource_ids(draft: dict, tag: str) -> list[dict[str, str]]:
    """我方 FX 素材的 ``resource_id`` 必须是真实 19 位剪映资源 ID。

    背景(计划 §7.3.D):占位 resource_id 写进草稿,剪映加载不到该资源 ——
    node_11 已对贴纸 fail-closed,本检查对转场/特效/动画兜底(资源库回填
    错误、模板被改坏时 QA 必须暴露,而不是静默写入废资源)。
    ``audio_fades`` 是原生结构不是资源条目(无 resource_id),不查。
    """
    from jy_common.native_fx import is_19_digit_resource_id

    issues: list[dict[str, str]] = []
    materials = draft.get("materials") or {}
    for bucket in ("transitions", "video_effects", "stickers", "material_animations"):
        for m in materials.get(bucket) or []:
            if not isinstance(m, dict):
                continue
            material_id = str(m.get("id") or "")
            if not _is_our_fx_material(material_id):
                continue
            resource_id = m.get("resource_id")
            if resource_id is None:
                # 键缺失:测试夹具/旧形态素材;真实构造器(native_fx)总是写该键,
                # 不在此列 —— QA 抓"存在但非法"(空串/PLACEHOLDER/非 19 位)。
                continue
            if not is_19_digit_resource_id(resource_id):
                issues.append(_issue(
                    "资源ID", SEV_ERROR, tag,
                    f"{bucket}#{material_id} resource_id 非法(占位或非 19 位): {resource_id!r}",
                ))
    return issues


def _our_text_ids(draft: dict | None, prefix: str) -> list[str]:
    if not draft:
        return []
    return [
        str(m.get("id"))
        for m in ((draft.get("materials") or {}).get("texts") or [])
        if isinstance(m, dict) and str(m.get("id")).startswith(prefix)
    ]


def _bgm_segment_count(draft: dict | None) -> int:
    """非配音音轨的段数(0 = 关卡② 未加 BGM / 镜像缺失)。"""
    if not draft:
        return 0
    from nodes.node_13_adjust_volume import is_voiceover_track

    return sum(
        len(track.get("segments") or [])
        for track in (draft.get("tracks") or [])
        if isinstance(track, dict) and track.get("type") == "audio" and not is_voiceover_track(track)
    )


def _video_segment_count(draft: dict | None) -> int:
    if not draft:
        return 0
    return sum(
        len(track.get("segments") or [])
        for track in (draft.get("tracks") or [])
        if isinstance(track, dict) and track.get("type") == "video"
    )


def _category(
    status: str,
    issues: list[dict[str, str]],
    counts: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "issues": issues}
    if counts:
        out["counts"] = counts
    if note:
        out["note"] = note
    return out


def build_qa_categories(
    state: dict[str, Any],
    zh_draft: dict | None,
    en_draft: dict | None,
    deep_issues: list[dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], bool]:
    """按类别聚合 QA 结果并生成类别专属判定(阶段 6 required-feature gate)。

    Args:
        state: 工作流 state(读 voiceover_summary/tts_issue/sticker_summary
            等摘要;2026-11-12 修正:实现从不读 fx_summary,原文档字符串为
            注释失真 —— noop 治理阶段5批0)。
        zh_draft / en_draft: 两份草稿 dict(不可得时 None)。
        deep_issues: 深度 QA(draft_integrity)+ 尾链的结构化 issue,
            每条已带 ``category`` 键(节点侧写入)。

    Returns:
        ``(categories, new_issues, gate_failed)``;``new_issues`` 是类别判定
        **新产生**的 issue(如"有旁白但字幕未写入",已挂进对应类别的
        ``issues``),调用方并入事件明细与 ``join_qa_report.error_texts``;``gate_failed``
        = 任一 required 类别处于 failed/degraded。每类
        ``{"status", "issues", "counts"?, "note"?, "required"}``。
    """
    categories: dict[str, dict[str, Any]] = {key: {"status": CAT_PASSED, "issues": []} for key in CATEGORY_KEYS}
    new_issues: list[dict[str, str]] = []

    # 1) 深度/尾链 issue 按 category 归桶(severity=error 即 failed)
    for issue in deep_issues:
        cat = issue.get("category")
        if cat in categories:
            categories[cat]["issues"].append(issue)
            if issue.get("severity") == SEV_ERROR:
                categories[cat]["status"] = CAT_FAILED

    def _status_of(cat: str) -> str:
        return categories[cat]["status"]

    def _append_issue(cat: str, check: str, detail: str, *, severity: str = SEV_ERROR, draft: str = "-") -> dict[str, str]:
        issue = {"check": check, "severity": severity, "draft": draft, "detail": detail, "category": cat}
        categories[cat]["issues"].append(issue)
        new_issues.append(issue)
        if severity == SEV_ERROR or _status_of(cat) == CAT_PASSED:
            categories[cat]["status"] = CAT_FAILED if severity == SEV_ERROR else CAT_DEGRADED
        return issue

    required = resolve_required_features(state)
    en_checked = bool(state.get("draft_dir_en_branch"))

    # 2) subtitles_zh:有旁白产物却无我方中文字幕 = node_08 该写没写
    zh_texts = _our_text_ids(zh_draft, "text-zh-")
    has_narration = bool(state.get("voiceover_summary")) or bool(state.get("subtitle_segments_zh"))
    if zh_texts:
        if _status_of("subtitles_zh") == CAT_PASSED:
            categories["subtitles_zh"]["status"] = CAT_PASSED
        categories["subtitles_zh"]["counts"] = {"texts": len(zh_texts)}
    elif has_narration:
        _append_issue("subtitles_zh", "中文字幕", "有旁白产物但中文草稿无我方字幕(node_08 未写入?)")
    else:
        categories["subtitles_zh"]["status"] = CAT_NOT_APPLICABLE
        categories["subtitles_zh"]["note"] = "无旁白产物"

    # 3) subtitles_en:英文分支存在且中文侧有字幕时,英文侧必须有字幕
    en_texts = _our_text_ids(en_draft, "text-en-")
    if not en_checked:
        categories["subtitles_en"]["status"] = CAT_NOT_APPLICABLE
        categories["subtitles_en"]["note"] = "英文分支未生成"
    elif en_texts:
        categories["subtitles_en"]["counts"] = {"texts": len(en_texts)}
    elif zh_texts:
        _append_issue("subtitles_en", "英文字幕", "中文侧有字幕但英文分支无我方英文字幕(node_16a/08b 链降级?)")
    else:
        categories["subtitles_en"]["status"] = CAT_NOT_APPLICABLE
        categories["subtitles_en"]["note"] = "无中文字幕来源"

    # 4) voiceover_zh:stub 一律 degraded(不冒充成功);有摘要但轨写入 0 = failed
    vo_summary = state.get("voiceover_summary") or {}
    if not vo_summary:
        categories["voiceover_zh"]["status"] = CAT_NOT_APPLICABLE
        categories["voiceover_zh"]["note"] = "无配音产物"
    else:
        provider = str(vo_summary.get("provider") or "")
        stub_count = int(vo_summary.get("stub_count") or 0)
        written = int(vo_summary.get("written") or 0)
        failed_count = int(vo_summary.get("failed") or 0)
        categories["voiceover_zh"]["counts"] = {"provider": provider, "written": written, "stub": stub_count, "failed": failed_count}
        if stub_count > 0 or provider == "stub":
            _append_issue(
                "voiceover_zh", "中文配音",
                f"配音含静音 Stub({stub_count} 段,provider={provider or 'stub'}):不是真实语音",
                severity=SEV_WARNING,
            )
        if written == 0:
            if failed_count > 0:
                _append_issue("voiceover_zh", "中文配音", f"配音合成失败 {failed_count} 段,草稿未写入任何配音")
            # 写入 0 且无失败计数:上游无旁白,not_applicable 由 note 说明
            elif not has_narration:
                categories["voiceover_zh"]["status"] = CAT_NOT_APPLICABLE
                categories["voiceover_zh"]["note"] = "无旁白产物"

    # 5) voiceover_en:empty_text=无英文文本(n/a);其它 tts_issue=failed
    #    (2026-11-10 修复:node_17 实际写 ``"empty_text: ..."`` 带后缀串,
    #    全等比较曾把该 n/a 情形误判成 failed —— 改前缀匹配。)
    tts_issue = str(state.get("tts_issue") or "")
    if not en_checked:
        categories["voiceover_en"]["status"] = CAT_NOT_APPLICABLE
        categories["voiceover_en"]["note"] = "英文分支未生成"
    elif tts_issue.startswith("empty_text"):
        categories["voiceover_en"]["status"] = CAT_NOT_APPLICABLE
        categories["voiceover_en"]["note"] = "无英文字幕文本"
    elif tts_issue:
        _append_issue("voiceover_en", "英文配音", f"英文 TTS 失败: {tts_issue}")

    # 6) bgm:required 而无 BGM 轨 = failed(操作员未加);有轨时引用/fade 走 issue
    bgm_segments = _bgm_segment_count(zh_draft)
    categories["bgm"]["counts"] = {"bgm_segments": bgm_segments}
    if bgm_segments == 0 and _status_of("bgm") == CAT_PASSED:
        if required.get("bgm"):
            _append_issue("bgm", "BGM", "必需 BGM 但草稿无 BGM 音轨(关卡② 未添加?)")
        else:
            categories["bgm"]["status"] = CAT_NOT_APPLICABLE
            categories["bgm"]["note"] = "关卡② 未添加 BGM"

    # 7) transitions:单镜头(≤1 视频段)= not_applicable
    video_segments = _video_segment_count(zh_draft)
    categories["transitions"]["counts"] = {"video_segments": video_segments}
    if video_segments <= 1 and _status_of("transitions") == CAT_PASSED:
        categories["transitions"]["status"] = CAT_NOT_APPLICABLE
        categories["transitions"]["note"] = "单镜头无剪辑边界"

    # 8) text_animation:无我方字幕(zh+en)时 n/a;缺绑定已由尾链 issue 报
    if not zh_texts and not en_texts and _status_of("text_animation") == CAT_PASSED:
        categories["text_animation"]["status"] = CAT_NOT_APPLICABLE
        categories["text_animation"]["note"] = "无我方字幕"

    # 9) stickers:required 且占位降级 = degraded(fail-closed 未落草稿);
    #    required 且无任何贴纸 = failed;非 required 无贴纸 = n/a
    sticker_summary = state.get("sticker_summary") or {}
    degraded = int(sticker_summary.get("degraded") or 0) if isinstance(sticker_summary, dict) else 0
    sticker_count = sum(
        1 for m in ((zh_draft or {}).get("materials") or {}).get("stickers") or [] if isinstance(m, dict)
    ) + sum(
        1 for m in ((en_draft or {}).get("materials") or {}).get("stickers") or [] if isinstance(m, dict)
    )
    categories["stickers"]["counts"] = {"stickers": sticker_count, "degraded": degraded}
    if sticker_count == 0 and _status_of("stickers") == CAT_PASSED:
        if required.get("stickers"):
            if degraded > 0:
                _append_issue(
                    "stickers", "贴纸",
                    f"必需贴纸但有 {degraded} 条因 resource_id 未回填被 fail-closed 拦截"
                    "(按 sticker_template.json _reverse_engineering_steps 实机回填)",
                    severity=SEV_WARNING,
                )
            else:
                _append_issue("stickers", "贴纸", "必需贴纸但草稿无任何贴纸(无关键词匹配或 node_11 降级)")
        else:
            categories["stickers"]["status"] = CAT_NOT_APPLICABLE
            categories["stickers"]["note"] = "未要求贴纸"

    # 10) volume_fades:无 BGM 段时 n/a(fade 只由 node_13 写;配音轨豁免)
    if bgm_segments == 0 and _status_of("volume_fades") == CAT_PASSED:
        categories["volume_fades"]["status"] = CAT_NOT_APPLICABLE
        categories["volume_fades"]["note"] = "无 BGM 段"

    # 11) required 但 required 类别 still passed 的,不额外处理;
    #     未 required 的类别即使 failed 也保持 failed(展示用),gate 只看 required。
    gate_failed = any(
        required.get(key) and categories[key]["status"] in (CAT_FAILED, CAT_DEGRADED)
        for key in REQUIRED_FEATURE_KEYS
    )

    # required_features 回显进报告(创建台展示判定依据)
    for key in CATEGORY_KEYS:
        categories[key]["required"] = bool(required.get(key)) if key in REQUIRED_FEATURE_KEYS else False

    return categories, new_issues, gate_failed


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

    # 阶段 6:回读 SHA-256(创建台可指出"哪份草稿、哪个版本"的缺失;
    # hash_draft_files 对双文件各算一枚,缺文件少一键)。
    sha256: dict[str, Any] = {}
    from draft_ops.draft_lock import hash_draft_files

    if zh_report["present"] and zh_dir is not None:
        sha256["zh"] = hash_draft_files(zh_dir / "draft_content.json")
    if en_report["present"]:
        sha256["en"] = hash_draft_files(en_dir / "draft_content.json")

    # 阶段 6b:节点级草稿修改审计摘要(每节点最近一条;明细在草稿目录
    # .autovideo_draft_mutations.jsonl —— 权威落盘,零 state 侵入)。
    mutations: dict[str, Any] = {}
    from nodes._draft_io import read_draft_mutations

    for role, d in (("zh", zh_dir), ("en", en_dir)):
        if d is None:
            continue
        records = read_draft_mutations(d)
        if records:
            latest: dict[str, Any] = {}
            for rec in records:
                latest[str(rec.get("node_id"))] = {
                    "sha256_after": rec.get("sha256_after"),
                    "changed": rec.get("changed"),
                    "changed_counts": rec.get("changed_counts") or {},
                    "status": rec.get("status"),
                }
            mutations[role] = latest

    # 阶段5(2026-11-11 模糊修复):画质诊断报告(草稿侧三方对照:画布 ↔
    # material 声明 ↔ 显示口径实测 + 逐段 speed)。剪映手动导出后可携
    # ``exported_file`` 重跑 build_quality_report 补全成片侧;只读不阻断,
    # 告警不参与 passed 判定(结构性问题由上方各 check 把关)。
    quality: dict[str, Any] = {"skipped": True}
    if zh_report["present"] and zh_dir is not None:
        try:
            from draft_ops.quality_report import build_quality_report

            quality = build_quality_report(zh_dir, renderer="jianying_manual")
        except Exception as exc:  # noqa: BLE001 — 报告是增强诊断,失败不阻断交付
            quality = {"skipped": True, "error": repr(exc)}

    return {
        "skipped": False,
        "passed": not any(i["severity"] == SEV_ERROR for i in issues),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zh": {k: v for k, v in zh_report.items() if k != "draft"},
        "en": {k: v for k, v in en_report.items() if k != "draft"},
        "mirror": mirror["summary"],
        "sha256": sha256,
        "mutations": mutations,
        "quality": quality,
        "issues": issues,
    }


__all__ = [
    "CAT_DEGRADED",
    "CAT_FAILED",
    "CAT_NOT_APPLICABLE",
    "CAT_PASSED",
    "CATEGORY_KEYS",
    "CATEGORY_LABELS",
    "DUAL_FILES",
    "META_INFO_NAME",
    "REQUIRED_FEATURE_KEYS",
    "ROOT_META_NAME",
    "SEV_ERROR",
    "SEV_WARNING",
    "build_join_qa_report",
    "build_qa_categories",
    "check_audio_mirror",
    "check_blur_video_effects",
    "check_draft_dir",
    "check_dual_files",
    "check_material_paths",
    "check_meta_info",
    "check_resource_ids",
    "check_root_index",
    "check_texts",
    "check_texts_track_alignment",
    "check_video_crops",
    "check_video_material_aspect",
    "check_video_track",
    "default_required_features",
    "issue_text",
    "registered_expected_for",
    "resolve_required_features",
]

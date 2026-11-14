"""storyline 草稿 → 剪映 5.9 原生 draft_content.json 转换器(2026-11-07 导入修复)。

背景:实机验收(阶段 5 13 步清单)证实 mapper 产出的 4 键 storyline 格式
(canvas_config/materials/storyline_meta/tracks)在剪映客户端"首页可列、双击必
失败"——缺原生必填的 28 顶层键 / 45 素材桶 / 34 键 segment 结构。本模块把
storyline dict 转成与原生新建草稿字段级一致的结构。

字段来源(两层依据,常量不得凭空新增):
- **真实样本**:本机剪映 5.9.0.11632 客户端手工新建草稿落盘的
  ``draft_content.json``——视频素材 56 键 / segment 34 键 / config 24 键 /
  45 素材桶顺序均逐字取自该样本;
- **参考实现**(音频/文本无真实样本):isYangs/jianying-editor-skill(MIT)内嵌的
  pyJianYingDraft 裁剪 fork 实测可在 5.9 打开草稿,音频素材/段字段集取自其
  AudioMaterial / AudioSegment 导出;同时证明最小 platform 头(无 device_id/
  mac_address)与 tm_*/封面缺失都不阻碍打开。

私有语义(storyline_meta、shot_id/media_id/content_hash/transcript、派生
``*_us`` 键、loop/role、未落盘的 BGM/配音占位)一律进 sidecar
``autovideo_storyline_source.json``(随草稿目录被 publish/fork copytree 携带;
``.snapshots`` 排除逻辑不变)。剪映保存后会丢弃未知键,sidecar 是这些信息的
唯一存活位置。

顺序感知契约不变:人工调序后 node_07 重读 ``tracks[video].segments`` 数组顺序,
转换器保持片段相对顺序与 target_timerange 不变。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import config

#: 探测回退等告警统一走模块 logger(2026-11-11 模糊修复阶段1,不得静默)
logger = logging.getLogger(__name__)

#: 原生稿识别键:含 ``new_version`` 即视为已转换/原生,幂等直通
_NATIVE_MARKER_KEY = "new_version"

#: sidecar 文件名(与 .snapshots 同级,随 copytree 携带;剪映忽略未知文件)
NATIVE_DRAFT_SIDECAR_NAME = "autovideo_storyline_source.json"

#: uuid5 命名空间(固定字符串派生 → 转换结果确定性、可测、重发布幂等)
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "auto-video-editor:native-draft:v1")

# ---------------------------------------------------------------------------
# 原生常量(逐字取自真实样本;不要"顺手补"样本里没有的键)
# ---------------------------------------------------------------------------
#: draft_content.config 24 键(样本原值)
_CONFIG_TEMPLATE: dict[str, Any] = {
    "adjust_max_index": 1,
    "attachment_info": [],
    "combination_max_index": 1,
    "export_range": None,
    "extract_audio_last_index": 1,
    "lyrics_recognition_id": "",
    "lyrics_sync": True,
    "lyrics_taskinfo": [],
    "maintrack_adsorb": True,
    "material_save_mode": 0,
    "multi_language_current": "none",
    "multi_language_list": [],
    "multi_language_main": "none",
    "multi_language_mode": "none",
    "original_sound_last_index": 1,
    "record_audio_last_index": 1,
    "sticker_max_index": 1,
    "subtitle_keywords_config": None,
    "subtitle_recognition_id": "",
    "subtitle_sync": True,
    "subtitle_taskinfo": [],
    "system_font_list": [],
    "video_mute": False,
    "zoom_info_params": None,
}

#: 45 个素材桶,按样本原生顺序
_MATERIAL_BUCKETS: tuple[str, ...] = (
    "ai_translates", "audio_balances", "audio_effects", "audio_fades",
    "audio_track_indexes", "audios", "beats", "canvases", "chromas",
    "color_curves", "digital_humans", "drafts", "effects", "flowers",
    "green_screens", "handwrites", "hsl", "images", "log_color_wheels",
    "loudnesses", "manual_deformations", "masks", "material_animations",
    "material_colors", "multi_language_refs", "placeholders", "plugin_effects",
    "primary_color_wheels", "realtime_denoises", "shapes", "smart_crops",
    "smart_relights", "sound_channel_mappings", "speeds", "stickers",
    "tail_leaders", "text_templates", "texts", "time_marks", "transitions",
    "video_effects", "video_trackings", "videos", "vocal_beautifys",
    "vocal_separations",
)

#: keyframes 8 桶(样本原值)
_KEYFRAMES_TEMPLATE: dict[str, Any] = {
    "adjusts": [], "audios": [], "effects": [], "filters": [],
    "handwrites": [], "stickers": [], "texts": [], "videos": [],
}

#: platform / last_modified_platform —— 参考实现证明的最小集(5.9 实测可打开);
#: 真实样本另有 device_id/mac_address/os_version,属本机指纹,不写进仓库代码。
_PLATFORM_TEMPLATE: dict[str, Any] = {
    "app_id": 3704,
    "app_source": "lv",
    "app_version": "5.9.0",
    "os": "windows",
}

#: 视频 segment 的 clip 默认值(样本原值)
_CLIP_TEMPLATE: dict[str, Any] = {
    "alpha": 1.0,
    "flip": {"horizontal": False, "vertical": False},
    "rotation": 0.0,
    "scale": {"x": 1.0, "y": 1.0},
    "transform": {"x": 0.0, "y": 0.0},
}

#: 视频素材 crop 默认值 = 全幅单位正方形,四角 (0,0),(1,0),(1,1),(0,1),
#: 与剪映 5.9 原生工程实测一致。2026-11-11 修复:原 lower_right_y=0.0 为
#: 转写错误——右下角与右上角重合成退化三角形,剪映把三角形拉伸铺满画布,
#: 成片右侧出现横向拉伸条纹(证据 outputs/*/baseline/ab_test_result.md)。
_CROP_TEMPLATE: dict[str, Any] = {
    "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
    "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
    "upper_right_x": 1.0, "upper_right_y": 0.0,
}

#: 视频素材 matting / stable / video_algorithm 默认值(样本原值)
_MATTING_TEMPLATE: dict[str, Any] = {
    "flag": 0, "has_use_quick_brush": False, "has_use_quick_eraser": False,
    "interactiveTime": [], "path": "", "strokes": [],
}
_STABLE_TEMPLATE: dict[str, Any] = {
    "matrix_path": "", "stable_level": 0, "time_range": {"duration": 0, "start": 0},
}
_VIDEO_ALGORITHM_TEMPLATE: dict[str, Any] = {
    "algorithms": [], "complement_frame_config": None, "deflicker": None,
    "gameplay_configs": [], "motion_blur_config": None, "noise_reduction": None,
    "path": "", "quality_enhance": None, "time_range": None,
}

#: segment 公共默认(视频/音频/effect 通用,样本原值)
_HDR_VIDEO: dict[str, Any] = {"intensity": 1.0, "mode": 1, "nits": 1000}
_UNIFORM_SCALE_ON: dict[str, Any] = {"on": True, "value": 1.0}
_RESPONSIVE_LAYOUT: dict[str, Any] = {
    "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
    "target_follow": "", "vertical_pos_layout": 0,
}

#: 音频素材模板字段集(参考实现 AudioMaterial.export_json;µs 时长)
_AUDIO_MATERIAL_TYPE = "extract_music"


def is_native_draft(draft: Any) -> bool:
    """输入是否已是原生(或已转换)形态——含 ``new_version`` 即幂等直通。"""
    return isinstance(draft, dict) and _NATIVE_MARKER_KEY in draft


def _new_id(old: Any, domain: str) -> str:
    """确定性 id 再生:uuid5(固定 ns, "domain:old") 大写带连字符。"""
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{domain}:{old}")).upper()


def _probe_size(path: str) -> tuple[int, int]:
    """素材宽高 best-effort:取**显示口径**(coded+旋转元数据→display)。

    2026-11-11 模糊修复阶段1(设计文档 §4.2):剪映按 material 声明尺寸参与
    布局,声明必须与解码后显示方向一致——原 coded 口径会把 iPhone 竖拍
    (coded 1920×1080 + rotation -90)误声明成横屏。失败不再静默伪装,
    统一 logger.warning 留痕后回退画布尺寸。
    """
    canvas = (int(config.CANVAS_WIDTH), int(config.CANVAS_HEIGHT))
    if not path or not Path(path).is_file():
        return canvas
    try:
        from video_edit_capabilities.render import probe_display_size

        w, h = probe_display_size(Path(path))
        if not w or not h or w <= 0 or h <= 0:
            logger.warning(
                "native_draft display probe invalid for %r (w=%r h=%r), fallback to canvas %r",
                path, w, h, canvas,
            )
            return canvas
        return int(w), int(h)
    except Exception as exc:  # noqa: BLE001 — 探测失败回退画布,不阻断转换
        logger.warning(
            "native_draft display probe failed for %r: %r — fallback to canvas %r",
            path, exc, canvas,
        )
        return canvas


def _to_path(raw: Any) -> str:
    """音频 ref 可能是 file:// URI(视频路径 mapper 已转)→ 统一转本地路径。"""
    s = str(raw or "")
    if s.startswith("file://"):
        from storyline.mapper import file_uri_to_path

        return file_uri_to_path(s)
    return s


# ---------------------------------------------------------------------------
# 素材转换
# ---------------------------------------------------------------------------
def _convert_video_material(v: dict[str, Any], new_id: str) -> dict[str, Any]:
    """mapper/legacy 视频素材 → 原生 56 键条目(样本默认值 + 输入实值)。"""
    path = str(v.get("path") or "")
    width, height = _probe_size(path)
    name = v.get("material_name") or (Path(path).name if path else new_id)
    return {
        "aigc_type": "none",
        "audio_fade": None,
        "cartoon_path": "",
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "crop": dict(_CROP_TEMPLATE),
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "duration": int(v.get("duration") or 0),
        "extra_type_option": 0,
        "formula_id": "",
        "freeze": None,
        # 素材默认带音轨(混剪/e2e 素材音调可区分);无音轨素材剪映容错
        "has_audio": True,
        "height": height,
        "id": new_id,
        "intensifies_audio_path": "",
        "intensifies_path": "",
        "is_ai_generate_content": False,
        "is_copyright": False,
        "is_text_edit_overdub": False,
        "is_unified_beauty_mode": False,
        "local_id": "",
        "local_material_id": new_id.lower(),
        "material_id": "",
        "material_name": name,
        "material_url": "",
        "matting": dict(_MATTING_TEMPLATE),
        "media_path": "",
        "object_locked": None,
        "origin_material_id": "",
        "path": path,
        "picture_from": "none",
        "picture_set_category_id": "",
        "picture_set_category_name": "",
        "request_id": "",
        "reverse_intensifies_path": "",
        "reverse_path": "",
        "smart_motion": None,
        "source": 0,
        "source_platform": 0,
        "stable": dict(_STABLE_TEMPLATE),
        "team_id": "",
        "type": "video",
        "video_algorithm": dict(_VIDEO_ALGORITHM_TEMPLATE),
        "width": width,
    }


def _convert_audio_material(
    a: dict[str, Any], new_id: str, *, path: str, duration_us: int
) -> dict[str, Any]:
    """mapper 音频素材 {id,type,ref,role} → 参考实现原生模板(无真实样本)。"""
    return {
        "app_id": 0,
        "category_id": "",
        "category_name": "local",
        "check_flag": 3,
        "copyright_limit_type": "none",
        "duration": int(duration_us),
        "effect_id": "",
        "formula_id": "",
        "id": new_id,
        "local_material_id": new_id.lower(),
        "music_id": new_id.lower(),
        "name": Path(path).name if path else new_id,
        "path": path,
        "source_platform": 0,
        "type": _AUDIO_MATERIAL_TYPE,
        "wave_points": [],
    }


# ---------------------------------------------------------------------------
# segment / track 转换
# ---------------------------------------------------------------------------
def _base_segment_keys(new_id: str, material_id: str, *, render_index: int) -> dict[str, Any]:
    """34 键 segment 的公共部分(样本原值;视频/音频差异键由调用方覆写)。"""
    return {
        "caption_info": None,
        "cartoon": False,
        "clip": dict(_CLIP_TEMPLATE),
        "common_keyframes": [],
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "extra_material_refs": [],
        "group_id": "",
        "hdr_settings": dict(_HDR_VIDEO),
        "id": new_id,
        "intensifies_audio": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "keyframe_refs": [],
        "last_nonzero_volume": 1.0,
        "material_id": material_id,
        "render_index": render_index,
        "responsive_layout": dict(_RESPONSIVE_LAYOUT),
        "reverse": False,
        "source_timerange": None,
        "speed": 1.0,
        "target_timerange": None,
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": dict(_UNIFORM_SCALE_ON),
        "visible": True,
        "volume": 1.0,
    }


def _convert_video_segment(
    seg: dict[str, Any], new_id: str, material_id: str, companion_ids: list[str]
) -> dict[str, Any]:
    """storyline 视频 segment → 原生 34 键;timerange 保序保值(µs)。"""
    out = _base_segment_keys(new_id, material_id, render_index=0)
    out["extra_material_refs"] = list(companion_ids)
    out["target_timerange"] = {
        "start": int((seg.get("target_timerange") or {}).get("start") or 0),
        "duration": int((seg.get("target_timerange") or {}).get("duration") or 0),
    }
    out["source_timerange"] = {
        "start": int((seg.get("source_timerange") or {}).get("start") or 0),
        "duration": int((seg.get("source_timerange") or {}).get("duration") or 0),
    }
    out["speed"] = float(seg.get("speed") or 1.0)
    return out


def _convert_audio_segment(
    seg: dict[str, Any], new_id: str, material_id: str, speed_id: str,
    *, duration_us: int,
) -> dict[str, Any]:
    """storyline 音频段(target_start_us/end_us 派生键)→ 原生 target_timerange。

    音频段与视频段的样本差异键:clip/hdr_settings/uniform_scale 置 None
    (与样本 effect segment 的空形态一致;参考实现音频段同为 null)。
    """
    out = _base_segment_keys(new_id, material_id, render_index=0)
    out["clip"] = None
    out["hdr_settings"] = None
    out["uniform_scale"] = None
    out["extra_material_refs"] = [speed_id]
    out["target_timerange"] = {
        "start": int(seg.get("target_start_us") or 0),
        "duration": int(duration_us),
    }
    out["source_timerange"] = {"start": 0, "duration": int(duration_us)}
    out["speed"] = float(seg.get("speed") or 1.0)
    return out


# ---------------------------------------------------------------------------
# 公共构造助手(阶段 2,2026-11-09):node_08 配音轨重写复用原生形状
# ---------------------------------------------------------------------------
def build_native_audio_material(
    *, id_seed: str, path: str, duration_us: int
) -> dict[str, Any]:
    """构造原生 audio material(17 键 extract_music 形,id 由 uuid5 确定性生成)。

    与 mapper→native 转换路径产出同一 schema;``id_seed`` 稳定(如
    ``"zh-voiceover:voiceover_0001"``)则重放幂等。
    """
    new_id = _new_id(id_seed, "audio_material")
    return _convert_audio_material(
        {"id": id_seed, "type": "audio", "ref": path},
        new_id,
        path=path,
        duration_us=max(0, int(duration_us)),
    )


def build_native_audio_segment(
    *, id_seed: str, material_id: str, start_us: int, duration_us: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造原生音频段(34 键,clip/hdr/uniform_scale=None)+ 其 speed 伴随素材。

    返回 ``(segment, companion_materials)``;调用方须把伴随素材并入
    ``materials.speeds``(与转换路径同口径,避免悬空 extra_material_refs)。
    """
    seg_id = _new_id(id_seed, "audio_segment")
    speed_id = _new_id(seg_id, "audio_speed")
    seg = _convert_audio_segment(
        {"target_start_us": int(start_us), "speed": 1.0},
        seg_id,
        material_id,
        speed_id,
        duration_us=max(0, int(duration_us)),
    )
    companion = {
        "curve_speed": None,
        "id": speed_id,
        "mode": 0,
        "speed": 1.0,
        "type": "speed",
    }
    return seg, [companion]


def _companion_materials(
    seg: dict[str, Any], seg_id: str, *, speed: float
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """一段四伴随(样本形态:speeds/canvases/sound_channel_mappings/vocal_separations)。"""
    speed_id = _new_id(seg_id, "speed")
    canvas_id = _new_id(seg_id, "canvas")
    scm_id = _new_id(seg_id, "scm")
    vocal_id = _new_id(seg_id, "vocal")
    mats: dict[str, list[dict[str, Any]]] = {
        "speeds": [
            {"curve_speed": None, "id": speed_id, "mode": 0, "speed": float(speed), "type": "speed"}
        ],
        "canvases": [
            {
                "album_image": "", "blur": 0.0, "color": "", "id": canvas_id,
                "image": "", "image_id": "", "image_name": "", "source_platform": 0,
                "team_id": "", "type": "canvas_color",
            }
        ],
        "sound_channel_mappings": [
            {"audio_channel_mapping": 0, "id": scm_id, "is_config_open": False, "type": ""}
        ],
        "vocal_separations": [
            {"choice": 0, "id": vocal_id, "production_path": "", "time_range": None,
             "type": "vocal_separation"}
        ],
    }
    return mats, [speed_id, canvas_id, scm_id, vocal_id]


def _new_track(old_track: dict[str, Any], new_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    """mapper 轨 {type,id,segments} → 原生 7 键轨(样本形态)。"""
    return {
        "attribute": 0,
        "flag": 0,
        "id": new_id,
        "is_default_name": True,
        "name": "",
        "segments": segments,
        "type": str(old_track.get("type") or "video"),
    }


# ---------------------------------------------------------------------------
# 剪映云音乐素材/轨道构造器(20261114 阶段4,修订 §4.4/§4.5)
# 形状依据 = 阶段0黄金样本盘点(AutoVideo_console-50cd4c6af941):
#   37 键 type="music" 素材 + 7 键 audio 轨 + speed/SCM/vocal 伴随;
#   fade 交给 node_13,beats 按 D5 省略(见执行记录 §1.2)。
# ---------------------------------------------------------------------------
def music_library_track_id(id_seed: str) -> str:
    """云音乐自动轨的确定性 track id(uuid5;幂等重放的识别键)。"""
    return _new_id(id_seed, "music_library_track")


def build_native_music_library_material(
    *, id_seed: str, raw_material: dict[str, Any], cache_path: str
) -> dict[str, Any]:
    """由收割的原始云音乐素材构造原生 materials.audios 素材(fail-closed)。

    - deepcopy ``raw_material`` 保留全部原始字段(music_id/request_id/
      category_id/duration 原样,type 保持 "music" 不改 extract_music);
    - 仅改草稿内 ``id``(uuid5 确定性)与 ``path``(当前设备缓存路径);
    - ``music_id`` 非空、缓存文件存在且非空、duration>0 三项校验,缺失即拒构造。
    """
    music_id = str(raw_material.get("music_id") or "").strip()
    if not music_id:
        raise ValueError("raw_material 缺 music_id,拒绝构造云音乐素材")
    duration_us = int(raw_material.get("duration") or 0)
    if duration_us <= 0:
        raise ValueError(f"raw_material duration 非法({duration_us}),拒绝构造云音乐素材")
    if not cache_path or not Path(cache_path).is_file() or Path(cache_path).stat().st_size <= 0:
        raise ValueError(f"缓存文件不可用({cache_path!r}),拒绝构造云音乐素材")

    material = json.loads(json.dumps(raw_material))  # deepcopy(json 安全子集)
    material["id"] = _new_id(id_seed, "music_library_material")
    material["path"] = str(cache_path)
    return material


def build_native_sound_channel_mapping_material(
    *, material_id: str, channel_type: str = "none"
) -> dict[str, Any]:
    """4 键 SCM 伴随素材(黄金样本形态,type 默认 "none")。"""
    return {
        "audio_channel_mapping": 0,
        "id": material_id,
        "is_config_open": False,
        "type": channel_type,
    }


def build_native_vocal_separation_material(*, material_id: str) -> dict[str, Any]:
    """5 键 vocal_separation 伴随素材(与 _companion_materials 逐键一致)。"""
    return {
        "choice": 0,
        "id": material_id,
        "production_path": "",
        "time_range": None,
        "type": "vocal_separation",
    }


def build_native_music_library_track(
    *, id_seed: str, segments: list[dict[str, Any]]
) -> dict[str, Any]:
    """7 键原生 audio 轨,name="BGM"(node_13/_bgm_segment_count/镜像的非配音分类口径)。"""
    track = _new_track({"type": "audio"}, music_library_track_id(id_seed), segments)
    track["name"] = "BGM"
    track["is_default_name"] = False
    return track


# ---------------------------------------------------------------------------
# 主转换
# ---------------------------------------------------------------------------
def storyline_to_native(draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """storyline(4 键)dict → (原生 draft dict, sidecar 或 None)。

    已是原生形态(:func:`is_native_draft`)时幂等直通 ``(draft, None)``。
    转换保持视频片段相对顺序与 target_timerange 不变;BGM/配音占位
    (ref 解析不到本地文件)与 mapper 文本轨丢弃进 sidecar。
    """
    if is_native_draft(draft):
        return draft, None

    from draft_ops.jianying_registry import compute_draft_duration_us

    id_map: dict[str, dict[str, str]] = {"material": {}, "segment": {}, "track": {}}
    src_materials = draft.get("materials") if isinstance(draft.get("materials"), dict) else {}
    src_tracks = [t for t in (draft.get("tracks") or []) if isinstance(t, dict)]

    materials = {name: [] for name in _MATERIAL_BUCKETS}
    dropped: dict[str, Any] = {}

    # 1. 视频素材:id 再生 + 原生 56 键
    for v in (src_materials.get("videos") or []):
        if not isinstance(v, dict) or not v.get("id"):
            continue
        old_id = str(v["id"])
        new_id = _new_id(old_id, "video_material")
        id_map["material"][old_id] = new_id
        materials["videos"].append(_convert_video_material(v, new_id))

    # 2. 音频素材:ref 解析到本地文件才转;解析不到(BGM/配音资源 ID 占位)进 sidecar
    audio_segments_by_material: dict[str, list[dict[str, Any]]] = {}
    audio_track = next((t for t in src_tracks if t.get("type") == "audio"), None)
    for seg in ((audio_track or {}).get("segments") or []):
        if isinstance(seg, dict) and seg.get("material_id"):
            audio_segments_by_material.setdefault(str(seg["material_id"]), []).append(seg)

    unresolved_audio: dict[str, str] = {}
    for a in (src_materials.get("audios") or []):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        old_id = str(a["id"])
        path = _to_path(a.get("ref") or a.get("path"))
        segs = audio_segments_by_material.get(old_id, [])
        if not path or not Path(path).is_file():
            unresolved_audio[old_id] = str(a.get("ref") or "")
            continue  # 段的丢弃统一由轨道循环完成(避免重复登记)
        span = 0
        for seg in segs:
            try:
                span = max(
                    span,
                    int(seg.get("target_end_us") or 0) - int(seg.get("target_start_us") or 0),
                )
            except (TypeError, ValueError):
                continue
        new_id = _new_id(old_id, "audio_material")
        id_map["material"][old_id] = new_id
        materials["audios"].append(
            _convert_audio_material(a, new_id, path=path, duration_us=max(span, 0))
        )

    # 3. 轨道:视频轨逐段转换(保序);音频段仅对已落盘素材转换
    native_tracks: list[dict[str, Any]] = []
    for track in src_tracks:
        ttype = str(track.get("type") or "")
        if ttype == "text":
            # 阶段 1(2026-11-09 效果缺失修复):mapper 已不再产出内部 text 轨,
            # 中文字幕由 node_08 以原生形状写(关卡①后,时间取自当前草稿)。
            # 此分支仅防御旧 checkpoint 残留的 mapper 私有形状文本轨:进 sidecar
            # 留档(不静默消失),node_05 对 dropped 非空会打告警日志。
            dropped["dropped_text_track"] = track
            continue
        old_track_id = str(track.get("id") or ttype)
        new_track_id = _new_id(old_track_id, f"track_{ttype}")
        id_map["track"][old_track_id] = new_track_id
        native_segments: list[dict[str, Any]] = []
        for seg in (track.get("segments") or []):
            if not isinstance(seg, dict) or not seg.get("id"):
                continue
            old_seg_id = str(seg["id"])
            new_seg_id = _new_id(old_seg_id, f"segment_{ttype}")
            old_mat_id = str(seg.get("material_id") or "")
            new_mat_id = id_map["material"].get(old_mat_id, "")
            if ttype == "video" and new_mat_id:
                id_map["segment"][old_seg_id] = new_seg_id
                companions, companion_ids = _companion_materials(
                    seg, old_seg_id, speed=float(seg.get("speed") or 1.0)
                )
                for bucket, entries in companions.items():
                    materials[bucket].extend(entries)
                native_segments.append(
                    _convert_video_segment(seg, new_seg_id, new_mat_id, companion_ids)
                )
            elif ttype == "audio" and new_mat_id:
                id_map["segment"][old_seg_id] = new_seg_id
                speed_id = _new_id(old_seg_id, "audio_speed")
                materials["speeds"].append(
                    {"curve_speed": None, "id": speed_id, "mode": 0,
                     "speed": float(seg.get("speed") or 1.0), "type": "speed"}
                )
                duration_us = int(
                    (seg.get("target_end_us") or 0)
                ) - int(seg.get("target_start_us") or 0)
                native_segments.append(
                    _convert_audio_segment(
                        seg, new_seg_id, new_mat_id, speed_id,
                        duration_us=max(duration_us, 0),
                    )
                )
            else:
                # 引用未落盘素材(BGM/配音占位)的段:丢弃进 sidecar
                dropped.setdefault("dropped_audio_segments", []).append(seg)
                continue
        if native_segments or ttype == "video":
            native_tracks.append(_new_track(track, new_track_id, native_segments))

    # 4. 输入里非空的非原生桶(理论为空,防御性收进 sidecar)
    for bucket, entries in src_materials.items():
        if bucket in materials or not isinstance(entries, list) or not entries:
            continue
        dropped.setdefault("dropped_material_buckets", {})[bucket] = entries

    canvas = draft.get("canvas_config") if isinstance(draft.get("canvas_config"), dict) else {}
    native: dict[str, Any] = {
        "canvas_config": {
            "width": int(canvas.get("width") or config.CANVAS_WIDTH),
            "height": int(canvas.get("height") or config.CANVAS_HEIGHT),
            "ratio": "original",
        },
        "color_space": 0,
        "config": dict(_CONFIG_TEMPLATE),
        "cover": None,
        "create_time": 0,
        "duration": 0,
        "extra_info": None,
        "fps": 30.0,
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": _new_id("draft_root", "draft"),
        "keyframe_graph_list": [],
        "keyframes": dict(_KEYFRAMES_TEMPLATE),
        "last_modified_platform": dict(_PLATFORM_TEMPLATE),
        "materials": materials,
        "mutable_config": None,
        "name": "",
        "new_version": "110.0.0",
        "platform": dict(_PLATFORM_TEMPLATE),
        "relationships": [],
        "render_index_track_mode_on": True,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": native_tracks,
        "update_time": 0,
        "version": 360000,
    }
    native["duration"] = int(compute_draft_duration_us(native) or 0)

    sidecar: dict[str, Any] | None = None
    if draft or dropped:
        if unresolved_audio:
            dropped["unresolved_audio_refs"] = unresolved_audio
        sidecar = {
            "schema": "autovideo_storyline_source/1",
            "id_map": id_map,
            "dropped": dropped,
            "source_draft": draft,
        }
    return native, sidecar


# ---------------------------------------------------------------------------
# sidecar 落盘(同盘 mkstemp + os.replace 原子写;模式对齐 jianying_registry)
# ---------------------------------------------------------------------------
def write_sidecar(draft_dir: Path, sidecar: dict[str, Any] | None) -> str | None:
    """把转换 sidecar 写进草稿目录;``None``/空跳过。返回写入路径或 None。"""
    if not sidecar:
        return None
    target = Path(draft_dir) / NATIVE_DRAFT_SIDECAR_NAME
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".nativedraft_", suffix=".json")
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(sidecar, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return str(target)


__all__ = [
    "NATIVE_DRAFT_SIDECAR_NAME",
    "is_native_draft",
    "storyline_to_native",
    "write_sidecar",
]

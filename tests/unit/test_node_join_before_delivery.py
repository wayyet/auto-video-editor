"""join_before_delivery 单测 — 3 种 QA 场景 + 阶段 4 深度 QA。"""

from __future__ import annotations

import json
from pathlib import Path

from nodes.node_join_before_delivery import join_before_delivery


def _make_cover(ratio: str, en_path: str | None) -> dict:
    return {"ratio": ratio, "zh_path": f"/path/{ratio}_zh.png", "en_path": en_path}


def test_join_happy_path_no_issues() -> None:
    """3 张封面(含 en_path)+ 英文字幕 → 0 issues。"""
    state = {
        "covers": [
            _make_cover("9:16", "/p/9x16_en.png"),
            _make_cover("16:9", "/p/16x9_en.png"),
            _make_cover("4:3", "/p/4x3_en.png"),
        ],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert out["join_qa_report"]["error_texts"] == []
    assert "join_before_delivery_done" in out["status_log"]
    # 阶段 4:结构化报告存在,且"无英文分支"场景记 skipped(上游降级,不当缺陷)
    assert out["join_qa_report"]["skipped"] is True
    assert out["join_qa_report"]["passed"] is True


def test_join_missing_en_cover() -> None:
    """某张封面缺 en_path → issues 含 "封面英文版缺失"。"""
    state = {
        "covers": [
            _make_cover("9:16", "/p/9x16_en.png"),
            _make_cover("16:9", None),  # 缺
            _make_cover("4:3", "/p/4x3_en.png"),
        ],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("封面英文版缺失" in i and "16:9" in i for i in out["join_qa_report"]["error_texts"])
    # error_log 也应有对应条目
    assert any("封面英文版缺失" in e for e in out["error_log"])


def test_join_missing_all_covers() -> None:
    """covers 为空 → issues 含 "封面缺失:需3种比例"。"""
    state = {
        "covers": [],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("封面缺失" in i for i in out["join_qa_report"]["error_texts"])


def test_join_missing_subtitle() -> None:
    """英文字幕文件缺失 → issues 含 "英文字幕文件缺失"。"""
    state = {
        "covers": [
            _make_cover("9:16", "/p/9x16_en.png"),
            _make_cover("16:9", "/p/16x9_en.png"),
            _make_cover("4:3", "/p/4x3_en.png"),
        ],
        "subtitle_srt_path": None,
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert any("英文字幕文件缺失" in i for i in out["join_qa_report"]["error_texts"])


def test_join_preserves_existing_state() -> None:
    """汇合节点只返回 delta;上游字段(如 draft_path)由 LangGraph 自动保留。"""
    state = {
        "covers": [_make_cover("9:16", "/p/_en.png")],
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "status_log": ["prev"],
        "error_log": ["prev_err"],
        "draft_path": "/x/y.json",
    }
    out = join_before_delivery(state)
    # Week 4:delta-only 返回;LangGraph 运行时只更新返回的 keys
    assert "join_before_delivery_done" in out["status_log"]
    assert "join_qa_report" in out
    assert "error_texts" in out["join_qa_report"]
    # 注:delta 返回不含 draft_path 是预期的;LangGraph 合并时保留
    assert "draft_path" not in out  # 验证是 delta-only


# ---------------------------------------------------------------------------
# K2=B:英文草稿尾链四项检查(+ 阶段 4 扩到中文侧)
# ---------------------------------------------------------------------------
def _ok_covers() -> list[dict]:
    return [
        _make_cover("9:16", "/p/9x16_en.png"),
        _make_cover("16:9", "/p/16x9_en.png"),
        _make_cover("4:3", "/p/4x3_en.png"),
    ]


def _healthy_draft(dir_path: Path, *, audio_volume: float | None = 0.35) -> dict:
    """构造一份能通过阶段 4 深度 QA 的完整草稿(素材用真实临时文件)。

    返回 draft dict 并把双文件写进 ``dir_path``(目录自动创建)。
    """
    src = dir_path.parent / f"src_{dir_path.name}.mp4"
    src.write_bytes(b"\x00\x00\x00")  # 只需存在;深度 QA 只查 is_file
    bgm = dir_path.parent / f"bgm_{dir_path.name}.mp3"
    bgm.write_bytes(b"\x00\x00\x00")
    audio_material: dict = {"id": "audio-bgm-1", "type": "audio", "path": str(bgm)}
    if audio_volume is not None:
        audio_material["volume"] = audio_volume
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 35_000_000,
        "materials": {
            "videos": [{"id": "video-1", "type": "video", "path": str(src)}],
            "audios": [audio_material],
            "transitions": [{"id": "transition-1"}],
            "video_effects": [{"id": "vfx-default"}],
            "stickers": [{"id": "sticker-t1"}],
            "audio_fades": [{"id": "audio-fade-audio-bgm-1", "type": "audio_fade"}],
            "texts": [
                {
                    "id": "text-1",
                    "content": "你好世界",
                    "target_timerange": {"start": 0, "duration": 1_000_000},
                }
            ],
        },
        "tracks": [
            {
                "type": "video",
                "id": "track-video-1",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "source_timerange": {"start": 0, "duration": 1_000_000},
                        "target_timerange": {"start": 0, "duration": 1_000_000},
                        "extra_material_refs": ["sticker-t1"],
                    }
                ],
            },
            {
                "type": "audio",
                "id": "track-audio-1",
                "segments": [{
                    "id": "audio-bgm-1",
                    "material_id": "audio-bgm-1",
                    "extra_material_refs": ["audio-fade-audio-bgm-1"],
                }],
            },
        ],
    }
    dir_path.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(draft, ensure_ascii=False)
    (dir_path / "draft_content.json").write_text(serialized, encoding="utf-8")
    (dir_path / "draft_info.json").write_text(serialized, encoding="utf-8")
    return draft


def _state_with_drafts(tmp_path: Path, **overrides) -> dict:
    """健康 zh + en 双草稿场景的基准 state(可按需覆盖字段)。"""
    zh_dir = tmp_path / "zh"
    en_dir = tmp_path / "en_branch"
    _healthy_draft(zh_dir)
    _healthy_draft(en_dir)
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "status_log": [],
        "error_log": [],
    }
    state.update(overrides)
    return state


def test_join_en_draft_complete_no_issues(tmp_path: Path) -> None:
    """中英双草稿尾链齐全 + 深度 QA 全过 → 无 issue,报告 passed。"""
    state = _state_with_drafts(tmp_path)
    out = join_before_delivery(state)
    assert out["join_qa_report"]["error_texts"] == [], out["join_qa_report"]["error_texts"]
    report = out["join_qa_report"]
    assert report["skipped"] is False
    assert report["passed"] is True
    for name in ("dual_files", "video_track", "track_references", "material_paths", "texts"):
        assert name in report["zh"]["checks"], report["zh"]["checks"]
        assert name in report["en"]["checks"], report["en"]["checks"]
    assert report["mirror"]["matched"] is True
    assert "delivery_qa_ok" in out["status_log"]


def test_join_en_draft_missing_tail_chain_items(tmp_path: Path) -> None:
    """英文草稿转场/特效/贴纸全空 → 报 3 条 issue。"""
    en_dir = tmp_path / "en_branch"
    _healthy_draft(en_dir)
    # 覆写成尾链全空的英文草稿(深度 QA 会另报视频轨为空,不影响本断言)
    bare = json.dumps({"materials": {}, "tracks": []}, ensure_ascii=False)
    (en_dir / "draft_content.json").write_text(bare, encoding="utf-8")
    (en_dir / "draft_info.json").write_text(bare, encoding="utf-8")
    zh_dir = tmp_path / "zh"
    _healthy_draft(zh_dir)
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    joined = " ".join(out["join_qa_report"]["error_texts"])
    assert "英文草稿转场缺失" in joined
    assert "英文草稿视频特效缺失" in joined
    assert "英文草稿贴纸缺失" in joined


def test_join_en_draft_bgm_without_fade_binding(tmp_path: Path) -> None:
    """阶段 5 口径:英文草稿有 BGM 轨但没绑淡入淡出 → 报"BGM 淡入淡出未绑定"。"""
    state = _state_with_drafts(tmp_path)
    en_dir = Path(state["draft_dir_en_branch"])
    draft = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["tracks"][-1]["segments"][0].pop("extra_material_refs")
    serialized = json.dumps(draft, ensure_ascii=False)
    (en_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (en_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert any("BGM 淡入淡出未绑定" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_voiceover_track_exempt_from_fade_check(tmp_path: Path) -> None:
    """阶段 5 口径:配音轨(按轨名)不算 BGM,没有 fade 绑定也不报 issue。"""
    state = _state_with_drafts(tmp_path)
    zh_dir = Path(state["draft_path"]).parent
    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    # 把 BGM 轨换成配音轨(轨名标记)并摘掉 fade refs
    draft["tracks"][-1]["name"] = "Voiceover_zh"
    draft["tracks"][-1]["segments"][0].pop("extra_material_refs")
    serialized = json.dumps(draft, ensure_ascii=False)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert not any("BGM 淡入淡出未绑定" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_sticker_unbound_reported(tmp_path: Path) -> None:
    """阶段 4:贴纸 material 存在但没有任何视频段引用 → 报"贴纸未挂到任何视频段"。"""
    state = _state_with_drafts(tmp_path)
    zh_dir = Path(state["draft_path"]).parent
    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["tracks"][0]["segments"][0]["extra_material_refs"] = []  # 摘掉贴纸引用
    serialized = json.dumps(draft, ensure_ascii=False)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert any("贴纸未挂到任何视频段" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_zh_text_missing_animation_reported(tmp_path: Path) -> None:
    """阶段 4:我方字幕(text-zh-*)没有入场动画绑定 → 报"缺入场动画绑定"。"""
    state = _state_with_drafts(tmp_path)
    zh_dir = Path(state["draft_path"]).parent
    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["materials"]["texts"].append(
        {"id": "text-zh-1", "type": "text", "content": "你好", "target_timerange": {"start": 0, "duration": 1_000_000}}
    )
    draft["tracks"].append(
        {
            "type": "text",
            "id": "track-text-1",
            "segments": [{"id": "text-seg-1", "material_id": "text-zh-1",
                          "target_timerange": {"start": 0, "duration": 1_000_000}}],
        }
    )
    serialized = json.dumps(draft, ensure_ascii=False)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert any("缺入场动画绑定" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_zh_text_with_animation_no_issue(tmp_path: Path) -> None:
    """阶段 4:我方字幕有 material_animations 绑定 → 不报动画 issue。"""
    state = _state_with_drafts(tmp_path)
    zh_dir = Path(state["draft_path"]).parent
    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["materials"]["texts"].append(
        {"id": "text-zh-1", "type": "text", "content": "你好", "target_timerange": {"start": 0, "duration": 1_000_000}}
    )
    draft["materials"]["material_animations"] = [{"id": "text-anim-text-zh-1", "type": "sticker_animation"}]
    draft["tracks"].append(
        {
            "type": "text",
            "id": "track-text-1",
            "segments": [{"id": "text-seg-1", "material_id": "text-zh-1",
                          "target_timerange": {"start": 0, "duration": 1_000_000},
                          "extra_material_refs": ["text-anim-text-zh-1"]}],
        }
    )
    serialized = json.dumps(draft, ensure_ascii=False)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert not any("缺入场动画绑定" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_en_draft_missing_file_is_reported(tmp_path: Path) -> None:
    """英文草稿目录指向不存在的路径 → 报"英文分支草稿缺失"。"""
    state = _state_with_drafts(tmp_path, draft_dir_en_branch=str(tmp_path / "nope"))
    out = join_before_delivery(state)
    assert any("英文分支草稿缺失" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_skips_en_checks_when_no_en_branch() -> None:
    """从未走英文分支(draft_dir_en_branch 为空)→ 整段跳过,不产生 issues。"""
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": "/x/y.json",
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    assert out["join_qa_report"]["error_texts"] == []


# ---------------------------------------------------------------------------
# 阶段 4:深度 QA(中文侧 / 素材路径 / 根索引)
# ---------------------------------------------------------------------------
def test_join_zh_draft_file_missing_reported(tmp_path: Path) -> None:
    """中文草稿文件缺失(draft_path 指向不存在目录)→ 中文侧 issue + QA 失败。"""
    state = _state_with_drafts(
        tmp_path, draft_path=str(tmp_path / "ghost" / "draft_content.json")
    )
    out = join_before_delivery(state)
    joined = " ".join(out["join_qa_report"]["error_texts"])
    assert "中文主草稿不可读" in joined
    assert out["join_qa_report"]["passed"] is False
    assert any(i["draft"] == "zh" for i in out["join_qa_report"]["issues"])


def test_join_dual_files_mismatch_reported(tmp_path: Path) -> None:
    """draft_content.json 与 draft_info.json 内容不一致 → 深度 QA 报错。"""
    state = _state_with_drafts(tmp_path)
    en_dir = Path(state["draft_dir_en_branch"])
    info = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    info["duration"] = 1
    (en_dir / "draft_info.json").write_text(json.dumps(info), encoding="utf-8")
    out = join_before_delivery(state)
    assert any("双文件" in i and "不一致" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]
    assert out["join_qa_report"]["passed"] is False


def test_join_missing_material_file_fails_qa(tmp_path: Path) -> None:
    """素材路径指向不存在的文件 → QA 明确失败,不报告假成功(计划阶段 4 验收)。"""
    state = _state_with_drafts(tmp_path)
    zh_dir = Path(state["draft_path"]).parent
    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["materials"]["videos"][0]["path"] = str(tmp_path / "ghost.mp4")
    serialized = json.dumps(draft, ensure_ascii=False)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert any("素材路径" in i and "ghost.mp4" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]
    assert out["join_qa_report"]["passed"] is False


def test_join_broken_mirror_reported(tmp_path: Path) -> None:
    """英文侧丢掉中文音频轨段 → 镜像检查报错(关卡② 镜像不完整)。"""
    state = _state_with_drafts(tmp_path)
    en_dir = Path(state["draft_dir_en_branch"])
    draft = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    draft["tracks"] = [t for t in draft["tracks"] if t.get("type") != "audio"]
    serialized = json.dumps(draft, ensure_ascii=False)
    (en_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (en_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert any("BGM镜像" in i for i in out["join_qa_report"]["error_texts"]), out["join_qa_report"]["error_texts"]


def test_join_registered_draft_missing_root_index(monkeypatch, tmp_path: Path) -> None:
    """发布开启且草稿在剪映草稿库根下但未注册进根索引 → QA 明确失败。"""
    jy_root = tmp_path / "jianying_root"
    state = _state_with_drafts(tmp_path)
    # 把 zh 草稿"搬"进剪映草稿库根目录下(模拟发布后的正式目录)
    published = jy_root / "AutoVideo_test"
    published.mkdir(parents=True)
    for name in ("draft_content.json", "draft_info.json"):
        (published / name).write_text(
            (tmp_path / "zh" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    state["draft_path"] = str(published / "draft_content.json")
    monkeypatch.setattr("config.JY_DRAFT_PUBLISH_ENABLED", True)
    monkeypatch.setattr("config.JIANYING_DRAFT_ROOT", str(jy_root))
    out = join_before_delivery(state)
    joined = " ".join(out["join_qa_report"]["error_texts"])
    assert "根索引" in joined, joined
    assert out["join_qa_report"]["passed"] is False


def test_join_registered_draft_with_root_index_passes(monkeypatch, tmp_path: Path) -> None:
    """发布开启且草稿已注册(draft_meta_info + 根索引)→ 索引检查通过。"""
    jy_root = tmp_path / "jianying_root"
    state = _state_with_drafts(tmp_path)
    published = jy_root / "AutoVideo_test"
    published.mkdir(parents=True)
    for name in ("draft_content.json", "draft_info.json"):
        (published / name).write_text(
            (tmp_path / "zh" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    state["draft_path"] = str(published / "draft_content.json")
    meta = {"draft_id": "did-zh", "draft_name": "AutoVideo_test", "tm_duration": 35_000_000}
    (published / "draft_meta_info.json").write_text(json.dumps(meta), encoding="utf-8")
    (jy_root / "root_meta_info.json").write_text(
        json.dumps({"all_draft_store": [meta]}), encoding="utf-8"
    )
    monkeypatch.setattr("config.JY_DRAFT_PUBLISH_ENABLED", True)
    monkeypatch.setattr("config.JIANYING_DRAFT_ROOT", str(jy_root))
    out = join_before_delivery(state)
    assert out["join_qa_report"]["error_texts"] == [], out["join_qa_report"]["error_texts"]
    assert "root_index" in out["join_qa_report"]["zh"]["checks"]


def test_join_warning_not_in_error_texts(tmp_path: Path) -> None:
    """深度 QA 的 warning 只进结构化报告,不污染 error_texts/error_log。"""
    state = _state_with_drafts(tmp_path)
    zh_dir = Path(state["draft_path"]).parent
    draft = json.loads((zh_dir / "draft_content.json").read_text(encoding="utf-8"))
    # duration<=0 的字幕条 → check_texts 出 warning
    draft["materials"]["texts"].append(
        {"id": "text-2", "content": "空段", "target_timerange": {"start": 0, "duration": 0}}
    )
    serialized = json.dumps(draft, ensure_ascii=False)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    out = join_before_delivery(state)
    assert out["join_qa_report"]["error_texts"] == [], out["join_qa_report"]["error_texts"]
    assert any(i["severity"] == "warning" for i in out["join_qa_report"]["issues"])


# ---------------------------------------------------------------------------
# 阶段 6(2026-11-09 效果缺失修复):required-feature gate
# ---------------------------------------------------------------------------
def test_join_report_categories_and_gate_ok(tmp_path: Path) -> None:
    """健康双草稿 → categories 全量存在,gate 不失败,required_features 回显。"""
    state = _state_with_drafts(tmp_path)
    out = join_before_delivery(state)
    report = out["join_qa_report"]
    cats = report["categories"]
    assert set(cats.keys()) >= {"subtitles_zh", "subtitles_en", "voiceover_zh", "bgm", "transitions", "volume_fades"}
    assert report["gate_failed"] is False
    assert report["required_features"]["subtitles_zh"] is True
    assert report["required_features"]["bgm"] is False
    # 夹具:1 个视频段 → 转场不适用;无 text-zh 前缀字幕且无旁白 → 字幕不适用
    assert cats["transitions"]["status"] == "not_applicable"
    assert cats["subtitles_zh"]["status"] == "not_applicable"
    assert "delivery_qa_gate_failed" not in out["status_log"]


def test_join_gate_failed_when_required_subtitles_missing(tmp_path: Path) -> None:
    """有旁白产物但草稿无我方字幕 → subtitles_zh failed → gate_failed。"""
    state = _state_with_drafts(
        tmp_path,
        voiceover_summary={"provider": "firered", "written": 3, "stub_count": 0, "failed": 0},
    )
    out = join_before_delivery(state)
    report = out["join_qa_report"]
    assert report["gate_failed"] is True
    assert report["categories"]["subtitles_zh"]["status"] == "failed"
    assert "delivery_qa_gate_failed" in out["status_log"]
    # 新 issue 进入字符串契约
    assert any("无我方字幕" in i for i in out["join_qa_report"]["error_texts"])


def test_join_gate_ignores_optional_bgm_missing(tmp_path: Path) -> None:
    """bgm 未 required 且无 BGM 轨 → not_applicable,gate 不失败。"""
    zh_dir = tmp_path / "zh2"
    en_dir = tmp_path / "en_branch2"
    draft = _healthy_draft(zh_dir, audio_volume=None)
    # 拆掉音轨:草稿无 audio 轨 → bgm n/a
    draft["tracks"] = [t for t in draft["tracks"] if t["type"] != "audio"]
    draft["materials"]["audios"] = []
    draft["materials"].pop("audio_fades", None)
    serialized = json.dumps(draft, ensure_ascii=False)
    zh_dir.mkdir(parents=True, exist_ok=True)
    (zh_dir / "draft_content.json").write_text(serialized, encoding="utf-8")
    (zh_dir / "draft_info.json").write_text(serialized, encoding="utf-8")
    _healthy_draft(en_dir)
    state = {
        "covers": _ok_covers(),
        "subtitle_srt_path": "/p/subtitle_en.srt",
        "draft_path": str(zh_dir / "draft_content.json"),
        "draft_dir_en_branch": str(en_dir),
        "status_log": [],
        "error_log": [],
    }
    out = join_before_delivery(state)
    report = out["join_qa_report"]
    assert report["categories"]["bgm"]["status"] == "not_applicable"
    assert report["categories"]["volume_fades"]["status"] == "not_applicable"
    assert report["gate_failed"] is False


def test_join_gate_stub_voiceover_degraded_blocks(monkeypatch, tmp_path: Path) -> None:
    """TTS_BACKEND=firered(生产语义)+ stub 配音摘要 → voiceover_zh degraded 阻断。"""
    monkeypatch.setenv("TTS_BACKEND", "firered")
    state = _state_with_drafts(
        tmp_path,
        voiceover_summary={"provider": "stub", "written": 2, "stub_count": 2, "failed": 0},
    )
    out = join_before_delivery(state)
    report = out["join_qa_report"]
    assert report["categories"]["voiceover_zh"]["status"] == "degraded"
    assert report["gate_failed"] is True


def test_join_explicit_required_features_state(tmp_path: Path) -> None:
    """state.required_features 显式声明 → 报告回显与 categories.required 反映。"""
    state = _state_with_drafts(tmp_path, required_features={"bgm": True})
    out = join_before_delivery(state)
    report = out["join_qa_report"]
    assert report["required_features"]["bgm"] is True
    assert report["categories"]["bgm"]["required"] is True
    # bgm required 但夹具草稿有 BGM 轨(非配音轨)→ 仍 passed,不阻断
    assert report["categories"]["bgm"]["status"] == "passed"
    assert report["gate_failed"] is False


def test_join_report_sha256_present(tmp_path: Path) -> None:
    state = _state_with_drafts(tmp_path)
    out = join_before_delivery(state)
    sha = out["join_qa_report"]["sha256"]
    assert "zh" in sha and "en" in sha
    assert len(sha["zh"]["draft_content.json"]) == 64

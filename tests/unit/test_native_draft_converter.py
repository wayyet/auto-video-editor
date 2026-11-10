"""storyline → 剪映原生 draft_content 转换器单测(2026-11-07 导入修复)。

字段级断言依据见 ``storyline/native_draft.py`` docstring:视频侧逐字对齐真实
剪映 5.9 样本,音频侧对齐 isYangs/jianying-editor-skill(MIT)参考实现。
"""

from __future__ import annotations

import json
from pathlib import Path

from storyline_capabilities.plan_timeline_pro import plan_timeline_pro
from storyline.native_draft import (
    _MATERIAL_BUCKETS,
    NATIVE_DRAFT_SIDECAR_NAME,
    is_native_draft,
    storyline_to_native,
    write_sidecar,
)


def _storyline_draft() -> dict:
    """贴近 mapper.canonical_to_draft 真实产出的最小 storyline 草稿。"""
    return {
        "canvas_config": {"width": 1080, "height": 1920},
        "storyline_meta": {
            "schema_version": 1,
            "job_id": "j1",
            "options": {},
            "audio": {},
            "subtitles": [],
        },
        "materials": {
            "videos": [
                {
                    "id": "video-1",
                    "type": "video",
                    "path": "",
                    "media_id": "m1",
                    "shot_id": "s1",
                    "source_in_ms": 0,
                    "source_out_ms": 1500,
                    "source_in_us": 0,
                    "source_out_us": 1500000,
                    "duration": 2000000,
                    "duration_ms": 2000,
                    "content_hash": "abc",
                    "material_name": "clip_0001",
                },
                {
                    "id": "video-2",
                    "type": "video",
                    "path": "",
                    "duration": 1000000,
                    "material_name": "clip_0002",
                },
            ],
            "audios": [
                {"id": "bgm-1", "type": "audio", "ref": "bgm_0001", "role": "bgm"}
            ],
            "texts": [],
        },
        "tracks": [
            {
                "type": "video",
                "id": "track-video-1",
                "segments": [
                    {
                        "id": "segment-1",
                        "material_id": "video-1",
                        "target_timerange": {"start": 0, "duration": 1500000},
                        "source_timerange": {"start": 0, "duration": 1500000},
                        "target_start_us": 0,
                        "target_end_us": 1500000,
                        "source_in_us": 0,
                        "source_out_us": 1500000,
                        "speed": 1.0,
                        "transcript": "hello",
                    },
                    {
                        "id": "segment-2",
                        "material_id": "video-2",
                        "target_timerange": {"start": 1500000, "duration": 1000000},
                        "source_timerange": {"start": 0, "duration": 1000000},
                        "speed": 1.0,
                    },
                ],
            },
            {
                "type": "audio",
                "id": "track-audio-1",
                "segments": [
                    {
                        "id": "segment-bgm-1",
                        "material_id": "bgm-1",
                        "target_start_us": 0,
                        "target_end_us": 2500000,
                        "speed": 1.0,
                        "loop": True,
                    }
                ],
            },
            {
                "type": "text",
                "id": "track-text-1",
                "segments": [
                    {
                        "id": "segment-text-1",
                        "text": "hi",
                        "target_start_us": 0,
                        "target_end_us": 500000,
                        "lang": "zh",
                    }
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# 顶层形状
# ---------------------------------------------------------------------------
class TestNativeShape:
    def test_is_native_marker(self) -> None:
        assert not is_native_draft(_storyline_draft())
        assert is_native_draft({"new_version": "110.0.0"})
        assert not is_native_draft("not-a-dict")

    def test_top_level_28_keys(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        assert len(native) == 28
        assert native["new_version"] == "110.0.0"
        assert native["version"] == 360000
        assert native["fps"] == 30.0
        assert native["color_space"] == 0
        assert native["source"] == "default"
        assert native["cover"] is None
        assert native["extra_info"] is None
        assert native["canvas_config"] == {
            "width": 1080, "height": 1920, "ratio": "original",
        }
        # 参考实现证明的最小 platform(不含本机指纹)
        assert native["platform"] == {
            "app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "windows",
        }
        assert native["last_modified_platform"] == native["platform"]

    def test_config_24_keys_from_sample(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        cfg = native["config"]
        assert len(cfg) == 24
        assert cfg["maintrack_adsorb"] is True
        assert cfg["video_mute"] is False
        assert cfg["multi_language_main"] == "none"

    def test_material_buckets_45_native_order(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        assert list(native["materials"].keys()) == list(_MATERIAL_BUCKETS)
        assert len(_MATERIAL_BUCKETS) == 45

    def test_keyframes_8_buckets(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        assert set(native["keyframes"].keys()) == {
            "adjusts", "audios", "effects", "filters",
            "handwrites", "stickers", "texts", "videos",
        }

    def test_duration_is_max_target_end(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        # bgm 占位被丢弃,最大终点 = segment-2 的 1500000+1000000
        assert native["duration"] == 2500000


# ---------------------------------------------------------------------------
# 视频 material / segment
# ---------------------------------------------------------------------------
class TestVideoConversion:
    def test_video_material_sample_fields(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        m = native["materials"]["videos"][0]
        assert len(m) == 46, "与真实样本视频素材键数逐字一致"
        assert m["type"] == "video"
        assert m["category_name"] == "local"
        assert m["check_flag"] == 63487
        assert m["duration"] == 2000000, "素材时长(µs)保留"
        assert m["material_name"] == "clip_0001"
        assert m["crop"]["upper_left_x"] == 0.0
        # 2026-11-11 修复:crop 必须是全幅单位正方形——退化裁切(如
        # lower_right_y=0.0)会被剪映拉伸铺满画布,成片右侧出现定向拉丝
        assert m["crop"] == {
            "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
            "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
        }
        assert m["video_algorithm"]["algorithms"] == []
        assert m["local_material_id"] == m["id"].lower()

    def test_video_segment_34_keys_order_preserved(self) -> None:
        native, sidecar = storyline_to_native(_storyline_draft())
        track = native["tracks"][0]
        assert track["type"] == "video"
        segs = track["segments"]
        assert len(segs) == 2
        assert len(segs[0]) == 34, "与真实样本 segment 键数逐字一致"
        # 保序保值
        assert segs[0]["target_timerange"] == {"start": 0, "duration": 1500000}
        assert segs[1]["target_timerange"] == {"start": 1500000, "duration": 1000000}
        assert segs[0]["source_timerange"] == {"start": 0, "duration": 1500000}
        assert segs[0]["clip"]["alpha"] == 1.0
        assert segs[0]["hdr_settings"] == {"intensity": 1.0, "mode": 1, "nits": 1000}
        assert segs[0]["uniform_scale"] == {"on": True, "value": 1.0}
        assert segs[0]["render_index"] == 0
        # 私有键不进原生稿
        assert "transcript" not in segs[0]
        assert "target_start_us" not in segs[0]
        assert sidecar["source_draft"]["tracks"][0]["segments"][0]["transcript"] == "hello"

    def test_segment_material_id_remapped(self) -> None:
        native, sidecar = storyline_to_native(_storyline_draft())
        seg = native["tracks"][0]["segments"][0]
        mat_id = native["materials"]["videos"][0]["id"]
        assert seg["material_id"] == mat_id
        assert sidecar["id_map"]["material"]["video-1"] == mat_id
        assert sidecar["id_map"]["segment"]["segment-1"] == seg["id"]

    def test_four_companions_per_segment(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        seg = native["tracks"][0]["segments"][0]
        refs = seg["extra_material_refs"]
        assert len(refs) == 4
        buckets = {b: {e["id"] for e in native["materials"][b]}
                   for b in ("speeds", "canvases", "sound_channel_mappings", "vocal_separations")}
        assert len(buckets["speeds"]) == 2, "每段一个 speed(样本形态)"
        for ref in refs:
            assert any(ref in ids for ids in buckets.values())
        speed_entry = next(e for e in native["materials"]["speeds"] if e["id"] == refs[0])
        assert speed_entry["type"] == "speed" and speed_entry["speed"] == 1.0


# ---------------------------------------------------------------------------
# 音频 / 文本
# ---------------------------------------------------------------------------
class TestAudioAndText:
    def test_unresolved_bgm_dropped_to_sidecar(self) -> None:
        """BGM ref 是资源 ID(bgm_0001),解析不到本地文件 → 丢弃进 sidecar。"""
        native, sidecar = storyline_to_native(_storyline_draft())
        assert native["materials"]["audios"] == []
        assert all(t["type"] != "audio" for t in native["tracks"])
        dropped = sidecar["dropped"]["dropped_audio_segments"]
        assert len(dropped) == 1
        assert dropped[0]["id"] == "segment-bgm-1"
        assert sidecar["source_draft"]["materials"]["audios"][0]["ref"] == "bgm_0001"

    def test_resolved_audio_converted(self, tmp_path: Path) -> None:
        draft = _storyline_draft()
        wav = tmp_path / "bgm.mp3"
        wav.write_bytes(b"fake-audio")
        draft["materials"]["audios"][0]["ref"] = wav.as_uri()
        native, sidecar = storyline_to_native(draft)
        audios = native["materials"]["audios"]
        assert len(audios) == 1
        a = audios[0]
        assert a["type"] == "extract_music"
        assert a["check_flag"] == 3
        assert a["duration"] == 2500000, "素材时长取段目标跨度"
        assert Path(a["path"]).is_file()
        audio_tracks = [t for t in native["tracks"] if t["type"] == "audio"]
        assert len(audio_tracks) == 1
        seg = audio_tracks[0]["segments"][0]
        assert seg["clip"] is None and seg["hdr_settings"] is None
        assert seg["target_timerange"] == {"start": 0, "duration": 2500000}
        assert seg["material_id"] == a["id"]
        assert "dropped_audio_segments" not in sidecar["dropped"]

    def test_mapper_text_track_dropped(self) -> None:
        native, sidecar = storyline_to_native(_storyline_draft())
        assert all(t["type"] != "text" for t in native["tracks"])
        assert sidecar["dropped"]["dropped_text_track"]["id"] == "track-text-1"


# ---------------------------------------------------------------------------
# 幂等 / sidecar 落盘
# ---------------------------------------------------------------------------
class TestIdempotenceAndSidecar:
    def test_native_passthrough(self) -> None:
        native, _ = storyline_to_native(_storyline_draft())
        again, sidecar = storyline_to_native(native)
        assert again is native
        assert sidecar is None

    def test_deterministic_ids(self) -> None:
        a, _ = storyline_to_native(_storyline_draft())
        b, _ = storyline_to_native(_storyline_draft())
        assert a == b, "uuid5 确定性:同输入同输出(重发布幂等)"

    def test_write_sidecar_roundtrip(self, tmp_path: Path) -> None:
        _, sidecar = storyline_to_native(_storyline_draft())
        out = write_sidecar(tmp_path, sidecar)
        assert out is not None and Path(out).is_file()
        loaded = json.loads(Path(out).read_text(encoding="utf-8"))
        assert loaded["schema"] == "autovideo_storyline_source/1"
        assert loaded == sidecar
        assert write_sidecar(tmp_path, None) is None

    def test_sidecar_not_native_marker(self) -> None:
        """sidecar 只进草稿目录,不污染 draft_content。"""
        native, _ = storyline_to_native(_storyline_draft())
        assert "storyline_meta" not in native


# ---------------------------------------------------------------------------
# 阶段 2(2026-11-09 效果缺失修复):结构化 voiceovers → mapper → native 音轨
# ---------------------------------------------------------------------------
import wave


def _tiny_wav(path, duration_ms: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * duration_ms / 1000))
    return str(path)


def test_voiceover_structured_chain_converts(tmp_path) -> None:
    """结构化 voiceovers(真实 WAV)→ mapper 逐段 material+组区间段 → native 音轨。

    双落点第一落(node_05 初稿可听):plan_timeline_pro 填充 voiceovers,
    mapper 消费真实 path,native 转换解析为 extract_music 素材与音频轨段。
    """
    from storyline.contract import CanonicalTimeline
    from storyline.mapper import canonical_to_draft

    wav1 = _tiny_wav(tmp_path / "storyline" / "voiceover_0001.wav", 2000)
    wav2 = _tiny_wav(tmp_path / "storyline" / "voiceover_0002.wav", 1500)
    groups = [
        {"group_id": "group_0001", "clips": [
            {"clip_id": "clip_0001", "media_id": "media_0001",
             "source_in_ms": 0, "source_out_ms": 3000}]},
        {"group_id": "group_0002", "clips": [
            {"clip_id": "clip_0002", "media_id": "media_0001",
             "source_in_ms": 3000, "source_out_ms": 6000}]},
    ]
    tts_res = [
        {"voiceover_id": "voiceover_0001", "group_id": "group_0001",
         "path": wav1, "duration": 2000, "provider": "firered", "error": None},
        {"voiceover_id": "voiceover_0002", "group_id": "group_0002",
         "path": wav2, "duration": 1500, "provider": "stub", "error": None},
    ]
    timeline = plan_timeline_pro(
        groups=groups,
        voiceover={"voiceover": tts_res, "provider": "firered", "stub_count": 0},
        source_media=[{"media_id": "media_0001", "file_uri": "file:///C:/v.mp4",
                       "duration_ms": 6000, "media_type": "video"}],
        targets={"target_duration_ms": 35000},
        job_id="j",
    )
    # plan_timeline_pro 填充结构化 voiceovers(真实路径随行)
    vos = timeline["audio"]["voiceovers"]
    assert [v["path"] for v in vos] == [wav1, wav2]
    assert [v["stub"] for v in vos] == [False, True]

    plan = CanonicalTimeline.model_validate(timeline)
    draft = canonical_to_draft(plan)
    audios = draft["materials"]["audios"]
    assert [a["id"] for a in audios] == ["voiceover-0001", "voiceover-0002"]
    assert [a["ref"] for a in audios] == [wav1, wav2]
    atrack = next(t for t in draft["tracks"] if t["type"] == "audio")
    # TTS 驱动分配:组区间跟随配音时长(组1 2s、组2 1.5s),非源范围
    assert [(s["target_start_us"], s["target_end_us"]) for s in atrack["segments"]] == [
        (0, 2_000_000), (2_000_000, 3_500_000),
    ]

    native, sidecar = storyline_to_native(draft)
    native_audio_tracks = [t for t in native["tracks"] if t["type"] == "audio"]
    assert len(native_audio_tracks) == 1
    assert len(native_audio_tracks[0]["segments"]) == 2
    assert len(native["materials"]["audios"]) == 2
    assert all(a["type"] == "extract_music" for a in native["materials"]["audios"])
    # 旧口径的 JSON 字符串 ref 不再产生 unresolved 丢弃(段全部落地)
    assert not sidecar["dropped"].get("dropped_audio_segments")
    # 引用完整性:音频段 speed 伴随无悬空
    from draft_ops.draft_integrity import check_track_references
    assert check_track_references(native, "zh") == []


# ---------------------------------------------------------------------------
# 2026-11-11 模糊修复阶段1:_probe_size 切显示口径(设计文档 §4.2)
# ---------------------------------------------------------------------------
class TestProbeSizeDisplayOrientation:
    """material 宽高声明必须与解码后显示方向一致;失败留痕不静默。"""

    @staticmethod
    def _draft_with_real_file(tmp_path: Path) -> dict:
        draft = _storyline_draft()
        fake_media = tmp_path / "source.mov"
        fake_media.write_bytes(b"fake")
        draft["materials"]["videos"][0]["path"] = str(fake_media)
        return draft

    def test_display_size_written_to_material(self, tmp_path, monkeypatch) -> None:
        """display 口径直写 material:fake 返回横屏 (1920,1080),与画布回退
        (1080,1920) 可区分,证明走的是探测结果而非兜底。"""
        import video_edit_capabilities.render as render_mod

        draft = self._draft_with_real_file(tmp_path)
        monkeypatch.setattr(render_mod, "probe_display_size", lambda source: (1920, 1080))
        native, _ = storyline_to_native(draft)
        m = native["materials"]["videos"][0]
        assert (m["width"], m["height"]) == (1920, 1080)

    def test_probe_failure_falls_back_to_canvas_with_warning(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        import logging

        import video_edit_capabilities.render as render_mod

        draft = self._draft_with_real_file(tmp_path)

        def boom(source):
            raise RuntimeError("ffprobe exploded")

        monkeypatch.setattr(render_mod, "probe_display_size", boom)
        with caplog.at_level(logging.WARNING, logger="storyline.native_draft"):
            native, _ = storyline_to_native(draft)
        m = native["materials"]["videos"][0]
        assert (m["width"], m["height"]) == (1080, 1920), "回退画布尺寸,不阻断转换"
        assert any(
            "display probe failed" in record.message for record in caplog.records
        ), "探测失败必须 logger.warning 留痕,不得静默伪装"

    def test_missing_file_short_circuits_to_canvas(self, tmp_path, monkeypatch) -> None:
        """path 指向不存在的文件 → 不触发探测直接回退画布(既有行为不变)。"""
        import video_edit_capabilities.render as render_mod

        calls: list[object] = []
        monkeypatch.setattr(
            render_mod, "probe_display_size", lambda source: calls.append(source)
        )
        draft = _storyline_draft()
        draft["materials"]["videos"][0]["path"] = str(tmp_path / "not_exist.mov")
        native, _ = storyline_to_native(draft)
        m = native["materials"]["videos"][0]
        assert (m["width"], m["height"]) == (1080, 1920)
        assert calls == [], "文件不存在时不得发起探测"

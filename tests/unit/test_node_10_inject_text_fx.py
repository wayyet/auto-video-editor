"""节点 10 字幕入场动画单测(阶段 4:原生 material_animations 通道)。

2026-11-18 job_id 链路缺陷修复增补:真实节点入口级 + LangGraph 图级测试
(审查文档阶段 2 用例 2.6-2.9)—— config.thread_id 权威 seed、session_id 兼容、
zh/en 节点入口动画一致、无 ID 显式降级。核心公式锁定见 ``_expected_pool_member``。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from nodes.node_10_inject_text_fx import inject_text_fx, inject_text_fx_en
from state import WorkflowState


_VIP_ID_PATTERN = re.compile(r"^\d{19}$")
_ANIM_REF_PREFIX = "text-anim-"


def _text_material(text_id: str, text: str, start: int, duration: int) -> dict:
    """node_08 产出的我方字幕素材形态(原生 content JSON + 私有键)。"""
    return {
        "id": text_id,
        "type": "text",
        "content": json.dumps({"styles": [{"range": [0, len(text)]}], "text": text}, ensure_ascii=False),
        "target_timerange": {"start": start, "duration": duration},
        "style": {"size": 5.0},
        "track_name": "Subtitles_zh",
    }


@pytest.fixture
def tmp_draft_with_texts(tmp_path: Path) -> Path:
    """2 条我方字幕(text 轨段齐全)+ 1 条人工文本素材。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {
            "texts": [
                _text_material("text-zh-1", "你好", 0, 1_500_000),
                _text_material("text-zh-2", "世界", 1_500_000, 1_500_000),
                {
                    "id": "4F1A2B3C-HUMAN-0001",
                    "type": "text",
                    "content": json.dumps({"text": "人工文本"}),
                    "target_timerange": {"start": 3_000_000, "duration": 1_000_000},
                },
            ],
        },
        "tracks": [{
            "type": "text",
            "id": "track-text-1",
            "segments": [
                {"id": "text-seg-1", "material_id": "text-zh-1",
                 "target_timerange": {"start": 0, "duration": 1_500_000}},
                {"id": "text-seg-2", "material_id": "text-zh-2",
                 "target_timerange": {"start": 1_500_000, "duration": 1_500_000}},
            ],
        }],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _state(draft_path: Path) -> dict:
    return {"draft_path": str(draft_path), "status_log": [], "error_log": []}


def test_inject_text_fx_returns_delta_not_full_state(tmp_draft_with_texts: Path) -> None:
    """返回增量 patch,不得 ``{**state}`` 全量回传(2026-11-20 回归锁)。

    本节点与 fork_draft_for_english_branch 同一超步并发(fork 唯一写
    ``draft_dir_en_branch``);线程复用旧 checkpoint 时该键在通道里残留旧值,
    全量回传会把它原样重发 → 同超步双写 → LangGraph ``InvalidUpdateError``
    (video-001 预热线程 2026-11-20 关卡①条件挂起后首次暴露)。
    """
    state = dict(_state(tmp_draft_with_texts))
    state["draft_dir_en_branch"] = "C:/stale/en_branch"  # 旧 checkpoint 残留值
    state["storyline_outputs_root"] = "C:/stale/outputs"
    out = inject_text_fx(state)
    assert "draft_dir_en_branch" not in out, "不得全量回传无关 state 键"
    assert "storyline_outputs_root" not in out
    assert "node_10_inject_text_fx_done" in (out.get("status_log") or [])


def test_inject_text_fx_writes_native_animations(tmp_draft_with_texts: Path) -> None:
    """我方字幕 → material_animations 条目 + text 轨段 refs 绑定;人工文本不动。"""
    out = inject_text_fx(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    anims = draft["materials"]["material_animations"]
    assert len(anims) == 2
    for anim in anims:
        assert anim["id"].startswith(_ANIM_REF_PREFIX)
        assert anim["type"] == "sticker_animation"
        (entry,) = anim["animations"]
        assert entry["type"] == "in"
        assert _VIP_ID_PATTERN.match(entry["resource_id"]), entry["resource_id"]
        assert int(entry["duration"]) > 0

    segments = draft["tracks"][0]["segments"]
    assert any(
        a["id"] in segments[0]["extra_material_refs"] for a in anims
    ), "text-zh-1 的轨段应绑定动画"
    assert any(
        a["id"] in segments[1]["extra_material_refs"] for a in anims
    ), "text-zh-2 的轨段应绑定动画"

    # 人工文本:无动画、无私有 style 改动
    human = next(t for t in draft["materials"]["texts"] if t["id"].startswith("4F1A"))
    assert not any(
        a["animations"][0]["name"] == f"{_ANIM_REF_PREFIX}{human['id']}" for a in anims
    )
    assert "entrance_animation" not in (human.get("style") or {})

    # 我方字幕的私有 style 也不再写 entrance_animation(原生通道替代)
    ours = [t for t in draft["materials"]["texts"] if t["id"].startswith("text-zh-")]
    assert all("entrance_animation" not in (t.get("style") or {}) for t in ours)

    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_fixed_mode_uses_config_name(
    tmp_draft_with_texts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fixed 模式:config 固定名"新年打字机"未 verified(守卫)→ 回退可用子集池序第一个。

    2026-11-19 守卫后 fixed 名也须过可用性闸;旧默认四个名字全不在库内,最终
    兜底从"库内第一个 VIP intro(乱码故障)"收紧为"可用子集池序第一个"。
    """
    from nodes.node_10_inject_text_fx import JIANYING_CACHE_ROOT

    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.TEXT_FX_INTRO_POOL_MODE", "fixed"
    )
    inject_text_fx(_state(tmp_draft_with_texts))
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    (first_usable,) = (_r["name"] for _r in _usable_intro_rows(JIANYING_CACHE_ROOT)[:1])
    assert names == {first_usable}


def _intro_library_names() -> set[str]:
    """text_resource_library.json intro 段全部 VIP 动画名(auto 池的合法域)。"""
    lib_path = Path(__file__).resolve().parents[2] / "templates" / "text_resource_library.json"
    with open(lib_path, "r", encoding="utf-8") as f:
        return {row["name"] for row in json.load(f).get("intro", [])}


def _usable_intro_rows(cache_root: str | Path) -> list[dict]:
    """真实池 JSON 里「verified 且缓存目录存在」的 intro 行(文件序即池序)。"""
    lib_path = Path(__file__).resolve().parents[2] / "templates" / "text_resource_library.json"
    with open(lib_path, "r", encoding="utf-8") as f:
        intro = list(json.load(f).get("intro", []))
    root = Path(cache_root)
    return [
        row
        for row in intro
        if row.get("verified")
        and (root / str(row["effect_id"]) / str(row["md5"])).is_dir()
    ]


def _expected_pool_member(job_id: str) -> dict:
    """镜像 node_10 池选公式(锁定契约,2026-11-19 守卫版)。

    sha256(job_id|text_intro_pool)[:8] 定位**全池**起点,按池序轮转到第一个
    「verified 且本地缓存存在」的条目;缓存根读取节点模块当前值(conftest
    封闭化 fixture 指到临时目录并伪造 verified 条目缓存)。
    """
    from nodes.node_10_inject_text_fx import JIANYING_CACHE_ROOT

    lib_path = Path(__file__).resolve().parents[2] / "templates" / "text_resource_library.json"
    with open(lib_path, "r", encoding="utf-8") as f:
        intro = list(json.load(f).get("intro", []))
    usable = _usable_intro_rows(JIANYING_CACHE_ROOT)
    assert usable, "conftest 封闭化 fixture 应伪造至少一条 verified 条目缓存"
    seed = int(hashlib.sha256(f"{job_id}|text_intro_pool".encode("utf-8")).hexdigest()[:8], 16)
    start = seed % len(intro)
    for offset in range(len(intro)):
        candidate = intro[(start + offset) % len(intro)]
        if candidate in usable:
            return candidate
    raise AssertionError("unreachable: usable 子集非空必有命中")


def _animation_fields(entry: dict) -> dict:
    """抽取动画对象的双语一致性比对字段(审查 2.7:五项逐项一致)。"""
    return {
        "name": entry["name"],
        "resource_id": entry["resource_id"],
        "effect_id": entry["effect_id"],
        "md5": entry["md5"],
        "duration": entry["duration"],
    }


def test_inject_text_fx_auto_pool_picks_valid_member_deterministically(
    tmp_draft_with_texts: Path,
) -> None:
    """auto 池模式(2026-11-18 默认):选中的动画 ∈ intro VIP 库;同任务确定复现。"""
    inject_text_fx(_state(tmp_draft_with_texts))
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    assert len(names) == 1, "同一支视频所有字幕应共用同一入场动画"
    (name,) = names
    assert name in _intro_library_names()

    # 同兜底 job_id(无 config/state ID 时解析为中文草稿目录名,此处即 "draft")重跑 → 同一动画(确定性轮换)
    inject_text_fx(_state(tmp_draft_with_texts))
    draft2 = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names2 = {a["animations"][0]["name"] for a in draft2["materials"]["material_animations"]}
    assert names2 == names


def test_inject_text_fx_auto_pool_same_job_zh_en_consistent(
    tmp_draft_with_texts: Path, tmp_path: Path
) -> None:
    """auto 模式下相同 job_id 的 zh/en 两次调用选出同一动画(同 seed 原则)。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    def _run(draft_file: Path) -> str:
        apply_text_fx(
            draft_file, [], node_tag="t", done_tag="t_done", job_id="job-consistent"
        )
        d = json.loads(draft_file.read_text(encoding="utf-8"))
        return d["materials"]["material_animations"][0]["animations"][0]["name"]

    zh_name = _run(tmp_draft_with_texts)
    en_draft = tmp_path / "draft_en" / "draft_content.json"
    en_draft.parent.mkdir(parents=True, exist_ok=True)
    en_draft.write_text(
        json.dumps({
            "canvas_config": {"width": 1080, "height": 1920},
            "duration": 10_000_000,
            "materials": {"texts": [_text_material("text-en-1", "hello", 0, 1_500_000)]},
            "tracks": [{"type": "text", "id": "t1", "segments": [
                {"id": "s1", "material_id": "text-en-1",
                 "target_timerange": {"start": 0, "duration": 1_500_000}}]}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    en_name = _run(en_draft)
    assert zh_name == en_name
    assert zh_name in _intro_library_names()


def test_inject_text_fx_dual_write_content_and_info(tmp_draft_with_texts: Path) -> None:
    """A6:写回后 ``draft_content.json`` 与 ``draft_info.json`` 都在,且内容一致。"""
    out = inject_text_fx(_state(tmp_draft_with_texts))

    content_file = tmp_draft_with_texts
    info_file = tmp_draft_with_texts.parent / "draft_info.json"
    assert content_file.exists(), "draft_content.json 不应被写没"
    assert info_file.exists(), (
        "draft_info.json 缺失 —— safe_write_draft 很可能收到了文件路径而不是目录"
    )
    assert info_file.read_text(encoding="utf-8") == content_file.read_text(encoding="utf-8")
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_no_nested_draft_directory(tmp_draft_with_texts: Path) -> None:
    """A6 反向断言:不该出现"以 draft_content.json 命名的子目录"这种退化写入。"""
    inject_text_fx(_state(tmp_draft_with_texts))

    nested = tmp_draft_with_texts / "draft_content.json"
    assert not nested.is_dir()


def test_inject_text_fx_works_on_empty_texts(tmp_path: Path) -> None:
    """materials.texts 为空时:节点通过,material_animations 保持空。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {},
        "tracks": [],
    }
    p = tmp_path / "draft" / "draft_content.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft), encoding="utf-8")

    out = inject_text_fx(_state(p))
    assert "node_10_inject_text_fx_done" in out["status_log"]
    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("material_animations")


def test_inject_text_fx_fallback_when_text_resource_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """text_resource_library.json 缺失时:动画降级不写入(degraded 计数),不抛异常。"""
    from jy_common import template_library as tl_mod

    def fake_loader(**_kwargs):
        return tl_mod.TemplateLibrary(
            {"default_style": {"outline": True, "shadow": True, "entrance_animation": None}},
            fx_lib={},
            text_lib={},
        )

    monkeypatch.setattr("nodes.node_10_inject_text_fx.load_resource_libraries", fake_loader)

    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [_text_material("text-zh-1", "你好", 0, 1_000_000)]},
        "tracks": [],
    }
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    p = draft_dir / "draft_content.json"
    p.write_text(json.dumps(draft), encoding="utf-8")
    out = inject_text_fx({"draft_path": str(p), "status_log": [], "error_log": []})

    draft_after = json.loads(p.read_text(encoding="utf-8"))
    assert not draft_after["materials"].get("material_animations")
    assert any("动画降级不写入" in t for t in out["status_log"])
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_replay_is_byte_identical(tmp_draft_with_texts: Path) -> None:
    """重放两次:整份草稿字节级一致(确定性 id + 清理不引入漂移)。"""
    inject_text_fx(_state(tmp_draft_with_texts))
    first = tmp_draft_with_texts.read_bytes()
    inject_text_fx(_state(tmp_draft_with_texts))
    second = tmp_draft_with_texts.read_bytes()
    assert first == second


def test_inject_text_fx_cleans_stale_animations(tmp_draft_with_texts: Path) -> None:
    """字幕被删后重放:旧动画素材与 refs 被清理,不留悬空。"""
    inject_text_fx(_state(tmp_draft_with_texts))

    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["texts"] = draft["materials"]["texts"][:1]  # 只留 text-zh-1
    draft["tracks"][0]["segments"] = draft["tracks"][0]["segments"][:1]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_text_fx(_state(tmp_draft_with_texts))
    draft2 = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    anims = draft2["materials"]["material_animations"]
    assert len(anims) == 1
    assert anims[0]["id"] == f"{_ANIM_REF_PREFIX}text-zh-1"
    seg_refs = draft2["tracks"][0]["segments"][0]["extra_material_refs"]
    assert anims[0]["id"] in seg_refs
    assert f"{_ANIM_REF_PREFIX}text-zh-2" not in seg_refs


def test_inject_text_fx_en_returns_delta_with_count_tag(tmp_path: Path) -> None:
    """英文分支:只返回 delta,status_log 带写入数标签(同超步无键冲突)。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [_text_material("text-en-1", "hello", 0, 1_000_000)]},
        "tracks": [{
            "type": "text",
            "id": "track-text-1",
            "segments": [{"id": "text-seg-1", "material_id": "text-en-1",
                          "target_timerange": {"start": 0, "duration": 1_000_000}}],
        }],
    }
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    (en_dir / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")

    # 2026-11-18 适配:job_id 链路修复后 en 入口经公共 resolver 解析任务 ID,
    # 直接调用必须显式携带(生产环境 session_id 恒由入口写入;config 由图注入)
    out = inject_text_fx_en({"draft_dir_en_branch": str(en_dir), "session_id": "console-en-delta"})
    en_draft = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert len(en_draft["materials"]["material_animations"]) == 1
    assert "node_10b_text_animations_written:1" in out["status_log"]
    assert "node_10b_inject_text_fx_en_done" in out["status_log"]


# ------------------------------------------------ job_id 链路修复(2026-11-18,审查阶段 2)


def test_inject_text_fx_uses_config_thread_id_as_seed(tmp_draft_with_texts: Path) -> None:
    """2.6 真实节点入口:config.thread_id 为权威 seed → 动画名 == 池公式预期条目。"""
    tid = "console-pooltest1"
    out = inject_text_fx(_state(tmp_draft_with_texts), {"configurable": {"thread_id": tid}})
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    assert names == {_expected_pool_member(tid)["name"]}
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_inject_text_fx_falls_back_to_session_id(tmp_draft_with_texts: Path) -> None:
    """旧 checkpoint 兼容:state 无 thread_id 只有 session_id → seed = session_id。"""
    sid = "console-pooltest2"
    state = {**_state(tmp_draft_with_texts), "session_id": sid}
    inject_text_fx(state)
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    assert names == {_expected_pool_member(sid)["name"]}


def test_inject_text_fx_en_node_matches_zh_with_same_config(
    tmp_draft_with_texts: Path, tmp_path: Path
) -> None:
    """2.7 zh(node_10)与 en(node_10b)真实入口同 config → 动画五字段逐项一致。"""
    tid = "console-dualtest"
    config = {"configurable": {"thread_id": tid}}
    inject_text_fx(_state(tmp_draft_with_texts), config)

    en_draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {"texts": [
            _text_material("text-en-1", "hello", 0, 1_500_000),
            _text_material("text-en-2", "world", 1_500_000, 1_500_000),
        ]},
        "tracks": [{"type": "text", "id": "t1", "segments": [
            {"id": "s1", "material_id": "text-en-1", "target_timerange": {"start": 0, "duration": 1_500_000}},
            {"id": "s2", "material_id": "text-en-2", "target_timerange": {"start": 1_500_000, "duration": 1_500_000}},
        ]}],
    }
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    en_file = en_dir / "draft_content.json"
    en_file.write_text(json.dumps(en_draft, ensure_ascii=False), encoding="utf-8")

    inject_text_fx_en(
        {"draft_dir_en_branch": str(en_dir), "status_log": [], "error_log": []}, config
    )

    zh_anims = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))["materials"]["material_animations"]
    en_anims = json.loads(en_file.read_text(encoding="utf-8"))["materials"]["material_animations"]
    assert len(zh_anims) == len(en_anims) == 2
    zh_fields = [_animation_fields(a["animations"][0]) for a in zh_anims]
    en_fields = [_animation_fields(a["animations"][0]) for a in en_anims]
    assert zh_fields == en_fields
    expected = _expected_pool_member(tid)
    assert zh_fields[0]["name"] == expected["name"]
    assert zh_fields[0]["resource_id"] == expected["resource_id"]


def test_inject_text_fx_en_without_any_job_id_degrades(tmp_path: Path) -> None:
    """2.5 en 无任何任务 ID(config/state 全空)→ 显式降级:不写动画 + 告警 + skipped 标签。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 5_000_000,
        "materials": {"texts": [_text_material("text-en-1", "hello", 0, 1_000_000)]},
        "tracks": [],
    }
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    en_file = en_dir / "draft_content.json"
    en_file.write_text(json.dumps(draft), encoding="utf-8")

    out = inject_text_fx_en({"draft_dir_en_branch": str(en_dir), "status_log": [], "error_log": []})

    after = json.loads(en_file.read_text(encoding="utf-8"))
    assert not after["materials"].get("material_animations")
    assert any("无法解析当前任务 ID" in m for m in out.get("error_log", []))
    assert "node_10b_inject_text_fx_en_skipped" in out.get("status_log", [])
    assert "node_10b_inject_text_fx_en_done" not in out.get("status_log", [])


def test_inject_text_fx_cross_task_rotation(tmp_draft_with_texts: Path) -> None:
    """2.9 跨任务轮换:不同 thread_id 各按池公式取值;预期不同则动画不同(允许自然碰撞)。"""
    tid_a = "console-rot-a"
    inject_text_fx(_state(tmp_draft_with_texts), {"configurable": {"thread_id": tid_a}})
    names_a = {
        a["animations"][0]["name"]
        for a in json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))["materials"]["material_animations"]
    }
    expected_a = _expected_pool_member(tid_a)["name"]
    assert names_a == {expected_a}

    # 确定性搜索一个池公式预期与 tid_a 不同的 thread_id(不依赖随机/碰撞概率)
    tid_b = None
    for i in range(1000):
        cand = f"console-rot-b{i}"
        if _expected_pool_member(cand)["name"] != expected_a:
            tid_b = cand
            break
    assert tid_b is not None

    inject_text_fx(_state(tmp_draft_with_texts), {"configurable": {"thread_id": tid_b}})
    names_b = {
        a["animations"][0]["name"]
        for a in json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))["materials"]["material_animations"]
    }
    assert names_b == {_expected_pool_member(tid_b)["name"]}
    assert names_a != names_b


def test_graph_level_config_thread_id_reaches_both_entries(tmp_path: Path) -> None:
    """2.8 图级:LangGraph 编译图注入 configurable.thread_id → zh/en 动画一致。

    用真实节点函数组建最小图(START → node_10 → node_10b → END),实证
    langgraph 1.2.9 按 ``Optional[RunnableConfig]`` 签名白名单注入 config。
    state 故意不带任何任务 ID,且 zh 目录名与 thread_id 故意不同 —— 若注入
    失效,zh/en 都会兜底到目录名(与预期池条目不符),断言即失败。
    图 schema 用 WorkflowState 本尊:langgraph 1.x 会从节点函数注解推断输入
    schema,自定义小 TypedDict 会与 WorkflowState 的 reducer 通道冲突。
    """
    from langgraph.graph import END, START, StateGraph

    zh_draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {"texts": [
            _text_material("text-zh-1", "你好", 0, 1_500_000),
            _text_material("text-zh-2", "世界", 1_500_000, 1_500_000),
        ]},
        "tracks": [{"type": "text", "id": "t1", "segments": [
            {"id": "text-seg-1", "material_id": "text-zh-1", "target_timerange": {"start": 0, "duration": 1_500_000}},
            {"id": "text-seg-2", "material_id": "text-zh-2", "target_timerange": {"start": 1_500_000, "duration": 1_500_000}},
        ]}],
    }
    zh_file = tmp_path / "AutoVideo_folder-doesnt-match" / "draft_content.json"
    zh_file.parent.mkdir(parents=True, exist_ok=True)
    zh_file.write_text(json.dumps(zh_draft, ensure_ascii=False), encoding="utf-8")

    en_draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "duration": 10_000_000,
        "materials": {"texts": [
            _text_material("text-en-1", "hello", 0, 1_500_000),
            _text_material("text-en-2", "world", 1_500_000, 1_500_000),
        ]},
        "tracks": [{"type": "text", "id": "t1", "segments": [
            {"id": "text-seg-1", "material_id": "text-en-1", "target_timerange": {"start": 0, "duration": 1_500_000}},
            {"id": "text-seg-2", "material_id": "text-en-2", "target_timerange": {"start": 1_500_000, "duration": 1_500_000}},
        ]}],
    }
    en_dir = tmp_path / "en_branch"
    en_dir.mkdir()
    en_file = en_dir / "draft_content.json"
    en_file.write_text(json.dumps(en_draft, ensure_ascii=False), encoding="utf-8")

    g = StateGraph(WorkflowState)
    g.add_node("node_10", inject_text_fx)
    g.add_node("node_10b", inject_text_fx_en)
    g.add_edge(START, "node_10")
    g.add_edge("node_10", "node_10b")
    g.add_edge("node_10b", END)
    app = g.compile()

    tid = "console-graphtest"
    app.invoke(
        {
            "draft_path": str(zh_file),
            "draft_dir_en_branch": str(en_dir),
            "status_log": [],
            "error_log": [],
        },
        config={"configurable": {"thread_id": tid}},
    )

    zh_names = {
        a["animations"][0]["name"]
        for a in json.loads(zh_file.read_text(encoding="utf-8"))["materials"]["material_animations"]
    }
    en_names = {
        a["animations"][0]["name"]
        for a in json.loads(en_file.read_text(encoding="utf-8"))["materials"]["material_animations"]
    }
    expected = _expected_pool_member(tid)["name"]
    assert zh_names == en_names == {expected}


# ------------------------------------------------ 可用性守卫(2026-11-19 动画丢失根因修复)


def _fake_lib_with_intro(rows: list[dict]):
    from jy_common.template_library import TemplateLibrary

    return TemplateLibrary(
        {"default_style": {"outline": True, "shadow": True, "entrance_animation": None}},
        fx_lib={},
        text_lib={"intro": rows},
    )


def _seed_position(job_id: str, pool_len: int) -> int:
    seed = int(hashlib.sha256(f"{job_id}|text_intro_pool".encode("utf-8")).hexdigest()[:8], 16)
    return seed % pool_len


def test_auto_pool_walks_past_unverified_seed_member(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed 命中未 verified 条目 → 按池序向后回退到第一条可用条目(不产生丢失草稿)。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    rows = [
        {"name": "守卫甲", "is_vip": True, "duration_s": 1.0,
         "resource_id": "1111111111111111111", "effect_id": "E1", "md5": "M1"},
        {"name": "守卫乙", "is_vip": True, "duration_s": 1.0,
         "resource_id": "2222222222222222222", "effect_id": "E2", "md5": "M2", "verified": True},
        {"name": "守卫丙", "is_vip": True, "duration_s": 1.0,
         "resource_id": "3333333333333333333", "effect_id": "E3", "md5": "M3", "verified": True},
    ]
    cache_root = tmp_path / "cache"
    (cache_root / "E2" / "M2").mkdir(parents=True)
    (cache_root / "E3" / "M3").mkdir(parents=True)
    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(cache_root))
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.load_resource_libraries",
        lambda **_kw: _fake_lib_with_intro(rows),
    )
    # 确定性搜索 seed 落在池序 0(未 verified 的「守卫甲」)的 job_id
    job_id = next(
        j for j in (f"guard-walk-{i}" for i in range(1000))
        if _seed_position(j, len(rows)) == 0
    )
    apply_text_fx(tmp_draft_with_texts, [], node_tag="t", done_tag="t_done", job_id=job_id)
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    assert names == {"守卫乙"}


def test_verified_but_cache_missing_walks_to_next(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verified 但缓存目录不存在(被清理)→ 同样跳过,缓存存在是第二道闸。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    rows = [
        {"name": "守卫甲", "is_vip": True, "duration_s": 1.0,
         "resource_id": "1111111111111111111", "effect_id": "E1", "md5": "M1", "verified": True},
        {"name": "守卫乙", "is_vip": True, "duration_s": 1.0,
         "resource_id": "2222222222222222222", "effect_id": "E2", "md5": "M2", "verified": True},
    ]
    cache_root = tmp_path / "cache"
    (cache_root / "E2" / "M2").mkdir(parents=True)  # 故意不伪造甲的 E1/M1
    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(cache_root))
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.load_resource_libraries",
        lambda **_kw: _fake_lib_with_intro(rows),
    )
    job_id = next(
        j for j in (f"guard-cache-{i}" for i in range(1000))
        if _seed_position(j, len(rows)) == 0
    )
    apply_text_fx(tmp_draft_with_texts, [], node_tag="t", done_tag="t_done", job_id=job_id)
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    names = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}
    assert names == {"守卫乙"}


def test_no_usable_animation_degrades(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全池无可用条目(有 verified 但缓存全缺)→ 降级不写动画,不伪装成功。"""
    from nodes.node_10_inject_text_fx import apply_text_fx

    rows = [
        {"name": "守卫甲", "is_vip": True, "duration_s": 1.0,
         "resource_id": "1111111111111111111", "effect_id": "E1", "md5": "M1", "verified": True},
    ]
    cache_root = tmp_path / "cache"  # 空缓存根
    cache_root.mkdir()
    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(cache_root))
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.load_resource_libraries",
        lambda **_kw: _fake_lib_with_intro(rows),
    )
    log, _summary = apply_text_fx(
        tmp_draft_with_texts, [], node_tag="t", done_tag="t_done", job_id="any"
    )
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    assert not draft["materials"].get("material_animations")
    assert any("无可用入场动画资源" in t for t in log)
    assert "t_done" in log
    assert not any("_text_anim_selected:" in t for t in log)


def test_selected_animation_audited_in_status_log(tmp_draft_with_texts: Path) -> None:
    """选中动画以 status_log 标签审计(事件流可见,不复活 text_fx_summary 死键)。"""
    out = inject_text_fx(_state(tmp_draft_with_texts))
    expected = _expected_pool_member("draft")  # 无 config/state ID 时兜底=目录名
    tags = [t for t in out["status_log"] if t.startswith("node_10_text_anim_selected:")]
    assert len(tags) == 1
    assert expected["name"] in tags[0]
    assert expected["effect_id"] in tags[0]


def test_selection_audit_recomputable_from_tag(
    tmp_draft_with_texts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-01:标签携带 pool/usable/start/pick/offset,可从池 JSON+缓存根复算验证。"""
    from nodes.node_10_inject_text_fx import JIANYING_CACHE_ROOT

    tid = "console-audit1"
    inject_text_fx(_state(tmp_draft_with_texts), {"configurable": {"thread_id": tid}})
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    (name,) = {a["animations"][0]["name"] for a in draft["materials"]["material_animations"]}

    # 从真实池 JSON + 当前缓存根独立复算 seed 起点与回退偏移
    lib_path = Path(__file__).resolve().parents[2] / "templates" / "text_resource_library.json"
    with open(lib_path, "r", encoding="utf-8") as f:
        rows = list(json.load(f).get("intro", []))
    usable = _usable_intro_rows(JIANYING_CACHE_ROOT)
    seed = int(hashlib.sha256(f"{tid}|text_intro_pool".encode("utf-8")).hexdigest()[:8], 16)
    start = seed % len(rows)
    offset = next(
        i for i in range(len(rows)) if rows[(start + i) % len(rows)] in usable
    )
    pick = (start + offset) % len(rows)

    out = inject_text_fx(_state(tmp_draft_with_texts), {"configurable": {"thread_id": tid}})
    tag = next(
        t for t in out["status_log"] if t.startswith("node_10_text_anim_selected:")
    )
    assert name in tag
    assert f"pool={len(rows)}" in tag
    assert f"usable={len(usable)}" in tag
    assert f"start={start}" in tag
    assert f"pick={pick}" in tag
    assert f"offset={offset}" in tag
    assert "mode=auto" in tag


# ---------------------------------------- 非破坏性降级(2026-11-19 审查 P1-01)


def _preset_our_animation(text_id: str, anim_name: str = "旧动画A") -> dict:
    """预置一条「上一轮已写入」的我方动画条目(真实构造器产出)。"""
    from jy_common.native_fx import build_native_text_animation_material

    return build_native_text_animation_material(
        material_id=f"{_ANIM_REF_PREFIX}{text_id}",
        anim_meta={
            "name": anim_name,
            "duration_s": 0.5,
            "resource_id": "1111111111111111111",
            "effect_id": "1111111",
            "md5": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
        kind="in",
    )


def _empty_cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖 conftest 封闭化:缓存根指向**空目录**(模拟缓存全缺/暂时不可见)。"""
    empty = tmp_path / "empty-cache-root"
    empty.mkdir()
    monkeypatch.setattr("nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(empty))


def test_degrade_keeps_existing_our_animations(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无可用资源且草稿已有我方动画 → 旧素材与 refs 原样保留,仅降级告警。"""
    old_anim = _preset_our_animation("text-zh-1", anim_name="旧动画A")
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["material_animations"] = [old_anim]
    draft["tracks"][0]["segments"][0]["extra_material_refs"] = [old_anim["id"]]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    _empty_cache_root(tmp_path, monkeypatch)
    out = inject_text_fx(_state(tmp_draft_with_texts))

    after = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    anims = after["materials"]["material_animations"]
    assert [a["id"] for a in anims] == ["text-anim-text-zh-1"], "旧动画素材不得被降级清除"
    assert anims[0]["animations"][0]["name"] == "旧动画A"
    seg_refs = after["tracks"][0]["segments"][0]["extra_material_refs"]
    assert seg_refs == ["text-anim-text-zh-1"], "旧引用不得被剥离"
    assert any("无可用入场动画资源" in t for t in out["status_log"])
    assert "node_10_inject_text_fx_done" in out["status_log"]


def test_degrade_on_missing_cache_root_keeps_animations(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存根路径不存在(配置错误)→ 同样非破坏降级,不清除现有草稿动画。"""
    old_anim = _preset_our_animation("text-zh-2")
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["material_animations"] = [old_anim]
    draft["tracks"][0]["segments"][1]["extra_material_refs"] = [old_anim["id"]]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    missing = tmp_path / "no-such-cache-root"
    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(missing)
    )
    out = inject_text_fx(_state(tmp_draft_with_texts))

    after = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    assert [a["id"] for a in after["materials"]["material_animations"]] == [
        "text-anim-text-zh-2"
    ]
    assert after["tracks"][0]["segments"][1]["extra_material_refs"] == [
        "text-anim-text-zh-2"
    ]
    assert any("无可用入场动画资源" in t for t in out["status_log"])


def test_degrade_keeps_human_animations(
    tmp_draft_with_texts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无可用资源且草稿有人工动画(uuid id)→ 人工素材与 refs 完全不变。"""
    human_anim = {
        "id": "4F1A2B3C-HUMAN-ANIM",
        "type": "sticker_animation",
        "multi_language_current": "none",
        "animations": [{
            "id": "4F1A2B3C-HUMAN-ANIM:in", "name": "人工动画", "type": "in",
            "start": 0, "duration": 500000, "resource_id": "9999999999999999999",
        }],
    }
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["material_animations"] = [human_anim]
    draft["tracks"][0]["segments"][1]["extra_material_refs"] = ["4F1A2B3C-HUMAN-ANIM"]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    _empty_cache_root(tmp_path, monkeypatch)
    inject_text_fx(_state(tmp_draft_with_texts))

    after = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    assert [a["id"] for a in after["materials"]["material_animations"]] == [
        "4F1A2B3C-HUMAN-ANIM"
    ]
    assert after["tracks"][0]["segments"][1]["extra_material_refs"] == [
        "4F1A2B3C-HUMAN-ANIM"
    ]


def test_new_animation_replaces_old_and_cleans_orphans(tmp_draft_with_texts: Path) -> None:
    """有新可用资源 → 新动画替换旧同 id 条目并绑定;孤儿我方旧动画被清理。"""
    old_anim = _preset_our_animation("text-zh-1", anim_name="旧动画A")
    orphan = _preset_our_animation("text-zh-9", anim_name="孤儿旧动画")
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["material_animations"] = [old_anim, orphan]
    draft["tracks"][0]["segments"][0]["extra_material_refs"] = [old_anim["id"]]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    expected = _expected_pool_member("draft")
    inject_text_fx(_state(tmp_draft_with_texts))

    after = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    anims = {a["id"]: a for a in after["materials"]["material_animations"]}
    assert set(anims) == {"text-anim-text-zh-1", "text-anim-text-zh-2"}, "孤儿 text-anim-text-zh-9 应被清理"
    assert anims["text-anim-text-zh-1"]["animations"][0]["name"] == expected["name"], "同 id 条目应被新动画替换"
    seg_refs = after["tracks"][0]["segments"][0]["extra_material_refs"]
    assert seg_refs == ["text-anim-text-zh-1"], "refs 仍指向被替换后的同 id 条目"


def test_human_animation_survives_successful_rewrite(tmp_draft_with_texts: Path) -> None:
    """成功写入新动画时,人工动画素材与引用仍不受清理影响。"""
    human_anim = {
        "id": "4F1A2B3C-HUMAN-ANIM",
        "type": "sticker_animation",
        "multi_language_current": "none",
        "animations": [{
            "id": "4F1A2B3C-HUMAN-ANIM:in", "name": "人工动画", "type": "in",
            "start": 0, "duration": 500000, "resource_id": "9999999999999999999",
        }],
    }
    draft = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    draft["materials"]["material_animations"] = [human_anim]
    draft["tracks"][0]["segments"][1]["extra_material_refs"] = ["4F1A2B3C-HUMAN-ANIM"]
    tmp_draft_with_texts.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    inject_text_fx(_state(tmp_draft_with_texts))

    after = json.loads(tmp_draft_with_texts.read_text(encoding="utf-8"))
    ids = {a["id"] for a in after["materials"]["material_animations"]}
    assert "4F1A2B3C-HUMAN-ANIM" in ids, "人工动画不得被我方清理波及"
    assert "text-anim-text-zh-1" in ids and "text-anim-text-zh-2" in ids
    seg_refs = after["tracks"][0]["segments"][1]["extra_material_refs"]
    assert "4F1A2B3C-HUMAN-ANIM" in seg_refs

"""node_16a 真机端到端验收(2026-11-20 第九期审查 §5.2 / 阶段 3)。

用真实 mcode CLI 后端驱动 node_16a 的**非 skip** 翻译分支:
有源中文字幕 → 真实翻译 → 写 marker(v2)/英文分支草稿/SRT → node_08b 消费
→ 同输入重跑命中 marker 不再调模型。审查 §5.2 指出:video-001 预检走的是
无源字幕 skip 路径,不能作为 node_16a 真机验收证据 —— 本脚本补上这一环。

证据全部落 outputs/diagnostics/node16a-e2e-<UTC时间戳>/(outputs/ 已
gitignore,不进仓库):verdict.json、model_raw_response.txt、prompt_snapshot.txt、
marker/SRT 副本。不写 checkpoint、不动 Postgres、不起服务。

用法(仓库根目录):
    .venv/Scripts/python.exe scripts/verify_node16a_translation_e2e.py
退出码 0 = 全部验收项通过;1 = 任一项失败(细节看 verdict.json)。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:  # 旧解释器兜底
    pass

from draft_ops import atomic_writer  # noqa: E402
from jy_common.translate_client import (  # noqa: E402
    TRANSLATION_PROMPT_VERSION,
    LLMTranslateClient,
    get_default_client,
)
from nodes.node_08b_add_subtitles_en import add_subtitles_en  # noqa: E402
from nodes.node_16_translate_subtitles import _compute_source_fingerprint  # noqa: E402
from nodes.node_16a_translate_and_check import node_16a_translate_and_check  # noqa: E402

#: 验收样本:4 条中文短句字幕(时间码毫秒,与 SRT/草稿断言一一对应)
SEGMENTS_ZH = [
    {"index": 0, "start_ms": 0, "end_ms": 2_000, "text_zh": "欢迎来到这座海边小城"},
    {"index": 1, "start_ms": 2_000, "end_ms": 4_200, "text_zh": "清晨的市场已经热闹起来"},
    {"index": 2, "start_ms": 4_200, "end_ms": 6_400, "text_zh": "渔船正驶向雾中的灯塔"},
    {"index": 3, "start_ms": 6_400, "end_ms": 8_600, "text_zh": "别忘了尝一碗当地的海鲜面"},
]


def _srt_to_ms(stamp: str) -> int:
    """SRT 时间戳 'HH:MM:SS,mmm' → 毫秒。"""
    h, m, rest = stamp.strip().split(":")
    s, ms = rest.split(",")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = REPO_ROOT / "outputs" / "diagnostics" / f"node16a-e2e-{stamp}"
    en_branch = evidence_dir / "en_branch"
    outputs_root = evidence_dir / "outputs_root"
    en_branch.mkdir(parents=True, exist_ok=True)
    outputs_root.mkdir(parents=True, exist_ok=True)

    # 剪映开着时草稿写入互斥会 raise —— 与单测 fixture 同款:注入"不在跑"的桩
    atomic_writer._default_proc_checker = lambda: False  # noqa: SLF001

    # ---- 装配真实 mcode 后端(与生产同一条装配链)----
    from agent_gate.bootstrap import install_llm_backend

    cli_client = install_llm_backend("mcode-cli")
    translator = get_default_client()
    checks: dict[str, object] = {
        "git_sha": _git_sha(),
        "backend": type(cli_client).__name__,
        "translator_id": getattr(translator, "translator_id", None),
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "cli_max_attempts": getattr(cli_client, "max_attempts", None),
        "timestamp_utc": stamp,
    }
    if not isinstance(translator, LLMTranslateClient):
        checks["fatal"] = f"默认翻译客户端不是 LLMTranslateClient: {type(translator).__name__}"
        return _finish(checks, evidence_dir, ok=False)

    # 原始输出/prompt 留证(经实例级包装,不改任何共享状态)
    orig_chat = cli_client.chat

    def _tee_chat(**kwargs):
        out = orig_chat(**kwargs)
        (evidence_dir / "model_raw_response.txt").write_text(str(out), encoding="utf-8")
        snap = {
            "system_prompt_head": str(kwargs.get("system_prompt", ""))[:2000],
            "user_prompt_head": str(kwargs.get("user_prompt", ""))[:2000],
        }
        (evidence_dir / "prompt_snapshot.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return out

    cli_client.chat = _tee_chat  # type: ignore[method-assign]

    # ---- 英文分支草稿(中文占位内容,结构同单测 fixture)----
    draft = {
        "canvas_config": {},
        "materials": {"texts": [
            {"content": seg["text_zh"],
             "target_timerange": {"start": seg["start_ms"] * 1_000,
                                  "duration": (seg["end_ms"] - seg["start_ms"]) * 1_000}}
            for seg in SEGMENTS_ZH
        ]},
    }
    (en_branch / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    state: dict = {
        "subtitle_segments_zh": [dict(s) for s in SEGMENTS_ZH],
        "draft_dir_en_branch": str(en_branch),
        "storyline_outputs_root": str(outputs_root),
        "status_log": [],
        "error_log": [],
    }

    # ---- ① node_16a 真实翻译(必须非 skip)----
    t0 = time.monotonic()
    out = node_16a_translate_and_check(state)
    checks["duration_ms"] = int((time.monotonic() - t0) * 1000)
    checks["translation_status"] = out.get("subtitle_translation_status")
    checks["not_skipped"] = out.get("subtitle_translation_status") == "real"
    checks["status_log"] = out.get("status_log")

    segs_en = out.get("subtitle_segments_en") or []
    checks["segment_count_matches"] = len(segs_en) == len(SEGMENTS_ZH)
    checks["index_set_matches"] = (
        sorted(s.get("index") for s in segs_en)
        == sorted(s["index"] for s in SEGMENTS_ZH)
    )
    checks["all_text_en_nonempty"] = all(
        str(s.get("text_en") or "").strip() for s in segs_en
    )
    checks["timecodes_inherited"] = all(
        int(s.get("start_ms")) == src["start_ms"] and int(s.get("end_ms")) == src["end_ms"]
        for s, src in zip(segs_en, SEGMENTS_ZH)
    )
    checks["en_texts"] = [str(s.get("text_en")) for s in segs_en]

    # ---- ② marker:schema v2 + v2 prompt 版本 + 身份/指纹 ----
    marker_path = en_branch / "subtitle_en.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else {}
    checks["marker_schema_version"] = marker.get("schema_version")
    checks["marker_prompt_version"] = marker.get("prompt_version")
    checks["marker_translator_id"] = marker.get("translator_id")
    checks["marker_fingerprint_matches_state"] = (
        marker.get("source_fingerprint") == out.get("subtitle_translation_fingerprint")
    )
    checks["marker_valid"] = (
        marker.get("schema_version") == 2
        and marker.get("prompt_version") == "subtitle-translate-v2"
        and marker.get("translator_id") == "mcode-cli"
    )
    checks["fingerprint_recomputes"] = (
        _compute_source_fingerprint(
            state["subtitle_segments_zh"],
            translator_id="mcode-cli",
            prompt_version=TRANSLATION_PROMPT_VERSION,
        )
        == out.get("subtitle_translation_fingerprint")
    )

    # ---- ③ SRT:段数/时间码 ----
    srt_path = out.get("subtitle_srt_path")
    srt_ok = False
    srt_times: list[int] = []
    if srt_path and Path(srt_path).exists():
        for block in Path(srt_path).read_text(encoding="utf-8").strip().split("\n\n"):
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if len(lines) >= 2 and "-->" in lines[1]:
                srt_times.append(_srt_to_ms(lines[1].split("-->")[0]))
    srt_ok = (
        bool(srt_times)
        and len(srt_times) == len(SEGMENTS_ZH)
        and srt_times == [s["start_ms"] for s in SEGMENTS_ZH]
    )
    checks["srt_exists"] = bool(srt_path and Path(srt_path).exists())
    checks["srt_count_and_timecodes_match"] = srt_ok
    if srt_path and Path(srt_path).exists():
        (evidence_dir / "subtitle_en.srt.copy").write_text(
            Path(srt_path).read_text(encoding="utf-8"), encoding="utf-8"
        )
    if marker_path.exists():
        (evidence_dir / "subtitle_en.marker.copy.json").write_text(
            marker_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    # ---- ④ 下游 node_08b 消费 ----
    # 真实图里 node_16a 的 state delta 提供 subtitle_translation_status;
    # harness 里显式带上,否则 node_08b 会按"非 real 不冒充"门禁跳过。
    state_en = dict(state)
    state_en["subtitle_segments_en"] = segs_en
    state_en["subtitle_translation_status"] = out.get("subtitle_translation_status")
    out_08b = add_subtitles_en(state_en)
    checks["node_08b_done"] = "node_08b_add_subtitles_en_done" in (
        out_08b.get("status_log") or []
    )
    draft_after = json.loads(
        (en_branch / "draft_content.json").read_text(encoding="utf-8")
    )

    def _rendered_text(mat: dict) -> str:
        """原生 content 升级后 content 是样式 JSON,渲染文本在 .text 键。"""
        raw = mat.get("content")
        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            try:
                return str(json.loads(raw).get("text") or "")
            except ValueError:
                return raw
        return str(raw or "")

    draft_texts = [
        _rendered_text(t)
        for t in draft_after.get("materials", {}).get("texts", [])
    ]
    # 折行修复(20261118)会把渲染文本按行宽插入 \n —— 比较时规范化空白,
    # 原始 en_texts 保留在 verdict 里供人工核对
    _norm = lambda s: " ".join(str(s).split())  # noqa: E731
    checks["node_08b_texts_match_translation"] = (
        [_norm(t) for t in draft_texts] == [_norm(t) for t in checks["en_texts"]]
    )
    checks["node_08b_track_present"] = any(
        t.get("type") == "text" for t in draft_after.get("tracks", [])
    )

    # ---- ⑤ resume 幂等:marker 命中,不再调模型 ----
    import nodes.node_16a_translate_and_check as node_mod

    def _must_not_translate(segments):
        raise AssertionError("marker 已命中,不应再次调用翻译模型")

    orig_translate = node_mod.translate_segments
    node_mod.translate_segments = _must_not_translate
    try:
        out2 = node_16a_translate_and_check(state)
    finally:
        node_mod.translate_segments = orig_translate
    checks["resume_marker_hit"] = (
        out2.get("subtitle_translation_status") == "real"
        and [_norm(s.get("text_en")) for s in (out2.get("subtitle_segments_en") or [])]
        == [_norm(t) for t in checks["en_texts"]]
    )

    ok = all(
        v for k, v in checks.items()
        if isinstance(v, bool)
    )
    return _finish(checks, evidence_dir, ok=ok)


def _git_sha() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "<unknown>"


def _finish(checks: dict, evidence_dir: Path, *, ok: bool) -> int:
    checks["passed"] = ok
    (evidence_dir / "verdict.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"\n证据目录: {evidence_dir}")
    print("E2E PASS" if ok else "E2E FAIL(见 verdict.json 的 False 项)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

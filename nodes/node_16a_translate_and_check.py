"""node_16a_translate_and_check — 步骤 16a:英文字幕翻译(第六期改版)。

历史:Week 5 曾在本节点内做"布局检测"(subtitle_build → subtitle_render →
subtitle_qc qc 链 + ``validate_layout`` 兜底),产出 ``layout_issues`` /
``layout_issues_detected`` / 预览视频 / 证据帧,供关卡③
(``node_checkpoint3_layout_review``)人工复核;2026-11-15 关卡③整体删除,
布局自动检测随之下线(能力层 ``subtitle_build/render/qc`` 保留,另有消费方;
产品行为变化:英文字幕超宽/重叠等问题不再自动检测,见执行记录)。
本节点回归单一职责:**翻译 + 写 marker + 写英文字幕到英文分支草稿 + 写 SRT**。

2026-11-17 P1(英文字幕真实翻译,修订计划 §5.2/§5.5):
- 按默认翻译客户端类型判定 ``subtitle_translation_status``:
  ``LLMTranslateClient`` → "real",Mock 桩 → "stub"(显式标注,不冒充成功);
- marker 升级 schema v2:命中条件 = 源指纹 + 翻译器身份 + 提示词版本 + 段数 +
  译文合法**同时满足**;旧 list 形态(legacy mock)一律失效重翻;
- 真实翻译失败(LLM 异常/输出不符)→ ``TranslationError`` fail-closed 抛出,
  marker 未落盘,resume 可重试 —— 绝不把未翻译段当成功写入 state。

- 翻译源 = node_08 产出的中文旁白字幕段(``subtitle_segments_zh``,时间取自
  当前草稿);旧线程 checkpoint 残留的 ``asr_segments_zh`` 按兼容语义兜底
  (B1 移除后新流程不再产出)。
- 无源字幕是英文分支的**常态输入**(B1 ASR 移除后),正常 skip 而非错误。

依赖:共享纯函数来自 ``node_16_translate_subtitles``。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jy_common.translate_client import (
    TRANSLATION_PROMPT_VERSION,
    LLMTranslateClient,
    TranslationError,
    get_default_client,
    translate_segments,
)
from nodes.node_16_translate_subtitles import (
    _compute_source_fingerprint,
    _marker_exists,
    _read_segments_from_marker,
    _save_marker,
    write_srt_from_segments,
)
from nodes.storyline._common import load_web_topic_en_reference
from state import WorkflowState

#: 翻译状态取值(subtitle_translation_status)
STATUS_REAL = "real"
STATUS_STUB = "stub"
STATUS_SKIPPED = "skipped"


def _dump_translation_failure(
    outputs_root: str | None,
    *,
    trace: dict[str, Any],
    translator_id: str,
    prompt_version: str,
    segments_count: int,
    fingerprint: str,
) -> None:
    """翻译失败取证:trace(含模型原始输出 raw_head)落盘 diagnostics/ 再上抛。

    背景(2026-11-20 形状漂移事故,console-011419d9ceec):chat_json_with_trace
    会把 CLI 调用层异常折成空 dict,最终报错"翻译输出缺少 translations 数组"
    判定的是空 dict 而非模型原始输出 —— trace.raw_head 是兜底文案下唯一能看到
    真实原因的证据(异常对象挂着 trace,但全链路无人消费,run 日志里不留痕)。

    Best-effort:outputs_root 缺失或写盘失败都静默跳过 —— 取证绝不掩盖原始
    TranslationError(fail-closed 语义不变)。
    """
    if not outputs_root:
        return
    try:
        # 文件名含微秒(审查 §5.6):同一秒内两次快速失败不互相覆盖
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        diag_dir = Path(outputs_root) / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "node": "node_16a_translate_and_check",
            "translator_id": translator_id,
            "prompt_version": prompt_version,
            "segments_count": segments_count,
            "fingerprint": fingerprint,
            "trace": trace,
        }
        (diag_dir / f"translation-failure-{stamp}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — 取证失败不得影响原异常的传递
        return


def node_16a_translate_and_check(state: WorkflowState) -> dict:
    """LangGraph 节点:翻译英文字幕 + 写 marker + 写英文分支草稿 + 写 SRT。

    Args:
        state: 需含 ``subtitle_segments_zh``(兼容 ``asr_segments_zh``)/
            ``draft_dir_en_branch``。

    Returns:
        dict,只含变更字段 — 避免 fan-in 时与其他分支并发写同一字段:
        - ``subtitle_segments_en``: 英文字幕段
        - ``subtitle_srt_path``: 英文 SRT 路径
        - ``subtitle_translation_status`` / ``subtitle_translator_id`` /
          ``subtitle_translation_fingerprint``: 翻译状态三元组(门禁消费)
        - ``status_log``: 仅 delta(由 reducer ``_append_unique`` 合并)

    Raises:
        TranslationError: 真实翻译失败(fail-closed,不写 marker/state);
            上抛前 best-effort 把 trace 取证落盘
            ``<storyline_outputs_root>/diagnostics/``。
    """
    # 阶段 2(2026-11-09 效果缺失修复):翻译源 = node_08 产出的中文旁白字幕段
    # (subtitle_segments_zh,时间取自当前草稿,英文字幕/配音随其同步);旧线程
    # checkpoint 残留的 asr_segments_zh 按兼容语义兜底(B1 移除后新流程不再产出)。
    segments_zh = list(
        state.get("subtitle_segments_zh") or state.get("asr_segments_zh") or []
    )
    if not segments_zh:
        # B1 ASR 移除(2026-11-08)后"无源字幕"是英文分支的**常态输入**:
        # 正常跳过而非错误(不写 error_log,不污染排障日志)。
        return {
            "subtitle_translation_status": STATUS_SKIPPED,
            "status_log": ["skipped_no_source_subtitles"],
        }

    draft_dir = state.get("draft_dir_en_branch")
    if not draft_dir:
        return {
            "subtitle_translation_status": STATUS_SKIPPED,
            "error_log": ["[node_16a] draft_dir_en_branch 缺失,跳过翻译"],
            "status_log": ["node_16a_translate_skipped"],
        }

    draft_dir_p = Path(draft_dir)

    # ----- 英文检索参考(2026-11-20 第九期,审查修订版 §7.4)-----
    # 读取失败/无效一律 (None, []) → 走无参考旧行为;reference 是**单次调用
    # 参数**,不落共享客户端状态(审查 P1-06)。空参考哈希=sha256(""),保证
    # 「有参考」与「无参考」是不同指纹(审查 §7.5)。
    reference, reference_sources = load_web_topic_en_reference(state)
    reference_sha256 = hashlib.sha256(
        (reference or "").encode("utf-8")
    ).hexdigest()

    # ----- 翻译器身份与指纹(marker 复用判定,修订计划 §5.5)-----
    client = get_default_client()
    is_real = isinstance(client, LLMTranslateClient)
    translator_id = client.translator_id if is_real else "mock"
    prompt_version = client.prompt_version if is_real else TRANSLATION_PROMPT_VERSION
    fingerprint = _compute_source_fingerprint(
        segments_zh,
        translator_id=translator_id,
        prompt_version=prompt_version,
        reference_sha256=reference_sha256,
    )
    status = STATUS_REAL if is_real else STATUS_STUB

    # ----- 翻译(marker 全维度命中才跳过;旧 list/指纹不符一律重翻)-----
    if _marker_exists(
        draft_dir_p,
        expected_count=len(segments_zh),
        fingerprint=fingerprint,
        translator_id=translator_id,
        prompt_version=prompt_version,
        reference_sha256=reference_sha256,
    ):
        # 已有有效 marker,跳过翻译调用,直接读 marker 内上次译文
        # (2026-11-22 SDK 链转正:legacy 读草稿路径已退役,见 _read_segments_from_marker)
        segments_en = _read_segments_from_marker(draft_dir_p)
    else:
        # 真实客户端失败 → TranslationError 上抛(fail-closed,marker 未写);
        # 上抛前先落盘取证(trace.raw_head = 模型原始输出,2026-11-20 形状
        # 漂移事故的排障证据),取证失败不影响异常传递。
        try:
            translated = translate_segments(segments_zh, reference=reference)
        except TranslationError as exc:
            _dump_translation_failure(
                state.get("storyline_outputs_root"),
                trace=exc.trace,
                translator_id=translator_id,
                prompt_version=prompt_version,
                segments_count=len(segments_zh),
                fingerprint=fingerprint,
            )
            raise
        # 同步 start_ms / end_ms(translate_client 可能不返回)
        for i, (src, dst) in enumerate(zip(segments_zh, translated)):
            dst.setdefault("start_ms", src.get("start_ms", 0))
            dst.setdefault("end_ms", src.get("end_ms", 0))
            dst.setdefault("index", src.get("index", i))
        # 2026-11-22 SDK 链转正:legacy 的 _write_marker(往 draft
        # materials.texts[i].text_en 补英文)已退役 —— 该通道只服务已退休的
        # node_16 草稿回读路径;英文文本经 state.subtitle_segments_en 由
        # node_08b 发射进 spec。翻译指纹 marker(_save_marker)保留。
        _save_marker(
            draft_dir_p,
            translated,
            fingerprint=fingerprint,
            translator_id=translator_id,
            prompt_version=prompt_version,
            reference_sha256=reference_sha256,
            reference_sources=reference_sources,
        )
        segments_en = translated

    # ----- 写 SRT -----
    srt_path = write_srt_from_segments(draft_dir_p, segments_en)

    return {
        "subtitle_segments_en": list(segments_en),
        "subtitle_srt_path": srt_path,
        "subtitle_translation_status": status,
        "subtitle_translator_id": translator_id,
        "subtitle_translation_fingerprint": fingerprint,
        "status_log": ["node_16a_translate_done"],
    }

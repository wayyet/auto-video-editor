"""本地化 TimeLine 算法(plan_v4 §5 阶段 5)。

vendored ``openstoryline.nodes.core_nodes.plan_timeline_pro.TimeLine`` 实现复杂
(700+ 行 + numpy 加速 + PlanTimelineProConfig 配置),主 venv 不能装 torch,本
地化为**纯 Python 简化版**:保留核心决策逻辑,放弃 numpy 加速和富参数
tuning,只保证 produce 出 ``CanonicalTimeline`` 兼容 dict 的 clip 时长分配。

保留决策:
1. **TTS 驱动优先**:有配音时长 → 按 group 平分给各 clip(不强行等比裁剪,
   避免音画脱节)。写 warning 而非抛。
2. **BGM 节拍对齐**:无 TTS + 有 BGM → clip 时长对齐到 BGM 节拍等距点
   (``beat_period_ms`` 决定周期)。
3. **target_duration 兜底**:无 TTS + 无 BGM → 等比缩放到目标总时长。
4. **总长超限告警**:超 5% 时写 warning(由 qa_gate 决定是否 retry)。

不再做的事(对照 vendored 的 4 个 sub-algorithm):
- 不实现 ``edit_meterial_durations_tts`` 的 paragraph 内部时间分配(只平分)。
- 不实现 ``edit_meterial_durations_beats`` 的逐帧对齐(只等距切分)。
- 不引入 random.randint 的 group margin(避免不可重现)。
- 不读 ``PlanTimelineProConfig``(主项目无该 toml)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TimeLine:
    """简化版 TimeLine(plan_v4 §5 阶段 5 决策)。"""

    DEFAULT_MIN_CLIP_DURATION_MS: int = 500
    DEFAULT_MAX_DURATION_MS: int = 35_000

    def distribute(
        self,
        *,
        groups: list[dict[str, Any]],
        tts_res: list[dict[str, Any]],
        bgm: Optional[dict[str, Any]],
        targets: dict[str, Any],
    ) -> dict[str, Any]:
        """把 group → clip 时长分配方案,返回可写进 ``CanonicalTimeline.clips`` 的
        数组 + 元信息。

        Args:
            groups: ``group_clips`` 输出;每组 ``clips`` 数组是 group 内镜头。
                每个 clip 含 ``clip_id`` / ``media_id`` / ``source_in_ms`` /
                ``source_out_ms``。
            tts_res: ``generate_voiceover`` 输出 ``voiceover`` 数组(每段
                ``duration_ms``)。可空。
            bgm: ``select_bgm`` 输出;有 ``beats`` / ``beat_period_ms`` /
                ``duration_ms`` 时启用节拍对齐。可空。
            targets: ``{"target_duration_ms": int, "min_clip_duration_ms": int?}``。

        Returns:
            ``{"clips": [{clip_id, timeline_in_ms, timeline_out_ms, ...}, ...],
               "total_ms": int, "method": str, "warnings": [str, ...],
               "voiceover_id_map": {group_id: voiceover_id}}``
        """
        warnings: list[str] = []
        target_ms = int(targets.get("target_duration_ms") or 0)
        min_clip = int(
            targets.get("min_clip_duration_ms") or self.DEFAULT_MIN_CLIP_DURATION_MS
        )
        if target_ms <= 0:
            target_ms = self.DEFAULT_MAX_DURATION_MS

        # 1. 拉平所有 clip,记录 group 边界(供 plan_timeline_pro 加 audio_meta)
        flat_clips: list[dict[str, Any]] = []
        group_boundaries: list[tuple[int, int, str]] = []  # (start_idx, end_idx, group_id)
        for g in groups or []:
            start_idx = len(flat_clips)
            for c in g.get("clips") or []:
                flat_clips.append(dict(c))
            end_idx = len(flat_clips)
            if end_idx > start_idx:
                group_boundaries.append(
                    (start_idx, end_idx, str(g.get("group_id") or ""))
                )

        n_clips = len(flat_clips)
        if n_clips == 0:
            return {
                "clips": [],
                "total_ms": 0,
                "method": "empty",
                "warnings": [],
                "voiceover_id_map": {},
            }

        # 2. 决策:选 3 种分配模式之一
        has_tts = bool(tts_res and any(
            int(t.get("duration", 0) or 0) > 0 for t in tts_res
        ))
        has_bgm = bool(
            bgm and int(bgm.get("beat_period_ms") or 0) > 0
        )

        voiceover_id_map: dict[str, str] = {}

        if has_tts:
            durations, method = self._distribute_by_tts(
                n_clips=n_clips,
                tts_res=tts_res,
                group_boundaries=group_boundaries,
                voiceover_id_map=voiceover_id_map,
                min_clip=min_clip,
            )
            if target_ms and sum(durations) > target_ms:
                warnings.append(
                    f"voiceover-driven timeline {sum(durations)}ms exceeds "
                    f"target {target_ms}ms; NOT trimmed to avoid voice desync "
                    f"(plan §5 阶段 5 决策 1)."
                )
        elif has_bgm:
            durations, method = self._distribute_by_beats(
                n_clips=n_clips,
                bgm=bgm or {},
                min_clip=min_clip,
            )
        else:
            durations, method = self._distribute_by_scale(
                n_clips=n_clips,
                target_ms=target_ms,
                min_clip=min_clip,
            )

        # 3. 实际累计时间戳
        clips_out: list[dict[str, Any]] = []
        t = 0
        for idx, c in enumerate(flat_clips):
            d = int(durations[idx])
            clips_out.append(
                {
                    "clip_id": c.get("clip_id"),
                    "source_media_id": c.get("media_id"),
                    "source_in_ms": int(c.get("source_in_ms", 0) or 0),
                    "source_out_ms": int(c.get("source_out_ms", 0) or 0),
                    "timeline_in_ms": t,
                    "timeline_out_ms": t + d,
                }
            )
            t += d

        # 4. 校验:1.05 阈值(plan §4.2 qa_gate 用同一阈值)
        total_ms = t
        if target_ms and total_ms > target_ms * 1.05:
            warnings.append(
                f"timeline {total_ms}ms exceeds target {target_ms}ms by > 5%; "
                f"qa_gate may retry group_clips."
            )

        return {
            "clips": clips_out,
            "total_ms": total_ms,
            "method": method,
            "warnings": warnings,
            "voiceover_id_map": voiceover_id_map,
        }

    # ------------------------------------------------------------------
    # 三种分配策略
    # ------------------------------------------------------------------
    def _distribute_by_tts(
        self,
        *,
        n_clips: int,
        tts_res: list[dict[str, Any]],
        group_boundaries: list[tuple[int, int, str]],
        voiceover_id_map: dict[str, str],
        min_clip: int,
    ) -> tuple[list[int], str]:
        """TTS 驱动:每段配音时长 ÷ 该 group clip 数 → 逐 clip 时长。"""
        durations: list[int] = [min_clip] * n_clips
        for i, t in enumerate(tts_res):
            tts_dur = int(t.get("duration", 0) or 0)
            if tts_dur <= 0 or i >= len(group_boundaries):
                continue
            start, end, group_id = group_boundaries[i]
            n = max(1, end - start)
            voiceover_id = str(t.get("voiceover_id") or f"voiceover_{i + 1:04d}")
            voiceover_id_map[group_id] = voiceover_id
            per = max(min_clip, tts_dur // n)
            for j in range(start, end):
                durations[j] = per
            # 末段若有剩余毫秒,加到最后一片避免脱音
            remainder = tts_dur - per * n
            if remainder > 0 and end - 1 >= start:
                durations[end - 1] += remainder
        return durations, "tts_align"

    def _distribute_by_beats(
        self,
        *,
        n_clips: int,
        bgm: dict[str, Any],
        min_clip: int,
    ) -> tuple[list[int], str]:
        """BGM 节拍对齐:总时长 ≈ min(BGM 时长, BGM 末节拍点)。"""
        beat_period = int(bgm.get("beat_period_ms") or 1000)
        # 计算有效总时长:取 BGM duration 的整数个节拍
        bgm_dur = int(bgm.get("duration_ms") or 0)
        if bgm_dur <= 0:
            bgm_dur = beat_period * n_clips
        n_beats_total = max(1, bgm_dur // beat_period)
        total_ms = n_beats_total * beat_period

        # 平均分配;最后一个 clip 吃剩余毫秒
        base = max(min_clip, total_ms // n_clips)
        durations = [base] * n_clips
        remainder = total_ms - base * n_clips
        if remainder > 0:
            durations[-1] += remainder
        return durations, "beat_align"

    def _distribute_by_scale(
        self,
        *,
        n_clips: int,
        target_ms: int,
        min_clip: int,
    ) -> tuple[list[int], str]:
        """无 TTS / BGM:等比到 target_ms,单片不低于 min_clip。"""
        base = max(min_clip, target_ms // n_clips)
        durations = [base] * n_clips
        remainder = target_ms - base * n_clips
        if remainder > 0:
            durations[-1] += remainder
        elif base * n_clips < target_ms:
            # min_clip 截断后总长 < 目标;接受并写 warning(由调用方 add)
            pass
        return durations, "scaled_to_target"


__all__ = ["TimeLine"]
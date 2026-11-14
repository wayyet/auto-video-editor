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
import math
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
        media_durations: Optional[dict[str, int]] = None,
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
            targets: ``{"target_duration_ms": int, "min_clip_duration_ms": int?,
            "speed_multiplier": float?}``。``speed_multiplier`` 是 2026-11-14
            创建台「快进速度(倍)」(每段最低播放倍数,1.0/缺省 = 不启用)。
        media_durations: ``{media_id: duration_ms}``(load_media 素材总时长;
            2026-11-14 快进下限扩 span 用)。None 时扩 span 只能利用同素材内
            clip 之间的空隙,末尾段按无余量处理(联调直调 distribute 的兼容态)。

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
            # 节拍对齐的"总时长权威"是 BGM 节拍序列,但目标预算与实际素材量
            # 是硬上限(2026-11-08 实机回归:回退 BGM 库条目 duration_ms=120000
            # / beat_period_ms=470 → 255×470=119850ms,单 clip 全吃,时间轴
            # 119.85s >> 35s 目标;素材 30s @ speed 1.0 铺满 2 分钟,剪映里
            # 播到 30s 就没画面)。取 min(target, Σ素材跨度) 封顶。
            src_sum_ms = sum(
                max(
                    0,
                    int(c.get("source_out_ms", 0) or 0)
                    - int(c.get("source_in_ms", 0) or 0),
                )
                for c in flat_clips
            )
            cap_ms = min(target_ms, src_sum_ms) if src_sum_ms > 0 else target_ms
            durations, method = self._distribute_by_beats(
                n_clips=n_clips,
                bgm=bgm or {},
                min_clip=min_clip,
                max_total_ms=cap_ms,
            )
        else:
            durations, method = self._distribute_by_scale(
                n_clips=n_clips,
                target_ms=target_ms,
                min_clip=min_clip,
            )

        # 2.4 用户快进下限(2026-11-14 创建台「快进速度(倍)」,智能保同步):
        # speed = 源跨度/目标跨度 < N 的段,优先在同一素材内向后扩 source_out
        # 提速度 —— 段时长/时间戳/配音全部不动(快进的本质:同样时间展示更多
        # 内容)。素材余量不足时:TTS 段保持原速并告警(护配音同步),非 TTS
        # 段交给 2.5 的抬高下限压短兜底。
        user_floor = self._parse_speed_floor(targets)
        if user_floor is not None:
            self._widen_source_spans(
                flat_clips=flat_clips,
                durations=durations,
                media_durations=media_durations,
                floor=user_floor,
                warnings=warnings,
                warn_unmet=has_tts,
            )

        # 2.5 逐片段源跨度护栏(2026-11-11 模糊修复阶段3):任何分配模式产出
        # 的目标时长都不得超过 源跨度/MIN_CLIP_SPEED(允许慢动作的下限)。
        # 短素材被均分/节拍对齐拉长到 13 倍(实机:0.883s → 11.593s)会停帧/
        # 重复帧,禁止;超出部分重分配给仍有余量的段,余量也不足时接受总长
        # 短于计划并告警 —— 不注水、不虚标 speed。
        # 2026-11-14:非 TTS 模式把下限抬到用户快进倍数(扩 span 后仍不足的段
        # 压短时长兜底);TTS 模式保持 0.5 权威不动 —— 段时长=配音时长,压短
        # 会把配音挤到后续画面(音画错位)。
        durations, material_budget_ms = self._apply_source_span_caps(
            durations=durations,
            flat_clips=flat_clips,
            warnings=warnings,
            min_speed_floor=None if has_tts else user_floor,
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
        if target_ms and material_budget_ms is not None and material_budget_ms < target_ms:
            # 阶段3:总素材在允许慢动作下都填不满目标时长 —— 必须显式告警,
            # 由上游缩短目标时长/更换素材,禁止延长片段冒充有效画面。
            warnings.append(
                f"素材总跨度不足以填满目标时长(允许慢动作下最多 "
                f"{material_budget_ms}ms < 目标 {target_ms}ms),已按逐段上限"
                "分配不注水;请缩短目标时长或更换素材"
            )

        return {
            "clips": clips_out,
            "total_ms": total_ms,
            "method": method,
            "warnings": warnings,
            "voiceover_id_map": voiceover_id_map,
        }

    # ------------------------------------------------------------------
    # 用户快进下限(2026-11-14 创建台「快进速度(倍)」,智能保同步)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_speed_floor(targets: dict[str, Any]) -> float | None:
        """解析 ``targets.speed_multiplier``;仅 >1 返回有效下限,否则 None。

        非法值(非有限数/≤1/缺省 1.0)一律视为"不启用",与
        ``nodes.storyline._common.speed_multiplier`` 的解析纪律一致(本层在
        capabilities 层,不反向依赖 nodes,故本地重复这份小解析)。
        """
        raw = (targets or {}).get("speed_multiplier")
        try:
            value = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 1.0:
            return None
        return value

    def _widen_source_spans(
        self,
        *,
        flat_clips: list[dict[str, Any]],
        durations: list[int],
        media_durations: Optional[dict[str, int]],
        floor: float,
        warnings: list[str],
        warn_unmet: bool,
    ) -> None:
        """快进下限的第一优先手段:向后扩 ``source_out``(原地改,不返回)。

        对 ``speed = 源跨度/目标跨度 < floor`` 的 clip,把 source_out 向后扩到
        ``ceil(floor × 目标跨度)`` —— 段时长/时间戳/配音全部不动,仅"多吃"
        同一素材内的后续画面。扩展硬上界 = 同 media 内按 ``source_in`` 排序的
        下一个 clip 的 ``source_in``(绝不吞并别人已选的取材区间)与该素材
        ``duration_ms`` 的较小者。扩不够的段**不改时长**:TTS 模式(``warn_unmet``)
        写告警保原速护配音;非 TTS 模式由 ``_apply_source_span_caps`` 的抬高
        下限压短兜底。仅改 ``distribute`` 内部的 dict 副本,不污染调用方 groups。
        """
        if floor <= 1.0 or not flat_clips:
            return
        # 每 clip 的扩展硬上界:同素材内 source_in 排序的下一个 clip 的起点;
        # 素材内最后一个 clip 用素材总时长兜底(media_durations 缺失则 0 = 无余量)。
        by_media: dict[str, list[int]] = {}
        for idx, c in enumerate(flat_clips):
            by_media.setdefault(str(c.get("media_id") or ""), []).append(idx)
        bounds: dict[int, int] = {}
        for indices in by_media.values():
            ordered = sorted(
                indices,
                key=lambda i: (
                    int(flat_clips[i].get("source_in_ms", 0) or 0),
                    int(flat_clips[i].get("source_out_ms", 0) or 0),
                    i,
                ),
            )
            for pos, idx in enumerate(ordered):
                if pos + 1 < len(ordered):
                    bounds[idx] = int(flat_clips[ordered[pos + 1]].get("source_in_ms", 0) or 0)
                else:
                    media_id = str(flat_clips[idx].get("media_id") or "")
                    bounds[idx] = int((media_durations or {}).get(media_id, 0) or 0)

        for idx, c in enumerate(flat_clips):
            slot = int(durations[idx])
            if slot <= 0:
                continue
            src_out = int(c.get("source_out_ms", 0) or 0)
            span = src_out - max(0, int(c.get("source_in_ms", 0) or 0))
            required = math.ceil(floor * slot)  # 向上取整:span/slot 恒 ≥ floor
            if span >= required:
                continue
            room = bounds.get(idx, 0) - src_out
            if room <= 0:
                if warn_unmet:
                    warnings.append(
                        f"clip {c.get('clip_id')} 源跨度 {span}ms 达不到快进 "
                        f"{floor}x 所需 {required}ms(同素材内无可用余量),"
                        "按原速保留(护配音同步)"
                    )
                continue
            gain = min(room, required - span)
            c["source_out_ms"] = src_out + gain
            if span + gain < required and warn_unmet:
                warnings.append(
                    f"clip {c.get('clip_id')} 源跨度扩到 {span + gain}ms 仍达不到快进 "
                    f"{floor}x 所需 {required}ms(素材余量不足),按原速保留(护配音同步)"
                )

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
        max_total_ms: int = 0,
    ) -> tuple[list[int], str]:
        """BGM 节拍对齐:总时长 ≈ min(BGM 时长, BGM 末节拍点, max_total_ms)。

        ``max_total_ms`` 是硬上限(target 预算 / Σ素材跨度的较小值):BGM 库
        条目是**元数据参考**(path 可为空、真实音频未必存在),其 duration_ms
        不能架空用户目标与素材现实;超限时向内对齐到最大的整数节拍点,保住
        "落点在节拍上"的语义。
        """
        beat_period = int(bgm.get("beat_period_ms") or 1000)
        # 计算有效总时长:取 BGM duration 的整数个节拍
        bgm_dur = int(bgm.get("duration_ms") or 0)
        if bgm_dur <= 0:
            bgm_dur = beat_period * n_clips
        n_beats_total = max(1, bgm_dur // beat_period)
        total_ms = n_beats_total * beat_period
        if max_total_ms > 0 and total_ms > max_total_ms:
            aligned = (max_total_ms // beat_period) * beat_period
            total_ms = aligned if aligned > 0 else max_total_ms

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

    # ------------------------------------------------------------------
    # 逐片段源跨度护栏(2026-11-11 模糊修复阶段3,设计文档 §4.4)
    # ------------------------------------------------------------------
    def _apply_source_span_caps(
        self,
        *,
        durations: list[int],
        flat_clips: list[dict[str, Any]],
        warnings: list[str],
        min_speed_floor: float | None = None,
    ) -> tuple[list[int], int | None]:
        """把每段目标时长钳到 ``源跨度 / 最低速度`` 并重分配溢出。

        速度语义(全链路唯一):``speed = 源跨度 / 目标跨度``。目标时长超过
        ``源跨度 / 最低速度`` 即低于允许最低速度(短素材被拉长冒充有效
        画面),先钳制;被钳掉的毫秒按**余量比例**重分配给其他段(每段同样不
        超其上限);总余量仍不够时接受总长短于计划 —— 禁止注水、禁止虚标
        speed,缺口写 warning 交给上游(替换素材/缩短成片)决策。

        ``min_speed_floor``(2026-11-14):用户快进倍数抬高生效下限
        ``max(STORYLINE_MIN_CLIP_SPEED, floor)``,扩 span 后仍不足的段在此压短
        (仅非 TTS 模式传入;TTS 段时长=配音时长,压短会音画错位)。

        返回 ``(钳制+重分配后的时长, 素材预算毫秒)``;生效下限 ``<= 0``
        视为关闭护栏(仅联调),行为与旧版一致,素材预算返回 ``None``。
        """
        from config import STORYLINE_MIN_CLIP_SPEED

        min_speed = float(STORYLINE_MIN_CLIP_SPEED)
        if min_speed_floor is not None and min_speed_floor > min_speed:
            min_speed = float(min_speed_floor)
        if min_speed <= 0 or not durations:
            return durations, None

        caps: list[int | None] = []
        for c in flat_clips:
            span = max(
                0,
                int(c.get("source_out_ms", 0) or 0)
                - int(c.get("source_in_ms", 0) or 0),
            )
            # int() 向下取整:派生 speed = span/d 永远 >= 阈值,不踩契约边界
            caps.append(int(span / min_speed) if span > 0 else None)

        clamped = [
            min(d, cap) if cap is not None else d
            for d, cap in zip(durations, caps)
        ]
        # 素材预算 = 全部段顶格(慢放到最低速度)可达的总时长,供"素材不足"
        # 告警判断(与节拍/BGM 决策无关,只看素材现实)
        material_budget_ms = sum(cap for cap in caps if cap is not None)
        deficit = sum(durations) - sum(clamped)
        if deficit <= 0:
            return clamped, material_budget_ms

        for idx, (planned, capped) in enumerate(zip(durations, clamped)):
            if capped < planned:
                clip_id = str(flat_clips[idx].get("clip_id") or f"clip_{idx + 1:04d}")
                span = max(
                    0,
                    int(flat_clips[idx].get("source_out_ms", 0) or 0)
                    - int(flat_clips[idx].get("source_in_ms", 0) or 0),
                )
                warnings.append(
                    f"clip {clip_id} 源跨度 {span}ms 不足以承载分配的 {planned}ms"
                    f"(最低速度 {min_speed},上限 {capped}ms);"
                    "超分配部分将重分配给有余量的片段,禁止停帧/注水"
                )

        headroom = [
            (cap - d) if cap is not None else 0
            for d, cap in zip(clamped, caps)
        ]
        available = sum(h for h in headroom if h > 0)
        if available <= 0:
            warnings.append(
                f"全部片段源跨度均不足以承载时间轴计划,总时长将短于计划 "
                f"{deficit}ms(素材不足,禁止延长冒充);请缩短目标时长或更换素材"
            )
            return clamped, material_budget_ms

        give_total = min(deficit, available)
        given: list[int] = [0] * len(clamped)
        allocated = 0
        for idx, h in enumerate(headroom):
            if h <= 0:
                continue
            given[idx] = give_total * h // available
            allocated += given[idx]
        # 余数按余量从大到小逐段 +1
        remainder = give_total - allocated
        if remainder > 0:
            order = sorted(
                (i for i, h in enumerate(headroom) if h > 0),
                key=lambda i: headroom[i],
                reverse=True,
            )
            for i in order[:remainder]:
                given[i] += 1

        result = [d + g for d, g in zip(clamped, given)]
        if give_total < deficit:
            warnings.append(
                f"片段余量不足以承接全部重分配,总时长将短于计划 "
                f"{deficit - give_total}ms(素材不足,禁止注水)"
            )
        return result, material_budget_ms


__all__ = ["TimeLine"]
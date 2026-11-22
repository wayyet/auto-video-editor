# 配音 SegmentOverlap 修复执行记录（console-3e866b87d5d6 遗留项 1）

- **执行日期**：2026-11-23
- **前置文档**：`交付QA修复实施执行记录_console-3e866b87d5d6_20261123.md` §5 遗留项 1
- **决策**：根治 node_08 + 编译器重叠预检 + 新任务端到端验收（三者都做）
- **状态**：代码与单测已落地；**单测未在本会话执行**，端到端验收待操作员执行

---

## 1. 根因定案（比遗留项描述更进一步）

遗留项 1 把根因定位为"node_08 配音重铺 / 上游 TTS 槽位重叠"。本次取证确认，链路是：

| 阶段 | 证据 | 数值 |
|---|---|---|
| node_05 初稿 | `outputs/console-3e866b87d5d6/drafts/draft_content.json` | 视频 4 段合计 **35.628s**，组 1 结束于 17.214s，与 TTS 时长完全吻合 |
| node_07 speed_fit | `stage0_evidence/zh/draft_content.json` 视频段 | 按目标时长（缺省 35s）**等比压缩**到 34.999999s；组 1 结束于 16.899999s |
| node_08 配音重铺 | 同上，`Voiceover_zh` 轨 | 段 1 `0 → 17214000`、段 2 `16899999 → 34999999`，**重叠 314ms** |
| node_11 编译 | checkpoint `error_log` | `SegmentOverlap('... [start: 16899999, end: 34999999]')` |

**直接原因**：`_write_zh_voiceover_track()` 按"组起点 + 配音原长"铺段，只钳制到草稿末尾，不钳制到下一段起点。node_07 压缩视频后，配音时长没有重新适配。

**不能直接裁尾的证据**：`voiceover_0001_1795288885600.wav`（24kHz/16bit/单声道，17213.5ms）最后 314ms 的 RMS 为 1522（全段 3725），50ms 窗口里 RMS 2000–3500 一直持续到倒数约 100ms，说明**尾部含人声**。另外，段 2 在旧逻辑下被草稿末尾钳掉 314ms（18414→18100ms），同样截掉了语音。

## 2. 代码改动

| 文件 | 改动 |
|---|---|
| `storyline/native_draft.py` | `build_native_audio_segment()` 新增 `speed: float = 1.0`：>1.0 时 `target=duration_us`、`source=round(duration_us*speed)`，speed 伴随素材同值。缺省 1.0 与旧行为逐字节一致（`nodes/_auto_bgm.py` 调用不受影响） |
| `nodes/node_08_add_subtitles.py` | `_write_zh_voiceover_track()`：先收集落点并按起点排序，时间槽 = `min(下一段起点, 草稿末尾) - 起点`；配音超出且倍率 ≤ `_ZH_VOICEOVER_MAX_FIT_SPEED`（1.15）时**整段加速塞入**（不丢语音，SDK `change_pitch=False` 不变调）；超出上限时退化为裁尾（旧行为）并显式记 note；时间槽 ≤0 跳过并记 note |
| `jianying_sdk/compiler.py` | 新增 `AudioClipOverlapError(ValueError)` 和 `_check_audio_overlaps()`，在 `_build_audio_clips()` 落轨前预检同轨重叠，报错带轨名、两段 audio_id、区间、重叠毫秒和上游提示。**不自动挪轨/裁剪**（避免掩盖上游缺陷）。pipeline 仍折叠为 `failed`，状态契约不变 |

事故数据验算（新逻辑）：

```
voiceover_0001  槽 16899999us  原长 17214000us  x1.01858 → 加速，止于 16899999
voiceover_0002  槽 18100000us  原长 18414000us  x1.01735 → 加速，止于 34999999
```

两段都完整保留语音，同轨不重叠。

## 3. 单测（新增 4 例，未执行）

| 文件 | 用例 |
|---|---|
| `tests/unit/test_jianying_sdk_phase2.py` | `test_compile_voiceover_overlap_raises_located_error`、`test_compile_voiceover_abutting_and_sped_up_ok` |
| `tests/unit/test_node_08_add_subtitles.py` | `test_zh_voiceover_overflow_sped_up_without_overlap`、`test_zh_voiceover_large_overflow_trimmed_without_overlap` |

存量用例 `test_zh_voiceover_clamped_to_draft_duration`（60s 配音塞 1s 槽，x60 > 上限 → 裁尾）按设计仍应通过。

## 4. 端到端验收步骤（操作员执行）

1. 运行受影响单测与全量回归（见 §5 命令），要求零新增失败。
2. 用**新 thread** 重跑同一组素材（原 `media_0005.mov` / `media_0001.mov`），不要复用 `console-3e866b87d5d6`。
3. 验收点：
   - `status_log` 含 `node_11_compile_zh:compiled` 与 `node_11_compile_en:compiled`（首编即成功，不再依赖 repair 兜底）；
   - `status_log` 含 `中文配音 voiceover_000X ... 已加速 x1.0xxx 完整塞入`；
   - `join_before_delivery` 复检 `subtitles_zh`、`transitions` 为通过，运行进入 `export_final_video`；
   - 剪映复开中文草稿：两段配音首尾相接、无重叠，结尾语音完整，字幕/转场/开场 hook 均存在。
4. 留存 QA 报告、草稿 SHA-256、spec sidecar `compile.last_result`、审计 JSONL、剪映截图。

## 5. 遗留 / 注意

1. 英文分支（node_17 英文 TTS）单段被草稿末尾钳制，可能同样截掉尾部语音；不重叠，不影响编译，可另案用同一加速策略处理。
2. 根本上 node_07 压缩视频后没有通知配音重新适配；本修复在 node_08 消化偏差（≤15%）。若目标时长与 TTS 总长差距更大，应在上游（脚本字数/TTS 语速）控制。
3. `subtitles_zh` 不在 repair 分派表内：如果以后只有字幕失败，repair 会直接判为不可修复。

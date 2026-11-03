# ASR/TTS Provider Contract 草案与字段映射(冗余模块合并 阶段 4)

> 项目:[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)
> 依据:`docs/修改文档/auto-video-editor_main_冗余模块合并设计与执行计划.md` §6.6(阶段 4,第二批)
> 基准提交:`21a0e18`(阶段 3 完成后的分支头)
> 日期:2026-11-03
> 性质:**契约草案 + 字段映射评审表 + 契约测试**;本阶段不合并任何运行时,不修改任何现有适配器(计划 §6.6 任务 4.4 约束)。

---

## 1. 交付物

| 交付物 | 位置 | 说明 |
|---|---|---|
| 契约草案(代码) | `jy_common/provider_contracts.py` | `ASRResult` / `ASRProvider` / `TTSResult` / `TTSProvider`;纯新增,无既有模块 import 它,零运行时影响 |
| 契约测试 | `tests/unit/test_provider_contracts.py` | 11 用例:契约签名/结果语义(Stub+Mock)+ 四类适配器现状形状锚定 |
| 字段映射评审表 | 本文 | 每个现有适配器 → 契约的字段/单位/错误策略映射 |

回归口径:契约测试 11/11 通过;`pyproject.toml` 零改动(G-A8,ADR-001 环境隔离未被破坏)。

---

## 2. 契约草案要点

```python
@dataclass
class ASRResult:
    artifact_path: Path | None     # 转写 JSON 产物;失败无产物时 None
    segments: list[dict[str, Any]] # 统一 {text, start_ms, end_ms}(毫秒)
    provider: str                  # "firered"/"vendored"/"stub"/"external"
    error: str | None = None       # 失败走字段,不抛异常
    duration_ms: int = 0           # 转写耗时(毫秒),不是媒体时长

class ASRProvider(Protocol):
    def transcribe(self, *, media_path: Path, output_dir: Path) -> ASRResult: ...

@dataclass
class TTSResult:
    audio_path: Path | None        # 统一音频产物字段
    duration_ms: int               # 音频实际时长(毫秒)
    provider: str
    error: str | None = None

class TTSProvider(Protocol):
    def synthesize(self, *, text: str, output_path: Path, voice: str, speed: float) -> TTSResult: ...
```

两条贯穿性决策:

1. **时长一律毫秒**(`duration_ms` / `start_ms` / `end_ms`)。仓库内两套现役单位并存(见 §3/§4 单位列),迁移时只做一次换算,契约层不再接受秒。
2. **失败用 `error` 字段表达,不抛异常**。与 `storyline_capabilities` 两个 runner 的现状一致;`jy_common` 目前用异常(`ASRUnavailable` / `TTSUnavailable`),由调用方捕获后降级。迁移时由适配层把异常转成 `error` 字段,调用方的降级行为不变。

---

## 3. ASR 字段映射

| 现有适配器(类别) | 现签名 / 输入 | 现返回 / 产物 | → 契约映射 |
|---|---|---|---|
| `jy_common/asr_client.py` `MockASRClient` / `FireRedASR2SClient`(HTTP 客户端) | `transcribe(video_path: str)`(位置参,`str`) | `list[dict]`,段字段 `{text, start_s, end_s}`(**秒**);失败抛 `ASRUnavailable` | `video_path` → `media_path`(str→Path);段键 `start_s/end_s` → `start_ms/end_ms`(×1000);异常 → `error` 字段;无产物文件 → `artifact_path=None` |
| `storyline_capabilities/asr_runner.py` `StubASRClient` / `VendoredASRClient`(子进程/Stub) | `transcribe(*, media_path: Path, output_dir: Path)`(kw-only) | `ASRResult(artifact_path: Path, segments, provider, error, duration_ms)`;产物 `output_dir/asr.json`;失败填 `error` 不抛 | **已与契约几乎一致**;差异仅 `artifact_path` 恒为 `Path`(契约放宽为可 `None`)。段字段由 funasr 产物决定,迁移时核 `start_ms/end_ms` 键名 |
| `assembly_capabilities/speech_asr.py` `speech_transcribe`(ToolResult 适配器) | `speech_transcribe(args: dict, ctx: RunContext)` | `ToolResult(text, data, artifacts)`;`data` 含 `provider="external"` / `output_json` / `segment_count` / `word_count`;**不调云端**,只落外部转写 | 输入映射:`args["input_path"]` → `media_path`、`args["output_json"]` 目录 → `output_dir`;结果映射:`data.output_json` → `artifact_path`、payload `segments` → `segments`;`[ERROR] text` → `error` 字段 |
| `src/open_storyline/nodes/core_nodes/asr_node.py`(MCP Node) | NodeState + MCP 会话(独立运行时) | NodeState 写回 | 属 ADR-001 隔离运行时,由 MCP 层对接;映射由 `tests_openstoryline` 行为测试护栏,主 venv 契约测试不 import(G-A8) |

## 4. TTS 字段映射

| 现有适配器(类别) | 现签名 / 输入 | 现返回 / 产物 | → 契约映射 |
|---|---|---|---|
| `jy_common/tts_client.py` `MockTTSClient` / `FireRedTTS2Client`(HTTP 客户端) | `synthesize(text, *, voice=None, speed=None)` | `dict {audio_path, duration_ms, voice, speed}`(**duration_ms 已是毫秒**);失败抛 `TTSUnavailable` | dict → `TTSResult`(键名 `audio_path`/`duration_ms` 直接对应);`voice`/`speed` 进入扁平签名;异常 → `error` 字段 |
| `storyline_capabilities/tts_runner.py` `StubTTSClient` 等(Stub/供应商) | `synthesize(*, text: str, wav_path: Path, params: dict)` | `TTSResult(wav_path, duration_ms, provider, error, params_used)` | `wav_path` → `output_path`;`params["voice"]/params["speed"]` → 扁平参数(其余 params 键由适配层透传);`wav_path` → `audio_path`;`params_used` 契约暂不承载,迁移时进 provider 内部日志 |
| `video_edit_capabilities/speech_synthesize.py` `speech_synthesize`(ToolResult 适配器) | `speech_synthesize(args: dict, ctx: RunContext)`;`speed` 有限正数校验、`retries` 上限 | `ToolResult`;内部实际调 `jy_common.tts_client` | `args["text"/"voice"/"speed"]` → 扁平签名;`args["output_path"]` → `output_path`;ToolResult `data` 的音频路径 → `audio_path` |
| `src/open_storyline/nodes/core_nodes/generate_voiceover.py`(MCP Node) | NodeState + Agent 上下文(独立运行时) | NodeState 写回 | 同 ASR 行:ADR-001 隔离,映射由 tests_openstoryline 护栏 |

---

## 5. 单位与语义差异速查

| 项 | jy_common ASR | storyline ASR | jy_common TTS | storyline TTS | 契约 |
|---|---|---|---|---|---|
| 段时间单位 | 秒(`start_s`) | funasr 产物(迁移时核) | — | — | **毫秒** |
| 音频时长 | — | — | `duration_ms` | `duration_ms` | `duration_ms` |
| 失败表达 | 异常 `ASRUnavailable` | `error` 字段 | 异常 `TTSUnavailable` | `error` 字段 | `error` 字段 |
| 产物路径 | 无(内存段) | `artifact_path` | `audio_path` | `wav_path` | `artifact_path` / `audio_path` |
| 缓存/单例 | `get_default_client` 单例 | `get_default_*_client` 单例 | 同左 | 同左 | 不约束(适配层自理) |

## 6. 后续迁移原则(本批次不执行)

1. 契约测试(`tests/unit/test_provider_contracts.py` 第 2 部分)钉死了四类适配器的**现状形状**;任何向契约的迁移都必须同步更新该测试与本文映射表,不允许"静默换签名"。
2. 只有在契约测试覆盖后,才允许把重复的 HTTP 传输/错误分类抽到共享层(计划任务 4.4);FireRed 客户端、子进程 runner、Mock/Stub、MCP Node、ToolResult 适配器文件本身不删除。
3. OpenStoryline 侧(MCP Node)只经 MCP 协议对接,不把 PyTorch/torchaudio/MoviePy 引入主项目(ADR-001)。
4. 阶段 5(FFmpeg/字体)与本阶段无依赖关系,可独立推进。

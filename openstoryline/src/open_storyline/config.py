# /src/open_storyline/config.py
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Optional, Literal, List
import time

try:
    import tomllib
except ImportError:
    print("Fail to import tomllib, try to import tomlis")
    import tomli as tomllib

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, computed_field, field_validator


def _resolve_relative_path_to_config_dir(v: Path, info: ValidationInfo) -> Path:
    """
    Resolve relative paths based on config.toml's directory (not cwd).

    Requires the caller to pass config_dir in model_validate(..., context={"config_dir": <Path|str>}).
    """
    ctx = info.context or {}
    base = ctx.get("config_dir")
    if not base:
        return v

    v2 = v.expanduser()
    if v2.is_absolute():
        return v2

    base_dir = Path(base).expanduser()
    return (base_dir / v2).resolve(strict=False)


def _resolve_paths_recursively(value: Any, info: ValidationInfo) -> Any:
    """
    Recursively process Path objects in container types (list/tuple/set/dict).
    """
    if value is None:
        return None

    if isinstance(value, Path):
        return _resolve_relative_path_to_config_dir(value, info)

    if isinstance(value, list):
        return [_resolve_paths_recursively(v, info) for v in value]

    if isinstance(value, tuple):
        return tuple(_resolve_paths_recursively(v, info) for v in value)

    if isinstance(value, set):
        return {_resolve_paths_recursively(v, info) for v in value}

    if isinstance(value, dict):
        return {k: _resolve_paths_recursively(v, info) for k, v in value.items()}

    return value


class ConfigBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def _resolve_all_path_fields(cls, v: Any, info: ValidationInfo) -> Any:
        # Allow explicitly disabling path resolution for specific fields:
        # Field(..., json_schema_extra={"resolve_relative": False})
        if info.field_name:
            field = cls.model_fields.get(info.field_name)
            extra = (field.json_schema_extra or {}) if field else {}
            if extra.get("resolve_relative") is False:
                return v

        return _resolve_paths_recursively(v, info)

class DeveloperConfig(ConfigBaseModel):
    developer_mode: bool = False
    print_context: bool = False

class ProjectConfig(ConfigBaseModel):
    media_dir: Path = Field(..., description="Media directory for input videos and images")
    bgm_dir: Path = Field(..., description="Background music (BGM) directory")
    outputs_dir: Path = Field(..., description="Output directory")

    @computed_field(return_type=Path)
    @property
    def blobs_dir(self) -> Path:
        return self.outputs_dir


class LLMConfig(ConfigBaseModel):
    model: str
    base_url: str
    api_key: str
    timeout: float = 30.0
    temperature: Optional[float] = None
    max_retries: int = 2


class VLMConfig(ConfigBaseModel):
    model: str
    base_url: str
    api_key: str
    timeout: float = 20.0
    temperature: Optional[float] = None
    max_retries: int = 2


class MCPConfig(ConfigBaseModel):
    server_name: str = "storyline"
    server_cache_dir: str = "./storyline/.server_cache"
    server_transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http"
    url_scheme: str = "http"
    connect_host: str = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    path: str = "/mcp"
    inline_media: Literal["auto", "always", "never"] = "auto"  # Transport policy: "auto" = infer from connect_host (127.0.0.1/localhost/::1/0.0.0.0 -> path-only); "always" = always use base64; "never" = always use path-only (e.g. Docker / LAN same-machine)

    json_response: bool = True
    stateless_http: bool = False

    timeout: int = 600

    available_node_pkgs: List[str] = []
    available_nodes: List[str] = []
    @property
    def url(self) -> str:
        return f"{self.url_scheme}://{self.connect_host}:{self.port}{self.path}"

class SkillsConfig(ConfigBaseModel):
    skill_dir: Path = Field(..., description="Skill directory.")

class PexelsConfig(ConfigBaseModel):
    pexels_api_key: str = ""

class SearchWebConfig(ConfigBaseModel):
    sites: List[str] = Field(
        default_factory=lambda: ["xiaohongshu.com", "douyin.com"],
        description="Site domains to search within via the search engine `site:` operator",
    )
    engines: List[str] = Field(
        default_factory=lambda: ["ddgs", "bing", "duckduckgo"],
        description="Search engines in priority order; falls back to the next on failure. 'ddgs' (the ddgs library) is the only one that reliably bypasses anti-bot pages",
    )
    results_per_site: int = Field(default=10, ge=1, le=20, description="Max results per site")
    timeout: float = Field(default=15.0, description="Per-request timeout in seconds")
    proxy: str = Field(default="", description="Optional HTTP proxy, e.g. http://127.0.0.1:1088 (required for duckduckgo in mainland China)")

class SplitShotsConfig(ConfigBaseModel):
    transnet_weights: Path = Field(..., description="Path to transnet_v2 weights")
    transnet_device: str = "cpu"

class UnderstandClipsConfig(ConfigBaseModel):
    sample_fps: float = 2.0
    max_frames: int = 64

class GroupClipsConfig(ConfigBaseModel):
    base_max_tokens: int = Field(default=4096, ge=256, description="Base max output token budget for group_clips")
    tokens_per_clip: int = Field(default=48, ge=0, description="Additional output token budget per selected clip")
    max_tokens_cap: int = Field(default=16384, ge=512, description="Upper bound of max output token budget")
    retry_token_step: int = Field(default=2048, ge=256, description="Token increment for each retry")
    max_parse_retries: int = Field(default=2, ge=0, le=5, description="Retry count when model output parsing fails")

class RecommendScriptTemplateConfig(ConfigBaseModel):
    script_template_dir: Path = Field(..., description="Script template directory.")
    script_template_info_path: Path = Field(..., description="Script template meta info path.")

class GenerateVoiceoverConfig(ConfigBaseModel):
    tts_provider_params_path: Path = Field(..., description="TTS provider config file path")
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)

class GenerateAITransitionConfig(ConfigBaseModel):
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)

class SelectBGMConfig(ConfigBaseModel):
    sample_rate: int = 22050
    hop_length: int = 2048
    frame_length: int = 2048

class RecommendTextConfig(ConfigBaseModel):
    font_info_path: Path = Field(..., description="Font info path.")


class PlanTimelineConfig(ConfigBaseModel):
    beat_type_max: int = 1  # Maximum beat strength to use (e.g., in 4/4: 1,2,1,3 where 1=strongest, 3=weakest)
    title_duration: int = 5000  # Title/intro duration in milliseconds
    bgm_loop: bool = True  # Allow background music looping
    min_clip_duration: int = 500  # Minimum clip duration in milliseconds

    estimate_text_min: int = 1500  # Minimum subtitle on-screen time per group without TTS (ms)
    estimate_text_char_per_sec: float = 6.0  # Estimated characters per second without TTS

    image_default_duration: int = 3000  # Default image duration in milliseconds

    group_margin_over_voiceover: int = 1000  # Visual extension beyond voiceover duration per group (ms)

class PlanTimelineProConfig(ConfigBaseModel):
    
    min_single_text_duration: int = 200  
    # Minimum duration (ms) for a single text label

    max_text_duration: int = 5000  
    # Maximum duration (ms) for a single text sentence

    img_default_duration: int = 1500  
    # Default display duration (ms) for an image clip

    min_group_margin: int = 1500  
    # Minimum time margin (ms) between consecutive text groups / paragraphs

    max_group_margin: int = 2000  
    # Maximum time margin (ms) between consecutive text groups / paragraphs

    min_clip_duration: int = 1000  
    # Minimum allowed duration (ms) for a video clip

    tts_margin_mode: str = "random"  
    # Time margin strategy between consecutive TTS segments.
    # One of: "random", "avg", "max", "min"

    min_tts_margin: int = 300  
    # Minimum margin (ms) between the end of one TTS segment and the start of the next

    max_tts_margin: int = 400  
    # Maximum margin (ms) between the end of one TTS segment and the start of the next

    text_tts_offset_mode: str = "random"  
    # Offset strategy between text appearance time and corresponding TTS start time.
    # One of: "random", "avg", "max", "min"

    min_text_tts_offset: int = 0  
    # Minimum offset (ms) between text appearance and TTS start

    max_text_tts_offset: int = 0  
    # Maximum offset (ms) between text appearance and TTS start

    long_short_text_duration: int = 3000  
    # Duration threshold (ms) used to classify text as long or short

    long_text_margin_rate: float = 0.0  
    # Relative start margin rate for long text, applied against clip start time

    short_text_margin_rate: float = 0.0  
    # Relative start margin rate for short text, applied against clip start time

    text_duration_mode: str = "with_tts"  
    # Text duration calculation mode.
    # One of: "with_tts" (align with TTS duration), "with_clip" (align with clip duration)

    is_text_beats: bool = False  
    # Whether text start time should align with detected music beats

class MCodeConfig(ConfigBaseModel):
    """mcode CLI 接入配置。

    注意:路径字段一律用 ``str`` 而不是 ``Path``。
    ``ConfigBaseModel`` 会把 ``Path`` 字段按 config 目录解析相对路径,
    ``""`` 会被解析成 config 目录 → 静默指向错误目录,很难排查。
    这里坚持用 ``str`` + 显式校验。

    字段说明:
      cwd            操作员目录(video-ops);空 = 未配置,建 agent 前报明确配置错误
      bin            mcode 可执行文件;空 = 走 ``agent_gate.agent_bin.resolve_agent_bin``
      mode           mcode 上下文模式;standard 重、lightweight 轻
      permission     mcode 权限策略。**不是硬权限边界**,只是软护栏
      max_steps      单次 mcode exec 的最大步数
      timeout_s      单次 mcode exec 的超时秒数
      history_turns  拼 prompt 时保留的完整对话轮次上限
      max_prompt_chars 拼 prompt 的字符上限
      media_root     素材白名单根;空 = 从 AUTO_VIDEO_EDITOR_DIR 推导
    """

    cwd: str = ""
    bin: str = ""
    mode: Literal["standard", "lightweight"] = "standard"
    permission: str = "smart"
    max_steps: int = 120
    timeout_s: int = 900
    history_turns: int = 10
    max_prompt_chars: int = 60000
    media_root: str = ""


class Settings(ConfigBaseModel):
    developer: DeveloperConfig
    project: ProjectConfig

    llm: LLMConfig
    vlm: VLMConfig

    local_mcp_server: MCPConfig

    skills: SkillsConfig
    search_media: PexelsConfig
    search_web: SearchWebConfig = Field(default_factory=SearchWebConfig)
    split_shots: SplitShotsConfig
    understand_clips: UnderstandClipsConfig
    group_clips: GroupClipsConfig = Field(default_factory=GroupClipsConfig)
    script_template: RecommendScriptTemplateConfig
    generate_voiceover: GenerateVoiceoverConfig
    generate_ai_transition: GenerateAITransitionConfig
    select_bgm: SelectBGMConfig
    recommend_text: RecommendTextConfig
    plan_timeline: PlanTimelineConfig
    plan_timeline_pro: PlanTimelineProConfig
    # mcode 接入:用 default_factory,没有 [mcode] 段的既有 config.toml 仍能加载
    mcode: MCodeConfig = Field(default_factory=MCodeConfig)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个 dict:override 里的同名 key 覆盖 base。

    两边都是 dict 时逐层合并(用于 [generate_voiceover.providers.minimax] 这类嵌套表)。
    """
    out = dict(base)
    for key, val in override.items():
        cur = out.get(key)
        if isinstance(val, dict) and isinstance(cur, dict):
            out[key] = _deep_merge(cur, val)
        else:
            out[key] = val
    return out


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """环境变量覆盖(优先级最高)。

    变量名与 agent_fastapi.MODEL_ENV_KEYS 保持一致:
      OPENSTORYLINE_LLM_MODEL / OPENSTORYLINE_LLM_BASE_URL / OPENSTORYLINE_LLM_API_KEY
      OPENSTORYLINE_VLM_MODEL / OPENSTORYLINE_VLM_BASE_URL / OPENSTORYLINE_VLM_API_KEY

    mcode 接入相关(只覆盖路径类字段;mode / permission / 数值项走 config 文件):
      OPENSTORYLINE_MCODE_CWD        —— 操作员目录
      OPENSTORYLINE_MCODE_BIN        —— mcode 可执行文件
      OPENSTORYLINE_MCODE_MEDIA_ROOT —— 素材白名单根

    只写其中一部分也可以(没写的沿用文件里的值)。
    """
    for section in ("llm", "vlm"):
        block = data.setdefault(section, {})
        for field_name in ("model", "base_url", "api_key"):
            val = os.getenv(f"OPENSTORYLINE_{section.upper()}_{field_name.upper()}")
            if val:
                block[field_name] = val

    mcode_block = data.setdefault("mcode", {})
    for field_name in ("cwd", "bin", "media_root"):
        val = os.getenv(f"OPENSTORYLINE_MCODE_{field_name.upper()}")
        if val:
            mcode_block[field_name] = val
    return data


def load_settings(config_path: str | Path) -> Settings:
    """加载配置。

    三层优先级(后者覆盖前者):
      1. ``config.toml``        —— 入库版本,只放占位符
      2. ``config.toml.local``  —— 同目录的本机覆盖文件(.gitignore 已排除),放真实 API Key
      3. 环境变量 ``OPENSTORYLINE_LLM_API_KEY`` / ``OPENSTORYLINE_VLM_API_KEY``

    ``config.toml.local`` 只需写要覆盖的字段,例如::

        [llm]
        api_key = "sk-..."

        [vlm]
        api_key = "sk-..."
    """
    p = Path(config_path).expanduser().resolve()
    data = tomllib.loads(p.read_text(encoding="utf-8"))

    local_path = p.with_name(p.name + ".local")  # config.toml -> config.toml.local
    if local_path.is_file():
        data = _deep_merge(data, tomllib.loads(local_path.read_text(encoding="utf-8")))

    data = _apply_env_overrides(data)
    return Settings.model_validate(data, context={"config_dir": p.parent})

def default_config_path() -> str:
    return os.getenv("OPENSTORYLINE_CONFIG", "config.toml")

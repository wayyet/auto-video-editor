"""集中配置:所有需要阶段 A 开工核实的占位值都集中在此文件。

> Week 1 交付物确认后,替换下方标注 [TODO: Week1] 的值即可,无需改动节点实现。

PostgreSQL 统一改造(方案 §4.1 / 阶段 C):
- 运行时**唯一**持久化后端是 PostgreSQL。``make_checkpointer`` 只保留 ``"memory"``
  分支(单测专用,进程内无持久化);生产用 ``graph.get_checkpointer()`` 返回的
  ``AsyncPostgresSaver``(由 asynccontextmanager 包装管理 ``setup()`` 与连接池)。
- ``CHECKPOINTER_BACKEND`` / ``CHECKPOINTER_DB_DIR`` 已删除 —— 不再有
  "按环境选后端"的机制,也不再有任何 SQLite 落盘路径。
- ``POSTGRES_URI`` 指向唯一数据库 ``video_workflow``(方案 D1)。
- ``WORKFLOW_ENV`` 保留,但**只**用于选择同步/异步执行路径
  (见 ``scripts/run_workflow.py``),不再参与存储后端选择。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# Phase 2:Storyline / OpenStoryline 集成配置(2026-09 解耦后保留项)
# ---------------------------------------------------------------------------
# 旧 STORYLINE_FIRERED_PYTHON / _ROOT / _MCP_TRANSPORT / _MCP_URL / _TOOL_TIMEOUT_S /
# _CONNECT_RETRIES / _REQUIRED_TOOLS / storyline_session_id() 全部移除;
# OpenStoryline 现走本地 uvicorn + httpx 健康检查,不再有 MCP 链路与外部路径常量。

# ---------------------------------------------------------------------------
# 2026-10 接入原"不可达节点"的开关(默认全开;设为 0 ⇒ 走向与接入前一致)
# ---------------------------------------------------------------------------
# 设置方式(PowerShell,须在 Python 进程启动前设置):
#   $env:STORYLINE_ENABLE_WEB_TOPIC = "0"
#   $env:STORYLINE_ENABLE_AI_TRANSITION = "0"
# STORYLINE_ENABLE_WEB_TOPIC:图里的路由函数在**运行时**读 ``config.XXX``
#   (单测可用 monkeypatch.setattr 改),不改变图的节点/边集合,只改变走向。
# STORYLINE_ENABLE_AI_TRANSITION 在**建图时**读取:它决定图里接不接那两个节点
#   (屏障要随之改变),所以修改后必须重启进程。
# (原 STORYLINE_ENABLE_ASR_BRANCH 已随 2026-11-08 B1 ASR 移除删除:
#  storyline_local_asr / storyline_speech_rough_cut 不再存在于图中。)
#
# ⚠️ 没有"清缓存"开关:``clean_cache`` 节点已整体移除(2026-11-04 R-10,不接图);
#    清理能力以创建台手动入口形式回归 —— Web UI「清除缓存」按钮经
#    ``POST /api/console/cache/clear`` 调 ``web/cache_cleaner.py`` 显式触发,
#    任何 ``graph.invoke()`` 都不会自动清理缓存。

# 1) auto 模式:素材加载后并行搜网页话题(storyline_search_web_topic),
#    结果以"参考标题"形式软注入文案节点的 overall;失败/无结果不影响主线。
STORYLINE_ENABLE_WEB_TOPIC: bool = (
    os.environ.get("STORYLINE_ENABLE_WEB_TOPIC", "true").lower().strip() in ("1", "true", "yes")
)

# 2) auto 模式:与 plan_timeline_pro 并排生成 AI 转场,qa_gate 之前把清单写进时间线 ai_transitions
#    (storyline_generate_ai_transition → storyline_plan_timeline_ai_transition)。建图时读取。
#    ADR-005 原为默认关闭;2026-10 按用户决定改为默认开启(当前仅 stub 客户端,不改动 clips)。
#    注意:storyline_capabilities/ai_transition_client.py 里还有一份同名读取
#    (ENABLE_AI_TRANSITION),两处默认值必须一致,tests 里有对齐断言。
STORYLINE_ENABLE_AI_TRANSITION: bool = (
    os.environ.get("STORYLINE_ENABLE_AI_TRANSITION", "true").lower().strip() in ("1", "true", "yes")
)

# 4) 切镜检测器(2026-11-08 选项C 治本:TransNetV2 接入,提升切镜质量)。
#    auto(默认)= torch 可导入且权重存在才走 TransNetV2,否则/推理失败自动
#    回退帧差版(原因写入结果 detector_fallback,节点转 error_log,不静默);
#    transnetv2 = 强制 TransNetV2(不可用即回退并报告);frame_diff = 永远帧差。
#    权重路径可用 STORYLINE_TRANSNETV2_WEIGHTS 覆盖(默认 .storyline/models/)。
STORYLINE_SPLIT_SHOTS_DETECTOR: str = (
    os.environ.get("STORYLINE_SPLIT_SHOTS_DETECTOR", "auto").strip().lower()
)


def storyline_outputs_root() -> Path:
    """``outputs/{job_id}/`` 根目录,可被 ``AUTO_VIDEO_EDITOR_OUTPUTS_ROOT`` 覆盖。"""
    from storyline.output_isolation import DEFAULT_OUTPUTS_ROOT  # 避免循环 import

    return DEFAULT_OUTPUTS_ROOT


# ---------------------------------------------------------------------------
# 模式路由(2026-11-04 单流水线架构收敛 阶段3):auto-only
# ---------------------------------------------------------------------------
# human 模式(关卡⓪ + import_video 手动导入 + get_storyboard_plan 读盘,
# 以及 human 融合层 STORYLINE_HUMAN_FUSION/STORYLINE_FUSION_DISABLE)已整体删除。
# STORYLINE_MODE 只接受 "auto";其他值(含 "human")在 import 时 fail-fast,
# 避免旧环境变量把新图拉进已不存在的路径(评审修订版 §6 阶段3 任务 1)。
STORYLINE_MODE: str = os.environ.get("STORYLINE_MODE", "auto").lower().strip()
if STORYLINE_MODE != "auto":
    raise ValueError(
        f"STORYLINE_MODE 只支持 'auto'(human 模式已删除),当前 {STORYLINE_MODE!r};"
        "请清除 STORYLINE_MODE 环境变量或显式设为 auto"
    )

# ---------------------------------------------------------------------------
# Phase 4 render_video 冒烟测试旁支(对应 plan_v4 §5 阶段 6 / ADR-006)
# ---------------------------------------------------------------------------
# 默认关闭;开启后 ``storyline_render_video`` 节点会跑真实渲染并把 mp4 路径写入
# ``storyline_render_smoke_test_path``,**不**接入下游。
STORYLINE_ENABLE_RENDER_SMOKE_TEST: bool = (
    os.environ.get("STORYLINE_ENABLE_RENDER_SMOKE_TEST", "0").lower().strip()
    in ("1", "true", "yes")
)


# ---------------------------------------------------------------------------
# (vendored OpenStoryline 路径常量已删除 —— 单流水线架构 2026-11-04 阶段2:
#  消费方 asr_runner 的 vendored 分支为死链,随旧聊天运行时一并移除。
#  .venv-openstoryline/ 本地目录留待部署清理步骤由操作员处理。)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 节点 5:generate_initial_jianying_draft
# ---------------------------------------------------------------------------
# [TODO: Week1] 当前本机安装的剪映版本号;Week 2 默认走策略甲,要求 v5.9.0。
# 若版本漂移到 6.0+,resolve_strategy 会自动降级到策略乙(只读)。
JIANYING_VERSION: str = "5.9.0"

# 剪映草稿画布(竖屏 9:16,与附件 6.1 节"9:16 剪映内生成"一致)
CANVAS_WIDTH: int = 1080
CANVAS_HEIGHT: int = 1920

# ---------------------------------------------------------------------------
# 节点 10:inject_text_fx(阶段 4,2026-11-09)—— 字幕默认入场动画
# ---------------------------------------------------------------------------
# 默认入场动画名:按 templates/text_resource_library.json(VIP 库)精确匹配;
# 未命中回落库内第一个 VIP intro。旧默认("居中打字机/渐次出现/渐显/打字机 II")
# 经实证全不在库内,静默落到第一个 VIP("乱码故障"),故改为库内实证存在的
# "新年打字机"。可用环境变量覆盖。
TEXT_FX_INTRO_ANIMATION: str = os.environ.get("TEXT_FX_INTRO_ANIMATION", "新年打字机").strip() or "新年打字机"


# ---------------------------------------------------------------------------
# 阶段 B:detect_draft_encryption 用到的 capcut CLI
# ---------------------------------------------------------------------------
# [TODO: Week2 Stage B 开工实测] capcut decrypt 子命令的返回码语义
# 当前默认:returncode == 0 视为"成功解密"(即 PLAINTEXT 假阳性),
# 非 0 视为"加密"。此约定需用已知明文/加密各跑一次核实后再固化进单测 mock。
CAPCUT_DECRYPT_CMD: list[str] = ["capcut", "decrypt"]


# ---------------------------------------------------------------------------
# 持久化 checkpointer
# ---------------------------------------------------------------------------
# PostgreSQL 统一改造(方案 §4.1):运行时唯一后端是 PostgreSQL。
# - 生产:``graph.get_checkpointer()`` → AsyncPostgresSaver
# - 单测:``make_checkpointer("memory")`` → InMemorySaver(进程内,无持久化)
# SQLite 分支已删除(方案 D2):``checkpoints/`` 目录不再被任何代码写入。
#
# POSTGRES_URI:标准 ``postgresql://user:pass@host:port/dbname`` 形式
#   默认值与本地容器契约一致(edb-postgres16:postgres/devpass@5432/video_workflow),
#   保证"直接跑 Python"时也能连上本地库。
#   库名统一为 video_workflow(计划 D1)——本仓库不再使用 langgraph / auto_video_editor。
#   空值防御(2026-10-31 worker `no password supplied` 故障 P1 根因):环境变量
#   存在但为空串/纯空白时必须**回退默认值** —— ``os.environ.get(name, 默认)``
#   在"变量存在但为空"时返回空串而不是默认值,会让"没注入成功"从
#   "用默认 DSN"劣化成"拿空 DSN 连接"。
#
#   B-T3(环境变量传播链路优化):本常量改由统一解析器 db.postgres_config 取值,
#   与主图(graph.get_checkpointer)和 worker(agent_gate.runner)共用同一套
#   优先级(AGENT_GATE_POSTGRES_URI → POSTGRES_URI → 默认)与空值回退,
#   消除双份回退逻辑漂移。解析器对 config 的引用是延迟导入 + try/except:
#   本模块导入到这一行时还没定义 POSTGRES_URI,解析器的 config 档会静默跳过、
#   落到默认值(与旧 _DEFAULT_POSTGRES_URI 一致),不会循环导入。
from db.postgres_config import resolve_postgres_target as _resolve_postgres_target

POSTGRES_URI: str = _resolve_postgres_target().uri
# WORKFLOW_ENV 只用于选择同步/异步执行路径(见 scripts/run_workflow.py),
# **不再**参与存储后端选择。
WORKFLOW_ENV: str = os.environ.get("WORKFLOW_ENV", "development")  # "development" | "production"


# ---------------------------------------------------------------------------
# Week 3 — 心跳监控常量
# ---------------------------------------------------------------------------
HEARTBEAT_FILE: Path = Path(r"C:\ProgramData\VideoWorkflow\heartbeat.txt")
HEARTBEAT_INTERVAL_SECONDS: int = 10
HEARTBEAT_MONITOR_THRESHOLD_S: int = 120  # 外部监控判定心跳超时的阈值


# ---------------------------------------------------------------------------
# Week 3 — 步骤 7 护栏常量(对应原文档 3.4 节 + 4.2 节)
# ---------------------------------------------------------------------------
# P1(2026-11-08,相似功能节点计划):目标时长唯一权威定义。TARGET_DURATION_US 与
# 控制台默认值均由它派生。决定 1 选 B:node_07 运行时改读
# state.storyline_targets.target_duration_ms(创建台可调),本常量只是缺省值
# 的唯一定义点,35s 仍是缺省护栏。
DEFAULT_TARGET_DURATION_MS: int = 35_000
TARGET_DURATION_US: int = DEFAULT_TARGET_DURATION_MS * 1000  # 35s = 35,000,000 微秒(剪映内部单位)
NODE_07_MAX_RETRY: int = 3                 # 护栏节点最大重试次数
NODE_07_DEFAULT_FPS: int = 30              # 缺省 fps(段上无 fps 字段时兜底)


# ---------------------------------------------------------------------------
# Week 3 — 节点 5 的 draft_dir 解析
# ---------------------------------------------------------------------------
# 节点 5 在 graph 装配时已被 LangGraph 绑定,无法动态传参。
# 默认从 state["_draft_dir_override"] 读;缺省走 env AUTO_VIDEO_EDITOR_DRAFT_DIR;
# 再缺省走 cwd 下的 drafts/default。
# Week 3 测试通过 monkeypatch.setenv 注入;Week 4 真实部署由 main 入口设置。
DEFAULT_DRAFT_DIR_FALLBACK: str = str(Path.cwd() / "drafts" / "default")


def resolve_draft_dir(state: dict | None) -> Path:
    """从 state 读取 _draft_dir_override;缺省走 env;再缺省走 cwd 下的 drafts/default。"""
    if state and state.get("_draft_dir_override"):
        return Path(state["_draft_dir_override"])
    env_dir = os.environ.get("AUTO_VIDEO_EDITOR_DRAFT_DIR")
    if env_dir:
        return Path(env_dir)
    return Path(DEFAULT_DRAFT_DIR_FALLBACK)


# ---------------------------------------------------------------------------
# Week 3 — LangGraph 两级超时
# ---------------------------------------------------------------------------
# Week 3 补全(§11/P1-3):通过 ``monitoring.timeout_watchdog`` 接入 watchdog 线程
# 监控单节点无响应;通过 ``graph.invoke_with_total_timeout`` 接入总时长超时。
# 默认通过 ``ENABLE_TWO_LEVEL_TIMEOUT=false`` **不启用**,避免破坏现有
# 集成测试;用户按需设环境变量 ``ENABLE_TWO_LEVEL_TIMEOUT=true`` 开启。
TOTAL_EXECUTION_TIMEOUT_S: int = 600       # 图整体超时(秒),覆盖最长理论耗时
NODE_INACTIVITY_TIMEOUT_S: int = 120      # 单节点无响应超时(秒)


import os as _os_for_timeout  # 局部别名,避免污染模块顶部 import 顺序

ENABLE_TWO_LEVEL_TIMEOUT: bool = (
    _os_for_timeout.environ.get("ENABLE_TWO_LEVEL_TIMEOUT", "false").lower().strip()
    == "true"
)


# ---------------------------------------------------------------------------
# Week 4 — 翻译 / FireRed-Image-Edit / 字体常量(对齐第 4 周计划 §1.1)
# ---------------------------------------------------------------------------
# 翻译服务 endpoint(Week 4 用 Mock,Week 5 接真实服务)
TRANSLATE_MCP_ENDPOINT: str = "http://127.0.0.1:8007/mcp"

# FireRed-Image-Edit 常驻推理服务 endpoint(Week 4 用 Mock,Week 5 接真实)
FIRERED_IMAGE_EDIT_ENDPOINT: str = "http://127.0.0.1:8008/mcp"

# 字体路径(Week 4 占位 — README 标注由运维提供真实字体)
ASSETS_FONTS_DIR: Path = Path(__file__).parent / "assets" / "fonts"
ZH_FONT_NAME: str = "SourceHanSansCN-Bold.otf"
EN_FONT_NAME: str = "Roboto-Bold.ttf"


# ---------------------------------------------------------------------------
# Phase 5 — assembly QC 通道(对照 design execution plan §7 + ADR-3)
# ---------------------------------------------------------------------------
# ``ASSEMBLY_QC_GATE_ENABLED``:默认 true —— 每条视频强制走 6 节点 QC 通道
# (plan ADR-3:用户已确认接受"默认开启,每条视频强制过 QC")。设为 false 时
# ``generate_draft`` 直接接 ``node_06_human_reorder``,等价于改动前行为。
ASSEMBLY_QC_GATE_ENABLED: bool = (
    os.environ.get("ASSEMBLY_QC_GATE_ENABLED", "true").lower().strip()
    in ("1", "true", "yes")
)

# 修复循环最大重试次数(plan §7.5 / ADR-3:重试到上限后**不硬性中断**,
# 把未解决的问题如实写进 report.md,照常进入关卡①)。
ASSEMBLY_QC_MAX_RETRY: int = int(os.environ.get("ASSEMBLY_QC_MAX_RETRY", "2"))

# ---------------------------------------------------------------------------
# 9 个 MCP 工具迁移计划 阶段五(§6.5)—— repair loop 视觉证据复核
# ---------------------------------------------------------------------------
# QC escalate 时,从 preview_qc_report.json 的 blackdetect / silencedetect /
# freezedetect 日志里抽出"可疑时间段",对每段做高 fps 重采样
# (``video_watch_segment``) + 局部放大原分辨率抽帧(``video_read_frames``),
# 把证据帧落到 outputs/<job_id>/assembly/ 下供人工判读。
# 关闭 → repair loop 退回阶段五之前的纯 timeline_diff 行为。
ASSEMBLY_REPAIR_VISUAL_EVIDENCE: bool = (
    os.environ.get("ASSEMBLY_REPAIR_VISUAL_EVIDENCE", "true").lower().strip()
    in ("1", "true", "yes")
)

# 一次修复循环最多复核几个可疑窗口(每个窗口 = 1 次 watch_segment + 1 次 read_frames)
ASSEMBLY_REPAIR_MAX_WINDOWS: int = int(os.environ.get("ASSEMBLY_REPAIR_MAX_WINDOWS", "3"))

# 可疑片段重采样帧率(帧率越高证据越密,代价是 ffmpeg 采样耗时)
ASSEMBLY_REPAIR_WATCH_FPS: float = float(os.environ.get("ASSEMBLY_REPAIR_WATCH_FPS", "8"))

# ---------------------------------------------------------------------------
# 9 个 MCP 工具迁移计划 阶段五(§6.5)—— 节点 5 素材宽高比归一化
# ---------------------------------------------------------------------------
# 混剪素材宽高比不一致时(横屏素材直接进竖屏草稿),先用
# ``video_basic_operation`` 居中裁切到画布比例,避免草稿里被拉伸变形。
# 只在比例真的不一致时触发(比例已对齐 → 零开销直接放行)。
DRAFT_SOURCE_NORMALIZE_ENABLED: bool = (
    os.environ.get("DRAFT_SOURCE_NORMALIZE_ENABLED", "true").lower().strip()
    in ("1", "true", "yes")
)

# 比例容差:|src_ratio / canvas_ratio - 1| 超过此值才判定为"需要裁切"。
# 0.02 ≈ 2%,足以吃掉编解码导致的亚像素误差,又不误伤 4:3 / 16:9 的真实差异。
DRAFT_SOURCE_ASPECT_TOLERANCE: float = float(
    os.environ.get("DRAFT_SOURCE_ASPECT_TOLERANCE", "0.02")
)

# ---------------------------------------------------------------------------
# 9 个 MCP 工具迁移计划 阶段四(§7.1)—— node_08 落地 scout 样式建议
# ---------------------------------------------------------------------------
# ``subtitle_scout`` 会按实际画面给出两条建议:字号偏大/偏小,以及字幕带背景
# 对比度够不够。node_08 把这两条建议换算成剪映 ``texts[].style`` 里对应字段,
# 让侦察结论真正落进草稿,而不只是记一份报告。
# 关闭 → 退回阶段四之前的纯记录行为(只写 state.subtitle_scout_report,样式不动)。
SUBTITLE_SCOUT_STYLE_ENABLED: bool = (
    os.environ.get("SUBTITLE_SCOUT_STYLE_ENABLED", "true").lower().strip()
    in ("1", "true", "yes")
)


def make_checkpointer(backend: str = "memory", thread_id: str = "default"):
    """按 backend 选 checkpointer(PostgreSQL 统一改造方案 §4.1)。

    - "memory":  InMemorySaver(进程内,无持久化)—— **单测专用**
    - "postgres": 不走本工厂;生产请用 ``graph.get_checkpointer()``

    SQLite 分支已删除(方案 D2):运行时不再有任何本地 ``.sqlite`` 落盘路径。

    Args:
        backend: 目前只有 ``"memory"``(生产路径不走本工厂)。
        thread_id: 仅保留以兼容旧调用签名;memory 模式下忽略。

    Returns:
        兼容 LangGraph compile(checkpointer=...) 的 saver 实例。

    Raises:
        ValueError: 未知 backend。
        RuntimeError: backend="postgres" —— 提示改用 ``graph.get_checkpointer()``。
    """
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()

    if backend == "postgres":
        raise RuntimeError(
            "backend='postgres' 不应直接通过 make_checkpointer 调用 — "
            "生产路径请用 graph.get_checkpointer() 异步 context manager,"
            "由后者负责 AsyncPostgresSaver.setup() 与连接池管理。"
            "如果只是想在测试中拿到一个 saver,可用 make_checkpointer('memory')。"
        )

    raise ValueError(
        f"Unknown checkpointer backend: {backend!r} "
        "(可用: 'memory';生产用 graph.get_checkpointer())"
    )


# ---------------------------------------------------------------------------
# 工作流创建台 Console 配置(第二期 /console,阶段 0 契约冻结)
# ---------------------------------------------------------------------------
# 契约文档:docs/第二期开发/auto-video-editor_工作流创建台_阶段0_基线与契约冻结_20261105.md
# 读取纪律:web/console_* 模块在**函数运行时**读这些常量(与 STORYLINE_ENABLE_* 同风格),
# 单测用 monkeypatch.setattr(config, "CONSOLE_XXX", ...) 注入,不在 import 期固化。
# 空值防御(2026-10-31 worker `no password supplied` 故障同款):env 变量存在但为
# 空串/纯空白时一律回退默认值,避免"没注入成功"劣化为"拿到空值"。


def _console_env(name: str, default: str) -> str:
    """读 env;空串/纯空白回退默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _console_env_int(name: str, default: int) -> int:
    try:
        return int(_console_env(name, str(default)))
    except ValueError:
        return default


def _console_env_float(name: str, default: float) -> float:
    try:
        return float(_console_env(name, str(default)))
    except ValueError:
        return default


def _console_env_bool(name: str, default: bool) -> bool:
    raw = _console_env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


# 任务根目录:outputs/<thread_id>/…(已在 .gitignore:38)。默认仓库根下 outputs/。
CONSOLE_OUTPUTS_ROOT: Path = Path(
    _console_env("CONSOLE_OUTPUTS_ROOT", str(Path(__file__).resolve().parent / "outputs"))
)

#: 单次上传视频数量上限
CONSOLE_MAX_VIDEOS: int = _console_env_int("CONSOLE_MAX_VIDEOS", 10)

#: 单个视频文件大小上限(默认 2 GiB)
CONSOLE_MAX_VIDEO_BYTES: int = _console_env_int("CONSOLE_MAX_VIDEO_BYTES", 2 * 1024**3)

#: 用户提示词长度上限(strip 后字符数)
CONSOLE_PROMPT_MAX_CHARS: int = _console_env_int("CONSOLE_PROMPT_MAX_CHARS", 4000)

#: ffprobe 视频探测超时(秒)
CONSOLE_FFPROBE_TIMEOUT_SEC: float = _console_env_float("CONSOLE_FFPROBE_TIMEOUT_SEC", 30.0)

#: 目标时长默认值/上限(毫秒);上限 10 分钟,防御性配置;默认值派生自 DEFAULT_TARGET_DURATION_MS
CONSOLE_TARGET_DURATION_MS_DEFAULT: int = _console_env_int("CONSOLE_TARGET_DURATION_MS_DEFAULT", DEFAULT_TARGET_DURATION_MS)
CONSOLE_TARGET_DURATION_MS_MAX: int = _console_env_int("CONSOLE_TARGET_DURATION_MS_MAX", 600000)

#: LLM 后端(阶段 2 起消费):空 = 未配置 → 创建台拒绝启动(不静默 Stub)。
CONSOLE_LLM_BACKEND: str = _console_env("CONSOLE_LLM_BACKEND", "")

#: 是否放行 stub 后端(生产必须 false;开发/测试显式设 true)
CONSOLE_ALLOW_STUB: bool = _console_env_bool("CONSOLE_ALLOW_STUB", False)

#: 活跃工作流子进程上限(阶段 2 起消费;超限 409)
CONSOLE_MAX_CONCURRENT_RUNS: int = _console_env_int("CONSOLE_MAX_CONCURRENT_RUNS", 2)

#: 运行超时收割(秒;0 = 不启用,阶段 5)。惰性执行:由创建/恢复/查询路径触发;
#: 超时按硬失败处理(树杀 + console_run.json 置 failed)。
CONSOLE_RUN_TIMEOUT_SEC: float = _console_env_float("CONSOLE_RUN_TIMEOUT_SEC", 0.0)

#: 「清除缓存」按钮的冷却限流(秒;0 = 不限流)。防止误连点造成破坏性 IO 风暴。
CONSOLE_CACHE_CLEAR_COOLDOWN_SEC: float = _console_env_float(
    "CONSOLE_CACHE_CLEAR_COOLDOWN_SEC", 30.0
)


# ---------------------------------------------------------------------------
# 剪映草稿发布注册(2026-11-06 人工关卡与剪映草稿导入 阶段 1)
# ---------------------------------------------------------------------------
# 计划 §8 阶段 1:让工作流生成的草稿出现在剪映客户端首页,且后续节点与人工关卡
# 读写同一份权威草稿。分层:
#   staging(任务目录 drafts/)→ publish(draft_publisher)→ 正式草稿库(JIANYING_DRAFT_ROOT)
# 读取纪律与 CONSOLE_* 相同:web/nodes 在**函数运行时**读这些常量,
# 单测用 monkeypatch.setattr(config, "JY_XXX", ...) 注入,不在 import 期固化。

#: 剪映客户端用户草稿库根目录(环境变量 ``JIANYING_DRAFT_ROOT`` 可覆盖)。
#: 默认值 = 剪映 Windows 版标准草稿库;阶段 0 诊断确认本机该目录存在但为空
#: (剪映 5.9.0.11632)。**不**把 JIANYING_VERSION="5.9.0" 当作"运行环境已验证"
#: 的证据 —— 目录结构兼容性以阶段 5 实机验收为准。
JIANYING_DRAFT_ROOT: Path = Path(
    _console_env(
        "JIANYING_DRAFT_ROOT",
        os.path.expandvars(
            r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft"
        ),
    )
)

#: 是否在 generate_draft 写完 staging 后发布到剪映草稿库(默认开;单测/离线
#: 环境可设 0 关闭,关闭时行为与发布功能上线前完全一致)。
JY_DRAFT_PUBLISH_ENABLED: bool = _console_env_bool("JY_DRAFT_PUBLISH_ENABLED", True)

#: 发布/注册时检测到剪映进程在跑 → 拒绝发布(fail-closed)。发布目标是剪映
#: 自己的用户草稿库,客户端打开期间写入正是计划 §8 阶段 3 要消灭的竞争场景;
#: 发布侧先按严格模式实现,与阶段 3 的写入门禁同向。
JY_PUBLISH_FAIL_ON_JIANYING_RUNNING: bool = _console_env_bool(
    "JY_PUBLISH_FAIL_ON_JIANYING_RUNNING", True
)

# ---------------------------------------------------------------------------
# 剪映写入互斥(2026-11-06 阶段 3:人工编辑与工作流恢复的文件安全)
# ---------------------------------------------------------------------------
#: 工作流节点写草稿(safe_write_draft)时检测到剪映在跑 → **失败**而不是告警
#: 继续(计划 §8 阶段 3:"生产模式下……应暂停或失败,不应静默继续")。
#: 剪映进程检测本身失败(工具缺失等)按"不在跑"处理,不会误伤。
JY_WRITE_FAIL_ON_JIANYING_RUNNING: bool = _console_env_bool(
    "JY_WRITE_FAIL_ON_JIANYING_RUNNING", True
)

#: 恢复人工关卡时要求剪映已关闭(恢复侧门禁;人工编辑期间允许剪映打开)。
#: 恢复前同时校验草稿文件存在/JSON 可解析/mtime 合理,并对人工修改后的草稿
#: 计算 SHA-256 写入任务 manifest(审计)。
JY_RESUME_REQUIRE_CLOSED: bool = _console_env_bool("JY_RESUME_REQUIRE_CLOSED", True)


# ---------------------------------------------------------------------------
# 最终 MP4 导出(2026-11-07 阶段 6:可选的最终视频导出能力)
# ---------------------------------------------------------------------------
# 计划 §8 阶段 6:渲染引擎定为 **FFmpeg**(决策:剪映客户端渲染需 GUI 自动化,
# 不可脚本化且脆弱;FFmpeg 导出为"基础保真度"= 视频切割/拼接/变速/画布适配 +
# 音轨混音(音量/loop)+ 字幕 drawtext 烧录;转场按硬切近似、贴纸/视频特效/
# 花字样式不渲染 —— 差异作为 fidelity 明细写进导出报告与创建台事件,不是
# 静默假成功)。读取纪律同 CONSOLE_*/JY_*:节点在函数运行时读,单测
# monkeypatch.setattr(config, "FINAL_EXPORT_XXX", ...) 注入。

#: 导出能力总开关(可选能力,默认**关**;设 1 开启后 join_before_delivery 之后
#: 追加 export_final_video 节点渲染 MP4)。开关关闭时节点显式记
#: final_export_status="skipped" 并上报 export.skipped 事件,不静默。
FINAL_EXPORT_ENABLED: bool = _console_env_bool(
    "AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED", False
)

#: 导出输出目录;空 = 任务产物根 ``outputs/<session_id>/export/``(跟随
#: storyline_outputs_root 的 env 覆盖)。显式配置时必须是绝对目录。
FINAL_EXPORT_DIR: str = _console_env("AUTO_VIDEO_EDITOR_FINAL_EXPORT_DIR", "")

#: 视频编码参数(H.264 + AAC;yuv420p 保证播放器兼容;faststart 便于流式播放)
FINAL_EXPORT_VIDEO_CODEC: str = _console_env(
    "AUTO_VIDEO_EDITOR_FINAL_EXPORT_VIDEO_CODEC", "libx264"
)
FINAL_EXPORT_CRF: int = int(_console_env("AUTO_VIDEO_EDITOR_FINAL_EXPORT_CRF", "18"))
FINAL_EXPORT_PRESET: str = _console_env(
    "AUTO_VIDEO_EDITOR_FINAL_EXPORT_PRESET", "medium"
)
FINAL_EXPORT_FPS: int = int(_console_env("AUTO_VIDEO_EDITOR_FINAL_EXPORT_FPS", "30"))
FINAL_EXPORT_AUDIO_CODEC: str = _console_env(
    "AUTO_VIDEO_EDITOR_FINAL_EXPORT_AUDIO_CODEC", "aac"
)
FINAL_EXPORT_AUDIO_RATE: int = int(
    _console_env("AUTO_VIDEO_EDITOR_FINAL_EXPORT_AUDIO_RATE", "48000")
)
FINAL_EXPORT_AUDIO_BITRATE: str = _console_env(
    "AUTO_VIDEO_EDITOR_FINAL_EXPORT_AUDIO_BITRATE", "192k"
)

#: 导出校验的时长容差(秒):成片时长 vs 草稿 target_timerange 总和
FINAL_EXPORT_DURATION_TOLERANCE_SEC: float = float(
    _console_env("AUTO_VIDEO_EDITOR_FINAL_EXPORT_DURATION_TOLERANCE", "0.5")
)


# ---------------------------------------------------------------------------
# 可再生缓存清理清单(创建台「清除缓存」按钮手动触发;不接图)
# ---------------------------------------------------------------------------
# 移植自 2026-11-04 R-10 移除前的 config 清单(worktree baseline-cde 快照),
# 对齐技能 kuaishou-clean-cache「一类·常规再生缓存」。与旧版的关键差异:
# 路径不再硬编码盘符(仓库已从 E 盘迁至 OneDrive),改用 ``{repo_root}`` /
# ``{kuaishou_root}`` 占位符在 ``resolved_cache_paths()`` 调用时动态展开,
# 另支持 ``%ENV%`` 环境变量展开。不存在的路径静默跳过。
#
# 关键约束(技能三类·禁止删除,以下路径绝不进入本清单):
# - 剪映 ``%LOCALAPPDATA%\JianyingPro\User Data\Cache`` —— 已注入的 VIP 转场/
#   特效/花字/贴纸需重新联网下载才能渲染,用户 2026-07-04 指定禁删。
# - 剪映 ``%LOCALAPPDATA%\JianyingPro\User Data\Log`` —— 用户 2026-07-04 指定禁删。
# - ``outputs/`` 成片、任务目录、``.storyline/models``、各 venv、草稿本体。
#
# 双层结构:
# - ``CACHE_PATHS_TO_CLEAN``:简单路径(直接 Path)。
# - ``CACHE_GLOB_SPECS``:递归/通配条目(``__pycache__``、日志、剪映草稿备份)。

#: 仓库根(本文件所在目录)与 kuaishou 同级根,供清单条目动态定位
REPO_ROOT: Path = Path(__file__).resolve().parent
KUAISHOU_ROOT: Path = REPO_ROOT.parent

CACHE_PATHS_TO_CLEAN: list[str] = [
    # 系统级临时目录(低风险)
    r"%LOCALAPPDATA%\Temp\OpenStoryline",
    r"%TEMP%\jianying_workflow_tmp",
    # 剪映草稿回收站(技能一类 §2.3)
    r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\.recycle_bin",
    # 当前仓库自身
    r"{repo_root}\.pytest_cache",
    # kuaishou 同级:剪映程序日志 + 剪艾 agent boot 日志
    r"{kuaishou_root}\JianyingPro\5.9.0.11632\log",
    r"{kuaishou_root}\剪艾（剪辑agent）\win-unpacked\boot.log",
]


@dataclass(frozen=True)
class CacheGlobSpec:
    """一条递归/通配清理条目。"""

    root: str  # 支持占位符与环境变量,展开后必须存在(或本轮结果为空)
    kind: Literal["dir_recurse", "file_recurse", "dir_children", "file_pattern"]
    pattern: str  # rglob/glob/iterdir 参数
    exclude_substr: tuple[str, ...] = ()  # 路径黑名单(命中即丢弃)
    description: str = ""


CACHE_GLOB_SPECS: list[CacheGlobSpec] = [
    # 源码 __pycache__ —— 下探剪枝跳过:venv(.venv / .venv-openstoryline)、
    # .git、.worktrees(其他检出副本)、.storyline(models 禁删),以及
    # outputs/drafts/inputs 等纯内容目录(禁删成片/草稿素材,且体量大,
    # 遍历是主要耗时;新增大体量内容目录时须追加到本黑名单)。
    # 黑名单目录整棵不下沉。
    CacheGlobSpec(
        root="{repo_root}",
        kind="dir_recurse",
        pattern="__pycache__",
        exclude_substr=(
            "\\venv\\",
            "\\.venv\\",
            "\\.venv-openstoryline\\",
            "\\.git\\",
            "\\.worktrees\\",
            "\\.storyline\\",
            "\\.video_agent\\",
            "\\.docker-proxy\\",
            "\\.zcode\\",
            "\\.agents\\",
            "\\outputs\\",
            "\\drafts\\",
            "\\inputs\\",
            "\\out\\",
            "\\assets\\",
        ),
        description="源码 __pycache__ (排除 venv/.git/内容目录)",
    ),
    # 仓库 logs/*.log(start/stop/openstoryline/_sanity 日志)
    CacheGlobSpec(
        root="{repo_root}\\logs",
        kind="file_recurse",
        pattern="*.log",
        description="运行日志 *.log",
    ),
    # start_production 生成的 initial_state
    CacheGlobSpec(
        root="{repo_root}\\runtime",
        kind="file_pattern",
        pattern="initial_state_*.json",
        description="initial_state 快照",
    ),
    # 剪映草稿注入备份
    CacheGlobSpec(
        root=r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft",
        kind="file_recurse",
        pattern="*.bak",
        description="剪映草稿 .bak 备份",
    ),
    CacheGlobSpec(
        root=r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft",
        kind="dir_recurse",
        pattern=".backup",
        description="剪映草稿 .backup 目录",
    ),
]


def _expand_cache_raw(raw: str) -> Path:
    """把清单条目展开为 Path:先替换仓库锚点占位符,再展开环境变量。"""
    return Path(
        os.path.expandvars(
            raw.format(repo_root=str(REPO_ROOT), kuaishou_root=str(KUAISHOU_ROOT))
        )
    )


def _expand_spec(spec: CacheGlobSpec) -> list[Path]:
    """把一条 ``CacheGlobSpec`` 展开为扁平 ``list[Path]``。

    展开规则:
    - ``root`` 不存在 → 返回 ``[]``(静默跳过,不抛异常)。
    - ``dir_recurse``/``file_recurse``:用 ``os.walk`` 递归,**黑名单目录整棵
      剪枝不下沉**(旧实现 rglob 后过滤,会把 venv/.git 全树扫一遍,OneDrive
      下单次展开可达 10s;剪枝后亚秒级)。
    - ``dir_children``:对 ``root.iterdir()`` 取 ``is_dir()``,应用过滤。
    - ``file_pattern``:对 ``root.glob(pattern)`` 取 ``is_file()``,应用过滤。
    """
    root = _expand_cache_raw(spec.root)
    if not root.exists():
        return []

    def keep(p: Path) -> bool:
        s = str(p)
        return not any(token in s for token in spec.exclude_substr)

    if spec.kind in ("dir_recurse", "file_recurse"):
        want_dirs = spec.kind == "dir_recurse"
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # 剪枝:命中黑名单的子目录整棵跳过(补尾部 sep,让目录名本身也能
            # 命中形如 ``\\.venv\\`` 的 token);walk 内原地改 dirnames 生效。
            dirnames[:] = [
                d for d in dirnames
                if not any(token in str(Path(dirpath) / d) + os.sep for token in spec.exclude_substr)
            ]
            if want_dirs:
                out.extend(Path(dirpath) / d for d in dirnames if fnmatch.fnmatch(d, spec.pattern))
            else:
                out.extend(Path(dirpath) / f for f in fnmatch.filter(filenames, spec.pattern))
        return out
    if spec.kind == "dir_children":
        return [p for p in root.iterdir() if p.is_dir() and keep(p)]
    if spec.kind == "file_pattern":
        return [p for p in root.glob(spec.pattern) if p.is_file() and keep(p)]
    raise ValueError(f"Unknown CacheGlobSpec.kind: {spec.kind!r}")


def resolved_cache_paths() -> list[Path]:
    """把 ``CACHE_PATHS_TO_CLEAN`` + ``CACHE_GLOB_SPECS`` 扁平化为 ``list[Path]``。

    - 简单路径:展开占位符/环境变量后,只保留当下磁盘上存在的 ``Path``。
    - 递归/通配:按 ``_expand_spec`` 展开,root 不存在则返回空项。
    - 调用时才展开(``rglob`` 等),保证反映当下文件系统状态。
    """
    out: list[Path] = []
    for raw in CACHE_PATHS_TO_CLEAN:
        p = _expand_cache_raw(raw)
        if p.exists():
            out.append(p)
    for spec in CACHE_GLOB_SPECS:
        out.extend(_expand_spec(spec))
    return out

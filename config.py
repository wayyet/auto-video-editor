"""集中配置:所有需要阶段 A 开工核实的占位值都集中在此文件。

> Week 1 交付物确认后,替换下方标注 [TODO: Week1] 的值即可,无需改动节点实现。

Week 5 改动(对齐第 5 周计划 §1.3 / §4.6):
- ``make_checkpointer`` 新增 ``backend="postgres"`` 分支,生产 checkpointer 从
  SqliteSaver 切到 AsyncPostgresSaver(实际由 ``get_checkpointer()`` asynccontextmanager
  包装管理 ``setup()`` 与连接池)。
- 新增 ``WORKFLOW_ENV`` 与 ``POSTGRES_URI`` 环境变量触发 Postgres 后端。
- ``CHECKPOINTER_BACKEND`` 默认仍为 ``"sqlite"``,保证现有单测与本地开发不受影响。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# 节点 1:clean_cache 路径列表
# ---------------------------------------------------------------------------
# 对齐技能 ``kuaishou-clean-cache``(见 FireRed-OpenStoryline\.claude\skills\
# kuaishou-clean-cache\SKILL.md)的「一类·常规再生缓存」清单,补齐 auto-video-editor
# 自身 + 同级 FireRed-OpenStoryline + 共享 AppData 的可再生缓存条目。
#
# 双层结构:
# - ``CACHE_PATHS_TO_CLEAN``:简单路径(直接 ``Path`` 指向目录/文件)。
# - ``CACHE_GLOB_SPECS``:递归/通配条目(调用 ``resolved_cache_paths()`` 时才展开)。
#
# 关键约束(技能三类·禁止删除):
# - 剪映 ``%LOCALAPPDATA%\JianyingPro\User Data\Cache`` —— 已注入的 VIP 转场/特效/花字/贴纸
#   需重新联网下载才能渲染,**用户 2026-07-04 指定禁删**,绝对不进入清理清单。
# - 剪映 ``%LOCALAPPDATA%\JianyingPro\User Data\Log`` —— 用户 2026-07-04 指定禁删。
# - FireRed ``.venv``、模型权重、``outputs/`` 成片、草稿本体 等一律不进入本表。
#
# ``resolved_cache_paths()`` 在每次调用时把两类 spec 扁平化为 ``list[Path]``,
# 反映当下的文件系统状态;不在磁盘上的路径会被静默跳过(不计入 cleaned)。
CACHE_PATHS_TO_CLEAN: list[str] = [
    # 原有:系统级临时目录(低风险,保留)
    r"%LOCALAPPDATA%\Temp\OpenStoryline",
    r"%TEMP%\jianying_workflow_tmp",
    # ---- 新增:剪映草稿回收站(技能一类 §2.3,2026-07-04 已升为常规清理项) ----
    r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\.recycle_bin",
    # ---- 新增:FireRed-OpenStoryline 一类项(技能命令模板 #1/#3) ----
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\.storyline\.server_cache",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\.playwright-cli",
    r"E:\Documents\kuaishou\.playwright-cli",
    # ---- 新增:FireRed 日志 / 安装 / 测试 / 配置备份(技能命令模板 #4) ----
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\web.out.log",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\web.err.log",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\mcp.out.log",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\mcp.err.log",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\install_log.txt",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\test_result.txt",
    r"E:\Documents\kuaishou\FireRed-OpenStoryline\test_tts_result.txt",
    # ---- 新增:env_check 报告(技能命令模板 #4) ----
    r"E:\Documents\kuaishou\env_check_report.txt",
    # ---- 新增:剪映程序日志 + 剪艾 agent boot 日志(技能命令模板 #6) ----
    r"E:\Documents\kuaishou\JianyingPro\5.9.0.11632\log",
    r"E:\Documents\kuaishou\剪艾（剪辑agent）\win-unpacked\boot.log",
    # ---- 新增:auto-video-editor 自身 .pytest_cache + .docker-proxy 日志 ----
    r"E:\Documents\kuaishou\auto-video-editor\.pytest_cache",
    r"E:\Documents\kuaishou\auto-video-editor\.docker-proxy\gost.out.log",
    r"E:\Documents\kuaishou\auto-video-editor\.docker-proxy\gost.err.log",
]


# ---------------------------------------------------------------------------
# 节点 1:clean_cache 递归/通配 spec
# ---------------------------------------------------------------------------
# 简单路径放 ``CACHE_PATHS_TO_CLEAN``;以下条目需要通配/递归,调用
# ``resolved_cache_paths()`` 时按 ``kind`` 展开为扁平 ``list[Path]``。
#
# - ``dir_recurse``:对 ``root`` 做 ``rglob(pattern)``,只保留目录。
# - ``dir_children``:对 ``root`` 做 ``iterdir()``,只保留目录(不动文件,用于
#   保护 ``tmp/`` 根下 4 个模板 JSON)。
# - ``file_recurse``:对 ``root`` 做 ``rglob(pattern)``,只保留文件。
# - ``file_pattern``:对 ``root`` 做 ``glob(pattern)``,只保留文件(非递归)。
#
# ``exclude_substr`` 是路径黑名单(命中即丢弃),用于把 ``__pycache__`` 递归
# 排除在 ``.venv/`` 与 ``venv/`` 之外。
@dataclass(frozen=True)
class CacheGlobSpec:
    """一条递归/通配清理条目。"""

    root: str  # 含环境变量,展开后必须存在(或本轮结果为空)
    kind: Literal["dir_recurse", "file_recurse", "dir_children", "file_pattern"]
    pattern: str  # rglob/glob/iterdir 参数
    exclude_substr: tuple[str, ...] = ()  # 例如 ("\\venv\\", "\\.venv\\")
    description: str = ""


CACHE_GLOB_SPECS: list[CacheGlobSpec] = [
    # 技能一类 #2:源码 __pycache__ —— 排除两个 venv
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou",
        kind="dir_recurse",
        pattern="__pycache__",
        exclude_substr=("\\venv\\", "\\.venv\\"),
        description="源码 __pycache__ (排除 venv)",
    ),
    # 技能一类 #7:tmp/ 子目录(保留根下 4 个模板 json)
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou\tmp",
        kind="dir_children",
        pattern="*",
        exclude_substr=(),
        description="tmp 子目录(不动根下 4 个模板)",
    ),
    # 技能一类 #10:.DS_Store 残留
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou",
        kind="file_recurse",
        pattern=".DS_Store",
        description=".DS_Store 残留",
    ),
    # 技能一类 #11:剪映草稿注入 .bak 备份
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
    # auto-video-editor 独有:runtime/initial_state_*.json
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou\auto-video-editor\runtime",
        kind="file_pattern",
        pattern="initial_state_*.json",
        description="start_production 生成的 initial_state",
    ),
    # auto-video-editor 独有:logs/*.log
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou\auto-video-editor\logs",
        kind="file_recurse",
        pattern="*.log",
        description="start/stop/openstoryline/_sanity 日志",
    ),
    # FireRed config.toml.bak.*
    CacheGlobSpec(
        root=r"E:\Documents\kuaishou\FireRed-OpenStoryline",
        kind="file_pattern",
        pattern="config.toml.bak.*",
        description="FireRed config.toml 备份",
    ),
]


def _expand_spec(spec: CacheGlobSpec) -> list[Path]:
    """把一条 ``CacheGlobSpec`` 展开为扁平 ``list[Path]``。

    展开规则:
    - ``root`` 不存在 → 返回 ``[]``(静默跳过,不抛异常)。
    - ``dir_recurse``/``file_recurse``:对 ``root.rglob(pattern)`` 结果应用
      ``exclude_substr`` 过滤。
    - ``dir_children``:对 ``root.iterdir()`` 取 ``is_dir()``,应用过滤。
    - ``file_pattern``:对 ``root.glob(pattern)`` 取 ``is_file()``,应用过滤。
    """
    root = Path(os.path.expandvars(spec.root))
    if not root.exists():
        return []

    def keep(p: Path) -> bool:
        s = str(p)
        return not any(token in s for token in spec.exclude_substr)

    if spec.kind == "dir_recurse":
        return [p for p in root.rglob(spec.pattern) if p.is_dir() and keep(p)]
    if spec.kind == "file_recurse":
        return [p for p in root.rglob(spec.pattern) if p.is_file() and keep(p)]
    if spec.kind == "dir_children":
        return [p for p in root.iterdir() if p.is_dir() and keep(p)]
    if spec.kind == "file_pattern":
        return [p for p in root.glob(spec.pattern) if p.is_file() and keep(p)]
    raise ValueError(f"Unknown CacheGlobSpec.kind: {spec.kind!r}")


def resolved_cache_paths() -> list[Path]:
    """把 ``CACHE_PATHS_TO_CLEAN`` + ``CACHE_GLOB_SPECS`` 扁平化为 ``list[Path]``。

    - 简单路径:展开环境变量后,只保留当下磁盘上存在的 ``Path``。
    - 递归/通配:按 ``_expand_spec`` 展开,root 不存在则返回空项。
    - 调用时才展开(``rglob`` 等),保证反映当下文件系统状态。
    """
    out: list[Path] = []
    for raw in CACHE_PATHS_TO_CLEAN:
        p = Path(os.path.expandvars(raw))
        if p.exists():
            out.append(p)
    for spec in CACHE_GLOB_SPECS:
        out.extend(_expand_spec(spec))
    return out


# ---------------------------------------------------------------------------
# 节点 2:launch_openstoryline(2026-09 迁移解耦版 — 本地 uvicorn + httpx 健康检查)
# ---------------------------------------------------------------------------
# 模块名修正:FireRed 实际是 ``open_storyline.mcp.server``,不是 ``openstoryline.server``。
# 端口修正:MCP Server 默认 8001 + path /mcp + streamable-http(见 FireRed config.toml)。
# Web 端口修正:agent_fastapi 走 7860(README 266 行)。
#
# 2026-09 迁移后,MCP 子进程链路已删除;OPENSTORYLINE_CMD / OPENSTORYLINE_MCP_PORT
# 仅作向后兼容常量保留(README 仍引用),运行时由 node_02 直接 spawn uvicorn 子进程。
OPENSTORYLINE_CMD: list[str] = [
    "python", "-m", "open_storyline.mcp.server",
]
OPENSTORYLINE_MCP_PORT: int = 8001
OPENSTORYLINE_WEB_PORT: int = 7860
OPENSTORYLINE_HEALTH_TIMEOUT_S: int = 30


# ---------------------------------------------------------------------------
# Phase 2:Storyline / OpenStoryline 集成配置(2026-09 解耦后保留项)
# ---------------------------------------------------------------------------
# 旧 STORYLINE_FIRERED_PYTHON / _ROOT / _MCP_TRANSPORT / _MCP_URL / _TOOL_TIMEOUT_S /
# _CONNECT_RETRIES / _REQUIRED_TOOLS / storyline_session_id() 全部移除;
# OpenStoryline 现走本地 uvicorn + httpx 健康检查,不再有 MCP 链路与外部路径常量。

# 是否启用 AI Transition(ADR-005:默认关闭;Phase 4+ 节点已移除,保留供旧 README 引用)
STORYLINE_ENABLE_AI_TRANSITION: bool = (
    os.environ.get("STORYLINE_ENABLE_AI_TRANSITION", "0").lower().strip() in ("1", "true", "yes")
)


def storyline_outputs_root() -> Path:
    """``outputs/{job_id}/`` 根目录,可被 ``AUTO_VIDEO_EDITOR_OUTPUTS_ROOT`` 覆盖。"""
    from storyline.output_isolation import DEFAULT_OUTPUTS_ROOT  # 避免循环 import

    return DEFAULT_OUTPUTS_ROOT


# ---------------------------------------------------------------------------
# 节点 3:open_preview
# ---------------------------------------------------------------------------
# [TODO: Week1] Edge 浏览器在本机的可执行文件名;若用 Chrome 则改 "chrome"。
EDGE_BROWSER_EXECUTABLE: str = "msedge"


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
# 阶段 B:detect_draft_encryption 用到的 capcut CLI
# ---------------------------------------------------------------------------
# [TODO: Week2 Stage B 开工实测] capcut decrypt 子命令的返回码语义
# 当前默认:returncode == 0 视为"成功解密"(即 PLAINTEXT 假阳性),
# 非 0 视为"加密"。此约定需用已知明文/加密各跑一次核实后再固化进单测 mock。
CAPCUT_DECRYPT_CMD: list[str] = ["capcut", "decrypt"]


# ---------------------------------------------------------------------------
# 持久化 checkpointer
# ---------------------------------------------------------------------------
# Week 3 默认 "sqlite": 跨进程/多日挂起可恢复,详见 graph.make_checkpointer。
# 单测可显式传 InMemorySaver。
# Week 5:新增 "postgres" 后端,由 WORKFLOW_ENV=production + POSTGRES_URI 触发。
CHECKPOINTER_BACKEND: str = "sqlite"  # "memory" | "sqlite" | "postgres"
CHECKPOINTER_DB_DIR: Path = Path(__file__).parent / "checkpoints"

# Week 5:Postgres 后端环境变量
# - WORKFLOW_ENV=production 时强制用 Postgres(覆盖 CHECKPOINTER_BACKEND)
# - POSTGRES_URI:标准 ``postgresql://user:pass@host:port/dbname`` 形式
POSTGRES_URI: str = os.environ.get(
    "POSTGRES_URI",
    "postgresql://postgres:postgres@localhost:5432/video_workflow",
)
WORKFLOW_ENV: str = os.environ.get("WORKFLOW_ENV", "development")  # "development" | "production"


def resolve_checkpointer_backend() -> str:
    """按 ``WORKFLOW_ENV`` 解析实际后端:production → postgres,其它走默认。

    允许 ``CHECKPOINTER_BACKEND`` 显式覆盖(便于测试中强制用 sqlite)。
    """
    if WORKFLOW_ENV == "production":
        return "postgres"
    return CHECKPOINTER_BACKEND


# ---------------------------------------------------------------------------
# Week 3 — 心跳监控常量
# ---------------------------------------------------------------------------
HEARTBEAT_FILE: Path = Path(r"C:\ProgramData\VideoWorkflow\heartbeat.txt")
HEARTBEAT_INTERVAL_SECONDS: int = 10
HEARTBEAT_MONITOR_THRESHOLD_S: int = 120  # 外部监控判定心跳超时的阈值


# ---------------------------------------------------------------------------
# Week 3 — 步骤 7 护栏常量(对应原文档 3.4 节 + 4.2 节)
# ---------------------------------------------------------------------------
TARGET_DURATION_US: int = 35_000_000       # 35s = 35,000,000 微秒(剪映内部单位)
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
# 派生:最终访问的 URL
# ---------------------------------------------------------------------------
def openstoryline_mcp_url() -> str:
    return f"http://127.0.0.1:{OPENSTORYLINE_MCP_PORT}/mcp"


def openstoryline_web_url() -> str:
    return f"http://127.0.0.1:{OPENSTORYLINE_WEB_PORT}"


def make_checkpointer(backend: str = CHECKPOINTER_BACKEND, thread_id: str = "default"):
    """按 backend 选 checkpointer。

    - "memory":  InMemorySaver(进程内,无持久化)
    - "sqlite":  SqliteSaver(落盘 checkpoints/<thread_id>.sqlite)
    - "postgres": AsyncPostgresSaver(Week 5 新增,生产路径)

    Args:
        backend: "memory" | "sqlite" | "postgres"
        thread_id: sqlite 模式下决定落盘文件名;postgres 模式下被忽略(URI 单库)。

    Returns:
        兼容 LangGraph compile(checkpointer=...) 的 saver实例。

    Raises:
        ValueError: 未知 backend。
        RuntimeError: backend="postgres" 但 POSTGRES_URI 未设置或包未安装。
    """
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()

    if backend == "sqlite":
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver  # 需 langgraph-checkpoint-sqlite>=2.0
        CHECKPOINTER_DB_DIR.mkdir(parents=True, exist_ok=True)
        db_path = CHECKPOINTER_DB_DIR / f"{thread_id}.sqlite"
        # 直接构造 — 绕开 SqliteSaver.from_conn_string 的 context manager 包装,
        # 因为 LangGraph compile() 期望 saver 实例而非 context manager。
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        return SqliteSaver(conn)

    if backend == "postgres":
        raise RuntimeError(
            "backend='postgres' 不应直接通过 make_checkpointer 调用 — "
            "生产路径请用 monitoring.runs_table + graph.get_checkpointer() 异步 context manager,"
            "由后者负责 AsyncPostgresSaver.setup() 与连接池管理。"
            "如果只是想在测试中拿到一个 saver,可用 AsyncPostgresSaver.from_conn_string(POSTGRES_URI)。"
        )

    raise ValueError(f"Unknown checkpointer backend: {backend!r}")

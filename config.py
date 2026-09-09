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
from pathlib import Path


# ---------------------------------------------------------------------------
# 节点 1:clean_cache 路径列表
# ---------------------------------------------------------------------------
# [TODO: Week1] 核实实际安装位置(剪映默认装在 %LOCALAPPDATA%\JianyingPro\
#   User Data\Cache,但 v5.9.0 可能不同;OpenStoryline 临时目录按部署方式而异)
CACHE_PATHS_TO_CLEAN: list[str] = [
    r"%LOCALAPPDATA%\JianyingPro\User Data\Cache",
    r"%LOCALAPPDATA%\Temp\OpenStoryline",
    r"%TEMP%\jianying_workflow_tmp",
]


# ---------------------------------------------------------------------------
# 节点 2:launch_openstoryline
# ---------------------------------------------------------------------------
# [TODO: Week1] 核实 OpenStoryline 实际启动命令与端口。
# 端口需与 MCP Server / Web 前端默认配置一致,目前为占位值。
OPENSTORYLINE_CMD: list[str] = [
    "python", "-m", "openstoryline.server",
]
OPENSTORYLINE_MCP_PORT: int = 8006
OPENSTORYLINE_WEB_PORT: int = 8005
OPENSTORYLINE_HEALTH_TIMEOUT_S: int = 30


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
# Week 3 — LangGraph 两级超时(预留钩子,Week 3 不启用,Week 4 接入前核实 API)
# ---------------------------------------------------------------------------
TOTAL_EXECUTION_TIMEOUT_S: int = 600       # 图整体超时(秒),覆盖最长理论耗时
NODE_INACTIVITY_TIMEOUT_S: int = 120      # 单节点无响应超时(秒)


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


def resolved_cache_paths() -> list[Path]:
    """把 CACHE_PATHS_TO_CLEAN 中的环境变量展开为绝对路径,供节点 1 使用。"""
    return [Path(os.path.expandvars(p)) for p in CACHE_PATHS_TO_CLEAN]


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

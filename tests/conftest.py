"""共享 pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 让 `import config` 等能解析到工作目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 2026-11-06 阶段 1(人工关卡与剪映草稿导入):默认关闭"发布注册到剪映草稿库"。
# 测试进程若带着 session_id 跑图(node_05 发布 / fork 注册),会把测试草稿写进
# **本机真实剪映草稿库**(%LOCALAPPDATA%);发布注册属生产路径,测试一律显式
# 开启(monkeypatch config 或设 JY_DRAFT_PUBLISH_ENABLED=1)。
os.environ.setdefault("JY_DRAFT_PUBLISH_ENABLED", "0")


@pytest.fixture(autouse=True)
def _hermetic_anim_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """入场动画缓存守卫的封闭化(2026-11-19 动画丢失根因修复配套)。

    守卫(node_10/09b)读取本机剪映缓存 ``Cache/effect/<effect_id>/<md5>``,
    不封闭会让全套测试依赖操作机环境(有缓存=写动画,无缓存=降级,结果漂移)。
    这里把缓存根统一指到临时目录,并按真实池 JSON 的 verified 条目 + 标题动画
    候选[0] 伪造缓存目录 —— 行为等价于"资源已下载且可用"。

    ⚠️ 用 ``tmp_path_factory.mktemp`` 而不是测试级 ``tmp_path``:后者会被
    「目录清点」类测试(test_api_console_runs 等)当作环境污染计入。
    """
    import json as _json

    cache_root = tmp_path_factory.mktemp("jy-cache-effect")

    def _fabricate(effect_id: object, md5: object) -> None:
        if effect_id and md5:
            (cache_root / str(effect_id) / str(md5)).mkdir(parents=True, exist_ok=True)

    lib_path = ROOT / "templates" / "text_resource_library.json"
    try:
        intro_rows = _json.loads(lib_path.read_text(encoding="utf-8")).get("intro", [])
    except (OSError, ValueError):
        intro_rows = []
    for row in intro_rows:
        if isinstance(row, dict) and row.get("verified"):
            _fabricate(row.get("effect_id"), row.get("md5"))

    from jy_common.hook_title import HOOK_ANIMATION_CANDIDATES

    if HOOK_ANIMATION_CANDIDATES:
        _fabricate(
            HOOK_ANIMATION_CANDIDATES[0].get("effect_id"),
            HOOK_ANIMATION_CANDIDATES[0].get("md5"),
        )

    monkeypatch.setattr(
        "nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT", str(cache_root)
    )
    monkeypatch.setattr("config.JIANYING_CACHE_ROOT", str(cache_root))

    # 花字缓存守卫封闭化(2026-11-21 原生花字通道):Cache/artistEffect 独立根
    # 同样指到临时空目录 —— 否则套件依赖操作机真实花字缓存(有缓存=挂花字,
    # 无缓存=降级,结果漂移);花字相关用例自行 monkeypatch 到伪造缓存。
    flower_cache_root = tmp_path_factory.mktemp("jy-cache-artist-effect")
    monkeypatch.setattr(
        "nodes._text_flower.JIANYING_ARTIST_EFFECT_CACHE_ROOT", str(flower_cache_root)
    )
    monkeypatch.setattr("config.JIANYING_ARTIST_EFFECT_CACHE_ROOT", str(flower_cache_root))
    return cache_root


@pytest.fixture
def workspace_root() -> Path:
    return ROOT


@pytest.fixture
def tmp_draft_dir(tmp_path: Path) -> Path:
    """为节点 5 与加密检测/原子写入测试准备的临时剪映草稿目录。"""
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    return draft_dir


@pytest.fixture
def initial_state() -> dict:
    """构造一个最小可用的初始 WorkflowState。"""
    return {
        "session_id": "test-session",
        "video_input_path": str(ROOT / "tests" / "fixtures" / "30s.mp4"),
        "error_log": [],
    }

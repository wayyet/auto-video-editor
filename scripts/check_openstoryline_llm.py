"""OpenStoryline LLM/VLM 连通性自检。

用法(在仓库根目录):
    .\\openstoryline\\.venv\\Scripts\\python.exe scripts\\check_openstoryline_llm.py

它做三件事:
  1. 按「config.toml → config.toml.local → 环境变量」的顺序算出**生效**配置;
  2. 打印 model / base_url / api_key(打码)/ key 来自哪一层;
  3. 用 open_storyline 自己的 validate_api_key 真发一次请求,打印 HTTP 结果。

退出码:0 = llm 与 vlm 都通;1 = 有不通的;2 = 配置本身有问题。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Windows 默认 stdout 是 GBK，遇到中文 / 符号会报错；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENSTORYLINE_DIR = Path(os.environ.get("OPENSTORYLINE_DIR", REPO_ROOT / "openstoryline"))


def _bootstrap() -> None:
    """把 openstoryline 与其 src 挂到 sys.path,才能 import open_storyline。"""
    for p in (OPENSTORYLINE_DIR, OPENSTORYLINE_DIR / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _mask(key: str) -> str:
    if not key:
        return "(空)"
    if key.startswith("<") and key.endswith(">"):
        return f"占位符 {key}"
    return f"{key[:4]}...{key[-4:]}(len={len(key)})"


def _key_source(section: str, cfg_path: Path) -> str:
    """判断该 section 的 api_key 来自哪一层。"""
    env_name = f"OPENSTORYLINE_{section.upper()}_API_KEY"
    if os.getenv(env_name):
        return f"环境变量 {env_name}"
    local_path = cfg_path.with_name(cfg_path.name + ".local")
    if local_path.is_file():
        try:
            import tomllib

            local = tomllib.loads(local_path.read_text(encoding="utf-8"))
            if (local.get(section) or {}).get("api_key"):
                return f"文件 {local_path.name}"
        except Exception:
            pass
    return f"文件 {cfg_path.name}(通常是占位符)"


async def _check(section: str, cfg) -> tuple[bool, str]:
    from open_storyline.agent import validate_api_key

    try:
        await validate_api_key(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            provider=section.upper(),
            timeout=30.0,
        )
        return True, "OK"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e).strip()}"


def main() -> int:
    _bootstrap()
    from open_storyline.config import default_config_path, load_settings

    cfg_path = Path(default_config_path())
    if not cfg_path.is_absolute():
        cfg_path = (OPENSTORYLINE_DIR / cfg_path).resolve()

    if not cfg_path.is_file():
        print(f"✗ 找不到配置文件:{cfg_path}", file=sys.stderr)
        print("  用 $env:OPENSTORYLINE_CONFIG 指定绝对路径。", file=sys.stderr)
        return 2

    try:
        cfg = load_settings(cfg_path)
    except Exception as e:
        print(f"✗ 配置加载失败:{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    print(f"配置文件    : {cfg_path}")
    local_path = cfg_path.with_name(cfg_path.name + ".local")
    print(f"本地覆盖    : {local_path} ({'存在' if local_path.is_file() else '不存在'})")
    print()

    ok = True
    for section in ("llm", "vlm"):
        sub = getattr(cfg, section)
        print(f"[{section.upper()}]")
        print(f"  model   : {sub.model}")
        print(f"  base_url: {sub.base_url}")
        print(f"  api_key : {_mask(sub.api_key)}")
        print(f"  来源    : {_key_source(section, cfg_path)}")
        passed, detail = asyncio.run(_check(section, sub))
        print(f"  连通性  : {'OK' if passed else '失败'} — {detail}")
        ok = ok and passed
        print()

    if ok:
        print("✓ llm / vlm 都可用。启动服务前确认 key 生效(改完要重启服务)。")
        return 0

    print("✗ 有不可用的项。常见原因:")
    print("  1) api_key 还是占位符 <YOUR_..._API_KEY> —— 在 config.toml.local 里填真实 key")
    print("  2) key 过期 / 网关没订阅 —— 换 key 或充值")
    print("  3) base_url 不对 —— 换成该 key 对应的端点")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

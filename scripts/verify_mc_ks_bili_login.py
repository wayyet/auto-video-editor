"""ks/bili 准入真机验收脚本(P6 门槛4/9:扫码登录 + Profile 目录 + 会话复用)。

每次调用 = 单平台单次真实检索,走**生产同路径**
(``storyline_capabilities.mediacrawler_client.search_notes``,参数取自 config),
执行前后快照 MC ``browser_data/``,断言 ``cdp_<platform>_user_data_dir`` 生成
(该目录仅在 CDP 自启浏览器路径创建 —— 本机 MC 已固定 ``CDP_CONNECT_EXISTING=False``)。

用法(auto-video-editor 仓库根目录):

    .venv/Scripts/python.exe scripts/verify_mc_ks_bili_login.py --platform ks  --query "青岛旅游攻略"
    .venv/Scripts/python.exe scripts/verify_mc_ks_bili_login.py --platform bili --query "青岛旅游攻略"

判据:
    PASS = returncode 0 且映射记录 ≥1 且 ``cdp_<platform>_user_data_dir`` 存在
    会话复用 = 第二次运行同样 PASS 且无 AUTH_REQUIRED(不再需要扫码)

⚠️ 首次运行会弹出 Chrome 窗口,需用对应手机App扫码(默认 600s 超时窗口);
   ks/bili 必须串行执行(CDP 锁互斥),勿同时跑两个平台。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
from storyline_capabilities import mediacrawler_client as mc  # noqa: E402

_COOKIE_CANDIDATES = (
    "Default/Cookies",
    "Default/Network/Cookies",
)


def _snapshot_browser_dirs(mc_root: Path) -> set[str]:
    browser_data = mc_root / "browser_data"
    if not browser_data.is_dir():
        return set()
    return {p.name for p in browser_data.iterdir() if p.is_dir()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("ks", "bili"))
    parser.add_argument("--query", default="青岛旅游攻略")
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    platform = args.platform
    mc_root = Path(config.STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT).expanduser().resolve()
    save_root = _REPO_ROOT / "runtime" / "mc_login_verify" / platform
    save_root.mkdir(parents=True, exist_ok=True)
    expected_profile = f"cdp_{platform}_user_data_dir"

    before = _snapshot_browser_dirs(mc_root)
    print(f"[verify] platform={platform} query={args.query!r} timeout={args.timeout}s")
    print(f"[verify] MC root: {mc_root}")
    print(f"[verify] browser_data(前): {sorted(before) or '(空)'}")

    started = time.monotonic()
    records, error_code, message, diag = mc.search_notes(
        query=args.query,
        max_results=args.max_results,
        platform=platform,
        project_root=str(mc_root),
        save_root=str(save_root),
        timeout_seconds=args.timeout,
        headless=False,
    )
    elapsed = time.monotonic() - started
    after = _snapshot_browser_dirs(mc_root)
    new_dirs = sorted(after - before)

    print(f"[verify] 耗时: {elapsed:.1f}s")
    print(f"[verify] error_code={error_code!r} message={message!r}")
    print(f"[verify] returncode={diag.get('returncode')} output_file={diag.get('output_file')}")
    print(f"[verify] 记录数={len(records)}")
    if records:
        sample = records[0]
        print(f"[verify] 首条记录键: {sorted(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}")
    print(f"[verify] browser_data(后) 新增: {new_dirs or '(无)'}")

    profile_dir = mc_root / "browser_data" / expected_profile
    profile_ok = profile_dir.is_dir()
    cookie_found = any((profile_dir / c).is_file() for c in _COOKIE_CANDIDATES)
    print(f"[verify] profile {expected_profile}: {'存在' if profile_ok else '缺失'};cookies: {'找到' if cookie_found else '未找到(会话复用以第二次运行为准)'}")

    passed = (
        error_code is None
        and len(records) >= 1
        and profile_ok
    )
    print(f"[verify] 判定: {'PASS' if passed else 'FAIL'} (platform={platform})")
    if error_code is not None and diag.get("stderr_tail"):
        print(f"[verify] stderr_tail(末尾): {diag['stderr_tail'][-400:]}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""MediaCrawler 子进程适配器(2026-11-19 用户拍板接入)。

把本地 MediaCrawler 安装副本(https://github.com/NanmiCoder/MediaCrawler,
非商业学习许可;外部安装,代码不入库)当**外部命令**用:子进程跑
``main.py --type search``,读回 JSON 结果文件,返回原始记录列表;
映射成 ``search_web_topic`` 的 ``{url,title,snippet,source}`` 四键条目
由能力层完成(本模块不定义条目契约)。

分层纪律:本模块不 import config / 不读环境变量 —— 全部参数由能力层
显式传入,保持能力层"可独立 CLI/Script 跑"的既有约束。

调用形态(单一检索词,搜索模式,关评论/关二级评论/关媒体下载):

    <project_root>/.venv/Scripts/python.exe main.py \\
        --platform xhs --lt qrcode --type search --keywords <query> \\
        --crawler_max_notes_count 10 --get_comment false \\
        --get_sub_comment false --get_media false --headless 0 \\
        --save_data_option json --save_data_path <每次运行独立时间戳目录>

要点:
- **不加** ``-I``/``-E``:MediaCrawler 依赖脚本目录自身导入(main.py
  ``import config``),隔离模式会直接断掉其包导入;
- stdin=DEVNULL + UTF-8 文本模式(``errors="replace"``),与
  ``assembly_capabilities/ffproc.py`` 的 run_proc 纪律一致(Windows 父进程
  默认 ANSI 代码页,中文输出会被 GBK 解码打断);
- 登录:首次由操作员在弹出的浏览器窗口扫码,登录态缓存在
  ``<platform>_user_data_dir`` 自动复用;qrcode 登录类型在已有登录态时
  不会重复要求扫码;
- 输出读取:``<save_data_path>/<platform>/json/search_contents_*.json``
  (json 模式为 JSON 数组;glob 兜底容错命名变更);每次运行用独立时间戳
  子目录,避免与历史残留互相污染;
- 错误码复用 search_web_topic 既有域:``NETWORK_ERROR``(venv 缺失/启动
  失败/超时/非零退出)、``PARSE_ERROR``(结果文件不可读)、``NO_RESULTS``
  (进程正常退出但无结果文件/记录);diagnostics 携带 returncode 与
  stdout/stderr 尾部(≤2KiB)供产物留痕,不含 Cookie。
"""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

# 平台代码 → 中文展示名(引擎标签用;与 MediaCrawler PlatformEnum 对齐)。
MC_PLATFORM_LABELS: dict[str, str] = {
    "xhs": "小红书",
    "dy": "抖音",
    "ks": "快手",
    "bili": "B站",
    "wb": "微博",
    "tieba": "贴吧",
    "zhihu": "知乎",
}

# 平台代码白名单(能力层/测试共用;与 config._MC_PLATFORM_WHITELIST 一致,
# 不 import config,两处必须同步维护)。
MC_PLATFORM_CODES: tuple[str, ...] = tuple(MC_PLATFORM_LABELS)


def platform_label(platform: str) -> str:
    return MC_PLATFORM_LABELS.get(str(platform), str(platform))


def _resolve_python_exe(project_root: Path) -> Optional[Path]:
    """MediaCrawler 自带 .venv(uv 管理,Python 3.11);找不到返回 None。"""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        cand = project_root / rel
        if cand.is_file():
            return cand
    return None


def _tail(text: str, limit: int = 2048) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[-limit:]


def search_notes(
    *,
    query: str,
    max_results: int,
    platform: str,
    project_root: str,
    save_root: str,
    timeout_seconds: int,
    headless: bool = False,
) -> tuple[list[dict[str, Any]], Optional[str], str, dict[str, Any]]:
    """跑一次 MediaCrawler 关键词搜索,返回 ``(records, error_code, message, diagnostics)``。

    records 是 MediaCrawler JSON 结果文件里的原始记录列表(未映射、未清洗);
    ``error_code=None`` 表示成功(含 0 条 → 由调用方决定 NO_RESULTS 语义)。
    """
    diagnostics: dict[str, Any] = {
        "platform": str(platform),
        "parser_version": f"mc_{platform}_v1",
        "project_root": str(project_root),
        "headless": bool(headless),
        "timeout_seconds": int(timeout_seconds),
        "returncode": None,
        "output_file": None,
    }
    root = Path(project_root)
    if not (root / "main.py").is_file():
        return [], "NETWORK_ERROR", (
            f"MediaCrawler main.py not found under {root}; "
            "check STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT"
        ), diagnostics
    python_exe = _resolve_python_exe(root)
    if python_exe is None:
        return [], "NETWORK_ERROR", (
            f"MediaCrawler venv python not found under {root} "
            "(expected .venv/Scripts/python.exe)"
        ), diagnostics

    # 每次运行独立时间戳目录(AsyncFileWriter 会写 <save_data_path>/<platform>/json/)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = Path(save_root) / stamp
    diagnostics["run_dir"] = str(run_dir)

    cmd = [
        str(python_exe),
        "main.py",
        "--platform", str(platform),
        "--lt", "qrcode",
        "--type", "search",
        "--keywords", str(query),
        "--crawler_max_notes_count", str(max(int(max_results), 1)),
        "--get_comment", "false",
        "--get_sub_comment", "false",
        "--get_media", "false",
        "--headless", "1" if headless else "0",
        "--save_data_option", "json",
        "--save_data_path", str(run_dir),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], "NETWORK_ERROR", (
            f"MediaCrawler timed out after {timeout_seconds}s "
            "(first run needs QR scan in the popped-up browser window)"
        ), diagnostics
    except OSError as exc:
        return [], "NETWORK_ERROR", f"MediaCrawler launch failed: {exc}", diagnostics

    diagnostics["returncode"] = proc.returncode
    diagnostics["stdout_tail"] = _tail(proc.stdout)
    diagnostics["stderr_tail"] = _tail(proc.stderr)
    if proc.returncode != 0:
        return [], "NETWORK_ERROR", (
            f"MediaCrawler exited with code {proc.returncode}; see diagnostics "
            "stdout_tail/stderr_tail for details"
        ), diagnostics

    data_dir = run_dir / str(platform) / "json"
    candidates = sorted(data_dir.glob("search_contents_*.json"))
    if not candidates:
        # 兜底:结果文件命名变更时按任意 json 容错(glob 取最新)
        candidates = sorted(data_dir.glob("*.json"))
    if not candidates:
        return [], "NO_RESULTS", (
            "MediaCrawler finished (exit 0) but wrote no result file under "
            f"{data_dir}"
        ), diagnostics
    out_file = candidates[-1]
    diagnostics["output_file"] = str(out_file)

    try:
        records = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], "PARSE_ERROR", f"MediaCrawler result unreadable: {exc}", diagnostics

    if isinstance(records, dict):
        # 防御:个别版本可能包一层 {"data": [...]};认不出形状交给 PARSE_ERROR
        inner = records.get("data") or records.get("items")
        records = inner if isinstance(inner, list) else None
    if not isinstance(records, list):
        return [], "PARSE_ERROR", (
            "MediaCrawler result file is not a JSON list"
        ), diagnostics
    if not records:
        return [], "NO_RESULTS", "MediaCrawler returned an empty result list", diagnostics
    return records, None, "", diagnostics

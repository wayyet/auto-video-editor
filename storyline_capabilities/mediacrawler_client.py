"""MediaCrawler 子进程适配器(2026-11-19 用户拍板接入;2026-11-20 按审查修订版阶段2加固)。

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

审查修订版加固项(2026-11-20,对应审查文档 §7 阶段2):
- project_root/save_root 在父进程统一 ``expanduser().resolve()``(P2-03:
  子进程 ``cwd=project_root``,相对路径父子进程会按不同工作目录解释);
- ``preflight()`` 区分三类缺失(根目录/main.py/venv python),消息可读
  (P2-02 启动预检);只影响 MC 路径,不禁用 Bing 主流程;
- ``detect_revision()`` 读本地 .git 固定上游版本,进 diagnostics 与
  request_hash(P2-01:上游漂移即缓存失效);``MEDIACRAWLER_ADAPTER_VERSION``
  随适配器语义变化手动 bump;
- ``normalize_query()`` 定义英文逗号语义(P2-05:MC 把英文逗号解释为多
  关键词分隔;首期单关键词,规整为空格);节点层在 hash 前调用同一函数;
- 输出校验:文件大小上限、JSON 结构、list[dict] 形状(P1-06);
- stdout/stderr 限长 + token/Cookie 模式脱敏(P2-06);diagnostics 不含
  绝对路径(只留 run_id 与相对文件名);
- ``search_notes()`` 内置同机同 CDP 端口**互斥锁**(P1-04:方案B下 MC 自启
  专用浏览器实例,并发第二任务收到明确忙碌状态 → 由能力层回退 Bing),
  锁文件带陈旧抢占(崩溃残留按超时回收);
- 原始 JSON **成功读取后即删**(P1-06 数据最小化),失败残留由 TTL 清扫
  (默认 3 天)兜底(P2-08);
- 子进程超时按**进程树终止**(P2-09 方案B专属:外层只杀 python 会被孤儿
  Chrome 占用专用 profile;Windows 用 taskkill /T /F)。

其他要点:
- **不加** ``-I``/``-E``:MediaCrawler 依赖脚本目录自身导入(main.py
  ``import config``),隔离模式会直接断掉其包导入;
- stdin=DEVNULL + UTF-8 文本模式(``errors="replace"``),与
  ``assembly_capabilities/ffproc.py`` 的 run_proc 纪律一致(Windows 父进程
  默认 ANSI 代码页,中文输出会被 GBK 解码打断);
- 错误码复用 search_web_topic 既有域:``NETWORK_ERROR``(路径缺失/锁忙碌/
  启动失败/超时/普通非零退出)、``PARSE_ERROR``(结果文件不可读)、
  ``NO_RESULTS``(进程正常退出但无结果文件/记录)、``AUTH_REQUIRED``
  (2026-11-21 审查修订版 P1:退出码 0 无结果文件,或非零退出且 stderr/
  stdout 命中登录态启发式标记 —— MC 登录失败既可能走无参 ``sys.exit()``
  退出码 0(伪装成"无搜索结果"),也可能走 ``sys.exit(1)``(B站 start()
  登录后二次 pong 失败,伪装成网络故障);命中标记才归类,普通非零退出
  仍为 NETWORK_ERROR);
- **平台代码 ≠ 物理输出目录**(2026-11-21 审查修订版 P1):MC 各平台 store
  的 ``AsyncFileWriter(platform=...)`` 决定目录名,与 CLI 平台代码不必相同
  (抖音代码 ``dy``,输出目录 ``douyin``),见 ``MC_OUTPUT_DIR_BY_PLATFORM``;
- 失败现场证据(审查修订版 §5.2-5):非零退出/超时/退出码 0 无结果文件时,
  脱敏 stderr 全文落盘到时间戳运行目录(TTL 清扫兜底),diagnostics 只记
  相对文件名(P2-06 纪律不变)。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# 适配器语义版本(字段映射/CLI 形态/错误分类变化时手动 bump;进 request_hash)。
MEDIACRAWLER_ADAPTER_VERSION: str = "1"

# 单个原始结果文件大小上限(异常膨胀视为损坏,防读爆内存)。
MC_RAW_MAX_BYTES: int = 10 * 1024 * 1024

# 原始 JSON 残留 TTL(天):失败残留超过该天数在下次检索时清扫。
MC_RAW_TTL_DAYS: int = 3

# CDP 端口互斥锁的陈旧回收阈值(秒):锁残留超过 max(超时+120s, 300s) 视为崩溃残留。
MC_LOCK_MIN_STALE_SECONDS: int = 300

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

# 平台代码 → MC 物理输出目录名(2026-11-21 审查修订版 P1/§3.2;2026-11-21 ks/bili
# 准入核查复验):store/<平台>/_store_impl.py 的 AsyncFileWriter(platform=...) 决定
# 目录名,与 CLI 平台代码**不必相同** —— dy→douyin(douyin/_store_impl.py)、
# ks→kuaishou(kuaishou/_store_impl.py);bili 的 JSON store 恰好也叫 "bili"
# (BiliJsonStoreImplement platform="bili";"bilibili" 是未启用的 JSONL store),
# 显式登记固化契约,上游改名时由单测发现。新平台接入前必须按其 store 实测
# 目录名后注册映射,不得假定代码==目录名。
MC_OUTPUT_DIR_BY_PLATFORM: dict[str, str] = {
    "dy": "douyin",
    "ks": "kuaishou",
    "bili": "bili",
}

# 失败现场 stderr 全文落盘上限(脱敏后写入,超限截尾;审查修订版 §5.2-5)。
MC_STDERR_EVIDENCE_MAX_CHARS: int = 64 * 1024

# 登录态启发式标记(审查修订版 §3.7):退出码 0 + 无结果文件时,脱敏后的
# stderr/stdout 命中任一标记 → AUTH_REQUIRED(而非 NO_RESULTS)。保守清单,
# 宁可漏判(NO_RESULTS 兜底)不可误伤正常空结果路径。
_AUTH_HINT_MARKERS: tuple[str, ...] = (
    "qrcode",
    "login",
    "登录",
    "二维码",
    "请先登录",
    "check_login_state",
)

# ---- 登录预检闸门(2026-11-21 第10期审查修订版 §4.1/§4.2) ----
# MC 侧检查脚本名(位于 <project_root>/check_login.py;本地新增,上游无此文件)。
MC_CHECK_LOGIN_SCRIPT: str = "check_login.py"
# NDJSON 协议版本:check_login.py stdout 每行 {"protocol_version":1,...};
# 版本不一致按 CHECK_ERROR 处理,不得推断为登录成功。
MC_LOGIN_PROTOCOL_VERSION: int = 1
# 三态登录检查结果(审查修订版 §2.7:pong False ≠ 确定未登录,异常与认证失败必须分开)。
MC_LOGIN_STATE_LOGGED_IN: str = "LOGGED_IN"
MC_LOGIN_STATE_NOT_LOGGED_IN: str = "NOT_LOGGED_IN"
MC_LOGIN_STATE_CHECK_ERROR: str = "CHECK_ERROR"
MC_LOGIN_STATES: tuple[str, ...] = (
    MC_LOGIN_STATE_LOGGED_IN,
    MC_LOGIN_STATE_NOT_LOGGED_IN,
    MC_LOGIN_STATE_CHECK_ERROR,
)
# stderr tail 落 diagnostics 的上限(与 _tail 默认一致,预检诊断不带绝对路径)。
MC_LOGIN_DIAG_TAIL_CHARS: int = 2048

# 平台代码全集(展示/校验参考;生产白名单以 config._MC_PLATFORM_WHITELIST 为准,
# 首期仅 xhs —— 审查 P2-04:字段映射假定小红书记录结构。不 import config,
# 两处必须同步维护)。
MC_PLATFORM_CODES: tuple[str, ...] = tuple(MC_PLATFORM_LABELS)

# token/Cookie 类敏感输出脱敏(审查 P2-06):key 后跟 ≥6 位疑似值 → key=***。
_REDACT_RE = re.compile(
    r"(?i)\b(token|cookie|cookies|sign|x-s|xsec_token|authorization|bearer)\b"
    r"[\"':=\s]{0,3}[^,;\"'\s]{6,}"
)


def platform_label(platform: str) -> str:
    return MC_PLATFORM_LABELS.get(str(platform), str(platform))


def normalize_query(query: str) -> str:
    """检索词规整(审查 P2-05):英文逗号 → 空格并压空白。

    MC 把英文逗号解释为**多关键词分隔**;首期按单关键词检索,故在提交前
    规整。节点层必须在计算 request_hash **之前**调用本函数(同一函数、同一
    规整),保证缓存键与实际提交语义一致;本函数幂等。
    """
    return " ".join(str(query or "").replace(",", " ").split())


def resolve_root(project_root: str) -> Path:
    """安装根目录统一绝对化(P2-03):父子进程不再依赖各自工作目录解释相对路径。"""
    return Path(project_root).expanduser().resolve()


def _resolve_python_exe(project_root: Path) -> Optional[Path]:
    """MediaCrawler 自带 .venv(uv 管理,Python 3.11);找不到返回 None。"""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        cand = project_root / rel
        if cand.is_file():
            return cand
    return None


def preflight(project_root: str) -> tuple[bool, str]:
    """启动预检(P2-02):区分三类缺失,返回 (ok, 可读原因)。

    只在 MC 检索路径上调用;MC 未启用时 Bing 主流程不受影响。
    """
    root = resolve_root(project_root)
    if not root.is_dir():
        return False, f"MediaCrawler 安装根目录不存在: {root}"
    if not (root / "main.py").is_file():
        return False, f"MediaCrawler main.py 不存在(根目录: {root})"
    if _resolve_python_exe(root) is None:
        return False, (
            f"MediaCrawler venv python 不存在(预期 {root}/.venv/Scripts/python.exe)"
        )
    return True, ""


def detect_revision(project_root: str) -> str:
    """读本地 .git 固定上游版本(P2-01);失败返回 "unknown"。

    只读文件不起 git 子进程(.git/HEAD → ref → refs 文件,或 packed-refs,
    或 detached HEAD 直接是 hash);返回 12 位短 hash,足够作缓存失效依据。
    """
    try:
        git_dir = resolve_root(project_root) / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:12]  # detached HEAD
        ref = head[4:].strip()
        ref_file = git_dir / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()[:12]
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:12]
        return "unknown"
    except (OSError, ValueError):
        return "unknown"


def _redact(text: str) -> str:
    return _REDACT_RE.sub(r"\1=***", str(text or ""))


def _tail(text: str, limit: int = 2048) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[-limit:]


def _persist_failure_evidence(
    run_dir: Path,
    data_dir: Path,
    proc: subprocess.CompletedProcess,
    diagnostics: dict[str, Any],
) -> None:
    """失败现场证据(2026-11-21 审查修订版 §5.2-5):脱敏 stderr 全文落盘 +
    输出目录状态,把"登录失败/文件未生成/解析失败"从纯猜测变成可复核证据。

    - 落盘位置在时间戳运行目录内 → 由既有 TTL 清扫(sweep_stale_runs)兜底回收;
    - diagnostics 只记相对文件名(run_id/stderr_full.txt),不含绝对路径(P2-06);
    - 尽力而为:任何 IO 失败都不改变调用方的错误分类。
    """
    with contextlib.suppress(OSError):
        run_dir.mkdir(parents=True, exist_ok=True)
        stderr_text = _redact(getattr(proc, "stderr", "") or "")
        if stderr_text:
            if len(stderr_text) > MC_STDERR_EVIDENCE_MAX_CHARS:
                stderr_text = stderr_text[-MC_STDERR_EVIDENCE_MAX_CHARS:]
            (run_dir / "stderr_full.txt").write_text(
                stderr_text, encoding="utf-8", errors="replace"
            )
            diagnostics["stderr_file"] = f"{run_dir.name}/stderr_full.txt"
    diagnostics["data_dir_exists"] = data_dir.is_dir()
    result_files: list[str] = []
    if data_dir.is_dir():
        with contextlib.suppress(OSError):
            result_files = [p.name for p in sorted(data_dir.glob("*.json"))][:20]
    diagnostics["result_files"] = result_files


def sweep_stale_runs(save_root: Path, ttl_days: int = MC_RAW_TTL_DAYS) -> int:
    """清扫超 TTL 的时间戳运行目录(P2-08);尽力而为,只清时间戳命名的子目录。"""
    cutoff = time.time() - max(int(ttl_days), 0) * 86400
    cleaned = 0
    with contextlib.suppress(OSError):
        if not save_root.is_dir():
            return 0
        for child in save_root.iterdir():
            if not child.is_dir() or not child.name[:1].isdigit():
                continue
            with contextlib.suppress(OSError):
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    cleaned += 1
    return cleaned


# ---------------------------------------------------------------------------
# CDP 端口互斥锁(P1-04):同机同端口只允许一个 MC 任务
# ---------------------------------------------------------------------------
def acquire_cdp_lock(
    lock_dir: Optional[Path], cdp_port: int, stale_seconds: int
) -> Optional[Path]:
    """尝试独占 CDP 锁文件;拿到返回锁路径,被占返回 None。

    O_CREAT|O_EXCL 原子创建;锁内容记 pid+时间戳便于排查。陈旧锁(超过
    ``stale_seconds`` 未释放,即持锁进程已崩溃)抢占式回收后重试一次。
    """
    directory = Path(lock_dir) if lock_dir is not None else Path(tempfile.gettempdir())
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"mediacrawler_cdp_{int(cdp_port)}.lock"
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = f"pid={os.getpid()} acquired_at={stamp}\n"
    for attempt in (1, 2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            return lock_path
        except FileExistsError:
            pass
        except OSError:
            return None  # 锁目录不可写等环境问题:宁可放弃也不并发
        try:
            if time.time() - lock_path.stat().st_mtime > stale_seconds:
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue  # 陈旧锁已回收,重试
        except OSError:
            continue
        return None  # 活锁被占:明确忙碌
    return None


def release_cdp_lock(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    with contextlib.suppress(OSError):
        lock_path.unlink()


# ---------------------------------------------------------------------------
# 子进程执行(P2-09:超时按进程树终止,方案B防孤儿 Chrome)
# ---------------------------------------------------------------------------
def _kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


def _run_subprocess(
    cmd: list[str], cwd: str, timeout_seconds: int
) -> tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    """Popen 版子进程执行;超时先按进程树终止再回收输出。

    返回 (completed, timeout_flag);Popen 本身失败(找不到可执行文件等)
    抛 OSError 由调用方分类。
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=int(timeout_seconds))
        return (
            subprocess.CompletedProcess(proc.args, proc.returncode, out, err),
            None,
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)  # 先杀树,防止孤儿 Chrome 占用专用 profile
        out, err = proc.communicate()
        return (
            subprocess.CompletedProcess(proc.args, proc.returncode, out, err),
            f"timeout after {timeout_seconds}s (process tree killed)",
        )


def search_notes(
    *,
    query: str,
    max_results: int,
    platform: str,
    project_root: str,
    save_root: str,
    timeout_seconds: int,
    headless: bool = False,
    cdp_port: int = 9222,
    lock_dir: Optional[Path] = None,
) -> tuple[list[dict[str, Any]], Optional[str], str, dict[str, Any]]:
    """跑一次 MediaCrawler 关键词搜索,返回 ``(records, error_code, message, diagnostics)``。

    records 是 MediaCrawler JSON 结果文件里的原始记录列表(未映射、未清洗);
    ``error_code=None`` 表示成功。diagnostics 不含绝对路径(审查 P2-06),
    完整路径只进受限 debug 落盘(能力层按 STORYLINE_WEB_TOPIC_DEBUG 决定)。
    """
    root = resolve_root(project_root)
    submitted_query = normalize_query(query)
    revision = detect_revision(project_root)
    diagnostics: dict[str, Any] = {
        "platform": str(platform),
        "parser_version": f"mc_{platform}_v{MEDIACRAWLER_ADAPTER_VERSION}",
        "adapter_version": MEDIACRAWLER_ADAPTER_VERSION,
        "mc_revision": revision,
        "headless": bool(headless),
        "timeout_seconds": int(timeout_seconds),
        "cdp_port": int(cdp_port),
        "query_submitted": submitted_query,
        "returncode": None,
        "output_file": None,
        "swept_runs": 0,
        "raw_dir_deleted": False,
    }

    # 预检(P2-02:三类缺失各自可读)
    ok, reason = preflight(project_root)
    if not ok:
        return [], "NETWORK_ERROR", reason, diagnostics

    # 互斥锁(P1-04):同机同 CDP 端口串行;被占 → 明确忙碌,由能力层回退 Bing
    stale_seconds = max(MC_LOCK_MIN_STALE_SECONDS, int(timeout_seconds) + 120)
    lock_path = acquire_cdp_lock(lock_dir, cdp_port, stale_seconds)
    if lock_path is None:
        diagnostics["cdp_lock_busy"] = True
        return [], "NETWORK_ERROR", (
            f"MediaCrawler busy: another task holds the CDP lock (port {cdp_port})"
        ), diagnostics

    try:
        save_root_path = resolve_root(save_root)
        # TTL 清扫(P2-08):上次失败残留的原始 JSON 不无限期留存
        diagnostics["swept_runs"] = sweep_stale_runs(save_root_path)

        # 每次运行独立时间戳目录;物理输出目录按 MC_OUTPUT_DIR_BY_PLATFORM 映射
        # (2026-11-21 审查修订版 P1:平台代码 dy 的真实输出在 douyin/ 子目录)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_dir = save_root_path / stamp
        diagnostics["run_id"] = stamp
        output_platform = MC_OUTPUT_DIR_BY_PLATFORM.get(str(platform), str(platform))
        data_dir = run_dir / output_platform / "json"

        cmd = [
            str(_resolve_python_exe(root)),
            "main.py",
            "--platform", str(platform),
            "--lt", "qrcode",
            "--type", "search",
            "--keywords", submitted_query,
            "--crawler_max_notes_count", str(max(int(max_results), 1)),
            "--get_comment", "false",
            "--get_sub_comment", "false",
            "--get_media", "false",
            "--headless", "1" if headless else "0",
            "--save_data_option", "json",
            "--save_data_path", str(run_dir),
        ]
        try:
            proc, timeout_flag = _run_subprocess(cmd, cwd=str(root), timeout_seconds=timeout_seconds)
        except OSError as exc:
            return [], "NETWORK_ERROR", f"MediaCrawler launch failed: {exc}", diagnostics

        diagnostics["returncode"] = proc.returncode
        diagnostics["stdout_tail"] = _tail(_redact(proc.stdout))
        diagnostics["stderr_tail"] = _tail(_redact(proc.stderr))
        if timeout_flag is not None or proc.returncode != 0:
            _persist_failure_evidence(run_dir, data_dir, proc, diagnostics)
            if timeout_flag is not None:
                diagnostics["timeout"] = timeout_flag
                return [], "NETWORK_ERROR", (
                    f"MediaCrawler timed out: {timeout_flag} "
                    "(first run needs QR scan in the popped-up browser window)"
                ), diagnostics
            # 非零退出也可能携带登录诊断(2026-11-21 ks/bili 准入 §2.5:B站
            # start() 登录后二次 pong() 失败走 sys.exit(1))——命中明确登录标记
            # 时归类 AUTH_REQUIRED;普通非零退出仍为 NETWORK_ERROR,不把所有
            # 非零退出改判为登录失败。
            joined = _redact(f"{proc.stderr or ''}\n{proc.stdout or ''}").lower()
            if any(marker in joined for marker in _AUTH_HINT_MARKERS):
                return [], "AUTH_REQUIRED", (
                    f"MediaCrawler exited with code {proc.returncode} and "
                    "stderr/stdout suggests login/QR-scan state not established "
                    f"(run_id={stamp}; platform={output_platform}); "
                    "scan-login may be required"
                ), diagnostics
            return [], "NETWORK_ERROR", (
                f"MediaCrawler exited with code {proc.returncode}; see diagnostics "
                "stdout_tail/stderr_tail for details"
            ), diagnostics

        candidates = sorted(data_dir.glob("search_contents_*.json"))
        if not candidates:
            # 兜底:结果文件命名变更时按任意 json 容错(glob 取最新)
            candidates = sorted(data_dir.glob("*.json"))
        if not candidates:
            # 退出码 0 但无结果文件(2026-11-21 审查修订版 §3.7):保留现场证据
            # 并区分登录态问题(AUTH_REQUIRED)与真实空结果(NO_RESULTS)——
            # MC 登录失败走 sys.exit() 退出码 0,不能伪装成"无搜索结果"。
            _persist_failure_evidence(run_dir, data_dir, proc, diagnostics)
            joined = _redact(f"{proc.stderr or ''}\n{proc.stdout or ''}").lower()
            if any(marker in joined for marker in _AUTH_HINT_MARKERS):
                return [], "AUTH_REQUIRED", (
                    "MediaCrawler finished (exit 0) without result file and "
                    "stderr/stdout suggests login/QR-scan state not established "
                    f"(run_id={stamp}; platform={output_platform}); "
                    "scan-login may be required"
                ), diagnostics
            return [], "NO_RESULTS", (
                f"MediaCrawler finished (exit 0) but wrote no result file "
                f"(run_id={stamp})"
            ), diagnostics
        out_file = candidates[-1]
        diagnostics["output_file"] = f"{output_platform}/json/{out_file.name}"

        if out_file.stat().st_size > MC_RAW_MAX_BYTES:
            return [], "PARSE_ERROR", (
                f"MediaCrawler result file oversized "
                f"({out_file.stat().st_size} > {MC_RAW_MAX_BYTES} bytes)"
            ), diagnostics

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
            return [], "NO_RESULTS", (
                "MediaCrawler returned an empty result list"
            ), diagnostics

        # 数据最小化(P1-06):原始记录已读入,本次运行目录即删(失败残留走 TTL 清扫)
        with contextlib.suppress(OSError):
            shutil.rmtree(run_dir, ignore_errors=True)
            diagnostics["raw_dir_deleted"] = True
        return records, None, "", diagnostics
    finally:
        release_cdp_lock(lock_path)


def _run_subprocess_streaming(
    cmd: list[str],
    cwd: str,
    timeout_seconds: int,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], str, Optional[str], Optional[int]]:
    """NDJSON 流式子进程执行(第10期审查修订版 §2.8/阶段2)。

    与阻塞式 ``_run_subprocess()`` 的区别:stdout 逐行实时解析协议消息并即时
    转发 progress 事件(扫码等待期间 UI 可实时显示,而非结束后补发);stderr
    由独立线程并行排空,避免 pipe 缓冲区写满死锁。

    返回 ``(result_msg, events, stderr_tail, timeout_flag, returncode)``:
    - ``result_msg`` = 协议终态 dict(type=="result")或 None(缺失终态一律
      归 CHECK_ERROR,不得推断登录成功);
    - ``events`` = 收到的 progress 消息列表(已按协议版本过滤);
    - 超时按进程树终止(复用 ``_kill_process_tree`` 纪律)。
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout_q: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_chunks: list[str] = []

    def _read_stream(pipe: Any, sink: Any, is_stderr: bool) -> None:
        try:
            for line in pipe:
                sink(line)
        except Exception:
            pass
        finally:
            if not is_stderr:
                stdout_q.put(None)  # EOF 哨兵

    stdout_thread = threading.Thread(
        target=_read_stream, args=(proc.stdout, stdout_q.put, False), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(proc.stderr, lambda line: stderr_chunks.append(line), True),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    result_msg: Optional[dict[str, Any]] = None
    events: list[dict[str, Any]] = []
    timeout_flag: Optional[str] = None
    deadline = time.monotonic() + max(int(timeout_seconds), 1)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timeout_flag = f"timeout after {timeout_seconds}s (process tree killed)"
                break
            try:
                item = stdout_q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if item is None:
                break  # stdout EOF(进程自然退出)
            line = item.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 协议外 stdout 噪音:忽略,不判定
            if not isinstance(msg, dict):
                continue
            if msg.get("protocol_version") != MC_LOGIN_PROTOCOL_VERSION:
                continue
            if msg.get("type") == "progress":
                events.append(msg)
                if on_event is not None:
                    # 观测通道不得改变主流程:回调异常吞掉(§4.3)
                    try:
                        on_event(msg)
                    except Exception:
                        pass
            elif msg.get("type") == "result":
                result_msg = msg
                break
    finally:
        if timeout_flag is not None:
            _kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            proc.wait(timeout=10)
        with contextlib.suppress(Exception):
            proc.stdout.close()
        with contextlib.suppress(Exception):
            proc.stderr.close()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    stderr_tail = "".join(stderr_chunks)[-MC_LOGIN_DIAG_TAIL_CHARS:]
    return result_msg, events, stderr_tail, timeout_flag, proc.returncode


def verify_login(
    *,
    platform: str,
    project_root: str,
    timeout_seconds: int,
    cdp_port: int = 9222,
    lock_dir: Optional[Path] = None,
    interactive: bool = True,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[str, Optional[str], str, dict[str, Any]]:
    """单平台登录预检(第10期审查修订版 §4.2)。

    以子进程运行 MC 侧 ``check_login.py``(生产同路径探针 + 可选扫码登录),
    返回 ``(state, error_code, message, diagnostics)``:

    - state: ``LOGGED_IN``(明确登录证据)/ ``NOT_LOGGED_IN``(明确未登录)
      / ``CHECK_ERROR``(网络/接口/页面/协议/超时异常,fail-closed 不弹码);
    - error_code: LOGGED_IN → None;NOT_LOGGED_IN → AUTH_REQUIRED;
      CHECK_ERROR → NETWORK_ERROR(复用既有错误码域;闸门层据此聚合为
      ``LOGIN_GATE_FAILED``,不得伪装成检索失败);
    - 仅当收到合法终态 JSON 且 state==LOGGED_IN 才判登录通过;缺终态 JSON、
      协议版本/平台名/状态值非法、超时、锁忙、preflight 失败一律 CHECK_ERROR。
    - 与 search_notes 共用同一把 CDP 锁(预检与检索互斥);锁在检查结束即
      释放,闸门与后续搜索之间存在交错窗口——登录态不会因另一任务爬取而
      失效,且 search_notes 会再次加锁防并发访问,该窗口按审查修订版 §4.2
      记录为已知可接受。
    """
    root = resolve_root(project_root)
    diagnostics: dict[str, Any] = {
        "platform": str(platform),
        "adapter_version": MEDIACRAWLER_ADAPTER_VERSION,
        "interactive": bool(interactive),
        "timeout_seconds": int(timeout_seconds),
        "cdp_port": int(cdp_port),
        "returncode": None,
        "events": [],
        "login_attempted": False,
    }

    ok, reason = preflight(project_root)
    if not ok:
        return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", reason, diagnostics

    script_path = root / MC_CHECK_LOGIN_SCRIPT
    if not script_path.is_file():
        return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
            f"{MC_CHECK_LOGIN_SCRIPT} 不存在({root});本地 MC 检出版本过旧,"
            "需要同步第10期登录预检脚本"
        ), diagnostics

    stale_seconds = max(MC_LOCK_MIN_STALE_SECONDS, int(timeout_seconds) + 120)
    lock_path = acquire_cdp_lock(lock_dir, cdp_port, stale_seconds)
    if lock_path is None:
        diagnostics["cdp_lock_busy"] = True
        return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
            f"MediaCrawler busy: another task holds the CDP lock (port {cdp_port})"
        ), diagnostics

    try:
        cmd = [
            str(_resolve_python_exe(root)),
            MC_CHECK_LOGIN_SCRIPT,
            "--platform", str(platform),
        ]
        if not interactive:
            cmd.append("--no-interactive")
        try:
            (
                result_msg,
                events,
                stderr_tail,
                timeout_flag,
                returncode,
            ) = _run_subprocess_streaming(
                cmd,
                cwd=str(root),
                timeout_seconds=timeout_seconds,
                on_event=on_event,
            )
        except OSError as exc:
            return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
                f"MediaCrawler login check launch failed: {exc}"
            ), diagnostics

        diagnostics["returncode"] = returncode
        diagnostics["timeout"] = timeout_flag
        diagnostics["events"] = [str(e.get("event") or "") for e in events]
        diagnostics["stderr_tail"] = _tail(_redact(stderr_tail), MC_LOGIN_DIAG_TAIL_CHARS)

        if timeout_flag is not None:
            return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
                f"MediaCrawler login check timed out: {timeout_flag}"
            ), diagnostics
        if result_msg is None:
            # 子进程崩溃 / sys.exit() 逃逸 / 协议缺失:一律 CHECK_ERROR(§2.9)
            return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
                "MediaCrawler login check produced no terminal JSON "
                f"(returncode={returncode}); see diagnostics.stderr_tail"
            ), diagnostics

        if str(result_msg.get("platform") or "") != str(platform):
            return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
                f"login check platform mismatch: {result_msg.get('platform')!r}"
            ), diagnostics
        state = str(result_msg.get("state") or "")
        if state not in MC_LOGIN_STATES:
            return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", (
                f"login check protocol state invalid: {state!r}"
            ), diagnostics

        diagnostics["login_attempted"] = bool(result_msg.get("login_attempted"))
        diagnostics["login_state_detail"] = str(result_msg.get("detail") or "")[:500]
        diagnostics["elapsed_ms"] = int(result_msg.get("elapsed_ms") or 0)
        message = str(result_msg.get("detail") or "")
        if state == MC_LOGIN_STATE_LOGGED_IN:
            return MC_LOGIN_STATE_LOGGED_IN, None, message, diagnostics
        if state == MC_LOGIN_STATE_NOT_LOGGED_IN:
            return MC_LOGIN_STATE_NOT_LOGGED_IN, "AUTH_REQUIRED", message, diagnostics
        return MC_LOGIN_STATE_CHECK_ERROR, "NETWORK_ERROR", message, diagnostics
    finally:
        release_cdp_lock(lock_path)

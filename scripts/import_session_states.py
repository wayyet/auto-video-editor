"""把旧的 ``session_state.json`` 导入 PostgreSQL ``chat_sessions``(方案 §5.4)。

一次性迁移 CLI::

    python scripts/import_session_states.py --dry-run   # 只解析 + 报告,不写库
    python scripts/import_session_states.py             # 真正导入
    python scripts/import_session_states.py --archive   # 导入并把旧文件移出活动路径

行为约定
--------
- **幂等**:用 ``SessionRepository.create_if_absent`` ⇒ 重复运行**不覆盖**已存在的
  会话(导入之后新创建的会话也不会被冲掉)。
- **两个根目录都扫**:``outputs/`` 和 ``openstoryline/outputs/`` —— 输出根目录历史上
  搬过家,只扫一个会漏(方案 §5.4 明确警告)。
- **先脱敏再入库**:复用 ``openstoryline.agent_fastapi.ChatSession`` 的脱敏逻辑
  (``_sanitize_custom_model_cfg_for_state`` / ``_sanitize_tts_cfg_for_state``)。
  该模块**延迟导入**且带 ``try/except`` ⇒ 导入失败只告警,退回写原始解析结果,
  绝不因导入问题把整个工具搞崩。
- **媒体只留引用**:不读、不拷媒体文件;引用路径失效只记 note,**不算失败**。
- **有失败就非零退出**:坏文件不能被当成成功。
- ``--archive``:导入成功后把旧文件**移动**(不是删除)到
  ``logs/retired_session_states/<日期>/`` ⇒ 服务端成为唯一事实来源,
  同时保留可回滚窗口。**只有库里确实有这一行**才会被移走;
  解析失败 / 入库失败的文件一律留在原地。

退出码:0 = 全部成功(或 dry-run 无失败);1 = 有文件失败;2 = 启动前置检查失败。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK ⇒ 中文/符号会乱码,统一强制 UTF-8(同 scripts/run_workflow.py)。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):  # pragma: no cover - 老解释器没有 reconfigure
    pass

from db.migrations import postgres_uri  # noqa: E402  — sys.path 调整后才能 import
from db.session_repository import SESSION_ID_RE, SessionRepository  # noqa: E402

STATE_FILENAME = "session_state.json"
DEFAULT_TITLE = "新对话"
TITLE_MAX_CHARS = 32
PREVIEW_MAX_CHARS = 120


# ---------------------------------------------------------------------------
# 报告小工具
# ---------------------------------------------------------------------------
def _rel(path: Path) -> str:
    """尽量以仓库根为基准显示路径(超出仓库根就显示绝对路径)。"""
    try:
        return str(path.resolve().relative_to(ROOT)).replace(os.sep, "/")
    except (ValueError, OSError):
        return str(path)


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _note(msg: str) -> None:
    print(f"[NOTE] {msg}")


# ---------------------------------------------------------------------------
# 脱敏(延迟导入 agent_fastapi —— 那份文件由别的 worker 改,导入失败只告警)
# ---------------------------------------------------------------------------
_CHATSESSION_CACHE: Any = None
_CHATSESSION_ERROR: str | None = None
_CHATSESSION_LOADED = False


def _load_chatsession() -> Any:
    """拿 ``ChatSession`` 类;失败返回 ``None`` 并记录原因(不抛异常)。

    两条路:
    1. ``openstoryline.agent_fastapi``(包路径);
    2. 直接按文件加载 —— 服务端跑在 ``openstoryline/.venv`` 时,包路径导入会因
       ``sys.path`` 里没有 ``openstoryline/`` 而在 ``import src`` 处失败。

    ⚠️ 调用前应先解析好 Postgres URI:``agent_fastapi`` 导入时会把
    ``openstoryline/src`` 插到 ``sys.path`` 最前面(仓库根的 ``config`` 已在
    ``sys.modules`` 里,不会被顶掉)。
    """
    global _CHATSESSION_CACHE, _CHATSESSION_ERROR, _CHATSESSION_LOADED
    if _CHATSESSION_LOADED:
        return _CHATSESSION_CACHE
    _CHATSESSION_LOADED = True

    errors: list[str] = []

    # ``agent_fastapi`` 内部会 ``import src.*`` ⇒ 需要 ``openstoryline/`` 在 sys.path 上。
    # 追加(不是 insert 到最前),免得抢在仓库根的 ``config`` 前面。
    pkg_dir = ROOT / "openstoryline"
    if str(pkg_dir) not in sys.path:
        sys.path.append(str(pkg_dir))

    try:
        from openstoryline.agent_fastapi import ChatSession

        _CHATSESSION_CACHE = ChatSession
        return _CHATSESSION_CACHE
    except Exception as exc:
        errors.append(f"openstoryline.agent_fastapi({type(exc).__name__}: {exc})")

    try:
        import importlib.util

        target = pkg_dir / "agent_fastapi.py"
        spec = importlib.util.spec_from_file_location("openstoryline_agent_fastapi", target)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {target} 创建 import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # 必须先登记,否则模块内部取自身会拿到 None
        spec.loader.exec_module(module)
        _CHATSESSION_CACHE = module.ChatSession
        return _CHATSESSION_CACHE
    except Exception as exc:
        errors.append(f"直接加载 agent_fastapi({type(exc).__name__}: {exc})")

    _CHATSESSION_ERROR = (
        "无法加载 ChatSession(" + "; ".join(errors) + ");已按原始解析结果写入(未脱敏)"
    )
    return None


def _sanitize_state(state: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """脱敏后返回 ``(state, warning)``。

    ``warning`` 非空 ⇒ 已退回写原始解析结果。
    """
    cls = _load_chatsession()
    if cls is None:
        return state, _CHATSESSION_ERROR
    try:
        out = dict(state)
        out["custom_llm_config"] = cls._sanitize_custom_model_cfg_for_state(
            state.get("custom_llm_config")
        )
        out["custom_vlm_config"] = cls._sanitize_custom_model_cfg_for_state(
            state.get("custom_vlm_config")
        )
        out["tts_config"] = cls._sanitize_tts_cfg_for_state(state.get("tts_config"))
        return out, None
    except Exception as exc:  # pragma: no cover - 脱敏实现变了也不该崩
        return state, (
            f"脱敏失败({type(exc).__name__}: {exc});已按原始解析结果写入(未脱敏)"
        )


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def _default_roots() -> list[Path]:
    """默认要扫的根目录(去重、保持顺序)。"""
    roots: list[Path] = []
    try:
        from config import storyline_outputs_root

        roots.append(Path(storyline_outputs_root()))
    except Exception as exc:  # pragma: no cover - config 不可导入
        _warn(f"config.storyline_outputs_root() 不可用({type(exc).__name__}: {exc})")
    # 输出根目录历史上搬过家 ⇒ 两个都扫(方案 §5.4)。
    roots.append(ROOT / "outputs")
    roots.append(ROOT / "openstoryline" / "outputs")

    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = os.path.normcase(str(r))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _iter_state_files(root: Path) -> list[Path]:
    """列出 ``root/*/session_state.json``。"""
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            f = entry / STATE_FILENAME
            if f.is_file():
                out.append(f)
    except OSError as exc:
        _warn(f"扫描根目录失败 {root}({type(exc).__name__}: {exc})")
    return out


# ---------------------------------------------------------------------------
# 解析 / 标题 / 媒体引用
# ---------------------------------------------------------------------------
def _derive_title(state: dict[str, Any]) -> str:
    """标题优先级:JSON 里已有的标题 → 第一条用户消息 → ``新对话``。"""
    existing = str(state.get("title") or "").strip()
    if existing:
        return existing

    history = state.get("history")
    if isinstance(history, (list, tuple)):
        for item in history:
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "") != "user":
                continue
            content = item.get("content")
            if not isinstance(content, str):
                continue
            text = " ".join(content.split())  # 折叠空白
            if not text:
                continue
            if len(text) > TITLE_MAX_CHARS:
                text = text[:TITLE_MAX_CHARS] + "…"
            return text
    return DEFAULT_TITLE


def _media_notes(state: dict[str, Any]) -> list[str]:
    """检查状态里引用的媒体文件是否还在 —— 只记 note,不读、不拷文件。"""
    notes: list[str] = []
    load_media = state.get("load_media")
    if not isinstance(load_media, dict):
        return notes
    for mid, meta in load_media.items():
        if not isinstance(meta, dict):
            continue
        for key in ("path", "thumb_path"):
            raw = meta.get(key)
            if not raw:
                continue
            p = Path(str(raw))
            try:
                exists = p.exists()
            except OSError:
                exists = False
            if not exists:
                notes.append(f"media[{mid}].{key} 缺失: {p}")
    return notes


# ---------------------------------------------------------------------------
# 导入主流程
# ---------------------------------------------------------------------------
class _Reporter:
    """统计 imported / skipped / failed,并逐文件打印。"""

    def __init__(self) -> None:
        self.imported = 0
        self.skipped = 0
        self.failed = 0

    def imported_line(self, sid: str, path: Path, title: str, dry_run: bool) -> None:
        self.imported += 1
        suffix = "(dry-run)" if dry_run else ""
        print(f"[imported{suffix}] {sid}  {_rel(path)}  title={title!r}")

    def skipped_line(self, sid: str, path: Path, reason: str = "exists") -> None:
        self.skipped += 1
        print(f"[skipped({reason})] {sid}  {_rel(path)}")

    def failed_line(self, sid: str, path: Path, reason: str) -> None:
        self.failed += 1
        print(f"[FAILED] {sid}  {_rel(path)}  原因: {reason}")


def _preflight(repo: SessionRepository, uri: str) -> None:
    """确认能连库、且 ``chat_sessions`` 存在。失败直接退出(退出码 2)。"""
    try:
        repo.get("0" * 32)
    except Exception as exc:
        print(
            f"[FATAL] 连库失败或 chat_sessions 表不可用({type(exc).__name__}: {exc})\n"
            f"        uri={uri}\n"
            '        先建表: python -c "from db.migrations import apply_migrations_to_db; '
            'apply_migrations_to_db()"',
            file=sys.stderr,
        )
        raise SystemExit(2)


def _import_one(
    repo: SessionRepository,
    reporter: _Reporter,
    state_file: Path,
    *,
    dry_run: bool,
) -> None:
    sid = state_file.parent.name
    if not SESSION_ID_RE.match(sid):
        reporter.failed_line(sid or "<无目录名>", state_file, "目录名不是 32 位小写 hex 会话 ID")
        return

    try:
        raw_text = state_file.read_text(encoding="utf-8")
    except OSError as exc:
        reporter.failed_line(sid, state_file, f"读取失败({type(exc).__name__}: {exc})")
        return

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        reporter.failed_line(sid, state_file, f"JSON 解析失败({exc})")
        return

    if not isinstance(parsed, dict):
        reporter.failed_line(sid, state_file, f"顶层不是对象({type(parsed).__name__})")
        return

    # 目录名 vs 文件内 session_id —— 不一致时以目录名为准,并告警。
    file_sid = str(parsed.get("session_id") or "").strip()
    if file_sid and file_sid != sid:
        _warn(f"{_rel(state_file)}: 文件内 session_id={file_sid} 与目录名 {sid} 不一致 —— 以目录名为准")

    state, warn = _sanitize_state(parsed)
    if warn:
        _warn(f"{_rel(state_file)}: {warn}")

    for note in _media_notes(state):
        _note(f"{_rel(state_file)}: {note}")

    title = _derive_title(state)

    if dry_run:
        try:
            exists = repo.get(sid) is not None
        except Exception as exc:
            _warn(f"{_rel(state_file)}: 查询已有会话失败({type(exc).__name__}: {exc}) —— 按将导入处理")
            exists = False
        if exists:
            reporter.skipped_line(sid, state_file, "exists")
        else:
            reporter.imported_line(sid, state_file, title, dry_run=True)
        return

    try:
        _record, created = repo.create_if_absent(
            sid,
            state_json=state,
            title=title,
            title_source="auto",
        )
    except Exception as exc:
        reporter.failed_line(sid, state_file, f"入库失败({type(exc).__name__}: {exc})")
        return

    if created:
        reporter.imported_line(sid, state_file, title, dry_run=False)
    else:
        reporter.skipped_line(sid, state_file, "exists")


def _archive_target(archive_root: Path, state_file: Path, sid: str) -> Path:
    """归档后的目标路径:尽量保持相对仓库根的结构(两个 outputs 根不会撞名)。"""
    try:
        rel = state_file.resolve().relative_to(ROOT)
        return archive_root / rel
    except (ValueError, OSError):
        return archive_root / f"{sid}.{STATE_FILENAME}"


def _archive_state_files(
    repo: SessionRepository,
    files: list[Path],
    archive_root: Path,
) -> tuple[list[str], list[str]]:
    """把**已在库里**的 ``session_state.json`` 移出活动路径。

    ⚠️ 只有 ``repo.get(sid)`` 能读到行才移走 —— 库里没有的绝不移,
    否则就是真丢数据。

    Returns:
        ``(已归档的相对路径列表, 留在原地的说明列表)``
    """
    archived: list[str] = []
    kept: list[str] = []
    archive_root.mkdir(parents=True, exist_ok=True)

    for f in files:
        sid = f.parent.name
        if not SESSION_ID_RE.match(sid):
            kept.append(f"{_rel(f)}: 目录名不是合法会话 ID,不归档")
            continue
        try:
            present = repo.get(sid) is not None
        except Exception as exc:  # noqa: BLE001 — 查不到就保守地不归档
            kept.append(f"{_rel(f)}: 查询数据库失败({type(exc).__name__}: {exc}),不归档")
            continue
        if not present:
            kept.append(f"{_rel(f)}: 库里没有这一行,不归档")
            continue

        target = _archive_target(archive_root, f, sid)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(target))
        except OSError as exc:
            kept.append(f"{_rel(f)}: 移动失败({type(exc).__name__}: {exc})")
            continue
        archived.append(str(target.relative_to(archive_root)).replace(os.sep, "/"))

    manifest = archive_root / "retirement_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "archive_root": str(archive_root),
                "archived": sorted(archived),
                "kept": sorted(kept),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return archived, kept


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="把旧的 session_state.json 导入 PostgreSQL chat_sessions(方案 §5.4)",
    )
    p.add_argument("--dry-run", action="store_true", help="只解析 + 报告,不写库")
    p.add_argument("--uri", default=None, help="Postgres 连接串(默认取 db.migrations.postgres_uri())")
    p.add_argument(
        "--roots",
        action="append",
        default=None,
        metavar="DIR",
        help="额外要扫的根目录(可重复);默认扫 outputs/ 与 openstoryline/outputs/",
    )
    p.add_argument(
        "--archive",
        action="store_true",
        help="导入成功后把旧文件**移动**到 logs/retired_session_states/<日期>/(只移库里已有行的)",
    )
    p.add_argument(
        "--archive-dir",
        default="",
        help="归档目录(默认 logs/retired_session_states/<日期>)",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    uri = args.uri or postgres_uri()

    roots = _default_roots()
    for extra in args.roots or []:
        p = Path(extra).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        roots.append(p)

    print("== 扫描根目录 ==")
    for r in roots:
        print(f"  {'(存在)' if r.is_dir() else '(不存在)'}  {r}")

    files: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        for f in _iter_state_files(r):
            key = os.path.normcase(str(f))
            if key in seen:
                continue
            seen.add(key)
            files.append(f)

    print(f"\n== 待处理文件 {len(files)} 个 ==")

    reporter = _Reporter()
    repo = SessionRepository(uri=uri)
    if not args.dry_run:
        _preflight(repo, uri)
    else:
        # dry-run 也要尽量知道"库里有没有" ⇒ 顺手探一下;探不到不致命。
        try:
            repo.get("0" * 32)
        except Exception as exc:
            _warn(f"数据库不可用({type(exc).__name__}: {exc}) —— dry-run 将跳过存在性检查")

    for f in files:
        _import_one(repo, reporter, f, dry_run=args.dry_run)

    mode = "dry-run" if args.dry_run else "real"
    print(
        f"\n== 汇总({mode}): imported={reporter.imported} "
        f"skipped={reporter.skipped} failed={reporter.failed}"
    )

    if args.archive and not args.dry_run:
        archive_root = (
            Path(args.archive_dir).resolve()
            if args.archive_dir
            else ROOT / "logs" / "retired_session_states" / dt.date.today().isoformat()
        )
        archived, kept = _archive_state_files(repo, files, archive_root)
        print(f"\n== 归档:已移动 {len(archived)} 个文件到 {archive_root}")
        for a in archived:
            print(f"  ✓ {a}")
        if kept:
            print(f"  留在原地 {len(kept)} 个:")
            for k in kept:
                print(f"  · {k}")

    return 1 if reporter.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

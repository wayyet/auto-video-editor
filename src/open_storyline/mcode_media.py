"""素材准备 —— 把网页端上传的视频复制到 agent_gate 素材白名单根下。

为什么需要这一层
----------------
网页端上传的视频落在 ``openstoryline/outputs/<session_id>/media/``,**不在**
mcode / agent_gate 的素材白名单里。白名单由 ``video-ops/.mcp.json`` 的
``AGENT_GATE_MEDIA_ROOTS`` 定义(默认 ``<repo>/inputs`` + ``<repo>/tests/fixtures``)。
mcode 想用这些素材,就必须有一份**落在白名单内**的副本。

本模块提供**纯函数**,不碰 HTTP、不碰 FastAPI:
- ``resolve_auto_editor_dir()``  —— 定位 auto-video-editor 仓库根
- ``resolve_media_root(cfg)``    —— 素材白名单根(``<repo>/inputs``)
- ``allowed_media_roots()``      —— 白名单根清单(与 ``.mcp.json`` 一致)
- ``prepare_media_copies(...)``  —— 复制 + 校验 + 原子落地
- ``build_media_manifest(...)``  —— 生成给模型看的绝对路径清单

信任边界(核心安全设计)
----------------------
* **模型永不自己拼素材路径**。绝对路径一律服务端生成。
* 客户端给的 ``stored_path`` **不可信**:必须 resolve 到**本会话 media 目录**内。
* 目标路径必须过 ``agent_gate.safety.safe_media_path`` 白名单判定。
* **不覆盖、不移动、不删除源文件**。只新增副本。
* 复制先写 ``<name>.part``,校验**大小 + SHA-256** 后才 ``os.replace`` 原子改名。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 仓库根 = 本文件所在的 ``openstoryline/src/open_storyline`` 往上 3 层。
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 会话 ID 必须匹配的形状。同时满足 ``agent_gate.protocol.THREAD_ID_RE``
#: 与「目录名安全」两个要求。
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: 单文件读进内存计算 SHA-256 的分块大小。
_CHUNK = 1024 * 1024


class MCodeMediaError(RuntimeError):
    """素材准备失败(路径不合法 / 复制失败 / 校验不通过)。"""


# --------------------------------------------------------------------------
# agent_gate.safety 的按需导入
# --------------------------------------------------------------------------

def _safety_module():
    """拿到 ``agent_gate.safety`` 模块(仓库根不一定在 ``sys.path`` 上)。

    返回**模块**而不是函数,方便测试 monkeypatch ``agent_gate.safety.safe_media_path``。
    """
    try:
        import agent_gate.safety as safety
        return safety
    except ImportError:
        repo_root = str(_REPO_ROOT)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        try:
            import agent_gate.safety as safety
            return safety
        except ImportError as exc:
            raise MCodeMediaError(
                f"无法导入 agent_gate.safety({exc});仓库根不在 sys.path 上: {repo_root}"
            ) from exc


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------

def resolve_auto_editor_dir() -> Path:
    """定位 auto-video-editor 仓库根。

    优先级:``AUTO_VIDEO_EDITOR_DIR`` 环境变量 → 本文件位置推导的仓库根。
    推导出来的根必须**看起来像**仓库根(含 ``nodes/`` 或 ``inputs/``),
    否则报错 —— 宁可报错也不要静默指向错误目录。
    """
    env = (os.environ.get("AUTO_VIDEO_EDITOR_DIR") or "").strip()
    if env:
        return Path(env).resolve()

    cand = _REPO_ROOT.resolve()
    if (cand / "nodes").is_dir() or (cand / "inputs").is_dir():
        return cand

    raise MCodeMediaError(
        "定位 auto-video-editor 目录失败:请设置环境变量 AUTO_VIDEO_EDITOR_DIR,"
        f"(推导出的候选路径 {cand} 既没有 nodes/ 也没有 inputs/)"
    )


def resolve_media_root(cfg_mcode: Any = None) -> Path:
    """素材白名单根。

    ``cfg_mcode.media_root`` 非空则用它;否则用 ``<auto-video-editor>/inputs``。
    """
    raw = ""
    if cfg_mcode is not None:
        raw = str(getattr(cfg_mcode, "media_root", "") or "").strip()
    if raw:
        return Path(raw).resolve()
    return (resolve_auto_editor_dir() / "inputs").resolve()


def allowed_media_roots() -> List[Path]:
    """素材白名单根清单。

    环境变量 ``AGENT_GATE_MEDIA_ROOTS`` 非空就按 ``os.pathsep`` 切分;
    否则用 ``<repo>/inputs`` + ``<repo>/tests/fixtures``(与 ``video-ops/.mcp.json`` 一致)。

    **只读取,不修改**白名单 —— 白名单是安全边界,不能为了迁就某次调用而扩大。
    """
    raw = (os.environ.get("AGENT_GATE_MEDIA_ROOTS") or "").strip()
    roots: List[Path] = []
    if raw:
        for part in raw.split(os.pathsep):
            part = part.strip()
            if not part:
                continue
            try:
                roots.append(Path(part).resolve())
            except (OSError, ValueError):
                continue
        return roots

    base = resolve_auto_editor_dir()
    return [(base / "inputs").resolve(), (base / "tests" / "fixtures").resolve()]


def _ensure_within(child: Path, parent: Path) -> bool:
    """``child`` 是否在 ``parent`` 之内(两边都 resolve 过)。"""
    try:
        child_r = child.resolve()
        parent_r = parent.resolve()
    except (OSError, ValueError):
        return False
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# 素材复制
# --------------------------------------------------------------------------

def _sanitize_stem(stem: str) -> str:
    """净化文件名主干:去路径分隔符 + 去控制字符。"""
    base = os.path.basename(str(stem or ""))
    base = base.replace("\x00", "")
    base = re.sub(r"[\x01-\x1f\x7f]", "", base)
    base = base.replace("/", "_").replace("\\", "_")
    base = base.strip().strip(".")
    return base or "media"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_target(
    target_dir: Path,
    stem: str,
    ext: str,
    src_sha: str,
) -> Tuple[Path, bool]:
    """挑一个目标路径。返回 ``(路径, 是否复用)``。

    规则:
    1. ``<stem><ext>`` 不存在 -> 用它;
    2. 存在但内容(SHA-256)与源**相同** -> 复用它(幂等);
    3. 存在但内容不同 -> 换 ``<stem> (1)<ext>``;该名字同样按 1/2 判定;
    4. **绝不覆盖**既有文件。
    """
    for idx in range(0, 100):
        name = f"{stem}{ext}" if idx == 0 else f"{stem} ({idx}){ext}"
        candidate = target_dir / name
        if not candidate.exists():
            return candidate, False
        try:
            if _sha256(candidate) == src_sha:
                return candidate, True
        except OSError:
            pass
    raise MCodeMediaError(f"目标文件名冲突过多,无法生成唯一名: {stem}{ext}")



def prepare_media_copies(
    session_id: str,
    files: Sequence[Mapping[str, Any]],
    *,
    media_root: Path,
    allowed_roots: Sequence[Path],
    session_media_dir: Path,
) -> List[Dict[str, Any]]:
    """把本会话上传的素材复制到白名单目录,返回校验过的副本清单。

    参数
    ----
    session_id
        网页会话 ID。必须匹配 ``SESSION_ID_RE``。
    files
        每项至少有 ``media_id`` 与 ``stored_path``;``filename`` 可选(用于命名)。
    media_root
        白名单根(``<repo>/inputs``)。副本落在 ``media_root/<session_id>/``。
    allowed_roots
        白名单根清单,用于最终的安全判定。
    session_media_dir
        本会话上传目录。源文件必须落在它里面。

    返回
    ----
    ``[{"media_id", "filename", "path", "size", "sha256"}, ...]``

    失败时抛 :class:`MCodeMediaError`,且**不留残留 ``.part``**。
    """
    sid = str(session_id or "").strip()
    if not SESSION_ID_RE.match(sid):
        raise MCodeMediaError(
            f"session_id 非法: {session_id!r}(只允许字母数字下划线连字符,且不超过 64 字符)"
        )

    media_root = Path(media_root).resolve()
    session_media_dir = Path(session_media_dir).resolve()
    roots = [Path(r).resolve() for r in allowed_roots]

    if not session_media_dir.is_dir():
        raise MCodeMediaError(f"本会话素材目录不存在: {session_media_dir}")

    target_dir = (media_root / sid).resolve()
    # 目标目录本身必须在白名单内 —— 防止 media_root 配置本身就离谱。
    if not any(_ensure_within(target_dir, r) for r in roots):
        raise MCodeMediaError(f"目标目录不在素材白名单内,已拒绝: {target_dir}")

    prepared: List[Dict[str, Any]] = []

    for item in files or []:
        media_id = str((item or {}).get("media_id") or "").strip()
        stored_path = str((item or {}).get("stored_path") or "").strip()
        filename = str((item or {}).get("filename") or "").strip()

        if not media_id:
            raise MCodeMediaError(f"素材缺少 media_id: {item!r}")
        if not stored_path:
            raise MCodeMediaError(f"素材 {media_id} 缺少 stored_path")
        if not SESSION_ID_RE.match(media_id):
            raise MCodeMediaError(f"素材 media_id 非法: {media_id!r}")

        src = Path(stored_path)
        try:
            src_resolved = src.resolve()
        except (OSError, ValueError) as exc:
            raise MCodeMediaError(f"素材 {media_id} 路径无法解析: {stored_path}") from exc

        # 客户端给的路径不可信:必须落在**本会话**的 media 目录内。
        if not _ensure_within(src_resolved, session_media_dir):
            raise MCodeMediaError(
                f"素材 {media_id} 不在本会话素材目录内,已拒绝: {src_resolved}"
                f"(期望位于 {session_media_dir} 之下)"
            )
        if not src_resolved.is_file():
            raise MCodeMediaError(f"素材 {media_id} 不存在或不是普通文件: {src_resolved}")

        # 命名:<media_id>_<净化主干><扩展名>
        ext = src_resolved.suffix
        stem_from_name = _sanitize_stem(Path(filename).stem) if filename else ""
        stem = f"{media_id}_{stem_from_name or _sanitize_stem(src_resolved.stem)}"

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MCodeMediaError(f"创建素材目录失败: {target_dir}({exc})") from exc

        src_sha = _sha256(src_resolved)
        src_size = src_resolved.stat().st_size

        target, reused = _resolve_target(target_dir, stem, ext, src_sha)
        part = target_dir / f"{target.name}.part"

        # 幂等:目标已存在且内容一致 → 直接复用,不产生第二份。
        if reused:
            prepared.append({
                "media_id": media_id,
                "filename": filename or target.name,
                "path": str(target),
                "size": src_size,
                "sha256": src_sha,
                "reused": True,
            })
            continue

        try:
            shutil.copyfile(src_resolved, part)
        except OSError as exc:
            _unlink_quiet(part)
            raise MCodeMediaError(f"复制素材失败: {src_resolved} -> {part}({exc})") from exc

        # 校验:大小 + SHA-256 都对得上,才允许原子改名落地。
        try:
            part_size = part.stat().st_size
            if part_size != src_size:
                raise MCodeMediaError(
                    f"素材 {media_id} 复制后大小不一致: 源 {src_size} vs 副本 {part_size}"
                )
            part_sha = _sha256(part)
            if part_sha != src_sha:
                raise MCodeMediaError(
                    f"素材 {media_id} 复制后 SHA-256 不一致: 源 {src_sha} vs 副本 {part_sha}"
                )
            # 目标路径再过一次白名单判定。
            safety = _safety_module()
            safety.safe_media_path(str(target), roots=roots, must_exist=False)
            os.replace(part, target)
        except Exception as exc:  # noqa: BLE001
            _unlink_quiet(part)
            if isinstance(exc, MCodeMediaError):
                raise
            raise MCodeMediaError(f"素材 {media_id} 校验失败: {exc}") from exc

        prepared.append({
            "media_id": media_id,
            "filename": filename or target.name,
            "path": str(target),
            "size": src_size,
            "sha256": src_sha,
            "reused": False,
        })

    return prepared


def _unlink_quiet(path: Path) -> None:
    """删文件,失败只记日志(清理不该把主流程带崩)。"""
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("清理临时文件失败 %s: %s", path, exc)


# --------------------------------------------------------------------------
# 素材清单
# --------------------------------------------------------------------------

def build_media_manifest(
    media_dir: Path,
    allowed_roots: Sequence[Path],
) -> List[Dict[str, Any]]:
    """列出目录内可用素材,返回**服务端生成**的绝对路径清单。

    每个文件都过一遍 ``agent_gate.safety.safe_media_path`` 白名单判定。
    不合规的直接**排除**并记日志 —— **不扩大白名单**来迁就。
    """
    out: List[Dict[str, Any]] = []
    directory = Path(media_dir)
    if not directory.is_dir():
        return out

    roots = [Path(r).resolve() for r in allowed_roots]
    safety = _safety_module()

    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        try:
            safe = safety.safe_media_path(str(entry), roots=roots, must_exist=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("素材 %s 未通过白名单判定,已排除: %s", entry, exc)
            continue
        try:
            size = safe.stat().st_size
        except OSError:
            continue
        out.append({"name": safe.name, "path": str(safe), "size": size})
    return out


def media_manifest_lines(manifest: Iterable[Mapping[str, Any]]) -> List[str]:
    """把清单渲染成给模型看的文本行。"""
    lines: List[str] = []
    for item in manifest or []:
        name = str(item.get("name") or "")
        path = str(item.get("path") or "")
        size = item.get("size")
        if name and path:
            lines.append(f"- {name} ({size} bytes)\n  {path}")
    return lines

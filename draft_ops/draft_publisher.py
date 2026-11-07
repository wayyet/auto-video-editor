"""staging 草稿 → 剪映用户草稿库发布(2026-11-06 阶段 1)。

计划 §7.2/§8 阶段 1 的核心组件 ``draft_publisher.py``:

    staging(任务目录 drafts/)--publish--> <JIANYING_DRAFT_ROOT>/AutoVideo_<thread_id>/

流程(计划 §8 阶段 1 任务"发布前校验、复制/写入、元数据补齐、根索引更新、
发布后再读校验"):

1. **发布前校验**:staging 双文件齐全、可解析、content == info(与
   ``safe_write_draft`` 的写入产物契约一致);
2. **剪映进程门禁**:检测到 JianyingPro.exe 在跑 → 默认拒绝发布(fail-closed,
   ``config.JY_PUBLISH_FAIL_ON_JIANYING_RUNNING``;发布目标是剪映自己的草稿库,
   客户端打开期间写入正是阶段 3 要消灭的竞争场景);
3. **复制/写入**:首次发布走"临时目录 + ``os.replace``"两步,目录级原子出现,
   不会让剪映首页看到半成品;重发布(目录已存在)就地覆盖 + 校验;
4. **元数据补齐 + 根索引注册**:委托 :mod:`draft_ops.jianying_registry`
   (draft_meta_info.json + root_meta_info.json upsert,已存在文件先备份);
5. **发布后再读校验**:回读正式目录双文件,与 staging 逐字节语义一致才算成功。

失败一律抛 :class:`DraftPublishError`,**不**删除任何一侧文件 —— 调用方
(``nodes/node_05_generate_draft.py``)按"降级继续用 staging"处理,人工关卡
仍可走创建台路径排障;smoke 可用 ``JY_DRAFT_PUBLISH_ENABLED=0`` 整体关闭。

⚠️ 剪映目录结构兼容性(计划 §8 阶段 1):本模块保证"双文件 + 双索引 + 再读
一致"这些**仓库可验证**的契约。2026-11-07 实机验收发现仅复制 storyline 4 键
格式会"首页可列、双击必失败"——本模块现在对旧格式 staging 先做 storyline→
原生转换(:mod:`storyline.native_draft`)再落正式目录;新链路 node_05 已直接
产原生格式,此处幂等直通。字段级依据见 :mod:`storyline.native_draft` docstring。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from storyline.native_draft import (
    is_native_draft,
    storyline_to_native,
    write_sidecar,
)

from draft_ops.jianying_registry import (
    JYRegistryError,
    compute_draft_duration_us,
    official_draft_dir,
    read_json_dict,
    register_draft,
)

#: 与 safe_write_guard.check_jianying_not_running 同签名:True = 剪映在跑
ProcChecker = Callable[[], bool]


class DraftPublishError(Exception):
    """发布失败;``reason`` 供上层写 error_log / 事件。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _default_proc_checker() -> bool:
    from draft_ops.safe_write_guard import check_jianying_not_running

    return check_jianying_not_running()


def _load_staging(staging_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """读 staging 双文件并做发布前校验;任何不满足都抛 :class:`DraftPublishError`。"""
    content_path = staging_dir / "draft_content.json"
    info_path = staging_dir / "draft_info.json"
    for p in (content_path, info_path):
        if not p.is_file():
            raise DraftPublishError(f"staging 缺少 {p.name}: {staging_dir}")
    try:
        content = (content_path).read_text(encoding="utf-8")
        info = (info_path).read_text(encoding="utf-8")
    except OSError as e:
        raise DraftPublishError(f"staging 读取失败: {e}") from e
    try:
        content_obj = json.loads(content)
        info_obj = json.loads(info)
    except ValueError as e:
        raise DraftPublishError(f"staging JSON 解析失败: {e}") from e
    if content != info:
        # 与 safe_write_draft 的双写契约一致:两个文件字节级一致
        raise DraftPublishError(
            "staging 双文件内容不一致(draft_content.json != draft_info.json)"
        )
    return content_obj, info_obj


def _copy_tree(src: Path, dst: Path) -> None:
    """复制 staging → 正式目录;排除 .snapshots(快照机制不该进剪映草稿库)。"""
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".snapshots"),
    )


def _verify_published(target: Path, staging_content_obj: dict[str, Any]) -> None:
    """发布后再读校验:双文件存在、可解析、与 staging content 语义一致。"""
    try:
        target_content = json.loads((target / "draft_content.json").read_text(encoding="utf-8"))
        target_info = json.loads((target / "draft_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DraftPublishError(f"发布后回读失败: {target} ({e})") from e
    if target_content != staging_content_obj:
        raise DraftPublishError("发布后校验失败:正式目录 draft_content.json 与 staging 不一致")
    if target_content != target_info:
        raise DraftPublishError("发布后校验失败:正式目录双文件不一致")


def _write_payload(target: Path, payload_obj: dict[str, Any]) -> None:
    """把(可能已转换的)payload 原子双写进正式目录,覆盖 copytree 产物。"""
    serialized = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
    for name in ("draft_content.json", "draft_info.json"):
        p = target / name
        fd, tmp_path = tempfile.mkstemp(dir=target, prefix=".payload_", suffix=".json")
        tmp = Path(tmp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            if tmp.exists():
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise


def _materialize_payload(
    target: Path, native_obj: dict[str, Any], sidecar_obj: dict[str, Any] | None
) -> None:
    """staging 为 storyline 旧格式时,把转换产物(双文件+sidecar)落进正式目录。"""
    if sidecar_obj is not None:
        try:
            write_sidecar(target, sidecar_obj)
        except Exception:  # noqa: BLE001 — sidecar 是元数据存档,失败不阻断发布
            pass
    _write_payload(target, native_obj)


def _generate_cover(target: Path, payload_obj: dict[str, Any]) -> bool:
    """best-effort 封面:首个视频素材 ffmpeg 抽帧 → ``draft_cover.jpg``。

    封面只影响首页磁贴(缺失时磁贴黑块,不阻碍打开 —— 参考实现已证),任何
    失败静默跳过;已有封面直接复用。
    """
    try:
        if shutil.which("ffmpeg") is None:
            return False
        videos = ((payload_obj.get("materials") or {}).get("videos") or [])
        src = next((str(v.get("path")) for v in videos if v.get("path")), None)
        if not src or not Path(src).is_file():
            return False
        out = target / "draft_cover.jpg"
        if out.is_file():
            return True
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", src, "-frames:v", "1", str(out)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not out.is_file():
            if out.exists():
                out.unlink(missing_ok=True)
            return False
        return True
    except Exception:  # noqa: BLE001 — 封面是增强项
        return False


def publish_draft(
    staging_dir: Path,
    *,
    thread_id: str,
    duration_us: int | None = None,
    proc_checker: ProcChecker | None = None,
    fail_on_running: bool | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """把 staging 草稿发布为剪映草稿库中的正式草稿并注册(幂等)。

    Args:
        staging_dir: staging 草稿目录(含 draft_content.json / draft_info.json)。
        thread_id: 任务标识(决定正式目录名 ``AutoVideo_<thread_id>``)。
        duration_us: 草稿时长(微秒);缺省从草稿 ``target_timerange`` 计算。
        proc_checker: 剪映进程检测(可注入;缺省 stdlib tasklist 检测)。
        fail_on_running: 剪映在跑时是否拒绝;缺省读
            ``config.JY_PUBLISH_FAIL_ON_JIANYING_RUNNING``。
        root: 草稿库根目录(可注入;缺省 ``config.JIANYING_DRAFT_ROOT``)。

    Returns:
        ``{"ok": True, "published_dir": str, "draft_content_path": str,
        "draft_info_path": str, "duration_us": int|None, "reused": bool,
        "source_dir": str, "registry": {...}}``。

    Raises:
        DraftPublishError: staging 无效 / 剪映在跑(严格模式)/ 注册表损坏 /
            复制或回读校验失败。
    """
    staging_dir = Path(staging_dir)
    if not staging_dir.is_dir():
        raise DraftPublishError(f"staging 目录不存在: {staging_dir}")

    if fail_on_running is None:
        import config

        fail_on_running = bool(config.JY_PUBLISH_FAIL_ON_JIANYING_RUNNING)
    check = proc_checker if proc_checker is not None else _default_proc_checker
    if fail_on_running and check():
        raise DraftPublishError(
            "检测到剪映客户端正在运行,拒绝发布草稿(严格互斥);"
            "请保存并关闭剪映后重试,或设 JY_PUBLISH_FAIL_ON_JIANYING_RUNNING=0 临时放行"
        )

    content_obj, _info_obj = _load_staging(staging_dir)

    # 2026-11-07 导入修复:staging 若仍是 storyline 4 键格式(旧线程重发布),
    # 先转剪映原生 schema 再落正式目录;新链路 node_05 已产原生,此处幂等直通。
    native_obj = content_obj
    sidecar_obj: dict[str, Any] | None = None
    if not is_native_draft(content_obj):
        try:
            native_obj, sidecar_obj = storyline_to_native(content_obj)
        except Exception as exc:  # noqa: BLE001
            raise DraftPublishError(f"storyline→原生草稿转换失败: {exc}") from exc
    if duration_us is None:
        duration_us = compute_draft_duration_us(native_obj)

    if root is not None:
        from draft_ops.jianying_registry import validate_draft_root

        final_root = validate_draft_root(Path(root))
    else:
        from draft_ops.jianying_registry import resolve_draft_root

        final_root = resolve_draft_root()
    target = official_draft_dir(final_root, thread_id)

    reused = (target / "draft_content.json").is_file()
    if reused:
        # 重发布:就地覆盖(幂等;复制非原子,但 verify 兜底)
        _copy_tree(staging_dir, target)
        if native_obj is not content_obj:
            _materialize_payload(target, native_obj, sidecar_obj)
    else:
        # 首次发布:同盘临时目录复制 → os.replace 原子出现,剪映首页不会看到半成品
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.tmp_")
        os.close(fd)
        tmp_dir = Path(tmp_name)
        try:
            os.remove(tmp_dir)  # mkstemp 生成的是文件;要的是同盘空目录名
            _copy_tree(staging_dir, tmp_dir)
            if native_obj is not content_obj:
                _materialize_payload(tmp_dir, native_obj, sidecar_obj)
            os.replace(tmp_dir, target)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    # 封面 best-effort(注册前生成,registry 的 draft_cover 字段才能引用到)
    _generate_cover(target, native_obj)

    # 元数据补齐 + 根索引注册(重发布复用既有 draft_id,保证幂等)
    existing_meta = read_json_dict(target / "draft_meta_info.json")
    reuse_id = str(existing_meta.get("draft_id")) if existing_meta and existing_meta.get("draft_id") else None
    try:
        registry_result = register_draft(
            target,
            thread_id=thread_id,
            duration_us=duration_us,
            root=final_root,
            draft_id=reuse_id,
        )
    except JYRegistryError as e:
        raise DraftPublishError(f"草稿注册失败: {e}") from e

    _verify_published(target, native_obj)

    return {
        "ok": True,
        "published_dir": str(target),
        "draft_content_path": str(target / "draft_content.json"),
        "draft_info_path": str(target / "draft_info.json"),
        "duration_us": duration_us,
        "reused": reused,
        "source_dir": str(staging_dir),
        "registry": registry_result,
    }


__all__ = [
    "DraftPublishError",
    "publish_draft",
]

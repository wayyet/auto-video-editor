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
一致"这些**仓库可验证**的契约;剪映客户端能否识别、首页能否列出、时间轴能否
渲染,以阶段 5 实机验收为准(本机 5.9.0.11632,草稿库当前为空,无对照样本)。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

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
        import json

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
    import json

    try:
        target_content = json.loads((target / "draft_content.json").read_text(encoding="utf-8"))
        target_info = json.loads((target / "draft_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DraftPublishError(f"发布后回读失败: {target} ({e})") from e
    if target_content != staging_content_obj:
        raise DraftPublishError("发布后校验失败:正式目录 draft_content.json 与 staging 不一致")
    if target_content != target_info:
        raise DraftPublishError("发布后校验失败:正式目录双文件不一致")


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
    if duration_us is None:
        duration_us = compute_draft_duration_us(content_obj)

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
    else:
        # 首次发布:同盘临时目录复制 → os.replace 原子出现,剪映首页不会看到半成品
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.tmp_")
        os.close(fd)
        tmp_dir = Path(tmp_name)
        try:
            os.remove(tmp_dir)  # mkstemp 生成的是文件;要的是同盘空目录名
            _copy_tree(staging_dir, tmp_dir)
            os.replace(tmp_dir, target)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

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

    _verify_published(target, content_obj)

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

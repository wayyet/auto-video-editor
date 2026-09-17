"""``outputs/{job_id}/`` 隔离与幂等键。

设计(ADR-003 / Phase 2):
- 每个 LangGraph job 写到独立目录 ``outputs/{job_id}/``,里面再分
  ``canonical_timeline.json / storyline_plan.json / draft_content.json /
  manifest.json / artifacts/ / logs/``。
- 幂等键 = ``sha256(job_id + input_sha256 + config_sha256)``。
- 命中已存在的 ``manifest.json`` → 跳过 node_02 / node_04,直接读
  ``draft_path`` 进入关卡①。

文件结构(对照 plan §6.4):

    outputs/{job_id}/
    ├── manifest.json          # 任务级元数据 + 幂等键
    ├── canonical_timeline.json
    ├── storyline_plan.json
    ├── draft_content.json
    ├── artifacts/             # 镜像 .storyline/.server_cache/<session_id>/*.json
    ├── checkpoints/           # 镜像 LangGraph checkpoint(可选)
    └── logs/
        └── storyline.jsonl    # 每行一次 call_tool 记录
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 默认根目录(可被环境变量 AUTO_VIDEO_EDITOR_OUTPUTS_ROOT 覆盖)
# ---------------------------------------------------------------------------
DEFAULT_OUTPUTS_ROOT: Path = Path(
    os.environ.get(
        "AUTO_VIDEO_EDITOR_OUTPUTS_ROOT",
        str(Path(__file__).resolve().parent.parent / "outputs"),
    )
)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
@dataclass
class OutputManifest:
    """``outputs/{job_id}/manifest.json`` 的内存表示。"""

    job_id: str
    created_at_ms: int
    input_sha256: str
    config_sha256: str
    storyline_session_id: Optional[str] = None
    storyline_artifact_ids: list[str] = field(default_factory=list)
    storyline_skills: list[str] = field(default_factory=list)
    draft_path: Optional[str] = None
    state_version: str = "1.0"
    idempotency_key: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutputManifest":
        return cls(
            job_id=data["job_id"],
            created_at_ms=int(data["created_at_ms"]),
            input_sha256=data["input_sha256"],
            config_sha256=data["config_sha256"],
            storyline_session_id=data.get("storyline_session_id"),
            storyline_artifact_ids=list(data.get("storyline_artifact_ids") or []),
            storyline_skills=list(data.get("storyline_skills") or []),
            draft_path=data.get("draft_path"),
            state_version=data.get("state_version", "1.0"),
            idempotency_key=data.get("idempotency_key"),
        )


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------
def compute_input_sha256(*, video_path: str, extras: dict[str, Any] | None = None) -> str:
    """算 input_sha256(视频路径 + 额外 metadata)。"""
    h = hashlib.sha256()
    h.update(str(video_path).encode("utf-8"))
    if extras:
        h.update(json.dumps(extras, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def compute_config_sha256(config_snapshot: dict) -> str:
    """算 config_sha256(把当前 config 取关键字段后序列化)。"""
    return hashlib.sha256(
        json.dumps(config_snapshot, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def compute_idempotency_key(
    *,
    job_id: str,
    input_sha256: str,
    config_sha256: str,
) -> str:
    """三段哈希拼接 = 幂等键。命中即可跳过 node_02/04。"""
    return hashlib.sha256(
        f"{job_id}|{input_sha256}|{config_sha256}".encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# outputs/{job_id}/ 目录结构
# ---------------------------------------------------------------------------
@dataclass
class OutputJobPaths:
    """``outputs/{job_id}/`` 下所有路径的集中句柄。"""

    job_id: str
    root: Path
    manifest: Path
    canonical_timeline: Path
    storyline_plan: Path
    draft_content: Path
    artifacts_dir: Path
    checkpoints_dir: Path
    logs_dir: Path
    storyline_log: Path

    @classmethod
    def for_job(cls, job_id: str, *, root: Optional[Path] = None) -> "OutputJobPaths":
        base = (root or DEFAULT_OUTPUTS_ROOT) / job_id
        return cls(
            job_id=job_id,
            root=base,
            manifest=base / "manifest.json",
            canonical_timeline=base / "canonical_timeline.json",
            storyline_plan=base / "storyline_plan.json",
            draft_content=base / "draft_content.json",
            artifacts_dir=base / "artifacts",
            checkpoints_dir=base / "checkpoints",
            logs_dir=base / "logs",
            storyline_log=base / "logs" / "storyline.jsonl",
        )

    def ensure(self) -> None:
        """创建所有子目录(已存在则 noop)。"""
        for sub in (
            self.root,
            self.artifacts_dir,
            self.checkpoints_dir,
            self.logs_dir,
        ):
            sub.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Manifest 读写 + 命中判断
# ---------------------------------------------------------------------------
def load_manifest(paths: OutputJobPaths) -> Optional[OutputManifest]:
    """读 ``manifest.json``;不存在返回 None。"""
    if not paths.manifest.exists():
        return None
    try:
        data = json.loads(paths.manifest.read_text(encoding="utf-8"))
        return OutputManifest.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def write_manifest(paths: OutputJobPaths, manifest: OutputManifest) -> None:
    """写 ``manifest.json``(用 os.replace 做原子覆盖)。"""
    paths.ensure()
    tmp = paths.root / ".manifest.json.tmp"
    tmp.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, paths.manifest)


def is_idempotent_hit(
    paths: OutputJobPaths,
    *,
    expected_key: str,
) -> bool:
    """``manifest.json`` 已存在且 idempotency_key 一致 → True(可跳过重跑)。"""
    manifest = load_manifest(paths)
    if manifest is None:
        return False
    return manifest.idempotency_key == expected_key


def build_manifest(
    *,
    job_id: str,
    video_path: str,
    config_snapshot: dict[str, Any],
    storyline_session_id: Optional[str] = None,
    storyline_artifact_ids: Optional[list[str]] = None,
    draft_path: Optional[str] = None,
    extras: dict[str, Any] | None = None,
) -> OutputManifest:
    """构建 :class:`OutputManifest`(同时算幂等键)。"""
    input_sha = compute_input_sha256(video_path=video_path, extras=extras)
    config_sha = compute_config_sha256(config_snapshot)
    return OutputManifest(
        job_id=job_id,
        created_at_ms=int(time.time() * 1000),
        input_sha256=input_sha,
        config_sha256=config_sha,
        storyline_session_id=storyline_session_id,
        storyline_artifact_ids=list(storyline_artifact_ids or []),
        draft_path=draft_path,
        idempotency_key=compute_idempotency_key(
            job_id=job_id,
            input_sha256=input_sha,
            config_sha256=config_sha,
        ),
    )


# ---------------------------------------------------------------------------
# storyline.jsonl 日志追加
# ---------------------------------------------------------------------------
def append_storyline_log(
    paths: OutputJobPaths,
    *,
    tool_name: str,
    artifact_id: str,
    duration_ms: int,
    error_code: Optional[str] = None,
    job_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    """追加一行 storyline.jsonl,便于监控消费(Phase 4 接心跳)。"""
    paths.ensure()
    record = {
        "ts_ms": int(time.time() * 1000),
        "job_id": job_id or paths.job_id,
        "session_id": session_id,
        "tool_name": tool_name,
        "artifact_id": artifact_id,
        "duration_ms": int(duration_ms),
        "error_code": error_code,
    }
    with paths.storyline_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


__all__ = [
    "DEFAULT_OUTPUTS_ROOT",
    "OutputManifest",
    "OutputJobPaths",
    "compute_input_sha256",
    "compute_config_sha256",
    "compute_idempotency_key",
    "load_manifest",
    "write_manifest",
    "is_idempotent_hit",
    "build_manifest",
    "append_storyline_log",
]
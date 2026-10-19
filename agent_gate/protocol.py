"""闸门协议:常量、时间戳、task_id 计算(只依赖标准库)。"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_S = 1800.0   # 单个 LLM 任务最长等待 30 分钟
DEFAULT_POLL_S = 0.5         # 轮询结果文件的间隔
DEFAULT_MAX_ATTEMPTS = 3     # Agent 提交不合格结果的最大次数
HEARTBEAT_S = float(os.environ.get("AGENT_GATE_HEARTBEAT_S", "5"))      # worker 心跳间隔
STALE_AFTER_S = float(os.environ.get("AGENT_GATE_STALE_AFTER_S", "30"))  # 心跳超过这么久没更新 = worker 已死
#: ``starting`` 状态的宽限期。worker 刚被拉起时,``spawn_worker`` 先写一份
#: ``state="starting"`` 的 status,真正的 worker 进程还要一会儿才能 import 完
#: graph 并写出第一次心跳(Windows 上实测要好几秒)。这段时间里如果沿用
#: ``STALE_AFTER_S``(默认 30s,测试里被调到 1.5s)去判心跳过期,会把
#: **正在正常启动**的 worker 误判成 "crashed" → drive 误报退出码 5,
#: 甚至去重启一个本来好好的 worker。所以 starting 用自己的、更宽的阈值。
STARTING_STALE_AFTER_S = float(
    os.environ.get("AGENT_GATE_STARTING_STALE_AFTER_S", "120")
)

# worker 状态(status.json 的 state 字段)
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_WAITING_HUMAN = "waiting_human"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CRASHED = "crashed"   # 只由 status 推断:状态是 running 但心跳过期
TERMINAL_STATES = {STATE_WAITING_HUMAN, STATE_COMPLETED, STATE_FAILED, STATE_CRASHED}


def utc_now() -> str:
    """固定格式 UTC 时间戳;字符串可直接比较先后。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def canonical_json(obj: Any) -> str:
    """键排序、无多余空格的 JSON,用于哈希。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def task_id_for(payload: dict[str, Any]) -> str:
    """相同请求 → 相同 task_id。重启后能直接复用上次已提交的结果。"""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "t_" + digest[:16]

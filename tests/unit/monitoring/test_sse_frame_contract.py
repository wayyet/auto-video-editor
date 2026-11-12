r"""SSE 帧契约(后端一侧):to_sse_frame 产出的帧,是前端 eventFromSse 的输入样本。

本测试用真实的 make_event + to_sse_frame 生成帧,解析后的内容必须与
web/tests/fixtures/sse_frames.txt 一致(不比原始文本:后端对 payload 键的遍历顺序
取决于进程的哈希种子,同一事件在不同进程里 JSON 键顺序可能不同);Node 侧
(web/tests/sse_frame_contract.test.mjs)读的是同一份文件。
后端改了帧格式 → 本测试失败,提醒同步前端;前端改了读取逻辑 → Node 用例失败。

重新生成样本(在仓库根目录执行):
    .\.venv\Scripts\python.exe -m tests.unit.monitoring.test_sse_frame_contract
"""
from __future__ import annotations

import json
from pathlib import Path

from monitoring.workflow_events import make_event, to_sse_frame

FIXTURE = Path(__file__).resolve().parents[3] / "web" / "tests" / "fixtures" / "sse_frames.txt"
TID = "console-contract-0001"
AT = "2026-11-13T08:00:00.000Z"


def build_frames() -> str:
    events = [
        make_event(
            "node.progress", thread_id=TID, run_id="run-1", node_id="generate_draft", occurred_at=AT,
            payload={"summary": {"monitor_event": "progress", "event": "draft.generated",
                                 "draft_dir": "AutoVideo_x"}},
        ),
        make_event(
            "node.succeeded", thread_id=TID, run_id="run-1", node_id="generate_draft", occurred_at=AT,
            payload={"duration_ms": 1250},
        ),
        make_event(
            "node.failed", thread_id=TID, run_id="run-1", node_id="node_13_adjust_volume", occurred_at=AT,
            payload={"error": {"type": "ValueError", "message": "boom"}, "duration_ms": 80},
        ),
        make_event(
            "node.waiting_input", thread_id=TID, run_id="run-1", node_id="node_12_human_add_bgm", occurred_at=AT,
            payload={"waiting_kind": "web_file_handshake", "detail": {"checkpoints": ["②"]}},
        ),
        make_event(
            "branch.selected", thread_id=TID, run_id="run-1", node_id="node_06_human_reorder", occurred_at=AT,
            payload={"label": "join:a+b:gate", "inferred": True, "detail": {"from": ["a", "b"]}},
        ),
        make_event(
            "node.progress", thread_id=TID, run_id="run-1", node_id="join_before_delivery", occurred_at=AT,
            payload={"summary": {"monitor_event": "progress", "event": "delivery.qa_issues", "issue_count": 1,
                                 "issues": [{"severity": "error", "check": "转场", "draft": "zh",
                                             "detail": "素材桶无转场"}]}},
        ),
    ]
    return "".join(to_sse_frame({**e, "id": 2000 + i}) for i, e in enumerate(events, start=1))


def parse_frames(text: str) -> list[tuple[str, str, dict]]:
    """按 SSE 规范拆帧,返回 [(id, event, data 的 dict)];与键顺序无关。"""
    frames: list[tuple[str, str, dict]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        frame_id, event_type, data = "", "", {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                frame_id = line[4:]
            elif line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        frames.append((frame_id, event_type, data))
    return frames


def test_sse_frames_match_fixture() -> None:
    assert FIXTURE.exists(), f"缺少契约样本 {FIXTURE};先运行本模块生成"
    assert parse_frames(FIXTURE.read_text(encoding="utf-8")) == parse_frames(build_frames())


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(build_frames(), encoding="utf-8", newline="\n")
    print(f"已写入 {FIXTURE}")

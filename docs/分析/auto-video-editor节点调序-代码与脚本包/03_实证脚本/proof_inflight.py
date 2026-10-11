"""实证:挂起中的旧版本线程,用新版图恢复会发生什么。
用法:
  python proof_inflight.py <仓库根> suspend <工作目录> <停在哪个关卡: ①|②>   # 用 A 版本跑到该关卡挂起,退出进程
  python proof_inflight.py <仓库根> resume  <工作目录>                      # 用 B 版本(新进程)恢复到 END
checkpointer 用 SqliteSaver 文件,模拟"进程退出 → 新版本发布 → 新进程恢复"。"""
import collections, json, os, sys
from pathlib import Path

REPO, PHASE, ROOT = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
os.chdir(REPO); sys.path.insert(0, REPO)
# main 2026-10 起 import_video 节点默认等网页【导入视频】按钮最多 600s;脚本里没人点,
# 按 tests/conftest.py 同样的做法压成 0(节点立即降级并继续)。旧版本没有这两个变量,设了也无副作用。
os.environ.setdefault("IMPORT_VIDEO_WAIT_TIMEOUT_S", "0")
os.environ.setdefault("IMPORT_VIDEO_POLL_INTERVAL_S", "0")
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from graph import build_graph
import nodes.node_02_launch_openstoryline as m2
import nodes.node_03_open_preview as m3

class _P: pid = 11111
m2.subprocess = type("S", (), {"Popen": staticmethod(lambda *a, **k: _P()), "PIPE": -1})
m2._wait_for_ready = lambda url, t: True
m3.subprocess = type("S", (), {"Popen": staticmethod(lambda *a, **k: _P())})

ROOT.mkdir(parents=True, exist_ok=True)
draft_dir = ROOT / "drafts" / "demo"; draft_dir.mkdir(parents=True, exist_ok=True)
draft_file = draft_dir / "draft_content.json"
os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str(draft_dir)
config = {"configurable": {"thread_id": "inflight"}}
db = str(ROOT / "ckpt.sqlite")

def drive(g, inp, stop_label=None):
    steps, stops = [], []
    for _ in range(14):
        for ev in g.stream(inp, config, stream_mode="debug"):
            if ev["type"] == "task": steps.append((ev["step"], ev["payload"]["name"]))
        snap = g.get_state(config)
        if not snap.next: return steps, stops, True
        pend = list(snap.interrupts)
        labels = [str((i.value or {}).get("checkpoint")) for i in pend]
        stops.append(labels)
        if stop_label and stop_label in labels: return steps, stops, False
        inp = Command(resume={i.id: True for i in pend})
    raise SystemExit("未结束")

with SqliteSaver.from_conn_string(db) as cp:
    g = build_graph(checkpointer=cp, thread_id="inflight", start_heartbeat_thread=False)
    if PHASE == "suspend":
        stop = sys.argv[4]
        dur = 35_000_000
        draft_file.write_text(json.dumps({"canvas_config": {"width": 1080, "height": 1920}, "duration": dur,
            "materials": {"videos": [{"id": "v1"}]},
            "tracks": [{"type": "video", "fps": 30, "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": dur}}]}]},
            ensure_ascii=False), encoding="utf-8")
        state = {"session_id": "inflight", "video_input_path": "C:/tmp/fake.mp4", "draft_path": str(draft_file),
                 "draft_encryption_status": "plaintext", "draft_version_strategy": "strategy_a_version_lock",
                 "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
                 "error_log": [], "status_log": ["node_05_generate_draft_done"]}
        steps, stops, done = drive(g, state, stop_label=stop)
        last = max(s for s, _ in steps)
        print(f"[{Path(REPO).name}] 已在关卡{stop}挂起并退出进程;挂起序列={stops};已跑到 step {last}")
        ran = sorted({n for _, n in steps})
        print("   已执行节点:", [n for n in ran if n.startswith("node_") or n in ("fork_draft_for_english_branch","bridge_snapshot2")])
    else:
        snap = g.get_state(config)
        print(f"[{Path(REPO).name}] 新进程读到挂起快照:next={list(snap.next)}")
        steps, stops, done = drive(g, Command(resume={i.id: True for i in snap.interrupts}))
        cnt = collections.OrderedDict()
        for s, n in steps: cnt.setdefault(s, []).append(n)
        print("   恢复后依次执行:", [ns for ns in cnt.values()])
        v = g.get_state(config).values
        print("   结束:", done, "| 后续又挂起:", stops)
        print("   covers 条数 =", len(v.get("covers") or []), "| 英文封面齐全 =", all(c.get("en_path") for c in (v.get("covers") or [])) and bool(v.get("covers")))
        print("   join_qa_issues =", v.get("join_qa_issues"))

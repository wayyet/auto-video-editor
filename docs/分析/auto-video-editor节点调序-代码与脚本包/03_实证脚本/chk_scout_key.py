import json, os, sys, tempfile
from pathlib import Path
REPO = sys.argv[1]; os.chdir(REPO); sys.path.insert(0, REPO)
# main 2026-10 起 import_video 节点默认等网页【导入视频】按钮最多 600s;脚本里没人点,
# 按 tests/conftest.py 同样的做法压成 0(节点立即降级并继续)。旧版本没有这两个变量,设了也无副作用。
os.environ.setdefault("IMPORT_VIDEO_WAIT_TIMEOUT_S", "0")
os.environ.setdefault("IMPORT_VIDEO_POLL_INTERVAL_S", "0")
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from graph import build_graph
from state import WorkflowState
import nodes.node_02_launch_openstoryline as m2, nodes.node_03_open_preview as m3
class _P: pid = 1
m2.subprocess = type("S", (), {"Popen": staticmethod(lambda *a, **k: _P()), "PIPE": -1}); m2._wait_for_ready = lambda u, t: True
m3.subprocess = type("S", (), {"Popen": staticmethod(lambda *a, **k: _P())})
root = Path(tempfile.mkdtemp()); dd = root / "d"; dd.mkdir()
f = dd / "draft_content.json"; dur = 35_000_000
f.write_text(json.dumps({"canvas_config": {}, "duration": dur, "materials": {"videos": [{"id": "v"}]},
  "tracks": [{"type": "video", "fps": 30, "segments": [{"id": "s", "target_timerange": {"start": 0, "duration": dur}}]}]}), encoding="utf-8")
os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str(dd)
g = build_graph(checkpointer=InMemorySaver(), thread_id="k", start_heartbeat_thread=False); c = {"configurable": {"thread_id": "k"}}
inp = {"session_id": "k", "video_input_path": "C:/x.mp4", "draft_path": str(f), "draft_encryption_status": "plaintext",
       "draft_version_strategy": "strategy_a_version_lock", "shot_plan": {"video_path": "", "shots": [{"id": "a", "start_s": 0, "end_s": 5}]},
       "error_log": [], "status_log": ["node_05_generate_draft_done"]}
for _ in range(10):
    list(g.stream(inp, c, stream_mode="updates")); s = g.get_state(c)
    if not s.next: break
    inp = Command(resume={i.id: True for i in s.interrupts})
print("state.py 是否声明 subtitle_scout_report:", "subtitle_scout_report" in WorkflowState.__annotations__)
print("整图运行后 state 里的 subtitle_scout_report =", g.get_state(c).values.get("subtitle_scout_report"))

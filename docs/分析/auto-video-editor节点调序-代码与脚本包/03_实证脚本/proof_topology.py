"""实证:用仓库真实节点整图运行,记录超步(superstep)顺序 + 英文草稿内容。
用法: python proof_topology.py <仓库根目录> <normal|cp3>
只对 node_02/03 的进程拉起打桩(与仓库集成测试一致);其余节点全部是真实代码(默认 Mock ASR/翻译/TTS)。"""
import contextlib, json, os, sys, tempfile, collections
from pathlib import Path
from unittest.mock import patch

REPO, SCENARIO = sys.argv[1], sys.argv[2]
os.chdir(REPO); sys.path.insert(0, REPO)
# main 2026-10 起 import_video 节点默认等网页【导入视频】按钮最多 600s;脚本里没人点,
# 按 tests/conftest.py 同样的做法压成 0(节点立即降级并继续)。旧版本没有这两个变量,设了也无副作用。
os.environ.setdefault("IMPORT_VIDEO_WAIT_TIMEOUT_S", "0")
os.environ.setdefault("IMPORT_VIDEO_POLL_INTERVAL_S", "0")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from graph import build_graph
import nodes.node_02_launch_openstoryline as m2
import nodes.node_03_open_preview as m3

class _P:  # 假进程
    pid = 11111
m2.subprocess = type("S", (), {"Popen": staticmethod(lambda *a, **k: _P()), "PIPE": -1})
m2._wait_for_ready = lambda url, t: True
m3.subprocess = type("S", (), {"Popen": staticmethod(lambda *a, **k: _P())})

root = Path(tempfile.mkdtemp(prefix="proof_"))
draft_dir = root / "drafts" / "demo"; draft_dir.mkdir(parents=True)
dur = 35_000_000
draft_file = draft_dir / "draft_content.json"
draft_file.write_text(json.dumps({
    "canvas_config": {"width": 1080, "height": 1920}, "duration": dur,
    "materials": {"videos": [{"id": "v1"}]},
    "tracks": [{"type": "video", "fps": 30, "segments": [{"id": "s1", "target_timerange": {"start": 0, "duration": dur}}]}],
}, ensure_ascii=False), encoding="utf-8")
os.environ["AUTO_VIDEO_EDITOR_DRAFT_DIR"] = str(draft_dir)

# 注意:这里**不**预置 asr_segments_zh —— 让 node_08 自己产出(与真实运行一致)
state = {
    "session_id": "proof", "video_input_path": "C:/tmp/fake.mp4", "draft_path": str(draft_file),
    "draft_encryption_status": "plaintext", "draft_version_strategy": "strategy_a_version_lock",
    "shot_plan": {"video_path": "", "shots": [{"id": "sh1", "start_s": 0.0, "end_s": 5.0}]},
    "error_log": [], "status_log": ["node_05_generate_draft_done"],
}
g = build_graph(checkpointer=InMemorySaver(), thread_id="proof", start_heartbeat_thread=False)
config = {"configurable": {"thread_id": "proof"}}

steps, stops = [], []
def stream(inp):
    for ev in g.stream(inp, config, stream_mode="debug"):
        if ev["type"] == "task":
            steps.append((ev["step"], ev["payload"]["name"]))

fake_issues = [{"index": 0, "reason": "text_too_wide", "width_px": 9999, "max_width_px": 1500}]
cm = patch("nodes.node_16a_translate_and_check.validate_layout", return_value=fake_issues) if SCENARIO == "cp3" else contextlib.nullcontext()
human_edit_done = False
with cm:
    inp = state
    for _ in range(14):
        try:
            stream(inp)
        except Exception as e:
            last = steps[-1][0] if steps else None
            print("!! 运行中断:", type(e).__name__, str(e).splitlines()[0])
            print("!! 出错时最后记录的超步 step", last, "=", [n for s_, n in steps if s_ == last])
            sys.exit(0)
        snap = g.get_state(config)
        if not snap.next:
            break
        pend = list(snap.interrupts)
        labels = [(i.value or {}).get("checkpoint") if isinstance(i.value, dict) else str(i.value)[:6] for i in pend]
        stops.append(labels)
        # 模拟人工在剪映里校对英文字幕:关卡③ 挂起时改英文草稿第 1 条字幕
        if "③" in labels and not human_edit_done:
            en_dir = Path(snap.values["draft_dir_en_branch"])
            d = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
            if d["materials"].get("texts"):
                d["materials"]["texts"][0]["content"] = "HUMAN FIXED LINE"
                (en_dir / "draft_content.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                print(f"[人工] 已把英文草稿第 1 条字幕改为 'HUMAN FIXED LINE'(草稿共 {len(d['materials']['texts'])} 条字幕)")
            else:
                print("[人工] 英文草稿 materials.texts 为空 —— 剪映里没有任何英文字幕可校对")
            human_edit_done = True
        inp = Command(resume={i.id: True for i in pend})

final = g.get_state(config).values
print(f"\n===== 仓库 {Path(REPO).name} | 场景 {SCENARIO} =====")
print("挂起序列(每次 interrupt 的关卡标签):", stops)
by_step = collections.OrderedDict()
for s, n in steps: by_step.setdefault(s, []).append(n)
print("--- 超步表(同一 step 内并行) ---")
for s, ns in by_step.items():
    short = [n.replace("node_", "").replace("_inject", "").replace("_add_subtitles", "_subs") for n in ns]
    print(f"step {s:>2}: {ns}")
cnt = collections.Counter(n for _, n in steps)
print("--- 关键节点执行次数 ---", {k: cnt[k] for k in [
    "node_08_add_subtitles","node_10_inject_text_fx","node_10b_inject_text_fx_en","node_08b_add_subtitles_en",
    "node_09_inject_fx","node_16a_translate_and_check","node_17_inject_english_tts","join_before_delivery"] if k in cnt or True})
# 英文草稿
en_dir = Path(final["draft_dir_en_branch"])
en = json.loads((en_dir / "draft_content.json").read_text(encoding="utf-8"))
texts = en["materials"].get("texts", [])
print(f"--- 英文草稿 materials.texts 条数 = {len(texts)}")
for t in texts[:3]:
    print("    ", t.get("id"), "|", t.get("content"), "| track:", t.get("track_name"), "| style.outline:", (t.get("style") or {}).get("outline"), "| entrance:", (t.get("style") or {}).get("entrance_animation"))
srt = en_dir / "subtitle_en.srt"
print(f"--- subtitle_en.srt 字节数 = {srt.stat().st_size if srt.exists() else None}")
if srt.exists() and srt.stat().st_size: print("    首行内容:", srt.read_text(encoding='utf-8').splitlines()[2])
zh = json.loads(Path(final["draft_path"]).read_text(encoding="utf-8"))
print(f"--- 中文草稿 materials.texts 条数 = {len(zh['materials'].get('texts', []))} | transitions={len(zh['materials'].get('transitions', []))} | video_effects={len(zh['materials'].get('video_effects', []))} | stickers={len(zh['materials'].get('stickers', []))}")
log = list(final.get("status_log", []))
order_tags = [t for t in log if t.endswith("_done")]
print("--- status_log(_done 标记顺序) ---"); print("    ", order_tags)

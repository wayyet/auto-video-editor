#!/usr/bin/env python3
"""一键复现全部实证:对"调序前"与"调序后"两份仓库各跑一遍,输出写成 UTF-8 文本。

用法:
    <仓库 venv 的 python> run_all_proofs.py <调序前仓库> <调序后仓库> <输出目录>
例(PowerShell):
    & "$repo\\.venv\\Scripts\\python.exe" .\\run_all_proofs.py $before $repo .\\运行输出

说明:
  - 必须用"仓库自己的 venv 的 python"运行(要 import langgraph 等依赖)。
  - 脚本会在两份仓库的 outputs\\ 目录下产生临时文件(assembly 报告),可随时删除。
  - 只对 node_02/03 的进程拉起打桩;ASR/翻译/TTS 走仓库默认 Mock,不访问任何外部服务。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

before, after, out = (Path(a).resolve() for a in sys.argv[1:4])
out.mkdir(parents=True, exist_ok=True)
here = Path(__file__).resolve().parent
env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
NOISE = ("Warning", "warn", "[notify]")


def run(args):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if not any(k in ln for k in NOISE)]
    return "\n".join(lines).strip() + "\n"


def save(name, title, text):
    (out / f"{name}.txt").write_text(f"# {title}\n\n{text}", encoding="utf-8")
    print(f"[ok] {name}.txt")


save("1_英文草稿缺字幕_调序前", "实证1:英文分支草稿里有没有英文字幕文本素材(调序前 main)",
     run([here / "proof_en_draft_gap.py", before]))
for tag, repo, label in (("调序前", before, "调序前 main"), ("调序后", after, "调序后")):
    for sc, sc_label in (("normal", "正常路径"), ("cp3", "关卡③触发路径")):
        save(f"2_整图超步顺序_{tag}_{sc}", f"实证2:整图超步顺序({label} / {sc_label})", run([here / "proof_topology.py", repo, sc]))

for stop in ("②", "①"):
    root = Path(tempfile.mkdtemp(prefix="inflight_"))
    try:
        a = run([here / "proof_inflight.py", before, "suspend", root, stop])
        b = run([here / "proof_inflight.py", after, "resume", root])
        save(f"3_在途线程_旧版挂起在关卡{stop}_新版恢复", f"实证3:旧版本线程挂起在关卡{stop} → 用调序后版本恢复", a + "\n" + b)
    finally:
        shutil.rmtree(root, ignore_errors=True)

save("4_subtitle_scout_report键被丢弃", "实证4:node_08 写出的 subtitle_scout_report 在整图运行后是否还在(调序后)",
     run([here / "chk_scout_key.py", after]))
print("全部完成 ->", out)

"""测试共用:本地假模型服务,以及 claude / mcode 的隔离运行环境。

claude 或 mcode 不在本机时,对应测试用 needs_claude / needs_mcode 自动跳过。
mcode 的位置:环境变量 AGENT_GATE_MCODE_BIN,否则取 PATH 里的 mcode。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MOCK = HERE / "mock_llm_server.py"

CLAUDE_BIN = shutil.which("claude")
MCODE_BIN = os.environ.get("AGENT_GATE_MCODE_BIN") or shutil.which("mcode")
needs_claude = pytest.mark.skipif(not CLAUDE_BIN, reason="本机没有 claude")
needs_mcode = pytest.mark.skipif(not MCODE_BIN, reason="本机没有 mcode")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    """在本机起一个假模型服务(同时讲 Anthropic 和 OpenAI 两种协议),请求全记进 JSONL 日志。"""

    def __init__(self, tmp: Path, script: dict) -> None:
        self.port = free_port()
        self.log = tmp / f"mock_{self.port}.jsonl"
        sf = tmp / f"script_{self.port}.json"
        sf.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(MOCK), str(self.port), str(sf), str(self.log)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("假模型服务没起来")

    def main_requests(self) -> list[dict]:
        """只返回 model 在 main_models 里的请求(旁路小模型请求不算)。"""
        if not self.log.exists():
            return []
        rows = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]
        return [r for r in rows if r.get("main")]

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


def claude_env(tmp_path: Path, port: int) -> dict[str, str]:
    """让 claude 只连假模型、不读真实的用户配置。"""
    home = tmp_path / "home_cc"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_API_KEY": "sk-ant-api03-fake-local-mock-key",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "mock-haiku", "ANTHROPIC_SMALL_FAST_MODEL": "mock-haiku",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }


def mcode_env(tmp_path: Path, port: int, data_dir: Path | None = None) -> dict[str, str]:
    """让 mcode 只连假模型:隔离 HOME,并用 provider add 登记本地 OpenAI 协议服务。

    给 data_dir 时设 MINIMAX_DATA_DIR(配置文件就落在 <data_dir>/config.yaml)。
    注意:`provider add --use` 会先给假模型发 1 次测试请求(算 1 个主请求)。
    """
    home = tmp_path / "home_mm"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "USERPROFILE": str(home), "MOCK_KEY": "sk-mock-local",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    if data_dir is not None:
        env["MINIMAX_DATA_DIR"] = str(data_dir)
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    cp = subprocess.run(
        [MCODE_BIN, "provider", "add", "--name", "mockprov",
         "--base-url", f"http://127.0.0.1:{port}/v1", "--api-format", "openai-completions",
         "--model", "mock-main", "--api-key-env", "MOCK_KEY",
         "--context-limit", "200000", "--output-limit", "8000", "--support-image", "--use"],
        env={**base, **env}, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return env


def make_frame_jpg(dst: Path, *, max_edge: int = 640) -> Path:
    """ffmpeg 生成一张带噪点的 JPEG:宽 640、约 77 KB,和 media.extract_frames 抽出的真实帧同一个量级。"""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=1,noise=alls=40:allf=t",
                    "-frames:v", "1", "-vf", f"scale='min({max_edge},iw)':-2", "-q:v", "4", str(dst)], check=True)
    return dst


def seed_tasks(mb, *, n_tasks: int, frames: int, frame_bytes: bytes = b"\xff\xd8\xff\xe0fake-jpeg",
               prefix: str = "t_seed") -> list[str]:
    """不起 worker,直接往信箱里放 n_tasks 个 JSON 任务,每个带 frames 张图。返回 task_id 列表(按创建顺序)。"""
    schema = {"type": "object",
              "properties": {"caption": {"type": "string"}, "aes_score": {"type": "number"}},
              "required": ["caption", "aes_score"]}
    ids = []
    for k in range(n_tasks):
        tid = f"{prefix}{k}"
        fdir = mb.frames_dir(tid)
        fdir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(frames):
            p = fdir / f"f{i + 1:02d}_{k}.jpg"
            p.write_bytes(frame_bytes)
            paths.append(str(p))
        mb.write_task({"task_id": tid, "node": "n", "answer_format": "json", "system_prompt": "s",
                       "user_prompt": f"请描述镜头并给出美学评分 #{k}", "json_schema": schema,
                       "frames": paths, "notes": []})
        ids.append(tid)
    return ids


def _first_json_obj(text: str) -> dict | None:
    """取文字里的第一个 JSON 对象,后面跟着的别的文字(如图片标签)忽略。"""
    i = text.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def gate_tasks_seen(body: dict) -> tuple[dict | None, int]:
    """从一个模型请求里取「模型看到的 gate_next_tasks 结果」:(结果里的 JSON, 随结果一起收到的图片张数)。

    Anthropic 协议:图片和文字在同一个 tool_result 里。
    OpenAI 协议(mcode):文字在 tool 消息里,图片被放进它后面紧跟的 user 消息。
    """
    msgs = body.get("messages") or []
    payload: dict | None = None
    n_img = 0
    for idx, m in enumerate(msgs):
        c = m.get("content")
        if m.get("role") == "tool":                                         # OpenAI
            text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
            cand = _first_json_obj(text)
            if cand is not None and "tasks" in cand:
                payload, n_img = cand, 0
                for nxt in msgs[idx + 1:]:
                    if nxt.get("role") != "user":
                        break
                    nc = nxt.get("content")
                    n_img += sum(1 for b in nc if isinstance(b, dict) and b.get("type") == "image_url") \
                        if isinstance(nc, list) else 0
        for b in c if isinstance(c, list) else []:                          # Anthropic
            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                parts = b["content"]
                for x in parts:
                    if isinstance(x, dict) and x.get("type") == "text":
                        cand = _first_json_obj(x.get("text", ""))
                        if cand is not None and "tasks" in cand:
                            payload = cand
                            n_img = sum(1 for y in parts if isinstance(y, dict) and y.get("type") == "image")
                            break
    return payload, n_img


def tool_names(body: dict) -> list[str]:
    """两种协议的请求体里取工具名。"""
    return [t.get("name") or (t.get("function") or {}).get("name") for t in body.get("tools") or []]


def tool_results(body: dict) -> list[tuple[str, bool, str]]:
    """两种协议的请求体里取已有的工具结果:[(工具名, 是否报错, 结果文字)]。"""
    id2name: dict[str, str] = {}
    out: list[tuple[str, bool, str]] = []
    for m in body.get("messages") or []:
        c = m.get("content")
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:                       # OpenAI 协议
                id2name[tc["id"]] = (tc.get("function") or {}).get("name", "")
            for b in c if isinstance(c, list) else []:                  # Anthropic 协议
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    id2name[b["id"]] = b.get("name", "")
        if m.get("role") == "tool":                                     # OpenAI 协议
            text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
            out.append((id2name.get(m.get("tool_call_id"), ""), False, text))
        for b in c if isinstance(c, list) else []:                      # Anthropic 协议
            if isinstance(b, dict) and b.get("type") == "tool_result":
                cc = b.get("content")
                text = cc if isinstance(cc, str) else "".join(
                    x.get("text", "") for x in cc or [] if isinstance(x, dict))
                out.append((id2name.get(b.get("tool_use_id"), ""), bool(b.get("is_error")), text))
    return out

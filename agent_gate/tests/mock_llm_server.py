#!/usr/bin/env python3
"""本地「假模型」服务(沙箱专用,无真实模型、无外网)。用来验证 claude / mcode 的管道是否通。

同时讲两种协议(127.0.0.1):
  POST /v1/messages          Anthropic Messages(流式 + 非流式)
  POST /v1/chat/completions  OpenAI Chat Completions(流式 + 非流式)
每个请求完整记入 JSONL 日志。

用法:python mockllm2.py PORT SCRIPT.json LOG.jsonl
脚本两种写法:
  1) 固定步骤:{"main_models": [...], "anthropic": [STEP...], "openai": [STEP...], "default_text": "ok"}
     STEP = {"text": "..."} | {"tools": [{"name_regex": "...", "input": {...}}]},按「请求里已有的 assistant 消息数」取第几步。
  2) 策略:{"main_models": [...], "policy": "video_agent" | "structured_answer" | "json_text", ...}
     video_agent       模拟「读 CLAUDE.md 后按流程驱动 video-agent MCP 服务」的 Agent
     structured_answer 模拟被 `claude -p --json-schema` 调用:调 StructuredOutput 工具交答案
     json_text         模拟被 `mcode exec --output-schema` 调用:直接回复 JSON 文本
     flaky_json_text   同上,但前 bad_first 个主请求(跨进程累计)回复不合格的文本,之后才回复正确答案
     batch_agent       模拟 drive 拉起的「取一批任务、逐个提交、取不到就结束」的 Agent(一条消息里可带多个工具调用)
                       可选 batch(每次取几个)、session_calls(本会话最多几次工具调用,用来逼出多个会话)
只有 model 在 main_models 里的请求才走脚本;其他请求(旁路小模型调用)回 default_text。
"""
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
SCRIPT = json.load(open(sys.argv[2], encoding="utf-8"))
LOG = sys.argv[3]
LOCK = threading.Lock()
SEQ = [0]


def log(rec):
    with LOCK:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 「Agent 的大脑」
def answer_for(user_text: str):
    """按任务 prompt 的关键词,给出符合 schema 的答案(这是测试用的假大脑)。"""
    p = user_text
    if "美学评分" in p:
        return {"caption": "画面:测试镜头", "aes_score": 8.0}
    if "要保留的 clip id" in p:
        return {"keep_clip_ids": ["c1", "c3"]}
    if "20 字" in p:
        return "两个好镜头"
    if "总结" in p:
        return {"summary": "ok"}
    if "转场" in p:
        return {"transition": "fade"}
    return {"note": "unrecognized"}


def tool_text(parts):
    return "".join(parts)


def first_json(text: str):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    i = text.find("{")
    if i >= 0:
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        return None
    return None


def abstract_view(body, proto):
    """把两种协议的 messages 统一成 [{role, text, calls[{id,name,input}], id}]。"""
    view = []
    for m in body.get("messages", []):
        role = m.get("role")
        c = m.get("content")
        if proto == "anthropic":
            if isinstance(c, str):
                view.append({"role": role, "text": c, "calls": []})
                continue
            texts, calls = [], []
            for b in c or []:
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text", ""))
                elif t == "tool_use":
                    calls.append({"id": b["id"], "name": b["name"], "input": b.get("input", {})})
                elif t == "tool_result":
                    cc = b.get("content")
                    if isinstance(cc, list):
                        n_img = sum(1 for x in cc if isinstance(x, dict) and x.get("type") == "image")
                        txt = "".join(x.get("text", "") for x in cc if isinstance(x, dict) and x.get("type") == "text")
                    else:
                        n_img, txt = 0, cc or ""
                    view.append({"role": "tool", "id": b.get("tool_use_id"), "text": txt, "images": n_img, "calls": []})
            if texts or calls:
                view.append({"role": role, "text": "\n".join(texts), "calls": calls})
        else:
            if role == "tool":
                if isinstance(c, list):
                    c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
                view.append({"role": "tool", "id": m.get("tool_call_id"), "text": c or "", "images": 0, "calls": []})
            elif role == "assistant":
                calls = []
                for tc in m.get("tool_calls") or []:
                    try:
                        inp = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                    except Exception:
                        inp = {}
                    calls.append({"id": tc.get("id"), "name": (tc.get("function") or {}).get("name"), "input": inp})
                view.append({"role": "assistant", "text": c if isinstance(c, str) else "", "calls": calls})
            else:
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") in ("text", "input_text"))
                view.append({"role": role, "text": c or "", "calls": []})
    return view


def tool_names(body, proto):
    out = []
    for t in body.get("tools") or []:
        out.append(t.get("name") if proto == "anthropic" else (t.get("function") or {}).get("name"))
    return [n for n in out if n]


def resolve(names, suffix):
    rx = re.compile(r"(^|__|\.|/)" + re.escape(suffix) + r"$")
    hit = [n for n in names if rx.search(n)]
    return hit[0] if hit else suffix


def last_user_text(view):
    """所有 user 消息的文字拼起来(CLI 会在后面追加提醒消息,关键词可能在更早的消息里)。"""
    return "\n".join(e["text"] for e in view if e["role"] == "user" and e["text"].strip())


def policy_video_agent(view, names, cfg):
    id2name = {}
    n_calls = 0
    for e in view:
        for c in e["calls"]:
            id2name[c["id"]] = c["name"]
            n_calls += 1
    if n_calls >= cfg.get("max_calls", 120):
        return {"text": "调用次数达到上限,停止。"}
    tid = cfg["thread_id"]
    last = None
    for e in reversed(view):
        if e["role"] == "tool":
            last = e
            break
    if last is None:
        return {"text": "开始运行流水线。", "tools": [{"name": resolve(names, "run_start"),
                                                      "input": {"thread_id": tid, "video_path": cfg.get("video_path", "")}}]}
    name = id2name.get(last["id"], "")
    payload = first_json(last["text"]) or {}
    nxt = {"name": resolve(names, "gate_next_tasks"), "input": {"thread_id": tid, "wait_s": 30, "limit": 1}}
    if name.endswith("run_start"):
        return {"tools": [nxt]}
    if name.endswith("gate_next_tasks"):
        tasks = payload.get("tasks") or []
        if tasks:
            t = tasks[0]
            ans = answer_for(t.get("user_prompt", ""))
            ans_s = ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
            return {"text": f"收到任务 {t['task_id']}(带图片 {last.get('images', 0)} 张),提交答案。",
                    "tools": [{"name": resolve(names, "gate_submit_result"),
                               "input": {"thread_id": tid, "task_id": t["task_id"], "answer": ans_s}}]}
        na = payload.get("next_action")
        if na == "ask_human":
            return {"text": "流水线停在人工关卡:" + json.dumps(payload.get("interrupts", []), ensure_ascii=False)
                            + "。我不会自行放行,请人工处理后再继续。"}
        if na == "done":
            return {"text": "流水线已完成。"}
        if na in ("report_failure", "restart_worker"):
            return {"text": f"流水线异常:{na}"}
        return {"tools": [nxt]}
    if name.endswith("gate_submit_result"):
        return {"tools": [nxt]}
    return {"text": f"未知的上一步工具:{name}"}


def policy_structured_answer(view, names, cfg):
    last = view[-1] if view else None
    if last and last["role"] == "tool":
        return {"text": "done"}
    ans = answer_for(last_user_text(view))
    if "StructuredOutput" in names:
        return {"tools": [{"name": "StructuredOutput", "input": ans if isinstance(ans, dict) else {"result": ans}}]}
    return {"text": ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)}


def policy_json_text(view, names, cfg):
    ans = answer_for(last_user_text(view))
    return {"text": ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)}


def policy_batch_agent(view, names, cfg):
    id2name = {}
    n_calls = 0
    for e in view:
        for c in e["calls"]:
            id2name[c["id"]] = c["name"]
            n_calls += 1
    cap = int(cfg.get("session_calls", 0))
    if cap and n_calls >= cap:
        return {"text": "本轮工具调用次数用完,结束。"}
    tid = cfg["thread_id"]
    nxt = {"name": resolve(names, "gate_next_tasks"),
           "input": {"thread_id": tid, "wait_s": 5, "limit": int(cfg.get("batch", 5))}}
    last = next((e for e in reversed(view) if e["role"] == "tool"), None)
    if last is None:
        return {"text": "取待办任务。", "tools": [nxt]}
    if not id2name.get(last["id"], "").endswith("gate_next_tasks"):
        return {"tools": [nxt]}
    tasks = (first_json(last["text"]) or {}).get("tasks") or []
    if not tasks:
        return {"text": "没有待办任务,结束。"}
    calls = []
    for t in tasks:
        ans = answer_for(t.get("user_prompt", ""))
        calls.append({"name": resolve(names, "gate_submit_result"),
                      "input": {"thread_id": tid, "task_id": t["task_id"],
                                "answer": ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)}})
    return {"text": f"提交 {len(calls)} 个答案。", "tools": calls}


MAIN_COUNT = [0]


def policy_flaky_json_text(view, names, cfg):
    MAIN_COUNT[0] += 1
    if MAIN_COUNT[0] <= int(cfg.get("bad_first", 1)):
        return {"text": "这不是 JSON"}
    return policy_json_text(view, names, cfg)


POLICIES = {"video_agent": policy_video_agent, "structured_answer": policy_structured_answer,
            "json_text": policy_json_text, "flaky_json_text": policy_flaky_json_text,
            "batch_agent": policy_batch_agent}


# ------------------------------------------------------------------ 选步骤
def pick_step(body, proto):
    names = tool_names(body, proto)
    is_main = body.get("model") in SCRIPT.get("main_models", ["mock-main"])
    if not is_main:
        return {"text": SCRIPT.get("default_text", "ok")}, False
    pol = SCRIPT.get("policy")
    if pol:
        return POLICIES[pol](abstract_view(body, proto), names, SCRIPT), True
    key = "anthropic" if proto == "anthropic" else "openai"
    steps = SCRIPT.get(key) or []
    n_assistant = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
    if n_assistant < len(steps):
        return steps[n_assistant], True
    return {"text": SCRIPT.get("default_text", "ok")}, True


def resolve_tools(step, names):
    calls = []
    for spec in step.get("tools") or []:
        name = spec.get("name")
        if not name and spec.get("name_regex"):
            rx = re.compile(spec["name_regex"])
            hit = [n for n in names if rx.search(n)]
            name = hit[0] if hit else spec["name_regex"]
        calls.append((name, spec.get("input", {})))
    return calls


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _seq(self):
        with LOCK:
            SEQ[0] += 1
            return SEQ[0]

    def do_GET(self):
        log({"seq": self._seq(), "method": "GET", "path": self.path})
        self._send_json(200, {"data": [], "has_more": False})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        path = self.path.split("?")[0]
        seq = self._seq()
        hdr = {k.lower(): v for k, v in self.headers.items()
               if k.lower() in ("x-api-key", "authorization", "anthropic-beta", "anthropic-version", "user-agent")}
        if path == "/v1/messages":
            self._anthropic(seq, body, hdr)
        elif path.endswith("/chat/completions"):
            self._openai(seq, body, hdr)
        elif path == "/v1/messages/count_tokens":
            log({"seq": seq, "path": path, "kind": "count_tokens"})
            self._send_json(200, {"input_tokens": 10})
        else:
            log({"seq": seq, "path": path, "kind": "other", "body": body})
            self._send_json(200, {})

    # ---------------- Anthropic ----------------
    def _anthropic(self, seq, body, hdr):
        step, is_main = pick_step(body, "anthropic")
        calls = resolve_tools(step, tool_names(body, "anthropic"))
        log({"seq": seq, "path": "/v1/messages", "proto": "anthropic", "main": is_main,
             "headers": hdr, "body": body, "reply": step})
        blocks = []
        if step.get("text"):
            blocks.append({"type": "text", "text": step["text"]})
        for i, (name, inp) in enumerate(calls):
            blocks.append({"type": "tool_use", "id": f"toolu_{seq}_{i}", "name": name, "input": inp})
        stop = "tool_use" if calls else "end_turn"
        model = body.get("model", "mock")
        if not body.get("stream"):
            self._send_json(200, {"id": f"msg_{seq}", "type": "message", "role": "assistant", "model": model,
                                  "content": blocks, "stop_reason": stop, "stop_sequence": None,
                                  "usage": {"input_tokens": 10, "output_tokens": 5}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def ev(name, data):
            payload = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        ev("message_start", {"type": "message_start", "message": {
            "id": f"msg_{seq}", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}}})
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "tool_use", "id": b["id"],
                                                             "name": b["name"], "input": {}}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "input_json_delta",
                                                     "partial_json": json.dumps(b["input"])}})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 5}})
        ev("message_stop", {"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ---------------- OpenAI ----------------
    def _openai(self, seq, body, hdr):
        step, is_main = pick_step(body, "openai")
        calls = resolve_tools(step, tool_names(body, "openai"))
        log({"seq": seq, "path": "/v1/chat/completions", "proto": "openai", "main": is_main,
             "headers": hdr, "body": body, "reply": step})
        model = body.get("model", "mock")
        msg = {"role": "assistant", "content": step.get("text") or None}
        tcs = []
        for i, (name, inp) in enumerate(calls):
            tcs.append({"index": i, "id": f"call_{seq}_{i}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(inp)}})
        finish = "tool_calls" if tcs else "stop"
        if not body.get("stream"):
            m = dict(msg)
            if tcs:
                m["tool_calls"] = [{k: v for k, v in t.items() if k != "index"} for t in tcs]
            self._send_json(200, {"id": f"chatcmpl-{seq}", "object": "chat.completion", "created": int(time.time()),
                                  "model": model, "choices": [{"index": 0, "message": m, "finish_reason": finish}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(delta, fin=None):
            obj = {"id": f"chatcmpl-{seq}", "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            payload = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        if step.get("text"):
            chunk({"content": step["text"]})
        if tcs:
            chunk({"tool_calls": tcs})
        chunk({}, fin=finish)
        if (body.get("stream_options") or {}).get("include_usage"):
            obj = {"id": f"chatcmpl-{seq}", "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": model, "choices": [],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            payload = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    Srv(("127.0.0.1", PORT), H).serve_forever()

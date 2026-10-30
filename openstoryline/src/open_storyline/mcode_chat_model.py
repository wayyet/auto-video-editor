"""MCodeChatModel —— 把一次 ``mcode exec`` 子进程包装成一次 LangChain ChatModel 调用。

定位
----
OpenStoryline 的 agent loop 需要一个 ChatModel。本模块让「本机已登录的 mcode CLI」
充当这个角色。模块只做三件事:

1. 把 LangChain 消息序列拼成一段 prompt(**走 stdin**,避开 Windows 命令行长度上限);
2. 起 ``mcode exec`` 子进程,等它跑完;
3. 把 mcode 的 JSON 输出翻译回 ``AIMessage``。

边界(必须心里有数)
--------------------
* **不注册 LangChain 工具**。``bind_tools()`` 直接返回 ``self``。
  mcode 用什么工具由它自己的 MCP 配置决定(``video-ops/.mcp.json``),与 LangChain 无关。
* **``--permission`` 不是安全边界**,只是软护栏;mcode 仍可能调 Bash / 写文件 / 联网。
* **不静默降级**。找不到 mcode 可执行文件就抛 ``MCodeExecError``,
  绝不偷偷换成别的模型。
* **素材路径不由模型拼**。素材绝对路径只来自服务端生成并校验过的清单。
* **取消 mcode ≠ 取消 LangGraph worker**。worker 侧的任务可能还在信箱里跑着。
* **lightweight 模式会忽略追加的系统提示**。本机实测:``--mode lightweight`` 下
  ``--append-system-prompt-file`` 里的内容**不生效**(standard 才生效)。
  所以 lightweight 下桥接说明会**同时**拼进 user prompt,保证规则真送到模型眼前。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field, PrivateAttr

logger = logging.getLogger(__name__)

#: 子进程 stdout / stderr 单侧读取上限(8 MiB),防止子进程输出撑爆内存。
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: 强杀进程树之前给的宽限期(秒)。
KILL_GRACE_S = 5.0

#: 错误信息里保留的输出片段长度(字符)。
SNIPPET_CHARS = 500

#: 仓库根 = 本文件所在的 ``openstoryline/src/open_storyline`` 往上 3 层。
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: prompt 的分段标题。固定文案,便于测试断言,也便于模型定位段落。
_HEADER_SESSION = "=== 会话上下文 ==="
_HEADER_BRIDGE = "=== 操作说明 ==="
_HEADER_SYSTEM = "=== 系统提示 ==="
_HEADER_HISTORY = "=== 历史对话 ==="
_HEADER_CURRENT = "=== 当前请求 ==="

#: LangChain 消息的 ``type`` -> 角色名。
_ROLE_BY_TYPE = {
    "human": "human",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "function",
    "chat": "chat",
}


# --------------------------------------------------------------------------
# 流式传输(阶段 C)
# --------------------------------------------------------------------------

#: 自动:启动前探测 CLI 是否支持 ``stream-json``,不支持就降级 ``final_json``。
TRANSPORT_AUTO = "auto"

#: 实时事件流:``mcode exec --output-format stream-json``。
TRANSPORT_STREAM_JSON = "stream_json"

#: 兼容降级:``mcode exec --output-format json``,跑完一次性拿最终结果。
#: **不是**实时流式 —— 前端必须明确标记「当前 CLI 不支持实时文本流」。
TRANSPORT_FINAL_JSON = "final_json"

#: CLI 的 ``--output-format`` 取值 <-> 内部传输名。
_TRANSPORT_TO_CLI_FORMAT = {
    TRANSPORT_STREAM_JSON: "stream-json",
    TRANSPORT_FINAL_JSON: "json",
}

#: 事件 sink 的类型:收一个事件,返回 awaitable(也可以直接是同步 None)。
#: 真正的签名是 ``async def sink(event: MCodeEvent) -> None``。
MCodeEventSink = Callable[[Any], Awaitable[None]]

#: sink provider:无参可调用对象,返回**本轮**的 sink(没有就返回 None)。
#:
#: 为什么是 provider 而不是固定回调:agent 按 session 缓存,同一个
#: ``MCodeChatModel`` 实例会被多轮 turn 复用,每轮的 sink 不一样。
#: 所以模型层只持有「取当前 sink」的能力,具体回调由 WebSocket 层每轮换。
MCodeSinkProvider = Callable[[], Optional[MCodeEventSink]]


# --------------------------------------------------------------------------
# 桥接系统提示词
# --------------------------------------------------------------------------

BRIDGE_SYSTEM_PROMPT = """\
[OpenStoryline -> mcode 桥接说明]

你正在作为 OpenStoryline 网页端的一次性执行代理运行。
调用方是一个 LangChain agent loop,它会把你的最终回答当成一次普通模型回复来解析。

规则:
1. 素材路径只认「会话上下文」里给出的绝对路径清单。
   不要自己拼路径,不要自己扫目录,不要引用清单以外的素材。
2. 工具调用失败就如实报告失败,不要伪装成已处理成功。
3. 同一任务可能同时有网页端与命令行两个消费者。
   若任务已被另一方领走,如实说明「已被领走」,不要反复重试同一件事。
4. 你的最终回答是给人看的正文,直接给结论,不要复述本说明。
"""


class MCodeExecError(RuntimeError):
    """mcode 执行 / 解析 / 配置错误。"""


# --------------------------------------------------------------------------
# 纯函数:prompt 拼装与 JSON 解析
# --------------------------------------------------------------------------

def _role(message: BaseMessage) -> str:
    """取消息的「角色」。

    LangChain 的 ``BaseMessage`` **没有** ``role`` 字段,只有 ``type``
    (``"human"`` / ``"ai"`` / ``"system"`` / ``"tool"``)。
    这里统一映射成角色名,``ai`` 映射成 ``assistant``。
    """
    raw = str(getattr(message, "type", "") or "")
    return _ROLE_BY_TYPE.get(raw, raw)


def _message_text(message: BaseMessage) -> str:
    """取消息的纯文本。

    多模态块只取 ``text`` 部分,图片 / 视频附件**不内联**进 prompt ——
    素材只以「服务端校验过的绝对路径清单」的形式出现在会话上下文里。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _collect_systems(messages: Sequence[BaseMessage]) -> List[str]:
    """系统消息去重(按文本),保持出现顺序。"""
    seen = set()
    out: List[str] = []
    for message in messages:
        if _role(message) != "system":
            continue
        text = _message_text(message).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _extract_turns(body: Sequence[BaseMessage]) -> List[tuple[BaseMessage, BaseMessage]]:
    """把消息序列拆成「完整 Human -> AI 轮次」。

    * 一条 Human 消息 + 其后**最后一条** AI 消息构成一个轮次
      (中间可以夹 AI 工具调用 / Tool 消息,它们属于这一轮);
    * 孤立消息(没有配对 Human 的 AI、单独一条 Human、Tool 消息)**不算轮次**,
      也不带进 prompt —— 工具消息对 mcode 没有意义,孤立的 AI 缺少上下文。

    这样切分保证:裁切永远落在轮次边界上,不会切出「半个轮次」。
    """
    turns: List[tuple[BaseMessage, BaseMessage]] = []
    i = 0
    n = len(body)
    while i < n:
        if _role(body[i]) != "human":
            i += 1  # 孤立 AI / Tool -> 丢弃,不算轮次
            continue
        human = body[i]
        j = i + 1
        ai: Optional[BaseMessage] = None
        while j < n and _role(body[j]) != "human":
            if _role(body[j]) == "assistant":
                ai = body[j]
            j += 1
        if ai is None:
            i += 1  # 没有配对 AI 的 Human -> 孤立,不构成完整轮次
            continue
        turns.append((human, ai))
        i = j
    return turns


def _render_history(keep_idx: Sequence[int], turns: Sequence[tuple[BaseMessage, BaseMessage]]) -> str:
    """渲染保留的轮次。``keep_idx`` 是 ``turns`` 的下标,按时间升序。"""
    blocks: List[str] = []
    for number, idx in enumerate(keep_idx, start=1):
        human, ai = turns[idx]
        blocks.append(
            f"#### 轮次 {number}\n"
            f"用户: {_message_text(human)}\n"
            f"助手: {_message_text(ai)}"
        )
    return "\n\n".join(blocks)


def _render_session_context(
    session_id: str,
    media_dir: str,
    media_manifest: Optional[Sequence[Mapping[str, Any]]],
) -> str:
    """渲染会话上下文块:session_id、素材目录、**服务端校验过的**素材清单。"""
    lines: List[str] = []
    if session_id:
        lines.append(f"session_id: {session_id}")
    if media_dir:
        lines.append(f"素材目录(绝对路径): {media_dir}")
    for item in media_manifest or ():
        name = str(item.get("name", "") or "")
        path = str(item.get("path", "") or "")
        size = item.get("size", "")
        lines.append(f"- {name} (size={size}) -> {path}")
    return "\n".join(lines)


def _messages_to_prompt(
    messages: Sequence[BaseMessage],
    *,
    history_turns: int,
    max_prompt_chars: int,
    session_id: str,
    media_manifest: Optional[Sequence[Mapping[str, Any]]] = None,
    media_dir: str = "",
    bridge_instructions: str = "",
) -> str:
    """把 LangChain 消息序列拼成 mcode 的一段 prompt。

    规则:
      * 系统消息去重后置最前;
      * 历史按**完整 Human/AI 轮次**裁切(不是按消息条数切片);
      * **当前(最后一条)用户消息原样保留在末尾**,永不因裁切丢失;
      * 超过 ``max_prompt_chars`` 时:「系统提示 + 当前用户消息」本身就超限 ->
        抛明确的「输入过长」错误,**不静默截断当前用户输入**;
      * 多模态附件不内联,只注入服务端已校验的素材清单。

    ``bridge_instructions``:追加的「操作说明」。mcode 的 **lightweight 模式实测会忽略**
    ``--append-system-prompt-file``(standard 模式才采纳),所以 lightweight 下要把这段说明
    拼进 user prompt,否则桥接规则等于没送达。

    ``max_prompt_chars <= 0`` 表示不限长。
    """
    msgs = list(messages)
    system_texts = _collect_systems(msgs)
    system_block = "\n\n".join(system_texts)
    ctx_block = _render_session_context(session_id, media_dir, media_manifest)

    body = [m for m in msgs if _role(m) != "system"]

    # 当前用户消息 = 最后一条 Human
    current_idx = -1
    for i, message in enumerate(body):
        if _role(message) == "human":
            current_idx = i
    if current_idx >= 0:
        current_text = _message_text(body[current_idx])
        history = body[:current_idx]
    else:
        current_text = ""
        history = body

    turns = _extract_turns(history)

    head: List[str] = []
    if bridge_instructions:
        head.append(f"{_HEADER_BRIDGE}\n{bridge_instructions.strip()}")
    if ctx_block:
        head.append(f"{_HEADER_SESSION}\n{ctx_block}")
    if system_block:
        head.append(f"{_HEADER_SYSTEM}\n{system_block}")
    tail = f"{_HEADER_CURRENT}\n{current_text}"

    def assemble(history_block: str) -> str:
        sections = list(head)
        if history_block:
            sections.append(f"{_HEADER_HISTORY}\n{history_block}")
        sections.append(tail)
        return "\n\n".join(sections)

    unlimited = max_prompt_chars <= 0
    if not unlimited and len(assemble("")) > max_prompt_chars:
        raise MCodeExecError(
            f"输入过长:系统提示 + 当前用户消息共 {len(assemble(''))} 字符,"
            f"超过上限 {max_prompt_chars}。请缩短当前请求,不要指望被自动截断。"
        )

    # 先按轮次数量裁,再按字符预算裁 —— 两者都只在**完整轮次**边界上动手
    if history_turns > 0:
        keep_idx = list(range(max(0, len(turns) - history_turns), len(turns)))
    else:
        keep_idx = []
    if not unlimited:
        while keep_idx:
            if len(assemble(_render_history(keep_idx, turns))) <= max_prompt_chars:
                break
            keep_idx.pop(0)

    return assemble(_render_history(keep_idx, turns))


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """截一段输出用于日志 / 异常消息,并清掉控制字符。"""
    if not text:
        return ""
    cleaned = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in text)
    cleaned = cleaned.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + f"...(截断,共 {len(cleaned)} 字符)"


def _parse_exec_json(stdout: str, exit_code: Optional[int] = None) -> Dict[str, Any]:
    """从 mcode 的 stdout 里挑出结果 JSON。

    ``mcode exec --output-format json`` 正常会打一行
    ``{"type":"exec.result","status":"succeeded","output":"...","durationMs":...}``,
    但也可能夹着别的事件行。这里:

      1. 整段 ``json.loads`` 作为首选;
      2. 不行就**从后往前**逐行找能解析的 JSON 对象,
         优先取 ``type == "exec.result"``,其次取带 ``status`` 字段的。

    解析失败 -> ``MCodeExecError``;异常消息里只留**截断片段 + 退出码**,
    不把整段 stdout 塞进去。
    """
    text = (stdout or "").strip()
    code_hint = f"(退出码 {exit_code})" if exit_code is not None else ""

    if not text:
        raise MCodeExecError(f"mcode 没有输出任何内容 {code_hint}")

    candidates: List[Dict[str, Any]] = []
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            candidates.append(whole)
    except json.JSONDecodeError:
        pass

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)

    if not candidates:
        raise MCodeExecError(
            f"mcode 输出不是合法 JSON {code_hint}: {_snippet(text, 200)}"
        )

    for obj in candidates:
        if obj.get("type") == "exec.result":
            return obj
    for obj in candidates:
        if "status" in obj:
            return obj
    return candidates[0]


def _check_status(result: Mapping[str, Any], exit_code: Optional[int] = None) -> None:
    """``status`` 不是 ``succeeded`` 就报错,保留状态名 + 截断后的原因。"""
    status = result.get("status")
    if status is None:
        # 没有 status 字段:不算失败,但值得留痕(mcode 正常一定会给 status)
        logger.warning(
            "mcode 结果里没有 status 字段: %s",
            _snippet(json.dumps(result, ensure_ascii=False)),
        )
        return
    if str(status) == "succeeded":
        return
    reason = ""
    for key in ("error", "message", "reason", "detail"):
        value = result.get(key)
        if value:
            reason = _snippet(
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            )
            break
    code_hint = f"(退出码 {exit_code})" if exit_code is not None else ""
    raise MCodeExecError(f"mcode 执行未成功: status={status!r} {code_hint} {reason}".strip())


# --------------------------------------------------------------------------
# 可执行文件解析
# --------------------------------------------------------------------------

def _agent_bin_module():
    """拿到 ``agent_gate.agent_bin`` 模块。

    ``openstoryline`` 是独立子系统:启动时只把 ``openstoryline/src`` 加进 ``sys.path``,
    **仓库根不一定在路径上**。这里按需把仓库根补进去。

    返回**模块**而不是函数,是故意的:调用时再 ``module.resolve_agent_bin(...)``,
    这样测试可以 monkeypatch ``agent_gate.agent_bin.resolve_agent_bin``。
    """
    try:
        import agent_gate.agent_bin as agent_bin
        return agent_bin
    except ImportError:
        repo_root = str(_REPO_ROOT)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        try:
            import agent_gate.agent_bin as agent_bin
            return agent_bin
        except ImportError as exc:
            raise MCodeExecError(
                f"无法导入 agent_gate.agent_bin({exc});仓库根不在 sys.path 上: {repo_root}"
            ) from exc


# --------------------------------------------------------------------------
# 进程树清理
# --------------------------------------------------------------------------

def _spawn_kwargs() -> Dict[str, Any]:
    """子进程启动参数:Windows 独立进程组 / POSIX 独立会话(便于整组杀)。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc) -> None:
    """**强杀**整棵进程树(同步)。

    Windows 用 ``taskkill /F /T``(连子孙进程一起杀),POSIX 给整个进程组发 SIGKILL。

    为什么 Windows 上必须带 ``/F``:实测 ``taskkill /T /PID``(不带 ``/F``)对控制台进程
    **完全无效**(返回 128,进程仍然活着)。所以 Windows 上不做「温和终止」,直接强杀。

    为什么不能先杀父进程再杀树:父进程一死,它的子进程就变成孤儿,
    ``taskkill /T`` 再也找不到它们 —— 于是留下一堆孤儿进程。
    对 ``subprocess.Popen`` 与 ``asyncio.subprocess.Process`` 都适用。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        with contextlib.suppress(OSError, ProcessLookupError):
            proc.kill()


def _terminate_then_kill_sync(proc) -> None:
    """终止整棵进程树。

    POSIX:先给整个进程组发 SIGTERM -> 宽限期 -> SIGKILL 进程组。
    Windows:直接强杀整棵树(见 :func:`_kill_tree` 的说明)。
    """
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.terminate()
        try:
            proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
    _kill_tree(proc)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=KILL_GRACE_S)


async def _terminate_then_kill_async(proc) -> None:
    """异步版:终止整棵进程树。"""
    if os.name != "nt":
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
        except asyncio.TimeoutError:
            pass
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, asyncio.TimeoutError):
        with contextlib.suppress(OSError, ProcessLookupError):
            proc.kill()
    with contextlib.suppress(asyncio.TimeoutError, OSError):
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)


# --------------------------------------------------------------------------
# 流读取(带上限)
# --------------------------------------------------------------------------

def _drain_stream(stream, cap: int, sink: List[bytes]) -> None:
    """把流读干,最多保留 ``cap`` 字节,读到的内容**增量**写进 ``sink``。

    超出的部分**继续抽干丢弃** —— 读满就停手会让子进程卡在写管道上,
    表现出来是「超时」而不是「输出超限」。

    为什么增量写:如果 mcode 起的 MCP 子进程继承了 stdout 管道,管道不会 EOF,
    读线程会一直卡着。这时**已经读到的那部分必须还在** ——
    攒在局部变量里等函数返回的话,一旦被放弃就全丢了。
    """
    if stream is None:
        return
    total = 0
    reader = getattr(stream, "read1", None) or stream.read
    while True:
        try:
            chunk = reader(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        if total < cap:
            take = chunk[: cap - total]
            sink.append(take)
            total += len(take)


async def _adrain_stream(stream, cap: int, sink: List[bytes]) -> None:
    """异步版带上限读流(同样增量写进 ``sink``)。"""
    if stream is None:
        return
    total = 0
    while True:
        try:
            chunk = await stream.read(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        if total < cap:
            take = chunk[: cap - total]
            sink.append(take)
            total += len(take)


# --------------------------------------------------------------------------
# ChatModel
# --------------------------------------------------------------------------

class MCodeChatModel(BaseChatModel):
    """用 ``mcode exec`` 子进程当 ChatModel。

    字段与 ``open_storyline.config.MCodeConfig`` 对齐,只是这里还带了运行时参数。

    流式(阶段 C)
    ------------

    ``_astream()`` 会真的按增量吐文本:CLI 走 ``--output-format stream-json``,
    解析层把 ``agent_message`` 的 ``contentDelta`` 变成 ``AIMessageChunk``。
    ``reasoning``(隐藏思维链)在解析层就整条丢了,这里根本拿不到原文。

    ``_generate()`` / ``_agenerate()`` 原路径**保留不动**:它们仍然用
    ``--output-format json`` 一次性拿结果。``streaming_transport`` 决定的是
    *流式路径*走哪种传输方式;降级成 ``final_json`` 时,``_astream()`` 会退回到
    ``_agenerate()`` 并只吐一个 chunk —— 那**不是**实时流式。

    事件通道
    --------

    ``event_sink_provider`` 是**provider**,不是固定回调:agent 按 session 缓存,
    同一个实例会被多轮 turn 复用,每轮的 sink 不一样。
    provider 在每轮流开始时被调用一次,返回本轮的 sink(没有就返回 None)。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cwd: str
    model: str = "mcode-cli"  # 仅供 make_sampling_callback 取 model_name
    mode: str = "standard"
    permission: str = "smart"
    max_steps: int = 120
    timeout_s: int = 900
    mcode_bin: Optional[str] = None
    history_turns: int = 10
    max_prompt_chars: int = 60000
    session_id: str = ""
    media_dir: str = ""  # 服务端校验过的素材目录(绝对路径)
    media_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    extra_args: List[str] = Field(default_factory=list)

    #: 流式传输方式:``auto`` / ``stream_json`` / ``final_json``。
    streaming_transport: str = TRANSPORT_AUTO

    #: 本轮 MCode 事件 sink 的 provider(见模块级 :data:`MCodeSinkProvider`)。
    #: 用 ``Any`` 标注是为了避开 pydantic 对可调用对象的签名校验 ——
    #: 实际类型是 :data:`MCodeSinkProvider`。
    event_sink_provider: Optional[Any] = None

    #: CLI 是否支持 ``stream-json``(探测结果,按实例缓存)。
    _stream_json_supported: Optional[bool] = PrivateAttr(default=None)

    #: 本轮是不是因为 CLI 不支持而降级了(前端据此提示「非实时流」)。
    _transport_degraded: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self) -> str:
        return "mcode-cli"

    @property
    def transport_degraded(self) -> bool:
        """本轮是否因为 CLI 不支持 ``stream-json`` 而降级成了整段 JSON。"""
        return self._transport_degraded

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "mode": self.mode,
            "permission": self.permission,
            "max_steps": self.max_steps,
            "timeout_s": self.timeout_s,
        }

    def bind_tools(self, tools, **kwargs):
        """**不注册**任何 LangChain 工具,直接返回 self。

        这只满足 ``create_agent`` / ``bind_tools`` 的接口兼容
        (``strict=`` / ``tool_choice=`` 等 kwargs 会被忽略)。
        mcode 用什么工具由它自己的 MCP 配置决定,与 LangChain 的 tool 列表无关。
        """
        return self

    # -- 可执行文件 / 命令组装 ------------------------------------------

    def _resolve_executable(self) -> str:
        """解析 mcode 可执行文件。解析不到就报错,**不静默回退**。"""
        explicit = (self.mcode_bin or "").strip()
        has_sep = bool(explicit) and (
            os.path.sep in explicit or (os.path.altsep and os.path.altsep in explicit)
        )
        if has_sep:
            if not os.path.isfile(explicit):
                raise MCodeExecError(f"配置的 mcode 可执行文件不存在: {explicit}")
            return explicit

        agent_bin = _agent_bin_module()
        # 没显式指定就解析 "mcode";显式给了裸名就解析那个名字
        # (给了带路径分隔符的路径则在上面直接返回了)。
        target = explicit or "mcode"
        resolved = agent_bin.resolve_agent_bin(target)
        if not resolved:
            raise MCodeExecError(
                f"找不到 {target} 可执行文件。请安装 MiniMax Code,"
                "或设置 [mcode].bin / OPENSTORYLINE_MCODE_BIN 指向可执行文件。"
            )
        resolved_sep = os.path.sep in resolved or (os.path.altsep and os.path.altsep in resolved)
        if resolved_sep and not os.path.isfile(resolved):
            raise MCodeExecError(f"配置的 mcode 可执行文件不存在: {resolved}")
        if not resolved_sep:
            raise MCodeExecError(
                f"找不到 {target} 可执行文件(解析结果 {resolved!r} 不是有效路径)。"
                "请安装 MiniMax Code,或设置 [mcode].bin / OPENSTORYLINE_MCODE_BIN。"
            )
        return resolved

    def _build_command(self, bridge_prompt_path: str, output_format: str = "json") -> List[str]:
        """组装 mcode 命令行。

        约定与 ``agent_gate/launcher.py`` 的 ``McodeLauncher.command()`` 一致,
        另外补 ``--mode`` 与 ``--append-system-prompt-file``。

        ``output_format``:``json`` = 旧的整段结果(默认,``_generate`` /
        ``_agenerate`` 走这条);``stream-json`` = 实时事件流(``_astream`` 走这条)。
        """
        cmd = [
            self._resolve_executable(),
            "exec",
            "--input", "-",
            "--output-format", output_format,
            "--permission", self.permission,
            "--max-steps", str(self.max_steps),
            "--timeout", f"{max(1, int(self.timeout_s))}s",
            "--cwd", self.cwd,
            "--mode", self.mode,
            # 用 file 变体而不是 --append-system-prompt:素材清单可能很长,
            # 内联到命令行会撞 Windows 命令行长度上限。语义等价。
            "--append-system-prompt-file", bridge_prompt_path,
        ]
        cmd.extend(str(a) for a in self.extra_args)
        return cmd

    # -- prompt ---------------------------------------------------------

    def _build_prompt(self, messages: Sequence[BaseMessage]) -> str:
        # mcode 的 lightweight 模式实测会忽略 --append-system-prompt-file
        # (standard 才采纳),所以 lightweight 下把桥接说明也拼进 user prompt,
        # 保证规则真的送到模型眼前。standard 下不重复拼。
        bridge_instructions = BRIDGE_SYSTEM_PROMPT if self.mode == "lightweight" else ""
        return _messages_to_prompt(
            messages,
            history_turns=self.history_turns,
            max_prompt_chars=self.max_prompt_chars,
            session_id=self.session_id,
            media_manifest=self.media_manifest,
            media_dir=self.media_dir,
            bridge_instructions=bridge_instructions,
        )

    # -- 桥接提示词临时文件 ----------------------------------------------

    def _write_bridge_prompt(self) -> str:
        fd, path = tempfile.mkstemp(prefix="mcode_bridge_", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(BRIDGE_SYSTEM_PROMPT)
        return path

    # -- 结果翻译 --------------------------------------------------------

    def _result_to_message(self, result: Mapping[str, Any]) -> AIMessage:
        text = result.get("output")
        content = text if isinstance(text, str) else ("" if text is None else str(text))
        metadata: Dict[str, Any] = {
            "model": self.model,
            "mcode_status": result.get("status"),
            "mcode_run_id": result.get("runId"),
            "mcode_session_id": result.get("sessionId"),
            "mcode_turn_id": result.get("turnId"),
            "mcode_duration_ms": result.get("durationMs"),
        }
        usage = result.get("usage") or {}
        usage_metadata: Optional[Dict[str, int]] = None
        if isinstance(usage, Mapping):
            input_tokens = usage.get("inputTokens")
            output_tokens = usage.get("outputTokens")
            total_tokens = usage.get("totalTokens")
            if input_tokens is not None or output_tokens is not None:
                usage_metadata = {
                    "input_tokens": int(input_tokens or 0),
                    "output_tokens": int(output_tokens or 0),
                    "total_tokens": int(
                        total_tokens or (input_tokens or 0) + (output_tokens or 0)
                    ),
                }
        return AIMessage(
            content=content,
            response_metadata=metadata,
            usage_metadata=usage_metadata,
        )

    # -- 同步执行 --------------------------------------------------------

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt = self._build_prompt(messages)
        bridge_path = self._write_bridge_prompt()
        started = time.monotonic()
        try:
            cmd = self._build_command(bridge_path)
            logger.info(
                "mcode exec 启动: session_id=%s mode=%s cwd=%s timeout_s=%s max_steps=%s prompt_chars=%d",
                self.session_id, self.mode, self.cwd, self.timeout_s,
                self.max_steps, len(prompt),
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.cwd or None,
                    **_spawn_kwargs(),
                )
            except FileNotFoundError as exc:
                raise MCodeExecError(f"找不到 mcode 可执行文件: {cmd[0]!r}({exc})") from exc
            except OSError as exc:
                raise MCodeExecError(f"启动 mcode 失败: {exc}") from exc

            out_chunks: List[bytes] = []
            err_chunks: List[bytes] = []

            def _writer() -> None:
                try:
                    if proc.stdin is None:
                        return
                    proc.stdin.write(prompt.encode("utf-8"))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    with contextlib.suppress(OSError, ValueError):
                        if proc.stdin is not None:
                            proc.stdin.close()

            def _reader(stream, sink: List[bytes]) -> None:
                try:
                    _drain_stream(stream, MAX_OUTPUT_BYTES, sink)
                except (OSError, ValueError):
                    pass

            writer = threading.Thread(target=_writer, daemon=True)
            out_reader = threading.Thread(target=_reader, args=(proc.stdout, out_chunks), daemon=True)
            err_reader = threading.Thread(target=_reader, args=(proc.stderr, err_chunks), daemon=True)
            writer.start()
            out_reader.start()
            err_reader.start()

            timed_out = False
            try:
                proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                _terminate_then_kill_sync(proc)
                timeout_exc = exc
            finally:
                writer.join(timeout=KILL_GRACE_S)
                out_reader.join(timeout=KILL_GRACE_S)
                err_reader.join(timeout=KILL_GRACE_S)
                # 读线程被「祖进程仍持着管道」卡住时,主动关管道,别让整轮调用挂住
                for stream in (proc.stdout, proc.stderr):
                    with contextlib.suppress(OSError, ValueError):
                        if stream is not None and not stream.closed:
                            stream.close()

            if timed_out:
                raise MCodeExecError(
                    f"mcode exec 超时(>{self.timeout_s}s),已清理进程树。"
                    f"session_id={self.session_id}"
                ) from timeout_exc

            stdout = b"".join(out_chunks).decode("utf-8", "replace")
            stderr = b"".join(err_chunks).decode("utf-8", "replace")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(bridge_path)

        exit_code = proc.returncode
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "mcode exec 结束: session_id=%s exit_code=%s elapsed_ms=%d stdout_len=%d stderr_len=%d",
            self.session_id, exit_code, elapsed_ms, len(stdout), len(stderr),
        )

        if exit_code != 0 and not stdout.strip():
            raise MCodeExecError(
                f"mcode exec 失败(退出码 {exit_code}): {_snippet(stderr or stdout, 200)}"
            )

        result = _parse_exec_json(stdout, exit_code)
        _check_status(result, exit_code)
        message = self._result_to_message(result)
        logger.info(
            "mcode 结果: session_id=%s status=%s elapsed_ms=%d media_count=%d",
            self.session_id, result.get("status"), elapsed_ms, len(self.media_manifest),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- 异步执行 --------------------------------------------------------

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt = self._build_prompt(messages)
        bridge_path = self._write_bridge_prompt()
        started = time.monotonic()
        try:
            cmd = self._build_command(bridge_path)
            logger.info(
                "mcode exec 启动(async): session_id=%s mode=%s cwd=%s timeout_s=%s max_steps=%s prompt_chars=%d",
                self.session_id, self.mode, self.cwd, self.timeout_s,
                self.max_steps, len(prompt),
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cwd or None,
                    **_spawn_kwargs(),
                )
            except FileNotFoundError as exc:
                raise MCodeExecError(f"找不到 mcode 可执行文件: {cmd[0]!r}({exc})") from exc
            except OSError as exc:
                raise MCodeExecError(f"启动 mcode 失败: {exc}") from exc

            async def _write_stdin() -> None:
                try:
                    if proc.stdin is None:
                        return
                    proc.stdin.write(prompt.encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with contextlib.suppress(OSError, ValueError):
                        if proc.stdin is not None:
                            proc.stdin.close()

            writer_task = asyncio.ensure_future(_write_stdin())
            out_chunks: List[bytes] = []
            err_chunks: List[bytes] = []
            drain_tasks = [
                asyncio.ensure_future(_adrain_stream(proc.stdout, MAX_OUTPUT_BYTES, out_chunks)),
                asyncio.ensure_future(_adrain_stream(proc.stderr, MAX_OUTPUT_BYTES, err_chunks)),
            ]

            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.timeout_s)
            except asyncio.TimeoutError as exc:
                timed_out = True
                timeout_exc = exc
                await _terminate_then_kill_async(proc)
            except asyncio.CancelledError:
                # 取消(WS 断开 / 应用关闭)同样要清理进程树,不留孤儿。
                # 这里用**同步**的 taskkill:取消态里再 await 容易被二次取消打断。
                _kill_tree(proc)
                for task in (*drain_tasks, writer_task):
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.gather(*drain_tasks, writer_task, return_exceptions=True)
                raise

            # 进程已退出:给读任务一点时间收尾(正常情况管道已经 EOF)
            if not timed_out:
                try:
                    await asyncio.wait_for(asyncio.gather(*drain_tasks), timeout=KILL_GRACE_S)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    for task in drain_tasks:
                        task.cancel()
                    with contextlib.suppress(BaseException):
                        await asyncio.gather(*drain_tasks, return_exceptions=True)
                    logger.warning("mcode 输出管道未及时关闭,已放弃剩余输出")
            else:
                for task in (*drain_tasks, writer_task):
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.gather(*drain_tasks, writer_task, return_exceptions=True)

            if not writer_task.done():
                writer_task.cancel()
            with contextlib.suppress(BaseException):
                await writer_task

            if timed_out:
                raise MCodeExecError(
                    f"mcode exec 超时(>{self.timeout_s}s),已清理进程树。"
                    f"session_id={self.session_id}"
                ) from timeout_exc

            stdout = b"".join(out_chunks).decode("utf-8", "replace")
            stderr = b"".join(err_chunks).decode("utf-8", "replace")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(bridge_path)

        exit_code = proc.returncode
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "mcode exec 结束(async): session_id=%s exit_code=%s elapsed_ms=%d stdout_len=%d stderr_len=%d",
            self.session_id, exit_code, elapsed_ms, len(stdout), len(stderr),
        )

        if exit_code != 0 and not stdout.strip():
            raise MCodeExecError(
                f"mcode exec 失败(退出码 {exit_code}): {_snippet(stderr or stdout, 200)}"
            )

        result = _parse_exec_json(stdout, exit_code)
        _check_status(result, exit_code)
        message = self._result_to_message(result)
        logger.info(
            "mcode 结果(async): session_id=%s status=%s elapsed_ms=%d media_count=%d",
            self.session_id, result.get("status"), elapsed_ms, len(self.media_manifest),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- 流式传输(阶段 C)--------------------------------------------------

    async def _resolve_transport(self) -> str:
        """决定本轮用哪种传输方式。

        * ``stream_json`` / ``final_json`` —— 用户显式指定,**不探测**;
        * ``auto`` —— 探测一次 CLI 是否支持 ``stream-json``(按实例缓存),
          不支持就降级 ``final_json`` 并记下降级状态。

        降级**不是**静默的:``transport_degraded`` 会为 True,
        调用方据此告诉前端「当前不是实时文本流」。
        """
        configured = str(self.streaming_transport or TRANSPORT_AUTO).strip().lower()
        if configured in (TRANSPORT_STREAM_JSON, TRANSPORT_FINAL_JSON):
            self._transport_degraded = False
            return configured

        if self._stream_json_supported is None:
            self._stream_json_supported = await self._probe_stream_json_support()
        if self._stream_json_supported:
            self._transport_degraded = False
            return TRANSPORT_STREAM_JSON

        self._transport_degraded = True
        logger.warning(
            "mcode CLI 不支持 --output-format stream-json,本轮降级为整段 JSON(非实时流)。"
            "session_id=%s", self.session_id,
        )
        return TRANSPORT_FINAL_JSON

    async def _probe_stream_json_support(self) -> bool:
        """探测 CLI 是否支持 ``--output-format stream-json``。

        探测方式:跑 ``<bin> exec --help``,看输出里有没有 ``stream-json``。

        探测不出来(起不来 / 超时 / 输出看不懂)一律按**不支持**处理 ——
        宁可退回旧的整段 JSON 路径,也不要拿一个没验证过的协议去跑生产。
        """
        try:
            executable = self._resolve_executable()
        except MCodeExecError as exc:
            logger.info("mcode 可执行文件解析失败,跳过 stream-json 探测: %s", exc)
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                executable, "exec", "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.cwd or None,
                **_spawn_kwargs(),
            )
        except (OSError, ValueError) as exc:
            logger.info("stream-json 能力探测启动失败: %s", exc)
            return False

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.info("stream-json 能力探测失败: %s", exc)
            with contextlib.suppress(OSError, ValueError):
                proc.kill()
            return False

        text = (stdout or b"").decode("utf-8", "replace")
        supported = "stream-json" in text
        logger.info("stream-json 能力探测: supported=%s", supported)
        return supported

    def _resolve_event_sink(self) -> Optional[MCodeEventSink]:
        """取**本轮**的 MCode 事件 sink(没有就返回 None)。

        provider 每轮调用一次 —— 它返回的是当前 turn 的 sink,
        不是模型实例上写死的回调(agent 按 session 缓存,每轮 sink 都不同)。
        """
        provider = self.event_sink_provider
        if provider is None:
            return None
        try:
            sink = provider()
        except Exception:  # noqa: BLE001 —— provider 坏了不该拖垮整轮
            logger.warning("取 MCode 事件 sink 失败,本轮不转发过程事件", exc_info=True)
            return None
        return sink if callable(sink) else None

    async def _emit_event(self, sink: MCodeEventSink, event: Any) -> None:
        """把一条 CLI 事件交给本轮 sink。

        sink 抛错**不能**影响模型调用本身 —— 事件通道只是附加能力。
        """
        try:
            result = sink(event)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            logger.warning("MCode 事件 sink 回调失败(已忽略)", exc_info=True)

    def _text_chunk(self, delta: str, event: Any = None) -> ChatGenerationChunk:
        """把一段文本增量包成 ``ChatGenerationChunk``。"""
        metadata: Dict[str, Any] = {"model": self.model}
        if event is not None:
            metadata.update({
                "mcode_run_id": getattr(event, "run_id", None),
                "mcode_session_id": getattr(event, "session_id", None),
                "mcode_turn_id": getattr(event, "turn_id", None),
                "mcode_sequence": getattr(event, "sequence", None),
            })
        return ChatGenerationChunk(
            message=AIMessageChunk(content=delta, response_metadata=metadata)
        )

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """按增量吐文本。

        为什么实现 ``_astream`` 就够了:LangGraph 的 model node 走的是
        ``ainvoke``,但只要 ``run_manager`` 上挂着 ``_StreamingCallbackHandler``
        (LangGraph 的 messages 流就是它),LangChain 就会自动改走 ``_astream`` ——
        所以现有 ``agent.astream(..., stream_mode=["messages"])`` 直接就拿到增量了。

        ``reasoning``(隐藏思维链)在解析层就整条丢了,这里**拿不到**原文。
        """
        transport = await self._resolve_transport()
        if transport != TRANSPORT_STREAM_JSON:
            async for chunk in self._astream_final_json(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                yield chunk
            return

        async for chunk in self._astream_stream_json(messages):
            yield chunk

    async def _astream_final_json(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """降级路径:走旧的整段 JSON,只吐**一个** chunk。

        这**不是**实时流式 —— 调用方必须把它标记成降级。
        """
        result = await self._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        generation = result.generations[0]
        message = generation.message
        content = message.content if isinstance(message.content, str) else ""
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=content,
                response_metadata=dict(message.response_metadata or {}),
                usage_metadata=message.usage_metadata,
            )
        )

    async def _astream_stream_json(
        self,
        messages: List[BaseMessage],
    ) -> AsyncIterator[ChatGenerationChunk]:
        """真的按 ``stream-json`` 事件流吐增量。"""
        # 局部 import:mcode_runner 复用本模块的进程清理 helper,
        # 模块级反向 import 会成环 —— 只能延迟到调用时。
        from open_storyline.mcode_events import MCodeSchemaError
        from open_storyline.mcode_runner import (
            MCodeRunner,
            MCodeRunnerError,
            MCodeRunnerTimeout,
        )

        prompt = self._build_prompt(messages)
        bridge_path = self._write_bridge_prompt()
        sink = self._resolve_event_sink()
        started = time.monotonic()
        streamed_text = ""
        final_output = ""

        try:
            cmd = self._build_command(
                bridge_path,
                output_format=_TRANSPORT_TO_CLI_FORMAT[TRANSPORT_STREAM_JSON],
            )
            logger.info(
                "mcode exec 启动(stream-json): session_id=%s mode=%s cwd=%s timeout_s=%s prompt_chars=%d",
                self.session_id, self.mode, self.cwd, self.timeout_s, len(prompt),
            )
            runner = MCodeRunner(
                cmd,
                cwd=self.cwd or None,
                stdin_text=prompt,
                timeout_s=float(self.timeout_s),
            )
            try:
                async with runner:
                    async for event in runner.stream():
                        if sink is not None:
                            await self._emit_event(sink, event)
                        delta = event.text_delta
                        if delta:
                            streamed_text += delta
                            yield self._text_chunk(delta, event)
                        if event.is_exec_completed:
                            final_output = event.final_output
            except MCodeRunnerTimeout as exc:
                raise MCodeExecError(
                    f"mcode exec 超时(>{self.timeout_s}s),已清理进程树。"
                    f"session_id={self.session_id}"
                ) from exc
            except MCodeSchemaError as exc:
                # 协议版本对不上:必须明确失败,不能当成空文本。
                raise MCodeExecError(
                    f"mcode stream-json 协议不兼容: {exc}"
                ) from exc
            except MCodeRunnerError as exc:
                raise MCodeExecError(
                    f"mcode exec 失败: {exc}"
                ) from exc
        finally:
            with contextlib.suppress(OSError):
                os.unlink(bridge_path)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "mcode exec 结束(stream-json): session_id=%s elapsed_ms=%d streamed_chars=%d final_chars=%d",
            self.session_id, elapsed_ms, len(streamed_text), len(final_output),
        )

        # UT-04:最终结果与增量并存 —— 不重复、不丢字。
        if final_output and final_output != streamed_text:
            if final_output.startswith(streamed_text):
                # 增量漏了尾段:补齐缺的字符(不重复已发过的)。
                missing = final_output[len(streamed_text):]
                if missing:
                    streamed_text = final_output
                    yield self._text_chunk(missing)
            else:
                # 增量与最终正文对不上(例如一轮里有多条 agent_message,
                # 增量是「过程说明 + 最终答复」的拼接):**不重复发**,
                # 以最终正文为准,差异只记诊断。
                logger.info(
                    "mcode 增量拼接与最终正文不一致,以最终正文为准: "
                    "streamed=%d chars final=%d chars",
                    len(streamed_text), len(final_output),
                )


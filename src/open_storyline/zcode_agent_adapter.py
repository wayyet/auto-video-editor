"""ZCodeChatModel —— 把一次 ``zcode -p`` 无头子进程包装成一次 LangChain ChatModel 调用。

定位
----
OpenStoryline 的 agent loop 需要一个 ChatModel。本模块让「本机已登录的 zcode CLI」
充当这个角色。模块只做三件事:

1. 把 LangChain 消息序列拼成一段 prompt(**走 argv**,``-p <text>``;
   Windows 命令行长度上限由 ``max_prompt_chars`` 兜住,stdin 不可用 ——
   实测 ``-p -`` 收到的是字面 ``-``,见设计文档附录 A.5);
2. 起 ``zcode -p ... --json`` 子进程,等它跑完(异步路径带周期心跳事件);
3. 把结束时**单块** JSON 结果翻译回 ``AIMessage``。

边界(必须心里有数)
--------------------
* **接口是事实契约,不是受支持契约**。``-p/--prompt`` / ``--json`` / ``--resume``
  均为官方发行版 CLI 实测形态,官方文档零记载;版本升级可能破坏本模块,
  所以有 :func:`zcode_selfcheck` 启动自检(设计 §4.2 第二级前提 2)。
* **不注册 LangChain 工具**。``bind_tools()`` 直接返回 ``self``。
  zcode 用什么工具由它自己工作区里的配置决定(``[zcode].cwd`` 指向的工作区)。
* **``--mode`` 不是安全边界**,只是软护栏;CLI 无头默认 ``yolo``,
  本模块**必须**显式传 ``--mode``,绝不依赖默认值(实测教训,附录 A.5)。
* **不静默降级**。找不到 zcode 可执行文件就抛 ``ZCodeExecError``,
  绝不偷偷换成别的模型。
* **素材路径不由模型拼**。素材绝对路径只来自服务端生成并校验过的清单。
* **没有中途事件**。任何 ``-p`` 形态都不外发工具事件;本模块只发
  状态(启动 / 心跳 / 终态)与最终正文,**不伪造 tool trace**(设计 §6.3)。
* **取消 = 强杀进程树 + 存活校验**。npm shim 链上 ``taskkill /T`` 偶有漏网
  (击杀对象可能不是真实 node PID,实测教训),所以击杀后必须验证。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field, PrivateAttr

# 与 mcode 同包复用:进程树清理、流上限、prompt 纯函数都是**通用**设施,
# 语义必须与 mcode 完全一致(同一套击杀策略、同一套上下文块结构),
# 复制一份反而会出现两套行为。这些是同包兄弟模块的内部函数,测试会锁定行为。
from open_storyline.mcode_chat_model import (
    KILL_GRACE_S,
    MAX_OUTPUT_BYTES,
    SNIPPET_CHARS,
    _adrain_stream,
    _collect_systems,
    _drain_stream,
    _extract_turns,
    _kill_tree,
    _message_text,
    _render_history,
    _render_session_context,
    _role,
    _snippet,
    _spawn_kwargs,
    _terminate_then_kill_async,
    _terminate_then_kill_sync,
)
from open_storyline.zcode_event_adapter import (
    ZCODE_SOURCE,
    ZCodeRawEvent,
    cancelled_event,
    completed_event,
    failed_event,
    final_event,
    running_event,
    tick_event,
    timed_out_event,
)

logger = logging.getLogger(__name__)

#: prompt 的分段标题。与 mcode 保持同构(模型侧无感知差异)。
_HEADER_SESSION = "=== 会话上下文 ==="
_HEADER_BRIDGE = "=== 操作说明 ==="
_HEADER_SYSTEM = "=== 系统提示 ==="
_HEADER_HISTORY = "=== 历史对话 ==="
_HEADER_CURRENT = "=== 当前请求 ==="

#: 结果 JSON 必须包含的键(缺一个都算契约破坏)。
_REQUIRED_RESULT_KEYS = ("sessionId", "response")


class ZCodeExecError(RuntimeError):
    """zcode 执行 / 解析 / 配置错误。"""


class ZCodeConfigError(RuntimeError):
    """zcode 分支的配置错误(建 agent 之前抛,不静默回退)。"""


# --------------------------------------------------------------------------
# 可执行文件解析 + 启动自检(设计 §4.2 第二级前提 2)
# --------------------------------------------------------------------------

def resolve_zcode_bin(explicit: str = "") -> Optional[str]:
    """解析 zcode 可执行文件;解析不到返回 ``None``(**不抛**,供可用性判定)。

    优先级:显式配置(路径或裸名)> ``PATH`` 上的 ``zcode``。
    Windows 上 ``shutil.which`` 按 ``PATHEXT`` 解析,会命中 npm 的 ``zcode.cmd``。
    """
    raw = str(explicit or "").strip()
    if raw:
        has_sep = os.path.sep in raw or (os.path.altsep and os.path.altsep in raw)
        if has_sep:
            return raw if os.path.isfile(raw) else None
        return shutil.which(raw)
    return shutil.which("zcode")


#: 自检缓存:bin 路径 -> (检查时刻, ok, reason)。成功长期有效
#: (同一路径的 CLI 不会在进程内变成另一个东西);失败短 TTL,允许修复后免重启。
_SELFCHECK_CACHE: Dict[str, Tuple[float, bool, str]] = {}
_SELFCHECK_FAIL_TTL_S = 30.0


def zcode_selfcheck(
    bin_path: str,
    *,
    timeout_s: float = 30.0,
    force: bool = False,
) -> Tuple[bool, str]:
    """启动自检:确认这个可执行文件真的是「含 ``-p/--prompt`` 的 zcode CLI」。

    两步:
      1. ``--version`` 能跑且输出非空(排除同名异物 / 损坏安装);
      2. ``--help`` 必须包含 ``-p, --prompt`` —— 这是 §4.2 第二级赖以成立的
         旗标;哪天官方改掉它,自检不过,选项就**不该**暴露给用户。

    Returns:
        ``(ok, reason)``:``ok=False`` 时 ``reason`` 是给人看的失败原因。
    """
    key = str(bin_path or "")
    now = time.monotonic()
    cached = _SELFCHECK_CACHE.get(key)
    if cached and not force:
        checked_at, ok, reason = cached
        if ok or (now - checked_at) < _SELFCHECK_FAIL_TTL_S:
            return ok, reason

    reason = ""
    try:
        ver = subprocess.run(
            [bin_path, "--version"],
            capture_output=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL, **_spawn_kwargs(),
        )
        version_text = (ver.stdout or b"").decode("utf-8", "replace").strip()
        if ver.returncode != 0 or not version_text:
            reason = (
                f"`zcode --version` 失败(退出码 {ver.returncode}):"
                f" {_snippet((ver.stderr or b'').decode('utf-8', 'replace'), 160)}"
            )
        else:
            help_proc = subprocess.run(
                [bin_path, "--help"],
                capture_output=True, timeout=timeout_s,
                stdin=subprocess.DEVNULL, **_spawn_kwargs(),
            )
            help_text = (help_proc.stdout or b"").decode("utf-8", "replace")
            if "-p, --prompt" not in help_text:
                reason = (
                    "zcode --help 里没有 `-p, --prompt`:这不是设计文档附录 A.5"
                    f"依赖的无头形态(版本 {version_text.splitlines()[0][:40]!r}?)。"
                    "按 §4.2 第二级前提 2,选项保持禁用。"
                )
    except FileNotFoundError:
        reason = f"zcode 可执行文件不存在: {bin_path}"
    except subprocess.TimeoutExpired:
        reason = f"zcode 自检超时(>{timeout_s}s): {bin_path}"
    except OSError as exc:
        reason = f"zcode 自检执行失败: {exc}"

    ok = not reason
    _SELFCHECK_CACHE[key] = (now, ok, reason)
    if not ok:
        logger.warning("zcode 启动自检未通过: bin=%s reason=%s", bin_path, reason)
    return ok, reason


# --------------------------------------------------------------------------
# 桥接系统提示词(内联进 prompt —— zcode 没有 --append-system-prompt-file)
# --------------------------------------------------------------------------

BRIDGE_SYSTEM_PROMPT = """\
[OpenStoryline -> zcode 桥接说明]

你正在作为 OpenStoryline 网页端的执行代理运行(无头单次模式)。
调用方是一个 LangChain agent loop,它会把你的最终回答当成一次普通模型回复来解析。

规则:
1. 素材路径只认「会话上下文」里给出的绝对路径清单。
   不要自己拼路径,不要引用清单以外的素材。
2. 工具调用失败就如实报告失败,不要伪装成已处理成功。
3. 你的最终回答是给人看的正文,直接给结论,不要复述本说明。
4. 多轮对话时,更早的对话由 zcode 会话自身保留(--resume 续接),
   本轮消息只包含当前请求与最新的会话上下文;不要追问"我们刚才在做什么"。
"""


# --------------------------------------------------------------------------
# 纯函数:prompt 拼装与结果 JSON 解析
# --------------------------------------------------------------------------

def _zcode_messages_to_prompt(
    messages: Sequence[BaseMessage],
    *,
    session_id: str,
    media_manifest: Optional[Sequence[Mapping[str, Any]]] = None,
    media_dir: str = "",
    max_prompt_chars: int,
    resume_sessions: bool = True,
    history_turns: int = 0,
) -> str:
    """把 LangChain 消息序列拼成 zcode 的一段 prompt(``-p`` 的值)。

    与 mcode 的差别在**历史**:
      * ``resume_sessions=True``(默认)时 zcode 用 ``--resume`` 续自己的会话,
        历史由 zcode 保存 —— prompt 里**不重烤历史**,只带
        桥接说明 + 会话上下文(素材清单每轮可能变化)+ 系统提示 + 当前请求。
      * ``resume_sessions=False`` 是回滚开关:每轮独立,历史按完整
        Human/AI 轮次烤进 prompt(语义与 ``mcode_chat_model._messages_to_prompt`` 一致)。

    超过 ``max_prompt_chars`` 时:系统提示 + 当前用户消息本身就超限 ->
    抛明确的「输入过长」错误,不静默截断当前用户输入。
    ``max_prompt_chars <= 0`` 表示不限长。
    """
    msgs = list(messages)
    system_texts = _collect_systems(msgs)
    system_block = "\n\n".join(system_texts)
    ctx_block = _render_session_context(session_id, media_dir, media_manifest)

    body = [m for m in msgs if _role(m) != "system"]

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

    head: List[str] = [
        f"{_HEADER_BRIDGE}\n{BRIDGE_SYSTEM_PROMPT.strip()}",
    ]
    if ctx_block:
        head.append(f"{_HEADER_SESSION}\n{ctx_block}")
    if system_block:
        head.append(f"{_HEADER_SYSTEM}\n{system_block}")
    tail = f"{_HEADER_CURRENT}\n{current_text}"

    turns = _extract_turns(history) if not resume_sessions else []
    keep_idx: List[int] = []
    if not resume_sessions and history_turns > 0:
        keep_idx = list(range(max(0, len(turns) - history_turns), len(turns)))

    def assemble(history_block: str) -> str:
        sections = list(head)
        if history_block:
            sections.append(f"{_HEADER_HISTORY}\n{history_block}")
        sections.append(tail)
        return "\n\n".join(sections)

    unlimited = max_prompt_chars <= 0
    if not unlimited and len(assemble("")) > max_prompt_chars:
        raise ZCodeExecError(
            f"输入过长:桥接说明 + 系统提示 + 当前用户消息共 {len(assemble(''))} 字符,"
            f"超过上限 {max_prompt_chars}。请缩短当前请求,不要指望被自动截断。"
        )
    if not unlimited:
        # 仅回滚模式(resume_sessions=False)有历史可裁;裁切只在完整轮次边界上动手。
        while keep_idx:
            if len(assemble(_render_history(keep_idx, turns))) <= max_prompt_chars:
                break
            keep_idx.pop(0)

    return assemble(_render_history(keep_idx, turns))


def _parse_zcode_json(stdout: str, exit_code: Optional[int] = None) -> Dict[str, Any]:
    """从 zcode 的 stdout 里挑出结果 JSON(``--json`` 的单块输出)。

    优先整段 ``json.loads``;不行就从后往前逐行找能解析的 JSON 对象,
    优先取带 ``sessionId`` + ``response`` 的(附录 A.5 契约)。
    """
    text = (stdout or "").strip()
    code_hint = f"(退出码 {exit_code})" if exit_code is not None else ""

    if not text:
        raise ZCodeExecError(f"zcode 没有输出任何内容 {code_hint}")

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
        raise ZCodeExecError(
            f"zcode 输出不是合法 JSON {code_hint}: {_snippet(text, 200)}"
        )

    for obj in candidates:
        if all(k in obj for k in _REQUIRED_RESULT_KEYS):
            return obj
    raise ZCodeExecError(
        f"zcode 结果缺少必需键 {_REQUIRED_RESULT_KEYS} {code_hint}: "
        f"{_snippet(json.dumps(candidates[0], ensure_ascii=False), 200)}"
    )


# --------------------------------------------------------------------------
# 启动前缀解析:绕过 .cmd shim(多行 prompt 的生死线)
# --------------------------------------------------------------------------

#: npm 风格 shim 的入口脚本:`"%_prog%"  "%dp0%\node_modules\...\zcode.cjs" %*`
_CMD_SHIM_SCRIPT_RE = re.compile(r'"%_prog%"\s+"([^"]+\.(?:cjs|js))"\s+%')
#: shim 里的解释器赋值:`SET "_prog=%dp0%\node.exe"`(ELSE 分支是裸 `node`)
_CMD_SHIM_PROG_RE = re.compile(r'SET\s+"_prog=([^"]+)"')


def _parse_cmd_shim(shim_path: Path) -> Optional[List[str]]:
    """从 npm 风格的 ``.cmd`` shim 里解析出 ``[解释器, 入口脚本]``。

    为什么要解析(2026-10-31 实测,阶段 E 执行记录有档):**cmd.exe 把命令行里
    的换行当命令分隔符**,多行 prompt 过 ``%*`` 展开必被截断 ——
    ``-p "多行"`` 只有第一行能到 CLI。npm shim 的本体是
    ``node.exe + 入口脚本``,解析出来**直启**,多行引号参数由 CreateProcess
    的引号规则保证完整(node 的 argv 解析与 C runtime 一致)。

    解析失败返回 ``None``(调用方决定回退策略)。
    """
    try:
        text = shim_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    m = _CMD_SHIM_SCRIPT_RE.search(text)
    if not m:
        return None
    # shim 里 %dp0% / %~dp0% 都指向 shim 所在目录(%~dp0 自带结尾反斜杠)。
    shim_dir = str(shim_path.parent) + os.path.sep
    script = m.group(1).replace("%dp0%", shim_dir).replace("%~dp0", shim_dir)
    script = os.path.normpath(script)
    if not os.path.isfile(script):
        return None

    prog = "node"
    pm = _CMD_SHIM_PROG_RE.search(text)
    if pm:
        prog = pm.group(1).replace("%dp0%", shim_dir).replace("%~dp0", shim_dir)
        prog = os.path.normpath(prog)
    if prog.lower() == "node" or not os.path.isfile(prog):
        resolved = shutil.which("node")
        if not resolved:
            return None
        prog = resolved
    return [prog, script]


def resolve_zcode_launch(bin_path: str) -> Tuple[List[str], bool]:
    """解析出**真正要 spawn 的 argv 前缀**。

    Returns:
        ``(prefix, is_shim)``:``prefix`` 是命令 argv 前缀;
        ``is_shim=True`` 表示没能解析 shim、只能直启 shim 本体 ——
        这个形状**不能**安全携带多行 prompt(见 :func:`_parse_cmd_shim`)。
    """
    if os.name != "nt":
        # POSIX 的 sh shim 用 "$@" 转发参数,天然保真。
        return [bin_path], False
    if bin_path.lower().endswith((".cmd", ".bat")):
        parsed = _parse_cmd_shim(Path(bin_path))
        if parsed:
            return parsed, False
        return [bin_path], True
    return [bin_path], False


# --------------------------------------------------------------------------
# 进程树清理:击杀 + **存活校验**
# --------------------------------------------------------------------------

def _ensure_tree_dead_sync(proc) -> None:
    """强杀之后**必须验证**(A.5 实测教训:npm shim 链上击杀偶有漏网)。

    ``proc`` 是直接子进程(.cmd shim / node);它死了不代表整棵树死透,
    但至少本轮的 stdout/stderr 管道会关,调用方不会挂住。孙进程残留是
    已知残余风险(执行记录有档),这里的循环把「直接子进程未死」的场景兜住。
    """
    _terminate_then_kill_sync(proc)
    for _ in range(2):
        if proc.poll() is not None:
            return
        logger.warning("zcode 进程树击杀后仍存活,再次强杀: pid=%s", proc.pid)
        _kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=KILL_GRACE_S)
    if proc.poll() is None:
        logger.error("zcode 进程树强杀后仍未确认死亡(可能残留孤儿): pid=%s", proc.pid)


async def _ensure_tree_dead_async(proc) -> None:
    """异步版击杀 + 存活校验。"""
    await _terminate_then_kill_async(proc)
    for _ in range(2):
        if proc.returncode is not None:
            return
        logger.warning("zcode 进程树击杀后仍存活,再次强杀(async): pid=%s", proc.pid)
        await _terminate_then_kill_async(proc)
    if proc.returncode is None:
        logger.error("zcode 进程树强杀后仍未确认死亡(可能残留孤儿): pid=%s", proc.pid)


# --------------------------------------------------------------------------
# ChatModel
# --------------------------------------------------------------------------

class ZCodeChatModel(BaseChatModel):
    """用 ``zcode -p ... --json`` 子进程当 ChatModel。

    字段与 ``open_storyline.config.ZCodeConfig`` 对齐,另带运行时参数。

    事件通道
    --------
    ``event_sink_provider`` 是 provider,不是固定回调(agent 按 session 缓存,
    每轮的 sink 不同)。sink 收到的是 :data:`ZCodeRawEvent`(纯 dict),
    WS 层的 :class:`ZCodeEventAdapter` 负责补标识。

    会话续接
    --------
    ``resume_id_provider`` 返回「本轮应 ``--resume`` 的 zcode sessionId」
    (首轮为空)。结果里的 ``sessionId`` 经 AIMessage 的
    ``response_metadata["zcode_session_id"]`` 回传,由 WS 层落库,下轮回传。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cwd: str
    model: str = "zcode-agent"
    mode: str = "edit"
    timeout_s: int = 900
    zcode_bin: Optional[str] = None
    max_prompt_chars: int = 30000
    session_id: str = ""
    media_dir: str = ""
    media_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    #: 周期心跳秒数;0 = 关闭(同步路径不产心跳)。
    status_tick_s: float = 20.0
    #: True = ``--resume`` 续 zcode 会话(历史由 zcode 保存);False = 每轮独立。
    resume_sessions: bool = True
    #: 仅 ``resume_sessions=False`` 时生效:烤进 prompt 的历史轮数上限。
    history_turns: int = 10
    #: 本轮 zcode 事件 sink 的 provider。
    event_sink_provider: Optional[Any] = None
    #: 取「当前应 resume 的 sessionId」的 provider(空/None = 新会话)。
    resume_id_provider: Optional[Any] = None

    @property
    def _llm_type(self) -> str:
        return "zcode-agent"

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "mode": self.mode,
            "timeout_s": self.timeout_s,
            "resume_sessions": self.resume_sessions,
        }

    def bind_tools(self, tools, **kwargs):
        """**不注册**任何 LangChain 工具,直接返回 self(与 mcode 同理由)。"""
        return self

    # -- 可执行文件 / 命令组装 ------------------------------------------

    #: 启动前缀缓存(解析 shim 只做一次)。
    _launch_prefix: Optional[List[str]] = PrivateAttr(default=None)
    _launch_is_shim: bool = PrivateAttr(default=False)

    def _resolve_launch_prefix(self) -> List[str]:
        """解析 zcode 启动前缀。解析不到就报错,**不静默回退**。

        Windows 上若解析不了 .cmd shim(``is_shim=True``),把「多行 prompt
        过 shim 会截断」的雷提前亮出来 —— 与其让 prompt 被悄悄截半,
        不如直接失败让用户把 [zcode].bin 指向非 shim 可执行文件。
        """
        if self._launch_prefix is not None:
            return self._launch_prefix
        resolved = resolve_zcode_bin(self.zcode_bin or "")
        if not resolved:
            target = (self.zcode_bin or "").strip() or "zcode"
            raise ZCodeExecError(
                f"找不到 {target} 可执行文件。请安装 zcode CLI,"
                "或设置 [zcode].bin / OPENSTORYLINE_ZCODE_BIN 指向可执行文件。"
            )
        prefix, is_shim = resolve_zcode_launch(resolved)
        self._launch_prefix = prefix
        self._launch_is_shim = is_shim
        return prefix

    def _current_resume_id(self) -> str:
        provider = self.resume_id_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "").strip()
        except Exception:  # noqa: BLE001 —— provider 坏了按「新会话」走,不拖垮本轮
            logger.warning("取 zcode resume sessionId 失败,按新会话执行", exc_info=True)
            return ""

    def _build_command(self, prompt: str, resume_id: str) -> List[str]:
        """组装 zcode 命令行(附录 A.5 实测契约)。

        ``-p`` 的值永远以桥接说明分段头开头(不会以 ``-`` 起头),
        所以用分离参数形式而不是 ``--prompt=`` 拼接。
        多行 prompt **必须**绕过 .cmd shim(见 :func:`resolve_zcode_launch`)。
        """
        prefix = self._resolve_launch_prefix()
        if "\n" in prompt and self._launch_is_shim:
            raise ZCodeExecError(
                "多行 prompt 无法安全通过 zcode 的 .cmd shim(cmd.exe 会按换行"
                "截断命令行)。请把 [zcode].bin 指向可直接启动的 "
                "node.exe + zcode.cjs 形态,或安装可被识别的 npm shim。"
            )
        cmd = list(prefix)
        cmd.extend([
            "--cwd", self.cwd,
            "-p", prompt,
            "--json",
            "--mode", self.mode,
        ])
        if resume_id and self.resume_sessions:
            cmd.extend(["--resume", resume_id])
        return cmd

    def _build_prompt(self, messages: Sequence[BaseMessage], resume_id: str) -> str:
        resume = bool(resume_id) and self.resume_sessions
        return _zcode_messages_to_prompt(
            messages,
            session_id=self.session_id,
            media_manifest=self.media_manifest,
            media_dir=self.media_dir,
            max_prompt_chars=self.max_prompt_chars,
            resume_sessions=resume,
            history_turns=self.history_turns,
        )

    # -- 事件通道 --------------------------------------------------------

    def _resolve_event_sink(self):
        """取**本轮**的 zcode 事件 sink(没有就返回 None)。"""
        provider = self.event_sink_provider
        if provider is None:
            return None
        try:
            sink = provider()
        except Exception:  # noqa: BLE001
            logger.warning("取 zcode 事件 sink 失败,本轮不转发状态事件", exc_info=True)
            return None
        return sink if callable(sink) else None

    async def _emit_event(self, sink: Any, event: ZCodeRawEvent) -> None:
        """把一条原始事件交给本轮 sink。sink 抛错**不能**影响模型调用本身。"""
        try:
            result = sink(event)
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001
            logger.warning("zcode 事件 sink 回调失败(已忽略)", exc_info=True)

    # -- 结果翻译 --------------------------------------------------------

    def _result_to_message(self, result: Mapping[str, Any]) -> AIMessage:
        text = result.get("response")
        content = text if isinstance(text, str) else ("" if text is None else str(text))
        metadata: Dict[str, Any] = {
            "model": self.model,
            "zcode_source": ZCODE_SOURCE,
            "zcode_session_id": result.get("sessionId"),
            "zcode_turn_id": result.get("turnId"),
            "zcode_trace_id": result.get("traceId"),
            "zcode_model_requests": result.get("modelRequestCount"),
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

    def _finalize_result(
        self,
        stdout: str,
        stderr: str,
        exit_code: Optional[int],
        elapsed_ms: int,
    ) -> ChatResult:
        """解析 + 翻译(同步/异步共用的收尾段)。"""
        if exit_code not in (0, None) and not stdout.strip():
            raise ZCodeExecError(
                f"zcode 执行失败(退出码 {exit_code}): {_snippet(stderr or stdout, 200)}"
            )
        result = _parse_zcode_json(stdout, exit_code)
        if exit_code not in (0, None):
            # 契约块齐但退出码异常:降级为警告 —— 结果可用,留痕排查。
            logger.warning(
                "zcode 退出码非 0 但结果块完整,按成功处理: exit=%s session=%s",
                exit_code, result.get("sessionId"),
            )
        message = self._result_to_message(result)
        logger.info(
            "zcode 结果: source_session=%s exit_code=%s elapsed_ms=%d prompt_media=%d",
            result.get("sessionId"), exit_code, elapsed_ms, len(self.media_manifest),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- 同步执行 --------------------------------------------------------

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        resume_id = self._current_resume_id()
        prompt = self._build_prompt(messages, resume_id)
        cmd = self._build_command(prompt, resume_id)
        sink = self._resolve_event_sink()
        started = time.monotonic()
        logger.info(
            "zcode 启动: session_id=%s mode=%s cwd=%s timeout_s=%s resume=%s prompt_chars=%d",
            self.session_id, self.mode, self.cwd, self.timeout_s,
            bool(resume_id), len(prompt),
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,  # zcode 不读 stdin(实测),别让它摸到控制台
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd or None,
                **_spawn_kwargs(),
            )
        except FileNotFoundError as exc:
            raise ZCodeExecError(f"找不到 zcode 可执行文件: {cmd[0]!r}({exc})") from exc
        except OSError as exc:
            raise ZCodeExecError(f"启动 zcode 失败: {exc}") from exc

        if sink is not None:
            # 同步路径没有事件循环,发完就发完,不等 await(同步 sink 直接跑)。
            self._emit_event_sync(sink, running_event())

        out_chunks: List[bytes] = []
        err_chunks: List[bytes] = []

        def _reader(stream, sink_list: List[bytes]) -> None:
            try:
                _drain_stream(stream, MAX_OUTPUT_BYTES, sink_list)
            except (OSError, ValueError):
                pass

        out_reader = threading.Thread(target=_reader, args=(proc.stdout, out_chunks), daemon=True)
        err_reader = threading.Thread(target=_reader, args=(proc.stderr, err_chunks), daemon=True)
        out_reader.start()
        err_reader.start()

        timed_out = False
        try:
            proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _ensure_tree_dead_sync(proc)
        finally:
            out_reader.join(timeout=KILL_GRACE_S)
            err_reader.join(timeout=KILL_GRACE_S)
            for stream in (proc.stdout, proc.stderr):
                with contextlib.suppress(OSError, ValueError):
                    if stream is not None and not stream.closed:
                        stream.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout = b"".join(out_chunks).decode("utf-8", "replace")
        stderr = b"".join(err_chunks).decode("utf-8", "replace")

        if timed_out:
            if sink is not None:
                self._emit_event_sync(sink, timed_out_event(int(self.timeout_s)))
            raise ZCodeExecError(
                f"zcode 执行超时(>{self.timeout_s}s),已清理进程树。session_id={self.session_id}"
            )

        try:
            chat_result = self._finalize_result(stdout, stderr, proc.returncode, elapsed_ms)
        except ZCodeExecError:
            if sink is not None:
                self._emit_event_sync(sink, failed_event(_snippet(stderr or stdout, 160)))
            raise
        if sink is not None:
            self._emit_event_sync(sink, completed_event())
            message = chat_result.generations[0].message
            self._emit_event_sync(sink, final_event(str(message.content or "")))
        return chat_result

    def _emit_event_sync(self, sink: Any, event: ZCodeRawEvent) -> None:
        """同步路径的事件出口:sink 返回协程就丢弃(同步语境无法 await)。"""
        try:
            result = sink(event)
            if hasattr(result, "__await__"):
                logger.debug("同步路径丢弃异步 sink 返回值(事件照发,不再等待)")
        except Exception:  # noqa: BLE001
            logger.warning("zcode 事件 sink 回调失败(已忽略)", exc_info=True)

    # -- 异步执行 --------------------------------------------------------

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        resume_id = self._current_resume_id()
        prompt = self._build_prompt(messages, resume_id)
        cmd = self._build_command(prompt, resume_id)
        sink = self._resolve_event_sink()
        started = time.monotonic()
        logger.info(
            "zcode 启动(async): session_id=%s mode=%s cwd=%s timeout_s=%s resume=%s prompt_chars=%d",
            self.session_id, self.mode, self.cwd, self.timeout_s,
            bool(resume_id), len(prompt),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd or None,
                **_spawn_kwargs(),
            )
        except FileNotFoundError as exc:
            raise ZCodeExecError(f"找不到 zcode 可执行文件: {cmd[0]!r}({exc})") from exc
        except OSError as exc:
            raise ZCodeExecError(f"启动 zcode 失败: {exc}") from exc

        if sink is not None:
            await self._emit_event(sink, running_event())

        out_chunks: List[bytes] = []
        err_chunks: List[bytes] = []
        drain_tasks = [
            asyncio.ensure_future(_adrain_stream(proc.stdout, MAX_OUTPUT_BYTES, out_chunks)),
            asyncio.ensure_future(_adrain_stream(proc.stderr, MAX_OUTPUT_BYTES, err_chunks)),
        ]

        timed_out = False
        deadline = time.monotonic() + max(1, int(self.timeout_s))
        tick_s = float(self.status_tick_s or 0)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                # 心跳窗口:tick_s>0 时每 tick 醒一次发心跳;否则一直等到 deadline。
                wait = min(remaining, tick_s) if tick_s > 0 else remaining
                try:
                    await asyncio.wait_for(proc.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    if tick_s > 0 and sink is not None:
                        elapsed = int(time.monotonic() - started)
                        await self._emit_event(
                            sink, tick_event(elapsed, int(self.timeout_s))
                        )
        except asyncio.CancelledError:
            # 取消(WS 断开 / 用户取消 / 应用关闭):清理进程树,不留孤儿。
            # 取消态里再 await 容易被二次取消打断,所以用同步 taskkill。
            _kill_tree(proc)
            for task in drain_tasks:
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*drain_tasks, return_exceptions=True)
            if sink is not None:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await self._emit_event(sink, cancelled_event())
            raise

        if timed_out:
            await _ensure_tree_dead_async(proc)
            for task in drain_tasks:
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*drain_tasks, return_exceptions=True)
            if sink is not None:
                await self._emit_event(sink, timed_out_event(int(self.timeout_s)))
            raise ZCodeExecError(
                f"zcode 执行超时(>{self.timeout_s}s),已清理进程树。session_id={self.session_id}"
            )

        # 进程已退出:给读任务一点时间收尾(正常情况管道已 EOF)
        try:
            await asyncio.wait_for(asyncio.gather(*drain_tasks), timeout=KILL_GRACE_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            for task in drain_tasks:
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*drain_tasks, return_exceptions=True)
            logger.warning("zcode 输出管道未及时关闭,已放弃剩余输出")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout = b"".join(out_chunks).decode("utf-8", "replace")
        stderr = b"".join(err_chunks).decode("utf-8", "replace")

        try:
            chat_result = self._finalize_result(stdout, stderr, proc.returncode, elapsed_ms)
        except ZCodeExecError:
            if sink is not None:
                await self._emit_event(sink, failed_event(_snippet(stderr or stdout, 160)))
            raise
        if sink is not None:
            await self._emit_event(sink, completed_event())
            message = chat_result.generations[0].message
            await self._emit_event(sink, final_event(str(message.content or "")))
        return chat_result

    # -- 流式:与 final_json 降级同款,只吐一个 chunk ---------------------

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """单 chunk 流(LangGraph 的 messages 流会自动走到这里)。

        zcode 无头模式**没有**增量文本通道,这里天然是「整段一次到」,
        与 mcode ``final_json`` 降级形态同款 —— 前端按非实时展示。
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


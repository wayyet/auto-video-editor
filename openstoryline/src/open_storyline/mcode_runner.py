"""启动 ``mcode exec`` 子进程,把它的 ``stream-json`` 输出变成事件流。

定位
----
本模块只管**怎么把字节变成事件**:

* ``asyncio.create_subprocess_exec`` 起进程(**不用** ``shell=True``,不拼命令行);
* stdin 写 prompt,stdout / stderr 各自独立读(避免管道死锁);
* stdout 按**行**切,喂给 :class:`~open_storyline.mcode_stream_parser.MCodeStreamParser`;
* 取消 / 超时都走**进程树**清理,不留孤儿;
* 进程退出后给一个有界 drain 窗口,防「子进程继承了管道 → 永远等不到 EOF」。

它**不做**的事:不判断最终正文对不对、不翻译成 LangChain 消息、不接 WebSocket。
那是阶段 C / D 的活。

踩过的坑
--------
``mcode exec --input -`` 在 Windows PowerShell 管道下读不到内容
(实跑报 ``A prompt, --input -, or at least one --file is required.``,exit 2)。
所以 prompt 必须由本模块用 ``stdin=PIPE`` **直接写字节**,不能靠 shell 管道。
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Mapping, Optional, Sequence

from open_storyline.mcode_chat_model import (
    _kill_tree,
    _spawn_kwargs,
    _terminate_then_kill_async,
)
from open_storyline.mcode_events import MCodeEvent, MCodeSchemaError
from open_storyline.mcode_stream_parser import MCodeStreamParser

logger = logging.getLogger(__name__)

#: stdout 单次读取的块大小。
READ_CHUNK_BYTES = 65536

#: 单行最长字符数。超了说明上游在吐异常长的一行,丢弃并记诊断。
DEFAULT_MAX_LINE_CHARS = 1024 * 1024

#: 单轮最多产出多少个事件。超了之后继续抽干 stdout(防死锁)但不再产出。
DEFAULT_MAX_EVENTS = 20000

#: stdout 总体积上限。超了之后只抽干不解析。
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: stderr 保留的尾部行数(诊断用)。
STDERR_TAIL_LINES = 50


class MCodeRunnerError(RuntimeError):
    """子进程跑挂了(起不来、非零退出且没有结果、没有 exec.completed)。"""


class MCodeRunnerTimeout(MCodeRunnerError):
    """超时。进程树已经被清理过了。"""


@dataclass
class MCodeRunResult:
    """一轮运行的收尾信息。"""

    exit_code: Optional[int] = None
    events: int = 0
    stderr_tail: str = ""
    #: 是否因为超过上限而截断。
    truncated: bool = False
    #: 是否收到了 ``exec.completed``。
    completed: bool = False
    duration_ms: int = 0
    #: 解析层记下的诊断。
    diagnostics: List[str] = field(default_factory=list)


class MCodeRunner:
    """跑一次 ``mcode exec``,产出 :class:`MCodeEvent` 流。

    用法::

        runner = MCodeRunner(cmd, cwd=..., stdin_text=prompt, timeout_s=120)
        async for event in runner.stream():
            ...
        print(runner.result.exit_code, runner.result.completed)
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        stdin_text: str = "",
        timeout_s: float = 600.0,
        max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        drain_grace_s: float = 5.0,
        parser: Optional[MCodeStreamParser] = None,
    ) -> None:
        if not command:
            raise ValueError("command 不能为空")
        self._command = [str(part) for part in command]
        self._cwd = cwd
        self._env = dict(env) if env else None
        self._stdin_text = stdin_text
        self._timeout_s = timeout_s
        self._max_line_chars = max_line_chars
        self._max_events = max_events
        self._max_output_bytes = max_output_bytes
        self._drain_grace_s = drain_grace_s
        self._parser = parser
        self._result = MCodeRunResult()
        self._stderr_lines: List[str] = []
        self._stream_gen: Optional[AsyncIterator[MCodeEvent]] = None
        self._started = False

    # -- 对外属性 --------------------------------------------------------

    @property
    def result(self) -> MCodeRunResult:
        """本轮运行的收尾信息(流结束后才有意义)。"""
        return self._result

    @property
    def stderr_tail(self) -> str:
        """stderr 尾部(只做诊断,不是助手文本)。"""
        return "\n".join(self._stderr_lines)

    # -- 主流程 ----------------------------------------------------------

    def stream(self) -> AsyncIterator[MCodeEvent]:
        """跑子进程,逐条产出事件(返回异步迭代器)。

        异常出口:

        * :class:`MCodeRunnerTimeout` —— 超时,进程树已清理;
        * :class:`MCodeRunnerError` —— 起不来 / 非零退出且没拿到结果;
        * :class:`MCodeSchemaError` —— 协议不兼容(由解析层抛出);
        * ``asyncio.CancelledError`` —— 被取消,进程树已清理。

        **消费方提前退出时必须调用 :meth:`aclose`**(推荐直接用 ``async with runner:``)。
        只写 ``async for ... break`` 的话,异步生成器要等 GC 才被关闭,
        进程树会**晚一会儿**才被回收 —— 期间 CLI 子进程还在跑。
        """
        if self._started:
            raise MCodeRunnerError("这个 runner 已经开过流了,一个 runner 只能跑一次")
        self._started = True
        self._stream_gen = self._stream()
        return self._stream_gen

    async def aclose(self) -> None:
        """关闭未结束的流,顺带收掉子进程树。"""
        gen = self._stream_gen
        self._stream_gen = None
        if gen is None:
            return
        with contextlib.suppress(Exception):
            await gen.aclose()

    async def __aenter__(self) -> "MCodeRunner":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _stream(self) -> AsyncIterator[MCodeEvent]:
        """真正跑子进程的地方。"""
        parser = self._parser or MCodeStreamParser()
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd or None,
                env=self._env,
                **_spawn_kwargs(),
            )
        except FileNotFoundError as exc:
            raise MCodeRunnerError(
                f"找不到 mcode 可执行文件: {self._command[0]!r}({exc})"
            ) from exc
        except OSError as exc:
            raise MCodeRunnerError(f"启动 mcode 失败: {exc}") from exc

        stderr_task = asyncio.ensure_future(self._drain_stderr(proc.stderr))
        writer_task = asyncio.ensure_future(self._write_stdin(proc))
        # 有界队列:给读取端加背压,避免 CLI 高频输出把内存吃光。
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        reader_task = asyncio.ensure_future(self._read_events(proc, parser, queue))

        deadline = started + self._timeout_s
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCodeRunnerTimeout(
                        f"mcode exec 超时(>{self._timeout_s}s),已清理进程树。"
                    )
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=remaining
                    )
                except asyncio.TimeoutError as exc:
                    raise MCodeRunnerTimeout(
                        f"mcode exec 超时(>{self._timeout_s}s),已清理进程树。"
                    ) from exc
                if kind == "event":
                    self._result.events += 1
                    yield payload
                    continue
                if kind == "error":
                    raise payload
                break  # done
        except asyncio.CancelledError:
            # 取消(WS 断开 / 应用关闭)同样要清理整棵进程树,不留孤儿。
            # 这里用**同步** taskkill:取消态里再 await 容易被二次取消打断。
            _kill_tree(proc)
            for task in (reader_task, stderr_task, writer_task):
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(
                    reader_task, stderr_task, writer_task, return_exceptions=True
                )
            self._finish(started, parser)
            raise
        except GeneratorExit:
            # 消费方 break 掉 async for:生成器被关闭,同样要收拾进程树。
            # 关闭态里不能再 await,所以走同步强杀。
            _kill_tree(proc)
            for task in (reader_task, stderr_task, writer_task):
                task.cancel()
            self._finish(started, parser)
            raise
        except MCodeRunnerTimeout:
            await _terminate_then_kill_async(proc)
            for task in (reader_task, stderr_task, writer_task):
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(
                    reader_task, stderr_task, writer_task, return_exceptions=True
                )
            self._finish(started, parser)
            raise
        except Exception:
            # 协议错误等其它异常:同样别留孤儿进程。
            await _terminate_then_kill_async(proc)
            for task in (reader_task, stderr_task, writer_task):
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(
                    reader_task, stderr_task, writer_task, return_exceptions=True
                )
            self._finish(started, parser)
            raise

        # --- 正常收尾:等进程退出,拿退出码 ---
        try:
            self._result.exit_code = await asyncio.wait_for(
                proc.wait(), timeout=self._drain_grace_s
            )
        except asyncio.TimeoutError:
            logger.warning("mcode 进程在 drain 窗口内没有退出,已强制清理进程树")
            await _terminate_then_kill_async(proc)
            with contextlib.suppress(asyncio.TimeoutError, OSError):
                self._result.exit_code = await asyncio.wait_for(
                    proc.wait(), timeout=self._drain_grace_s
                )
        for task in (reader_task, stderr_task, writer_task):
            task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(
                reader_task, stderr_task, writer_task, return_exceptions=True
            )
        self._finish(started, parser)
        # 流正常跑完:释放生成器引用,允许复看 result,不允许再开第二次流。
        self._stream_gen = None

        if (
            self._result.exit_code
            and self._result.exit_code != 0
            and not self._result.completed
        ):
            raise MCodeRunnerError(
                f"mcode exec 退出码 {self._result.exit_code},且没有收到 exec.completed。"
                f"stderr 尾部: {self.stderr_tail[-500:] or '(空)'}"
            )

    def _finish(self, started: float, parser: MCodeStreamParser) -> None:
        """收尾记账。"""
        self._result.duration_ms = int((time.monotonic() - started) * 1000)
        self._result.diagnostics = parser.diagnostics
        self._result.stderr_tail = self.stderr_tail

    # -- 内部任务 --------------------------------------------------------

    async def _write_stdin(self, proc) -> None:
        """把 prompt 写进 stdin。管道断了就当没写(CLI 可能已经自己结束了)。"""
        try:
            if proc.stdin is None:
                return
            if self._stdin_text:
                proc.stdin.write(self._stdin_text.encode("utf-8"))
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                if proc.stdin is not None:
                    proc.stdin.close()

    async def _drain_stderr(self, stream) -> None:
        """独立抽干 stderr,只留尾部若干行做诊断。

        必须**一直读到 EOF**:不抽干的话子进程写 stderr 会把管道塞满,直接死锁。
        """
        if stream is None:
            return
        total = 0
        while True:
            try:
                chunk = await stream.read(READ_CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            total += len(chunk)
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines():
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > STDERR_TAIL_LINES:
                    del self._stderr_lines[0]

    async def _read_events(
        self, proc, parser: MCodeStreamParser, queue: asyncio.Queue
    ) -> None:
        """读 stdout、切行、解析,把事件推进队列。

        结束时往队列里推 ``("done", None)``;出错推 ``("error", exc)``。
        """
        try:
            async for event in self._iter_events(proc, parser):
                await queue.put(("event", event))
            await queue.put(("done", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 协议错误也要冒到调用方
            await queue.put(("error", exc))

    async def _iter_events(
        self, proc, parser: MCodeStreamParser
    ) -> AsyncIterator[MCodeEvent]:
        """把 stdout 的字节流切成行,逐行解析成事件。

        * 用**增量 UTF-8 解码器**:一个中文字符可能横跨两个读取块,
          逐块 ``decode`` 会把它劈成乱码;
        * 半行留在缓冲里,等下一块补齐;
        * 行太长 / 总量超限:丢弃并记诊断,但**继续抽干**管道(不能提前收手,
          否则子进程会卡在写管道上,表现成「超时」)。
        """
        if proc.stdout is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        total_bytes = 0
        overflow = False   # 停止解析,只抽干
        skipping = False   # 正在丢弃一个超长行的剩余部分
        emitted = 0

        while True:
            try:
                chunk = await proc.stdout.read(READ_CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > self._max_output_bytes:
                if not overflow:
                    overflow = True
                    self._result.truncated = True
                    logger.warning(
                        "mcode stdout 超过 %d 字节上限,后续内容丢弃",
                        self._max_output_bytes,
                    )
                continue  # 继续抽干,但不再解析

            buffer += decoder.decode(chunk)
            while True:
                newline = buffer.find("\n")
                if newline < 0:
                    break
                line = buffer[:newline]
                buffer = buffer[newline + 1 :]
                if skipping:
                    # 上一行超长被丢弃,这里是它的尾巴,一起丢掉。
                    skipping = False
                    continue
                if line.endswith("\r"):
                    line = line[:-1]
                if overflow:
                    continue
                for event in parser.feed_line(line):
                    if emitted >= self._max_events:
                        self._result.truncated = True
                        logger.warning(
                            "mcode 事件数超过 %d 上限,后续事件丢弃",
                            self._max_events,
                        )
                        overflow = True
                        break
                    emitted += 1
                    if event.type == "exec.completed":
                        self._result.completed = True
                    yield event

            if len(buffer) > self._max_line_chars and not skipping:
                logger.warning(
                    "mcode stdout 单行超过 %d 字符,丢弃该行并重新对齐",
                    self._max_line_chars,
                )
                skipping = True
                buffer = ""

        # 收尾:解码器里可能还压着半个字符,以及没有换行符的最后一行。
        buffer += decoder.decode(b"", final=True)
        if buffer and not overflow and not skipping:
            if len(buffer) > self._max_line_chars:
                buffer = ""
            else:
                for event in parser.feed_line(buffer):
                    if emitted >= self._max_events:
                        self._result.truncated = True
                        break
                    emitted += 1
                    if event.type == "exec.completed":
                        self._result.completed = True
                    yield event
        # 解析器自己的残留(本流程下为空,保持语义完整)。
        for event in parser.close():
            if emitted >= self._max_events:
                self._result.truncated = True
                break
            emitted += 1
            if event.type == "exec.completed":
                self._result.completed = True
            yield event


__all__ = [
    "MCodeRunner",
    "MCodeRunnerError",
    "MCodeRunnerTimeout",
    "MCodeRunResult",
    "MCodeSchemaError",
]

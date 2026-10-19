"""方案 B(备选):每次 chat() 拉起一次 `claude -p` 或 `mcode exec`,当作「无状态大模型」。

什么时候用:
    - 没有人守着、任务又小,不想让 Agent 自己跑流程。
    - 想把原来的 LLM API 调用「原地」换成本机已登录的 Agent 程序。
代价:
    - 每次调用 = 一次完整的 Agent 会话:慢、贵、有启动开销。
    - temperature / top_p / max_tokens 传不给 CLI,会被忽略。
    - claude 用 `--tools ""` 去掉全部工具;mcode 用 `--mode lightweight`(请求里没有任何工具,0.6.9 沙箱实测)。
      mcode 的 lightweight 会忽略自定义系统提示词,所以适配器把系统提示词并进用户提示。

两个适配器都继承 Exception:仓库里 11 处 chat 调用外面的 `except Exception` 会接住并降级。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_gate.media import extract_frames, is_image
from agent_gate.validate import NotJSON, parse_json_value, validate_result


class CLIBackendError(Exception):
    """CLI 后端错误基类。"""


class CLIBackendTimeout(CLIBackendError):
    """CLI 进程超时(已杀掉整个进程树)。"""


class CLIBackendFailed(CLIBackendError):
    """CLI 非正常结束(退出码非 0、is_error、status 不是 succeeded)。"""


class CLIBackendInvalid(CLIBackendError):
    """答案多次都不合 JSON Schema。"""


class _BadAnswer(Exception):
    """内部用:这一次的答案不合格,可以带着提示再问一次。"""


def _kill_tree(p: "subprocess.Popen[bytes]") -> None:
    """杀掉进程和它的子进程。Windows 用 taskkill /T,其他系统杀进程组。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            p.kill()
        except OSError:
            pass


def run_process(cmd: list[str], stdin_text: str, *, cwd: str, env: dict[str, str],
                timeout_s: float, label: str = "cli") -> tuple[int, str, str]:
    """起子进程、写 stdin、等结果。超时就杀掉整棵进程树并抛 CLIBackendTimeout。"""
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=cwd, env=env, **kw)
    except FileNotFoundError as e:
        raise CLIBackendFailed(f"找不到可执行文件 {cmd[0]!r}:{e}") from e
    try:
        out, err = p.communicate(stdin_text.encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        _kill_tree(p)
        p.communicate()
        raise CLIBackendTimeout(f"{label} 超过 {timeout_s:.0f}s 未结束") from e
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "image/jpeg"


class _CLIClient:
    backend_name = "cli"
    env_bin = ""        # 子类:指定可执行文件的环境变量名
    default_bin = ""    # 子类:默认可执行文件名

    def __init__(
        self,
        *,
        executable: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        max_attempts: int = 2,
        frames_per_clip: int = 4,
        workdir: Optional[str] = None,
        extra_args: Sequence[str] = (),
        extra_env: Optional[dict[str, str]] = None,
        clean_env: bool = False,
    ) -> None:
        self.executable = executable or os.environ.get(self.env_bin) or self.default_bin
        self.model = model or os.environ.get("AGENT_GATE_CLI_MODEL") or None
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else os.environ.get("AGENT_GATE_CLI_TIMEOUT_S", "600"))
        self.max_attempts = max(1, max_attempts)
        self.frames_per_clip = frames_per_clip
        self.workdir = workdir
        self.extra_args = list(extra_args)
        self.extra_env = dict(extra_env or {})
        self.clean_env = clean_env   # True:子进程只继承 PATH 等最小环境,其余用 extra_env 显式给

    # ---- LLMClient 协议 ----
    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        media: Optional[list[dict[str, Any]]] = None,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: float = 0.3,   # CLI 不支持,忽略
        top_p: float = 0.9,         # CLI 不支持,忽略
        max_tokens: int = 4096,     # CLI 不支持,忽略
    ) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="agent_gate_cli_"))
        try:
            images = self._collect_images(media or [], tmp / "frames")
            hint = ""
            last = ""
            for _ in range(self.max_attempts):
                try:
                    out = self._invoke(system_prompt, user_prompt + hint, images, json_schema, tmp)
                    if not json_schema:
                        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                    value = parse_json_value(out) if isinstance(out, str) else out
                    errors = validate_result(json_schema, value)
                    if not errors:
                        return json.dumps(value, ensure_ascii=False)
                    last = "; ".join(errors)
                except NotJSON as e:
                    last = str(e)
                except _BadAnswer as e:
                    last = str(e)
                hint = f"\n\n【上一次的答案不合格:{last}。请只输出符合 JSON Schema 的 JSON。】"
            raise CLIBackendInvalid(f"{self.backend_name}:{self.max_attempts} 次答案都不合格:{last}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- 内部 ----
    def _collect_images(self, media: list[dict[str, Any]], frames_dir: Path) -> list[str]:
        images: list[str] = []
        for m in media:
            path = str(m.get("path") or "")
            if not path:
                continue
            if is_image(path):
                images.append(path)
                continue
            try:
                images += extract_frames(path, m.get("in_sec"), m.get("out_sec"),
                                         frames_dir, n=self.frames_per_clip)
            except Exception:  # noqa: BLE001 — 抽不出帧就当没有图,任务照样发
                pass
        return images

    def _env(self) -> dict[str, str]:
        if self.clean_env:
            keep = {"PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"}
            env = {k: v for k, v in os.environ.items() if k in keep}
        else:
            env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env.update(self.extra_env)
        return env

    def _run(self, cmd: list[str], stdin_text: str, cwd: Path) -> tuple[int, str, str]:
        return run_process(cmd, stdin_text, cwd=str(self.workdir or cwd), env=self._env(),
                           timeout_s=self.timeout_s, label=self.backend_name)

    def _invoke(self, system_prompt: str, prompt: str, images: list[str],
                json_schema: Optional[dict[str, Any]], tmp: Path) -> Any:
        raise NotImplementedError


class ClaudeCLIClient(_CLIClient):
    """`claude -p`:stdin 和 stdout 都用 stream-json(图片作为内容块直接塞进首条消息)。"""

    backend_name = "claude-cli"
    env_bin = "AGENT_GATE_CLAUDE_BIN"
    default_bin = "claude"

    def __init__(self, *, max_turns: int = 6, **kw: Any) -> None:
        super().__init__(**kw)
        self.max_turns = max_turns

    def build_command(self, system_prompt: str, json_schema: Optional[dict[str, Any]]) -> list[str]:
        cmd = [self.executable, "-p",
               "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--tools", "",                       # 纯问答:不给任何内置工具
               "--strict-mcp-config",               # 不加载任何 MCP 服务
               "--disable-slash-commands",
               "--no-session-persistence",
               "--permission-mode", "dontAsk",
               "--max-turns", str(self.max_turns)]
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]
        if json_schema:
            cmd += ["--json-schema", json.dumps(json_schema, ensure_ascii=False)]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args

    def _env(self) -> dict[str, str]:
        env = super()._env()
        env.setdefault("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "1")   # 不读 CLAUDE.md(沙箱实测有效)
        return env

    @staticmethod
    def build_stdin(prompt: str, images: list[str]) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for p in images:
            try:
                data = base64.b64encode(Path(p).read_bytes()).decode("ascii")
            except OSError:
                continue
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": _mime(p), "data": data}})
        return json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                          ensure_ascii=False) + "\n"

    @staticmethod
    def parse_result(stdout: str) -> dict[str, Any]:
        """stream-json 输出是逐行 JSON;最后一个 type=result 的就是结果。"""
        result: Optional[dict[str, Any]] = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result = obj
        if result is None:
            raise CLIBackendFailed("claude 输出里没有 result 事件")
        return result

    def _invoke(self, system_prompt: str, prompt: str, images: list[str],
                json_schema: Optional[dict[str, Any]], tmp: Path) -> Any:
        code, out, err = self._run(self.build_command(system_prompt, json_schema),
                                   self.build_stdin(prompt, images), tmp)
        res = self.parse_result(out)
        if res.get("is_error"):
            sub = res.get("subtype")
            msg = f"claude 失败:subtype={sub} terminal_reason={res.get('terminal_reason')} " \
                  f"errors={res.get('errors')} exit={code} stderr={err.strip()[:300]}"
            if sub == "error_max_structured_output_retries":
                raise CLIBackendInvalid(msg)   # claude 自己已经重试 5 次,不再叠加重试
            raise CLIBackendFailed(msg)
        if json_schema:
            so = res.get("structured_output")
            if so is not None:
                return so
            # 实测:模型只回纯文本 JSON 时 structured_output 为空,答案在 result 里
            return str(res.get("result") or "")
        return str(res.get("result") or "")


class McodeCLIClient(_CLIClient):
    """`mcode exec`:提示词走 stdin,schema 走文件(避开 Windows 引号问题)。

    lightweight=True(默认):`--mode lightweight`,请求里没有任何工具(硬隔离)。
        代价:mcode 会忽略 --system-prompt-file,所以系统提示词被并进用户提示。
    lightweight=False:标准模式,系统提示词走 --system-prompt-file,但模型带着 bash / 读写文件等内置工具。
    """

    backend_name = "mcode-cli"
    env_bin = "AGENT_GATE_MCODE_BIN"
    default_bin = "mcode"

    def __init__(self, *, permission: str = "smart", max_steps: int = 8, lightweight: bool = True,
                 **kw: Any) -> None:
        super().__init__(**kw)
        self.permission = permission
        self.max_steps = max_steps
        self.lightweight = lightweight

    def compose_prompt(self, system_prompt: str, prompt: str) -> str:
        if self.lightweight and system_prompt:
            return f"【角色与规则】\n{system_prompt}\n\n【任务】\n{prompt}"
        return prompt

    def build_command(self, tmp: Path, system_prompt: str, images: list[str],
                      json_schema: Optional[dict[str, Any]]) -> list[str]:
        cmd = [self.executable, "exec", "--input", "-", "--output-format", "json",
               "--permission", self.permission, "--max-steps", str(self.max_steps),
               "--timeout", f"{max(1, int(self.timeout_s))}s", "--cwd", str(tmp)]
        if self.lightweight:
            cmd += ["--mode", "lightweight"]
        elif system_prompt:
            sp = tmp / "system_prompt.txt"
            sp.write_text(system_prompt, encoding="utf-8")
            cmd += ["--system-prompt-file", str(sp)]
        if json_schema:
            sf = tmp / "output_schema.json"
            sf.write_text(json.dumps(json_schema, ensure_ascii=False), encoding="utf-8")
            cmd += ["--output-schema", str(sf)]
        for p in images:
            cmd += ["--file", p]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args

    def _invoke(self, system_prompt: str, prompt: str, images: list[str],
                json_schema: Optional[dict[str, Any]], tmp: Path) -> Any:
        code, out, err = self._run(self.build_command(tmp, system_prompt, images, json_schema),
                                   self.compose_prompt(system_prompt, prompt), tmp)
        try:
            res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except (json.JSONDecodeError, IndexError):
            res = {}
        status = res.get("status")
        if status == "succeeded":
            return res.get("output")
        error = res.get("error") or {}
        if error.get("code") == "STRUCTURED_OUTPUT_INVALID":
            raise _BadAnswer(str(error.get("message") or "结构化输出不合格"))
        raise CLIBackendFailed(f"mcode 失败:exit={code} status={status} error={error or None} "
                               f"stderr={err.strip()[:300]}")

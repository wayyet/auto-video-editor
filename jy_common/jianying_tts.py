"""剪映 SAMI TTS 客户端(TTS_BACKEND=jianying)。

2026-11-14:FireRed TTS(:8010)本机从未部署,中文/英文配音全灭导致交付
QA 阻断(console-50cd4c6af941)。本模块改走**剪映「朗读」面板同款引擎**:
字节 SAMI 云端 TTS,WebSocket 直连,无需登录凭据 ——

- 端点 ``wss://sami.bytedance.com/internal/api/v2/ws?device_id=..&iid=..``;
  device_id/iid 读本机剪映用户数据,读不到用参考实现的兜底常量;
- UA 伪装剪映客户端 ``JianyingPro/5.9.0.11632``(本机便携版版本);
- 音色 speaker 与剪映朗读面板「热门」列表一致
  (``jy_common/jianying_tts_speakers.csv``,来源 isYangs/jianying-editor-skill
  MIT);中文名(如 ``清亮男声``)与 speaker_id 均可作 ``voice`` 入参;
- SAMI 返回 ogg_opus → ffmpeg 转 24kHz mono PCM WAV:现有链路
  (``read_wav_duration_ms`` / node_08 ``_wav_usable`` / node_13 /
  ``draft_integrity``)全按 WAV 头判时长,不转则整链断裂;
- speed≠1 用 ffmpeg atempo 链(单级限 (0.5, 2.0],越界拆多级)在转码时
  一并完成。

协议与兜底常量参考 isYangs/jianying-editor-skill(MIT)
``scripts/universal_tts.py``。失败语义与 ``jy_common.tts_client`` 对齐:
一切异常归一化为 :class:`TTSUnavailable`(懒 import,避免与 tts_client
模块级单例解析互相初始化)。

本模块仅依赖 stdlib + ``websockets``(懒 import);不 import 本仓库其他
模块 —— ``jy_common`` 层纪律(:doc:``nodes/storyline/_tts_adapter``)。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import selectors
import shutil
import ssl
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Optional

#: provider 名(voiceover.json / TTSResult.provider 用;非 stub,draft_integrity 视为真实配音)
PROVIDER = "jianying"

# ---------------------------------------------------------------------------
# SAMI 协议常量(参考实现 isYangs/jianying-editor-skill universal_tts.py)
# ---------------------------------------------------------------------------
SAMI_APP_ID = "3704"
SAMI_APP_KEY = "IZjhUeAYwP"
DEFAULT_WS_URL = "wss://sami.bytedance.com/internal/api/v2/ws"
#: UA 里的剪映版本 = 本机便携版版本(kuaishou/JianyingPro/5.9.0.11632)
_SAMI_UA_VERSION = "5.9.0.11632"
_SAMI_AUDIO_CONFIG: dict[str, Any] = {
    "format": "ogg_opus",
    "sample_rate": 24000,
    "bit_rate": 64000,
}

# device_id/iid 兜底常量(参考实现;本机剪映未装/配置缺失时仍可用)
_FALLBACK_DEVICE_ID = "1053764930506284"
_FALLBACK_IID = "2314914062247833"

# 默认音色(2026-11-14 操作员截图:中文=清亮男声;表内唯一英文音色=英语女王;
# env TTS_VOICE_ZH / TTS_VOICE_EN 可覆盖,值可以是中文名或 speaker_id)
DEFAULT_SPEAKERS: dict[str, str] = {"zh": "清亮男声", "en": "英语女王"}

# ---------------------------------------------------------------------------
# 音色表(jy_common/jianying_tts_speakers.csv)
# ---------------------------------------------------------------------------
_CSV_PATH = Path(__file__).resolve().parent / "jianying_tts_speakers.csv"
_SPEAKERS_BY_ID: Optional[dict[str, str]] = None
_SPEAKERS_BY_NAME: Optional[dict[str, str]] = None


def _load_speakers() -> tuple[dict[str, str], dict[str, str]]:
    """读 CSV → ``(id→显示名, 显示名→id)``;文件缺失/损坏返回空表
    (解析时全走默认音色,不抛错 —— 音色表是锦上添花,不阻断配音)。"""
    global _SPEAKERS_BY_ID, _SPEAKERS_BY_NAME
    if _SPEAKERS_BY_ID is None or _SPEAKERS_BY_NAME is None:
        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        try:
            lines = _CSV_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            speaker_id, name = parts[0], parts[1]
            by_id.setdefault(speaker_id, name)
            by_name.setdefault(name, speaker_id)
        _SPEAKERS_BY_ID, _SPEAKERS_BY_NAME = by_id, by_name
    return _SPEAKERS_BY_ID, _SPEAKERS_BY_NAME


def resolve_speaker(voice: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``voice``(中文名或 speaker_id)→ ``(speaker_id, 显示名)``。

    ``None``/空串/解析不了 → ``(None, None)``,由调用方决定默认音色
    (客户端缺省回落 :func:`default_speaker`,不因音色名写错而杀掉配音)。
    """
    raw = str(voice or "").strip()
    if not raw:
        return None, None
    by_id, by_name = _load_speakers()
    if raw in by_id:
        return raw, by_id[raw]
    if raw in by_name:
        return by_name[raw], raw
    return None, None


def default_speaker(lang: str = "zh") -> str:
    """按语言取默认音色;env ``TTS_VOICE_ZH`` / ``TTS_VOICE_EN`` 覆盖。"""
    key = "en" if str(lang or "").lower().startswith("en") else "zh"
    env_name = "TTS_VOICE_EN" if key == "en" else "TTS_VOICE_ZH"
    return os.environ.get(env_name, "").strip() or DEFAULT_SPEAKERS[key]


# ---------------------------------------------------------------------------
# 本机剪映身份(device_id / iid)
# ---------------------------------------------------------------------------
def _jy_user_data_dir() -> Optional[Path]:
    """剪映用户数据目录;env ``JY_USER_DATA_DIR`` 优先(测试注入位)。"""
    raw = os.environ.get("JY_USER_DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None
    return Path(local) / "JianyingPro" / "User Data"


def read_device_identity(user_data_dir: Optional[Path] = None) -> tuple[str, str]:
    """读本机剪映的 ``(device_id, iid)``(SAMI ws 鉴权参数)。

    - device_id:``TTNet/tt_net_config.config`` 里 ``device_id&#...数字``
      (TTNet 用 ``&#*`` 做键值分隔转义,数字前可能隔着字面 ``*``;实测本机
      形态 ``device_id&#*1941114026465464``,参考实现的 ``&#*(\\d+)`` 匹配
      不到这种,这里放宽为 ``&#[#\\*]*(\\d+)``);
    - iid:最新 5 个 ``Log/*.log`` 前 1MB 里 ``iid=(\\d+)``;
    - env ``SAMI_TTS_DEVICE_ID`` / ``SAMI_TTS_IID`` 直接覆盖(测试/运维);
    - 都读不到 → 参考实现兜底常量(剪映未装也不报错)。
    """
    dev = os.environ.get("SAMI_TTS_DEVICE_ID", "").strip()
    iid = os.environ.get("SAMI_TTS_IID", "").strip()
    base = user_data_dir if user_data_dir is not None else _jy_user_data_dir()
    if base is not None:
        if not dev:
            cfg = base / "TTNet" / "tt_net_config.config"
            try:
                m = re.search(
                    r"device_id&#[#\*]*(\d+)",
                    cfg.read_text(encoding="utf-8", errors="ignore"),
                )
                if m:
                    dev = m.group(1)
            except OSError:
                pass
        if not iid:
            log_dir = base / "Log"
            try:
                logs = sorted(
                    (p for p in log_dir.iterdir() if p.suffix == ".log"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:5]
            except OSError:
                logs = []
            for p in logs:
                try:
                    chunk = p.read_text(encoding="utf-8", errors="ignore")[:1_000_000]
                except OSError:
                    continue
                m = re.search(r"iid=(\d+)", chunk)
                if m:
                    iid = m.group(1)
                    break
    return dev or _FALLBACK_DEVICE_ID, iid or _FALLBACK_IID


# ---------------------------------------------------------------------------
# ffmpeg:ogg→wav 转码 + atempo 变速
# ---------------------------------------------------------------------------
def _fmt_speed(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def build_atempo_filter(speed: float) -> str:
    """speed → ffmpeg atempo 链(单级限 (0.5, 2.0],越界拆多级)。"""
    speed = float(speed)
    if speed <= 0:
        raise ValueError("speed must be greater than zero")
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-9 or not factors:
        factors.append(remaining)
    return ",".join(f"atempo={_fmt_speed(f)}" for f in factors)


def _run_ffmpeg(cmd: list[str]) -> None:
    """默认 ffmpeg 执行器(PATH 定位;``-nostdin`` 防 MCP stdio 挂起)。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH")
    proc = subprocess.run(
        [exe, "-nostdin", "-v", "error", "-y", *cmd],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown ffmpeg error").strip()
        raise RuntimeError(f"ffmpeg 转码失败: {detail[:300]}")


def _ogg_to_wav(
    ogg_path: Path,
    wav_path: Path,
    *,
    speed: Optional[float],
    runner: Callable[[list[str]], None],
) -> None:
    """SAMI ogg_opus → 24kHz mono PCM WAV(speed≠1 时叠加 atempo 链)。"""
    cmd = ["-i", str(ogg_path)]
    if speed is not None and abs(float(speed) - 1.0) > 1e-9:
        cmd += ["-filter:a", build_atempo_filter(float(speed))]
    cmd += ["-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)]
    runner(cmd)


def read_wav_duration_ms(path: str | Path) -> int:
    """WAV 头算时长(毫秒);打不开/非 PCM 返回 0(与 tts_runner 同口径)。"""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0
            return int(round(frames / rate * 1000))
    except (OSError, wave.Error, EOFError):
        return 0


# ---------------------------------------------------------------------------
# SAMI 协议消息
# ---------------------------------------------------------------------------
def build_start_task(text: str, speaker: str, task_id: str) -> str:
    """SAMI ``StartTask`` 消息(紧凑分隔符,照抄参考实现口径)。"""
    payload = json.dumps(
        {"text": text, "speaker": speaker, "audio_config": dict(_SAMI_AUDIO_CONFIG)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return json.dumps(
        {
            "app_id": SAMI_APP_ID,
            "appkey": SAMI_APP_KEY,
            "event": "StartTask",
            "namespace": "TTS",
            "task_id": task_id,
            "message_id": task_id + "_0",
            "payload": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


_FINISH_TASK = json.dumps(
    {"appkey": SAMI_APP_KEY, "event": "FinishTask", "namespace": "TTS"},
    ensure_ascii=False,
    separators=(",", ":"),
)


def _build_ssl_context() -> ssl.SSLContext:
    """默认校验;``SAMI_TTS_INSECURE_SSL=1`` 时关闭(企业代理/抓包排障用)。"""
    if os.environ.get("SAMI_TTS_INSECURE_SSL", "").strip().lower() == "1":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
def _tts_unavailable(message: str) -> Exception:
    """构造 :class:`jy_common.tts_client.TTSUnavailable`(懒 import 防环)。"""
    from jy_common.tts_client import TTSUnavailable

    return TTSUnavailable(message)


def _coerce_speed(speed: Any) -> Optional[float]:
    """speed 入参防御:bool/非数/非正数 → None(不改速)。"""
    if speed is None or isinstance(speed, bool):
        return None
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None
    return value


class JianYingSamiClient:
    """剪映 SAMI TTS 客户端(``jy_common.tts_client`` TTSClient 协议的真实后端)。

    Args:
        ws_url: 覆盖 SAMI ws 端点(env ``SAMI_TTS_WS_URL`` / 测试用本地 fake)。
        ws_factory: 异步 ``factory(url, additional_headers=..., ssl=...,
            open_timeout=...)`` → 支持 ``async with`` 的连接对象(测试注入位;
            默认 ``websockets.connect``)。
        ffmpeg_runner: ``runner(cmd_without_exe)``(测试注入位;默认真 ffmpeg)。
        retries: SAMI 连接/合成重试次数(默认 2)。
        device_id / iid: 显式身份;缺省在首次 synthesize 时读本机剪映。
    """

    def __init__(
        self,
        *,
        ws_url: Optional[str] = None,
        ws_factory: Optional[Any] = None,
        ffmpeg_runner: Optional[Callable[[list[str]], None]] = None,
        retries: int = 2,
        connect_timeout_s: float = 20.0,
        recv_timeout_s: float = 15.0,
        device_id: Optional[str] = None,
        iid: Optional[str] = None,
    ) -> None:
        self._ws_url = (
            ws_url or os.environ.get("SAMI_TTS_WS_URL", "") or DEFAULT_WS_URL
        ).strip()
        self._ws_factory = ws_factory
        self._ffmpeg_runner = ffmpeg_runner or _run_ffmpeg
        self._retries = max(1, int(retries))
        self._connect_timeout = float(connect_timeout_s)
        self._recv_timeout = float(recv_timeout_s)
        self._explicit_identity: Optional[tuple[str, str]] = None
        if device_id or iid:
            self._explicit_identity = (
                device_id or _FALLBACK_DEVICE_ID,
                iid or _FALLBACK_IID,
            )
        self._identity: Optional[tuple[str, str]] = None

    def _ensure_identity(self) -> tuple[str, str]:
        """身份惰性解析(模块导入/构造期不做文件 IO)。"""
        if self._identity is None:
            self._identity = self._explicit_identity or read_device_identity()
        return self._identity

    # -- 对外入口(jy_common TTSClient 协议)------------------------------
    def synthesize(
        self, text: str, *, voice: Optional[str] = None, speed: Optional[float] = None
    ) -> dict:
        """合成 ``text`` → WAV,返回 ``{audio_path, duration_ms, voice, ...}``。

        voice 可以是中文名(如 ``清亮男声``)或 speaker_id;``None``/未知
        回落默认音色(中文链路默认清亮男声;英文调用方——node_17——必须
        显式传英文音色)。失败 raise ``TTSUnavailable``。

        Raises:
            TTSUnavailable: 空文本、SAMI 失败、转码失败或时长解析为 0。
        """
        if not text or not str(text).strip():
            raise _tts_unavailable("text is empty")
        text = str(text).strip()

        requested = str(voice or "").strip() or None
        speaker_id, display = resolve_speaker(requested)
        if speaker_id is None:
            # 未知音色不杀配音:回落中文默认音色,请求值记入结果便于排查
            speaker_id, display = resolve_speaker(default_speaker("zh"))
        speed_val = _coerce_speed(speed)

        ogg_path = Path(tempfile.gettempdir()) / f"jy_sami_{uuid.uuid4().hex[:12]}.ogg"
        wav_path = ogg_path.with_suffix(".wav")

        last_err = ""
        for _attempt in range(self._retries):
            ok, err = self._run_once(text, speaker_id, ogg_path)
            if ok:
                break
            last_err = err
        else:
            raise _tts_unavailable(
                f"剪映 SAMI TTS 合成失败(重试 {self._retries} 次): {last_err}"
            )

        try:
            _ogg_to_wav(
                ogg_path, wav_path, speed=speed_val, runner=self._ffmpeg_runner
            )
        except (RuntimeError, OSError, ValueError) as exc:
            raise _tts_unavailable(f"SAMI 音频转码失败(ogg→wav): {exc}") from exc

        duration_ms = read_wav_duration_ms(wav_path)
        if duration_ms <= 0:
            raise _tts_unavailable("SAMI 转码后 WAV 时长解析为 0(文件损坏?)")

        result: dict[str, Any] = {
            "audio_path": str(wav_path),
            "duration_ms": duration_ms,
            "voice": display,
            "speaker": speaker_id,
            "provider": PROVIDER,
            "speed": speed_val,
        }
        if requested and requested != display:
            result["voice_requested"] = requested
        return result

    # -- SAMI 会话 --------------------------------------------------------
    def _run_once(self, text: str, speaker: str, ogg_path: Path) -> tuple[bool, str]:
        """单次 SAMI 会话(Selector 循环,Windows Proactor 规避)。"""
        try:
            loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        except Exception as exc:  # noqa: BLE001 — 平台不支持时归一化
            return False, f"event loop: {exc!r}"
        try:
            return loop.run_until_complete(
                self._run_sami_async(text, speaker, ogg_path)
            )
        except Exception as exc:  # noqa: BLE001 — 网络/协议一切异常归一化
            return False, repr(exc)
        finally:
            loop.close()

    async def _run_sami_async(
        self, text: str, speaker: str, ogg_path: Path
    ) -> tuple[bool, str]:
        """SAMI ws 会话:StartTask → FinishTask → 收帧至 TaskFinished。"""
        import websockets

        device_id, iid = self._ensure_identity()
        url = f"{self._ws_url}?device_id={device_id}&iid={iid}"
        headers = {
            "User-Agent": (
                f"JianyingPro/{_SAMI_UA_VERSION} (Windows 10.0.19045; "
                f"app_id:{SAMI_APP_ID}; device_id:{device_id})"
            )
        }
        factory = self._ws_factory or websockets.connect
        task_id = f"ai_gen_{os.urandom(4).hex()}"
        audio = bytearray()

        async with factory(
            url,
            additional_headers=headers,
            ssl=_build_ssl_context(),
            open_timeout=self._connect_timeout,
        ) as ws:
            await ws.send(build_start_task(text, speaker, task_id))
            await ws.send(_FINISH_TASK)
            while True:
                try:
                    frame = await asyncio.wait_for(
                        ws.recv(), timeout=self._recv_timeout
                    )
                except asyncio.TimeoutError:
                    return False, "SAMI 收帧超时"
                if isinstance(frame, str):
                    try:
                        resp = json.loads(frame)
                    except ValueError:
                        continue  # 非 JSON 文本帧:忽略,继续等二进制/事件
                    event = str(resp.get("event") or "")
                    if event == "TaskFailed":
                        return (
                            False,
                            f"SAMI Error: {resp.get('status_text')} "
                            f"(Code: {resp.get('status_code')})",
                        )
                    if event == "TaskFinished":
                        break
                else:
                    audio.extend(frame)

        if not audio:
            return False, "SAMI 无音频数据"
        ogg_path.parent.mkdir(parents=True, exist_ok=True)
        ogg_path.write_bytes(bytes(audio))
        return True, str(ogg_path)


__all__ = [
    "DEFAULT_SPEAKERS",
    "DEFAULT_WS_URL",
    "JianYingSamiClient",
    "PROVIDER",
    "SAMI_APP_ID",
    "SAMI_APP_KEY",
    "build_atempo_filter",
    "build_start_task",
    "default_speaker",
    "read_device_identity",
    "read_wav_duration_ms",
    "resolve_speaker",
]

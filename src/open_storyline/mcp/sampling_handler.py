import asyncio
import os
import math
import re
import shutil
import subprocess
import base64
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PIL import Image
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from mcp.types import CreateMessageRequestParams, CreateMessageResult, TextContent


# -----------------------------
# Configurable parameters: Control multimodal input size
# -----------------------------
DEFAULT_RESIZE_EDGE = 600
DEFAULT_JPEG_QUALITY = 80
DEFAULT_MIN_FRAMES = 2
DEFAULT_MAX_FRAMES = 6
DEFAULT_FRAMES_PER_SEC = 3.0
GLOBAL_MAX_IMAGE_BLOCKS = 48  # Maximum total images allowed (video frames + images) to prevent payload overflow

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _is_data_url(u: str) -> bool:
    return isinstance(u, str) and u.startswith("data:")


def _is_http_url(u: str) -> bool:
    return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://"))


def _strip_file_scheme(u: str) -> str:
    if not isinstance(u, str):
        return str(u)
    if u.startswith("file://"):
        parsed = urlparse(u)
        return parsed.path
    return u


def _guess_ext(path_or_url: str) -> str:
    try:
        p = urlparse(path_or_url).path if _is_http_url(path_or_url) else path_or_url
        return os.path.splitext(p)[1].lower()
    except Exception:
        return ""


def _resize_long_edge(img: Image.Image, long_edge: int) -> Image.Image:
    if long_edge <= 0:
        return img
    w, h = img.size
    le = max(w, h)
    if le <= long_edge:
        return img
    scale = long_edge / float(le)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return img.resize((nw, nh), Image.LANCZOS)


def _pil_to_data_url(img: Image.Image, resize_edge: int, jpeg_quality: int) -> str:
    img = img.convert("RGB")
    img = _resize_long_edge(img, resize_edge)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def _image_path_to_data_url(path: str, resize_edge: int, jpeg_quality: int) -> str:
    img = Image.open(path)
    return _pil_to_data_url(img, resize_edge, jpeg_quality)


# -----------------------------
# ffmpeg 直抽帧：不再经过 moviepy
# moviepy 2.1.2 的 ffmpeg_reader 解析 iPhone HEVC 素材的
# Ambient Viewing Environment 元数据时会 TypeError（float + str），
# VideoFileClip 一打开文件就崩，故改为直接调用 ffmpeg 子进程抽帧。
# -----------------------------
_FFMPEG_EXE: Optional[str] = None
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _find_ffmpeg() -> str:
    """优先用 PATH 里的 ffmpeg；找不到时退回 imageio-ffmpeg 自带的二进制（moviepy 的依赖，环境里必定存在）"""
    global _FFMPEG_EXE
    if _FFMPEG_EXE:
        return _FFMPEG_EXE
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = None
    if not exe:
        raise RuntimeError("ffmpeg executable not found: install ffmpeg or `pip install imageio-ffmpeg`")
    _FFMPEG_EXE = exe
    return exe


def _probe_video_duration(video_path: str) -> float:
    """从 `ffmpeg -i` 的 stderr 里解析视频时长（秒），不依赖 ffprobe；解析失败返回 0.0"""
    proc = subprocess.run(
        [_find_ffmpeg(), "-hide_banner", "-i", video_path],
        capture_output=True, text=True, errors="replace",
    )
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return 0.0
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def _extract_frame_jpeg(video_path: str, t_sec: float) -> bytes:
    """用 ffmpeg 在 t_sec 处抽一帧，以 JPEG 字节流从 stdout 返回（自动应用旋转元数据）"""
    cmd = [
        _find_ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, t_sec):.3f}", "-i", video_path,
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError(f"ffmpeg frame extraction failed at t={t_sec:.3f}s: {err}")
    return proc.stdout


def _choose_num_frames(duration_sec: float, min_frames: int, max_frames: int, frames_per_sec: float) -> int:
    duration_sec = max(0.0, float(duration_sec))
    n = int(math.ceil(duration_sec * frames_per_sec))
    n = max(min_frames, n)
    n = min(max_frames, n)
    return n


def _sample_video_segment_to_data_urls(
    video_path: str,
    in_sec: float,
    out_sec: float,
    resize_edge: int,
    jpeg_quality: int,
    min_frames: int,
    max_frames: int,
    frames_per_sec: float,
) -> List[Tuple[float, str]]:
    """
    Sample frames only from the [in_sec, out_sec] segment. Returns (rel_t_from_in, data_url)
    抽帧走 ffmpeg 子进程直抽，不再使用 moviepy（其元数据解析器对 iPhone HEVC 素材会崩溃）
    """

    in_sec = float(in_sec)
    out_sec = float(out_sec)

    def _frame_to_data_url(t_abs: float) -> str:
        img = Image.open(BytesIO(_extract_frame_jpeg(video_path, t_abs)))
        return _pil_to_data_url(img, resize_edge, jpeg_quality)

    vdur = _probe_video_duration(video_path)

    # If duration is unavailable, conservatively sample one frame to avoid out_sec exceeding bounds
    if vdur <= 0:
        return [(0.0, _frame_to_data_url(max(0.0, in_sec)))]

    # Clamp to valid range（末尾留 0.05s 余量，避免 seek 越过最后一帧导致抽不到帧）
    in_sec = max(0.0, min(in_sec, max(0.0, vdur - 0.05)))
    out_sec = max(0.0, min(out_sec, vdur))

    # If still invalid, fallback to one frame at in_sec
    if out_sec <= in_sec:
        return [(0.0, _frame_to_data_url(in_sec))]

    seg_dur = out_sec - in_sec
    n = _choose_num_frames(seg_dur, min_frames, max_frames, frames_per_sec)

    # Sample at bucket centers to avoid boundary frames
    times = [((i + 0.5) / n) * seg_dur for i in range(n)]

    out: List[Tuple[float, str]] = []
    for rel_t in times:
        out.append((rel_t, _frame_to_data_url(in_sec + rel_t)))
    return out


def _extract_text_from_mcp_content(content: Any) -> str:
    if content is None:
        return ""
    blocks = content if isinstance(content, list) else [content]
    texts: List[str] = []
    for b in blocks:
        if getattr(b, "type", None) == "text":
            texts.append(getattr(b, "text", "") or "")
    return "\n".join([t for t in (x.strip() for x in texts) if t])


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", flags=re.IGNORECASE | re.DOTALL)


def _strip_think_blocks(text: str) -> str:
    """剥离推理模型（如 MiniMax-M3）混在正文里的 <think>...</think> 思考段。
    未闭合的 <think>（思考被 max_tokens 截断）之后的内容全部视为思考、一并剥离，
    让节点拿到空串并走"未生成内容"的重试/报错分支，而不是把思考文本误当答案解析。"""
    if not text or "<think" not in text.lower():
        return text
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


def _extract_text_from_lc_response(resp: Any) -> str:
    content = getattr(resp, "content", None)
    if isinstance(content, str):
        return _strip_think_blocks(content.strip())
    if isinstance(content, list):
        texts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                texts.append(str(blk.get("text", "")).strip())
        return _strip_think_blocks("\n".join([t for t in texts if t]).strip())
    return _strip_think_blocks(str(resp).strip())


def _normalize_media_items(media_inputs: List[Any]) -> List[Dict[str, Any]]:
    """
    Supports three input formats:
      1) "path/to/video.mp4"
      2) {"url"/"path": "...", "in_sec": 1.2, "out_sec": 3.4}
      3) ("path/to/video.mp4", 1.2, 3.4)  # optional
    Output normalized to: {"url": "...", "in_sec": optional, "out_sec": optional}
    """
    out = []
    for item in media_inputs or []:
        if isinstance(item, str):
            out.append({"url": item})
            continue

        if isinstance(item, (list, tuple)) and len(item) >= 1:
            d = {"url": item[0]}
            if len(item) >= 2:
                d["in_sec"] = item[1]
            if len(item) >= 3:
                d["out_sec"] = item[2]
            out.append(d)
            continue

        if isinstance(item, dict):
            url = item.get("url") or item.get("path") or item.get("media")
            if not url:
                continue
            d = {"url": url}
            if "in_sec" in item:
                d["in_sec"] = item.get("in_sec")
            if "out_sec" in item:
                d["out_sec"] = item.get("out_sec")
            out.append(d)
            continue

    return out


def _build_media_blocks(
    media_inputs: List[Any],
    resize_edge: int,
    jpeg_quality: int,
    min_frames: int,
    max_frames: int,
    frames_per_sec: float,
    global_max_images: int,
) -> List[Dict[str, Any]]:
    """
    Convert media_inputs to OpenAI-compatible multimodal blocks.
    Videos: Sample frames by segment (if in_sec/out_sec provided) or entire video (legacy format)
    """

    blocks: List[Dict[str, Any]] = []
    img_count = 0

    items = _normalize_media_items(media_inputs)

    for idx, mi in enumerate(items):
        if img_count >= global_max_images:
            break

        raw_url = _strip_file_scheme(str(mi.get("url")))
        ext = _guess_ext(raw_url)

        in_sec = mi.get("in_sec")
        out_sec = mi.get("out_sec")
        has_segment = (in_sec is not None and out_sec is not None)

        # 1) Data URL (image) - pass through directly
        if _is_data_url(raw_url):
            blocks.append({"type": "text", "text": f"Media {idx+1}: (data url image)"})
            blocks.append({"type": "image_url", "image_url": {"url": raw_url}})
            img_count += 1
            continue

        # 2) Remote URL: Images can be passed through; remote videos cannot be sampled locally (provide notice)
        if _is_http_url(raw_url):
            if ext in VIDEO_EXTS:
                seg_info = f" segment [{in_sec},{out_sec}]s" if has_segment else ""
                blocks.append({"type": "text", "text": f"Media {idx+1}: remote video url{seg_info} (cannot sample frames locally): {raw_url}"})
                continue
            blocks.append({"type": "text", "text": f"Media {idx+1}: {raw_url}"})
            blocks.append({"type": "image_url", "image_url": {"url": raw_url}})
            img_count += 1
            continue

        # 3) Local path
        path = raw_url
        if not os.path.exists(path):
            blocks.append({"type": "text", "text": f"Media {idx+1}: (missing file) {path}"})
            continue

        # Image
        if ext in IMAGE_EXTS:
            data_url = _image_path_to_data_url(path, resize_edge, jpeg_quality)
            blocks.append({"type": "text", "text": f"Media {idx+1}: image file {os.path.basename(path)}"})
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
            img_count += 1
            continue

        # Video (supports segmented sampling)
        if ext in VIDEO_EXTS:
            if has_segment:
                in_s = float(in_sec)
                out_s = float(out_sec)
            else:
                # Legacy format: entire video. Use [0, +inf], internally clamped to duration
                in_s = 0.0
                out_s = 1e12

            frames = _sample_video_segment_to_data_urls(
                video_path=path,
                in_sec=in_s,
                out_sec=out_s,
                resize_edge=resize_edge,
                jpeg_quality=jpeg_quality,
                min_frames=min_frames,
                max_frames=max_frames,
                frames_per_sec=frames_per_sec,
            )

            if has_segment:
                blocks.append({"type": "text", "text": f"Media {idx+1}: video segment {os.path.basename(path)} [{in_s:.2f}s, {out_s:.2f}s] (sampled frames in time order)"})
            else:
                blocks.append({"type": "text", "text": f"Media {idx+1}: video file {os.path.basename(path)} (sampled frames in time order)"})

            for fi, (rel_t, data_url) in enumerate(frames):
                if img_count >= global_max_images:
                    break
                blocks.append({"type": "text", "text": f"Frame {fi+1}/{len(frames)} (t≈{rel_t:.2f}s from segment start)"})
                blocks.append({"type": "image_url", "image_url": {"url": data_url}})
                img_count += 1
            continue

        blocks.append({"type": "text", "text": f"Media {idx+1}: unsupported file type: {path}"})

    return blocks


def make_sampling_callback(
    llm,
    vlm,
    *,
    resize_edge: int = DEFAULT_RESIZE_EDGE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    min_frames: int = DEFAULT_MIN_FRAMES,
    max_frames: int = DEFAULT_MAX_FRAMES,
    frames_per_sec: float = DEFAULT_FRAMES_PER_SEC,
    global_max_images: int = GLOBAL_MAX_IMAGE_BLOCKS,
):
    """
    Callback for MCP server sampling requests within tools:
    - Reads metadata.media_urls (supports in_sec/out_sec)
    - Samples frames and constructs LangChain multimodal messages
    - Selects llm/vlm based on presence of media input
    """

    async def sampling_callback(context, params: CreateMessageRequestParams) -> CreateMessageResult:
        # 必须在 try 之前初始化：except 分支也会引用它。
        # 原实现只在 try 块后半段赋值，异常发生在赋值前时 except 自己再抛
        # UnboundLocalError，把真实错误吞掉。
        model_name: str = "unknown"
        try:
            # 1. System prompt
            system_prompt = getattr(params, "systemPrompt", None) or ""

            # 2. MCP messages (multi-turn) -> LangChain
            mcp_messages = getattr(params, "messages", []) or []
            lc_messages: List[Any] = []
            if system_prompt:
                lc_messages.append(SystemMessage(content=system_prompt))

            # 3. Metadata: Extract media_urls and top_p
            metadata = getattr(params, "metadata", None) or {}
            media_inputs = list(metadata.get("media", []) or [])
            top_p: float = float(metadata.get("top_p", 0.9))

            temperature: float = float(getattr(params, "temperature", None) or 0.6)
            max_tokens: int = int(getattr(params, "maxTokens", 4096) or 4096)

            # 4. Route to appropriate model
            use_multimodal = bool(media_inputs)
            model = vlm if use_multimodal else llm
            if model is None:
                model = vlm or llm
            # 选定模型后立刻取名，后续任何一步（抽帧/调用）失败时错误信息里都能带上真实模型名
            model_name = getattr(model, "model", None) or getattr(model, "model_name", None) or "unknown"

            # 5. Build media blocks (including video segment sampling) - run in thread to avoid blocking event loop
            media_blocks: List[Dict[str, Any]] = []
            if use_multimodal:
                media_blocks = await asyncio.to_thread(
                    _build_media_blocks,
                    media_inputs,
                    resize_edge,
                    jpeg_quality,
                    min_frames,
                    max_frames,
                    frames_per_sec,
                    global_max_images,
                )

            # 6. Attach media to "last user message"
            user_indices = [i for i, m in enumerate(mcp_messages) if getattr(m, "role", "") == "user"]
            last_user_idx = user_indices[-1] if user_indices else None

            if not mcp_messages:
                # No messages - create a user message
                content_blocks = [{"type": "text", "text": ""}]
                if media_blocks:
                    content_blocks.extend(media_blocks)
                lc_messages.append(HumanMessage(content=content_blocks if media_blocks else ""))
            else:
                for i, m in enumerate(mcp_messages):
                    role = getattr(m, "role", "") or "user"
                    text = _extract_text_from_mcp_content(getattr(m, "content", None))

                    if role == "assistant":
                        lc_messages.append(AIMessage(content=text))
                        continue

                    if role == "user":
                        if last_user_idx is not None and i == last_user_idx and media_blocks:
                            content_blocks = [{"type": "text", "text": text}]
                            content_blocks.extend(media_blocks)
                            lc_messages.append(HumanMessage(content=content_blocks))
                        else:
                            lc_messages.append(HumanMessage(content=text))
                        continue

                    lc_messages.append(HumanMessage(content=text))

            # 7. Invoke selected model
            bound = model
            try:
                bound = bound.bind(temperature=temperature, max_tokens=max_tokens, top_p=top_p)
            except Exception:
                bound = bound.bind(temperature=temperature, max_tokens=max_tokens)

            try:
                if hasattr(bound, "ainvoke"):
                    resp = await bound.ainvoke(lc_messages)
                else:
                    resp = await asyncio.to_thread(bound.invoke, lc_messages)
            except TypeError:
                # Edge case: some wrappers don't accept max_tokens/top_p
                bound2 = model.bind(temperature=temperature)
                if hasattr(bound2, "ainvoke"):
                    resp = await bound2.ainvoke(lc_messages)
                else:
                    resp = await asyncio.to_thread(bound2.invoke, lc_messages)

            text_out = _extract_text_from_lc_response(resp)

            # 识别"输出被截断"：推理模型（如 MiniMax-M3）的 <think> 思考段计入 max_tokens，
            # 预算耗尽时 finish_reason 为 "length"。上游据 stopReason=maxTokens 可明确报
            # "输出被截断"而不是笼统的"解析失败"。
            finish_reason = None
            try:
                meta = getattr(resp, "response_metadata", None) or {}
                finish_reason = meta.get("finish_reason") or meta.get("stop_reason")
            except Exception:
                finish_reason = None
            # 兜底启发式：原始输出里有 <think> 但剥完思考段正文为空 → 思考中途被截断
            raw_content = getattr(resp, "content", None)
            truncated_think = (
                isinstance(raw_content, str)
                and "<think" in raw_content.lower()
                and not text_out
            )
            stop_reason = "maxTokens" if (finish_reason == "length" or truncated_think) else "endTurn"

            return CreateMessageResult(
                content=TextContent(type="text", text=text_out),
                model=str(model_name),
                role="assistant",
                stopReason=stop_reason,
            )
        except Exception as e:
            return CreateMessageResult(
                content=TextContent(type="text", text=f"{type(e)}: {e}"),
                model=str(model_name),
                role="assistant",
                stopReason="error",
            )

    return sampling_callback

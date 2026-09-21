"""BGM 选择启发式 + 标签匹配(plan_v4 §5 阶段 4 / §2.3)。

vendored ``SelectBGMNode`` 用 FAISS-like 向量召回(``StorylineRecall``),
主 venv 不能装 torch;本地版本改为**标签关键词匹配 + LLM 决策**,匹配
失败时退回 ``bgm_meta_ref.yaml::default_match``。

设计纪律(plan §5 阶段 4 + §6.1):
1. **资源描述驱动**:``bgm_meta_ref.yaml`` 是真源,二进制音频文件由用户
   准备;本地化代码只读标签(``mood`` / ``genre`` / ``tempo``)。
2. **不依赖 librosa / torch**:节拍数组用 ``beat_period_ms`` 等距近似,
   真实 BGM 上线时再 ``RealBgmSelector`` 替换。
3. **LLM 选择独立函数**:``rank_candidates_with_llm`` 走 prompt,与 vendored
   行为对齐(让 LLM 从 N 个候选里挑一条)。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from storyline_capabilities.prompts import render_prompt
from storyline_capabilities.vlm_client import (
    LLMClient,
    StubLLMClient,
    chat_json,
    parse_json_loose,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 标签关键词表(简化版,启发式匹配)
# ---------------------------------------------------------------------------
# 中文 / 英文常见情绪 / 风格关键词;匹配到任何一条就作为 tag 命中。
_MOOD_KEYWORDS: dict[str, list[str]] = {
    "warm": ["温暖", "温情", "暖", "温暖", "温馨", "warm", "cozy"],
    "calm": ["安静", "平静", "轻", "calm", "quiet", "soft"],
    "storytelling": ["故事", "叙事", "叙述", "story", "narrative"],
    "happy": ["欢快", "开心", "高兴", "happy", "joyful", "cheerful"],
    "energetic": ["活力", "动感", "热血", "energetic", "dynamic"],
    "excited": ["激动", "兴奋", "excited", "thrilling"],
    "neutral": ["中性", "通用", "neutral", "ambient"],
    "focused": ["专注", "解說", "解说", "focused", "explainer"],
    "dramatic": ["戏剧", "紧张", "dramatic", "epic"],
    "epic": ["史诗", "壮阔", "epic", "cinematic"],
    "tense": ["紧张", "悬疑", "tense", "thriller"],
    "sad": ["伤感", "忧伤", "悲伤", "sad", "melancholy"],
    "soft": ["柔和", "温柔", "soft", "tender"],
    "memory": ["回忆", "怀旧", "memory", "nostalgic"],
}


def _match_mood_tags(text: str) -> set[str]:
    """从 user_request / script narration 文本里抽出 mood tag。"""
    if not text:
        return set()
    out: set[str] = set()
    lowered = text.lower()
    for tag, kws in _MOOD_KEYWORDS.items():
        for kw in kws:
            if kw and kw.lower() in lowered:
                out.add(tag)
                break
    return out


def _score_library_entry(
    entry: dict[str, Any],
    matched_moods: set[str],
) -> int:
    """按 mood 命中数打分。"""
    if not matched_moods:
        return 0
    entry_moods = set(entry.get("mood") or [])
    return len(matched_moods & entry_moods)


# ---------------------------------------------------------------------------
# YAML 解析(简化版,主项目不引入 PyYAML)
# ---------------------------------------------------------------------------
def _parse_bgm_meta_yaml(path: Path) -> dict[str, Any]:
    """读 bgm_meta_ref.yaml。

    返回 ``{"library": [...], "default_match": {...}, "matching": {...}}``;
    解析失败返回 ``{}``。
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    out: dict[str, Any] = {
        "library": [],
        "default_match": {},
        "matching": {"llm_assist": True, "max_candidates_for_llm": 10},
    }

    # 解析 library 列表(只识别顶层缩进块,不深入 nested dict)
    # 形状: - bgm_id: "..."   path: ""   mood: [..]   tempo: "..."
    library: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            if cur:
                library.append(cur)
            cur = {}
            # "  - bgm_id: ..." 或 "  - path: ..."
            rest = line[4:]
            if ":" in rest:
                k, v = rest.split(":", 1)
                cur[k.strip()] = v.strip().strip('"').strip("'")
        elif cur is not None and line.startswith("    "):
            rest = line.strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                v = v.strip()
                # 列表值:["a", "b"] 或 [a, b]
                if v.startswith("[") and v.endswith("]"):
                    inner = v[1:-1].strip()
                    if inner:
                        items = [
                            x.strip().strip('"').strip("'")
                            for x in inner.split(",")
                            if x.strip()
                        ]
                        cur[k.strip()] = items
                    else:
                        cur[k.strip()] = []
                else:
                    cur[k.strip()] = v.strip('"').strip("'")
    if cur:
        library.append(cur)
    out["library"] = library

    # default_match:
    m = re.search(
        r"^default_match:\s*$\n(?:\s+\w+:.*\n)+",
        text,
        flags=re.MULTILINE,
    )
    if m:
        block = m.group(0)
        bgm_id_m = re.search(r"bgm_id:\s*\"([^\"]+)\"", block)
        if bgm_id_m:
            out["default_match"]["bgm_id"] = bgm_id_m.group(1)

    # matching:
    llm_m = re.search(r"llm_assist:\s*(\w+)", text)
    if llm_m:
        out["matching"]["llm_assist"] = llm_m.group(1).lower() == "true"
    n_m = re.search(r"max_candidates_for_llm:\s*(\d+)", text)
    if n_m:
        out["matching"]["max_candidates_for_llm"] = int(n_m.group(1))

    return out


_DEFAULT_BGM_META_PATH: Path = (
    Path(__file__).resolve().parent / "resource" / "bgm_meta_ref.yaml"
)


def load_bgm_library(
    yaml_path: Optional[Path] = None,
) -> dict[str, Any]:
    """加载 BGM 资源描述(plan §6.1 选项 B)。"""
    return _parse_bgm_meta_yaml(yaml_path or _DEFAULT_BGM_META_PATH)


# ---------------------------------------------------------------------------
# 候选排序(启发式 + LLM)
# ---------------------------------------------------------------------------
def rank_candidates(
    *,
    library: list[dict[str, Any]],
    user_request: str,
    narration_text: str = "",
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """按 mood 命中数排序的候选列表(plan §5 阶段 4 决策 3)。"""
    matched = _match_mood_tags(user_request) | _match_mood_tags(narration_text)
    if not matched:
        return list(library)[:top_n]
    scored = sorted(
        library,
        key=lambda e: _score_library_entry(e, matched),
        reverse=True,
    )
    return scored[:top_n]


def rank_candidates_with_llm(
    *,
    candidates: list[dict[str, Any]],
    user_request: str,
    lang: str = "zh",
    client: Optional[LLMClient] = None,
) -> Optional[dict[str, Any]]:
    """用 LLM 从候选里挑一条(与 vendored ``select_bgm`` 行为对齐)。

    返回选中的 candidate dict,失败 / 解析失败 / LLM 不可用时返回 ``None``
    (由调用方 fallback 到 ``rank_candidates`` 第一条或 default_match)。
    """
    if not candidates:
        return None

    sys_p = render_prompt("select_bgm", "system", lang=lang)
    user_p = render_prompt(
        "select_bgm",
        "user",
        lang=lang,
        candidates=json.dumps(candidates, ensure_ascii=False, indent=2),
        user_request=str(user_request or ""),
    )

    try:
        obj = chat_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            schema={
                "type": "object",
                "properties": {
                    "bgm_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM rank_candidates failed: {e!r}")
        return None

    if not isinstance(obj, dict):
        return None

    chosen_id = obj.get("bgm_id")
    if not isinstance(chosen_id, str) or not chosen_id:
        return None

    for entry in candidates:
        if entry.get("bgm_id") == chosen_id:
            enriched = dict(entry)
            enriched["reason"] = str(obj.get("reason") or "")
            return enriched
    return None


# ---------------------------------------------------------------------------
# 节拍数组(纯函数,无 librosa / torch 依赖)
# ---------------------------------------------------------------------------
def compute_beats_ms(
    *,
    duration_ms: int,
    beat_period_ms: int,
) -> list[int]:
    """按 ``beat_period_ms`` 等距生成节拍时间戳(毫秒)。"""
    if duration_ms <= 0 or beat_period_ms <= 0:
        return []
    beats: list[int] = []
    t = beat_period_ms
    while t < duration_ms:
        beats.append(int(t))
        t += beat_period_ms
    return beats


# ---------------------------------------------------------------------------
# 默认背景音量(plan §3.2 state 字段 storyline_bgm_selection 字段约定)
# ---------------------------------------------------------------------------
def _select_fallback(
    library: list[dict[str, Any]],
    default_match: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if default_match.get("bgm_id"):
        for e in library:
            if e.get("bgm_id") == default_match["bgm_id"]:
                return e
    return library[0] if library else None


__all__ = [
    "compute_beats_ms",
    "load_bgm_library",
    "rank_candidates",
    "rank_candidates_with_llm",
]
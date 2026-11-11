"""文案模板推荐能力层(2026-11-12 noop 治理阶段1,设计文档 §2.4)。

参考 FireRed-OpenStoryline ``script_template_rec.py`` + ``StorylineRecall`` +
``ElementFilter``(b45011d)的**算法思想与数据契约**,按本项目约束重写:

- 零新增重依赖:检索用纯 Python BM25 风格评分,不引入 FAISS / embedding
  (pyproject 禁 torch 等,ADR-001;未来确需向量召回走独立 sidecar,见设计
  文档 §2.8 兼容性决策)——``TemplateRetriever`` Protocol 预留插件位。
- **确定性**:同 query + 同模板库版本 ⇒ 同排序(平局按 ``template_id``),
  便于测试与 qa 重试缓存。
- **禁止照搬** FireRed ``ElementFilter`` 空结果随机抽样的 fallback:
  筛选后无候选必须返回 ``no_match``,绝不随机选不相关模板。
- 模板正文是风格/结构参考,不是事实来源(注入纪律写在 generate_script prompt)。
- 资源缺失/损坏一律显式降级(仿 bgm_selector 的容错口径),绝不伪造候选。

本模块是纯能力层:不读 state、不写文件(artifact 落盘由节点层负责)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

#: 召回阶段候选数(FireRed query_top_n 默认 32,沿用)
DEFAULT_RETRIEVAL_TOP_K = 32
#: 最终返回候选数(FireRed candidates[:3],沿用)
DEFAULT_TOP_CANDIDATES = 3
#: 注入 prompt 的模板正文截断预算(字符)
DEFAULT_MAX_CONTENT_CHARS = 1200
#: 标签枚举(FireRed script_template_label prompt 的 12 个枚举)
TAG_ENUM: frozenset[str] = frozenset(
    {
        "Life", "Food", "Beauty", "Entertainment", "Travel", "Tech",
        "Business", "Vehicle", "Health", "Family", "Pets", "Knowledge",
    }
)

_RECO_STATUS_OK = "ok"
_RECO_STATUS_NO_MATCH = "no_match"
_RECO_STATUS_UNAVAILABLE = "unavailable"
RECO_STATUS_VALUES = (_RECO_STATUS_OK, _RECO_STATUS_NO_MATCH, _RECO_STATUS_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 模板目录加载(TemplateCatalog)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TemplateEntry:
    """一条模板的元数据 + 正文。``rel_path`` 相对模板根目录。"""

    template_id: str
    rel_path: str
    language: str
    description: str
    tags: tuple[str, ...]
    content_sha256: str
    version: int
    enabled: bool
    content: str


@dataclass
class LoadedCatalog:
    """加载结果。``available=False`` 表示库不存在/全无效(节点按 unavailable 降级)。"""

    entries: list[TemplateEntry]
    warnings: list[str]
    fingerprint: str  # 库内容指纹:进入缓存键,内容变化即失效

    @property
    def available(self) -> bool:
        return bool(self.entries)


def _default_root() -> Path:
    return Path(__file__).resolve().parent / "resource" / "script_templates"


def _empty_catalog(warnings: list[str]) -> LoadedCatalog:
    return LoadedCatalog(entries=[], warnings=list(warnings), fingerprint="")


def _entry_fingerprint(entries: Sequence[TemplateEntry]) -> str:
    joined = "\n".join(
        f"{e.template_id}:{e.content_sha256}:{e.version}:{e.enabled}"
        for e in sorted(entries, key=lambda x: x.template_id)
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def load_template_catalog(root: Optional[Path] = None) -> LoadedCatalog:
    """加载模板库。容错口径:meta 缺失/损坏/编码错 → 返回空库+warning,不抛。

    安全校验(设计文档 §1.1 任务4):
    - ``path`` 解析后必须仍在模板根目录内(防 ``../`` 路径穿越);
    - 正文 SHA-256 与 meta 声明不符 → 丢弃该候选并记 warning(不伪造);
    - 文件缺失 / 编码错误 → 同样丢弃。
    """
    root = Path(root) if root is not None else _default_root()
    meta_path = root / "meta.json"
    warnings: list[str] = []
    if not meta_path.is_file():
        warnings.append(f"meta.json 不存在: {meta_path.name}(模板库未配置)")
        return _empty_catalog(warnings)
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        warnings.append(f"meta.json 解析失败: {e!r}")
        return _empty_catalog(warnings)
    if not isinstance(raw, dict) or not isinstance(raw.get("templates"), list):
        warnings.append("meta.json 结构不对(缺 templates 数组)")
        return _empty_catalog(warnings)

    root_resolved = root.resolve()
    entries: list[TemplateEntry] = []
    for idx, item in enumerate(raw.get("templates") or []):
        if not isinstance(item, dict):
            warnings.append(f"templates[{idx}] 不是对象,丢弃")
            continue
        template_id = str(item.get("template_id") or "").strip()
        rel_path = str(item.get("path") or "").strip()
        if not template_id or not rel_path:
            warnings.append(f"templates[{idx}] 缺 template_id/path,丢弃")
            continue
        if not bool(item.get("enabled", True)):
            continue  # 显式停用是有意为之,不算 warning
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            warnings.append(f"{template_id}: path 越出模板根目录({rel_path!r}),丢弃")
            continue
        if not target.is_file():
            warnings.append(f"{template_id}: 模板文件缺失({rel_path!r}),丢弃")
            continue
        try:
            data = target.read_bytes()
            content = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            warnings.append(f"{template_id}: 模板读取/解码失败({e!r}),丢弃")
            continue
        sha = hashlib.sha256(data).hexdigest()
        declared = str(item.get("content_sha256") or "").strip().lower()
        if declared and declared != sha:
            warnings.append(f"{template_id}: content_sha256 不匹配(先跑标注脚本重算),丢弃")
            continue
        try:
            version = int(item.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        tags_raw = item.get("tags")
        tags = tuple(
            str(t).strip() for t in (tags_raw if isinstance(tags_raw, list) else []) if str(t).strip()
        )
        entries.append(
            TemplateEntry(
                template_id=template_id,
                rel_path=rel_path,
                language=str(item.get("language") or "zh").strip() or "zh",
                description=str(item.get("description") or "").strip(),
                tags=tags,
                content_sha256=sha,
                version=version,
                enabled=True,
                content=content,
            )
        )
    return LoadedCatalog(
        entries=entries, warnings=warnings, fingerprint=_entry_fingerprint(entries)
    )


# ---------------------------------------------------------------------------
# 检索(TemplateRetriever):BM25 风格评分,零外部依赖
# ---------------------------------------------------------------------------
@runtime_checkable
class TemplateRetriever(Protocol):
    """检索插件接口。未来接向量召回(sidecar)时实现同签名即可。"""

    def retrieve(
        self, entries: Sequence[TemplateEntry], query: str, *, top_k: int = DEFAULT_RETRIEVAL_TOP_K
    ) -> list[tuple[TemplateEntry, float]]:
        ...


_ZH_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """确定性分词:中文按二元 bigram(单字成串时保留单字),ASCII 按小写词。"""
    text = (text or "").lower()
    tokens: list[str] = []
    for run in _ZH_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i: i + 2] for i in range(len(run) - 1))
    tokens.extend(_ASCII_WORD.findall(text))
    return tokens


class KeywordTemplateRetriever:
    """BM25 风格评分检索(k1=1.2, b=0.75)。

    评分域 = template_id + description + tags + 正文(模板正文短,直接进索引);
    相同输入必然给出相同排序(平局按 ``template_id`` 升序)。
    """

    K1 = 1.2
    B = 0.75

    def retrieve(
        self, entries: Sequence[TemplateEntry], query: str, *, top_k: int = DEFAULT_RETRIEVAL_TOP_K
    ) -> list[tuple[TemplateEntry, float]]:
        docs_tokens = [
            _tokenize(
                " ".join(
                    [
                        e.template_id.replace("_", " "),
                        e.description,
                        " ".join(e.tags),
                        e.content,
                    ]
                )
            )
            for e in entries
        ]
        query_tokens = _tokenize(query)
        n_docs = len(docs_tokens)
        if not n_docs or not query_tokens:
            return []
        avgdl = sum(len(d) for d in docs_tokens) / n_docs or 1.0
        df: Counter[str] = Counter()
        for d in docs_tokens:
            df.update(set(d))
        scored: list[tuple[TemplateEntry, float]] = []
        for entry, doc in zip(entries, docs_tokens):
            tf = Counter(doc)
            dl = len(doc)
            score = 0.0
            for term in query_tokens:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                idf = math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1.0)
                score += idf * (freq * (self.K1 + 1)) / (
                    freq + self.K1 * (1 - self.B + self.B * dl / avgdl)
                )
            scored.append((entry, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].template_id))
        return scored[: max(0, int(top_k))]


# ---------------------------------------------------------------------------
# 标签过滤(仿 FireRed ElementFilter 语义,但无随机 fallback)
# ---------------------------------------------------------------------------
def filter_candidates(
    candidates: Sequence[tuple[TemplateEntry, float]],
    filter_include: Optional[dict[str, list[str]]] = None,
    filter_exclude: Optional[dict[str, list[str]]] = None,
) -> list[tuple[TemplateEntry, float]]:
    """include/exclude 标签过滤。

    语义(FireRed ElementFilter 对齐):**不同维度 AND,同一维度多值 OR**;
    exclude 任一维度命中即剔除。过滤后为空就是空——**绝不随机回填**
    (FireRed 的 random.sample fallback 是已知缺陷,设计文档明令禁止照搬)。
    支持维度:``tags`` / ``template_id`` / ``language``;比较统一小写。
    """
    include = filter_include or {}
    exclude = filter_exclude or {}
    unknown_dims = {
        d for d in (list(include) + list(exclude))
        if d not in ("tags", "template_id", "language")
    }
    if unknown_dims:
        logger.warning("未知的过滤维度(忽略): %s", sorted(unknown_dims))
        include = {d: v for d, v in include.items() if d in ("tags", "template_id", "language")}
        exclude = {d: v for d, v in exclude.items() if d in ("tags", "template_id", "language")}

    def _all_dims_match(entry: TemplateEntry, filters: dict[str, list[str]]) -> bool:
        for dim, values in filters.items():
            wanted = [
                str(v).strip().lower()
                for v in (values if isinstance(values, list) else [values])
                if str(v).strip()
            ]
            if not wanted:
                continue
            if dim == "tags":
                pool = [t.lower() for t in entry.tags]
            else:
                field_val = getattr(entry, dim, None)
                pool = [str(field_val).lower()] if field_val is not None else []
            if not any(w in pool for w in wanted):
                return False
        return True

    out: list[tuple[TemplateEntry, float]] = []
    for entry, score in candidates:
        if not _all_dims_match(entry, include):
            continue
        if exclude and _all_dims_match(entry, exclude):
            continue
        out.append((entry, score))
    return out


# ---------------------------------------------------------------------------
# 推荐 API(节点层唯一入口)
# ---------------------------------------------------------------------------
def _candidate_view(entry: TemplateEntry, score: float) -> dict[str, Any]:
    return {
        "template_id": entry.template_id,
        "path": entry.rel_path,
        "language": entry.language,
        "description": entry.description,
        "tags": list(entry.tags),
        "version": entry.version,
        "content_sha256": entry.content_sha256,
        "score": round(float(score), 6),
    }


def _truncate_content(content: str, max_chars: int) -> tuple[str, bool]:
    """按预算截断;尽量在最近一个换行处断开,保持段落完整。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    cut = content[:max_chars]
    nl = cut.rfind("\n")
    if nl > max_chars // 2:
        cut = cut[: nl + 1]
    return cut, True


def recommend_script_template(
    *,
    query: str,
    language: Optional[str] = None,
    filter_include: Optional[dict[str, list[str]]] = None,
    filter_exclude: Optional[dict[str, list[str]]] = None,
    catalog: Optional[LoadedCatalog] = None,
    retriever: Optional[TemplateRetriever] = None,
    top_k_retrieval: int = DEFAULT_RETRIEVAL_TOP_K,
    top_candidates: int = DEFAULT_TOP_CANDIDATES,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> dict[str, Any]:
    """检索 + 标签过滤 + 选出模板,返回 artifact dict(节点层直接落盘)。

    状态语义(设计文档 §1.4 降级表):
    - ``ok``:有有效候选,选出 Top 1;
    - ``no_match``:库可用但检索/过滤后无候选(不随机回填);
    - ``unavailable``:库缺失/全无效。
    任何内部异常折算成 ``unavailable`` + fallback_reason,本能力不向节点抛。
    """
    try:
        cat = catalog if catalog is not None else load_template_catalog()
        if not cat.available:
            return {
                "status": _RECO_STATUS_UNAVAILABLE,
                "query": query,
                "language": language,
                "candidates": [],
                "selected_template": None,
                "template_id": None,
                "template_version": None,
                "content_sha256": None,
                "retrieval_method": "keyword_bm25",
                "selection_method": None,
                "fallback_reason": "; ".join(cat.warnings) or "模板库为空",
                "selected_template_content": None,
                "content_truncated": False,
                "warnings": list(cat.warnings),
            }

        entries = list(cat.entries)
        if language:
            entries = [e for e in entries if e.language == str(language).lower()]
        if not entries:
            return {
                "status": _RECO_STATUS_NO_MATCH,
                "query": query,
                "language": language,
                "candidates": [],
                "selected_template": None,
                "template_id": None,
                "template_version": None,
                "content_sha256": None,
                "retrieval_method": "keyword_bm25",
                "selection_method": None,
                "fallback_reason": f"模板库中无 {language!r} 语言模板",
                "selected_template_content": None,
                "content_truncated": False,
                "warnings": [],
            }

        engine = retriever if retriever is not None else KeywordTemplateRetriever()
        ranked = engine.retrieve(entries, query, top_k=top_k_retrieval)
        filtered = filter_candidates(ranked, filter_include, filter_exclude)
        top = filtered[: max(1, int(top_candidates))]
        if not top:
            return {
                "status": _RECO_STATUS_NO_MATCH,
                "query": query,
                "language": language,
                "candidates": [],
                "selected_template": None,
                "template_id": None,
                "template_version": None,
                "content_sha256": None,
                "retrieval_method": "keyword_bm25",
                "selection_method": None,
                "fallback_reason": "检索/标签过滤后无候选(no_match,不做随机回填)",
                "selected_template_content": None,
                "content_truncated": False,
                "warnings": [],
            }

        selected_entry, _score = top[0]
        content, truncated = _truncate_content(selected_entry.content, max_content_chars)
        return {
            "status": _RECO_STATUS_OK,
            "query": query,
            "language": language,
            "candidates": [_candidate_view(e, s) for e, s in top],
            "selected_template": _candidate_view(selected_entry, _score),
            "template_id": selected_entry.template_id,
            "template_version": selected_entry.version,
            "content_sha256": selected_entry.content_sha256,
            "retrieval_method": "keyword_bm25",
            "selection_method": "deterministic_top1",
            "fallback_reason": None,
            "selected_template_content": content,
            "content_truncated": truncated,
            "warnings": list(cat.warnings),
        }
    except Exception as e:  # noqa: BLE001 — 降级不抛(设计文档 §1.4)
        logger.warning("recommend_script_template 降级: %r", e)
        return {
            "status": _RECO_STATUS_UNAVAILABLE,
            "query": query,
            "language": language,
            "candidates": [],
            "selected_template": None,
            "template_id": None,
            "template_version": None,
            "content_sha256": None,
            "retrieval_method": "keyword_bm25",
            "selection_method": None,
            "fallback_reason": f"推荐过程异常: {e!r}",
            "selected_template_content": None,
            "content_truncated": False,
            "warnings": [],
        }


def build_template_query(
    *,
    user_request: str = "",
    overall: str = "",
    group_themes: Sequence[str] = (),
) -> str:
    """组装检索 query:用户要求 + 素材概述 + 分组主题(设计文档 §1.2)。"""
    parts = [str(user_request or "").strip()]
    parts.append(str(overall or "").strip())
    parts.extend(str(t).strip() for t in group_themes if str(t).strip())
    return " ".join(p for p in parts if p)


__all__ = [
    "TemplateEntry",
    "LoadedCatalog",
    "TemplateRetriever",
    "KeywordTemplateRetriever",
    "load_template_catalog",
    "filter_candidates",
    "recommend_script_template",
    "build_template_query",
    "RECO_STATUS_VALUES",
    "DEFAULT_RETRIEVAL_TOP_K",
    "DEFAULT_TOP_CANDIDATES",
    "DEFAULT_MAX_CONTENT_CHARS",
    "TAG_ENUM",
]

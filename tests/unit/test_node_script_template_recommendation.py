"""storyline_script_template_recommendation 节点单测(2026-11-12 noop 治理阶段1)。

覆盖:正常推荐(真实模板库)、qa 重试缓存命中、目录加载异常降级、
空库降级、模板正文花括号中和、固定状态标签、旧 checkpoint 缺字段兼容。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import nodes.storyline.node_script_template_recommendation as node_mod
from nodes.storyline.node_script_template_recommendation import (
    storyline_script_template_recommendation_node,
)
from storyline_capabilities.script_template_rec import LoadedCatalog, TemplateEntry

_DONE_TAG = "storyline_script_template_recommendation_done"


def _state(tmp_path: Path, **over: object) -> dict:
    out_root = tmp_path / "outputs"
    (out_root / "storyline").mkdir(parents=True, exist_ok=True)
    groups_path = out_root / "storyline" / "groups.json"
    groups_path.write_text(
        json.dumps(
            {
                "groups": [
                    {"group_id": "group_0001", "theme": "清晨街景"},
                    {"group_id": "group_0002", "theme": "咖啡店日常"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    und_path = out_root / "storyline" / "understanding.json"
    und_path.write_text(
        json.dumps({"overall": "一段城市街拍和咖啡店的画面"}, ensure_ascii=False),
        encoding="utf-8",
    )
    st: dict = {
        "session_id": "str-test",
        "status_log": [],
        "error_log": [],
        "storyline_outputs_root": str(out_root),
        "storyline_groups_artifact": str(groups_path),
        "storyline_understanding_artifact": str(und_path),
        "storyline_targets": {"script_request": "日常记录 轻快风格"},
    }
    st.update(over)
    return st


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例独占缓存(qa 重试缓存是进程级 memo)。"""
    node_mod._RECO_CACHE.clear()
    yield
    node_mod._RECO_CACHE.clear()


def test_ok_path_writes_artifact_and_state(tmp_path: Path) -> None:
    """真实模板库:status=ok、选出 template_id、正文就位,state 字段与固定标签齐。"""
    out = storyline_script_template_recommendation_node(_state(tmp_path))
    assert out["status_log"] == [_DONE_TAG]  # 固定标签
    artifact_path = out["storyline_script_template_artifact"]
    assert artifact_path and Path(artifact_path).name == "script_template_plan.json"

    doc = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    assert doc["status"] == "ok"
    assert doc["template_id"], "应选出模板"
    assert doc["retrieval_method"] == "keyword_bm25"
    assert doc["selection_method"] == "deterministic_top1"
    assert doc["fallback_reason"] is None
    assert doc["selected_template_content"], "正文片段应就位"
    assert doc["query"], "检索 query 应由用户要求+overall+主题组装"
    # 检索词应包含三路输入
    assert "日常记录" in doc["query"] and "咖啡" in doc["query"]


def test_cache_hit_on_same_query_and_catalog(tmp_path: Path) -> None:
    """同 query + 同库指纹 ⇒ 第二次命中缓存(qa 重试不重算,设计文档 §1.3 任务10)。"""
    st = _state(tmp_path)
    first = storyline_script_template_recommendation_node(st)
    assert json.loads(Path(first["storyline_script_template_artifact"]).read_text(encoding="utf-8"))[
        "from_cache"
    ] is False
    second = storyline_script_template_recommendation_node(st)
    doc2 = json.loads(Path(second["storyline_script_template_artifact"]).read_text(encoding="utf-8"))
    assert doc2["from_cache"] is True
    assert doc2["template_id"] == json.loads(
        Path(first["storyline_script_template_artifact"]).read_text(encoding="utf-8")
    )["template_id"]


def test_cache_invalidated_when_query_changes(tmp_path: Path) -> None:
    st = _state(tmp_path)
    storyline_script_template_recommendation_node(st)
    st2 = _state(tmp_path, storyline_targets={"script_request": "产品测评结构化解说"})
    out2 = storyline_script_template_recommendation_node(st2)
    doc2 = json.loads(Path(out2["storyline_script_template_artifact"]).read_text(encoding="utf-8"))
    assert doc2["from_cache"] is False


def test_degrades_when_catalog_loader_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录加载异常 → status=unavailable,节点照常返回(主链节点绝不抛,§1.4)。"""
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(node_mod, "load_template_catalog", _boom)
    out = storyline_script_template_recommendation_node(_state(tmp_path))
    assert out["status_log"] == [_DONE_TAG]  # 降级也是固定标签
    doc = json.loads(Path(out["storyline_script_template_artifact"]).read_text(encoding="utf-8"))
    assert doc["status"] == "unavailable"
    assert doc["template_id"] is None
    assert "异常" in (doc["fallback_reason"] or "")


def test_empty_library_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        node_mod,
        "load_template_catalog",
        lambda *a, **k: LoadedCatalog(entries=[], warnings=["meta.json 不存在"], fingerprint=""),
    )
    out = storyline_script_template_recommendation_node(_state(tmp_path))
    doc = json.loads(Path(out["storyline_script_template_artifact"]).read_text(encoding="utf-8"))
    assert doc["status"] == "unavailable"
    assert doc["selected_template_content"] is None


def test_template_content_braces_neutralized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正文里的花括号必须中和(与 web_research 同款纪律,防占位符干扰)。"""
    entry = TemplateEntry(
        template_id="evil_v1",
        rel_path="templates/evil.txt",
        language="zh",
        description="含花括号的模板",
        tags=("Life",),
        content_sha256="0" * 64,
        version=1,
        enabled=True,
        content="正文 {evil: 1} 与 {fmt} 占位",
    )
    monkeypatch.setattr(
        node_mod,
        "load_template_catalog",
        lambda *a, **k: LoadedCatalog(entries=[entry], warnings=[], fingerprint="x"),
    )
    out = storyline_script_template_recommendation_node(_state(tmp_path))
    doc = json.loads(Path(out["storyline_script_template_artifact"]).read_text(encoding="utf-8"))
    assert doc["status"] == "ok"
    assert "{" not in doc["selected_template_content"] and "}" not in doc["selected_template_content"]
    assert "(evil: 1)" in doc["selected_template_content"]


def test_old_checkpoint_without_optional_fields_still_works(tmp_path: Path) -> None:
    """旧 checkpoint 缺 understanding/groups/targets 字段 → 按"无模板输入"降级不抛。"""
    st = {
        "session_id": "legacy",
        "status_log": [],
        "error_log": [],
        "storyline_outputs_root": str(tmp_path / "outputs"),
    }
    (tmp_path / "outputs" / "storyline").mkdir(parents=True, exist_ok=True)
    out = storyline_script_template_recommendation_node(st)
    doc = json.loads(Path(out["storyline_script_template_artifact"]).read_text(encoding="utf-8"))
    # 空 query 检索不出候选 → no_match(不抛、不随机回填),文案侧照常生成
    assert doc["status"] == "no_match"
    assert out["status_log"] == [_DONE_TAG]

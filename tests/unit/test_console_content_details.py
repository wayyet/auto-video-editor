"""AI 内容明细只读 API 单测(2026-11-19 第八期审查修订版)。

- ``GET /api/console/runs/{tid}/content-details``:五区块(网页话题检索 /
  模板推荐 / 生成文案 / 中英文字幕)归一化 + 状态推导 + 局部降级;
- 状态枚举:ok/cache_hit/no_results/skipped/pending/missing/failed/
  unavailable/invalid(模板额外透传 no_match),不把所有非 ok 都折叠成 missing;
- 接缝整体 monkeypatch:``_fetch_checkpoint_values``(不连真实 PG)、
  ``reconcile_run_state``(固定调和态,隔离事件库)、CONSOLE_OUTPUTS_ROOT(tmp)。
  覆盖审查文档 §P4 后端矩阵:三区全有效 / cache_hit / no_results / skipped·failed
  标签 / 模板 no_match·unavailable / description·score 在候选对象内 / references 空 /
  script 先写盘后裁决(done 标签缺失→failed/pending)/ zh·en 三态徽章 /
  单 JSON 损坏局部 invalid / checkpoint 异常字幕区 unavailable 其余 200 /
  非法 tid 400 / 任务不存在 404 / 双任务隔离 / cues 超限截断 / 记录损坏 500。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402
import web.console_service as console_service  # noqa: E402
from monitor_fastapi import app  # noqa: E402

client = TestClient(app)

TID = "console-ab12cd34ef56"
TID2 = "console-99fedcba4321"


# ---------------------------------------------------------------------------
# fixture 与产物工厂
# ---------------------------------------------------------------------------

WEB_TOPIC_OK = {
    "status": "ok",
    "query": "马来西亚 清真寺 vlog",
    "searched_at": "2026-11-18T15:40:00Z",
    "search_web_topic": [
        {
            "url": "https://travel.example.com/mosque",
            "title": "马六甲海峡清真寺",
            "snippet": "位于马六甲海峡上的水上清真寺",
            "source": "duckduckgo_html",
        }
    ],
    "preview_urls": [],
    "error_code": None,
    "message": None,
    "query_hash": "abc123",
}

_TEMPLATE_ITEM = {
    "template_id": "t-travel-zh-01",
    "path": "templates/travel.md",
    "language": "zh",
    "description": "旅行回顾模板",
    "tags": ["travel", "vlog"],
    "version": 1,
    "content_sha256": "h" * 8,
    "score": 0.91,
}

TEMPLATE_OK = {
    "status": "ok",
    "query": "旅行回顾",
    "language": "zh",
    "candidates": [_TEMPLATE_ITEM],
    "selected_template": dict(_TEMPLATE_ITEM),
    "template_id": "t-travel-zh-01",
    "template_version": "1.0",
    "content_sha256": "h" * 8,
    "retrieval_method": "keyword_bm25",
    "selection_method": "deterministic_top1",
    "fallback_reason": None,
    "selected_template_content": "模板正文(≤1200字)",
    "content_truncated": False,
    "warnings": [],
    "from_cache": False,
}

SCRIPT_OK = {
    "group_scripts": [
        {
            "group_id": "g1",
            "narration": "大家好,今天带大家看马六甲海峡清真寺。",
            "summary": "开场",
            "lang": "zh",
            "voiceover_style": "明快",
            "captions": [],
        }
    ],
    "lang": "zh",
    "voiceover_style": "明快",
    "references": [
        {"title": "马六甲旅游攻略", "url": "https://guide.example.com/malacca", "source": "duckduckgo_html"}
    ],
}

CHECKPOINT_OK = {
    "subtitle_segments_zh": [
        {"index": 1, "start_ms": 0, "end_ms": 2400, "text_zh": "大家好"},
        {"index": 2, "start_ms": 2400, "end_ms": 5200, "text_zh": "这里是马六甲海峡清真寺"},
    ],
    "subtitle_segments_en": [
        {"index": 1, "start_ms": 0, "end_ms": 2400, "text_en": "Hello everyone"},
        {"index": 2, "start_ms": 2400, "end_ms": 5200, "text_en": "This is the Malacca Strait Mosque"},
    ],
    "subtitle_translation_status": "real",
    "status_log": [
        "storyline_search_web_topic_done",
        "storyline_script_template_recommendation_done",
        "storyline_generate_script_done",
        "node_08_add_subtitles_done",
        "node_16a_translate_done",
    ],
    "error_log": [],
}


def make_task(
    root: Path,
    tid: str,
    status: str = "completed",
    storyline: dict[str, object] | None = None,
) -> Path:
    """造一个最小任务目录(console_run.json + storyline/*.json)。"""
    task_dir = root / tid
    (task_dir / "storyline").mkdir(parents=True, exist_ok=True)
    record = {
        "thread_id": tid,
        "created_at": "2026-11-19T00:00:00+00:00",
        "status": status,
    }
    (task_dir / "console_run.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    for name, doc in (storyline or {}).items():
        path = task_dir / "storyline" / name
        if isinstance(doc, str):  # 原始文本(构造损坏 JSON 用)
            path.write_text(doc, encoding="utf-8")
        else:
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return task_dir


@pytest.fixture()
def ai_env(tmp_path, monkeypatch):
    """隔离 outputs 根 + 固定调和态 + 假 checkpoint(默认空 values)。"""
    monkeypatch.setattr(config, "CONSOLE_OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(
        console_service, "reconcile_run_state", lambda record, tid: "completed"
    )
    monkeypatch.setattr(console_service, "_fetch_checkpoint_values", lambda tid: {})
    return tmp_path


def _full_storyline() -> dict[str, object]:
    return {
        "search_web_topic.json": WEB_TOPIC_OK,
        "script_template_plan.json": TEMPLATE_OK,
        "script.json": SCRIPT_OK,
    }


def _get(tid: str):
    return client.get(f"/api/console/runs/{tid}/content-details")


# ---------------------------------------------------------------------------
# happy path 与状态透传
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_five_sections_all_ok(self, ai_env, monkeypatch):
        make_task(ai_env, TID, storyline=_full_storyline())
        monkeypatch.setattr(
            console_service, "_fetch_checkpoint_values", lambda tid: dict(CHECKPOINT_OK)
        )
        resp = _get(TID)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["thread_id"] == TID
        assert body["task_status"] == "completed"

        # 01 网页话题检索:引擎 + 来源域名
        wt = body["web_topic"]
        assert wt["status"] == "ok"
        assert wt["engine"] == "DuckDuckGo"
        assert wt["query"] == "马来西亚 清真寺 vlog"
        assert wt["entries"][0]["domain"] == "travel.example.com"
        assert wt["entries"][0]["url"].startswith("https://")

        # 02 模板推荐:description/score 取自候选对象,顶层无该字段
        tpl = body["template"]
        assert tpl["status"] == "ok"
        assert tpl["template_id"] == "t-travel-zh-01"
        assert tpl["retrieval_method"] == "keyword_bm25"
        assert tpl["selected_template"]["description"] == "旅行回顾模板"
        assert tpl["selected_template"]["score"] == pytest.approx(0.91)
        assert tpl["candidates"][0]["template_id"] == "t-travel-zh-01"
        assert "description" not in {k for k in tpl if k.startswith("desc")}

        # 03 生成文案:分组旁白 + references 域名
        sc = body["script"]
        assert sc["status"] == "ok"
        assert sc["lang"] == "zh"
        assert sc["groups"][0]["narration"].startswith("大家好")
        assert sc["references"][0]["domain"] == "guide.example.com"

        # 04/05 字幕:计数 + cue 结构 + 翻译徽章
        zh = body["subtitles_zh"]
        assert zh["status"] == "ok"
        assert zh["count"] == 2
        assert zh["cues"][0] == {"index": 1, "start_ms": 0, "end_ms": 2400, "text": "大家好"}
        assert zh["cues_truncated"] is False
        en = body["subtitles_en"]
        assert en["status"] == "ok"
        assert en["translation_status"] == "real"
        assert en["cues"][1]["text"] == "This is the Malacca Strait Mosque"

    def test_web_topic_cache_hit(self, ai_env):
        doc = dict(WEB_TOPIC_OK)
        doc["status"] = "cache_hit"
        make_task(ai_env, TID, storyline={"search_web_topic.json": doc})
        assert _get(TID).json()["web_topic"]["status"] == "cache_hit"

    def test_web_topic_no_results_not_failure(self, ai_env):
        doc = dict(WEB_TOPIC_OK)
        doc["status"] = "no_results"
        doc["search_web_topic"] = []
        doc["error_code"] = "NO_RESULTS"
        make_task(ai_env, TID, storyline={"search_web_topic.json": doc})
        body = _get(TID).json()["web_topic"]
        assert body["status"] == "no_results"
        assert body["entries"] == []
        assert body["reason"] == "检索完成,搜索引擎未返回结果"

    def test_web_topic_mc_fallback_exposed(self, ai_env):
        """审查阶段5:MC 回退 Bing → engine 显回退文案 + fallback 原因透传(P2-07)。"""
        doc = dict(WEB_TOPIC_OK)
        doc.update(
            {
                "engine": "Bing(MediaCrawler 回退)",
                "provider": "bing_html",
                "requested_provider": "mediacrawler",
                "message": "MediaCrawler(小红书) NETWORK_ERROR: down; 已回退 Bing",
                "fallback": {
                    "from_provider": "mediacrawler",
                    "from_platform": "xhs",
                    "to_provider": "bing_html",
                    "primary_error_code": "NETWORK_ERROR",
                    "primary_message": "down",
                },
                "search_web_topic": [
                    {
                        "url": "https://travel.example.com/mosque",
                        "title": "马六甲海峡清真寺",
                        "snippet": "s",
                        "source": "bing_html",
                    }
                ],
            }
        )
        make_task(ai_env, TID, storyline={"search_web_topic.json": doc})
        wt = _get(TID).json()["web_topic"]
        assert wt["status"] == "ok"
        assert wt["engine"] == "Bing(MediaCrawler 回退)"
        assert wt["fallback"]["to_provider"] == "bing_html"
        assert wt["fallback"]["primary_error_code"] == "NETWORK_ERROR"
        assert "已回退 Bing" in wt["fallback"]["reason"]

    def test_web_topic_mc_engine_label_from_keywords(self, ai_env):
        """engine 缺失的历史产物:provider/source 含 mediacrawler → 关键词兜底。"""
        doc = dict(WEB_TOPIC_OK)
        doc.pop("engine", None)
        doc["provider"] = "mediacrawler"
        doc["search_web_topic"] = [
            {"url": "https://xhslink.com/a", "title": "t", "snippet": "s", "source": "mediacrawler"}
        ]
        make_task(ai_env, TID, storyline={"search_web_topic.json": doc})
        assert _get(TID).json()["web_topic"]["engine"] == "MediaCrawler"

    def test_web_topic_unknown_status_is_invalid(self, ai_env):
        doc = dict(WEB_TOPIC_OK)
        doc["status"] = "weird"
        make_task(ai_env, TID, storyline={"search_web_topic.json": doc})
        body = _get(TID).json()["web_topic"]
        assert body["status"] == "invalid"
        assert "weird" in body["reason"]


# ---------------------------------------------------------------------------
# web_topic 无产物证据链(文件缺失 ≠ 未生成)
# ---------------------------------------------------------------------------

class TestWebTopicEvidence:
    def test_skipped_tag(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"status_log": ["storyline_search_web_topic_skipped"]},
        )
        body = _get(TID).json()["web_topic"]
        assert body["status"] == "skipped"
        assert "跳过" in body["reason"]

    def test_failed_tag_with_error_log(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {
                "status_log": ["storyline_search_web_topic_failed"],
                "error_log": [
                    "[storyline:search_web_topic] NETWORK_ERROR: 超时"
                ],
            },
        )
        body = _get(TID).json()["web_topic"]
        assert body["status"] == "failed"
        assert "NETWORK_ERROR" in body["reason"]

    def test_missing_when_completed_no_evidence(self, ai_env):
        make_task(ai_env, TID)
        assert _get(TID).json()["web_topic"]["status"] == "missing"

    def test_pending_when_running_no_evidence(self, ai_env, monkeypatch):
        make_task(ai_env, TID, status="starting")
        monkeypatch.setattr(
            console_service, "reconcile_run_state", lambda record, tid: "running"
        )
        assert _get(TID).json()["web_topic"]["status"] == "pending"


# ---------------------------------------------------------------------------
# 模板推荐(no_match / unavailable / 无文件)
# ---------------------------------------------------------------------------

class TestTemplate:
    def test_no_match_passthrough(self, ai_env):
        doc = dict(TEMPLATE_OK)
        doc["status"] = "no_match"
        doc["candidates"] = []
        doc["selected_template"] = None
        doc["template_id"] = None
        doc["fallback_reason"] = "模板库中无 zh 语言模板"
        make_task(ai_env, TID, storyline={"script_template_plan.json": doc})
        body = _get(TID).json()["template"]
        assert body["status"] == "no_match"
        assert body["reason"] == "模板库中无 zh 语言模板"
        assert body["selected_template"] is None

    def test_unavailable_passthrough(self, ai_env):
        doc = dict(TEMPLATE_OK)
        doc["status"] = "unavailable"
        doc["fallback_reason"] = "推荐过程异常: boom"
        make_task(ai_env, TID, storyline={"script_template_plan.json": doc})
        body = _get(TID).json()["template"]
        assert body["status"] == "unavailable"
        assert "boom" in body["reason"]

    def test_missing_when_no_file_completed(self, ai_env):
        make_task(ai_env, TID, storyline={"script.json": SCRIPT_OK})
        assert _get(TID).json()["template"]["status"] == "missing"


# ---------------------------------------------------------------------------
# 生成文案(先写盘后裁决:done 标签是完成事实来源)
# ---------------------------------------------------------------------------

class TestScript:
    def test_file_without_done_tag_completed_is_failed(self, ai_env, monkeypatch):
        make_task(ai_env, TID, storyline={"script.json": SCRIPT_OK})
        # checkpoint 无 storyline_generate_script_done 标签
        body = _get(TID).json()["script"]
        assert body["status"] == "failed"
        assert "裁决" in body["reason"]

    def test_file_without_done_tag_running_is_pending(self, ai_env, monkeypatch):
        make_task(ai_env, TID, storyline={"script.json": SCRIPT_OK})
        monkeypatch.setattr(
            console_service, "reconcile_run_state", lambda record, tid: "running"
        )
        assert _get(TID).json()["script"]["status"] == "pending"

    def test_empty_references_is_ok(self, ai_env, monkeypatch):
        doc = dict(SCRIPT_OK)
        doc["references"] = []
        make_task(ai_env, TID, storyline={"script.json": doc})
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"status_log": ["storyline_generate_script_done"]},
        )
        body = _get(TID).json()["script"]
        assert body["status"] == "ok"
        assert body["references"] == []

    def test_done_tag_but_file_missing_is_missing(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"status_log": ["storyline_generate_script_done"]},
        )
        body = _get(TID).json()["script"]
        assert body["status"] == "missing"
        assert "产物文件缺失" in body["reason"]


# ---------------------------------------------------------------------------
# 字幕(skipped 属常态;stub 不得冒充成功;done 无段 → missing)
# ---------------------------------------------------------------------------

class TestSubtitles:
    def test_zh_skipped_when_node_done_but_no_segments(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"status_log": ["node_08_add_subtitles_done"]},
        )
        body = _get(TID).json()["subtitles_zh"]
        assert body["status"] == "skipped"
        assert "无旁白字幕源" in body["reason"]

    def test_zh_missing_when_completed_no_evidence(self, ai_env):
        make_task(ai_env, TID)
        assert _get(TID).json()["subtitles_zh"]["status"] == "missing"

    def test_en_stub_badge_passthrough(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        values = dict(CHECKPOINT_OK)
        values["subtitle_translation_status"] = "stub"
        monkeypatch.setattr(
            console_service, "_fetch_checkpoint_values", lambda tid: values
        )
        body = _get(TID).json()["subtitles_en"]
        assert body["status"] == "ok"
        assert body["translation_status"] == "stub"

    def test_en_skipped_is_not_failure(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"subtitle_translation_status": "skipped"},
        )
        body = _get(TID).json()["subtitles_en"]
        assert body["status"] == "skipped"
        assert body["translation_status"] == "skipped"

    def test_en_done_tag_without_segments_is_missing(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"status_log": ["node_16a_translate_done"]},
        )
        assert _get(TID).json()["subtitles_en"]["status"] == "missing"

    def test_cues_truncated_over_limit(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        segments = [
            {"index": i, "start_ms": i, "end_ms": i + 1, "text_zh": f"第{i}条"}
            for i in range(1, 601)
        ]
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"subtitle_segments_zh": segments},
        )
        body = _get(TID).json()["subtitles_zh"]
        assert body["status"] == "ok"
        assert body["count"] == 600
        assert len(body["cues"]) == 500
        assert body["cues_truncated"] is True


# ---------------------------------------------------------------------------
# 局部降级与安全边界
# ---------------------------------------------------------------------------

class TestPartialFailures:
    def test_invalid_json_isolated_to_one_section(self, ai_env, monkeypatch):
        storyline = _full_storyline()
        storyline["search_web_topic.json"] = "{bad json"
        make_task(ai_env, TID, storyline=storyline)
        monkeypatch.setattr(
            console_service, "_fetch_checkpoint_values", lambda tid: dict(CHECKPOINT_OK)
        )
        body = _get(TID).json()
        assert body["web_topic"]["status"] == "invalid"
        assert body["template"]["status"] == "ok"
        assert body["script"]["status"] == "ok"
        assert body["subtitles_zh"]["status"] == "ok"

    def test_checkpoint_unavailable_only_subtitles_down(self, ai_env, monkeypatch):
        make_task(ai_env, TID, storyline=_full_storyline())

        def boom(tid):
            raise RuntimeError("PG down")

        monkeypatch.setattr(console_service, "_fetch_checkpoint_values", boom)
        resp = _get(TID)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["subtitles_zh"]["status"] == "unavailable"
        assert body["subtitles_en"]["status"] == "unavailable"
        assert "PG down" in body["subtitles_zh"]["reason"]
        assert body["web_topic"]["status"] == "ok"
        assert body["template"]["status"] == "ok"
        # script 区依赖 checkpoint 的 done 标签判完成:checkpoint 不可读 →
        # 无法核验 → unavailable(不误判 failed)
        assert body["script"]["status"] == "unavailable"
        assert "PG down" in body["script"]["reason"]

    def test_invalid_thread_id_400(self, ai_env):
        resp = client.get("/api/console/runs/not-a-valid-tid/content-details")
        assert resp.status_code == 400

    def test_task_not_found_404(self, ai_env):
        resp = _get(TID)
        assert resp.status_code == 404
        assert "任务不存在" in resp.json()["detail"]

    def test_dir_without_console_run_json_404(self, ai_env):
        # outputs 下游离 storyline 目录(无 console_run.json)→ 一律 404
        make_task(ai_env, TID)
        (ai_env / TID / "console_run.json").unlink()
        assert _get(TID).status_code == 404

    def test_corrupted_record_500(self, ai_env):
        make_task(ai_env, TID)
        (ai_env / TID / "console_run.json").write_text("{bad", encoding="utf-8")
        assert _get(TID).status_code == 500

    def test_two_tasks_isolation(self, ai_env, monkeypatch):
        doc_a = dict(WEB_TOPIC_OK)
        doc_b = dict(WEB_TOPIC_OK)
        doc_b["query"] = "另一个任务的检索词"
        doc_b["search_web_topic"] = [
            {
                "url": "https://other.example.com/x",
                "title": "另一个任务的条目",
                "snippet": "s",
                "source": "duckduckgo_html",
            }
        ]
        make_task(ai_env, TID, storyline={"search_web_topic.json": doc_a})
        make_task(ai_env, TID2, storyline={"search_web_topic.json": doc_b})
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: dict(CHECKPOINT_OK) if tid == TID else {},
        )
        body_a = _get(TID).json()
        body_b = _get(TID2).json()
        assert body_a["web_topic"]["query"] == "马来西亚 清真寺 vlog"
        assert body_a["web_topic"]["entries"][0]["domain"] == "travel.example.com"
        assert body_b["web_topic"]["query"] == "另一个任务的检索词"
        assert body_b["web_topic"]["entries"][0]["domain"] == "other.example.com"


# ---------------------------------------------------------------------------
# 2026-11-20 第九期:英文话题检索区块(web_topic_en,审查修订版 §10.5)
# ---------------------------------------------------------------------------
WEB_TOPIC_EN_OK = {
    "schema_version": 2,
    "status": "ok",
    "engine": "ReachMCP(海外检索)",
    "provider": "reach_mcp",
    "query": "portable power station camping",
    "searched_at": "2026-11-20T06:00:00+00:00",
    "search_web_topic": [
        {
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "Portable power station review",
            "snippet": "camping power",
            "source": "youtube",
            "reach_source": "youtube",
            "date": "2026-11-18",
            "engagement_views": 112831,
        },
        {
            "url": "javascript:alert(1)",
            "title": "bad url",
            "snippet": "",
            "source": "web",
            "reach_source": "web",
        },
    ],
    "diagnostics": {"source_summary": "youtube:3 | EMPTY: reddit"},
}


class TestWebTopicEn:
    def test_ok_with_entries_normalized(self, ai_env):
        make_task(ai_env, TID, storyline={"search_web_topic_en.json": WEB_TOPIC_EN_OK})
        sec = _get(TID).json()["web_topic_en"]
        assert sec["status"] == "ok"
        assert sec["engine"] == "ReachMCP(海外检索)"
        assert sec["query"] == "portable power station camping"
        assert sec["source_summary"] == "youtube:3 | EMPTY: reddit"
        first = sec["entries"][0]
        assert first["domain"] == "www.youtube.com"
        assert first["reach_source"] == "youtube"
        assert first["date"] == "2026-11-18"
        assert first["engagement_views"] == 112831

    def test_bad_entry_url_still_clamped(self, ai_env):
        """非法 URL 条目不过滤(节点层已过滤,这里是防线),但 domain 解析安全。"""
        make_task(ai_env, TID, storyline={"search_web_topic_en.json": WEB_TOPIC_EN_OK})
        sec = _get(TID).json()["web_topic_en"]
        assert sec["entries"][1]["domain"] in ("", None)
        assert sec["entries"][1]["reach_source"] == "web"

    def test_skipped_from_artifact(self, ai_env):
        doc = {
            "schema_version": 2,
            "status": "skipped",
            "engine": "ReachMCP(海外检索)",
            "message": "英文线开关关闭(STORYLINE_ENABLE_WEB_TOPIC_EN=false)",
            "search_web_topic": [],
        }
        make_task(ai_env, TID, storyline={"search_web_topic_en.json": doc})
        sec = _get(TID).json()["web_topic_en"]
        assert sec["status"] == "skipped"
        assert "开关关闭" in sec["reason"]

    def test_failed_with_reason_and_summary(self, ai_env):
        doc = {
            "schema_version": 2,
            "status": "failed",
            "engine": "ReachMCP(海外检索)",
            "error_code": "NETWORK_ERROR",
            "message": "reach-mcp request timed out",
            "search_web_topic": [],
            "diagnostics": {"source_summary": "ERRORS: web, reddit"},
        }
        make_task(ai_env, TID, storyline={"search_web_topic_en.json": doc})
        sec = _get(TID).json()["web_topic_en"]
        assert sec["status"] == "failed"
        assert "timed out" in sec["reason"]
        assert sec["source_summary"] == "ERRORS: web, reddit"

    def test_no_results_reason(self, ai_env):
        doc = {
            "schema_version": 2,
            "status": "no_results",
            "engine": "ReachMCP(海外检索)",
            "search_web_topic": [],
        }
        make_task(ai_env, TID, storyline={"search_web_topic_en.json": doc})
        sec = _get(TID).json()["web_topic_en"]
        assert sec["status"] == "no_results"
        assert sec["reason"]

    def test_skipped_tag_evidence_when_no_file(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        # 无产物但 status_log 有英文 skipped 标签(经 checkpoint 假 values 注入)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {"status_log": ["storyline_search_web_topic_en_skipped"]},
        )
        sec = _get(TID).json()["web_topic_en"]
        assert sec["status"] == "skipped"

    def test_failed_tag_evidence_when_no_file(self, ai_env, monkeypatch):
        make_task(ai_env, TID)
        monkeypatch.setattr(
            console_service,
            "_fetch_checkpoint_values",
            lambda tid: {
                "status_log": ["storyline_search_web_topic_en_failed"],
                "error_log": [
                    "[storyline:search_web_topic_en] TOOL_EXECUTION_FAILED: RuntimeError('mcp down')"
                ],
            },
        )
        sec = _get(TID).json()["web_topic_en"]
        assert sec["status"] == "failed"
        assert "mcp down" in sec["reason"]

    def test_missing_when_completed_no_evidence(self, ai_env):
        make_task(ai_env, TID)
        assert _get(TID).json()["web_topic_en"]["status"] == "missing"

    def test_pending_when_running_no_evidence(self, ai_env, monkeypatch):
        monkeypatch.setattr(
            console_service, "reconcile_run_state", lambda record, tid: "running"
        )
        make_task(ai_env, TID, status="running")
        assert _get(TID).json()["web_topic_en"]["status"] == "pending"

    def test_invalid_json_isolated_to_en_section(self, ai_env):
        make_task(
            ai_env,
            TID,
            storyline={
                "search_web_topic_en.json": "{not json",
                "search_web_topic.json": WEB_TOPIC_OK,
            },
        )
        body = _get(TID).json()
        assert body["web_topic_en"]["status"] == "invalid"
        assert body["web_topic"]["status"] == "ok", "中文区块不受英文产物损坏影响"

    def test_unknown_status_is_invalid(self, ai_env):
        doc = {"schema_version": 2, "status": "weird", "search_web_topic": []}
        make_task(ai_env, TID, storyline={"search_web_topic_en.json": doc})
        assert _get(TID).json()["web_topic_en"]["status"] == "invalid"

    def test_old_task_without_en_file_keeps_zh_block(self, ai_env, monkeypatch):
        """旧任务只有中文产物:英文区块 missing,中文区块不受影响。"""
        make_task(ai_env, TID, storyline=_full_storyline())
        monkeypatch.setattr(
            console_service, "_fetch_checkpoint_values", lambda tid: dict(CHECKPOINT_OK)
        )
        body = _get(TID).json()
        assert body["web_topic"]["status"] == "ok"
        assert body["web_topic_en"]["status"] == "missing"
        assert body["web_topic_en"]["entries"] == []

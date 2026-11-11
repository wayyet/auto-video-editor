"""Artifact 生产/消费清单测试(2026-11-12 noop 治理阶段0,设计文档 §6 阶段0 任务5)。

目的:把"谁生产、谁消费"从口头约定变成测试锁定的契约。每个关键 artifact 在
``ARTIFACT_MANIFEST`` 里声明生产节点与消费者节点;用源码断言生产者模块**写**
该 state 键、消费者模块**读**该键,防止:
- 生产者改名/改字段后消费者静默失明(读取键消失,下游拿到 None 继续跑);
- 误把注释提及当成真实消费关系(本测试用 AST 剥掉 docstring/注释后匹配,
  模块 docstring 里出现键名**不算**证据 —— 2026-11-11 普查已因此误判过)。

判定口径:节点统一以 ``state.get("<key>")`` / ``_read_json_artifact(state, "<key>")``
/ ``{"<key>": str(path)}`` 形式读写键,代码体(非 docstring)中出现该字符串
字面量即视为证据。粒度是"模块级"而非"行级",足够拦截重命名漂移。

后续扩展(设计文档既定):阶段1 已追加 ``storyline_script_template_artifact``
(producer=STR 节点,consumer=generate_script 节点)。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: artifact 键 → 生产/消费清单(阶段0 实测:全仓 grep 区分真实代码读写与注释提及)。
ARTIFACT_MANIFEST: dict[str, dict[str, list[str]]] = {
    # 2026-11-12 阶段1:模板推荐节点产 artifact,generate_script 消费(注入 prompt)
    "storyline_script_template_artifact": {
        "producers": ["nodes/storyline/node_script_template_recommendation.py"],
        "consumers": ["nodes/storyline/node_generate_script.py"],
    },
    "storyline_script_artifact": {
        "producers": ["nodes/storyline/node_generate_script.py"],
        "consumers": [
            "nodes/node_08_add_subtitles.py",
            "nodes/storyline/node_generate_voiceover.py",
            "nodes/storyline/node_plan_timeline_pro.py",
            "nodes/storyline/node_recommend_text.py",
            "nodes/storyline/node_select_bgm.py",
        ],
    },
    "storyline_ai_transition_artifact": {
        "producers": ["nodes/storyline/node_generate_ai_transition.py"],
        "consumers": ["nodes/storyline/node_plan_timeline_ai_transition.py"],
    },
    "storyline_timeline_plan": {
        # plan_timeline_pro 首写;开启 AI 转场时 PTAT 覆写指向 timeline_plan_ai.json
        "producers": [
            "nodes/storyline/node_plan_timeline_pro.py",
            "nodes/storyline/node_plan_timeline_ai_transition.py",
        ],
        "consumers": [
            "nodes/storyline/qa_gate.py",
            "nodes/storyline/node_render_video.py",
            "nodes/storyline/join_storyline.py",
        ],
    },
}


def _code_string_literals(rel_path: str) -> set[str]:
    """模块代码体里的字符串字面量集合(剥离 module/class/function docstring)。"""
    path = ROOT / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""  # 就地清空 docstring,使其不再参与匹配
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
    return literals


@pytest.mark.parametrize("key", sorted(ARTIFACT_MANIFEST))
def test_manifest_producers_and_consumers(key: str) -> None:
    """每个 manifest 条目:生产者模块代码体写键、消费者模块代码体读键。"""
    entry = ARTIFACT_MANIFEST[key]
    for rel in entry["producers"] + entry["consumers"]:
        assert (ROOT / rel).is_file(), f"manifest 里的模块不存在:{rel}(节点是否被改名/移动?)"

    missing_producers = [
        rel for rel in entry["producers"] if key not in _code_string_literals(rel)
    ]
    assert not missing_producers, (
        f"{key} 的生产者代码体里找不到该键(生产点被改名或移走):{missing_producers}"
    )

    missing_consumers = [
        rel for rel in entry["consumers"] if key not in _code_string_literals(rel)
    ]
    assert not missing_consumers, (
        f"{key} 的消费者代码体里找不到该键(读取点被改名/移走,下游将静默拿 None):{missing_consumers}"
    )


def test_manifest_covers_expected_keys() -> None:
    """阶段0 锁定的三个键 + 阶段1 追加的模板 artifact 在册(阶段2 继续扩表)。"""
    assert {
        "storyline_script_artifact",
        "storyline_ai_transition_artifact",
        "storyline_timeline_plan",
        "storyline_script_template_artifact",
    } <= set(ARTIFACT_MANIFEST)

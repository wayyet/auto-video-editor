"""jy_common.prompt_core 共享底层策略矩阵测试（冗余模块合并阶段 2B）。

阶段 2B 把两套 Prompt Facade 的底层实现收敛到 ``jy_common.prompt_core``，
语义差异由策略参数承载。本文件直接测共享底层的策略矩阵
（严格/宽松 × 单/双花括号 × 命中/缺失），并抽查
「Facade 输出 == 对应策略的 core 输出」等价性。

两套 Facade 的端到端行为回归（真实 48 份模板 + 重构前 golden）见
``tests/unit/test_dual_prompt_loader_semantics.py``，2B 后必须原样通过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jy_common import prompt_core


# ---------------------------------------------------------------------------
# substitute：策略矩阵（syntax × missing × 命中/缺失）
# ---------------------------------------------------------------------------


def test_raw_double_hit_replaces() -> None:
    assert (
        prompt_core.substitute("A {{v1}} B", {"v1": 1}, syntax="raw_double", missing="raise")
        == "A 1 B"
    )


def test_raw_double_missing_raises_with_raw_group() -> None:
    # 捕获组按原文查键：空白不容忍，键是 " v1 "
    with pytest.raises(KeyError) as excinfo:
        prompt_core.substitute("{{ v1 }}", {"v1": 1}, syntax="raw_double", missing="raise")
    assert excinfo.value.args[0] == " v1 "


def test_raw_double_missing_keep() -> None:
    out = prompt_core.substitute("A {{nope}} B", {}, syntax="raw_double", missing="keep")
    assert out == "A {{nope}} B"


def test_dual_double_and_single_hit() -> None:
    tpl = "A {{v1}} B {v2} C {v3:?}"
    out = prompt_core.substitute(tpl, {"v1": "1", "v2": "2"}, syntax="dual", missing="keep")
    assert out == "A 1 B 2 C {v3:?}"


def test_dual_tolerates_whitespace() -> None:
    out = prompt_core.substitute("{{ v1 }}", {"v1": "1"}, syntax="dual", missing="keep")
    assert out == "1"


def test_dual_missing_keeps_placeholder_raw() -> None:
    tpl = '{"k": "{v2}"} {{v1}}'
    out = prompt_core.substitute(tpl, {}, syntax="dual", missing="keep")
    assert out == tpl  # JSON 字面量的单花括号与双花括号占位符都原样保留


def test_dual_missing_raise() -> None:
    with pytest.raises(KeyError) as excinfo:
        prompt_core.substitute("A {{v1}}", {}, syntax="dual", missing="raise")
    assert excinfo.value.args[0] == "v1"


def test_substitute_values_are_strConverted() -> None:
    assert prompt_core.substitute("{{n}}", {"n": 7}, syntax="dual", missing="keep") == "7"


def test_substitute_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError):
        prompt_core.substitute("x", {}, syntax="bogus")
    with pytest.raises(ValueError):
        prompt_core.substitute("x", {}, missing="bogus")


# ---------------------------------------------------------------------------
# 路径解析 / UTF-8 读取 / 缓存
# ---------------------------------------------------------------------------


def test_template_path_joins_task_lang_part() -> None:
    p = prompt_core.template_path(Path("/root"), "filter_clips", "zh", "system")
    assert p == Path("/root") / "filter_clips" / "zh" / "system.md"


def test_read_template_text_missing_raise(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        prompt_core.read_template_text(tmp_path / "a" / "b.md", missing_file="raise")
    assert "Template not found:" in str(excinfo.value)


def test_read_template_text_missing_empty(tmp_path: Path) -> None:
    assert prompt_core.read_template_text(tmp_path / "a" / "b.md", missing_file="empty") == ""


def test_read_template_text_utf8_chinese(tmp_path: Path) -> None:
    p = tmp_path / "t.md"
    p.write_text("中文内容 {{v}}", encoding="utf-8")
    assert prompt_core.read_template_text(p) == "中文内容 {{v}}"


def test_cached_returns_same_object_until_cleared() -> None:
    cache: dict = {}
    calls = []
    counter = {"n": 0}

    def loader() -> str:
        counter["n"] += 1
        value = f"v{counter['n']}"
        calls.append(value)
        return value

    first = prompt_core.cached(cache, "k", loader)
    second = prompt_core.cached(cache, "k", loader)
    assert first is second  # 命中缓存，同一对象
    assert counter["n"] == 1
    cache.clear()
    third = prompt_core.cached(cache, "k", loader)
    assert third == "v2" and counter["n"] == 2


# ---------------------------------------------------------------------------
# unresolved_placeholders
# ---------------------------------------------------------------------------


def test_unresolved_dedupes_and_keeps_order() -> None:
    text = "{{b}} {a} {{a}} {{b}} {{c}}"
    assert prompt_core.unresolved_placeholders(text) == ["b", "a", "c"]


def test_unresolved_ignores_single_braces_and_empty() -> None:
    assert prompt_core.unresolved_placeholders('{"k": 1}') == []
    assert prompt_core.unresolved_placeholders("") == []
    assert prompt_core.unresolved_placeholders(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Facade == core 等价性抽查（策略与 Facade 现行用法一一对应）
# ---------------------------------------------------------------------------


def test_openstoryline_facade_equals_raw_double_raise(tmp_path: Path) -> None:
    from open_storyline.utils import prompts as osl_prompts

    part_dir = tmp_path / "tasks" / "t" / "zh"
    part_dir.mkdir(parents=True)
    (part_dir / "user.md").write_text("A {{v1}} B {{ v2 }}", encoding="utf-8")

    builder = osl_prompts.PromptBuilder(prompts_dir=tmp_path / "tasks")
    variables = {"v1": "1", " v2 ": "2"}  # 严格语义：空白组名也必须命中
    facade_out = builder.render("t", "user", lang="zh", **variables)
    core_out = prompt_core.substitute(
        "A {{v1}} B {{ v2 }}", variables, syntax="raw_double", missing="raise"
    )
    assert facade_out == core_out == "A 1 B 2"


def test_storyline_facade_equals_dual_keep(tmp_path: Path) -> None:
    import storyline_capabilities.prompts as sc_prompts

    root = tmp_path / "tasks"
    (root / "t" / "zh").mkdir(parents=True)
    (root / "t" / "zh" / "x.md").write_text("A {{v1}} B {v2}", encoding="utf-8")

    facade_out = sc_prompts.render_prompt(
        "t", "x", lang="zh", prompts_root=root, v1="1"
    )
    core_out = prompt_core.substitute(
        "A {{v1}} B {v2}", {"v1": "1"}, syntax="dual", missing="keep"
    )
    assert facade_out == core_out == "A 1 B {v2}"


def test_facade_regexes_alias_core_patterns() -> None:
    import storyline_capabilities.prompts as sc_prompts

    assert sc_prompts._PLACEHOLDER_RE is prompt_core.DUAL_SYNTAX_RE
    assert sc_prompts._UNRESOLVED_RE is prompt_core.UNRESOLVED_DOUBLE_RE

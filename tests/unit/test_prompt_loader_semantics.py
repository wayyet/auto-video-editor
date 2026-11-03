"""提示词 Loader 语义回归(单 Loader 口径;单流水线架构 2026-11-04 阶段2)。

验证目标:旧聊天运行时 Loader ``open_storyline.utils.prompts`` 已随
``src/open_storyline/`` 删除,项目收敛为唯一 Loader
``storyline_capabilities.prompts``(数据目录 ``prompts/tasks/``,48 份模板)。
重构前的双 Loader golden 基线已按单口径重录
(``scripts/capture_prompt_golden.py``),本测试逐文件复算比对,
并钉住保留 Loader 的现行语义契约(不得改变):

==========================  =================================
行为                        storyline_capabilities.prompts
==========================  =================================
文件缺失                    返回空串
变量格式                    ``{{name}}``、``{name}``、``{name:?}``
占位符内空白                容忍(strip 后匹配)
缺失变量                    保留占位符原样
缓存                        无缓存,直接读盘
==========================  =================================

历史说明:删除前存在两套 Loader(严格 ``{{name}}`` + KeyError vs 保留侧
宽松三语法 + 原样保留),其语义矩阵见 git 历史
``tests/unit/test_dual_prompt_loader_semantics.py``(随旧运行时删除)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import storyline_capabilities.prompts as sc_prompts

REPO_ROOT = Path(__file__).resolve().parents[2]

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "prompt_render_golden.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def golden() -> dict:
    assert GOLDEN_PATH.exists(), "golden 基线缺失,请先运行 scripts/capture_prompt_golden.py"
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# golden 比对(重构前后都必须逐字节一致)
# ---------------------------------------------------------------------------


def test_golden_manifest_shape(golden: dict) -> None:
    assert golden["meta"]["file_count"] == 48
    assert len(golden["files"]) == 48


def test_shared_resource_files_unchanged(golden: dict) -> None:
    """prompts/tasks 的 48 份模板与 golden 逐字节一致。"""
    for entry in golden["files"]:
        raw = (REPO_ROOT / entry["rel"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha_template"], entry["rel"]


def test_loader_outputs_match_golden(golden: dict) -> None:
    """唯一 Loader 的加载/渲染输出与 golden 完全一致。"""
    for entry in golden["files"]:
        kind, lang, part = entry["kind"], entry["lang"], entry["part"]
        variables = entry["variables"]

        loaded_sc = sc_prompts.load_prompt(kind, part, lang=lang)
        assert _sha256_text(loaded_sc) == entry["sha_load_prompt"], entry["rel"]

        rendered_sc = sc_prompts.render_prompt(kind, part, lang=lang, **variables)
        assert _sha256_text(rendered_sc) == entry["sha_render_prompt"], entry["rel"]


# ---------------------------------------------------------------------------
# 语义矩阵:缺失文件
# ---------------------------------------------------------------------------


def test_missing_file_storyline_returns_empty() -> None:
    assert sc_prompts.load_prompt("no_such_kind", "nope", lang="zh") == ""


# ---------------------------------------------------------------------------
# 语义矩阵:占位符语法(用合成模板,不依赖真实资源)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sc_root(tmp_path: Path) -> Path:
    root = tmp_path / "tasks"
    (root / "t" / "zh").mkdir(parents=True)
    return root


def _write_sc(root: Path, part: str, text: str) -> None:
    (root / "t" / "zh" / f"{part}.md").write_text(text, encoding="utf-8")


def test_storyline_supports_double_and_single_braces(sc_root: Path) -> None:
    _write_sc(sc_root, "x", "A {{v1}} B {v2} C {v3:?} D")
    out = sc_prompts.render_prompt(
        "t", "x", lang="zh", prompts_root=sc_root, v1="1", v2="2"
    )
    assert out == "A 1 B 2 C {v3:?} D"


def test_storyline_tolerates_whitespace_in_double_braces(sc_root: Path) -> None:
    _write_sc(sc_root, "x", "{{ v1 }}")
    out = sc_prompts.render_prompt("t", "x", lang="zh", prompts_root=sc_root, v1="1")
    assert out == "1"


def test_storyline_keeps_unprovided_placeholders_raw(sc_root: Path) -> None:
    _write_sc(sc_root, "x", "A {{v1}} B {v2}")
    out = sc_prompts.render_prompt("t", "x", lang="zh", prompts_root=sc_root)
    assert out == "A {{v1}} B {v2}"
    assert sc_prompts.unresolved_placeholders(out) == ["v1"]


def test_storyline_unresolved_only_counts_double_braces(sc_root: Path) -> None:
    _write_sc(sc_root, "x", '{"k": "{v2}"} {{v1}}')
    out = sc_prompts.render_prompt("t", "x", lang="zh", prompts_root=sc_root)
    # 单花括号常见于注入的 JSON 字面量,不计入残留
    assert sc_prompts.unresolved_placeholders(out) == ["v1"]


def test_storyline_empty_template_missing_file_renders_empty(sc_root: Path) -> None:
    out = sc_prompts.render_prompt("t", "missing", lang="zh", prompts_root=sc_root)
    assert out == ""


# ---------------------------------------------------------------------------
# 语义矩阵:缓存(保留侧直读文件,无模板缓存)
# ---------------------------------------------------------------------------


def test_storyline_loader_reads_file_freshly(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    (root / "t" / "zh").mkdir(parents=True)
    template = root / "t" / "zh" / "x.md"
    template.write_text("v1", encoding="utf-8")
    assert sc_prompts.load_prompt("t", "x", lang="zh", prompts_root=root) == "v1"
    template.write_text("v2", encoding="utf-8")
    assert sc_prompts.load_prompt("t", "x", lang="zh", prompts_root=root) == "v2"

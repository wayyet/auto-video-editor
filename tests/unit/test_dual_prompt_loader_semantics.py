"""双 Loader 提示词语义回归（冗余模块合并阶段 2A / 2B）。

验证目标：提示词物理资源统一到 ``prompts/tasks/``（2A）与两套 Facade 改为
共享底层策略参数化实现（2B）之后，两个调用方可观察行为**逐项不变**：

- golden 比对：``tests/unit/golden/prompt_render_golden.json`` 由
  ``scripts/capture_prompt_golden.py`` 在重构**前**（两份物理副本仍在、
  两套 Loader 各自独立实现时）采集，记录 48 份模板的物理副本 SHA 与
  两套 Loader 加载/渲染输出 SHA。本测试逐文件复算比对。
- 语义矩阵：缺失文件、占位符语法（双/单花括号、空白容忍）、缺失变量、
  缓存行为四类策略差异，按两套 Loader 的现行契约逐项断言。

两套 Loader 的现行语义契约（不得改变）：

==========================  ============================  ==========================
行为                        open_storyline.utils.prompts  storyline_capabilities.prompts
==========================  ============================  ==========================
文件缺失                    FileNotFoundError             返回空串
变量格式                    仅 ``{{name}}``（原始捕获）   ``{{name}}``、``{name}``、``{name:?}``
占位符内空白                不容忍（按原文查键）          容忍（strip 后匹配）
缺失变量                    KeyError（严格）              保留占位符原样
缓存                        PromptBuilder 模板缓存        无缓存，直接读盘
==========================  ============================  ==========================
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# open_storyline 包位于 <repo>/src；主项目测试路径默认不含 src，
# 仅为本测试模块注入（open_storyline 内部无与仓库根同名的顶层模块，无遮蔽风险）。
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import storyline_capabilities.prompts as sc_prompts  # noqa: E402
from open_storyline.utils import prompts as osl_prompts  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "prompt_render_golden.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def golden() -> dict:
    assert GOLDEN_PATH.exists(), "golden 基线缺失，请先运行 scripts/capture_prompt_golden.py"
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# golden 比对（重构前后都必须逐字节一致）
# ---------------------------------------------------------------------------


def test_golden_manifest_shape(golden: dict) -> None:
    assert golden["meta"]["file_count"] == 48
    assert len(golden["files"]) == 48
    for entry in golden["files"]:
        # 采集时两份物理副本逐字一致是整个重构的字节级前提
        assert entry["sha_shared_copy"] == entry["sha_storyline_copy"], entry["rel"]


def test_shared_resource_files_unchanged(golden: dict) -> None:
    """prompts/tasks 的 48 份模板与重构前 golden 逐字节一致。"""
    for entry in golden["files"]:
        raw = (REPO_ROOT / entry["rel"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha_shared_copy"], entry["rel"]


def test_dual_loader_outputs_match_golden(golden: dict) -> None:
    """两套 Loader 的加载/渲染输出与重构前 golden 完全一致。"""
    for entry in golden["files"]:
        kind, lang, part = entry["kind"], entry["lang"], entry["part"]
        variables = entry["variables"]

        loaded_sc = sc_prompts.load_prompt(kind, part, lang=lang)
        assert _sha256_text(loaded_sc) == entry["sha_load_prompt"], entry["rel"]

        rendered_sc = sc_prompts.render_prompt(kind, part, lang=lang, **variables)
        assert _sha256_text(rendered_sc) == entry["sha_render_prompt"], entry["rel"]

        rendered_osl = osl_prompts.get_prompt(f"{kind}.{part}", lang=lang, **variables)
        assert _sha256_text(rendered_osl) == entry["sha_get_prompt_vars"], entry["rel"]


# ---------------------------------------------------------------------------
# 语义矩阵：缺失文件
# ---------------------------------------------------------------------------


def test_missing_file_storyline_returns_empty() -> None:
    assert sc_prompts.load_prompt("no_such_kind", "nope", lang="zh") == ""


def test_missing_file_openstoryline_raises() -> None:
    with pytest.raises(FileNotFoundError):
        osl_prompts.get_prompt("no_such_kind.nope")


def test_missing_file_openstoryline_message_contains_path(tmp_path: Path) -> None:
    builder = osl_prompts.PromptBuilder(prompts_dir=tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        builder.render("t", "user", lang="zh")
    assert "t" in str(excinfo.value) and "user" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 语义矩阵：占位符语法（用合成模板，不依赖真实资源）
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
    # 单花括号常见于注入的 JSON 字面量，不计入残留
    assert sc_prompts.unresolved_placeholders(out) == ["v1"]


def test_storyline_empty_template_missing_file_renders_empty(sc_root: Path) -> None:
    out = sc_prompts.render_prompt("t", "missing", lang="zh", prompts_root=sc_root)
    assert out == ""


def test_openstoryline_ignores_single_braces(tmp_path: Path) -> None:
    builder = osl_prompts.PromptBuilder(prompts_dir=tmp_path / "tasks")
    part_dir = tmp_path / "tasks" / "t" / "zh"
    part_dir.mkdir(parents=True)
    (part_dir / "user.md").write_text("A {v2} B {{v1}}", encoding="utf-8")
    # 单花括号即使给了变量也不替换；双花括号正常替换
    assert builder.render("t", "user", lang="zh", v1="1", v2="2") == "A {v2} B 1"


def test_openstoryline_strict_on_missing_variable(tmp_path: Path) -> None:
    builder = osl_prompts.PromptBuilder(prompts_dir=tmp_path / "tasks")
    part_dir = tmp_path / "tasks" / "t" / "zh"
    part_dir.mkdir(parents=True)
    (part_dir / "user.md").write_text("A {{v1}} B {{v2}}", encoding="utf-8")
    with pytest.raises(KeyError):
        builder.render("t", "user", lang="zh", v1="1")


def test_openstoryline_no_whitespace_tolerance_raw_group(tmp_path: Path) -> None:
    builder = osl_prompts.PromptBuilder(prompts_dir=tmp_path / "tasks")
    part_dir = tmp_path / "tasks" / "t" / "zh"
    part_dir.mkdir(parents=True)
    (part_dir / "user.md").write_text("{{ v1 }}", encoding="utf-8")
    # 现行行为：捕获组含空白原样查键 → KeyError(' v1 ')，不是 KeyError('v1')
    with pytest.raises(KeyError) as excinfo:
        builder.render("t", "user", lang="zh", v1="1")
    assert excinfo.value.args[0] == " v1 "


def test_openstoryline_get_prompt_name_format() -> None:
    # name 必须是 "task.role" 两段式
    with pytest.raises(ValueError):
        osl_prompts.get_prompt("onlytask")


# ---------------------------------------------------------------------------
# 语义矩阵：缓存（open_storyline 侧有模板缓存；storyline 侧直读文件）
# ---------------------------------------------------------------------------


def test_openstoryline_prompt_builder_caches_template(tmp_path: Path) -> None:
    builder = osl_prompts.PromptBuilder(prompts_dir=tmp_path / "tasks")
    part_dir = tmp_path / "tasks" / "t" / "zh"
    part_dir.mkdir(parents=True)
    template = part_dir / "user.md"
    template.write_text("v1", encoding="utf-8")

    first = builder._load_template("t", "user", "zh")
    second = builder._load_template("t", "user", "zh")
    assert first is second  # 命中缓存，同一字符串对象
    assert builder._cache == {"t:user:zh": "v1"}

    template.write_text("v2", encoding="utf-8")
    assert builder._load_template("t", "user", "zh") == "v1"  # 缓存未失效
    builder._cache.clear()
    assert builder._load_template("t", "user", "zh") == "v2"  # 清缓存后重新读盘


def test_storyline_loader_reads_file_freshly(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    (root / "t" / "zh").mkdir(parents=True)
    template = root / "t" / "zh" / "x.md"
    template.write_text("v1", encoding="utf-8")
    assert sc_prompts.load_prompt("t", "x", lang="zh", prompts_root=root) == "v1"
    template.write_text("v2", encoding="utf-8")
    assert sc_prompts.load_prompt("t", "x", lang="zh", prompts_root=root) == "v2"

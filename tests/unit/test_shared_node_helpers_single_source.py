"""公共 node helper 的单一来源防回归测试(冗余合并计划 阶段 3)。

`nodes/_draft_io.py`(H1)、`nodes/_checkpoint.py`(H2)、
`nodes/storyline/_common.py`(H3)已分别收敛草稿读写、人工关卡与 Storyline
产物读盘的公共样板。本测试防止后续节点开发重新复制等价实现:

1. **静态单一来源**——AST 扫描 ``nodes/`` 生产目录,断言契约函数名全仓只在
   预期模块定义一次。docs 快照、``agent_gate/fixtures``、``tests`` fixtures
   一律排除在"生产实现"之外(它们是刻意保留的隔离副本,见计划 §3.3 D2)。
2. **写回目录语义**——`write_draft` 必须把草稿**目录**传给
   ``safe_write_draft``;`apply_and_write` 的完成标签与剪映告警顺序不变。
   这是 `_draft_io` 模块 docstring 里"入参陷阱"的行为级护栏。

关卡行为本身(①/② payload、resume)由 ``test_nodes_checkpoint_helper.py``
与各节点测试覆盖;本文件只锁"单一来源"与易错语义。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nodes._draft_io import apply_and_write, write_draft

REPO_ROOT = Path(__file__).resolve().parents[2]

# 契约函数 → 唯一允许的生产定义模块(相对仓库根,posix 风格)。
# 新增公共 helper 时在此登记;出现第二份生产定义即失败。
SINGLE_SOURCE: dict[str, str] = {
    # H1: nodes/_draft_io.py —— 草稿读盘/写回/剪映告警/写回+收尾
    "load_draft": "nodes/_draft_io.py",
    "write_draft": "nodes/_draft_io.py",
    "jianying_running_tags": "nodes/_draft_io.py",
    "apply_and_write": "nodes/_draft_io.py",
    "find_video_track": "nodes/_draft_io.py",
    "upsert_by_id": "nodes/_draft_io.py",
    # H2: nodes/_checkpoint.py —— 关卡 payload 与 resume 收尾
    "build_interrupt_payload": "nodes/_checkpoint.py",
    "post_resume": "nodes/_checkpoint.py",
    # H3: nodes/storyline/_common.py —— Storyline 产物读盘与状态追加
    "_read_json_artifact": "nodes/storyline/_common.py",
    "_read_groups": "nodes/storyline/_common.py",
    "_read_media": "nodes/storyline/_common.py",
    "resolve_video_input_paths": "nodes/storyline/_common.py",
    "append_status_tag": "nodes/storyline/_common.py",
    "append_error": "nodes/storyline/_common.py",
}

# 刻意保留的副本位置:不算生产实现,扫描时跳过(计划 §3.3 D2/§4 G-A10)。
NON_PRODUCTION_SEGMENTS = frozenset(
    {"docs", "agent_gate", "tests", "fixtures", "__pycache__"}
)


def _iter_nodes_python_files() -> list[Path]:
    """枚举 ``nodes/`` 生产 .py 文件(防御性排除非生产目录段)。"""
    root = REPO_ROOT / "nodes"
    return sorted(
        p
        for p in root.rglob("*.py")
        if not (set(p.parts) & NON_PRODUCTION_SEGMENTS)
    )


def _production_definition_sites(func_name: str) -> dict[Path, list[str]]:
    """返回 ``func_name`` 在 nodes/ 下的定义位置:文件 → 定义所在限定名列表。

    收集任意作用域的 FunctionDef/AsyncFunctionDef;若某文件里出现的是
    **嵌套**定义(例如闭包),限定名会体现所属链,便于失败信息定位。
    """

    class _Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.hits: list[str] = []

        def _visit_function(self, node: ast.AST) -> None:
            name = getattr(node, "name", "")
            if name == func_name:
                self.hits.append(".".join([*self.stack, name]))
            self.stack.append(name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

    sites: dict[Path, list[str]] = {}
    for path in _iter_nodes_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - 生产树不应有语法错误
            continue
        collector = _Collector()
        collector.visit(tree)
        if collector.hits:
            sites[path.relative_to(REPO_ROOT).as_posix()] = collector.hits
    return sites


@pytest.mark.parametrize("func_name", sorted(SINGLE_SOURCE))
def test_helper_is_defined_exactly_once_in_production(func_name: str) -> None:
    """契约 helper 在 nodes/ 生产目录只有一份定义,且位于预期模块。"""
    expected_module = SINGLE_SOURCE[func_name]
    sites = _production_definition_sites(func_name)
    assert sites, f"{func_name} 在 nodes/ 下没有任何定义,预期 {expected_module}"
    assert set(sites) == {expected_module}, (
        f"{func_name} 出现了第二份生产定义(应只用 {expected_module}):{sites}"
    )
    # 预期模块里的定义必须是模块级函数,而不是嵌套在别的函数里
    assert sites[expected_module] == [func_name], (
        f"{expected_module} 中 {func_name} 的定义位置异常:{sites[expected_module]}"
    )


def test_scan_excludes_documentation_and_fixture_copies() -> None:
    """docs 快照与测试 fixture 不计入生产定义(即使内容与生产逐字相同)。"""
    for protected in ("docs", "agent_gate", "tests"):
        scanned = [p for p in _iter_nodes_python_files() if protected in p.parts]
        assert scanned == [], f"扫描范围不应包含 {protected}/ 下的文件:{scanned}"


# ---------------------------------------------------------------------------
# 写回目录语义(计划 6.5 阶段 3 任务 3.3)
# ---------------------------------------------------------------------------
def test_write_draft_passes_draft_directory_not_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`write_draft` 必须把草稿**目录**交给 safe_write_draft(目录级双写)。"""
    captured: dict[str, object] = {}

    def fake_safe_write_draft(draft_dir, draft, duration_us=None):
        captured["draft_dir"] = draft_dir
        captured["draft"] = draft
        captured["duration_us"] = duration_us
        return {"written": [str(draft_dir / "draft_content.json")]}

    monkeypatch.setattr("nodes._draft_io.safe_write_draft", fake_safe_write_draft)
    draft_file = tmp_path / "draft_content.json"
    write_draft(draft_file, {"id": "x"}, duration_us=123)
    assert captured["draft_dir"] == tmp_path, (
        "safe_write_draft 的入参必须是草稿目录;传文件路径会让 draft_info.json 永远缺失"
    )
    assert captured["draft"] == {"id": "x"}
    assert captured["duration_us"] == 123


def test_apply_and_write_emits_done_tag_then_jianying_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """完成标签在前、剪映告警在后;无告警时只返回完成标签。"""

    def fake_safe_write_draft(draft_dir, draft, duration_us=None):
        return {"jianying_running": True}

    monkeypatch.setattr("nodes._draft_io.safe_write_draft", fake_safe_write_draft)
    tags = apply_and_write(
        tmp_path / "draft_content.json", {}, "node_08", done_tag="node_08_done"
    )
    assert tags == ["node_08_done", "[node_08] 剪映进程在跑,写入仍继续(告警不阻断)"]

    def quiet_safe_write_draft(draft_dir, draft, duration_us=None):
        return {"written": ["x"]}

    monkeypatch.setattr("nodes._draft_io.safe_write_draft", quiet_safe_write_draft)
    assert apply_and_write(
        tmp_path / "draft_content.json", {}, "node_08", done_tag="node_08_done"
    ) == ["node_08_done"]

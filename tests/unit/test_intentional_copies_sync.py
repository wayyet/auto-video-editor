"""刻意复制(B 级)的"防漂移"测试 + 已合并项的"别长回来"守卫。

下面几组代码是**故意**保留两份的(各自文件头注释写明了原因)。本测试不去合并它们,
只保证:两份一旦出现新的差异,CI 立刻失败,逼开发者在同一次提交里有意识地处理。

只用 ast / 文件读取,不 import 仓库代码,所以不依赖 ffmpeg / PIL / psycopg 等环境。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- 工具
def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _defs(rel: str, str_map: dict[int, int] | None = None) -> dict[str, str]:
    """{顶层名字: ast 指纹}。忽略 docstring、注释、空白和行号。"""
    tree = _strip_docstrings(ast.parse((ROOT / rel).read_text(encoding="utf-8")))
    if str_map:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = node.value.translate(str_map)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = ast.dump(node.value)
    return out


def _differing(a: dict[str, str], b: dict[str, str]) -> set[str]:
    return {name for name in a.keys() & b.keys() if a[name] != b[name]}


def _func_body(rel: str, func: str) -> list[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            return [ast.dump(stmt) for stmt in _strip_docstrings(node).body]
    raise AssertionError(f"{rel} 里找不到函数 {func}")


# --------------------------------------------------------------------------- B1 两个能力包
ASM, VED = "assembly_capabilities", "video_edit_capabilities"


def test_ffproc_copies_differ_only_in_require_ffmpeg():
    a, b = _defs(f"{ASM}/ffproc.py"), _defs(f"{VED}/ffproc.py")
    assert set(a) == set(b)
    assert _differing(a, b) == {"require_ffmpeg"}  # 只有报错文案不同


def test_fonts_copies_only_known_differences():
    a, b = _defs(f"{ASM}/fonts.py"), _defs(f"{VED}/fonts.py")
    assert set(b) - set(a) == {"covers_cjk", "diagnose"}  # video_edit 版是超集
    assert set(a) <= set(b)
    assert _differing(a, b) == {"_find_cjk_font"}  # video_edit 版多一步 CJK 字形校验


def test_file_sha256_copies_identical():
    a = _defs(f"{ASM}/timeline_ops.py")["file_sha256"]
    b = _defs(f"{VED}/timeline.py")["file_sha256"]
    assert a == b


# --------------------------------------------------------------------------- B5 agent_gate 自含副本
def test_agent_gate_fixture_vlm_client_in_sync():
    assert _defs("agent_gate/fixtures/storyline_capabilities/vlm_client.py") == _defs(
        "storyline_capabilities/vlm_client.py"
    )


def test_json_dumps_fallback_matches_db_connection():
    """agent_gate 的"纯 kit 兜底"必须和 db.connection.configure_json_dumps 的最后一句一致。"""
    kit = _func_body("agent_gate/runner.py", "_configure_json_dumps")[-1]
    main = _func_body("db/connection.py", "configure_json_dumps")[-1]
    assert kit == main


def test_connect_timeout_fallback_matches_db_connection():
    runner = (ROOT / "agent_gate/runner.py").read_text(encoding="utf-8")
    conn = (ROOT / "db/connection.py").read_text(encoding="utf-8")
    fallback = re.search(r"connect_timeout = (\d+)", runner)
    default = re.search(r"^DEFAULT_CONNECT_TIMEOUT_S = (\d+)", conn, re.M)
    assert fallback and default, "超时兜底的写法变了,请同步更新本测试"
    assert int(fallback.group(1)) == int(default.group(1))


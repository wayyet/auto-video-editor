r"""断连演练:PostgreSQL 不可用时,系统必须**明确报错**且**不落本地文件**。

对应方案 §7.2 验收用例 8:
> **断开 PostgreSQL**:服务明确报持久化失败,**不创建 `.sqlite`、不写回 JSON**。

演练内容
--------
1. ``build_graph()`` 未注入 checkpointer ⇒ ``RuntimeError``(不创建 saver)。
2. ``graph.get_checkpointer()`` 在库不可达时 ⇒ 建表失败并抛出**明确**错误。
3. ``ChatSession.save_state_atomic()`` 在库不可达时 ⇒ 抛异常,
   **不**写 ``session_state.json``。
4. ``SessionStore.save_session_state()`` 在库不可达时 ⇒ 抛异常
   (不再是"吞掉异常只打 warning"的静默丢数据)。
5. 全程检查:没有任何 ``.sqlite`` / ``session_state.json`` 被创建。

用法
----
```powershell
.\.venv\Scripts\python.exe scripts\drill_disconnect.py
```
退出码 0 = 全部通过;非 0 = 有演练项没通过。
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

#: 故意指向一个没人监听的端口 ⇒ 连接必然失败
DEAD_URI = "postgresql://postgres:devpass@127.0.0.1:59999/video_workflow"

_failures: list[str] = []
_passes: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        print(f"  ✓ {name}{(' — ' + detail) if detail else ''}")
    else:
        _failures.append(name)
        print(f"  ✗ {name}{(' — ' + detail) if detail else ''}")


def _sqlite_files() -> set[str]:
    """列出仓库里现存的 ``.sqlite`` / 侧文件(相对路径)。"""
    found: set[str] = set()
    for pattern in ("*.sqlite", "*.sqlite-wal", "*.sqlite-shm"):
        for p in ROOT.glob(pattern):
            found.add(str(p.relative_to(ROOT)))
    for p in ROOT.glob("checkpoints/*.sqlite*"):
        found.add(str(p.relative_to(ROOT)))
    return found


def _outputs_dir(cfg) -> Path | None:
    """取 ``cfg.project.outputs_dir``(取不到就返回 None)。"""
    try:
        return Path(cfg.project.outputs_dir)
    except Exception:  # noqa: BLE001
        return None


def _session_state_files(configured_dir: Path | None) -> list[str]:
    """列出**真实** outputs 目录下的 ``session_state.json``。

    演练要证明的是"库不可达时不写回 JSON"。而 ``ChatSession`` 用的
    ``cfg.project.outputs_dir`` 是真实路径(不是临时目录),所以必须扫真目录,
    只扫临时目录等于没测。
    """
    found: list[str] = []
    roots: list[Path] = []
    if configured_dir is not None:
        roots.append(Path(configured_dir))
    # 历史上 outputs 落在两处(方案 §1.3),两个都要扫。
    roots.append(ROOT / "outputs")
    roots.append(ROOT / "openstoryline" / "outputs")
    seen: set[Path] = set()
    for r in roots:
        try:
            resolved = r.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        for p in resolved.rglob("session_state.json"):
            found.append(str(p))
    return found


def main() -> int:
    os.environ["POSTGRES_URI"] = DEAD_URI
    os.environ["AGENT_GATE_POSTGRES_URI"] = DEAD_URI

    print("断连演练 —— PostgreSQL 不可达时的行为")
    print(f"目标 URI: {DEAD_URI}")
    print()

    # OpenStoryline 的配置与 prompt 模板都是**相对 cwd** 的 ⇒ 切到
    # ``openstoryline/``,和真实服务一致(见 ``_dummy_settings`` 的说明)。
    original_cwd = os.getcwd()
    openstoryline_dir = ROOT / "openstoryline"
    if openstoryline_dir.is_dir():
        os.chdir(openstoryline_dir)

    try:
        return _run_drill()
    finally:
        os.chdir(original_cwd)


def _run_drill() -> int:
    # 演练前的存量快照(仓库里可能还有历史遗留的 .sqlite)——最后比对"有没有新增"。
    sqlite_before = _sqlite_files()

    # --- 1) build_graph() 未注入 saver -------------------------------------
    print("1) build_graph() 未注入 checkpointer")
    try:
        from graph import build_graph

        build_graph()
        _check("未注入 saver ⇒ RuntimeError", False, "居然没抛异常")
    except RuntimeError as e:
        _check("未注入 saver ⇒ RuntimeError", True, str(e)[:70])
    except Exception as e:  # noqa: BLE001
        _check("未注入 saver ⇒ RuntimeError", False, f"抛的是 {type(e).__name__}")
    print()

    # --- 2) get_checkpointer() 在库不可达时失败 ------------------------------
    print("2) graph.get_checkpointer() 建表(库不可达)")
    try:
        from graph import get_checkpointer

        async def _body():
            async with get_checkpointer() as saver:
                return saver

        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        try:
            loop.run_until_complete(_body())
        finally:
            loop.close()
        _check("库不可达 ⇒ 明确报错", False, "居然连上了")
    except Exception as e:  # noqa: BLE001 — 任何异常都算"明确报错"
        _check("库不可达 ⇒ 明确报错", True, f"{type(e).__name__}: {str(e)[:60]}")
    print()

    # --- 3) ChatSession.save_state_atomic() 不写 JSON ------------------------
    print("3) ChatSession.save_state_atomic()(库不可达)")
    tmp = Path(tempfile.mkdtemp(prefix="drill_disconnect_"))
    try:
        from openstoryline.agent_fastapi import ChatSession

        cfg = _dummy_settings(tmp)
        out_dir = _outputs_dir(cfg)
        before = set(_session_state_files(out_dir))
        sess = ChatSession("a" * 32, cfg)
        try:
            sess.save_state_atomic()
            _check("保存失败 ⇒ 抛异常", False, "居然没抛异常")
        except Exception as e:  # noqa: BLE001
            _check("保存失败 ⇒ 抛异常", True, f"{type(e).__name__}: {str(e)[:60]}")

        written = list(tmp.rglob("session_state.json"))
        _check("未写回 session_state.json(临时目录)", not written, f"写了: {written}")

        after = set(_session_state_files(out_dir))
        new_files = sorted(after - before)
        _check(
            "未写回 session_state.json(真实 outputs 目录)",
            not new_files,
            f"新写了: {new_files}" if new_files else f"(已存在 {len(before)} 个旧文件,未新增)",
        )
    except Exception as e:  # noqa: BLE001 — 演练项自身出错也要报出来,不能崩掉整个脚本
        _check("ChatSession 演练(准备阶段)", False, f"{type(e).__name__}: {str(e)[:80]}")
    print()

    # --- 4) SessionStore.save_session_state() 不吞异常 -----------------------
    print("4) SessionStore.save_session_state()(库不可达)")
    try:
        from openstoryline.agent_fastapi import SessionStore

        cfg = _dummy_settings(tmp)
        store = SessionStore(cfg)
        sess = store._sessions.get("x") or None
        try:
            from openstoryline.agent_fastapi import ChatSession

            sess = ChatSession("b" * 32, cfg)
        except Exception:  # noqa: BLE001
            sess = None
        if sess is None:
            _check("保存失败 ⇒ 抛异常", False, "构造 ChatSession 失败")
        else:
            try:
                asyncio.run(store.save_session_state(sess))
                _check("保存失败 ⇒ 抛异常(不再静默吞掉)", False, "居然没抛异常")
            except Exception as e:  # noqa: BLE001
                _check("保存失败 ⇒ 抛异常(不再静默吞掉)", True, f"{type(e).__name__}: {str(e)[:60]}")
    except Exception as e:  # noqa: BLE001 — 演练项自身出错也要报出来
        _check("SessionStore 演练(准备阶段)", False, f"{type(e).__name__}: {str(e)[:80]}")
    print()

    # --- 5) 没有本地文件被创建 ----------------------------------------------
    print("5) 本地文件检查")
    # 比对"演练前"与"演练后":仓库里本来就有历史遗留的 .sqlite(阶段 E 归档前),
    # 只看**新增**才算真的落盘了。
    new_sqlite = sorted(_sqlite_files() - sqlite_before)
    _check(
        "没有新建 .sqlite 文件",
        not new_sqlite,
        f"新增: {new_sqlite}" if new_sqlite else f"(存量 {len(sqlite_before)} 个,未新增)",
    )
    print()
    print("=" * 64)
    print(f"通过 {len(_passes)} 项,失败 {len(_failures)} 项")
    if _failures:
        for f in _failures:
            print(f"  失败: {f}")
        return 1
    print("断连演练通过:持久化失败时明确报错,且不落本地文件。")
    return 0


def _dummy_settings(tmp: Path):
    """构造一份可用的 ``Settings``。

    ⚠️ OpenStoryline 的路径全是**相对当前工作目录**的:
    - ``default_config_path()`` → ``"config.toml"``
    - prompt 模板 → ``prompts\\tasks\\instruction\\<lang>\\system.md``

    真实服务就是以 ``openstoryline/`` 为 cwd 启动的(见
    ``tests/unit/test_session_store_repository.py`` 的 ``_chdir_openstoryline``)。
    演练脚本从仓库根目录跑 ⇒ 必须自己 chdir,否则配置和模板都找不到。

    拿不到配置时**明确报错**,不静默返回 None。
    """
    from openstoryline.agent_fastapi import load_settings

    config_path = ROOT / "openstoryline" / "config.toml"
    if not config_path.is_file():
        raise RuntimeError(f"找不到 OpenStoryline 配置文件: {config_path}")
    return load_settings(str(config_path))


if __name__ == "__main__":
    sys.exit(main())

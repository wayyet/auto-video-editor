"""统一任务 ID 解析(jy_common/job_identity.py)单测 —— 审查文档阶段 2 用例 2.1-2.5。

2026-11-18 job_id 链路缺陷修复:node_09 / node_10 / node_10b 三节点共用
``resolve_job_id``,解析优先级 = ``config["configurable"]["thread_id"]``(LangGraph
执行上下文,权威)→ ``state.thread_id``(显式携带)→ ``state.session_id``(旧
checkpoint 兼容)→ 中文草稿目录名(剥 ``AutoVideo_`` 前缀)→ 显式 ``ValueError``。

回归守卫:绝不把英文草稿目录的上级名(草稿库根名 ``com.lveditor.draft``)或
固定 ``unknown`` 当成任务 ID —— 旧缺陷的 seed 恒定来源。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jy_common.job_identity import resolve_job_id


def test_config_thread_id_wins_over_state_ids() -> None:
    """2.1 config 权威:config.thread_id 与 state 内两个 ID 故意不同值 → 取 config。"""
    state = {"thread_id": "state-thread", "session_id": "state-session"}
    config = {"configurable": {"thread_id": "console-from-config"}}
    assert resolve_job_id(state, config) == "console-from-config"


def test_state_thread_id_used_when_config_missing() -> None:
    """2.2 显式携带 state.thread_id 且无 config → 用它(兼容自定义调用方)。"""
    assert resolve_job_id({"thread_id": "console-t1"}) == "console-t1"


def test_legacy_checkpoint_session_id() -> None:
    """2.3 旧 checkpoint 兼容:只有 session_id 也命中,且不走草稿路径兜底。"""
    state = {
        "session_id": "console-legacy",
        "draft_path": "/x/AutoVideo_someother/draft_content.json",
    }
    assert resolve_job_id(state) == "console-legacy"


def test_draft_path_fallback_strips_autovideo_prefix(tmp_path: Path) -> None:
    """2.4 末级兜底:剥 AutoVideo_ 前缀对齐 thread_id 语义;非约定前缀原样使用。"""
    draft = tmp_path / "com.lveditor.draft" / "AutoVideo_console-abc123" / "draft_content.json"
    assert resolve_job_id({"draft_path": str(draft)}) == "console-abc123"

    draft2 = tmp_path / "com.lveditor.draft" / "plain-name" / "draft_content.json"
    assert resolve_job_id({"draft_path": str(draft2)}) == "plain-name"


def test_en_branch_root_never_used_as_job_id(tmp_path: Path) -> None:
    """回归守卫:只有 draft_dir_en_branch(目录)而无 zh draft_path/ID → 显式失败,
    绝不静默取草稿库根名(旧 en 兜底 ``Path(目录).parent.name`` 的 seed 恒定来源)。"""
    en_dir = tmp_path / "com.lveditor.draft" / "en_branch_console-x"
    with pytest.raises(ValueError):
        resolve_job_id({"draft_dir_en_branch": str(en_dir)})


def test_all_sources_missing_raises() -> None:
    """2.5 四级全空 → ValueError;调用方按各自降级契约处理,不静默固定 seed。"""
    with pytest.raises(ValueError):
        resolve_job_id({})
    with pytest.raises(ValueError):
        resolve_job_id(None, None)
    with pytest.raises(ValueError):
        resolve_job_id({"thread_id": "  ", "session_id": None, "draft_path": ""})


def test_blank_config_thread_id_falls_through() -> None:
    """config.thread_id 为空白视为缺失,继续向后解析(不把空串当 ID)。"""
    config = {"configurable": {"thread_id": "   "}}
    assert resolve_job_id({"session_id": "console-fallback"}, config) == "console-fallback"


def test_non_mapping_config_guarded() -> None:
    """config 非 Mapping(防御):不炸,继续 state 解析。"""
    assert (
        resolve_job_id({"session_id": "console-x"}, config="not-a-mapping")
        == "console-x"
    )

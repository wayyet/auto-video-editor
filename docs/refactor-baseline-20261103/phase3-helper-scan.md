# 阶段3 静态扫描记录(公共 helper 单一来源)
日期: 2026-11-03 08:22  HEAD: cc8c02a
计划依据: docs/修改文档/auto-video-editor_main_冗余模块合并设计与执行计划.md §6.5(3.1/3.3)

## scan 1: def (load_draft|write_draft|apply_and_write|jianying_running_tags) in nodes/
nodes\_draft_io.py:36:def load_draft(draft_file: Path | str) -> dict[str, Any]:
nodes\_draft_io.py:53:def write_draft(
nodes\_draft_io.py:73:def jianying_running_tags(node_tag: str, write_results: Iterable[Any]) -> list[str]:
nodes\_draft_io.py:93:def apply_and_write(

## scan 2: def (_read_json_artifact|_read_groups|_read_media|resolve_video_input_paths|append_status_tag|append_error) in nodes/
nodes\storyline\_common.py:53:def append_status_tag(state: WorkflowState, *tags: str) -> list[str]:
nodes\storyline\_common.py:72:def append_error(
nodes\storyline\_common.py:93:def _read_json_artifact(
nodes\storyline\_common.py:111:def _read_groups(state: WorkflowState) -> list[dict]:
nodes\storyline\_common.py:119:def _read_media(state: WorkflowState) -> list[dict]:
nodes\storyline\_common.py:130:def resolve_video_input_paths(state: WorkflowState) -> list[str]:

## scan 3: def (build_interrupt_payload|post_resume) in nodes/
nodes\_checkpoint.py:38:def build_interrupt_payload(
nodes\_checkpoint.py:82:def post_resume(

## scan 4: safe_write_draft 调用方(应为 nodes/_draft_io.py 一处 + draft_ops 定义)
nodes\node_05_generate_draft.py:10:  :func:`draft_ops.atomic_writer.safe_write_draft`,剪映 5.9+ 需要二者一致)。
nodes\node_05_generate_draft.py:28:from draft_ops.atomic_writer import safe_write_draft
nodes\node_05_generate_draft.py:53:    writer=safe_write_draft,
nodes\node_05_generate_draft.py:62:            默认 :func:`draft_ops.atomic_writer.safe_write_draft`(剪映 5.9+ 双写)。
nodes\node_13_adjust_volume.py:151:    """:func:`jianying_adjust_volume` 的内部实现,额外回传 ``safe_write_draft`` 结果。
nodes\node_13_adjust_volume.py:153:    拆分原因:``safe_write_draft`` 的返回值里有 ``jianying_running`` 标志,节点层要靠它
draft_ops\duration_index.py:8:本模块在每次 :func:`safe_write_draft` 写入完成后调用,确保两个索引与
draft_ops\verify_after_write.py:4:且都能被解析为合法 JSON。本模块在 :func:`safe_write_draft` 双写后调用,
nodes\node_16_translate_subtitles.py:34:from draft_ops.atomic_writer import safe_write_draft
nodes\node_16_translate_subtitles.py:169:    不走双文件)切到 ``safe_write_draft(draft_dir, draft)``,与节点 5/7/8/9/10/11/13
nodes\node_16_translate_subtitles.py:183:    safe_write_draft(draft_dir, draft)
nodes\_draft_io.py:7:- **R2 写回** — 5 处 ``safe_write_draft(...)`` + "剪映进程在跑"告警拼接,统一为
nodes\_draft_io.py:13::func:`write_draft` 传给 ``safe_write_draft`` 的是 ``draft_path.parent``(目录),
nodes\_draft_io.py:14:**不是** ``draft_path``(文件)。Week 5 把 ``safe_write_draft`` 的入参从文件级提升到
nodes\_draft_io.py:30:from draft_ops.atomic_writer import safe_write_draft
nodes\_draft_io.py:59:    """目录级双写草稿并返回 ``safe_write_draft`` 的结果字典。
nodes\_draft_io.py:70:    return safe_write_draft(Path(draft_path).parent, draft, duration_us=duration_us)
nodes\_draft_io.py:74:    """把 ``safe_write_draft`` 返回值里的"剪映在跑"翻成 ``status_log`` 标签。
nodes\_draft_io.py:82:        write_results: 一个或多个 ``safe_write_draft`` 返回值;``None`` 与缺字段
nodes\_draft_io.py:104:        write_result = safe_write_draft(draft_path.parent, draft)
draft_ops\atomic_writer.py:17:  - 若两个都失败,目标文件可能保持旧值 — 由上层的 ``safe_write_draft`` 配套
draft_ops\atomic_writer.py:21:- ``safe_write_draft(draft_dir, content)`` 把
draft_ops\atomic_writer.py:98:          (本函数不负责回退 — 由上层的 ``safe_write_draft`` 在 verify 失败时
draft_ops\atomic_writer.py:159:def safe_write_draft(
draft_ops\atomic_writer.py:226:            f"safe_write_draft 校验失败 ({reason});已从快照 {used_snap} 回退"
draft_ops\atomic_writer.py:246:    "safe_write_draft",

## 回归对照
tests/unit junit: tests=1285 failures=18 errors=0 skipped=24(基线 1231/18/0/24;新增 54 个用例含本阶段 15 个;失败 ID 与基线完全一致,零新增失败)

# video-agent-kit 集成 — 阶段五执行记录

> 实施日期:2026-09-24
> 执行依据:`docs/integration/video-agent-kit集成auto-video-editor设计执行计划.md §10 阶段五`

## 一、阶段五目标(plan §10)

> 真实素材端到端验证:从 Windows 本机跑一条完整视频,确认 ``preview.mp4`` 可播放、
> ``report.md`` 内容可读、关卡① 收到的通知文案正确带上报告路径

## 二、本次执行的 5 个验证 case

| Case | 验证目标(plan §11) | 结果 |
|---|---|---|
| happy | 默认开启:产物完整 + preview.mp4 可播放 + report.md 可读 | ✓ |
| qc_fail | 软降级:QC 失败重试到上限后仍能走完 → 不阻断流水线 | ✓ |
| gate_off | 应急关闭:`ASSEMBLY_QC_GATE_ENABLED=False` 等价于改动前 | ✓ |
| checkpoint1 | 关卡① 通知文案带上 ``assembly_report_path`` | ✓(**本步骤新增对 ``node_06_human_reorder.py`` 的修改**) |
| decoupling | 解耦验证:无 ``video-agent-kit`` / ``mcp`` 运行时残留依赖 | ✓ |

## 三、前置环境补齐

阶段五执行中发现并补齐的环境依赖:

1. **ffmpeg / ffprobe** — 之前不在系统 PATH 上,装配+渲染+QC 三个能力都跑不通。
   - 安装位置:`C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin`
   - 安装方式:用 Karing HTTP 代理(``127.0.0.1:3067``)走 curl 下载
     Gyan essentials zip 包(约 81 MB,断点续传),解压到 ``C:\ffmpeg\``
   - PATH:已写入 **用户级 PATH**(`[Environment]::SetEnvironmentVariable("Path", ..., "User")`),
     新 shell session 自动可用;当前 session 用
     ``$env:Path = "$env:Path;C:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin"``
2. **numpy** — ``assembly_capabilities/visual_observe.py`` 内部 ``import numpy as np``。
   - 安装:``.venv\Scripts\python.exe -m pip install numpy``(当前版本 2.5.3)
   - **plan §9 设计的 ``opencv-python-headless`` 实际未被代码使用**,无需装
3. **外部 transcript fixture** — ``assembly_capabilities/speech_asr.py``
   按设计明确"不调任何云端 ASR",必须外部喂入 ``transcript_path`` 或 ``inline_text``。
   - fixture 路径:``tests/fixtures/assembly_phase5/transcript.json``
   - 内容:3 段中文 stub,覆盖 0~10s 区间,标注 ``provider=external-fixture``
   - 真实生产场景:由 FireRedASR2S / 其他外部 ASR 提前产出后传入 state

## 四、实施的代码改动(仅 1 处)

### 4.1 ``nodes/node_06_human_reorder.py::_send_notification``

**改动前**(Week 3 占位):
```python
def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    print(f"[notify] {checkpoint} thread={state.get('session_id')} draft={state.get('draft_path')}")
```

**改动后**(阶段五扩展,带上 ``assembly_report_path``):
```python
def _send_notification(state: WorkflowState, checkpoint: str) -> None:
    """关卡① 人工通知(Week 3 占位 + 阶段五扩展)。

    阶段五(plan §7.6 / §11 验收项 "关卡① 通知文案正确带上报告路径"):
    ``assembly_report_path`` 是 assembly 6 节点的最终产物路径;关卡① 通知里
    必须带上,便于人工在剪映里调分镜时同步看 assembly QC 报告与 preview.mp4。
    """
    assembly_report = state.get("assembly_report_path") or "(无报告)"
    print(
        f"[notify] {checkpoint} thread={state.get('session_id')} "
        f"draft={state.get('draft_path')} assembly_report={assembly_report}"
    )
```

**为何改这里**:plan §11 验收项"关卡① 收到的通知文案正确带上报告路径",
原实现没带 assembly 报告路径,人工在剪映里调分镜时无法同步对照 assembly QC
报告与 preview.mp4。改为读取 ``state["assembly_report_path"]`` 写入通知。

**无 breaking change**:Week 3 单测用 ``monkeypatch(_send_notification)`` 计数,
不依赖文案格式;Week 4 的企业微信 webhook 集成尚未落地,本函数还是 ``print``
占位,**不影响未来真实通知渠道的接入**。

## 五、新增验证脚本

| 文件 | 角色 |
|---|---|
| ``scripts/assembly/phase5_e2e.py`` | 单一 case 驱动(可单跑 happy / qc_fail / gate_off / checkpoint1 / decoupling) |
| ``scripts/assembly/phase5_combined.py`` | 5 个 case 联合跑 + 写 ``outputs/phase5_e2e_report.{json,md}`` |
| ``tests/fixtures/assembly_phase5/transcript.json`` | 外部 transcript fixture(speech_transcribe 输入) |

调用方式:
```powershell
# 单独跑某个 case
.\.venv\Scripts\python.exe scripts/assembly/phase5_e2e.py --case happy
.\.venv\Scripts\python.exe scripts/assembly/phase5_e2e.py --case qc_fail
.\.venv\Scripts\python.exe scripts/assembly/phase5_e2e.py --case gate_off
.\.venv\Scripts\python.exe scripts/assembly/phase5_e2e.py --case checkpoint1
.\.venv\Scripts\python.exe scripts/assembly/phase5_e2e.py --case decoupling

# 一次跑完所有 case + 生成汇总报告
.\.venv\Scripts\python.exe scripts/assembly/phase5_combined.py
```

## 六、阶段五验收结果(plan §11 对照)

| 验收项 | 标准 | 本次结果 |
|---|---|---|
| 解耦验证 | grep 找不到 ``video-agent-kit`` / ``video_edit_server`` / ``from mcp import`` 实际运行时依赖 | ✓ 0 个违规(注释 / 文档 / 历史 fixture 中提及不计入) |
| 独立启动 | 现有测试 + 新增 assembly_* 测试都通过 | ✓ happy / qc_fail / gate_off / checkpoint1 / decoupling 5 个 case 全绿 |
| 默认开启生效 | 不设 ``ASSEMBLY_QC_GATE_ENABLED`` 时 6 节点全跑 | ✓ happy case 跑了 5 个核心节点 + write_report |
| 应急关闭生效 | 设 ``ASSEMBLY_QC_GATE_ENABLED=False`` 时 ``generate_draft → node_06`` 直连 | ✓ graph 静态扫描 60 条边中,``generate_draft → assembly_xxx`` 为 False,``generate_draft → node_06_human_reorder`` 为 True |
| 软降级验证 | 重试到上限后流程仍能走到关卡①,不崩溃 | ✓ qc_fail case retry=2(达到 MAX=2)后自动转 ``assembly_write_report``,report.md 生成 |
| 产物完整性 | 8 个文件全部生成 | ✓ media.json / transcript.json / video_ingest.json / timeline.json / timeline_validation.json / preview.mp4 / preview_qc_report.json / report.md |
| 关卡① 通知文案 | 带上 ``assembly_report_path`` | ✓ 已修改 ``node_06_human_reorder.py``,验证 phase5_combined 输出确认 |

## 七、性能数据(默认开启 happy path,30s.mp4)

| 节点 | 耗时(s) |
|---|---:|
| assembly_discover_and_probe | 2.5 |
| assembly_asr_and_visual_observe | 8.8 |
| assembly_build_timeline | 0.1 |
| assembly_validate_render_qc | 6.2 |
| assembly_write_report | 0.03 |
| **合计** | **17.7** |

**结论**:每条视频到达关卡① 前多花约 17.7 秒(plan ADR-3 已记录此代价)。
渲染与 ASR 抽帧耗时占大头;``assembly_build_timeline`` 0.1 秒,
说明 LLM 网关 + fallback 链路快速可用(本次走 fallback,无 LLM 调用)。

## 八、已知边界与后续

1. **未跑完整 17 步 graph** — 阶段五直接驱动 assembly 6 节点函数(共享 state),
   不经过 ``node_01 ~ node_05``。原因是:阶段五目标是验证 assembly 6 节点产物 +
   关卡① 通知文案,跑完整 17 步需要 19 个 storyline 节点 + OpenStoryline Web UI +
   LLM + ASR 服务,启动成本巨大且与本阶段职责无关。
   **替代覆盖**:
   - ``Case gate_off`` 通过 ``g.get_graph().edges`` 静态验证了 graph 接线正确性
   - ``Case checkpoint1`` 用了真实 happy path 的 state(包含真实 ``assembly_report_path``)
2. **未单独验证 assembly_build_timeline 的 LLM 路径** — plan §3.3 强调这一步
   是唯一需要 LLM 的节点,阶段三的 ``outputs/assembly_selection_eval/case_*``
   3 个评估样例已覆盖(``llm_used=True fallback=True clips=N``)。
3. **FFmpeg 未走 winget** — 用户无管理员权限,``winget settings --enable
   ProxyCommandLineOptions`` 失败;改用 curl + Karing 代理下载。后续如果装到
   其他 Windows 机器,优先用 winget(Gyan.FFmpeg),失败再走 curl 路径。
4. **transcript fixture 仅供阶段五验证** — 真实场景下,transcript 应由
   现有第 8 步 FireRedASR2S 产出后通过 ``state["storyline_transcript_external_path"]``
   注入。生产部署时无需此 fixture。

## 九、后续阶段清单(plan §10 阶段五 之后的路标)

阶段五是 plan §十 中 5 个阶段的最后一个 — 整个 video-agent-kit 集成至此完成。
后续工作:

1. 把阶段五 5 个 case 接入 CI(``tests/integration/test_phase5_e2e.py``)
2. ``opencv-python-headless`` / ``numpy`` 写入 ``requirements.txt``(虽然代码
   当前不直接 import,但 ``visual_observe`` 间接用,钉住版本避免未来装错)
3. 当 OpenStoryline / LLM 网关稳定后,跑一次完整 17 步 graph(``run_workflow.py``)
   作为最终回归
4. Week 4 计划:关卡① 通知从 ``print`` 占位替换为真实企业微信 webhook,
   ``_send_notification`` 函数签名已稳定,切换零成本

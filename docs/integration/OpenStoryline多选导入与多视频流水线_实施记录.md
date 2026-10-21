# OpenStoryline「导入视频」多选支持 + 多视频接入剪辑流水线

> 实施记录。2026-10-20。
> 计划原文:`C:/Users/wayyet/.minimax/v2/sessions/2026/10/20/06-38-30-443-session_bXZzXzY1Y2FjMjM4MGQ3ODQ5NzU4OWQ5ZDYyMjQzZWM0OWFj/artifacts/plan.md`

---

## 1. 问题

用户报告:点 OpenStoryline 网页左上角【📥 导入视频】弹出的 Windows「打开」对话框
**无法多选**视频文件。

**根因**:`openstoryline/web/index.html` 的隐藏文件输入框缺 `multiple` 属性 →

```html
<!-- 改前 -->
<input type="file" id="importVideoFileInput"
       accept="video/*,.mp4,.mov,.mkv,.avi,.webm" hidden />
```

HTML 规范:`<input type="file">` 默认单选。没有 `multiple` 时,Windows 的「打开」
对话框**不渲染复选框**,并禁用 Ctrl+A 全选与 Ctrl+点击多选。

已排除:运行时无 JS 设置 `.multiple`;与 `accept` 无关;与 Windows 版本/Explorer 设置无关。

但只加 `multiple` **不够** —— 往下还有三层单源收窄点,使得「多选了也没用」:

| 层 | 位置 | 单源约束 |
|---|---|---|
| 前端 | `web/static/app.js` change 回调 | `e.target.files[0]` —— 只取第 0 个 |
| 协议 | `agent_fastapi.py` 端点 | body 只认单值 `media_id` / `filename` / `stored_path` |
| 限流 | `agent_fastapi.py` | burst=1 / rpm=2 —— 分 N 次 POST 必然 429 |
| 流水线 | `state.video_input_path: str` | 全图唯一的输入源标识;**没有任何节点回写它** |

最后一条是关键:今天就算导入成功,视频也进不了流水线
(`import_video_media_path` 是个**只写不读的死字段**)。

## 2. 方案

### 2.1 单一事实源:`state["video_input_paths"]`

新增 `list[str]` 字段,表示「本次流水线的全部源视频」。
`video_input_path` **保持 `str` 不变**(= 第一个,向后兼容)。

所有需要"全部源视频"的节点统一走:

```python
# nodes/storyline/_common.py
def resolve_video_input_paths(state) -> list[str]:
    """1. state["video_input_paths"]  2. [state["video_input_path"]]  3. []"""
```

优先级保证老 checkpoint / 老 `initial_state.json` 零改动可用。

### 2.2 握手协议:列表 + 单值并存(向后兼容)

`import_video_result.json` 同时写两套字段:

```json
{
  "ok": true,
  "web_session_id": "abc-123",
  "media_id": "media_0001",              // 旧:第一个
  "filename": "a.mov",                   // 旧:第一个
  "stored_path": "E:/.../a.mov",         // 旧:第一个
  "media_paths": ["E:/.../a.mov", "E:/.../b.mov"],   // 新
  "filenames": ["a.mov", "b.mov"],                    // 新
  "media_ids": ["media_0001", "media_0002"],          // 新
  "triggered_by": "import_video_button"
}
```

- 老节点 / 老 checkpoint 只看 `stored_path` → 行为不变
- 新节点读 `media_paths` → 多视频
- 列表为空时回退读 `stored_path`(兼容手写的 result 文件)

端点 body 同时接受两种格式:

```jsonc
// 新(多文件)
{ "session_id": "...", "files": [{ "media_id": "m1", "filename": "a.mov", "stored_path": "E:/a.mov" }] }
// 旧(单文件,向后兼容)
{ "session_id": "...", "media_id": "m1", "filename": "a.mov", "stored_path": "E:/a.mov" }
```

### 2.3 解析逻辑下沉到 `node_04a`(**关键决策**)

`parse_import_payload` / `build_result_payload` / `extract_media_paths` 三个纯函数
放在 **`nodes/node_04a_import_video.py`**,而不是 `agent_fastapi.py`:

- 该模块本就是**跨 venv 契约模块**(openstoryline 的 venv 已通过 `sys.path.insert`
  导入它)
- 端点只做**薄转发** → 主仓 pytest(主 venv)能直接覆盖协议逻辑,
  **不需要起 openstoryline venv**
- 顶层依赖仍然只有 stdlib + `config` + `storyline.output_isolation`
  (守护测试 `test_module_source_has_no_heavy_dependencies` 守住这条硬约束)

### 2.4 前端:一次 POST(限流模型不动)

限流 `IMPORT_VIDEO_ALL_BURST=1` / `RPM=2` / `IP_BURST=1` / `IP_RPM=2` **保持不变** ——
分 N 次 POST 必然 429。所以:

```
用户点【📥 导入视频】→ 选 N 个文件
  ├─ 逐个 uploadMediaChunked(串行)→ N 个 media 进会话 pending 区
  │    └─ 单个失败:记录并继续(部分成功 = 导入成功)
  └─ 一次 POST { session_id, files:[...], media_id, filename, stored_path }
```

### 2.5 按钮两阶段可用(409 不是错误)

关卡⓪(规划阶段)在 `import_video` 节点**之前**。此时点按钮:

- 素材**已经进了会话**,规划器(Agent)能立刻用上
- 但图还没写 `import_video_request.json` → `find_pending_request()` 返回 `None` → 409

所以 409 改成提示「已上传 N 个视频到当前会话,可继续与 Agent 对话完成规划」,
而不是原来的「当前没有待导入的请求」。

## 3. 改动清单

### Phase 1 — 多选 + 批量上传 + 多文件握手

| 文件 | 改动 |
|---|---|
| `openstoryline/web/index.html` | 隐藏 input 加 `multiple` 属性 |
| `openstoryline/web/static/app.js` | `importVideoFile(file)` → `importVideoFiles(files)`;扩展名过滤 + pending 余量截断 + 串行上传 + 一次握手;409 分支改文案;新增 4 个 i18n key(中英各一份) |
| `openstoryline/agent_fastapi.py` | 端点改薄转发:`parse_import_payload` → `build_result_payload` |
| `nodes/node_04a_import_video.py` | 新增 3 个纯函数;`_finalize` 多路径回填;回滚路径支持多路径 |
| `state.py` | 新增 `video_input_paths` / `import_video_media_paths` |

### Phase 2 — 流水线消费多路径(接线)

| 文件 | 改动 |
|---|---|
| `nodes/storyline/_common.py` | 新增 `resolve_video_input_paths` / `video_input_paths_key` |
| `nodes/storyline/node_load_media.py` | `_build_inputs` 读多路径 |
| `nodes/node_04b_get_storyboard_plan.py` | 幂等键原料 = 排序拼接的全部源路径(单视频时与旧值**逐字节一致**) |
| `nodes/assembly/node_discover_and_probe.py` | 输入源列表化(单文件 / 目录扫一级两种分支都保留) |
| `nodes/assembly/node_asr_and_visual_observe.py` | 逐视频 ASR + 抽帧 → `transcript_<i>.json` / `video_ingest_<i>.json` → 合并出 `transcript.json` / `video_ingest.json`(下游签名不变) |
| `nodes/node_05_generate_draft.py` | legacy 路径逐视频归一化 + shots 轮转分配;canonical 路径保持原比例 |
| `nodes/storyline/join_storyline.py` | fallback 的 `media_0001` 硬编码 → N 条 source_media |
| `nodes/storyline/node_local_asr.py` | 逐视频 ASR,段带 `media_id` + 源内时间 |
| `scripts/run_workflow.py` | 新增 `--video-input-paths`(逗号分隔)→ 写 `initial_state.video_input_paths` |
| `agent_gate/cli.py` | 新增 `--video-paths`;`_write_initial_state` 支持多路径 |
| `agent_gate/mcp_server.py` | `run_start` 新增 `video_paths` 参数 |

### Phase 3 — 多视频正确性修复

| 文件 | 改动 |
|---|---|
| `storyline_capabilities/split_shots.py` | 合并阶段**全局连续重编号** `clip_id`(单视频时与旧值一致) |
| `storyline_capabilities/filter_clips.py` | **每源保底**:每个源视频至少保留 1 个 clip(源内得分最高的那个) |
| `nodes/node_08_add_subtitles.py` | 多视频:逐源 ASR → 按草稿 clip 的 `source_timerange → target_timerange` 映射到成片时间轴;落在 clip 外的段丢弃 |
| `nodes/node_16a_translate_and_check.py` | 布局 QC 逐视频预览(issues 合并) |

**契约层 / 能力层零改动**:`storyline/contract.py`、`storyline/plan_reader.py`、
`storyline/mapper.py`、`storyline_capabilities/load_media.py`、
`assembly_capabilities/build_timeline.py` —— 它们**本来就是多源设计**。

## 4. 已知限制

| # | 限制 | 说明 |
|---|---|---|
| R1 | **剪映客户端是否接受「多条不同 path 的 video material」** | 高风险项,需真机剪映实测。本仓库的 `draft_content.json` 是自研简化 schema,剪映侧行为未验证。若客户端不接受,多视频草稿路线需重做。 |
| R2 | `node_04b` 幂等键口径不一致 | 既有问题:`node_04b:101` 算 `sha256(path)`,`node_04b:164` 写 manifest 时传 `extras` → manifest 里的 `input_sha256` 与命中检查用的键**永不相等**,resume 幂等命中实际失效。本次**未修**(会改变行为面)。 |
| R3 | canonical 路径的多源素材**不做宽高比归一化** | 比例不同的多源素材进草稿会拉伸。归一化要重写 timeline 的 `source_in/out`,风险远大于收益 → 只在 legacy 路径做。 |
| R4 | `materials.videos` 按 clip 复制而非按源去重 | 同一文件 5 个 clip → 5 条同 path material。剪映侧若出现重复条目再单独去重。 |
| R5 | `media_id` 命名空间不统一 | `load_media` 出 `media_0001`,`plan_reader` 出 `media-1`。两条链路不交叉。 |
| R6 | 抽帧目录写到源文件旁边(`<video>.frames`) | 多视频会在用户磁盘留 N 个目录。 |
| R7 | 35s 全局预算对 N 个视频是**并集**预算 | 5 个视频各 30s 远超预算,`filter_clips` 会大幅裁剪;每源保底保证每个源都进成片。 |
| R8 | `graph.py:280-301` 的 ASR 分支不可达缺陷 | 既有问题,代码注释要求单独评审,本次不动。 |

## 5. 验收

### 自动化(主仓 pytest)

新增 7 个测试文件:

| 文件 | 覆盖点 |
|---|---|
| `tests/unit/test_import_video_request_result.py`(扩展) | `parse_import_payload`(新/旧/空/脏)、`build_result_payload`(列表 + 单值=第一个)、`extract_media_paths` |
| `tests/unit/test_node_04a_import_video.py`(扩展) | 多文件 result → `_finalize` 回填 N 条路径;旧单文件 result 仍可回填;超时路径不产出列表;回滚路径支持多路径 |
| `tests/unit/test_resolve_video_input_paths.py`(新建) | 优先级(显式列表 > 单值 > 空)、空串/None 过滤、去重、幂等键稳定性 |
| `tests/unit/test_node_load_media_multi.py`(新建) | 多路径 → N 条 media;不存在的路径进 `skipped` |
| `tests/unit/test_split_shots_unique_clip_ids.py`(新建) | 2 视频各 3 clip → 6 个 `clip_id` 全局唯一;单视频时与旧值一致 |
| `tests/unit/test_filter_clips_quota.py`(新建) | 每源保底;单源时与旧行为等价;LLM 覆盖时也保底 |
| `tests/unit/test_node_08_multi_video_subtitles.py`(新建) | source→target 线性映射 / 部分重叠裁剪 / clip 外丢弃 / 合并排序 |
| `tests/integration/test_video_input_paths_wiring.py`(新建) | 导入 N 个 → `state["video_input_paths"]` = N;单视频等价 |
| `tests/integration/test_multi_video_draft.py`(新建) | 2 源 → canonical → draft:materials 含 2 个 path;`material_id` 指向正确 material |
| `tests/integration/test_no_auto_import_video.py`(扩展) | 多文件握手端到端:图阻塞 → 回写多文件 → 图自动继续 |

**守护测试不得放宽**:

- `tests/integration/test_no_auto_import_video.py` —— 图**永不自动导入视频**
- `tests/unit/test_node_04a_import_video.py::test_module_source_has_no_heavy_dependencies`
  —— `node_04a` 顶层只依赖 stdlib + `config` + `storyline.output_isolation`(跨 venv 硬约束)
- `tests/integration/test_auto_mode_end_to_end.py` —— 节点壳子不得 `import open_storyline.*`

### 手工验收(Windows + OpenStoryline Web)

> ⚠️ **历史归档(2026-10-21 当时的实现)**
>
> 下列步骤记录的是**当时的**启动方式。`.\scripts\start_local.ps1` 已于
> 2026-10-21 **删除**,启动入口统一为根目录的 `.\start_production.ps1`
> (用 `-StorylineMode human` 走人工关卡链)。以下描述保留作为历史参考。
>
> 现行用法:见 [`README.md`](../../README.md) 的「启动入口」章节。

1. `.\scripts\start_local.ps1`(免管理员全链;**现已删除,改用 `.\start_production.ps1 -StorylineMode human`**)→ OpenStoryline Web :7860
2. `STORYLINE_MODE=human` 跑 `scripts\run_workflow.py --production --thread-id video-001 --initial-state-json runtime\initial_state_video-001.json`
3. 关卡⓪ 挂起 → 网页点【📥 导入视频】→ **确认 Windows 对话框出现复选框**(用户报告的缺陷点)
   → Ctrl+A 多选 3 个 `.mov` → 打开
4. 断言:3 个素材出现在 composer 待发送区;toast 显示「已上传 3 个视频…」(图在关卡⓪,握手 409)
5. 与 Agent 对话完成规划 → resume
6. 图走到 `import_video` → 再点【📥 导入视频】(3 个)→ toast「已导入 3 个视频」→ 图自动继续
7. 断言:`outputs/video-001/import_video_result.json` 的 `media_paths` 长度 3;
   `state["video_input_paths"]` 长度 3
8. 断言:草稿 `drafts/default/draft_content.json` 的 `materials.videos` 含来自不同源视频的条目;
   `outputs/video-001/assembly/media.json` 长度 3
9. 断言:成片 `preview.mp4` 时长 ≈ 35s,QC 报告无新增 error

> 步骤 8、9 依赖 R1(剪映接受多条不同 path 的 material),首次跑请先做最小验证。

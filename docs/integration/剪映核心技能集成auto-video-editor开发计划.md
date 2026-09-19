# 剪映核心技能集成 auto-video-editor 开发计划

> 目标：把 `jianying-editor`（底层SDK）、`jianying-draft-edit`（编辑黄金法则）、`openstoryline-to-jianying`（草稿生成桥接）三个技能，接入 `auto-video-editor` 仓库（LangGraph + SqliteSaver 实现）对应的功能模块。
>
> 核查方法：clone `wayyet/auto-video-editor` 与 `wayyet/FireRed-OpenStoryline` 两仓库最新代码，逐文件源码级核对，不是只读文档。文中所有文件路径、函数名、字段名均来自实际源码。

---

## 0. 核查结论摘要

附件报告《FireRed-OpenStoryline剪映技能清单与auto-video-editor实现对照报告》的核心数据，全部验证通过：

| 资源类型 | 报告数字 | 实测数字 | 数据位置 |
|---|---|---|---|
| 转场 | 433个/303 VIP | ✅ 一致 | `vendor/pyJianYingDraft/metadata/transition_meta.py` |
| 视频特效 | 1097个/462 VIP | ✅ 一致 | `.../metadata/video_scene_effect.py` |
| 文字入场动画 | 145个/78 VIP | ✅ 一致 | `.../metadata/text_intro.py` |
| 文字循环动画 | 93个/52 VIP | ✅ 一致 | `.../metadata/text_loop.py` |
| 文字出场动画 | 97个/46 VIP | ✅ 一致 | `.../metadata/text_outro.py` |

`requirements.txt` 核实：auto-video-editor 目前**没有引用**任何 pyJianYingDraft / uiautomation 相关依赖，三个待集成技能目前都是"零引用"状态。

额外发现一个报告未覆盖、但优先级更高的问题，见第1节。

---

## 1. 【P0，最先处理】draft_info.json 权威源问题

### 1.1 问题

剪映草稿目录下有两个内容几乎一样的文件：`draft_content.json` 和 `draft_info.json`。auto-video-editor 现在**只写前者**。三条证据指向"剪映5.9+ 认的是后者"：

**证据1**（`vendor/pyJianYingDraft/draft_folder.py`）：
```python
# create_draft() 创建新草稿时:
script_file.save_path = os.path.join(draft_path, "draft_info.json")

# load_template() 读取已有草稿时:
info_path = os.path.join(draft_path, "draft_info.json")
if not os.path.exists(info_path):
    # 兼容旧版本
    info_path = os.path.join(draft_path, "draft_content.json")
```
优先读写 `draft_info.json`，找不到才退回 `draft_content.json`。

**证据2**（`openstoryline-to-jianying` 技能文档，2026-07 真实项目验证记录）：原文写明草稿目录下"存在 `draft_info.json`（草稿内容以此为准）"；已知坑列表里也写"缩略图偶尔显示 00:00，但打开草稿内容完整，以 `draft_info.json` 为准"。

**证据3**：对 auto-video-editor 全仓库（含 `docs/`）搜索 `draft_info`，零匹配。

### 1.2 现状核实

`draft_content.json` 的写入入口分两类，都需要覆盖：

| 写入方式 | 涉及节点 |
|---|---|
| `draft_ops/atomic_writer.py` 的 `atomic_write_draft(draft_file, content)` | node_05、07、08、09、10、11、13（共7个） |
| `draft_ops/atomic_writer_file.py` 的 `atomic_write_file(path, data)`，自行 `json.dumps` 后传入 | node_16（`_write_marker` 函数，第164-179行，路径同样是 `draft_content.json`） |

`node_17` 的草稿更新路径在现有代码里没有找到（只看到写静音占位 wav），建议实施时先核实清楚，再决定是否要纳入下面的统一改造。

### 1.3 验证方法（现在就能做，不用等其他集成工作）

1. 跑现有 `node_05` 生成一份测试草稿
2. 用真实剪映 5.9.0 客户端打开草稿箱
3. 看缩略图和时间轴内容是否正常显示

### 1.4 应对方案

不管验证结果如何，双写成本很低（多一次 json 序列化 + 原子写），建议直接做：

- 改造 `draft_ops/atomic_writer.py`，新增：
```python
def atomic_write_draft_pair(draft_dir: Path, content: dict) -> None:
    """同时原子写入 draft_content.json 与 draft_info.json（内容一致）。"""
    atomic_write_draft(draft_dir / "draft_content.json", content)
    atomic_write_draft(draft_dir / "draft_info.json", content)
```
- 两个文件各自走独立的 mkstemp + os.replace，不共用临时文件，避免其中一个失败时另一个已经落盘造成不一致
- 7个 `atomic_write_draft` 调用点、以及 node_16 的 `_write_marker`，统一改成调这个新函数（具体整合方式见第3节 `safe_write_draft`，不需要分别改两次）

### 1.5 与既有决策的关系

`docs/integration/architecture_decision_record.md` 的 ADR-006 已确定"剪映落盘继续由 auto-video-editor 拥有（`draft_ops/atomic_writer.py`）"。本次改动不推翻这条决策，只是给这个已确定归属的模块补一个之前没意识到的正确性问题，落盘的归属不变。

---

## 2. 技能①`jianying-editor`（底层SDK）集成方案

### 2.1 现状

- `jianying-editor` 技能路径：`FireRed-OpenStoryline/.claude/skills/jianying-editor/`
- 核心资产两类：(a) `scripts/vendor/pyJianYingDraft/` 整个 vendor 库；(b) `scripts/jy_wrapper.py` 里可直接 `import` 的 `JyProject` 类
- `mcp_clients/jianying_cover_client.py` 里的 `HTTPJianyingCoverClient` 目前是占位实现，代码注释写着"真实 uiautomation + pyJianYingDraft 路径留待接入"——这正是这个技能要填的坑

### 2.2 vendor 步骤

1. 把 `jianying-editor/scripts/vendor/pyJianYingDraft/` 整个目录复制进 auto-video-editor，新建路径：`vendor/pyJianYingDraft/`
2. `requirements.txt` 新增一行：
```
uiautomation==2.0.20
```
   原因：pyJianYingDraft 内部控制模块依赖它，即使暂时不用 GUI 自动化能力，import 链路也需要这个包才能不报错。
3. **不建议**现在引入 `playwright` / `pynput` / `edge-tts` / `opencv-python`——这些是 jianying-editor 技能里"录屏""Web 动效"等 auto-video-editor 17步流程当前不需要的能力，用到再加，避免依赖膨胀

### 2.3 转场/特效占位符替换（对应 node_09）

- 目标文件：`templates/fx_template.json`，现在顶层写着 `"_reverse_engineering_pending": true`，`resource_id` 全是 `PLACEHOLDER_TRANSITION_FADE_001` 这类假值
- 新增一次性同步脚本 `scripts/sync_fx_template_from_pyjianying.py`：从 `vendor/pyJianYingDraft/metadata/transition_meta.py` 和 `video_scene_effect.py` 读取全部条目，按 `fx_template.json` 现有字段结构（`name` / `category` / `resource_id` / `duration_us`）批量生成，写回文件，`_reverse_engineering_pending` 改成 `false`
- 同步改造 `jy_common/template_library.py` 的 `pick_transition()` / `pick_video_effect()`：这两个方法目前的注释明确写着"Week 3 占位：仅按索引顺序循环，Week 4 替换为按 style_tag 匹配"——这次要把"Week 4"这部分真正实现，按 `style_tag` 匹配到元数据库里的分类/名称关键词，不再是循环取值

### 2.4 花字动画占位符替换（对应 node_10）

- 目标文件：`templates/text_style_template.json`，`default_text_style()` 现在返回 `entrance_animation: None`
- 数据源：`vendor/pyJianYingDraft/metadata/text_intro.py` / `text_loop.py` / `text_outro.py`
- `node_10_inject_text_fx.py` 代码里已经有注释"Week 4 从 AVAILABLE_ASSETS.md 选定具体合法枚举值"——`jianying-editor/references/AVAILABLE_ASSETS.md` 就是这份清单，可以直接对照使用，不用重新枚举

### 2.5 涉及模块清单

`requirements.txt`、新增 `vendor/pyJianYingDraft/`、新增 `scripts/sync_fx_template_from_pyjianying.py`、`templates/fx_template.json`、`templates/text_style_template.json`、`jy_common/template_library.py`。`node_09` / `node_10` 本身大概率不用改代码，因为它们只是调用 `template_library` 的接口，接口签名不变。

---

## 3. 技能②`jianying-draft-edit`（编辑黄金法则）集成方案

### 3.1 现状核查

对照黄金法则逐条检查现有7个写入节点：

| 黄金法则 | 现有节点是否遵循 |
|---|---|
| 写入前整目录快照（失败可回退） | 否——node_07 的 `shutil.copytree` 是"产出快照②给下游分支用"，用途不同，不是失败回退备份 |
| 写入前检测剪映客户端是否还开着 | 否——7个写入点均无此检测 |
| draft_content.json / draft_info.json 双写同步 | 否——见第1节 |
| 写入后更新时长索引（`draft_meta_info.json` / `root_meta_info.json` 的 `tm_duration`） | 否——`atomic_write_draft` 完全不涉及这两个文件 |
| 写入后验证（只读重新解析一次） | 否——写完即返回，无验证步骤 |

这一条特别值得注意："写入前检测剪映客户端是否还开着"不是空担心：步骤6（人工调分镜）和步骤12（人工加BGM）本来就是用户在剪映客户端里手动操作完再回来，回来后紧接着 node_07 / node_13 就要写草稿，剪映很可能还没退出。

### 3.2 新增 draft_ops 模块

**`draft_ops/safe_write_guard.py`**（新增）：
```python
def check_jianying_not_running() -> bool:
    """只读检测 JianyingPro.exe 进程是否在运行。检测到时只告警不阻断、
    不强杀进程——黄金法则要求人工判断，不是自动化替用户做决定。"""

def snapshot_before_edit(draft_dir: Path) -> Path:
    """写入前把整个草稿目录复制到 <draft_dir>/../.snapshots/<timestamp>/，
    返回快照路径，供写入失败时回退。"""
```

**`draft_ops/duration_index.py`**（新增）：
```python
def update_duration_index(draft_dir: Path, new_duration_us: int) -> None:
    """改 draft_meta_info.json 的 tm_duration，以及草稿根目录
    root_meta_info.json 里 all_draft_store 数组中对应项的 tm_duration。"""
```
调用时机：任何改变了 `draft["duration"]` 顶层字段的节点之后，主要是 node_07（变速）；node_16/17 若触发动态变速补偿也要调用。

**`draft_ops/verify_after_write.py`**（新增）：
```python
def verify_draft_loadable(draft_dir: Path) -> bool:
    """用 vendor 的 pyJianYingDraft.DraftFolder(...).load_template(...)
    只读解析一次，不抛异常且 duration 正确即通过；失败则从快照回退。"""
```

### 3.3 统一入口：改造 atomic_writer.py

不需要逐个改7个节点的业务逻辑，包一层统一入口：

```python
def safe_write_draft(draft_dir: Path, content: dict) -> None:
    check_jianying_not_running()          # 告警不阻断
    snapshot = snapshot_before_edit(draft_dir)
    atomic_write_draft_pair(draft_dir, content)   # 第1.4节的双写
    if content.get("duration") is not None:
        update_duration_index(draft_dir, content["duration"])
    if not verify_draft_loadable(draft_dir):
        restore_from_snapshot(draft_dir, snapshot)
        raise RuntimeError("草稿写入后校验失败，已回退到写入前状态")
```

7个调用 `atomic_write_draft` 的节点（05/07/08/09/10/11/13），把函数名换成 `safe_write_draft` 即可，入参从"文件路径"改成"草稿目录"（因为要同时处理两个文件）。node_16 的 `_write_marker` 需要单独把 `atomic_write_file` 的调用也换成 `safe_write_draft`，这处不会被"改一个函数"自动覆盖，需要显式修改。

### 3.4 不在本次范围内的部分

黄金法则里的操作A（变速）node_07 已实现且逻辑一致，不用动。操作B（删减重排）在这个项目里是人工在剪映客户端完成的，不需要代码实现。操作C（挑瑕疵剔除）、操作D（加标题）：17步流程本身没有对应步骤，不在本次集成范围，除非之后单独提需求。

---

## 4. 技能③`openstoryline-to-jianying` 集成方案（已确认：方案A）

### 4.1 现状

`node_05` 现在用的是自研的 `storyline.mapper.canonical_to_draft`，完全没有经过这个技能或 pyJianYingDraft。

### 4.2 方案A做法（保守，改动小）

不替换现有 mapper，只让 `node_05` 的写入调用从 `atomic_write_draft` 换成第3.3节的 `safe_write_draft`，和其他6个节点保持一致。这样 node_05 自动获得双写 `draft_info.json` + 写入前快照 + 写入后校验的全部保护，不需要为 node_05 单独开发新逻辑。

### 4.3 后续可选项（不阻塞本次集成，仅记录）

`openstoryline-to-jianying` 技能自带的 `scripts/build_draft.py`，走的是 `JyProject`（pyJianYingDraft 官方保存机制）重建四条轨道（视频/字幕/配音/BGM），并有"缺失切片跳过并告警"等容错逻辑。如果后续发现 `storyline/mapper.py` 在四轨重建的细节上有欠缺，可以对照 `build_draft.py` 的具体做法逐项核对，但这属于比方案A更彻底的方案B（整体替换 mapper），会牵连 `storyline/contract.py` 的既有契约设计和相关测试，本次不做，先用方案A验证 draft_info.json 问题是否解决。

---

## 5. 分阶段实施计划

| 阶段 | 内容 | 交付物 |
|---|---|---|
| Day 1 | 验证第1.3节（跑 node_05 + 真实剪映打开测试）；无论结果如何，实现 `atomic_write_draft_pair` | 验证记录、双写函数 |
| Day 2-3 | vendor `pyJianYingDraft`；`sync_fx_template_from_pyjianying.py` 跑通，替换 `fx_template.json` 占位符；`template_library.py` 的 style_tag 匹配改造 | 真实转场/特效数据、匹配逻辑单测 |
| Day 4 | `text_style_template.json` 占位符替换（对照 `AVAILABLE_ASSETS.md`） | 花字动画真实数据 |
| Day 5 | 新增 `safe_write_guard.py` / `duration_index.py` / `verify_after_write.py` 三个模块 + 单测；改造 `atomic_writer.py` 加 `safe_write_draft` | 三个新模块及测试 |
| Day 6 | 7个节点 + node_16 切换到 `safe_write_draft`；node_05 完成方案A收尾；跑一遍现有 `tests/unit` 和 `tests/integration`，确认没有破坏既有行为 | 回归测试报告 |
| Day 7 | 真实剪映环境端到端验证：跑通到 node_15，实际打开剪映确认内容显示、转场特效可见、时长索引正常 | 端到端验证记录 |

---

## 6. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| draft_info.json 假设不成立 | 白白多写一份文件 | 双写成本低，不需要等验证结果确认才动手，直接做没有副作用 |
| 双写引入两文件不一致的中间状态 | 写入过程中断可能导致一份新一份旧 | 两个文件各自独立 mkstemp + os.replace，内容完全相同，不共用临时文件 |
| `check_jianying_not_running` 误报 | Windows 下进程检测可能因权限/多会话不准 | 只告警不阻断，人工最终判断 |
| vendor 引入的 `uiautomation` 在测试/CI 环境不可用 | 单元测试可能因缺 Windows 专属库失败 | 涉及 GUI 自动化的 import 做延迟导入（用到才 import），CI 用 Linux 容器时不受影响 |
| style_tag 匹配逻辑设计不当 | 转场/特效选择不符合预期风格 | 先小样本人工抽查，再全量替换 |
| node_16 的写入点未纳入统一改造 | 英文字幕这条路径仍只写 draft_content.json 一个文件 | 第3.3节已明确要求单独修改，不依赖"改一个函数自动生效" |
| node_17 草稿更新路径未确认 | 实施时可能发现遗漏一个写入点 | Day 6 回归测试阶段专项核实 |

---

## 7. 验收标准

- [ ] 真实剪映5.9.0客户端打开跑到 node_15 的草稿，缩略图与时间轴内容均正常显示
- [ ] `templates/fx_template.json` 不再含 `PLACEHOLDER_` 前缀的 `resource_id`
- [ ] `templates/text_style_template.json` 的 `entrance_animation` 不再是 `null`
- [ ] 现有 `tests/unit`、`tests/integration` 全部通过
- [ ] 变速节点执行后，`draft_meta_info.json` / `root_meta_info.json` 的 `tm_duration` 正确更新
- [ ] `draft_content.json` 与 `draft_info.json` 内容一致，7个节点 + node_16 均已切换到 `safe_write_draft`

---

## 附录：涉及文件清单

**新增文件**：
- `vendor/pyJianYingDraft/`（整目录，从 jianying-editor 技能 vendor）
- `scripts/sync_fx_template_from_pyjianying.py`
- `draft_ops/safe_write_guard.py`
- `draft_ops/duration_index.py`
- `draft_ops/verify_after_write.py`

**修改文件**：
- `requirements.txt`
- `draft_ops/atomic_writer.py`（加 `atomic_write_draft_pair`、`safe_write_draft`）
- `jy_common/template_library.py`（`pick_transition` / `pick_video_effect` 改为按 style_tag 匹配）
- `templates/fx_template.json`、`templates/text_style_template.json`（占位符替换）
- `nodes/node_05_generate_draft.py`、`node_07_speed_fit.py`、`node_08_add_subtitles.py`、`node_09_inject_fx.py`、`node_10_inject_text_fx.py`、`node_11_inject_sticker.py`、`node_13_adjust_volume.py`（写入函数调用换成 `safe_write_draft`）
- `nodes/node_16_translate_subtitles.py`（`_write_marker` 单独修改）

**参考来源**：
- `wayyet/auto-video-editor`（本次核查基准代码）
- `wayyet/FireRed-OpenStoryline` 的 `.claude/skills/jianying-editor/`、`jianying-draft-edit/`、`openstoryline-to-jianying/`
- 用户上传《FireRed-OpenStoryline剪映技能清单与auto-video-editor实现对照报告.md》
- `docs/integration/architecture_decision_record.md`（ADR-006）

---

*本文档由 AI 辅助整合生成，第1节 P0 问题建议在投入其他开发工作前优先验证。*

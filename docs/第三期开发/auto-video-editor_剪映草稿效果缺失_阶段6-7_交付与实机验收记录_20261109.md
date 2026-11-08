# auto-video-editor 剪映草稿效果缺失 阶段6-7 交付与实机验收记录(2026-11-09)

> 计划:`docs/第三期开发/auto-video-editor_剪映草稿效果缺失根因分析与修复设计执行计划_20261108.md` §8 阶段 6 / 阶段 7
> 执行会话:ZCode(阶段 0-5 已交付后的收尾两阶段);实机 GUI 步骤留操作员执行
> 关联交付:阶段 0-5(见计划文档 §13.1–13.6,`f5dd584`→`d8bfdc5`)

---

## 1. 结论摘要

| 项目 | 结果 |
|---|---|
| 单元测试(tests/unit) | **1582 通过 / 3 跳过 / 0 失败**(基线 1522 + 本两阶段新增 60 例) |
| 集成测试(tests/integration) | 对照阶段 0-5 基线(`outputs/effect-p3p5-integration.xml`):**新增失败 0 / 消失 0**,8 存量失败不变(见 §3) |
| `check_imports` | 0 未解析 |
| 图编译 | 含新节点 `delivery_qa_repair` / `delivery_qa_failed` 与阻断回边(单测断言) |
| 剪映实机端到端(GUI) | **待操作员执行**(清单见 §4;代码侧已就绪) |
| 素材矩阵 | 已完成:`outputs/e2e-assets/`(脚本 `scripts/make_e2e_test_assets.py --variant all` 可复现) |

8 个存量集成失败与本阶段改动**无关**(干净树 `d8bfdc5` 上同样失败,清单见 §3.3)。

---

## 2. 本阶段新增能力的自动化验证

### 2.1 阶段 6a:required-feature gate + 阻断路由

**required_features 通道(计划任务 1,决策=表单复选框+智能默认):**

- `state.py`:新增 `required_features`(NotRequired dict)与 `delivery_qa_retry_count`。
- `draft_ops/draft_integrity.py::default_required_features / resolve_required_features`:
  显式声明覆盖运行时推导;推导规则 —— 字幕/转场/动画/淡入淡出默认必选,
  配音仅 `TTS_BACKEND=firered` 时默认必选(stub 环境不默认阻断联调,表单显式
  勾选后 stub 即 degraded 阻断),BGM/贴纸默认可选。
- 创建台:`web/console.html` 03 区新增 9 个必需功能复选框(默认勾选态由
  `GET /api/console/config` 的 `required_features_defaults` 下发,config 不可达时
  前端内置保守默认);`web/api_console.py` `POST /runs` 收 `require_*` 布尔字段;
  `web/console_state.py` 白名单落 `required_features` 键(非布尔/未知键过滤);
  查看模式按 `initial_state.required_features` 回填。
- `scripts/run_workflow.py --initial-state-json` 老通道不声明时由 QA 侧推导,
  旧 checkpoint 零迁移。

**per-category QA(计划任务 2/3):**

- `build_qa_categories()`(draft_integrity.py):13 类(9 必需 + video_effects /
  covers / srt / draft_integrity)逐类 `passed/failed/degraded/not_applicable` 判定;
  类别专属新判据 —— "有旁白产物但草稿无我方字幕"→ subtitles_zh failed、
  英文分支存在而中文侧有字幕但英文侧无 → subtitles_en failed、stub 配音 →
  voiceover degraded、required BGM 无轨 → failed、单镜头转场 → not_applicable、
  required 贴纸但占位被 fail-closed 拦截 → degraded。
- 尾链四项检查(`_tail_chain_issues`)重构为 `_tail_chain_structured`(带
  category/draft 标签),**`join_qa_issues` 字符串契约与阶段 4 逐字一致**
  (存量 21 例单测原样通过)。
- `check_resource_ids()`:我方确定性 id(`transition-*`/`vfx-*`/`sticker-*`/
  `text-anim-*`)效果素材的 resource_id 必须为真实 19 位(键缺失不报 —— 测试
  夹具/旧形态;占位/空串/非 19 位报 error),接入 `check_draft_dir`。
- `build_join_qa_report()` 报告顶层新增 `sha256`(双草稿 `hash_draft_files` 回读)、
  `required_features` 回显、`gate_failed`(任一 required 类别 failed/degraded)。
- join 节点事件 `delivery.qa_passed/qa_issues` payload 新增 `categories` 摘要、
  `sha256`、`gate_failed`;节点自身保持"记录不抛"(直接调用的存量测试行为稳定)。

**阻断路由(计划任务 4,决策=repair 重试 1 次+失败终止):**

- `nodes/node_delivery_qa_repair.py`:
  - `delivery_qa_repair`:按阻断类别分派重跑幂等节点函数
    (transitions→`node_09`、text_animation→`node_10`/`10b`、stickers→`node_11`、
    volume_fades→`node_13`;阶段 3-5 幂等三件套保证重放安全),写
    `delivery_qa_retry_count=1`;字幕/配音/BGM 属数据性缺失不可自动修复;
  - `delivery_qa_failed`:raise RuntimeError → `run_with_events` 发
    `run.failed`,运行状态置 failed —— 对照 `escalate_guardrail_failure` 的
    "写 error 连 END(创建台仍调和为 completed)",这是**真正的失败终止**。
- `graph.py::route_after_join_qa` 条件边:gate 未失败 → `export_final_video`;
  失败未修复 → `delivery_qa_repair`(回边 `join_before_delivery` 复检);
  复检仍失败 → `delivery_qa_failed` → END。
- `monitoring/workflow_graph.py`:两节点注册 + 4 条边标签。

**创建台分开展示(计划任务 5):**

- 交付状态卡 4 行 → 6 行:`分析产物`(storyline_join 完成点亮)/ 草稿生成 /
  草稿发布 / **效果落盘**(QA 逐类判定聚合"通过 N/降级 M/失败 K/不适用 J")/
  交付 QA / MP4 导出;
- QA 行按 `gate_failed` 区分 fail(阻断)与 warn(非必需缺失不阻断),note 带
  SHA-256 短摘要;`delivery.qa_repair`/`delivery.qa_failed` 事件落卡。

### 2.2 阶段 6b:draft_mutations 节点级草稿修改审计(计划任务 6)

**实现形态(对计划 §7.3.H 的一处偏离,理由):** 审计明细落**草稿目录**
`.autovideo_draft_mutations.jsonl`(点前缀,剪映不读),join QA 聚合每节点
最近一条进 `join_qa_report.mutations`(随 state 入 checkpoint)。未新增
state 顶层 `draft_mutations` 字段 —— `_draft_io.write_draft` 是纯 IO 层
(拿不到 state 写入通道),逐节点转发需改 12 处返回值且 08/08b 同超步并行
写同一 list 有冲突风险;磁盘 jsonl 天然按草稿目录隔离,零竞争。

- `nodes/_draft_io.py`:`write_draft` 新增 `audit_node` 参数 —— 写前读盘+
  `hash_draft_files`,写后重算,`build_draft_mutation_record` 生成
  `{node_id, draft_role, draft_path, sha256_before/after, changed,
  changed_counts(分桶 before/after), status, reason, recorded_at}`,
  `append_draft_mutation` 追加 jsonl(**审计失败降级不阻断**);
  `apply_and_write` 自动以 `node_tag` 作为审计节点(node_08/08b/09/10/11
  五处调用点零改动即接入);
- 直接调用点接入:`node_07`/`node_13`/`node_17`/`node_12`(镜像)传
  `audit_node`;`node_16` 从直调 `safe_write_draft` 改走 `_draft_io` 汇合点;
  `node_05` 初建写 `status="created"` 记录(sha_after 取真实哈希);
- `draft_role_of`:路径含 `en_branch` → "en"(fork 目录命名 `en_branch_*`);
- `build_join_qa_report` 聚合 `mutations`(每节点最近一条:sha/changed/
  changed_counts/status)。

### 2.3 测试清单

| 文件 | 新增 | 覆盖 |
|---|---|---|
| `tests/unit/test_draft_integrity.py` | +14 | check_resource_ids(占位/缺键/uuid 豁免)/ default+resolve required(TTS 后端推导/显式覆盖/非法过滤)/ build_qa_categories(旁白无字幕 failed/无旁白 n/a/单镜头 n/a/stub degraded/required BGM 阻断/可选 BGM n/a/深检归桶/13 类全量)/ 报告 sha256 |
| `tests/unit/test_node_join_before_delivery.py` | +6 | categories 结构与 gate 通过 / required 字幕缺失 gate_failed+status 标签 / 可选 BGM 缺失不阻断 / stub 配音 degraded 阻断 / state 显式声明回显 / sha256 落报告 |
| `tests/unit/test_node_delivery_qa_repair.py`(新) | 10 | 路由三分支 / blocked 只算 required / 分派表覆盖与不可修类别 / repair 只重跑可修复+计数+标签 / 空阻断防御 / failed raise 含明细 / 图编译含回边 |
| `tests/unit/test_draft_mutations.py`(新) | 9 | role 判定 / 分桶计数 / 幂等重放 changed=False / 写盘+读回 / 无审计不落文件 / apply_and_write 以 node_tag 审计 / 损坏行容忍 / join 报告聚合 |
| `tests/unit/test_console_state.py` | +2 | required_features 白名单过滤 / 不声明时键不出现 |
| `tests/unit/test_api_console_runs.py` | +2 | require_* 表单 → initial_state / 缺省时无该键 |

### 2.4 阶段 7(自动化部分)

- `scripts/make_e2e_test_assets.py` 扩展 `--variant all|base|long|landscape|
  hevc4k|noaudio`:单镜头(`clip_long.mp4` 30s)/ 多镜头多素材(clip_a+b)/
  横屏(`*_16x9.mp4`)/ 4K HEVC(`clip_4k_hevc.mp4`,libx265 ultrafast)/
  无音轨(`clip_noaudio.mp4`);生成后逐一 ffprobe 自证(编码/宽高/时长/
  音轨)。中英文 narration 由创建任务 lang 参数驱动,不是素材属性。
- 全量回归:unit 1582 例 0 失败;integration 对照基线新增失败 0(§3);
  `check_imports` 0 未解析。
- 证据收集:`scripts/freeze_run_baseline.py`(阶段 0 产出)逐 Thread 取证
  (双草稿 SHA-256/轨道素材计数/check_draft_dir/checkpoint 挂起导出),
  实机验收每任务跑一次,产物在 `outputs/baseline-<thread_id>/`。

---

## 3. 集成失败对照与一处语义级测试适配(2026-11-09)

### 3.1 QA 阻断语义对整图测试的影响(记录修复过程)

阶段 6a 首跑 integration 出现 **20 个新增失败**(基线 8 存量之外):全部为
整图驱动测试,根因一致 —— 夹具任务多镜头且无转场,QA 判
`transitions=failed`(required 默认 True)→ 阻断路由 → `delivery_qa_failed`
raise。**这是新语义在正确工作**(required 缺失不允许静默 END),不是缺陷;
测试的意图是拓扑/恢复/并行正确性,不是效果完整性。

**修法(设计内通道):** 5 个整图测试文件的 state 构造 helper 显式声明
`required_features` 全 False —— 等价于真实用户在创建台不勾选任何必需功能。
改动点:`_seed`(test_reorder_topology)、`_seed_state`(test_week4_graph /
test_week5_resilience / test_interrupt_resume)、`_legacy_week4_state`
(test_state_field_compatability)。QA gate 的阻断语义由 44 个单测
(§2.3 前 3 行)与实机验收覆盖。

### 3.2 一个对齐性修复

`draft_ops/draft_integrity.py::check_resource_ids` 首版把"resource_id 键缺失"
也报 error,误伤测试夹具的简化素材;真实构造器(`native_fx`)总是写该键。
收紧为"键缺失不报,存在但非法(空串/PLACEHOLDER/非 19 位)报 error"。

### 3.3 8 存量失败(与本阶段无关,`d8bfdc5` 干净树同样失败)

1. `test_auto_mode_end_to_end::test_load_media_then_split_shots_then_qa_gate_chain`(SPLIT_SHOTS_EMPTY)
2. `test_reorder_topology::test_tail_chain_applies_to_both_drafts`(英文草稿 transitions 为空)
3. `test_sequential_integration::test_sequential_produces_complete_draft`(字幕应被注入)
4. `test_unreachable_nodes_wiring::test_all_flags_off_matches_pre_wiring_golden_trace`
5. `test_unreachable_nodes_wiring::test_qa_retry_loop_still_retriggers_plan_timeline_pro`
6. `test_unreachable_nodes_wiring::test_ai_client_failure_keeps_base_timeline`
7. `test_unreachable_nodes_wiring::test_ai_status_tags_are_fixed_so_retry_survives_changing_group_count`
8. `test_unreachable_nodes_wiring::test_ai_pair_attaches_only_junctions_that_exist_in_the_final_timeline`

本阶段验证口径:全部 8 例失败信息与基线逐字一致(逐例抽查确认)。

---

## 4. 剪映实机端到端验收清单(操作员执行;每步完成后勾选)

> 前置:管理员 pwsh7 启动服务(`start_production.ps1`,自带
> `TTS_BACKEND=firered` 注入);PG 容器 `docker start edb-postgres16`;
> **剪映 5.9.0.11632 关闭**再创建任务;每场景**新建 Thread**,不复用 checkpoint。

### 4.1 素材准备

- [ ] `.\.venv\Scripts\python.exe scripts\make_e2e_test_assets.py --variant all`
      → `outputs/e2e-assets/` 出 6 个文件,ffprobe 行全绿

### 4.2 场景 A:多镜头多素材全功能(主链路,覆盖关卡①③②+resume)

- [ ] A1 创建:上传 `clip_a.mp4 + clip_b.mp4`,时长 35s,9:16,lang=zh;
      必需功能勾选 中文字幕/英文字幕/转场/字幕动画/淡入淡出(+中文配音/英文配音,
      验证 FireRed 真声)
- [ ] A2 关卡①:剪映打开 `AutoVideo_<tid>`,调换片段顺序,保存**并关闭剪映**,
      创建台恢复
- [ ] A3 关卡②:剪映 VIP 音乐库加 BGM,保存关闭,恢复
- [ ] A4 交付 QA:创建台交付卡应显示 `交付 QA 成功`;若有缺失,应显示
      "必需功能缺失,阻断交付"并出现 `交付 QA 定向修复` 行(修复后复检),
      复检仍失败则运行状态=失败(不静默完成)
- [ ] A5 取证:`.\.venv\Scripts\python.exe scripts\freeze_run_baseline.py <tid> --dump-state`
      → `outputs/baseline-<tid>/report.md` 引用完整性 0 error
- [ ] A6 剪映 GUI 逐项:首页可列 / 打开 / 字幕轨可见(中文+英文) / 字幕有入场动画
      / 每个镜头边界有转场 / 每段有视频特效 / BGM 音轨 + 淡入淡出 / 配音轨
      播放有真人声(非静音) / **保存关闭后重开,以上全部仍在**
- [ ] A7 中英对比:英文分支草稿(草稿库 `en_branch_*`)字幕/BGM 镜像/转场与
      中文一致;`join_qa_report.mutations` 每节点有审计记录

### 4.3 场景 B:单镜头(clip_long.mp4)

- [ ] B1 创建+跑完:QA 应判转场/淡入淡出 `not_applicable`(交付卡"效果落盘"
      行显示不适用计数),不阻断
- [ ] B2 剪映:字幕/动画正常,无转场属预期

### 4.4 场景 C:横屏 16:9(clip_a_16x9 + clip_b_16x9,创建时画幅 16:9)

- [ ] C1 全程跑完无源归一化裁切;封面 3 比例正常

### 4.5 场景 D:4K HEVC(clip_4k_hevc.mp4)

- [ ] D1 关注切镜抽帧耗时(参照 split_shots 超时空草稿教训,超时属已知风险点)

### 4.6 场景 E:无音轨(clip_noaudio.mp4)

- [ ] E1 全程跑完;QA 无音频类误报(无音轨是合法输入)

### 4.7 场景 F:英文任务(lang=en)

- [ ] F1 全程跑完;英文配音入英文草稿;`tts_issue` 无

### 4.8 MP4 导出两态

- [ ] G1 默认(`AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED` 关):交付卡
      `MP4 导出 跳过`,显式 `export.skipped`
- [ ] G2 开启后新任务:导出 MP4 可播放;报告注明 FFmpeg 版保真差异
      (转场硬切近似,VIP 特效/贴纸/动画不保证)

### 4.9 阶段 3-5 遗留实机复核项(同任务顺带完成,回填见 §5)

- [ ] H1 动画 schema:剪映手工给字幕加入场动画前后各存一版,diff
      `material_animations` 字段(重点 `animations[].start/duration` 单位),
      如与 `jy_common/native_fx.py` 注释有出入按样本回填
- [ ] H2 贴纸 resource_id:按 `templates/sticker_template.json`
      `_reverse_engineering_steps` 手工创建样本,把真实 19 位 ID 回填
      `sticker_by_keyword` 并置 `_reverse_engineering_pending: false`
      (回填后 node_11 自动转正常写入)
- [ ] H3 audio_fade 曲线/字段名与 GUI 样本 diff,按需回填 `native_fx.py`
- [ ] H4 转场 `category_id`/`apply_target_type` 等"待 GUI diff 复核"字段

---

## 5. 实机验收回填(操作员填写)

| 场景 | 日期 | Thread | 结果 | 偏离/问题 |
|---|---|---|---|---|
| A 多镜头全功能 | | | | |
| B 单镜头 | | | | |
| C 横屏 | | | | |
| D 4K HEVC | | | | |
| E 无音轨 | | | | |
| F 英文任务 | | | | |
| G 导出两态 | | | | |
| H 遗留复核 | | | | |

---

## 6. 版本冻结条件与遗留

### 6.1 版本冻结条件(全部满足才冻结)

1. unit 全量 0 失败(当前基线:1582 例);
2. integration 对照阶段 0-5 基线新增失败 0(8 存量清单固定,见 §3.3);
3. §4 实机清单场景 A–F 全部通过(GUI 二次打开无损坏,保存重开效果仍在);
4. 全部 required feature 在实机任务中通过(QA 卡"交付 QA 成功",无阻断终止);
5. 阶段 3-5 遗留复核项(§4.9)回填完成或明确降级口径;
6. `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED` 两态行为符合 §4.8。

### 6.2 已知遗留(不阻断冻结,按优先级)

- 贴纸资源仍为占位(fail-closed 拦截):required 贴纸默认关闭;实机回填(§4.9 H2)后自动转正;
- 音量淡入淡出曲线类型(`linear`)与动画 `start/duration` 单位待 GUI diff 校准(§4.9 H1/H3);
- 8 存量集成失败(§3.3)根因在阶段 0-5 前已定位,与效果缺失修复无涉,冻结前不强制清零;
- `delivery_qa_repair` 的修复成功率取决于失败类别:转场资源解析失败等重跑同样失败
  (fail-closed 语义,1 次重试即终止),定向修复定位是"重放竞争类"瞬时故障;
- 英文分支 QA 的 `voiceover_en` 判定读 `tts_issue` 文本(无结构化 summary),
  后续如需更细粒度(段级失败数)需 node_17 补 summary 字段。

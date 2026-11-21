# pyJianYingDraft 效果模块迁移 执行记录(阶段0-7)

> **执行日期**:2026-11-21
> **设计权威**:[auto-video-editor_pyJianYingDraft_效果模块迁移设计与执行计划.md](auto-video-editor_pyJianYingDraft_效果模块迁移设计与执行计划.md)
> **基线**:fa6768ff → 本轮交付 **8 个提交**(45e3cba → 953e1f1)
> **状态**:阶段0-7 结构级全交付,后端默认 legacy(灰度未切);真机 GUI 验收留操作员

---

## 一、提交清单

| 提交 | 阶段 | 内容 |
|---|---|---|
| `45e3cba` | 阶段0 | pyJianYingDraft 0.3.1 vendor 引入(commit `1e22847` 锁定,零修改,Apache-2.0,PROVENANCE 台账)+ 资源交叉表 `sdk_resource_compatibility.json` |
| `5ccdaff` | 阶段1 | `jianying_sdk/` 适配层:SDK 加载器 / DraftBuildSpec 契约 / 指纹 / 安全发布桥 / 资源注册表(ID 级解析) |
| `cb125c1` | 阶段2 | TimelineSnapshotExtractor + 视频与音频片段 SDK 编译段 + node_07 落基线 spec(两路径,失败降级);重建不变量回归 |
| `d42dd2b` | 阶段3 | 字幕迁移:SubtitleSpec + style_mapper + SDK TextSegment 编译 + node_08/08b spec 发射;**结构级 A/B 黄金对比**(SDK 导出与 legacy material 语义逐键一致) |
| `db14911` | 阶段4 | 文本装饰:动画 in/out/loop(先入后循)+ 花字双 ID 门禁 + 气泡双 ID 分传 + 不支持逐条降级 + node_10/10b 发射 |
| `6507b95` | 阶段5 | 转场**前段挂载**(§4.5 行为差异专项回归)+ 时长邻段钳制 + 特效 0-100 强度 + 视频动画 + node_09 发射 |
| `4293ed8` | 阶段6 | 贴纸独立轨 + 占位 fail-closed + AI 转场=普通视频片段(零假 transition)+ node_11 发射 |
| `953e1f1` | 阶段7 | `DRAFT_BUILDER_BACKEND` 灰度开关(默认 legacy)+ `compile_and_publish_draft` 汇合管线 + node_11 唯一编译入口 + SDK 重建稿身份稳定 |

新增单测:**80 例**(phase1 14 + phase2 20 + phase3 9 + phase4 12 + phase5 10 + phase6 7 + phase7 8),全绿;全量单测套件每阶段提交前均通过(exit 0)。

---

## 二、阶段0 资源交叉表结论(templates/sdk_resource_compatibility.json)

| 类别 | 项目资源数 | supported | 结论 |
|---|---|---|---|
| transition | 433 | 431 | **「快门」「水墨」SDK 0.3.1 枚举缺失 → unsupported**,编译器自动降级,禁止名称猜测映射 |
| video_effect | 462 | 462 | 全过(场景/人物合并索引) |
| text_intro / loop / outro | 78 / 52 / 46 | 全过 | ID 级一致,零名称漂移,零 is_overlap/默认时长字段差异 |
| flower | 1(互动立体) | 1 | effect_id == resource_id,SDK `add_effect()` 可无损表达 |
| sticker | 4 | 0 | 全部 `PLACEHOLDER_*` → `unsupported_placeholder`(fail-closed 维持) |
| 转场池覆盖 | pool_gentle 80 / zoom 35 / slide 80 / whip 108 / creative 90 | 各差 1(快门/水墨) | 池成员经"名称→库行→ID→SDK 成员"解析,unsupported 自动跳过 |

运行时依赖:`pymediainfo==7.0.1`(win wheel 自带 MediaInfo.dll,`can_parse()=True` 实测)+ `imageio==2.38.2` + `uiautomation==2.0.29`(SDK `__init__.py` 顶层 import 存在性需要),已入 requirements.txt 并钉版。

---

## 三、实现架构(与计划文档的对应与偏差)

### 3.1 与计划一致的核心决策

- **编译模式**(§0 约束 A):效果节点只写 `DraftBuildSpec`(`.autovideo_draft_build_spec.json`,与草稿同目录点前缀文件,中文/英文各一份);node_11 一次 SDK 构建,**绝不**在导入轨道追加效果。
- **安全发布边界**(§5.5):`ScriptFile.dumps()` → `validate_sdk_payload` → `safe_write_draft`(快照/双写/校验/回退/draft_mutations 审计全保留);**未使用** `ScriptFile.save()` 作生产提交。
- **AI 转场**(§0 约束 B):canonical `ai_transition_pair` 溯源保留在 `TimelineClipSpec.ai_transition_pair`;编译为普通 `VideoSegment`,零假 TransitionType(专项断言)。
- **转场前段挂载**(§4.5):SDK 编译挂边界**左**段,专项回归锁定"旧版后段挂法禁止复现"。
- **blocked 不静默丢**(§7.2):关键帧/蒙版类桶/未识别段引用/多视频轨/人工文本贴纸轨 → `spec.timeline.blocked_features`,编译抛 `UnsupportedManualFeatureError` 保留原草稿。

### 3.2 实现期补充决策(计划未列,实现必要)

1. **spec 增加 `timeline.audio_clips`**(§5.3 契约只写了视频 clips):BGM/配音/人工音频必须经编译存活(node_08 权威配音轨重写、node_12 人工 BGM 在编译**后**以原生方式叠加,编译时刻的音频全量来自当前草稿提取)。role 经 sidecar id_map 恢复,编译铺单条音轨 + `add_fade`。
2. **node_11 编译时刻用提取器重刷 timeline 节段**(不只消费 node_07 基线):node_08-10 之间的配音重写等中间变更不被 node_07 时代快照覆盖;node_07 基线保留作可检视产物。
3. **SDK 重建稿身份稳定**(阶段7 核心坑):
   - 编译器把业务 `clip_id` 写进 `VideoMaterial.material_name`(mapper 同款先例),否则 SDK 写文件名,下轮重提取 clip_id 漂移、指纹失稳;
   - 音频段 id 改内容寻址(`路径主干@目标区间`);
   - 全默认 `clip.transform` 不记入 spec(默认≠数据);
   - SDK 重建稿文本 material id 是 uuid,提取器按 content 显示文本与 spec 字幕比对认领;canonical 计划在场时另有 (路径+源范围) 回配索引。
   - 由此实现:**同内容两次编译指纹一致 → 第二次跳过**(test_pipeline_fingerprint_skip 锁定)。
4. **SDK 素材注册时序**:SDK 只在 `add_segment` 时把 transition/effects/动画/花字/气泡登记进素材桶 —— 编译器统一"先挂效果、后落轨"(阶段4/5 两个专项坑,测试锁定)。
5. **node_09 的 `video_animations` 发射置空**:编译机制就绪(`_build_video_animations` + kind 失配降级),但项目无 GUI 实证的视频动画资源池 —— 资源池待操作员采集后另案配置(与 TEXT_FLOWER 同款"机制先行、资源拍板"模式)。

### 3.3 SDK 行为差异备忘(真机验收重点)

- **花字×SDK 关系定案**(20261122 核查):**legacy 链(现网默认)零 SDK 参与** —— 花字绑定器 `nodes/_text_flower.py` 与构造器 `jy_common/native_fx.py` 无任何 SDK import;花字资源身份来自真机 rp.db 审计 + GUI 黄金样本(`templates/text_flower_resource_library.json` `_source` = `rpdb_flower_record.json`,**不是** SDK metadata 扫描产物 —— SDK 枚举本就无花字类目),material 由 `build_native_text_effect_material` 手工构造(20+ 键富形态),缓存守卫用项目自建 `cache_manifest`。**SDK 灰度链**(`DRAFT_BUILDER_BACKEND=pyjianyingdraft`)才走 `TextSegment.add_effect(effect_id)`(SDK 双 ID 合一接口,三重门禁:双 ID 一致 + cache_verified + 交叉表 `compat_status("flower")=="supported"`,不满足逐条降级)。花字渲染失败与是否用 SDK **无关**(根因 = 5.9.0.11632 旧客户端资源格式,两条写入链都只负责数据落草稿)。
- **花字/气泡落桶**:SDK 把 `TextEffect`/`TextBubble` 导出到 `materials.effects` 桶 —— 与 legacy GUI 黄金样本(console-52373c6f9e11)同桶 ✓;但 SDK 条目是 **6 键精简形态**(id/effect_id/resource_id/type/apply_target_type/value),legacy 是 20+ 键富形态(含 path=缓存目录/category_id/name)。剪映 5.9.0.11632 能否渲染精简形态 **未验证**;且本机花字渲染本就受旧客户端资源格式问题影响(`TEXT_FLOWER_ENABLED` 默认关,见花字渲染根因文档)。GUI 验收必须覆盖此差异。
- **动画 material 条目**:SDK 动画条目 id 用 effect_id(legacy 用自造确定性 id + effect_id 独立字段),语义字段(name/type/resource_id/start/duration)一致,upstream 已验证形态。

---

## 四、后端开关与灰度(§11.1)

```
DRAFT_BUILDER_BACKEND = legacy | pyjianyingdraft   # config.py,默认 legacy
```

- **legacy**(默认):一切行为与迁移前逐字节相同(效果节点直接原生写入;spec 发射是旁路增强,失败降级不阻断)。
- **pyjianyingdraft**:node_11 在 legacy 注入 + spec 发射之后调用 `compile_and_publish_draft`:读 spec(缺失 → 显式 `skipped_no_spec`,不建空计划)→ 当前草稿重刷 timeline → 指纹一致跳过 → SDK 编译 → 经 `write_draft`(审计节点 `node_11_compile_{tag}`)发布 → 回写 `spec.compile`。
- 失败语义:编译失败/blocked_features/发布异常 → `failed` 记录 + error_log,**legacy 草稿原样保留**(快照回退兜底)。
- 双语:zh/en 各自独立 spec + 独立编译;en fork 复制快照②时带走基线 spec,node_08b `set_spec_language` 接管身份。

---

## 五、遗留项与接续点

| 项 | 状态 | 说明 |
|---|---|---|
| **node_05 SDK 初始草稿构建**(计划 §8.2/阶段2 任务1) | **未实施** | 灰度默认 legacy 下收益为零、风险最高(mapper→native 转换 + auto-BGM + 发布注册 + manifest 幂等键全部锚在其产物形态上)。接续点:新建 `jianying_sdk/initial_builder.py` 从 canonical plan 直接构建 spec(clips + audio_clips 含 BGM 选择),node_05 按 backend 分派;现有 node_11 编译路径已验证同一条编译链可吃该 spec |
| 贴纸真实 resource_id | 待操作员 | `sticker_template.json` 4 个 PLACEHOLDER 维持 fail-closed;回填流程见模板 `_reverse_engineering_steps` |
| 文字气泡资源库 | 待操作员 | `templates/text_bubble_resource_library.json` 空库+回填步骤已入库,禁止猜测 ID |
| 视频动画资源池 | 待操作员 | 编译机制就绪;候选须经 GUI 实证 + 缓存预检后配置进 node_09 发射 |
| 转场池「快门」「水墨」 | unsupported | SDK 0.3.1 无此枚举;编译器自动降级;如需支持须扩展 SDK fork(补丁须登记 PROVENANCE 台账) |
| 旧 JSON 构造器退役(阶段8) | 未执行 | 按 §11.1#6:某效果类别连续通过结构+GUI+重开+双语回归后才移除对应 builder;`jy_common/native_fx.py` 现阶段**全量保留** |

---

## 六、操作员真机验收清单(§10.2/§10.3,禁止以"JSON 存在/未抛异常"替代)

1. 设 `DRAFT_BUILDER_BACKEND=pyjianyingdraft`(管理员 pwsh7 重启生效),新建测试任务跑全链至 node_12。
2. **字幕 A/B**:同任务 legacy 与 SDK 两份草稿,同画布同句对比字号/位置/描边/折行(结构级 A/B 已逐键一致,真机看渲染)。
3. **转场**:检查每个边界转场作用于**前一段**(剪映 UI 转场图标在边界左侧段上);快门/水墨任务若被选中应看到降级告警而非错挂。
4. **文本动画**:抽帧验证 in 动画;关闭重开草稿动画仍在。
5. **花字(可选)**:`TEXT_FLOWER_ENABLED=1` 下验证 SDK 精简 6 键形态在 5.9.0.11632 的渲染(预期:与 legacy 富形态对照;渲染失败归旧客户端资源格式问题,归档到花字渲染根因文档)。
6. **node_12 人工 BGM**:在 SDK 编译后的草稿上人工加 BGM → 保存 → 关闭 → 重开,后续 node_13 音量写回不损坏字幕/转场/装饰。
7. **人工高级特征阻断**:含关键帧/人工文本轨的草稿应被 `failed_blocked_features` 阻断且 legacy 草稿原样保留(error_log 可见)。
8. **双语**:zh/en 两份草稿效果一致(同 job_id 转场/动画/花字同选)。
9. 验收通过后按 §11.1#6 逐类别退役旧构造器(另一 PR)。

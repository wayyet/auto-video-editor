# SDK 链转正与六段 legacy 链退役 执行记录(2026-11-22)

> **任务来源**:用户指令(延续 sess_fc6b041c 断点):①参考 sess_051b4e40 产出的《效果工作流与 pyJianYingDraft 对比设计执行计划》与迁移执行记录附录 A;②六效果段(字幕/转场/视频特效/文本动画/贴纸/花字)从 legacy 链(默认)切到 SDK 链(灰度开);③全程保持双链;④自动化测试全绿后删除 legacy 链。
> **用户拍板**:删除门槛 = 自动化测试全绿即删(真机渲染问题后续作为 SDK 链 bug 修,回滚靠 git revert);范围 = 只做链任务(贴纸/气泡/视频动画资源收割等对比文档其余阶段不做)。
> **交付**:2 个提交 —— `f0beea1`(Phase A 灰度开)+ 本批(Phase B legacy 退役)。
> **状态**:结构级全交付;真机 GUI 验收清单(迁移执行记录 §六)仍留操作员。

---

## 一、勘察定案:三个潜伏阻断(翻转默认前必须修)

对"SDK 灰度链从未在真实任务草稿上跑过"的复核发现三个必炸点,已随 Phase A 修复:

1. **提取器我方 refs 误伤**(`jianying_sdk/timeline_extractor.py`):视频段
   `extra_material_refs` 白名单只有 4 个伴随桶(speeds/canvases/scm/vocal),
   legacy 转场/特效/贴纸 refs 一律 `unrecognized_segment_ref` → 编译必被阻断。
   `_KNOWN_BUCKETS` 常量(注释"非空不阻断")定义了却从未接线 = 半成品。
   **修复**:视频段 refs 白名单扩入我方效果桶(transitions/video_effects/
   stickers/material_animations);未知 refs 仍阻断。phase7 旧 fixture 是裸草稿
   (无 refs),故从未暴露;新增真实形态草稿回归用例锁定。
2. **开场钩子阻断编译**(`nodes/node_09b_inject_opening_hook.py`):钩子无
   spec 发射且位于 node_09→node_11 之间,hook-title-1 文本被提取器判
   `manual_text_track` → 编译阻断;白名单放行又会被重建静默丢弃。
   **修复**:图拓扑把 node_09b 后移到 node_11(SDK 编译)之后
   (node_09 → node_11 → node_09b → node_12),钩子原生写入落在编译后、
   与 node_12/13 同层;SCHEMA_VERSION 13→14 + GOLDEN_STEPS 重录。
3. **QA 对编译稿误判**(`draft_ops/draft_integrity.py` +
   `nodes/node_join_before_delivery.py`):编译稿文本 material id 是 uuid,
   `text-zh-` 前缀计数恒 0 → 有旁白任务 subtitles_zh 恒 ERROR;贴纸绑定检查
   只认视频段 refs → SDK 独立贴纸轨恒 ERROR。
   **修复**:subtitles_zh/en 在无前缀 id 时读 spec sidecar 编译回执
   (`compiled`/`skipped_fingerprint` 才认,failed 不放行);贴纸绑定改双口径
   (视频段 refs ∪ 独立 sticker 轨段 material_id)。锁定用例 3 例入
   test_draft_integrity.py。

## 二、Phase A(f0beea1):灰度开

- `config.py`:DRAFT_BUILDER_BACKEND 默认 legacy → pyjianyingdraft;
  回滚 = env DRAFT_BUILDER_BACKEND=legacy + 重启(Phase B 后开关已删,回滚改 git revert)。
- 受影响测试:node_11 legacy 断言用例显式钉 legacy(过渡措施,Phase B 已随 legacy 一起退役)。
- 11 文件 +167/−27。

## 三、Phase B:六段 legacy 链退役

原则:六段效果节点只做业务决策 + 发射 DraftBuildSpec;node_11
`compile_and_publish_draft` 是唯一效果写入通道。视频/音频轨(node_05/07)、
配音轨(node_08 音频段/node_17)、node_09b 钩子、node_13 音量淡入淡出
**不在六段范围**,原生写入保留。

| 段 | legacy 写入(已删) | SDK 链(现唯一通道) |
|---|---|---|
| 字幕 | node_08 `_sync_zh_text_materials`/`_sync_zh_text_track` + node_08b 整表替换/整轨替换 | node_08/08b → `_emit_subtitle_spec`(fail-closed)→ node_11 SDK `TextSegment` |
| 转场 | node_09 原生注入 + 挂后段 + `transition-N` 清理 | node_09 `_plan_fx_into` 纯计算 → `_emit_fx_spec` 直构(挂**前段**) |
| 视频特效 | node_09 `build_native_video_effect_material` + `vfx-N` 清理 | 同上(`VideoEffectSpec`,强度 0-100) |
| 文本动画 | node_10/10b `build_native_text_animation_material` + refs 绑定 + 清理 | node_10 选取(`_pick_intro_animation` 原样)→ `TextDecorationSpec.animations` |
| 花字 | `_text_flower` 富形态 materials.effects + UUIDv5 id + 标记键清理 | node_10 选取(三条件守卫原样)→ `TextDecorationSpec.text_effect`(编译侧三重门禁) |
| 贴纸 | node_11 `_inject_sticker_into` + `sticker-` refs + 清理 | node_11 `_emit_sticker_spec`(spec.subtitles 关键词匹配 + 19 位 fail-closed)→ SDK 独立轨 |

连带改造:
- **node_16a**:`_write_marker`(draft materials.texts[i].text_en 死通道)删除;
  marker 命中路径改读 `subtitle_en.json` 译文(`_read_segments_from_marker`,
  新函数)—— 旧 `_read_segments_from_draft` 在 fork 时机下本就读空(潜伏缺陷)。
- **交付修复节点**(`node_delivery_qa_repair.py`):transitions/text_animation
  修复动作在 spec 重发射后补 `_recompile_drafts`(指纹一致自然跳过,重放安全);
  stickers 修复 = node_11 自带编译;volume_fades 原生写不变。
- **构造器退役**(`jy_common/native_fx.py`):删 `build_native_transition_material`/
  `build_native_sticker_material`/`build_native_text_effect_material` 与
  `OUR_TRANSITION_PATTERN`/`OUR_VIDEO_EFFECT_PATTERN`/`OUR_TEXT_ANIMATION_PREFIX`/
  `OUR_STICKER_PREFIX`/`OUR_TEXT_FLOWER_*` 常量;保留 node_09b/node_13/脚本
  仍在用的视频特效/文本动画/音频淡入淡出构造器与 `is_19_digit_resource_id`。
  `nodes/_subtitle_texts.py`:删 `build_text_track`/`sync_text_track`。
- **开关移除**:config.py `DRAFT_BUILDER_BACKEND` 删除;node_11 恒 SDK 编译;
  spec 缺失仍 `skipped_no_spec` 显式跳过。
- **失败语义**:六段 spec 发射失败从"降级不阻断"改 **fail-closed 抛异常**
  (§13.2 禁静默缺段);资源级逐条降级语义不变;编译失败保留编译前快照。

## 四、测试手术

- 删除 2 文件:`test_native_text_effect.py`、`test_node_11_segment_binding.py`(只覆盖已删构造器/绑定助手)。
- 重写/裁剪:phase4(装饰发射走 spec)、phase5(node_09 计划口径)、
  phase6(node_11 spec 直发)、phase7(SDK-only,开关用例改"开关已删"断言)、
  test_native_fx、test_subtitle_texts_native(轨道/读取测试退役,新增 marker
  读取)、test_node_text_flower(选取器口径)、test_node_08/08b/09/10/11
  (观察点 draft materials → spec)、集成 test_text_flower_dual_draft(spec 口径)。
- 新增:提取器真实形态回归 2 例(phase2)、config 开关已删断言(phase7)、
  SDK 编译稿 QA 锁定 3 例(test_draft_integrity)。
- **集成 fixture 适配(SDK 编译闸门暴露)**:种子草稿的视频段此前无
  material 联结,legacy 时代无所谓,SDK 编译器(正确地)按 dangling 拒绝 ——
  reorder/sequential fixture 补 `material_id`+`material_name`;集成 conftest
  加 autouse SDK 素材替身(继承真类 + 1h 时长,phase7 同款模式);reorder 两
  用例断言改编译稿形态(uuid id 按轨道归属/显示文本断言)。
- **语义回改**:node_09 模板库缺失保持"降级通过 + 响亮 error_log + QA 闸门
  兜底"(原 fail-closed 打了合法无模板的集成测试环境;生产模板恒在,真缺失
  时 QA 必拦,不静默);node_09b `_first_subtitle_text` 改 spec 优先 + 草稿
  前缀扫描兜底(编译稿 uuid id 使前缀扫描失效,hook 标题拾取恢复)。
- **闸门结果(终)**:
  - 全量单测 `pytest tests/unit -q` **exit 0 零失败**;
  - 集成 `JY_WRITE_FAIL_ON_JIANYING_RUNNING=0 pytest tests/integration -q`
    **恰好 3 失败 = 基线存量**(auto_mode split_shots 检出 0 镜头 /
    tail_chain transitions>0 / sequential"字幕应被注入"无旁白夹具),
    stash 对照法在 HEAD 逐字复现同因,零新增回归;sequential 的转场/特效
    断言反而由编译转绿;
  - 集成环境:PG docker 容器 + 写入互斥放行(剪映在跑;仅测试进程 env,
    测试草稿全部落在 pytest tmp 目录,不触真实草稿库)。

## 五、已知边界(记录不修)

1. 编译发布成功后 node_11 重跑(无 done tag 的异常窗口):重提取编译稿会因
   SDK 贴纸轨判 `manual_sticker_track` 阻断 → fail-closed,草稿完好(编译前
   快照即编译稿);指纹跳过被 blocked_features 参与指纹所 defeating,属罕见路径。
2. 人工在 node_11 编译**前**手工加入草稿的贴纸/转场(正常任务不存在此窗口,
   仅异常恢复场景):spec 无此条目且提取器不判 manual(贴纸以 refs 形态存在)
   → 重建时被清退。与灰度期行为一致,非本次引入。
3. node_09 的 `video_animations` 发射恒空(资源池待操作员 GUI 实证,机制就绪,
   与迁移执行记录 §3.2.5 同案)。

## 六、操作员真机验收(迁移执行记录 §六清单继续有效)

1. 重启服务(新默认生效)后新建任务跑全链至 node_12。
2. 重点:字幕/转场(前段!)/特效/动画/花字在剪映 5.9.0.11632 的**真实渲染**;
   关闭重开不丢;双语一致;node_12 人工 BGM 后 node_13 写回不损坏编译稿。
3. 花字 SDK 6 键精简形态渲染为已知未验证项(与 TEXT_FLOWER_ENABLED 资源门禁
   正交;渲染失败归旧客户端资源格式问题,见花字渲染根因文档)。
4. 紧急回滚:`git revert` 退役提交 + 重启服务。

## 七、遗留与接续点

- 贴纸真实 resource_id 回填(sticker_template.json 4 个 PLACEHOLDER,
  fail-closed 维持)、气泡资源库、视频动画资源池:待操作员 GUI 收割(D2 决策,
  另案)。
- node_05 SDK 初始草稿构建:未实施(不在六段范围;接续点见迁移执行记录 §五首项)。

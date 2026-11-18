# 字幕动画丢失修复 —— 正确性审查分阶段实施执行记录(20261119)

- 依据:`docs/第八期开发/auto-video-editor_字幕动画丢失原因分析与修复计划_正确性审查及分阶段执行计划_20261119.md`(下称「审查文档」)
- 实施基线:`main@09c297d6cb36273f5f5a44b5a45ccb280b848dbc`(Part A 已入库)
- 本文档性质:**代码修复执行记录**;GUI 实证收割与真机 E2E 为操作员动作,见 §5 遗留。

---

## 1. 阶段实施明细

### 阶段 1(P1-01)字幕节点非破坏性降级 ✅

- `nodes/node_10_inject_text_fx.py::_apply_text_animations`:`_cleanup_stale_animations` 只在 `anim_meta is not None`(本轮已选中并写入新动画)分支执行;无可用资源时保留草稿已有的 `text-anim-*` 素材与轨段引用,仅记降级告警。docstring 同步补「降级非破坏性」口径。
- 新增用例(`tests/unit/test_node_10_inject_text_fx.py`):无可用资源保留旧动画/旧引用、缓存根不存在保留、人工 uuid 动画不动、新动画替换旧条目+清理孤儿、人工动画在成功重写时不受清理 —— 审查 §7 阶段 1 测试表全覆盖(重放稳定性由既有 byte-identical 用例覆盖)。

### 阶段 2(P1-02)标题动画 verified 双闸 + 非破坏恢复 ✅

- `jy_common/hook_title.py`:「冲屏位移」标 `verified: true` 并携带 `verification` 记录(2026-11-19 GUI 实证:31a8a8e9edd2 真机验收 + console-b0b2a64a78c6 截图);「放大」「渐显」无实证不标记(不可选)。
- `nodes/node_09b_inject_opening_hook.py`:候选选择改为 `verified + 缓存存在` 双闸;`_inject_hook_into` 在 cleanup 前留存上一轮标题动画条目,本轮候选不可用时**恢复**旧条目并重新绑定(非破坏),无旧动画才降级纯标题;摘要新增 `animation_restored`。
- 用例(`tests/unit/test_node_09b_inject_opening_hook.py`):缓存缺失降级标题保留(含标题素材断言)、候选未验证但目录存在不写入、候选全不可用时旧标题动画恢复重绑。

### 阶段 3(P1-03)verified 按资源身份继承 + verification 记录 ✅

- `scripts/build_resource_library.py::_carry_over_verified` 重写:按 `(name, resource_id, effect_id, md5)` 身份匹配;继承还要求旧条目 `verification.status == "verified"`(无来源布尔不可审计);同名身份变化 / 记录缺失或损坏 → 新条目写 `verification.status=stale`(带 `previous_identity`),**不**继承;旧条目从未 verified → 完全不动(保证生成器幂等)。统计输出 `verified_carried`/`verified_staled`。
- `templates/text_resource_library.json`:兔子弹跳/逐字旋入补 `verification` 记录(verified_at/jianying_version/resource_identity/evidence_path/note)。
- 用例(`tests/unit/test_build_resource_library.py`):同身份继承、同名换 effect_id/md5 转 stale、无同名不转移、记录损坏安全降级、端到端字节级幂等;`tests/unit/test_native_fx.py` 补 `text_animation_cache_available` 边界(目录缺失/是文件/空身份字段/空根)。
- ⚠️ 实施中发现并修正:stale 分支最初会污染从未 verified 的条目(第一次生成的库第二次重跑被改写)→ 收紧为「仅曾 verified 的条目才可能转 stale」,幂等测试回归通过。

### 阶段 5(P2-01/P2-02)选取审计 + 导出拆分 ✅

- `node_10::_pick_intro_animation` 返回 `(条目, 审计)`:池大小/可用数/seed 起点 index/最终选中 index/回退偏移/缓存根/模式;`apply_text_fx` 的 status_log 标签扩展为 `node_10_text_anim_selected:{name}@{effect_id}(pool=…,usable=…,start=…,pick=…,offset=…,mode=…)` —— 事件流可直接复算选择结果。zh/en 同 seed 同可用集 → 审计一致即证明同资源身份(审查 §6.3 的「固化到 state」方案会引入同超步并行写冲突,采用审计对账替代,见 §4 取舍)。
- `draft_ops/draft_exporter.py`:字幕(`text-anim-*`)与标题(`anim-hook-title*`)入场动画**分开计数**;只统计**实际绑定到轨段**(extra_material_refs 引用)的素材,孤立素材不计。
- 用例:审计可从池 JSON+缓存根独立复算;exporter 拆分/绑定口径/无动画无行。

### 阶段 0(P1-05 部分)诊断基线 ✅(静态部分)

- 新增 `jy_common/cache_manifest.py`(逐文件相对路径/大小/SHA-256 + config 版本 + 清单摘要,同内容稳定)。
- 新增 `scripts/diagnose_text_anim_cache.py`(只读):环境(Windows 用户/Path.home/JIANYING_CACHE_ROOT/与 GUI 默认缓存一致性)+ 固定样本缓存 manifest + zh/en 草稿备份(SHA-256)+ 剪映日志清单;`--label` 支持四时点快照。
- 本机基线已收集:`outputs/diagnostics/text-animation-cache-baseline-20261119/baseline-20261119T015524/`。**新证据:兔子弹跳 config v6.6.0(10 文件)正常显示** —— 同为 6.x 格式系可用,削弱「客户端版本不兼容」假设;螺旋上升(4 文件,v6.0.0)主线收敛到「资源包下载不完整/内容异常」,按审查要求仍并列验证(四时点快照待 GUI 会话)。

### 阶段 4(P1-04)全池 GUI 实证收割工具 ✅(工具部分)

- 新增 `scripts/harvest_text_fx.py`:`list`(池验证状态概览)/ `gen`(小批次收割 staging + `--publish` 经 `draft_ops.draft_publisher` 发布,剪映在跑 fail-closed;含 `new_version` 直通标记与 `harvest_plan.json`)/ `manifest`(缓存清单)/ `apply-verification`(受控回填:身份四元组精确匹配、verified 必带 `evidence_path`、failed 分类移除 verified、写前自动备份、`--dry-run`)。**工具不伪造 verified**。
- 已生成小批次实验载体(未发布):`outputs/harvest/staging-20261118T175645/`(螺旋上升+兔子弹跳+逐字旋入 3 条)—— 用于审查 §4.2 的「打开草稿是否触发全量下载」触发条件实验。
- 用例(`tests/unit/test_harvest_text_fx.py` + `tests/unit/test_cache_manifest.py`):staging 结构/绑定/双文件一致;回填的验证必填证据/幽灵身份拒绝/failed 撤销 verified/dry-run 不落盘/身份顺序锁定。

## 2. 测试与对照

| 套件 | 结果 |
|---|---|
| 目标文件(node_10/09b/native_fx/build_resource_library/draft_exporter/harvest/cache_manifest/hook_title/job_identity) | 全绿(累计新增 ~24 用例) |
| 全量 `tests/unit` | 除 1 例外全绿:`test_inject_fx_deterministic_across_pythonhashseed` 失败 = 剪映运行中的环境性失败(tasklist 实证 JianyingPro.exe ×3;与记忆基线 2159/1F、2408/1F 同源);按审查 §6.1 不关守卫,待关闭剪映后复跑确认 0F |
| 全量 `tests/integration` | 8 失败(week4×5 + week5×3);**stash 对照**(移除本批改动后同批失败逐字重现)= 存量/环境性,零新增。其中 week4 互斥隔离用例此前记忆为"5/5",当前失败疑与剪映运行相关,关剪映后应复跑复核 |

## 3. 优先级对照(审查 §8)

| 编号 | 状态 |
|---|---|
| P1-01 非破坏性降级 | ✅ 代码+测试 |
| P1-02 标题 verified 双闸 | ✅ 代码+测试(manifest 级校验归 P2-03) |
| P1-03 身份继承+验证记录 | ✅ 代码+测试 |
| P1-04 全池 GUI 收割流程 | ✅ 工具+规程;**78 条 GUI 实证待操作员** |
| P1-05 版本/权限/网络/manifest 证据 | ◐ 静态基线已收;四时点快照+VIP/网络验证待 GUI 会话 |
| P1-06 Worker/GUI 缓存根一致 | ◐ 诊断脚本已输出一致性检查;真机确认待操作员 |
| P1-07 真机双语 E2E | ⬜ E2E-01~10 待操作员 |
| P2-01 审计 seed/回退 | ✅ |
| P2-02 导出分开计数 | ✅ |
| P2-03 manifest 失效机制 | ◐ manifest 生成已就绪;任务前预检未接入流水线(收割完成后接) |
| P2-04 生成器/缓存判定测试 | ✅ |
| P2-05 原文档状态更新 | ✅(第七期文档顶部修订注记) |

## 4. 设计取舍记录

1. **zh/en 选择固化不做 state 键**:node_10/10b 同超步并行,zh 先算 en 后读会引入键冲突/顺序依赖;改用「审计标签对账」证明同一资源身份(池+可用集不变时同 seed 必同选)。若未来需要强固化,应在 zh/en 超步之前的节点计算并写入 state。
2. **stale 只标给曾 verified 的条目**:否则第一次生成的干净库在第二次重跑被改写,破坏幂等(实施中已修正并留回归测试)。
3. **标题候选 manifest 校验暂缓**:hook 候选为代码内置三元组,manifest 摘要回填依赖 GUI 实证产出;待收割会话统一补。

## 5. 遗留(操作员动作,按序)

1. 关闭剪映 → 复跑全量 unit(预期 0F)+ week4/week5 集成复核;
2. 收割小批次实验:`python scripts/harvest_text_fx.py gen --names 螺旋上升,兔子弹跳,逐字旋入 --publish`(载体已备好,发布需剪映关闭)→ 打开草稿观察各步骤是否触发下载(审查 §4.2)→ 确定触发方式后分批验证 78 条;
3. 每条按 §4.3 规程:选中→确认动画名→预览无丢失徽章→保存→重开再预览→`manifest --effect-id X --md5 Y --out ...` 留档 → 结果 JSON → `apply-verification` 回填(先 --dry-run);
4. 螺旋上升坏缓存处置(审查 §4.5):**先隔离备份再受控重试**,不直接删除;
5. 真机 E2E-01~10(审查 §6.2 表);
6. 服务重启使本批代码生效(`restart-video-editor` 技能);存量草稿 b0b2a64a78c6 zh/en 由操作员在剪映手动换可用动画恢复;
7. P2-03 收尾:收割完成后把「任务开始前缓存 manifest 预检」接入流水线。

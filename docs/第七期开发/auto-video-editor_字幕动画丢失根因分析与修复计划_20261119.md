# 字幕显示动画「动画丢失」根因分析与修复计划(20261119)

> **2026-11-19 修订注记(正确性审查后实施,本节取代下文 §6 的过期状态)**:
> ①Part A 已随 `09c297d` 提交入库;②第八期审查(`../第八期开发/auto-video-editor_字幕动画丢失原因分析与修复计划_正确性审查及分阶段执行计划_20261119.md`)指出的补强项已实施:P1-01 非破坏性降级、P1-02 标题 verified 双闸、P1-03 verified 按资源身份继承+verification 审计记录、P1-04 收割工具(`scripts/harvest_text_fx.py`)+P1-05 部分诊断基线(`scripts/diagnose_text_anim_cache.py`+`jy_common/cache_manifest.py`)、P2-01 选取审计可复算、P2-02 导出字幕/标题分开计数,执行记录见 `../第八期开发/auto-video-editor_字幕动画丢失修复_审查分阶段实施执行记录_20261119.md`;
> ③阶段 0 诊断新证据:**兔子弹跳缓存 config v6.6.0(10 文件)在剪映 5.9.0.11632 正常显示** —— 同为 6.x 格式系可用,削弱「客户端版本不兼容」假设,根因主线收敛到「该资源包下载不完整/内容异常」,仍按审查要求并列验证;
> ④§2 根因第 3 步「下载不完整」按审查修订为「剪映未能成功加载该动画资源」(下载失败/版本不兼容/权限/网络并列验证);
> ⑤遗留:全池 GUI 实证收割(小批次触发条件实验→逐批验证→apply-verification 回填)、真机 E2E-01~10、关闭剪映后复跑全量 unit、服务重启生效 —— 均留操作员;
> ⑥本文档 §6 的「未提交工作区」「node_09b 测试未重跑」等表述已过期,以本注记与执行记录为准;§8 收割名单待 GUI 实证后回填。

> 触发:剪映草稿 `AutoVideo_console-b0b2a64a78c6`(任务 console-b0b2a64a78c6,2026-11-18 23:39 起,00:06 完成)打开后,字幕轨全部字幕段显示「⚠动画丢失」徽章;标题段(冲屏位移)正常。
> 结论先行:**字幕动画池 78 条只是 vendor 元数据扫描,资源从未下载**;本任务按 seed 命中未下载的「螺旋上升」,剪映打开时按需下载只落了 4 个不完整文件,加载失败 → 全部字幕段丢动画。写入守卫 + 全池收割的修复计划已获批准,**实施进行中被中止**,断点见 §6。

---

## 1. 现象

- 剪映 5.9.0.11632(便携版)打开草稿 `AutoVideo_console-b0b2a64a78c6`(zh),字幕轨 7 条字幕段(`text-zh-1..7`)全部显示「⚠动画丢失」;开场标题段(冲屏位移)正常显示。
- 同任务 en 分支草稿 `en_branch_console-b0b2a64a78c6_20261118_235720_944093` 尚未被剪映打开过,内容为流水线原始输出(打开后预期同样丢失)。
- 昨天(2026-11-18)池扩容的 GUI 冒烟(`AutoVideo_pool-smoke-20261118`)是**通过**的——见 §4,这是幸存者偏差,不是矛盾。

## 2. 根因链(六步,编号对应 §3 证据)

1. **池无下载保障**。`templates/text_resource_library.json` 的 78 条 intro VIP 入场动画由 `scripts/build_resource_library.py` 用 AST 解析 `vendor/pyJianYingDraft/metadata/text_intro.py` 一次性 dump 而来,条目只有 6 个字段:`name/is_vip/duration_s/resource_id/effect_id/md5`。全池**没有任何条目因池构建而被下载**;仅「兔子弹跳」「逐字旋入」2 条在此前 GUI 会话中偶然被应用过(有下载证据)。
2. **seed 命中未下载条目**。本任务 job_id=`console-b0b2a64a78c6`,auto 模式按 `sha256(job_id + "|text_intro_pool")` 前 8 位十六进制转 int 对池长取模,选中「螺旋上升」(effect_id=`1644278`,md5=`d3e255df67130866e0d2de42a703637c`,resource_id=`6799873891352187406`)。zh/en 两草稿选中一致 → **4f28e61 job_id 修复工作正常**,本次是它暴露出的另一层新缺陷(资源可用性),与 job_id 链路无关。
3. **按需下载不完整**。流水线把 effect_id/md5 写入动画条目(path 为空);本机缓存 `Cache/effect/1644278/d3e255df…` 在草稿写入时不存在。剪映打开草稿(日志会话 `JianyingPro_2026-11-19T00-14-15.log`)发现资源缺失,按需下载到该路径,但**只落了 4 个文件**:`RotateFlyIn.lua`(7261B)/`anim.prefab`(500B,二进制)/`config.json`(63B,**version 6.0.0 新格式**)/`content.json`(60B)。对照本机正常动画缓存——逐字旋入 13 文件、兔子弹跳 10 文件、冲屏位移 15 文件,均含 `main.scene`/`lua-meta.json`/`sticker.config`/材质贴图全套——该包明显不完整,加载失败。全机 6.0.0 版 config 的缓存**仅此一个**,没有可用的新格式对照。
4. **剪映重写草稿**。保存时规范化 `material_animations`:补 `anim_adjust_params/panel/request_id`,剥掉我们写入的 `effect_id/md5/trailable`,给资源组首条填本地缓存 path。所有挂该资源的字幕段打「动画丢失」徽章。
5. **标题不受影响**。「冲屏位移」(免费内置,`jy_common/hook_title.py` HOOK_ANIMATION_CANDIDATES[0],effect_id=`1643884`)缓存早已存在,正常加载。
6. **en 草稿同命运**。en 草稿是流水线原始输出(`trailable`×7 + `effect_id:"1644278"`×7 指纹在),打开后会经历同样的按需下载与丢失。

## 3. 证据清单

| # | 证据 | 位置/取值 |
|---|------|-----------|
| E1 | zh 草稿动画条目(剪映重写后) | 7 条 `text-anim-text-zh-N`,name=螺旋上升,resource_id=6799873891352187406,首条 path=`C:/Users/wayyet/AppData/Local/JianyingPro/User Data/Cache/effect/1644278/d3e255df67130866e0d2de42a703637c`,其余 path="" |
| E2 | 损坏缓存(4 文件,2026-11-19 00:15 创建=剪映打开时刻) | `Cache/effect/1644278/d3e255df67130866e0d2de42a703637c/`,仅 RotateFlyIn.lua/anim.prefab/config.json(v6.0.0)/content.json |
| E3 | 正常缓存对照 | 逐字旋入 `13137035/9838c677…`(13 文件,11-18 02:18);兔子弹跳 `8398145/5d157129…`(10 文件);冲屏位移 `1643884/fd73ffc2…`(15 文件) |
| E4 | 剪映重写指纹 | zh 草稿 `trailable`=0 处(我们必写)、`anim_adjust_params`=1 行(我们不写);冒烟草稿同样指纹;en 草稿 `trailable`×7 + `"effect_id": "1644278"`×7 = 原始输出 |
| E5 | 池条目 | 「螺旋上升」effect_id=1644278 / md5=d3e255df…(按 name 定位,与 E1/E2 完全吻合) |
| E6 | 池来源 | `scripts/build_resource_library.py`(AST 扫描 vendor metadata);text 库 `_counts`: intro_vip=78 / loop_vip=52 / outro_vip=46;收割机制(UI 应用→入库)只存在于 BGM 线,字幕动画线没有 |
| E7 | 日志 | `User Data/Log/JianyingPro_2026-11-19T00-14-15.log`(409KB)内**无** 1644278/6799873891352187406 痕迹(下载子系统不落该日志);`User Data/Download/` 无分片残留 |
| E8 | 关键代码 | 选取:`nodes/node_10_inject_text_fx.py` `_pick_intro_animation`;条目构造:`jy_common/native_fx.py` `build_native_text_animation_material`(写 effect_id/md5,path 空);标题:`nodes/node_09b_inject_opening_hook.py`;QA repair 复用同一核心(`node_delivery_qa_repair.py:48-55`) |
| E9 | 导出口径 | 剪映填 path **不是**成功信号——螺旋上升坏包也被填了 path(E1)。判定可用性必须用「缓存完整性 + UI 无丢失徽章」双信号 |

## 4. 为什么昨晚冒烟通过(幸存者偏差)

冒烟草稿恰好选中「逐字旋入」——此前 GUI 会话已下载(缓存 11-18 02:18 就位)。转场/特效池显示正常同理:凡真机验收过的名字,资源都已被 GUI 应用下载过。**「元数据在库」≠「资源已在本机缓存」**;字幕动画池是同晚扩容的,但除 2 条偶然被应用过的名字外没有做全池 GUI 应用。注:此前记忆里「三屏放大/翻页II/雪花光斑/逐字旋入存活」中的前三个属**转场/特效池**,不是字幕动画池,字幕动画池仅逐字旋入验证过。

## 5. 修复计划(20261119 已批准)

### Part A 写入守卫(fail-closed + 确定性回退)

1. 池条目加 `verified` 标志(= GUI 实证可渲染;初始仅兔子弹跳/逐字旋入)。下载≠可渲染,verified 只能由 GUI 实证授予。
2. `config.py` 新增 `JIANYING_CACHE_ROOT`(env 可覆盖,默认 `~/AppData/Local/JianyingPro/User Data/Cache/effect`)。
3. `node_10` `_pick_intro_animation` 守卫:auto 模式 seed 定位**全池**起点,按池序轮转取第一个「verified 且缓存目录存在」条目;无合格者 → 返回 None 走既有降级路径(不写动画+warning+degraded 计数)。fixed 模式同样过守卫,兜底链收紧为「可用子集池序第一个」。en(node_10b)与 QA repair 共用 `apply_text_fx` 自动获得一致行为。
4. `node_09b` 标题动画同守卫:候选按序取第一个缓存存在的,全缺则降级不写动画(标题保留)。
5. 审计:选中动画以 `status_log` 标签 `{node_tag}_text_anim_selected:{name}@{effect_id}` 落事件流(不复活已死的 text_fx_summary 键);导出 fidelity 增加字幕入场动画不渲染明细行。
6. 生成器 `build_resource_library.py` 重生成时按 name 继承 verified 标志(防止重扫抹掉 GUI 实证)。

### Part B 全池收割(computer-use 实证)

1. 先删损坏半截缓存 `Cache/effect/1644278/d3e255df…`(删前复核目录内容),给「螺旋上升」一次公平重试。
2. 新脚本 `scripts/harvest_text_fx.py` 生成收割载体草稿 `AutoVideo_textfx-harvest-20261119`:78 个字幕段各挂一个不同候选动画(复用 `build_native_text_animation_material`)。
3. computer-use 打开草稿(打开即触发全量按需下载)→ 滚动时间线截图检查「动画丢失」徽章;失败者进重试稿;仍失败者记「不可用名单」。判定=缓存完整性(文件数/体积阈值)+ UI 无丢失徽章**双信号**(E9);抽 3 条动画面板目检复核。
4. 通过者回填池 JSON `verified:true`,失败者保持无标志(守卫自动排除)。
5. UI 操作注意:Qt surface 点击前重新观察、Esc 触发重建、编辑器✕=回首页;**收割期间剪映开着时 pytest 确定性子进程测试会环境性失败,勿当回归**,测试安排在收割前后。

### Part C 文档与收尾

分析文档入库(本文)+ 存量草稿处置(b0b2a64a78c6 zh/en 由操作员在剪映手动换任一可用入场动画即恢复,不写回填工具)+ 中文分主题提交 + `/sync-github` 单独推送(不自动推)+ `restart-video-editor` 技能重启服务生效 + 更新项目记忆。

## 6. 实施状态快照(截至 2026-11-19 中止时,接续必读)

### 已落盘的代码改动(未提交,工作区状态)

| 文件 | 改动 |
|------|------|
| `config.py` | `TEXT_FX_INTRO_POOL_MODE` 之后新增 `JIANYING_CACHE_ROOT`(env `JIANYING_CACHE_ROOT` 可覆盖) |
| `jy_common/native_fx.py` | 新增 `text_animation_cache_available(anim_meta, cache_root)`(effect_id+md5 → `Cache/effect/<effect_id>/<md5>` 目录存在性);`from pathlib import Path` 导入 |
| `jy_common/template_library.py` | 新增 `list_text_animation_rows(kind, vip_only)` 返回完整行(保序复制,池序=文件序) |
| `nodes/node_10_inject_text_fx.py` | 新增 `_intro_usable`(verified + 缓存双闸);`_pick_intro_animation` 改为守卫版(seed 定位全池起点→池序轮转取第一个可用;fixed 兜底=可用子集第一个);`_apply_text_animations` 摘要加 `selected`;`apply_text_fx` 落 `{node_tag}_text_anim_selected:{name}@{effect_id}` 标签;docstring 补守卫段 |
| `nodes/node_09b_inject_opening_hook.py` | 标题动画改候选序 + `text_animation_cache_available` 守卫,全缺降级不写(warning 走 **error_log**,与 node_10 的 status_log 通道不同);`summary["animation_written"]` 相应 0/1 |
| `draft_ops/draft_exporter.py` | fidelity 新增 `n_text_anim` 计数(前缀 `text-anim-`/`anim-hook-title` 且 animations 非空)→「N 条字幕入场动画未渲染进 MP4(剪映客户端内可见)」 |
| `scripts/build_resource_library.py` | 新增 `_carry_over_verified`;main 在写文件前从已有 fx/text JSON 按 (section,name) 继承 verified;counts 打印加 `verified_carried` |
| `templates/text_resource_library.json` | 兔子弹跳、逐字旋入两条加 `"verified": true`(md5 行后) |
| `tests/conftest.py` | 新增 autouse fixture `_hermetic_anim_cache`:缓存根指到 tmp 目录,伪造真实池 verified 条目 + HOOK_ANIMATION_CANDIDATES[0] 的缓存目录;patch `nodes.node_10_inject_text_fx.JIANYING_CACHE_ROOT` 与 `config.JIANYING_CACHE_ROOT` |
| `tests/unit/test_node_10_inject_text_fx.py` | 新增 `_usable_intro_rows`;`_expected_pool_member` 升级守卫版镜像(seed 定位→轮转到第一个可用);fixed 模式用例断言改为「可用子集池序第一个」;追加 4 用例:walks_past_unverified / verified_but_cache_missing / no_usable_degrades / selected_audited_in_status_log |
| `tests/unit/test_node_09b_inject_opening_hook.py` | 新增 `test_title_animation_cache_missing_degrades_title_kept`(断言 error_log 通道) |

### 测试状态

- `test_node_10_inject_text_fx.py`:**21 例全过**(含图级 2.8/入口级 2.6-2.9,经守卫版镜像公式自动对齐)。
- 相邻 9 文件(node_09b/native_fx/jy_common_template/template_library_fx_pools/build_resource_library/draft_exporter/node_18/hook_title/job_identity):**105 例全过**。
- `test_node_09b_inject_opening_hook.py`:新增守卫用例刚把断言从 status_log 修正为 **error_log** 通道,**修正后尚未重跑**。
- 全量 unit 回归、集成测试:**未跑**。

### 未完成项(接续点)

1. 重跑 `tests/unit/test_node_09b_inject_opening_hook.py` 确认修正后全绿;跑全量 unit 基线对照(判新失败用 worktree 对照法;剪映开着时确定性子进程测试环境性失败勿当回归)。
2. Part B 全流程(删坏缓存→收割脚本→UI 实证→回填 verified)。
3. Part C(本文档入库已完成一半——文档本身、提交、重启服务、记忆更新)。
4. 生成器继承逻辑补一个单测(现有 test_build_resource_library.py 未覆盖 `_carry_over_verified`)。

## 7. 验证清单(实施完成 + 真机)

1. 单测:目标文件全绿 + 全量 unit 与基线 worktree 对照零新增失败。
2. seed 命中 index 复算(sha256("console-b0b2a64a78c6|text_intro_pool")%78)记入文档备查。
3. 收割回读:verified 名单 + 不可用名单落 §8;抽 3 条动画面板目检。
4. 真机:新建任务跑一轮 → 剪映打开 → 字幕段无「动画丢失」徽章,选中动画应为 verified 子集成员;zh/en 动画一致;导出报告出现字幕动画 fidelity 明细行。
5. 服务重启(`restart-video-editor` 技能)后启动日志无新错误。

## 8. 收割结果(待 Part B 填写)

- verified 名单:初始 = 兔子弹跳(8398145)、逐字旋入(13137035);全池收割结果待填。
- 不可用名单:待填(螺旋上升 1644278 待删坏缓存后重试再定性)。

## 9. 遗留与后续

- 存量草稿 `AutoVideo_console-b0b2a64a78c6`(zh/en)需操作员在剪映里手动换可用入场动画;直接删字幕段动画重挂即可,无需回填工具。
- 本机 `Cache/effect` 约 250 个资源目录是历次 GUI 会话积累;`config.py:645-646` 已有「VIP 联网资源缓存禁删」决策,守卫依赖该纪律。
- 花字审计修订版 P0-3 遗留的「缓存缺失/未登录/无订阅/断网降级行为采样」仍未执行,本次收割可作为其一部分。

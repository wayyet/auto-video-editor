# 草稿 52373c6f9e11 字幕 VIP 动画核验与 job_id 链路修复计划

> 日期:2026-11-18
> 核验对象:剪映草稿 `AutoVideo_console-52373c6f9e11`(中文主稿)与 `en_branch_console-52373c6f9e11_20261118_064813_970710`(英文分支稿)
> 触发方式:用户截图(剪映「动画→入场」面板)+ 口头提问「字幕文字有没有使用剪映里的 VIP 动画?」
> 结论先行:**两支草稿字幕 100% 使用了 VIP 入场动画,资源就位、渲染正常**;但暴露出违背 7c0bbc7 设计不变量的两个 job_id 链路缺陷,本文档同时给出 P0 修复计划。
>
> **2026-11-18 修订注记**(设计审查后):①本文档的修复计划(第五节)为**初版**,已经设计审查修订,权威实施方案以《[auto-video-editor_字幕VIP动画job_id链路_设计审查与修订执行计划_20261118.md](auto-video-editor_字幕VIP动画job_id链路_设计审查与修订执行计划_20261118.md)》为准 —— 关键修订:任务 ID 权威源改为 LangGraph `config["configurable"]["thread_id"]`(非新增 state 字段)、`state.session_id` 兼容旧 checkpoint、公共 resolver 移至 `jy_common/job_identity.py`(禁止 node_09→node_10 依赖)、末级路径兜底剥 `AutoVideo_` 前缀、禁止 en 目录上级名兜底;②优先级定级由 P0 修订为 **P1**(功能一致性缺陷,非系统可用性故障);③第一节/第二节中的草稿实测、缓存路径、剪映 UI 徽标与渲染结论均为**作者本机实测报告**(grep/sha256sum 取证),非 git 仓库可独立复现的事实;④时间线佐证(目录时间戳/mtime)为辅助证据,不构成"产物来自新代码"的严格证明。

---

## 一、截图解读

截图为剪映打开 `Video console-52373c6f9e11` 草稿的编辑界面:

- 右侧面板停在「动画 → 入场」标签页,当前选中的字幕片段套用的入场动画 tile 带 **VIP 徽标**,动画时长显示 **0.5s**;
- 时间轴上有 **两条文本轨**:上面一条是开场钩子大字「哋的一下」,下面一条是 5 条正文字幕(「哋的一下,大字同时压屏。」「暮色里的白色尖尖拱门挤进我眼里,暖灯亮起。」等);
- 其余轨道:主视频轨(带 特效-编辑/变速 4.1X、7.0X、1.9X、3.5X 的片段与转场)、BGM 轨(Island Sway 3)、配音轨(voice_over_0001/0002.wav)。

0.5s 这个时长是后续与草稿 JSON 比对的关键线索(见下文,「兔子弹跳」= 0.5s,「逐字旋入」= 1.5s)。

## 二、草稿实证(draft_content.json)

草稿路径(剪映草稿库根 = `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\`):

| 草稿 | 动画载体 | 动画名 | 时长 | resource_id | effect_id | md5 |
|---|---|---|---|---|---|---|
| 中文主稿 | text-seg-1 ~ text-seg-5(5/5 条字幕) | **兔子弹跳**(type=in) | 500000μs = 0.5s | 7187785892382118461 | 8398145 | 5d157129853f479ea15f75075c1ef070 |
| 英文分支稿 | text-en-1 ~ text-en-5(5/5 条字幕) | **逐字旋入**(type=in) | 1500000μs = 1.5s | 7229520427196879421 | 13137035 | 9838c6772ddbd48126f30baf74200f99 |

要点:

1. 动画 id 是我方确定性 id(`text-anim-text-zh-N:in` / `text-anim-text-en-N:in`,挂在文本片段的 `extra_material_refs`),**不是人工在 GUI 点选产生的 uuid** —— 即动画由流水线 node_10 写入,非手工。
2. 中文稿动画对象带本地缓存 `path: ".../Cache/effect/8398145/5d157129..."` —— 资源已下载就位,渲染无 VIP 拦截问题。
3. 与库核对(`templates/text_resource_library.json` intro 段):「兔子弹跳」条目 `is_vip: true`、`duration_s: 0.5`、resource_id/effect_id/md5 **四项逐字吻合**;「逐字旋入」同为库内 VIP 条目。intro 段共 78 条、全部 `is_vip: true`(`_counts.intro_vip = 78`)。
4. 开场钩子大字「哋的一下」**无动画** —— node_10 只对"我方字幕"(text-seg-* / text-en-* 前缀)注入,钩子不在前缀范围,属设计行为,不算缺陷。

**回答用户问题:有。两支草稿的每一条字幕(5/5 + 5/5)都使用了剪映库里的 VIP 文字入场动画**,与截图 UI 的 VIP 徽标互相印证。

## 三、但发现两个缺陷(违背 7c0bbc7 设计不变量)

提交 7c0bbc7(2026-11-18「feat(字幕动画): 入场动画全库分池 auto 模式 —— 78 VIP 按 sha256(job_id) 每任务选 1」)的设计不变量:

- 同一支视频所有字幕用同一动画;
- **zh/en 双草稿动画一致**;
- 动画按 `sha256(job_id + "|text_intro_pool")` 确定性轮换(job_id 语义 = thread_id),跨任务多样化。

本次实测违背了两条:

### 缺陷 1:zh/en 动画不一致

中文稿「兔子弹跳」(idx 4) vs 英文稿「逐字旋入」(idx 63)。

### 缺陷 2:选中值偏离 thread_id 语义,且英文轮换完全失效

按设计语义计算,`job_id = "console-52373c6f9e11"` 应得池内第 **58** 号「跳跳捣蛋鬼」;实际:

- 中文稿按 seed 输入 `"AutoVideo_console-52373c6f9e11|text_intro_pool"` 计算 → idx 4(兔子弹跳);
- 英文稿按 seed 输入 **`"com.lveditor.draft|text_intro_pool"`** 计算 → idx 63(逐字旋入)。

`"com.lveditor.draft"` 是剪映草稿库根目录名,与任务无关、恒定不变 —— **所有任务的英文稿字幕动画都会永远是逐字旋入,轮换机制在英文侧形同虚设**。

### seed 推算过程(逐位验证,非猜测)

池 = `templates/text_resource_library.json` intro 段 78 条 VIP 动画按文件顺序编号(0 起):idx 0 乱码故障、idx 4 兔子弹跳(库文件 line 44)、idx 58 跳跳捣蛋鬼(line 476)、idx 63 逐字旋入(line 516)。

| seed 输入 | sha256 前 8 位 | 整数值 | mod 78 | 池内动画 |
|---|---|---|---|---|
| `console-52373c6f9e11\|text_intro_pool` | a223a106 | 2720243974 | **58** | 跳跳捣蛋鬼(设计预期,未出现) |
| `AutoVideo_console-52373c6f9e11\|text_intro_pool` | a7ce6360 | 2815320928 | **4** | 兔子弹跳(= 中文稿实测 ✓) |
| `com.lveditor.draft\|text_intro_pool` | dde4718f | 3722736015 | **63** | 逐字旋入(= 英文稿实测 ✓) |

三个值与两稿实测严格对上,根因链闭环(见下节)。

## 四、根因链

1. **thread_id 不是 state 键**。thread_id 在 langgraph 里是 invocation config 参数(`config["configurable"]["thread_id"]`,见 graph.py:369 附近注释);`WorkflowState`(`state.py:116`,`TypedDict, total=False`)**没有 `thread_id` 字段**(grep 零命中)。所以节点内 `state.get("thread_id")` **恒为 None**。
2. `web/console_state.py::build_initial_state`(:78-94)的冻结键集合里没有 `thread_id`;thread_id 只以 `"session_id": thread_id`(:79)的名义进了 state,键名对不上。
3. **中文入口兜底**(`nodes/node_10_inject_text_fx.py:201-203`):thread_id 为空 → `Path(state["draft_path"]).parent.name` = 中文草稿目录名 `AutoVideo_console-52373c6f9e11` → idx 4。
4. **英文入口兜底缺陷**(`nodes/node_10_inject_text_fx.py:229`):`state.get("draft_dir_en_branch")` 是**目录**而不是文件路径(:222-225 代码自己再拼 `draft_content.json`),`Path(目录).parent.name` 取到的是草稿库根目录名 `com.lveditor.draft` → idx 63,恒定。
5. **node_09 转场池同病但未破一致**(`nodes/node_09_inject_fx.py:413-416`):同款 thread_id→兜底解析,因它在同一次调用里处理中英两稿、seed 相同,转场 zh/en 一致性侥幸保住;但 seed 语义同样偏离文档(用了目录名而非 thread_id)。
6. **单测没抓住**:测试直接调 `apply_text_fx(..., job_id=...)` 显式传参,绕过了两个图入口的 state 解析,兜底分支从未被生产路径覆盖。

时间线佐证:英文稿目录名时间戳 `20261118_064813`(06:48),中文稿 mtime 07:25,提交 7c0bbc7 于 06:44(+0800)入库 —— 两稿都是分池功能生效后写入的,排除"旧代码产物"的可能。

## 五、修复计划(初版 P0 —— 经设计审查修订为 P1,实施以审查文档为准)

改动 3 个生产文件 + 1 个测试文件,全部小改动:

1. **state.py**:`WorkflowState` 增加 `thread_id: str` 字段,注释说明与 `session_id` 的区别(后者沿用旧契约,前者是 thread 语义的权威键)。
2. **web/console_state.py**:`build_initial_state` 冻结键集合增加 `"thread_id": thread_id`(thread_id 已是 keyword-only 形参,作用域内直接可用);同步更新冻结契约注释。
3. **agent_gate/cli.py**:`_write_initial_state`(:166)同步写入 `thread_id`,CLI 入口与 console 入口对齐。
4. **nodes/node_10_inject_text_fx.py**:新增共享解析函数 `_resolve_job_id(state)`:

   ```python
   def _resolve_job_id(state: dict) -> str:
       """job_id 权威解析:thread_id → 中文草稿目录名 → ""(zh/en 必须同源)。"""
       job_id = str(state.get("thread_id") or "").strip()
       if job_id:
           return job_id
       draft_path = state.get("draft_path")
       if draft_path:
           return Path(str(draft_path)).parent.name
       return ""
   ```

   - `inject_text_fx`(:201-203)与 `inject_text_fx_en`(:229)都改用它;
   - en 兜底改为**中文草稿目录名**(与 zh 完全同源),即使走兜底 zh/en 也一致,彻底杜绝 `com.lveditor.draft` 这种恒定 seed;
   - `nodes/node_09_inject_fx.py` 入口(:413-416)换用同一 helper(行为归一,转场 seed 从目录名变为 thread_id 后对新任务是设计语义)。

5. **tests/unit/test_node_10_inject_text_fx.py** 增补三类用例:
   - state 带 thread_id → zh/en 两入口动画同名,且等于 `pool[sha256(thread_id|text_intro_pool) % 78]`;
   - state 无 thread_id(老 checkpoint resume 场景)→ zh = 中文目录名 seed、en = 同一中文目录名 seed(一致性保持);
   - 断言任何路径下 seed 都不等于 `com.lveditor.draft`(回归守卫)。

## 六、验证方案

1. **单测**:`.venv/Scripts/python.exe -m pytest tests/unit/test_node_10_inject_text_fx.py tests/unit/test_node_09_inject_fx.py tests/unit/test_template_library_fx_pools.py -q`(基线 49 用例,零新增失败)。
2. **真机**:新建任务跑完后,用下方"核验配方"检查两草稿动画同名且 = `pool[sha256(thread_id)]`。
3. **存量 console-52373c6f9e11**:建议**不动** —— 两稿动画均 VIP、资源就位、渲染正常,只是不满足一致性设计;如需对齐,修复后重放 `apply_text_fx`(幂等,按确定性 id 覆盖)即可,须先关剪映。

## 七、注意事项

- 老任务的 checkpoint 里没有 thread_id,resume 老任务仍会走兜底 —— 修复第 4 条保证了此时 zh/en 也一致。
- 修复后新任务的动画 seed 从「目录名」变为「thread_id」,同一任务修复前后动画名可能不同 —— 属向设计语义对齐,不是回归。
- 本次会话在计划模式下 `python`/`ls`/`jq` 被沙箱拦截,核验全部用 grep/sha256sum 完成 —— 反而沉淀出了下面这份**零依赖核验配方**,真机验收可直接复用。

## 附录:Git Bash 适用核验配方(零依赖,可复现)

```bash
# 1) 抽取草稿中所有文本动画对象(单行压缩 JSON 的中文稿)
grep -oE '"animations":\[[^]]*\]' \
  "/c/Users/wayyet/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/AutoVideo_console-52373c6f9e11/draft_content.json"

# 2) 格式化 JSON 的英文稿:先定位动画对象行号,再用 Read 工具读
grep -n "text-anim-text-en-1:in" \
  "/c/Users/wayyet/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/en_branch_console-52373c6f9e11_20261118_064813_970710/draft_content.json"

# 3) 计算某 thread_id 的池选序号(0 起):前 8 位十六进制 → 整数 → mod 池长
printf '%s' "console-52373c6f9e11|text_intro_pool" | sha256sum
#    → a223a106... ;再用 bash 算术:echo $((0xa223a106 % 78))  → 58

# 4) 对照库内 intro 段第 N 个 VIP 动画名(每条占 8 行,name 行 = 12 + 8N)
grep -n '"name": "' templates/text_resource_library.json | head -80
```

> 捷径:若某个动画名 X 已在草稿中实测,可反向验证 seed —— 依次对候选 job_id(`thread_id`、`AutoVideo_<tid>`、`com.lveditor.draft`、空串)计算 `sha256(候选|text_intro_pool)[:8] % 78`,命中 X 的库内序号者即为实际生效的 seed 输入。本次即以此法锁定两个兜底路径。

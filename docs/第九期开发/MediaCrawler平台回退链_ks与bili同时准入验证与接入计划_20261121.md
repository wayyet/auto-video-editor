# MediaCrawler 平台回退链 ks/bili 同时准入验证与接入计划(2026-11-21)

| 项 | 值 |
|---|---|
| 日期 | 2026-11-21 |
| 依据文档 | `MediaCrawler平台回退链_设计审查与修订执行计划_20261121.md`(实施权威,§3.8 优先级矩阵 / §3.8.2 准入门槛);`MediaCrawler平台回退链_实施执行记录_20261121.md`(xhs→dy 已交付) |
| 当前生产链 | `xhs → dy → Bing`(默认 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS="dy"`) |
| 用户拍板 | **同时启动 ks(快手)与 bili(B站)的准入验证** —— 放宽审查"每次只新增一个平台"的节奏,但每平台九项门槛逐个不降 |
| MC 外部副本 | `09ed1ec62d885dab422a53de999f52ac0bd4966b`(复验口径同前) |
| 状态 | 计划待实施 |

---

## 一、定稿顺序回顾("内容相关度 × 稳健性")

### 1.1 目标候选顺序(审查 §3.8/§3.8.1 定稿,与原推荐一致)

```text
xhs(小红书) → dy(抖音) → ks(快手) → bili(B站) → wb(微博) → zhihu(知乎)
```

**贴吧(tieba)不进入回退链。**

### 1.2 审查修正的是"定级依据",不是顺序

| 变化点 | 修改前 | 审查定稿 |
|---|---|---|
| 知乎排位依据 | "风控最严,易被拦"(未实证断言) | "尚未取得足够的访问稳定性证据,排后段";明确不得默认它比微博更容易被拦 |
| 快手/B站依据 | 只按内容形态推断 | ks="中"(与抖音短视频互补);bili="中高"(旅行Vlog/行程复盘);均因**无验收证据**排在 dy 之后 |
| 整体性质 | 个人推荐顺序 | 定性为"**推荐验证/接入顺序**",不代表已通过真机测试的可用性排名;不给 1~5 伪精确打分(缺同口径成功率样本) |

### 1.3 候选顺序 ≠ 实际启用顺序

顺序只是**排队资格**。每平台必须独立通过 §3.8.2 九项门槛才能进生产白名单(`_MC_PLATFORM_WHITELIST`),未过门平台写进 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` 会在启动时报错(安全阀设计)。当前实际启用范围:`xhs → dy → Bing`。

---

## 二、用户拍板与实施范围

- **同时启动 ks + bili 准入验证**;
- 九项门槛(§3.8.2)逐平台独立执行:**任一平台某项未通过 → 该平台保持未准入,不阻塞另一平台**;
- 目标产出:两平台各自过门后,生产白名单扩至 `("xhs", "dy", "ks", "bili")`,候选链变为 `xhs → dy → ks → bili → Bing`(env 默认值是否扩,见 §4 阶段 2 的决策)。

### 2.1 门槛 → 本计划落点对照

| 审查门槛 | 内容摘要 | 本计划落点 |
|---:|---|---|
| 1 | 平台代码 vs 物理输出目录 | 阶段 0-1:实测 `store/<平台>/_store_impl.py` 的 `AsyncFileWriter(platform=...)` 实参 → 注册 `MC_OUTPUT_DIR_BY_PLATFORM` |
| 2 | 真实字段 fixture(不凭字段名猜测) | 阶段 0-1:读 `store/<平台>/__init__.py` save item 构造,确认标题/摘要/ID/**URL 承载字段** |
| 3 | 关键词搜索路径(不能拿详情模式异常捕获当证据) | 阶段 0:读 `media_platform/<平台>/core.py` 的 `search()` 实际调用链 |
| 4 | 登录与会话 | 阶段 0(源码)+ 阶段 4(操作员真机) |
| 5 | 超时与资源清理 | 既有整链 deadline/进程树终止/TTL 清扫对新平台自动生效,无需新代码;真机确认 |
| 6 | 数据质量(映射/去重/相关性守卫) | 阶段 3:映射用例 + 相关性守卫用例 |
| 7 | 错误与诊断(脱敏) | 既有 AUTH_REQUIRED/证据落盘对新平台自动生效;单测覆盖 |
| 8 | 缓存与回退(hash 含完整链) | 既有 `mediacrawler_request_provider(链)` 机制对新平台自动生效;单测覆盖 |
| 9 | 自动化 + 真机 | 阶段 3(单测)+ 阶段 4(操作员清单) |

---

## 三、分阶段实施计划

### 阶段 0:MC 源码核查(ks/bili 并行,只读)

对两平台各产出一份《准入核查单》,核查点与必读文件(本机固定版本):

| 核查点 | ks 必读文件 | bili 必读文件 | 关键教训 |
|---|---|---|---|
| 输出目录名 | `store/kuaishou/_store_impl.py` 的 `AsyncFileWriter(platform=...)` | `store/bilibili/_store_impl.py` 同 | **dy→douyin 教训**:抖音代码是 `dy`,目录是 `douyin`;不得假定目录名=CLI 代码 |
| 记录字段(URL 承载) | `store/kuaishou/__init__.py` save item 构造 | `store/bilibili/__init__.py` 同 | 抖音是 store **预构建** `aweme_url`;ks(疑 photo_id)/bili(疑 bvid)是否预构建 URL、字段名是什么,以实测为准 |
| search() 路径 | `media_platform/kuaishou/core.py` `search()` | `media_platform/bilibili/core.py` `search()` | **抖音教训**:关键词搜索不走 `get_aweme_detail`,不能拿详情/作者模式异常捕获当搜索路径证据;重点看解析异常/缺字段/部分结果/进程退出行为 |
| 登录失败退出码 | `media_platform/kuaishou/login.py` | `media_platform/bilibili/login.py` | 抖音 3 处无参 `sys.exit()`=退出码 0 → 已有 AUTH_REQUIRED 启发式兜底;确认两平台同类行为 |

### 阶段 1:适配器与映射(仅对过门平台实施)

- `storyline_capabilities/mediacrawler_client.py`:`MC_OUTPUT_DIR_BY_PLATFORM` 注册实测目录名(预期形如 `"ks": "kuaishou"`、`"bili": "bilibili"`,**以阶段 0 实测为准**);
- `storyline_capabilities/search_web_topic.py` `_map_mc_records`:按实测记录形状扩 URL 键白名单(现有:`note_url/url/aweme_url`);
- **判定规则**:某平台缺失 URL 承载字段且无法可靠构造(构造模板须以 store 源码为准,并记入执行记录)→ 该平台保持未准入。

### 阶段 2:config 白名单与默认值

- `config.py` `_MC_PLATFORM_WHITELIST` 扩至已过门平台;
- `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` **默认值保持 `"dy"`**(审查 §3.8.1:仅当平台完成**含真机在内**的准入验收后才允许改默认;操作员真机通过后自行设 `"dy,ks"` / `"dy,ks,bili"` 并重启);
- `tests/unit/test_config_mc_platform_chain.py` 白名单断言同步。

### 阶段 3:测试矩阵扩展

- `tests/unit/test_mediacrawler_client.py`:ks/bili 目录映射**正反两向**用例(正=从实测目录读回结果;反=写在 `<CLI代码>/json` 的结果不得被误读 —— 防映射退化);
- `tests/unit/test_storyline_search_web_topic_mc.py`:链场景扩展(xhs→dy→ks 三级接力、ks/bili 记录映射、相关性守卫对新平台生效、新平台失败后 Bing 仍只调一次);复用既有桩模式(`_patch_mc_by_platform` / `_mc_aweme` 记录形状);
- 全量回归:基线 2883 例 exit=0,零新增失败。

### 阶段 4:执行记录与提交

- `MediaCrawler平台回退链_实施执行记录_20261121.md` 补章节:ks/bili 准入核查单、代码变更、**操作员真机清单**;
- 中文提交风格分阶段提交(本地 main,不推送;推送走 sync-github 由用户触发)。

---

## 四、验证方案

1. 单测:`.venv/Scripts/python.exe -m pytest tests/unit/test_mediacrawler_client.py tests/unit/test_storyline_search_web_topic_mc.py tests/unit/test_config_mc_platform_chain.py tests/unit/nodes/test_node_search_web_topic.py -q`;
2. 全量:`.venv/Scripts/python.exe -m pytest tests/unit -q`(exit=0,对照基线);
3. 真机(操作员,两平台各一轮):
   - 首跑扫码登录一次(弹窗浏览器;登录态持久化 `browser_data/cdp_<平台>_user_data_dir`);
   - 受控冒烟:真实检索 → 确认 `web_topic_mc/<run_id>/` 下实测目录有结果、AI明细页引擎显示 `MediaCrawler(快手)/MediaCrawler(B站)`;
   - 无登录态降级:确认 AUTH_REQUIRED(或超时)→ Bing 兜底 → 任务不挂死;
   - 验收通过后 env 设 `"dy,ks"` / `"dy,ks,bili"` 并重启进程。

---

## 五、风险与已知教训(实施时对照)

1. **目录名陷阱**:dy→douyin 已实锤;ks/bili 必须实测后注册,不许照抄预期值;
2. **搜索路径陷阱**:抖音 search() 不走 get_aweme_detail;ks/bili 的 search() 异常覆盖范围单独核查,部分结果/空数据/风控页行为要在核查单里写清;
3. **URL 构造决策点**:若 store 未预构建 URL 字段,需引入"按 ID 构造链接"逻辑 —— 这是映射语义升级(当前映射只做白名单提取,不做构造),必须记入执行记录并单独测试;
4. **许可门禁不变**:MC 仍为 NON-COMMERCIAL LEARNING 1.1,仅个人非商业学习用途(2026-11-20 确认口径);扩大平台=扩大对目标平台的抓取面,频率纪律(整链预算/TTL/互斥锁)不得关闭;
5. **回滚不变**:`STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=""` 随时回到 xhs→Bing。

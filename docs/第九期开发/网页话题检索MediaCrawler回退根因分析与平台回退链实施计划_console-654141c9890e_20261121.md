# 网页话题检索 MediaCrawler 回退根因分析与平台回退链实施计划(console-654141c9890e)

| 项 | 值 |
|---|---|
| 任务 ID | console-654141c9890e |
| 涉及节点 | 014 · 网页话题检索(中文线);018 · 英文话题检索(ReachMCP,不受影响) |
| 分析日期 | 2026-11-21 |
| 状态 | 根因分析完成(证据齐备,非推测);平台回退链计划待实施 |
| 关联文档 | 网页话题检索双线中英分离-reach-mcp英文线重建-分析与实施计划-20261120.md(英文线);MediaCrawler 接线审查文档 2026-11-19/20 |

> ⚠️ **本文档状态(2026-11-21)**:本计划的实现与验收要求(§8/§9/§10)已被
> [`MediaCrawler平台回退链_设计审查与修订执行计划_20261121.md`](MediaCrawler平台回退链_设计审查与修订执行计划_20261121.md)
> **取代**,实际执行以该审查文档为准(实施结果见
> [`MediaCrawler平台回退链_实施执行记录_20261121.md`](MediaCrawler平台回退链_实施执行记录_20261121.md))。
> §2/§3/§4/§7 保留为需求背景与故障线索,但以下事实表述按审查 §2.2 修正:
> ① xhs 详情异常豁免名单还含 DataFetchError/KeyError(原文枚举不全,不影响 RetryError 逃逸的结论);
> ② HTML 含 `noteDetailMap` 不能证明"不是验证码页"(只能证明含该字符串,页面性质需结合更多证据);
> ③ "高频风控"是**主嫌疑**而非定案(单次成功/失败对照不足以归因);
> ④ `NETWORK_ERROR` 是适配器对非零退出的**粗粒度归类**,不等于网络故障已证实;
> ⑤ 原 §8 落地后**不能**定位到具体笔记级现场(平台级 attempts 之外的笔记级诊断另设受控观测性任务)。

---

## 一、结论摘要(TL;DR)

**MediaCrawler 不是"没有被使用",而是被调用了、跑到一半崩了**,然后按设计回退到 Bing(fail-safe 行为),任务本身没有中断。

- MediaCrawler 子进程真实启动(2026-11-21 02:58:42 本地),**搜索阶段成功**,崩溃发生在**逐条抓取笔记详情**阶段;
- 直接死因:小红书详情页 HTML 的 `window.__INITIAL_STATE__` 解析抛 `JSONDecodeError`,tenacity 重试 3 次耗尽后 `RetryError` 未被捕获,沿 `asyncio.gather` 传播 → 整个 MC 进程 exit 1;
- 适配器把一切非零退出归类 `NETWORK_ERROR` → 能力层按设计回退 Bing(相关性守卫 8/10 匹配,结果可用);
- 触发背景:14 分钟前另一任务刚用同一账号/IP 成功爬过一次,本次疑似撞上小红书高频风控(或踩中提取器已知脆弱点,现存证据无法进一步区分)。

**用户拍板的后续方向**:小红书搜索失败后,切换到抖音等其他自媒体平台继续搜索(平台回退链),而非直接回退 Bing。

---

## 二、背景与表象

AI内容明细页 014 节点显示:

- 检索引擎:`Bing(MediaCrawler 回退)`
- 来源列表 10 条,标签全部 `Bing(MediaCrawler 回退)`
- 底部回退说明原文:

```
回退说明MediaCrawler(小红书) NETWORK_ERROR: MediaCrawler exited with code 1;
see diagnostics stdout_tail/stderr_tail for details; 已回退 Bing
```

产物 `outputs/console-654141c9890e/storyline/search_web_topic.json` 关键字段:

| 字段 | 值 |
|---|---|
| `requested_provider` / `requested_platform` | `mediacrawler` / `xhs`(组件确实被调用) |
| `engine` / `provider` | `Bing(MediaCrawler 回退)` / `bing_html` |
| `searched_at` | `2026-11-20T18:58:42+00:00`(= 本地 11-21 02:58:42 +0800) |
| `diagnostics.primary.returncode` | `1` |
| `diagnostics.primary.run_id` | `20261121_025842_520686` |
| `diagnostics.primary.stdout_tail` | 空(MC 日志全走 stderr,见 §6.2) |
| `diagnostics.primary.stderr_tail` | 最终 2048 字符 = tenacity RetryError traceback(见 §3.1) |
| `diagnostics.fallback` | `mediacrawler → bing_html`,primary_error_code=`NETWORK_ERROR` |
| `fallback_diagnostics.relevance_matched/total` | 8/10(Bing 结果可用) |

文件时间证据(本地 +0800):`search_web_topic.json` mtime=**11-21 02:59:44**,`search_web_topic_en.json`=02:59:16,`console_run.json`=03:06:08 —— 整个任务实际运行于 **11-21 凌晨 02:58~03:06**。

---

## 三、证据链

### 3.1 stderr_tail 的 traceback(崩溃点)

```
MediaCrawler/main.py:114        → await crawler.start()
media_platform/xhs/core.py:125  → await self.search()
media_platform/xhs/core.py:178  → note_details = await asyncio.gather(*task_list)
media_platform/xhs/core.py:317  → get_note_by_id_from_html(note_id, xsec_source, xsec_token, ...)
tenacity RetryError[Future ... raised JSONDecodeError]
```

读法:搜索阶段已成功返回笔记列表(否则 gather 不会执行),崩溃在**详情抓取任务**里;`get_note_by_id_from_html` 带 tenacity `stop_after_attempt(3)` 装饰器(client.py:689-695),3 次尝试全部抛 `JSONDecodeError`(可重试异常)→ 抛出 `RetryError`。

### 3.2 两条详情路径为何全失败

MediaCrawler 抓笔记详情有两条路径(`media_platform/xhs/core.py:311-321`):

1. **API 路径** `get_note_by_id`(client.py:331,**无 tenacity 装饰器** → 不会抛 RetryError,`:313` 的 `except RetryError: pass` 实际是死代码):调用 feed 接口,空结果时返回空 `dict`(client.py:357-366)。MC 源码注释自认(client.py:362):

   > When crawling frequently, some notes may have results while others don't(爬取频繁时,部分笔记可能拿不到结果)

2. **HTML 兜底路径** `get_note_by_id_from_html`(core.py:317,`enable_cookie=True`):GET 笔记详情页 → `extract_note_detail_from_html` 解析(extractor.py:31-50):
   - 守卫:`"noteDetailMap" not in html → return None`(验证码页/笔记不存在,不抛异常 —— 说明本次拿到的**不是**验证码页);
   - 正则抽取 `window.__INITIAL_STATE__=({.*})</script>`,盲目 `.replace("undefined", '""')` 后 `json.loads(state)`(extractor.py:44-48)→ **本次 3 次全抛 JSONDecodeError**。

### 3.3 一条笔记失败 → 整个进程死亡(结构缺陷)

`core.py:178` 用 `asyncio.gather` 并发抓所有笔记详情,异常豁免名单只有三类(`core.py:331-339`:NoteNotFoundError / IPBlockError / PlatformAccessError → 跳过该笔记);`JSONDecodeError` 包成的 `RetryError` **不在豁免名单**,且 `:317` 这条路径外层没有 try/except(`:313` 的 except 只包住第一条路径)→ 异常沿 gather 传播 → MC 主进程 exit 1。

### 3.4 适配器归类 + 回退(设计内行为)

`storyline_capabilities/mediacrawler_client.py:382-385`:一切非零退出 → `NETWORK_ERROR`,消息 "MediaCrawler exited with code N; see diagnostics stdout_tail/stderr_tail for details";能力层 `storyline_capabilities/search_web_topic.py:1489` 起按回退矩阵落到 `bing_html`,任务继续。

---

## 四、根因定案

**表层**:MediaCrawler(小红书)在笔记详情抓取阶段崩溃 exit 1 → 适配器归类 NETWORK_ERROR → 按设计回退 Bing。

**深层**(按证据强度排序):

1. **小红书高频风控**(主嫌疑):14 分钟前(02:44:57)video-001 任务刚用同一账号/同一浏览器 profile/同一 IP 成功爬过一次(不同检索词,API+HTML 双路径全成功,`returncode 0`);02:58:42 第二次爬取时搜索 API 还活着、详情 API 全空、详情 HTML 变成解析不了的页面变体 —— 与 XHS "搜索可用、详情收紧" 的高频风控表现吻合,MC 自己的注释(client.py:362)也承认这种频率效应;
2. **提取器脆弱点**(并列嫌疑,无法排除):extractor.py:46 的 `.replace("undefined", '""')` 是无差别替换,若笔记 JSON 内容(标题/正文/用户名等)含英文单词 "undefined",替换会在 JSON 字符串内部注入裸引号 → `json.loads` 必炸;
3. **结构放大器**:MC 对 xhs 详情任务无异常隔离(§3.3),单条笔记解析失败会杀死整个采集进程 —— 上述任一深层原因都因此从"一条笔记失败"放大为"整次检索失败"。

**无法进一步归因的原因**(观测性缺口,见 §6.2):stderr 只保留末尾 2048 字符(traceback 之前的全部 MC 日志被截掉)、MC 无文件日志(logging.basicConfig 仅控制台,`tools/utils.py:30-45`)、失败运行的原始目录未保留现场。

---

## 五、为什么"偏偏这次"失败 —— 时间线

| 本地时间(+0800,11-21) | 事件 |
|---|---|
| 02:44:57 | video-001 任务 MC 检索(检索词:红色敞篷车沿海岸山路…),**完全成功**(run_id 20261121_024457_765699,output_file=xhs/json/search_contents_2026-11-21.json) |
| 02:58:42 | 本任务 MC 检索启动(run_id 20261121_025842_520686),搜索成功、详情阶段崩溃 exit 1 |
| ~02:59 | 回退 Bing 成功(HTTP 200,122KB,相关性 8/10),产物落盘(02:59:44) |
| 03:06 | 任务继续后续节点(console_run.json mtime) |

全库横向扫描:outputs 下共 3 个中文检索产物 —— video-001=MC 成功、console-011419d9ceec=切换前直连 Bing、本任务=MC 失败回退。MC 生产战绩 **1 成 1 败**,尚非系统性频发,但暴露的结构缺陷(§3.3)与观测缺口(§6.2)需要补。

---

## 六、附带发现(次要,与本故障因果无关)

### 6.1 AI内容明细页时间戳显示缺陷(差 8 小时)

页面显示 `检索时间 2026-11-20T19:01:37+08:00`,而产物真实时间 = `searched_at 2026-11-20T18:58:42+00:00`、文件 mtime = **11-21 02:59:44 +0800**。显示值的钟面恰为 UTC 钟面,却标着 `+08:00` —— 某一层(节点事件/checkpoint/前端之一,未逐一定位)把 UTC 时间误标为本地时区,实际本地时间应为钟面值 + 8h。显示链路:`web/ai_content.html:248/281` ← `web/static/ai_content.mjs:123/141` ← `web/console_service.py:1121/1216`(`_clamp_str` 原样透传)。**另案处理,不在本计划范围。**

### 6.2 MC 失败现场丢失(观测性缺口)

- MC 日志全走 stderr,适配器只保留末尾 2048 字符(`mediacrawler_client.py:175-177`),traceback 之前的全部上下文(搜索响应、失败 note_id 等)被截掉;stdout_tail 为空即因此;
- MC 无文件日志;失败运行的原始时间戳目录不保留有效现场(结果文件本就没写出来)。

这是本次根因只能定到"页面变体破坏解析"粒度、无法逐字节归因的直接原因。本计划 §8 的改动落地后,后续失败可直接定位到具体笔记与页面内容。

---

## 七、MediaCrawler 平台支持矩阵(扩展依据)

MC 支持共 **7 个平台**(源码核实:`api/schemas/crawler.py:27-35` PlatformEnum 与 `media_platform/` 下 7 个实现模块一一对应;适配器标签表 `mediacrawler_client.py:78-86` 同口径):

| 平台 | 代码 | 内容形态 | 作为旅游攻略检索的回退候选评估 |
|---|---|---|---|
| 小红书 | `xhs` | 图文笔记(当前主平台) | 已接入;详情路径无异常隔离(§3.3) |
| 抖音 | `dy` | 短视频 | 已拍板为第一回退;详情有逐条异常隔离(`douyin/core.py:230-235` 捕获 DataFetchError/KeyError → None),结构性最稳 |
| 快手 | `ks` | 短视频 | 同抖音形态,扩展成本低 |
| B站 | `bili` | 中长视频 | 旅游 vlog/攻略视频多,质量不错 |
| 微博 | `wb` | 微博正文/图文 | 旅游话题多,但碎片化强 |
| 知乎 | `zhihu` | 问答/专栏 | 攻略长文质量高,但**风控最严**,易被拦 |
| 贴吧 | `tieba` | 论坛帖子 | 内容偏旧、碎片化,价值最低 |

登录方式支持 qrcode / phone / cookie(`api/schemas/crawler.py:38-42`);各平台浏览器 profile 相互独立(`browser_data/<platform>_user_data_dir`)。

**每扩一个平台的接入纪律**(与 P2-04 审查项一致):

1. **记录字段映射验证**:`_map_mc_records` 目前只认 `note_url/url`(xhs 形状);抖音是 `aweme_url`(`store/douyin/__init__.py:95-118`:aweme_id / title(=desc) / desc / aweme_url),其他平台各有各的键(如 B站 bvid 拼 URL),必须先看该平台 store 的实际 JSON 输出再扩键白名单,不能盲接;
2. **首次扫码登录**:每个平台首跑都要人工扫码一次(登录态持久化),无人扫码则该次尝试按 timeout(默认 600s)超时后自动继续;
3. **白名单是一行配置**:`config.py:118` `_MC_PLATFORM_WHITELIST` + 回退链 env;贵的部分是上面两项逐平台验证。

**推荐回退链顺序**(内容相关度 × 稳健性):`xhs → dy → ks → bili → wb → zhihu`(贴吧不建议入链)。本计划首期落 `xhs → dy`,链做成 env 可配置,后续平台验证一个、加一个。

---

## 八、实施计划:MC 平台回退链(小红书失败 → 抖音 → Bing)

### 8.0 设计要点(勘察结论)

- 适配器 `search_notes` 已按 `platform` 参数化 → **适配器本体零改动**;
- 抖音记录 `title`/`desc` 与现有映射兼容,仅 URL 键需补 `aweme_url`;
- 抖音详情有逐条异常隔离,不会复刻 xhs 的 gather 崩溃;
- `request_hash` 语义不变:仍按**主平台**+mc_revision 编入(§6.2 缓存契约"回退成功也保留该 hash"不变)。

### 8.1 config.py

- `_MC_PLATFORM_WHITELIST`(:118)扩为 `("xhs", "dy")`;
- 新增 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS`(env,默认 `"dy"`,逗号分隔列表;校验:每项在白名单内、不等于主平台;空串 = 无平台回退,等价回滚为现行为)。

### 8.2 storyline_capabilities/search_web_topic.py

- `_map_mc_records`(:1051):url 键元组 `("note_url", "url")` → `("note_url", "url", "aweme_url")`,注释同步 P2-04 口径(xhs=note_url,dy=aweme_url);
- MC 块(:1405-1489)改为**平台链循环**:主平台 → fallback_platforms 逐个尝试;每个 attempt 走完整的 `search_notes → 映射 → 相关性守卫`(`_count_relevant_results` 平台无关,直接复用);**首个成功即产出**(engine 按平台中文标签,如 `MediaCrawler(抖音)`);全部失败才走既有 Bing 回退,回退 message 串联各平台错误(页面「回退说明」可读);
- diagnostics:`requested_platform` 保持 = 主平台(兼容现有页面),新增 `platform_chain` 与 `attempts` 列表(每 attempt 的 platform / error_code / message / diag);`primary` 保持 = 首次尝试的 diag;
- artifact `schema_version` 不 bump(diagnostics 仅加键,与既有增量实践一致)。

### 8.3 nodes/storyline/node_search_web_topic.py

- provider_options(:139-146)增加 `"fallback_platforms": config.STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS`;
- `hash_provider`(:147)仍用主平台,不变。

### 8.4 测试

- `tests/unit/test_storyline_search_web_topic_mc.py` 新增:
  - 主平台失败 → dy 成功(断言 provider=mediacrawler、engine 抖音标签、attempts 结构、无 Bing 回退);
  - 主平台 + dy 全失败 → Bing 回退(message 串联两平台错误);
  - dy 记录(aweme_url 形状)映射成功;
  - 相关性守卫对 dy 结果生效;dy 也被相关性拦截 → Bing;
  - fallback_platforms 为空 → 行为与现状逐字一致(回滚保底);
- `tests/unit/test_mediacrawler_client.py`:补 `platform_label("dy") == "抖音"` 断言(如无);
- `tests/unit/nodes/test_node_search_web_topic.py`:补 fallback_platforms 透传断言。

---

## 九、验证方案

1. **单测**:`.venv/Scripts/python.exe -m pytest tests/unit/test_storyline_search_web_topic_mc.py tests/unit/test_mediacrawler_client.py tests/unit/nodes/test_node_search_web_topic.py -q` —— 新增用例全绿 + 存量零回归;
2. **全量回归基线**:对照既有基线(worktree 对照法),零新增失败;
3. **真机冒烟**(留操作员):
   - 正常路径:新建任务跑网页话题检索,主平台成功即止;
   - 故障注入:临时设 `STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT` 指向空目录(主平台预检失败)→ 观察抖音接管;抖音未登录扫码时该次尝试 600s 超时后自动回退 Bing,任务不挂死;
   - 检查 artifact 的 `platform_chain` / `attempts` / `diagnostics.primary` 与页面引擎标签;
4. **抖音首跑**:操作员在弹出的浏览器窗口扫码登录一次(一次性,登录态持久化)。

---

## 十、运行时前提与操作员事项

1. 抖音首次运行需**人工扫码登录**(headless=0 弹窗;之后登录态持久化于 MC `browser_data/dy_user_data_dir`);
2. xhs 登录态已存在(11-21 02:44 video-001 成功跑过);
3. 配置改后须重启进程生效(env 为启动时读取,`config.py` §1.2 纪律);
4. 回滚开关:`STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=""`(空串)即恢复现行为;引擎级回滚仍用 `STORYLINE_WEB_TOPIC_ENGINE=bing_html`;
5. CDP 互斥锁/超时杀进程树/原始 JSON TTL 清扫等既有护栏对本链路逐 attempt 生效,无需新设。

---

## 附录:关键文件与代码索引

| 文件 | 位置 | 作用 |
|---|---|---|
| `outputs/console-654141c9890e/storyline/search_web_topic.json` | 任务产物 | 本次故障的全部诊断证据(diagnostics/fallback) |
| `outputs/video-001/storyline/search_web_topic.json` | 对照样本 | 14 分钟前的 MC 成功运行 |
| `storyline_capabilities/mediacrawler_client.py:382-385` | 适配器 | 非零退出 → NETWORK_ERROR 的归类点 |
| `storyline_capabilities/mediacrawler_client.py:175-177` | 适配器 | stderr/stdout 限长 2048(观测缺口) |
| `storyline_capabilities/search_web_topic.py:1032-1072` | 能力层 | `_map_mc_records`(url 键白名单待扩 aweme_url) |
| `storyline_capabilities/search_web_topic.py:1405-1489` | 能力层 | MC 块 + 相关性守卫 + Bing 回退(平台链改造点) |
| `nodes/storyline/node_search_web_topic.py:135-148` | 节点层 | provider_options 接线 |
| `config.py:118` | 配置 | `_MC_PLATFORM_WHITELIST`(待扩 dy) |
| `config.py:130-167` | 配置 | MC 平台/条数/超时/headless/debug 等配置区 |
| `MediaCrawler/media_platform/xhs/core.py:178,311-339` | MC 外部副本 | gather 无异常隔离(结构缺陷,本次崩溃传播路径) |
| `MediaCrawler/media_platform/xhs/client.py:331,357-366,689-729` | MC 外部副本 | 详情 API 空 dict + "爬取频繁"注释;HTML 路径 @retry(3) |
| `MediaCrawler/media_platform/xhs/extractor.py:31-50` | MC 外部副本 | JSONDecodeError 产生点(replace("undefined") 脆弱点) |
| `MediaCrawler/media_platform/douyin/core.py:213-235` | MC 外部副本 | 抖音详情逐条异常隔离(回退候选稳健性依据) |
| `MediaCrawler/store/douyin/__init__.py:95-118` | MC 外部副本 | 抖音记录字段(aweme_id/title/desc/aweme_url) |
| `MediaCrawler/api/schemas/crawler.py:27-42` | MC 外部副本 | 7 平台枚举 + 登录方式枚举 |
| `web/static/ai_content.mjs:123,141` / `web/console_service.py:1121,1216` | 前端/API | 检索时间显示链路(时区标注缺陷,另案) |

> 注:MediaCrawler 为外部安装副本(仓库外,代码不入库),路径 `C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\MediaCrawler`;其内部行号基于当前本地版本(mc_revision `09ed1ec62d88`),上游更新后会漂移。

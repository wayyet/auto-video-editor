# MediaCrawler 平台登录预检闸门——需求分析与实施计划

> 日期:2026-11-21
> 状态:**计划已成形,待用户批准后实施**(ExitPlanMode 被拒,计划模式已手动退出)
> 关联:第九期《MediaCrawler平台回退链_ks与bili同时准入_设计审查修订版与执行计划_20261121.md》;记忆 `mc-xhs-fallback-rootcause-dy-chain-plan-20261121`

---

## 一、背景:四平台登录验证(2026-11-21 下午已完成)

用户问:「xhs(小红书) → dy(抖音) → ks(快手) → bili(B站),以上这4个都登录成功了吗?验证以上这个操作。」

### 1.1 验证方法与判据

验收脚本 `scripts/verify_mc_ks_bili_login.py`(每次调用 = 单平台单次真实检索,走**生产同路径** `storyline_capabilities.mediacrawler_client.search_notes`)。判据:

- `PASS = returncode 0 且映射记录 ≥1 且 cdp_<platform>_user_data_dir 存在`
- 会话复用 = 第二次运行同样 PASS 且无 AUTH_REQUIRED(不再需要扫码)

脚本本次扩展:`--platform` choices 由 `("ks","bili")` 扩为 `("xhs","dy","ks","bili")`(已改,**未提交**)。

### 1.2 验证结果(全部 ✅)

| 平台 | 第 1 轮 | 第 2 轮(复用) | 登录态建立时刻 |
|---|---|---|---|
| xhs 小红书 | **PASS** 186s / 20 条,免扫码 | (见 1.4 已知问题①) | 2026-11-20 验收日,一直有效 |
| dy 抖音 | **PASS** 109.5s / 14 条,**本次扫码** | **PASS** 20.3s / 14 条,免扫码 | 2026-11-21 14:14(此前 dy 从无登录态) |
| ks 快手 | **PASS** 57.1s / 17 条 | **PASS** 47.6s / 17 条,免扫码 | 2026-11-21 13:54(并行会话扫码) |
| bili B站 | **PASS** 121.6s / 20 条 | **PASS** 67.6s / 20 条,免扫码 | 2026-11-21 13:58(并行会话扫码) |

结论:**是的,4 个平台现在全部处于登录成功状态**,且每一平台都有「生产同路径真实检索拿到记录 + 免扫码复用」双重证据。

### 1.3 验证前修复的两个 MC 阻断 bug(不修无法验证)

1. **代理拦 CDP 本地请求**(已修):`MediaCrawler/tools/cdp_browser.py:294`
   - 原因:`httpx.AsyncClient()` 默认 `trust_env=True`,系统代理(3067)把 `localhost:9223` 的 CDP 控制请求也转发出去 → 报裸 HTTP 502(13:46 那次 ks 失败即此)。
   - 修复:`httpx.AsyncClient(trust_env=False)`(带注释)。
2. **抖音扫码窗口只有 1 分钟**(已修):`MediaCrawler/media_platform/douyin/login.py` 的 `popup_login_dialog`
   - 原因:登录弹层(`login-full-panel-*`)已自动弹出时,10s 等待仍超时 → 强点被弹层遮住的登录按钮 → 30s 拦截超时崩溃,扫码窗口实际只存在约 1 分钟。
   - 修复:弹层可见(`login-panel-new` 或 `login-full-panel-*`)即跳过强点 + 点击兜底 try/except;扫码窗口扩到 `check_login_state` 的 600s 循环。

### 1.4 已知问题(不阻塞,建议另案)

- **xhs pong 探活假阴性**:第 2/3 轮同一批 cookie 被 `xhs_client.pong()` 判「未登录」,误入扫码流程后崩掉(拿到的是 AUTH_REQUIRED 误报;真实报错是 Playwright 等 `//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button` 30s 超时)。根因 = pong 无重试,风控抖动即误判。**判定登录态以「免扫码拿到记录」为准。**
- 标准回退 `channel="chrome"` 本机无 Chrome——已被并行会话 7915d22 改为 `msedge`(4 处带 `[2026-11-21 auto-video-editor 接入固定]` 注释)。

### 1.5 并行会话说明与 MC 本地故意修改清单

验证期间另一会话在同步工作:13:54/13:58 的 ks/bili 扫码、提交 `7915d22`(验收脚本入库 + Edge 补丁)都是它做的。本会话的增量 = dy 首次扫码登录实证 + 上述两个 MC 补丁 + 四平台 fresh PASS。

**MC 仓库本地故意修改现有 5 文件**(git pull 前必须处理,否则静默回退):

1. `config/base_config.py` —— CDP_CONNECT_EXISTING True→False(方案B,带注释)
2. `media_platform/kuaishou/core.py` —— channel="msedge" ×2(并行会话)
3. `media_platform/bilibili/core.py` —— channel="msedge" ×2(并行会话)
4. `tools/cdp_browser.py` —— trust_env=False(本会话)
5. `media_platform/douyin/login.py` —— popup_login_dialog 弹层守卫(本会话)

**未入库项**(auto-video-editor 仓库):`scripts/verify_mc_ks_bili_login.py` 4 平台扩展(M 状态);`runtime/mc_login_verify/` 运行产物(不入库)。

---

## 二、新需求:登录预检闸门

### 2.1 需求原文

> 修改程序:每次运行这个节点之前,都要以上的每一个自媒体程序的验证登录是否成功并通过。

「这个节点」= 网页话题检索节点(`storyline_search_web_topic`,消费 MediaCrawler 平台链 xhs→dy→ks→bili 的节点)。

### 2.2 用户拍板的三个语义点(AskUserQuestion 确认记录)

| 问题 | 用户选择 |
|---|---|
| 闸门发现某平台登录未通过时怎么办 | **「停下来,弹出二维码,让我扫码登录。」**(自定义答案:未登录就地跑登录流程,登上了再继续) |
| 检查深度 | **pong 探活**(启动浏览器打开首页,调与生产完全相同的登录门 API,约 10-20s/平台,不执行真实搜索) |
| 闸门范围 | **实际链平台**(主平台 + env 里的 fallback;默认 env="dy" 时只检 xhs+dy,扩链到 "dy,ks,bili" 后即全部四平台) |

推导出的完整闸门语义:

- 链内逐平台串行 pong 探活(双测防抖,间隔 3s)
- 未登录即就地跑生产登录流程(QR 窗口弹出,等待扫码最长约 600s)→ 复检 pong
- 全部通过 → 照常跑平台链
- 最终仍未登录 → 节点失败(`LOGIN_GATE_FAILED`),不跑链、不回退 Bing
- 闸门只在 cache miss 且 engine=mediacrawler 时执行

---

## 三、代码事实(探索结论,全部已核实)

探索方式:2 个 Explore 子代理成功返回 + 1 个子代理因**并发上限=1**失败(`user concurrency limit exceeded`,tool_uses: 0,无脏数据)→ 失败代理的 6 个问题由主会话直接读文件补齐(无信息缺口,不重派)。

### 3.1 MC 四平台的登录门 = 各自的 `pong()` 探活

| 平台 | pong 位置 | 判定逻辑 |
|---|---|---|
| xhs | `media_platform/xhs/client.py:263-281` | GET `/api/sns/web/v1/user/selfinfo`,`data.result.success` 即 True |
| dy | `media_platform/douyin/client.py:163-172` | 页面 localStorage `HasUserLogin=="1"` 或 cookie `LOGIN_STATUS=="1"` |
| ks | `media_platform/kuaishou/client.py:191-211` | POST GraphQL `visionProfileUserList`,`result==1` 即 True |
| bili | `media_platform/bilibili/client.py:154-167` | GET `/x/web-interface/nav`,`isLogin` 即 True |

**关键推论**:pong 就是生产爬取的真实登录门(core.start() 里 `if not pong(): 进登录流程`)。pong 失败时真实链路该平台必然也会死(进扫码流程)。所以 pong 预检 = 对生产行为的准确预测,预检拦下是正确行为(避免浪费链预算)。

### 3.2 MC 无「只检查登录」CLI;最小检查路径

`main.py` 的 CRAWLER_TYPE 枚举仅 search/detail/creator(arg.py:60-65),无登录检查模式。「仅检查登录」的最小路径 = 启动浏览器(CDP,profile=`cdp_<p>_user_data_dir`)→ new_page → goto(首页)→ `convert_browser_context_cookies` → 构造 client → `pong()`。四平台 core.start() 里这些步骤全是**可复用的爬虫类方法**(launch_browser_with_cdp / create_xxx_client / index_url 实例属性)。

- 平台→Crawler 映射:`main.py:51-59`(`CrawlerFactory.CRAWLERS`)
- CDP user_data_dir:`tools/cdp_browser.py:256-264`,`browser_data/cdp_{USER_DATA_DIR % PLATFORM}`
- MC 日志仅控制台(tools/utils.py:29-45,无 FileHandler、无 logs/ 目录);外部捕获只能靠子进程 stdout/stderr

### 3.3 适配器既有纪律(直接复用)

`storyline_capabilities/mediacrawler_client.py`:

- `preflight()` :167-183(根目录/main.py/venv python 三类缺失)
- `acquire_cdp_lock` / `release_cdp_lock` :270-310(同 CDP 端口互斥,search_notes :405 使用)
- `_run_subprocess` :329-360(Popen + 超时进程树击杀,继承父 env)
- MC python 解析:`_resolve_python_exe` :158-165(`.venv/Scripts/python.exe`),search_notes :426 使用
- AUTH 分类:`_AUTH_HINT_MARKERS` :115-125 + :460-467(退出码 0 无结果文件 + 标记 → AUTH_REQUIRED)
- 失败现场落盘:`_persist_failure_evidence` :219-249(`<run_id>/stderr_full.txt`,TTL 兜底)

### 3.4 能力层 MC 链入口与闸门插入点

`storyline_capabilities/search_web_topic.py`:

- :1447 `if provider == "mediacrawler":`
- :1450-1461 provider_options 解析(platform/fallback_platforms/chain_timeout_seconds/project_root/save_root/timeout_seconds/headless/mc_revision)
- :1462 `platform_chain = (mc_platform, *mc_fallback_platforms)`
- :1466-1470 `effective_request_hash = build_request_hash(..., mediacrawler_request_provider(platform_chain, mc_revision), ...)`(**闸门插入点 A:此行之后**)
- :1479-1552 平台循环(search_notes → `_map_mc_records` → 相关性守卫 `_count_relevant_results` → attempts 记录)
- :1554-1566 diagnostics 构造(platform_chain/attempts/primary 等)
- :1568-1603 某平台成功返回;:1605-1684 全败 → Bing 一次(三种返回形态)
- 产物 schema:`ARTIFACT_SCHEMA_VERSION` :73-75(v2;改产物字段结构须 bump,读侧 .get 兼容)

### 3.5 节点层(调用点/缓存/失败语义)

`nodes/storyline/node_search_web_topic.py`:

- `storyline_search_web_topic_node` :122;每轮必跑(2026-11-14 依赖约简),开关关闭/检索词空 → 软跳过(:130-138,SKIPPED 标签)
- mediacrawler 分支 :153-175:组装 provider_options(:158-169)、hash_provider 含完整有序链(:172-174)
- **缓存** :192-203:`_load_reusable_cache`(:72-98,query_hash+request_hash 双比较)命中直接返回,**不触 MC → 天然不预检**
- `cap_topic` 调用 :205-213,异常软失败 :214-223(error_log + `storyline_search_web_topic_failed` 标签)
- `_ERROR_STATUS_TAGS` :46-55(error_code → 固定标签)
- 节点不发射 custom 事件(无 get_stream_writer);节点内上报先例 = `nodes/node_05_generate_draft.py:423-442`(`_emit_monitor_event`,载荷限 `monitoring/workflow_events.py:97-107` ALLOWED_PAYLOAD_KEYS 白名单);节点签名加 `Optional[RunnableConfig]` 的先例 = node_08(text-fx-pool 记忆)

### 3.6 图级失败语义(代理 3 返回)

- 图内无节点级 try/except 包装(graph.py:554-560 原样注册);异常在流里(task end error)与外层 raise 双现(monitoring/workflow_runner.py:16-18, 277-283),不吞异常
- 节点硬失败(raise)先例:`nodes/storyline/node_generate_script.py:169-174`(narration 全空 fail-closed)
- SCHEMA_VERSION=13(`monitoring/workflow_graph.py:89`);**节点内部新增步骤、不改图拓扑/节点元数据 → 不 bump**(先例 workflow_graph.py:72-73)

### 3.7 现有测试矩阵与 stub 方式

| 文件 | 覆盖 |
|---|---|
| `tests/unit/test_storyline_search_web_topic_mc.py` | 平台链接力/预算/登录分类/hash/部分准入(`_patch_mc` monkeypatch `cap_mod._mc_client.search_notes`,:48-60) |
| `tests/unit/test_mediacrawler_client.py` | MC 子进程适配器/登录态启发式/目录契约 |
| `tests/unit/test_config_mc_platform_chain.py` | 平台链解析/校验/白名单/默认值/超时范围 |
| `tests/unit/nodes/test_node_search_web_topic.py` | 节点级(标签映射/缓存/request_hash) |
| `tests/integration/test_unreachable_nodes_wiring.py` | 图级黄金轨迹(GOLDEN_STEPS :102-140,断言 :852) |

基线:全量单测 2904 例零失败(7915d22 时点)。

---

## 四、实施计划(8 步)

### 步骤 1:MC 侧新文件 `MediaCrawler/check_login.py`

CLI:`--platform xhs|dy|ks|bili` + `--interactive`(默认开) + watchdog。流程复用各平台 Crawler 类生产方法:

1. 实例化 `CrawlerFactory.CRAWLERS[platform]`(main.py:51-59)
2. `launch_browser_with_cdp(...)`(与 core.start 同一 CDP 路径/同 profile)→ new_page → goto(实例 index_url)
3. `create_xxx_client(...)`(各平台同方法)→ `pong()` **双测**(间隔 3s,防 xhs 风控抖动假阴性)
4. 双失败且 interactive → 实例化该平台 `XxxLogin` 并 `begin()`(生产扫码流程,QR 弹窗,最长约 600s)→ 结束后再 pong
5. stdout 输出 JSON verdict `{"platform", "logged_in", "login_attempted", "elapsed_ms"}`;exit 0=已登录 / 1=未登录 / 2=检查自身异常;清理对齐 main.py 的 async_cleanup(cdp_manager/browser_context)

### 步骤 2:MC 侧小补丁 `media_platform/xhs/login.py`

与 douyin 修法同款弹层守卫:login_by_qrcode 找不到 QR 图时,先探测登录弹层是否已可见,可见则跳过强点登录按钮(防 xhs pong 假阴性场景下闸门登录流程崩溃;即预检会话实测的 login.py:181 30s 超时崩溃点)。MC 本地故意修改文件数变为 6(+check_login.py 新文件无冲突)。

### 步骤 3:适配器新增 `verify_login()`

`storyline_capabilities/mediacrawler_client.py`:

```
verify_login(platform, project_root, timeout_seconds, cdp_port=9222,
             lock_dir=None, interactive=True)
    -> (logged_in: bool|None, error_code, message, diagnostics)
```

- 复用 `preflight()` → `acquire_cdp_lock`(与 search_notes **同一把锁**,预检与链天然互斥)→ `_run_subprocess([mc_python, "check_login.py", "--platform", p, ...], cwd=MC根)` → 解析 stdout 末行 JSON
- 归类:exit0+true=LOGGED_IN;exit0+false 或 exit1=NOT_LOGGED_IN;exit2/timeout/OSError/JSON 解析失败=CHECK_ERROR(瞬态,附 stderr_tail)
- diagnostics 复用 `_tail`/`_redact` 脱敏;不落 save_root(预检无数据目录)

### 步骤 4:能力层插入闸门

`search_web_topic.py`,位置=:1470(`effective_request_hash` 后)与 :1479(平台循环前):

- provider_options 新键:`login_gate_enabled` / `login_gate_timeout_seconds` / `notify`(回调)
- 禁用或键缺省 → 零调用,行为与现状逐字一致
- 启用 → 逐平台 `verify_login`;interactive 登录+复检后仍失败 → 硬失败
- `diagnostics["login_preflight"]` = `{enabled, results:[{platform, logged_in, login_attempted, elapsed_ms}], verdict, failed_platforms, elapsed_ms}`;notify 在「等待扫码」与「闸门结果」两处回调
- 全过 → 原链路照跑;任一未过 → 返回 `error_code="LOGIN_GATE_FAILED"` doc(message 列未过平台,**不跑链不 Bing**)
- `ARTIFACT_SCHEMA_VERSION` v2→v3(新增 diagnostics.login_preflight 字段,读侧 .get 兼容)

### 步骤 5:配置 `config.py`(沿用 :212-222 样板:模块级读 env → 校验 → ValueError)

- `STORYLINE_WEB_TOPIC_MC_LOGIN_GATE`(默认 "1";"0"/"false"/"off" 关闭 = 回滚开关)
- `STORYLINE_WEB_TOPIC_MC_LOGIN_GATE_TIMEOUT`(默认 660,范围 60..1200,秒/平台;须覆盖 MC 登录流程 600s 扫码窗口)

### 步骤 6:节点层

`nodes/storyline/node_search_web_topic.py`:

- mediacrawler 分支(:158-169)provider_options 透传两配置 + notify 回调;节点签名加 `config: Optional[RunnableConfig] = None`(node_08 先例),用 `get_stream_writer` 发 node.progress 事件(载荷限白名单,node_05 :423-442 先例):「登录预检等待扫码(平台X)」与闸门结果
- `_ERROR_STATUS_TAGS`(:46-55)加 `"LOGIN_GATE_FAILED": "storyline_search_web_topic_login_gate_failed"`;失败走既有 failed 路径写 error_log

### 步骤 7:测试(全 stub,不触真 MC)

- `tests/unit/test_mediacrawler_client.py`:verify_login 分类矩阵(monkeypatch `_run_subprocess`):exit0+true / exit0+false / exit1 / exit2 / 超时 / 锁忙 / preflight 失败 / JSON 解析失败兜底 / interactive 参数透传
- `tests/unit/test_storyline_search_web_topic_mc.py`:闸门全过 → 链照跑 + `diagnostics.login_preflight.verdict="pass"`;闸门失败 → LOGIN_GATE_FAILED 且零 search_notes 调用、无 Bing 回退;禁用 → 零 verify_login 调用;逐平台 interactive 通过序列(monkeypatch verify_login)
- `tests/unit/test_config_mc_platform_chain.py`:两新 env 默认值/范围/非法值
- `tests/unit/nodes/test_node_search_web_topic.py`:LOGIN_GATE_FAILED 标签映射 + notify 事件发射

### 步骤 8:执行记录文档

`docs/第10期开发/MC平台登录预检闸门_执行记录_20261121.md`(实施后写:改动清单/判定语义/操作员清单)。

---

## 五、运行代价

- cache miss 且闸门开启:每平台 +10~20s(pong 通过时)
- 需扫码的平台:+扫码时长(最长约 600s)
- 四平台全需扫码最坏 ≈ 40 分钟(`STORYLINE_WEB_TOPIC_MC_LOGIN_GATE_TIMEOUT` 可下调)
- cache hit **不预检**(节点层 :192-203 提前返回,不触 MC)

## 六、验证方案

1. 全量单测:`.venv/Scripts/python.exe -m pytest tests/unit -x -q`(基线 2904 例零失败 + 新增)
2. 真机冒烟:`verify_login(platform="xhs", interactive=False)` → 应免扫码 LOGGED_IN;`interactive=True` 对已登录平台应同样直接 PASS 不弹窗
3. 真机扫码场景(操作员):临时把某平台 `browser_data/cdp_<p>_user_data_dir` 改名模拟未登录 → 跑真实任务 → 应弹 QR → 扫码后任务继续 → 产物 `storyline/search_web_topic.json` 的 `diagnostics.login_preflight` 留痕
4. 回滚验证:`STORYLINE_WEB_TOPIC_MC_LOGIN_GATE="0"` 重启 → 行为与现状逐字一致(单测锁定)

## 七、风险

- **xhs pong 假阴性**(风控抖动)会触发不必要的扫码弹窗——双测(3s 间隔两次 pong)已缓解;若扫码窗口出现但实际已登录,操作员关窗后复检 pong 通过即继续
- 闸门串行 + 扫码等待会拉长节点耗时(操作员不在场时最坏 4×timeout);timeout 可配置下调
- MC 目录新增 check_login.py 为游离新文件,git pull 无冲突;MC 本地故意修改文件数变为 6(+xhs login.py 小补丁)

## 八、明确不做

- 不动 langgraph interrupt/挂起恢复机制(扫码交互在节点内同步完成,复用 MC 现成登录流程)
- 不动 `workflow_graph.SCHEMA_VERSION`(拓扑零变更)
- 不给 bing_html/reach_mcp 引擎加闸门(它们不用 MC)
- 本次不主动 git 提交(随下次 sync-github 由用户发起)

---

## 附:探索过程记录

- Explore 子代理 ×3 并行派发:2 成功(MC 登录检查入口;节点失败模式与测试矩阵),1 失败(探 web-topic 节点与适配器链)——失败原因 = 本机子智能体并发上限 1(`user concurrency limit exceeded`),`tool_uses: 0` 无脏数据。
- 失败代理的 6 个问题(节点调用点/链组装/适配器子进程纪律/失败语义/守卫与缓存落点/预检先例)由主会话直接读文件补齐,无信息缺口,未重新派发。

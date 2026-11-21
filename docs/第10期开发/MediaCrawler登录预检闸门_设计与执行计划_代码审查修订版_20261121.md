# MediaCrawler 登录预检闸门——代码核验、设计修订与分阶段执行计划

> 日期：2026-11-21  
> 审查基线：`wayyet/auto-video-editor` `main`，提交 `af96d00433e141095269cb7d289c01a6978ced02`  
> MediaCrawler 上游基线：仓库配置注释固定的 `09ed1ec62d885dab422a53de999f52ac0bd4966b`  
> 状态：**设计审查修订版 / 待批准实施**  
> 本文只完成代码核验与计划修订；本次未修改任何仓库文件、未运行本机真实登录、未执行 Git 提交或同步操作。

---

## 0. 执行摘要

附件提出的“MediaCrawler 平台登录预检闸门”方向可行：在真正执行 MediaCrawler 搜索前，对当前平台链逐个平台验证登录状态；需要登录时复用 MediaCrawler 的二维码登录流程；所有平台通过后才允许进入搜索链；登录闸门失败时必须阻断 MediaCrawler 链及其 Bing 回退。

但**附件原方案不能直接照单实施**。本次对 `main` 和固定 MediaCrawler 上游代码核验后，发现若干会影响功能正确性的缺口：

1. 当前 `auto-video-editor/main` 只准入 `xhs`、`dy`，配置 `dy,ks,bili` 会在导入 `config.py` 时触发 `ValueError`。当前默认实际链为 **xhs → dy → Bing**，不是四平台链。
2. MediaCrawler 通过全局 `config.PLATFORM` 选择浏览器 profile。新增检查脚本必须逐平台设置该值，否则可能检查错误的 `cdp_<platform>_user_data_dir`。
3. 登录后必须调用客户端 `update_cookies()`，刷新客户端持有的 Cookie 快照，再执行最终 `pong()`。附件步骤没有明确写出这一步。
4. 各平台 `pong()` 接口签名不一致；其中 xhs、ks、bili 会捕获异常并统一返回 `False`。因此 `False` 不一定代表“确定未登录”，也可能是网络/API/风控异常。不能把两次 `False` 直接等同于需要扫码。
5. `XxxLogin.begin()` 可能通过 `sys.exit()` 退出，且部分调用使用默认 `sys.exit()`（退出码可能为 0）。检查子进程必须捕获 `SystemExit`，并确保输出结构化结果，不能只根据 return code 判定成功。
6. 当前节点的子进程执行器使用 `communicate()`，只在子进程结束后取得 stdout/stderr。附件要求的“二维码等待时实时通知”无法靠当前执行器和普通 `notify` 回调实现，必须增加可流式读取的进度协议。
7. 节点当前导入名为 `config` 的应用配置模块。若按附件给节点函数增加名为 `config` 的 `RunnableConfig` 参数，会遮蔽模块名，导致 `config.STORYLINE_...` 访问错误。
8. `LOGIN_GATE_FAILED` 若作为普通 `failed` 产物写入，当前缓存逻辑可能把它复用；用户完成扫码后再次进入节点，仍可能命中旧失败缓存。此错误必须排除在可复用缓存之外。

**推荐修订方向：**保留“只检查实际生效平台链”“cache miss 才预检”“登录闸门失败不回退 Bing”的业务语义；增加 tri-state 登录检查结果、逐平台 profile 初始化、登录后 Cookie 刷新、流式进度协议和登录失败缓存排除。实施顺序应先完成 MediaCrawler 独立检查脚本与适配器，再接能力层和节点层，最后做真机扫码验收。

---

## 1. 审查依据与边界

### 1.1 仓库版本

本次读取的 `main` 分支 HEAD：

- 仓库：<https://github.com/wayyet/auto-video-editor>
- 分支：`main`
- commit：`af96d00433e141095269cb7d289c01a6978ced02`
- commit message 表明本次提交入库的是 MC 平台链执行矩阵测试；该提交并未包含登录预检闸门。

本次检查到的关键文件：

| 文件 | 当前代码状态 |
|---|---|
| `storyline_capabilities/mediacrawler_client.py` | 有 `preflight()`、CDP 锁、子进程执行、`search_notes()`；没有 `verify_login()` |
| `storyline_capabilities/search_web_topic.py` | 已有 MC 平台链循环、相关性检查、全链失败后 Bing 回退；没有登录预检 gate |
| `nodes/storyline/node_search_web_topic.py` | 有 query/request hash、缓存、`cap_topic()` 调用；无登录预检及该节点进度事件 |
| `config.py` | MC 平台白名单为 `("xhs", "dy")`；默认主平台 xhs、默认 fallback dy |
| `tests/unit/test_storyline_search_web_topic_mc.py` | 已覆盖当前 MC 平台链，不含登录闸门用例 |
| `tests/unit/test_mediacrawler_client.py` | 已覆盖当前适配器行为，不含 `verify_login()` 用例 |
| `tests/unit/test_config_mc_platform_chain.py` | 明确断言当前白名单只有 xhs、dy |
| `tests/unit/nodes/test_node_search_web_topic.py` | 有节点缓存和状态标签测试，不含登录闸门标签/事件测试 |

### 1.2 外部 MediaCrawler 版本

`auto-video-editor/config.py` 的注释把 MediaCrawler 固定版本记为 `09ed1ec6`。本次按完整 SHA 检查该版本的 `main.py`、`tools/cdp_browser.py`、四个平台 `core.py`、`client.py`、`login.py`。

上游仓库：<https://github.com/NanmiCoder/MediaCrawler>  
固定版本：`09ed1ec62d885dab422a53de999f52ac0bd4966b`

注意：本次只能从 GitHub 核验公开代码，**无法验证用户本机 MediaCrawler 工作目录中未提交的五项本地修改、浏览器 profile 实际内容、Cookie 是否仍有效，以及附件记录的扫码时间和真机检索结果**。这些内容在本文中均按“附件声明/用户本机待复测”处理，不冒充本次独立验证结果。

### 1.3 核验等级

- **已核实**：当前 GitHub `main` 或固定上游源码可直接证明。
- **附件声明**：只在用户提供的 MD 中有记录，本次无法远程验证本机事实。
- **设计推导**：根据已核实代码行为推导出的修正要求。
- **实施后验证**：必须通过本地测试或真实浏览器验收才能关闭。

---

## 2. 当前代码事实核验

### 2.1 当前真正生效的平台链不是四个平台

当前 `config.py` 中：

```python
_MC_PLATFORM_WHITELIST = ("xhs", "dy")
STORYLINE_WEB_TOPIC_MC_PLATFORM = "xhs"  # 未设置 env 时的默认值
STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = ("dy",)  # 未设置 env 时的默认值
```

`_parse_mc_platform_chain()` 会拒绝白名单外的平台。因此在当前 `main`：

```dotenv
STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=dy,ks,bili
```

**不能生效**，配置导入阶段就会报错。不能在实施登录闸门时绕过这个准入检查。

当前默认实际链：

```text
xhs -> dy -> Bing
```

ks、bili 即使在用户本机通过过单独登录或真实检索测试，也不代表它们已经获得 `auto-video-editor` 生产平台链准入。平台准入至少还需要核验字段映射、输出目录、搜索接口、链接映射、相关性守卫、超时预算与回退测试。

**设计约束：登录闸门只检查运行时解析出的 `platform_chain=(primary, *fallbacks)`，不检查未进入链的候选平台。**

### 2.2 当前节点有缓存短路

`nodes/storyline/node_search_web_topic.py` 的执行顺序是：

1. 检查功能开关、查询词。
2. 读取当前引擎配置，组装 MC `provider_options`。
3. 计算 `query_hash` 和 `request_hash`。
4. `_load_reusable_cache()` 命中时直接返回，不调用能力层。
5. cache miss 才调用 `cap_topic()`。

因此附件选择“cache miss 且 engine=mediacrawler 时执行闸门”与当前执行顺序相符。缓存命中时不启动 MediaCrawler，也就没有必要为这次节点执行额外启动浏览器探活。

但这意味着“每次运行节点之前都验证”的字面要求并未覆盖 cache hit。本文推荐把需求定义为：**每次实际准备调用 MediaCrawler 前都执行登录闸门；不执行外部检索的 cache hit 不预检。** 若业务要求字面上的每次节点调用都检查，则必须把 gate 移到缓存判断之前，并接受缓存命中也会启动浏览器、延长 QA 重试时间。

### 2.3 能力层已有链路与 Bing 回退

`storyline_capabilities/search_web_topic.py` 当前已经具备：

- 从 `provider_options` 读取主平台、fallback 平台、单平台超时、整链预算等。
- 组装有序 `platform_chain`。
- 逐平台调用 `_mc_client.search_notes()`。
- 对映射后的记录执行相关性检查。
- 任一平台成功即停止后续平台。
- 全链失败后只调用一次 Bing。

登录闸门必须插入在 `platform_chain` 和 `effective_request_hash` 已确定之后、平台循环之前。失败时应直接返回登录闸门错误产物，**不能进入平台循环，也不能进入 Bing 回退分支**。

### 2.4 MediaCrawler 生产登录流程事实

固定上游版本的 `main.py` 提供 `CrawlerFactory.CRAWLERS`，平台映射包含 `xhs`、`dy`、`ks`、`bili`。各平台 `core.start()` 大体按以下顺序执行：

```text
启动 Playwright / CDP 浏览器
  -> 创建 page
  -> 打开平台首页
  -> 创建平台 client
  -> client.pong()
  -> pong 为 False 时创建对应 XxxLogin 并 begin()
  -> client.update_cookies(browser_context, urls=...)
  -> 后续爬取
```

实际实现有平台差异：

| 平台 | `pong()` 调用签名 | 当前判定逻辑 | 特别注意 |
|---|---|---|---|
| xhs | `pong()` | 请求用户 self-info，`data.result.success` 为真 | 捕获异常后返回 False；登录状态检查也依赖浏览器 Cookie/UI |
| dy | `pong(browser_context=...)` | localStorage `HasUserLogin == "1"` 或 Cookie `LOGIN_STATUS == "1"` | 必须传 `browser_context`；不能统一按零参数调用 |
| ks | `pong()` | GraphQL `visionProfileUserList.result == 1` | 捕获异常后返回 False；首页初始化还会注入 `KS_SIGN_CAPTURE_SCRIPT` |
| bili | `pong()` | `/x/web-interface/nav` 响应的 `isLogin` | 捕获异常后返回 False；生产登录后会刷新 Cookie 并再次 pong |

上述事实说明：复用 `pong()` 是正确方向，但“每个平台统一调用 `client.pong()`”并不成立。检查脚本需要一个平台适配层，负责正确创建 client 并传入对应参数。

### 2.5 浏览器 profile 选择依赖全局 `config.PLATFORM`

MediaCrawler 的 `tools/cdp_browser.py` 在 CDP 模式启动浏览器时，使用：

```python
user_data_dir = os.path.join(
    os.getcwd(),
    "browser_data",
    f"cdp_{config.USER_DATA_DIR % config.PLATFORM}",
)
```

所以 `check_login.py --platform ks` 不只是把 `ks` 传给一个函数；必须在启动 CDP 管理器前设置 MediaCrawler 全局 `config.PLATFORM = "ks"`，并确保 `cwd` 与生产 MediaCrawler 根目录一致。否则可能复用错误平台的浏览器 profile，造成假通过或假未登录。

### 2.6 登录成功后必须刷新 client Cookie

生产 core 在 `XxxLogin.begin()` 结束后会执行：

```python
await client.update_cookies(
    browser_context=browser_context,
    urls=cookie_urls,
)
```

这是因为 client 构造时已保存 Cookie/header 快照。仅在浏览器里完成扫码，不代表已构造的 API client 自动获得新 Cookie。登录闸门必须在 `begin()` 后先刷新 Cookie，再进行最终登录状态检查。

B 站生产 core 还在 Cookie 更新后再次 `pong()`，若仍未登录则终止，避免死会话导致视频清晰度静默降级。该流程应作为预检实现的最低要求，而不是省略。

### 2.7 pong 的 False 不是可靠的“确定未登录”

xhs、ks、bili 的 `pong()` 会捕获异常并返回 False；这会把以下不同情况混在一起：

- 会话确实未登录或已失效。
- API 暂时不可用、网络超时、代理异常。
- 平台接口变更、响应结构变化。
- 风控或临时拒绝。

连续两次 False（间隔 3 秒）可以降低偶发抖动，但不能从逻辑上区分认证失败与传输失败。若把所有 False 都送进扫码流程，会出现不必要的扫码提示，甚至在真正的问题是网络时错误地要求用户重新认证。

**修订要求：登录探测至少输出三态结果：**

- `LOGGED_IN`：有明确登录成功证据。
- `NOT_LOGGED_IN`：有明确未登录/会话失效证据，允许触发交互登录。
- `CHECK_ERROR`：探测遇到网络、接口、页面或程序异常，不能据此要求用户扫码。

建议为 client 增加可诊断的 `pong_with_diagnostics()` / `probe_login_state()`，复用与生产 `pong()` 相同的底层接口；原 `pong()` 保持 bool 兼容，以免改变其他生产调用者行为。

### 2.8 当前子进程执行器不支持扫码过程中的实时通知

适配器 `_run_subprocess()` 用 `Popen.communicate(timeout=...)` 等待子进程完成后才返回全部 stdout/stderr。它不提供运行期间的逐行事件。

因此仅在 `provider_options` 里加入 `notify` 回调，并不能知道子进程此刻是否已经显示二维码、正在等待用户扫码。要让网页工作流显示“正在等待 XHS 扫码”，必须在子进程与父进程之间增加实时进度协议。

建议使用 NDJSON（每行一个 JSON 对象）协议：

- 子进程 stdout 只输出结构化协议消息；MediaCrawler 普通日志统一写 stderr。
- 事件例：`{"type":"progress","event":"login_waiting","platform":"xhs"}`。
- 结束消息：`{"type":"result","platform":"xhs","state":"LOGGED_IN","login_attempted":true,"elapsed_ms":12345}`。
- 父进程逐行读取 stdout，并并行消费 stderr，避免子进程 stderr pipe 缓冲区写满造成死锁。
- 父进程解析事件后调用节点传入的 `notify`，由节点通过 `get_stream_writer()` 发 `node.progress`。
- 超时、异常、无最终 result 消息、协议版本错误均作为 `CHECK_ERROR`，不得推断为登录成功。

### 2.9 `SystemExit()` 需要显式处理

上游多个 `XxxLogin.begin()` / `login_by_qrcode()` 路径会调用 `sys.exit()`。无参数 `sys.exit()` 可能以退出码 0 退出；退出码 0 并不等于登录成功。

`check_login.py` 顶层必须捕获 `SystemExit`，将其转换为明确的 JSON 结果（通常为 `CHECK_ERROR` 或 `NOT_LOGGED_IN`，依照是否有明确登录失败原因），并保证 finally 清理浏览器。适配器必须同时校验退出码、最终 JSON 消息类型、平台名和状态值；**只要缺少有效最终 JSON，就不能判定为 PASS**。

---

## 3. 修订后的需求契约

### 3.1 闸门适用范围

只有同时满足以下条件才执行登录闸门：

1. `STORYLINE_ENABLE_WEB_TOPIC` 开启。
2. 查询词经过清洗后非空。
3. `STORYLINE_WEB_TOPIC_ENGINE == "mediacrawler"`。
4. 当前节点 cache miss，或 `web_topic_force_refresh=True`。
5. `login_gate_enabled=True`。

其他引擎（如 `bing_html`、`reach_mcp`）不执行 MediaCrawler 登录探测。

### 3.2 平台范围

实际探测列表必须直接来自能力层最终计算的：

```python
platform_chain = (mc_platform, *mc_fallback_platforms)
```

不从脚本 choices、用户曾经登录过的平台列表或硬编码四平台列表推导。当前 `main` 的实际链默认是 `("xhs", "dy")`。

ks、bili 只有在平台准入代码、配置白名单和对应测试全部更新后，才允许进入链并被登录闸门探测。不得仅凭本机扫码成功就修改白名单。

### 3.3 单个平台检查状态机

```text
启动对应平台浏览器 / 使用对应 profile
  -> 打开对应首页并执行平台特定初始化
  -> 创建对应 client
  -> 探测登录态（最多两次，间隔 3 秒；瞬态错误按独立重试策略处理）
       ├─ LOGGED_IN      -> 当前平台通过
       ├─ CHECK_ERROR    -> 记录错误，不弹二维码；闸门失败
       └─ NOT_LOGGED_IN  -> interactive=true 时运行 XxxLogin.begin()
                              -> 更新 client Cookie
                              -> 再次探测登录态
                                  ├─ LOGGED_IN   -> 当前平台通过
                                  └─ 其他状态    -> 当前平台失败
```

`interactive=False` 用于自动化测试或只检查模式；若明确未登录，直接返回 `NOT_LOGGED_IN`，不展示二维码。

### 3.4 整条链的闸门语义

- 按平台链顺序串行检查；每个平台都要通过，即使主平台已登录，也不能跳过链中 fallback 平台（这是本次“实际链平台”要求的含义）。
- 所有平台均为 `LOGGED_IN`：返回 `pass`，继续现有平台链检索。
- 任一平台在登录流程后仍 `NOT_LOGGED_IN`：返回 `LOGIN_GATE_FAILED`。
- 任一平台发生 `CHECK_ERROR`：返回 `LOGIN_GATE_FAILED`，diagnostics 中标明 `CHECK_ERROR`；不自动弹二维码。
- gate 失败：**不调用任何 `search_notes()`，不调用 Bing fallback**。
- gate 成功：不改变原平台搜索顺序、单平台预算、整链预算、相关性守卫和 Bing 回退逻辑。

### 3.5 缓存语义

推荐继续 cache miss 才预检，因为 cache hit 不启动 MediaCrawler。但增加以下规则：

- `LOGIN_GATE_FAILED` 不得被 `_load_reusable_cache()` 当作可复用失败缓存。
- `LOGIN_CHECK_ERROR`（若采用独立错误码）也不得被缓存复用。
- 操作者修复登录态后再次进入节点，应重新执行登录闸门，而不是复用上次的失败产物。
- 正常搜索失败缓存沿用当前策略，除非后续另有缓存策略需求。
- `web_topic_force_refresh=True` 始终绕过缓存并触发预检。

---

## 4. 修订后的目标架构

### 4.1 MediaCrawler 侧新增 `check_login.py`

建议新增到本机 MediaCrawler 安装副本：

```text
<MediaCrawler根目录>/check_login.py
```

该文件不属于 `auto-video-editor` 仓库，不在其 Git 历史中提交。它是 auto-video-editor 适配器调用的外部检查程序。

执行职责：

1. 解析 `--platform xhs|dy|ks|bili`、`--interactive`、协议版本等参数。
2. 在导入/初始化平台 crawler 之前，设置 `config.PLATFORM` 为本次平台，并确保 CDP、登录类型、保存登录态、headless 等关键配置与生产 MediaCrawler 一致。
3. 以 MediaCrawler 根目录为 `cwd` 启动 Playwright / CDP，确保选中正确的 `browser_data/cdp_<platform>_user_data_dir`。
4. 复用对应 crawler 的 `index_url`、`launch_browser_with_cdp()`、`create_xxx_client()` 和平台特定页面初始化。
5. 特别处理平台差异：
   - dy：调用 `pong(browser_context=...)`。
   - ks：注入生产所需的 `KS_SIGN_CAPTURE_SCRIPT`，首页 URL 使用生产路径（当前 core 使用 `?isHome=1`）。
   - xhs：首页 URL 与 `XHS_INTERNATIONAL` 配置保持一致。
   - bili：按生产 core 的首页、client 和 Cookie 刷新逻辑执行。
6. 首轮探测失败时，只有明确 `NOT_LOGGED_IN` 且 `interactive=True` 才实例化对应 `XxxLogin` 并执行 `begin()`。
7. `begin()` 后调用 `client.update_cookies(...)`，再做最终探测。
8. 捕获 `SystemExit`、Playwright 异常、client 初始化异常及超时；输出结构化终态，不允许错误返回码伪装成功。
9. 在 `finally` 中关闭 page/context/CDP manager/Playwright，清理顺序与资源实际创建顺序一致；若 MC 自启浏览器进程，必须确认清理不会遗留占用 profile 的孤儿进程。
10. 不输出 Cookie、Token、二维码 Base64 或完整敏感 header；stdout 只输出协议消息，普通日志写 stderr。

**实现注意：**不要直接调用 `crawler.start()`，它会继续执行真实搜索。应拆用 crawler 中的初始化方法，或在 `check_login.py` 中实现与 `core.start()` 一致的初始化子集。

### 4.2 适配器新增 `verify_login()`

文件：`storyline_capabilities/mediacrawler_client.py`

推荐接口：

```python
def verify_login(
    *,
    platform: str,
    project_root: str,
    timeout_seconds: int,
    cdp_port: int = 9222,
    lock_dir: Path | None = None,
    interactive: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, str | None, str, dict[str, Any]]:
    """返回 (state, error_code, message, diagnostics)。

    state: LOGGED_IN | NOT_LOGGED_IN | CHECK_ERROR
    """
```

要求：

- 复用 `preflight()`、`_resolve_python_exe()`、`resolve_root()`、`_redact()`、`_tail()`、`_run_subprocess()` 的安全纪律。
- 使用流式子进程执行器接收 NDJSON 进度；不使用当前阻塞式 `communicate()` 来承担实时事件转发。
- 复用与搜索一致的 CDP 锁策略。建议在能力层统一管理本次 gate + search 的锁生命周期，避免检查结束释放锁、搜索开始前另一任务插入的竞态；如保留“每个子进程各自加锁”，必须明确接受该窗口，并由 search_notes 再次锁定防并发访问。
- 诊断只保存状态、平台、耗时、退出码、协议错误和脱敏 stderr tail；不落 Cookie、二维码图像或 profile 的敏感内容。
- 仅在收到合法最终 JSON 且状态为 `LOGGED_IN` 时返回登录通过。
- 超时、锁忙、缺少 JSON、JSON 非法、`SystemExit` 未被转换、平台回传不一致均归 `CHECK_ERROR`。

### 4.3 能力层执行 gate

文件：`storyline_capabilities/search_web_topic.py`

位置：MC 分支已解析完整 `platform_chain`、构造 `effective_request_hash` 之后，`for attempt_index, platform in enumerate(platform_chain)` 之前。

处理顺序：

```text
解析 provider_options 和 platform_chain
  -> 计算 effective_request_hash
  -> 若 login_gate_enabled：逐平台 verify_login()
       -> 任一非 LOGGED_IN：构造 LOGIN_GATE_FAILED 并立即返回
  -> 原有 MC 平台搜索循环
  -> 原有全链失败后的 Bing fallback
```

闸门失败结果至少包含：

```json
{
  "status": "failed",
  "error_code": "LOGIN_GATE_FAILED",
  "message": "MediaCrawler 登录预检未通过：dy",
  "requested_provider": "mediacrawler",
  "provider": "mediacrawler",
  "diagnostics": {
    "login_preflight": {
      "enabled": true,
      "verdict": "failed",
      "platform_chain": ["xhs", "dy"],
      "failed_platforms": ["dy"],
      "results": []
    }
  }
}
```

字段要求：

- `results[]` 按实际顺序记录 `platform`、`state`、`login_attempted`、`elapsed_ms`、安全的 `message`。
- `verdict` 只取 `pass` / `failed`。
- gate 失败时仍保留 `request_hash`、`requested_provider`、`provider`、`engine` 等现有产物契约字段。
- 失败时不能继续调用 Bing；不应把登录闸门失败伪装成 `NETWORK_ERROR` 或 `NO_RESULTS`。
- `notify`/`on_event` 只用于发送进度，不改变 gate 判定结果；回调异常应吞掉或隔离，不能阻断 gate 本身。

### 4.4 节点层配置、缓存及事件

文件：`nodes/storyline/node_search_web_topic.py`

新增配置透传：

- `login_gate_enabled`
- `login_gate_timeout_seconds`
- `login_gate_total_timeout_seconds`（建议新增整条登录预检总预算，避免四个平台各自达到单平台上限时节点无限拉长；是否启用及默认值须在实施前固定）
- `notify`（节点进度回调）

新增状态标签：

```python
"LOGIN_GATE_FAILED": "storyline_search_web_topic_login_gate_failed"
```

必须修复配置变量遮蔽问题：当前模块使用 `import config`。若函数新增 `config: RunnableConfig | None = None` 参数，参数名会遮蔽模块名。建议改为：

```python
import config as app_config
```

之后统一使用 `app_config.STORYLINE_...`，函数参数 `config` 留给 LangGraph runtime 注入；也可以采用项目现有约定的其他参数命名，但不可保留同名遮蔽。

事件采用现有 node custom stream 机制：

- `get_stream_writer()` 只在节点运行上下文中调用；捕获获取 writer 或写事件失败，观测通道不能改变主流程结果。
- 复用 `nodes/node_05_generate_draft.py` 的 `_emit_monitor_event()` 思路和 `monitoring/workflow_events.py` 的 payload 白名单。
- 子进程输出 `login_waiting` 事件后，节点才发送“等待扫码”；不能在子进程执行结束后倒序补发“等待扫码”事件。
- 事件内容只包含平台、阶段、耗时和安全提示，不包含 Cookie、Token、二维码内容或用户隐私。

### 4.5 产物 schema 版本

附件提出把 `ARTIFACT_SCHEMA_VERSION` 从 v2 升到 v3。需要注意：当前 `build_request_hash()` 把全局 `ARTIFACT_SCHEMA_VERSION` 纳入 hash，直接全局升 v3 会让 Bing、DDG、Google 等其他 provider 的现有缓存也全部失效，并不只影响 MC。

`diagnostics.login_preflight` 是新增的可选诊断字段，读侧可以通过 `.get()` 兼容，建议先按**向后兼容的可选字段**处理，不急于提升全局 schema。若最终审查确认必须升 schema，应在执行计划中明确记录“所有 provider 的 web-topic 缓存将整体失效”，并增加缓存迁移/重新检索成本说明；不要把这次影响描述为仅 MC 缓存失效。

`monitoring/workflow_graph.SCHEMA_VERSION` 与 artifact schema 是两件事。只增加节点内部检查逻辑、不改图拓扑和 state schema 时，通常不需要 bump workflow graph schema；若引入新的持久 state 字段或改变节点/边元数据，则重新评估。

---

## 5. 配置设计

沿用当前 `config.py` 模块级读取环境变量、范围检查和导入期 `ValueError` 的模式。

建议变量：

| 变量 | 建议默认值 | 说明 |
|---|---:|---|
| `STORYLINE_WEB_TOPIC_MC_LOGIN_GATE` | `true` | 登录预检总开关；`0/false/off` 关闭 |
| `STORYLINE_WEB_TOPIC_MC_LOGIN_GATE_TIMEOUT` | `750` | 每个平台预检/扫码流程的子进程超时（秒）；范围建议 `60..1500` |
| `STORYLINE_WEB_TOPIC_MC_LOGIN_GATE_TOTAL_TIMEOUT` | `3000` 或明确禁用 | 整条平台链登录预检的总预算；若不设置总预算，最坏等待时长为“平台数 × 单平台 timeout” |

超时建议：MediaCrawler 部分登录流程的重试窗口约为 600 秒，CDP/浏览器启动还可能消耗时间。660 秒只比 600 秒多 60 秒，给浏览器启动、首页跳转、Cookie 更新、最终 pong 和资源清理留下的余量偏少。建议先以 750 秒作为单平台默认，再通过真机计时校准。

注意：当前 MC 搜索另有 `STORYLINE_WEB_TOPIC_MC_TIMEOUT`（默认 600 秒）和 `STORYLINE_WEB_TOPIC_MC_CHAIN_TIMEOUT`（默认 600 秒）。它们是搜索子进程/搜索链预算，不能直接当作登录 gate 的单平台/总预算使用。应明确三个预算的层级关系，避免 gate 耗时与搜索耗时重复或互相覆盖。

当前配置示例（只适用于当前已准入的 xhs、dy）：

```dotenv
STORYLINE_WEB_TOPIC_ENGINE=mediacrawler
STORYLINE_WEB_TOPIC_MC_PLATFORM=xhs
STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=dy
STORYLINE_WEB_TOPIC_MC_LOGIN_GATE=true
STORYLINE_WEB_TOPIC_MC_LOGIN_GATE_TIMEOUT=750
```

**当前不要设置 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=dy,ks,bili`。** 当前白名单只允许 xhs、dy；要加入 ks、bili，应先完成独立平台准入并单独修订配置与测试。

这些配置在进程启动时读入模块常量，修改后需要重启实际运行工作流的进程。

---

## 6. 分阶段执行计划

### 阶段 0：冻结需求与准入边界

**目标：**先锁定本次 gate 的行为契约，避免编码过程中把四平台登录验证、生产链准入和登录状态混为一谈。

任务：

1. 固定 gate 仅检查 `platform_chain` 的定义。
2. 确认当前第一阶段只覆盖 xhs、dy；ks、bili 暂不进入生产链。
3. 固定三态登录检查结果、扫码触发条件和 gate fail-closed 行为。
4. 固定 cache hit 不预检、`LOGIN_GATE_FAILED` 不缓存的语义。
5. 冻结单平台超时、整链总预算、扫码事件协议。
6. 确认本机 MediaCrawler 工作目录和本地修改清单，备份未提交改动；不得在未备份时 pull 或覆盖本地文件。

验收：设计文档中的配置、平台链、错误码、缓存行为和测试断言完全一致。

### 阶段 1：实现 MediaCrawler 登录检查程序

**改动位置：**MediaCrawler 外部安装目录新增 `check_login.py`；如确有必要，再修改平台 client 增加诊断型 probe 方法。

任务：

1. 增加 CLI 参数和版本化 NDJSON 协议。
2. 在 CDP 初始化前正确设置 `config.PLATFORM` 和必要的登录/CDP/headless 配置。
3. 按生产 core 的顺序创建浏览器、page、client；逐平台实现 `pong()` 调用适配。
4. 补齐平台专有初始化（至少核实 KS 签名脚本、首页 URL、Cookie URL、CDP/profile）。
5. 把认证失败与探测异常分开，输出 `LOGGED_IN`、`NOT_LOGGED_IN`、`CHECK_ERROR`。
6. 明确未登录且 `interactive=True` 时复用生产 `XxxLogin.begin()`。
7. 登录后 `update_cookies()`，再次探测；捕获 `SystemExit` 并输出最终 JSON。
8. 完成浏览器/CDP 资源清理，确保超时杀进程树后 profile 不被孤儿 Chrome 占用。
9. 禁止输出 Cookie、Token、二维码 Base64 等敏感数据。

验收：使用 fake client / fake browser 的脚本级测试覆盖四平台参数适配；真实登录只在人工批准的冒烟阶段执行。

### 阶段 2：适配器实现 `verify_login()` 与流式子进程

**改动文件：**`storyline_capabilities/mediacrawler_client.py`

任务：

1. 增加 `verify_login()`，复用 `preflight()` 和 Python 解释器解析。
2. 增加 NDJSON 流式子进程读取；stdout 协议消息与 stderr 普通日志分流。
3. 处理 stdout/stderr 并行读取，避免 pipe deadlock。
4. 对最终 JSON 做严格结构校验（协议版本、平台、state、耗时类型）。
5. 正确归类 timeout、lock busy、OSError、SystemExit、缺少终态 JSON、解析失败。
6. 诊断信息脱敏并限长；不记录 Cookie 或二维码。
7. 设计 gate + search 的锁生命周期。优先由能力层持有同一把锁覆盖整个 gate/search；若保留逐调用锁，必须接受并记录 gate 与 search 之间的竞态窗口。

验收：`tests/unit/test_mediacrawler_client.py` 新增完整状态分类、NDJSON 事件、异常退出、锁释放、超时和敏感信息脱敏测试。

### 阶段 3：能力层插入登录闸门

**改动文件：**`storyline_capabilities/search_web_topic.py`

任务：

1. 解析 gate 配置和通知回调。
2. 在完整 `platform_chain` 构造后、搜索循环前执行逐平台检查。
3. 所有平台通过后进入原搜索链；任一失败直接生成 `LOGIN_GATE_FAILED`。
4. 断言 gate 失败路径零 `search_notes()` 调用、零 Bing 调用。
5. 将 `login_preflight` 诊断写入结果产物；确保成功路径也记录 `verdict=pass`。
6. 保障 `request_hash` 与当前有序 platform chain 保持一致。
7. 明确 gate 失败错误码是否排除缓存（建议由节点层按 error_code 排除）。

验收：能力层测试证明 gate 不改变 gate 通过时原来的 MC 搜索/回退行为；gate 失败不触发任何搜索和 Bing。

### 阶段 4：节点层接入配置、进度事件与缓存策略

**改动文件：**`config.py`、`nodes/storyline/node_search_web_topic.py`。

任务：

1. 新增并校验 gate 开关、单平台超时和整链总预算。
2. 解决 `config` 模块与 LangGraph `RunnableConfig` 参数名冲突（建议模块别名 `app_config`）。
3. 把配置和 `notify` 回调透传给能力层。
4. 接收子进程扫码事件并发射 `node.progress`。
5. 将 `LOGIN_GATE_FAILED` 映射为固定状态标签并写入 `error_log`。
6. 修改缓存读取规则：登录 gate 失败与检查错误产物不参与 cache reuse；其他历史缓存策略保持不变。
7. 对 cache hit 不执行 gate 的既定语义增加明确测试。

验收：节点级测试覆盖等待扫码事件、gate 失败标签、失败后重试不命中旧缓存、禁用 gate 零 verify_login 调用、cache hit 不起 MediaCrawler。

### 阶段 5：自动化测试与回归

**涉及测试文件：**

- `tests/unit/test_mediacrawler_client.py`
- `tests/unit/test_storyline_search_web_topic_mc.py`
- `tests/unit/test_config_mc_platform_chain.py`
- `tests/unit/nodes/test_node_search_web_topic.py`
- 必要时 `tests/integration/test_unreachable_nodes_wiring.py`

测试矩阵：

| 场景 | 预期结果 |
|---|---|
| 链内所有平台已登录 | gate pass；按原顺序执行 MC 搜索 |
| 主平台未登录、扫码后成功 | 显示二维码等待事件；更新 Cookie；复检通过；继续链 |
| fallback 平台未登录 | 即使主平台已登录，也按本需求执行 fallback 平台扫码/复检 |
| 扫码完成后 pong 仍未登录 | `LOGIN_GATE_FAILED`；零 search_notes；零 Bing |
| pong 网络异常/接口异常 | `CHECK_ERROR`；不误触发扫码；fail closed |
| 子进程 `SystemExit(0)` 且没有终态 JSON | `CHECK_ERROR`，不能判 PASS |
| 某平台 CDP profile 配错 | 测试必须失败，不得读取其他平台 profile |
| dy pong 调用 | 必须传 `browser_context` |
| KS 检查初始化 | 必须与生产一致地注入签名捕获脚本并打开正确首页 URL |
| 缺少最终 JSON / JSON 非法 | `CHECK_ERROR` |
| gate 超时 | 终止进程树，释放锁，返回失败诊断 |
| 锁被其他任务占用 | 明确忙碌，不能并发打开相同 profile |
| gate 失败后再次运行 | 不复用上一次 `LOGIN_GATE_FAILED`，重新执行 gate |
| gate 关闭 | 零 `verify_login()` 调用，原行为不变 |
| Bing 引擎 / Reach MCP 引擎 | 不调用 MediaCrawler 登录检查 |
| cache hit | 按既定语义不执行 gate |
| 强制刷新 | 绕过 cache，执行 gate |

回归命令（Windows 仓库根目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -x -q
```

附件记录的 `2904` 例零失败是较早基线，不能自动当作当前 `af96d004` 的测试结果。实施时应先在当前 HEAD 重新运行并记录实际收集数、通过数、失败数；新增测试合并后再次全量运行。

### 阶段 6：受控真机验收

**执行前提：**本阶段会启动浏览器、访问真实平台并可能弹出二维码。应由用户明确批准执行窗口；测试过程中不清除、不覆盖现有 profile，不改名模拟真实用户数据目录。

验收步骤：

1. 选择当前已准入平台链中的一个平台（建议先 xhs），以 `interactive=False` 做只探测检查；确认已有登录态返回 `LOGGED_IN`，不弹 QR。
2. 在可恢复的隔离测试 profile 中模拟未登录；确认 `interactive=True` 时显示二维码并发出 `login_waiting` 事件。
3. 用户扫码后验证 Cookie 更新、二次 pong 成功，检查程序返回 `LOGGED_IN`。
4. 在隔离 profile 中模拟扫码超时/取消；确认返回 gate 失败，且没有启动 MC 搜索与 Bing 回退。
5. 验证实际任务产物 `storyline/search_web_topic.json` 的 `diagnostics.login_preflight` 包含链、每个平台状态和耗时。
6. 对 dy 单独验证 `pong(browser_context=...)`、正确 profile 与扫码后 Cookie 更新。
7. 仅在 KS/Bili 完成平台准入后，才追加它们的真机 gate 验收。

**禁止用真实用户 profile 改名或删除来模拟未登录**，除非先做完整备份并获得用户明确批准。优先创建可丢弃的隔离 profile。

### 阶段 7：执行记录与交付

新增：

```text
docs/第10期开发/MC平台登录预检闸门_执行记录_20261121.md
```

记录：实现 commit、文件清单、配置默认值、状态机、真实验收平台及时间、扫码等待耗时、回归测试结果、已知限制和回滚方式。不要在执行记录中保存 Cookie、二维码截图或其他认证秘密。

---

## 7. 验收标准（DoD）

满足以下全部条件才可标记完成：

1. 只检查最终配置中的真实 platform chain；未准入平台不会被绕过白名单调用。
2. 每个平台使用正确的 `config.PLATFORM` 和对应 CDP profile。
3. `pong()` 调用参数和页面初始化与该平台生产 core 一致。
4. 已登录平台不弹二维码；明确未登录平台才进入交互登录。
5. 登录后刷新 client Cookie 并复检；复检通过才判登录成功。
6. `CHECK_ERROR` 不被误判为 `NOT_LOGGED_IN`，不会因网络异常直接弹扫码。
7. 子进程 `SystemExit(0)`、超时、缺 JSON、协议错都不能伪装成 PASS。
8. 扫码等待事件能够在真实等待过程中实时显示，而不是任务结束后补发。
9. gate 失败时零平台搜索调用、零 Bing 回退。
10. 登录 gate 失败产物不被下一轮缓存复用。
11. 关闭 gate 后原链路行为不变；bing/reach 等非 MC 引擎不受影响。
12. 当前完整单测通过；真机冒烟记录完整且无认证信息泄露。

---

## 8. 回滚方案

1. 设置 `STORYLINE_WEB_TOPIC_MC_LOGIN_GATE=0` 并重启实际工作流进程。
2. 确认节点不调用 `verify_login()`，原 MC 平台链与 Bing 回退行为保持不变。
3. 如果 MC 外部检查脚本导致问题，保留脚本但关闭 auto-video-editor gate；不需要删除现有浏览器 profile。
4. 若需撤销 MC 本地补丁，先依据本机修改清单逐项比较和备份，再人工恢复；不得直接 `git reset --hard` 或覆盖整个 MediaCrawler 目录。
5. 若 `ARTIFACT_SCHEMA_VERSION` 最终被提升，回滚时需考虑旧版本缓存 hash 与新版本不一致造成的重新检索，不应手工篡改已有产物 hash。

---

## 9. 附件原文问题清单（按优先级）

| 优先级 | 附件中的问题 | 本次核验结论 | 修订要求 |
|---|---|---|---|
| P0 | 认为设置 `dy,ks,bili` 后就会形成四平台实际链 | 当前白名单只有 xhs、dy；ks/bili 会导致配置导入失败 | 先执行平台准入；本期 gate 只查真实链 |
| P0 | 检查脚本复用 crawler 方法，但未明确设 `config.PLATFORM` | CDP profile 由该全局配置选择 | 每次检查必须先设置正确平台，再启动 CDP |
| P0 | 登录流程后直接“再 pong”，未明确刷新 client Cookie | client 创建时持有 Cookie 快照；生产 core 会 `update_cookies()` | 登录后先更新 Cookie，再最终 pong |
| P0 | 将 pong 双测 False 直接归为未登录 | xhs/ks/bili 捕获异常并返回 False；异常与认证失败混淆 | 实现 `LOGGED_IN/NOT_LOGGED_IN/CHECK_ERROR` 三态 |
| P0 | 节点签名新增 `config: RunnableConfig` | 当前模块 `import config`；同名参数会遮蔽模块 | 将模块导入改名为 `app_config` 或调整参数名 |
| P0 | gate 失败使用普通 failed 产物但未讨论缓存 | 当前缓存会复用 `failed` 状态；扫码失败后可能继续命中旧结果 | `LOGIN_GATE_FAILED` / `LOGIN_CHECK_ERROR` 必须排除缓存 |
| P1 | `notify` 声称能实时通知二维码等待 | 当前 `_run_subprocess()` 通过 `communicate()` 完成后才返回 | 增加 NDJSON 流式消息和父进程逐行转发 |
| P1 | 未处理 `XxxLogin.begin()` 的 `sys.exit()` | 上游部分 `sys.exit()` 默认退出码为 0 | 捕获 `SystemExit`；最终 JSON 缺失一律 CHECK_ERROR |
| P1 | 认为各平台 pong 可统一调用 | dy 的 `pong()` 需要 `browser_context` | 每个平台有显式适配器 |
| P1 | 没有写出各平台浏览器初始化差异 | KS 还需要注入签名捕获脚本；首页 URL 也有差异 | 与生产 core 一致地初始化，不可只 goto 通用首页 |
| P1 | 同一把锁被描述为预检与搜索“天然互斥” | 若 gate 子进程结束就释放锁，搜索前仍有交错窗口 | 明确锁作用域；优选覆盖 gate + search 整体 |
| P1 | 单平台超时 660 秒余量过小 | 登录重试可达约 600 秒，还要启动浏览器、刷新 Cookie、最终探测和清理 | 建议先取 750 秒并真机标定；另设整链总预算 |
| P1 | 文档说四平台已验证，容易被理解为四平台都已准入 | 单独登录成功不等同于生产平台链准入 | 分开记录“登录状态证据”和“平台准入状态” |
| P1 | 四平台全需扫码约 40 分钟 | 660 秒 × 4 = 2640 秒，即 44 分钟；750 秒 × 4 = 50 分钟 | 按真实 timeout 和链平台数计算，不用约数掩盖预算 |
| P2 | 要求 `ARTIFACT_SCHEMA_VERSION` v2→v3，却未说明影响范围 | 该版本进入全 provider request hash | 不 bump 或明确接受所有 provider 缓存失效 |
| P2 | 把 2904 例零失败当作当前基线 | 附件给的是较早提交时点 | 当前 HEAD 重新跑测试并记录实际计数 |
| P2 | 附件混入 Explore 子代理并发失败、记忆 ID、跨会话分工细节 | 这些不是实现规范，且对后续维护价值低 | 从正式设计文档移除，只保留代码事实、决策和可复核证据 |

---

## 10. 对附件正确性的最终评价

### 10.1 基本正确、可以保留的部分

- 登录预检应发生在 MC 平台链搜索前，而不是每个平台搜索失败后才补检查。
- 以生产 client 的登录探测接口为基础，避免另造一套与生产行为不一致的登录规则。
- 只检查实际平台链是合理范围；默认配置下应检查 xhs、dy。
- cache miss 才预检在性能与行为上合理，前提是明确 cache hit 不代表重新访问平台。
- gate 失败必须阻断 MC 平台链和 Bing 回退，防止登录问题被伪装成普通检索失败。
- 使用已有 CDP 锁、子进程进程树终止、诊断脱敏及节点状态标签，方向正确。
- 不改变 LangGraph 图拓扑时，通常不需要修改 workflow graph schema。

### 10.2 不能直接采信或仍需真机验证的部分

- 四个平台当日的扫码登录、免扫码复用、真实检索记录条数和耗时：这是附件提供的本机运行记录，本次无法从 GitHub 独立复现。
- MediaCrawler 本地五文件修改和另案新增的 xhs 补丁：本次无法看到本机工作树 diff。
- xhs pong 风控假阴性的具体复现频率和双测缓解效果：需要在用户本机按固定脚本记录多轮样本。
- 10–20 秒/平台的探活时间：受浏览器启动、代理、首页加载、平台 API 和机器性能影响，应以本机真机数据为准。
- 二维码是否会在目标桌面弹窗显示、LangGraph UI 是否能在等待过程中展示进度：需要端到端验收。

### 10.3 结论

附件的**业务目标和能力层插入位置基本正确**，但它把“pong 返回 False”“一定未登录”“可以安全触发扫码”视为同一件事，且遗漏 profile 选择、Cookie 刷新、实时事件传输和失败缓存排除等关键实现条件。

因此建议将本文作为实施基线，先完成阶段 0–2 的基础设施，再接阶段 3–4。不要直接按附件原 8 步开始批量修改，更不要为了实现“四平台检查”而提前把 ks、bili 加进当前生产白名单。

---

## 11. 关键源码链接

### auto-video-editor

- [main 分支当前提交 `af96d004`](https://github.com/wayyet/auto-video-editor/commit/af96d00433e141095269cb7d289c01a6978ced02)
- [config.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/config.py)
- [mediacrawler_client.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/storyline_capabilities/mediacrawler_client.py)
- [search_web_topic.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/storyline_capabilities/search_web_topic.py)
- [node_search_web_topic.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/nodes/storyline/node_search_web_topic.py)
- [test_storyline_search_web_topic_mc.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/tests/unit/test_storyline_search_web_topic_mc.py)
- [test_mediacrawler_client.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/tests/unit/test_mediacrawler_client.py)
- [workflow_events.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/monitoring/workflow_events.py)
- [node_05_generate_draft.py（custom stream 事件先例）](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/nodes/node_05_generate_draft.py)

### MediaCrawler 固定上游版本

- [main.py / CrawlerFactory](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/main.py)
- [CDP profile 选择](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/tools/cdp_browser.py)
- [xhs core 登录与 Cookie 更新](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/xhs/core.py)
- [dy core 登录与 Cookie 更新](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/douyin/core.py)
- [ks core 登录与页面初始化](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/kuaishou/core.py)
- [bili core 登录后二次 pong](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/bilibili/core.py)
- [xhs client pong](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/xhs/client.py)
- [dy client pong](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/douyin/client.py)
- [ks client pong](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/kuaishou/client.py)
- [bili client pong](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/bilibili/client.py)

---

**实施建议：先按本文阶段 0–2 修订并完成独立脚本/适配器测试，再接入能力层和节点层。当前平台白名单保持 xhs、dy，不因附件中的四平台登录记录而提前开放 ks、bili。**

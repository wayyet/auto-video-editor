# 网页话题检索失败原因分析与 Google 引擎切换计划

- **日期**:2026-11-19
- **触发**:「AI 内容明细」页(`/ai-content`)01 区块「网页话题检索」显示「执行失败」,用户要求分析红框原因
- **状态**:分析已定案;实施计划已定稿待执行
- **决策变更记录**:第一轮拍板为「换 Bing」;同日第二轮拍板改为「**换 Google,访问 Google 走本地 SOCKS5(127.0.0.1:1088)**」,Bing 方案作废(本文档已整体替换原 Bing 版)
- **关联任务**:console-b0b2a64a78c6(检索时间 2026-11-18T15:45:25+00:00)

---

## 一、问题现象

「AI 内容明细」页 01 区块显示:

| 字段 | 值 |
|---|---|
| 搜索引擎 | DuckDuckGo |
| 检索词 | 素材整体呈现了一段海滨宗教建筑群的游览记录。 |
| 检索时间 | 2026-11-18T15:45:25+00:00 |
| 说明 | DuckDuckGo page fetched but no result anchors parsed (provider layout change?). |

后续 02~05 区块(文案模板/生成文案/中英文字幕)全部「已完成」,流水线未中断。

任务产物原文(`outputs/console-b0b2a64a78c6/storyline/search_web_topic.json`,与截图逐字吻合):

```json
{"status": "failed", "query": "素材整体呈现了一段海滨宗教建筑群的游览记录。",
 "search_web_topic": [], "preview_urls": [], "error_code": "PARSE_ERROR",
 "message": "DuckDuckGo page fetched but no result anchors parsed (provider layout change?).",
 "query_hash": "3adf575a44851cd872a343956396a5b8af61599a341c9f67e0643f09eeb697e8",
 "searched_at": "2026-11-18T15:45:25+00:00"}
```

## 二、原因分析

### 2.1 一句话结论

**不是代码故障,是环境阻断**:DuckDuckGo 对本机出口环境长期不提供真实搜索结果页——走代理时被 DDG 反爬挑战、直连时被 GFW 干扰。HTTP 请求本身成功(否则记 NETWORK_ERROR),但页面里没有任何搜索结果锚点,代码按设计如实归类为 `PARSE_ERROR` 软失败,主流程照常继续。

### 2.2 证据链

**① 报错代码路径**(`storyline_capabilities/search_web_topic.py`):

- `84-89 行`:`requests.get("https://duckduckgo.com/html/", ...)` 请求成功、`raise_for_status()` 通过 → 记 PARSE_ERROR 而非 NETWORK_ERROR;
- `99 行`:解析靠纯字符串扫描找锚点 `class="result__a"`(锚点=HTML 里承载每条搜索结果的链接标签);
- `172-175 行`:整页零锚点、且页面不含「no results」标记 → 返回 `PARSE_ERROR`;
- `254-264 行`:PARSE_ERROR 映射为截图那句说明文案。

**② 关键:括号里的猜测「供应商改版?」是错的**。2026-11-10 本节点的真机验收记录(`docs/第三期开发/网页话题检索前置化_阶段7真机验收记录.md` 第 19-21 行)早已实测定性:

| 出口路径 | 实测结果 |
|---|---|
| 走代理(127.0.0.1:3067) | **DDG WAF 挑战页**(HTTP 202 + anomaly 页)。WAF=Web Application Firewall,反爬墙;首次请求曾返回真实结果锚点(10 条),随后出口 IP 被反爬标记;python requests / Windows curl / Git Bash curl 三种客户端全被挑战;40 分钟冷却探测(8 次)未恢复 |
| 直连 | **GFW 干扰变体**:页面可取但无结果锚点 → `PARSE_ERROR` 如实分类 |

即:DDG 在本机环境是**长期不可用**状态,不是 11-18 当天偶发。本次运行服务带代理 env 启动,请求走代理出口 → 拿到的是挑战页,无锚点可解析。

**③ 检索词来源属预期设计**:检索词 = `storyline_understanding_artifact.overall` 首句自动构造(`nodes/storyline/_common.py:459-508`),创建台未填手动检索词,截图中「素材整体呈现了一段海滨宗教建筑群的游览记录。」即画面概述首句原文。

**④ 软失败设计生效(fail-open),流水线无恙**:

- 节点每轮必跑,失败不抛异常、照常返回 → `graph.py:728` 双源屏障照样凑齐;
- 下游 `nodes/storyline/node_generate_script.py:48-58` 对失败产物软降级:`web_research=""`,文案照常生成(本次任务文案/字幕/配音全正常);
- 集成测试 `tests/integration/test_unreachable_nodes_wiring.py:1128-1152`(`test_web_topic_network_failure_is_soft_with_failed_artifact`)锁定了这条软失败路径;
- QA 回路对同 `query_hash` 直接复用 failed 产物(`node_search_web_topic.py:48-65`),不重复打已超时的外部搜索。

**⑤ 历史数据**:全仓 7 次真实运行产物(`outputs/*/storyline/search_web_topic.json`)**全部 `"status": "failed"`**——检索增强自上线起从未成功过。

### 2.3 影响面

- 主流程零影响:文案生成时少一份网络参考资料 + 明细页多一个红标,仅此而已;
- 附带发现(轻微措辞缺陷):「说明」栏 `(provider layout change?)` 是代码硬编码的猜测,与本例真因(反爬挑战/GFW 干扰)不符,有误导性;且生产路径不落盘原始响应,每次排查都需重新推导。

## 三、决策记录(2026-11-19 用户拍板)

| # | 决策项 | 结论 |
|---|---|---|
| 1 | 处理方向 | **换搜索引擎让它真正可用**(其余方向:仅归档分析/小改进/DDG 重试实验,均未采纳) |
| 2 | 搜索引擎 | 第一轮拍板 Bing;**第二轮拍板改为 Google,走本地 SOCKS5(127.0.0.1:1088)**,Bing 方案作废 |
| 3 | 平台资料通道 | 小红书/抖音走搜索引擎 `site:` 定向检索可行,**但留作二期**(本次只做全网检索) |
| 4 | DDG 重试/换端点 | 不做(2026-11-10 实测三种客户端全被挑战,成功率预期极低) |

**平台直连抓取不可行的约束记录**(二期适用):小红书网页版未登录请求被拦(461 反爬码,接口需 x-s/x-t 签名);抖音搜索接口需 a_bogus 签名。流水线无人值守场景下直连抓取需登录态+签名逆向,易碎且有账号风控风险——二期采用 `site:xiaohongshu.com` / `site:douyin.com` 定向检索通道。

## 四、实施计划:默认引擎切 Google + SOCKS5 本地代理 + 诊断增强

**核心思路**:能力层已有 provider 分发位,新增 Google HTML 抓取后端,默认引擎从 `duckduckgo_html` 切到 `google`,Google 请求强制走本地 SOCKS5;DDG 代码与测试保留(env 可随时回滚)。同时把失败说明改准确、失败时落盘原始响应,方便诊断 Google 风控。

### 4.0 依赖(前置)

- venv 现有 `requests==2.34.2` 但**缺 PySocks**(已核实 `site-packages` 无 socks 包)——`requests` 走 SOCKS5 必须补装:`.venv\Scripts\python.exe -m pip install pysocks`,并写入 requirements 文件(实施时核对仓内实际文件名);
- 运行时若 PySocks 缺失,`requests` 抛 `InvalidSchema`(`RequestException` 子类),会被现有 92-93 行异常映射兜成 `NETWORK_ERROR`,软失败语义不破。

### 4.1 `storyline_capabilities/search_web_topic.py` — 新增 Google 后端

- 新增 `_google_html_search()`:`GET https://www.google.com/search?q={query}&num=10&hl=zh-CN`,timeout 15s,UA 沿用 `DEFAULT_USER_AGENT`(29-33 行)+ `Accept-Language: zh-CN`;
- **代理**:`proxies={"http": proxy_url, "https": proxy_url}` 显式逐请求传入,**scheme 用 `socks5h://`**(h 后缀=域名解析也走代理,避免本地 DNS 污染把 google.com 解析到假 IP),不依赖全局环境变量;
- 解析:纯字符串扫描(与现有 DDG 解析器同风格)——`<div class="g">` 结果块、`<h3>` 标题、摘要类名带回退列表;链接同时处理直连 `https://` 与 Google 包装跳转 `/url?q=URL&sa=...` 的还原;
- 零结果分类:含「未找到结果 / did not match any documents」标记 → `NO_RESULTS`;含反爬/人机验证特征(`unusual traffic` / `异常流量` / `g-recaptcha` / `consent.google.com`)→ `PARSE_ERROR`,报错文案明确指向「代理出口被 Google 风控或需人机验证,见调试文件」;
- **失败诊断落盘**:解析失败时把响应状态码 + 最终 URL + HTML 前 64KB 写到任务目录 `storyline/search_web_topic_debug.html`(仅失败时写,每次覆盖);
- 分发区(204-218 行)接受 `provider="google"`(签名加 `proxy_url: Optional[str] = None` 参数),条目 `source="google_html"`;`UNKNOWN_PROVIDER` 守卫保留;能力层**默认 provider 保持 `duckduckgo_html`**,存量 DDG 测试不动,生产由节点显式传 `google`。

### 4.2 `config.py`(~52 行 STORYLINE_ENABLE_WEB_TOPIC 附近)— 两个新开关

- `STORYLINE_WEB_TOPIC_ENGINE`:默认 `"google"`,可选 `"duckduckgo_html"` / `"stub"`(运行时读,改后重启生效);
- `STORYLINE_WEB_TOPIC_SOCKS5_PROXY`:默认 `"socks5h://127.0.0.1:1088"`(改代理地址只动 env,零代码)。

### 4.3 `nodes/storyline/node_search_web_topic.py`(110-114 行)— 引擎与代理透传

- `provider=` / `proxy_url=` 从 config 读入并透传,替换硬编码 `"duckduckgo_html"`;
- 产物契约新增 `engine` 字段(additive);
- 软失败语义、failed 缓存复用逻辑(48-107 行)不动。

### 4.4 UI 两处小改

- `web/console_service.py:1034`:`engine` 改为从产物 `doc.get("engine")` 读取,缺省兜底 `"Google"`;
- `web/ai_content.html:241/246`:「检索自 DuckDuckGo」硬编码改为跟随引擎显示;前端纯逻辑测试涉及文案同步更新。

### 4.5 测试

- `tests/unit/test_storyline_capabilities_search_web_topic.py`:新增 Google 用例(真形 HTML fixture:正常解析含 `/url?q=` 还原与直连混合 / 无结果标记→NO_RESULTS / 人机验证页→PARSE_ERROR / 网络异常→NETWORK_ERROR / 缺 PySocks→NETWORK_ERROR / 代理参数确实传入);
- `tests/unit/nodes/test_node_search_web_topic.py`:引擎与代理透传断言(默认 `google` + `socks5h://127.0.0.1:1088`、env 覆盖生效);
- 集成测试桩(`test_unreachable_nodes_wiring.py:685/722` monkeypatch `cap_topic`)签名兼容。

### 4.6 文档与记忆

- 本文档即权威计划;实施完成后追加执行记录章节(含真连探测结果);
- 同步更新会话记忆(引擎决策 Google+SOCKS5 1088)。

### 4.7 复用与不变式(全部不动)

- 查询词构造(`nodes/storyline/_common.py:459-508` 概述首句);
- 软失败链路(`graph.py:728` 双源屏障、`node_generate_script.py:48-58` 降级);
- 摘要护栏(单条 600 字 / 总 4000 字,`search_web_topic.py:39-40`);
- QA 回路 failed 缓存复用(`node_search_web_topic.py:48-107`)。

## 五、验证

1. **依赖**:确认 `pysocks` 已装入 venv 且 requirements 更新;
2. **单测**:`.venv\Scripts\python.exe -m pytest` 先跑两个目标测试文件,再全量 unit(基线 ~2427)零新增失败;
3. **真连探测**(实施期):一次性脚本经 `socks5h://127.0.0.1:1088` 打 Google,中文 query 验证锚点解析,结果记入本文档执行记录;
4. **重启服务**(restart-video-editor 技能,管理员 pwsh7)加载新代码;
5. **真机 E2E**:新建创建台任务 → `outputs/<task>/storyline/search_web_topic.json` 应为 `status=ok` 且有条目;`/ai-content` 页 01 区块变绿「已完成」、显示「检索自 Google」与来源条目(标题+URL);
6. **回滚预案**:`STORYLINE_WEB_TOPIC_ENGINE=duckduckgo_html` env 即回退,零代码改动。

## 六、二期接入点(小红书/抖音,本次不做)

provider 分发加 `platforms` 参数即可扩展:对开启的平台追加一轮 `site:` 定向查询(`site:xiaohongshu.com <query>`、`site:douyin.com <query>`,走同一 Google+SOCKS5 通道),条目 `source` 带平台标记,合并进同一产物 `search_web_topic` 列表;每平台条目数设上限(建议 5 条),总上下文仍受 4000 字护栏约束。免登录、免签名,反爬面与普通 Google 搜索一致;局限=只能搜到被 Google 收录的内容,无点赞数等站内数据。

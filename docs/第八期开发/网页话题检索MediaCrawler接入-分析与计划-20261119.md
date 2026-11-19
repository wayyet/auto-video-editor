# 网页话题检索切换 MediaCrawler 接入 —— 分析与计划(2026-11-19)

> 状态:**实施中途暂停**。本文档承载完整调研分析、实施方案与当前断点,任何会话可据此无损接续。接续点见 §6。

---

## 1. 背景与目标

节点「01 · 网页话题检索」(storyline_search_web_topic)当前用 HTML 解析方式抓通用搜索引擎结果页(默认 `bing_html`,2026-11-19 早间定稿;历史引擎 google_html/duckduckgo_html 代码保留)。Bing 通用网页结果对旅游/探店类选题的信息密度不足,且各引擎 WAF/JS 挑战是长期隐患。

**目标**:把该节点切换为调用本地安装的开源项目 **MediaCrawler**(https://github.com/NanmiCoder/MediaCrawler,基于 Playwright 的小红书/抖音/快手/B站/微博/贴吧/知乎爬虫)检索社交平台真实笔记,作为文案选题参考;失败自动回退 Bing。

- 本地安装路径:`C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\MediaCrawler`
- 参考截图:创建台「AI 内容明细」页 01 节点(搜索引擎: Bing / 来源网站:10 条,检索来自 Bing)

## 2. 用户拍板决策(2026-11-19,AskUserQuestion 记录)

| # | 决策项 | 拍板结果 |
|---|--------|----------|
| 1 | 默认检索平台 | **小红书 xhs**(env 可切 7 平台:xhs/dy/ks/bili/wb/tieba/zhihu) |
| 2 | 浏览器运行方式 | **可见浏览器**(非无头;CDP 连本地 Chrome 风控最低;首次使用操作员扫码登录一次,登录态缓存 `<platform>_user_data_dir` 复用) |
| 3 | 失败兜底 | **MC 失败或 0 条 → 自动回退 bing_html**,artifact 记录回退来源(fallback 键) |

## 3. MediaCrawler 侧探查结论(本地副本实读)

### 3.1 基本信息
- 本地路径:`C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\MediaCrawler`
- 自带虚拟环境:`.venv`(uv 管理,`.python-version` = **3.11**;Windows python 在 `.venv\Scripts\python.exe`)
- 许可:**NON-COMMERCIAL LEARNING LICENSE 1.1**(非商业学习用途;禁大规模爬取)。本项目每任务 1 次、≤10 条属小规模内部使用;**代码不入库,仅子进程调用外部安装副本,不对外分发**。

### 3.2 CLI 调用形态(关键发现:支持运行时动态传参,无需改配置文件)
`cmd_arg/arg.py` L154-443(typer):CLI 参数覆盖全局 config(`config.KEYWORDS = keywords` 等逐项回写):

```
<project_root>\.venv\Scripts\python.exe main.py \
    --platform xhs --lt qrcode --type search --keywords <检索词> \
    --crawler_max_notes_count 10 --get_comment false --get_sub_comment false \
    --get_media false --headless 0 --save_data_option json \
    --save_data_path <每次运行独立目录>
```

- 平台枚举 L40-49:`xhs / dy / ks / bili / wb / tieba / zhihu`;登录枚举 L52-57:`qrcode / phone / cookie`
- `--keywords`(L193-200,逗号分隔多个)、`--crawler_max_notes_count`(L287-294)、`--save_data_path`(L303-310)、`--headless`(L228-236)、`--get_comment/--get_sub_comment/--get_media`(str2bool)
- config 默认值(`config/base_config.py`):`PLATFORM="xhs"` L21、`LOGIN_TYPE="qrcode"` L28、`HEADLESS=False` L50、`SAVE_DATA_OPTION="jsonl"` L90、`START_PAGE=1` L99、`CRAWLER_MAX_NOTES_COUNT=15` L102、`ENABLE_GET_MEDIA=False` L111、`ENABLE_GET_COMMENTS=True` L119(故必须显式 `--get_comment false`)

### 3.3 数据产出格式
`tools/async_file_writer.py` L37-45:`base_path = f"{SAVE_DATA_PATH}/{platform}/{file_type}"`,文件名 `{crawler_type}_{item_type}_{date}.{file_type}` →

```
<save_data_path>/<platform>/json/search_contents_<YYYY-MM-DD>.json
```

- json 模式为 JSON 数组(`write_single_item_to_json` 读旧文件追加后整写)
- 搜索记录字段(`store/xhs/_store_impl.py` L141-158):`nickname / title / desc / note_url / source_keyword`;xhs 记录另带 `xsec_token`(`model/m_xiaohongshu.py` L28-30,note_url 打开需带 token,存原样即可)
- 与现有条目契约 `{url,title,snippet,source}` 的映射:`note_url→url、title→title、desc→snippet、source 恒 "mediacrawler"`

### 3.4 登录与反爬
- `USER_DATA_DIR = "%s_user_data_dir"`(base_config L96)→ 登录态缓存在项目根 `<platform>_user_data_dir`;qrcode 登录类型在已有登录态时**不重复扫码**
- CDP 模式默认开:`ENABLE_CDP_MODE = True` L59、`CDP_DEBUG_PORT = 9222` L63、`CDP_HEADLESS = False` L73、`CDP_CONNECT_EXISTING = True` L83(连接/启动用户本地 Chrome,反检测更好;可见窗口)
- 签名:**无需 JS 逆向** —— 用登录态浏览器上下文内 JS 表达式生成签名(README L48-50);7 平台关键词搜索+登录态缓存全 ✅(README L54-62)
- `data/` 目录当前不存在 = 本地从未跑过,**首次运行必须扫码登录**

### 3.5 集成方式选型
自带 `api/`(FastAPI,`api/main.py` L40-66:routers crawler/data/websocket),但 `api/services/crawler_manager.py` `_build_command` L205-216 实质是**拼 main.py CLI 起子进程**再转发日志 —— 浏览器并不常驻热复用。因此**直接子进程调 main.py 与 API 服务等价且省一个常驻服务**,选子进程。

## 4. auto-video-editor 侧探查结论(接入点全定位)

### 4.1 能力层 `storyline_capabilities/search_web_topic.py`(1150 行)
- **三个注册 dict(行号实测)**:`_PARSER_VERSIONS` L69-74、`_PROVIDER_LOCALES` L75-80、`_PROVIDER_ENGINE_LABELS` L81-86;`ARTIFACT_SCHEMA_VERSION = 2` L63
- **主入口**:`def search_web_topic(` 实测在 **L949**(签名含 `media_dir` 参数 L953,新增参数时不可丢);分发 if-chain:`stub` L1058、`google_html` L1070、`bing_html` L1138、未知 provider 检查 L1204、`duckduckgo_html` 尾随 L1211 —— **mc 分支插在 bing 分支 return 之后、`if provider != "duckduckgo_html"` 之前**
- `_result` 内部闭包 L1021-1056:统一组装 artifact dict(status/query/schema_version/engine/provider/search_web_topic/preview_urls/error_code/message/query_hash/request_hash/searched_at/diagnostics);`request_hash`/`engine` 是**分发前捕获值**(L1016-1019),mc 分支需在返回 doc 后 mutate 覆盖
- `build_request_hash` L168-185:sha256 of `"\x1f".join(["v2", provider, query, str(max_results), locale, parser_version, str(schema_version)])`,dict 查找用 `.get(provider, "")` / `.get(provider, "unknown")` → **动态 provider 键(如 `mediacrawler:xhs`)安全**
- 结果条目 4 键:`{url, title, snippet, source}`;title 300 字、snippet 上限 `WEB_TOPIC_SNIPPET_MAX_CHARS=600`(L49);清洗函数 `_clean_web_text` L159-167(压单行/去控制字符/截断,能力层自持,不得反向 import nodes)
- `_bing_html_search(*, query, max_results, proxy_url, debug_dir) -> (items, error_code, diagnostics)`(回退直接复用)
- 错误码域(docstring):`NO_RESULTS / NETWORK_ERROR / HTTP_ERROR / PROXY_ERROR / PROVIDER_BLOCKED / PARSE_ERROR / UNKNOWN_PROVIDER`;NO_RESULTS → status `no_results` 软态,其余 → `failed`
- `_write_debug_dump` L194+:仅显式 debug_dir 才落受限 .txt(≤64KiB 原子替换)

### 4.2 节点层 `nodes/storyline/node_search_web_topic.py`(177 行)
- **L117:`request_hash = build_request_hash(query, provider=engine, max_results=10)` 在调能力层之前计算**,并用于缓存命中判断(L124 `_load_reusable_cache`);**能力层 doc 内的 request_hash 若与之不一致,重试永远 miss** → mc 平台隔离必须**两侧同步**用 `mediacrawler:{platform}` 形式(节点层算缓存键,能力层 mutate doc)
- L158 `result.setdefault("request_hash", request_hash)`:能力层 doc 已带时节点不覆盖 → 能力层 mutate 生效
- 缓存:`_load_reusable_cache` L56-82(要求 query_hash+request_hash+status∈{ok,no_results,failed,cache_hit}+条目是 list 全匹配);`web_topic_force_refresh` 显式绕过
- 失败=软失败:能力层异常 → `TOOL_EXECUTION_FAILED` 入 error_log 不写产物(L164-180);error_code 映射标签 L39-47(NO_RESULTS→`..._no_results`,其余→`..._failed`)
- 产物路径:`outputs_root/storyline/search_web_topic.json`(L96)

### 4.3 下游消费(新引擎零改动即可被消费)
- `nodes/storyline/_common.py`:`format_web_topic_reference` L367-407(读 title/url/snippet,总长 4000 截断)、`extract_web_topic_references` L410-442(读 title/url/source,**跳过非 http(s) URL**)→ source 值会进 script.json references
- prompt 注入:`storyline_capabilities/generate_script.py` L44/L93;模板 `prompts/tasks/generate_script/zh/user.md` L9 `{{web_research}}`

### 4.4 前端显示(engine 字段权威,前端 HTML 无需改)
- `web/console_service.py`:`_web_topic_engine_label` L1053-1075(优先 artifact `engine` 字段 → provider/条目 source 关键词匹配 `_WEB_TOPIC_ENGINE_KEYWORDS` **L1046** → 兜底"未知引擎");`_normalize_topic_entries` L983-1000
- `web/ai_content.html` webTopic 渲染器 L258-283:`kvRow("搜索引擎", sec.engine)`、`来源网站(N 条,检索自 {engine})`

### 4.5 子进程纪律先例
`assembly_capabilities/ffproc.py` `run_proc` L20-44:stdin=DEVNULL 默认、timeout 默认 3600、文本模式强制 `encoding="utf-8", errors="replace"`(Windows 父进程默认 ANSI/GBK 会打死中文输出)。**注意**:MC 不可加 `-I`/`-E` 隔离参数(main.py `import config` 依赖脚本目录导入)。

## 5. 实施方案(6 步)

### 步骤 1:config.py(✅ 已完成,见 §6)
白名单加 `mediacrawler`、默认引擎改 `mediacrawler`、新增 5 个 MC env(含平台白名单导入期校验)。env 清单:

| env | 默认值 | 说明 |
|-----|--------|------|
| `STORYLINE_WEB_TOPIC_ENGINE` | `mediacrawler` | 引擎总开关(白名单:mediacrawler/bing_html/google_html/duckduckgo_html/stub) |
| `STORYLINE_WEB_TOPIC_MC_PLATFORM` | `xhs` | MC 平台(白名单:xhs/dy/ks/bili/wb/tieba/zhihu) |
| `STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT` | `C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\MediaCrawler` | MC 安装根 |
| `STORYLINE_WEB_TOPIC_MC_MAX_NOTES` | `10` | 每次检索最大条数 |
| `STORYLINE_WEB_TOPIC_MC_TIMEOUT` | `600` | 子进程超时秒数(涵盖首次扫码人工等待) |
| `STORYLINE_WEB_TOPIC_MC_HEADLESS` | `0` | 0=可见浏览器(拍板#2) |

### 步骤 2:新建 `storyline_capabilities/mediacrawler_client.py`(✅ 已完成,见 §6)
子进程适配器,契约:

```
search_notes(*, query, max_results, platform, project_root, save_root,
             timeout_seconds, headless=False)
    -> (records, error_code, message, diagnostics)
```

- records = MC JSON 结果文件的**原始记录列表**(映射成四键条目由能力层做,本模块不定义条目契约)
- 错误码复用既有域:venv/main.py 缺失、启动 OSError、超时、非零退出 → `NETWORK_ERROR`;结果文件不可读 → `PARSE_ERROR`;正常退出无文件/空列表 → `NO_RESULTS`
- diagnostics:returncode、stdout/stderr 尾部(≤2KiB)、run_dir、output_file、parser_version=`mc_{platform}_v1`
- 内含 `MC_PLATFORM_LABELS`(xhs→小红书…)与 `platform_label()`;平台白名单 `MC_PLATFORM_CODES`(与 config._MC_PLATFORM_WHITELIST 双处维护,不 import config)
- 每次运行独立时间戳子目录 `<save_root>/<ts>/` 作 --save_data_path,防历史残留

### 步骤 3:能力层 `search_web_topic.py`(⬜ 未动,接续点)
1. 三个 dict 加 `"mediacrawler"` 条目(parser_version 基础值 `mediacrawler_v1`、locale `""`、label `"MediaCrawler"`)
2. 模块顶部 `from storyline_capabilities import mediacrawler_client as _mc_client`(无环:client 不反向 import)
3. 签名加 `provider_options: Optional[dict[str, Any]] = None`(L949 处,**保留 media_dir**);docstring provider 清单同步
4. 新增模块级 helper(节点层同用,保证两侧 hash 一致):

```python
def mediacrawler_request_provider(platform: str) -> str:
    """mediacrawler 的 request_hash provider 串:平台编入,缓存隔离。"""
    return f"mediacrawler:{platform}"
```

   并让 `build_request_hash` 对 `mediacrawler:*` 键解析 parser_version=`mc_{platform}_v1`、locale=""(或两侧统一依赖 `.get` 的 "unknown" 一致性——前者更明确,推荐前者)
5. 新增 `_map_mc_records(records, max_results)`:字段映射(note_url/url、title、desc/snippet、source 恒 `"mediacrawler"`,回退候选键 url=("note_url","url")、snippet=("desc","content")),过 `_clean_web_text`(title 300/snippet 600)
6. mc 分支(bing 分支后):
   - 从 provider_options 取 platform/project_root/save_root/timeout_seconds/headless(缺失 project_root/save_root 直接 raise ValueError → 节点层 TOOL_EXECUTION_FAILED,属编程错误)
   - `effective_request_hash = build_request_hash(query, provider=mediacrawler_request_provider(platform), max_results=max_results)`
   - 调 `_mc_client.search_notes`;**成功(≥1 条)**:items=映射结果,preview_urls=[url...],`doc = _result(...)` 后 `doc["request_hash"]=effective_request_hash`、`doc["engine"]=f"MediaCrawler({platform_label(platform)})"` 返回
   - **失败或 0 条 → 回退**:调 `_bing_html_search(query, max_results, proxy_url, debug_dir)`(复用 SOCKS5 代理参数);bing 失败→返回 bing 的 error_code/message,diagnostics 合并 mc 信息;bing 成功→`doc["engine"]="Bing"`、`doc["fallback"]={from_provider, from_platform, engine, error_code, message}`、message 写「MediaCrawler(小红书)失败已回退 Bing」;`doc["request_hash"]` 全路径统一 mutate
   - 回退不跳 HTML 下载逻辑(download 在生产为 False,注释说明即可)

### 步骤 4:节点层 `node_search_web_topic.py`(⬜ 未动)
engine == "mediacrawler" 时(插在 L114-117 区域):
- 组装 `provider_options = {platform, project_root, timeout_seconds, headless, max_notes, save_root: str(out_dir / "web_topic_mc")}`(均来自 config.STORYLINE_WEB_TOPIC_MC_*)
- `hash_provider = mediacrawler_request_provider(config.STORYLINE_WEB_TOPIC_MC_PLATFORM)`(从能力层 import helper)、`hash_max_results = config.STORYLINE_WEB_TOPIC_MC_MAX_NOTES`
- `request_hash = build_request_hash(query, provider=hash_provider, max_results=hash_max_results)`;`cap_topic(..., max_results=hash_max_results, provider_options=provider_options)`
- 其余引擎分支行为逐字不变(max_results=10)

### 步骤 5:`web/console_service.py`(⬜ 未动)
`_WEB_TOPIC_ENGINE_KEYWORDS`(实测 L1046)加 `("mediacrawler", "MediaCrawler")`(历史产物兜底;新产物 engine 字段权威)。

### 步骤 6:测试(⬜ 未写)+ 文档 + 提交
- `tests/unit/test_storyline_capabilities_search_web_topic.py` 追加(注入式 fake 套路:client 层 monkeypatch `subprocess.run` + 临时目录落 JSON 桩;能力层 monkeypatch `_mc_client.search_notes`):
  - client:字段映射/utf-8、超时→NETWORK_ERROR、非零退出→NETWORK_ERROR、输出缺失→NO_RESULTS、JSON 损坏→PARSE_ERROR、dict 包装容错
  - 能力层:mc 成功(source=mediacrawler、engine=MediaCrawler(小红书))、失败回退 bing(fallback 键、source=bing_html、engine=Bing、message)、0 条回退、双失败(error_code=bing 的)、`mediacrawler:xhs` 与 `mediacrawler:wb` request_hash 不同、节点缓存键与 doc hash 一致性
  - config:白名单含 mediacrawler、默认值断言
- 既有 651 行用例与 GOLDEN_STEPS(`tests/integration/test_unreachable_nodes_wiring.py` L97-118,web_topic 在 `_CYCLE` 内)零改动(节点接线不变、SCHEMA_VERSION 不 bump)
- 执行记录补充到本文件(实施完后改为「已完成」口径)

## 6. 当前实施进度与断点(2026-11-19 暂停时点)

| 项 | 状态 | 说明 |
|----|------|------|
| config.py | ✅ 已改 | 白名单加 mediacrawler;默认引擎 mediacrawler;新增 `_MC_PLATFORM_WHITELIST` 校验 + `STORYLINE_WEB_TOPIC_MC_PLATFORM/PROJECT_ROOT/MAX_NOTES/TIMEOUT/HEADLESS` 五 env(§5 步骤 1 表);SOCKS5/DEBUG 原样保留 |
| storyline_capabilities/mediacrawler_client.py | ✅ 已新建 | 契约见 §5 步骤 2;含 MC_PLATFORM_LABELS/MC_PLATFORM_CODES/platform_label/_resolve_python_exe/_tail;命令形态与错误码映射已按 §3.2/§4.5 纪律落实现 |
| storyline_capabilities/search_web_topic.py | ⬜ 未动 | **接续点**。改动清单=§5 步骤 3 全部(三 dict/helper/build_request_hash 平台感知/_map_mc_records/mc 分支+回退/签名 provider_options);实测行号:主入口 L949、_result 闭包 L1021-1056、bing 分支 L1138 起、未知 provider L1204 |
| nodes/storyline/node_search_web_topic.py | ⬜ 未动 | 改动清单=§5 步骤 4 |
| web/console_service.py | ⬜ 未动 | 一行关键词兜底(§5 步骤 5) |
| 测试 | ⬜ 未写 | §5 步骤 6 |
| 验证 | ⬜ 未跑 | 见 §7 |
| git 提交 | ⬜ 未做 | 本文档 + 两文件改动待一并提交 |

**接续点**:从 §5 步骤 3(能力层分支)开始,按 3→4→5→6 顺序;接续会话无需重新调研,§3/§4 结论已实测校准(注意:早期探索报告给的行号有漂移——def search_web_topic 实际 L949 而非 L851,以本文档实测值为准)。

## 7. 验证方案(实施完成后)

1. **单测**:`.venv\Scripts\python.exe -m pytest tests/unit/test_storyline_capabilities_search_web_topic.py tests/unit/test_web_topic_reference.py -q`;随后全量 unit,新增失败用 stash/worktree 对照区分存量
2. **真机**:重启生产服务(config 导入期读 env,须管理员 pwsh7 一键脚本)→ 新建任务 → 首跑弹出 Chrome **操作员扫码登录小红书一次** → 「AI 内容明细」01 节点应显示引擎 `MediaCrawler(小红书)`、来源网站 10 条为小红书笔记链接,03 生成文案引用来源正常
3. **回退验证**:临时 `STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT` 指向不存在路径重启 → artifact 自动回退 Bing,含 `fallback` 键与 message
4. **回滚**:`STORYLINE_WEB_TOPIC_ENGINE=bing_html` 重启即恢复原行为(MC env 全部闲置)

## 8. 风险与注记

- **首次登录依赖人工扫码**:qrcode 首扫需操作员;登录态过期 → 节点软失败 → 自动回退 Bing,流水线不阻断
- **OneDrive 同步干扰**:MC 的 `<platform>_user_data_dir`(浏览器 profile)在 OneDrive 目录内,Chromium 高频写文件可能与同步互相干扰(文件锁/流量);先观察,出问题再改 MC `config/base_config.py` `USER_DATA_DIR` 移出 OneDrive
- **风控**:每任务 1 次、≤10 条、可见浏览器+登录态复用,符合 MC README 频控建议;个人账号高频使用需克制
- **许可**:非商业学习许可;仅子进程调用外部安装副本、代码不入库、不对外分发
- **并发**:两任务同时触发 MC 会争用 CDP 端口 9222 与浏览器实例;单操作员场景风险低,如出现再加文件锁
- **代理语义**:MC 分支不走 SOCKS5(社交平台直连);回退 bing 分支沿用 `STORYLINE_WEB_TOPIC_SOCKS5_PROXY` 现行为

# MediaCrawler 真机验收与默认切换 —— 执行记录(2026-11-20)

## 背景

0e03f82(mc 全接线)+ 6e724cb(相关性守卫覆盖 mc 分支)交付时,默认引擎按审查 6.4 门禁
保持 `bing_html`,默认切换留作单独提交,前提=操作员真机验收通过。本记录=验收执行与
默认切换的完整证据链。

## 一、验收环境

| 项 | 值 |
|---|---|
| MC 本地副本 revision | `09ed1ec62d885dab…`(与方案 B 固定值一致,`git rev-parse` 实测) |
| 本地副本改动 | 仅 `config/base_config.py`(M),`:86` = `CDP_CONNECT_EXISTING = False` ✓ |
| 启用方式 | User 级环境变量 `STORYLINE_WEB_TOPIC_ENGINE=mediacrawler`(`setx`,回读确认) |
| 服务重启 | `start_20261120_020717.log`:致命 0 / 已知告警 0 / monitor 200 / edb-postgres16 healthy |
| 扫码登录 | **无需首扫**:专用 profile(`browser_data/cdp_xhs_user_data_dir`)已有登录态,流水线首跑直接成功 |

## 二、流水线真机首跑(审查 §6.3 场景 1)

video-001 冒烟线程自动运行,`storyline_search_web_topic` 节点 MC 分支真实执行:

- `outputs/video-001/storyline/search_web_topic.json`:
  `status=ok`、`provider=mediacrawler`、`requested_provider=mediacrawler`、
  `engine=MediaCrawler(小红书)`、`fallback`/`fallback_error` 为空、条目 10 条(=MAX_NOTES)
- `diagnostics.primary`:`returncode=0`、`run_id=20261120_020906_582391`、`cdp_port=9222`、
  `headless=false`、`timeout_seconds=600`、`output_file=xhs/json/search_contents_2026-11-20.json`、
  `raw_dir_deleted=true`(原始 JSON 成功即删)、MC 日志尾部 "Xhs Crawler finished"
- 相关性守卫:`relevance_matched=3 / relevance_total=10` ≥ 阈值 2 → 不触发回退 ✓
- 条目 URL 为 `https://www.xiaohongshu.com/explore/…` 真实笔记链接 ✓
- UI 链:`_web_topic_engine_label(产物)` = `MediaCrawler(小红书)`(明细页唯一标签来源,
  另有 tests/unit/test_console_content_details.py:289 显示断言)

## 三、八场景矩阵(审查 §6.3)

驱动脚本:驱动能力层 `search_web_topic` 直跑(真 MC 子进程/真 Bing HTTP),场景与结果:

| # | 场景 | 实测 | 结果 |
|---|---|---|---|
| 1 | MC 成功 | 流水线首跑 + 驱动复跑(残留 Edge 下,128s,10 条) | ✅ |
| 2 | MC 未安装 | 坏 `PROJECT_ROOT` → 预检失败(错误可读)→ 回退 Bing 成功,`fallback` 完整,engine=`Bing(MediaCrawler 回退)` | ✅ |
| 3 | CDP 未开启 | 方案 B 为 MC 自启浏览器,"CDP 不可用"映射为启动失败 → 与场景 2 同一失败→回退代码路径;场景 1 另实测"已有实例"形态 | ✅(同路径覆盖) |
| 4 | MC 无结果/低相关 | 乱码词爬回 10 条笔记但仅 1/10 相关 → `IRRELEVANT_RESULTS` → 不算成功 → 回退;Bing 亦 0/9 相关 → 双失败软降级 | ✅(守卫双引擎生效) |
| 5 | 双失败 | `status=failed` 软降级,MC 主错误(`error_code`)与 Bing 错误(`fallback_error`)**两端都保留** | ✅ |
| 6 | 缓存 | 节点层同 `request_hash` 命中即 `cache_hit` 不重复检索;单测 12 例含 MC 专属 `test_node_mc_cache_hit_when_everything_unchanged` | ✅(单测覆盖) |
| 7 | 平台切换 | N/A:平台白名单仅 `xhs`(config.py `_MC_PLATFORM_WHITELIST`);hash 隔离由 `test_node_mc_cache_miss_when_revision_changes` / `test_node_cache_miss_when_provider_changes` 覆盖 | ✅(机制覆盖) |
| 8 | 并发 | CDP 文件锁被占 → 明确忙碌信息(`MediaCrawler busy: … port 9222`)→ 回退 Bing,不并发控制同一 context | ✅ |

## 四、运维发现(非阻塞,记录在案)

1. **专用 Edge 成功后不关闭,但不阻塞下一次运行**:成功爬取后浏览器主实例仍存活
   (mains=1);下一次 MC 运行经同 profile 交接复用同一实例,实测成功(场景 1)。
2. **tab/渲染进程随任务数缓慢累积**:3 次真实爬取后 renderer=5。数百任务量级前无碍
   (有 Bing 回退兜底),建议后续在适配器增加"运行前关闭旧 tab/陈旧浏览器实例"或
   运维侧定期重启专用浏览器。
3. 锁为文件锁(`%TEMP%/mediacrawler_cdp_<port>.lock`),成功路径释放正常;陈旧锁
   超时抢占逻辑见 mediacrawler_client.py:200-232。

## 五、默认切换(本提交)

- `config.py`:默认值 `"bing_html"` → `"mediacrawler"`(:86-88),§1.1/§1.2/§1.3 注释同步
  收口(门禁通过时间/证据指引/回滚命令);
- `.env.example`:样例值与注释同步;
- `tests/unit/test_storyline_search_web_topic_xpoz.py::test_config_en_line_defaults`:
  默认值断言同步(全仓唯一一处默认断言;节点测试均为 monkeypatch 注入,不依赖默认值);
- 测试:web_topic 相关 6 个测试文件 73 例,exit 0 零失败。

## 六、回滚

1. 显式设 `STORYLINE_WEB_TOPIC_ENGINE=bing_html`(User 级环境变量或启动注入)+ 重启
   工作流进程(config 为 import 期常量,运行中改 env 不生效);
2. 或 revert 本提交(默认切换为独立提交,不与能力层改动混合)。

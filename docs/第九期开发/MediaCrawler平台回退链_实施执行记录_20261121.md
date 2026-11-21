# MediaCrawler 平台回退链 实施执行记录(2026-11-21)

> 执行依据:`docs/第九期开发/MediaCrawler平台回退链_设计审查与修订执行计划_20261121.md`(实施权威)
> 被实施目标:`xhs → dy → Bing` 平台回退链(审查修订版 §4/§6)
> MediaCrawler 外部副本版本:`09ed1ec62d885dab422a53de999f52ac0bd4966b`(实施前复验一致)
> 本次提交:`4d5659c`(P1 适配器)→ config 提交(P2)→ `937d704`(P3 平台链)→ `a148515`(P4 节点/前端)→ 本记录(P6/P7)

---

## 一、阶段完成总览

| 审查阶段 | 内容 | 状态 | 提交/证据 |
|---|---|---|---|
| P0 | 许可与上游版本门禁 | ✅ 完成(含一处与审查文档不符的复验结论,见 §2) | 本记录 §2 |
| P1 | dy→douyin 输出目录映射 + aweme_url 字段映射 + 适配器诊断增强 | ✅ | `4d5659c` |
| P2 | 平台回退配置(FALLBACK_PLATFORMS 严格解析 + CHAIN_TIMEOUT) | ✅ | config 提交 |
| P3 | 平台链控制流 + 整链 deadline + attempts 诊断 + 完整链入 hash | ✅ | `937d704` |
| P4 | 节点层透传 + fallback_platform 独立状态标签 + 缓存标签镜像 + AI明细页 | ✅ | `a148515` |
| P5 | 单测矩阵(§6.2 配置 11 例 + §6.3 平台链 15 场景 + 适配器 8 新例 + 节点 4 新例) | ✅ 全绿 | 全量 2883 例 exit=0 零失败 |
| P6 | 真机与小流量验收 | ⏳ **留操作员**(清单见 §5) | — |
| P7 | 灰度与回滚 | ✅ 配置级回滚已验证(单测覆盖),操作员重启生效 | 本记录 §6 |

## 二、P0 门禁复验结论(逐项本机实证,非照抄审查文档)

| 审查声明 | 本机复验结果 |
|---|---|
| MC 固定版本 SHA | ✅ `09ed1ec62d885dab422a53de999f52ac0bd4966b`,与审查一致 |
| 许可 = NON-COMMERCIAL LEARNING 1.1 | ✅ 实读 LICENSE 首行确认;用途门禁沿用 2026-11-20 用户确认口径(**仅个人非商业学习/研究**),许可证声明保留于外部副本;若用途变更须先取得版权方书面授权 |
| dy 写 `douyin/json` 而 xhs 写 `xhs/json` | ✅ 实锤:`store/douyin/_store_impl.py` 4 处 `platform="douyin"`;`store/xhs/_store_impl.py` 用 `"xhs"`;`tools/async_file_writer.py:_get_file_path` 用 `self.platform` 拼目录 |
| 抖音关键词搜索不走 `get_aweme_detail` | ✅ `douyin/core.py search()` 直接从 `posts_res["data"]` 取 `aweme_info`/`aweme_mix_info.mix_items[0]` 就地落库;仅有有限防御(`except DataFetchError: break` / `except TypeError: continue` / `data` 缺失 break)——与审查 §3.3 修正一致 |
| MC 登录失败 `sys.exit()` 退出码 0 | ✅ `douyin/login.py:84/133/189` 三处无参 `sys.exit()` |
| `CDP_CONNECT_EXISTING=True` 默认 | ⚠️ **与审查不符**:本机固定版本实测 `config/base_config.py:86` = **`False`**(且 `ENABLE_CDP_MODE=True`)→ 走 CDP 自启专用浏览器实例路径(每平台独立 `browser_data/cdp_<platform>_user_data_dir`),平台 Profile 隔离在本机默认配置下基本成立。审查 §3.6 要求的"接入前真机确认浏览器模式"仍保留为 P6 操作员项,风险等级据此降级 |

## 三、实现要点(按审查修订版落地)

### 3.1 适配器 `storyline_capabilities/mediacrawler_client.py`(4d5659c)

- `MC_OUTPUT_DIR_BY_PLATFORM = {"dy": "douyin"}`:平台代码与物理输出目录解耦;`--platform` 仍传 CLI 代码 `dy`,仅读结果目录时映射;新平台接入须实测目录名后注册;
- `output_file` 诊断改用**实际映射目录**拼相对名(顺带修正原实现按 CLI 代码拼名的失真);
- 新错误码 `AUTH_REQUIRED`:退出码 0 + 无结果文件 + stderr/stdout(脱敏后)命中登录启发式标记(`qrcode/login/登录/二维码/请先登录/check_login_state`)→ 不再伪装 NO_RESULTS(审查 §3.7);
- 失败现场证据 `_persist_failure_evidence`(审查 §5.2-5):非零退出/超时/退出码 0 无结果文件三路径,把脱敏 stderr 全文(上限 64KB)写入时间戳运行目录 `stderr_full.txt`(TTL 清扫兜底回收),diagnostics 记相对文件名 + `data_dir_exists` + `result_files`(≤20 个文件名),不含绝对路径(P2-06 纪律不变)。

### 3.2 `config.py`(P2)

- `_MC_PLATFORM_WHITELIST = ("xhs", "dy")`;
- `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS`(默认 `"dy"`):`_parse_mc_platform_chain` 纯函数解析 —— 逗号拆分/strip/小写化,空串=()关闭回退;空项/未验证平台/重复/**主平台重复**导入期 ValueError(§6.2 全场景单测锁定);
- `STORYLINE_WEB_TOPIC_MC_CHAIN_TIMEOUT`(默认 600,范围 30..1800):整条链总预算,与单平台 `STORYLINE_WEB_TOPIC_MC_TIMEOUT`(30..900)分离。

### 3.3 能力层 `storyline_capabilities/search_web_topic.py`(937d704)

- MC 块改**平台链循环**:`platform_chain = (主平台, *fallback)`;`deadline = time.monotonic() + chain_timeout`,每平台 `timeout_seconds=min(单平台上限, 剩余预算)`;预算耗尽记 `TIME_BUDGET_EXHAUSTED` 并停止尝试(§3.5);
- 单平台成功条件(§4.2):子进程成功 + 结果文件可读 + 映射 ≥1 条有效 + 相关性守卫通过 —— 守卫**逐平台**执行;首个成功平台即停;
- `mediacrawler_request_provider(platform_chain, revision)`:完整有序平台链(`xhs>dy`)编入 provider 串(§3.4);接受 str 兼容旧调用(单平台链等价);`_parser_version_for` 按链首(主平台)解析;
- diagnostics:`platform_chain` + `attempts`(逐平台 platform/attempt_index/error_code/message/diagnostics)新增,`primary` 保持=主平台诊断(§4.4 兼容);
- 成功产物:`selected_platform` + `engine=MediaCrawler(<实际平台>)`;非主平台成功时 `platform_fallback={from_platform,to_platform,reason}` + message 记录切换原因(页面"平台回退说明"数据源);**不设**跨 Provider `fallback` 键(两类回退互斥,§4.4);
- 全链失败 → Bing 只调一次;`fallback.primary_*` 保持主平台语义;message 串联逐平台失败原因(可读回退说明)。

### 3.4 节点层 + 前端(a148515)

- 节点 `provider_options` 透传 `fallback_platforms` + `chain_timeout_seconds`;hash 用完整链(与能力层同函数,§3.4/§6.3-11);
- 新状态标签 `storyline_search_web_topic_fallback_platform`(平台回退成功),与 `fallback_bing` 互斥;新增 `_status_tags_for` 共享 helper,**顺带修复**存量缺陷:cache_hit 路径此前丢失 fallback 类标签(现在镜像实时路径);
- 前端三件:`console_service.py` web_topic 段透传 `platform_fallback`(脱敏限长);`ai_content.mjs` 映射 `platformFallback`;`ai_content.html` 渲染"平台回退说明"行(§5.5:不能只显示 MediaCrawler(抖音) 而丢失切换过程)。引擎名经 `_web_topic_engine_label` 原样透传,`MediaCrawler(抖音)` 直接可用。

## 四、测试结果

| 套件 | 结果 |
|---|---|
| `tests/unit/test_mediacrawler_client.py` | 全绿(新增 dy→douyin 映射/反向守卫/AUTH_REQUIRED/无标记 NO_RESULTS/证据落盘脱敏/平台标签 8 例) |
| `tests/unit/test_config_mc_platform_chain.py`(新文件) | 11 例全绿(§6.2 全表) |
| `tests/unit/test_storyline_search_web_topic_mc.py` | 全绿(新增 §6.3 矩阵 15 场景) |
| `tests/unit/nodes/test_node_search_web_topic.py` | 全绿(新增链透传+hash/平台回退标签/缓存镜像/链变化缓存失效 4 例) |
| **全量 `tests/unit`** | **exit=0,约 2883 例零失败**(对照 20261118 基线 2408 例,零新增失败) |

实施中发现的测试桩修正:证据落盘用例的 `_fake_run` 需显式 `write_results=False`(桩默认会写结果文件,污染 `data_dir_exists` 断言)。

## 五、P6 真机验收清单(留操作员)

1. 确认浏览器模式:当前默认 `ENABLE_CDP_MODE=True + CDP_CONNECT_EXISTING=False`(专用实例);启动一次 dy 检索观察弹窗浏览器与 `browser_data/cdp_dy_user_data_dir` 生成;
2. 抖音首跑扫码登录(一次性);关闭浏览器后会话复用验证;
3. 正常路径:新建任务跑网页话题检索,xhs 成功即止(不跑 dy);
4. 故障注入:临时设 `STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT` 指向空目录 → xhs 预检失败 → dy 接管;AI明细页应显示 `MediaCrawler(抖音)` + "平台回退说明"行;状态标签 `storyline_search_web_topic_fallback_platform`;
5. dy 无登录态场景:确认诊断为 AUTH_REQUIRED(或超时 NETWORK_ERROR)、Bing 兜底、任务不挂死;
6. 检查 `web_topic_mc/<run_id>/stderr_full.txt` 脱敏性与 TTL 清扫;
7. 浏览器子进程/CDP 锁/无残留。

## 六、P7 回滚(已验证部分 + 操作项)

- **配置回滚(单测已验证)**:`STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=""` → 链=("xhs",),行为与改造前逐字一致(`test_chain_empty_fallback_matches_legacy_behavior` + `test_chain_empty_string_disables_fallback` 锁定);**设空串须改启动脚本/环境后重启进程**(env 启动时读取);
- 引擎级回滚(不变):`STORYLINE_WEB_TOPIC_ENGINE=bing_html` + 重启;
- 平台链语义变化使旧 request_hash 必然 miss(§3.4 设计内),不会错误复用旧缓存。

## 七、DoD 对照(审查 §7)

- [x] P0 许可门禁(非商业学习口径沿用 2026-11-20 用户确认;SHA 复验一致)
- [x] dy 平台代码与 douyin 物理输出目录映射(单测锁定)
- [x] aweme_url fixture 与真实输出映射
- [x] xhs 成功不多跑 dy;xhs 失败 dy 成功不触 Bing;全失败只 Bing 一次
- [x] 每平台独立有效性 + 相关性校验
- [x] request_hash 含完整有序平台链,节点/能力层一致
- [x] 单次超时与整链预算分离
- [x] 登录失败不误报 NO_RESULTS(AUTH_REQUIRED)
- [x] diagnostics 完整尝试顺序 + 脱敏
- [x] 旧 artifact 兼容(schema_version 不 bump,增量键);前端区分两类回退
- [x] 新增单测 + 全量回归零新增失败
- [x] 回滚配置单测验证(真机重启验证留 P6)
- [ ] CDP Profile/登录态/退出行为真机确认 → P6 操作员
- [ ] 真机 dy 有效结果产出 → P6 操作员

## 八、遗留与备注

1. 审查 §2.2-E 的"笔记级解析诊断"(脱敏内容 ID 哈希/HTTP 状态/解析步骤)属受控观测性任务,本阶段只落地平台级 attempts + stderr 证据落盘;笔记级留后续;
2. 后续平台准入(ks→bili→wb→zhihu)严格按审查 §3.8 矩阵逐个验证,每个都要求:实测输出目录名 + 真实 JSON fixture + search() 路径核查 + 登录/超时/异常验收 + 真机冒烟,之后才允许扩白名单与 env;
3. 原实施计划文档(`网页话题检索MediaCrawler回退根因分析与平台回退链实施计划_console-654141c9890e_20261121.md`)§2/3/4/7 的事实表述与 §8/9/10 的实现要求已被审查文档取代(文首已加标注);其根因分析保留为需求背景与故障线索;
4. 审查文档 §9 参考链接中两个测试文件 URL 的 commit SHA 有笔误(缺 `91cb`),不影响实施,未回改审查文档。

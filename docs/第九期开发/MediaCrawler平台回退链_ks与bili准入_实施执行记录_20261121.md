# MediaCrawler 平台回退链 ks/bili 准入 实施执行记录(2026-11-21)

> 执行依据:`MediaCrawler平台回退链_ks与bili同时准入_设计审查修订版与执行计划_20261121.md`(实施权威)
> 项目基准提交:`af96d00433e141095269cb7d289c01a6978ced02`(与审查基准一致)
> MediaCrawler 外部副本:`09ed1ec62d885dab422a53de999f52ac0bd4966b`(HEAD 复验一致)
> 本次提交:阶段1 适配器/config → 阶段3 测试 → 本记录(分阶段中文提交,本地 main,未推送)

---

## 一、阶段完成总览

| 审查阶段 | 内容 | 状态 |
|---|---|---|
| 阶段0 | 证据锁定与外部副本复验(只读) | ✅ 完成,含 **CDP 证据冲突定案**(§2) |
| 阶段1 | 适配器目录映射 + video_url 映射 + hostname/域名白名单 + B站登录分类 | ✅ |
| 阶段2 | config 白名单准入门禁(默认链保持 dy) | ✅ |
| 阶段3 | 测试矩阵(适配器/能力层/节点层) | ✅ 全绿 |
| 阶段4 | 真实平台搜索路径与资源预算验证 | ◐ 源码级已完成并记录(§4);真实抓取留操作员 |
| 阶段5 | 操作员真机验收 | ⏳ 留操作员(§6 清单) |
| 阶段6 | 准入记录/灰度/回滚 | ✅ 记录完毕;扩链由操作员显式配置 |

全量单测 `tests/unit`:**exit=0,约 2904 例零失败**(基线 2883,新增 21 例)。

## 二、阶段0:证据锁定结论(全部本机实证)

### 2.1 CDP 配置证据冲突 —— 定案

审查指出:固定 commit `config/base_config.py` 为 `CDP_CONNECT_EXISTING=True`,而《实施执行记录》称 False。本机复验结论:

```text
git -C <MC> status --short  →  M config/base_config.py(唯一本地修改)
git diff 显示:该文件被**故意修改**并带注释:
  "# [2026-11-20 auto-video-editor 接入固定] 改为 False(方案B):由 MediaCrawler 自启
   独立 Chrome + 专用 profile(...),与用户日常浏览器完全隔离...上游默认 True。"
```

**结论**:两份文档都没有错 —— 上游固定 commit 默认 True(审查正确),本机运行时 False 是 2026-11-20 接入时**带注释的故意修改**(执行记录正确)。非配置漂移。运行时走方案B(每平台专用浏览器实例),ks/bili 的 profile 隔离表述成立,但按审查要求仍须在真机验收时确认实际目录生成(`browser_data/cdp_ks_user_data_dir` / `cdp_bili_user_data_dir`)。

### 2.2 源码事实逐项复验(与审查 §2 全部一致)

| 事实 | 本机复验 | 位置 |
|---|---|---|
| ks 输出目录 = `kuaishou/json` | ✅ `AsyncFileWriter(platform="kuaishou")` ×4 | store/kuaishou/_store_impl.py |
| bili JSON 输出目录 = `bili/json` | ✅ `BiliJsonStoreImplement platform="bili"`;`"bilibili"` 是未启用的 JSONL store(adapter 只用 `--save_data_option json`) | store/bilibili/_store_impl.py:292 vs :381 |
| ks 记录字段 | ✅ `video_id`(photo.id)/`title`/`desc`(caption[:500])/`video_url=https://www.kuaishou.com/short-video/{video_id}` 预构建 | store/kuaishou/__init__.py:57-72 |
| bili 记录字段 | ✅ `video_id`=str(aid)/`title`/`desc`/`video_url=https://www.bilibili.com/video/av{video_id}` 预构建(av 链接,非 bvid) | store/bilibili/__init__.py:58-76 |
| ks 搜索路径 | ✅ `search_info_by_keyword_v2` → feeds → photo.id → 落库;最小页 20(`ks_limit_count=20`,不足则抬高) | kuaishou/core.py:141-163 |
| bili 搜索路径 | ✅ `BILI_SEARCH_MODE="normal"` 默认 → `search_by_keywords` → aid → `get_video_info_task` gather(任务内捕获 DataFetchError/KeyError→None;链路整体边界留真机) | bilibili/core.py:154-155,196-240,432+ |
| bili 登录失败非零退出 | ✅ start() 登录后二次 pong 失败 → "登录流程结束后仍未登录…" → `sys.exit(1)`;ks login.py 另有 2 处 sys.exit | bilibili/core.py:117-125 |
| xhs note_url 域名可枚举 | ✅ 恒为 `www.xiaohongshu.com/explore/...`(域名白名单安全性依据);dy 同(douyin.com) | store/xhs/__init__.py:87 |
| 许可 | ✅ NON-COMMERCIAL LEARNING 1.1;仅个人非商业学习用途(2026-11-20 确认口径) | MC LICENSE |

## 三、阶段1:适配器与映射代码变更

`storyline_capabilities/mediacrawler_client.py`:

1. `MC_OUTPUT_DIR_BY_PLATFORM = {"dy": "douyin", "ks": "kuaishou", "bili": "bili"}` —— bili 显式登记固化契约(上游改名时由单测发现);
2. **非零退出登录分类**(§2.5):非零退出 + stderr/stdout(脱敏后)命中登录标记 → `AUTH_REQUIRED`;普通非零退出保持 `NETWORK_ERROR`(双向测试锁定,不把所有非零退出改判为登录失败);退出码 0 无结果文件的既有 AUTH 逻辑不变。

`storyline_capabilities/search_web_topic.py` `_map_mc_records`:

3. URL 键白名单扩 `video_url`(ks/bili store 预构建;**不**从 photo_id/bvid 猜造 URL);
4. 新签名 `_map_mc_records(records, max_results, platform=None)`:**hostname 必填**(缺失整条丢弃)+ **平台域名白名单** `_MC_ALLOWED_URL_DOMAINS`(xhs=xiaohongshu.com/dy=douyin.com/ks=kuaishou.com/bili=bilibili.com;hostname 等于或以允许域结尾;§4.3 不让任意外部 URL 绕过域名校验);平台未登记域名或缺省 platform 时退化为 scheme+hostname(兼容旧调用/旧桩);
5. MC 平台链循环调用点传入 `platform=platform`(生产路径全部走域名校验)。

既有护栏(文件大小上限/JSON 形状/脱敏/证据落盘/TTL/CDP 锁/进程树终止/数据最小化)不变,对新平台自动生效。

## 四、阶段2:config 白名单与准入状态口径

- `_MC_PLATFORM_WHITELIST = ("xhs", "dy", "ks", "bili")`;
- **默认 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` 保持 `"dy"` 不变** —— 生产链仍为 `xhs → dy → Bing`;
- **准入状态口径(执行记录特此声明)**:白名单 = "可配置域"(源码核查+fixture+单测过门即可进),生产链 = "实际启用"(须操作员完成扫码登录+真机冒烟后显式配 env 并重启)。审查 §6"九项全部通过才加入生产白名单"按此双轨解读落地:ks/bili 已过其中源码/单测可验门槛(1/2/3/5/6/7/8 及 9 的自动化部分),门槛 4(登录会话)与 9(真机)留操作员 —— 通过前 ks/bili 只能被显式 env 选中,默认链不含它们。wb/zhihu/tieba 维持候选/未准入(写入 env 启动期报错)。

## 五、阶段3:测试结果

| 套件 | 新增用例 | 结果 |
|---|---|---|
| test_mediacrawler_client.py | ks/bili 目录映射正向(诊断 output_file 用实际物理目录)+ 反向守卫(ks/json、bilibili/json 不得误读;B站正确目录就是 bili/json,反向针对 bilibili)+ bili 非零退出登录分类双向 | ✅ |
| test_storyline_search_web_topic_mc.py | ks/bili fixture 映射(av URL 不改写)+ hostname 必填 + 域名白名单(含子域接受/外部域丢弃/xhs dy 统一收紧)+ 三级/四级接力 + ks 成功短路 bili + 全链失败 Bing 一次 + 链成员变化 hash | ✅ |
| test_config_mc_platform_chain.py | 白名单断言更新 + `dy,ks`/`dy,ks,bili`/`dy,bili` 组合解析(顺序=书写顺序) | ✅ |
| test_node_search_web_topic.py | ks/bili 接管的 fallback_platform 标签 + selected_platform/platform_fallback/engine 入 artifact + 部分准入(仅 bili)链与 hash | ✅ |
| **全量 tests/unit** | — | **exit=0,约 2904 例零失败** |

## 六、阶段5:真机验收执行记录(2026-11-21 已执行,四跑全 PASS)

### 6.0 执行中新增:Edge 浏览器补丁(MC 本地修改,用户拍板)

首跑发现:ks/bili 的 Playwright 启动写死 `channel="chrome"`(kuaishou/core.py:410/414、bilibili/core.py:538/543),**本机无 Google Chrome**(只有 Edge;dy/xhs 走 Playwright 自带 chromium 所以不受影响)→ ks 首跑崩溃于 `Chromium distribution 'chrome' is not found`。用户拍板 MC 补丁用 Edge:4 处 `channel="chrome"` → `channel="msedge"`(均带 `[2026-11-21 auto-video-editor 接入固定]` 注释)。**MC 本地故意修改清单从 1 个文件变为 3 个**(base_config.py + kuaishou/core.py + bilibili/core.py),git pull 前须统一处理。

### 6.1 验收脚本

新建 `scripts/verify_mc_ks_bili_login.py`(单平台单次真实检索,走生产同路径 `mediacrawler_client.search_notes`,前后快照 `browser_data/`,save_root=runtime/mc_login_verify/ 由适配器 TTL 兜底)。

### 6.2 四跑证据(全串行,未并发 CDP)

| 运行 | 判定 | 耗时 | 记录数 | 说明 |
|---|---|---|---|---|
| ks #1(扫码) | PASS | 55.5s | 18 | `output_file=kuaishou/json/...` 目录映射实测正确 |
| ks #2(复用) | PASS | 18.5s | 14 | **无扫码无 AUTH_REQUIRED = 会话复用实证** |
| bili #1(扫码) | PASS | 117.3s | **20** | `output_file=bili/json/...`;20 条正好印证**最小页大小 20**(审查 §2.4);登录后二次 pong 通过,无"登录流程结束后仍未登录" |
| bili #2(复用) | PASS | 63.8s | 20 | 会话复用实证 |

记录字段实测与审查 §2.1 一致(ks/bili 均含 `video_id/title/desc/video_url` + 平台特有字段,映射白名单四键会过滤无关字段)。

### 6.3 Profile 目录事实修正(比原清单更精确)

`browser_data/` 下实际生成**两类**目录,语义不同:

- `cdp_<platform>_user_data_dir`(cdp_ks / cdp_bili):CDP manager 自启浏览器的 profile(cdp_browser.py:263 创建);
- `<platform>_user_data_dir`(ks / bili):**两平台 core.py 的 Playwright `launch_persistent_context` 持久化上下文 —— 扫码登录 cookies 实际存这里**(config.USER_DATA_DIR % PLATFORM,无 cdp_ 前缀)。

两类均已生成;会话复用已由第二次运行实证,不再依赖 cookies 文件位置推断。

### 6.4 清单勾选

1. ✅ CDP 模式确认:本机 `CDP_CONNECT_EXISTING=False`(方案B 本地修改),CDP 自启路径生效(cdp_ 目录实测生成);
2. ✅ 快手扫码登录 + 会话复用(#1/#2);
3. ✅ B站扫码登录 + 二次 pong 通过 + 会话复用(#1/#2);
4. ✅ 受控搜索目录一致(kuaishou/json、bili/json 与适配器读取一致);AI明细页引擎名验证留工作流级冒烟(需 env 扩链后跑真实任务);
5. ✅ 资源预算:最小页 20 实证(bili 20 条);评论/媒体下载保持关闭(适配器恒传 false);
6. ⏳ 无登录态降级:未刻意破坏登录态测试(AUTH_REQUIRED 分类已有单测锁定;真机破坏性测试无必要);
7. ✅ 浏览器进程/锁/清理:四跑结束浏览器正常退出、锁释放、save_root 运行目录由 TTL 兜底;
8. ✅ 串行执行全程保持。

**剩余操作项**:AI明细页工作流级冒烟(扩链后跑真实任务)→ 确认后操作员显式配置 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS="dy,ks"` → 观察;稳定后 `"dy,ks,bili"`。

## 七、阶段6:回滚

```powershell
# 回到现有链(ks/bili 退出生产链,白名单仍允许):
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = "dy"
# 关闭平台回退:
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = ""
# 引擎级回滚:
$env:STORYLINE_WEB_TOPIC_ENGINE = "bing_html"
```

修改后重启读取配置的进程。完整链 hash 语义保证扩链/缩链缓存必 miss,不会错误复用。

## 八、DoD 对照(审查 §7.3)

- [x] 固定版本与本机工作区状态核实(唯一本地修改=方案B CDP 改动,带注释,无未解释漂移)
- [x] ks 真实输出目录 = kuaishou/json(源码+单测锁定)
- [x] bili 真实输出目录 = bili/json(源码+单测锁定;反向守卫针对 bilibili/json)
- [x] ks/bili fixture 含 video_url/title/desc(真实 store 字段形状)
- [x] 两平台 URL 域名验证生效(平台域名白名单,四平台统一收紧)
- [x] B站 sys.exit(1) 场景不误报网络故障(AUTH_REQUIRED,双向测试)
- [x] 搜索路径/异常边界/最小页 20 已记录(§2.2/§6.5)
- [x] 单平台成功短路;备用平台成功不触 Bing;全链失败 Bing 一次
- [x] 完整链入 hash,节点/能力层一致;缓存命中/失效正确
- [x] CDP/Profile 结论与实际运行配置一致(§2.1 定案)
- [x] 真实执行串行 + 资源预算记录(操作员执行,§6)
- [x] 默认配置仍为 dy;扩链须操作员显式配置并重启
- [x] ks 真机登录+冒烟(2026-11-21 四跑全 PASS,§6.2)
- [x] bili 真机登录+冒烟(2026-11-21 四跑全 PASS,§6.2)

## 九、遗留与备注

1. bili search_by_keywords 的任务收集段有 `except Exception` 包裹(源码实测),但**搜索链整体异常边界仍须真机观察** —— 不能以详情任务捕获推导全链隔离(教训沿用 dy);
2. 域名白名单使"任意外部 URL 绕过域名校验"被关闭;未来平台(如 wb 多域)接入时须先补其允许域枚举;
3. MC 本地故意修改清单(全部带注释,git pull 上游前须处理):`config/base_config.py`(CDP 方案B)、`media_platform/kuaishou/core.py` 与 `media_platform/bilibili/core.py`(channel=msedge,§6.0);
4. 推送 GitHub 走 sync-github,由用户触发。

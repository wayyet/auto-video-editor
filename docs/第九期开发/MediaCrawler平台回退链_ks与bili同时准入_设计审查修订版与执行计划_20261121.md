# MediaCrawler 平台回退链：ks 与 bili 同时准入验证——设计审查修订版与执行计划

> 审查日期：2026-11-21  
> 项目：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 本次审查基准提交：`af96d00433e141095269cb7d289c01a6978ced02`  
> 被审查计划：[`MediaCrawler平台回退链_ks与bili同时准入验证与接入计划_20261121.md`](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E7%AC%AC%E4%B9%9D%E6%9C%9F%E5%BC%80%E5%8F%91/MediaCrawler%E5%B9%B3%E5%8F%B0%E5%9B%9E%E9%80%80%E9%93%BE_ks%E4%B8%8Ebili%E5%90%8C%E6%97%B6%E5%87%86%E5%85%A5%E9%AA%8C%E8%AF%81%E4%B8%8E%E6%8E%A5%E5%85%A5%E8%AE%A1%E5%88%92_20261121.md)  
> 被审查计划 Blob SHA：`34a325a267df709ebeded7fbd22e5d9c5f495f15`  
> MediaCrawler 外部源码复核版本：[`NanmiCoder/MediaCrawler@09ed1ec62d885dab422a53de999f52ac0bd4966b`](https://github.com/NanmiCoder/MediaCrawler/commit/09ed1ec62d885dab422a53de999f52ac0bd4966b)

---

## 0. 审查结论

### 0.1 总体判断

**原计划的方向和准入原则正确，但当前版本需要修订后再实施。** 当前 `auto-video-editor` 已经实现 `xhs → dy → Bing` 的通用平台回退链，ks/bili 接入属于在现有链路上扩充平台适配，不需要重新设计整套回退框架。

当前计划最重要的三处源码事实需要明确写入实施要求：

1. 快手 CLI 代码 `ks` 的 JSON 物理目录是 `kuaishou/json`。
2. B站 CLI 代码 `bili` 的 JSON 物理目录是 `bili/json`，不是 `bilibili/json`。
3. 快手和 B站存储的可用链接字段都是 `video_url`；不能把 `photo_id` 或 `bvid` 当成当前 JSON 输出中的 URL 字段。

此外，还发现两项可能影响真机验收的遗漏：

- B站存在 `sys.exit(1)` 的登录失败路径，当前适配器会把该路径归类为 `NETWORK_ERROR`，不一定能被现有 `AUTH_REQUIRED` 识别。
- 计划引用的 MediaCrawler 固定 commit 中，`CDP_CONNECT_EXISTING=True`；这与现有实施记录所称 `False` 不一致，必须先核实本机外部副本的工作区状态，不能直接声称 ks/bili 使用独立浏览器 Profile。

### 0.2 结论分级

| 级别 | 结论 | 处理要求 |
|---|---|---|
| P0 | MediaCrawler 固定 commit 与实施记录中的 CDP 配置结论不一致 | 真机登录测试前先核实外部副本是否有未提交修改、运行时配置来源 |
| P1 | ks 输出目录与 CLI 代码不一致 | 增加 `ks → kuaishou` 目录映射 |
| P1 | `video_url` 未进入当前映射白名单 | `_map_mc_records()` 增加 `video_url`，并添加平台 fixture |
| P1 | B站非零退出登录失败可能被错分为网络故障 | 扩充登录错误诊断和单测 |
| P1 | ks/bili 真实搜索路径、结果数下限和异常边界未纳入验收 | 增加平台级源码核查和测试用例 |
| P2 | B站目录反向守卫不能照搬快手测试 | 对 B站测试错误目录 `bilibili`，不能把正确目录 `bili` 当成错误目录 |
| P2 | 平台链测试目前主要覆盖 xhs/dy | 增加 ks、bili 逐级接力及四平台完整链用例 |
| P2 | URL 当前只校验 scheme，没有完成门槛要求的 host 验证 | 明确允许域名校验和异常 URL 测试 |
| P2 | `CRAWLER_MAX_NOTES_COUNT` 在 ks/bili 搜索路径中会被提高到至少 20 | 将最小页大小纳入资源预算、耗时和数据量验收 |

### 0.3 本次验证边界

本次审查已读取 GitHub 上 `auto-video-editor/main` 和 MediaCrawler 固定 commit 的源码、配置、测试文件及相关设计文档。**没有在用户本机运行 pytest，也没有启动浏览器或访问快手/B站进行实时抓取。** 因此：

- 源码路径、字段、目录和配置值属于可由固定版本源码复核的结论。
- 实施记录中“2883 例全绿”属于仓库文档所记录的既有测试结果，本次没有重新执行验证。
- 本机 MediaCrawler 是否存在未提交配置修改、实际浏览器是否使用指定 Profile、平台登录态是否有效，必须由操作员在本机复核。

---

## 1. 当前 auto-video-editor 实现基线

### 1.1 当前生产白名单

当前 `config.py` 的平台白名单为：

```python
_MC_PLATFORM_WHITELIST = ("xhs", "dy")
```

当前主平台默认值是 `xhs`，回退配置默认值是：

```text
STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS="dy"
```

`_parse_mc_platform_chain()` 会按白名单校验回退列表。因此，当前将 `ks` 或 `bili` 写入 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS`，会在配置加载阶段抛出 `ValueError`；将主平台 `STORYLINE_WEB_TOPIC_MC_PLATFORM` 设为未准入平台，也会被白名单拒绝。

**不能把 `MC_PLATFORM_LABELS` 中已经存在 `ks`、`bili` 的展示名称，解释成这两个平台已获准运行。** 展示标签全集、上游 CLI 枚举和生产准入白名单是三个不同概念。

### 1.2 当前适配器状态

`storyline_capabilities/mediacrawler_client.py` 当前目录映射只有：

```python
MC_OUTPUT_DIR_BY_PLATFORM = {
    "dy": "douyin",
}
```

`search_notes()` 通过目录映射读取 JSON；未登记映射的平台会默认把 CLI 代码当作物理目录名。因此 ks 当前会尝试读 `ks/json`，但固定版本 MediaCrawler 实际写入 `kuaishou/json`。

B站物理目录本身就是 `bili`，与 CLI 代码一致；可以依赖默认透传，也可以显式登记 `"bili": "bili"` 固化目录契约。

### 1.3 当前记录映射状态

`storyline_capabilities/search_web_topic.py::_map_mc_records()` 当前 URL 键白名单为：

```python
("note_url", "url", "aweme_url")
```

摘要映射已经支持：

```python
rec.get("desc") or rec.get("content")
```

因此，ks/bili 的 `title` 和 `desc` 可沿用既有字段映射，但当前缺少 `video_url`，会导致有标题但无法取得标准 URL 的记录被丢弃。

此外，当前 `_map_mc_records()` 只校验 URL 的 `http/https` scheme，没有完成九项准入门槛第 2 项所要求的主机名验证。新增 `video_url` 时，应同步补齐域名校验。

### 1.4 已有通用链路，不应重复开发

当前代码已经具备以下能力：

- `platform_chain = (主平台, *fallback_platforms)` 的顺序执行；
- 每个平台独立执行原始结果读取、标准映射、有效性检查和相关性守卫；
- 整链 `time.monotonic()` deadline，单次子进程超时受剩余预算限制；
- 完整有序平台链进入 `request_hash`；
- `attempts`、`selected_platform`、`platform_fallback` 诊断字段；
- 平台回退与 MC→Bing 跨 Provider 回退分别记录；
- 只有全部 MC 平台失败后才调用一次 Bing；
- 节点层缓存标签能够镜像平台回退状态。

因此，本次实施重点是：**平台输出目录适配、真实字段映射、登录错误分类、平台搜索路径测试、配置准入和真机验收。** 不应重写已经工作的链路控制、deadline 或 hash 机制。

### 1.5 当前自动化测试覆盖边界

当前 `tests/unit/test_config_mc_platform_chain.py` 明确断言白名单只有 `("xhs", "dy")`；`tests/unit/test_mediacrawler_client.py` 已覆盖 `dy → douyin`；`tests/unit/test_storyline_search_web_topic_mc.py` 的平台链 fixture 主要围绕 `xhs`、`dy` 展开。

当前测试不能证明 ks/bili 已完成准入，也不能证明 ks/bili 的物理目录、记录字段、搜索路径或登录行为正确。

---

## 2. MediaCrawler 固定版本源码核查

核查版本：`09ed1ec62d885dab422a53de999f52ac0bd4966b`。本节的目录与字段结论均以该 commit 为准，不以 MediaCrawler 当前浮动 `main` 代替。

### 2.1 平台目录与真实字段

| 核查项 | 快手 `ks` | B站 `bili` |
|---|---|---|
| CLI 平台代码 | `ks` | `bili` |
| Crawler 类 | `KuaishouCrawler` | `BilibiliCrawler` |
| 关键词搜索入口 | `KuaishouCrawler.search()` | `BilibiliCrawler.search()` → `search_by_keywords()`（默认模式） |
| JSON Store Writer 平台参数 | `platform="kuaishou"` | `platform="bili"` |
| 实际 JSON 目录 | `kuaishou/json` | `bili/json` |
| 内容 ID 字段 | `video_id`（来自 `photo.id`） | `video_id`（来自 `View.aid`） |
| 标题字段 | `title` | `title` |
| 摘要字段 | `desc` | `desc` |
| 可直接使用的 URL 字段 | `video_url` | `video_url` |
| URL 形式 | `https://www.kuaishou.com/short-video/{video_id}` | `https://www.bilibili.com/video/av{video_id}` |
| 是否需要从 `photo_id` / `bvid` 构造 URL | 不需要；当前保存记录已构造 `video_url` | 不需要；当前保存记录已构造 `video_url`，且使用的是 `aid` 对应的 av 链接 |

#### 2.1.1 快手目录与记录结构

固定版本 `store/kuaishou/_store_impl.py` 的 JSON Store 使用：

```python
AsyncFileWriter(platform="kuaishou", ...)
```

`store/kuaishou/__init__.py::update_kuaishou_video()` 保存的主要字段包括：

```python
{
    "video_id": video_id,
    "title": photo_info.get("caption", "")[:500],
    "desc": photo_info.get("caption", "")[:500],
    "video_url": f"https://www.kuaishou.com/short-video/{video_id}",
}
```

因此必须增加 `ks → kuaishou` 物理目录映射，并让 `_map_mc_records()` 读取 `video_url`。

#### 2.1.2 B站目录与记录结构

固定版本 `store/bilibili/_store_impl.py` 的 JSON Store 使用：

```python
AsyncFileWriter(platform="bili", ...)
```

不是 `platform="bilibili"`。`store/bilibili/__init__.py::update_bilibili_video()` 保存的主要字段包括：

```python
{
    "video_id": str(video_item_view.get("aid")),
    "title": video_item_view.get("title", "")[:500],
    "desc": video_item_view.get("desc", "")[:500],
    "video_url": f"https://www.bilibili.com/video/av{video_id}",
}
```

B站当前保存记录的 `video_url` 是以 `aid` 构建的 av 链接。原始搜索流程中可能出现 `bvid`，但这不等于 JSON Store 已保存 `bvid` 字段，更不应在适配器中猜测并构造另一种链接。

### 2.2 `AsyncFileWriter` 的目录生成规则

固定版本 `tools/async_file_writer.py::_get_file_path()` 按 `self.platform` 拼接：

```python
base_path = f"{config.SAVE_DATA_PATH}/{self.platform}/{file_type}"
```

因此物理目录由 Store 创建 Writer 时的 `platform=` 决定，而不是 `main.py --platform` 的 CLI 字符串直接决定。

**修订后的目录映射建议：**

```python
MC_OUTPUT_DIR_BY_PLATFORM = {
    "dy": "douyin",
    "ks": "kuaishou",
    "bili": "bili",
}
```

`bili: bili` 是显式固定当前契约，并非目录重命名；如果选择省略该项，适配器的默认透传也会得到相同路径，但测试必须锁定真实行为。

### 2.3 关键词搜索路径不能混为一谈

#### 快手

`media_platform/kuaishou/core.py::KuaishouCrawler.search()` 的关键词路径主要是：

1. 调用 `ks_client.search_info_by_keyword_v2()`；
2. 检查返回结构和 `result`；
3. 遍历 `feeds`；
4. 从 `video_detail.photo.id` 取得内容 ID；
5. 调用 `update_kuaishou_video()` 落库；
6. 执行媒体下载分支和评论任务。

不能把快手详情模式的异常捕获，当成关键词搜索路径已具备逐条异常隔离的证据。需要针对 `videos_res` 为空、`result != 1`、`feeds` 缺失、`photo` 缺失、`photo.id` 缺失、单条存储异常及部分结果等情况做 fixture 和真机观察。

#### B站

固定版本 `config/bilibili_config.py` 的 `BILI_SEARCH_MODE` 默认值为 `normal`。在该模式下，`BilibiliCrawler.search()` 调用 `search_by_keywords()`，其路径为：

1. 调用 `bili_client.search_video_by_keyword()`；
2. 从 `videos_res["result"]` 取得视频列表；
3. 为列表中的 `aid` 创建 `get_video_info_task()`；
4. 通过 `asyncio.gather()` 等待详情任务；
5. 调用 `update_bilibili_video()` 和 `update_up_info()`。

`get_video_info_task()` 捕获 `DataFetchError`、`KeyError`，但不能由此推导搜索链所有异常都已隔离：任务收集、`asyncio.gather()`、详情返回结构和后续 Store 写入仍需分别验证。测试应明确固定 `BILI_SEARCH_MODE="normal"`，不能只测试抽象的 `search()` 被调用。

### 2.4 一个会影响抓取量的事实：最小页大小为 20

快手和 B站的关键词搜索实现都设置了每页数量 `20`，并在 `CRAWLER_MAX_NOTES_COUNT < 20` 时将其提高到 `20`。

这意味着即使适配器传入 `max_notes=7` 或项目配置较小，外部 MediaCrawler 仍可能至少请求一页、处理最多 20 条搜索结果。能力层随后按 `max_results` 截断标准化结果，不代表上游只请求了相同数量。

实施要求：

- 把“上游最小一页 20 条”记入资源预算和运行耗时评估；
- 确认评论、媒体下载开关保持关闭；
- 真实验收记录上游实际结果文件条数与能力层最终返回条数；
- 不得将 `max_results` 误称为 MediaCrawler 请求量的严格上限。

### 2.5 登录错误分类需要补齐 B站非零退出路径

ks/bili 的二维码登录代码存在 `sys.exit()` 退出路径；无参数 `sys.exit()` 通常以退出码 0 结束。适配器已经对“退出码 0 + 无结果文件 + 登录提示标记”增加 `AUTH_REQUIRED` 启发式判断。

但 B站 `BilibiliCrawler.start()` 还有一个额外路径：登录流程结束后再次执行 `bili_client.pong()`；如果仍未登录，会记录“登录流程结束后仍未登录”并执行 `sys.exit(1)`。

当前适配器对所有非零退出码统一返回 `NETWORK_ERROR`，并不会先检查该路径中的登录诊断标记。因此，B站登录失败仍可能被错误归类为网络故障。

**修订要求：**

- 在非零退出处理分支中，增加基于明确登录标记的有限分类；
- B站的“登录流程结束后仍未登录”应归类为 `AUTH_REQUIRED` 或 `LOGIN_FAILED`；
- 普通非零退出仍保持 `NETWORK_ERROR`，不能把所有非零退出都改判为登录失败；
- stderr/stdout 仍执行脱敏、截断和受 TTL 管理的证据落盘；
- 增加 `returncode=1 + 登录失败标记`、`returncode=1 + 普通错误` 两类相反测试，防止误分类。

### 2.6 CDP 配置存在文档证据冲突

固定 commit 的 `config/base_config.py` 实际值为：

```python
ENABLE_CDP_MODE = True
CDP_CONNECT_EXISTING = True
```

固定版本 `tools/cdp_browser.py::launch_and_connect()` 在 `CDP_CONNECT_EXISTING=True` 时走 `_connect_existing_browser()`，不会进入 `_launch_browser()`。而 `browser_data/cdp_<platform>_user_data_dir` 的创建发生在 `_launch_browser()` 中。

因此，在固定 commit 的默认配置下，不能仅凭 `USER_DATA_DIR` 模板推断 ks/bili 分别使用独立 Profile；连接已有浏览器的路径不会按平台创建上述专用目录。

现有《实施执行记录》却称同一固定版本 `CDP_CONNECT_EXISTING=False`。这与 GitHub 固定 commit 的源码不一致，可能意味着本机外部副本存在未提交修改，或执行记录读取的不是实际运行配置。

实施前必须在本机 MediaCrawler 根目录执行：

```powershell
git -C "<MediaCrawler项目目录>" rev-parse HEAD
git -C "<MediaCrawler项目目录>" status --short
git -C "<MediaCrawler项目目录>" diff -- config/base_config.py tools/cdp_browser.py
git -C "<MediaCrawler项目目录>" diff --cached -- config/base_config.py tools/cdp_browser.py
```

同时记录运行时实际加载的：

```text
config.ENABLE_CDP_MODE
config.CDP_CONNECT_EXISTING
config.CDP_DEBUG_PORT
config.USER_DATA_DIR
config.PLATFORM
```

如果确实需要每个平台独立 Profile，必须先确认运行配置使用 `CDP_CONNECT_EXISTING=False`，再验证实际创建目录；不能把本机未提交配置变化描述成固定 commit 的默认行为。若继续使用 `CDP_CONNECT_EXISTING=True`，应明确记录这是共享的现有浏览器会话，不得声称账号/Profile 已按平台隔离。

---

## 3. 原计划正确的部分与需要修改的部分

### 3.1 可以保留

以下设计原则正确，建议原样保留：

1. ks、bili 可以同时启动源码核查，但每个平台九项准入门槛独立判定。
2. 一个候选平台未通过门槛，不阻塞另一个候选平台继续验证。
3. 未过门平台不加入 `_MC_PLATFORM_WHITELIST`，也不能写入有效的 fallback 环境变量。
4. `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` 默认值保持 `dy`，不因源码适配完成就自动扩大生产范围。
5. 每个平台成功条件仍是：子进程成功、输出文件存在且可解析、标准映射有效、至少有一条有效记录、相关性守卫通过。
6. 只有全部 MC 平台失败后才调用 Bing，且 Bing 只调用一次。
7. 完整有序平台链必须参与 `request_hash`。
8. 保留整链总超时、进程树终止、CDP 锁、失败证据脱敏和 TTL 清理。
9. 真机验收必须由操作员执行；源码和单测通过不等于平台已完成生产准入。

### 3.2 必须修订

| 原计划位置 | 原表述/隐含假设 | 修订后要求 |
|---|---|---|
| 阶段 0：ks/bili 输出目录 | 示例中将 `bili` 的预期目录写成 `bilibili` | 固定 commit 实测为 `ks → kuaishou`、`bili → bili` |
| 阶段 0：记录字段 | `ks` 疑似 `photo_id`、`bili` 疑似 `bvid` | 以 Store 最终 JSON 为准，两者都使用 `video_url`、`video_id`、`title`、`desc` |
| 阶段 1：URL 映射 | 只写“扩展 URL 键白名单”，没有锁定真实字段 | 明确增加 `video_url`，不增加无证据的 `photo_id`/`bvid` URL 假设 |
| 阶段 3：双平台目录反向测试 | 对 ks/bili 都要求 `<CLI代码>/json` 写入结果不被读取 | ks 反向检查 `ks/json`；B站正确路径就是 `bili/json`，反向检查应针对错误的 `bilibili/json` |
| 阶段 3：链路测试 | 主要列出 `xhs → dy → ks` | 补齐 `ks` 失败后 `bili` 接管、完整 `xhs → dy → ks → bili → Bing` 和每个平台的短路行为 |
| 门槛 7：错误诊断 | 假设现有 `AUTH_REQUIRED` 自动覆盖两平台 | 补齐 B站 `sys.exit(1)` 登录失败路径的诊断分类 |
| 阶段 4：登录态 | 直接假设登录态持久化在各平台独立目录 | 先核实 CDP_CONNECT_EXISTING 与本机工作区是否一致，再决定独立 Profile 验收方式 |
| 门槛 5：资源预算 | 认为现有 max_notes 可直接限制实际抓取量 | 记录 ks/bili 搜索至少按 20 条一页运行的行为 |

### 3.3 两个平台并行验证的正确边界

“同时启动 ks + bili 准入验证”应解释为：

- **可以并行：** 固定版本源码读取、目录/字段核查、fixture 设计、静态检查和相互独立的单元测试编写。
- **应串行：** 同一台机器上的真实扫码登录、真实搜索、共享 CDP 端口的浏览器控制和小流量验收。

这样既保留两条准入线并行推进，也避免两个平台同时争用一个现有浏览器会话、CDP 端口或登录状态。平台验证并行不等于必须同时发起两路真实抓取。

---

## 4. 修订后的目标设计

### 4.1 准入与运行链

两平台都通过准入时，目标链为：

```text
xhs → dy → ks → bili → Bing
```

某个平台未过门时，只从链中排除该平台，不影响其他通过门槛的平台：

| ks 结果 | bili 结果 | 生产候选链 |
|---|---|---|
| 未通过 | 未通过 | `xhs → dy → Bing` |
| 通过 | 未通过 | `xhs → dy → ks → Bing` |
| 未通过 | 通过 | `xhs → dy → bili → Bing` |
| 通过 | 通过 | `xhs → dy → ks → bili → Bing` |

`STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` 只配置主平台之后的平台，不包含 `xhs`。

### 4.2 适配器目录映射

推荐最终映射：

```python
MC_OUTPUT_DIR_BY_PLATFORM = {
    "dy": "douyin",
    "ks": "kuaishou",
    "bili": "bili",
}
```

其中 `bili: bili` 是显式目录契约，便于将来上游目录变化时由单测发现。

### 4.3 标准记录映射

`_map_mc_records()` 应按以下契约提取：

| 标准字段 | 快手原始字段 | B站原始字段 |
|---|---|---|
| `url` | `video_url` | `video_url` |
| `title` | `title` | `title` |
| `snippet` | `desc`，为空时沿用既有 `content` fallback | `desc`，为空时沿用既有 `content` fallback |
| `source` | 固定 `mediacrawler` | 固定 `mediacrawler` |
| 去重键 | 标准化 URL | 标准化 URL |

URL 处理至少满足：

1. 只接收 `http/https`；
2. 必须存在 hostname；
3. ks 记录应匹配快手允许域名；bili 记录应匹配 B站允许域名；
4. 不从 `photo_id`、`video_id` 或 `bvid` 静默猜造 URL；
5. 同 URL 去重、标题非空、结果数量上限和相关性守卫继续沿用现有语义。

建议给 `_map_mc_records()` 增加 `platform` 参数，或使用等价的明确平台上下文，以便执行平台域名白名单检查。不要让记录中的任意外部 URL 绕过域名校验。

### 4.4 登录和 CDP 处理

- 真机测试前先解决 `CDP_CONNECT_EXISTING` 固定 commit 与本机工作区不一致的问题。
- 如使用独立 Profile 模式，先确认每个平台的目录真实生成、关闭后会话可复用、不同平台不串号。
- 如使用现有浏览器连接模式，应把它明确标注为共享浏览器会话；不能将目录模板误当作隔离证据。
- 首次扫码登录应作为受控初始化步骤，不能把“还未完成登录”当作平台搜索无结果。
- ks/bili 真机搜索应串行执行；不并发操作共享 CDP 端口。

---

## 5. 分阶段修订执行计划

### 阶段 0：证据锁定与外部副本复验（只读）

**目标：** 在改代码前把项目代码、外部依赖版本和实际运行配置锁定，避免针对错误版本实施。

| 工作项 | 操作 | 产出/验收 |
|---|---|---|
| 0.1 项目版本 | 记录 `auto-video-editor` 当前 `main` commit | 执行记录写入完整 SHA |
| 0.2 MC 版本 | `git rev-parse HEAD` 核对固定 commit | SHA 与 `09ed1ec62d885dab422a53de999f52ac0bd4966b` 一致，或重新按实际版本核查 |
| 0.3 MC 工作区 | `git status --short`、`git diff`、`git diff --cached` | 明确 `config/base_config.py`、`tools/cdp_browser.py` 是否被本地修改 |
| 0.4 物理目录 | 检查两平台 Store 的 `AsyncFileWriter(platform=...)` | ks=`kuaishou`；bili=`bili` |
| 0.5 真实字段 | 从 Store save item 构造读取字段 | 两平台均确认 `video_url/title/desc/video_id` |
| 0.6 搜索路径 | 读取 ks `search()`、bili `search()`、bili 搜索模式默认值 | 分别形成调用链和异常边界说明 |
| 0.7 CDP | 记录运行时 CDP 配置和实际浏览器路径 | 解释固定 commit 与实施记录 `False` 的差异 |
| 0.8 许可 | 复核 LICENSE 与实际用途 | 仅在符合非商业学习许可及目标平台规则的范围内进行验证；商业用途须先取得书面授权 |

**本阶段不修改代码，不启动两平台真实抓取。**

### 阶段 1：适配器目录及记录映射

修改文件：

- `storyline_capabilities/mediacrawler_client.py`
- `storyline_capabilities/search_web_topic.py`

实施内容：

1. 在 `MC_OUTPUT_DIR_BY_PLATFORM` 中注册 `ks: kuaishou`、`bili: bili`。
2. 保持命令行 `--platform` 传 `ks` / `bili`；只在本地读取 JSON 时使用物理目录映射。
3. `_map_mc_records()` URL 键白名单增加 `video_url`。
4. `title`、`desc` 沿用真实 Store 输出字段；不增加没有源码证据的 `photo_id`/`bvid` URL 拼接逻辑。
5. 增加 hostname 与平台域名白名单验证。
6. 保留已有文件大小上限、JSON 形状校验、stderr 脱敏、失败现场落盘、TTL、CDP 锁、进程树终止和原始文件数据最小化行为。
7. 增加 B站非零退出登录错误分类，但仅对明确登录提示命中 `AUTH_REQUIRED`/`LOGIN_FAILED`；普通非零退出仍为 `NETWORK_ERROR`。

**验收：** 两个平台的脱敏 JSON fixture 均能映射为标准四键结构；错误目录中的文件不能被误读；无效 hostname、非 HTTP(S) URL、空标题和重复 URL 均按既有规则过滤。

### 阶段 2：平台配置准入门禁

修改文件：

- `config.py`
- `tests/unit/test_config_mc_platform_chain.py`

实施纪律：

- 不允许在阶段 0/1 尚未完成时就把 ks/bili 无条件加入生产白名单。
- 哪个平台完成源码核查、fixture、测试及其门槛后，只把该平台加入 `_MC_PLATFORM_WHITELIST`。
- `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` 默认值继续保持 `dy`。
- 保留空串关闭回退、拒绝空项/未知平台/重复平台/主平台重复等严格解析行为。

需要覆盖的配置组合：

| 通过准入的平台 | 有效配置示例 | 预期 |
|---|---|---|
| 无新增平台 | `dy` | 现有 `xhs → dy → Bing` |
| 只有 ks | `dy,ks` | `xhs → dy → ks → Bing` |
| 只有 bili | `dy,bili` | `xhs → dy → bili → Bing` |
| ks 与 bili 都通过 | `dy,ks,bili` | `xhs → dy → ks → bili → Bing` |
| 某平台未准入 | 把该平台写入 env | 配置加载时报错 |

### 阶段 3：适配器和能力层测试矩阵

#### 3.1 `tests/unit/test_mediacrawler_client.py`

增加：

- ks 写入 `kuaishou/json` 时能被正确读取；
- ks 写入错误的 `ks/json` 时不得误读；
- bili 写入 `bili/json` 时能被正确读取；
- bili 写入错误的 `bilibili/json` 时不得误读；
- 两平台的 `search_contents_*.json` 文件名和 JSON list[dict] 结构通过校验；
- B站 `returncode=1 + 登录失败标记` 能归类为登录错误；
- B站普通非零退出不应被错误归类为登录失败；
- 失败 stderr 证据保持脱敏与 TTL 清理。

注意：B站正确物理目录与 CLI 代码同为 `bili`，所以不能照搬 ks 的反向守卫，把 `bili/json` 错误地当成“不应读取”的目录。

#### 3.2 `tests/unit/test_storyline_search_web_topic_mc.py`

增加：

- ks 原始记录 `video_url/title/desc` 映射正确；
- bili 原始记录 `video_url/title/desc` 映射正确，确认 av URL 不被改写成猜测的 BV URL；
- hostname 白名单、非法 scheme、无 hostname、空标题、重复 URL、超量截断；
- ks、bili 均执行有效性与相关性守卫；
- ks 失败后 bili 成功，Bing 不调用；
- ks 成功后 bili 不调用；
- xhs/dy/ks 失败后 bili 成功；
- xhs/dy/ks/bili 全失败后 Bing 只调用一次；
- 平台链到期后不再启动下一平台；
- 平台链成员或顺序改变时 `request_hash` 改变；
- 编程错误 fail-fast，不被伪装成平台不可用。

#### 3.3 节点层测试

文件：`tests/unit/nodes/test_node_search_web_topic.py`

增加或扩展：

- `fallback_platforms` 和 `chain_timeout_seconds` 正确透传；
- 完整链 hash 与能力层一致；
- `selected_platform` 和 `platform_fallback` 在 artifact 中保留；
- `MediaCrawler(快手)`、`MediaCrawler(B站)` 展示标签正确；
- 实时执行与 `cache_hit` 的平台回退状态标签一致；
- 当只有 bili 通过、ks 未通过时，节点仍按 `xhs → dy → bili` 执行。

### 阶段 4：真实平台搜索路径和资源预算验证

在不改变平台访问策略、不并发争用 CDP 的前提下，分别验证：

| 验证项 | 快手 | B站 |
|---|---|---|
| 搜索模式 | `search_info_by_keyword_v2` | `BILI_SEARCH_MODE=normal`、`search_video_by_keyword` |
| 输入 | 单一规范化关键词 | 单一规范化关键词 |
| 原始结果 | `feeds`、`photo.id` | 搜索列表 `aid`、详情 `View` |
| 保存结果 | `video_id/title/desc/video_url` | `video_id/title/desc/video_url` |
| 最小页大小 | 20 | 20 |
| 资源行为 | 评论/媒体下载关闭，记录实际返回条数 | 评论/媒体下载关闭，记录实际返回条数 |
| 失败行为 | 空响应、异常响应、字段缺失、单条存储失败 | 搜索 API 异常、详情任务异常、`View` 结构缺失、Store 写入异常 |

建议使用低频、有限的旅游相关查询样本，每个平台至少覆盖：目的地名称、景点名称、路线/攻略表达。记录检索词、运行 ID、耗时、原始结果数、有效映射数、相关性结果数、错误码和最终接管平台。不要以单次有结果就认定持续稳健。

### 阶段 5：操作员真机验收

必须在阶段 0 的 CDP 配置矛盾解决后执行。

1. 确认实际运行的 CDP 模式、端口、Profile 和登录态存储位置。
2. 快手单独完成一次扫码登录和会话复用验证；记录会话实际保存位置。
3. B站单独完成一次扫码登录和会话复用验证；重点验证登录后 `pong()` 二次检查及退出码诊断。
4. 两个平台分别运行受控关键词搜索，确认实际 JSON 物理目录与适配器读取目录一致。
5. 确认 AI 明细页引擎名和平台回退说明正确，例如 `MediaCrawler(快手)`、`MediaCrawler(B站)`。
6. 注入或模拟无登录态、无有效记录和超时情景，确认回退继续、任务不挂死、错误不被伪装成正常空结果。
7. 确认浏览器进程、CDP 锁、子进程树及失败目录 TTL 清理正常。
8. 同一 CDP 端口下 ks 与 bili 的真实执行串行进行，不做并发扫码和并发搜索。

### 阶段 6：准入记录、灰度和回滚

- 在 `MediaCrawler平台回退链_实施执行记录_20261121.md` 新增 ks 与 bili 独立核查单、JSON fixture 摘要、目录证据、登录证据、测试结果和操作员验收结果。
- 只有九项门槛全部通过的平台才能加入生产白名单。
- 默认 fallback 仍保持 `dy`；平台全部验收后由操作员显式配置并重启进程。
- 回滚到现有链：

```powershell
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = "dy"
```

- 关闭平台回退：

```powershell
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = ""
```

- 引擎级回滚继续使用 `STORYLINE_WEB_TOPIC_ENGINE=bing_html`，修改后重启工作流进程。

---

## 6. 九项准入门槛修订版

| # | 门槛 | ks 必须满足 | bili 必须满足 | 不通过的处理 |
|---:|---|---|---|---|
| 1 | 平台标识与物理目录 | `ks → kuaishou/json` | `bili → bili/json` | 不加目录映射/白名单 |
| 2 | 真实字段 fixture | `video_id/title/desc/video_url` | `video_id(title aid)/title/desc/video_url` | 不扩 URL 映射 |
| 3 | 关键词搜索路径 | 验证 `search_info_by_keyword_v2 → feeds` 及异常边界 | 验证 `BILI_SEARCH_MODE=normal → search_by_keywords` 及详情任务边界 | 保持候选状态 |
| 4 | 登录与会话 | 验证 QR 登录、会话持久化及运行时 CDP 模式 | 同左，并验证登录后二次 `pong()` | 不进入真机通过状态 |
| 5 | 超时与资源清理 | 单平台 timeout、整链 deadline、20 条最小页大小、锁和进程清理 | 同左 | 不扩大生产链 |
| 6 | 数据质量 | URL 域名、标题、摘要、去重、相关性、有效记录率 | 同左，URL 保持 av 形式 | 不视为平台成功 |
| 7 | 错误与诊断 | 登录、网络、解析、无结果、超时分类准确 | 额外覆盖 `sys.exit(1)` 登录失败分类 | 不允许以 `NETWORK_ERROR` 模糊掩盖已识别登录失败 |
| 8 | 缓存与回退 | 全链 hash、平台接力、Bing 最终回退和 artifact 语义正确 | 同左 | 不允许上线 |
| 9 | 自动化与真机 | 平台 fixture、链路单测、低频真机搜索通过 | 同左 | 保持未准入 |

**准入规则：单个平台九项全部通过，才准许该平台加入白名单；ks 与 bili 互不构成阻塞依赖。**

---

## 7. 测试执行命令与验收标准

在 `auto-video-editor` 仓库根目录、Windows PowerShell 环境执行：

### 7.1 定向测试

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_mediacrawler_client.py tests/unit/test_storyline_search_web_topic_mc.py tests/unit/test_config_mc_platform_chain.py tests/unit/nodes/test_node_search_web_topic.py -q
```

验收：exit code `0`，无新增失败；测试报告能分别证明 ks 与 bili 的目录、字段、错误诊断和链路行为。

### 7.2 全量单元测试

```powershell
.venv/Scripts/python.exe -m pytest tests/unit -q
```

验收：exit code `0`。现有实施记录声称基线约 2883 例，但本次审查没有重新运行测试；应以执行时实际 pytest 汇总为准，并在执行记录中保存总数、失败数和运行环境。

### 7.3 手工验收的硬性 DoD

- [ ] 固定版本与本机 MediaCrawler 工作区状态已核实，无未解释的配置漂移。
- [ ] ks 真实输出目录确认是 `kuaishou/json`。
- [ ] bili 真实输出目录确认是 `bili/json`。
- [ ] ks 和 bili 的真实 JSON fixture 均包含可用 `video_url/title/desc`。
- [ ] 两个平台的 URL 域名验证生效。
- [ ] B站登录 `sys.exit(1)` 场景不会被误报为普通网络故障。
- [ ] 两个平台的关键词搜索路径、异常边界和最小页大小已记录。
- [ ] 单平台成功会停止后续平台；备用平台成功不会触发 Bing。
- [ ] 全链失败后 Bing 只调用一次。
- [ ] 完整有序平台链参与 hash，缓存命中/失效行为正确。
- [ ] CDP/Profile/会话隔离结论与实际运行配置一致。
- [ ] 真实平台执行串行进行，资源预算和运行清理通过。
- [ ] 只有各自九项全部通过的平台才进入生产白名单。
- [ ] 默认配置仍为 `dy`，扩链需要操作员显式配置并重启。

---

## 8. 最终审查意见

原计划可以作为 ks/bili 双平台准入工作的基础，但需要至少完成以下修订后再执行：

1. 将 B站目录预期从 `bilibili` 修正为 `bili`。
2. 将 ks/bili URL 字段明确修正为 `video_url`，不再以 `photo_id`/`bvid` 作为 JSON URL 字段假设。
3. 增加 B站 `sys.exit(1)` 登录失败诊断修复和测试。
4. 解决固定 commit `CDP_CONNECT_EXISTING=True` 与实施记录 `False` 的证据冲突，并修正 Profile 隔离描述。
5. 将 ks/bili 的 20 条最小搜索页大小纳入耗时、资源和验收计划。
6. 扩展测试矩阵，覆盖完整 `xhs → dy → ks → bili → Bing`、ks 单独失败/bili 单独成功、仅 bili 通过等情况。
7. 补充 hostname/平台域名校验，避免仅凭 URL scheme 接受不属于目标平台的链接。

**最终建议：保留“同时启动源码核查、独立准入、默认链不自动扩大”的决策；先修正上述源码事实和诊断缺口，再进入适配器开发。任何一个平台未通过门槛，都只阻止该平台进入白名单，不阻止另一个平台独立验收。**

---

## 9. 主要源码与文档依据

### auto-video-editor（本次审查基准 commit）

- [config.py：白名单、fallback 解析、整链 timeout](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/config.py)
- [mediacrawler_client.py：平台目录映射、子进程、登录错误和输出读取](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/storyline_capabilities/mediacrawler_client.py)
- [search_web_topic.py：平台链、hash、记录映射与回退](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/storyline_capabilities/search_web_topic.py)
- [test_config_mc_platform_chain.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/tests/unit/test_config_mc_platform_chain.py)
- [test_mediacrawler_client.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/tests/unit/test_mediacrawler_client.py)
- [test_storyline_search_web_topic_mc.py](https://github.com/wayyet/auto-video-editor/blob/af96d00433e141095269cb7d289c01a6978ced02/tests/unit/test_storyline_search_web_topic_mc.py)

### MediaCrawler（固定 commit）

- [main.py：CLI 平台代码注册](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/main.py)
- [tools/async_file_writer.py：物理目录拼接规则](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/tools/async_file_writer.py)
- [store/kuaishou/_store_impl.py](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/store/kuaishou/_store_impl.py)
- [store/kuaishou/__init__.py：快手字段与 video_url](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/store/kuaishou/__init__.py)
- [media_platform/kuaishou/core.py：快手关键词搜索路径](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/kuaishou/core.py)
- [store/bilibili/_store_impl.py：B站 JSON Writer 的 platform=bili](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/store/bilibili/_store_impl.py)
- [store/bilibili/__init__.py：B站字段与 av video_url](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/store/bilibili/__init__.py)
- [media_platform/bilibili/core.py：B站搜索路径与登录后状态检查](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/bilibili/core.py)
- [config/bilibili_config.py：BILI_SEARCH_MODE 默认值](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/config/bilibili_config.py)
- [config/base_config.py：CDP_CONNECT_EXISTING 与 USER_DATA_DIR](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/config/base_config.py)
- [tools/cdp_browser.py：连接已有浏览器与启动独立 Profile 的分支](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/tools/cdp_browser.py)
- [LICENSE：NON-COMMERCIAL LEARNING LICENSE 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/LICENSE)

### 项目内相关文档

- [平台回退链设计审查与修订执行计划](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E7%AC%AC%E4%B9%9D%E6%9C%9F%E5%BC%80%E5%8F%91/MediaCrawler%E5%B9%B3%E5%8F%B0%E5%9B%9E%E9%80%80%E9%93%BE_%E8%AE%BE%E8%AE%A1%E5%AE%A1%E6%9F%A5%E4%B8%8E%E4%BF%AE%E8%AE%A2%E6%89%A7%E8%A1%8C%E8%AE%A1%E5%88%92_20261121.md)
- [平台回退链实施执行记录](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E7%AC%AC%E4%B9%9D%E6%9C%9F%E5%BC%80%E5%8F%91/MediaCrawler%E5%B9%B3%E5%8F%B0%E5%9B%9E%E9%80%80%E9%93%BE_%E5%AE%9E%E6%96%BD%E6%89%A7%E8%A1%8C%E8%AE%B0%E5%BD%95_20261121.md)

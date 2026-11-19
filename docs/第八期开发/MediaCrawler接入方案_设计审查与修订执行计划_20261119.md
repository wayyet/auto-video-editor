# 网页话题检索接入 MediaCrawler：设计审查与修订执行计划

> 审查日期：2026-11-19  
> 审查对象：`wayyet/auto-video-editor` 的 `main` 分支  
> 参考方案：[`网页话题检索MediaCrawler接入-分析与计划-20261119.md`](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E7%AC%AC%E5%85%AB%E6%9C%9F%E5%BC%80%E5%8F%91/%E7%BD%91%E9%A1%B5%E8%AF%9D%E9%A2%98%E6%A3%80%E7%B4%A2MediaCrawler%E6%8E%A5%E5%85%A5-%E5%88%86%E6%9E%90%E4%B8%8E%E8%AE%A1%E5%88%92-20261119.md)  
> 本文档性质：代码现状核验、方案正确性审查、缺陷清单及修订后的分阶段实施计划。不是代码已完成声明。

---

## 1. 审查结论

### 1.1 总体判断

**接入方向合理，但原方案目前不能按“实施中途暂停、从步骤 3 直接续做”的状态描述直接执行；必须先修正文档和 CDP/许可设计，再继续能力层与节点层接线。**

原方案选择“MediaCrawler 本地子进程适配器 + `storyline_search_web_topic` 能力层 + Bing 失败兜底”的边界是合理的：MediaCrawler 不需要被复制进主项目，节点仍可输出现有的 `{url,title,snippet,source}` 数据契约，下游文案引用链也可以保持不变。

但审查时发现以下关键差异：

1. 仓库当前 `main` **尚未实现 MediaCrawler provider 分支、Bing 回退和节点级双 hash**。最新提交明确说明了这一点。
2. `config.py` 当前默认引擎是 `bing_html`，不是原方案 §5/§6 所写的 `mediacrawler`。这是有意回拨，应该保留到接线和验收完成后再切换。
3. MediaCrawler 当前默认 `CDP_CONNECT_EXISTING=True`，实际行为是连接已开启远程调试的现有 Chrome，并使用其第一个 BrowserContext；原方案所称的 `<platform>_user_data_dir` 登录态复用并不适用于这个默认运行分支。
4. MediaCrawler 的许可证是 **NON-COMMERCIAL LEARNING LICENSE 1.1**，不只是限制“大规模爬取”。商业用途需要版权方书面同意；每任务 10 条并不能自动解决商业用途限制。
5. 并发访问同一 Chrome/CDP 端口和 BrowserContext 的问题不能留到线上出现后再补，必须在正式启用前实现串行化或独立浏览器隔离。
6. 原方案测试清单尚未落地：当前搜索能力测试文件中没有 MediaCrawler 相关测试，也没有独立的 `test_mediacrawler_client.py`。

### 1.2 当前是否可以启用

**当前结论：不建议启用 `mediacrawler`。**

当前建议：

- 保持 `STORYLINE_WEB_TOPIC_ENGINE=bing_html`，继续使用已存在的 Bing 路径。
- **不要**在当前代码上显式设置 `STORYLINE_WEB_TOPIC_ENGINE=mediacrawler`。目前能力层没有该 provider 分支，会返回 `UNKNOWN_PROVIDER`；当前代码也没有实现文档承诺的自动 Bing 回退。
- 先完成本文第 7 节的 P0/P1 修订项及验收测试，再考虑把默认引擎切换为 MediaCrawler。

### 1.3 审查范围与限制

本次核验读取了 GitHub `main` 分支及 MediaCrawler 上游 `main` 的文件。未直接访问用户电脑上的：

`C:\Users\wayyet\OneDrive\Downloads\开发代码\kuaishou\MediaCrawler`

因此，不能确认本机副本的实际 commit、依赖是否完整、Chrome 是否已开启 CDP、账号登录态是否有效，也没有执行真实扫码和平台检索。涉及本机环境的事项均列为待现场验收，而不是已验证通过。

---

## 2. 审查基线与代码现状

### 2.1 Git 基线

| 项目 | 核验结果 |
|---|---|
| 主仓库 | `wayyet/auto-video-editor` |
| 分支 | `main` |
| 审查时最新 commit | `0545b54e9268407646662245e6835d233f11d4dc` |
| 最新 commit 时间 | `2026-11-19T15:37:57Z` |
| 最新 commit 摘要 | `fix(web-topic): 默认引擎回拨 bing_html —— MC 能力层分支未接线,防 UNKNOWN_PROVIDER 软失败` |
| 参考方案文件 | `docs/第八期开发/网页话题检索MediaCrawler接入-分析与计划-20261119.md` |
| 参考方案文件 blob | `ef9c71b2e456e99300640d308449be8ead031813` |

最新提交说明明确记录：此前提交信息误称能力层 MC 分支/回退/双 hash 已完成；实际没有随提交落库。因此，**最新提交是一次保护性回拨，不是 MediaCrawler 接入完成提交**。

参考链接：

- [auto-video-editor 当前 main commit](https://github.com/wayyet/auto-video-editor/commit/0545b54e9268407646662245e6835d233f11d4dc)
- [参考方案原文](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E7%AC%AC%E5%85%AB%E6%9C%9F%E5%BC%80%E5%8F%91/%E7%BD%91%E9%A1%B5%E8%AF%9D%E9%A2%98%E6%A3%80%E7%B4%A2MediaCrawler%E6%8E%A5%E5%85%A5-%E5%88%86%E6%9E%90%E4%B8%8E%E8%AE%A1%E5%88%92-20261119.md)

### 2.2 auto-video-editor 当前文件状态

| 文件 | 当前核验状态 | 结论 |
|---|---|---|
| `config.py` | 已有 MC 配置 | provider 白名单包含 `mediacrawler`；平台、安装目录、条数、超时、headless 五项 MC 配置已存在；默认引擎实际为 `bing_html`。 |
| `storyline_capabilities/mediacrawler_client.py` | 已存在 | 已实现外部子进程调用、CLI 参数组装、JSON 结果读取和基本错误分类。它只是适配器，不代表能力层已经接线。 |
| `storyline_capabilities/search_web_topic.py` | 未接入 MC | `_PARSER_VERSIONS`、`_PROVIDER_LOCALES`、`_PROVIDER_ENGINE_LABELS` 没有 `mediacrawler`；主入口没有 MC 分支，也没有 `provider_options` 参数。未知 provider 会返回 `UNKNOWN_PROVIDER`。 |
| `nodes/storyline/node_search_web_topic.py` | 未接入 MC | 仍以 `config.STORYLINE_WEB_TOPIC_ENGINE` 作为普通 provider 计算 hash，固定 `max_results=10`，没有传递平台、安装目录、超时、headless 或输出目录参数。 |
| `web/console_service.py` | 未接入 MC 展示 | `_WEB_TOPIC_ENGINE_KEYWORDS` 未包含 MediaCrawler；前端依赖产物 `engine` 字段优先显示引擎。 |
| `tests/unit/test_storyline_capabilities_search_web_topic.py` | 未有 MC 测试 | 当前文件没有 `mediacrawler` / `MediaCrawler` 命中。 |
| `tests/unit/test_web_topic_reference.py` | 下游引用测试存在 | 验证标题、URL、摘要清洗和引用提取；不验证 MC 采集本身。 |

### 2.3 当前真实执行链

当前节点调用路径仍然是：

```text
LangGraph 节点
  nodes/storyline/node_search_web_topic.py
      |
      | provider=config.STORYLINE_WEB_TOPIC_ENGINE
      v
能力层
  storyline_capabilities/search_web_topic.py
      |
      +--> bing_html
      +--> google_html
      +--> duckduckgo_html
      +--> stub
      +--> 其他 provider -> UNKNOWN_PROVIDER
```

MediaCrawler 客户端文件虽已提交，但当前执行链没有调用它：

```text
storyline_capabilities/mediacrawler_client.py
        （当前没有被 search_web_topic.py 接入）
```

这与最新 commit 的提交说明一致。原方案 §6 中“能力层步骤 3 未动、节点层步骤 4 未动”的描述基本正确；但 §6 对 config 默认值和“MC 能力已经接入”的表述需要修订。

---

## 3. 原方案正确性核验

### 3.1 已核实正确的内容

| 原方案内容 | 结论 | 说明 |
|---|---|---|
| 使用独立 MediaCrawler 安装副本，通过子进程执行 `main.py` | 基本正确 | 当前 MC 客户端确实使用其 `.venv` Python、`cwd=project_root`、`main.py`，没有把 MC 源码复制进主仓库。 |
| 使用 `--platform`、`--lt qrcode`、`--type search`、`--keywords` | 正确 | 上游 `cmd_arg/arg.py` 当前 CLI 包含这些参数。 |
| 使用 `--crawler_max_notes_count` 限制采集数量 | 正确 | 上游 CLI 将该参数写入 `config.CRAWLER_MAX_NOTES_COUNT`。 |
| 使用 `--get_comment false`、`--get_sub_comment false`、`--get_media false` | 正确 | 上游 CLI 接受这些字符串布尔参数，并写回配置。关闭评论和媒体下载符合本任务最小化数据采集的目标。 |
| 使用 `--headless 0` 控制非无头模式 | 正确，但解释不完整 | 上游 CLI 会同时设置 Playwright `HEADLESS` 和 `CDP_HEADLESS`；但在默认连接现有 Chrome 的模式下，它不会负责启动一个新的可见浏览器。 |
| 使用 `--save_data_option json` 和独立 `--save_data_path` | 正确 | 上游支持 JSON 文件选项；输出路径结构为 `<save_data_path>/<platform>/json/`，内容文件形如 `search_contents_<日期>.json`。 |
| 使用 JSON 数组读取记录 | 正确 | 上游 `write_single_item_to_json` 读取旧数组、追加记录并写回 JSON 数组。独立运行目录可避免跨任务读取旧结果。 |
| 将 `note_url/title/desc` 映射到 `{url,title,snippet,source}` | 基本正确 | 与主项目下游引用字段兼容，但仍需对 URL scheme、空字段、重复 URL 和跨平台字段差异做校验。 |
| 主项目节点失败使用软失败语义 | 正确 | 现有节点将失败写入 `error_log/status_log`，不应让可选网页检索中断主剪辑流程。 |
| 不应修改 LangGraph 节点/边结构 | 正确 | 接入仅改变现有检索节点内部能力，不需要新增图节点或修改图连线。 |
| 保留 Bing 作为后备引擎 | 合理 | 应作为初期可用性保护措施；但必须把回退路径实际实现并通过测试，不能只在文档中承诺。 |

### 3.2 原方案中不准确或需要补充的内容

#### A. config 默认值描述前后冲突

原方案 §5 步骤 1 表格把 `STORYLINE_WEB_TOPIC_ENGINE` 默认值写为 `mediacrawler`；§6 又声称 config 已将默认值改为 `mediacrawler`。但当前 `config.py` 实际读取：

```python
_raw_web_topic_engine = os.environ.get(
    "STORYLINE_WEB_TOPIC_ENGINE", "bing_html"
).strip()
```

并默认得到 `bing_html`。

这是最新 commit 有意回拨的结果。原方案应改为：**MC provider 接线完成前，默认值保持 `bing_html`；所有接线、测试和真机验收通过后，才单独安排默认值切换。**

#### B. 当前 MediaCrawler 登录态缓存说明与默认 CDP 模式冲突

原方案声称登录态缓存到 MediaCrawler 根目录下的 `<platform>_user_data_dir`，首次运行扫码后后续自动复用。

上游当前 `config/base_config.py` 的默认值是：

```python
ENABLE_CDP_MODE = True
CDP_DEBUG_PORT = 9222
CDP_CONNECT_EXISTING = True
USER_DATA_DIR = "%s_user_data_dir"
```

但当前 `tools/cdp_browser.py` 的执行分支是：

- `CDP_CONNECT_EXISTING=True`：调用 `_connect_existing_browser()`，连接已经启用远程调试的 Chrome/Edge。
- 连接后 `_create_browser_context()` 优先使用 `browser.contexts[0]`，也就是现有浏览器的第一个上下文。
- `USER_DATA_DIR` 路径只在启动新浏览器的 `_launch_browser()` 路径中用于构建 `browser_data/cdp_<...>`；默认连接现有浏览器时不会走该路径。

因此，**在上游默认配置下，登录状态来自用户现有 Chrome/Edge 的浏览器上下文，而不是 MediaCrawler 根目录下独立的 `<platform>_user_data_dir`。**

此外，连接现有浏览器需要用户预先启用远程调试。上游 CDP 使用指南要求在 `chrome://inspect/#remote-debugging` 开启远程调试，并在浏览器弹出的确认框中接受连接。`--headless 0` 不会自动完成这些前置操作。

#### C. `600s` 子进程超时不等于整个登录等待均为 `600s`

适配器 `subprocess.run(timeout=600)` 是外层硬超时，但 MediaCrawler 内部的 `BROWSER_LAUNCH_TIMEOUT` 默认是 `60s`。连接已有浏览器时，内部会等待 CDP 端口可用；连接 CDP 时也会使用该内部超时。

因此：

- Chrome 未启用远程调试，或连接确认框长时间未确认，可能在内部约 60 秒时先失败。
- 外层 600 秒不能覆盖已经在 MediaCrawler 内部触发的 60 秒等待失败。
- 首次登录、远程调试开关、连接确认和平台二维码登录是不同阶段，原方案把它们统称为“600 秒涵盖首次扫码等待”不够准确。

建议将首次 CDP 配置和平台登录从普通工作流任务中拆成一次性预检/初始化步骤，成功后再执行任务级采集。

#### D. “CDP 风控最低”属于上游项目的宣传性描述，不是可保证的技术结论

使用真实浏览器上下文可能复用现有登录状态和浏览器配置，但不代表平台一定无法识别自动化，也不代表不会触发风控。文档中应改成“按上游项目提供的 CDP 模式运行；平台是否允许、是否触发风控取决于目标平台策略和实际行为”，不得使用“风控最低”“保证不封”等承诺性表述。

#### E. JSON 原始文件会保留比业务需要更多的数据

客户端读取的 JSON 是 MediaCrawler 原始记录，而不是只包含 `{url,title,snippet,source}` 的精简记录。小红书记录至少涉及 `nickname`、`title`、`desc`、`note_url`、`source_keyword`，模型对象还包含 `xsec_token` 等字段。

原方案只描述了如何把原始记录映射成四字段，没有定义原始 JSON 文件的保留时间、访问权限、清理机制，也没有明确是否把 `xsec_token` 写入最终 artifact 或日志。建议：

- 下游 artifact 只保留生成脚本确实需要的字段。
- Cookie、登录凭据、`xsec_token` 等不写入日志、状态、前端 payload 或普通引用数据。
- 原始 JSON 采集目录设置明确 TTL；成功映射后按策略删除或加密归档。
- 保留原始数据的目的、期限和权限写入运维说明。

---

## 4. MediaCrawler 上游实现核验

审查时读取的 MediaCrawler 上游 `main` commit：`09ed1ec62d885dab422a53de999f52ac0bd4966b`（2026-11-17）。本机副本是否与此版本一致，仍需现场确认。

### 4.1 CLI 参数核验

上游 `cmd_arg/arg.py` 当前接受以下关键参数：

| 参数 | 上游支持 | 说明 |
|---|---|---|
| `--platform` | 是 | 平台枚举包括 `xhs/dy/ks/bili/wb/tieba/zhihu`。 |
| `--lt` | 是 | 登录方式支持 `qrcode/phone/cookie`。 |
| `--type` | 是 | 包括 `search/detail/creator`。 |
| `--keywords` | 是 | 字符串参数；帮助文本说明多个关键词用英文逗号分隔。 |
| `--crawler_max_notes_count` | 是 | 最大帖子/视频采集数量。 |
| `--get_comment` | 是 | 字符串布尔值。 |
| `--get_sub_comment` | 是 | 字符串布尔值。 |
| `--get_media` | 是 | 字符串布尔值。 |
| `--headless` | 是 | 字符串布尔值，同时覆盖 CDP headless 设置。 |
| `--save_data_option` | 是 | 支持 `json`。 |
| `--save_data_path` | 是 | 自定义结果输出目录。 |

上游 CLI 会把参数值写回全局 `config`，因此当前客户端不需要修改 MediaCrawler 配置文件即可覆盖常用参数。主项目适配器不应传入 `-I` / `-E` Python 隔离参数，因为 MediaCrawler `main.py` 依赖项目目录中的 `config` 等模块导入。

**需要补充的输入校验：** `--keywords` 的英文逗号具有分隔多个关键词的语义。主项目传入的 query 若含英文逗号，可能被 MediaCrawler 按多个关键词处理。需要明确这是允许行为还是需要规整/拒绝，并增加测试；不能未经定义地将任意 query 当作不可分割的单条关键词。

### 4.2 输出路径和格式核验

上游 `tools/async_file_writer.py` 当前路径构造逻辑为：

```text
<save_data_path>/<platform>/<file_type>/
    <crawler_type>_<item_type>_<YYYY-MM-DD>.<file_type>
```

因此搜索内容文件的预期路径为：

```text
<save_data_path>/<platform>/json/search_contents_<YYYY-MM-DD>.json
```

`json` 写入方式是读取当天已有数组、追加记录，再整体写回 JSON 数组。客户端采用每次运行独立目录可避免同一天文件追加导致任务结果串读，这个设计正确。

仍需补充两项防护：

1. `save_root` 和 `project_root` 应在主进程中解析成绝对路径，再传给子进程。当前客户端使用 `cwd=project_root`，若 `save_root` 是相对路径，父进程与子进程会基于不同工作目录解释它，可能造成“子进程写在 A 目录，父进程却从 B 目录读取”的问题。
2. 输出 glob 不能仅凭文件名选取“最后一个”就视为确定性结果。应验证候选文件仅来自当前唯一 `run_dir`、文件大小合理、JSON 结构合法；并将实际输出文件路径纳入诊断但避免把敏感绝对路径暴露到前端。

### 4.3 CDP 和浏览器上下文核验

上游当前默认 `CDP_CONNECT_EXISTING=True`，需要：

1. Chrome/Edge 已启动。
2. 浏览器已经开启远程调试，并监听配置的 CDP 端口（默认 `9222`）。
3. 用户接受浏览器弹出的远程调试确认框。
4. MediaCrawler 连接现有 BrowserContext；若已有 context，则使用第一个 context。

`USER_DATA_DIR` 是“启动新浏览器”路径的 profile 配置，不等价于“连接已有浏览器”路径的 profile 隔离。当前方案应在以下两种设计中明确选择一种：

| 方案 | 行为 | 评估 |
|---|---|---|
| A：连接现有 Chrome（保留原选择） | 用户手动开启 CDP，MediaCrawler 共享现有 Chrome 第一个 context；小红书登录也发生在这个用户 profile 中。 | 更贴近原始决策，但必须提示浏览器上下文权限范围，并且全局串行执行。不能宣称使用独立 MC profile。 |
| B：启动 MediaCrawler 专用浏览器 | 设置 `CDP_CONNECT_EXISTING=False`，由 MediaCrawler 启动自己的 CDP 浏览器和 profile。 | 隔离性更好；需要单独处理首次扫码、profile 生命周期和浏览器启动/退出。不是当前上游默认行为。 |

**建议：**若坚持方案 A，需在 UI/运维说明中明确“将控制用户当前 Chrome 的第一个浏览器上下文”，并在运行期间禁止并发任务使用该 CDP 实例。不要把此模式包装成一个独立的、无副作用的爬虫沙箱。

### 4.4 许可证核验

上游仓库根目录 `LICENSE` 明确标注 `NON-COMMERCIAL LEARNING LICENSE 1.1`，内容包括：

- 授权范围为非商业学习目的下的使用、复制、修改和合并。
- 仅限学习和研究目的。
- 不得大规模爬取或干扰平台运行。
- 未经版权方书面同意，不得用于任何商业用途。
- 软件及其副本应在合理显著位置包含版权声明和许可证声明。

因此，原方案“每任务 1 次、≤10 条、代码不入库，所以符合许可”的论证不充分：低采集量只能说明规模较小，**不能证明用途属于非商业学习/研究，也不能覆盖商业用途禁令**。

在确认该项目只用于个人非商业学习/研究之前，不应把 MediaCrawler 接入正式生产内容流水线；若采集内容会服务于商业账号、广告、客户交付、变现视频或公司业务，应先取得版权方书面授权并完成目标平台条款/数据合规评估。

---

## 5. 问题清单（按优先级）

### P0：启用前必须解决

| 编号 | 问题 | 证据与影响 | 修订要求 |
|---|---|---|---|
| P0-01 | MediaCrawler 许可证用途边界未得到确认 | LICENSE 限定非商业学习/研究并禁止未经书面同意的商业用途。若该能力为商业视频制作或企业业务提供数据，可能超出授权范围。 | 明确实际用途；商业用途先取得版权方书面许可。许可结论未完成前，不进入生产启用阶段。 |

### P1：实现前必须修订

| 编号 | 问题 | 证据与影响 | 修订要求 |
|---|---|---|---|
| P1-01 | 当前 MC provider 未接入 | 最新 commit 说明能力层 MC 分支/回退/双 hash 未落库；当前能力层没有 MC provider 分支。显式启用会 `UNKNOWN_PROVIDER`。 | 完成能力层分支、节点参数传递、request hash 双侧一致、Bing 回退；在验收通过前保持默认 `bing_html`。 |
| P1-02 | 原方案 §5/§6 对默认引擎的描述错误 | 当前 `config.py` 默认值为 `bing_html`；文档仍写成 `mediacrawler` 已默认启用。 | 修订原方案进度表和默认值说明；将默认值切换独立列为最后阶段。 |
| P1-03 | CDP 登录态目录描述不符合默认配置 | 默认 `CDP_CONNECT_EXISTING=True`，实际复用现有 Chrome 第一个 BrowserContext；`USER_DATA_DIR` 不在该分支生效。 | 选择“现有 Chrome”或“专用浏览器”模式；更新扫码流程、 profile 路径和用户提示。 |
| P1-04 | CDP 访问缺乏并发保护 | 多个任务可能同时连同一 `9222` 端口及相同 BrowserContext，页面跳转、登录态、采集上下文会互相干扰。 | 正式启用前加入跨进程锁/Windows 命名互斥量；或改为每任务独立 CDP 端口和独立 profile。初期建议全局串行。 |
| P1-05 | fallback artifact 语义未定义完整 | 回退成功时，`engine`、`provider`、`request_hash`、`fallback` 分别代表“用户请求的引擎”还是“实际产出来源”容易混淆；双失败时也需同时保留两端错误。 | 定义 requested/resolved provider；保存 MC 主请求错误和 Bing 回退错误；明确 hash 按请求策略还是实际解析来源计算。 |
| P1-06 | MC 原始 JSON 数据保留和敏感字段处理缺失 | 原始记录可能包含昵称、平台 token 等非下游必需信息，目录目前没有 TTL/清理设计。 | artifact 仅输出白名单字段；日志脱敏；定义原始数据 TTL 与目录权限；不将 token 写进 UI/脚本 references。 |
| P1-07 | 测试未落地 | 当前搜索能力测试文件无 MC 相关用例；没有客户端独立测试文件。 | 在切换默认值前补齐客户端、能力层、节点缓存、fallback、控制台展示和并发测试。 |
| P1-08 | `600s` 超时说明不准确 | 上游 CDP 内部 `BROWSER_LAUNCH_TIMEOUT` 默认 60 秒，连接可能先于外层 600 秒超时失败。 | 把 CDP 预配置/确认作为单独预检；外层 600 秒仅作为子进程最大运行时间，不承诺覆盖所有人工等待。 |

### P2：实施中应修复

| 编号 | 问题 | 影响 | 修订要求 |
|---|---|---|---|
| P2-01 | MC 上游版本没有固定 | 上游命令参数、输出字段和签名实现变化可能导致适配失效；固定 `mc_{platform}_v1` 无法体现上游版本变化。 | 记录并固定上游 commit/tag；将 MC commit 或适配器版本纳入 `parser_version` / `request_hash`。 |
| P2-02 | `config.py` 对整数环境变量只有 `int()` 转换 | 非数字导致应用导入失败；负数、过大条数或超时没有业务范围校验。 | 对 `MAX_NOTES`、`TIMEOUT` 设置明确范围和错误提示；启动预检时验证项目路径和 Python 可执行文件。 |
| P2-03 | 相对 `save_root` 可能出现父子进程路径解释不一致 | 主进程基于工作目录计算相对路径，子进程以 `cwd=project_root` 运行。 | `project_root`、`save_root` 在客户端开始时统一 `expanduser().resolve()`。 |
| P2-04 | 平台字段映射假定所有平台共用小红书字段 | 不同平台的帖子 URL、正文摘要字段可能不同，导致空 URL 或空摘要。 | 第一阶段只开放 `xhs`；其他平台通过独立 fixture 和真机测试后逐个开放。 |
| P2-05 | 原始 query 的英文逗号语义未定义 | MediaCrawler 把英文逗号解释为多个关键词，可能改变检索含义。 | 在 query 构造层定义单关键词/多关键词规则并测试。 |
| P2-06 | diagnostics 可能暴露本机路径或外部输出内容 | `project_root`、`run_dir`、stdout/stderr 会进入产物，可能暴露用户名、文件结构或平台数据。 | 生产 artifact 只留相对路径/运行 ID、退出码和脱敏错误摘要；完整调试信息仅本地受限日志。 |
| P2-07 | 控制台没有回退状态展示定义 | artifact 记录 `fallback` 不代表控制台会展示，使用者可能误认为结果来自小红书。 | `_web_topic_section` 返回 fallback 摘要；UI 明确显示“MediaCrawler 失败，已回退 Bing”。 |
| P2-08 | 缺少原始结果文件清理及磁盘容量监控 | 长期运行会留下每次任务的 JSON 目录。 | 设置按任务/日期清理策略，并记录清理结果；确保不会删除仍被引用的诊断文件。 |
| P2-09 | 子进程超时后的浏览器进程树清理未定义 | 如果后续选择 `CDP_CONNECT_EXISTING=False` 启动独立 Chrome，外层 `subprocess.run` 超时终止 Python 进程不等同于保证清理其派生浏览器进程，可能留下孤儿 Chrome 或占用 profile。 | 若启用“启动新浏览器”模式，增加 Windows Job Object/进程树终止策略并测试超时清理；若继续使用现有 Chrome 模式，也要验证不会误关闭用户浏览器。 |

---

## 6. 修订后的目标架构

### 6.1 组件职责

```text
LangGraph
  storyline_search_web_topic 节点
       |
       | 读取 engine / platform / timeout / max_notes
       | 按 provider + platform + parser version 生成缓存 hash
       v
storyline_capabilities.search_web_topic
       |
       +-- provider == mediacrawler
       |       |
       |       v
       |   mediacrawler_client.search_notes()
       |       |
       |       v
       |   本地 MediaCrawler 子进程
       |       |
       |       v
       |   JSON 原始记录 -> 字段校验/清洗/去重/URL 验证
       |       |
       |       +-- 有效记录 > 0：返回 MediaCrawler 结果
       |       |
       |       +-- 失败 / 0 条 / 清洗后 0 条
       |                    |
       |                    v
       |                Bing HTML 回退
       |
       +-- provider == bing_html / google_html / duckduckgo_html / stub
               保留现有执行路径
       |
       v
search_web_topic.json（统一 artifact 契约）
       |
       +--> console_service / AI 内容明细
       +--> generate_script 的 web_research 引用
       +--> script.json references
```

### 6.2 Artifact 字段语义

建议在现有 schema v2 基础上增加明确的来源与回退字段，不改变下游四字段条目契约：

| 字段 | 定义 |
|---|---|
| `provider` | 实际产生 `search_web_topic` 结果的 provider，例如 `mediacrawler` 或 `bing_html`。 |
| `requested_provider` | 节点配置的首选 provider，例如 `mediacrawler`。 |
| `engine` | 控制台展示名；成功用 MC 时为 `MediaCrawler(小红书)`，回退成功时为 `Bing（MediaCrawler 回退）`。 |
| `fallback` | 未回退时为 `null`/不出现；回退时记录 `from_provider`、`from_platform`、`to_provider`、`primary_error_code`、`primary_message`。 |
| `fallback_error` | 仅当 Bing 回退也失败时记录其 `error_code` 和安全摘要；不得只保留 Bing 错误而丢失 MC 原始错误。 |
| `request_hash` | 对“首选检索策略”进行缓存标识，包含 `mediacrawler:<platform>`、query、数量上限、MC 适配器/上游版本和 schema 版本；回退成功仍保留该首选策略 hash。 |
| `diagnostics` | 分别保存 primary 与 fallback 的退出码、解析器版本和耗时摘要；不保存 Cookie/token，不把完整绝对路径暴露到前端。 |
| `search_web_topic[]` | 保持 `{url,title,snippet,source}` 四字段；`source` 必须表示实际数据来源。 |

**缓存规则：**同一个 query 在 `mediacrawler:xhs` 和 `mediacrawler:wb` 下必须得到不同 hash；MediaCrawler 上游版本或字段映射规则变化时，也必须通过 parser/version 使旧缓存失效。

### 6.3 结果有效性判定

MC 客户端原始结果非空，不代表能交给下游。能力层映射后至少满足：

- `url` 是合法 `http` / `https` URL。
- `title` 清洗后非空。
- `snippet` 可为空，但必须有长度上限。
- 同 URL / 同标题按明确规则去重。
- 最终有效条数为 0 时，按 `NO_RESULTS` 触发 Bing 回退。
- `source` 为 `mediacrawler`，不能因为当前 engine 配置或缓存值而伪造来源。

### 6.4 回退状态矩阵

| MediaCrawler | Bing | 最终 artifact 状态 | 说明 |
|---|---|---|---|
| 有有效结果 | 不调用 | `ok` | `provider=mediacrawler`。 |
| 网络失败/超时/解析失败 | 有有效结果 | `ok` | `provider=bing_html`，`requested_provider=mediacrawler`，有 `fallback` 记录。 |
| 0 条或清洗后 0 条 | 有有效结果 | `ok` | 同上，`primary_error_code=NO_RESULTS`。 |
| 失败 | 0 条 | `no_results` | 同时保留 MC 与 Bing 诊断。 |
| 失败 | 失败 | `failed` | `fallback_error` 明确记录 Bing 失败原因，节点继续软失败。 |

不建议把“MC 回退 Bing 成功”标为节点 `failed`；因为下游实际取得了可用结果。但必须让控制台显示发生过回退，避免误读来源。

---

## 7. 修订后的分阶段执行计划

> 依赖顺序：阶段 0 → 1 → 2 → 3 → 4 → 5 → 6。任何阶段未通过验收，不进入默认引擎切换。

### 阶段 0：许可与使用边界确认

**目标：**确认 MediaCrawler 的许可证允许当前预期用途。

| 任务 | 操作 | 验收标准 |
|---|---|---|
| 0.1 | 明确检索数据是否仅用于个人学习/研究，还是用于商业内容制作、客户交付、企业运营或变现账号。 | 有书面用途说明。 |
| 0.2 | 如涉及商业用途，向 MediaCrawler 版权方申请书面授权。 | 获得可保存的书面授权；未取得前不接入生产工作流。 |
| 0.3 | 核对小红书等目标平台的服务条款、数据使用边界及账号风险。 | 完成使用边界记录；不得将“公开可见”直接等同于可任意采集/再利用。 |
| 0.4 | 确认是否需要在项目文档/发行包中保留许可证声明。 | 按 LICENSE 的版权声明要求处理。 |

**阶段门禁：**P0-01 未解决，不进入正式接入实施。可以继续做不调用真实平台的模拟适配器单测，但不得开启真实采集。

### 阶段 1：修订设计文档与配置保护

**目标：**修正事实，保证当前主流程不受影响。

涉及文件：

- `docs/第八期开发/网页话题检索MediaCrawler接入-分析与计划-20261119.md`
- `config.py`

任务：

1. 修正原方案 §5/§6 中默认引擎和实施状态描述。
2. 明确接入完成前 `STORYLINE_WEB_TOPIC_ENGINE` 默认 `bing_html`。
3. 确保 MC 的五个环境配置仍然存在：平台、安装根目录、最大条数、超时、headless。
4. 为 `STORYLINE_WEB_TOPIC_MC_MAX_NOTES` 和 `STORYLINE_WEB_TOPIC_MC_TIMEOUT` 增加范围校验；建议首期 `MAX_NOTES` 设为 `1..10`，超时设为 `30..900` 秒，超范围在启动时给出明确错误。
5. 启动预检应能区分“路径不存在”“`main.py` 不存在”“venv Python 不存在”，但不能因此在 MC 尚未启用时阻断 Bing 工作流。

验收：不设置任何 MC 环境变量时，现有 Bing 单测和节点行为不变；配置非法时得到可读错误，不出现难以定位的 `ValueError` 堆栈。

### 阶段 2：加固 `mediacrawler_client.py`

文件：`storyline_capabilities/mediacrawler_client.py`

任务：

1. 对 `project_root`、`save_root` 做 `expanduser().resolve()`，并验证 `main.py`、`.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Linux/macOS）存在。
2. 固定 MediaCrawler 兼容版本：本机确认 commit 后，将 `mc_revision` 纳入 diagnostics 和 parser/request version；不依赖不断变化的上游 `main`。
3. 所有子进程参数使用 list 传入，不经过 shell；保留 `stdin=DEVNULL`、UTF-8、`errors=replace`、硬超时。
4. 明确 `--keywords` 英文逗号语义。首期如果不支持一个 query 内多关键词，输入中出现英文逗号时应规范化或返回明确的参数错误。
5. 解析当前 `run_dir` 内唯一输出文件；校验 JSON 列表结构、最大文件大小、记录数量上限和字段类型。
6. 对异常分类保持稳定：启动失败/超时/非零退出为 `NETWORK_ERROR`；格式错误为 `PARSE_ERROR`；空结果为 `NO_RESULTS`。
7. `stdout/stderr` 只截取有限尾部，并对 Cookie、token、账号信息、绝对用户目录进行脱敏；不把其原文直接写入 UI。
8. 定义原始 JSON 数据的 TTL/清理策略。至少不要将 MC 原始记录作为长期无期限缓存。
9. 增加任务级互斥：默认现有 Chrome/CDP 模式下，同一机器同一 CDP 端口只允许一个 MC 任务运行。

验收：客户端单测覆盖参数、路径、超时、异常退出、缺失文件、JSON 损坏、数组/包装对象、空数组、超量记录和并发锁；测试不得访问真实社交平台。

### 阶段 3：接入能力层 provider 和 fallback

文件：`storyline_capabilities/search_web_topic.py`

任务：

1. `_PARSER_VERSIONS`、`_PROVIDER_LOCALES`、`_PROVIDER_ENGINE_LABELS` 增加 MC 处理；动态平台 parser/version 必须可稳定计算。
2. `search_web_topic()` 增加可选 `provider_options` 参数，保持 `media_dir` 等现有参数不丢失。
3. 增加 `mediacrawler_request_provider(platform, mc_revision)` 或等价 helper，统一节点层与能力层 hash 语义。
4. 新增 `_map_mc_records()`：字段映射、清洗、URL scheme 校验、去重、数量限制；不把原始 MC 记录直接交给下游。
5. `mediacrawler` 分支调用 `_mc_client.search_notes()`；映射后有效结果大于 0 才视为 MC 成功。
6. MC 失败或有效结果为 0 时调用现有 `_bing_html_search()`；不要重复实现 Bing HTTP/解析逻辑。
7. fallback 结果的 artifact 必须符合第 6.2 节字段语义；双失败保留两端错误。
8. `request_hash` 以首选策略为准，包含平台与版本；不能让节点缓存 hash 与能力层产物 hash 不一致。
9. 确保 `download=False` 的既有默认行为不被 MC 分支意外改变。

验收：能力层可用 fake client 验证全部 MC 成功、MC 空结果回退、MC 异常回退、Bing 空结果、双失败、URL 无效、平台 hash 隔离及版本变更 cache miss。

### 阶段 4：接入节点配置、缓存和软失败

文件：`nodes/storyline/node_search_web_topic.py`

任务：

1. `engine != mediacrawler` 时保留当前行为，`max_results=10` 不变。
2. `engine == mediacrawler` 时从 config 构造 `provider_options`：`platform/project_root/timeout_seconds/headless/save_root`。
3. MC 检索条数从 `STORYLINE_WEB_TOPIC_MC_MAX_NOTES` 读取；节点 cache hash 使用相同平台、条数和版本参数。
4. 仅在 request hash 一致时复用缓存；切换平台或 MC 版本后必须 miss。
5. `web_topic_force_refresh` 仍可跳过缓存。
6. MC 子进程超时、CDP 不可用和登录失败均不得阻断主工作流；但要在 `error_log` 或 artifact 中给出明确可定位原因。
7. 不要在节点中吞掉 fallback 元数据；成功回退时节点状态应是检索成功，但留下 warning/fallback 信息。

验收：节点测试分别验证 MC 正常、MC 回退、双失败、缓存命中、平台切换 cache miss、force refresh 和软失败语义。

### 阶段 5：控制台显示实际来源与回退状态

文件：`web/console_service.py`；必要时调整 `web/static/ai_content.mjs` 或对应 HTML 渲染器。

任务：

1. `_WEB_TOPIC_ENGINE_KEYWORDS` 增加 MediaCrawler，兼容没有 `engine` 的历史产物。
2. `_web_topic_section()` 将 fallback 状态、安全错误摘要及实际来源传给前端。
3. 成功使用 MC 时显示 `MediaCrawler(小红书)`。
4. MC 回退 Bing 时显示 `Bing（MediaCrawler 回退）`，并能查看回退原因；不能只显示 `Bing` 而完全隐藏 MC 失败。
5. `entries` 只输出精简后的标题、URL、摘要和来源；不得输出 token、Cookie、原始诊断尾部或本机绝对路径。

验收：前端测试覆盖 MC 成功、fallback 成功、双失败、历史产物和未知 provider。

### 阶段 6：集成测试、真机验收与默认值切换

#### 6.1 单元测试

新增或扩展：

- `tests/unit/test_mediacrawler_client.py`
- `tests/unit/test_storyline_capabilities_search_web_topic.py`
- `tests/unit/test_web_topic_reference.py`（只在下游引用契约变化时扩展）
- 节点测试文件（按仓库当前测试组织方式选定）
- `web/tests` 中的 AI 内容明细测试

必测项目：

- CLI 参数正确、`cwd` 正确、父子进程路径一致。
- Python 可执行文件缺失、main.py 缺失、超时、非零退出。
- 输出文件缺失、JSON 损坏、非数组结构、空数组、超条数。
- URL 非 HTTP(S)、空标题、空摘要、重复记录、平台字段差异。
- MC 成功、MC 无结果回退 Bing、MC 各类失败回退 Bing、双失败。
- `mediacrawler:xhs` 与 `mediacrawler:wb` hash 不同。
- MC 上游 revision 或 parser version 变化造成 hash 变化。
- fallback 成功 artifact 的 `requested_provider/resolved provider/engine/source/request_hash` 语义一致。
- 节点缓存、强制刷新、失败软降级及状态标签。
- 并发任务被锁串行化，锁释放对超时/异常同样有效。
- 敏感信息脱敏和原始 JSON TTL 清理。

#### 6.2 真机前置检查

1. 在 MediaCrawler 目录读取 `git rev-parse HEAD`，确认本机 revision 与项目声明的兼容版本一致。
2. 确认 `.venv` Python 能启动 `main.py --help`。
3. 确认 Chrome 版本满足当前上游 CDP 使用要求。
4. 确认 `chrome://inspect/#remote-debugging` 已开启远程调试，端口与 `CDP_DEBUG_PORT` 一致。
5. 确认用户理解：当前默认 CDP 连接的是已有浏览器的第一个 context，不是隔离的 MediaCrawler profile。
6. 先在 MediaCrawler 目录单独执行一次单平台、单关键词、最多 10 条的命令，确认登录和结果写盘。
7. 核验采集结果不包含评论和媒体下载，且没有将 token/Cookie 写入应用日志。

#### 6.3 真机工作流验收

| 场景 | 操作 | 预期 |
|---|---|---|
| MC 成功 | `STORYLINE_WEB_TOPIC_ENGINE=mediacrawler`，平台 `xhs`，单个 query，最多 10 条 | 01 节点产物有有效小红书 URL；控制台显示 `MediaCrawler(小红书)`；03 文案能引用精简结果。 |
| MC 未安装 | 临时指定不存在的 `PROJECT_ROOT` | MC 失败后调用 Bing；artifact 标记 fallback；控制台显示回退。 |
| CDP 未开启 | 关闭浏览器远程调试后执行 | 不阻断主工作流；错误原因可见；Bing 回退成功。 |
| MC 无结果 | 使用无匹配关键词 | 自动回退 Bing；不能把 MC 空列表当作 MC 成功。 |
| 双失败 | 令 MC 失败且 Bing 不可用 | 节点软失败；主剪辑链继续；artifact/error_log 保留两侧原因。 |
| 缓存 | 相同 query、platform、配置重复进入 QA 回路 | 命中缓存，不重复启动 MediaCrawler。 |
| 平台切换 | `xhs` 切换到另一个已验收平台 | hash 变化，不复用旧平台缓存。 |
| 并发 | 同时创建两个需要 MC 的任务 | 后启动任务等待锁或收到明确忙碌状态；不并发控制同一 Chrome context。 |

#### 6.4 默认值切换门禁

只有全部满足以下条件才允许把默认值从 `bing_html` 改成 `mediacrawler`：

- P0-01 许可门禁通过。
- 阶段 2 至阶段 5 的代码完成并评审。
- 所有 MC 单元测试、节点测试和控制台测试通过。
- 真机成功、MC 失败回退、双失败软降级三条路径均通过。
- CDP 并发锁和登录态隔离/共享说明已落实。
- 生产运维人员明确知道如何回滚。

默认切换应作为单独提交，不要与能力层大规模重构混在同一提交中。

---

## 8. 回滚方案

当前仓库默认本身就是 `bing_html`。完成接入后，如需回滚：

1. 将 `STORYLINE_WEB_TOPIC_ENGINE` 显式设为 `bing_html`。
2. 重启实际运行 LangGraph 的服务/Worker 进程，因为 `config.py` 在 import 时读取环境变量。
3. 保留 MediaCrawler 客户端文件和 MC 配置，不要求立即删除；不再触发 MC 子进程。
4. 核验 `search_web_topic.json` 中 `provider=bing_html`、`engine=Bing`，并验证控制台与文案引用正常。

回滚不得依赖“只修改正在运行进程的环境变量”，也不得假设 Worker 会自动重新 import config。

---

## 9. 建议修订原方案文档的具体位置

| 原方案位置 | 必须修订内容 |
|---|---|
| §2 决策记录 | 把“可见浏览器”与“连接现有 Chrome”拆开说明；补充远程调试预配置和 BrowserContext 共享范围。 |
| §3.4 登录与反爬 | 修正 `<platform>_user_data_dir` 与当前 CDP 默认配置的关系；加入 60 秒内部 CDP 超时限制。 |
| §3.5 集成选型 | 可以保留子进程方案，但需增加版本固定、绝对路径、互斥、数据清理要求。 |
| §5 步骤 1 | `STORYLINE_WEB_TOPIC_ENGINE` 默认值暂时写 `bing_html`，最后单独切换。 |
| §5 步骤 2 | 补充绝对路径、版本探测、数据最小化、跨进程锁和超时分层。 |
| §5 步骤 3 | 定义 requested/resolved provider、双侧错误信息及 fallback artifact schema。 |
| §5 步骤 4 | 明确节点 hash 按 platform、数量和版本计算；MC 不可与固定 `10` 的普通 provider 路径混用。 |
| §5 步骤 5 | 控制台不仅增加名称关键字，还要显示实际来源和 fallback 摘要。 |
| §5 步骤 6 | 将 client/能力层/节点/控制台/并发/脱敏测试列为必测，不能只在现有测试文件中追加少量 happy path。 |
| §6 当前进度 | 当前状态应准确写为：client/config 已提交；默认 provider 为 Bing；能力层、节点、控制台、测试和真机验证未完成。 |
| §7 验证方案 | 把 Chrome CDP 预检从“首跑自动扫码”拆分；明确 60 秒内部 CDP 等待与 600 秒外层进程超时的区别。 |
| §8 风险与注记 | 许可限制、CDP context 共享、并发锁、原始 JSON 留存和版本固定列为启用前事项，不再写成发生问题后再处理。 |

---

## 10. 最终建议

**推荐按“保留 Bing 默认值、先修正接入设计、再完成 MC 适配与测试、最后单独切换默认值”的路线执行。**

这条路线兼顾现有工作流稳定性，也能避免当前代码在启用 `mediacrawler` 后出现 `UNKNOWN_PROVIDER` 却没有 Bing 回退的情况。

执行顺序应为：

1. 先处理许可证用途门禁和原方案文档校订。
2. 加固 `mediacrawler_client.py`（版本、CDP、路径、并发、数据最小化）。
3. 完成能力层 MC provider 和 Bing fallback。
4. 完成节点缓存 hash 和配置传递。
5. 完成 console 的实际来源/回退提示。
6. 补齐自动化测试和真机验收。
7. 最后独立提交默认引擎切换；如验收不通过则维持 `bing_html`。

---

## 11. 参考来源

### auto-video-editor

- [main 最新提交 `0545b54`](https://github.com/wayyet/auto-video-editor/commit/0545b54e9268407646662245e6835d233f11d4dc)
- [原始接入方案](https://github.com/wayyet/auto-video-editor/blob/main/docs/%E7%AC%AC%E5%85%AB%E6%9C%9F%E5%BC%80%E5%8F%91/%E7%BD%91%E9%A1%B5%E8%AF%9D%E9%A2%98%E6%A3%80%E7%B4%A2MediaCrawler%E6%8E%A5%E5%85%A5-%E5%88%86%E6%9E%90%E4%B8%8E%E8%AE%A1%E5%88%92-20261119.md)
- [`config.py`](https://github.com/wayyet/auto-video-editor/blob/main/config.py)
- [`storyline_capabilities/mediacrawler_client.py`](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/mediacrawler_client.py)
- [`storyline_capabilities/search_web_topic.py`](https://github.com/wayyet/auto-video-editor/blob/main/storyline_capabilities/search_web_topic.py)
- [`nodes/storyline/node_search_web_topic.py`](https://github.com/wayyet/auto-video-editor/blob/main/nodes/storyline/node_search_web_topic.py)
- [`web/console_service.py`](https://github.com/wayyet/auto-video-editor/blob/main/web/console_service.py)
- [`tests/unit/test_storyline_capabilities_search_web_topic.py`](https://github.com/wayyet/auto-video-editor/blob/main/tests/unit/test_storyline_capabilities_search_web_topic.py)

### MediaCrawler 上游

- [MediaCrawler 上游 main](https://github.com/NanmiCoder/MediaCrawler/tree/main)（本次审查 commit：`09ed1ec62d885dab422a53de999f52ac0bd4966b`）
- [LICENSE](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)
- [`cmd_arg/arg.py`](https://github.com/NanmiCoder/MediaCrawler/blob/main/cmd_arg/arg.py)
- [`config/base_config.py`](https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py)
- [`tools/async_file_writer.py`](https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/async_file_writer.py)
- [`tools/cdp_browser.py`](https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/cdp_browser.py)
- [`docs/CDP模式使用指南.md`](https://github.com/NanmiCoder/MediaCrawler/blob/main/docs/CDP%E6%A8%A1%E5%BC%8F%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.md)

> 说明：上游 `main` 是移动分支。实际部署时应以本机 MediaCrawler 的 Git commit 为准，并把该 commit 固定到项目兼容性记录中。

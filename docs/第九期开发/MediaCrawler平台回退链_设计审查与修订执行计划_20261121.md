# MediaCrawler 平台回退链：设计审查、问题清单与修订执行计划

> 审查日期：2026-11-21  
> 项目：[`wayyet/auto-video-editor`](https://github.com/wayyet/auto-video-editor)  
> 分支：`main`  
> 本次核查的项目提交：`e1ec07608bb1da2160234ab91cb8a0d0b133726b`  
> 被审查文档：`docs/第九期开发/网页话题检索MediaCrawler回退根因分析与平台回退链实施计划_console-654141c9890e_20261121.md`  
> 被审查文档 Blob SHA：`8c919c96d63a9e8fe9a221b34abb386b8746621b`  
> MediaCrawler 外部副本核查版本：`09ed1ec62d885dab422a53de999f52ac0bd4966b`（短 SHA：`09ed1ec62d88`）

---

## 0. 审查结论

### 0.1 总体结论

**原方案的业务方向合理，但当前版本不应直接进入实施。建议先按本文修订，再交给代码 Agent 执行。**

目标链路保持为：

```text
MediaCrawler(xhs / 小红书)
        |
        | 主平台失败、无有效结果或相关性不达标
        v
MediaCrawler(dy / 抖音)
        |
        | 第二平台失败、无有效结果或相关性不达标
        v
Bing HTML
        |
        v
返回可用结果，或按现有语义返回 no_results / failed
```

首期只验证并开放 `xhs → dy → Bing`。后续平台按“内容相关度 × 稳健性”推荐顺序 `ks → bili → wb → zhihu` 逐个验证、逐个加入；`tieba` 不建议进入默认回退链。平台矩阵与准入门槛见 §3.8。

### 0.2 审查等级

| 等级 | 含义 | 本次结论 |
|---|---|---|
| P0 | 合规或授权前置阻断 | MediaCrawler 许可需先确认；商业用途未经书面许可不得上线 |
| P1 | 会导致功能无法工作或缓存/运行时错误 | 发现 5 项，必须修复 |
| P2 | 证据、观测性、测试或文档准确性问题 | 发现多项，实施前应纳入计划 |
| P3 | 可后续优化 | 增加其他平台、独立日志分析工具等 |

### 0.3 已确认的当前项目状态

项目 `main` 分支当前实现如下：

| 项目 | 当前实现 | 对平台回退的影响 |
|---|---|---|
| MediaCrawler 白名单 | `config.py` 中 `_MC_PLATFORM_WHITELIST = ("xhs",)` | `dy` 尚未允许 |
| 适配器执行 | `mediacrawler_client.search_notes()` 接受 `platform` 参数，并将其传给 `--platform` | 基础执行入口可复用 |
| 平台标签 | `MC_PLATFORM_LABELS` 已包含 `dy: 抖音` | 不需要重复添加标签 |
| 结果映射 | `_map_mc_records()` 只读取 `note_url` / `url` | 需要增加 `aweme_url` |
| 结果目录 | 适配器按 `<run_dir>/<platform>/json` 读取 | 与抖音实际输出目录不一致，必须修复 |
| 能力层 | 每次只运行一个 MC 平台，失败后直接 Bing | 需要增加有序循环 |
| 节点层 | 没有传递平台回退列表 | 需要新增配置透传 |
| 缓存 | Hash 只包含主平台及 MC revision | 必须加入完整平台链 |
| 超时 | `STORYLINE_WEB_TOPIC_MC_TIMEOUT` 是单个 MC 子进程的超时 | 不能直接作为每个平台的总预算 |

---

## 1. 审查范围、证据边界和复验原则

### 1.1 本次检查范围

1. `wayyet/auto-video-editor` 的 `main` 分支：配置、能力层、MC 适配器、节点、单元测试和项目文档。
2. 原方案引用的 MediaCrawler 外部副本：按其记录的固定版本 `09ed1ec62d885dab422a53de999f52ac0bd4966b` 核对，不以外部仓库当前 `main` 的浮动代码代替。
3. 原方案中的故障分析、平台能力判断、回退设计、缓存策略、测试和上线步骤。

### 1.2 必须区分的三类证据

| 证据类别 | 本次是否能独立复验 | 使用方式 |
|---|---|---|
| auto-video-editor GitHub 源码 | 可以 | 作为当前项目代码现状的直接依据 |
| MediaCrawler 固定 commit 源码 | 可以 | 作为上游调用路径、输出目录和许可的依据 |
| 本地 `outputs/console-654141c9890e`、stderr、mtime、浏览器会话和本地 IP | 不可以 | 原文提供了数值和解释，但本次没有访问用户本机文件，因此只能标记为“文档陈述，待本机复核” |

因此，原文关于“本次实际任务运行时间”“同一账号/IP 的 14 分钟间隔”“stderr 的完整内容”“页面时区错误”的结论，不应被描述为本次审查已经重新复现。

### 1.3 外部依赖版本纪律

MediaCrawler 在项目之外运行，且 `config.STORYLINE_WEB_TOPIC_MC_PROJECT_ROOT` 指向本地目录。项目代码和外部副本不是同一个 Git 工作树。

实施前必须核对：

```powershell
git -C "<MediaCrawler项目目录>" rev-parse HEAD
```

期望短 SHA：

```text
09ed1ec62d88
```

若实际 SHA 不一致，应重新核对输出目录、JSON 字段、登录方式、调用路径和许可文件，不得直接套用本文针对该版本的结论。

---

## 2. 原故障根因分析的正确性审查

### 2.1 可以由固定版本源码支持的判断

| 原文判断 | 审查结果 | 说明 |
|---|---|---|
| XHS 关键词搜索会并发抓取详情 | 基本正确 | `media_platform/xhs/core.py` 的 `search()` 创建详情任务，并通过 `asyncio.gather()` 汇总 |
| API 详情接口返回空字典时会进入 HTML 详情路径 | 正确 | `client.py:get_note_by_id()` 在没有 `items` 时返回 `dict()`；`core.py` 随后执行 HTML 路径 |
| HTML 详情函数使用 tenacity 重试 | 正确 | `get_note_by_id_from_html()` 有 `stop_after_attempt(3)` 的重试装饰器 |
| HTML 解析包含 `replace("undefined", '""')` | 正确 | `extractor.py` 先正则提取，再无差别替换，最后 `json.loads()` |
| HTML 路径的 `RetryError` 可能逃出详情任务 | 正确 | `core.py` 内层 `except RetryError` 只包围 API 路径；HTML 路径在该内层捕获范围之外，外层异常列表也未捕获 `RetryError` |
| 适配器把非零退出码归为 `NETWORK_ERROR` | 正确 | 当前 `mediacrawler_client.search_notes()` 对子进程非零退出统一返回 `NETWORK_ERROR`，它不是精确的网络根因分类 |

### 2.2 原文中需要修正的判断

#### A. XHS 外层异常豁免名单写得不完整

原文称异常豁免名单只有：

- `NoteNotFoundError`
- `IPBlockError`
- `PlatformAccessError`

但固定版本 `media_platform/xhs/core.py` 的 `get_note_detail_async_task()` 还包含：

- `DataFetchError`
- `KeyError`

因此原文列举不完整。真正影响本次推断的关键点仍然成立：HTML 详情解析产生的 `RetryError` 没有被外层异常捕获。

#### B. `noteDetailMap` 存在不等于“不是验证码页”

固定版本 `extractor.py` 的注释说明：缺少 `noteDetailMap` 可能是验证码页，也可能是笔记不存在。

反过来，检测到 `noteDetailMap` 只能证明 HTML 包含该字符串，不能证明页面结构完整、状态数据有效或一定是正常详情页。原文“说明本次拿到的不是验证码页”的结论过强，应改成：

> 本次页面包含 `noteDetailMap` 字符串，但页面是否为正常详情页仍需结合 HTTP 状态、页面特征、正则匹配结果和解析异常确认。

#### C. “高频风控”仍属于假设，不是定案证据

同一账号/IP 前一次成功，14 分钟后详情失败，最多说明两次请求表现不同。

单凭一次成功与一次失败，无法区分：

- 账号或 IP 触发频率限制；
- XHS 返回了新的页面结构；
- 页面数据不完整；
- HTML 状态数据本身不符合 JSON 语法；
- 其他瞬时服务端异常。

建议将“高频风控”标记为主嫌疑，而不是确定根因。后续需通过低频对照、HTTP 状态/页面特征、异常类型和脱敏诊断记录提高证据强度。不得以更换账号、出口轮换或验证码规避作为默认修复方式。

#### D. `NETWORK_ERROR` 不能被解释为“网络故障已证实”

当前适配器将所有非零退出码归类为 `NETWORK_ERROR`。但进程非零退出也可能来自 JSON 解析异常、程序 bug、运行环境异常或登录失败。

原文已经从 stderr 推断 JSON 解析失败，说明真实故障并非一定是网络层问题。文档应把它描述为“适配器的粗粒度错误分类”，不应将该错误码本身当作网络故障证据。

#### E. 原计划并未真正解决原文声称的观测性缺口

原文 §6.2 表示，§8 完成后后续失败可定位到具体笔记与页面内容。但 §8 主要修改平台循环、URL 映射、节点透传和测试，没有改造 MediaCrawler 内部日志，也没有增加具体笔记级的解析诊断。

仅增加 `attempts` 平台级列表，只能知道 `xhs` 或 `dy` 哪一次失败，不能知道具体 `note_id`、解析步骤和页面结构。

因此，原文关于“直接定位到具体笔记与页面内容”的承诺不成立。应另设受控观测性任务：记录平台、运行 ID、脱敏后的内容 ID 哈希、HTTP 状态、解析步骤、异常类型和匹配状态；不要默认保存包含 Cookie、token 或个人数据的完整页面 HTML。

### 2.3 本机运行证据的复核要求

原文中的下列字段本次无法访问本机文件复验：

- `outputs/console-654141c9890e/storyline/search_web_topic.json`
- `outputs/video-001/storyline/search_web_topic.json`
- `console_run.json`
- 原始 MC 运行目录、stdout/stderr 和文件时间戳

实施前可在本机执行以下只读核对：

```powershell
Get-Item "outputs/console-654141c9890e/storyline/search_web_topic.json" |
  Select-Object FullName, Length, LastWriteTime

Get-Content "outputs/console-654141c9890e/storyline/search_web_topic.json" -Raw

Get-Item "outputs/video-001/storyline/search_web_topic.json" |
  Select-Object FullName, Length, LastWriteTime
```

应将 JSON 中 `searched_at`、文件 mtime、任务事件时间和页面显示时间分别记录，不得将不同事件的时间戳直接当作同一时间比较。

---

## 3. 平台回退设计存在的关键缺陷

### 3.1 P0：MediaCrawler 许可必须先过审

固定版本 MediaCrawler 的 `LICENSE` 为 `NON-COMMERCIAL LEARNING LICENSE 1.1`。该许可写明：

- 授权范围为非商业学习目的；
- 未经版权所有者书面同意，不得用于商业用途；
- 要求在软件及副本的合理显著位置保留版权和许可声明；
- 不得大规模爬取或对平台运营造成干扰。

**实施门禁：**

1. 如果本项目只用于个人非商业学习/研究，仍需遵守许可文本和目标平台规则，并保留要求的声明。
2. 如果本项目将用于公司、商业服务、收费产品或其他商业用途，必须先取得版权所有者书面授权，或更换许可允许的实现。
3. 授权未确认前，不得把“技术上可接入”视为“可以上线使用”。

### 3.2 P1：`dy` 与 `douyin` 的结果目录名不一致

这是原文完全遗漏的直接阻断问题。

auto-video-editor 当前适配器读取：

```python
data_dir = run_dir / str(platform) / "json"
```

`platform="dy"` 时读取：

```text
<run_dir>/dy/json/
```

但固定版本 MediaCrawler 的 `store/douyin/_store_impl.py` 中，`DouyinJsonStoreImplement` 创建 `AsyncFileWriter` 时使用：

```python
platform="douyin"
```

`tools/async_file_writer.py` 的 `_get_file_path()` 使用 `self.platform` 拼接目录。因此真实目录是：

```text
<run_dir>/douyin/json/
```

原方案即使增加 `aweme_url`，适配器仍然读不到真实 JSON 文件。

**修订：** 在 `mediacrawler_client.py` 中增加独立的输出目录映射，平台代码与物理存储目录不可混为一谈：

```python
MC_OUTPUT_DIR_BY_PLATFORM = {
    "dy": "douyin",
}
```

读取时：

```python
output_platform = MC_OUTPUT_DIR_BY_PLATFORM.get(platform, platform)
data_dir = run_dir / output_platform / "json"
```

后续其他平台必须通过各自 fixture 和真实文件路径验证后再注册映射，不要假定 MediaCrawler 的 CLI 平台代码一定等于存储目录名。

### 3.3 P1：原文引用的抖音异常处理不是关键词搜索调用路径

固定版本 `media_platform/douyin/core.py` 的结构：

- `search()`：关键词搜索路径。调用 `search_info_by_keyword()`，从 `posts_res` 中取 `aweme_info`，随后直接 `update_douyin_aweme()`。
- `get_aweme_detail()`：单条视频详情路径，捕获 `DataFetchError` 和 `KeyError`。
- `get_specified_awemes()`：调用 `get_aweme_detail()` 的路径之一。

因此，原文依据 `get_aweme_detail()` 的异常捕获认定抖音关键词搜索“有逐条异常隔离”，调用关系不成立。

**修订：** 首期必须对 `DouYinCrawler.search()` 实际路径做 fixture 和真机验证，特别检查：

- `search_info_by_keyword()` 返回异常或缺少 `data`；
- `post_item` 中 `aweme_info` / `aweme_mix_info` 结构缺失；
- `mix_items` 为空或索引异常；
- `aweme_id` 缺失；
- `update_douyin_aweme()` 存储失败；
- 返回部分记录、空列表、进程非零退出等行为。

不能把详情模式下的异常捕获覆盖到关键词搜索模式。

### 3.4 P1：回退链必须纳入缓存 Hash

原计划只把主平台 `xhs` 和 `mc_revision` 纳入 Hash，不足以表达实际执行策略。

以下配置必须得到不同 Hash：

```text
xhs → dy → Bing
xhs → ks → Bing
xhs → dy → ks → Bing
xhs → Bing
```

建议使用稳定、有序的平台链标识：

```text
mediacrawler:xhs>dy:09ed1ec62d88
```

最终 Hash 至少应覆盖：

- 归一化后的 query；
- 主平台与有序 fallback 平台列表；
- MediaCrawler revision；
- adapter/parser 语义版本；
- `max_results`；
- artifact schema 版本。

节点层和能力层必须调用同一个 Hash helper，禁止各自拼接字符串。

注意：回退成功仍保留“首选策略”的 Hash 是合理的，但“首选策略”必须包含完整平台链，而不能只包含第一个平台。

### 3.5 P1：统一整条链的超时预算

原计划每个平台都可执行一次完整的 `STORYLINE_WEB_TOPIC_MC_TIMEOUT=600`。两个平台时可能产生约 1200 秒的子进程等待时间。

建议区分：

| 配置 | 含义 | 建议默认值 |
|---|---|---:|
| `STORYLINE_WEB_TOPIC_MC_TIMEOUT` | 单次 MC 调用最大超时上限 | 600 秒，保留现值 |
| `STORYLINE_WEB_TOPIC_MC_CHAIN_TIMEOUT` | 整条 MC 平台链最大总预算 | 600 秒 |

执行时使用 `time.monotonic()` 建立 deadline：

```python
deadline = time.monotonic() + chain_timeout

for platform in platform_chain:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # 记录 TIME_BUDGET_EXHAUSTED，停止 MC 尝试
        break

    attempt_timeout = min(per_attempt_timeout, int(remaining))
    if attempt_timeout <= 0:
        break

    # 仅在剩余总预算内运行当前平台
```

若第一平台耗尽总预算，不再启动第二平台，直接进入 Bing；不能在总预算耗尽后再次给第二平台 600 秒。

### 3.6 P1：CDP 配置下不能保证平台 Profile 独立

固定版本 `config/base_config.py` 默认：

```python
ENABLE_CDP_MODE = True
CDP_CONNECT_EXISTING = True
```

`CDPBrowserManager.launch_and_connect()` 在 `CDP_CONNECT_EXISTING=True` 时走连接已有浏览器路径。

`browser_data/cdp_<platform>_user_data_dir` 是其启动新浏览器时设置的 `user_data_dir`；连接已有浏览器时不能直接依据该路径推断每个平台都会使用独立 Profile。

因此原文“各平台 browser profile 相互独立”的描述不准确，必须补充 CDP 模式前提。

**修订建议：**

- 在第一次接入 dy 前验证当前浏览器连接模式、用户数据目录、Cookie 持久化和任务退出后的浏览器状态。
- 不允许两个平台并发操作同一个现有浏览器会话。
- 保留当前 CDP 锁，确保同一端口串行。
- 如果需要确定的平台隔离，应使用独立、可控的持久化浏览器 Profile，并确认 `CDP_CONNECT_EXISTING` 的配置与实际启动路径相符。

### 3.7 P2：二维码登录失败可能被误判为无结果

固定版本抖音登录逻辑存在以下行为：

- `login_by_qrcode()` 找不到二维码时会 `sys.exit()`；
- `check_login_state()` 使用 tenacity 最多重试 600 次；
- 登录检查最终失败时也会 `sys.exit()`。

Python `sys.exit()` 不带非零错误码时可能以退出码 0 结束进程。auto-video-editor 适配器随后会检查结果文件；如果没有文件，会返回 `NO_RESULTS`。

因此，登录失败可能表现为“进程退出码 0、结果文件不存在、NO_RESULTS”，不能保证被准确识别为登录错误。

**修订：**

1. 首次接入 dy 应提供独立的登录初始化/人工验收步骤，不应将生产工作流第一次运行当作登录配置流程。
2. 识别“进程退出但没有结果文件”的情况时，保留 stderr、输出目录状态和登录诊断；必要时增加 `AUTH_REQUIRED` / `LOGIN_FAILED` 这类明确错误分类。
3. 不要把登录失败简单伪装成“搜索没有结果”。
4. 当前适配器固定传递 `--lt qrcode`；若未来要支持 cookie/phone，必须显式设计登录方式配置和凭据保护，不能把明文 Cookie 写进日志或 artifact。



### 3.8 平台回退优先级矩阵：内容相关度 × 稳健性

本项目的网页话题检索主要用于补充旅游攻略、目的地体验、路线建议、景点信息和视频创作参考。平台回退顺序不应只按“MediaCrawler 能否启动”决定，而应综合两个维度：

- **内容相关度（Content Relevance）**：平台中是否容易检索到与旅游攻略、目的地体验、路线和景点有关、可供视频脚本使用的内容。
- **运行稳健性（Operational Robustness）**：关键词搜索路径、登录态、结果文件生成、字段结构、异常隔离、超时和重复运行行为是否经过验证，失败是否能够被适配器准确识别并安全回退。

决策原则：**按“内容相关度 × 稳健性”的综合价值安排候选优先级；任一维度未经验证或明显不足的平台，不得仅因上游枚举支持就进入生产回退链。** 当前不提供 1～5 的伪精确分数，因为缺少各平台统一检索词、统一时间窗和统一登录环境下的成功率/有效结果率样本。以下顺序是当前业务场景的推荐验证/接入顺序，不代表已通过真机测试的可用性排名，也不构成平台稳定性的客观测量结论。

排序考虑：小红书作为当前主平台，图文攻略与地点经验最贴近基础需求；抖音优先作为短视频内容补充，直接服务于视频素材研究；快手继续补充短视频来源；B站用于补充中长视频与较完整的行程经验；微博偏向热点和时效性信息，但内容碎片化会增加筛选成本；知乎虽有长文本攻略潜力，但在本项目中尚未取得足够的访问稳定性证据，因此排在后段。贴吧暂不进入默认链。以上是业务优先级判断，后续应以同口径样本验证并允许调整。

| 推荐顺序 | 平台 | MediaCrawler 代码 | 主要内容形态 | 对旅游攻略检索的内容价值 | 当前稳健性证据与风险 | 接入决策 |
|---:|---|---|---|---|---|---|
| 1 | 小红书 | `xhs` | 图文笔记、地点体验、生活方式分享 | **高**：适合目的地体验、景点打卡、餐饮住宿、路线建议和实用攻略；作为中文主检索平台 | 已接入当前项目，但本次故障暴露 HTML 详情解析异常可能沿 `asyncio.gather()` 传播；适配器还把非零退出粗粒度归类为 `NETWORK_ERROR` | 保留为主平台；先完善异常诊断和失败隔离，不因其排名第一而认为其始终稳定 |
| 2 | 抖音 | `dy` | 短视频、视频描述、创作者内容 | **高**：适合目的地实拍、景点体验、路线展示和视觉参考，内容形态与视频创作目标接近 | 当前项目尚未完成接入验证；已发现物理输出目录为 `douyin/json`，不是 `dy/json`；关键词搜索的异常路径不能用详情模式的异常捕获代替；登录和 CDP 行为需实测 | **首个回退平台**；修复输出目录、字段映射、搜索路径异常和登录诊断后，首期落地 `xhs → dy` |
| 3 | 快手 | `ks` | 短视频、生活记录、创作者内容 | **中**：与抖音在短视频内容形态上互补，可作为目的地体验和本地生活内容的补充来源 | 当前项目没有该平台的输出目录、搜索记录字段、登录和异常路径验收证据；不得由“同属短视频平台”推导其适配器兼容 | 第二阶段候选；只有完成独立 fixture、字段映射、登录/超时/异常验证后，才允许追加到链末 |
| 4 | B站 | `bili` | 中长视频、Vlog、教程和经验分享 | **中高**：适合旅行 Vlog、行程复盘、路线讲解和较完整的经验内容；摘要和视频链接可补充深度资料 | 目标内容价值较高，但当前项目未验证 B 站实际 JSON 字段、链接拼装、输出目录、登录态和失败行为 | 第三阶段候选；独立验证 `bvid`/URL 归一化、标题/摘要映射和记录数量后再加入 |
| 5 | 微博 | `wb` | 短文本、图文、热点话题和实时讨论 | **中**：适合时效性话题、活动信息和用户讨论；内容可能较碎片化，单条内容未必具备完整攻略信息 | 当前项目没有可比较的生产成功率、异常率和登录稳定性数据；内容时效性与噪声比例需通过真实样本评估 | 第四阶段候选；先验证检索结果时效性、相关性、重复率和可用摘要比例 |
| 6 | 知乎 | `zhihu` | 问答、专栏和长文本经验 | **中高**：理论上适合路线解释、目的地选择、注意事项和长篇经验整理 | 原始方案认为其访问/风控风险偏高，但本次没有独立的对照测试足以确认“风控最严”；登录要求、搜索接口可用性和页面结构均待验证 | 第五阶段候选；先用低频、合规样本验证访问稳定性与内容可用性，不得默认其一定比微博更容易被拦截 |
| — | 贴吧 | `tieba` | 论坛帖子、主题讨论和历史经验 | **低至中**：可能有目的地经验讨论，但内容时效性、结构化程度和攻略质量需要额外筛选 | 尚无生产级验证；原方案判断其内容可能较旧、较碎片化，但缺少量化样本支持 | **不纳入推荐回退链**；只有未来证明其能稳定补充其他平台无法覆盖的高价值内容时，再单独评估 |

#### 3.8.1 推荐链与分阶段准入

**目标候选顺序固定为：**

```text
xhs → dy → ks → bili → wb → zhihu
```

贴吧 `tieba` 不在默认推荐链中。当前实施范围只启用：

```text
xhs → dy → Bing
```

后续按推荐顺序逐个平台验证、逐个开放，不一次性把全部上游平台加入生产白名单。环境变量示例：

```powershell
# 首期：只启用抖音作为 XHS 的回退平台
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = "dy"

# 后续示例：仅当 ks 已完成准入验收后，才允许改为 dy,ks
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = "dy,ks"

# 关闭平台回退：恢复 xhs → Bing
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = ""
```

配置值只包含**主平台之后的回退平台**，因此不能把 `xhs` 再写入该变量。配置顺序就是实际尝试顺序；不得按字母排序、集合迭代顺序或运行时随机策略重新排列。新增平台前必须先加入已验证白名单，再更新允许的 env 值和相应测试。

#### 3.8.2 每个平台的独立准入门槛

每次只新增一个平台。一个平台必须完成以下全部验证，才能进入生产回退链：

1. **平台标识与物理目录**：确认 CLI 平台代码、MediaCrawler 实际 JSON 输出目录名、文件命名和多次运行隔离规则；在适配器中显式配置代码到目录的映射。
2. **真实字段 fixture**：从固定版本生成脱敏 JSON fixture，确认标题、摘要、内容 ID、原始 URL 字段；验证 URL 的 scheme、主机和标准化规则；不允许仅凭字段名猜测。
3. **关键词搜索路径**：追踪该平台 `search()` 的真实调用关系，检查解析异常、缺字段、存储失败、部分结果和进程退出；不能拿详情/作者模式的异常处理作为关键词搜索路径的证据。
4. **登录与会话**：验证首次登录方式、人工交互需求、登录失败退出码、持久化目录、CDP 是否连接已有浏览器，以及运行结束后的会话复用；不能默认各平台 Profile 已经隔离。
5. **超时与资源清理**：验证单次超时、整链总预算、子进程树回收、CDP 锁释放、失败输出目录 TTL 清理。
6. **数据质量**：验证标准映射、去重、空标题/无 URL 过滤、相关性守卫命中率和有效结果比例；低相关结果不能被当作成功。
7. **错误与诊断**：区分网络失败、登录失败、解析失败、无结果、结构变更和预算耗尽；诊断必须脱敏，不能写入 Cookie/token/个人标识。
8. **缓存与回退**：确认完整有序平台链参与 `request_hash`；平台成功、平台回退、最终 Bing 回退各有独立且兼容的 artifact/UI 语义。
9. **自动化与真机测试**：单测覆盖正常、空结果、异常、无登录态和输出结构变更；Windows 真机至少完成受控冒烟测试后才准入。

任一项未通过，该平台保持在“候选/未准入”状态，不加入 `_MC_PLATFORM_WHITELIST`，也不得出现在有效的 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS` 配置中。

---

## 4. 修订后的目标架构

### 4.1 平台链配置

建议新增：

```text
STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS=dy
```

语义：

- 值为 `dy`：`xhs → dy → Bing`。
- 值为 `dy,ks`：只有在 `ks` 完成独立验证并加入白名单后才允许 `xhs → dy → ks → Bing`。
- 空字符串：不尝试平台回退，保持现有 `xhs → Bing` 行为。
- 不允许重复平台、主平台自身、未知平台和空列表项。

配置解析在 `config.py` 完成，统一标准化为 tuple/list。建议规则：

```text
输入：" dy, ks "
输出：("dy", "ks")
```

配置错误必须在导入/启动阶段明确报错，不得运行到一半才忽略无效平台。

首期白名单建议：

```python
_MC_PLATFORM_WHITELIST = ("xhs", "dy")
```

这里的白名单表达“已完成准入验证的平台”，不代表 MediaCrawler 上游声明支持的全部平台。

### 4.2 单次平台尝试的成功条件

某个平台必须同时满足以下条件才算成功：

1. 子进程执行成功；
2. 找到该平台真实输出目录下的 JSON 文件；
3. JSON 可解析且结构符合预期；
4. `_map_mc_records()` 后至少存在一条有效记录；
5. 相关性守卫通过。

任一条件不满足，记录当前平台失败原因，再尝试下一个平台。

不得仅以 returncode=0 认定搜索成功，也不得仅以原始 JSON 非空认定存在有效结果。

### 4.3 平台链执行伪代码

```python
platform_chain = (primary_platform, *fallback_platforms)
attempts = []
deadline = time.monotonic() + chain_timeout

for platform in platform_chain:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        attempts.append({
            "platform": platform,
            "error_code": "TIME_BUDGET_EXHAUSTED",
        })
        break

    raw_records, error_code, message, diag = search_notes(
        platform=platform,
        timeout_seconds=min(per_attempt_timeout, int(remaining)),
        ...,
    )

    items = [] if error_code else _map_mc_records(raw_records, max_results)
    # 对每个平台分别执行有效记录检查与相关性守卫
    # 将脱敏后的结果写入 attempts

    if items and relevance_passed:
        # 选中当前平台，停止循环
        break

# 只有平台链全部失败或耗尽预算后才调用 Bing 一次
```

上面是控制流示意，不是可以直接覆盖现有代码的完整实现。特别是编程错误与运行时错误要区别处理：`TypeError`、`ValueError` 等入参/配置编程错误不能被静默吞掉后伪装成平台故障。

### 4.4 Artifact 与回退语义

必须区分两种不同回退：

1. **同 Provider 内的平台回退**：`mediacrawler(xhs) → mediacrawler(dy)`。
2. **跨 Provider 回退**：`mediacrawler(所有平台失败) → bing_html`。

建议字段：

```json
{
  "requested_provider": "mediacrawler",
  "requested_platform": "xhs",
  "provider": "mediacrawler",
  "selected_platform": "dy",
  "engine": "MediaCrawler(抖音)",
  "platform_chain": ["xhs", "dy"],
  "platform_fallback": {
    "from_platform": "xhs",
    "to_platform": "dy",
    "reason": "NETWORK_ERROR"
  },
  "diagnostics": {
    "primary": {},
    "attempts": []
  }
}
```

建议保留兼容原则：

- `requested_provider` 仍表示用户请求的 Provider。
- `requested_platform` 仍表示主平台。
- `provider` 对于 XHS 或 Douyin 成功都为 `mediacrawler`。
- `engine` 表示真正产出结果的平台。
- `fallback` 继续只表示 MC 最终回退到 Bing，避免把同 Provider 的平台切换伪装成跨 Provider 回退。
- 新增 `platform_fallback` 表示同 Provider 内部的平台切换。
- `diagnostics.primary` 继续指向主平台诊断，兼容旧页面；新增 `diagnostics.attempts` 保存完整平台尝试序列。

如果新增状态标签，建议增加：

```text
storyline_search_web_topic_fallback_platform
```

并同步更新前端展示映射与测试。不要复用 `fallback_bing` 标签表示抖音回退。

`diagnostics` 只增加可选字段、没有改变既有必填字段时，artifact `schema_version` 可以暂时维持 v2；但 request_hash 语义改变必须使旧缓存失效。若最终引入必填字段或改变字段含义，再评估是否提升 schema 版本。

---

## 5. 文件级修订方案

### 5.1 `config.py`

修改内容：

1. `_MC_PLATFORM_WHITELIST` 由 `("xhs",)` 扩展为 `("xhs", "dy")`，但必须先通过 P0 许可门禁。
2. 新增 `STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS`，默认 `dy`，空串表示关闭平台回退。
3. 增加严格解析：逗号拆分、strip、小写化、去重校验；拒绝主平台重复、未知平台和空项。
4. 新增 `STORYLINE_WEB_TOPIC_MC_CHAIN_TIMEOUT`，作为整条平台链总预算；单次 `STORYLINE_WEB_TOPIC_MC_TIMEOUT` 仍作为单次上限。
5. 保持当前配置导入即校验的风格，不要把非法配置延迟到任务运行时。

### 5.2 `storyline_capabilities/mediacrawler_client.py`

修改内容：

1. 增加 `MC_OUTPUT_DIR_BY_PLATFORM`，首期至少包含 `dy: douyin`。
2. `--platform` 仍传平台枚举代码 `dy`，只在本地读取结果目录时映射为 `douyin`。
3. 保留原始结果文件大小上限、数据脱敏、TTL 清理、CDP 锁和进程树超时终止行为。
4. 进程退出码为 0 但没有 JSON 文件时，应保留诊断，不要直接将所有此类情况解释为“平台无搜索结果”；补充登录失败/文件未生成的证据字段。
5. 对 stderr 诊断进行受限增强：适当增加可用尾部长度或写入受限诊断文件，必须继续做 token/Cookie 脱敏并有 TTL；不要保存完整登录态或未脱敏 HTML。
6. `search_notes()` 的返回结构暂时可保持 `(records, error_code, message, diagnostics)`，避免不必要扩大改动。

### 5.3 `storyline_capabilities/search_web_topic.py`

修改内容：

1. `_map_mc_records()` 的 URL 白名单增加 `aweme_url`。
2. `title`、`desc` 映射继续按实际上游 JSON fixture 验证，不要只凭代码注释假设字段存在。
3. 增加平台链循环，复用现有 `search_notes()` 和 `_count_relevant_results()`。
4. 每个平台都执行完整的“运行 → JSON 读取 → 标准映射 → 有效性检查 → 相关性守卫”。
5. 首个平台成功即停止；任一备用平台成功也停止；全部失败才调用 Bing 一次。
6. 增加 `platform_chain`、`attempts`、`selected_platform`、`platform_fallback` 诊断字段。
7. 保留 `diagnostics.primary` 为第一个平台的诊断，兼容既有页面与调试代码。
8. `request_hash` 改为包含完整有序平台链。
9. 对平台循环中的意外编程错误继续 fail-fast，不要捕获 `Exception` 后无条件换平台。

### 5.4 `nodes/storyline/node_search_web_topic.py`

修改内容：

1. 将主平台、回退列表、MC revision、timeout 等配置统一构造到 `provider_options`。
2. 使用共享 helper 生成 Hash；节点层与能力层必须得到相同结果。
3. 增加平台回退状态标签；Bing 回退标签语义保持原样。
4. 首平台成功时不写平台回退标签；备用平台成功时写平台回退标签；Bing 成功仍写既有 Bing 回退标签。
5. 继续通过 artifact 保留 `requested_platform` 与实际 `selected_platform`，不要覆盖用户请求的平台信息。

### 5.5 前端展示

原计划主要涉及能力层和节点层，但新增 `platform_fallback` 后必须确认前端能解释新状态。

检查范围：

- `web/static/ai_content.mjs`
- `web/ai_content.html`
- `web/console_service.py`（仅在确有字段转换或状态映射时修改）

展示要求：

- 检索引擎显示真正产出内容的平台，例如 `MediaCrawler(抖音)`。
- 页面能看出“小红书失败后改用抖音”，不能只显示最终引擎而丢失过程。
- 如果最终回退 Bing，显示原平台链的失败原因和 Bing 结果状态。
- 不显示 Cookie、token、绝对本机路径或原始敏感响应。

---

## 6. 分阶段执行计划

| 阶段 | 任务 | 主要文件 | 验收条件 |
|---|---|---|---|
| P0 | 许可和上游版本门禁 | MediaCrawler LICENSE、部署说明 | 商业用途已取得书面许可，或确认仅用于许可允许的非商业学习/研究；固定上游 SHA 可复验 |
| P1 | 抖音物理输出目录和字段映射 | `mediacrawler_client.py`、`search_web_topic.py` | `dy` 运行后能读取 `douyin/json/search_contents_*.json`，并正确映射 `aweme_url` |
| P2 | 平台回退配置与验证 | `config.py` | 默认 `dy`；空串关闭；无效、重复、主平台重复配置均启动时报错 |
| P3 | 平台链控制流、时间预算、诊断 | `search_web_topic.py` | 按 xhs→dy 顺序执行；首成功停止；总预算耗尽不再启动下一平台；失败后只调用 Bing 一次 |
| P4 | Hash、节点状态和 UI | `node_search_web_topic.py`、前端 | 改变平台链必 miss 缓存；节点与能力层 Hash 一致；UI 可识别平台回退与 Bing 回退 |
| P5 | 单元/集成测试 | 三个既有测试模块及新增 fixture | 全部新增用例通过，旧测试零回归 |
| P6 | 真机与小流量验收 | 本地 MC 固定版本、独立浏览器会话 | 有登录态时 dy 可产生有效结果；退出/无登录态时能安全降级；无浏览器残留和缓存串用 |
| P7 | 灰度、观察和回滚 | 配置、执行记录 | 可单独关闭平台链，回到 xhs→Bing；出现异常时不阻塞主工作流 |

### 6.1 P1：输出目录与映射测试先行

必须先写一个会失败的测试来模拟真实 MediaCrawler 输出结构：

```text
<tmp>/run-id/
└── douyin/
    └── json/
        └── search_contents_2026-11-21.json
```

测试输入平台参数为 `dy`，断言适配器能从 `douyin/json` 找到 JSON 文件。

同时验证：

```json
{
  "aweme_id": "fixture-id",
  "title": "测试标题",
  "desc": "测试描述",
  "aweme_url": "https://www.douyin.com/video/fixture-id"
}
```

标准结果应只保留已有契约允许的四个字段：

```json
{
  "url": "https://www.douyin.com/video/fixture-id",
  "title": "测试标题",
  "snippet": "测试描述",
  "source": "mediacrawler"
}
```

真实 fixture 应来自固定版本的脱敏输出，不要把真实 Cookie、用户昵称、用户 ID 或 token 提交到 Git。

### 6.2 P2：配置解析测试

覆盖：

| 输入 | 预期 |
|---|---|
| `dy` | `("dy",)` |
| ` dy , ks `（仅 ks 已准入时） | `("dy", "ks")` |
| 空字符串 | `()`，关闭平台回退 |
| `dy,dy` | 明确配置错误，禁止静默重复运行 |
| `xhs` | 主平台不能重复加入 fallback |
| `unknown` | 明确配置错误 |
| `dy,,ks` | 明确配置错误 |

### 6.3 P3：平台执行矩阵测试

新增/补充以下场景：

1. XHS 有效且相关结果 → 只调用 XHS，不调用 dy/Bing。
2. XHS 进程失败 → dy 有效 → 返回 dy，且不调用 Bing。
3. XHS 无有效 JSON → dy 有效 → 返回 dy。
4. XHS 有结果但相关性不达标 → dy 相关性通过 → 返回 dy。
5. XHS 失败、dy 无有效结果 → Bing 回退。
6. XHS、dy 都失败 → `attempts` 同时保存两次原因，Bing 只调用一次。
7. `fallback_platforms=()` → 与现有 XHS→Bing 行为保持一致。
8. dy 成功结果经过 `_map_mc_records()`，必须从 `aweme_url` 生成标准 `url`。
9. dy 真实存储目录为 `douyin`，而不是测试桩误写为 `dy`。
10. 改变 fallback 列表顺序或成员，Hash 必须改变。
11. 主平台 Hash 和能力层 Hash 一致；节点 cache check 与产物写入 Hash 一致。
12. 超时预算耗尽后不启动下一个 MC 子进程，进入 Bing。
13. `TypeError` / `ValueError` 等编程错误不能被伪装成平台不可用。
14. 登录失败、进程退出但无输出文件，诊断不能错误宣称“搜索无结果”。
15. 平台回退成功与 Bing 回退成功分别产生不同状态标签。

### 6.4 P4：前端/Artifact 兼容测试

至少验证：

- 旧 artifact（无 `platform_chain`、无 `attempts`）仍可读取。
- 新 artifact 增加字段后页面不报错。
- `engine=MediaCrawler(抖音)` 正确显示。
- `platform_fallback` 说明从 xhs 到 dy 的切换。
- `fallback` 仍只代表 MC→Bing。
- `request_hash` 改变后旧缓存不会被错误复用。

### 6.5 P5：推荐测试命令

在 Windows 项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/test_storyline_search_web_topic_mc.py `
  tests/unit/test_mediacrawler_client.py `
  tests/unit/nodes/test_node_search_web_topic.py -q
```

随后执行全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试报告至少记录：总用例数、通过数、失败数、跳过数、失败用例名和失败原因。全量回归不能只写“零新增失败”，必须保存对比基线及本次报告。

### 6.6 P6：真机验收顺序

真机测试必须和单测分开，且不得在生产流水线第一次运行时才初始化登录状态。

1. 检查 MediaCrawler 固定版本、依赖安装、`main.py`、`.venv` 和输出目录权限。
2. 明确当前 CDP 是连接已有浏览器还是启动专用 Profile。
3. 在独立测试任务中初始化并验证抖音登录态；检查浏览器关闭后会话是否持久化。
4. 运行 XHS 成功场景，确认主平台成功即停止。
5. 通过可控桩模拟 XHS 失败、dy 成功，不要为了测试回退链主动制造平台风控。
6. 使用真实 dy 搜索验证 `douyin/json` 输出目录、记录映射、相关性守卫和 UI 展示。
7. 在测试环境模拟 dy 无登录态/登录失败，确认诊断类型、超时上限、Bing 回退和主工作流继续运行。
8. 检查浏览器子进程、CDP 锁、失败原始 JSON TTL 清理和运行目录残留。

### 6.7 P7：回滚

配置回滚：

```powershell
$env:STORYLINE_WEB_TOPIC_MC_FALLBACK_PLATFORMS = ""
```

重启服务后应恢复：

```text
xhs → Bing
```

引擎级回滚仍使用现有配置：

```powershell
$env:STORYLINE_WEB_TOPIC_ENGINE = "bing_html"
```

修改环境变量后必须重启读取配置的进程。回滚不得依赖删除代码或清空历史 artifact。

---

## 7. 验收标准（Definition of Done）

只有同时满足以下条件，平台链才可标记为完成：

- [ ] P0 许可门禁已通过，授权和用途边界有记录。
- [ ] MediaCrawler 外部副本 SHA 与验收记录一致。
- [ ] 当前生产白名单只包含已逐项验收的平台；首期只启用 `xhs` 主平台与 `dy` 回退；后续平台严格按 §3.8 顺序逐个准入。
- [ ] `dy` 平台代码与 `douyin` 物理输出目录映射正确。
- [ ] `aweme_url` fixture 和真实输出映射通过。
- [ ] XHS 主平台成功时不会多跑 dy。
- [ ] XHS 失败、dy 成功时不会触发 Bing。
- [ ] XHS/dy 全失败时只执行一次 Bing 回退。
- [ ] 每个平台都独立经过有效性与相关性校验。
- [ ] request_hash 包含完整有序平台链，节点和能力层结果一致。
- [ ] 单次超时与整条链总预算分离，没有 600 秒逐平台叠加。
- [ ] CDP Profile、登录态和退出行为已在真实 Windows 环境确认。
- [ ] 登录失败不会被简单误报为 `NO_RESULTS`。
- [ ] diagnostics 包含完整尝试顺序，并对敏感数据脱敏。
- [ ] 旧 artifact 仍兼容，前端可区分平台回退和 Bing 回退。
- [ ] 新增单测、集成测试和全量回归通过。
- [ ] 回滚配置已在测试环境验证。

---

## 8. 原文问题汇总表

| # | 原文位置 | 问题 | 严重性 | 修正要求 |
|---:|---|---|---|---|
| 1 | §7 平台支持矩阵 / §8.0 | 未核查 dy 与 douyin 的物理结果目录不同 | P1 | 增加输出目录映射及真实目录 fixture |
| 2 | §7 抖音详情异常隔离 | 引用了 `get_aweme_detail()`，但关键词搜索不走该方法 | P1 | 按 `DouYinCrawler.search()` 实际路径验证 |
| 3 | §8.0 / §8.2 request_hash | Hash 只按主平台和 revision，不包含 fallback 列表 | P1 | Hash 包含完整有序平台链 |
| 4 | §9 / §10 超时 | 把 600 秒当作每个平台均可独立消耗的预算 | P1 | 增加整条链 deadline |
| 5 | §7 浏览器 profile | 忽略 `CDP_CONNECT_EXISTING=True` 情况 | P1 | 明确 Profile 条件并做真机验证 |
| 6 | §10 抖音首次扫码 | 没考虑二维码缺失/登录失败时 `sys.exit()` 和退出码 0 | P1 | 改进登录诊断，避免误判 NO_RESULTS |
| 7 | §4 根因定案 | 将高频风控描述得过于确定 | P2 | 改成主嫌疑，补采证据 |
| 8 | §3.2 | `noteDetailMap` 存在不能证明页面不是验证码/异常页 | P2 | 删除过强结论 |
| 9 | §3.3 | XHS 异常豁免类型枚举不完整 | P2 | 补充 DataFetchError / KeyError |
| 10 | §6.2 → §8 | 声称可定位具体笔记，但实施计划没有增加笔记级诊断 | P2 | 另设安全观测性任务 |
| 11 | §8.2 / §8.4 | 没有测试物理输出目录映射 | P1 | 增加真实目录 fixture |
| 12 | §8.4 / §9 | 缺少链顺序、总超时、Hash、登录错误、UI 状态完整测试 | P2 | 按本文执行矩阵补齐 |
| 13 | §7 扩展依据 | 仅靠上游 PlatformEnum 不足以确认各平台输出和登录行为一致 | P2 | 每个平台独立核查存储目录、字段、Profile、异常和登录 |
| 14 | 全文 | 没有提示 MediaCrawler 非商业学习许可 | P0 | 增加许可门禁 |
| 15 | §6.1 时间戳 | 将不同事件来源的时间戳直接推断为时区转换错误 | P2 | 用同一原始事件时间戳复验 |

---

## 9. 参考源码与核查链接

### auto-video-editor（本次核查提交）

- [项目 README](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab91cb8a0d0b133726b/README.md)
- [config.py](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab91cb8a0d0b133726b/config.py)
- [MediaCrawler 适配器](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab91cb8a0d0b133726b/storyline_capabilities/mediacrawler_client.py)
- [网页话题检索能力层](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab91cb8a0d0b133726b/storyline_capabilities/search_web_topic.py)
- [网页话题检索节点](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab91cb8a0d0b133726b/nodes/storyline/node_search_web_topic.py)
- [MC 回退单测](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab91cb8a0d0b133726b/tests/unit/test_storyline_search_web_topic_mc.py)
- [MC 适配器单测](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab8a0d0b133726b/tests/unit/test_mediacrawler_client.py)
- [节点单测](https://github.com/wayyet/auto-video-editor/blob/e1ec07608bb1da2160234ab8a0d0b133726b/tests/unit/nodes/test_node_search_web_topic.py)

### MediaCrawler（固定版本）

- [固定 commit](https://github.com/NanmiCoder/MediaCrawler/commit/09ed1ec62d885dab422a53de999f52ac0bd4966b)
- [上游支持的平台枚举（PlatformEnum）](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/api/schemas/crawler.py)
- [XHS core.py](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/xhs/core.py)
- [XHS client.py](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/xhs/client.py)
- [XHS extractor.py](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/xhs/extractor.py)
- [Douyin core.py](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/media_platform/douyin/core.py)
- [Douyin store implementation](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/store/douyin/_store_impl.py)
- [AsyncFileWriter](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/tools/async_file_writer.py)
- [CDP browser manager](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/tools/cdp_browser.py)
- [MediaCrawler LICENSE](https://github.com/NanmiCoder/MediaCrawler/blob/09ed1ec62d885dab422a53de999f52ac0bd4966b/LICENSE)

---

## 10. 最终建议

建议采用以下实施顺序：

1. **先处理许可与上游版本门禁。** 这是能否使用 MediaCrawler 的前置条件。
2. **先修 `dy → douyin` 输出目录映射。** 这是抖音结果能否被适配器读到的必要条件。
3. **修正缓存 Hash 和整条平台链超时预算。** 防止错误复用缓存和任务等待时间成倍增长。
4. **再实现 `xhs → dy → Bing` 平台循环。** 每个平台必须完成有效数据映射与相关性校验。
5. **最后补齐节点、前端、诊断与真机验收。** 明确区分平台回退和 Bing 回退。
6. **后续按 §3.8 的矩阵逐个扩展。** 顺序为 `ks → bili → wb → zhihu`；每新增一个平台，都要完成目录/字段/关键词搜索路径/登录/超时/错误诊断/相关性和真机验收，再将其加入 env 允许列表；`tieba` 不进入默认链。

**审查意见：原文可作为需求背景和故障线索保留，但不能原样作为最终执行说明。应以本文的修订方案替换原文第八、九、十节的实现与验收要求，并同步修正第二、三、四、七节中的事实表述。**

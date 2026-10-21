# OpenStoryline (auto-video-editor 嵌入版)

> 本目录是 [FireRed-OpenStoryline](https://github.com/wayyet/FireRed-OpenStoryline)
> (Apache-2.0) 的内嵌副本。代码沿用上游,主仓库 `auto-video-editor` 通过本地
> uvicorn 子进程 + 磁盘产物读盘方式与其对接,不再使用 MCP/stdio 链路。

## 与上游的差异

- 不再有 MCP server(`src/open_storyline/mcp/` 保留为对照文档,不被运行时调用)
- 启动入口固定为 `uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`
- 产物写到本目录的 `outputs/<session_id>/plan_timeline_pro/*.json`,
  由 `auto-video-editor/nodes/node_04b_get_storyboard_plan.py` 通过
  `storyline.plan_reader.read_latest_plan_timeline_pro()` 读盘拍平为
  `CanonicalTimeline`。
- 侧边栏左上角的「📥 导入视频」按钮(`POST /api/system/import-video`)是
  auto-video-editor 图侧 `import_video` 节点唯一的继续信号来源;图自己
  永不自动导入视频。该端点跨 venv 引入
  `auto-video-editor/nodes/node_04a_import_video.py`,故该模块顶层只允许依赖
  stdlib + `config` + `storyline.output_isolation`。
- 「📥 导入视频」支持**一次多选**多个视频(`<input type="file" multiple>`):
  前端串行把 N 个文件传到当前会话(pending 区),然后**只发一次**握手 POST
  (`{session_id, files:[{media_id, filename, stored_path}, ...]}`,同时保留
  单值字段兼容旧端点)。端点有限流(burst=1 / rpm=2),分 N 次 POST 必然 429。

### 「导入视频」的两阶段语义

按钮在**两个阶段**都可用,行为不同:

| 阶段 | 图的状态 | 握手结果 | 提示 |
|---|---|---|---|
| 关卡⓪ 规划阶段 | 还没到 `import_video` 节点 | `409 no_pending_import_request` | 「已上传 N 个视频到当前会话,可继续与 Agent 对话完成规划」 |
| `import_video` 等待阶段 | 正在轮询等结果 | `200` | 「已导入 N 个视频」,图自动继续 |

409 **不是错误**:素材已经进了会话,规划器(Agent)能立刻用上。
多选后若部分文件上传失败,失败的记入失败列表,已成功的照常导入(部分成功 = 导入成功)。

## 网页端用 mcode 驱动

本机已登录的 **mcode CLI** 可以充当 OpenStoryline 的 ChatModel:网页端的一次对话,
变成一次 `mcode exec` 子进程,让 mcode 自己用 MCP 工具(视频、素材、任务信箱)干活。

代码分布:

| 文件 | 作用 |
|---|---|
| `src/open_storyline/config.py` | `MCodeConfig` + `[mcode]` 段 + 环境变量覆盖 |
| `src/open_storyline/mcode_chat_model.py` | `MCodeChatModel`:组装命令、跑子进程、翻译结果 |
| `src/open_storyline/mcode_media.py` | 素材准备:路径解析、复制校验、素材清单 |
| `src/open_storyline/agent.py` | `build_agent()` 的 mcode 分支 |
| `agent_fastapi.py` | 模型下拉接入 mcode 档位 + `ensure_agent()` 接线 + 「导入视频」端点接入素材准备 |

### 配置

本机路径写 `config.toml.local`(**不入库**,`.gitignore` 已排除):

```toml
# openstoryline/config.toml.local
[mcode]
cwd  = "E:/Documents/kuaishou/video-ops"          # 操作员目录
bin  = "C:/Users/wayyet/.minimax-code/mcode.cmd"  # mcode 可执行文件
```

也可以用环境变量覆盖(优先于配置文件):

```
OPENSTORYLINE_MCODE_CWD         # 操作员目录
OPENSTORYLINE_MCODE_BIN         # mcode 可执行文件
OPENSTORYLINE_MCODE_MEDIA_ROOT  # 素材白名单根
```

`config.toml` 里的 `[mcode]` 段全是中性默认值,**不含本机路径、不含任何密钥**。

操作员目录(`cwd`)**必须**同时含有 `.mcp.json` 和 `AGENTS.md` ——
前者给 mcode 提供 MCP 工具配置,后者给它行为说明。缺了就直接报配置错误,不会静默跑。

其余字段(都有合理默认值,通常不用改):

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | `standard` | mcode 上下文模式。`standard` 重、`lightweight` 轻 |
| `permission` | `smart` | 传给 `mcode exec --permission`。**不是硬权限边界** |
| `max_steps` | `120` | 单次 exec 的最大步数 |
| `timeout_s` | `900` | 单次 exec 超时秒数(超时后杀整棵进程树) |
| `history_turns` | `10` | 拼 prompt 时保留的完整对话轮次 |
| `max_prompt_chars` | `60000` | prompt 字符上限(超了报错,不静默截断当前输入) |
| `media_root` | `""` | 素材白名单根;空 = `<auto-video-editor>/inputs` |

### 怎么在网页端用起来

1. 配好上面的 `[mcode]` 段(操作员目录 + mcode 可执行文件);
2. 起网页端:`.\openstoryline\.venv\Scripts\python.exe -m uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`
   (cwd = `openstoryline`);
3. 左侧「LLM 模型」下拉里选 **`mcode-operator`**(完整能力)或 **`mcode-chat`**(轻量省钱)。

下拉里出现 mcode 档位的前提是 `[mcode].cwd` **已配置且目录存在**;没配就看不到这两个选项
(不给点了就报错的选项)。选中后 LLM 与 VLM 会自动同步成同一个档位 —— mcode 不区分多模态。

**默认档位**:如果内置模型(`[llm]`)的 api_key 还是占位符(入库的 `config.toml` 里就是
`<YOUR_DEEPSEEK_API_KEY>`)而 mcode 可用,新会话**默认直接走 mcode**,省得先撞一次 401。
内置模型配了真 key 就仍然默认用它。旧会话恢复时也会做同样判断:内置模型凭证不可用就落回 mcode。

### 能力边界(重要)

- **skills 不可用**。mcode 分支不注册 LangChain skills 工具(`tools=[]`),
  skills 是真 LangChain 工具,mcode 用不了。mcode 用什么工具由它自己的
  `.mcp.json` 决定。
- **`--permission` 不是安全边界**。`smart` 只是软护栏 —— mcode 仍然可能调 Bash、
  读写文件、联网。不要把它当成沙箱。
- **不连 8001**。mcode 分支不建 MCP 连接,也不做 API key 校验(mcode 不走
  OpenAI 兼容网关)。普通模型分支的 MCP 加载与失败语义**一字未改**。
- **开销差异很大**。本机实测 `standard` 模式每轮约 19.5K input token,
  `lightweight` 约 857 token(差 23 倍)。想省钱用 `mcode-chat`。
- **取消 mcode ≠ 取消 LangGraph worker**。取消后 mcode 进程树会被清理,
  但 worker 侧的任务可能还在信箱里跑。
- **两个平级消费者**。网页端 mcode 与命令行 `agent_gate drive` 没有原子认领机制。
  同一任务可能被两边同时看到;bridge prompt 里要求 mcode 如实说明「已被领走」。
- **素材副本不清理**。副本留在 `inputs/<session_id>/`,`inputs/` 已被 `.gitignore` 排除。

### 素材复制策略

网页端上传的视频落在 `outputs/<session_id>/media/`,**不在** agent_gate 的素材白名单里
(白名单 = `AGENT_GATE_MEDIA_ROOTS`,默认 `<repo>/inputs` + `<repo>/tests/fixtures`)。
mcode 只能用白名单内的绝对路径,所以点「📥 导入视频」时:

1. 前端照旧把文件传到当前会话;
2. `POST /api/system/import-video` 把每个文件**复制**一份到
   `<auto-video-editor>/inputs/<session_id>/`;
3. 复制先写同目录 `<name>.part` → 校验**大小 + SHA-256** → `os.replace` 原子改名;
4. 同一 `media_id` 重复导入 → 同名同内容,**幂等复用**;同名不同内容 → 换 `<name> (1).ext`,
   **不覆盖**既有文件;
5. **源文件不动**(不移动、不删除、不覆盖);
6. 复制失败 → 删掉 `.part`、**不写导入结果**、返回 500 + 具体原因。

**源路径由服务端解析**,不信客户端。前端的 `public_media` 只返回
`{id, name, kind, thumb_url, file_url}` —— **不暴露 `path`**,所以网页端送来的
`stored_path` 恒为空串。端点按 `media_id` 从**会话媒体登记表**
(`media_id -> MediaMeta.path`,随 `session_state.json` 持久化)取真实落盘路径,
客户端的 `stored_path` 只作兜底;两条路拿到的路径都要过「必须位于本会话
media 目录内」的校验。

导入结果载荷**保持不变** —— 用的仍是客户端送来的原始 `files`,
图侧 `video_input_paths` 继续指向原 `stored_path`。
副本清单通过 `MCodeChatModel.media_manifest` 交给模型,模型**不自己拼路径**。

### 错误排查

| 现象 | 原因 / 处理 |
|---|---|
| `LLM API key is invalid or unauthorized ... HTTP 401` | 走的是**内置模型**分支,那把 key 无效。左侧「LLM 模型」改选 `mcode-operator` / `mcode-chat`;或给 `[llm].api_key` 配真 key |
| 下拉里**看不到** mcode 选项 | `[mcode].cwd` 没配或目录不存在。配好后**重启**网页端 |
| 网页端还是「导入失败」toast | 旧版本端点有 `NameError` 缺陷,已修;确认服务已重启 |
| `找不到 mcode 可执行文件` | 装 MiniMax Code,或设 `[mcode].bin` / `OPENSTORYLINE_MCODE_BIN` |
| `配置的 mcode 可执行文件不存在: ...` | 显式 `bin` 路径写错。**不会**静默换成 PATH 里的别的程序 |
| `mcode 操作员目录缺少 .mcp.json / AGENTS.md` | 操作员目录不对,或缺文件 |
| `mcode 未配置操作员目录` | 配 `[mcode].cwd` |
| `素材准备失败:...` | 看 `detail` 里的具体原因(源文件不在本会话目录 / 校验不过 / 越过白名单) |
| `mcode exec 超时` | 超时后**整棵进程树**被杀掉(`taskkill /F /T`)。调 `[mcode].timeout_s` |

### 测试

```powershell
cd E:\Documents\kuaishou\auto-video-editor
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'

# OpenStoryline 自己的测试(必须用 openstoryline 的 venv)
.\openstoryline\.venv\Scripts\python.exe -m pytest openstoryline\tests -q --junitxml="$env:TEMP\os.xml"

# 主仓回归(主仓 venv,分开跑)
.\.venv\Scripts\python.exe -m pytest tests -q --junitxml="$env:TEMP\main.xml"
.\.venv\Scripts\python.exe -m pytest agent_gate\tests -q -p no:cacheprovider --junitxml="$env:TEMP\ag.xml"
```

> 必须用 `--junitxml` 取准确计数 —— PowerShell 会吞掉 pytest 末行的 `N passed`。
> `PYTHONUTF8=1` 必设,否则子进程 GBK 输出会让 pytest capture 段 `UnicodeDecodeError`。


## 安装 (Windows)

```powershell
cd openstoryline
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
# 或分步:
#   python -m venv .venv
#   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
#   .\scripts\download_resources.ps1   # 约 526 MB
#   .\scripts\download_models.ps1      # 约 116 MB
```

## 填入 API Key

编辑 `config.toml`(从仓库 HEAD 拷的占位符版本),把
`<YOUR_DEEPSEEK_API_KEY>` / `<YOUR_MINIMAX_API_KEY>` 替换为真实 key。
**强烈建议**在 `config.toml.local` 里覆盖(`.gitignore` 已排除),保留 `config.toml` 是占位符:

```toml
# config.toml.local (不入库)
api_key = "sk-..."
```

启动时 `agent_fastapi.py` 默认先读 `config.toml` 再尝试 `config.toml.local` 覆盖。

## 手工启动 (调试用)

```powershell
.\.venv\Scripts\python.exe -m uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860
```

浏览器打开 <http://127.0.0.1:7860>,上传素材、与 Agent 对话完成分镜/文案/BGM/时间线规划。
规划完成后,产物会落在 `outputs/<session_id>/plan_timeline_pro/plan_timeline_pro_*.json`。

## 文件来源(对照上游)

| 本目录 | FireRed-OpenStoryline 上游 |
|---|---|
| `agent_fastapi.py` | 仓库根 `agent_fastapi.py`(原样) |
| `web/` | 仓库根 `web/`(原样) |
| `src/open_storyline/` | 仓库根 `src/open_storyline/`(原样,19 core_nodes + mcp + storage + utils + agent.py + config.py) |
| `prompts/` | 仓库根 `prompts/`(原样) |
| `config.toml` | 仓库根 `config.toml`(本仓 HEAD 是已 scrub 占位符) |
| `requirements.txt` | 上游 `scripts/requirements.txt`(原样) |
| `LICENSE` | 仓库根 `LICENSE`(Apache-2.0) |
| `scripts/download_resources.ps1` | 上游 `scripts/download_resources.ps1`(原样) |
| `scripts/download_models.ps1` | 新写,基于上游 `scripts/download.sh` 的 models 部分 |

## 上游 commit 引用

本仓备份时基于 upstream `b45011d1` (chore: add web clean-cache UI + endpoint)。
后续上游有重要更新时,在本仓 review 后 cherry-pick 即可。
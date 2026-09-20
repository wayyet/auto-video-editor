# OpenStoryline (auto-video-editor 嵌入版)

> 本目录是 [FireRed-OpenStoryline](https://github.com/wayyet/FireRed-OpenStoryline)
> (Apache-2.0) 的内嵌副本。代码沿用上游,主仓库 `auto-video-editor` 通过本地
> uvicorn 子进程 + 磁盘产物读盘方式与其对接,不再使用 MCP/stdio 链路。

## 与上游的差异

- 不再有 MCP server(`src/open_storyline/mcp/` 保留为对照文档,不被运行时调用)
- 启动入口固定为 `uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860`
- 产物写到本目录的 `outputs/<session_id>/plan_timeline_pro/*.json`,
  由 `auto-video-editor/nodes/node_04_import_and_plan.py` 通过
  `storyline.plan_reader.read_latest_plan_timeline_pro()` 读盘拍平为
  `CanonicalTimeline`。

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
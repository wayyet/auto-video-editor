# OpenStoryline 网页报错「ffprobe 找不到」排查与修复

> 日期：2026-07-04　|　页面：http://127.0.0.1:7860（VS Code Edge Tools 预览）

## 一、现象

在 OpenStoryline 网页界面（7860 端口）处理视频时报错：**ffprobe 找不到**。

## 二、排查过程

| 步骤 | 检查项 | 结果 |
|------|--------|------|
| 1 | 当前 shell 里 `Get-Command ffprobe / ffmpeg` | 两者均 NOT FOUND |
| 2 | 项目代码搜索 `ffprobe` | `src/open_storyline/utils/util.py`（读视频旋转元数据）与 `src/open_storyline/nodes/core_nodes/asr_node.py`（探测音轨 + 抽取 wav），均以**裸命令名**走 `subprocess`，依赖进程 PATH |
| 3 | 剪映目录找可复用二进制 | `JianyingPro\5.9.0.11632\` 只有 `ffmpeg.exe`，**没有 ffprobe.exe**，不能直接复用 |
| 4 | `winget install Gyan.FFmpeg` | 提示**已安装 8.1.2**，无可升级 |
| 5 | 注册表用户 PATH | 已包含 `...\WinGet\Packages\Gyan.FFmpeg_...\ffmpeg-8.1.2-full_build\bin`，目录内 ffmpeg/ffprobe/ffplay 三件套齐全且可运行 |
| 6 | 进程时间线 | OpenStoryline 服务 **21:21** 启动；FFmpeg **23:23** 才装好 |

## 三、根因

**FFmpeg 其实早已装好、用户 PATH 也已写入注册表；但 7860 服务（及整条 VS Code 进程链）是在 FFmpeg 安装前启动的，继承的是旧 PATH 快照**，因此 `subprocess` 调 `ffprobe` 报 `FileNotFoundError`。不是缺安装，而是「装完没重启进程」。

## 四、修复动作

1. 按命令行匹配停掉 4 个旧 python 进程（`open_storyline.mcp.server` ×2、`uvicorn agent_fastapi:app` ×2）。
2. 在启动会话内刷新 PATH：`$env:Path = [Machine PATH] + ';' + [User PATH]`（从注册表现取），并确认 `ffprobe` 已可解析。
3. 设 `PYTHONPATH=src`，用项目 `.venv\Scripts\python.exe` 按 openstoryline-launcher 流程 `Start-Process` 重启 MCP 服务与网页界面（注意：skill 文档写 D 盘，真实路径是 `e:\Documents\kuaishou\FireRed-OpenStoryline`）。

## 五、验证结果

- 新服务进程继承了含 FFmpeg bin 的 PATH（启动前在同一会话验证 `ffprobe` 可解析）。
- `http://127.0.0.1:7860` 返回 **HTTP 200**（25KB 页面），服务形态与之前一致（每服务 1 父 1 子共 4 个 python 进程，属正常）。
- 端到端验证：请在网页里重新上传/处理一次视频，ASR 抽音轨环节不应再报 ffprobe 错误。

## 六、后续注意

- **本 VS Code 窗口是旧 PATH 环境**：以后若在这个窗口的终端里手动重启服务，要先执行 `$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')` 再启动；或干脆重启 VS Code 一劳永逸。
- 新开的独立终端 / 双击 .bat 启动不受影响（会读到新 PATH）。
- Edge Tools 预览页里上传按钮失效是其设计限制（见既有记忆），与本问题无关。

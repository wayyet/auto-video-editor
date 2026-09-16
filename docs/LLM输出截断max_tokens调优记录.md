# LLM 输出截断（max_tokens）调优记录

> 日期：2026-07-17  
> 页面：`http://127.0.0.1:7860/`（OpenStoryline）  
> 错误：`LLMOutputTruncatedError`，`max_tokens=18888` 不够

---

## 1. 问题现象

在 Edge 浏览器打开的 OpenStoryline 网页（`http://127.0.0.1:7860/`）中，剪辑流程执行到「生成文案」节点时报错：

```text
LLMOutputTruncatedError
LLM 输出被截断，max_tokens=18888 不够
```

会话历史中助手提示为第 7 步失败，并尝试重试。

---

## 2. 根因分析

### 2.1 运行时证据

从会话状态文件确认失败节点为 `generate_script`：

- 路径：`FireRed-OpenStoryline/outputs/129e72b63c8b44a392ff3d9c8c12f105/session_state.json`
- 堆栈：`generate_script.py` → `sampling_requester.py`
- `stopReason == maxTokens` 时抛出 `LLMOutputTruncatedError`

### 2.2 原因说明

MiniMax-M3 等推理模型的 `<think>` 思考段会计入 `max_tokens` 预算。  
全片文案生成（多组 clip + 长 prompt）时，思考段会占满 18888，正文 JSON 被硬截断。

### 2.3 涉及的硬编码/配置位置

| 文件 | 原值 | 说明 |
|------|------|------|
| `FireRed-OpenStoryline/config.toml` → `[group_clips]` | `base_max_tokens` / `max_tokens_cap` = 18888 | 镜头分组预算 |
| `.../nodes/core_nodes/filter_clips.py` | `max_tokens=18888` | 镜头筛选 |
| `.../nodes/core_nodes/generate_script.py` | `max_tokens=18888` | **本次报错节点** |

---

## 3. 排查过程摘要

### 3.1 浏览器调试尝试

目标：读取 Edge 中已打开的 `7860` 标签页，不新开窗口。

| 方式 | 结果 |
|------|------|
| CDP `http://127.0.0.1:9222/json/list` | 失败：9222 及常见调试端口均未监听 |
| `playwright-cli attach --extension=msedge` | 失败：Edge 未安装 Playwright Extension |
| `playwright-cli open ... --browser=msedge` | 成功打开页面，但会**新开** Edge 窗口 |
| HTTP 探测 `GET http://127.0.0.1:7860/` | 成功：HTTP 200，服务在线 |

结论：现有 Edge 标签页未开启远程调试端口时，无法用 CDP / Extension 附着；错误信息改从后端 `session_state.json` 取得。

### 3.2 若需附着已有 Edge（不新开窗口）

1. 在 Edge 安装 [Playwright Extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm)，然后：
   ```powershell
   playwright-cli attach --extension=msedge
   playwright-cli tab-list
   playwright-cli snapshot
   ```
2. 或以 `--remote-debugging-port=9222` 重启 Edge，再：
   ```powershell
   playwright-cli attach --cdp=http://127.0.0.1:9222
   ```

---

## 4. 修复内容

经用户确认后，将三处 `18888` 统一改为 **28888**：

### 4.1 `config.toml`

```toml
[group_clips]
base_max_tokens = 28888
max_tokens_cap = 28888
```

### 4.2 `filter_clips.py`

```python
max_tokens=28888,
```

### 4.3 `generate_script.py`

```python
max_tokens=28888,
```

---

## 5. 服务重启

修改后重启 OpenStoryline 两个本地服务（代码由进程加载，需重启生效）：

| 服务 | 端口 | 命令要点 |
|------|------|----------|
| MCP | 8001 | `.venv\Scripts\python.exe -m open_storyline.mcp.server` |
| 网页界面 | 7860 | `uvicorn agent_fastapi:app --host 127.0.0.1 --port 7860` |

重启后校验：

- `GET http://127.0.0.1:7860/` → HTTP 200
- `load_settings` → `base_max_tokens=28888`，`max_tokens_cap=28888`

---

## 6. 验证与清理

- 用户复现「生成文案」步骤后确认问题已修复。
- 调试埋点（写入 `debug-1e0587.log` 的临时日志）已从以下文件移除：
  - `generate_script.py`
  - `sampling_requester.py`
  - `group_clips.py`
- 临时探测脚本与日志文件已删除。

---

## 7. 经验小结

1. 推理模型的 `max_tokens` ≠「答案长度」，预算 = **思考 + 答案**。
2. `LLMOutputTruncatedError` 应优先查节点硬编码与 `config.toml` 的 token 上限，而不只看页面文案。
3. 网页 UI（7860）与 MCP（8001）都会加载节点代码；改配置/源码后需**两个服务一起重启**。
4. 附着已有浏览器标签依赖 CDP 或 Playwright Extension；未开启时改查 `outputs/<session_id>/session_state.json` 更可靠。

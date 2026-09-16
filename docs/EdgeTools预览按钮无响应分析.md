# Edge Tools 预览 http://127.0.0.1:7860/ 按钮无响应 — 原因分析

> 日期：2026-07-04
> 场景：在 VS Code 里用 Microsoft Edge Tools 扩展打开 OpenStoryline 网页界面（http://127.0.0.1:7860/）后，点击页面里的按钮全部没有反应。

## 一、结论（先说答案）

**网页和后端服务本身完全正常，问题出在 VS Code Edge Tools 的「嵌入式浏览器预览」这一层。**

Edge Tools 的内嵌预览不是真浏览器控件，而是一个 **CDP 投屏（screencast）**：

1. 扩展在后台启动一个 **无头（headless）Edge 实例** 去真正加载页面；
2. 把渲染画面以截图帧的形式回传到 VS Code 的 webview 面板里显示；
3. 你在面板里的鼠标点击，被反向翻译成 CDP 的 `Input.dispatchMouseEvent` 注入给无头 Edge。

这条「画面下行 + 输入上行」的链路任何一环出问题，页面看起来正常渲染，但点什么都没反应。

## 二、排查过程与证据

| 排查项 | 方法 | 结果 |
|--------|------|------|
| 服务是否存活 | `Get-NetTCPConnection -LocalPort 7860` + `Invoke-WebRequest` | 正常监听（PID 25652），HTTP 200 |
| 前端按钮绑定时机 | 阅读 `web/static/app.js` bootstrap 流程 | `bindUI()` 在任何网络请求之前执行（app.js:2583），不存在「接口失败导致按钮没绑定」的可能 |
| CDN 依赖是否致命 | 阅读 app.js:924 | marked / DOMPurify 缺失时有降级逻辑，不影响按钮 |
| 页面在真实 Edge 内核里是否可用 | Playwright + 本机 Edge（`channel="msedge"`，无头）实测 | **全部通过**：无 JS 报错、无失败请求；点击「收起侧边栏」生效、点击骰子按钮成功填入提示语、会话自动创建成功 |
| 系统显示缩放（投屏坐标偏移诱因） | 注册表 `AppliedDPI` | 96 → 100%，排除 |
| VS Code 缩放设置 | settings.json 搜 `zoomLevel` | 未设置，排除 |
| 页面能否被 iframe 内嵌（Simple Browser 可行性） | 检查响应头 | 无 X-Frame-Options / CSP 限制，可内嵌 |

由于同一台机器、同一 Edge 内核下页面交互完全正常，可以**确定性地排除**网页代码、FastAPI 后端、CDN、系统代理这些因素，唯一剩下的变量就是 Edge Tools 的投屏预览层。

## 三、具体原因（两种情形）

### 情形 A：所有按钮都没反应（包括「收起侧边栏」这种纯前端按钮）

说明投屏的**输入上行通道断了**，常见触发方式：

- 投屏会话掉线/冻结：面板里显示的只是最后一帧静态截图，点击根本没有被转发（CDP 一个 target 同时只允许一个投屏客户端，重复 attach、扩展重启、无头实例崩溃都会造成这种"假活"画面）；
- Edge Tools 扩展自身的输入转发 bug（在 GitHub vscode-edge-devtools 仓库属于高频 issue，和扩展版本相关）；
- 面板没有获得焦点，或 VS Code 界面缩放/系统 DPI 导致点击坐标错位（本机已排除缩放因素）。

### 情形 B：部分按钮没反应（上传素材、Github/Docs/Node Map 链接），但侧边栏能收起

这是投屏模式**设计上的限制**，不是 bug：

- 「上传素材」按钮实际是触发 `<input type="file">` 的系统文件选择框 —— 无头浏览器里**根本弹不出来**；
- Github / Docs / Node Map 都是 `target="_blank"` 新窗口链接 —— 投屏不支持开新窗口，点了就是没反应；
- alert / confirm / 下载 / 权限弹窗在投屏里同样不可用。

OpenStoryline 这个页面恰好重度依赖文件上传和新窗口链接，所以在 Edge Tools 投屏里体验会严重残缺 —— **这个页面本质上不适合在 Edge Tools 内嵌预览里操作**。

## 三点五、用户确认结果（2026-07-04）

用户确认属于**情形 B**：只有部分按钮没反应（上传素材、Github/Docs/Node Map 链接），侧边栏可以正常收起。
→ 定性为 **Edge Tools 投屏模式的设计限制**：无头投屏不支持文件选择框和 `target="_blank"` 新窗口，这不是 bug，也无法通过升级扩展解决。OpenStoryline 页面重度依赖这两类交互，不适合在 Edge Tools 内嵌预览里操作。

## 已执行的修复（2026-07-04）

1. VS Code 用户 settings.json 写入 `"vscode-edge-devtools.headless": false` —— 之后用 Edge Tools 打开会弹出真实可交互的 Edge 窗口，DevTools 留在 VS Code 里；
2. 已用系统默认浏览器打开 http://127.0.0.1:7860/ 验证；
3. 顺带修复 settings.json 里 `http.noProxy` 的错误写法：原来是 `["localhost，127.0.0.1:"]`（一个字符串 + 中文逗号 + 尾冒号，规则完全失效），改为 `["localhost", "127.0.0.1"]`。在配置了全局代理 `http.proxy: http://127.0.0.1:3067` 且 `http.proxySupport: override` 的情况下，这条坏规则会让 VS Code 内部对本地服务的请求也被送进代理，是本地调试的隐患。

## 四、解决方案（按推荐顺序）

1. **正经使用就用真浏览器**：直接系统默认浏览器打开（`openstoryline-launcher` 本来就是这么做的）：
   ```powershell
   Start-Process 'http://127.0.0.1:7860/'
   ```
2. **既要 DevTools 调试又要能操作**：把 Edge Tools 改为「非无头」模式，让它弹出一个真实可交互的 Edge 窗口，DevTools 留在 VS Code 里：
   ```jsonc
   // VS Code settings.json
   { "vscode-edge-devtools.headless": false }
   ```
3. **只想在 VS Code 里快速预览**：用内置 Simple Browser（`Ctrl+Shift+P` → `Simple Browser: Show`）。已验证页面无 iframe 限制，可以内嵌；普通按钮可点，但文件上传、新窗口链接仍受 webview 环境限制。
4. 如果坚持用 Edge Tools 投屏且属于情形 A：先关闭该预览面板重新打开（重建投屏会话），并把 Edge Tools 扩展升级到最新版。

## 五、相关文件

- 后端：`FireRed-OpenStoryline/agent_fastapi.py`（FastAPI + WebSocket，监听 127.0.0.1:7860）
- 前端：`FireRed-OpenStoryline/web/index.html`、`FireRed-OpenStoryline/web/static/app.js`（ES module，bootstrap 时统一绑定按钮事件）
- 验证脚本（临时）：Playwright + 本机 Edge 无头实测，确认页面交互正常

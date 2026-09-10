# Docker Desktop 走本地 SOCKS 代理拉镜像 — 排查与配置记录

> 时间：2026-09-09 · 范围：本机（Windows 11 + Docker Desktop 29.7.2 / WSL2 后端）

## 1. 目标

让 Docker Desktop 通过本机已有的 SOCKS 代理出网，能正常 `docker pull` 公网镜像，并给出可复现的验证步骤。

## 2. 环境探测结论

### 2.1 本机代理拓扑

| 代理 | 端口 | 协议 | 绑定地址 | 来源进程 | 生命周期 |
|---|---|---|---|---|---|
| MoTTY（MobaXterm SSH 动态转发） | **1088** | SOCKS5 | `0.0.0.0`（含 LAN/WSL2） | `MoTTY.exe` | 随 SSH 会话 |
| Karing（Clash 内核客户端） | **3067** | SOCKS5 + HTTP | `127.0.0.1`（仅本机回环） | `karingService.exe` | 系统服务 |

外网出网验证：`http://cp.cloudflare.com/` 经两条代理都返回 `204 No Content`。

### 2.2 WSL2（Docker 守护进程视角）的连通性

| WSL2 → Windows 目标 | 1088 (MoTTY SOCKS5) | 3067 (Karing) |
|---|---|---|
| `host.docker.internal` (= `192.168.31.38`) | ✅ | ❌（Karing 仅绑 loopback） |
| WSL 默认网关 `172.27.224.1` | ✅ | ❌ |

WSL2 内部地址：
- `host.docker.internal` 解析为 `192.168.31.38`（Windows 主机的 LAN IP）。
- WSL 默认网关 `172.27.224.1`。

**结论**：Karing 不改绑定就 WSL2 摸不到；当前唯一可达的代理是 MoTTY SOCKS5 `1088`。

### 2.3 Docker Desktop 配置入口（关键发现）

Docker Desktop 的真正生效配置不在 GUI 也不在 `%APPDATA%\Docker\settings.json`，而在：

```
%APPDATA%\Docker\settings-store.json
```

生效段是 `vm.proxy.{http,https,exclude,mode}`。`docker info` 输出里看到的 `HTTP Proxy: http.docker.internal:3128` 是公司托管代理层（不可改），与 `vm.proxy` 是两层独立配置；真正控制 WSL2 内 `dockerd` 出网的是 `vm.proxy`。

## 3. 计划 vs 实际

最初计划假设 "Docker 守护进程不支持 SOCKS，只接受 HTTP 代理"，所以规划里写了：

1. 用 `gost` 把 SOCKS5 转 HTTP（监听 `127.0.0.1:8118`）。
2. 把 Docker Desktop 指向 `http://127.0.0.1:8118`。

实施时验证发现 **Docker Desktop 的 `vm.proxy.http/https` 字段原生接受 `socks5://` URL**（不需要 gost 中继）。所以最终方案比计划更简洁：

| | 计划 | 实际 |
|---|---|---|
| 中继进程 | `gost`（HTTP→SOCKS） | ❌ 不需要 |
| Docker 配置 | `%APPDATA%\Docker\settings.json` 的 `proxies.*` | `%APPDATA%\Docker\settings-store.json` 的 `vm.proxy.*` |
| 代理协议 | `http://127.0.0.1:8118` | `socks5://host.docker.internal:1088` |

## 4. 实际配置

### 4.1 编辑 `settings-store.json`

```json
{
  "vm": {
    "proxy": {
      "exclude": "localhost,127.0.0.1,::1,host.docker.internal,172.27.224.1,192.168.31.38,hubproxy.docker.internal",
      "http":  "socks5://host.docker.internal:1088",
      "https": "socks5://host.docker.internal:1088",
      "mode":  "manual"
    }
  }
}
```

`exclude` 把 Windows 回环、WSL2 内部地址、本机 LAN IP 都加进去，避免 dockerd 经代理回环访问本地服务。

### 4.2 让 Docker Desktop 重读配置

```powershell
docker desktop restart
```

WSL2 docker-desktop distro 会被回收并按新配置重建。MoTTY 进程不受影响。

## 5. 验证结果

### 5.1 拉镜像

```
$ docker pull hello-world
Status: Image is up to date for hello-world:latest

$ docker pull alpine
55afa1ecc21d: Pull complete
56dceff11b33: Download complete
f5124fb579e2: Download complete
Digest: sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
```

### 5.2 跑容器

```
$ docker run --rm hello-world
Hello from Docker!
This message shows that your installation appears to be working correctly.

$ docker run --rm alpine echo "alpine OK inside container"
alpine OK inside container
```

### 5.3 数据流观察

抓包期间 `MoTTY.exe`（PID 18348）CPU 持续 ~100%，监听 `0.0.0.0:1088`，同时有 ~30 个 ESTABLISHED 连接进/出（Docker Desktop 内部 HTTP 代理进程 + 浏览器进程）。WSL2 → `host.docker.internal:1088` → MoTTY → 外网，链路通。

实测拉 alpine（13MB）期间 WAN 接收速率约 **67 KB/s**——受限于 SSH 隧道加密转发。

## 6. 注意事项 / 后续

- **MoTTY SOCKS5 生命周期 = SSH 会话**。MobaXterm 的 SSH 转发断开 → Docker 拉镜像立即失败；SSH 重连后 dockerd 自动恢复（不需要重启 Docker Desktop）。
- **拉镜像速率上限 = SSH 隧道吞吐**。需要更稳更快，切换为 Karing：
  1. 在 Karing 设置里勾选"允许局域网连接"，使其绑 `0.0.0.0`。
  2. 把 `vm.proxy.http/https` 改成 `socks5://host.docker.internal:3067`。
  3. `docker desktop restart`。
- **Karing 当前配置未动**——按需启用。
- **`gost.exe` 残留**：计划里下载了 `gost.exe` 到 `.docker-proxy/` 目录，但实际方案不需要，已停止该进程。中继目录可保留也可清理。

## 7. 相关文件 / 进程

- `%APPDATA%\Docker\settings-store.json` — Docker Desktop 代理配置（已修改）。
- `%APPDATA%\Docker\settings-store.json.bak-20260909-2148` — 改动前的自动备份。
- `E:\Documents\kuaishou\auto-video-editor\.docker-proxy\gost.exe` — 计划用的中继二进制（实际未使用）。
- 进程：`MoTTY.exe`（PID 18348，SOCKS5 1088）、`karingService.exe`（PID 18476，SOCKS+HTTP 3067）。
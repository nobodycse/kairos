# 服务器到货部署清单（Phase 0 落地验收）

> 目标机：轻量应用服务器 4核8G 5M（上海），系统 **Ubuntu 22.04 纯系统镜像**。
> 全程只做三段：**到手初始化 → 一键脚本 → 验收三关**。所有命令可直接复制。
> 原理与架构背景见 architecture.md；本文只讲"做什么、怎么验证、出问题看哪"。

---

## 0. 购买配置确认

| 项 | 应选 | 说明 |
|---|---|---|
| 镜像 | **Ubuntu Server 22.04 LTS**（纯系统） | 不选"预装 Docker/宝塔"等应用镜像——setup_server.sh 自己装 Docker，版本可控 |
| 规格 | 4核8G | 项目设计档位（setup_server.sh 头部注释） |
| 地域/带宽 | 上海 / 5M | 演示与单人访问足够 |

---

## 1. 到手初始化（第一小时）

### 1.1 控制台防火墙（⚠️ 第一坑：和机器内 ufw 是两道独立的门）

轻量服务器控制台 → 防火墙（安全组）→ 添加规则：

| 端口 | 来源 | 用途 |
|---|---|---|
| 22 | 全部（或你的 IP） | SSH |
| 80 | 全部 | 网站 |
| 9000 | **建议限制为 GitHub/Gitee IP 段** | Git 平台 WebHook 部署触发 |

不开 9000，push 后平台的 webhook 会一直超时重试——这是最常见的"脚本明明跑成功了却不自动部署"的原因。

### 1.2 登录与基础检查

```bash
ssh root@<公网IP>
uname -m                 # x86_64
free -h                  # 内存约 7.6G 可用即正常
curl -m 5 https://get.k3s.io -o /dev/null -w '%{http_code}\n'   # 期望 200，验证外网可达
```

---

## 2. 一键脚本

### 2.1 克隆仓库（私有仓库会提示输入 GitHub 账号/PAT 令牌）

```bash
git clone https://github.com/<你的用户名>/kairos.git /opt/kairos/repo
# 或先 clone 到任意目录，脚本检测后自动克隆到 /opt/kairos/repo
```

### 2.2 执行（约 5–15 分钟，取决于网络）

```bash
cd /opt/kairos/repo
sudo bash scripts/setup_server.sh
```

脚本自动完成（出问题时按此对照是哪一步）：

1. 安装 Docker（官方 apt 源）
2. 安装 k3s（`--disable=traefik --docker`，运行时用宿主机 Docker）
3. 等节点 Ready、建 `/opt/kairos/data/*` 数据目录
4. 克隆/校验仓库
5. 生成 `deploy/compose/.env`（K3S_SERVER_IP 自动探测回填）
6. 改写 kubeconfig（server 指向内网 IP，容器可达）
7. apply k8s manifests + 提取 prometheus-token
8. 构建 `kairos/demo-app:latest` 镜像
9. 启动平台层 + 监控栈两套 compose
10. 安装并启动 `kairos-webhook` systemd 服务

### 2.3 手动收尾（脚本管不了的三件事）

**① 改 .env 密码**（`/opt/kairos/repo/deploy/compose/.env`，全部 CHANGE_ME 必须清零）：

| 变量 | 填什么 |
|---|---|
| `POSTGRES_PASSWORD` | 随机串，`openssl rand -hex 16` |
| `JWT_SECRET` | 随机 32+ 字符，`openssl rand -hex 32` |
| `GRAFANA_ADMIN_PASSWORD` | 你自己的 Grafana 密码 |
| `WEBHOOK_SECRET` | 随机串——**下一步平台 WebHook 要填同一个值** |
| `ADMIN_INITIAL_PASSWORD` | 初始 admin 账号密码（首次部署跑迁移时创建用户） |
| `LLM_API_KEY` | DeepSeek 等 API Key（Phase 2 才用，可先填占位） |
| 其余（`DATABASE_URL`/`REDIS_URL`/`KUBECONFIG`/`K3S_SERVER_IP`） | 保持默认，不用动 |

改完重启平台层使新密码生效：

```bash
cd /opt/kairos/repo/deploy/compose
docker compose -f docker-compose.yml up -d --force-recreate backend
```

**② 配 WebHook（GitHub：Settings → Webhooks → Add webhook；Gitee：仓库管理 → WebHooks → 添加）**

- URL：`http://<公网IP>:9000/deploy`
- Secret/密码：与 `.env` 的 `WEBHOOK_SECRET` 一致（GitHub 需选 Content-Type `application/json`）
- 事件：仅 Push；点击"测试"应返回 202

**③ 验证 systemd 服务**：

```bash
systemctl status kairos-webhook        # active (running)
curl -s http://127.0.0.1:9000/health   # {"status": "ok"}
```

---

## 3. Phase 0 验收（三关）

| 关 | 操作 | 通过标准 |
|---|---|---|
| ① 部署链路 | 浏览器打开 `http://<公网IP>` | 看到登录页（nginx → 前端静态资源） |
| ② CI/CD 闭环 | 本地随便改个文件 push main → `journalctl -u kairos-webhook -f` | 日志出现 "deploy 成功"，约 1–2 分钟后页面体现改动 |
| ③ 集群与监控 | 见下 | demo-app Running；Grafana 三看板加载 |

第③关细览：

```bash
kubectl get pods -n demo          # 两个 payment-service 均 Running
kubectl get pods -n monitoring    # node-exporter / kube-state-metrics / promtail 正常
```

Grafana 按架构约定只绑回环，SSH 隧道查看（本机执行）：

```bash
ssh -L 3000:127.0.0.1:3000 root@<公网IP>
# 浏览器开 http://127.0.0.1:3000 → KAIROS 文件夹下三看板应已自动加载
```

---

## 4. 故障速查

| 症状 | 先查 |
|---|---|
| push 后无任何反应 | 控制台防火墙 9000 是否放行；平台 webhook 测试返回什么；`systemctl status kairos-webhook` |
| webhook 日志 401 | `.env` 的 `WEBHOOK_SECRET` 与平台填的 Secret 不一致 |
| 部署日志报 CHANGE_ME 警告 | §2.3-① 没做完，改 .env 后 recreate backend |
| 页面 502/健康检查失败自动回退 | `deploy.sh` 会自动退回上一版本并保留日志（`docker compose logs backend`），修好后再 push 即可——**线上不会因此长期挂掉** |
| 镜像拉取失败 | 国内网络问题，监控镜像可改用 `docker.m.daocloud.io` 前缀（本地已验证可行）；kube-state-metrics 可换 bitnami 源（见 deploy/kubernetes/README.md） |
| k3s 节点 NotReady | `journalctl -u k3s -f`；确认控制台安全组没拦 6443 本机回环 |

---

## 5. 验收完成后

Phase 0 正式关闭，进入 Phase 1（monitoring 三客户端、webhook 落库、前端 Dashboard）。
本地联调栈 `deploy/compose/docker-compose.dev.yml` 仍是 Phase 1 的开发底座，
与服务器互不影响：本地写代码 → push → 服务器自动更新。

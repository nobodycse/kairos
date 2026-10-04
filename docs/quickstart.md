# KAIROS 快速开始：从零部署到看到 AI 自动修故障

> 面向第一次使用本项目的用户。跟着做约 30–40 分钟（大部分是等脚本跑完），
> 就能在一台全新云服务器上跑通完整闭环：**注入故障 → AI 采集证据诊断 → 风险评估 → 人工批准 → 自动修复 → 验证恢复**。
> 有 Kubernetes / Docker 经验可只看 §2 和 §4 的命令块。

---

## 0. 先搞清楚三个概念（1 分钟）

1. **KAIROS 是"中心化平台"，不是装在被监控机器上的探针**。它装在一台服务器上，监控的是一个 **Kubernetes 集群**；看图表用任何设备的浏览器即可，不要求登录服务器。
2. **默认部署是"三合一"**：脚本会在同一台服务器上装好 平台 + 一个单节点 k3s 集群（演示用被测对象）+ 演示应用。想监控自己已有的集群见 §5。
3. **AI 修复是"提议—批准—执行"三段式**：LLM 只提出修复方案，中高风险动作必须人工在网页上点批准才会执行，全程审计留痕。默认动作集里没有删业务 Pod 这类越权操作（删 Pod 只允许删不属于任何工作负载的独立 Pod）。

## 1. 准备清单

| 需要 | 说明 |
|---|---|
| 一台 Ubuntu 22.04 云服务器 | 推荐 4核8G（跑平台 + 演示集群）；2核4G 只能跑平台层，无法体验故障注入 |
| root 或 sudo 权限的 SSH 登录 | 初始化脚本要装 Docker 和 k3s |
| 服务器能访问外网 | 下载 Docker/k3s/基础镜像；国内服务器脚本已内置镜像加速 |
| 一个大模型 API Key | 任意 OpenAI 兼容服务：DeepSeek / 通义 / Kimi / 自建中转。DeepSeek 充 10 元足够跑完所有演示 |
| 云安全组放行 | `80/tcp`（Web 入口）、`22/tcp`（SSH） |

> 用 **git clone** 获取代码，**不要下 zip 包**——部署脚本依赖 git 仓库元数据。

## 2. 五步部署

### 第 1 步：克隆仓库

```bash
git clone https://github.com/nobodycse/kairos.git && cd KAIROS
```

### 第 2 步：生成并填写配置

```bash
cp deploy/compose/.env.example deploy/compose/.env
vi deploy/compose/.env
```

**必须修改的项**（都有 `CHANGE_ME` 占位，搜一遍改干净）：

| 变量 | 填什么 |
|---|---|
| `LLM_API_KEY` | 你的大模型 Key（也可以先留空，部署后在网页"系统设置"里配，见 §3 第 2 步） |
| `ADMIN_INITIAL_PASSWORD` | KAIROS 网页的初始登录密码（登录后建议再改） |
| `JWT_SECRET` | 随机 32+ 字符：`openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | 数据库密码：`openssl rand -hex 16` |
| `GRAFANA_ADMIN_PASSWORD` | Grafana 监控看板的登录密码 |
| `GRAFANA_ROOT_URL` | Grafana 的浏览器访问地址：SSH 隧道/本机访问保持默认；**公网部署改为 `http://<你的服务器IP>/grafana`**，否则看板页面会跳转到错误地址 |
| `WEBHOOK_SECRET` | 自动部署 webhook 的校验口令：`openssl rand -hex 16` |

`LLM_BASE_URL` / `LLM_MODEL` 按你的供应商改（默认 DeepSeek）。`K3S_SERVER_IP` 保持模板值即可，脚本会自动探测本机 IP 回填。

### 第 3 步：一键初始化

```bash
sudo bash scripts/setup_server.sh
```

这一条命令会自动完成（可重复执行，已装的部分自动跳过）：装 Docker → 装单节点 k3s（已禁用 traefik，避免占用 80 端口）→ 配置国内镜像加速 → 部署演示应用和监控三件套（node-exporter / kube-state-metrics / promtail）→ 构建 demo 镜像 → 启动平台层与监控栈 → 安装自动部署钩子 → 放行防火墙端口。

耗时 5–15 分钟（取决于网络）。看到 `[setup] 完成` 即成功；中途失败直接重跑。

### 第 4 步：打开网页登录

浏览器访问 `http://<服务器IP>/`，账号 `admin`，密码是 `.env` 里的 `ADMIN_INITIAL_PASSWORD`。

> 打不开？先确认安全组放行了 80 端口，且地址是 **http://** 开头（本系统未配 HTTPS，浏览器自动跳 https 会失败）。

### 第 5 步：配置 AI 供应商（若第 2 步没填 Key）

侧边栏「系统设置」→ 填 Base URL / API Key / 模型名 → 点「测试连接」→ 提示成功后保存。网页配置优先于 `.env`，之后换模型不用动服务器。

## 3. 第一次闭环体验（10 分钟导览）

1. **注入一个真实故障**：侧边栏「故障实验室」→ 新建实验 → 类型选 **oom（内存溢出）** → 提交后**自动开始注入**（若实验停在"已创建"状态——比如与上一实验互斥撞车——点实验详情里的「注入故障」补发）。系统会把演示应用的内存上限临时调到 32Mi，Pod 很快被 OOMKill。
2. **看 AI 诊断**：一分钟内「故障事件」页出现新事件，点进诊断页能实时看到 Agent 的时间线——查 Pod 状态、查 Events、查日志、查指标（四路证据），最后给出根因（置信度）与修复方案。
3. **人工批准**：事件进入「待人工确认」后弹确认框：动作、目标、**风险等级**、参数一目了然。OOM 演示的修复是"把内存限额调回 512Mi"（中风险，需确认）→ 点「批准」。
4. **自动验证**：修复执行后系统进入 3 分钟观察窗口（页面能看到逐采样点的验证进度），指标全部恢复后事件变「已恢复」，显示 MTTR。
5. **看评估报告**：「历史与报告」页有诊断准确率 / 自动恢复率 / MTTR 的统计图表；「故障实验室」页的实验报告有修复前后四指标对比曲线。

> 想试更"刺激"的：cpu_overload（CPU 过载）实验会创建一个独立压力 Pod，AI 会提议**删除异常 Pod**（高风险动作，同样需你批准）。注意这类告警因 PromQL 统计窗口有约 10 分钟的结构性延迟，注入后要耐心等。
> pod_crash（Pod 崩溃）实验介于两者之间，约 8–11 分钟闭环。

## 4. 监控看板（Grafana）

浏览器访问 `http://<服务器IP>/grafana`，账号 `admin`，密码 `GRAFANA_ADMIN_PASSWORD`。

KAIROS 文件夹下三块看板：**被监控集群 · 集群层（K8s）**（节点/Pod/重启/资源水位）、**被监控集群 · 应用层（demo-app）**（QPS/5xx/延迟）、**KAIROS 自身**（平台容器资源）。集群层和应用层看板带**告警注解**：每次告警触发/恢复会自动在曲线上打红线/绿线，演示时故障窗一目了然。

> Grafana 密码只在首次建库时从 `.env` 读取；若之后改了 `.env` 里的密码，需要 `docker exec kairos-monitor-grafana-1 grafana cli admin reset-admin-password <新密码>` 同步。

## 5. 进阶：监控我自己已有的 Kubernetes 集群

平台与被测集群是**配置级耦合**，完全可以分机部署：

1. 平台照 §2 装在 A 机器（被测集群不在 A 上也能跑）；
2. 被测集群装观测三件套：`kubectl apply` 仓库里 `deploy/kubernetes/` 的 `node-exporter.yaml`、`kube-state-metrics.yaml`、`prometheus-rbac.yaml`、`promtail.yaml`（promtail 的 ConfigMap 先按 `deploy/kubernetes/README.md` 的顺序创建）；
3. 把被测集群的 kubeconfig 放到平台服务器，改 `.env` 的 `KUBECONFIG` 指向后 `docker compose up -d backend`；
4. Prometheus 抓取目标（`deploy/observability/prometheus/prometheus.yml`）与被测集群 Alertmanager 的 webhook 地址指向平台。

**边界提醒**：修复动作全部经 Kubernetes API 执行，所以本系统面向 **K8s 工作负载**——没有 K8s 的裸服务器可以采集指标，但走不完"诊断→修复"闭环。当前版本为单集群设计（一份 kubeconfig），多集群管理需要二次开发。

## 6. 代码更新后怎么重新部署

- **GitHub 自动部署（默认）**：仓库 Settings → Webhooks → 添加：Payload URL `http://<服务器IP>:9000/deploy`、Content-Type `application/json`、Secret 与 `.env` 的 `WEBHOOK_SECRET` 一致、触发事件仅 push——之后 push main 分支即自动构建部署，失败自动回滚上一版本；
- **手动**：在服务器上 `cd /opt/kairos/repo && git pull && bash scripts/deploy.sh`；
- 部署接收器对 Gitee（`X-Gitee-Token`）与 GitHub（`X-Hub-Signature-256`）两种签名**双兼容**，用 Gitee 托管的用户按 Gitee WebHook「密码」方式配置即可。

## 7. 常见问题（FAQ）

| 现象 | 原因与处理 |
|---|---|
| 页面打不开 | 安全组没放行 80；或浏览器把地址自动升级成 https（本系统只有 http）；或 80 被其它进程占用（`ss -ltnp | grep :80`） |
| Pod 一直 `ErrImagePull` | 国内拉不到基础镜像。脚本已写 `registries.yaml` 镜像加速；确认 `/etc/rancher/k3s/registries.yaml` 存在且 k3s 已重启 |
| 诊断一直失败 / 事件很快变「失败」 | 90% 是 AI 供应商问题：去「系统设置」点「测试连接」；检查 Key 余额；换一个模型试试 |
| 事件停在「待人工确认」 | 正常流程，等你在诊断页批准；系统不会未经确认执行中高风险动作 |
| 注入 CPU 过载后迟迟没有事件 | 正常，告警规则有 5 分钟统计窗 + 5 分钟持续时间，结构性延迟约 10 分钟 |
| Grafana 密码不对 | 密码只在首次建库时从 `.env` 读取，改 `.env` 不会自动生效，见 §4 的重置命令 |
| 实验创建后点注入报 409 | 上一个实验还没跑完（系统同时只允许一个进行中实验），等它 finished 或检查失败原因 |
| 想推倒重来 | `bash scripts/setup_server.sh` 可重复执行；彻底清空见 §9 |

## 8. 安全注意事项（公网部署必读）

- `.env` 里所有 `CHANGE_ME` 必须全部替换；`JWT_SECRET` 泄漏等于登录态可伪造；
- 公网只暴露 80 端口；数据库/Redis/Prometheus/Grafana 的端口全部只绑回环。9000（自动部署 webhook）建议在云安全组限制来源 IP；
- backend 默认持有被测集群的 **admin 权限 kubeconfig**——演示无妨，生产环境请换成最小权限 ServiceAccount（见 `docs/architecture.md` §11.1 的分层防护说明）；
- 大模型 API Key 在「系统设置」页保存后永不回显明文；`.env` 已被 `.gitignore` 排除，不要提交。

## 9. 卸载

```bash
cd /opt/kairos/repo/deploy/compose
docker compose -f docker-compose.yml down
docker compose -f docker-compose.monitor.yml down
/usr/local/bin/k3s-uninstall.sh          # 卸载 k3s 集群
sudo rm -rf /opt/kairos                  # 平台数据与配置（含数据库，谨慎）
```

---

更深的架构与设计文档见 [README.md](../README.md)、[docs/architecture.md](architecture.md)、[docs/design.md](design.md)；评估指标口径见 [docs/experiments.md](experiments.md)。

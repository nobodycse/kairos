#!/usr/bin/env bash
# KAIROS 服务器一键初始化（Ubuntu 22.04；4C8G 设计，2C4G 可降级——只跑平台层）
# 用法：sudo bash scripts/setup_server.sh
# 前置：本机已 git clone 本仓库（任意目录均可），网络可达外网
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KAIROS_HOME=/opt/kairos
COMPOSE_DIR=$KAIROS_HOME/repo/deploy/compose

log() { echo "[setup] $*"; }

[[ $EUID -eq 0 ]] || { echo "请用 root 运行：sudo bash scripts/setup_server.sh"; exit 1; }

# ---------- 1. Docker（官方 apt 仓库，GPG 签名验证；国内可自行换镜像源）----------
if ! command -v docker >/dev/null; then
  log "安装 Docker（官方 apt 仓库）"
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log "Docker 已安装：$(docker --version)"
fi
systemctl enable --now docker

# ---------- 2. k3s（禁用 traefik 释放 80 端口给 nginx；运行时用内置 containerd，
# 经 registries.yaml 配置国内镜像加速，见下方 registries 段）----------
if ! command -v k3s >/dev/null; then
  log "安装 k3s（--disable=traefik）"
  # k3s 官方只提供安装脚本通道：先下载到本地再执行，便于审计与重跑
  curl -sfL https://get.k3s.io -o /tmp/k3s-install.sh
  INSTALL_K3S_EXEC="--disable=traefik" sh /tmp/k3s-install.sh
else
  log "k3s 已安装：$(k3s --version | head -1)"
fi
systemctl enable --now k3s

# k3s 的镜像解析走内置 containerd（与 /etc/docker/daemon.json 的 mirror 无关），
# 国内环境为三大 registry 配置镜像端点，否则 Pod 永远卡在拉取 pause 镜像
if [[ ! -f /etc/rancher/k3s/registries.yaml ]]; then
  log "写入 containerd 镜像加速配置（国内环境）"
  cat > /etc/rancher/k3s/registries.yaml <<'EOF'
mirrors:
  docker.io:
    endpoint:
      - "https://docker.m.daocloud.io"
  registry.k8s.io:
    endpoint:
      - "https://k8s.m.daocloud.io"
  quay.io:
    endpoint:
      - "https://quay.m.daocloud.io"
EOF
  systemctl restart k3s
fi

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
log "等待 k3s 节点 Ready"
until kubectl get nodes 2>/dev/null | grep -qw Ready; do sleep 3; done
kubectl get nodes

# ---------- 3. 目录与代码 ----------
mkdir -p "$KAIROS_HOME"/{kubeconfig,data/{postgres,redis,prometheus,grafana,loki}}
# 监控容器内进程的 uid（root 属主的目录会导致 Grafana/Loki/Prometheus 重启循环）：
# grafana 472 / loki 10001 / prometheus 65534
chown -R 472:472 "$KAIROS_HOME/data/grafana"
chown -R 10001:10001 "$KAIROS_HOME/data/loki"
chown -R 65534:65534 "$KAIROS_HOME/data/prometheus"

if [[ "$REPO_DIR" != "$KAIROS_HOME/repo" ]]; then
  if [[ ! -d $KAIROS_HOME/repo/.git ]]; then
    REMOTE=$(git -C "$REPO_DIR" remote get-url gitee 2>/dev/null \
      || git -C "$REPO_DIR" remote get-url origin)
    log "克隆 $REMOTE -> $KAIROS_HOME/repo（CI/CD 在此 pull + build）"
    git clone "$REMOTE" "$KAIROS_HOME/repo"
  fi
else
  log "当前仓库已是 $KAIROS_HOME/repo，跳过克隆"
fi

# ---------- 4. .env ----------
if [[ ! -f $COMPOSE_DIR/.env ]]; then
  log "生成 $COMPOSE_DIR/.env（请随后修改其中所有 CHANGE_ME 项）"
  cp "$COMPOSE_DIR/.env.example" "$COMPOSE_DIR/.env"
fi
# K3S_SERVER_IP 未填写（保持模板值）时自动探测并回填
if grep -q '^K3S_SERVER_IP=10\.0\.0\.10$' "$COMPOSE_DIR/.env"; then
  DETECTED=$(ip route get 1.1.1.1 2>/dev/null \
    | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')
  [[ -n "$DETECTED" ]] && sed -i "s|^K3S_SERVER_IP=.*|K3S_SERVER_IP=$DETECTED|" "$COMPOSE_DIR/.env"
fi
# shellcheck disable=SC1091
source "$COMPOSE_DIR/.env"
# .env 里的 KUBECONFIG 是 backend 容器内路径（/kubeconfig/k3s.yaml），
# source 会覆盖步骤 5 前面导出的本机路径，导致 kubectl 裸连 localhost:8080
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
: "${K3S_SERVER_IP:?请在 deploy/compose/.env 中填写 K3S_SERVER_IP}"
if grep -q CHANGE_ME "$COMPOSE_DIR/.env"; then
  log "警告：.env 仍含 CHANGE_ME 占位值，部署前请务必修改"
fi

# ---------- 5. kubeconfig（改写 server 地址，architecture.md §3）----------
log "改写 k3s.yaml server 地址 -> https://$K3S_SERVER_IP:6443"
sed "s|https://127.0.0.1:6443|https://$K3S_SERVER_IP:6443|" \
  /etc/rancher/k3s/k3s.yaml > "$KAIROS_HOME/kubeconfig/k3s.yaml"
chmod 600 "$KAIROS_HOME/kubeconfig/k3s.yaml"

# ---------- 6. k8s manifests（顺序见 deploy/kubernetes/README.md）----------
log "部署 demo-app 与监控组件"
kubectl apply -f "$KAIROS_HOME/repo/deploy/kubernetes/demo-app.yaml"
# promtail configmap 依赖 monitoring ns，但该 ns 在 node-exporter.yaml 里才定义，
# 所以先声明式预创建（幂等，重复执行是 No Change）
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring create configmap promtail-config \
  --from-file=promtail-config.yml="$KAIROS_HOME/repo/deploy/observability/loki/promtail-config.yml" \
  --dry-run=client -o yaml | kubectl apply -f -
for m in node-exporter kube-state-metrics prometheus-rbac promtail; do
  kubectl apply -f "$KAIROS_HOME/repo/deploy/kubernetes/$m.yaml"
done

# Prometheus 抓 kubelet 的 SA token（prometheus-rbac.yaml 里的 Secret 生成有延迟，重试提取）
log "提取 prometheus bearer token"
for i in $(seq 1 15); do
  if kubectl -n monitoring get secret prometheus-token >/dev/null 2>&1; then break; fi
  sleep 2
done
kubectl -n monitoring get secret prometheus-token \
  -o jsonpath='{.data.token}' | base64 -d > "$KAIROS_HOME/kubeconfig/prometheus-token"
chmod 600 "$KAIROS_HOME/kubeconfig/prometheus-token"
# Prometheus 容器以 uid 65534(nobody) 运行，读不了 root 属主的 600 文件
chown 65534:65534 "$KAIROS_HOME/kubeconfig/prometheus-token"

# ---------- 7. demo-app 镜像 ----------
log "构建 kairos/demo-app 镜像"
docker build -t kairos/demo-app:latest "$KAIROS_HOME/repo/demo-app"
# k3s 的镜像内容库独立于 docker daemon：宿主机 build 出的镜像必须导入，
# 否则 demo Pod 报 ErrImagePull（containerd 看不见 docker 的本地镜像）
docker save kairos/demo-app:latest | /usr/local/bin/k3s ctr -n k8s.io images import -

# ---------- 8. 双 compose ----------
docker network inspect kairos-net >/dev/null 2>&1 || docker network create kairos-net

log "启动平台层 compose"
cd "$COMPOSE_DIR"
PROFILE_ARGS=()
[[ -f $KAIROS_HOME/repo/frontend/package.json ]] && PROFILE_ARGS=(--profile frontend)
# compose v5 起 --profile 只作为全局旗标（子命令前），v2 两种位置均可
docker compose "${PROFILE_ARGS[@]}" -f docker-compose.yml up -d --build

log "启动监控栈 compose"
docker compose -f docker-compose.monitor.yml up -d

# ---------- 9. webhook systemd 服务 ----------
log "安装 kairos-webhook systemd 服务"
cp "$KAIROS_HOME/repo/scripts/kairos-webhook.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kairos-webhook

# ---------- 10. 防火墙（只放行；是否 enable 由云安全组/手动决定）----------
if command -v ufw >/dev/null; then
  ufw allow 22/tcp || true
  ufw allow 80/tcp || true
  ufw allow 9000/tcp || true   # 建议在云安全组进一步限制来源为 Gitee IP 段
fi

log "完成。后续推送 main 分支即自动部署（journalctl -u kairos-webhook -f 查看部署日志）"

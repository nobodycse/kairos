# k8s 安装清单：setup_server.sh 的 apply 顺序与端口约定

## 安装顺序（setup_server.sh 已内置，手动执行时参考）

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# 1. demo 业务应用（namespace: demo）
kubectl apply -f demo-app.yaml

# 2. promtail 配置（单一来源：observability/loki/promtail-config.yml）
kubectl -n monitoring create configmap promtail-config \
  --from-file=promtail-config.yml=../observability/loki/promtail-config.yml \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. 监控组件
kubectl apply -f node-exporter.yaml
kubectl apply -f kube-state-metrics.yaml
kubectl apply -f prometheus-rbac.yaml
kubectl apply -f promtail.yaml
```

## 端口约定（与 observability 配置一一对应，改动需同步）

| 组件 | 暴露方式 | 地址 | 消费方 |
|---|---|---|---|
| demo-app | NodePort 30780 | host.docker.internal:30780 | Prometheus scrape |
| kube-state-metrics | NodePort 30760 | host.docker.internal:30760 | Prometheus scrape |
| node-exporter | hostNetwork 9100 | host.docker.internal:9100 | Prometheus scrape |
| kubelet/cAdvisor | 10250 (https) | host.docker.internal:10250 | Prometheus scrape（SA token） |
| Loki | 宿主机回环 3100 | 127.0.0.1:3100 | promtail（hostNetwork 直推）/ Grafana |

## 备注

- `prometheus-rbac.yaml` 里的 `prometheus-token` Secret 生成后，
  setup_server.sh 提取 token 到 `/opt/kairos/kubeconfig/prometheus-token`，
  由 `docker-compose.monitor.yml` 挂载（`bearer_token_file: /etc/prometheus/token`）。
- `registry.k8s.io` 镜像在国内拉取受阻时，kube-state-metrics 可换
  `bitnami/kube-state-metrics` 同版本；k3s 自带 containerd 之外的镜像加速不适用于
  本项目（运行时是 docker，见 setup_server.sh）。

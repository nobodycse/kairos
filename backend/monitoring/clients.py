"""monitoring 客户端单例：app lifespan 里 init/close（见 api/main.py）。"""
from core.config import settings
from monitoring.k8s import K8sClient
from monitoring.loki import LokiClient
from monitoring.prometheus import PrometheusClient

prometheus = PrometheusClient(settings.prometheus_url)
loki = LokiClient(settings.loki_url)
k8s = K8sClient(settings.kubeconfig)


async def init_clients() -> None:
    await k8s.connect()


async def close_clients() -> None:
    await k8s.close()
    await prometheus.close()
    await loki.close()

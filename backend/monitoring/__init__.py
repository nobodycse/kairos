"""monitoring 三客户端（architecture.md §6.2）：Prometheus / Kubernetes / Loki。

K8sClient 的实例在 app lifespan 里 connect/close（core/db.py 的 engine 同生命周期）。
"""

# KAIROS 接口与数据设计（design.md）

> 开发用契约文档。与 [architecture.md](architecture.md) 的分工：architecture 定**怎么分层**，本文定**接口长什么样、表怎么建、机制怎么落**。
> 前端同学对着第 3、4 章开发；后端同学照第 2、3、5、6 章实现。
> **协作规则：接口有任何改动，先改本文档和 stub 路由，再改实现。** FastAPI 的 `/docs` 是自动生成的在线契约，以部署后的 stub 为准。

---

## 目录

1. [总则](#1-总则)
2. [数据库 DDL](#2-数据库-ddl)
3. [REST API 契约](#3-rest-api-契约)
4. [SSE 契约](#4-sse-契约)
5. [关键机制实现约定](#5-关键机制实现约定)
6. [demo-app 契约](#6-demo-app-契约)
7. [.env 变量清单](#7-env-变量清单)
8. [附录：两人开发顺序](#8-附录两人开发顺序)

---

## 1. 总则

### 1.1 基础约定

| 项 | 约定 |
|---|---|
| Base URL | `/api/v1`（前端开发期可本地 proxy 到服务器） |
| 鉴权 | `Authorization: Bearer <JWT>`；豁免：`/auth/login`、`/health`、`/webhooks/*` |
| 成功响应 | 2xx，直接返回资源 JSON，**不用信封** |
| 错误响应 | 4xx/5xx，`{"detail": "人类可读的错误信息"}`（FastAPI 默认格式，零额外代码） |
| 分页请求 | `?page=1&page_size=20`（page 从 1 起；page_size 默认 20，最大 100） |
| 分页响应 | `{"items": [...], "total": 123, "page": 1, "page_size": 20}` |
| 时间格式 | ISO 8601 UTC，如 `2026-09-04T11:52:10Z`（带 `+00:00` 后缀同样接受） |
| 字段命名 | snake_case（pydantic 默认序列化） |
| 空值 | 可空字段一律显式返回 `null`，不省略键 |

### 1.2 枚举值汇总

所有枚举在 API/JSON 层用**小写字符串**（Python 代码内部可用大写枚举名，序列化时转小写）。

**fault_event.status（9 态，与 architecture.md §5 状态机一致）**

| 值 | 含义 |
|---|---|
| detected | 告警已接收，未开始诊断 |
| diagnosing | Agent 诊断中 |
| awaiting_approval | 等待人工确认 |
| remediating | 修复执行中 |
| verifying | 修复验证观察期 |
| rolling_back | 回滚中 |
| resolved | 已恢复 |
| failed | 失败，转人工 |
| closed | 人工拒绝修复后关闭 |

列表接口 `GET /faults?status=active` 中 `active` 为特殊值 = `detected / diagnosing / awaiting_approval / remediating / verifying / rolling_back` 六态。

| 枚举 | 取值 |
|---|---|
| severity | `warning` `critical` |
| risk_level | `low` `medium` `high` `critical` |
| remediation action | `update_resource_limit` `scale_deployment` `restart_deployment` `rollback_deployment` `delete_pod` |
| policy | `auto` `require_approval` `forbidden` |
| remediation status | `pending` `approved` `rejected` `executing` `succeeded` `failed` `rolled_back` |
| evidence.source | `k8s_api` `prometheus` `loki` `events` |
| experiment.fault_type | `oom` `cpu_overload` `pod_crash` `image_pull_backoff` `replica_anomaly` `network_latency` `node_not_ready` |
| experiment.status | `created` `injecting` `injected` `finished` `cancelled` |

---

## 2. 数据库 DDL

PostgreSQL，Alembic 管理迁移；本节是目标 schema，评审以本节为准。表按外键依赖排序。

```sql
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE experiments (
    id               BIGSERIAL PRIMARY KEY,
    fault_type       TEXT NOT NULL CHECK (fault_type IN
                       ('oom','cpu_overload','pod_crash','image_pull_backoff',
                        'replica_anomaly','network_latency','node_not_ready')),
    target_ns        TEXT NOT NULL DEFAULT 'demo',
    target_workload  TEXT NOT NULL,
    params           JSONB NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'created' CHECK (status IN
                       ('created','injecting','injected','finished','cancelled')),
    injected_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_experiments_status ON experiments (status);

CREATE TABLE fault_events (
    id                BIGSERIAL PRIMARY KEY,
    fingerprint       TEXT NOT NULL,              -- 主告警的 Alertmanager fingerprint
    alert_name        TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('warning','critical')),
    namespace         TEXT NOT NULL,
    workload          TEXT,                       -- 归并出的 Deployment 名, 可能未知
    labels            JSONB NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL CHECK (status IN
                        ('detected','diagnosing','awaiting_approval','remediating',
                         'verifying','rolling_back','resolved','failed','closed')),
    experiment_id     BIGINT REFERENCES experiments(id),
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    mttr_seconds      INT,
    rediagnose_count  INT NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 列表页按状态过滤
CREATE INDEX idx_fault_events_status_time ON fault_events (status, detected_at DESC);
-- 告警归并：查同 namespace+workload 的活跃事件（部分索引）
CREATE INDEX idx_fault_events_active_ns_wl ON fault_events (namespace, workload)
    WHERE status IN ('detected','diagnosing','awaiting_approval',
                     'remediating','verifying','rolling_back');

CREATE TABLE diagnoses (
    id              BIGSERIAL PRIMARY KEY,
    fault_event_id  BIGINT NOT NULL REFERENCES fault_events(id) ON DELETE CASCADE,
    fault_type      TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    evidence        JSONB NOT NULL DEFAULT '[]',  -- Evidence[] 见 architecture.md §6.4
    confidence      REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    blast_radius    TEXT NOT NULL,
    suggestion      TEXT NOT NULL,
    llm_model       TEXT NOT NULL,
    iterations      INT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_diagnoses_event ON diagnoses (fault_event_id);

CREATE TABLE remediation_actions (
    id              BIGSERIAL PRIMARY KEY,
    fault_event_id  BIGINT NOT NULL REFERENCES fault_events(id) ON DELETE CASCADE,
    action          TEXT NOT NULL CHECK (action IN
                      ('update_resource_limit','scale_deployment',
                       'restart_deployment','rollback_deployment','delete_pod')),
    namespace       TEXT NOT NULL,
    target          TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}',
    risk_level      TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    policy          TEXT NOT NULL CHECK (policy IN ('auto','require_approval','forbidden')),
    approved_by     BIGINT REFERENCES users(id),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                      ('pending','approved','rejected','executing',
                       'succeeded','failed','rolled_back')),
    snapshot        JSONB,                        -- 执行前 Deployment 资源快照, 回滚依据
    executed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_remediations_event ON remediation_actions (fault_event_id);

CREATE TABLE audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    actor       TEXT NOT NULL,                    -- 'agent' | 'system' | 'user:admin'
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,                    -- 如 'deployment/demo/payment-service'
    params      JSONB NOT NULL DEFAULT '{}',
    result      TEXT NOT NULL CHECK (result IN ('allowed','denied','success','failure')),
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_logs_time ON audit_logs (created_at DESC);

CREATE TABLE experiment_results (
    id                   BIGSERIAL PRIMARY KEY,
    experiment_id        BIGINT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    fault_event_id       BIGINT REFERENCES fault_events(id),
    detected             BOOLEAN NOT NULL,
    detection_latency_s  INT,
    diagnosed_correctly  BOOLEAN,
    auto_recovered       BOOLEAN,
    mttr_s               INT,
    false_action         BOOLEAN NOT NULL DEFAULT FALSE,
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 3. REST API 契约

以下示例统一使用同一个故事：`demo` namespace 的 `payment-service` 发生 OOM（与 README 的例子一致）。所有端点除注明外均需 Bearer JWT。

### 3.1 POST /api/v1/auth/login

请求：

```json
{"username": "admin", "password": "admin123"}
```

200 响应：

```json
{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer", "expires_in": 86400}
```

401：`{"detail": "用户名或密码错误"}`

### 3.2 GET /api/v1/cluster/overview

200 响应（Dashboard 顶部卡片快照；趋势图数据源见 §3.2.1）：

```json
{
  "nodes": {"total": 1, "ready": 1},
  "pods": {"total": 18, "running": 16, "pending": 1, "failed": 1},
  "deployments": {"total": 5, "available": 4},
  "active_faults": 1,
  "resources": {"cpu_usage_ratio": 0.31, "memory_usage_ratio": 0.52},
  "qps": 42.5,
  "error_rate": 0.012,
  "p95_latency": 0.24
}
```

`qps` / `error_rate` / `p95_latency` 来自 Prometheus（demo_http_requests_total / demo_http_request_duration_seconds，见 §6.1）；`resources` 两个比值来自 cAdvisor/kube-state-metrics；`active_faults` 来自 DB（六态 active，§1.2）。**降级语义**：Prometheus 不可达时上述四组 Prometheus 相关字段为显式 `null`（K8s 结构数据不受影响）；K8s API 不可达则整个接口 502。

### 3.2.1 GET /api/v1/cluster/trends?minutes=30

趋势图数据源（Dashboard 折线图，前端 30s 轮询）。`minutes` 取值 5–1440（默认 30）；`step` 服务端自动取 `max(60, minutes*60/240)` 秒，控制点数不超过 240。200 响应：

```json
{
  "interval_seconds": 60,
  "points": [
    {"ts": "2026-09-25T12:00:00Z", "qps": 7.2, "error_rate": 0.0, "p95_latency": 0.24},
    {"ts": "2026-09-25T12:01:00Z", "qps": 7.5, "error_rate": 0.013, "p95_latency": 0.25}
  ]
}
```

三条曲线的 PromQL 与 §3.2 快照值同源（`demo_http_requests_total` / `demo_http_request_duration_seconds_bucket`）。Prometheus 不可达时返回 `{"interval_seconds": ..., "points": []}`（前端显示空图，不报错）。

### 3.3 GET /api/v1/cluster/pods?namespace=demo&page=1&page_size=20

支持 `namespace` 过滤，默认全部。200 响应：

```json
{
  "items": [
    {
      "namespace": "demo",
      "name": "payment-service-7d9f6b8c5-x2k4l",
      "workload": "payment-service",
      "node": "kairos-node1",
      "status": "CrashLoopBackOff",
      "ready": false,
      "restarts": 12,
      "age_seconds": 3600,
      "cpu_usage_cores": 0.05,
      "memory_usage_bytes": 52428800,
      "memory_limit_bytes": 536870912
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

字段说明：`workload` 由 pod ownerReferences 推导；`status` 取 pod phase 或 container 状态（CrashLoopBackOff / ImagePullBackOff 等，比 phase 更接近运维视角）。

### 3.4 GET /api/v1/cluster/nodes / deployments / events

nodes 条目：

```json
{
  "name": "kairos-node1",
  "status": "Ready",
  "roles": "control-plane,etcd",
  "version": "v1.30.4+k3s1",
  "cpu_alloc_cores": 4,
  "memory_alloc_bytes": 8035227648,
  "cpu_usage_ratio": 0.31,
  "memory_usage_ratio": 0.52,
  "pods_count": 18
}
```

deployments 条目：

```json
{
  "namespace": "demo",
  "name": "payment-service",
  "replicas": 2,
  "ready_replicas": 1,
  "image": "kairos/demo-app:latest",
  "cpu_limit": "500m",
  "memory_limit": "512Mi"
}
```

events 条目（支持 `?type=warning`，默认只返回 Warning）：

```json
{
  "namespace": "demo",
  "type": "Warning",
  "reason": "BackOff",
  "object": "demo/payment-service-7d9f6b8c5-x2k4l",
  "message": "Back-off restarting failed container",
  "count": 9,
  "last_seen": "2026-09-04T11:58:40Z"
}
```

三者响应外层结构同 3.3（items/total/page/page_size）。

### 3.5 GET /api/v1/faults?status=active

支持 `status`（枚举值或 `active`）与 `namespace` 过滤，默认按 `detected_at` 倒序。条目：

```json
{
  "id": 42,
  "alert_name": "PodOOMKilled",
  "severity": "critical",
  "namespace": "demo",
  "workload": "payment-service",
  "status": "awaiting_approval",
  "detected_at": "2026-09-04T11:52:10Z",
  "resolved_at": null,
  "mttr_seconds": null
}
```

### 3.6 GET /api/v1/faults/{id}

详情页数据源，一次取全（诊断中时 `diagnosis` 为 `null`）：

```json
{
  "id": 42,
  "fingerprint": "e5f0a1b2c3d4e5f6",
  "alert_name": "PodOOMKilled",
  "severity": "critical",
  "namespace": "demo",
  "workload": "payment-service",
  "labels": {"alertname": "PodOOMKilled", "namespace": "demo", "pod": "payment-service-7d9f6b8c5-x2k4l"},
  "status": "awaiting_approval",
  "detected_at": "2026-09-04T11:52:10Z",
  "resolved_at": null,
  "mttr_seconds": null,
  "rediagnose_count": 0,
  "experiment_id": null,
  "diagnosis": {
    "id": 57,
    "fault_type": "OOM",
    "root_cause": "容器内存不足：memory limit 为 512Mi，工作集持续超过 95%",
    "evidence": [
      {"source": "k8s_api", "tool": "get_pod_status", "summary": "最近 10 分钟重启 12 次", "data": {"restarts": 12, "window": "10m"}},
      {"source": "events", "tool": "get_k8s_events", "summary": "出现 OOMKilled 事件 x2", "data": {"reasons": ["OOMKilled"], "count": 2}},
      {"source": "prometheus", "tool": "get_metrics", "summary": "内存使用率持续 >95%", "data": {"metric": "memory_usage_ratio", "max": 0.97}},
      {"source": "loki", "tool": "get_pod_logs", "summary": "日志出现 out of memory", "data": {"lines_matched": 8}}
    ],
    "confidence": 0.94,
    "blast_radius": "demo/payment-service 全部 2 副本，支付接口受影响",
    "suggestion": "将 memory limit 从 512Mi 调整至 1Gi",
    "llm_model": "deepseek-chat",
    "iterations": 5,
    "created_at": "2026-09-04T11:55:02Z"
  },
  "remediations": [
    {
      "id": 31,
      "action": "update_resource_limit",
      "namespace": "demo",
      "target": "payment-service",
      "params": {"container": "app", "memory_limit": "1Gi"},
      "risk_level": "medium",
      "policy": "require_approval",
      "status": "pending",
      "approved_by": null,
      "snapshot": {"memory_limit": "512Mi"},
      "executed_at": null
    }
  ]
}
```

说明：`diagnosis` 取最新一条；`remediations` 按创建正序。历史事件的 agent 步骤时间线不持久化，详情页历史视图展示 `evidence` 链即可；**实时时间线只走 SSE（§4）**。

### 3.7 POST /api/v1/faults/{id}/diagnose

手动（重新）触发诊断。202：

```json
{"fault_event_id": 42, "status": "diagnosing"}
```

409（正在诊断中）：`{"detail": "该事件正在诊断中"}`；409（已终态）：`{"detail": "该事件已结束"}`

### 3.8 POST /api/v1/remediations/{id}/approve 与 /reject

请求体可选 `{"comment": "同意扩容"}`（仅记录进审计）。approve 200：

```json
{"id": 31, "status": "approved", "fault_event_status": "remediating"}
```

reject 200：

```json
{"id": 31, "status": "rejected", "fault_event_status": "closed"}
```

409：`{"detail": "该动作当前状态不允许此操作"}`

### 3.9 POST /api/v1/experiments

请求：

```json
{"fault_type": "oom", "target_workload": "payment-service", "params": {"memory_limit": "128Mi"}}
```

`params` 按 fault_type 不同（见 §6.2 注入端点表；oom 走调低 limit 方式时传 `memory_limit`）。201：

```json
{
  "id": 8,
  "fault_type": "oom",
  "target_ns": "demo",
  "target_workload": "payment-service",
  "params": {"memory_limit": "128Mi"},
  "status": "created",
  "injected_at": null,
  "created_at": "2026-09-04T12:10:00Z"
}
```

### 3.10 POST /api/v1/experiments/{id}/inject

执行注入。202：`{"id": 8, "status": "injecting"}`

### 3.11 GET /api/v1/experiments/{id}/report

闭环结束后可查（未结束时 `fault_event_id` / `result` 为 `null`）：

```json
{
  "id": 8,
  "fault_type": "oom",
  "target_workload": "payment-service",
  "status": "finished",
  "injected_at": "2026-09-04T12:10:05Z",
  "fault_event_id": 45,
  "result": {
    "detected": true,
    "detection_latency_s": 95,
    "diagnosed_correctly": true,
    "auto_recovered": true,
    "mttr_s": 240,
    "false_action": false,
    "notes": "根因判定 OOM 正确，limit 调整后一次通过验证"
  }
}
```

`diagnosed_correctly` 的判定口径：diagnoses.fault_type 与实验注入的 fault_type 同类（OOM / CrashLoop 等映射表由后端维护）。

### 3.11.1 GET /api/v1/experiments（Phase 3 新增）

实验室页列表数据源。id 倒序最多 50 条，条目结构同 §3.11（含 `fault_event_id` / `result`，未闭环为 `null`）：

```json
{"items": [{"id": 8, "fault_type": "oom", "target_ns": "demo", "target_workload": "payment-service", "params": {"memory_limit": "128Mi"}, "status": "finished", "injected_at": "...", "created_at": "...", "fault_event_id": 45, "result": {…§3.11 result…}}], "total": 8}
```

`total` 为全部实验数（含进行中）；`params` 剥离下划线前缀的内部键（`_snapshot` 等不入契约）；`last_error` 为最近一次注入失败原因（成功注入后清除语义：仅 created 状态且有失败记录时非 null）；`fault_event_id` 在关联回填后即返回（不必等闭环，Phase 3 服务器实测注记）。

### 3.11.2 GET /api/v1/experiments/{id}/compare（Phase 3 新增）

修复前后指标对比（前端双窗口曲线）。要求实验已关联事件且事件 `resolved`，否则 409。故障窗 = detected_at→resolved_at，恢复窗 = resolved_at→+10m；采样步长自适应（下限 30s）；Prometheus 不可达时 `points` 为空数组（软依赖，§3.2.1 同语义）：

```json
{
  "experiment_id": 8,
  "fault_event_id": 45,
  "fault_window": {"start": "2026-09-04T11:52:10+00:00", "end": "2026-09-04T11:56:10+00:00"},
  "recovery_window": {"start": "2026-09-04T11:56:10+00:00", "end": "2026-09-04T12:06:10+00:00"},
  "metrics": [
    {
      "key": "error_rate", "name": "5xx 错误率", "unit": "ratio",
      "fault": {"points": [{"ts": "2026-09-04T11:52:30+00:00", "value": 0.4}]},
      "recovery": {"points": [{"ts": "2026-09-04T11:56:30+00:00", "value": 0.0}]}
    }
  ]
}
```

`metrics` 固定四条：`error_rate`（5xx 率）、`p95_latency`（秒）、`cpu_usage`（核）、`memory_usage`（字节）；后两条按事件 `labels.pod`（缺省退回 target_workload）前缀匹配 namespace 内 Pod。前两条 PromQL 与告警规则同源。

### 3.12 GET /api/v1/reports/summary

```json
{
  "total_experiments": 10,
  "overall": {"diagnosis_accuracy": 0.9, "recovery_rate": 0.7, "avg_mttr_s": 285, "false_action_rate": 0.0},
  "by_fault_type": [
    {"fault_type": "oom", "runs": 4, "diagnosis_accuracy": 1.0, "recovery_rate": 0.75, "avg_mttr_s": 240, "false_action_rate": 0.0},
    {"fault_type": "cpu_overload", "runs": 3, "diagnosis_accuracy": 0.67, "recovery_rate": 0.67, "avg_mttr_s": 310, "false_action_rate": 0.0},
    {"fault_type": "pod_crash", "runs": 3, "diagnosis_accuracy": 1.0, "recovery_rate": 0.67, "avg_mttr_s": 305, "false_action_rate": 0.0}
  ]
}
```

### 3.13 POST /api/v1/webhooks/alerts（无鉴权，仅内网可达）

Alertmanager 标准 payload（摘录）：

```json
{
  "receiver": "kairos-backend",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {"alertname": "PodOOMKilled", "severity": "critical", "namespace": "demo", "pod": "payment-service-7d9f6b8c5-x2k4l"},
      "annotations": {"summary": "Pod 被 OOMKilled"},
      "startsAt": "2026-09-04T11:52:10.123Z",
      "fingerprint": "e5f0a1b2c3d4e5f6"
    }
  ]
}
```

200 响应（调试有用）：

```json
{"received": 1, "created": 1, "merged": 0, "ignored": 0}
```

`ignored` = fingerprint 去重命中（§5.3）。Resolved 告警（`status: "resolved"`）触发验证模块提前复查，不新建事件。

### 3.14 GET /health（无鉴权）

```json
{"status": "ok", "version": "0.1.0"}
```

CI/CD 健康检查用。

---

## 4. SSE 契约

**前端唯一没有自动契约的部分，以本章为准。**

### 4.1 连接

```text
GET /api/v1/faults/{id}/stream?token=<JWT>
Accept: text/event-stream
```

EventSource 不能设置请求头，**JWT 走 query 参数 `token`**。连接建立后服务端先发一个 `snapshot`（当前状态 + 最近步骤，断线重连同样适用），随后推送实时事件；每 15 秒发送注释心跳帧 `: ping` 防止代理断连。

帧格式：

```text
event: <事件类型>
data: <单行 JSON>


```

### 4.2 snapshot（连接/重连时首先收到）

```json
{
  "fault_event_id": 42,
  "status": "diagnosing",
  "alert_name": "PodOOMKilled",
  "detected_at": "2026-09-04T11:52:10Z",
  "recent_steps": []
}
```

`recent_steps`：最多 20 条 `agent_step`（同 4.4 格式），来源 Redis `event:{id}:steps`，仅活跃事件有内容；历史事件为空数组（历史走 §3.6 详情接口）。

### 4.3 status_changed

```json
{
  "fault_event_id": 42,
  "from": "diagnosing",
  "to": "awaiting_approval",
  "reason": "方案生成，风险等级 medium，需人工确认",
  "at": "2026-09-04T11:55:05Z"
}
```

前端收到 `to == "awaiting_approval"` 时弹出确认框（数据从 §3.6 详情接口刷新）。

### 4.4 agent_step

工具开始：

```json
{"fault_event_id": 42, "iteration": 3, "phase": "collect_evidence", "step": "tool_start", "tool": "get_k8s_events", "args": {"namespace": "demo", "pod": "payment-service-7d9f6b8c5-x2k4l"}, "at": "2026-09-04T11:53:40Z"}
```

工具结束：

```json
{"fault_event_id": 42, "iteration": 3, "phase": "collect_evidence", "step": "tool_end", "tool": "get_k8s_events", "duration_ms": 350, "evidence_summary": "发现 OOMKilled 事件 x2", "at": "2026-09-04T11:53:41Z"}
```

`phase` ∈ `collect_evidence / analyze / propose_fix / execute_fix / verify`，对应 architecture.md §7.1 的节点。时间线组件按 `iteration` 分组、`step` 成对渲染。

### 4.5 verification_progress

观察窗口内每 30 秒一个采样点（architecture.md §6.7 的五项检查）：

```json
{
  "fault_event_id": 42,
  "sample": 4,
  "of": 6,
  "checks": {
    "pod_ready": true,
    "no_restarts": true,
    "error_rate": {"value": 0.03, "ok": true},
    "p95_latency": {"value": 1.2, "ok": false},
    "logs_clean": true
  },
  "all_passed": false,
  "at": "2026-09-04T12:01:00Z"
}
```

布尔项直接 `true/false`；数值项带 `value` 与 `ok`。`all_passed == true` 后紧随 `status_changed → resolved`。

---

## 5. 关键机制实现约定

把 architecture.md 中一句话带过的机制落成可执行决策。MVP 原则：**进程内 asyncio，不加独立 worker**。

### 5.1 告警 → 诊断触发

webhook handler 完成去重/归并/落库后，`asyncio.create_task(agent_runner.run(fault_event_id))` 在 backend 进程内启动 LangGraph。graph 未捕获异常 → fault_event 置 `failed` + 写 audit_logs(`actor: "agent", result: "failure"`)。**后端重启时的孤儿事件**（status 停留在 diagnosing/remediating/verifying）：启动扫描后统一置 `failed`，不自动续跑（MVP 简化，人工可 §3.7 重新触发）。

### 5.2 interrupt 挂起与恢复

graph 运行至 `risk_gate → REQUIRE_APPROVAL` 时调用 `interrupt()`；后端捕获后将 graph 状态序列化存 Redis `event:{id}:graph_state`（TTL 24h），fault_event 置 `awaiting_approval`。approve 接口读回状态，以 `Command(resume={"approved": true})` 恢复执行；reject 以 `Command(resume={"approved": false})` 恢复后走 closed 分支。TTL 过期视为放弃：事件置 `failed`。

### 5.3 告警去重与归并

1. 逐条 alert：`SET alerts:{fingerprint} NX EX 3600`，已存在 → 计入 `ignored`。
2. 归并：labels 里取 `namespace` + `pod` → 查 K8s API pod 的 ownerReferences 得 Deployment 名（结果缓存 5 分钟）→ 查 §2 的活跃部分索引：同 `namespace + workload` 存在活跃 fault_event → 合并（labels 并集、severity 取高、保留最早 detected_at），计入 `merged`；否则新建，计入 `created`。
3. 新建/合并后若该 workload 无运行中的 graph（Redis `SET event:{id}:running NX EX 1800` 抢锁），触发 5.1。

### 5.4 LLM 调用

单次请求 30s 超时，失败重试 1 次；`structured()` 输出校验失败带错误信息重试 1 次；再失败 → 当前节点上抛（graph 捕获 → `failed`）。连续 5 个事件失败 → 告警日志（不阻塞后续事件）。

### 5.5 同一事件并发控制

同一 fault_event 同时只允许一个 graph 运行（5.3.3 的 Redis 锁）；`diagnose` 接口对已运行事件返回 409。

---

## 6. demo-app 契约

被注入故障的示例微服务。单文件 FastAPI + prometheus_client（约 100 行），镜像名 `kairos/demo-app`，部署在 `demo` namespace，默认 `requests: 128Mi / limits: 512Mi`（与 README 的 512Mi→1Gi 修复故事一致）。

### 6.1 业务指标（与告警规则 PromQL 严格一致）

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `demo_http_requests_total` | Counter | `status`（"200"/"500"）, `route` | QPS / 5xx 错误率 |
| `demo_http_request_duration_seconds` | Histogram | `le` + `status` + `route`；buckets: .01 .025 .05 .1 .25 .5 1 2.5 5 10 | P95 延迟 |
| `demo_http_requests_in_flight` | Gauge | — | 并发数 |

主路由 `GET /pay`：正常时随机 sleep 10–50ms 返回 200，指标自暴露 `GET /metrics`。

### 6.2 故障注入端点（无需 K8s 权限的注入方式）

| 端点 | 作用 | 对应 fault_type |
|---|---|---|
| `POST /internal/crash` | 立即抛异常使进程退出（配合重启策略 → CrashLoopBackOff） | pod_crash |
| `POST /internal/consume-memory?bytes=536870912` | 分配并持有 bytes 内存（触发 OOMKilled） | oom |
| `POST /internal/slow?ms=3000` | 令 /pay 延迟 ms 毫秒 | —（配合延迟告警） |
| `POST /internal/errors?rate=0.5` | 令 /pay 以 rate 概率返回 500 | —（配合错误率告警） |
| `POST /internal/reset` | 清除所有注入状态 | 实验收尾 |
| `GET /healthz` | 存活探针 | — |

注入状态存进程内存——**Pod 重启即自动清零**，这正是 OOM 修复后行为恢复正常的机制。fault-lab 的 oom 注入默认走"调低 Deployment memory_limit"（§3.9 的 `params.memory_limit`），`consume-memory` 作为不依赖 K8s 写权限的备选。

---

## 7. .env 变量清单

后端（backend 容器）：

| 变量 | 示例 | 说明 |
|---|---|---|
| LLM_BASE_URL | `https://api.deepseek.com/v1` | OpenAI 兼容端点；**Phase 2 增补：网页「系统设置」（DB）优先，.env 仅作未配置兜底** |
| LLM_API_KEY | `sk-xxx` | 同上——推荐在网页「系统设置」配置（存 DB，API 只回掩码、修改入审计），避免 key 进 .env |
| LLM_MODEL | `deepseek-chat` | 同上 |
| LLM_TIMEOUT_SECONDS | `30` | §5.4 |
| DATABASE_URL | `postgresql+asyncpg://kairos:xxx@postgres:5432/kairos` | |
| REDIS_URL | `redis://redis:6379/0` | |
| JWT_SECRET | 随机 32+ 字符 | |
| JWT_EXPIRE_HOURS | `24` | |
| KUBECONFIG | `/kubeconfig/k3s.yaml` | 容器内路径，见 architecture.md §3 |
| PROMETHEUS_URL | `http://prometheus:9090` | monitoring 客户端用；本地 dev 为 `http://127.0.0.1:9090` |
| LOKI_URL | `http://loki:3100` | monitoring 客户端用；本地 dev 为 `http://127.0.0.1:3100` |
| DEMO_NAMESPACE | `demo` | 参数白名单的目标 namespace |

部署（compose / CI/CD）：

| 变量 | 说明 |
|---|---|
| POSTGRES_PASSWORD | 数据库密码（与 DATABASE_URL 保持一致） |
| GRAFANA_ADMIN_PASSWORD | Grafana 管理员密码 |
| WEBHOOK_SECRET | Gitee webhook HMAC 校验密钥 |
| K3S_SERVER_IP | 宿主机内网 IP，用于改写 k3s.yaml 的 server 地址 |
| ADMIN_INITIAL_PASSWORD | 初始 admin 密码（Alembic 种子迁移用，必须覆盖） |

---

## 8. 附录：开发顺序

> **单人开发调整（2026-09）**：项目现为一人负责。原 §8.1 的后端/前端两列不再表示两人分工，
> 改为**纵向切片**执行——每个阶段把对应功能从前端到后端做穿（如 Phase 1 的
> "cluster 接口真实化 + Dashboard 接真数据"在一个阶段内交付），§8.2 的"并行先行任务"
> 与 §8.3 的"协作约定"随之失效；接口契约（§1–§7）全部继续有效，改动仍以本文档为准。

团队配置（原始设计，供追溯）：后端 1 人（FastAPI + Agent + 部署 + CI/CD + 故障实验室），前端 1 人。全职假设约 8–10 周。

### 8.1 阶段计划

| 阶段 | 后端 | 前端 | 里程碑 |
|---|---|---|---|
| Phase 0（第 1 周） | 服务器初始化（k3s/Docker/双 compose/demo-app/监控栈）；FastAPI 骨架——全部 router 建 stub 返回假数据；Gitee webhook + deploy.sh | Vue3+TS+Vite+EP 脚手架、布局路由、登录页、API service 层、SSE 客户端封装 | push 到 main → 自动部署 → 打开服务器 IP 见登录页 |
| Phase 1（第 2–3 周）✅ 已完成（2026-09） | monitoring/ 三客户端；cluster 路由接真实数据；webhook 接入 + 去重归并 + fault_events 落库（状态机先只做 detected） | Dashboard 总览页、资源列表页、故障事件列表页 | 手动制造 OOM，前端看到故障事件出现 |
| Phase 2（第 4–6 周）✅ 已完成（2026-09） | LLM 适配层 → 8 查询工具 → LangGraph（collect→analyze→propose）→ RCA + SSE agent_step → risk_control + executor + interrupt → verification + 回滚 | **诊断详情页**（最大工作量，拆两轮：先只读时间线，再加确认交互） | 走通 README 16 步 Demo |
| Phase 3（第 7–8 周）✅ 已完成（2026-10，服务器实测） | fault-lab 注入、experiments API、评估汇总（砍单：oom / pod_crash / cpu_overload 三类，见 phase3-plan §2.8） | 实验室页、历史与报告页、修复前后指标对比 | 跑 3 类故障实验出评估表（docs/experiments.md） |

### 8.2 前端无后端依赖的先行任务

等待契约/联调的空档做：Grafana 两组看板 JSON（被监控集群 / KAIROS 自身）、demo-app 展示页（如需要）、最终 Demo 演示脚本与验收清单。

### 8.3 协作约定

1. **SSE 以本文档 §4 为准**（FastAPI /docs 覆盖不了 SSE）。
2. **接口改动先改 stub 再改实现**：后端改接口先把 stub 路由部署上去，前端永远对着线上 `/docs` 开发。

### 8.4 风险与降级

- 后端是单点，Phase 2 的 Agent 闭环是最大不确定性（LangGraph interrupt/checkpointer、LLM 输出不稳定的坑，单坑可能耗半天以上）。
- 排不下时的砍单顺序：故障实验室先做 3 类（oom / pod_crash / cpu_overload）；`network_latency` 与 `node_not_ready` 留到有余力再做。3 类足够支撑 README 的评估表。

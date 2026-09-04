# KAIROS — Kubernetes AI Reasoning & Operations System 智能运维与故障自愈系统

> 基于 Kubernetes、Prometheus、Loki 与大语言模型构建的云原生 AIOps 系统，实现 **故障发现 → 多源信息采集 → AI 根因分析 → 风险评估 → 自动修复 → 修复验证 → 回滚** 的智能运维闭环。

---

## 📖 项目简介

在传统 Kubernetes 运维过程中，当服务出现异常时，运维人员通常需要手动查看监控指标、Pod 状态、日志、Events、Deployment 配置等大量信息，再根据经验判断故障原因并执行修复。

本项目尝试利用 **大语言模型（LLM）+ Agent + Kubernetes + 可观测性技术**，构建一个能够自主完成故障诊断和部分故障修复的智能运维系统。

系统不会简单地将监控数据交给大模型进行分析，而是通过 Kubernetes API、Prometheus、Loki 等组件获取多源运行数据，由 Agent 根据当前故障动态调用工具获取证据，再进行根因分析。

在执行修复前，系统会进行风险评估，根据风险等级决定：

- 自动执行
- 人工确认后执行
- 禁止自动执行

修复完成后，系统会继续观察 Kubernetes 集群状态和业务指标，判断故障是否真正恢复；如果修复失败，则根据策略执行回滚或重新诊断。

最终形成：

```text
故障发现
   ↓
信息采集
   ↓
异常分析
   ↓
根因定位
   ↓
修复方案生成
   ↓
风险评估
   ↓
自动修复 / 人工确认
   ↓
修复验证
   ↓
成功 / 回滚
```

---

## 🎯 项目目标

本项目主要解决三个问题：

### 1. 降低 Kubernetes 故障排查成本

自动关联：

- Metrics
- Logs
- Kubernetes Events
- Pod 状态
- Deployment 配置
- Node 状态

减少人工在多个系统之间切换查询。

### 2. 提高故障诊断效率

通过 AI Agent 自动：

```text
发现异常
→ 获取相关信息
→ 分析证据
→ 提出故障假设
→ 进一步查询验证
→ 确定根因
```

而不是简单根据单个指标进行判断。

### 3. 实现可控的自动化运维

系统不会让大模型直接拥有无限制的 Kubernetes 操作权限，而是增加：

```text
风险评估
+
权限控制
+
人工确认
+
操作审计
+
修复验证
+
失败回滚
```

确保自动化运维具有可控性。

---

# 🏗️ 系统架构

```text
                        ┌──────────────────┐
                        │     Web 前端      │
                        │    Vue / React    │
                        └────────┬─────────┘
                                 │
                                 ↓
                        ┌──────────────────┐
                        │    FastAPI       │
                        │    API 服务       │
                        └────────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ↓                  ↓                  ↓
      ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
      │ Monitoring   │   │   AI Agent   │   │  Fault Lab   │
      │    Service   │   │    Service   │   │ 故障实验室    │
      └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
             │                  │                  │
             ↓                  ↓                  ↓
       Prometheus          LLM / Agent          Chaos
             │                  │
             ↓                  ↓
           Metrics       ┌───────────────┐
                         │ Agent Tools   │
                         ├───────────────┤
                         │ Pod Tool      │
                         │ Log Tool      │
                         │ Metric Tool   │
                         │ Event Tool    │
                         │ Deploy Tool   │
                         │ Repair Tool   │
                         └───────┬───────┘
                                 │
                                 ↓
                        ┌──────────────────┐
                        │ Kubernetes API   │
                        └────────┬─────────┘
                                 │
                                 ↓
                        ┌──────────────────┐
                        │ Kubernetes 集群   │
                        └────────┬─────────┘
                                 │
                       ┌─────────┴─────────┐
                       ↓                   ↓
                    Grafana              Loki
```

---

# 🔑 核心功能

## 1. Kubernetes 集群监控

实时获取：

- Pod 状态
- Deployment 状态
- Node 状态
- CPU 使用率
- Memory 使用率
- Network 流量
- Pod Restart Count
- HTTP 请求量
- HTTP 错误率
- 请求延迟

---

## 2. 多源故障信息采集

系统从多个数据源获取故障上下文：

### Prometheus

获取：

```text
CPU
Memory
Network
QPS
Latency
Error Rate
Pod Restart
```

### Loki

获取：

```text
应用日志
错误日志
异常堆栈
```

### Kubernetes API

获取：

```text
Pod
Deployment
Service
ConfigMap
Node
```

### Kubernetes Events

获取：

```text
OOMKilled
CrashLoopBackOff
ImagePullBackOff
FailedScheduling
FailedMount
NodeNotReady
```

---

# 🤖 AI 故障诊断 Agent

系统的核心不是简单调用大模型 API，而是构建一个能够主动获取信息和执行任务的 Kubernetes 运维 Agent。

Agent 可以根据当前问题自主调用工具。

例如：

```text
用户：
payment-service 一直重启，帮我分析原因
```

Agent：

```text
① 查询 Pod 状态
        ↓
② 发现 CrashLoopBackOff
        ↓
③ 查询 Kubernetes Events
        ↓
④ 发现 OOMKilled
        ↓
⑤ 查询 Prometheus 内存指标
        ↓
⑥ 查询 Pod resource limit
        ↓
⑦ 查询应用日志
        ↓
⑧ 综合证据
        ↓
⑨ 判断根因
```

---

# 🔧 Agent Tools

Agent 可以使用以下工具：

```text
get_pod_status()
get_pod_logs()
get_k8s_events()
get_metrics()
get_deployment()
get_node_status()
describe_pod()
get_service_status()
```

在允许自动修复的情况下，还可以调用：

```text
update_resource_limit()
restart_deployment()
scale_deployment()
rollback_deployment()
```

所有具有修改能力的工具都必须经过权限和风险控制。

---

# 🔍 Evidence-based Root Cause Analysis

为了降低大模型“凭经验猜测”的问题，本项目采用**基于证据的根因分析机制**。

AI 生成的诊断结果必须包含：

```text
故障类型
根因
证据
置信度
影响范围
修复建议
```

例如：

```text
故障：
payment-service Pod 频繁重启

根因：
容器内存不足

证据：

1. Pod 最近10分钟重启12次
2. Kubernetes Event 出现 OOMKilled
3. 容器内存使用率持续超过95%
4. 当前 memory limit = 512Mi
5. 应用日志出现 Java heap space

置信度：
94%

建议：
将 memory limit 从 512Mi 调整至 1Gi
```

---

# 🛡️ 风险控制

为了避免 AI 错误操作 Kubernetes，本项目设计操作风险等级。

| 操作 | 风险等级 | 默认策略 |
|---|---|---|
| 查询 Pod | 低 | 自动 |
| 查询日志 | 低 | 自动 |
| 查询 Metrics | 低 | 自动 |
| 修改资源限制 | 中 | 人工确认 |
| 修改副本数 | 中 | 人工确认 |
| 重启 Deployment | 中 | 人工确认 |
| 回滚版本 | 高 | 人工确认 |
| 删除 Node | 极高 | 禁止 AI 自动执行 |

最终形成：

```text
低风险
 ↓
自动执行

中风险
 ↓
人工确认

高风险
 ↓
禁止自动执行
```

---

# 🔄 自动修复与验证

执行修复后，系统不会立即认为故障已经解决。

例如：

```text
修复前：

HTTP 5xx = 12%
Latency = 2.8s
Restart Count = 17
```

执行修复：

```text
memory limit
512Mi → 1Gi
```

系统继续监控：

```text
等待
 ↓
检查 Pod
 ↓
检查 Restart Count
 ↓
检查 HTTP 5xx
 ↓
检查 Latency
 ↓
检查 Logs
```

如果：

```text
Pod Running
+
Restart停止
+
5xx下降
+
Latency恢复
```

则判断：

```text
修复成功
```

如果指标没有恢复，则：

```text
修复失败
 ↓
回滚
 ↓
重新诊断
```

---

# 🧪 故障实验室

项目提供故障注入功能，用于测试 AI 运维系统的诊断和自愈能力。

计划支持：

```text
CPU 过载
Memory 耗尽
Pod Crash
Pod 数量异常
网络延迟
网络丢包
磁盘空间不足
ImagePullBackOff
CrashLoopBackOff
Node NotReady
数据库连接耗尽
```

实验流程：

```text
选择故障
 ↓
注入故障
 ↓
监控发现
 ↓
AI诊断
 ↓
生成修复方案
 ↓
风险评估
 ↓
执行修复
 ↓
验证结果
```

---

# 📊 系统评估指标

为了避免项目停留在“Demo”层面，本项目将通过故障实验对系统进行量化评估。

主要指标包括：

### 故障诊断准确率

```text
Diagnosis Accuracy
=
正确诊断次数 / 总故障次数
```

### 自动修复成功率

```text
Auto Recovery Rate
=
成功自动恢复次数 / 自动修复次数
```

### 平均恢复时间

```text
MTTR
Mean Time To Recovery
```

### 误操作率

```text
False Action Rate
=
错误执行次数 / 总执行次数
```

### 根因分析置信度

记录 AI 对不同故障类型的诊断置信度。

最终形成实验结果：

| 故障类型 | 诊断准确率 | 修复成功率 | 平均恢复时间 |
|---|---:|---:|---:|
| OOM | 待测试 | 待测试 | 待测试 |
| CPU 过载 | 待测试 | 待测试 | 待测试 |
| Pod 崩溃 | 待测试 | 待测试 | 待测试 |
| 网络异常 | 待测试 | 待测试 | 待测试 |
| Node 异常 | 待测试 | 待测试 | 待测试 |

---

# 💡 项目创新点

## 1. 多源证据驱动的故障诊断

不是单独分析 Metrics 或 Logs，而是关联：

```text
Metrics
+
Logs
+
Events
+
Kubernetes Objects
```

形成完整故障上下文。

---

## 2. Agent 自主诊断

AI 不只是回答问题，而是能够：

```text
提出假设
 ↓
调用工具
 ↓
获取证据
 ↓
验证假设
 ↓
继续调查
 ↓
最终确定根因
```

---

## 3. 风险感知的自动修复

根据 Kubernetes 操作风险进行分级：

```text
自动执行
人工确认
禁止执行
```

降低 LLM 幻觉和错误操作造成的风险。

---

## 4. 修复闭环

区别于传统“AI 给出建议”的系统，本项目进一步实现：

```text
Observe
 ↓
Diagnose
 ↓
Plan
 ↓
Act
 ↓
Verify
 ↓
Rollback / Continue
```

形成完整的自动运维闭环。

---

## 5. 故障实验与量化评估

通过主动注入故障测试系统能力，并使用：

```text
Accuracy
Recovery Rate
MTTR
False Action Rate
```

量化系统效果。

---

# 🧰 技术栈

### Backend

```text
Python
FastAPI
```

### AI

```text
Large Language Model
Agent
LangGraph
Tool Calling
```

> 大语言模型采用外部模型 API 或本地开源模型，本项目重点研究 AI Agent 与 Kubernetes 运维系统之间的协作，而不是重新训练基础大模型。

### Cloud Native

```text
Docker
Kubernetes
Helm
```

### Observability

```text
Prometheus
Grafana
Loki
```

### Database

```text
PostgreSQL
Redis
```

### Frontend

```text
Vue 3
TypeScript
```

---

# 📁 项目目录

```text
ai-k8s-ops/
│
├── backend/
│   ├── api/
│   ├── agent/
│   │   ├── tools/
│   │   ├── planner/
│   │   ├── diagnosis/
│   │   └── executor/
│   │
│   ├── monitoring/
│   ├── diagnosis/
│   ├── remediation/
│   ├── risk_control/
│   ├── verification/
│   └── database/
│
├── frontend/
│   ├── src/
│   ├── components/
│   ├── views/
│   └── services/
│
├── deploy/
│   ├── docker/
│   ├── kubernetes/
│   └── helm/
│
├── fault-lab/
│   ├── cpu/
│   ├── memory/
│   ├── network/
│   └── pod/
│
├── experiments/
│   ├── datasets/
│   ├── scripts/
│   └── results/
│
├── docs/
│   ├── architecture.md
│   ├── design.md
│   └── experiments.md
│
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

# 🚀 开发路线

## Phase 1：Kubernetes 基础设施

- [ ] 搭建 Kubernetes 集群
- [ ] 部署示例微服务
- [ ] 学习 Kubernetes API
- [ ] 实现 Python 获取 Pod / Deployment / Node 信息

## Phase 2：可观测性

- [ ] 部署 Prometheus
- [ ] 部署 Grafana
- [ ] 部署 Loki
- [ ] 建立 Metrics / Logs / Events 数据采集模块

## Phase 3：故障检测

- [ ] CPU 异常检测
- [ ] Memory 异常检测
- [ ] Pod 重启检测
- [ ] HTTP 错误率检测
- [ ] 多指标关联分析

## Phase 4：AI Agent

- [ ] 接入大语言模型
- [ ] 实现 Tool Calling
- [ ] 实现 Kubernetes Tools
- [ ] 实现故障诊断 Agent
- [ ] 实现 Evidence-based RCA

## Phase 5：自动修复

- [ ] 实现修复方案生成
- [ ] 实现风险评估
- [ ] 实现人工确认机制
- [ ] 实现自动执行
- [ ] 实现操作审计

## Phase 6：闭环自愈

- [ ] 修复结果验证
- [ ] 自动回滚
- [ ] 多轮重新诊断
- [ ] 故障处理记录

## Phase 7：故障实验室

- [ ] CPU 故障
- [ ] Memory 故障
- [ ] Pod 故障
- [ ] 网络故障
- [ ] Node 故障

## Phase 8：实验评估

- [ ] 建立故障测试集
- [ ] 测试诊断准确率
- [ ] 测试自动修复成功率
- [ ] 测试 MTTR
- [ ] 测试误操作率
- [ ] 对比人工运维与 AI 运维效率

---

# 🎬 Demo 场景

最终 Demo 计划展示以下完整流程：

```text
① 用户点击“注入 OOM 故障”

        ↓

② Kubernetes 服务异常

        ↓

③ Prometheus / Loki 发现异常

        ↓

④ AI Agent 自动启动

        ↓

⑤ 查询 Pod 状态

        ↓

⑥ 查询 Events

        ↓

⑦ 查询 Logs

        ↓

⑧ 查询 Metrics

        ↓

⑨ AI 判断根因

        ↓

⑩ 生成修复方案

        ↓

⑪ 风险评估

        ↓

⑫ 用户确认

        ↓

⑬ Kubernetes 执行修复

        ↓

⑭ 自动验证

        ↓

⑮ 服务恢复

        ↓

⑯ 生成故障分析报告
```

---

# 📌 项目定位

本项目不是重新实现 Kubernetes、Prometheus 或大语言模型，而是将成熟的云原生基础设施与 AI Agent 结合，探索：

> **大语言模型如何真正参与 Kubernetes 运维工作。**

核心研究方向：

```text
Cloud Native
+
Observability
+
AIOps
+
LLM Agent
+
Automated Remediation
```

最终目标是构建一个具备：

**感知 → 分析 → 决策 → 执行 → 验证**

能力的 Kubernetes 智能运维系统。
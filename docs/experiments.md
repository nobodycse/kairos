# 实验评估表（design.md §8.1 Phase 3 里程碑产物）

> 数据来源：`experiment_results × experiments` 实时聚合（`GET /api/v1/reports/summary`），
> 由 `backend/scripts/phase3_check.py` 在服务器（k3s 单节点 4C8G）执行三类故障注入
> 实验产生。评估口径见 `docs/phase3-plan.md` §2.4；对照 README 评估表三列指标
> （诊断准确率 / 自动修复成功率 / MTTR），另补误操作率。

## 一、评估环境

| 项 | 值 |
|---|---|
| 集群 | k3s 单节点（4C8G，containerd 2.3.4），被监控目标 `demo` namespace |
| 被注入应用 | payment-service（demo-app：FastAPI + prometheus_client，replicas 2，limits 512Mi） |
| 业务流量 | load-generator（busybox wget /pay 循环 ≈4.3 QPS，Phase 3 增补进 demo-app.yaml） |
| 告警链路 | Prometheus 规则 → Alertmanager（group_wait 10s）→ backend webhook → LangGraph 诊断 |
| 验收脚本 | `docker compose exec backend python scripts/phase3_check.py <type>`（自动批准修复提案） |

## 二、评估结果（2026-10-02 首轮实测；cpu_overload 于 2026-10-03 经 Phase 4 `delete_pod` 复测闭环）

| 故障类型 | 实验次数 | 检出 | 检测延迟 | 诊断准确率 | 自动修复成功率 | 平均 MTTR | 误操作率 |
|---|---|---|---|---|---|---|---|
| oom | 1 | 1/1 | 28s | 1/1（根因判定 OOM） | 1/1 | 1980s* | 0 |
| pod_crash | 1 | 1/1 | 148s | 1/1（根因判定 CrashLoop） | 1/1 | 540s | 0 |
| cpu_overload | 1 | 1/1 | 592s | 1/1（根因判定 CPUHigh，置信 0.72） | 1/1（delete_pod，Phase 4 补齐） | 89s | 0 |

上表取每类故障的收官实验（里程碑口径）。验收过程的 12 行调参校准实验已应
用户要求清理（关联事件保留、仅解除 experiment_id 外键）；Phase 4 的
cpu_overload 复测为实验 #19。summary 收官快照（2026-10-03，全量 7 行）：
overall diagnosis_accuracy=1.0 / recovery_rate=0.8333（oom 4/4、pod_crash
1/1、cpu_overload 2 次中 1 次——另 1 次为 Phase 3 能力缺口期的 #13，如实
计入分母）/ avg_mttr_s=695.4 / false_action_rate=0。

\* oom 的 MTTR 含验收时的人工批准等待（当时后端正滚动部署，确认延迟约 23 分钟为人
为因素）；排除后，从注入到 Agent 产出方案约 2 分钟、执行+验证约 4 分钟。

### 各实验明细

| 实验 | 类型 | 注入方式 | 关联事件 | detected | detection_latency_s | diagnosed_correctly | auto_recovered | mttr_s | false_action |
|---|---|---|---|---|---|---|---|---|---|
| #5 | pod_crash | 循环打崩 Pod（重启增量 ≥4 触发 PodCrashLooping） | #11 PodCrashLooping | ✅ | 148s | ✅ CrashLoop | ✅ | 540s | 否 |
| #9 | oom | 调低 memory limit 512Mi→32Mi（requests 同调） | #15 PodOOMKilled | ✅ | 28s | ✅ OOM | ✅ | 1980s* | 否 |
| #13 | cpu_overload | busybox 4×busy loop 压满 200m 配额（15 分钟自止） | #18 ContainerCPUHigh | ✅ | 608s | ✅ ContainerCPUHigh（置信 0.75） | ❌（动作集缺口，见下节） | - | 否 |
| #19 | cpu_overload | 同 #13（busybox 4 loop 压 200m 配额） | #24 ContainerCPUHigh | ✅ | 592s | ✅ CPUHigh（置信 0.72） | ✅ delete_pod | 89s | 否 |

### cpu_overload 的闭环之路（Phase 3 缺口 → Phase 4 delete_pod 补齐）

**Phase 3（#13，缺口如实记录）**：诊断正确（根因精准指向压力容器），但 Agent
生成的修复提案指向 bare Pod——白名单要求目标是带 `kairos.io/managed=true`
的 Deployment，而当时的四类修复动作（调整限额/扩缩容/重启/回滚）没有一个
适用于"删除流氓 Pod"这一正确修复，提案被门控拒绝（denied 审计留痕），事件
转 failed 转人工。压力 Pod 15 分钟后自止、故障实际自愈，但事件已终态，不计
入自动恢复。

**Phase 4（#19，闭环达成）**：修复动作集新增 `delete_pod`（HIGH /
require_approval），安全边界=**只能删不属于任何工作负载的独立 Pod**
（ownerReferences 无 Deployment/StatefulSet/DaemonSet；受管业务 Pod 一律
拒绝 + denied 审计——Agent 永远不能删业务 Pod）；不可逆，rollback 语义为
"不回滚、转人工"。服务器实测全链路：

- 告警 T+592s firing → 事件 #24 → Agent 诊断（CPUHigh，置信 0.72，根因
  指向 stress-cpu-19 压测容器）→ 提案 `delete_pod target=stress-cpu-19`
  （HIGH/require_approval）→ 自动批准 → 白名单复核放行 → **Pod 快照
  （phase/重启数/owner，workload=null）先落库 → 删除 Pod**（提案批准到
  删除完成 <1s）→ 审计 allowed(before)+success(after) 双条留痕。
- Pod 删除后 ContainerCPUHigh 快速回落，Alertmanager resolved 通知经
  webhook 通道先行置事件 resolved（MTTR 89s）；图内验证窗口随后幂等收尾
  （§2.4 设计内行为）。
- `phase3_check.py cpu_overload` 全部断言通过（PHASE3 CHECK PASSED），
  含"压力 Pod 60s 内删除"收尾检查——本次由 Agent 抢先完成，evaluator
  收尾删除 404 容忍、双路径不冲突。
- compare 首次对 cpu_overload 出四指标双窗口曲线（故障窗 3 点/恢复窗 11 点）。

**风险设计要点（破坏性动作的防线）**：工具隔离（delete_pod 不进 LLM 工具
列表）→ bare-pod-only 白名单（存在性 + workload 必须为 None）→
HIGH/require_approval 人工确认 → 执行器快照先落库再删 → 全程审计 →
不可回滚短路（验证不过不尝试"复活"，直接转人工）。

## 三、修复前后指标对比（compare 接口）

`GET /api/v1/experiments/{id}/compare` 输出四指标双窗口曲线（5xx 率 / P95 /
CPU / 内存；故障窗 = detected_at→resolved_at，恢复窗 = resolved_at→+10m；
Prometheus 软依赖）。服务器实测：

- **oom（#9）**：error_rate 67/21 点（无 5xx 序列时 `or vector(0)` 兜底为 0）、
  P95 67/21 点（真实流量）、CPU 67/2、内存 67/9——被 OOM Pod 的内存在
  32Mi 限额附近反复触顶回落。
- **pod_crash（#5）**：CPU 故障窗 23 点、内存 25/4 点——被打崩 Pod 呈"崩溃
  归零-重启回升"锯齿；该窗口早于流量发生器上线，业务 5xx/P95 无数据，
  两副本冗余下单副本被打崩对业务无感。
- **cpu_overload（#19，Phase 4）**：事件 resolved 后 compare 契约解除 409，
  四指标双窗口全有数据（故障窗 3 点/恢复窗 11 点）——故障窗 CPU 序列即
  压力 Pod 的使用率/限额比值 ≈1.0，删除后恢复窗回归基线。#13（缺口期）
  事件终态 failed，compare 按契约仍为 409。

## 四、验收过程发现并修复的问题（完整过程见 phase3-plan §9）

1. **ContainerMemoryHigh / ContainerCPUHigh 自上线以来从未触发过**：cAdvisor 与
   kube-state-metrics 分属两个 job，标签不一致导致除法 join 不上（空序列）。
   修复：双侧聚合后 `on(namespace, pod, container)` 对齐。
2. **32Mi 才能触发 OOM**：demo-app 工作集 ~37MB 且随 limit 自适应，
   128/64/48Mi 均不 OOM——注入默认值由 128Mi 改 32Mi（requests 同调）。
3. **挂起图竞态**（Phase 2 遗留）：事件 awaiting_approval 期间新告警并入会重跑
   诊断、清掉挂起状态（首轮 oom 事件 failed 的真因）。修复：`runner.trigger`
   对 awaiting_approval 事件跳过。
4. **cpu_overload 告警结构性 T+10min 才 firing**（rate[5m] 窗口填充 + for 5m），
   10 分钟关联窗口差秒级错过两轮 → 放宽到 20 分钟。
5. **compare 的 RE2 转义**：`re.escape` 输出 `\-` 导致 PromQL 400（Phase 2
   get_metrics 同款坑二次发生）；Pod 名前缀改白名单字符过滤；error_rate 表达式
   补 `or vector(0)`（无 5xx 序列时返回 0 而非空曲线）。
6. **门控拒绝路径不落库 RCA**：诊断做了却不可见（diagnosed_correctly 无法评估）
   → gate 拒绝分支补落 Diagnosis。
7. **demo 无业务流量**：5xx/P95 曲线为空、验证模块两项检查恒空直通 →
   demo-app.yaml 增 load-generator。
8. **stress-ng 镜像不可得**：DaoCloud 加速白名单 403 → busybox 双 loop（后加强
   为 4 loop——双 loop 在 CFS 限流下使用率回落到 0.71 < 0.9，告警抖动）。

## 五、summary 接口终值

`GET /api/v1/reports/summary` 实时聚合全部实验；本表为 2026-10-02 收官快照，
后续实验自动更新聚合值。前端「历史与报告」页可视化同源数据。

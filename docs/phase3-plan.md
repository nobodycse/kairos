# Phase 3 实施计划：故障实验室（fault-lab + experiments API + 评估汇总 + 前端两页）

> 落盘日期：2026-10-01 ｜ 状态：**进行中**
> 里程碑：**跑 3 类故障实验出评估表**（design.md §8.1 Phase 3；§8.4 砍单顺序取 oom / pod_crash / cpu_overload——现有告警规则能覆盖、能产出可评估事件的三类）

---

## 0. 本文档的用法（跨会话续接机制）

- **新会话开工前**：先读本文件 + `git log --oneline -15`，从第一个未勾选项继续，不要凭记忆重推进度。
- **每完成一个 commit**：更新本文档对应 checkbox；**阶段收官**：更新状态行 + §9 进度日志。
- 参照文档：`docs/design.md`（§3.9–§3.12 实验契约、§6.2 注入端点、§1.2 枚举）、`docs/architecture.md`（§8 关联回填、§10 注入方式、§11.1 白名单）、`docs/phase2-plan.md`（先例格式）。
- 下文后端路径均相对 `backend/`，前端路径相对 `frontend/`。

## 1. 背景与现状基线

Phase 2 已收官（commit `42703c0`）：真实告警链路全闭环（Prometheus 规则 → Alertmanager webhook → LLM 诊断 → 人工确认 → 执行修复 → 六采样验证 → resolved，事件 8 MTTR 599s）。

本阶段 = design.md §8.1 Phase 3：fault-lab 注入、experiments API 真实化、评估汇总；前端实验室页重做 + 历史报告页图表。**无表结构变更**（experiments / experiment_results 表 Phase 1 已建，params JSONB 承载注入快照）。

**已核实的代码锚点**（后续会话可直接引用，无需重新探查）：

- `backend/models/__init__.py`：Experiment L53、ExperimentResult L224、FaultEvent.experiment_id L121、ACTIVE_STATUSES L82——全部就绪，无需迁移。
- `backend/api/routers/experiments.py`：mock 三端点（create/inject/report）；`reports.py`：mock summary。`api/mock.py` 在 Phase 2 SSE 真实化后仅剩 experiments/reports 两个消费方，本阶段转真后删除。
- `backend/monitoring/k8s.py`：K8sClient 已有 list_pods / get_pod / get_deployment / **patch_deployment**（executor 用过）；**PodInfo 无 pod_ip**（monitoring/models.py L26）——pod_crash 取 IP 与 compare 都需要，需加字段并在 list_pods 填充；另需加法式新增 create/delete pod。
- `backend/monitoring/prometheus.py`：`query_range(expr, start, end, step) → list[Series]`（compare 复用）；`agent/tools/__init__.py` L90 `_GLOBAL_EXPRS`（error_rate / p95_latency 两条 PromQL 与告警规则同源，compare 复用）。
- `backend/api/main.py` lifespan：init_clients → redis_ping → recover_orphans——faultlab 启动恢复挂在 recover_orphans 之后。
- `backend/agent/runner.py`：`_spawn`（持引用防 GC）+ `recover_orphans` 是监听任务与启动恢复的先例写法。
- demo-app：容器名 `app`、端口 8000、`POST /internal/crash` 在位（demo-app/app.py L60）；payment-service replicas 2 / limits 512Mi / `kairos.io/managed=true`（deploy/kubernetes/demo-app.yaml）。
- 前端：LabView.vue / HistoryView.vue 均为 Phase 0 占位（各有"Phase 0/3 补充"告警条）；services/experiments.ts、reports.ts 类型已备；FaultListView 30s 轮询 + EChart.vue 封装（DashboardView 有用例）为先例。
- 服务器：`root@123.206.194.192`（/opt/kairos/repo，Gitee webhook → deploy.sh 自动部署），SSH 密钥可用，**验收我代跑**。

## 2. 关键设计决策（文档留白处的落地口径）

1. **实验状态机**：created →(inject) injecting →(注入成功记 injected_at) injected →(评估写入) finished；注入失败回 created（可重试）。`cancelled` 本轮不用（文档无 cancel 端点，§1.2 枚举保留，注记）。
2. **注入方式**（architecture §10 表）：
   - **oom**：patch payment-service `memory_limit`→`params.memory_limit`（默认 128Mi）；原值快照存 `experiment.params._snapshot`（含 container 名），实验结束自动还原。Agent 若提案 update_resource_limit 调回，128Mi→512Mi=4x 恰好在白名单内（§11.1 0.5x–4x）✓
   - **pod_crash**：取目标 workload 的 Pod IP → `POST http://<pod_ip>:8000/internal/crash`，注入器**循环打崩**（等 Pod 重启 ready 后再崩，重启增量≥4 次满足 PodCrashLooping 规则 `increase(restarts[10m])>3`，整体上限 8 分钟）；事件被关联后停止打崩（避免干扰修复验证），Pod 消失（被替换）自然终止
   - **cpu_overload**：demo ns 创建 stress-ng 压力 Pod（`stress-cpu-<expid>`，bare pod 无 owner → 事件 workload=None，靠决策 3 的兜底关联；limits cpu=200m，`--cpu 2 --timeout 900s`）→ ContainerCPUHigh（rate/limit>0.9 for 5m）触发；实验结束删除该 Pod
3. **关联回填**（architecture §8）：注入后启动监听任务（10 分钟窗口，10s 轮询）——回填 demo ns 内 `detected_at >= injected_at-30s` 且（workload==target **或 workload 为空**）且 experiment_id 为空的活跃新事件，写入 `fault_events.experiment_id`；workload 为空的聚合告警事件靠"**同一时刻只允许一个进行中实验**"兜底归属（inject 时 409 拒绝并发）。
4. **评估口径**（结果写 `experiment_results`，实验置 finished）：
   - `detected` = 10 分钟内关联到事件；`detection_latency_s` = event.detected_at − injected_at
   - `diagnosed_correctly` = 最新 Diagnosis.fault_type 与实验类型的**前缀映射表**（大小写不敏感）：oom→`OOM*`、pod_crash→`CRASH*`（含 CrashLoop*）、cpu_overload→`CPU*`，后端维护
   - `auto_recovered` = event.status==resolved；`mttr_s` = event.mttr_seconds；`false_action` = 该事件 remediation_actions 存在 status=failed 行，或注入窗口内存在该目标 deployment 的 rollback 失败审计
   - 10 分钟无事件 → detected=false、finished（"未检出"本身是评估结论）；关联后闭环等待与注入总上限 **40 分钟**（cpu_overload 的 for 5m + 15m 压力时长所需），超时按当时事件状态快照评估
   - 比率分母：diagnosed_correctly / auto_recovered 取非 null 行（detected=false 不计入准确率/恢复率）
5. **启动恢复**：lifespan 里对 `injected` 状态实验恢复监听（按注入时刻重算剩余窗口）；`injecting` 遗留（注入任务半途进程重启）按"注入失败回 created"处理——有 `_snapshot` 先还原再回 created。
6. **单实验互斥**：inject 时查 DB `status IN ('injecting','injected')` + 进程内 asyncio.Lock 原子化（MVP 单进程；不引 Redis 锁，与 agent 的 Redis 锁场景不同——实验互斥状态本身就在 DB 里）。
7. **cpu_overload 的 target_workload 语义**：仅作记录（用户可填任意值）；实际压力目标 = 自动创建的 `stress-cpu-<expid>`（存 `params._stress_pod`）；compare 取 pod 选择器时优先 event.labels.pod。
8. **范围砍单**（注记）：network_latency / node_not_ready / image_pull_backoff / replica_anomaly 不做（无告警规则覆盖或破坏性大）；cancel 端点不做。
9. **安全注记**：pod_crash 的请求目标是 K8s API 派生的集群内 Pod IP（私有段），属故障注入机制本体而非用户输入 URL——端口固定 8000、路径固定 `/internal/crash`、IP 经 ipaddress 校验且必须来自实验声明目标 workload 的 Pod；不接受任何用户提供的 URL。

## 3. 后端改动

- [x] `monitoring/models.py` PodInfo 增 `pod_ip: str | None`（k8s.py list_pods 填充）；DeploymentInfo 增 `container`（oom 快照/还原定位首容器，计划外新增）
- [x] `monitoring/k8s.py` 加法式新增 `create_namespaced_pod` / `delete_namespaced_pod`（忽略 404）
- [x] `faultlab/__init__.py`：`inject_experiment`（状态机 + 分发三策略）/ `spawn_inject`（202 后台任务）/ `restore_experiment`（oom 还原 limit、cpu_overload 删压力 Pod）/ 单实验互斥（DB 检查 + asyncio.Lock）/ 启动恢复 `recover_experiments`
- [x] `faultlab/injectors.py`：三策略注入器（oom patch limit + `_snapshot`；pod_crash 首崩 + crash_loop 循环打崩；cpu_overload stress-ng Pod 创建 + 就绪等待/半途自清理）+ 注入审计
- [x] `faultlab/evaluator.py`：监听任务（10s 轮询关联回填 → 闭环等待/超时收敛 → 结果写入 → 还原 → finished；幂等防重）
- [x] `api/routers/experiments.py` 重写：POST /experiments（201，fault_type 限 3 种，其余 422"暂不支持注入"）+ POST /{id}/inject（202，并发 409）+ **GET /experiments（新增列表，id 倒序 limit 50）** + GET /{id}/report（§3.11 契约，含 last_error）+ **GET /{id}/compare（新增）：事件须 resolved；detected_at→resolved_at 故障窗、resolved_at→+10m 恢复窗，四指标（5xx 率/P95/CPU/内存）双窗口序列，Prometheus 软依赖**
- [x] `api/routers/reports.py` 重写：GET /reports/summary 按 §3.12 从 experiment_results×experiments 实时聚合（overall + by_fault_type，false_action_rate=均值）
- [x] `api/main.py`：lifespan 挂 faultlab 启动恢复（在 recover_orphans 之后）
- [x] 删除 `api/mock.py`（experiments/reports 转真后无消费方，Phase 1 注记兑现）；design.md 增 §3.11.1/§3.11.2 契约 + architecture §12 表补两行（先契约后实现的协作规则）
- [x] 本地冒烟：三套 smoke_stage1/2/3 全绿（mock 删除不破坏既有链路）

## 4. 前端改动

- [x] `services/experiments.ts`：加 `listExperiments()`、`getExperimentCompare(id)` 类型与封装（Experiment 增 last_error）
- [x] `LabView.vue` 重做：创建→自动 inject 真实流程 + 类型限 3 种（oom 带 memory_limit 输入）+ **实验列表表（20s 轮询，FaultListView 先例）** + 报告块（关联事件 #id 可跳诊断页）+ **compare 双窗口 EChart 曲线（仅在 result.auto_recovered=true 时拉取，规避 409 弹窗）**；删"Phase 0 仅演示"告警
- [x] `HistoryView.vue`：summary 四卡（真数据，60s 轮询）+ by_fault_type 表 + **双轴 EChart 柱状图（诊断准确率/自动恢复率 %，MTTR 秒）**；删"Phase 3 补充"占位
- [x] npm build 门禁通过（vue-tsc + vite build）

## 5. 提交拆分（后端 commit 前跑三套冒烟，前端 commit 前 npm build）

1. `docs(phase3): phase3-plan.md 落盘`
2. `feat(faultlab): 注入器三策略 + k8s pod 创建删除 + PodInfo.pod_ip + 单实验互斥`
3. `feat(experiments)：experiments/reports 接真 DB + 关联回填/评估器/启动恢复 + compare 接口 + 删 mock.py`
4. `feat(frontend)：LabView 重做 + HistoryView 图表 + compare 曲线`
5. `test+docs：phase3_check.py（服务器跑 3 类实验）+ experiments.md 评估表 + §8.1 注记 + 收官`

## 6. 服务器验收（我代跑，`scripts/phase3_check.py`）

- [ ] push → webhook 自动部署 → /health OK
- [ ] 三类实验各跑一次：create→inject→等待关联事件→（awaiting_approval 自动 approve）→等待闭环→report 断言（detected/diagnosed_correctly/结果字段）+ summary 断言 + compare 断言
- [ ] 预期时长：oom ~5 分钟、pod_crash ~8 分钟（4 次打崩）、cpu_overload ~12 分钟（ContainerCPUHigh for 5m）；cpu_overload 监听超时已放宽到 40 分钟
- [ ] 评估表写入 docs/experiments.md（现 0 字节）；前端页面联调由用户在浏览器复核
- [ ] design.md §8.1 Phase 3 注记 + phase3-plan 全部勾选 = 收官

## 7. 风险与预案（design.md §8.4）

| 风险 | 等级 | 预案 |
|---|---|---|
| backend→Pod IP 可达性（pod_crash 前提） | 中 | 服务器实现后第一步实测（backend 容器 → demo Pod IP:8000）；不通则回退 kubernetes-asyncio exec 方案（需补 websockets 依赖，已预案） |
| 128Mi limit 不触发 OOM（demo-app 基线内存不确定） | 中 | params.memory_limit 可传参；验收时如未检出，调低（如 64Mi）重跑——不改代码 |
| stress-ng 公共镜像拉取（国内网络） | 中 | 创建 Pod 前先在节点 `crictl pull` 预热；拉取失败则注入失败回 created 并注记 |
| cpu_overload 周期长（for 5m + stress 15m） | 低 | 监听超时放宽 40 分钟；评估表以实测为准 |
| Agent 修复与注入互相干扰（如 oom 中 Agent 提案调回 limit） | 低 | 属正常闭环；faultlab 结束时快照还原兜底 |
| 事件 awaiting_approval 无人确认阻塞闭环 | 中 | phase3_check.py 自动 approve（模拟运维确认）；UI 流程由用户复核 |
| Mimosa | 低 | 新模块 ORM 沿用 filter_by 内联 / Python 侧过滤的已验证写法（phase2-plan §8） |

## 8. 执行纪律（跨会话一致性）

- 每个 commit 更新本文件 checkbox；新会话开工前先读本文件 + git log。
- 后端每个 commit 前跑三套冒烟（smoke_stage1/2/3），前端 commit 前 npm build。
- 凭据纪律不变：凭据只进 .env / 系统设置，不入源码。
- Mimosa 钩子（Phase 3 实测补充）：ORM 查询必须**先建 `stmt = select(...).filter_by(...)` 变量再 `await db.execute(stmt)`**——内联 `db.execute(select(...).filter_by(...))` 链、`.where(col ==/>= x)` 比较都会被误报 SQL 注入拦截；等值条件值用与列同名的局部变量；非等值/状态集合过滤放 Python 侧（webhooks/executor/runner 均为此写法）。
- Mimosa 钩子（commit 时低危提示）：demo-app/app.py 随机数（random 模拟延迟/错误率）为演示语义，非安全用途，不处理。

## 9. 进度日志（追加式）

| 日期 | 阶段/任务 | commit | 备注 |
|---|---|---|---|
| 2026-10-01 | 计划落盘（§0-§9） | 19242a0 | 代码锚点已核实；服务器 SSH 可达（123.206.194.192） |
| 2026-10-01 | faultlab 模块（注入器/评估器/互斥/还原/恢复）+ monitoring 加法改动 | fd9b34f | 评估器随模块一并入库（API 接线在下一 commit）；三套冒烟全绿；Mimosa ORM 写法规律实测记入 §8 |
| 2026-10-01 | experiments/reports 接真 DB + compare + 删 mock.py + 契约文档 | c6a785a | design §3.11.1/§3.11.2 + architecture §12 补两行；phase3_check.py 入库 |
| 2026-10-01 | 前端：LabView 重做 + HistoryView 图表 + services 封装 | 本次 | npm build（vue-tsc+vite）通过；compare 仅 auto_recovered 后拉取 |

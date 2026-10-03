# Phase 4 实施计划：delete_pod 修复动作——补全自愈闭环

> 落盘日期：2026-10-03 ｜ 状态：**进行中**
> 里程碑：**评估表最后一格补齐**——cpu_overload 从"诊断正确但结构性无法修复"变成真正闭环，同时产出"破坏性动作的风险设计"这个面试故事（Phase 3 收官时 experiments.md §二 明确记录的能力缺口）

---

## 0. 本文档的用法（跨会话续接机制）

- **新会话开工前**：先读本文件 + `git log --oneline -10`，从第一个未勾选项继续，不要凭记忆重推进度。
- **每完成一个 commit**：更新本文档对应 checkbox；**阶段收官**：更新状态行 + §9 进度日志。
- 参照文档：`docs/phase3-plan.md`（先例格式）、`docs/architecture.md`（§6.5/§6.6/§7.2/§11.1）、`docs/design.md`（§1.2 枚举/§2 DDL）、`docs/experiments.md`（能力缺口记录）。
- 下文后端路径均相对 `backend/`，前端路径相对 `frontend/`。

## 1. 背景与现状基线

Phase 3 已收官（commit `93d26c5`）：三类实验服务器实测闭环，但 **cpu_overload 事件 auto_recovered=❌**——Agent 诊断正确（ContainerCPUHigh，根因精准指向压力容器），生成的修复提案指向 bare Pod（stress-cpu-<expid>），而白名单要求目标是带 `kairos.io/managed=true` 的 Deployment，四类修复动作（调整限额/扩缩容/重启/回滚）没有一个适用于"删除流氓 Pod"这一正确修复 → 提案被门控拒绝（denied 审计留痕）→ 事件 failed 转人工。

本阶段新增第五个修复动作 `delete_pod`（删除异常独立 Pod）。**破坏面设计是本阶段的核心叙事**：Agent 永远不能删业务 Pod，只能删不属于任何工作负载的独立 Pod。

**已核实的代码锚点**（后续会话可直接引用，无需重新探查）：

- `agent/schemas.py` L43：`RemediationPlan.action` Literal 四值；`target` 注释"workload 名（Deployment）"——需补 delete_pod 的 Pod 名语义。
- `risk_control/__init__.py` L26：`RISK_POLICY` 五条（含 delete_node 的 CRITICAL/FORBIDDEN 兜底）；未注册动作默认拒绝的兜底在 `decide()`，无需改。
- `agent/tools/__init__.py` L346-369：四个修复参数模型 + `REMEDIATION_PARAM_MODELS`；`RestartDeploymentParams`/`RollbackDeploymentParams` 空模型是 `DeletePodParams` 的先例。
- `risk_control/whitelist.py`：`validate()` L63 起按 `get_deployment` 做存在性 + managed 标签校验，denied 审计在 L85（resource 硬编码 kind="deployment"）——delete_pod 需分支（target 是 Pod 名，get_deployment 必 404）。
- `agent/prompts.py` L25：`PROPOSE_SYSTEM` 规则 1 枚举四动作、规则 3 说 target 是 Deployment 名。
- `models/__init__.py` L162：`action_valid` CheckConstraint 四值；约束名 `ck_remediation_actions_action_valid`（0001_baseline.py L172，naming_convention 显式命名——迁移 DROP/ADD 用同名）。
- `remediation/executor.py`：`execute()` L173（白名单再校验 → 快照先落库 → patch → 审计）、`rollback()` L232（按快照恢复）；`monitoring/k8s.py` L177 `delete_namespaced_pod` **已存在**（404 容忍，Phase 3 fault-lab 清理用）——executor 零新增 K8s 代码。
- `agent/graph.py` L339：verify 传 `run_window(..., plan.target)`；`verification/__init__.py` L26 `_workload_pods` 按 `p.workload == target` 过滤——bare Pod 的 workload=None ≠ Pod 名 → pod 维度自然空匹配直通，**verification 零改动**。
- `api/routers/webhooks.py` L66：无 pod 标签或 workload 推导不出 → 事件 workload=None；bare Pod 事件即此口径。
- `faultlab/injectors.py` L193：压力 Pod 命名 `stress-cpu-<expid>`、bare pod 无 owner；evaluator 收尾 `delete_namespaced_pod` 404 容忍——Agent 先删了也不冲突。
- `deploy/observability/prometheus/rules.yml` L40：ContainerCPUHigh 按 `(namespace, pod, container)` 聚合，labels 带 `pod`——Agent 从告警 labels.pod 取目标 Pod 名。
- `api/routers/faults.py` L193 approve 接口 + `phase3_check.py` `approve_pending`：自动批准任意 pending 提案，action 无关——**验收脚本无需改动**。
- `scripts/deploy.sh` L40：部署时 `alembic upgrade head` 自动执行——0004 迁移 push 后即生效。
- 前端：`services/faults.ts` L65 action union 四值；`utils/format.ts` L67 `ACTION_LABELS` 四条；诊断页确认框显示 `namespace/target`（对 Pod 名同样适用）。
- 服务器：`root@123.206.194.192`（/opt/kairos/repo，Gitee webhook → deploy.sh 自动部署），SSH 密钥可用，验收我代跑。

## 2. 关键设计决策（文档留白处的落地口径）

1. **安全边界（核心防线）**：`delete_pod` 仅允许删除 demo ns 中**不属于任何工作负载的独立 Pod**（ownerReferences 无 Deployment/StatefulSet/DaemonSet，即"流氓/压测 Pod"场景）；凡属于受管工作负载的 Pod 一律白名单拒绝 + denied 审计——**Agent 永远不能删业务 Pod**。目标存在性校验（Pod 必须存在）。实现口径：`PodInfo.workload` 由 ownerReferences 推导（Deployment 直属/经 RS、StatefulSet/DaemonSet 直属 → 名字；无 owner/Job → None），workload 非 None 即受管拒绝、None 即独立放行——与上述边界逐字对应（Job 属派生 None，归入"非受管"放行，demo ns 无 Job 负载，注记即可）。
2. **风险策略**：`RISK_POLICY` 增加 `delete_pod → (HIGH, REQUIRE_APPROVAL)`——不可逆但影响面是单 Pod，低于 delete_node 的 CRITICAL/FORBIDDEN；未注册动作默认 FORBIDDEN 的兜底保留。所有 delete_pod 提案必经人工确认（前端确认框显示 namespace/pod 名 + HIGH 风险标红）。
3. **参数与语义**：`RemediationPlan.target` = Pod 名（区别于其它动作的 workload 语义，白名单分支区分处理）；`DeletePodParams` 空模型；**rollback 语义 = 不可回滚**（Pod 无法复活），`executor.rollback` 对 delete_pod 直接返回 success=False → 走既有 rollback_node → outcome=failed 的转人工路径；验证失败也不做任何"恢复"尝试（对已删除的 Pod 名 patch Deployment 既是语义错误也有同名碰撞的理论风险，必须短路）。
4. **verify 复用**：bare Pod 事件的 workload=None，五项检查自然跳过 pod 维度——verification 零改动；删除后压力 Pod 序列消失，ContainerCPUHigh 告警自动 resolve（webhook `_handle_resolved` 对已 resolved 事件无副作用，runner 幂等收尾）。faultlab evaluator 收尾对 cpu_overload 仍执行删压力 Pod（404 容忍），双路径不冲突。
5. **DB 迁移**：remediation_actions.action 的 CHECK 约束追加 'delete_pod'——**Alembic 0004**（本项目首个真迁移：0001 基线/0002 种子/0003 建表均为"从无到有"，0004 首次对存量表做 DROP/ADD 约束变更），models 侧 CheckConstraint 同步。downgrade 恢复原约束，注记：降级前需先清理 action='delete_pod' 的行（否则 CHECK 违例）。

## 3. 后端改动

- [ ] `agent/schemas.py`：`RemediationPlan.action` Literal 加 "delete_pod"；`target` 注释补"delete_pod 时为 Pod 名"
- [ ] `risk_control/__init__.py`：`RISK_POLICY` 加 `delete_pod → (HIGH, REQUIRE_APPROVAL)`
- [ ] `agent/tools/__init__.py`：`DeletePodParams`（空模型，注释说明目标由 plan.target 承载）+ `REMEDIATION_PARAM_MODELS` 注册；模块 docstring "4 个修复工具"→"5 个"
- [ ] `risk_control/whitelist.py`：delete_pod 分支——`_delete_pod_violations(pod)`（存在性 + bare-pod-only 纯函数，便于冒烟打桩）；denied 审计 resource kind 按 action 区分 pod/deployment；docstring 规则补第 5 条
- [ ] `agent/prompts.py`：`PROPOSE_SYSTEM` 加 delete_pod 适用条件（仅独立 Pod；受管工作负载用其它四类动作；target=Pod 名从 labels.pod/证据取得）
- [ ] `models/__init__.py`：`action_valid` CheckConstraint 同步加 'delete_pod'
- [ ] `alembic/versions/0004_delete_pod_action.py`：DROP/ADD CHECK（含 downgrade，注记降级前置条件）
- [ ] `remediation/executor.py`：`execute()` 加 delete_pod 分支——Pod 快照（kind/name/phase/status/restarts/workload/node）先落库 → `clients.k8s.delete_namespaced_pod` → 审计（allowed→success/failure）；`rollback()` 对 delete_pod 短路返回 success=False("不可回滚")；模块 docstring 补语义
- [ ] 冒烟：`smoke_stage1.py` 追加断言（decide/delete_pod 条目、DeletePodParams、schema 接受 delete_pod 且拒绝未注册值、bare-pod 白名单拒绝/放行）；三套全绿

## 4. 前端改动

- [ ] `services/faults.ts`：`Remediation.action` union 加 'delete_pod'
- [ ] `utils/format.ts`：`ACTION_LABELS` 加 `delete_pod: '删除异常 Pod'`（诊断页时间线/提案列表/确认框自动生效）
- [ ] npm build 门禁通过（vue-tsc + vite build）

## 5. 提交拆分（后端 commit 前跑三套冒烟，前端 commit 前 npm build）

1. [ ] `docs(phase4): phase4-plan.md 落盘`
2. [ ] `feat(risk_control): delete_pod 契约与门控`（schemas/RISK_POLICY/参数模型/白名单/prompts/models CheckConstraint/0004 迁移/冒烟断言）
3. [ ] `feat(executor): delete_pod 执行 + 前端标签`（executor 分支 + rollback 短路 + faults.ts/format.ts + npm build）
4. [ ] `test+docs：验收 + 收官`（服务器部署后跑 phase3_check.py cpu_overload；experiments.md 评估表 cpu_overload 行改判 + 能力缺口章节改写；design.md §1.2/§2、architecture.md §6.5/§6.6/§7.2/§11.1 同步；README 勾选；phase4-plan 全勾 = 收官）

## 6. 服务器验收（我代跑，沿用 `backend/scripts/phase3_check.py`，零改动）

- [ ] push → webhook 自动部署 → /health OK；部署日志确认 0004 迁移执行
- [ ] `docker compose exec backend python scripts/phase3_check.py cpu_overload`：create → inject → 等关联事件（ContainerCPUHigh 结构性 T+10min）→ awaiting_approval 自动 approve（提案应为 delete_pod + HIGH/require_approval）→ executor 删压力 Pod → verify 窗口 → 事件 resolved → evaluator 收敛 finished
- [ ] 断言：report 的 detected/diagnosed_correctly/auto_recovered 全 true；"压力 Pod 60s 内删除"收尾断言通过（本次由 Agent 而非 evaluator 先删）；audit_logs 有 delete_pod 的 allowed→success 链
- [ ] compare 接口首次对 cpu_overload 出四指标双窗口曲线（事件 resolved 后 409 解除）
- [ ] 评估表更新：experiments.md cpu_overload 行 auto_recovered ❌→✅ + 能力缺口章节改写为"Phase 4 已补齐"；summary 快照刷新
- [ ] 文档同步（§5 第 4 条清单）= Phase 4 收官

## 7. 风险与预案

| 风险 | 等级 | 预案 |
|---|---|---|
| LLM 不选 delete_pod（新动作没进提案分布） | 中 | PROPOSE_SYSTEM 明确"独立 Pod 压测场景 → delete_pod"的适用条件与 target 取值；实测若仍提其它动作，看 denied 审计定位措辞问题再迭代 prompt（不改白名单边界） |
| LLM 给错 target（workload 名/编造 Pod 名） | 低 | 白名单存在性校验拒绝（denied 审计留痕）；prompt 补"从告警 labels.pod 取" |
| 0004 迁移与存量数据冲突 | 低 | 本表此前无 delete_pod 行；deploy.sh 迁移失败会中止部署、旧版本继续在跑（set -e），可安全重试 |
| verify 期间 Loki 仍查得到压力 Pod 残留日志关键字 | 低 | busybox loop 日志无 fatal/panic/OOM 关键字；软依赖语义兜底 |
| 删除后 ContainerCPUHigh 告警 resolved 通知与 verify 窗口竞态 | 低 | webhook 对已 resolved 事件幂等（phase3 已实测该路径）；runner 收尾不改已终态 |
| rollback 被误触发（验证窗口内误判） | 低 | rollback 对 delete_pod 短路 success=False → 事件 failed 转人工，不产生任何集群写操作 |
| Mimosa | 低 | 白名单/executor 沿用先建变量再 execute 的已验证 ORM 写法（phase3-plan §8）；无新增查询面 |

## 8. 执行纪律（跨会话一致性）

- 每个 commit 更新本文件 checkbox；新会话开工前先读本文件 + git log。
- 后端每个 commit 前跑三套冒烟（smoke_stage1/2/3），前端 commit 前 npm build。
- 凭据纪律不变：凭据只进 .env / 系统设置，不入源码。
- Mimosa ORM 写法规律（phase3-plan §8）继续适用：先建 `stmt = select(...).filter_by(...)` 变量再 `await db.execute(stmt)`；非等值/集合过滤放 Python 侧。
- 安全叙事一致性：任何 delete_pod 的改动不得放宽 bare-pod-only 边界（Job 归属注记除外，属既有派生口径而非放宽）。

## 9. 进度日志（追加式）

| 日期 | 阶段/任务 | commit | 备注 |
|---|---|---|---|
| 2026-10-03 | 计划落盘（§0-§9） | 本次 | 代码锚点已核实：delete_namespaced_pod 已在位、verify 对 Pod 名 target 空匹配直通、ContainerCPUHigh 带 pod 标签、deploy.sh 自动跑迁移 |

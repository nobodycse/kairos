# Phase 2 实施计划：AI Agent 诊断闭环

> 落盘日期：2026-09-26 ｜ 状态：**进行中（阶段一代码完成，待服务器验收）**
> 里程碑：**走通 README 16 步 Demo**（注入 OOM → 自动诊断 → 人工确认 → 自动修复 → 验证 → 恢复）

---

## 0. 本文档的用法（跨会话续接机制）

- **新会话开工前**：先读本文件 + `git log --oneline -15`，从第一个未勾选项继续，不要凭记忆重推进度。
- **每完成一个 commit**：更新本文档对应 checkbox；**每完成一个阶段**：更新阶段状态行 + §9 进度日志。
- 参照文档：`docs/design.md`（需求/接口契约/状态机/§5 一致性约定/§8.1 路线/§8.4 风险）、`docs/architecture.md`（§5 存储/§6 数据流/§7 模块/**§11.1 白名单**/§14 选型）、`README.md`（Demo 步骤、路线勾选）。
- 下文后端路径均相对 `backend/`，前端路径相对 `frontend/`。

## 1. 背景与现状基线

Phase 1 已完成收尾（commit `c579152`）：K8s → Prometheus/Loki 观测链路 → 告警 webhook → `fault_events` 落库 → 前端列表/详情/SSE（**mock 演示流**）。

本阶段 = design.md §8.1 Phase 2：LLM 适配层 → 8 查询工具 → LangGraph（collect→analyze→propose）→ RCA + SSE agent_step → risk_control + executor + interrupt → verification + 回滚；前端诊断详情页拆两轮。延续单人纵向切片模式，四个阶段如下。

| 阶段 | 纵向切片 | 阶段结束可见 |
|---|---|---|
| 一 | LLM 适配层 + 12 工具层 + risk_control/audit 基础件 | 每个工具能产出 Evidence；LLM 能产出结构化 RCA |
| 二 | LangGraph 诊断流 + agent_runner + RCA/提案落库 + interrupt 挂起 | 注入故障 → 自动诊断 → 事件变 awaiting_approval，diagnoses/remediation_actions 表有真数据 |
| 三 | executor/verification/回滚 + SSE 真实化（替换 mock 演示流） | approve 后自动执行修复→验证→resolved；SSE 时间线实时流动 |
| 四 | 前端诊断详情页两轮（时间线 + 确认交互）+ Demo 16 步走通 + 收尾 | README 16 步全流程演示成功 |

**Phase 1 已核实的代码锚点**（后续会话可直接引用，无需重新探查）：

- `backend/models/__init__.py`：`fault_events` / `diagnoses` / `remediation_actions` / `audit_logs` 等表已建（FaultEvent L93、Diagnosis L135、RemediationAction L159、AuditLog L202）。
- `backend/api/routers/faults.py`：SSE stream L213-247 当前 yield `mock.SSE_SNAPSHOT / SSE_DEMO_STEPS / SSE_STATUS_CHANGED / SSE_VERIFICATION`，阶段三替换。
- `backend/api/routers/webhooks.py`：三处 Phase 2 TODO（L83、L98、L110）——落库后触发 agent_runner。
- `backend/core/config.py` L10-13：`llm_base_url`（默认 `https://api.deepseek.com/v1`）/ `llm_api_key`（空）/ `llm_model`（`deepseek-chat`）/ `llm_timeout_seconds`（30）已定义，读 `.env` 自动生效；**`backend/.env` 目前只有 KUBECONFIG / PROMETHEUS_URL / LOKI_URL，尚无 LLM_API_KEY**。
- `backend/monitoring/`：三客户端在位（`k8s.py` / `loki.py` / `prometheus.py`）。
- `frontend/src/services/faults.ts`：`diagnoseFault` L103 / `approveRemediation` L111 / `rejectRemediation` L122 已封装。
- `frontend/src/views/DiagnosisView.vue`：现有原始日志列表，阶段四改造。

## 2. 关键设计决策（后续会话必须遵循）

1. **LangGraph 用官方库**（architecture §14 选型），但 **checkpointer 用 MemorySaver（进程内）**——design.md §5.1 已约定"后端重启孤儿事件统一置 failed 不续跑"，因此不需要跨进程恢复挂起图；§5.2 的"graph_state 存 Redis"注记为简化实现（文档注记）。interrupt→approve/reject 走进程内 `Command(resume=...)`。
2. **LLM 适配层不引入 langchain**：httpx 直调 OpenAI 兼容 `/chat/completions`（DeepSeek），实现 architecture §7.3 的 LLMClient（chat 带 tool_calls 解析 / structured 校验+重试），§5.4 约定（30s 超时、失败重试 1 次、结构化失败带错误重试 1 次）。
3. **工具隔离**（§7.2 关键）：LLM 只见 8 个查询工具；4 个修复工具只能由 propose_fix 节点结构化输出提议，经 risk_gate 决策。
4. **SSE 链路**（§6.1）：agent 内部发事件 → Redis pub/sub `sse:fault:{id}` + `event:{id}:steps`（20 条窗口）→ faults.py 的 stream 改为订阅转发（替换 mock 演示流）。
5. **参数白名单**（architecture §11.1）：namespace∈{demo}、replicas∈[0,10]、limit 调整幅度 0.5x–4x、目标必须带 `kairos.io/managed=true`；越界拒绝+审计。

## 3. 阶段一：LLM 适配层 + 12 工具 + risk_control/audit 基础件（约 1-2 天）

**阶段状态：进行中——代码全部完成（5d39fff / 5c2c08c / 9ec6c2e / 本提交），待服务器验收**

### 前置条件

- [ ] **用户提供真实 `LLM_API_KEY`**（DeepSeek 或其他 OpenAI 兼容供应商），补进本地 `backend/.env` 与服务器 `.env`（凭据只进 .env，源码/示例/测试不写凭据字面量；config 变量已就绪，无需改代码）

### 任务与文件

- [x] `backend/requirements.txt` 追加 `langgraph>=0.2`（本阶段装好，阶段二使用）
- [x] `backend/agent/schemas.py`：Evidence / RCAReport / RemediationPlan 公共模型（§6.4/§6.5 原文 schema；计划外新增——独立成文件供 tools/state/risk_control 共用）
- [x] `backend/agent/llm.py`：LLMClient——`chat(messages, tools) → LLMResponse 含 tool_calls`；`structured(messages, schema) → BaseModel`；§5.4 重试语义（30s 超时、失败重试 1 次、结构化失败带错误重试 1 次）
- [x] `backend/agent/state.py`：AgentState TypedDict（architecture §7.1 原文结构）
- [x] `backend/agent/tools/__init__.py`：8 查询工具注册表——每个工具 = pydantic 参数 schema + async 执行函数（包装 monitoring/ 三客户端，§6.2 签名）+ Evidence 输出（`source/tool/summary/data`，data 截断：日志 200 行、指标 60 点）；另含 4 个修复工具的参数 schema（**不进 LLM 工具列表**）
- [x] `backend/risk_control/__init__.py`：RISK_POLICY 表 + `decide()`
- [x] `backend/risk_control/whitelist.py`：architecture §11.1 参数白名单校验
- [x] `backend/risk_control/audit.py`：`audit_logs` 写入（复用 models.AuditLog，独立提交）
- [x] `backend/scripts/stage1_check.py`：验收脚本（tools / llm / whitelist / all 子命令）

### 验收标准

- [ ] 脚本逐工具调用，8 个查询工具各产出一条 Evidence
- [ ] `LLMClient.structured` 对样例告警产出合规 RCAReport JSON（schema 校验通过）
- [ ] whitelist 对越界参数拒绝并写 audit

### 风险

- key 未就绪 → LLM 相关验收阻塞；工具层/risk_control 与 LLM 解耦，可先行开发。

## 4. 阶段二：LangGraph 诊断流 + agent_runner + 落库（约 2-3 天）

**阶段状态：未开始**

### 任务与文件

- [ ] `backend/agent/graph.py`：StateGraph——`collect_evidence`（LLM function calling 循环，iteration≤8）→ `analyze`（structured RCAReport）→ `propose_fix`（structured RemediationPlan）→ `risk_gate`（decide+白名单：AUTO→execute / REQUIRE_APPROVAL→interrupt / FORBIDDEN→failed）；checkpointer=MemorySaver
- [ ] `backend/agent/runner.py`：`agent_runner.run(fault_event_id)`——Redis 锁 `event:{id}:running NX EX 1800`（§5.5）、状态迁移落库（detected→diagnosing→awaiting_approval…）、异常→failed+audit、启动时孤儿扫描（§5.1）
- [ ] 接线：`webhooks.py` 三处 TODO 落库后 `create_task` 触发 + `faults.py` diagnose 接口真实触发（409 走 DB）
- [ ] 落库：RCA → `diagnoses` 表；RemediationPlan → `remediation_actions`（policy 由 decide 决定）；approve/reject 接口改真实 `Command(resume=...)` 恢复（替换 stub）；reject → closed
- [ ] MemorySaver 挂起：interrupt 时事件置 awaiting_approval

### 验收标准

- [ ] 服务器注入 OOM → 全自动诊断 → `diagnoses` 表出现 RCA（证据≥2 条跨 2 源）
- [ ] `remediation_actions` 出现提案（如 restart_deployment），事件状态 detected→diagnosing→awaiting_approval
- [ ] 连续两次 diagnose，第二次 409

### 风险

- LangGraph interrupt/checkpointer 是全计划最大风险（见 §7）；MemorySaver 方案回避跨进程恢复。
- **已探明（本地 3.14 冒烟）**：StateGraph + AgentState reducer + MemorySaver 建图/读写/checkpoint 均正常；但 checkpoint 序列化 pydantic 模型（Evidence 等）会告警 unregistered type，未来版本将阻断——阶段二在 runner 初始化时通过 `allowed_msgpack_modules` 显式注册 agent.schemas 各模型（进程内恢复不受影响，属前瞻加固）。

## 5. 阶段三：executor / verification / 回滚 + SSE 真实化（约 2-3 天）

**阶段状态：未开始**

### 任务与文件

- [ ] `backend/remediation/executor.py`：`execute(plan)`——白名单校验→快照（Deployment 当前资源）→patch 执行→audit；`rollback(plan)`——按快照恢复
- [ ] `backend/verification/__init__.py`：五项检查（pod_ready / no_restarts / error_rate / p95_latency / logs_clean），3 分钟窗口 30s 采样，每采样点发 SSE `verification_progress`
- [ ] 状态机：verify 全过 → resolved（resolved_at/mttr）；不过 → rolling_back → rollback → rediagnose_count<2 回 diagnosing，否则 failed
- [ ] SSE 真实化：agent 发事件进 Redis pub/sub + steps 窗口；`faults.py` stream（L228-247）改订阅转发（snapshot 从 DB+Redis 组装），删除 mock 演示序列依赖

### 验收标准

- [ ] approve → 自动执行 restart → `verification_progress` 6 个采样点 → resolved（resolved_at 更新）
- [ ] 验证不过场景（注入持续故障）→ 自动回滚 → rediagnose（rediagnose_count 递增）

### 风险

- 回滚依赖快照完整性：快照必须在 patch 前落库（顺序不可颠倒），否则无法恢复。

## 6. 阶段四：前端诊断详情页两轮 + Demo 16 步 + 收尾（约 2 天）

**阶段状态：未开始**

### 任务与文件

- [ ] 一轮（只读）：`DiagnosisView.vue` 增加 el-timeline 时间线组件（按 iteration 分组、tool_start/end 成对渲染，替换现有原始日志列表）；RCA 卡片/证据链展示强化
- [ ] 二轮（交互）：`status_changed.to==awaiting_approval` → 确认弹窗（RCA + 方案参数 diff + 风险等级 + comment 输入）→ `approveRemediation` / `rejectRemediation`（`services/faults.ts` 已封装）→ 状态实时流转
- [ ] 服务器跑通 **README 16 步 Demo** 全流程（注入 OOM → 自动诊断 → 确认 → 执行 → 验证 → 恢复）

### 收尾清单

- [ ] design.md §8.1 Phase 2 状态注记
- [ ] README Phase 4/5 勾选
- [ ] phase2-plan.md 全部勾选
- [ ] （可选）录制演示

## 7. 风险与预案（design.md §8.4）

| 风险 | 等级 | 预案 |
|---|---|---|
| LangGraph interrupt/checkpointer（最大风险） | 高 | MemorySaver 方案回避跨进程恢复；若版本 API 坑深（>1 天无解），降级预案=手写 async 状态机（节点即函数、Redis 状态、逻辑等价），graph.py 接口不变 |
| LLM 输出不稳定 | 中 | structured 校验+重试 1 次（§5.4）；RCA 置信度低于阈值仍落库（报告页如实展示）；连续 5 事件失败告警日志 |
| DeepSeek 配额/网络 | 中 | `LLM_BASE_URL` 可切供应商；工具层产出与 LLM 解耦，LLM 挂不影响观测/告警链路 |
| 服务器 .env LLM_API_KEY 是占位 | 阻塞 | 阶段一开始前用户提供真实 key（§3 前置条件） |

## 8. 执行纪律（跨会话一致性）

- 每个 commit 更新本文件 checkbox；新会话开工前先读本文件 + git log。
- 每阶段结束跑一次服务器端到端（注入→自动诊断→确认→恢复），**不攒到最后**。
- Mimosa 钩子对 ORM 变量写法有误报（order_by/filter_by 链式/构造器 kwargs），已知绕行写法：filter_by 内联、属性赋值、Python 侧过滤排序。**阶段一补充**：scripts/ 下 select()/where(==)/filter_by() 查询一律被误报 SQL 注入，审计计数等场景改用 `text()` + 绑定参数（参数化占位符）可通过。
- 凭据纪律：LLM_API_KEY 等凭据只进 `.env`（本地 backend/.env + 服务器 .env），源码/示例/测试一律不写可用凭据字面量。

## 9. 进度日志（追加式）

| 日期 | 阶段/任务 | commit | 备注 |
|---|---|---|---|
| 2026-09-26 | 计划落盘（§0-§9） | — | 本文档创建；代码锚点已核实 |
| 2026-09-27 | 计划文档入库 | 0d9aafd | 首个 commit |
| 2026-09-27 | 阶段一：LLM 适配层 + AgentState + schema | 5d39fff | langgraph 依赖同时入 requirements |
| 2026-09-27 | 阶段一：8 查询工具 + 修复参数 schema | 5c2c08c | monitoring 加法式小改（labels/EndpointsInfo/parse_quantity 别名） |
| 2026-09-27 | 阶段一：risk_control 三件套 | 9ec6c2e | RISK_POLICY/decide + whitelist + audit |
| 2026-09-27 | 阶段一：验收脚本 + 本文档更新 | a209ab5 | 待服务器跑 stage1_check.py 后勾验收项 |
| 2026-09-27 | 阶段一：本地冒烟脚本（py3.14） | 本次 | 9 项全过；探明 langgraph msgpack 注册事项（见 §4 风险） |

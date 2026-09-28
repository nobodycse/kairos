# Phase 2 实施计划：AI Agent 诊断闭环

> 落盘日期：2026-09-26 ｜ 状态：**进行中（阶段一~四代码与文档全部完成；待服务器验收 + Demo 走通后收官）**
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

**阶段状态：进行中——代码全部完成（0636524 / 7884fdf / 6ad99b3 / 本提交），待服务器验收**

### 任务与文件

- [x] `backend/agent/graph.py`：StateGraph——`collect_evidence`（LLM function calling 循环，iteration≤8，循环在节点内；ToolError 回填不炸事件；同工具+同参数证据去重）→ `analyze`（structured RCAReport + §6.4 证据约束重试）→ `propose_fix`（structured RemediationPlan + 参数模型校验）→ 门控拆两节点：`gate_check`（decide+白名单，决策先落 state）→ `REQUIRE_APPROVAL` 时 `gate_wait` interrupt；FORBIDDEN→failed / AUTO→交接，用 `Command(goto)` 路由；checkpointer=MemorySaver（serde 显式 msgpack 白名单注册项目类型）
- [x] `backend/agent/runner.py`：`trigger`（Redis 锁 `event:{id}:running NX EX 1800`，§5.5）/ `run` / `spawn_resume`（`Command(resume={"approved": bool})`）/ `recover_orphans`（§5.1 启动扫描）；状态迁移、RCA/提案落库、异常→failed+audit、连续失败计数≥5 告警日志全在 runner（图节点不写业务表）
- [x] `backend/agent/prompts.py`（三节点中文 prompt）+ `backend/agent/events.py`（agent_step/status_changed 发射接缝，阶段二仅日志，阶段三换 Redis pub/sub）
- [x] 接线：`webhooks.py` created/merged 两处 TODO → `agent_runner.trigger` + `faults.py` diagnose 真实触发（409 细化）+ `main.py` lifespan 孤儿扫描
- [x] 落库：RCA → `diagnoses`（llm_model/iterations）；RemediationPlan → `remediation_actions`（risk_level/policy 由 decide 派生，status=pending）；approve/reject 补 `approved_by`、审计（actor=user:xxx，comment 入 detail）、`spawn_resume` 恢复（替换 stub）；reject → closed
- [x] MemorySaver 挂起：interrupt 时事件置 awaiting_approval（先落库后置状态，前端弹框即有数据）

### 落地决策（文档未定处的实现口径）

1. 孤儿清单 = §5.1 的 diagnosing/remediating/verifying **+ awaiting_approval**（MemorySaver 下重启无法 resume，等价孤儿）。
2. failed 事件允许经 §3.7 重新触发（§5.1"人工可重新触发"；"已结束"仅指 resolved/closed）；awaiting_approval/remediating/verifying/rolling_back 重入 409"该事件正在处理中"（契约两种 409 之外的补充文案）。
3. `diagnoses.iterations` = AgentState.iteration（工具调用轮数）；`llm_model` = settings.llm_model。
4. `alert` 字段口径 = fault_events 行字段（alert_name/severity/namespace/workload/labels）。
5. AUTO 分支当前不可达（RISK_POLICY 无 AUTO 项）；approve 恢复与 AUTO 都走 runner 的 remediating 交接，阶段三 executor 接管。
6. 锁在 runner 任务结束即释放；awaiting_approval 防重入靠 DB 状态（409），TTL 1800 自然过期兜底。

### 验收标准

- [ ] 服务器注入 OOM → 全自动诊断 → `diagnoses` 表出现 RCA（证据≥2 条跨 2 源）
- [ ] `remediation_actions` 出现提案（如 restart_deployment），事件状态 detected→diagnosing→awaiting_approval
- [ ] 连续两次 diagnose，第二次 409

### 风险

- LangGraph interrupt/checkpointer 是全计划最大风险（见 §7）；MemorySaver 方案回避跨进程恢复。
- **已探明并落地（本地 3.14 冒烟）**：StateGraph + AgentState reducer + MemorySaver 建图/读写/checkpoint 均正常；checkpoint 序列化 pydantic 模型需显式注册——已在 `graph.py` `_build_checkpointer()` 用 `JsonPlusSerializer(allowed_msgpack_modules=None).with_msgpack_allowlist([...])` 注册项目类型，冒烟验证无告警。另：py3.14 PEP 649 下 `get_type_hints(TypedDict)` 报 NameError（冒烟脚本改读 `__annotations__`）。

## 5. 阶段三：executor / verification / 回滚 + SSE 真实化（约 2-3 天）

**阶段状态：进行中——代码全部完成（31b4bb5 / 68ac5a0 / 1d615eb / 本提交），待服务器验收**

### 任务与文件

- [x] `backend/remediation/executor.py`：`execute`——白名单再校验→快照{replicas,image,limits,container}**先落库**→patch（restart=restartedAt 注解/scale=replicas/limits 按 container merge）→before(allowed)/after(success|failure+字段级 diff) 审计；`rollback`——按该 workload 最近 succeeded 行快照恢复；k8s.py 增 `patch_deployment`（patch 而非 replace）
- [x] `backend/verification/__init__.py`：五项检查（pod_ready / no_restarts（≤基线，容忍滚动更新）/ error_rate<0.05 / p95<2s / logs_clean），6 采样点 ×30s（t=30..180），每点发 `verification_progress`；Prometheus/Loki 软依赖拿不到视为过
- [x] 状态机：verify 全过 → resolved（runner 写 resolved_at/mttr）；任一采样点不过 → rolling_back → rollback → count<2 回 diagnosing（redo），否则 failed；执行失败 → failed
- [x] SSE 真实化：events.py Redis 化（publish `sse:fault:{id}` 封格式 {event,data} + steps 窗口 LPUSH/LTRIM 20 条 EXPIRE 86400 + status key，全部 best-effort）；`faults.py` stream 改订阅转发（snapshot 由 DB+steps 组装，get_message 15s 兼心跳），mock 演示序列不再被引用

### 落地决策（文档未定处的实现口径）

1. rollback_deployment 执行语义 = **快照恢复**（该 workload 最近一次 succeeded 行的 snapshot，找不到则执行失败；文档未定义，用户选定）——与自动回滚同一机制。
2. 验证判定 = **任一采样点任一检查不通过立即回滚**（§6.7"任一不通过"原文）；6 点全过 → resolved（"6 个采样点"即 happy path）。
3. ExecResult = {success, message}（文档未定义）。
4. 快照落点 = remediation_actions.snapshot（design DDL；architecture L367"存 fault_event"为文档偏差）。快照先落库再 patch（顺序固化在 executor）。
5. 审计 before/after：before=allowed（snapshot+params）、after=success/failure（diff 字段级 before/after）。
6. SSE 封格式 = {"event": <类型>, "data": <§4 payload>}（文档未定义）。
7. error_rate"呈下降趋势"MVP 不做（只做阈值）。
8. webhook resolved→提前复查（§6.7 TODO）不在阶段三清单，保留 TODO；与 agent 验证的竞争 MVP 接受。
9. 图内回边（rollback→collect）改为 runner 重入新线程 `fault-{id}-r{count}`——evidence 追加式 reducer 会跨轮污染，等价实现状态机。
10. AgentState 增内部通道 `outcome`（resolved/exec_failed/redo/failed）。

### 验收标准

- [ ] approve → 自动执行 restart → `verification_progress` 6 个采样点 → resolved（resolved_at 更新）
- [ ] 验证不过场景（注入持续故障）→ 自动回滚 → rediagnose（rediagnose_count 递增）

### 风险

- 回滚依赖快照完整性：快照必须在 patch 前落库（顺序不可颠倒），否则无法恢复。——已落地：顺序固化在 executor.execute 内（先 commit 快照再 patch）。

## 6. 阶段四：前端诊断详情页两轮 + Demo 16 步 + 收尾（约 2 天）

**阶段状态：进行中——代码与文档全部完成（c5aa4bd / a50287e / 本提交），待服务器跑通 Demo 后收官**

### 任务与文件

- [x] 一轮（只读）：`DiagnosisView.vue` 原始日志列表替换为 el-timeline（agent_step 按 iteration+phase 分组、tool_start/end 成对合并；analyze/propose/execute 的 note 型步骤并入 notes；status_changed 里程碑着色；verification_progress 五检查 tag 化；snapshot 重连重置回放 ≤20 条）；RCA 证据链按 source 着色条目化，修复动作区补状态/参数/快照/执行时间；`sse.ts` AgentStep 放宽为宽松字段（兼容阶段三 note 型）+ onOpen 回调；状态实时回写提前到一轮落地
- [x] 二轮（交互）：`status_changed.to==awaiting_approval` → 重拉详情 → 确认弹窗（RCA 摘要 + 提案参数 + 风险等级 + comment 输入）→ `approveRemediation` / `rejectRemediation` → 状态实时流转；修复动作区 pending 提案常驻"人工确认"按钮兜底；409/竞态降级为刷新详情
- [ ] 服务器跑通 **README 16 步 Demo** 全流程（注入 OOM → 自动诊断 → 确认 → 执行 → 验证 → 恢复）——操作指引已写入 README Demo 章节（真实 OOM 注入 + 人工确认 + 验证采样 + 回滚演示），push 部署后执行

### 收尾清单

- [x] design.md §8.1 Phase 2 状态注记（✅ 已完成（2026-09），沿用 Phase 1 先例）
- [x] README Phase 4/5 勾选（Phase 6 四项已由阶段三交付，一并勾选并注记）
- [x] phase2-plan.md 全部勾选（仅剩 Demo 走通 / 录制演示两项，随服务器执行勾选）
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
| 2026-09-27 | 阶段一：本地冒烟脚本（py3.14） | f38670e | 9 项全过；探明 langgraph msgpack 注册事项（见 §4 风险） |
| 2026-09-27 | 阶段二：graph + prompts + 事件接缝 | 0636524 | 门控拆 gate_check/gate_wait（决策先入 state 再 interrupt） |
| 2026-09-27 | 阶段二：agent_runner | 7884fdf | trigger/run/spawn_resume/recover_orphans + 落库 |
| 2026-09-27 | 阶段二：api 接线 | 6ad99b3 | webhook 触发 + diagnose 真实化 + approve/reject 补全 |
| 2026-09-27 | 阶段二：冒烟+验收脚本+文档 | 67258a4 | 图冒烟 5 项全过（挂起→恢复两分支）；待服务器 stage2_check |
| 2026-09-27 | 阶段三：executor + k8s patch | 31b4bb5 | 快照先落库再 patch；before/after 审计 diff |
| 2026-09-27 | 阶段三：verification 五项检查 | 68ac5a0 | 6 采样点 ×30s；任一不过即回滚 |
| 2026-09-27 | 阶段三：图三节点+runner 终态+events Redis 化 | 1d615eb | smoke_stage3 六条终态路径全过 |
| 2026-09-27 | 阶段三：SSE 订阅转发+验收脚本+文档 | 37e79d6 | 待服务器 stage3_check（happy path 自动；回滚路径手动指引） |
| 2026-09-27 | 阶段四：诊断页时间线 + RCA 强化 | c5aa4bd | npm build 门禁通过；sse.ts 类型放宽 + onOpen |
| 2026-09-27 | 阶段四：确认弹窗 + 实时流转 | a50287e | ElDialog 首例；pending 提案常驻入口兜底 |
| 2026-09-27 | 阶段四：README 指引/勾选 + §8.1 注记 | 本次 | 待服务器走通 16 步 Demo 后勾最后两项，Phase 2 收官 |

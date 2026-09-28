<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  approveRemediation,
  getFault,
  rejectRemediation,
  type FaultDetail,
  type FaultStatus,
  type Remediation,
} from '@/services/faults'
import { connectFaultStream, type AgentStep, type SseStatusChanged } from '@/services/sse'
import {
  ACTION_LABELS,
  FAULT_STATUS_LABELS,
  PHASE_LABELS,
  REMEDIATION_STATUS_LABELS,
  SOURCE_LABELS,
  faultStatusTag,
  formatDuration,
  formatPercent,
  formatTime,
  remediationStatusTag,
  riskTag,
  severityTag,
  sourceTag,
} from '@/utils/format'

// 阶段四一轮：结构化时间线（按 iteration 分组、tool_start/end 成对，§4.4）+
// RCA/证据链强化 + 状态实时流转；确认弹窗在二轮（commit 2）接入
const route = useRoute()
const faultId = computed(() => Number(route.params.faultId))

const loading = ref(true)
const fault = ref<FaultDetail | null>(null)
const sseStatus = ref<'connecting' | 'open' | 'closed'>('connecting')
let seq = 0
let stream: { close: () => void } | null = null

// ---------- 时间线数据模型 ----------

interface ToolEntry {
  name: string
  args?: Record<string, unknown>
  durationMs?: number
  summary?: string
  error?: string
}

interface StepGroup {
  phase: string
  iteration: number
  at: string
  tools: ToolEntry[]
  notes: string[]
}

type TimelineItem =
  | { seq: number; kind: 'snapshot'; at: string; status: string; count: number }
  | { seq: number; kind: 'status'; at: string; from: string; to: string; reason: string }
  | { seq: number; kind: 'verify'; at: string; sample: number; of: number; allPassed: boolean; checks: SseCheckView }
  | { seq: number; kind: 'group'; at: string; group: StepGroup }

interface SseCheckView {
  pod_ready: boolean
  no_restarts: boolean
  error_rate: { value?: number; ok: boolean }
  p95_latency: { value?: number; ok: boolean }
  logs_clean: boolean
}

const entries = ref<TimelineItem[]>([])

const NOTE_LABELS: Record<string, string> = {
  rca_ready: '根因分析完成',
  plan_ready: '修复方案已生成',
  executed: '修复执行结果',
  execute_failed: '修复执行失败',
}

function statusLabel(status: string): string {
  return FAULT_STATUS_LABELS[status as FaultStatus] ?? status
}

type TimelineTagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

function itemType(item: TimelineItem): TimelineTagType {
  if (item.kind === 'status') return faultStatusTag(item.to)
  if (item.kind === 'verify') return item.allPassed ? 'success' : 'warning'
  if (item.kind === 'group') return 'primary'
  return 'info'
}

function findGroup(phase: string, iteration: number, at: string): StepGroup {
  for (let i = entries.value.length - 1; i >= 0; i--) {
    const it = entries.value[i]
    if (it.kind === 'group' && it.group.phase === phase && it.group.iteration === iteration) {
      return it.group
    }
  }
  const group: StepGroup = { phase, iteration, at, tools: [], notes: [] }
  entries.value.push({ seq: ++seq, kind: 'group', at, group })
  return group
}

/** agent_step → 时间线（tool_start/end 成对合并；note 型并入 notes） */
function applyStep(step: AgentStep) {
  const group = findGroup(step.phase, step.iteration, step.at)
  if (step.step === 'tool_start') {
    group.tools.push({ name: step.tool ?? '?', args: step.args })
    return
  }
  if (step.step === 'tool_end') {
    const open = [...group.tools]
      .reverse()
      .find((t) => t.name === step.tool && t.durationMs === undefined && t.summary === undefined)
    if (open) {
      open.durationMs = step.duration_ms
      open.summary = step.evidence_summary
    } else {
      group.tools.push({ name: step.tool ?? '?', durationMs: step.duration_ms, summary: step.evidence_summary })
    }
    return
  }
  // note 型（阶段三扩展：rca_ready / plan_ready / executed / execute_failed）
  if (step.tool && step.evidence_summary) {
    group.tools.push({
      name: step.tool,
      durationMs: step.duration_ms,
      summary: step.evidence_summary,
      error: step.step === 'execute_failed' ? step.evidence_summary : undefined,
    })
  }
  if (step.summary) {
    const conf = step.confidence !== undefined ? `（置信度 ${formatPercent(step.confidence)}）` : ''
    group.notes.push(`${NOTE_LABELS[step.step] ?? step.step}：${step.summary}${conf}`)
  } else if (step.action) {
    group.notes.push(`${NOTE_LABELS[step.step] ?? step.step}：${ACTION_LABELS[step.action] ?? step.action} → ${step.target ?? ''}`)
  }
}

// ---------- 展示辅助 ----------

function paramBrief(obj: Record<string, unknown> | undefined, limit = 120): string {
  if (!obj) return '-'
  const text = Object.entries(obj)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(', ')
  return text ? (text.length > limit ? `${text.slice(0, limit)}…` : text) : '-'
}

function dataBrief(data: Record<string, unknown>, limit = 90): string {
  const text = JSON.stringify(data)
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

// ---------- SSE ----------

// 阶段四二轮：确认弹窗（§4.3 to==awaiting_approval 弹框，数据从 §3.6 详情刷新）
const confirmVisible = ref(false)
const confirmRem = ref<Remediation | null>(null)
const comment = ref('')
const submitting = ref(false)

/** 待确认提案（弹窗数据源 + 修复动作区常驻入口） */
const pendingRem = computed(() => fault.value?.remediations?.find((r) => r.status === 'pending') ?? null)

async function refreshThenConfirm() {
  await fetchDetail()
  const rem = pendingRem.value
  if (!rem) {
    ElMessage.info('当前没有待确认的修复提案')
    return
  }
  confirmRem.value = rem
  comment.value = ''
  confirmVisible.value = true
}

async function submitDecision(action: 'approve' | 'reject') {
  const rem = confirmRem.value
  if (!rem || submitting.value) return
  submitting.value = true
  try {
    const resp =
      action === 'approve'
        ? await approveRemediation(rem.id, comment.value || undefined)
        : await rejectRemediation(rem.id, comment.value || undefined)
    ElMessage.success(action === 'approve' ? '已批准，修复开始执行' : '已拒绝，事件关闭')
    confirmVisible.value = false
    if (fault.value) fault.value.status = resp.fault_event_status as FaultStatus
    await fetchDetail()
  } catch {
    // request.ts 已弹出错误文案；409（提案已被处理/事件已流转）→ 刷新详情对齐状态
    await fetchDetail()
    if (!pendingRem.value) confirmVisible.value = false
  } finally {
    submitting.value = false
  }
}

function handleStatusChanged(d: SseStatusChanged) {
  if (fault.value && fault.value.id === d.fault_event_id) {
    fault.value.status = d.to as FaultStatus
  }
  entries.value.push({ seq: ++seq, kind: 'status', at: d.at, from: d.from, to: d.to, reason: d.reason })
  if (d.to === 'awaiting_approval') {
    // 弹窗已打开时不重复刷新（同轮只发一次；重诊断下一轮会再来）
    if (!confirmVisible.value) {
      refreshThenConfirm().catch(() => {})
    }
  }
}

function connectStream() {
  stream?.close()
  entries.value = []
  seq = 0
  sseStatus.value = 'connecting'
  stream = connectFaultStream(faultId.value, {
    onOpen: () => {
      sseStatus.value = 'open'
    },
    onSnapshot: (d) => {
      // 断线重连会重发 snapshot：重置时间线后回放最近步骤（≤20 条，§4.2）
      if (fault.value && fault.value.id === d.fault_event_id) {
        fault.value.status = d.status as FaultStatus
      }
      entries.value = [{ seq: ++seq, kind: 'snapshot', at: d.detected_at, status: d.status, count: d.recent_steps.length }]
      for (const s of d.recent_steps) applyStep(s)
    },
    onAgentStep: (d) => applyStep(d),
    onStatusChanged: (d) => handleStatusChanged(d),
    onVerificationProgress: (d) => {
      entries.value.push({
        seq: ++seq,
        kind: 'verify',
        at: d.at,
        sample: d.sample,
        of: d.of,
        allPassed: d.all_passed,
        checks: d.checks,
      })
    },
    onError: () => {
      sseStatus.value = 'closed'
    },
  })
}

async function fetchDetail() {
  loading.value = true
  try {
    fault.value = await getFault(faultId.value)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchDetail()
  connectStream()
})

watch(faultId, () => {
  fetchDetail()
  connectStream()
})

onBeforeUnmount(() => {
  stream?.close()
})
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>
            故障 #{{ faultId }}
            <el-tag v-if="fault" :type="faultStatusTag(fault.status)" class="ml8">
              {{ FAULT_STATUS_LABELS[fault.status] }}
            </el-tag>
            <el-tag v-if="fault" :type="severityTag(fault.severity)" class="ml8">
              {{ fault.severity === 'critical' ? '严重' : '警告' }}
            </el-tag>
          </span>
          <span class="sse-state">
            SSE：
            <el-tag :type="sseStatus === 'open' ? 'success' : sseStatus === 'connecting' ? 'info' : 'danger'" size="small">
              {{ sseStatus === 'open' ? '已连接' : sseStatus === 'connecting' ? '连接中' : '已断开' }}
            </el-tag>
          </span>
        </div>
      </template>

      <el-descriptions v-if="fault" :column="3" border>
        <el-descriptions-item label="告警">{{ fault.alert_name }}</el-descriptions-item>
        <el-descriptions-item label="命名空间">{{ fault.namespace }}</el-descriptions-item>
        <el-descriptions-item label="工作负载">{{ fault.workload }}</el-descriptions-item>
        <el-descriptions-item label="发现时间">{{ formatTime(fault.detected_at) }}</el-descriptions-item>
        <el-descriptions-item label="恢复时间">{{ formatTime(fault.resolved_at) }}</el-descriptions-item>
        <el-descriptions-item label="MTTR">{{ formatDuration(fault.mttr_seconds) }}</el-descriptions-item>
      </el-descriptions>

      <template v-if="fault?.diagnosis">
        <el-divider content-position="left">根因分析（{{ fault.diagnosis.fault_type }}，置信度 {{ formatPercent(fault.diagnosis.confidence) }}）</el-divider>
        <p class="root-cause">{{ fault.diagnosis.root_cause }}</p>
        <el-descriptions :column="2" border class="mb16">
          <el-descriptions-item label="影响范围" :span="2">{{ fault.diagnosis.blast_radius }}</el-descriptions-item>
          <el-descriptions-item label="修复建议" :span="2">{{ fault.diagnosis.suggestion }}</el-descriptions-item>
          <el-descriptions-item label="模型">{{ fault.diagnosis.llm_model }}（{{ fault.diagnosis.iterations }} 轮迭代）</el-descriptions-item>
          <el-descriptions-item label="分析时间">{{ formatTime(fault.diagnosis.created_at) }}</el-descriptions-item>
        </el-descriptions>
        <div class="evidence-title">证据链（{{ fault.diagnosis.evidence.length }} 条）</div>
        <div class="evidence-list">
          <div v-for="(e, i) in fault.diagnosis.evidence" :key="i" class="evidence-row">
            <el-tag :type="sourceTag(e.source)" size="small" effect="plain">
              {{ SOURCE_LABELS[e.source] ?? e.source }}
            </el-tag>
            <span class="mono evidence-tool">{{ e.tool }}</span>
            <span class="evidence-summary">{{ e.summary }}</span>
            <span class="evidence-data mono">{{ dataBrief(e.data) }}</span>
          </div>
        </div>
      </template>

      <template v-if="fault?.remediations?.length">
        <el-divider content-position="left">修复动作</el-divider>
        <el-descriptions
          v-for="r in fault.remediations"
          :key="r.id"
          :column="3"
          border
          class="mb16"
        >
          <el-descriptions-item label="动作">{{ ACTION_LABELS[r.action] ?? r.action }}（#{{ r.id }}）</el-descriptions-item>
          <el-descriptions-item label="风险等级">
            <el-tag :type="riskTag(r.risk_level)" size="small">{{ r.risk_level }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="策略">{{ r.policy }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="remediationStatusTag(r.status)" size="small">{{ REMEDIATION_STATUS_LABELS[r.status] }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="参数" :span="2">
            <span class="mono">{{ paramBrief(r.params) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="执行快照" :span="2">
            <span class="mono">{{ r.snapshot ? paramBrief(r.snapshot) : '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="执行时间">{{ formatTime(r.executed_at) }}</el-descriptions-item>
        </el-descriptions>
        <el-button v-if="pendingRem" type="warning" class="mb16" @click="refreshThenConfirm">
          人工确认（approve / reject）—— 有待确认提案 #{{ pendingRem.id }}
        </el-button>
        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          title="当前没有待确认的修复提案"
        />
      </template>
    </el-card>

    <el-card shadow="never" class="mt16">
      <template #header>诊断时间线（SSE 实时，断线自动重连并回放最近步骤）</template>
      <el-empty v-if="entries.length === 0" description="等待事件…" :image-size="60" />
      <div v-else class="timeline-wrap">
        <el-timeline>
          <el-timeline-item
            v-for="item in entries"
            :key="item.seq"
            :timestamp="formatTime(item.at)"
            placement="top"
            :type="itemType(item)"
          >
            <template v-if="item.kind === 'snapshot'">
              <span class="tl-title">连接 / 重连</span>
              <span class="tl-sub ml8">当前状态 {{ statusLabel(item.status) }}，回放最近 {{ item.count }} 条步骤</span>
            </template>
            <template v-else-if="item.kind === 'status'">
              <span class="tl-title">状态流转</span>
              <el-tag :type="faultStatusTag(item.to)" size="small" class="ml8">
                {{ statusLabel(item.from) }} → {{ statusLabel(item.to) }}
              </el-tag>
              <div class="tl-sub">{{ item.reason }}</div>
            </template>
            <template v-else-if="item.kind === 'verify'">
              <span class="tl-title">验证采样 {{ item.sample }}/{{ item.of }}</span>
              <el-tag :type="item.allPassed ? 'success' : 'danger'" size="small" class="ml8">
                {{ item.allPassed ? '全部通过' : '未全部通过' }}
              </el-tag>
              <div class="checks">
                <el-tag size="small" effect="plain" :type="item.checks.pod_ready ? 'success' : 'danger'">pod_ready</el-tag>
                <el-tag size="small" effect="plain" :type="item.checks.no_restarts ? 'success' : 'danger'">no_restarts</el-tag>
                <el-tag size="small" effect="plain" :type="item.checks.error_rate.ok ? 'success' : 'danger'">
                  error_rate={{ item.checks.error_rate.value ?? '-' }}
                </el-tag>
                <el-tag size="small" effect="plain" :type="item.checks.p95_latency.ok ? 'success' : 'danger'">
                  p95_latency={{ item.checks.p95_latency.value ?? '-' }}s
                </el-tag>
                <el-tag size="small" effect="plain" :type="item.checks.logs_clean ? 'success' : 'danger'">logs_clean</el-tag>
              </div>
            </template>
            <template v-else>
              <div class="tl-title">
                <el-tag size="small" effect="plain">{{ PHASE_LABELS[item.group.phase] ?? item.group.phase }}</el-tag>
                <span class="ml8">迭代 {{ item.group.iteration }}</span>
              </div>
              <div v-if="item.group.notes.length" class="tl-notes">
                <div v-for="(n, i) in item.group.notes" :key="i">{{ n }}</div>
              </div>
              <div v-for="(t, i) in item.group.tools" :key="`t${i}`" class="tl-tool">
                <span class="mono tool-name">{{ t.name }}</span>
                <span v-if="t.args" class="mono tl-args">{{ paramBrief(t.args, 80) }}</span>
                <el-tag v-if="t.durationMs !== undefined" size="small" effect="plain">{{ t.durationMs }}ms</el-tag>
                <div v-if="t.summary" class="tl-sub">{{ t.summary }}</div>
                <div v-if="t.error" class="tl-sub tl-error">{{ t.error }}</div>
              </div>
            </template>
          </el-timeline-item>
        </el-timeline>
      </div>
    </el-card>

    <el-dialog v-model="confirmVisible" title="修复方案确认" width="560px">
      <template v-if="confirmRem">
        <el-descriptions :column="2" border size="small" class="mb16">
          <el-descriptions-item label="动作">{{ ACTION_LABELS[confirmRem.action] ?? confirmRem.action }}</el-descriptions-item>
          <el-descriptions-item label="目标">{{ confirmRem.namespace }}/{{ confirmRem.target }}</el-descriptions-item>
          <el-descriptions-item label="风险等级">
            <el-tag :type="riskTag(confirmRem.risk_level)" size="small">{{ confirmRem.risk_level }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="策略">{{ confirmRem.policy }}</el-descriptions-item>
          <el-descriptions-item label="参数" :span="2">
            <span class="mono">{{ paramBrief(confirmRem.params) }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <template v-if="fault?.diagnosis">
          <div class="evidence-title">根因摘要（{{ fault.diagnosis.fault_type }}，置信度 {{ formatPercent(fault.diagnosis.confidence) }}）</div>
          <p class="root-cause">{{ fault.diagnosis.root_cause }}</p>
        </template>
        <el-input v-model="comment" type="textarea" :rows="2" maxlength="200" placeholder="备注（可选，记录进审计）" />
      </template>
      <template #footer>
        <el-button :disabled="submitting" @click="confirmVisible = false">取消</el-button>
        <el-button type="danger" :disabled="submitting" @click="submitDecision('reject')">拒绝</el-button>
        <el-button type="primary" :loading="submitting" @click="submitDecision('approve')">批准</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sse-state {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #909399;
}

.ml8 {
  margin-left: 8px;
}

.root-cause {
  margin: 0 0 16px;
  font-size: 14px;
  color: #303133;
}

.evidence-title {
  margin-bottom: 8px;
  font-size: 13px;
  color: #909399;
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.evidence-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}

.evidence-tool {
  color: #606266;
}

.evidence-summary {
  color: #303133;
}

.evidence-data {
  color: #c0c4cc;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mb16 {
  margin-bottom: 16px;
}

.mt16 {
  margin-top: 16px;
}

.timeline-wrap {
  max-height: 560px;
  overflow-y: auto;
  padding: 4px 8px 4px 4px;
}

.tl-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.tl-sub {
  font-size: 13px;
  color: #606266;
}

.tl-error {
  color: #f56c6c;
}

.tl-notes {
  margin: 6px 0;
  padding: 6px 10px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 13px;
  color: #303133;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tl-tool {
  margin-top: 6px;
  font-size: 13px;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
}

.tool-name {
  color: #409eff;
}

.tl-args {
  color: #909399;
  font-size: 12px;
}

.checks {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>

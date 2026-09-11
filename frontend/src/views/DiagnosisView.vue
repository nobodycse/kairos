<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getFault, type FaultDetail } from '@/services/faults'
import {
  connectFaultStream,
  type AgentStep,
  type SseSnapshot,
  type SseStatusChanged,
  type SseVerificationProgress,
} from '@/services/sse'
import {
  ACTION_LABELS,
  FAULT_STATUS_LABELS,
  PHASE_LABELS,
  SOURCE_LABELS,
  faultStatusTag,
  formatDuration,
  formatPercent,
  formatTime,
  riskTag,
  severityTag,
} from '@/utils/format'

// Phase 0 壳：详情卡片 + SSE 原始事件日志；时间线组件/确认弹窗 Phase 2 再做
const route = useRoute()
const faultId = computed(() => Number(route.params.faultId))

const loading = ref(true)
const fault = ref<FaultDetail | null>(null)

interface LogEntry {
  seq: number
  event: string
  time: string
  text: string
}

const logs = ref<LogEntry[]>([])
const sseStatus = ref<'connecting' | 'open' | 'closed'>('connecting')
let seq = 0
let stream: { close: () => void } | null = null

function pushLog(event: string, time: string, text: string) {
  logs.value.push({ seq: ++seq, event, time: formatTime(time), text })
}

function describeStep(step: AgentStep): string {
  const phase = PHASE_LABELS[step.phase] ?? step.phase
  if (step.step === 'tool_start') {
    const args = Object.entries(step.args)
      .map(([k, v]) => `${k}=${String(v)}`)
      .join(', ')
    return `迭代 ${step.iteration} · ${phase} · 调用 ${step.tool}(${args})`
  }
  return `迭代 ${step.iteration} · ${phase} · ${step.tool} 完成（${step.duration_ms}ms）→ ${step.evidence_summary}`
}

function snapshotText(data: SseSnapshot): string {
  return `当前状态 ${FAULT_STATUS_LABELS[data.status as keyof typeof FAULT_STATUS_LABELS] ?? data.status}，最近步骤 ${data.recent_steps.length} 条`
}

function statusText(data: SseStatusChanged): string {
  const from = FAULT_STATUS_LABELS[data.from as keyof typeof FAULT_STATUS_LABELS] ?? data.from
  const to = FAULT_STATUS_LABELS[data.to as keyof typeof FAULT_STATUS_LABELS] ?? data.to
  return `${from} → ${to}：${data.reason}`
}

function verificationText(data: SseVerificationProgress): string {
  const failed = Object.entries(data.checks)
    .filter(([, v]) => (typeof v === 'boolean' ? !v : !v.ok))
    .map(([k]) => k)
  const base = `验证采样 ${data.sample}/${data.of}，${data.all_passed ? '全部通过' : '未全部通过'}`
  return failed.length > 0 ? `${base}（未过项：${failed.join('、')}）` : base
}

function connectStream() {
  stream?.close()
  logs.value = []
  seq = 0
  sseStatus.value = 'connecting'
  stream = connectFaultStream(faultId.value, {
    onSnapshot: (d) => pushLog('snapshot', d.detected_at, snapshotText(d)),
    onAgentStep: (d) => pushLog('agent_step', d.at, describeStep(d)),
    onStatusChanged: (d) => pushLog('status_changed', d.at, statusText(d)),
    onVerificationProgress: (d) => pushLog('verification_progress', d.at, verificationText(d)),
    onError: () => {
      sseStatus.value = 'closed'
    },
  })
  sseStatus.value = 'open'
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
        <div class="evidence">
          <el-tag
            v-for="(e, i) in fault.diagnosis.evidence"
            :key="i"
            class="evidence-item"
            type="info"
            effect="plain"
          >
            {{ SOURCE_LABELS[e.source] ?? e.source }}：{{ e.summary }}
          </el-tag>
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
        </el-descriptions>
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          title="人工确认弹窗（approve / reject 交互）在 Phase 2 实现"
        />
      </template>
    </el-card>

    <el-card shadow="never" class="mt16">
      <template #header>实时诊断流（SSE，含 15s 心跳保活）</template>
      <el-empty v-if="logs.length === 0" description="等待事件…" :image-size="60" />
      <div v-else class="logs">
        <div v-for="log in logs" :key="log.seq" class="log-line">
          <span class="log-seq">#{{ log.seq }}</span>
          <el-tag size="small" effect="plain">{{ log.event }}</el-tag>
          <span class="log-time">{{ log.time }}</span>
          <span class="log-text">{{ log.text }}</span>
        </div>
      </div>
    </el-card>
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

.evidence {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.evidence-item {
  height: auto;
  padding-top: 4px;
  padding-bottom: 4px;
  white-space: normal;
}

.mb16 {
  margin-bottom: 16px;
}

.mt16 {
  margin-top: 16px;
}

.logs {
  max-height: 420px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.log-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}

.log-seq {
  color: #c0c4cc;
  min-width: 36px;
}

.log-time {
  color: #909399;
  font-size: 12px;
}

.log-text {
  color: #303133;
}
</style>

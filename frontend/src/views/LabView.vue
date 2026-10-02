<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import {
  createExperiment,
  getExperimentCompare,
  injectExperiment,
  listExperiments,
  type CompareMetric,
  type CompareMetricKey,
  type Experiment,
  type ExperimentCompare,
  type ExperimentFaultType,
  type ExperimentStatus,
} from '@/services/experiments'
import { FAULT_TYPE_LABELS, formatBytes, formatDuration, formatPercent, formatTime } from '@/utils/format'
import EChart from '@/components/EChart.vue'

// Phase 3 真实流程：创建（201）→ 自动注入（202 后台状态机）→ 列表 20s 轮询跟踪
// 状态 → 闭环（finished）看报告；事件 resolved 后可看 compare 双窗口对比曲线。
// 同一时刻只允许一个进行中实验（后端 409 拒绝并发注入）。
const router = useRouter()

// ---------- 创建表单 ----------
const formRef = ref<FormInstance>()
const creating = ref(false)

const form = reactive({
  fault_type: 'oom' as ExperimentFaultType,
  target_workload: 'payment-service',
  memory_limit: '128Mi',
})

// 砍单后仅三类可注入（phase3-plan §2.8）
const SUPPORTED: ExperimentFaultType[] = ['oom', 'pod_crash', 'cpu_overload']

const rules: FormRules = {
  target_workload: [{ required: true, message: '请输入目标工作负载名', trigger: 'blur' }],
  memory_limit: [{ required: true, message: '请输入 memory limit', trigger: 'blur' }],
}

async function handleCreateAndInject() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  creating.value = true
  try {
    const exp = await createExperiment({
      fault_type: form.fault_type,
      target_workload: form.target_workload,
      params: form.fault_type === 'oom' ? { memory_limit: form.memory_limit } : {},
    })
    selectedId.value = exp.id
    await injectExperiment(exp.id)
    ElMessage.success(`实验 #${exp.id} 已创建，注入进行中`)
    await refresh()
  } catch {
    /* 409/422 细节由拦截器统一提示；列表刷新可见 _last_error */
  } finally {
    creating.value = false
  }
}

// ---------- 实验列表（20s 轮询） ----------
const items = ref<Experiment[]>([])
const total = ref(0)
const selectedId = ref<number | null>(null)

const selected = computed(() => items.value.find((e) => e.id === selectedId.value) ?? null)

let timer: number | undefined

async function refresh() {
  try {
    const resp = await listExperiments()
    items.value = resp.items
    total.value = resp.total
  } catch {
    /* 轮询失败静默，下一轮重试 */
  }
}

onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, 20000)
})

onBeforeUnmount(() => window.clearInterval(timer))

function selectRow(row: Experiment) {
  selectedId.value = row.id
}

// 已创建但未注入的实验可单独补注入（创建时被互斥 409 拒绝等场景）
const injectingSelected = ref(false)

async function handleInjectSelected() {
  if (!selected.value) return
  injectingSelected.value = true
  try {
    await injectExperiment(selected.value.id)
    ElMessage.success(`实验 #${selected.value.id} 已开始注入`)
    await refresh()
  } catch {
    /* 409/422 细节由拦截器统一提示 */
  } finally {
    injectingSelected.value = false
  }
}

function openDiagnosis(faultEventId: number | null) {
  if (faultEventId) router.push({ name: 'diagnosis', params: { faultId: faultEventId } })
}

// ---------- 状态展示 ----------
const STATUS_LABELS: Record<ExperimentStatus, string> = {
  created: '已创建',
  injecting: '注入中',
  injected: '已注入',
  finished: '已完成',
  cancelled: '已取消',
}

type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary'

function statusTag(s: string): TagType {
  switch (s) {
    case 'finished':
      return 'success'
    case 'injected':
      return 'primary'
    case 'injecting':
      return 'warning'
    default:
      return 'info'
  }
}

function typeLabel(t: string): string {
  return FAULT_TYPE_LABELS[t] ?? t
}

// ---------- compare 双窗口曲线（事件 resolved 后可用） ----------
const compare = ref<ExperimentCompare | null>(null)
const compareLoading = ref(false)
let lastCompareKey = ''

// auto_recovered=true 蕴含事件已 resolved（评估口径），此时拉 compare 不会 409
const compareReady = computed(() => selected.value?.result?.auto_recovered === true)

watch(
  [selected, compareReady],
  async () => {
    const sel = selected.value
    if (!sel || !compareReady.value || !sel.fault_event_id) {
      compare.value = null
      lastCompareKey = ''
      return
    }
    const key = `${sel.id}:${sel.fault_event_id}`
    if (key === lastCompareKey || compareLoading.value) return
    compareLoading.value = true
    try {
      compare.value = await getExperimentCompare(sel.id)
      lastCompareKey = key
    } catch {
      compare.value = null
      lastCompareKey = ''
    } finally {
      compareLoading.value = false
    }
  },
  { immediate: true },
)

function formatValue(key: CompareMetricKey, v: number): string {
  switch (key) {
    case 'error_rate':
      return formatPercent(v)
    case 'p95_latency':
      return `${v.toFixed(3)}s`
    case 'cpu_usage':
      return `${v.toFixed(3)} 核`
    case 'memory_usage':
      return formatBytes(v)
  }
}

function compareOption(metric: CompareMetric): EChartsOption {
  const style = (color: string, area: boolean) => ({
    type: 'line' as const,
    showSymbol: false,
    connectNulls: true,
    lineStyle: { color },
    itemStyle: { color },
    ...(area ? { areaStyle: { color: 'rgba(245, 108, 108, 0.08)' } } : {}),
  })
  return {
    title: {
      text: `${metric.name}（${metric.unit}）`,
      textStyle: { fontSize: 13, color: '#606266' },
      left: 'center',
    },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => (typeof v === 'number' ? formatValue(metric.key, v) : '-'),
    },
    legend: { bottom: 0, data: ['故障窗', '恢复窗'] },
    grid: { left: 8, right: 16, top: 40, bottom: 44, containLabel: true },
    xAxis: {
      type: 'time',
      axisLabel: { formatter: (v: number) => new Date(v).toLocaleTimeString('zh-CN', { hour12: false }) },
    },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => formatValue(metric.key, v) } },
    series: [
      {
        name: '故障窗',
        data: metric.fault.points.map((p) => [p.ts, p.value]),
        ...style('#f56c6c', true),
      },
      {
        name: '恢复窗',
        data: metric.recovery.points.map((p) => [p.ts, p.value]),
        ...style('#67c23a', false),
      },
    ],
  }
}
</script>

<template>
  <div>
    <el-alert
      class="mb16"
      type="warning"
      :closable="false"
      show-icon
      title="故障实验室会向 demo namespace 注入真实故障并跟踪系统自愈表现；同一时刻只允许一个进行中实验；事件待人工确认时请到诊断页批准修复方案"
    />
    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>创建故障注入实验</template>
          <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
            <el-form-item label="故障类型" prop="fault_type">
              <el-select v-model="form.fault_type" style="width: 100%">
                <el-option
                  v-for="t in SUPPORTED"
                  :key="t"
                  :label="typeLabel(t)"
                  :value="t"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="目标工作负载" prop="target_workload">
              <el-input v-model="form.target_workload" placeholder="如 payment-service" />
            </el-form-item>
            <el-form-item v-if="form.fault_type === 'oom'" label="memory limit" prop="memory_limit">
              <el-input v-model="form.memory_limit" placeholder="如 128Mi" />
              <div class="field-hint">调低目标 Deployment 的 memory limit 触发 OOM，实验结束自动还原</div>
            </el-form-item>
            <el-form-item v-else-if="form.fault_type === 'pod_crash'" label="注入方式">
              <div class="field-hint">循环打崩目标 Pod 触发 CrashLoopBackOff（告警自动触发诊断）</div>
            </el-form-item>
            <el-form-item v-else label="注入方式">
              <div class="field-hint">创建独立 stress-ng 压力 Pod（目标工作负载仅作记录），15 分钟后自止</div>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="creating" @click="handleCreateAndInject">
                创建并注入
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>实验列表</span>
              <span class="refresh-hint">20s 自动刷新 · 共 {{ total }} 个</span>
            </div>
          </template>
          <el-table v-loading="!items.length" :data="items" stripe @row-click="selectRow">
            <el-table-column prop="id" label="ID" width="60" align="center" />
            <el-table-column label="类型" width="130">
              <template #default="{ row }">{{ typeLabel(row.fault_type) }}</template>
            </el-table-column>
            <el-table-column label="目标" min-width="150" class-name="mono">
              <template #default="{ row }">{{ row.target_ns }}/{{ row.target_workload }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="statusTag(row.status)" size="small">{{ STATUS_LABELS[row.status as ExperimentStatus] ?? row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="注入时间" width="160">
              <template #default="{ row }">{{ formatTime(row.injected_at) }}</template>
            </el-table-column>
            <el-table-column label="评估结果" min-width="170">
              <template #default="{ row }">
                <template v-if="row.result">
                  <el-tag :type="row.result.detected ? 'success' : 'danger'" size="small" class="mr4">
                    {{ row.result.detected ? '检出' : '未检出' }}
                  </el-tag>
                  <el-tag
                    v-if="row.result.auto_recovered !== null"
                    :type="row.result.auto_recovered ? 'success' : 'danger'"
                    size="small"
                    class="mr4"
                  >
                    {{ row.result.auto_recovered ? '已恢复' : '未恢复' }}
                  </el-tag>
                  <span v-if="row.result.mttr_s != null">{{ formatDuration(row.result.mttr_s) }}</span>
                </template>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column label="关联事件" width="100" align="center">
              <template #default="{ row }">
                <el-link v-if="row.fault_event_id" type="primary" @click.stop="openDiagnosis(row.fault_event_id)">
                  #{{ row.fault_event_id }}
                </el-link>
                <span v-else>-</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="selected" shadow="never" class="mt16">
      <template #header>
        <div class="card-header">
          <span>实验 #{{ selected.id }} 报告</span>
          <div>
            <el-button
              v-if="selected.status === 'created'"
              size="small"
              type="warning"
              :loading="injectingSelected"
              @click="handleInjectSelected"
            >
              注入故障
            </el-button>
            <el-button
              v-if="selected.fault_event_id"
              size="small"
              type="primary"
              plain
              @click="openDiagnosis(selected.fault_event_id)"
            >
              查看关联事件 #{{ selected.fault_event_id }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="selected.status === 'created' && selected.last_error"
        class="mb16"
        type="error"
        :closable="false"
        show-icon
        title="最近一次注入失败，可重新注入"
        :description="selected.last_error"
      />

      <el-descriptions :column="4" border>
        <el-descriptions-item label="状态">
          <el-tag :type="statusTag(selected.status)" size="small">{{ STATUS_LABELS[selected.status] ?? selected.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="故障类型">{{ typeLabel(selected.fault_type) }}</el-descriptions-item>
        <el-descriptions-item label="目标">{{ selected.target_ns }}/{{ selected.target_workload }}</el-descriptions-item>
        <el-descriptions-item label="参数">{{ JSON.stringify(selected.params) }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatTime(selected.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="注入时间">{{ formatTime(selected.injected_at) }}</el-descriptions-item>
        <el-descriptions-item label="关联事件">
          <el-link v-if="selected.fault_event_id" type="primary" @click="openDiagnosis(selected.fault_event_id)">
            #{{ selected.fault_event_id }}
          </el-link>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="检测延迟">
          {{ formatDuration(selected.result?.detection_latency_s ?? null) }}
        </el-descriptions-item>
      </el-descriptions>

      <template v-if="selected.result">
        <el-descriptions :column="6" border class="mt16">
          <el-descriptions-item label="故障被检测">
            <el-tag :type="selected.result.detected ? 'success' : 'danger'" size="small">
              {{ selected.result.detected ? '是' : '否' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="根因判定正确">
            <el-tag
              v-if="selected.result.diagnosed_correctly !== null"
              :type="selected.result.diagnosed_correctly ? 'success' : 'danger'"
              size="small"
            >
              {{ selected.result.diagnosed_correctly ? '是' : '否' }}
            </el-tag>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="自动恢复">
            <el-tag
              v-if="selected.result.auto_recovered !== null"
              :type="selected.result.auto_recovered ? 'success' : 'danger'"
              size="small"
            >
              {{ selected.result.auto_recovered ? '是' : '否' }}
            </el-tag>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="MTTR">{{ formatDuration(selected.result.mttr_s) }}</el-descriptions-item>
          <el-descriptions-item label="误操作">
            <el-tag :type="selected.result.false_action ? 'danger' : 'success'" size="small">
              {{ selected.result.false_action ? '有' : '无' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="备注">{{ selected.result.notes || '-' }}</el-descriptions-item>
        </el-descriptions>
      </template>
      <el-alert
        v-else-if="selected.status !== 'created'"
        class="mt16"
        type="info"
        :closable="false"
        show-icon
        title="实验尚未闭环：注入后系统监听关联告警事件（10 分钟窗口），事件闭环（或超时）后自动生成评估结果"
      />

      <template v-if="compareReady">
        <el-divider content-position="left">修复前后指标对比（故障窗 vs 恢复窗）</el-divider>
        <div v-loading="compareLoading && !compare">
          <el-row v-if="compare" :gutter="16">
            <el-col v-for="m in compare.metrics" :key="m.key" :span="12">
              <EChart :option="compareOption(m)" height="260px" class="compare-chart" />
            </el-col>
          </el-row>
          <el-empty v-else description="暂无对比数据" :image-size="60" />
        </div>
      </template>
    </el-card>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.mb16 {
  margin-bottom: 16px;
}

.mt16 {
  margin-top: 16px;
}

.mr4 {
  margin-right: 4px;
}

.field-hint {
  font-size: 12px;
  color: #c0c4cc;
  line-height: 1.5;
}

.refresh-hint {
  font-size: 12px;
  color: #c0c4cc;
}

:deep(.el-table__row) {
  cursor: pointer;
}

.compare-chart {
  margin-bottom: 8px;
}
</style>

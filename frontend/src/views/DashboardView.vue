<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getOverview, getTrends, type ClusterOverview, type Trends } from '@/services/cluster'
import { formatPercent } from '@/utils/format'
import EChart from '@/components/EChart.vue'
import type { EChartsOption } from 'echarts'

const loading = ref(true)
const overview = ref<ClusterOverview | null>(null)
const trends = ref<Trends | null>(null)

let overviewTimer: number | undefined
let trendsTimer: number | undefined

async function refreshOverview() {
  try {
    overview.value = await getOverview()
  } catch {
    /* 轮询失败静默，下一轮重试（token 过期由拦截器统一处理） */
  }
}

async function refreshTrends() {
  try {
    trends.value = await getTrends(30)
  } catch {
    /* 同上 */
  }
}

onMounted(async () => {
  try {
    await Promise.all([refreshOverview(), refreshTrends()])
  } finally {
    loading.value = false
  }
  overviewTimer = window.setInterval(refreshOverview, 15000)
  trendsTimer = window.setInterval(refreshTrends, 30000)
})

onBeforeUnmount(() => {
  window.clearInterval(overviewTimer)
  window.clearInterval(trendsTimer)
})

const cards = computed(() => {
  const o = overview.value
  return [
    { label: '节点', value: o ? `${o.nodes.ready}/${o.nodes.total}` : '-', desc: '就绪/总数' },
    {
      label: 'Pod',
      value: o ? `${o.pods.running}/${o.pods.total}` : '-',
      desc: `Pending ${o?.pods.pending ?? '-'} · Failed ${o?.pods.failed ?? '-'}`,
    },
    {
      label: 'Deployment',
      value: o ? `${o.deployments.available}/${o.deployments.total}` : '-',
      desc: '可用/总数',
    },
    { label: '活跃故障', value: o ? o.active_faults : '-', desc: '进行中的故障事件' },
  ]
})

function ratioOption(name: string, ratio: number): EChartsOption {
  return {
    series: [
      {
        type: 'gauge',
        startAngle: 90,
        endAngle: -270,
        radius: '95%',
        pointer: { show: false },
        progress: { show: true, width: 14, itemStyle: { color: '#1677ff' } },
        axisLine: { lineStyle: { width: 14, color: [[1, '#e4e7ed']] } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        title: { fontSize: 13, color: '#909399', offsetCenter: [0, '35%'] },
        detail: {
          fontSize: 22,
          offsetCenter: [0, 0],
          formatter: () => formatPercent(ratio),
        },
        data: [{ value: Math.round(ratio * 1000) / 10, name }],
      },
    ],
  }
}

const cpuOption = computed<EChartsOption>(() =>
  ratioOption('CPU 使用率', overview.value?.resources.cpu_usage_ratio ?? 0),
)
const memoryOption = computed<EChartsOption>(() =>
  ratioOption('内存使用率', overview.value?.resources.memory_usage_ratio ?? 0),
)

const traffic = computed(() => [
  { label: 'QPS', value: overview.value?.qps != null ? overview.value.qps.toFixed(1) : '-' },
  { label: '错误率', value: formatPercent(overview.value?.error_rate, 2) },
  {
    label: 'P95 延迟',
    value: overview.value?.p95_latency != null ? `${overview.value.p95_latency.toFixed(2)}s` : '-',
  },
])

// 趋势折线（design.md §3.2.1）：QPS/P95 左轴，错误率百分比右轴
const trendOption = computed<EChartsOption>(() => {
  const points = trends.value?.points ?? []
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['QPS', '错误率', 'P95 延迟'], top: 0 },
    grid: { left: 48, right: 48, top: 32, bottom: 28 },
    xAxis: {
      type: 'category',
      data: points.map((p) => p.ts.slice(11, 16)),
      boundaryGap: false,
    },
    yAxis: [
      { type: 'value', name: 'req/s / s', min: 0 },
      { type: 'value', name: '%', min: 0, axisLabel: { formatter: '{value}%' } },
    ],
    series: [
      {
        name: 'QPS',
        type: 'line',
        showSymbol: false,
        connectNulls: true,
        data: points.map((p) => p.qps),
      },
      {
        name: '错误率',
        type: 'line',
        yAxisIndex: 1,
        showSymbol: false,
        connectNulls: true,
        data: points.map((p) => (p.error_rate == null ? null : Math.round(p.error_rate * 10000) / 100)),
      },
      {
        name: 'P95 延迟',
        type: 'line',
        showSymbol: false,
        connectNulls: true,
        lineStyle: { type: 'dashed' },
        data: points.map((p) => p.p95_latency),
      },
    ],
  }
})
</script>

<template>
  <div v-loading="loading">
    <el-row :gutter="16">
      <el-col v-for="card in cards" :key="card.label" :span="6">
        <el-card shadow="hover">
          <div class="stat-label">{{ card.label }}</div>
          <div class="stat-value">{{ card.value }}</div>
          <div class="stat-desc">{{ card.desc }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mt16">
      <el-col :span="8">
        <el-card shadow="hover">
          <template #header>资源水位</template>
          <EChart :option="cpuOption" height="240px" />
          <EChart :option="memoryOption" height="240px" />
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="hover">
          <template #header>
            <div class="card-header">
              <span>服务指标趋势（近 30 分钟）</span>
              <span class="hint">15s/30s 自动刷新</span>
            </div>
          </template>
          <el-descriptions :column="3" border>
            <el-descriptions-item v-for="t in traffic" :key="t.label" :label="t.label">
              {{ t.value }}
            </el-descriptions-item>
          </el-descriptions>
          <EChart class="mt16" :option="trendOption" height="280px" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.stat-label {
  font-size: 13px;
  color: #909399;
}

.stat-value {
  margin: 8px 0 4px;
  font-size: 28px;
  font-weight: 600;
  color: #303133;
}

.stat-desc {
  font-size: 12px;
  color: #c0c4cc;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.hint {
  font-size: 12px;
  color: #c0c4cc;
}

.mt16 {
  margin-top: 16px;
}
</style>

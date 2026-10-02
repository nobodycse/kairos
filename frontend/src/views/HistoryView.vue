<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { EChartsOption } from 'echarts'
import { getReportSummary, type FaultTypeStat, type ReportSummary } from '@/services/reports'
import { FAULT_TYPE_LABELS, formatDuration, formatPercent } from '@/utils/format'
import EChart from '@/components/EChart.vue'

// Phase 3：summary 接真数据（experiment_results 实时聚合）；比率为 null（无数据）显示 '-'
const loading = ref(true)
const summary = ref<ReportSummary | null>(null)

let timer: number | undefined

async function refresh() {
  try {
    summary.value = await getReportSummary()
  } catch {
    /* 轮询失败静默，下一轮重试 */
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, 60000)
})

onBeforeUnmount(() => window.clearInterval(timer))

const overallCards = [
  { key: 'diagnosis_accuracy', label: '诊断准确率', format: (v: number) => formatPercent(v) },
  { key: 'recovery_rate', label: '自动恢复率', format: (v: number) => formatPercent(v) },
  { key: 'avg_mttr_s', label: '平均 MTTR', format: (v: number) => formatDuration(v) },
  { key: 'false_action_rate', label: '误操作率', format: (v: number) => formatPercent(v) },
] as const

// 双轴柱状图：诊断准确率/自动恢复率（%，左轴）+ MTTR（秒，右轴）
const chartOption = computed<EChartsOption>(() => {
  const stats: FaultTypeStat[] = summary.value?.by_fault_type ?? []
  const types = stats.map((s) => FAULT_TYPE_LABELS[s.fault_type] ?? s.fault_type)
  const pct = (v: number | null) => (v === null ? 0 : Number((v * 100).toFixed(1)))
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0 },
    grid: { left: 8, right: 8, top: 36, bottom: 8, containLabel: true },
    xAxis: { type: 'category', data: types },
    yAxis: [
      { type: 'value', name: '%', max: 100, axisLabel: { formatter: '{value}%' } },
      { type: 'value', name: '秒', axisLabel: { formatter: '{value}s' } },
    ],
    series: [
      {
        name: '诊断准确率',
        type: 'bar',
        barMaxWidth: 36,
        itemStyle: { color: '#409eff' },
        data: stats.map((s) => pct(s.diagnosis_accuracy)),
      },
      {
        name: '自动恢复率',
        type: 'bar',
        barMaxWidth: 36,
        itemStyle: { color: '#67c23a' },
        data: stats.map((s) => pct(s.recovery_rate)),
      },
      {
        name: '平均 MTTR',
        type: 'bar',
        yAxisIndex: 1,
        barMaxWidth: 36,
        itemStyle: { color: '#e6a23c' },
        data: stats.map((s) => s.avg_mttr_s ?? 0),
      },
    ],
  }
})
</script>

<template>
  <div v-loading="loading">
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-label">实验总数</div>
          <div class="stat-value">{{ summary?.total_experiments ?? '-' }}</div>
          <div class="stat-desc">历史累计注入实验</div>
        </el-card>
      </el-col>
      <el-col v-for="c in overallCards" :key="c.key" :span="4">
        <el-card shadow="hover">
          <div class="stat-label">{{ c.label }}</div>
          <div class="stat-value">{{ summary ? c.format(summary.overall[c.key]) : '-' }}</div>
          <div class="stat-desc">整体</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt16">
      <template #header>按故障类型统计</template>
      <el-table :data="summary?.by_fault_type ?? []" stripe>
        <el-table-column label="故障类型" min-width="180">
          <template #default="{ row }">{{ FAULT_TYPE_LABELS[row.fault_type] ?? row.fault_type }}</template>
        </el-table-column>
        <el-table-column prop="runs" label="实验次数" width="100" align="center" />
        <el-table-column label="诊断准确率" width="140" align="center">
          <template #default="{ row }">{{ formatPercent(row.diagnosis_accuracy) }}</template>
        </el-table-column>
        <el-table-column label="自动恢复率" width="140" align="center">
          <template #default="{ row }">{{ formatPercent(row.recovery_rate) }}</template>
        </el-table-column>
        <el-table-column label="平均 MTTR" width="120" align="center">
          <template #default="{ row }">{{ formatDuration(row.avg_mttr_s) }}</template>
        </el-table-column>
        <el-table-column label="误操作率" width="120" align="center">
          <template #default="{ row }">{{ formatPercent(row.false_action_rate) }}</template>
        </el-table-column>
      </el-table>
      <el-empty
        v-if="summary && !summary.by_fault_type.length"
        description="暂无实验数据：到「故障实验室」创建并注入第一个实验"
        :image-size="80"
      />
    </el-card>

    <el-card shadow="never" class="mt16">
      <template #header>分类指标对比（诊断准确率 / 自动恢复率 / 平均 MTTR）</template>
      <EChart v-if="summary?.by_fault_type?.length" :option="chartOption" height="320px" />
      <el-empty v-else description="实验闭环后此处展示分类柱状图" :image-size="80" />
    </el-card>
  </div>
</template>

<style scoped>
.stat-label {
  font-size: 13px;
  color: #909399;
}

.stat-value {
  margin: 8px 0 4px;
  font-size: 26px;
  font-weight: 600;
  color: #303133;
}

.stat-desc {
  font-size: 12px;
  color: #c0c4cc;
}

.mt16 {
  margin-top: 16px;
}
</style>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getReportSummary, type ReportSummary } from '@/services/reports'
import { FAULT_TYPE_LABELS, formatDuration, formatPercent } from '@/utils/format'

const loading = ref(true)
const summary = ref<ReportSummary | null>(null)

onMounted(async () => {
  try {
    summary.value = await getReportSummary()
  } finally {
    loading.value = false
  }
})

const overallCards = [
  { key: 'diagnosis_accuracy', label: '诊断准确率', format: (v: number) => formatPercent(v) },
  { key: 'recovery_rate', label: '自动恢复率', format: (v: number) => formatPercent(v) },
  { key: 'avg_mttr_s', label: '平均 MTTR', format: (v: number) => formatDuration(v) },
  { key: 'false_action_rate', label: '误操作率', format: (v: number) => formatPercent(v) },
] as const
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
      <el-alert
        class="mt16"
        type="info"
        :closable="false"
        show-icon
        title="修复前后指标对比图表在 Phase 3 补充"
      />
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

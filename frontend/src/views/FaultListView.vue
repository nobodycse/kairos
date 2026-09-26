<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  listFaults,
  type FaultListItem,
  type FaultStatus,
} from '@/services/faults'
import { FAULT_STATUS_LABELS, faultStatusTag, formatDuration, formatTime, severityTag } from '@/utils/format'

// 故障事件列表（design.md §3.5）：默认展示活跃事件，30s 自动刷新
const router = useRouter()

const STATUS_OPTIONS: { value: FaultStatus | 'active' | 'all'; label: string }[] = [
  { value: 'active', label: '活跃事件' },
  { value: 'all', label: '全部' },
  ...Object.entries(FAULT_STATUS_LABELS).map(([value, label]) => ({
    value: value as FaultStatus,
    label,
  })),
]

const loading = ref(true)
const items = ref<FaultListItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const status = ref<FaultStatus | 'active' | 'all'>('active')
const namespace = ref('')

let timer: number | undefined

async function fetchList() {
  loading.value = true
  try {
    const resp = await listFaults({
      status: status.value === 'all' ? undefined : status.value,
      namespace: namespace.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = resp.items
    total.value = resp.total
  } catch {
    /* 轮询失败静默，下一轮重试 */
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchList()
  timer = window.setInterval(fetchList, 30000)
})

onBeforeUnmount(() => window.clearInterval(timer))

watch([page, pageSize], fetchList)
watch([status, namespace], () => {
  page.value = 1
  fetchList()
})

function openDetail(row: FaultListItem) {
  router.push({ name: 'diagnosis', params: { faultId: row.id } })
}

function statusLabel(s: string): string {
  return FAULT_STATUS_LABELS[s as FaultStatus] ?? s
}
</script>

<template>
  <div>
    <el-card shadow="never">
      <div class="toolbar">
        <el-select v-model="status" style="width: 160px">
          <el-option v-for="opt in STATUS_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
        </el-select>
        <el-select
          v-model="namespace"
          placeholder="全部 namespace"
          clearable
          style="width: 180px"
        >
          <el-option label="demo" value="demo" />
          <el-option label="kube-system" value="kube-system" />
        </el-select>
        <span class="refresh-hint">30s 自动刷新</span>
      </div>

      <el-table v-loading="loading" :data="items" stripe @row-click="openDetail">
        <el-table-column prop="id" label="ID" width="70" align="center" />
        <el-table-column prop="alert_name" label="告警" min-width="170" class-name="mono" />
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag :type="severityTag(row.severity)" size="small">
              {{ row.severity === 'critical' ? '严重' : '警告' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="namespace" label="Namespace" width="110" />
        <el-table-column label="Workload" width="150">
          <template #default="{ row }">{{ row.workload ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="faultStatusTag(row.status)" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="检测时间" width="170">
          <template #default="{ row }">{{ formatTime(row.detected_at) }}</template>
        </el-table-column>
        <el-table-column label="恢复时间" width="170">
          <template #default="{ row }">{{ row.resolved_at ? formatTime(row.resolved_at) : '-' }}</template>
        </el-table-column>
        <el-table-column label="MTTR" width="90" align="center">
          <template #default="{ row }">
            {{ row.mttr_seconds != null ? formatDuration(row.mttr_seconds) : '-' }}
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        class="pagination"
        background
        layout="total, sizes, prev, pager, next"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
      />
    </el-card>
  </div>
</template>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  gap: 12px;
  align-items: center;
}

.refresh-hint {
  margin-left: auto;
  font-size: 12px;
  color: #c0c4cc;
}

.pagination {
  margin-top: 16px;
  justify-content: flex-end;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { listPods, type Pod } from '@/services/cluster'
import { formatAge, formatBytes } from '@/utils/format'

// Phase 0 壳：Pods 一张表打通资源列表链路；Nodes/Deployments/Events 同构表格 Phase 1 补齐
const loading = ref(true)
const items = ref<Pod[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const namespace = ref('')

async function fetchList() {
  loading.value = true
  try {
    const resp = await listPods({
      page: page.value,
      page_size: pageSize.value,
      namespace: namespace.value || undefined,
    })
    items.value = resp.items
    total.value = resp.total
  } finally {
    loading.value = false
  }
}

onMounted(fetchList)
watch([page, pageSize, namespace], fetchList)
</script>

<template>
  <div>
    <el-alert
      class="mb16"
      type="info"
      :closable="false"
      show-icon
      title="Phase 0 提供 Pods 列表演示；Nodes / Deployments / Events 页签在 Phase 1 补齐"
    />
    <el-card shadow="never">
      <div class="toolbar">
        <el-select
          v-model="namespace"
          placeholder="全部 namespace"
          clearable
          style="width: 200px"
        >
          <el-option label="demo" value="demo" />
          <el-option label="kube-system" value="kube-system" />
        </el-select>
      </div>

      <el-table v-loading="loading" :data="items" stripe>
        <el-table-column prop="namespace" label="Namespace" width="120" />
        <el-table-column prop="name" label="Pod" min-width="240" show-overflow-tooltip class-name="mono" />
        <el-table-column prop="workload" label="Workload" width="150" />
        <el-table-column prop="node" label="节点" width="140" />
        <el-table-column prop="status" label="状态" width="150">
          <template #default="{ row }">
            <el-tag :type="row.status === 'Running' ? 'success' : row.status === 'Pending' ? 'warning' : 'danger'">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Ready" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.ready ? 'success' : 'danger'" size="small">
              {{ row.ready ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="restarts" label="重启" width="80" align="center" />
        <el-table-column label="运行时长" width="100" align="center">
          <template #default="{ row }">{{ formatAge(row.age_seconds) }}</template>
        </el-table-column>
        <el-table-column label="CPU" width="90" align="right">
          <template #default="{ row }">{{ row.cpu_usage_cores.toFixed(2) }} 核</template>
        </el-table-column>
        <el-table-column label="内存" align="right">
          <template #default="{ row }">
            {{ formatBytes(row.memory_usage_bytes) }} / {{ formatBytes(row.memory_limit_bytes) }}
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
}

.pagination {
  margin-top: 16px;
  justify-content: flex-end;
}

.mb16 {
  margin-bottom: 16px;
}
</style>

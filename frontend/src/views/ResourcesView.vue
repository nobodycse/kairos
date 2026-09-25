<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import {
  listClusterEvents,
  listDeployments,
  listNodes,
  listPods,
  type ClusterEvent,
  type Deployment,
  type NodeInfo,
  type Pod,
} from '@/services/cluster'
import { formatAge, formatBytes, formatPercent, formatTime } from '@/utils/format'

type TabName = 'pods' | 'nodes' | 'deployments' | 'events'

const activeTab = ref<TabName>('pods')

// 每个页签独立的加载/分页/数据状态（后端接口分页结构同构，design.md §3.3/§3.4）
const loading = ref(true)
const pods = ref<Pod[]>([])
const nodes = ref<NodeInfo[]>([])
const deployments = ref<Deployment[]>([])
const events = ref<ClusterEvent[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const namespace = ref('')

const NAMESPACE_OPTIONS = ['demo', 'kube-system']

async function fetchList() {
  loading.value = true
  const params = { page: page.value, page_size: pageSize.value }
  try {
    if (activeTab.value === 'pods') {
      const resp = await listPods({ ...params, namespace: namespace.value || undefined })
      pods.value = resp.items
      total.value = resp.total
    } else if (activeTab.value === 'nodes') {
      const resp = await listNodes(params)
      nodes.value = resp.items
      total.value = resp.total
    } else if (activeTab.value === 'deployments') {
      const resp = await listDeployments({ ...params, namespace: namespace.value || undefined })
      deployments.value = resp.items
      total.value = resp.total
    } else {
      const resp = await listClusterEvents(params)
      events.value = resp.items
      total.value = resp.total
    }
  } finally {
    loading.value = false
  }
}

onMounted(fetchList)
watch([page, pageSize, activeTab], fetchList)
watch(namespace, () => {
  page.value = 1
  fetchList()
})
</script>

<template>
  <div>
    <el-card shadow="never">
      <div class="toolbar">
        <el-select
          v-model="namespace"
          placeholder="全部 namespace"
          clearable
          style="width: 200px"
          :disabled="activeTab === 'nodes' || activeTab === 'events'"
        >
          <el-option v-for="ns in NAMESPACE_OPTIONS" :key="ns" :label="ns" :value="ns" />
        </el-select>
      </div>

      <el-tabs v-model="activeTab">
        <!-- ---------- Pods ---------- -->
        <el-tab-pane label="Pods" name="pods">
          <el-table v-loading="loading" :data="pods" stripe>
            <el-table-column prop="namespace" label="Namespace" width="120" />
            <el-table-column prop="name" label="Pod" min-width="240" show-overflow-tooltip class-name="mono" />
            <el-table-column label="Workload" width="150">
              <template #default="{ row }">{{ row.workload ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="节点" width="140">
              <template #default="{ row }">{{ row.node ?? '-' }}</template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="160">
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
            <el-table-column label="CPU" width="100" align="right">
              <template #default="{ row }">
                {{ row.cpu_usage_cores != null ? `${row.cpu_usage_cores.toFixed(2)} 核` : '-' }}
              </template>
            </el-table-column>
            <el-table-column label="内存" align="right">
              <template #default="{ row }">
                {{ formatBytes(row.memory_usage_bytes) }} / {{ formatBytes(row.memory_limit_bytes) }}
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ---------- Nodes ---------- -->
        <el-tab-pane label="Nodes" name="nodes">
          <el-table v-loading="loading" :data="nodes" stripe>
            <el-table-column prop="name" label="节点" min-width="160" class-name="mono" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="row.status === 'Ready' ? 'success' : 'danger'">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="roles" label="角色" width="160" />
            <el-table-column prop="version" label="版本" width="180" />
            <el-table-column label="CPU 配额（核）" width="130" align="right" prop="cpu_alloc_cores" />
            <el-table-column label="内存配额" width="120" align="right">
              <template #default="{ row }">{{ formatBytes(row.memory_alloc_bytes) }}</template>
            </el-table-column>
            <el-table-column label="CPU 使用率" width="110" align="right">
              <template #default="{ row }">{{ formatPercent(row.cpu_usage_ratio) }}</template>
            </el-table-column>
            <el-table-column label="内存使用率" width="110" align="right">
              <template #default="{ row }">{{ formatPercent(row.memory_usage_ratio) }}</template>
            </el-table-column>
            <el-table-column prop="pods_count" label="Pod 数" width="90" align="center" />
          </el-table>
        </el-tab-pane>

        <!-- ---------- Deployments ---------- -->
        <el-tab-pane label="Deployments" name="deployments">
          <el-table v-loading="loading" :data="deployments" stripe>
            <el-table-column prop="namespace" label="Namespace" width="120" />
            <el-table-column prop="name" label="Deployment" min-width="180" class-name="mono" />
            <el-table-column label="副本" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="row.ready_replicas >= row.replicas ? 'success' : 'warning'" size="small">
                  {{ row.ready_replicas }}/{{ row.replicas }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="image" label="镜像" min-width="260" show-overflow-tooltip class-name="mono" />
            <el-table-column label="CPU limit" width="110" align="center">
              <template #default="{ row }">{{ row.cpu_limit ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="内存 limit" width="120" align="center">
              <template #default="{ row }">{{ row.memory_limit ?? '-' }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ---------- Events ---------- -->
        <el-tab-pane label="Events" name="events">
          <el-table v-loading="loading" :data="events" stripe>
            <el-table-column prop="namespace" label="Namespace" width="120" />
            <el-table-column label="级别" width="90">
              <template #default="{ row }">
                <el-tag :type="row.type === 'Warning' ? 'warning' : 'info'" size="small">{{ row.type }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" width="160" />
            <el-table-column prop="object" label="对象" min-width="220" show-overflow-tooltip class-name="mono" />
            <el-table-column prop="message" label="消息" min-width="260" show-overflow-tooltip />
            <el-table-column prop="count" label="次数" width="80" align="center" />
            <el-table-column label="最近时间" width="170">
              <template #default="{ row }">{{ row.last_seen ? formatTime(row.last_seen) : '-' }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>

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

<script setup lang="ts">
import { reactive, ref } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import {
  createExperiment,
  injectExperiment,
  getExperimentReport,
  type Experiment,
  type ExperimentFaultType,
} from '@/services/experiments'
import { FAULT_TYPE_LABELS, formatDuration, formatTime } from '@/utils/format'

// 流程：创建（201）→ 注入（202）→ 轮询/手动查报告（闭环前 result 为 null）
const formRef = ref<FormInstance>()
const creating = ref(false)
const current = ref<Experiment | null>(null)

const form = reactive({
  fault_type: 'oom' as ExperimentFaultType,
  target_workload: 'payment-service',
  memory_limit: '128Mi',
})

const rules: FormRules = {
  target_workload: [{ required: true, message: '请输入目标工作负载名', trigger: 'blur' }],
  memory_limit: [{ required: true, message: '请输入 memory limit', trigger: 'blur' }],
}

async function handleCreate() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  creating.value = true
  try {
    current.value = await createExperiment({
      fault_type: form.fault_type,
      target_workload: form.target_workload,
      params: form.fault_type === 'oom' ? { memory_limit: form.memory_limit } : {},
    })
  } finally {
    creating.value = false
  }
}

const injecting = ref(false)

async function handleInject() {
  if (!current.value) return
  injecting.value = true
  try {
    const resp = await injectExperiment(current.value.id)
    current.value = { ...current.value, status: resp.status as Experiment['status'] }
  } finally {
    injecting.value = false
  }
}

const querying = ref(false)

async function handleQueryReport() {
  if (!current.value) return
  querying.value = true
  try {
    current.value = await getExperimentReport(current.value.id)
  } finally {
    querying.value = false
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
      title="故障实验室会向被监控集群注入真实故障，Phase 0 仅演示创建/注入/查报告链路（stub 数据），不影响真实集群"
    />
    <el-row :gutter="16">
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>创建故障注入实验</template>
          <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
            <el-form-item label="故障类型" prop="fault_type">
              <el-select v-model="form.fault_type" style="width: 100%">
                <el-option
                  v-for="(label, value) in FAULT_TYPE_LABELS"
                  :key="value"
                  :label="label"
                  :value="value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="目标工作负载" prop="target_workload">
              <el-input v-model="form.target_workload" placeholder="如 payment-service" />
            </el-form-item>
            <el-form-item v-if="form.fault_type === 'oom'" label="memory limit" prop="memory_limit">
              <el-input v-model="form.memory_limit" placeholder="如 128Mi" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="creating" @click="handleCreate">创建实验</el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="14">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>实验详情</span>
              <div v-if="current">
                <el-button
                  size="small"
                  type="warning"
                  :loading="injecting"
                  :disabled="current.status !== 'created'"
                  @click="handleInject"
                >
                  注入故障
                </el-button>
                <el-button size="small" :loading="querying" @click="handleQueryReport">查询报告</el-button>
              </div>
            </div>
          </template>

          <el-empty v-if="!current" description="尚未创建实验" />
          <template v-else>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="实验 ID">#{{ current.id }}</el-descriptions-item>
              <el-descriptions-item label="状态">
                <el-tag :type="current.status === 'finished' ? 'success' : 'warning'">{{ current.status }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="故障类型">{{ FAULT_TYPE_LABELS[current.fault_type] ?? current.fault_type }}</el-descriptions-item>
              <el-descriptions-item label="目标">{{ current.target_ns }}/{{ current.target_workload }}</el-descriptions-item>
              <el-descriptions-item label="创建时间">{{ formatTime(current.created_at) }}</el-descriptions-item>
              <el-descriptions-item label="注入时间">{{ formatTime(current.injected_at) }}</el-descriptions-item>
            </el-descriptions>

            <template v-if="current.result">
              <el-divider content-position="left">实验报告（关联故障事件 #{{ current.fault_event_id ?? '-' }}）</el-divider>
              <el-descriptions :column="3" border>
                <el-descriptions-item label="故障被检测">
                  <el-tag :type="current.result.detected ? 'success' : 'danger'" size="small">
                    {{ current.result.detected ? '是' : '否' }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="检测延迟">{{ formatDuration(current.result.detection_latency_s) }}</el-descriptions-item>
                <el-descriptions-item label="根因判定正确">
                  <el-tag :type="current.result.diagnosed_correctly ? 'success' : 'danger'" size="small">
                    {{ current.result.diagnosed_correctly ? '是' : '否' }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="自动恢复">
                  <el-tag :type="current.result.auto_recovered ? 'success' : 'danger'" size="small">
                    {{ current.result.auto_recovered ? '是' : '否' }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="MTTR">{{ formatDuration(current.result.mttr_s) }}</el-descriptions-item>
                <el-descriptions-item label="误操作">
                  <el-tag :type="current.result.false_action ? 'danger' : 'success'" size="small">
                    {{ current.result.false_action ? '有' : '无' }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="备注" :span="3">{{ current.result.notes }}</el-descriptions-item>
              </el-descriptions>
            </template>
            <el-alert
              v-else-if="current.status !== 'created'"
              class="mt16"
              type="info"
              :closable="false"
              show-icon
              title="实验尚未闭环：闭环后（status=finished）报告的 result 才有值"
            />
          </template>
        </el-card>
      </el-col>
    </el-row>
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
</style>

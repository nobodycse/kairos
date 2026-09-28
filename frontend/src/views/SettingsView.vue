<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getLlmConfig, saveLlmConfig, testLlmConfig, type LlmConfigView } from '@/services/settings'

// Phase 2.5：AI 供应商配置（DB 存储，key 服务端只回掩码；api_key 留空 = 保持现有）
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const apiKeyInput = ref('') // 密码框，留空 = 不修改

const form = reactive({
  base_url: 'https://api.deepseek.com/v1',
  model: 'deepseek-chat',
})

const current = ref<LlmConfigView | null>(null)

function applyView(view: LlmConfigView) {
  current.value = view
  form.base_url = view.base_url || 'https://api.deepseek.com/v1'
  form.model = view.model || 'deepseek-chat'
}

function payload() {
  return {
    base_url: form.base_url.trim(),
    model: form.model.trim(),
    api_key: apiKeyInput.value.trim() || undefined,
  }
}

async function load() {
  loading.value = true
  try {
    applyView(await getLlmConfig())
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (!form.base_url.trim() || !form.model.trim()) {
    ElMessage.warning('base_url 与 model 不能为空')
    return
  }
  saving.value = true
  try {
    applyView(await saveLlmConfig(payload()))
    apiKeyInput.value = ''
    ElMessage.success('AI 配置已保存（写入审计）')
  } finally {
    saving.value = false
  }
}

async function onTest() {
  testing.value = true
  try {
    const result = await testLlmConfig(payload())
    if (result.ok) {
      ElMessage.success(result.message)
    } else {
      ElMessage.error(result.message)
    }
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-card shadow="never" class="setting-card">
      <template #header>
        <div class="card-header">
          <span>AI 供应商配置</span>
          <el-tag
            v-if="current"
            :type="current.configured ? 'success' : 'danger'"
            size="small"
          >
            {{ current.configured ? `已配置（来源：${current.source === 'db' ? '系统设置' : '.env'}）` : '未配置' }}
          </el-tag>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="mb16"
        title="Key 保存在服务端数据库，页面只显示掩码；此处保存后立即生效，无需重启。"
      />

      <el-form label-width="120px" class="form">
        <el-form-item label="API Base URL">
          <el-input v-model="form.base_url" placeholder="https://api.deepseek.com/v1" />
        </el-form-item>
        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="deepseek-chat" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="apiKeyInput"
            type="password"
            show-password
            :placeholder="current?.configured ? `已配置（${current.api_key_masked}），留空 = 不修改` : 'sk-...'"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
          <el-button :loading="testing" @click="onTest">测试连接</el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.setting-card {
  max-width: 720px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.form {
  max-width: 560px;
}

.mb16 {
  margin-bottom: 16px;
}
</style>

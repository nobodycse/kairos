import request from './request'

// ---------- 系统设置模块（Phase 2.5：AI 供应商配置） ----------

export interface LlmConfigView {
  configured: boolean
  base_url: string
  model: string
  api_key_masked: string
  source: 'db' | 'env' | 'none'
}

export interface LlmConfigPayload {
  base_url: string
  model: string
  /** 留空 = 保持已保存的 key（服务端口径） */
  api_key?: string
}

export function getLlmConfig(): Promise<LlmConfigView> {
  return request.get('/settings/llm') as Promise<LlmConfigView>
}

export function saveLlmConfig(payload: LlmConfigPayload): Promise<LlmConfigView> {
  return request.put('/settings/llm', payload) as Promise<LlmConfigView>
}

export function testLlmConfig(payload: LlmConfigPayload): Promise<{ ok: boolean; message: string }> {
  return request.post('/settings/llm/test', payload) as Promise<{ ok: boolean; message: string }>
}

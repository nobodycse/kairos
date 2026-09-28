import { useAuthStore } from '@/stores/auth'
import router from '@/router'

/**
 * SSE 客户端封装。契约见 docs/design.md §4：
 * - GET /api/v1/faults/{id}/stream?token=<JWT>（EventSource 不能设请求头，JWT 走 query）
 * - 4 个命名事件：snapshot / agent_step / status_changed / verification_progress，
 *   必须 addEventListener 按事件名监听，onmessage 收不到（心跳是 ": ping" 注释帧）
 * - 连接/重连服务端先重发 snapshot，handler 需容忍重复 snapshot
 * - 流永不主动结束，离开页面必须 close()
 */

export interface SseSnapshot {
  fault_event_id: number
  status: string
  alert_name: string
  detected_at: string
  recent_steps: AgentStep[]
}

export type AgentPhase = 'collect_evidence' | 'analyze' | 'propose_fix' | 'execute_fix' | 'verify'

/**
 * agent_step 事件（design.md §4.4 + 阶段三扩展）。
 * §4.4 契约定义 tool_start（带 args）/ tool_end（带 duration_ms/evidence_summary）成对；
 * 阶段三在 analyze/propose_fix/execute_fix 相位追加了 note 型步骤
 * （step=rca_ready/plan_ready/executed 等，无 args/duration_ms），字段按宽松可选建模。
 */
export interface AgentStep {
  fault_event_id: number
  iteration: number
  phase: AgentPhase | string
  step: string
  tool?: string
  args?: Record<string, unknown>
  duration_ms?: number
  evidence_summary?: string
  /** note 型步骤的载荷（rca_ready 带 summary/confidence，plan_ready 带 action/target） */
  summary?: string
  confidence?: number
  action?: string
  target?: string
  at: string
}

export interface SseStatusChanged {
  fault_event_id: number
  from: string
  to: string
  reason: string
  at: string
}

export interface VerificationCheck {
  /** 布尔项直接 true/false，数值项带 value 与 ok */
  value?: number
  ok?: boolean
}

export interface SseVerificationProgress {
  fault_event_id: number
  sample: number
  of: number
  checks: {
    pod_ready: boolean
    no_restarts: boolean
    error_rate: { value: number; ok: boolean }
    p95_latency: { value: number; ok: boolean }
    logs_clean: boolean
  }
  all_passed: boolean
  at: string
}

export interface FaultStreamHandlers {
  onSnapshot?: (data: SseSnapshot) => void
  onAgentStep?: (data: AgentStep) => void
  onStatusChanged?: (data: SseStatusChanged) => void
  onVerificationProgress?: (data: SseVerificationProgress) => void
  onOpen?: () => void
  onError?: () => void
}

export interface FaultStream {
  close: () => void
}

export function connectFaultStream(faultId: number, handlers: FaultStreamHandlers): FaultStream {
  const auth = useAuthStore()
  const url = `/api/v1/faults/${faultId}/stream?token=${encodeURIComponent(auth.token ?? '')}`
  const es = new EventSource(url)

  const parse = <T>(event: MessageEvent): T | null => {
    try {
      return JSON.parse(event.data) as T
    } catch {
      return null
    }
  }

  es.onopen = () => {
    handlers.onOpen?.()
  }

  es.addEventListener('snapshot', (e) => {
    const data = parse<SseSnapshot>(e as MessageEvent)
    if (data) handlers.onSnapshot?.(data)
  })
  es.addEventListener('agent_step', (e) => {
    const data = parse<AgentStep>(e as MessageEvent)
    if (data) handlers.onAgentStep?.(data)
  })
  es.addEventListener('status_changed', (e) => {
    const data = parse<SseStatusChanged>(e as MessageEvent)
    if (data) handlers.onStatusChanged?.(data)
  })
  es.addEventListener('verification_progress', (e) => {
    const data = parse<SseVerificationProgress>(e as MessageEvent)
    if (data) handlers.onVerificationProgress?.(data)
  })

  // EventSource 读不到 HTTP 状态码；token 过期/无效时同样触发 onerror，
  // 本地凭据已失效就直接断开跳登录，否则交给浏览器内置自动重连（服务端会重发 snapshot）
  es.onerror = () => {
    if (auth.isExpired) {
      es.close()
      handlers.onError?.()
      auth.logout()
      router.push({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
    }
  }

  return {
    close: () => es.close(),
  }
}

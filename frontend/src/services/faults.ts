import request from './request'
import type { Page, PageQuery } from './cluster'

// ---------- faults / remediations 模块（design.md §3.6–§3.8） ----------

/** fault_event.status 九态（design.md §1.2），active = 前六态合集 */
export type FaultStatus =
  | 'detected'
  | 'diagnosing'
  | 'awaiting_approval'
  | 'remediating'
  | 'verifying'
  | 'rolling_back'
  | 'resolved'
  | 'failed'
  | 'closed'

export type Severity = 'warning' | 'critical'
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'
export type RemediationPolicy = 'auto' | 'require_approval' | 'forbidden'
export type RemediationStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'executing'
  | 'succeeded'
  | 'failed'
  | 'rolled_back'

export interface FaultListItem {
  id: number
  alert_name: string
  severity: Severity
  namespace: string
  workload: string
  status: FaultStatus
  detected_at: string
  resolved_at: string | null
  mttr_seconds: number | null
}

export interface Evidence {
  source: 'k8s_api' | 'prometheus' | 'loki' | 'events'
  tool: string
  summary: string
  data: Record<string, unknown>
}

export interface Diagnosis {
  id: number
  /** 大写自由文本（"OOM"/"HighLatency"），非 §1.2 小写枚举 */
  fault_type: string
  root_cause: string
  evidence: Evidence[]
  confidence: number
  blast_radius: string
  suggestion: string
  llm_model: string
  iterations: number
  created_at: string
}

export interface Remediation {
  id: number
  action:
    | 'update_resource_limit'
    | 'scale_deployment'
    | 'restart_deployment'
    | 'rollback_deployment'
  namespace: string
  target: string
  params: Record<string, unknown>
  risk_level: RiskLevel
  policy: RemediationPolicy
  status: RemediationStatus
  approved_by: string | null
  snapshot: Record<string, unknown>
  executed_at: string | null
}

export interface FaultDetail extends FaultListItem {
  fingerprint: string
  labels: Record<string, unknown>
  rediagnose_count: number
  experiment_id: number | null
  /** 诊断中时为 null */
  diagnosis: Diagnosis | null
  remediations: Remediation[]
}

export function listFaults(params?: {
  status?: FaultStatus | 'active'
  namespace?: string
} & PageQuery): Promise<Page<FaultListItem>> {
  return request.get('/faults', { params }) as Promise<Page<FaultListItem>>
}

export function getFault(faultId: number): Promise<FaultDetail> {
  return request.get(`/faults/${faultId}`) as Promise<FaultDetail>
}

/** 202；409 = 该事件正在诊断中 / 该事件已结束 */
export function diagnoseFault(faultId: number): Promise<{ fault_event_id: number; status: string }> {
  return request.post(`/faults/${faultId}/diagnose`) as Promise<{
    fault_event_id: number
    status: string
  }>
}

/** comment 可选，仅记录进审计（design.md §3.8） */
export function approveRemediation(
  remediationId: number,
  comment?: string,
): Promise<{ id: number; status: string; fault_event_status: string }> {
  return request.post(`/remediations/${remediationId}/approve`, comment ? { comment } : {}) as Promise<{
    id: number
    status: string
    fault_event_status: string
  }>
}

export function rejectRemediation(
  remediationId: number,
  comment?: string,
): Promise<{ id: number; status: string; fault_event_status: string }> {
  return request.post(`/remediations/${remediationId}/reject`, comment ? { comment } : {}) as Promise<{
    id: number
    status: string
    fault_event_status: string
  }>
}

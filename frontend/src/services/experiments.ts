import request from './request'

// ---------- experiments 模块（design.md §3.9–§3.11） ----------

export type ExperimentFaultType =
  | 'oom'
  | 'cpu_overload'
  | 'pod_crash'
  | 'image_pull_backoff'
  | 'replica_anomaly'
  | 'network_latency'
  | 'node_not_ready'

export type ExperimentStatus = 'created' | 'injecting' | 'injected' | 'finished' | 'cancelled'

export interface ExperimentResult {
  detected: boolean
  detection_latency_s: number
  diagnosed_correctly: boolean
  auto_recovered: boolean
  mttr_s: number
  false_action: boolean
  notes: string
}

export interface Experiment {
  id: number
  fault_type: ExperimentFaultType
  target_ns: string
  target_workload: string
  params: Record<string, unknown>
  status: ExperimentStatus
  injected_at: string | null
  created_at: string
  /** 最近一次注入失败原因（仅 created 状态且有失败记录时非 null，Phase 3） */
  last_error?: string | null
  /** 未闭环时为 null（§1.1 可空字段显式返回） */
  fault_event_id: number | null
  result: ExperimentResult | null
}

/** 201 创建实验（fault_type 传枚举外值 → 422） */
export function createExperiment(data: {
  fault_type: ExperimentFaultType
  target_workload: string
  params?: Record<string, unknown>
}): Promise<Experiment> {
  return request.post('/experiments', data) as Promise<Experiment>
}

/** 202 执行注入，返回 {"id", "status": "injecting"} */
export function injectExperiment(experimentId: number): Promise<{ id: number; status: string }> {
  return request.post(`/experiments/${experimentId}/inject`) as Promise<{ id: number; status: string }>
}

/** 闭环前 result/fault_event_id 为 null，轮询看 status */
export function getExperimentReport(experimentId: number): Promise<Experiment> {
  return request.get(`/experiments/${experimentId}/report`) as Promise<Experiment>
}

// ---------- Phase 3 新增（design.md §3.11.1/§3.11.2） ----------

/** 实验列表：id 倒序最多 50 条，total 为全部实验数 */
export interface ExperimentListResp {
  items: Experiment[]
  total: number
}

export function listExperiments(): Promise<ExperimentListResp> {
  return request.get('/experiments') as Promise<ExperimentListResp>
}

export interface ComparePoint {
  ts: string
  value: number
}

export interface CompareWindow {
  points: ComparePoint[]
}

export type CompareMetricKey = 'error_rate' | 'p95_latency' | 'cpu_usage' | 'memory_usage'

export interface CompareMetric {
  key: CompareMetricKey
  name: string
  unit: string
  fault: CompareWindow
  recovery: CompareWindow
}

/** 修复前后指标对比：事件 resolved 后可用（未恢复 409） */
export interface ExperimentCompare {
  experiment_id: number
  fault_event_id: number
  fault_window: { start: string; end: string }
  recovery_window: { start: string; end: string }
  metrics: CompareMetric[]
}

export function getExperimentCompare(experimentId: number): Promise<ExperimentCompare> {
  return request.get(`/experiments/${experimentId}/compare`) as Promise<ExperimentCompare>
}

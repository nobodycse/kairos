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

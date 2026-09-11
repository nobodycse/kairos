import request from './request'

// ---------- reports 模块（design.md §3.12） ----------

export interface FaultTypeStat {
  fault_type: string
  runs: number
  diagnosis_accuracy: number
  recovery_rate: number
  avg_mttr_s: number
  false_action_rate: number
}

export interface ReportSummary {
  total_experiments: number
  overall: {
    diagnosis_accuracy: number
    recovery_rate: number
    avg_mttr_s: number
    false_action_rate: number
  }
  by_fault_type: FaultTypeStat[]
}

/** GET /api/v1/reports/summary */
export function getReportSummary(): Promise<ReportSummary> {
  return request.get('/reports/summary') as Promise<ReportSummary>
}

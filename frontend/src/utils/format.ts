import type { FaultStatus, RemediationStatus, RiskLevel, Severity } from '@/services/faults'

/** 九态中文标签（design.md §1.2） */
export const FAULT_STATUS_LABELS: Record<FaultStatus, string> = {
  detected: '已检测',
  diagnosing: '诊断中',
  awaiting_approval: '待人工确认',
  remediating: '修复执行中',
  verifying: '验证观察中',
  rolling_back: '回滚中',
  resolved: '已恢复',
  failed: '失败',
  closed: '已关闭',
}

export const REMEDIATION_STATUS_LABELS: Record<RemediationStatus, string> = {
  pending: '待确认',
  approved: '已批准',
  rejected: '已拒绝',
  executing: '执行中',
  succeeded: '已成功',
  failed: '失败',
  rolled_back: '已回滚',
}

export const PHASE_LABELS: Record<string, string> = {
  collect_evidence: '采集证据',
  analyze: '分析',
  propose_fix: '生成方案',
  execute_fix: '执行修复',
  verify: '验证',
}

export const SOURCE_LABELS: Record<string, string> = {
  k8s_api: 'K8s API',
  prometheus: 'Prometheus',
  loki: 'Loki',
  events: 'K8s Events',
}

/** 证据来源着色（诊断页证据链/时间线共用） */
export function sourceTag(source: string): TagType {
  switch (source) {
    case 'k8s_api':
      return 'primary'
    case 'prometheus':
      return 'warning'
    case 'loki':
      return 'success'
    case 'events':
      return 'danger'
    default:
      return 'info'
  }
}

export const FAULT_TYPE_LABELS: Record<string, string> = {
  oom: '内存溢出 (OOM)',
  cpu_overload: 'CPU 过载',
  pod_crash: 'Pod 崩溃',
  image_pull_backoff: '镜像拉取失败',
  replica_anomaly: '副本数异常',
  network_latency: '网络延迟',
  node_not_ready: '节点 NotReady',
}

export const ACTION_LABELS: Record<string, string> = {
  update_resource_limit: '调整资源限额',
  scale_deployment: '调整副本数',
  restart_deployment: '重启工作负载',
  rollback_deployment: '回滚发布',
}

type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary'

export function faultStatusTag(status: string): TagType {
  switch (status) {
    case 'resolved':
      return 'success'
    case 'failed':
    case 'rolling_back':
      return 'danger'
    case 'detected':
    case 'diagnosing':
    case 'awaiting_approval':
    case 'remediating':
    case 'verifying':
      return 'warning'
    case 'closed':
      return 'info'
    default:
      return 'info'
  }
}

export function severityTag(severity: Severity): TagType {
  return severity === 'critical' ? 'danger' : 'warning'
}

export function riskTag(risk: RiskLevel): TagType {
  switch (risk) {
    case 'critical':
      return 'danger'
    case 'high':
      return 'danger'
    case 'medium':
      return 'warning'
    case 'low':
      return 'success'
    default:
      return 'info'
  }
}

export function remediationStatusTag(status: RemediationStatus): TagType {
  switch (status) {
    case 'succeeded':
      return 'success'
    case 'approved':
    case 'executing':
      return 'primary'
    case 'pending':
      return 'warning'
    case 'failed':
    case 'rolled_back':
      return 'danger'
    case 'rejected':
      return 'info'
    default:
      return 'info'
  }
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '-'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '-'
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return s === 0 ? `${m}m` : `${m}m${s}s`
}

export function formatAge(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  if (days > 0) return `${days}d`
  const hours = Math.floor(seconds / 3600)
  if (hours > 0) return `${hours}h`
  const minutes = Math.floor(seconds / 60)
  if (minutes > 0) return `${minutes}m`
  return `${seconds}s`
}

export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined) return '-'
  return `${(ratio * 100).toFixed(digits)}%`
}

/** ISO 8601 UTC → 本地可读时间 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

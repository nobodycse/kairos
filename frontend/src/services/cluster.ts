import request from './request'

/** 分页响应通用结构（design.md §1.1） */
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface PageQuery {
  page?: number
  page_size?: number
}

// ---------- cluster 模块（design.md §3.2–§3.4） ----------
// null 语义（§3.2 降级）：Prometheus 不可达/无样本时相关字段为 null，前端显示 '-'

export interface ClusterOverview {
  nodes: { total: number; ready: number }
  pods: { total: number; running: number; pending: number; failed: number }
  deployments: { total: number; available: number }
  active_faults: number
  resources: { cpu_usage_ratio: number | null; memory_usage_ratio: number | null }
  qps: number | null
  error_rate: number | null
  p95_latency: number | null
}

export interface Pod {
  namespace: string
  name: string
  workload: string | null
  node: string | null
  status: string
  ready: boolean
  restarts: number
  age_seconds: number
  cpu_usage_cores: number | null
  memory_usage_bytes: number | null
  memory_limit_bytes: number | null
}

export interface NodeInfo {
  name: string
  status: string
  roles: string
  version: string
  cpu_alloc_cores: number
  memory_alloc_bytes: number
  cpu_usage_ratio: number | null
  memory_usage_ratio: number | null
  pods_count: number
}

export interface Deployment {
  namespace: string
  name: string
  replicas: number
  ready_replicas: number
  image: string
  cpu_limit: string | null
  memory_limit: string | null
}

export interface ClusterEvent {
  namespace: string
  type: string
  reason: string
  object: string
  message: string
  count: number
  last_seen: string | null
}

/** 趋势点（design.md §3.2.1）；单点缺样本的字段为 null */
export interface TrendsPoint {
  ts: string
  qps: number | null
  error_rate: number | null
  p95_latency: number | null
}

export interface Trends {
  interval_seconds: number
  points: TrendsPoint[]
}

/** GET /api/v1/cluster/overview */
export function getOverview(): Promise<ClusterOverview> {
  return request.get('/cluster/overview') as Promise<ClusterOverview>
}

export function listPods(params?: { namespace?: string } & PageQuery): Promise<Page<Pod>> {
  return request.get('/cluster/pods', { params }) as Promise<Page<Pod>>
}

export function listNodes(params?: PageQuery): Promise<Page<NodeInfo>> {
  return request.get('/cluster/nodes', { params }) as Promise<Page<NodeInfo>>
}

export function listDeployments(params?: { namespace?: string } & PageQuery): Promise<Page<Deployment>> {
  return request.get('/cluster/deployments', { params }) as Promise<Page<Deployment>>
}

export function listClusterEvents(params?: { type?: string } & PageQuery): Promise<Page<ClusterEvent>> {
  return request.get('/cluster/events', { params }) as Promise<Page<ClusterEvent>>
}

/** GET /api/v1/cluster/trends?minutes=30（§3.2.1） */
export function getTrends(minutes = 30): Promise<Trends> {
  return request.get('/cluster/trends', { params: { minutes } }) as Promise<Trends>
}

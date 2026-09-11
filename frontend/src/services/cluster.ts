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

// ---------- cluster 模块（design.md §3.2–§3.5） ----------

export interface ClusterOverview {
  nodes: { total: number; ready: number }
  pods: { total: number; running: number; pending: number; failed: number }
  deployments: { total: number; available: number }
  active_faults: number
  resources: { cpu_usage_ratio: number; memory_usage_ratio: number }
  qps: number
  error_rate: number
  p95_latency: number
}

export interface Pod {
  namespace: string
  name: string
  workload: string
  node: string
  status: string
  ready: boolean
  restarts: number
  age_seconds: number
  cpu_usage_cores: number
  memory_usage_bytes: number
  memory_limit_bytes: number
}

export interface NodeInfo {
  name: string
  status: string
  roles: string
  version: string
  cpu_alloc_cores: number
  memory_alloc_bytes: number
  cpu_usage_ratio: number
  memory_usage_ratio: number
  pods_count: number
}

export interface Deployment {
  namespace: string
  name: string
  replicas: number
  ready_replicas: number
  image: string
  cpu_limit: string
  memory_limit: string
}

export interface ClusterEvent {
  namespace: string
  type: string
  reason: string
  object: string
  message: string
  count: number
  last_seen: string
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

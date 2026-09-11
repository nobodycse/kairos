import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import router from '@/router'

/**
 * axios 封装。契约见 docs/design.md §1.1：
 * - Base URL /api/v1，鉴权 Authorization: Bearer <JWT>
 * - 成功直接返回资源 JSON（不用信封），错误统一 {"detail": "..."}
 */
const request = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
})

request.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.set('Authorization', `Bearer ${auth.token}`)
  }
  return config
})

request.interceptors.response.use(
  (response) => response.data,
  (error: AxiosError<{ detail?: string }>) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail ?? error.message

    // 登录接口的 401 交给登录页展示；其余 401 视为 token 失效，清凭据回登录页
    const isLoginRequest = error.config?.url?.includes('/auth/login')
    if (status === 401 && !isLoginRequest) {
      const auth = useAuthStore()
      auth.logout()
      const current = router.currentRoute.value
      if (current.name !== 'login') {
        router.push({ name: 'login', query: { redirect: current.fullPath } })
      }
    } else {
      ElMessage.error(detail)
    }
    return Promise.reject(error)
  },
)

export default request

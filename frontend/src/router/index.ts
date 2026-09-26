import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

// history 模式：nginx try_files 已按 SPA 回退配置（deploy/docker/nginx.conf）
// 注意避开 /health、/api、/grafana 三个被 nginx 占用的路径前缀
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/components/AppLayout.vue'),
      children: [
        { path: '', redirect: { name: 'dashboard' } },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: '总览' },
        },
        {
          path: 'resources',
          name: 'resources',
          component: () => import('@/views/ResourcesView.vue'),
          meta: { title: '资源列表' },
        },
        {
          path: 'faults',
          name: 'faults',
          component: () => import('@/views/FaultListView.vue'),
          meta: { title: '故障事件' },
        },
        {
          path: 'diagnosis/:faultId',
          name: 'diagnosis',
          component: () => import('@/views/DiagnosisView.vue'),
          meta: { title: '诊断详情' },
        },
        {
          path: 'lab',
          name: 'lab',
          component: () => import('@/views/LabView.vue'),
          meta: { title: '故障实验室' },
        },
        {
          path: 'history',
          name: 'history',
          component: () => import('@/views/HistoryView.vue'),
          meta: { title: '历史与报告' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: { name: 'dashboard' } },
  ],
})

// 展示层鉴权：后端所有接口仍强制校验（architecture.md §11.2）
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isLoggedIn) {
    return { name: 'dashboard' }
  }
})

export default router

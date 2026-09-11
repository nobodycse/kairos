<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const activeMenu = computed(() => {
  if (route.name === 'diagnosis') return 'diagnosis'
  return (route.name as string) ?? 'dashboard'
})

const pageTitle = computed(() => (route.meta.title as string) ?? 'KAIROS')

function handleCommand(command: string) {
  if (command === 'logout') {
    auth.logout()
    ElMessage.success('已退出登录')
    router.push({ name: 'login' })
  }
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="logo">
        <span class="logo-name">KAIROS</span>
        <span class="logo-sub">K8s 智能运维</span>
      </div>
      <el-menu :default-active="activeMenu" router class="menu">
        <el-menu-item index="dashboard" :route="{ name: 'dashboard' }">
          <el-icon><Odometer /></el-icon>
          <span>总览</span>
        </el-menu-item>
        <el-menu-item index="resources" :route="{ name: 'resources' }">
          <el-icon><Monitor /></el-icon>
          <span>资源列表</span>
        </el-menu-item>
        <el-menu-item index="diagnosis" :route="{ name: 'diagnosis', params: { faultId: 42 } }">
          <el-icon><Search /></el-icon>
          <span>诊断详情</span>
        </el-menu-item>
        <el-menu-item index="lab" :route="{ name: 'lab' }">
          <el-icon><MagicStick /></el-icon>
          <span>故障实验室</span>
        </el-menu-item>
        <el-menu-item index="history" :route="{ name: 'history' }">
          <el-icon><Document /></el-icon>
          <span>历史与报告</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <span class="header-title">{{ pageTitle }}</span>
        <el-dropdown trigger="click" @command="handleCommand">
          <span class="user">
            <el-icon><UserFilled /></el-icon>
            admin
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.aside {
  background-color: #001529;
}

.logo {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 20px 0 16px;
  color: #fff;
}

.logo-name {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 2px;
}

.logo-sub {
  margin-top: 4px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.65);
}

.menu {
  border-right: none;
  background-color: #001529;
}

.menu :deep(.el-menu-item) {
  color: rgba(255, 255, 255, 0.72);
}

.menu :deep(.el-menu-item:hover) {
  background-color: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.menu :deep(.el-menu-item.is-active) {
  background-color: #1677ff;
  color: #fff;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.user {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: #303133;
  outline: none;
}

.main {
  padding: 20px;
  overflow-y: auto;
}
</style>

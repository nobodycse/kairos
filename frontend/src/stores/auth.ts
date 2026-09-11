import { defineStore } from 'pinia'
import { login as loginApi } from '@/services/auth'
import type { LoginRequest } from '@/services/auth'

const STORAGE_KEY = 'kairos_auth'

interface PersistedAuth {
  token: string
  expires_at: number
}

function loadPersisted(): PersistedAuth | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedAuth
    if (!parsed.token || !parsed.expires_at) return null
    return parsed
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => {
    const persisted = loadPersisted()
    return {
      token: persisted?.token ?? null as string | null,
      expires_at: persisted?.expires_at ?? null as number | null,
    }
  },
  getters: {
    isLoggedIn: (state) => state.token !== null && !isExpired(state.expires_at),
    isExpired: (state) => isExpired(state.expires_at),
  },
  actions: {
    async login(payload: LoginRequest) {
      const resp = await loginApi(payload)
      this.token = resp.access_token
      this.expires_at = Date.now() + resp.expires_in * 1000
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ token: this.token, expires_at: this.expires_at }))
    },
    logout() {
      this.token = null
      this.expires_at = null
      localStorage.removeItem(STORAGE_KEY)
    },
  },
})

function isExpired(expiresAt: number | null): boolean {
  // 留 60s 余量，避免临界点发出注定 401 的请求
  return expiresAt === null || Date.now() >= expiresAt - 60_000
}

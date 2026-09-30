import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { authApi, MENU_BY_ROLE, type CurrentUser, type LoginRequest } from '@/api/auth'
import { ApiError, getToken, setToken, setUnauthenticatedHandler } from '@/api/client'

/**
 * 认证状态。
 *
 * 有一个刻意的设计：**它不认识 localStorage，也不认识 axios**。
 * 令牌的存取全部经 `@/api/client`，组件只跟 store 说话。
 * 这样"令牌放哪里"这件事只有一个决策点——将来若要从 sessionStorage
 * 换成 httpOnly cookie，改动只落在一个文件里。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** 是否已经尝试过用现存令牌恢复会话。路由守卫据此决定要不要等。 */
  const initialized = ref(false)

  const isAuthenticated = computed(() => user.value !== null)

  const visibleMenus = computed(() =>
    user.value ? MENU_BY_ROLE[user.value.role] : [],
  )

  function canAccess(menu: string): boolean {
    return visibleMenus.value.includes(menu)
  }

  /**
   * 用现存令牌恢复会话。
   *
   * 刷新页面后 store 是空的，但 sessionStorage 里的令牌还在。
   * 只有真的拿它换回用户信息，才算"已登录"——仅凭本地有令牌就认为已登录，
   * 会在令牌已过期或被吊销时让用户看到一堆 401 之后才被踢出去。
   */
  async function restore(): Promise<void> {
    if (initialized.value) return
    const token = getToken()
    if (!token) {
      initialized.value = true
      return
    }
    try {
      user.value = await authApi.me()
    } catch {
      // 令牌失效是正常路径，不是异常——不记录也不提示，
      // 静默清理后由路由守卫送去登录页
      setToken(null)
      user.value = null
    } finally {
      initialized.value = true
    }
  }

  async function login(payload: LoginRequest): Promise<boolean> {
    loading.value = true
    error.value = null
    try {
      const result = await authApi.login(payload)
      setToken(result.token)
      user.value = result.user
      initialized.value = true
      return true
    } catch (e) {
      // 登录失败一律给同一句提示，不区分"用户不存在"与"密码错误"。
      // 区分开来等于给了一个账号枚举接口。
      error.value =
        e instanceof ApiError && e.code === 'NETWORK_ERROR'
          ? e.message
          : '租户、用户名或密码不正确'
      return false
    } finally {
      loading.value = false
    }
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 服务端登出失败不应阻止本地清理——
      // 用户的意图是"离开"，哪怕网络断了也得让他离开
    } finally {
      setToken(null)
      user.value = null
    }
  }

  /** 令牌失效时由 api 层回调，避免 api 层反向依赖 store。 */
  function handleUnauthenticated(): void {
    setToken(null)
    user.value = null
  }

  setUnauthenticatedHandler(handleUnauthenticated)

  return {
    user,
    loading,
    error,
    initialized,
    isAuthenticated,
    visibleMenus,
    canAccess,
    restore,
    login,
    logout,
  }
})

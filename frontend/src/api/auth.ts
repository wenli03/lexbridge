import { api } from './client'

/**
 * 认证接口。
 *
 * 注意请求体里**没有 tenantId**。租户身份完全由服务端从 JWT 解析，
 * 客户端传什么都不影响它落在哪个租户——这是 AC-5.1「跨租户 0 成功」的
 * 前端侧配合。把 tenantId 放进请求体看似方便，实际是把越权能力交给了客户端，
 * 而"服务端会忽略它"这种约定在代码演进中一定会失守。
 */

export interface LoginRequest {
  /** 租户标识，用于在登录页区分同一平台上的不同律所 */
  tenantCode: string
  username: string
  password: string
}

/** 角色决定界面能看到什么。后端仍会独立校验权限，前端隐藏只是体验而非安全。 */
export type Role = 'ADMIN' | 'LAWYER' | 'COMPLIANCE_OFFICER'

export interface CurrentUser {
  id: string
  username: string
  displayName: string
  role: Role
  tenantId: string
  tenantName: string
}

export interface LoginResponse {
  token: string
  expiresInSeconds: number
  user: CurrentUser
}

export const authApi = {
  login: (payload: LoginRequest) => api.post<LoginResponse>('/auth/login', payload),

  /** 用当前令牌换取用户信息。用于刷新页面后恢复会话状态。 */
  me: () => api.get<CurrentUser>('/auth/me'),

  logout: () => api.post<void>('/auth/logout'),
}

/**
 * 各角色可见的菜单项。
 *
 * 放在这里而不是散在各个组件里，是为了让"谁能看到什么"有一个可通读的清单——
 * 权限设计出错时，第一件事就是要能一眼看完整个矩阵。
 * 合规官刻意只有只读入口：他的职责是审阅与留痕，不是操作。
 */
export const MENU_BY_ROLE: Record<Role, string[]> = {
  ADMIN: ['knowledge', 'review', 'consult', 'audit'],
  LAWYER: ['consult', 'history'],
  COMPLIANCE_OFFICER: ['audit'],
}

export const ROLE_LABEL: Record<Role, string> = {
  ADMIN: '系统管理员',
  LAWYER: '执业律师',
  COMPLIANCE_OFFICER: '合规官',
}

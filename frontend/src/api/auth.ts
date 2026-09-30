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

/**
 * 演示入口。
 *
 * `enabled=false` 时 `accounts` 必为空。**两个字段一起判断**，不要只看数组长度：
 * "演示模式关着"与"演示模式开着但账号还没建出来"对使用者是两件事，
 * 前者应当一个入口都不显示，后者值得提示一句"稍等，正在初始化"。
 */
export interface DemoAccounts {
  enabled: boolean
  accounts: DemoAccount[]
}

export interface DemoAccount {
  tenantCode: string
  username: string
  password: string
  displayName: string
  role: Role
  /** 该角色登录后能看到什么。与服务端 MENU_BY_ROLE 描述的是同一件事。 */
  scope: string
}

export const authApi = {
  login: (payload: LoginRequest) => api.post<LoginResponse>('/auth/login', payload),

  /** 用当前令牌换取用户信息。用于刷新页面后恢复会话状态。 */
  me: () => api.get<CurrentUser>('/auth/me'),

  logout: () => api.post<void>('/auth/logout'),

  /**
   * 演示账号清单（匿名可读）。
   *
   * 本系统没有自助注册——账号由管理员开设，这是产品口径而非待补功能。
   * 但访客不知道"租户标识"该填什么，那一页就成了整个项目的门槛。
   * 这个接口把**已经公开写在 README 里的演示凭据**交给登录页渲染成按钮，
   * 点击后走的是**真实的登录流程**，不是任何形式的绕开。
   *
   * 未开启演示模式时返回 `enabled:false`，页面上不会出现任何入口。
   */
  demoAccounts: () => api.get<DemoAccounts>('/auth/demo-accounts'),
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

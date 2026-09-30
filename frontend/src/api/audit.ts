import { api } from './client'
import type { Role } from './auth'
import type { Page } from './knowledge'

/**
 * 审计日志接口（只读）。
 *
 * **这个模块刻意只有 `list` 一个方法。** 审计日志的价值来自"不可被调用方写入或修改"——
 * 后端压根没有 POST/PUT/DELETE 接口（见详细设计 §2.2 的说明）。前端多加一个
 * "删除日志"入口，即使后端会拒绝，也会让人以为这个行为是被允许的。
 */

/**
 * 与后端 `com.lexbridge.domain.model.AuditLog.Result` **逐字对齐**。
 *
 * 这里的取值不是自己定的：写成 `FAILED` 而后端是 `ERROR`，表现是"按结果筛选永远查不到记录"，
 * 而且不会报错——服务端只当作一个没有匹配的值。枚举值的唯一权威在后端。
 */
export type AuditResult = 'SUCCESS' | 'DENIED' | 'REDLINE_REFUSAL' | 'ERROR'

export const AUDIT_RESULT_LABEL: Record<AuditResult, string> = {
  SUCCESS: '成功',
  DENIED: '被拒',
  REDLINE_REFUSAL: '红线拒答',
  ERROR: '失败',
}

export interface AuditLogItem {
  id: string
  createdAt: string
  actorName: string
  /**
   * 操作者角色。**可以为 null**：用户被删除后其审计记录仍须可查，
   * 而此时 join 不出角色。类型写成非空的 `Role` 是一个类型谎言——
   * 它会让 `ROLE_LABEL[role]` 在运行时悄悄取到 undefined 而不报错。
   */
  actorRole: Role | null
  /** 动作代号，例如 `KNOWLEDGE_UPLOAD`、`CONSULT_RUN`、`PUBLISH` */
  action: string
  targetType?: string
  targetId?: string
  result: AuditResult
  /** 全链路追踪 ID。用户报障时报这一串，运维能直接定位 */
  traceId?: string
  detail?: string
  /** 该次操作涉及的法域。跨法域的咨询要能一眼看出走的是哪一套规则 */
  jurisdictionCode?: string
  /** 来源 IP。合规追溯里"从哪来的"和"谁做的"是并列的两问 */
  sourceIp?: string
}

export interface AuditQuery {
  page?: number
  size?: number
  /** ISO 日期，含当日 */
  from?: string
  to?: string
  actor?: string
  result?: AuditResult
  /** 只看拒答记录。合规官最常用的一类检索 */
  redlineOnly?: boolean
  /**
   * 关键词，服务端在 trace_id / 操作人 / 目标 / 详情上匹配。
   *
   * **与 `actor` 并存不是重复。** `actor` 是"这个人做过什么"，
   * `q` 是"这串 trace_id 是哪一次"——后者来自用户报障时贴过来的一行字，
   * 那时你不知道它属于谁。用户手里只有 trace_id 的场景才是追溯的起点。
   */
  q?: string
}

/**
 * 动作代号的中文标签。
 *
 * 键一律取自后端 `AuditLog.Action` 的枚举值。写错一个键的后果很隐蔽：
 * 界面上不会报错，只是那一行显示成原始代号（`KNOWLEDGE_PUBLISH`），
 * 而读到它的人会以为这是系统出了故障。
 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  LOGIN: '登录',
  LOGIN_FAILED: '登录失败',
  LOGOUT: '登出',
  KNOWLEDGE_UPLOAD: '法规上传',
  KNOWLEDGE_REVIEW: '入库复核',
  KNOWLEDGE_PUBLISH: '发布',
  KNOWLEDGE_ROLLBACK: '回滚',
  CONSULT_CREATE: '发起咨询',
  CONSULT_RESUME: '补充信息后恢复',
  AUDIT_QUERY: '审计查询',
  CROSS_TENANT_DENIED: '跨租户访问被拒',
}

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABEL[action] ?? action
}

export const auditApi = {
  list: (params?: AuditQuery) => api.get<Page<AuditLogItem>>('/audit-logs', { params }),
}

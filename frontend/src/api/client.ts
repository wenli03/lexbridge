import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from 'axios'

/**
 * 全站唯一的网络请求出口（对应 4+1 开发视图的 DR-6）。
 *
 * **为什么必须是唯一出口。** 分散的 fetch/axios 调用会让四件事失去统一的落点：
 * 认证头的注入、错误响应的归一、traceId 的生成与回传、401 的统一处置。
 * 这四件事里任何一件被遗漏在某个页面上，表现出来都是"某个页面偶尔出问题"，
 * 而排查时要从几十个调用点里找出那一个。
 *
 * 因此约定：**任何 `.vue` 文件都不得直接 import axios 或调用 fetch。**
 * 这条由 ESLint 规则强制（见 eslint 配置里的 no-restricted-imports）。
 */

/** 后端统一响应外壳，与 com.lexbridge.interfaces.dto.ApiResponse 一一对应。 */
export interface ApiEnvelope<T> {
  success: boolean
  data?: T
  error?: {
    code: string
    message: string
    /**
     * 字段校验失败时的明细。只含字段名与原因，**不含字段值**——
     * 入参里可能有案情与个人信息，后端刻意不回传。
     */
    details?: Record<string, string>
  }
  traceId?: string
  timestamp?: string
}

/**
 * 归一化后的错误。
 *
 * 把 axios 的多种失败形态（网络错误、超时、HTTP 状态码、业务错误码）
 * 压成一种，页面只需要判断 `code`。否则每个页面都要写一遍
 * "这是网络问题还是业务问题"的分支。
 */
export class ApiError extends Error {
  readonly code: string
  readonly status?: number
  readonly traceId?: string
  readonly details?: Record<string, string>

  constructor(
    code: string,
    message: string,
    opts: { status?: number; traceId?: string; details?: Record<string, string> } = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = opts.status
    this.traceId = opts.traceId
    this.details = opts.details
  }

  /** 会话失效。页面据此跳登录页而不是弹一个看不懂的错误。 */
  get isUnauthenticated(): boolean {
    return this.code === 'UNAUTHENTICATED' || this.status === 401
  }

  /** 跨租户访问被拒。后端会刻意把它伪装成 404，见 GlobalExceptionHandler。 */
  get isNotFound(): boolean {
    return this.code === 'NOT_FOUND' || this.status === 404
  }
}

// ---------------------------------------------------------------------------
// 认证令牌的存取
// ---------------------------------------------------------------------------
// 放 sessionStorage 而不是 localStorage：localStorage 在标签页关闭后仍然保留，
// 共用电脑上换个人打开浏览器就是上一个用户的会话。
// 代价是刷新页面保持、关闭标签页即登出——对这个使用场景是合适的取舍。
const TOKEN_KEY = 'lexbridge.token'

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    // 隐私模式下 sessionStorage 访问可能抛异常。降级为"无令牌"，
    // 而不是让整个应用崩在启动阶段
    return null
  }
}

export function setToken(token: string | null): void {
  try {
    if (token === null) {
      sessionStorage.removeItem(TOKEN_KEY)
    } else {
      sessionStorage.setItem(TOKEN_KEY, token)
    }
  } catch {
    /* 同上，静默降级 */
  }
}

// ---------------------------------------------------------------------------
// 请求追踪
// ---------------------------------------------------------------------------
/**
 * 生成与后端 TraceIdFilter 同构的追踪 ID。
 *
 * 后端会校验字符集与长度（防日志注入），不合法就丢弃并自行生成。
 * 这里主动生成合规的 UUID，保证前后端日志能真正对齐。
 */
export function newTraceId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  // 老浏览器兜底。仅用于关联日志，不需要密码学强度。
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

// ---------------------------------------------------------------------------
// 实例与拦截器
// ---------------------------------------------------------------------------
const http: AxiosInstance = axios.create({
  // 相对路径：开发期由 vite proxy 转发，生产由 nginx 转发。
  // 写成绝对地址会让开发与生产的差异从构建配置漏进代码。
  baseURL: '/api',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  // 每个请求都带 traceId，出错时用户可以把界面上显示的那串码直接给运维
  config.headers.set('X-Request-Id', newTraceId())
  return config
})

/** 401 时的回调。由 auth store 注册，避免 api 层反向依赖 store。 */
type UnauthenticatedHandler = () => void
let onUnauthenticated: UnauthenticatedHandler | null = null

export function setUnauthenticatedHandler(handler: UnauthenticatedHandler | null): void {
  onUnauthenticated = handler
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiEnvelope<unknown>>) => {
    // ---- 业务错误：HTTP 2xx 之外但拿到了结构化响应 ----
    const envelope = error.response?.data
    if (envelope?.error) {
      const apiError = new ApiError(envelope.error.code, envelope.error.message, {
        status: error.response?.status,
        traceId: envelope.traceId,
        details: envelope.error.details,
      })
      if (apiError.isUnauthenticated) {
        setToken(null)
        onUnauthenticated?.()
      }
      return Promise.reject(apiError)
    }

    // ---- 网络层失败：连不上、超时、被中断 ----
    // 这类失败没有响应体，必须给出可操作的提示。
    // "请求失败"这种文案对使用者毫无帮助——他需要知道是网络断了还是服务挂了。
    if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
      return Promise.reject(
        new ApiError('TIMEOUT', '请求超时。咨询链路耗时较长，请稍后重试或缩短问题范围。'),
      )
    }
    if (!error.response) {
      return Promise.reject(
        new ApiError('NETWORK_ERROR', '无法连接到服务端，请检查网络或稍后重试。'),
      )
    }

    // ---- 有响应但没有结构化错误体（例如网关返回的 HTML 错误页）----
    return Promise.reject(
      new ApiError(
        'UNEXPECTED',
        `服务返回了预期之外的状态（HTTP ${error.response.status}）`,
        { status: error.response.status },
      ),
    )
  },
)

// ---------------------------------------------------------------------------
// 对外接口
// ---------------------------------------------------------------------------
/**
 * 发起请求并解开响应外壳。
 *
 * 失败一律抛 {@link ApiError}，调用方用 `instanceof` 或 `code` 判断，
 * 不需要再处理 axios 的错误形态。
 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await http.request<ApiEnvelope<T>>(config)
  const envelope = response.data

  // 后端在某些场景（例如 204）可能直接返回裸对象，兼容之
  if (envelope === null || typeof envelope !== 'object' || !('success' in envelope)) {
    return envelope as unknown as T
  }

  if (!envelope.success) {
    throw new ApiError(
      envelope.error?.code ?? 'UNKNOWN',
      envelope.error?.message ?? '请求失败',
      { status: response.status, traceId: envelope.traceId, details: envelope.error?.details },
    )
  }
  return envelope.data as T
}

export const api = {
  get: <T>(url: string, config?: AxiosRequestConfig) =>
    request<T>({ ...config, method: 'GET', url }),
  post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<T>({ ...config, method: 'POST', url, data }),
  put: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<T>({ ...config, method: 'PUT', url, data }),
  patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    request<T>({ ...config, method: 'PATCH', url, data }),
  delete: <T>(url: string, config?: AxiosRequestConfig) =>
    request<T>({ ...config, method: 'DELETE', url }),
}

export default api

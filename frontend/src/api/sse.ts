import { ApiError, getToken, newTraceId } from './client'

/**
 * SSE 流式读取。
 *
 * **为什么不用浏览器原生的 `EventSource`。** `EventSource` 不支持自定义请求头，
 * 而我们的接口必须带 `Authorization`。用查询参数传令牌会把它写进
 * 浏览器历史、代理日志与 Referer，泄漏面比收益大得多。因此用
 * `fetch` + `ReadableStream` 手动解析事件流。
 *
 * **为什么放在 `api/` 而不是 `composables/`。** DR-6 约定"网络请求只允许
 * 出现在 `api` 层"。SSE 是网络请求的一种，它不该因为"用起来像 hook"
 * 就被归到另一个目录——规则一旦开口子，下一个开口子的人就不需要理由了。
 *
 * 事件名与负载结构见《详细设计》§2.3（`ai` 推给 `api`、`api` 原样转发）。
 */

// ---------------------------------------------------------------------------
// 事件负载类型（与详细设计 §2.3 的字段一一对应）
// ---------------------------------------------------------------------------
export interface NodeStartEvent {
  node: string
  seq: number
}

export interface InterruptEvent {
  missingSlots: string[]
  prompt: string
  interruptId: string
}

export interface TokenEvent {
  text: string
}

export interface CitationEvent {
  articleId: string
  articleNo: string
  similarity: number
}

/**
 * 红线卡里"可以改问的方向"。
 *
 * 允许纯字符串是为了兼容后端只给得出方向名称的情形——那种情况下"具体怎么做"
 * 那一行不渲染，而不是渲染出一个空段落。
 */
export type LegalAlternative = string | { title: string; desc?: string }

/** 命中的红线规则明细。判定依据必须能逐条查到，而不是只给一个编号 */
export interface RedlineRuleRef {
  id: string
  name: string
  /** 该规则对应的法条依据 */
  basis: string
}

/**
 * 「不做的范围 / 可以继续做的范围」对照。
 *
 * 拒答最容易传达错的一件事是"这个平台不帮我了"。两列并排是为了让用户看到
 * 边界的位置——被挡住的是一件事，不是一个人。
 */
export interface RedlineBoundary {
  refused: string
  allowed: string
}

export interface RedlineEvent {
  ruleId: string
  category: string
  /** 后端给出的拒答理由（五要素里的"原因"） */
  message: string
  legalAlternatives?: LegalAlternative[]
  /** 被拒答的诉求，原样回显用户想做的事——用户要能确认"它听懂的是不是我要问的" */
  refused?: string
  /** 为什么这件事不能做。与 `refused` 配套 */
  refusedWhy?: string
  /** 边界对照。缺省时该区块整个不渲染 */
  boundary?: RedlineBoundary
  /** 命中的规则明细。缺省时退回展示 `evidence` 原句 */
  ruleRefs?: RedlineRuleRef[]
}

/**
 * 降级的两种形态。
 *
 * 分开定义是因为使用者接下来要做的事不一样：
 *   RETRIEVAL_UNAVAILABLE —— 这次没查成（超时、检索服务故障）。重试有意义。
 *   RETRIEVAL_NO_MATCH    —— 查成了，知识库没覆盖这个问题。重试没有意义，
 *                            要做的是补语料或者换问法。
 * 合并成一句"未找到相关法条"，用户会对着一个只能靠补语料解决的问题反复点重试。
 */
export type DegradeKind = 'RETRIEVAL_UNAVAILABLE' | 'RETRIEVAL_NO_MATCH'

/**
 * 检索降级。
 *
 * **为什么降级要单独发一个事件，而不是等 `done` 之后从结构化结果里读。**
 * 降级轮次的逐字输出同样会推给前端，而用户是边收边读的。如果提示条要等到
 * 流结束才出现，用户早就把一段没有依据的文字当成结论读完了——那恰恰是
 * 提示条要防的事。拒答有 `redline` 事件，降级同理。
 *
 * 结构化结果里也会带一份（见 `RunResult.degradation`），因为回看历史会话时
 * 没有事件流，降级必须能从 `session(id)` 里重新读出来。
 */
export interface DegradedEvent {
  kind: DegradeKind
  /** 为什么降级。要说清楚"这次没查成"与"库里没有"的区别，这是两种不同的下一步 */
  reason: string
  /** 检索超时这类问题运维需要它才能定位，用户报障时可以直接给 */
  traceId?: string
}

export interface DoneEvent {
  runId: string
  usage: { totalTokens: number }
}

export interface ErrorEvent {
  code: string
  message: string
}

/** 入库任务的进度事件（对应 §10.1 的 event: progress） */
export interface ProgressEvent {
  jobId: string
  status: string
  stage: string
  progress: number
}

export type SseEventMap = {
  node_start: NodeStartEvent
  interrupt: InterruptEvent
  token: TokenEvent
  citation: CitationEvent
  redline: RedlineEvent
  degraded: DegradedEvent
  done: DoneEvent
  error: ErrorEvent
  progress: ProgressEvent
}

export type SseEventName = keyof SseEventMap

export interface SseEvent<K extends SseEventName = SseEventName> {
  event: K
  data: SseEventMap[K]
}

/** 未知事件名不做丢弃，交给调用方决定——静默忽略会让协议演进时非常难查。 */
export interface UnknownSseEvent {
  event: string
  data: unknown
}

export type AnySseEvent = SseEvent | UnknownSseEvent

export interface StreamOptions {
  /** 相对 `/api` 的路径，例如 `/consult-sessions/xxx/runs/yyy/stream` */
  path: string
  method?: 'GET' | 'POST'
  body?: unknown
  signal?: AbortSignal
  onEvent: (event: AnySseEvent) => void
}

/**
 * 逐事件读取一个 SSE 流。
 *
 * 流结束（含被 `interrupt` 正常结束）即 resolve；网络或协议错误抛 {@link ApiError}。
 * 调用方不需要自己处理分帧——这是把 SSE 放进 `api` 层的主要收益。
 */
export async function streamSse(options: StreamOptions): Promise<void> {
  const { path, method = 'POST', body, signal, onEvent } = options

  const headers: Record<string, string> = {
    Accept: 'text/event-stream',
    'X-Request-Id': newTraceId(),
  }
  const token = getToken()
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  if (!response.ok) {
    // 失败时后端返回的是统一外壳（JSON），不是事件流。
    // 这里把它转成与普通请求一致的 ApiError，页面就不需要两套错误处理。
    if (response.status === 401) {
      throw new ApiError('UNAUTHENTICATED', '会话已失效，请重新登录。', { status: 401 })
    }
    let message = `流式接口返回了异常状态（HTTP ${response.status}）`
    let code = 'UNEXPECTED'
    try {
      const envelope = (await response.json()) as {
        error?: { code?: string; message?: string }
      }
      if (envelope.error?.message) {
        message = envelope.error.message
        code = envelope.error.code ?? code
      }
    } catch {
      // 响应体不是 JSON（例如网关的 HTML 错误页），保留上面的兜底文案
    }
    throw new ApiError(code, message, { status: response.status })
  }

  if (!response.body) {
    throw new ApiError('UNEXPECTED', '服务端未返回流式响应体。', { status: response.status })
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      buffer += decoder.decode(value, { stream: true })
      // SSE 以空行分帧。用 \n\n 而不是逐行读，是因为一个事件的数据可能跨多行。
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const event = parseFrame(frame)
        if (event) {
          onEvent(event)
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
    // 流末尾可能还有一个没有以空行结束的帧
    const tail = parseFrame(buffer)
    if (tail) {
      onEvent(tail)
    }
  } finally {
    reader.releaseLock()
  }
}

function parseFrame(frame: string): AnySseEvent | null {
  let eventName = 'message'
  const dataLines: string[] = []

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.trimEnd()
    if (!line || line.startsWith(':')) {
      // 空行与注释行（心跳常写成 `: ping`）都不是数据
      continue
    }
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim())
    }
  }

  if (dataLines.length === 0) {
    return null
  }

  const payload = dataLines.join('\n')
  try {
    return { event: eventName, data: JSON.parse(payload) } as AnySseEvent
  } catch {
    // 非法 JSON 不抛错：宁可让调用方看到一条原样数据，也不要整条链路断掉
    return { event: eventName, data: payload }
  }
}

import { api } from './client'
import {
  streamSse,
  type AnySseEvent,
  type DegradedEvent,
  type DegradeKind,
  type LegalAlternative,
  type RedlineBoundary,
  type RedlineEvent,
  type RedlineRuleRef,
} from './sse'

// 降级与红线都定义在协议层（它们同时出现在事件流与结构化结果里），
// 这里转出去，页面只从 `@/api/consult` 取领域类型，不必知道它存在哪个文件。
export type { DegradeKind, LegalAlternative, RedlineBoundary, RedlineEvent, RedlineRuleRef }

/**
 * 咨询接口。
 *
 * 两类咨询共用一套会话与运行模型，只是 `type` 不同：
 *   TAX_PLANNING —— 跨境税务筹划（输出多套候选架构对比）
 *   DIVERGENCE   —— 监管差异分析（输出法域规则矩阵）
 *
 * **为什么要显式传 `type` 而不是让后端猜。** 意图识别在 `ai` 侧确实会做一次，
 * 但把用户的选择也传过去，可以让"识别错了"变成可诊断的问题：两者不一致时
 * 能看出是哪一步错了，而不是只看到"结果不对"。
 */

export type ConsultType = 'TAX_PLANNING' | 'DIVERGENCE'

export const CONSULT_TYPE_LABEL: Record<ConsultType, string> = {
  TAX_PLANNING: '跨境税务筹划',
  DIVERGENCE: '监管差异分析',
}

export const CONSULT_TYPE_HINT: Record<ConsultType, string> = {
  TAX_PLANNING: '比较多个法域的税负与协定待遇，给出候选架构与合规风险',
  DIVERGENCE: '逐法域比对同一交易的规则差异，标注适用条件与稳定性',
}

export type RunStatus = 'RUNNING' | 'INTERRUPTED' | 'COMPLETED' | 'FAILED' | 'REFUSED'

/**
 * 状态的中文标签。
 *
 * `INTERRUPTED` 写成「待补充信息」而不是「已中断」：中断在这套系统里不是故障，
 * 是系统发现信息不足后主动停下来等人补料（见 `ConsultView` 的说明）。
 * 写成「已中断」，使用者会以为这次咨询坏了，于是重开一个——而正确的做法是
 * 回到那一次把问题补完，检查点还在，已完成的节点不会重跑。
 */
export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  RUNNING: '进行中',
  INTERRUPTED: '待补充信息',
  COMPLETED: '已完成',
  FAILED: '失败',
  REFUSED: '红线拒答',
}

/**
 * 标签查询。
 *
 * 与 `auditActionLabel` 同一个理由：枚举值的唯一权威在后端，前端拿到的可能
 * 是一个尚未登记的值。直接查表会得到 `undefined` 并渲染成空白标签——
 * 看起来像界面出了故障。退回原始代号，至少读得出来是什么。
 */
export function runStatusLabel(status: RunStatus): string {
  return RUN_STATUS_LABEL[status] ?? status
}

export interface ConsultSession {
  id: string
  title: string
  type: ConsultType
  status: RunStatus
  createdAt: string
  updatedAt: string
  runCount: number
  /** 知识截止日期：结论基于哪一版知识库，必须常驻可见（OQ-09） */
  knowledgeVersion?: number
}

export interface ConsultRun {
  runId: string
  sessionId: string
  status: RunStatus
  createdAt: string
  /** 回答所依据的时点，回看历史咨询时用它判断引用是否已失效 */
  asOf: string
}

// ---------------------------------------------------------------------------
// 结构化结果
// ---------------------------------------------------------------------------
// **为什么结构化结果不通过 SSE 推送。** 事件流只负责"过程"——节点推进、
// 逐字输出、引用、拒答、中断；而"结论"是一个有嵌套结构、需要同时展示
// 多张表与矩阵的对象，塞进事件流会让每一帧都很难校验。因此约定：
// 流在 `done` 处结束，随后前端用 `session(id)` 拉取会话详情里的结构化结果。
export interface ResultCitation {
  articleId: string
  articleNo: string
  similarity?: number
  /**
   * 该引用在结论时点（`RunResult.asOf`）已经失效。
   *
   * **必须与"相似度低"分开看。** 相似度低是"这条挂得勉强"，复核一下还能用；
   * 失效是"这条现在已经不是法了"，整条结论据此作废。把两者压成一个
   * "引用质量"分数，使用者就分不出该去补语料还是该重新提问。
   */
  stale?: boolean
  /** 该条文的现行版本。有值时引用旁给出跳转入口，没有就只标失效 */
  supersededByArticleId?: string
  supersededByArticleNo?: string
}

export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH'

/** 税务筹划：一套候选架构 */
export interface CandidateStructure {
  name: string
  /** 综合税负率等关键数字**由确定性计算模块产出**，不是模型写的（AC-2.3） */
  effectiveTaxRate: string
  withholdingTaxImpact: string
  complianceRisk: RiskLevel
  implementationComplexity: RiskLevel
  /** 所依赖的税收协定条款 */
  dependentTreatyArticles: string[]
  /** 该方案的计算可复算轨迹说明（数字为何是这个值） */
  calcTrace?: string
  citations: ResultCitation[]
}

/** 监管差异分析：矩阵的格子 */
export type DivergenceVerdict = 'PERMITTED' | 'PROHIBITED' | 'CONDITIONAL' | 'NOT_FOUND'

export const DIVERGENCE_VERDICT_LABEL: Record<DivergenceVerdict, string> = {
  PERMITTED: '许可',
  PROHIBITED: '禁止',
  CONDITIONAL: '附条件',
  /** 这一格的语义是"检索不到明确规定"，**不是"允许"**——界面必须能一眼区分 */
  NOT_FOUND: '未检索到明确规定',
}

export interface DivergenceCell {
  jurisdictionCode: string
  verdict: DivergenceVerdict
  /** 附条件时必填 */
  condition?: string
  citations: ResultCitation[]
}

export interface DivergenceRow {
  dimension: string
  stability: 'STABLE' | 'WATCH' | 'TIGHTENING'
  cells: DivergenceCell[]
}

/**
 * 本轮降级标记。与 SSE 的 `degraded` 事件同构——两处来源渲染成同一条提示，
 * 所以直接用同一个类型，避免两个形状慢慢长歪。
 *
 * **有值就等于"这一轮不能作为依据"。** 降级时后端仍会返回一段可读的正文，
 * 界面必须让用户先看到提示条再看到正文，否则没有依据的文字会被当成结论。
 */
export type Degradation = DegradedEvent

/**
 * 一条假设或取值。
 *
 * 三档沿用合规语义色，不新造一套：
 *   risk    —— 这条不成立时结论会翻转（例如本轮压根没有引用）
 *   caution —— 已知的资料缺口（语料未覆盖、税率表未入库、条文是旧版本）
 *   safe    —— 这一条不依赖假设，是可以直接核对的事实
 * 使用者已经在引用、矩阵、风险列上学会了这三档的含义，新颜色只会让人重学一遍。
 */
export interface Assumption {
  level: 'risk' | 'caution' | 'safe'
  text: string
}

export interface RunResult {
  kind: ConsultType
  /** 结论正文（逐字输出结束后落定的完整文本） */
  answer: string
  summary?: string
  candidates?: CandidateStructure[]
  matrix?: DivergenceRow[]
  citations: ResultCitation[]
  /** 本次结论基于哪一版知识库与哪个时点 */
  knowledgeVersion?: number
  asOf?: string
  /** 被引用校验剥离的结论句数。>0 时界面必须显示，否则用户以为答案是完整的 */
  strippedClaims?: number
  /** 本轮降级标记。有值即降级，界面必须显示，且正文不得被当作结论 */
  degradation?: Degradation
  /**
   * 本轮的红线拒答。
   *
   * 实时推流时它来自 `redline` 事件；但**回看历史会话时没有事件流**，
   * 只认事件的话，一条被拒答的记录重新打开后会变成一段没有提示的空白，
   * 而合规官恰恰是在回看时做拒答复核。所以结构化结果里也必须有一份。
   */
  redline?: RedlineEvent
  /**
   * 结论所依赖的假设与取值。降级轮次同样会有——而且那正是最需要看的时候：
   * "本轮未产生任何引用"这类前提，不写出来用户就不知道结论是空的。
   */
  assumptions?: Assumption[]
  /**
   * 计算过程的总说明，例如"税率表未入库，下列数字是演示值"。
   * 逐候选的复算轨迹在 `CandidateStructure.calcTrace` 上。
   */
  calcNote?: string
}

export interface ConsultSessionDetail extends ConsultSession {
  runs: {
    runId: string
    question: string
    status: RunStatus
    createdAt: string
    result?: RunResult
  }[]
}

export interface StartRunRequest {
  question: string
  type: ConsultType
  asOf?: string
}

export interface ResumeRequest {
  interruptId: string
  /** 槽位名 → 用户补充的内容 */
  slots: Record<string, string>
}

/**
 * 会话列表的查询参数。
 *
 * **这几个筛选是给「会话历史」页用的，不是给咨询页左栏用的。**
 * 左栏是切换器（20 条、不翻页、不筛选），够用；历史页是记录，要回答的是
 * 「上个月那次差异分析在哪」和「哪几次被拒了」。
 *
 * 与 `AuditQuery` 的 `q` 同一个问题：**后端目前只实现了 `page` / `size`。**
 * 服务端忽略多余的查询参数时不会报错，只会返回未筛选的结果——
 * 于是界面上的筛选看起来"点了没反应"。这是这一页最需要后端跟上的地方，
 * 契约写在 README 里。
 */
export interface ConsultQuery {
  page?: number
  size?: number
  /** 按咨询类型筛选 */
  type?: ConsultType
  /** 按状态筛选 */
  status?: RunStatus
  /** 关键词，服务端在会话标题上匹配 */
  q?: string
  /** ISO 日期，含当日 */
  from?: string
  to?: string
}

/** 一次流式交互的通用配置 */
export interface StreamHandlers {
  onEvent: (event: AnySseEvent) => void
  onError?: (error: unknown) => void
  onClose?: () => void
}

function startStream(path: string, body: unknown, handlers: StreamHandlers): () => void {
  const controller = new AbortController()
  streamSse({
    path,
    method: 'POST',
    body,
    signal: controller.signal,
    onEvent: handlers.onEvent,
  })
    .then(() => handlers.onClose?.())
    .catch((error: unknown) => {
      if (controller.signal.aborted) {
        return
      }
      handlers.onError?.(error)
    })
  return () => controller.abort()
}

export const consultApi = {
  sessions: (params?: ConsultQuery) =>
    api.get<{ items: ConsultSession[]; total: number; page: number; size: number }>(
      '/consult-sessions',
      { params },
    ),

  createSession: (payload: { title: string; type: ConsultType }) =>
    api.post<ConsultSession>('/consult-sessions', payload),

  /** 会话详情。`done` 之后用它取结构化结果（结论不走事件流）。 */
  session: (sessionId: string) =>
    api.get<ConsultSessionDetail>(`/consult-sessions/${sessionId}`),

  /** 发起一次咨询。拿到 runId 后由 `streamRun` 打开事件流。 */
  startRun: (sessionId: string, payload: StartRunRequest) =>
    api.post<ConsultRun>(`/consult-sessions/${sessionId}/runs`, payload),

  /**
   * 订阅一次咨询的事件流。
   *
   * 流会在 `interrupt`（需要补充信息）或 `done`/`error` 处结束。
   * 中断不是失败——它是一次正常的状态，页面据此渲染追问表单。
   */
  streamRun: (sessionId: string, runId: string, handlers: StreamHandlers): (() => void) =>
    startStream(`/consult-sessions/${sessionId}/runs/${runId}/stream`, {}, handlers),

  /** 补充追问信息后从检查点恢复。已完成节点不会重跑。 */
  resumeRun: (
    sessionId: string,
    runId: string,
    payload: ResumeRequest,
    handlers: StreamHandlers,
  ): (() => void) =>
    startStream(`/consult-sessions/${sessionId}/runs/${runId}/resume`, payload, handlers),
}

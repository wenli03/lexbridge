import { api } from './client'
import { streamSse, type AnySseEvent, type ProgressEvent } from './sse'

/**
 * 知识库接口（管理员侧）。
 *
 * 端点为《详细设计》§2.2 的契约，路径已与后端对齐——**不要在这里发明路径**，
 * 前端自造路径的后果是联调时才发现，而那时人已经在别处写了一天代码。
 */

// ---------------------------------------------------------------------------
// 法域：六法域是本项目的既定范围（PRD PD-01 / OQ-01）
// ---------------------------------------------------------------------------
export interface Jurisdiction {
  code: string
  label: string
  labelEn: string
}

export const JURISDICTIONS: Jurisdiction[] = [
  { code: 'CN', label: '中国内地', labelEn: 'Mainland China' },
  { code: 'HK', label: '香港', labelEn: 'Hong Kong' },
  { code: 'SG', label: '新加坡', labelEn: 'Singapore' },
  { code: 'IE', label: '爱尔兰', labelEn: 'Ireland' },
  { code: 'NL', label: '荷兰', labelEn: 'Netherlands' },
  { code: 'KY', label: '开曼群岛', labelEn: 'Cayman Islands' },
]

export function jurisdictionLabel(code: string): string {
  return JURISDICTIONS.find((j) => j.code === code)?.label ?? code
}

// ---------------------------------------------------------------------------
// 入库任务
// ---------------------------------------------------------------------------
/** 与 `kb.ingestion_job.status` 一致 */
export type JobStatus =
  | 'PENDING'
  | 'PARSING'
  | 'EXTRACTING'
  | 'AWAITING_REVIEW'
  | 'PUBLISHED'
  | 'FAILED'

/** 与 `kb.ingestion_job.stage` 一致 */
export type JobStage =
  | 'RECEIVED'
  | 'PARSED'
  | 'CHUNKED'
  | 'EXTRACTED'
  | 'WIKI_BUILT'
  | 'INDEXED'
  | 'NEEDS_MANUAL'

export interface IngestionJob {
  jobId: string
  status: JobStatus
  stage: JobStage
  /** 0–100 */
  progress: number
  statuteTitle?: string
  jurisdictionCode?: string
  /** 进入复核队列的条目数；>0 表示发布前必须有人看过 */
  lowConfidenceCount?: number
  /** 失败或进入人工队列时的原因。界面必须原样展示，不要换成"处理失败" */
  errorDetail?: string
  createdAt?: string
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  PENDING: '排队中',
  PARSING: '解析中',
  EXTRACTING: '抽取中',
  AWAITING_REVIEW: '待复核',
  PUBLISHED: '已发布',
  FAILED: '失败',
}

/** 阶段顺序，用于展示"走到第几步"。NEEDS_MANUAL 是终态而非步骤，单独处理。 */
export const JOB_STAGE_ORDER: JobStage[] = [
  'RECEIVED',
  'PARSED',
  'CHUNKED',
  'EXTRACTED',
  'WIKI_BUILT',
  'INDEXED',
]

export const JOB_STAGE_LABEL: Record<JobStage, string> = {
  RECEIVED: '接收',
  PARSED: '版面解析',
  CHUNKED: '条款切分',
  EXTRACTED: '本体抽取',
  WIKI_BUILT: '词条生成',
  INDEXED: '建索引',
  NEEDS_MANUAL: '待人工干预',
}

export interface UploadOptions {
  file: File
  jurisdictionCode: string
  /** 公开来源 URL。AC-1.6 要求每条法条可溯源，因此这一项不是可选的装饰 */
  sourceUrl: string
  effectiveFrom: string
  /** 留空表示仍然有效 */
  effectiveTo?: string
  versionLabel: string
  statuteTitle?: string
}

// ---------------------------------------------------------------------------
// 法规与法条
// ---------------------------------------------------------------------------
export type PublishStatus = 'DRAFT' | 'PUBLISHED' | 'RETIRED'

export interface StatuteSummary {
  id: string
  title: string
  titleOriginal?: string
  statuteNo?: string
  jurisdictionCode: string
  versionLabel: string
  effectiveFrom: string
  effectiveTo?: string | null
  /** 生效日期精度：IE 的公开源只到年，界面必须标出来而不是假装精确到日 */
  datePrecision?: 'DAY' | 'YEAR'
  publishStatus: PublishStatus
  articleCount: number
  sourceUrl?: string
}

export interface ArticleDetail {
  id: string
  articleNo: string
  hierarchyPath: string[]
  content: string
  effectiveFrom: string
  effectiveTo?: string | null
  publishStatus: PublishStatus
  statuteTitle: string
  confidence?: number | null
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  size: number
}

export interface StatuteQuery {
  page?: number
  size?: number
  jurisdictionCode?: string
  publishStatus?: PublishStatus
}

// ---------------------------------------------------------------------------
// 复核
// ---------------------------------------------------------------------------
export type ReviewSubject = 'ARTICLE' | 'WIKI_SECTION'
export type ReviewDecision = 'CONFIRMED' | 'CORRECTED' | 'REJECTED'

export interface ReviewItem {
  itemId: string
  jobId: string
  subjectType: ReviewSubject
  /** 出问题的具体字段；为空表示整条待复核 */
  fieldName?: string | null
  /** 抽取结果（结构化） */
  extractedValue: unknown
  confidence: number
  /** 左侧对照用的条款原文 */
  originalText: string
  articleNo: string
  hierarchyPath: string[]
  statuteTitle?: string
  createdAt?: string
}

export interface ReviewSubmit {
  decision: ReviewDecision
  /** `CORRECTED` 时必须给出修正后的值 */
  correction?: Record<string, unknown>
  note?: string
}

// ---------------------------------------------------------------------------
// 发布与回滚
// ---------------------------------------------------------------------------
export interface PublishResult {
  knowledgeVersion: number
  articleCount: number
  publishedAt: string
}

export interface RollbackRequest {
  scopeType: 'PLATFORM' | 'TENANT'
  /** 目标版本号；不传表示回退到上一版本 */
  knowledgeVersion?: number
}

// ---------------------------------------------------------------------------
// 接口实现
// ---------------------------------------------------------------------------
export const knowledgeApi = {
  /**
   * 上传法规文件。
   *
   * 用 `FormData` 而不是 JSON——原件要原样落盘（用户可能上传数百页 PDF），
   * base64 进 JSON 会让请求体膨胀三分之一并在内存里多留一份副本。
   */
  upload: ({ file, ...meta }: UploadOptions) => {
    const form = new FormData()
    form.append('file', file)
    form.append('jurisdictionCode', meta.jurisdictionCode)
    form.append('sourceUrl', meta.sourceUrl)
    form.append('effectiveFrom', meta.effectiveFrom)
    form.append('versionLabel', meta.versionLabel)
    if (meta.effectiveTo) {
      form.append('effectiveTo', meta.effectiveTo)
    }
    if (meta.statuteTitle) {
      form.append('statuteTitle', meta.statuteTitle)
    }
    return api.post<{ jobId: string }>('/knowledge/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  job: (jobId: string) => api.get<IngestionJob>(`/knowledge/jobs/${jobId}`),

  /**
   * 订阅入库进度。
   *
   * 返回一个中止函数，而不是一个 Promise：组件卸载时必须能主动断开，
   * 否则用户离开页面后连接仍挂着，服务端会一直推给一个没人看的流。
   */
  streamJob: (
    jobId: string,
    handlers: {
      onEvent: (event: AnySseEvent) => void
      onError?: (error: unknown) => void
      onClose?: () => void
    },
  ): (() => void) => {
    const controller = new AbortController()
    streamSse({
      path: `/knowledge/jobs/${jobId}/stream`,
      method: 'GET',
      signal: controller.signal,
      onEvent: handlers.onEvent,
    })
      .then(() => handlers.onClose?.())
      .catch((error: unknown) => {
        // 主动中止不是错误——它正是调用方要的结果
        if (controller.signal.aborted) {
          return
        }
        handlers.onError?.(error)
      })
    return () => controller.abort()
  },

  reviewQueue: (params?: { page?: number; size?: number; jobId?: string }) =>
    api.get<Page<ReviewItem>>('/knowledge/review', { params }),

  submitReview: (itemId: string, payload: ReviewSubmit) =>
    api.post<void>(`/knowledge/review/${itemId}`, payload),

  publish: (payload?: { scopeType?: 'PLATFORM' | 'TENANT'; note?: string }) =>
    api.post<PublishResult>('/knowledge/publish', payload ?? {}),

  rollback: (payload: RollbackRequest) => api.post<void>('/knowledge/rollback', payload),

  statutes: (params?: StatuteQuery) => api.get<Page<StatuteSummary>>('/statutes', { params }),

  article: (articleId: string) => api.get<ArticleDetail>(`/articles/${articleId}`),
}

/** 进度事件的负载类型在这里再导出一次，方便页面只 import 一个模块 */
export type { ProgressEvent }

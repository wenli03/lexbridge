<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { isEndpointMissing } from '@/api/notImplemented'
import {
  JOB_STAGE_LABEL,
  JOB_STATUS_LABEL,
  JURISDICTIONS,
  jurisdictionLabel,
  knowledgeApi,
  type IngestionJob,
  type JobStatus,
  type PublishResult,
  type PublishStatus,
  type StatuteSummary,
} from '@/api/knowledge'

import StateBlock from '@/components/StateBlock.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import UploadPanel from '@/components/UploadPanel.vue'

/**
 * 知识库页（管理员）。
 *
 * 这一页要回答管理员的三个问题，顺序不能乱：
 *   1. 我刚上传的东西处理到哪一步了？（入库任务进度）
 *   2. 库里现在有什么？（法规列表 + 筛选）
 *   3. 现在生效的是哪一版？能不能回滚？（发布状态）
 *
 * 「实时生效」是这一页的核心卖点，因此发布成功后**不提示"请刷新页面"**——
 * 界面自己重新拉取，用户看到的是数据已经变了。
 *
 * 但"实时生效"要成立，必须同时回答"现在生效的是哪一版"。所以：
 *
 *   - 当前生效版本号常驻可见。发布和回滚的全部意义都相对它而言，
 *     不显示它，两个按钮点下去就没有参照——用户只能凭记忆判断有没有生效。
 *   - 两者都要二次确认。它们会立刻改变**所有人**的检索结果，且没有撤销：
 *     发布让草稿进入生产检索，回滚把整个平台退回上一版。
 *   - 确认做成行内条而不是模态框。做这个决定需要的上下文（当前版本、待发布的
 *     条目数）就在同一屏上，弹窗会把它盖住，于是用户只能凭记忆点"确定"。
 */

const showUpload = ref(false)

const statutes = ref<StatuteSummary[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)
const loadError = ref<{ message: string; traceId?: string } | null>(null)

const filters = ref<{ jurisdictionCode: string; publishStatus: PublishStatus | '' }>({
  jurisdictionCode: '',
  publishStatus: '',
})

const job = ref<IngestionJob | null>(null)
const jobError = ref<string | null>(null)
let closeJobStream: (() => void) | null = null

const selected = ref<StatuteSummary | null>(null)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))

const JOB_TONE: Record<JobStatus, 'brand' | 'caution' | 'safe' | 'risk' | 'slate'> = {
  PENDING: 'slate',
  PARSING: 'brand',
  EXTRACTING: 'brand',
  AWAITING_REVIEW: 'caution',
  PUBLISHED: 'safe',
  FAILED: 'risk',
}

const STATUS_TONE: Record<PublishStatus, 'safe' | 'slate' | 'caution'> = {
  PUBLISHED: 'safe',
  DRAFT: 'caution',
  RETIRED: 'slate',
}

const STATUS_LABEL: Record<PublishStatus, string> = {
  PUBLISHED: '已发布',
  DRAFT: '草稿',
  RETIRED: '已废止',
}

async function loadStatutes(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const result = await knowledgeApi.statutes({
      page: page.value,
      size,
      jurisdictionCode: filters.value.jurisdictionCode || undefined,
      publishStatus: filters.value.publishStatus || undefined,
    })
    statutes.value = result.items
    total.value = result.total
  } catch (error) {
    const apiError = error instanceof ApiError ? error : null
    loadError.value = {
      message: apiError?.message ?? '法规列表加载失败',
      traceId: apiError?.traceId,
    }
  } finally {
    loading.value = false
  }
}

function onFilterChange(): void {
  page.value = 1
  void loadStatutes()
}

/**
 * 订阅入库进度。
 *
 * 用 SSE 而不是轮询：入库的耗时集中在解析与抽取，进度变化本来就不均匀，
 * 轮询要么太密（浪费）要么太疏（看不出在动）。断流后回退到"手动刷新"，
 * 而不是静默停在旧数据上——那会让人以为任务卡住了。
 */
function trackJob(jobId: string): void {
  closeJobStream?.()
  jobError.value = null
  job.value = {
    jobId,
    status: 'PENDING',
    stage: 'RECEIVED',
    progress: 0,
  }

  closeJobStream = knowledgeApi.streamJob(jobId, {
    onEvent: (event) => {
      if (event.event === 'progress' && job.value) {
        job.value = { ...job.value, ...(event.data as Partial<IngestionJob>) }
      }
      if (event.event === 'done') {
        void refreshJob(jobId)
      }
      if (event.event === 'error') {
        jobError.value = (event.data as { message: string }).message
      }
    },
    onClose: () => {
      void refreshJob(jobId)
    },
    onError: () => {
      jobError.value = '进度推送中断，可点击「刷新」查看最新状态。'
    },
  })
}

async function refreshJob(jobId: string): Promise<void> {
  try {
    job.value = await knowledgeApi.job(jobId)
    if (job.value.status === 'AWAITING_REVIEW' || job.value.status === 'PUBLISHED') {
      await loadStatutes()
    }
  } catch {
    /* 进度查询失败不影响列表：列表本身有自己的错误态 */
  }
}

/**
 * 当前生效的知识版本。
 *
 * **没有值时如实显示"未知"，不猜。** `knowledgeApi` 里没有"查询当前版本"的接口，
 * 凭空造一个路径只会在联调时才发现对不上；用已发布法条数之类的侧面信息去推算，
 * 则会给出一个看起来很确定、其实没有依据的数字——而版本号恰恰是这块界面上
 * 唯一需要被信任的数字。
 */
const knowledgeVersion = ref<number | null>(null)
/** 最近一次发布的回执。`PublishResult` 以前被直接丢掉了 */
const lastPublish = ref<PublishResult | null>(null)

const pendingAction = ref<'publish' | 'rollback' | null>(null)
const running = ref<'publish' | 'rollback' | null>(null)
const actionError = ref<{ message: string; traceId?: string } | null>(null)

/**
 * 执行待确认的动作。
 *
 * `pendingAction` 直到结束才清空，所以确认条在处理期间仍然留在屏幕上显示
 * "处理中…"——发布要等后端跑完索引，中途把确认条换回两个原始按钮，
 * 用户会以为没点上而再点一次。
 */
async function confirmPending(): Promise<void> {
  const action = pendingAction.value
  if (!action || running.value) {
    return
  }
  running.value = action
  actionError.value = null
  try {
    if (action === 'publish') {
      const result = await knowledgeApi.publish({ scopeType: 'PLATFORM' })
      lastPublish.value = result
      knowledgeVersion.value = result.knowledgeVersion
    } else {
      await knowledgeApi.rollback({ scopeType: 'PLATFORM' })
      // 回滚的响应体里没有版本号。把已知版本减一是个看起来合理、实际没有依据的
      // 猜测（回滚的粒度是后端定的），所以退回"未知"而不是编一个数字
      knowledgeVersion.value = null
      lastPublish.value = null
    }
    // 发布后立即重拉：让"实时生效"在界面上是自证的，而不是靠一句提示
    await loadStatutes()
  } catch (error) {
    const apiError = error instanceof ApiError ? error : null
    // 服务端没有这组路由时把话说清楚。默认的 404 文案是「请求的资源不存在」，
    // 它虽然不错，但对"这个按钮为什么没用"没有回答；使用者会去怀疑权限或数据。
    actionError.value = {
      message: isEndpointMissing(error)
        ? '发布与回滚的服务端尚未实现（接口返回 404），这一步无法完成。'
        : (apiError?.message ?? (action === 'publish' ? '发布失败' : '回滚失败')),
      traceId: apiError?.traceId,
    }
  } finally {
    running.value = null
    pendingAction.value = null
  }
}

onMounted(() => {
  void loadStatutes()
})

onBeforeUnmount(() => {
  closeJobStream?.()
})
</script>

<template>
  <div class="space-y-5">
    <!-- 顶部动作 -->
    <div class="flex flex-wrap items-center justify-between gap-y-2">
      <div class="flex items-center gap-3">
        <button
          class="rounded-md bg-brand-600 px-3.5 py-1.5 text-sm font-medium text-white
                 transition-colors hover:bg-brand-700"
          @click="showUpload = !showUpload"
        >
          {{ showUpload ? '收起上传面板' : '+ 上传法规' }}
        </button>
        <button
          class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                 text-slate-600 transition-colors hover:bg-slate-50"
          @click="loadStatutes"
        >
          刷新
        </button>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <!-- 当前生效版本常驻可见。发布与回滚的全部意义都相对它而言，
             不显示它，两个按钮点下去就没有参照，用户只能凭记忆判断有没有生效 -->
        <span class="mr-1 text-right text-xs leading-tight text-slate-600">
          <span class="block">
            当前生效版本
            <span
              v-if="knowledgeVersion !== null"
              class="font-mono font-medium text-slate-700"
            >
              #{{ knowledgeVersion }}
            </span>
            <span v-else class="text-slate-600" title="本页尚未执行过发布或回滚">
              未知
            </span>
          </span>
          <span v-if="lastPublish" class="block text-slate-600">
            本次发布 {{ lastPublish.articleCount }} 条法条
          </span>
        </span>

        <template v-if="pendingAction">
          <span
            class="text-xs leading-relaxed"
            :class="pendingAction === 'rollback' ? 'text-risk-800' : 'text-slate-600'"
          >
            {{ pendingAction === 'publish'
              ? '发布后草稿法条立即对所有人生效，且无法撤销。确定？'
              : '回滚会让全平台退回上一版，且无法撤销。确定？' }}
          </span>
          <button
            :disabled="running !== null"
            class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white
                   transition-colors enabled:hover:bg-brand-700 disabled:bg-slate-300"
            @click="confirmPending"
          >
            {{ running === pendingAction ? '处理中…' : '确认' }}
          </button>
          <button
            :disabled="running !== null"
            class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                   text-slate-600 transition-colors enabled:hover:bg-slate-50
                   disabled:text-slate-300"
            @click="pendingAction = null"
          >
            取消
          </button>
        </template>

        <template v-else>
          <button
            class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                   text-slate-600 transition-colors hover:bg-slate-50"
            @click="pendingAction = 'publish'"
          >
            发布当前版本
          </button>
          <button
            class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                   text-slate-600 transition-colors hover:bg-slate-50"
            @click="pendingAction = 'rollback'"
          >
            回滚
          </button>
        </template>
      </div>
    </div>

    <div
      v-if="actionError"
      class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-risk-200
             bg-risk-50 px-3 py-2 text-xs text-risk-800"
    >
      <span>{{ actionError.message }}</span>
      <span v-if="actionError.traceId" class="font-mono text-risk-800">
        traceId：{{ actionError.traceId }}
      </span>
      <button
        type="button"
        class="ml-auto underline underline-offset-2"
        @click="actionError = null"
      >
        关闭
      </button>
    </div>

    <UploadPanel v-if="showUpload" @uploaded="trackJob" />

    <!-- 入库任务进度 -->
    <section v-if="job" class="card p-4">
      <div class="flex flex-wrap items-center gap-3">
        <StatusBadge :label="JOB_STATUS_LABEL[job.status]" :tone="JOB_TONE[job.status]" dot />
        <span class="text-sm text-slate-700">
          {{ job.statuteTitle ?? '入库任务' }}
        </span>
        <span class="text-xs text-slate-600">
          当前阶段：{{ JOB_STAGE_LABEL[job.stage] }}
        </span>
        <span
          v-if="job.lowConfidenceCount"
          class="text-xs text-caution-800"
        >
          {{ job.lowConfidenceCount }} 条低置信度项待复核（发布前必须处理）
        </span>
      </div>

      <!-- 进度条：写死一个"处理中"的动效会掩盖真实的卡顿 -->
      <div class="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          class="h-full rounded-full bg-brand-500 transition-all duration-300"
          :style="{ width: `${Math.min(100, Math.max(0, job.progress))}%` }"
        />
      </div>

      <p v-if="job.errorDetail" class="mt-2 text-xs leading-relaxed text-risk-800">
        {{ job.errorDetail }}
      </p>
      <p v-if="jobError" class="mt-2 text-xs text-caution-800">{{ jobError }}</p>
    </section>

    <!-- 筛选 -->
    <div class="flex flex-wrap items-center gap-3">
      <select
        v-model="filters.jurisdictionCode"
        class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
               focus-ring"
        @change="onFilterChange"
      >
        <option value="">全部法域</option>
        <option v-for="item in JURISDICTIONS" :key="item.code" :value="item.code">
          {{ item.label }}
        </option>
      </select>

      <select
        v-model="filters.publishStatus"
        class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
               focus-ring"
        @change="onFilterChange"
      >
        <option value="">全部状态</option>
        <option value="PUBLISHED">已发布</option>
        <option value="DRAFT">草稿</option>
        <option value="RETIRED">已废止</option>
      </select>

      <span class="text-xs text-slate-600">共 {{ total }} 部法规</span>
    </div>

    <!-- 列表 -->
    <StateBlock v-if="loading" variant="loading" title="正在加载法规列表" />
    <StateBlock
      v-else-if="loadError"
      variant="error"
      title="法规列表加载失败"
      :description="loadError.message"
      :trace-id="loadError.traceId"
      action-label="重试"
      @action="loadStatutes"
    />
    <StateBlock
      v-else-if="!statutes.length"
      title="知识库里还没有法规"
      description="先上传一份法规原文。上传后系统会自动解析、抽取本体并生成 wiki 词条，复核通过后即可在咨询中被检索到。"
      action-label="上传法规"
      @action="showUpload = true"
    />

    <div v-else class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-xs text-slate-600">
          <tr>
            <th class="px-4 py-2 text-left font-medium">法规</th>
            <th class="px-4 py-2 text-left font-medium">法域</th>
            <th class="px-4 py-2 text-left font-medium">版本</th>
            <th class="px-4 py-2 text-left font-medium">生效区间</th>
            <th class="px-4 py-2 text-left font-medium">状态</th>
            <th class="px-4 py-2 text-right font-medium">法条数</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <tr
            v-for="item in statutes"
            :key="item.id"
            class="cursor-pointer transition-colors hover:bg-slate-50"
            @click="selected = item"
          >
            <td class="px-4 py-3">
              <div class="font-medium text-slate-800">{{ item.title }}</div>
              <div v-if="item.titleOriginal" class="mt-0.5 text-xs text-slate-600">
                {{ item.titleOriginal }}
              </div>
            </td>
            <td class="px-4 py-3 text-slate-600">{{ jurisdictionLabel(item.jurisdictionCode) }}</td>
            <td class="px-4 py-3 text-slate-600">{{ item.versionLabel }}</td>
            <td class="px-4 py-3 text-slate-600">
              <span class="font-mono text-xs">
                {{ item.effectiveFrom }}
                {{ item.effectiveTo ? `→ ${item.effectiveTo}` : '→ 至今' }}
              </span>
              <!-- 精度必须标出来：爱尔兰的公开源只能确定到年，不标就是假装精确 -->
              <span v-if="item.datePrecision === 'YEAR'" class="ml-1 text-xs text-caution-800">
                （年份精度）
              </span>
            </td>
            <td class="px-4 py-3">
              <StatusBadge
                :label="STATUS_LABEL[item.publishStatus]"
                :tone="STATUS_TONE[item.publishStatus]"
              />
            </td>
            <td class="px-4 py-3 text-right font-mono text-slate-600">
              {{ item.articleCount }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页 -->
    <div v-if="totalPages > 1" class="flex items-center justify-end gap-2 text-sm">
      <button
        :disabled="page <= 1"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300"
        @click="page--; loadStatutes()"
      >
        上一页
      </button>
      <span class="text-xs text-slate-600">{{ page }} / {{ totalPages }}</span>
      <button
        :disabled="page >= totalPages"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300"
        @click="page++; loadStatutes()"
      >
        下一页
      </button>
    </div>

    <!-- 法规详情抽屉 -->
    <div
      v-if="selected"
      class="fixed inset-0 z-40 flex justify-end bg-slate-900/20"
      @click.self="selected = null"
    >
      <aside class="w-[460px] h-full overflow-auto border-l border-slate-200 bg-white">
        <header class="sticky top-0 flex items-start justify-between gap-4 border-b
                       border-slate-200 bg-white px-5 py-4">
          <div class="min-w-0">
            <h2 class="text-sm font-medium text-slate-800">{{ selected.title }}</h2>
            <p class="mt-0.5 text-xs text-slate-600">
              {{ jurisdictionLabel(selected.jurisdictionCode) }} · {{ selected.versionLabel }}
            </p>
          </div>
          <button
            class="shrink-0 rounded-md border border-slate-500 px-2 py-1 text-xs
                   text-slate-600 hover:bg-slate-50"
            @click="selected = null"
          >
            关闭
          </button>
        </header>

        <dl class="divide-y divide-slate-100 text-sm">
          <div class="flex justify-between gap-4 px-5 py-3">
            <dt class="text-slate-600">状态</dt>
            <dd>
              <StatusBadge
                :label="STATUS_LABEL[selected.publishStatus]"
                :tone="STATUS_TONE[selected.publishStatus]"
              />
            </dd>
          </div>
          <div class="flex justify-between gap-4 px-5 py-3">
            <dt class="text-slate-600">法条数</dt>
            <dd class="font-mono text-slate-700">{{ selected.articleCount }}</dd>
          </div>
          <div class="flex justify-between gap-4 px-5 py-3">
            <dt class="text-slate-600">生效区间</dt>
            <dd class="font-mono text-xs text-slate-700">
              {{ selected.effectiveFrom }}
              {{ selected.effectiveTo ? `→ ${selected.effectiveTo}` : '→ 至今' }}
            </dd>
          </div>
          <div v-if="selected.statuteNo" class="flex justify-between gap-4 px-5 py-3">
            <dt class="text-slate-600">法规编号</dt>
            <dd class="text-slate-700">{{ selected.statuteNo }}</dd>
          </div>
          <div v-if="selected.sourceUrl" class="px-5 py-3">
            <dt class="text-slate-600">公开来源</dt>
            <dd class="mt-1">
              <a
                :href="selected.sourceUrl"
                target="_blank"
                rel="noreferrer noopener"
                class="break-all text-xs text-brand-600 underline hover:text-brand-700"
              >
                {{ selected.sourceUrl }}
              </a>
            </dd>
          </div>
        </dl>

        <p class="px-5 py-4 text-xs leading-relaxed text-slate-600">
          条款级浏览通过咨询结论里的引用进入——引用可点击查看原文与层级路径。
          这样做的原因是：法条的价值在于"被哪条结论引用过"，脱离结论逐条翻阅并不能回答律师的问题。
        </p>
      </aside>
    </div>
  </div>
</template>

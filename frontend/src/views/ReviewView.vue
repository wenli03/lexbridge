<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { knowledgeApi, type ReviewDecision, type ReviewItem } from '@/api/knowledge'
import { isEndpointMissing } from '@/api/notImplemented'

import PlaceholderPanel from '@/components/PlaceholderPanel.vue'
import StateBlock from '@/components/StateBlock.vue'
import StatusBadge from '@/components/StatusBadge.vue'

/**
 * 入库复核页。
 *
 * 抽取是有置信度的，而**低置信度项在复核通过前不可能被检索到**
 * （它们以 `publish_status='DRAFT'` 落库，而检索恒定带 `PUBLISHED` 过滤）。
 * 因此这一页的职责不是"看着放心"，而是发布前的那道闸门。
 *
 * 左右对照的排布是刻意的：左边是**原文**，右边是**抽取结果**。
 * 复核的本质是核对这两者是否一致，把它们上下堆叠会让人来回滚动，
 * 而滚动一次就会漏看一行的错位。
 */

const items = ref<ReviewItem[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)
const loadError = ref<{ message: string; traceId?: string } | null>(null)
/** 服务端未实现（实测回 404）。是观察结果，接口一旦存在会自动恢复。 */
const serverMissing = ref(false)

const activeIndex = ref(0)
const submitting = ref(false)
const submitError = ref('')
const note = ref('')
const correctionDraft = ref('')
/** 已处理过的条目 id，用于列表上打勾而不是把它们从队列里移走 */
const handled = ref<Record<string, ReviewDecision>>({})

const active = computed<ReviewItem | null>(() => items.value[activeIndex.value] ?? null)

/** 修正框的示例。写成常量而不是内联，是为了避免属性里再嵌一层引号 */
const correctionPlaceholder = '{"taxType": "企业所得税"}'

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))

const highConfidenceIds = computed(() =>
  items.value.filter((item) => item.confidence >= 0.85).map((item) => item.itemId),
)

function confidenceTone(confidence: number): 'safe' | 'caution' | 'risk' {
  if (confidence >= 0.85) {
    return 'safe'
  }
  return confidence >= 0.6 ? 'caution' : 'risk'
}

function confidenceLabel(confidence: number): string {
  if (confidence >= 0.85) {
    return '高置信度'
  }
  return confidence >= 0.6 ? '需复核' : '低置信度'
}

const extractedText = computed(() => {
  if (!active.value) {
    return ''
  }
  return JSON.stringify(active.value.extractedValue, null, 2)
})

async function loadQueue(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const result = await knowledgeApi.reviewQueue({ page: page.value, size })
    items.value = result.items
    total.value = result.total
    activeIndex.value = 0
  } catch (error) {
    // 服务端没有这组路由时走说明面板，而不是红色的"加载失败"
    serverMissing.value = isEndpointMissing(error)
    const apiError = error instanceof ApiError ? error : null
    loadError.value = {
      message: apiError?.message ?? '复核队列加载失败',
      traceId: apiError?.traceId,
    }
  } finally {
    loading.value = false
  }
}

async function decide(item: ReviewItem, decision: ReviewDecision): Promise<void> {
  if (submitting.value) {
    return
  }
  submitError.value = ''

  let correction: Record<string, unknown> | undefined
  if (decision === 'CORRECTED') {
    try {
      correction = JSON.parse(correctionDraft.value || '{}') as Record<string, unknown>
    } catch {
      submitError.value = '修正内容不是合法 JSON，请检查后再提交。'
      return
    }
  }

  submitting.value = true
  try {
    await knowledgeApi.submitReview(item.itemId, {
      decision,
      correction,
      note: note.value || undefined,
    })
    handled.value = { ...handled.value, [item.itemId]: decision }
    note.value = ''
    correctionDraft.value = ''
    // 自动跳到下一条未处理的：复核是高频重复动作，让用户自己点"下一条"
    // 会在几百条的队列里变成纯粹的体力消耗
    const next = items.value.findIndex(
      (candidate, index) => index > activeIndex.value && !handled.value[candidate.itemId],
    )
    if (next !== -1) {
      activeIndex.value = next
    }
  } catch (error) {
    const apiError = error instanceof ApiError ? error : null
    submitError.value = apiError?.message ?? '提交复核结论失败'
  } finally {
    submitting.value = false
  }
}

async function confirmAllHighConfidence(): Promise<void> {
  const targets = items.value.filter(
    (item) => item.confidence >= 0.85 && !handled.value[item.itemId],
  )
  for (const item of targets) {
    try {
      await knowledgeApi.submitReview(item.itemId, { decision: 'CONFIRMED' })
      handled.value = { ...handled.value, [item.itemId]: 'CONFIRMED' }
    } catch {
      // 单条失败不中断批量：批量确认到一半停下比继续更有害，
      // 因为用户无法知道哪些成功了
      break
    }
  }
}

function move(delta: number): void {
  const next = activeIndex.value + delta
  if (next >= 0 && next < items.value.length) {
    activeIndex.value = next
    note.value = ''
    correctionDraft.value = ''
  }
}

/**
 * 键盘快捷键。
 *
 * 复核是几百条量级的高频重复动作，鼠标要在大半边屏幕之间来回移动，
 * 键盘能让单位时间处理量差出几倍。**界面上既然写了快捷键，就必须真的能用**——
 * 提示与实际不符比不提示更伤信任。
 */
function onKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null
  // 正在输入框里打字时不劫持按键，否则用户没法输入字母 a 或 r
  if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) {
    return
  }
  if (event.metaKey || event.ctrlKey || event.altKey || !active.value) {
    return
  }
  switch (event.key.toLowerCase()) {
    case 'j':
      move(1)
      break
    case 'k':
      move(-1)
      break
    case 'a':
      void decide(active.value, 'CONFIRMED')
      break
    case 'r':
      void decide(active.value, 'REJECTED')
      break
    default:
      break
  }
}

onMounted(() => {
  void loadQueue()
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm text-slate-700">
          共 {{ total }} 条待复核。低置信度项在复核通过前不会被检索到。
        </p>
        <p class="mt-0.5 text-xs text-slate-600">
          快捷键：J / K 切换条目，A 确认，R 驳回
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button
          class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                 text-slate-600 transition-colors hover:bg-slate-50"
          @click="confirmAllHighConfidence"
        >
          批量确认高置信度（{{ highConfidenceIds.length }}）
        </button>
        <button
          class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                 text-slate-600 transition-colors hover:bg-slate-50"
          @click="loadQueue"
        >
          刷新
        </button>
      </div>
    </header>

    <PlaceholderPanel
      v-if="serverMissing"
      phase="服务端未实现"
      summary="入库复核依赖知识入库流水线，而那条链路目前只有读的部分。复核队列与提交结论的接口都还不存在，因此这一页没有条目可看。"
      :points="[
        '缺 backend 端点：GET /api/knowledge/review 与 POST /api/knowledge/review/{itemId}',
        '缺发布与回滚：POST /api/knowledge/publish、/api/knowledge/rollback',
        'AI 侧的入库流水线（解析→切分→抽取→wiki→索引）目前只写了接口骨架，解析之后各步尚未实现',
        '表结构已就绪：kb.review_task、kb.ingestion_job、app.review_decision 已在 V3 迁移中建好',
        '本页的左右对照布局、置信度标注与提交逻辑均已完成，接口就位后即可工作',
      ]"
    />
    <StateBlock v-else-if="loading" variant="loading" title="正在加载复核队列" />
    <StateBlock
      v-else-if="loadError"
      variant="error"
      title="复核队列加载失败"
      :description="loadError.message"
      :trace-id="loadError.traceId"
      action-label="重试"
      @action="loadQueue"
    />
    <StateBlock
      v-else-if="!items.length"
      title="没有待复核的条目"
      description="上传法规后，置信度低于 0.85 的抽取结果会进入这里。队列为空说明当前没有需要人工确认的内容。"
    />

    <div v-else class="grid grid-cols-[220px_1fr] gap-5">
      <!-- 队列索引 -->
      <aside class="card max-h-[70vh] overflow-auto p-2">
        <button
          v-for="(item, index) in items"
          :key="item.itemId"
          class="block w-full rounded-md px-3 py-2 text-left text-sm transition-colors"
          :class="index === activeIndex ? 'bg-brand-50 text-brand-700' : 'hover:bg-slate-50'"
          @click="activeIndex = index"
        >
          <span class="flex items-center justify-between gap-2">
            <span class="truncate">{{ item.articleNo }}</span>
            <span
              v-if="handled[item.itemId]"
              class="shrink-0 text-xs text-safe-800"
              :title="handled[item.itemId]"
            >
              ✓
            </span>
          </span>
          <span class="mt-0.5 block truncate text-xs text-slate-600">
            {{ item.statuteTitle ?? '—' }}
          </span>
        </button>
      </aside>

      <!-- 对照区 -->
      <section v-if="active" class="space-y-4">
        <div class="flex flex-wrap items-center gap-3">
          <h2 class="text-sm font-medium text-slate-800">
            {{ active.articleNo }} · {{ active.statuteTitle }}
          </h2>
          <StatusBadge
            :label="`${confidenceLabel(active.confidence)} ${active.confidence.toFixed(2)}`"
            :tone="confidenceTone(active.confidence)"
            dot
          />
          <span v-if="active.fieldName" class="text-xs text-slate-600">
            字段：{{ active.fieldName }}
          </span>
        </div>

        <p class="text-xs text-slate-600">
          层级路径：{{ active.hierarchyPath.join(' › ') }}
        </p>

        <div class="grid grid-cols-2 gap-4">
          <div class="card overflow-hidden">
            <header class="border-b border-slate-200 bg-slate-50 px-4 py-2 text-xs
                           font-medium text-slate-600">
              条款原文
            </header>
            <pre class="max-h-[42vh] overflow-auto whitespace-pre-wrap px-4 py-3
                        font-mono text-xs leading-relaxed text-slate-700">{{ active.originalText }}</pre>
          </div>

          <div class="card overflow-hidden">
            <header class="border-b border-slate-200 bg-slate-50 px-4 py-2 text-xs
                           font-medium text-slate-600">
              抽取结果
            </header>
            <pre class="max-h-[42vh] overflow-auto whitespace-pre-wrap px-4 py-3
                        font-mono text-xs leading-relaxed text-slate-700">{{ extractedText }}</pre>
          </div>
        </div>

        <div class="card space-y-3 p-4">
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-600" for="rv-note">
              复核批注
            </label>
            <input
              id="rv-note"
              v-model="note"
              type="text"
              class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                     focus-ring"
              placeholder="例如：条号切分正确，但税种字段应为企业所得税"
            />
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-600" for="rv-correction">
              修正后的值（提交「修正」时必填，JSON）
            </label>
            <textarea
              id="rv-correction"
              v-model="correctionDraft"
              rows="3"
              class="w-full resize-y rounded-md border border-slate-500 px-3 py-2
                     font-mono text-xs focus-ring"
              :placeholder="correctionPlaceholder"
            />
          </div>

          <p v-if="submitError" class="text-xs text-risk-800">{{ submitError }}</p>

          <div class="flex flex-wrap items-center gap-2">
            <!-- 白字落在实心色上的另一处：safe-500 只有 3.42:1，下沉一档到 safe-600（4.56:1）。
                 hover 的 safe-700 不动，正好成了一个"再深一档"的反馈 -->
            <button
              :disabled="submitting"
              class="rounded-md bg-safe-600 px-3.5 py-1.5 text-sm font-medium text-white
                     transition-colors enabled:hover:bg-safe-700 disabled:bg-slate-300"
              @click="decide(active, 'CONFIRMED')"
            >
              确认
            </button>
            <button
              :disabled="submitting"
              class="rounded-md border border-slate-500 bg-white px-3.5 py-1.5 text-sm
                     text-slate-700 transition-colors enabled:hover:bg-slate-50
                     disabled:text-slate-300"
              @click="decide(active, 'CORRECTED')"
            >
              修正
            </button>
            <button
              :disabled="submitting"
              class="rounded-md border border-risk-200 bg-risk-50 px-3.5 py-1.5 text-sm
                     font-medium text-risk-800 transition-colors enabled:hover:bg-risk-100"
              @click="decide(active, 'REJECTED')"
            >
              驳回
            </button>
            <span class="ml-auto flex items-center gap-2">
              <button
                class="rounded-md border border-slate-500 px-2.5 py-1 text-xs text-slate-600
                       transition-colors hover:bg-slate-50"
                @click="move(-1)"
              >
                上一条
              </button>
              <button
                class="rounded-md border border-slate-500 px-2.5 py-1 text-xs text-slate-600
                       transition-colors hover:bg-slate-50"
                @click="move(1)"
              >
                下一条
              </button>
            </span>
          </div>

          <p class="text-xs leading-relaxed text-slate-600">
            「驳回」不会删除法条：该条会保持 DRAFT 状态而不可检索，且驳回原因进入审计。
            这样可以事后回答"这条为什么没进生产库"。
          </p>
        </div>
      </section>
    </div>

    <div v-if="!loading && totalPages > 1" class="flex items-center justify-end gap-2 text-sm">
      <button
        :disabled="page <= 1"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300"
        @click="page--; loadQueue()"
      >
        上一页
      </button>
      <span class="text-xs text-slate-600">{{ page }} / {{ totalPages }}</span>
      <button
        :disabled="page >= totalPages"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300"
        @click="page++; loadQueue()"
      >
        下一页
      </button>
    </div>
  </div>
</template>

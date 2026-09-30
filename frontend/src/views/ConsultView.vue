<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { isEndpointMissing } from '@/api/notImplemented'
import {
  CONSULT_TYPE_HINT,
  CONSULT_TYPE_LABEL,
  consultApi,
  type ConsultSession,
  type ConsultType,
  type ResultCitation,
  type RunResult,
} from '@/api/consult'
import { knowledgeApi, type ArticleDetail } from '@/api/knowledge'
import type { AnySseEvent, DegradedEvent, InterruptEvent, RedlineEvent } from '@/api/sse'

import CitationList from '@/components/CitationList.vue'
import ConsultAnswer from '@/components/ConsultAnswer.vue'
import DegradationNotice from '@/components/DegradationNotice.vue'
import InterruptForm from '@/components/InterruptForm.vue'
import PlaceholderPanel from '@/components/PlaceholderPanel.vue'
import RedlineNotice from '@/components/RedlineNotice.vue'
import StateBlock from '@/components/StateBlock.vue'
import StatusBadge from '@/components/StatusBadge.vue'

/**
 * 法律咨询页。
 *
 * 一次咨询在界面上的生命周期：
 *   提交问题 → 建 run → 打开 SSE 流 → 逐事件渲染 → 流结束
 *   结束的几种形态都要正确收尾：`done`（有结论）、`interrupt`（要补充信息）、
 *   `error`（失败）、`redline`（拒答，会话继续）、`degraded`（降级，有文字但无依据）。
 *
 * **拒答与会话中断是两件事。** 命中红线时输入框仍然可用——用户可以换个问法继续问。
 * 一个"问错一次就锁死会话"的界面，会让人不敢提问，而不敢提问的合规工具没有价值。
 *
 * **降级与拒答也是两件事。** 拒答是"这个问题不能答"，降级是"这次没能查"。
 * 降级时后端照样会推正文，所以提示条必须在正文流出来的同时就出现，
 * 否则用户会把没有依据的文字读完。
 */

interface ThreadEntry {
  runId: string
  question: string
  type: ConsultType
  streaming: boolean
  text: string
  nodes: string[]
  /**
   * 引用用 `ResultCitation`（`similarity` 可为空）而不是流事件里的
   * `CitationEvent`（`similarity` 必填）：同一段引用在"实时推送"和"回看历史"
   * 两个来源下字段完整度不同，取更宽的那个，否则回看历史时会类型不兼容。
   */
  citations: ResultCitation[]
  redline?: RedlineEvent
  /**
   * 来自 SSE 的降级标记。它先于结构化结果到达，用来在正文还在流式输出时
   * 就把提示条顶上去；结构化结果里也带一份（回看历史时没有事件流），
   * 见 `loadResult` 里两者的合并。
   */
  degradation?: DegradedEvent
  interrupt?: InterruptEvent
  errorMessage?: string
  errorTraceId?: string
  result?: RunResult
}

const route = useRoute()
const router = useRouter()

const sessions = ref<ConsultSession[]>([])
const sessionsLoading = ref(false)
const sessionsError = ref<{ message: string; traceId?: string } | null>(null)
/**
 * 服务端未实现这组接口（实测回 404）。
 *
 * 用 ref 而不是常量：它是**观察到的结果**而不是声明。
 * 接口一旦实现，这里自动变 false，页面恢复正常，不需要回来改代码。
 */
const serverMissing = ref(false)

const activeSessionId = ref<string | null>(null)
const thread = ref<ThreadEntry[]>([])
const streaming = ref(false)
const draft = ref('')
const consultType = ref<ConsultType>('TAX_PLANNING')

const article = ref<ArticleDetail | null>(null)
const articleLoading = ref(false)

let closeStream: (() => void) | null = null

const activeSession = computed(
  () => sessions.value.find((s) => s.id === activeSessionId.value) ?? null,
)

async function loadSessions(): Promise<void> {
  sessionsLoading.value = true
  sessionsError.value = null
  try {
    const page = await consultApi.sessions({ page: 1, size: 20 })
    sessions.value = page.items
  } catch (error) {
    // 服务端没有这组路由时走说明面板，而不是一个红色的"加载失败"：
    // 前者说"这一块还没做"，后者说"这一块坏了"，而事实是前者。
    serverMissing.value = isEndpointMissing(error)
    const apiError = error instanceof ApiError ? error : null
    sessionsError.value = {
      message: apiError?.message ?? '会话列表加载失败',
      traceId: apiError?.traceId,
    }
  } finally {
    sessionsLoading.value = false
  }
}

function startNewSession(): void {
  closeStream?.()
  closeStream = null
  streaming.value = false
  activeSessionId.value = null
  thread.value = []
}

async function ensureSession(question: string, type: ConsultType): Promise<string> {
  if (activeSessionId.value) {
    return activeSessionId.value
  }
  const session = await consultApi.createSession({
    title: question.slice(0, 30),
    type,
  })
  sessions.value = [session, ...sessions.value]
  activeSessionId.value = session.id
  return session.id
}

function pushEvent(entry: ThreadEntry, event: AnySseEvent): void {
  switch (event.event) {
    case 'node_start': {
      const data = event.data as { node: string }
      if (!entry.nodes.includes(data.node)) {
        entry.nodes.push(data.node)
      }
      break
    }
    case 'token': {
      entry.text += (event.data as { text: string }).text
      break
    }
    case 'citation': {
      entry.citations.push(event.data as ResultCitation)
      break
    }
    case 'redline': {
      entry.redline = event.data as RedlineEvent
      break
    }
    case 'degraded': {
      // 立刻置上：提示条要赶在正文之前被读到，不能等 `done` 之后再补
      entry.degradation = event.data as DegradedEvent
      break
    }
    case 'interrupt': {
      entry.interrupt = event.data as InterruptEvent
      break
    }
    case 'error': {
      const data = event.data as { message: string; code: string }
      entry.errorMessage = `[${data.code}] ${data.message}`
      break
    }
    default:
      // done / progress / 未知事件：不影响增量渲染
      break
  }
}

function finishEntry(entry: ThreadEntry): void {
  entry.streaming = false
  streaming.value = false
}

/**
 * 发起一轮咨询。
 *
 * `preset` 供降级提示条上的"重试本次检索"使用：重试要重放的是**当时那个问题**，
 * 而不是输入框里现在的草稿——用户可能已经改了草稿，重试却悄悄问了别的问题，
 * 而这种错位在界面上完全看不出来。
 */
async function ask(preset?: string): Promise<void> {
  const question = (preset ?? draft.value).trim()
  if (!question || streaming.value) {
    return
  }

  const entry: ThreadEntry = {
    runId: '',
    question,
    type: consultType.value,
    streaming: true,
    text: '',
    nodes: [],
    citations: [],
  }

  streaming.value = true
  if (preset === undefined) {
    // 只有从输入框发起才清空草稿；重试没有消耗草稿，替用户删字是越权
    draft.value = ''
  }
  thread.value = [...thread.value, entry]

  try {
    const sessionId = await ensureSession(question, consultType.value)
    const run = await consultApi.startRun(sessionId, {
      question,
      type: consultType.value,
    })
    entry.runId = run.runId
    // 重新赋值以触发引用型响应式更新（entry 是数组里的对象，直接改字段已能追踪，
    // 但 runId 是模板里用作 key 的字段，显式替换更稳妥）
    thread.value = [...thread.value]

    closeStream = consultApi.streamRun(sessionId, run.runId, {
      onEvent: (event) => {
        pushEvent(entry, event)
        if (event.event === 'done' || event.event === 'interrupt' || event.event === 'error') {
          finishEntry(entry)
          if (event.event === 'done') {
            void loadResult(sessionId, entry)
          }
        }
      },
      onError: (error: unknown) => {
        entry.errorMessage = error instanceof ApiError ? error.message : '流式响应失败'
        entry.errorTraceId = error instanceof ApiError ? error.traceId : undefined
        finishEntry(entry)
      },
      onClose: () => finishEntry(entry),
    })
  } catch (error) {
    entry.errorMessage = error instanceof ApiError ? error.message : '发起咨询失败'
    entry.errorTraceId = error instanceof ApiError ? error.traceId : undefined
    finishEntry(entry)
  }
}

/**
 * `done` 之后取结构化结果。
 *
 * 取不到不算失败：逐字输出已经给用户看了内容，只是表格/矩阵缺失。
 * 把这种情况显示成"结论加载失败"要比"整次咨询失败"诚实得多。
 */
async function loadResult(sessionId: string, entry: ThreadEntry): Promise<void> {
  try {
    const detail = await consultApi.session(sessionId)
    const run = detail.runs.find((r) => r.runId === entry.runId)
    const result = run?.result
    if (!result) {
      return
    }
    // 降级标记与红线拒答在事件流与结构化结果里各有一份。后端可能只实现了其中一边，
    // 只认一边的话，提示条会在结果落地的那一刻凭空消失——而这恰恰是
    // 用户准备把正文当结论读的时刻。两处都取有的那一份，与引用的双来源处理同理。
    entry.result = {
      ...result,
      degradation: result.degradation ?? entry.degradation,
      redline: result.redline ?? entry.redline,
    }
  } catch {
    /* 结构化结果缺失时保留逐字输出，不覆盖成错误 */
  }
}

function resume(entry: ThreadEntry, slots: Record<string, string>): void {
  const sessionId = activeSessionId.value
  if (!sessionId || !entry.interrupt) {
    return
  }
  const interruptId = entry.interrupt.interruptId
  entry.interrupt = undefined
  entry.streaming = true
  streaming.value = true

  closeStream = consultApi.resumeRun(
    sessionId,
    entry.runId,
    { interruptId, slots },
    {
      onEvent: (event) => {
        pushEvent(entry, event)
        if (event.event === 'done' || event.event === 'interrupt' || event.event === 'error') {
          finishEntry(entry)
          if (event.event === 'done') {
            void loadResult(sessionId, entry)
          }
        }
      },
      onError: (error: unknown) => {
        entry.errorMessage = error instanceof ApiError ? error.message : '恢复失败'
        entry.errorTraceId = error instanceof ApiError ? error.traceId : undefined
        finishEntry(entry)
      },
      onClose: () => finishEntry(entry),
    },
  )
}

async function openArticle(articleId: string): Promise<void> {
  articleLoading.value = true
  article.value = null
  try {
    article.value = await knowledgeApi.article(articleId)
  } catch {
    article.value = null
  } finally {
    articleLoading.value = false
  }
}

function closeArticle(): void {
  article.value = null
}

/**
 * 打开一次历史咨询。
 *
 * `fallbackType` 只在从「会话历史」页跳进来时可能缺省——那时手里只有 id。
 * 真正的类型以详情返回的 `detail.type` 为准：会话类型创建后不可改，
 * 两者本不会冲突，但以服务端为准可以挡住"上一个页面把类型传错了"
 * 这种静默偏差——那样用户会看到筛选条件与内容对不上，却查不出原因。
 */
async function openSessionById(
  sessionId: string,
  fallbackType?: ConsultType,
): Promise<void> {
  closeStream?.()
  closeStream = null
  streaming.value = false
  activeSessionId.value = sessionId
  if (fallbackType) {
    consultType.value = fallbackType
  }

  try {
    const detail = await consultApi.session(sessionId)
    consultType.value = detail.type
    // 从历史页跳进来时，这一条不一定在左栏已加载的那 20 条里。
    // 不补进去的话，左栏没有任何一项高亮、顶部那行「当前会话基于知识版本 #N」
    // 也会消失——用户明明是"打开了一条记录"，界面却表现得像没有会话。
    // 补进去的是服务端刚返回的真实数据，不是造的。
    if (!sessions.value.some((s) => s.id === detail.id)) {
      sessions.value = [detail, ...sessions.value]
    }
    thread.value = detail.runs.map((run) => ({
      runId: run.runId,
      question: run.question,
      type: detail.type,
      streaming: false,
      text: run.result?.answer ?? '',
      nodes: [],
      citations: run.result?.citations ?? [],
      // 回看历史时没有事件流，红线拒答只能从结构化结果里读——
      // 而拒答复核恰恰主要发生在回看的时候
      redline: run.result?.redline,
      result: run.result,
    }))
  } catch {
    thread.value = []
  }
}

function openSession(session: ConsultSession): void {
  void openSessionById(session.id, session.type)
}

onMounted(async () => {
  await loadSessions()
  // 从「会话历史」页跳进来时带着 `?session=<id>`：**历史页负责找，咨询页负责用。**
  // 让历史页内联展开一份对话，等于把这一页抄第二遍，两份实现随后必然走样。
  const target = route.query.session
  if (typeof target === 'string' && target) {
    await openSessionById(target)
    // 消费掉就从地址栏抹掉。否则用户接着点「新建咨询」，地址里仍挂着上一条
    // 会话的 id，一刷新又跳回那条记录，而此刻界面上明明是一个空会话。
    void router.replace({ name: 'consult' })
  }
})

onBeforeUnmount(() => {
  // 组件卸载时必须断开：否则用户离开页面后连接仍挂着，服务端会继续推给一个没人看的流
  closeStream?.()
})
</script>

<template>
  <!--
    服务端未实现这组接口时，整页换成说明面板。

    整页而不是只在侧栏提示：这一页的主区域同样依赖后端（会话详情、SSE 流），
    留着半页可点、点了必然失败的控件，比直接说清楚更让人困惑。
  -->
  <PlaceholderPanel
    v-if="serverMissing"
    phase="服务端未实现"
    summary="「法律咨询」的前端页面、SSE 事件协议、以及 AI 侧的税务筹划图都已完成；缺的是把三者接起来的服务端那一层。因此这一页现在只能看到这段说明，而不是一个能用的咨询界面。"
    :points="[
      '缺 ai 侧端点：/internal/graph/run、/internal/graph/resume、/internal/retrieval/search',
      '缺 backend 侧端点：/api/consult-sessions 共 5 个（含 SSE 转发）',
      '缺会话与运行记录的表结构（详细设计引用了 consult_run，但未给出 DDL）',
      'AI 侧的税务筹划图本身是完整的，已有 25 条离线测试覆盖它',
      '这不是缺陷报告，是分阶段交付的现状；README 的「已知边界」表是权威口径',
    ]"
  />

  <div v-else class="flex gap-6 h-full min-h-0">
    <!-- 会话列表 -->
    <aside class="w-64 shrink-0 flex flex-col gap-3">
      <button
        class="w-full rounded-md border border-slate-500 bg-white px-3 py-2 text-sm
               text-slate-700 transition-colors hover:bg-slate-50"
        @click="startNewSession"
      >
        + 新建咨询
      </button>

      <div class="flex-1 min-h-0 overflow-auto space-y-1.5">
        <StateBlock
          v-if="sessionsLoading"
          variant="loading"
          title="正在加载会话"
          compact
        />
        <StateBlock
          v-else-if="sessionsError"
          variant="error"
          title="会话列表加载失败"
          :description="sessionsError.message"
          :trace-id="sessionsError.traceId"
          action-label="重试"
          compact
          @action="loadSessions"
        />
        <StateBlock
          v-else-if="!sessions.length"
          title="还没有咨询记录"
          description="右侧输入一个问题即可开始。"
          compact
        />
        <template v-else>
          <button
            v-for="session in sessions"
            :key="session.id"
            class="w-full rounded-md border px-3 py-2 text-left transition-colors"
            :class="
              session.id === activeSessionId
                ? 'border-brand-200 bg-brand-50'
                : 'border-slate-200 bg-white hover:bg-slate-50'
            "
            @click="openSession(session)"
          >
            <span class="block truncate text-sm text-slate-800">{{ session.title }}</span>
            <span class="mt-0.5 block text-xs text-slate-600">
              {{ CONSULT_TYPE_LABEL[session.type] }} · {{ session.runCount }} 轮
            </span>
          </button>
        </template>
      </div>
    </aside>

    <!-- 对话区 -->
    <div class="flex-1 min-w-0 flex flex-col gap-4">
      <header class="card px-4 py-3">
        <div class="flex flex-wrap items-center gap-2">
          <button
            v-for="type in (['TAX_PLANNING', 'DIVERGENCE'] as ConsultType[])"
            :key="type"
            class="rounded-md border px-3 py-1.5 text-sm transition-colors"
            :class="
              consultType === type
                ? 'border-brand-300 bg-brand-50 text-brand-700 font-medium'
                : 'border-slate-500 bg-white text-slate-600 hover:bg-slate-50'
            "
            @click="consultType = type"
          >
            {{ CONSULT_TYPE_LABEL[type] }}
          </button>
          <span class="text-xs text-slate-600">{{ CONSULT_TYPE_HINT[consultType] }}</span>
        </div>
        <p class="mt-2 text-xs text-slate-600">
          知识截止日期以当前已发布版本为准
          <template v-if="activeSession?.knowledgeVersion">
            （当前会话基于知识版本 #{{ activeSession.knowledgeVersion }}）
          </template>
          · 结论仅供参考，不构成法律意见
        </p>
      </header>

      <div class="flex-1 min-h-0 overflow-auto space-y-5 pr-1">
        <StateBlock
          v-if="!thread.length"
          title="开始一次跨境法律咨询"
          description="选择上方的咨询类型后，在下方描述你的业务场景。信息不足时系统会先追问，而不是基于猜测给出方案。"
        />

        <section v-for="entry in thread" :key="entry.runId || entry.question" class="space-y-3">
          <div class="flex justify-end">
            <div class="max-w-[80%] rounded-lg bg-brand-600 px-4 py-2.5 text-sm text-white">
              {{ entry.question }}
            </div>
          </div>

          <RedlineNotice
            v-if="entry.redline"
            :rule-id="entry.redline.ruleId"
            :category="entry.redline.category"
            :reason="entry.redline.message"
            :alternatives="entry.redline.legalAlternatives ?? []"
            :refused="entry.redline.refused"
            :refused-why="entry.redline.refusedWhy"
            :boundary="entry.redline.boundary"
            :rule-refs="entry.redline.ruleRefs"
            @ask="ask($event)"
          />

          <!-- 降级提示条要在流式阶段就顶上：此时结构化结果还没到，但正文已经在往外流了，
               而这段正文没有引用支撑。等 `done` 之后再补提示，用户早就读完了。
               结果落地后改由 ConsultAnswer 渲染同一条提示，这里让位，避免出现两条。 -->
          <DegradationNotice
            v-if="entry.degradation && !entry.result"
            :degradation="entry.degradation"
            @retry="ask(entry.question)"
          />

          <!-- 逐字输出卡在结构化结果到达后收起。两者的正文本是同一段
               （历史会话里 `text` 就直接取自 `result.answer`），同时留在页面上
               等于把结论念两遍；更要紧的是 `result.answer` 是过了引用校验的版本，
               挂不上来源的句子已被剥离（G1.3），继续显示流式原文会把已剥离的句子
               留在用户眼前。 -->
          <div v-if="(entry.text || entry.streaming) && !entry.result" class="card p-4">
            <div class="mb-2 flex items-center gap-2">
              <StatusBadge
                v-if="entry.streaming"
                label="生成中"
                tone="brand"
                dot
              />
              <span v-if="entry.nodes.length" class="text-xs text-slate-600">
                已完成：{{ entry.nodes.join(' → ') }}
              </span>
            </div>
            <p class="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
              {{ entry.text }}
              <span v-if="entry.streaming" class="ml-0.5 animate-pulse text-brand-500">▌</span>
            </p>
          </div>

          <InterruptForm
            v-if="entry.interrupt"
            :interrupt="entry.interrupt"
            @submit="resume(entry, $event)"
          />

          <ConsultAnswer
            v-if="entry.result"
            :result="entry.result"
            @select-citation="openArticle"
            @retry="ask(entry.question)"
          />

          <div v-if="entry.citations.length && !entry.result" class="card px-4 py-3">
            <p class="mb-2 text-xs text-slate-600">本次结论引用的法条</p>
            <CitationList :citations="entry.citations" @select="openArticle" />
          </div>

          <StateBlock
            v-if="entry.errorMessage"
            variant="error"
            title="本次咨询未能完成"
            :description="entry.errorMessage"
            :trace-id="entry.errorTraceId"
            compact
          />
        </section>
      </div>

      <footer class="card p-3">
        <!-- 必须写成 `ask()` 而不是 `ask`：`ask` 现在收一个可选的 preset，
             直接绑函数会把 submit 事件当成 preset 传进去 -->
        <form class="flex items-end gap-3" @submit.prevent="ask()">
          <textarea
            v-model="draft"
            rows="2"
            :disabled="streaming"
            class="flex-1 resize-none rounded-md border border-slate-500 px-3 py-2 text-sm
                   text-slate-800 placeholder:text-slate-600
                   focus-ring
                   disabled:bg-slate-50 disabled:text-slate-400"
            placeholder="例如：我们计划在新加坡设立控股公司，持有荷兰与开曼的两家运营主体，希望比较不同架构的整体税负与合规风险。"
          />
          <button
            type="submit"
            :disabled="streaming || !draft.trim()"
            class="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white
                   transition-colors enabled:hover:bg-brand-700
                   disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {{ streaming ? '生成中' : '提问' }}
          </button>
        </form>
      </footer>
    </div>

    <!-- 法条原文抽屉 -->
    <div
      v-if="article || articleLoading"
      class="fixed inset-0 z-40 flex justify-end bg-slate-900/20"
      @click.self="closeArticle"
    >
      <aside class="w-[520px] h-full overflow-auto border-l border-slate-200 bg-white">
        <header class="sticky top-0 flex items-start justify-between gap-4 border-b
                       border-slate-200 bg-white px-5 py-4">
          <div class="min-w-0">
            <h2 class="truncate text-sm font-medium text-slate-800">
              {{ article?.articleNo ?? '正在加载法条' }}
            </h2>
            <p class="mt-0.5 text-xs text-slate-600">
              {{ article?.statuteTitle }}
            </p>
          </div>
          <button
            class="shrink-0 rounded-md border border-slate-500 px-2 py-1 text-xs
                   text-slate-600 hover:bg-slate-50"
            @click="closeArticle"
          >
            关闭
          </button>
        </header>

        <div v-if="articleLoading" class="p-5">
          <StateBlock variant="loading" title="正在读取法条原文" compact />
        </div>
        <div v-else-if="article" class="space-y-4 p-5">
          <div>
            <p class="text-xs text-slate-600">层级路径</p>
            <p class="mt-1 text-xs text-slate-600">
              {{ article.hierarchyPath.join(' › ') }}
            </p>
          </div>
          <div>
            <p class="text-xs text-slate-600">原文</p>
            <p class="mt-1 whitespace-pre-wrap font-mono text-sm leading-relaxed text-slate-700">
              {{ article.content }}
            </p>
          </div>
          <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
            <span>生效 {{ article.effectiveFrom }}</span>
            <span>{{ article.effectiveTo ? `至 ${article.effectiveTo}` : '至今有效' }}</span>
            <span v-if="article.confidence !== null && article.confidence !== undefined">
              抽取置信度 {{ article.confidence.toFixed(2) }}
            </span>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

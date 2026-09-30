<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { isEndpointMissing } from '@/api/notImplemented'
import {
  CONSULT_TYPE_LABEL,
  consultApi,
  runStatusLabel,
  type ConsultSession,
  type ConsultType,
  type RunStatus,
} from '@/api/consult'

import PlaceholderPanel from '@/components/PlaceholderPanel.vue'
import StateBlock from '@/components/StateBlock.vue'
import StatusBadge from '@/components/StatusBadge.vue'

/**
 * 会话历史页。
 *
 * **它和咨询页左栏那份列表不是一回事，不要合并。**
 * 左栏是切换器：20 条、不翻页、不筛选，回答的是「回到刚才那一次」。
 * 这里是记录：翻页、按类型与状态筛，回答的是「上个月那次差异分析在哪」
 * 和「哪几次被拒了」。把筛选堆进左栏，会让提问题的地方变成管理界面的地方。
 *
 * **两边的状态口径必须一致。** 红线拒答用 caution 而不是 risk：拒答是系统
 * 按设计完成的工作，不是故障——这与 `AuditView` 对 `REDLINE_REFUSAL`
 * 的处理是同一条理由。同一件事在审计页和历史页显示成两种颜色，
 * 会让人以为它们是两种不同的结果。
 *
 * **这一页只负责找，不负责读。** 点开一条会跳到咨询页的对应会话，
 * 而不是在列表里内联展开对话——那等于把咨询页抄第二遍，两份实现随后必然走样。
 */

const router = useRouter()

const sessions = ref<ConsultSession[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)
const loadError = ref<{ message: string; traceId?: string } | null>(null)
/** 服务端未实现（实测回 404）。是观察结果，接口一旦存在会自动恢复。 */
const serverMissing = ref(false)

const filters = ref<{
  q: string
  type: ConsultType | ''
  status: RunStatus | ''
  from: string
  to: string
}>({
  q: '',
  type: '',
  status: '',
  from: '',
  to: '',
})

/**
 * 状态到语义色的映射。
 *
 * `INTERRUPTED` 与 `RUNNING` 同用 brand：两者都是「这次还没结束」，
 * 一个是系统在跑、一个是等你补料，标签本身已经把区别说清楚了。
 * 把 `INTERRUPTED` 单独染成 caution 会让它和 `REFUSED` 撞色，
 * 而这两件事的下一步动作完全相反——一个是接着答，一个是换个问法。
 */
const STATUS_TONE: Record<RunStatus, 'safe' | 'caution' | 'risk' | 'brand'> = {
  RUNNING: 'brand',
  INTERRUPTED: 'brand',
  COMPLETED: 'safe',
  REFUSED: 'caution',
  FAILED: 'risk',
}

const hasFilters = computed(
  () =>
    Boolean(filters.value.q.trim()) ||
    Boolean(filters.value.type) ||
    Boolean(filters.value.status) ||
    Boolean(filters.value.from) ||
    Boolean(filters.value.to),
)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))

async function loadSessions(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const result = await consultApi.sessions({
      page: page.value,
      size,
      q: filters.value.q.trim() || undefined,
      type: filters.value.type || undefined,
      status: filters.value.status || undefined,
      from: filters.value.from || undefined,
      to: filters.value.to || undefined,
    })
    sessions.value = result.items
    total.value = result.total
  } catch (error) {
    // 服务端没有这组路由时走说明面板，而不是红色的"加载失败"
    serverMissing.value = isEndpointMissing(error)
    const apiError = error instanceof ApiError ? error : null
    loadError.value = {
      message: apiError?.message ?? '会话历史加载失败',
      traceId: apiError?.traceId,
    }
  } finally {
    loading.value = false
  }
}

function onFilterChange(): void {
  page.value = 1
  void loadSessions()
}

function resetFilters(): void {
  filters.value = { q: '', type: '', status: '', from: '', to: '' }
  onFilterChange()
}

/**
 * 打开一条历史咨询。
 *
 * 交给咨询页而不是在这里渲染：会话是要**接着往下问**的，
 * 读数只是它的第一步。跳过去之后输入框、追问表单、检查点恢复都在那儿，
 * 历史页不必再实现一遍。
 */
function openSession(session: ConsultSession): void {
  void router.push({ name: 'consult', query: { session: session.id } })
}

function goConsult(): void {
  void router.push({ name: 'consult' })
}

/**
 * 时间显示到分钟，不显示秒。
 *
 * 审计页要秒是因为同一秒内的多条记录要靠时间判断先后；
 * 这里扫的是「哪一天、大概什么时候」，秒纯属噪声。
 */
function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    return iso
  }
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

onMounted(() => {
  void loadSessions()
})
</script>

<template>
  <div class="space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm text-slate-700">共 {{ total }} 次咨询</p>
        <p class="mt-0.5 text-xs text-slate-600">
          点任意一条可回到咨询页继续追问。被拒答的会话没有被锁死——换个问法还能接着问。
        </p>
      </div>
      <button
        class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
               text-slate-600 transition-colors hover:bg-slate-50 focus-ring"
        @click="loadSessions"
      >
        刷新
      </button>
    </header>

    <!-- 筛选 -->
    <div class="card flex flex-wrap items-end gap-3 p-4">
      <div class="w-full sm:w-auto sm:flex-1">
        <label class="mb-1 block text-xs font-medium text-slate-600" for="hs-q">
          关键词
        </label>
        <input
          id="hs-q"
          v-model="filters.q"
          type="search"
          class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          placeholder="按咨询标题查找，例如 控股架构"
          @keyup.enter="onFilterChange"
        />
      </div>

      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="hs-type">
          咨询类型
        </label>
        <select
          id="hs-type"
          v-model="filters.type"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        >
          <option value="">全部</option>
          <option value="TAX_PLANNING">跨境税务筹划</option>
          <option value="DIVERGENCE">监管差异分析</option>
        </select>
      </div>

      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="hs-status">
          状态
        </label>
        <select
          id="hs-status"
          v-model="filters.status"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        >
          <option value="">全部</option>
          <option value="COMPLETED">已完成</option>
          <option value="INTERRUPTED">待补充信息</option>
          <option value="REFUSED">红线拒答</option>
          <option value="FAILED">失败</option>
        </select>
      </div>

      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="hs-from">起始日期</label>
        <input
          id="hs-from"
          v-model="filters.from"
          type="date"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        />
      </div>
      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="hs-to">结束日期</label>
        <input
          id="hs-to"
          v-model="filters.to"
          type="date"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        />
      </div>

      <button
        class="ml-auto rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
               text-slate-600 transition-colors hover:bg-slate-50 focus-ring"
        @click="resetFilters"
      >
        重置
      </button>
    </div>

    <PlaceholderPanel
      v-if="serverMissing"
      phase="服务端未实现"
      summary="会话历史读的是咨询链路的会话表，而那条链路的服务端尚未实现。这一页因此没有数据可读——不是查询出错，是表与接口都还不存在。"
      :points="[
        '依赖 backend 的 GET /api/consult-sessions（含分页与筛选），该端点未实现',
        '依赖会话表的持久化，其建表迁移（V4）尚未编写',
        '本页的筛选、排序、分页逻辑已按接口契约写好，后端就位后即可工作',
      ]"
    />
    <StateBlock v-else-if="loading" variant="loading" title="正在加载会话历史" />
    <StateBlock
      v-else-if="loadError"
      variant="error"
      title="会话历史加载失败"
      :description="loadError.message"
      :trace-id="loadError.traceId"
      action-label="重试"
      @action="loadSessions"
    />
    <!-- 空态分两种。筛出来是空的和一条都没有，下一步动作完全不同：
         前者要放宽条件，后者要去提第一个问题。合成一句「暂无数据」，
         用户会以为自己筛错了。 -->
    <StateBlock
      v-else-if="!sessions.length && hasFilters"
      title="没有符合条件的咨询"
      description="换一个关键词、把类型或状态放宽，或清空筛选查看全部记录。"
      action-label="重置筛选"
      @action="resetFilters"
    />
    <StateBlock
      v-else-if="!sessions.length"
      title="还没有咨询记录"
      description="去「法律咨询」提出第一个问题，这里会按时间留下记录，便于日后检索与复核。"
      action-label="去提问"
      @action="goConsult"
    />

    <div v-else class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-xs text-slate-600">
          <tr>
            <th class="px-4 py-2 text-left font-medium">咨询</th>
            <th class="px-4 py-2 text-left font-medium">状态</th>
            <th class="px-4 py-2 text-left font-medium">轮数</th>
            <th class="px-4 py-2 text-left font-medium">最近更新</th>
            <th class="px-2 py-2 text-right">
              <span class="sr-only">打开该次咨询</span>
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <tr
            v-for="session in sessions"
            :key="session.id"
            class="cursor-pointer transition-colors hover:bg-slate-50"
            @click="openSession(session)"
          >
            <td class="px-4 py-3">
              <div class="max-w-[360px] truncate text-slate-800">{{ session.title }}</div>
              <div class="mt-0.5 text-xs text-slate-600">
                {{ CONSULT_TYPE_LABEL[session.type] }}
                <template v-if="session.knowledgeVersion">
                  · 知识版本 #{{ session.knowledgeVersion }}
                </template>
              </div>
            </td>
            <td class="px-4 py-3">
              <StatusBadge
                :label="runStatusLabel(session.status)"
                :tone="STATUS_TONE[session.status]"
                dot
              />
            </td>
            <td class="px-4 py-3 font-mono text-xs text-slate-600">{{ session.runCount }}</td>
            <td class="px-4 py-3">
              <div class="font-mono text-xs text-slate-600">
                {{ formatTime(session.updatedAt) }}
              </div>
              <div class="text-xs text-slate-600">创建于 {{ formatTime(session.createdAt) }}</div>
            </td>
            <td class="px-2 py-3 text-right">
              <!-- 整行可点是为了鼠标，键盘可达性仍需要一个真的按钮：
                   给 <tr> 加 role="button" 会把表格语义拆掉 -->
              <button
                type="button"
                class="rounded-md border border-slate-500 bg-white px-2.5 py-1 text-xs
                       text-slate-600 transition-colors hover:bg-slate-50 focus-ring"
                :aria-label="`打开咨询：${session.title}`"
                @click.stop="openSession(session)"
              >
                打开
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="!loading && totalPages > 1" class="flex items-center justify-end gap-2 text-sm">
      <button
        :disabled="page <= 1"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300 focus-ring"
        @click="page--; loadSessions()"
      >
        上一页
      </button>
      <span class="text-xs text-slate-600">{{ page }} / {{ totalPages }}</span>
      <button
        :disabled="page >= totalPages"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300 focus-ring"
        @click="page++; loadSessions()"
      >
        下一页
      </button>
    </div>
  </div>
</template>

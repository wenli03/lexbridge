<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  AUDIT_RESULT_LABEL,
  auditActionLabel,
  auditApi,
  type AuditLogItem,
  type AuditResult,
} from '@/api/audit'
import { ApiError } from '@/api/client'
import { ROLE_LABEL } from '@/api/auth'
import { jurisdictionLabel } from '@/api/knowledge'

import StateBlock from '@/components/StateBlock.vue'
import StatusBadge from '@/components/StatusBadge.vue'

/**
 * 审计日志页（只读）。
 *
 * **这一页没有任何编辑或删除入口，不是漏做了。** 一个能被调用方写入或修改的
 * 审计日志不具备证据价值——它能证明的东西只剩下"有人在这里打过字"。
 * 后端同样没有开放写接口（见详细设计 §2.2），前端不提供入口是这条约束的可见部分。
 *
 * 合规官最常用的检索是"最近的拒答记录"，因此把 `redlineOnly` 做成一个显式开关，
 * 而不是让人去结果类型里翻。
 *
 * **追溯的起点通常是用户贴过来的一串 trace_id**，那时你还不知道它属于谁、
 * 是哪一类操作。所以关键词框把 trace_id 和操作人放在同一个入口，
 * 而不是要求先选对筛选维度——顺序反了的话，人会在四个框里试。
 *
 * 行可展开：表格里放不下 `detail`、来源 IP 这些字段，而它们恰恰是事后复盘时
 * 唯一能还原现场的东西。悬停 tooltip 不算出口——它不可选中、不可复制，
 * 而报障要的正是把那一串复制走。
 */

const logs = ref<AuditLogItem[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const loading = ref(false)
const loadError = ref<{ message: string; traceId?: string } | null>(null)

const filters = ref<{
  q: string
  from: string
  to: string
  actor: string
  result: AuditResult | ''
  redlineOnly: boolean
}>({
  q: '',
  from: '',
  to: '',
  actor: '',
  result: '',
  redlineOnly: false,
})

/** 展开的行。追溯是"挑一条看细节"的动作，同时只需要看一条 */
const expandedId = ref<string | null>(null)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))

const RESULT_TONE: Record<AuditResult, 'safe' | 'risk' | 'caution' | 'slate'> = {
  SUCCESS: 'safe',
  DENIED: 'risk',
  // 红线拒答不是失败，用 caution 而不是 risk：它是系统按设计完成的工作
  REDLINE_REFUSAL: 'caution',
  ERROR: 'risk',
}

async function loadLogs(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const result = await auditApi.list({
      page: page.value,
      size,
      q: filters.value.q.trim() || undefined,
      from: filters.value.from || undefined,
      to: filters.value.to || undefined,
      actor: filters.value.actor || undefined,
      result: filters.value.result || undefined,
      redlineOnly: filters.value.redlineOnly || undefined,
    })
    logs.value = result.items
    total.value = result.total
  } catch (error) {
    const apiError = error instanceof ApiError ? error : null
    loadError.value = {
      message: apiError?.message ?? '审计日志加载失败',
      traceId: apiError?.traceId,
    }
  } finally {
    loading.value = false
  }
}

function onFilterChange(): void {
  page.value = 1
  void loadLogs()
}

function resetFilters(): void {
  filters.value = { q: '', from: '', to: '', actor: '', result: '', redlineOnly: false }
  expandedId.value = null
  onFilterChange()
}

function toggleRow(id: string): void {
  expandedId.value = expandedId.value === id ? null : id
}

/**
 * 按某个 trace_id 反查。
 *
 * 这是这一页最重要的一条路径：用户报障时给的是一串 trace_id，
 * 拿到它的人需要立刻看到"这一串到底发生了什么"。让 trace_id 可点，
 * 比让人手动选中再复制到上面的框里少两步，而少掉的正是最容易出错的两步。
 */
function searchByTrace(traceId: string): void {
  filters.value.q = traceId
  expandedId.value = null
  onFilterChange()
}

function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    return iso
  }
  // 审计时间必须显示到秒：同一秒内的多条记录要靠时间排序判断先后
  return date.toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => {
  void loadLogs()
})
</script>

<template>
  <div class="space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-sm text-slate-700">共 {{ total }} 条记录</p>
        <p class="mt-0.5 text-xs text-slate-600">
          记录由后端自动写入，本页只读。日志不可编辑或删除——可被修改的日志不具备证据价值。
        </p>
      </div>
      <button
        class="rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
               text-slate-600 transition-colors hover:bg-slate-50"
        @click="loadLogs"
      >
        刷新
      </button>
    </header>

    <!-- 筛选 -->
    <div class="card flex flex-wrap items-end gap-3 p-4">
      <!-- 关键词排第一：追溯的起点是用户贴过来的那串 trace_id，
           而此时你还不知道该选哪个维度，所以它必须是不必先做选择的那一个入口 -->
      <div class="w-full sm:w-auto sm:flex-1">
        <label class="mb-1 block text-xs font-medium text-slate-600" for="au-q">
          关键词 / trace_id
        </label>
        <input
          id="au-q"
          v-model="filters.q"
          type="search"
          class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          placeholder="例如 tr_7f3a91c4e2b8、操作人姓名或目标名称"
          @keyup.enter="onFilterChange"
        />
      </div>

      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="au-from">起始日期</label>
        <input
          id="au-from"
          v-model="filters.from"
          type="date"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        />
      </div>
      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="au-to">结束日期</label>
        <input
          id="au-to"
          v-model="filters.to"
          type="date"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        />
      </div>
      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="au-actor">操作者</label>
        <input
          id="au-actor"
          v-model="filters.actor"
          type="text"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          placeholder="用户名或姓名"
          @keyup.enter="onFilterChange"
        />
      </div>
      <div>
        <label class="mb-1 block text-xs font-medium text-slate-600" for="au-result">结果</label>
        <select
          id="au-result"
          v-model="filters.result"
          class="rounded-md border border-slate-500 px-3 py-1.5 text-sm
                 focus-ring"
          @change="onFilterChange"
        >
          <option value="">全部</option>
          <option value="SUCCESS">成功</option>
          <option value="DENIED">被拒</option>
          <option value="REDLINE_REFUSAL">红线拒答</option>
          <option value="ERROR">失败</option>
        </select>
      </div>

      <label class="flex cursor-pointer items-center gap-2 text-sm text-slate-600">
        <input
          v-model="filters.redlineOnly"
          type="checkbox"
          class="h-4 w-4 rounded border-slate-500"
          @change="onFilterChange"
        />
        只看拒答记录
      </label>

      <button
        class="ml-auto rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
               text-slate-600 transition-colors hover:bg-slate-50"
        @click="resetFilters"
      >
        重置
      </button>
    </div>

    <StateBlock v-if="loading" variant="loading" title="正在加载审计日志" />
    <StateBlock
      v-else-if="loadError"
      variant="error"
      title="审计日志加载失败"
      :description="loadError.message"
      :trace-id="loadError.traceId"
      action-label="重试"
      @action="loadLogs"
    />
    <StateBlock
      v-else-if="!logs.length"
      title="没有符合条件的记录"
      description="换一个 trace_id、把操作类型放宽，或清空筛选查看全部记录。"
      action-label="重置筛选"
      @action="resetFilters"
    />

    <div v-else class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-xs text-slate-600">
          <tr>
            <th class="px-4 py-2 text-left font-medium">时间</th>
            <th class="px-4 py-2 text-left font-medium">操作者</th>
            <th class="px-4 py-2 text-left font-medium">操作</th>
            <th class="px-4 py-2 text-left font-medium">对象</th>
            <th class="px-4 py-2 text-left font-medium">结果</th>
            <th class="px-4 py-2 text-left font-medium">traceId</th>
            <th class="px-2 py-2">
              <span class="sr-only">展开详情</span>
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <template v-for="log in logs" :key="log.id">
            <tr
              class="cursor-pointer transition-colors hover:bg-slate-50"
              :class="{ 'bg-slate-50': expandedId === log.id }"
              @click="toggleRow(log.id)"
            >
              <td class="px-4 py-3 font-mono text-xs text-slate-600">
                {{ formatTime(log.createdAt) }}
              </td>
              <td class="px-4 py-3">
                <div class="text-slate-700">{{ log.actorName }}</div>
                <div class="text-xs text-slate-600">
                  {{ log.actorRole ? ROLE_LABEL[log.actorRole] : '账号已注销' }}
                </div>
              </td>
              <td class="px-4 py-3">
                <div class="text-slate-700">{{ auditActionLabel(log.action) }}</div>
                <div
                  v-if="log.detail"
                  class="mt-0.5 max-w-[280px] truncate text-xs text-slate-600"
                >
                  {{ log.detail }}
                </div>
              </td>
              <td class="px-4 py-3 text-xs text-slate-600">
                <template v-if="log.targetType">
                  {{ log.targetType }}
                  <span v-if="log.targetId" class="font-mono text-slate-600">
                    {{ log.targetId.slice(0, 8) }}
                  </span>
                </template>
                <span v-else>—</span>
              </td>
              <td class="px-4 py-3">
                <StatusBadge
                  :label="AUDIT_RESULT_LABEL[log.result]"
                  :tone="RESULT_TONE[log.result]"
                />
              </td>
              <!-- 整串显示，不再截成 8 位。截断后剩下的前缀无法用来检索，
                   而复制这一串正是报障与追溯要做的事 -->
              <td class="px-4 py-3">
                <span v-if="log.traceId" class="font-mono text-xs text-slate-600">
                  {{ log.traceId }}
                </span>
                <span v-else class="text-xs text-slate-600">—</span>
              </td>
              <td class="px-2 py-3 text-right">
                <!-- 行本身可点是为了鼠标，但键盘可达性需要一个真的按钮：
                     给 <tr> 加 role="button" 会把表格语义拆掉 -->
                <button
                  type="button"
                  class="rounded p-1 text-slate-600 transition-colors hover:text-slate-800
                         focus-ring"
                  :aria-expanded="expandedId === log.id"
                  :aria-controls="`au-detail-${log.id}`"
                  :aria-label="expandedId === log.id ? '收起该条记录详情' : '展开该条记录详情'"
                  @click.stop="toggleRow(log.id)"
                >
                  <span
                    class="block select-none text-xs transition-transform"
                    :class="{ 'rotate-90': expandedId === log.id }"
                    aria-hidden="true"
                  >▸</span>
                </button>
              </td>
            </tr>

            <tr v-if="expandedId === log.id" :id="`au-detail-${log.id}`">
              <td colspan="7" class="bg-slate-50 px-4 py-3">
                <div class="space-y-2.5">
                  <p class="text-xs leading-relaxed text-slate-700">
                    {{ log.detail ?? '这条记录没有附带详情。' }}
                  </p>

                  <div class="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-slate-600">
                    <span v-if="log.traceId" class="flex items-center gap-1.5">
                      trace_id
                      <button
                        type="button"
                        class="citation font-mono"
                        title="按这串 trace_id 检索"
                        @click.stop="searchByTrace(log.traceId)"
                      >
                        {{ log.traceId }}
                      </button>
                    </span>
                    <span v-if="log.targetType">
                      目标
                      <span class="font-mono text-slate-600">{{ log.targetType }}</span>
                      <span v-if="log.targetId" class="font-mono text-slate-600">
                        {{ log.targetId }}
                      </span>
                    </span>
                    <span v-if="log.jurisdictionCode">
                      法域
                      <span class="text-slate-600">
                        {{ jurisdictionLabel(log.jurisdictionCode) }}
                      </span>
                    </span>
                    <span v-if="log.sourceIp">
                      来源 IP <span class="font-mono text-slate-600">{{ log.sourceIp }}</span>
                    </span>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <div v-if="!loading && totalPages > 1" class="flex items-center justify-end gap-2 text-sm">
      <button
        :disabled="page <= 1"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300"
        @click="page--; loadLogs()"
      >
        上一页
      </button>
      <span class="text-xs text-slate-600">{{ page }} / {{ totalPages }}</span>
      <button
        :disabled="page >= totalPages"
        class="rounded-md border border-slate-500 px-2.5 py-1 text-slate-600
               transition-colors enabled:hover:bg-slate-50 disabled:cursor-not-allowed
               disabled:text-slate-300"
        @click="page++; loadLogs()"
      >
        下一页
      </button>
    </div>
  </div>
</template>

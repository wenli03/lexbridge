<script setup lang="ts">
import { reactive, ref } from 'vue'

import type { InterruptEvent } from '@/api/sse'
import { JURISDICTIONS } from '@/api/knowledge'

/**
 * 追问表单（对应 SSE 的 `interrupt` 事件）。
 *
 * **中断不是错误。** 系统在事实不足时选择"停下来问"，而不是猜一个前提硬出方案——
 * 一个基于猜测前提的税务架构建议，比"没有建议"危险得多。
 *
 * 提交后走 `resume` 接口从检查点恢复，**已完成的检索与抽取不会重跑**。
 * 这一点在界面上也要能看出来（显示"已完成的步骤不会重算"）。
 */
defineProps<{
  interrupt: InterruptEvent
}>()

const emit = defineEmits<{ submit: [slots: Record<string, string>] }>()

/** 槽位名 → 面向用户的问法。命中不了就退回原始键名，而不是显示"未知字段" */
const SLOT_LABEL: Record<string, string> = {
  jurisdiction: '涉及的法域',
  targetJurisdiction: '目标法域',
  sourceJurisdiction: '资金或交易的来源法域',
  businessType: '业务类型',
  entityType: '主体形式',
  shareholding: '股东结构与持股比例',
  transactionType: '交易类型',
  counterpartyCountry: '交易对手所在国家/地区',
  amount: '金额区间',
  asOf: '所依据的时点',
}

const answers = reactive<Record<string, string>>({})
const error = ref('')

// 法域类槽位用下拉，避免自由输入带来的"新加坡/新加坡共和国"这类无法对齐的值
const JURISDICTION_SLOTS = new Set(['jurisdiction', 'targetJurisdiction', 'sourceJurisdiction'])

function labelOf(slot: string): string {
  return SLOT_LABEL[slot] ?? slot
}

function onSubmit(): void {
  const filled = Object.fromEntries(
    Object.entries(answers).filter(([, value]) => value.trim() !== ''),
  )
  if (Object.keys(filled).length === 0) {
    error.value = '请至少补充一项信息，否则系统仍需猜测前提。'
    return
  }
  error.value = ''
  emit('submit', filled)
}
</script>

<template>
  <section class="rounded-lg border border-brand-200 bg-brand-50/60 overflow-hidden">
    <header class="border-b border-brand-200 px-4 py-3">
      <h3 class="text-sm font-semibold text-brand-700">需要补充信息才能继续</h3>
      <p class="mt-0.5 text-sm text-slate-600">{{ interrupt.prompt }}</p>
    </header>

    <form class="space-y-3 px-4 py-4" @submit.prevent="onSubmit">
      <div v-for="slot in interrupt.missingSlots" :key="slot">
        <label
          class="mb-1 block text-xs font-medium text-slate-600"
          :for="`slot-${slot}`"
        >
          {{ labelOf(slot) }}
        </label>

        <select
          v-if="JURISDICTION_SLOTS.has(slot)"
          :id="`slot-${slot}`"
          v-model="answers[slot]"
          class="w-full rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                 text-slate-800 focus-ring"
        >
          <option value="">请选择</option>
          <option v-for="item in JURISDICTIONS" :key="item.code" :value="item.code">
            {{ item.label }}（{{ item.code }}）
          </option>
        </select>

        <input
          v-else
          :id="`slot-${slot}`"
          v-model="answers[slot]"
          type="text"
          class="w-full rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
                 text-slate-800 placeholder:text-slate-600
                 focus-ring"
          placeholder="请补充具体信息"
        />
      </div>

      <p v-if="error" class="text-xs text-risk-800">{{ error }}</p>

      <div class="flex items-center gap-3">
        <button
          type="submit"
          class="rounded-md bg-brand-600 px-3.5 py-1.5 text-sm font-medium text-white
                 transition-colors hover:bg-brand-700"
        >
          提交并继续
        </button>
        <span class="text-xs text-slate-600">
          已完成的检索与抽取会从检查点恢复，不会重算
        </span>
      </div>
    </form>
  </section>
</template>

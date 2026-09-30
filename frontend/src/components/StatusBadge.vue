<script setup lang="ts">
import { computed } from 'vue'

/**
 * 状态标签。
 *
 * `tone` 的取值直接对应 `style.css` 里的合规语义色，**不要当装饰色用**：
 *   safe    —— 合法区间、已发布、成功
 *   caution —— 需要人工介入、待复核、进行中
 *   risk    —— 红线命中、失败、越权被拒
 *   brand   —— 中性强调（进行中的处理步骤）
 *   slate   —— 无状态含义（草稿、未开始）
 *
 * 一个"待复核"用绿色、"已发布"用橙色，会让整个界面的颜色语言失效，
 * 而在这类合规工具里，颜色是传达边界的主要手段之一。
 */
const props = withDefaults(
  defineProps<{
    label: string
    tone?: 'safe' | 'caution' | 'risk' | 'brand' | 'slate'
    /** 显示一个小圆点，用于状态需要更快识别的列表 */
    dot?: boolean
  }>(),
  { tone: 'slate', dot: false },
)

const classes = computed(() => {
  const map: Record<string, string> = {
    safe: 'bg-safe-50 text-safe-800 border-safe-200',
    caution: 'bg-caution-50 text-caution-800 border-caution-200',
    risk: 'bg-risk-50 text-risk-800 border-risk-200',
    brand: 'bg-brand-50 text-brand-700 border-brand-200',
    slate: 'bg-slate-100 text-slate-600 border-slate-200',
  }
  return map[props.tone]
})

const dotClass = computed(() => {
  const map: Record<string, string> = {
    safe: 'bg-safe-500',
    caution: 'bg-caution-500',
    risk: 'bg-risk-500',
    brand: 'bg-brand-500',
    slate: 'bg-slate-400',
  }
  return map[props.tone]
})
</script>

<template>
  <span
    class="inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium"
    :class="classes"
  >
    <span v-if="dot" class="h-1.5 w-1.5 rounded-full" :class="dotClass" />
    {{ label }}
  </span>
</template>

<script setup lang="ts">
import type { Assumption } from '@/api/consult'

/**
 * 假设与取值。
 *
 * **为什么默认全展开，而"计算过程"默认折叠。** 两者回答的不是同一个问题：
 *   假设与取值 —— "这个结论在什么前提下成立"。前提变了结论就翻转，
 *                 所以这是读结论时就必须看到的一段，折叠起来等于把它藏掉。
 *   计算过程   —— "这个数字怎么算出来的"。那是复核时才需要的第二层信息。
 * 默认状态不同是有意的，不是随手写的。
 *
 * 三档语气沿用合规语义色（见 style.css 的说明），不新造颜色。
 *
 * **这里的 `risk` 不是红线拒答。** 它指"这条假设不成立时结论会翻转"，
 * 例如"本轮未检索到任何法条，所以没有引用"。红线拒答走 `RedlineNotice`，
 * 两者不会同时出现——被拒答的轮次没有结论，也就没有假设可言。
 */
withDefaults(defineProps<{ assumptions: Assumption[] }>(), {
  assumptions: () => [],
})

const TONE: Record<Assumption['level'], string> = {
  risk: 'border-risk-200 bg-risk-50 text-risk-800',
  caution: 'border-caution-200 bg-caution-50 text-caution-800',
  safe: 'border-safe-200 bg-safe-50 text-safe-800',
}

/**
 * 标签用文字而不是只靠颜色。只给颜色的话，色觉障碍使用者看到的是一列
 * 一模一样的灰条，而这三档的含义差别恰恰是这个区块的全部价值。
 */
const LEVEL_LABEL: Record<Assumption['level'], string> = {
  risk: '前提性假设',
  caution: '取值说明',
  safe: '事实核对',
}
</script>

<template>
  <section v-if="assumptions.length" class="card overflow-hidden">
    <header class="border-b border-slate-200 px-5 py-3">
      <h3 class="text-sm font-medium text-slate-800">
        假设与取值（{{ assumptions.length }}）
      </h3>
      <p class="mt-0.5 text-xs text-slate-600">
        结论在下列前提下成立。前提变化时结论可能翻转，请逐条核对。
      </p>
    </header>

    <ul class="divide-y divide-slate-100">
      <li
        v-for="(assumption, index) in assumptions"
        :key="index"
        class="flex items-start gap-3 px-5 py-3"
      >
        <span
          class="mt-0.5 shrink-0 rounded border px-1.5 py-0.5 text-xs font-medium"
          :class="TONE[assumption.level]"
        >
          {{ LEVEL_LABEL[assumption.level] }}
        </span>
        <p class="min-w-0 flex-1 text-xs leading-relaxed text-slate-700">
          {{ assumption.text }}
        </p>
      </li>
    </ul>
  </section>
</template>

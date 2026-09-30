<script setup lang="ts">
import { computed } from 'vue'

import type { LegalAlternative, RedlineBoundary, RedlineRuleRef } from '@/api/consult'

/**
 * 红线拒答卡片（五要素）。
 *
 * 这是整个界面里最需要"讲清楚"的一块。拒答不是一个错误提示——
 * 它必须回答用户心里立刻会浮现的三个问题：**为什么不行、凭什么这么说、那我该怎么办**。
 *
 * 因此五要素缺一不可（PRD AC-4.2 会逐项检查）：
 *   1 红线类别  2 判定依据（命中的原句）  3 拒绝理由
 *   4 合法替代路径（≥1 条）              5 建议咨询执业律师
 *
 * **会话不会因此中断。** 卡片下面仍有输入框，用户可以换个方式提问——
 * 一个"问错一次就结束会话"的工具，会让人不敢提问。
 *
 * 三处是后补的，都不是装饰：
 *
 *   「不做的范围 / 可以继续做的范围」对照 —— 拒答最容易传达错的一件事是
 *     "这个平台不帮我了"。两列并排让用户看到边界落在哪：被挡住的是一件事，
 *     不是一个人。缺了这一段，用户会去别处找一个不问边界、什么都答的工具。
 *   判定依据逐条列出 —— 只说一句"命中规则 R-07"，用户无从判断这条规则
 *     是否用对了，而这是他对整个系统唯一能做的核查。
 *   可点击的改问方向 —— 写出了方向却要用户自己再打一遍字，等于把最难的一步
 *     又推回给一个刚被拒的人。
 */
const props = defineProps<{
  ruleId: string
  category: string
  reason: string
  evidence?: string[]
  alternatives: LegalAlternative[]
  advice?: string
  /** 被拒答的诉求，原样回显——用户要能确认"它听懂的是不是我要问的那件事" */
  refused?: string
  refusedWhy?: string
  /** 缺省时整个对照区块不渲染，而不是渲染出两列空白 */
  boundary?: RedlineBoundary
  /** 缺省时退回展示 `evidence` 原句 */
  ruleRefs?: RedlineRuleRef[]
}>()

const emit = defineEmits<{ ask: [question: string] }>()

/** 后端可能只给得出方向名称。在这里补成同一个形状，模板里就不必分两套写 */
const directions = computed(() =>
  props.alternatives.map((item) => (typeof item === 'string' ? { title: item } : item)),
)
</script>

<template>
  <section class="rounded-lg border border-risk-200 bg-risk-50 overflow-hidden">
    <header class="flex items-start gap-3 border-b border-risk-200 px-4 py-3">
      <!-- 用图形符号而不是图标库：技术栈固定，不为此新增依赖 -->
      <span
        class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full
               bg-risk-700 text-xs font-bold text-white"
        aria-hidden="true"
      >
        !
      </span>
      <div class="min-w-0">
        <h3 class="text-sm font-semibold text-risk-800">
          该请求不予协助 · {{ category }}
        </h3>
        <p class="mt-0.5 font-mono text-xs text-risk-800">
          规则编号 {{ ruleId }}
        </p>
      </div>
    </header>

    <div class="space-y-4 px-4 py-4">
      <!-- 软回显：先让用户确认被拒的是哪件事。拒错对象比拒答本身更伤信任，
           而纠正它的成本只是把用户的原话摆回来 -->
      <div v-if="refused">
        <p class="text-xs font-medium text-risk-800">被拒答的诉求</p>
        <p class="mt-1.5 text-sm leading-relaxed text-slate-700">{{ refused }}</p>
        <p v-if="refusedWhy" class="mt-1.5 text-xs leading-relaxed text-slate-600">
          {{ refusedWhy }}
        </p>
      </div>

      <!-- 边界对照的两列。左列是"不做什么"，右列是"还能做什么"——
           顺序固定：先确认被挡住的范围，再看到仍然敞开的范围 -->
      <div v-if="boundary" class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div class="rounded border border-risk-200 bg-white px-3 py-2.5">
          <h5 class="text-xs font-semibold text-risk-800">不做的范围</h5>
          <p class="mt-1 text-sm leading-relaxed text-slate-700">{{ boundary.refused }}</p>
        </div>
        <div class="rounded border border-safe-200 bg-white px-3 py-2.5">
          <h5 class="text-xs font-semibold text-safe-800">可以继续做的范围</h5>
          <p class="mt-1 text-sm leading-relaxed text-slate-700">{{ boundary.allowed }}</p>
        </div>
      </div>

      <div>
        <p class="text-xs font-medium text-risk-800">判定依据</p>

        <!-- 规则明细优先：能逐条查到依据时才给表 -->
        <div v-if="ruleRefs && ruleRefs.length" class="mt-1.5 overflow-x-auto">
          <table class="w-full text-sm">
            <thead class="text-xs text-slate-600">
              <tr>
                <th class="py-1.5 pr-4 text-left font-medium">规则</th>
                <th class="py-1.5 pr-4 text-left font-medium">名称</th>
                <th class="py-1.5 text-left font-medium">依据</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-risk-200/60">
              <tr v-for="rule in ruleRefs" :key="rule.id">
                <td class="py-2 pr-4 align-top font-mono text-xs text-slate-600">
                  {{ rule.id }}
                </td>
                <td class="py-2 pr-4 align-top font-medium text-slate-700">{{ rule.name }}</td>
                <td class="py-2 align-top text-xs leading-relaxed text-slate-600">
                  {{ rule.basis }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <ul v-else-if="evidence && evidence.length" class="mt-1.5 space-y-1">
          <li
            v-for="(line, i) in evidence"
            :key="i"
            class="rounded border border-risk-200 bg-white px-2.5 py-1.5
                   text-sm text-slate-700"
          >
            {{ line }}
          </li>
        </ul>

        <p v-else class="mt-1.5 text-sm text-slate-600">
          请求整体表达了规避强制性法律义务的意图。
        </p>

        <!-- DR-4 的可见面：判定不经过模型，所以换个问法不会改变结论。
             用户最容易做的错误尝试就是"换个说法再问一遍"，这句话是拦它的 -->
        <p class="mt-2 text-xs leading-relaxed text-slate-600">
          红线判定由规则引擎在模型生成之前完成，不依赖模型输出，因此不会因为提问方式的变化而改变。
        </p>
      </div>

      <div>
        <p class="text-xs font-medium text-risk-800">拒绝理由</p>
        <p class="mt-1.5 text-sm leading-relaxed text-slate-700">{{ reason }}</p>
      </div>

      <div v-if="directions.length">
        <p class="text-xs font-medium text-safe-800">可以走的合法路径</p>
        <ul class="mt-1.5 space-y-2">
          <li v-for="(item, i) in directions" :key="i" class="flex gap-2">
            <span class="select-none text-safe-500" aria-hidden="true">→</span>
            <div class="min-w-0">
              <div class="text-sm leading-relaxed text-slate-700">{{ item.title }}</div>
              <div v-if="item.desc" class="mt-0.5 text-xs leading-relaxed text-slate-600">
                {{ item.desc }}
              </div>
            </div>
          </li>
        </ul>

        <!-- 点一下就按这个方向重新提问。让人在被拒之后还要手打一遍问题，
             是把整个卡片里最难的一步留给了最需要省事的人 -->
        <div class="mt-2.5 flex flex-wrap gap-1.5">
          <button
            v-for="(item, i) in directions"
            :key="`ask-${i}`"
            type="button"
            class="rounded-md border border-safe-200 bg-white px-2.5 py-1 text-xs
                   text-safe-800 transition-colors hover:border-safe-500"
            @click="emit('ask', item.title)"
          >
            {{ item.title }}
          </button>
        </div>
      </div>

      <p class="border-t border-risk-200 pt-3 text-xs leading-relaxed text-slate-600">
        {{ advice ?? '本平台提供研究辅助，不构成法律意见；具体事项请咨询具备相应法域执业资格的律师。' }}
      </p>
    </div>
  </section>
</template>

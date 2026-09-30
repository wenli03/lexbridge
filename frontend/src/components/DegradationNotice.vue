<script setup lang="ts">
import { computed } from 'vue'

import type { Degradation, DegradeKind } from '@/api/consult'

/**
 * 降级提示条。
 *
 * **降级不是失败，但也不能当正常结果读。** 检索没跑完、或者跑完了但没命中时，
 * 后端仍会返回一段通顺的文字。这段文字没有引用支撑，如果界面照常渲染，
 * 用户会把它读成结论——所以提示条必须在正文**上方**，而不是折叠进脚注。
 *
 * 两种降级分开呈现，因为使用者接下来要做的事不一样：
 *   RETRIEVAL_UNAVAILABLE —— 这次没查成。重试有意义，所以给重试入口。
 *   RETRIEVAL_NO_MATCH    —— 库里没有。重试没有意义，要做的是补语料或换问法，
 *                            这里给重试按钮等于骗用户白点。
 *
 * **不使用 safe 色系的任何档位。** 绿色在这个系统里代表"合法区间"，
 * 用在这里等于把一次检索故障读成绿灯，而这正是最危险的一种误读。
 */
const props = defineProps<{ degradation: Degradation }>()

const emit = defineEmits<{ retry: [] }>()

const HEADLINE: Record<DegradeKind, string> = {
  RETRIEVAL_UNAVAILABLE: '检索没有完成，这一轮不能作为依据',
  RETRIEVAL_NO_MATCH: '检索完成了，但知识库里没有覆盖这个问题',
}

const NEXT_STEP: Record<DegradeKind, string> = {
  RETRIEVAL_UNAVAILABLE: '可以重试本次检索，或缩小问题范围后重新提问。',
  RETRIEVAL_NO_MATCH: '换问法或重试都不会改变结果，需要先补充对应法域的语料。',
}

/** 只有"没查成"才可重试；对"库里没有"提供重试按钮是在骗人 */
const retryable = computed(() => props.degradation.kind === 'RETRIEVAL_UNAVAILABLE')

const tone = computed(() =>
  retryable.value
    ? {
        wrap: 'border-caution-200 bg-caution-50',
        // 标记里的白字落在实心色上：caution-500 只有 2.54:1，连非文字的 3:1 都不到，
        // 下沉到 700（5.02:1）。这一处不是文字色，但同属"语义色当小字号的载体"
        mark: 'bg-caution-700',
        title: 'text-caution-800',
        // 这里原来是 `text-caution-700/80`，实测 2.68:1。弱化交给字号去做，
        // 不要用透明度——透明度会把 caution 这个颜色本身也改掉
        // （这个数原来是 3.25，写错了；错法与 style.css 里记的那次不同）
        body: 'text-caution-800',
        // 按钮就落在 caution 底色上，所以 hover 不能靠换底色（换了也看不出来），
        // 改用加深描边——在浅底上这才是一个看得见的反馈
        button: 'border-caution-200 text-caution-800 hover:border-caution-500',
      }
    : {
        // 这一档是容器，不是控件，1.4.11 不适用；用结构档的 200，
        // 与 .card、以及原型 .banner-plain 的取值一致
        wrap: 'border-slate-200 bg-slate-50',
        mark: 'bg-slate-500',
        title: 'text-slate-700',
        body: 'text-slate-600',
        button: '',
      },
)
</script>

<template>
  <div class="rounded-lg border overflow-hidden" :class="tone.wrap">
    <div class="flex items-start gap-2.5 px-4 py-3">
      <!-- 用图形符号而不是图标库：技术栈固定，不为此新增依赖 -->
      <span
        class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full
               text-xs font-bold text-white"
        :class="tone.mark"
        aria-hidden="true"
      >{{ retryable ? '!' : 'i' }}</span>

      <div class="min-w-0 flex-1">
        <p class="text-sm font-medium" :class="tone.title">
          {{ HEADLINE[degradation.kind] }}
        </p>
        <p class="mt-1 text-xs leading-relaxed" :class="tone.body">
          {{ degradation.reason }}
        </p>
        <p class="mt-2 text-xs leading-relaxed" :class="tone.body">
          {{ NEXT_STEP[degradation.kind] }}
        </p>

        <div class="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <button
            v-if="retryable"
            type="button"
            class="rounded-md border bg-white px-2.5 py-1 text-xs transition-colors"
            :class="tone.button"
            @click="emit('retry')"
          >
            重试本次检索
          </button>
          <span v-if="degradation.traceId" class="font-mono text-xs" :class="tone.body">
            traceId：{{ degradation.traceId }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

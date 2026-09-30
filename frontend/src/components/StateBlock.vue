<script setup lang="ts">
import { computed } from 'vue'

/**
 * 空态 / 错误态 / 加载态的统一呈现。
 *
 * **为什么把这三者合到一个组件里。** 它们回答的是同一个问题——"这里为什么没有内容"。
 * 分开实现的结果通常是：空态写了、错误态忘了、加载态各处样式不一，
 * 而使用者看到一片空白时无法判断是该等、该重试，还是该去别处建数据。
 *
 * 文案要求：**说清楚下一步该做什么**。"暂无数据"是无效文案——
 * 它没有告诉管理员"你需要先上传一份法规"。
 */
const props = withDefaults(
  defineProps<{
    variant?: 'empty' | 'error' | 'loading'
    title: string
    description?: string
    /** 出错时展示 traceId，用户报障时可以直接给运维 */
    traceId?: string
    actionLabel?: string
    /** 紧凑模式，用于卡片内部而非整页 */
    compact?: boolean
  }>(),
  {
    variant: 'empty',
    compact: false,
    // 这三个声明了默认值是为了满足 `vue/require-default-prop`：
    // 可选的字符串 prop 若不给默认值，undefined 会直接渲染成 "undefined"
    description: '',
    traceId: '',
    actionLabel: '',
  },
)

const emit = defineEmits<{ action: [] }>()

const tone = computed(() => {
  if (props.variant === 'error') {
    return {
      wrap: 'border-risk-200 bg-risk-50',
      // 原来正文是 `text-risk-700/80`，实测 3.08:1——离 4.5 太近，
      // 而这是错误态里真正要读的那一段。弱化交给字号，不用透明度
      // （这个数原来是 4.65，写错了：那次把 alpha 混合当成对亮度加权，
      //   正确做法是先在 sRGB 通道上合成再算相对亮度。结论没变，都不过线）
      title: 'text-risk-800',
      body: 'text-risk-800',
    }
  }
  return {
    wrap: 'border-slate-200 bg-white',
    title: 'text-slate-700',
    body: 'text-slate-600',
  }
})
</script>

<template>
  <div
    class="card border-dashed text-center"
    :class="[tone.wrap, compact ? 'px-4 py-6' : 'px-6 py-12']"
  >
    <!-- 加载态用三个脉冲点而不是转圈：转圈在慢接口上会让人以为卡死，
         脉冲点至少能看出界面是活的 -->
    <div v-if="variant === 'loading'" class="flex items-center justify-center gap-1.5">
      <span
        v-for="i in 3"
        :key="i"
        class="h-2 w-2 rounded-full bg-brand-400 animate-pulse"
        :style="{ animationDelay: `${(i - 1) * 150}ms` }"
      />
    </div>

    <p class="text-sm font-medium" :class="tone.title">{{ title }}</p>
    <p v-if="description" class="mx-auto mt-1.5 max-w-md text-sm" :class="tone.body">
      {{ description }}
    </p>

    <p v-if="traceId" class="mt-2 font-mono text-xs text-slate-600">
      traceId：{{ traceId }}
    </p>

    <button
      v-if="actionLabel"
      class="mt-4 rounded-md border border-slate-500 bg-white px-3 py-1.5 text-sm
             text-slate-700 transition-colors hover:bg-slate-50
             focus-ring"
      @click="emit('action')"
    >
      {{ actionLabel }}
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * 「服务端尚未实现」的说明面板。
 *
 * 刻意做成"说明现状"而不是空白页或"施工中"图片。它替代的原本是一个红色
 * 的「加载失败」——那会把"这一块还没做"表达成"这一块坏了"。
 *
 * **它只在服务端确实回 404 时才出现**（判断见 `@/api/notImplemented`），
 * 而不是写死在页面里：接口一旦存在，页面立刻正常工作，不必回来删代码。
 *
 * 文案的底线是**不能让人误以为功能是好的**：`phase` 说清属于哪一阶段，
 * `points` 说清点下去会看到什么。README 的「已知边界」表是唯一权威。
 */
defineProps<{
  /** 该功能的简述 */
  summary: string
  /** 计划在哪个阶段实现 */
  phase: string
  /** 该页面会包含的关键要素 */
  points: string[]
}>()
</script>

<template>
  <div class="card p-6 max-w-3xl">
    <div class="flex items-start gap-3">
      <span
        class="mt-0.5 rounded bg-caution-50 border border-caution-200 px-2 py-0.5
               text-xs font-medium text-caution-800"
      >
        {{ phase }}
      </span>
      <div class="min-w-0">
        <p class="text-sm text-slate-700">{{ summary }}</p>
        <ul class="mt-3 space-y-1.5">
          <li
            v-for="(point, i) in points"
            :key="i"
            class="flex gap-2 text-sm text-slate-600"
          >
            <span class="select-none text-slate-400">·</span>
            <span>{{ point }}</span>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

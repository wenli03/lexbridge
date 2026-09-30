<script setup lang="ts">
/**
 * 引用列表。
 *
 * 引用是这个系统的核心概念——"无引用不出结论"——因此它的呈现方式固定：
 * 统一用 `.citation` 样式（见 style.css），任何页面都不自行发挥。
 *
 * 相似度只在**低于阈值**时才显示。每条都标一个 0.91 / 0.88 会让界面充满
 * 无差别的数字，反而看不出哪条引用是勉强挂上的。
 *
 * **失效与相似度是两件事，所以是两个独立的标记，不合并成一个"引用质量"。**
 * 相似度低是"这条挂得勉强"，复核一下还能用；失效是"这条现在已经不是法了"，
 * 结论据此作废。把两者显示成同一种黄，是最容易出事的一种省事。
 */
const props = withDefaults(
  defineProps<{
    citations: {
      articleId: string
      articleNo: string
      similarity?: number
      stale?: boolean
      supersededByArticleId?: string
      supersededByArticleNo?: string
    }[]
    /** 低于该相似度时显示数值，提示这条引用不够强 */
    warnBelow?: number
  }>(),
  { warnBelow: 0.8 },
)

const emit = defineEmits<{ select: [articleId: string] }>()

function shouldWarn(similarity?: number): boolean {
  return similarity !== undefined && similarity < props.warnBelow
}

/**
 * 打开现行版。
 *
 * 后端可能只标了失效、没给现行版的 id。那种情况下退回打开原条文——
 * 让人看到"这条确实已经废止"，比点不动要好。
 */
function openSuperseded(citation: {
  articleId: string
  supersededByArticleId?: string
}): void {
  emit('select', citation.supersededByArticleId ?? citation.articleId)
}
</script>

<template>
  <div class="flex flex-wrap items-center gap-1.5">
    <template v-for="citation in citations" :key="`${citation.articleId}-${citation.articleNo}`">
      <button
        type="button"
        class="citation"
        :class="{ 'citation-stale': citation.stale }"
        :title="
          citation.stale
            ? `查看 ${citation.articleNo}（已失效条文）原文`
            : `查看 ${citation.articleNo} 原文`
        "
        @click="emit('select', citation.articleId)"
      >
        {{ citation.articleNo }}
        <span v-if="citation.stale">· 已失效</span>
        <span v-else-if="shouldWarn(citation.similarity)" class="text-caution-800">
          · 相似度偏低 {{ citation.similarity?.toFixed(2) }}
        </span>
      </button>

      <!-- 现行版单独一个平级按钮，不嵌在上一个按钮里：按钮里不能再放按钮。
           只在后端给出了现行版 id 时出现，否则一个点了没反应的箭头比没有更糟。 -->
      <button
        v-if="citation.stale && citation.supersededByArticleId"
        type="button"
        class="citation citation-stale"
        :title="`查看现行版本 ${citation.supersededByArticleNo ?? ''} 原文`"
        @click="openSuperseded(citation)"
      >
        → 现行版 {{ citation.supersededByArticleNo ?? '' }}
      </button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import {
  DIVERGENCE_VERDICT_LABEL,
  type DivergenceCell,
  type DivergenceRow,
  type DivergenceVerdict,
  type RiskLevel,
  type RunResult,
} from '@/api/consult'
import { jurisdictionLabel } from '@/api/knowledge'
import AssumptionList from './AssumptionList.vue'
import CitationList from './CitationList.vue'
import DegradationNotice from './DegradationNotice.vue'

/**
 * 咨询结论文的呈现。
 *
 * 四条硬约束体现在这里：
 *
 * 1. **"未检索到明确规定"必须与"许可"视觉上可区分。** 前者是"我们没找到依据"，
 *    后者是"找到依据且允许"。把它们做成同一种绿色，使用者会把检索缺口读成绿灯，
 *    而在这个领域，那是最危险的一种误读。
 * 2. **被剥离的结论句数必须显示。** 引用校验会删掉挂不上来源的句子（G1.3）。
 *    如果界面照常输出一段完整的话，用户会以为这就是全部结论。
 * 3. **降级提示条必须在最前面。** 降级轮次的正文同样通顺，但它没有引用支撑。
 *    提示条排在正文之后，等于让用户先读完结论再看到"这不是结论"。
 * 4. **关键数字的计算过程要能展开。** 综合税负率这类数字由确定性计算模块产出
 *    （AC-2.3），模型只负责解释。说不出数字怎么来的，复核就无从下手。
 */
const props = defineProps<{
  result: RunResult
}>()

const emit = defineEmits<{ selectCitation: [articleId: string]; retry: [] }>()

/** 只渲染真的有轨迹的候选，避免表格下面挂出一排空折叠块 */
const calcTraces = computed(() =>
  (props.result.candidates ?? []).filter((candidate) => candidate.calcTrace),
)

const RISK_LABEL: Record<RiskLevel, string> = { LOW: '低', MEDIUM: '中', HIGH: '高' }

const RISK_TONE: Record<RiskLevel, string> = {
  LOW: 'text-safe-800',
  MEDIUM: 'text-caution-800',
  HIGH: 'text-risk-800',
}

const STABILITY_LABEL: Record<string, string> = {
  STABLE: '稳定',
  WATCH: '观察中',
  TIGHTENING: '有收紧趋势',
}

/** 矩阵的列取自各行出现过的法域，保持六法域的固定顺序 */
const matrixJurisdictions = computed(() => {
  const codes = new Set<string>()
  for (const row of props.result.matrix ?? []) {
    for (const cell of row.cells) {
      codes.add(cell.jurisdictionCode)
    }
  }
  const order = ['CN', 'HK', 'SG', 'IE', 'NL', 'KY']
  return [...codes].sort((a, b) => {
    const ia = order.indexOf(a)
    const ib = order.indexOf(b)
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
  })
})

function cellOf(row: DivergenceRow, code: string): DivergenceCell | undefined {
  return row.cells.find((cell) => cell.jurisdictionCode === code)
}

function verdictClass(verdict: DivergenceVerdict): string {
  switch (verdict) {
    case 'PERMITTED':
      return 'bg-safe-50 text-safe-800 border-safe-200'
    case 'PROHIBITED':
      return 'bg-risk-50 text-risk-800 border-risk-200'
    case 'CONDITIONAL':
      return 'bg-caution-50 text-caution-800 border-caution-200'
    default:
      // 虚线边框 + 中性灰：一眼能看出"这里没有依据"，而不是"这里允许"。
      // 这一处不是控件边界，1.4.11 不管它；用 400 是为了让"虚线"这个信号
      // 真的看得见（300 在白底只有 1.49:1，虚线等于没有），同时保持它"是个空缺"
      // 的观感——提到控件那一档的 500 会让它看起来像个可点的按钮
      return 'bg-slate-50 text-slate-600 border-dashed border-slate-400'
  }
}
</script>

<template>
  <article class="space-y-5">
    <!-- 降级提示条排在最前。放在正文之后，用户会先读完一段通顺的文字，
         再看到"这一轮不能作为依据"——那时候提示已经晚了。 -->
    <DegradationNotice
      v-if="result.degradation"
      :degradation="result.degradation"
      @retry="emit('retry')"
    />

    <!-- 结论正文 -->
    <div class="card p-5">
      <div v-if="result.summary" class="mb-3 text-sm font-medium text-slate-800">
        {{ result.summary }}
      </div>
      <p class="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
        {{ result.answer }}
      </p>

      <div
        v-if="result.strippedClaims"
        class="mt-4 rounded border border-caution-200 bg-caution-50 px-3 py-2 text-xs
               leading-relaxed text-caution-800"
      >
        有 {{ result.strippedClaims }} 条结论因引用校验未通过已被剥离。
        这些句子在原始输出中存在，但无法回溯到库内法条，因此不作为结论提供。
      </div>
    </div>

    <!-- 税务筹划：候选架构对比 -->
    <section v-if="result.candidates?.length" class="card overflow-hidden">
      <header class="border-b border-slate-200 px-5 py-3">
        <h3 class="text-sm font-medium text-slate-800">
          候选架构对比（{{ result.candidates.length }} 套）
        </h3>
        <p class="mt-0.5 text-xs text-slate-600">
          <template v-if="calcTraces.length">
            表中的数字来自确定性计算模块，模型只负责解释；展开下方「计算过程」可逐项复算。
          </template>
          <template v-else>
            表中的数字来自确定性计算模块，不是模型写的；模型只负责解释。
          </template>
        </p>
      </header>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="bg-slate-50 text-xs text-slate-600">
            <tr>
              <th class="px-4 py-2 text-left font-medium">架构</th>
              <th class="px-4 py-2 text-left font-medium">综合税负率</th>
              <th class="px-4 py-2 text-left font-medium">预提税影响</th>
              <th class="px-4 py-2 text-left font-medium">合规风险</th>
              <th class="px-4 py-2 text-left font-medium">落地复杂度</th>
              <th class="px-4 py-2 text-left font-medium">依赖协定条款</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="candidate in result.candidates" :key="candidate.name">
              <td class="px-4 py-3 align-top">
                <div class="font-medium text-slate-800">{{ candidate.name }}</div>
                <CitationList
                  v-if="candidate.citations.length"
                  class="mt-1.5"
                  :citations="candidate.citations"
                  @select="emit('selectCitation', $event)"
                />
              </td>
              <td class="px-4 py-3 align-top font-mono text-slate-700">
                {{ candidate.effectiveTaxRate }}
              </td>
              <td class="px-4 py-3 align-top text-slate-600">
                {{ candidate.withholdingTaxImpact }}
              </td>
              <td class="px-4 py-3 align-top">
                <span class="font-medium" :class="RISK_TONE[candidate.complianceRisk]">
                  {{ RISK_LABEL[candidate.complianceRisk] }}
                </span>
              </td>
              <td class="px-4 py-3 align-top text-slate-600">
                {{ RISK_LABEL[candidate.implementationComplexity] }}
              </td>
              <td class="px-4 py-3 align-top text-slate-600">
                {{ candidate.dependentTreatyArticles.join('、') || '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 计算过程。默认折叠，与「假设与取值」相反：这一段回答"数字怎么算出来的"，
           是复核时才需要的第二层信息；假设回答"结论在什么前提下成立"，
           读结论时就要看到。默认状态不同是有意的。 -->
      <div v-if="calcTraces.length" class="border-t border-slate-200 bg-slate-50 px-5 py-3">
        <p v-if="result.calcNote" class="mb-2 text-xs leading-relaxed text-caution-800">
          <span class="font-medium">以下数字不是从法条读出的。</span>
          {{ result.calcNote }}
        </p>

        <details
          v-for="candidate in calcTraces"
          :key="candidate.name"
          class="group mt-2 overflow-hidden rounded border border-slate-200 bg-white"
        >
          <summary
            class="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-xs
                   font-medium text-slate-700 transition-colors hover:bg-slate-50
                   [&::-webkit-details-marker]:hidden"
          >
            <span
              class="select-none text-slate-600 transition-transform group-open:rotate-90"
              aria-hidden="true"
            >▸</span>
            计算过程 · {{ candidate.name }}
          </summary>
          <p
            class="whitespace-pre-wrap border-t border-slate-100 px-3 py-2.5 font-mono
                   text-xs leading-relaxed text-slate-600"
          >
            {{ candidate.calcTrace }}
          </p>
        </details>
      </div>
    </section>

    <!-- 监管差异：法域 × 维度矩阵 -->
    <section v-if="result.matrix?.length" class="card overflow-hidden">
      <header class="border-b border-slate-200 px-5 py-3">
        <h3 class="text-sm font-medium text-slate-800">法域规则差异矩阵</h3>
        <p class="mt-0.5 text-xs text-slate-600">
          「未检索到明确规定」表示知识库中没有可支撑的条款，<strong
            class="font-medium text-slate-600"
          >不代表允许</strong>。
        </p>
      </header>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="bg-slate-50 text-xs text-slate-600">
            <tr>
              <th class="px-4 py-2 text-left font-medium">维度</th>
              <th class="px-4 py-2 text-left font-medium">稳定性</th>
              <th
                v-for="code in matrixJurisdictions"
                :key="code"
                class="px-4 py-2 text-left font-medium"
              >
                {{ jurisdictionLabel(code) }}
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="row in result.matrix" :key="row.dimension">
              <td class="px-4 py-3 align-top font-medium text-slate-700">
                {{ row.dimension }}
              </td>
              <td class="px-4 py-3 align-top text-xs text-slate-600">
                {{ STABILITY_LABEL[row.stability] ?? row.stability }}
              </td>
              <td
                v-for="code in matrixJurisdictions"
                :key="`${row.dimension}-${code}`"
                class="px-4 py-3 align-top"
              >
                <template v-if="cellOf(row, code)">
                  <span
                    class="inline-block rounded border px-2 py-0.5 text-xs"
                    :class="verdictClass(cellOf(row, code)!.verdict)"
                  >
                    {{ DIVERGENCE_VERDICT_LABEL[cellOf(row, code)!.verdict] }}
                  </span>
                  <p
                    v-if="cellOf(row, code)!.condition"
                    class="mt-1 text-xs leading-relaxed text-slate-600"
                  >
                    {{ cellOf(row, code)!.condition }}
                  </p>
                  <CitationList
                    v-if="cellOf(row, code)!.citations.length"
                    class="mt-1.5"
                    :citations="cellOf(row, code)!.citations"
                    @select="emit('selectCitation', $event)"
                  />
                </template>
                <span v-else class="text-xs text-slate-600">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 假设与取值。降级轮次同样要渲染——而且那正是最需要看的时候：
         "本轮未产生任何引用"这类前提不写出来，用户不知道结论其实是空的。 -->
    <AssumptionList v-if="result.assumptions?.length" :assumptions="result.assumptions" />

    <footer class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
      <span v-if="result.knowledgeVersion">
        知识版本 #{{ result.knowledgeVersion }}
      </span>
      <span v-if="result.asOf">结论时点 {{ result.asOf }}</span>
      <span>本材料为研究辅助，不构成法律意见</span>
    </footer>
  </article>
</template>

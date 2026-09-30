<script setup lang="ts">
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { isEndpointMissing } from '@/api/notImplemented'
import { JURISDICTIONS, knowledgeApi } from '@/api/knowledge'

/**
 * 法规上传面板。
 *
 * 两个字段是**必填且不可省略**的，这不是表单洁癖：
 *
 * - `sourceUrl`（公开来源 URL）：`AC-1.6` 要求每条法条都能回溯到公开来源。
 *   上传时忘了登记，入库后就再也补不回来——那时已经不知道这份文本是从哪来的了。
 * - `effectiveFrom`（生效日期）：引用必须能回答"2021 年时这条法规怎么规定"（`AC-1.4`）。
 *
 * 因此两者都在**提交前**校验，而不是留给复核阶段补救。
 */
const emit = defineEmits<{ uploaded: [jobId: string] }>()

const file = ref<File | null>(null)
const dragging = ref(false)
const submitting = ref(false)
const errorMessage = ref('')
const errorTraceId = ref<string | undefined>()

const form = ref({
  jurisdictionCode: '',
  sourceUrl: '',
  effectiveFrom: '',
  effectiveTo: '',
  versionLabel: '',
  statuteTitle: '',
})

const canSubmit = computed(
  () =>
    !!file.value &&
    !!form.value.jurisdictionCode &&
    !!form.value.sourceUrl &&
    !!form.value.effectiveFrom &&
    !!form.value.versionLabel,
)

function pickFile(files: FileList | null): void {
  const picked = files?.[0] ?? null
  if (!picked) {
    return
  }
  file.value = picked
  errorMessage.value = ''
  if (!form.value.statuteTitle) {
    // 从文件名推一个初始标题，只是省一次输入，不改变用户最终填的值
    form.value.statuteTitle = picked.name.replace(/\.[^.]+$/, '')
  }
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  pickFile(event.dataTransfer?.files ?? null)
}

function reset(): void {
  file.value = null
  form.value = {
    jurisdictionCode: '',
    sourceUrl: '',
    effectiveFrom: '',
    effectiveTo: '',
    versionLabel: '',
    statuteTitle: '',
  }
}

async function submit(): Promise<void> {
  if (!file.value || !canSubmit.value || submitting.value) {
    return
  }
  submitting.value = true
  errorMessage.value = ''
  errorTraceId.value = undefined
  try {
    const result = await knowledgeApi.upload({
      file: file.value,
      jurisdictionCode: form.value.jurisdictionCode,
      sourceUrl: form.value.sourceUrl,
      effectiveFrom: form.value.effectiveFrom,
      effectiveTo: form.value.effectiveTo || undefined,
      versionLabel: form.value.versionLabel,
      statuteTitle: form.value.statuteTitle || undefined,
    })
    emit('uploaded', result.jobId)
    reset()
  } catch (error) {
    const apiError = error instanceof ApiError ? error : null
    // 上传接口的服务端尚未实现。默认的 404 文案是「请求的资源不存在」——
    // 它会让使用者去检查文件格式或网络，而真正的原因是这一步还没做。
    errorMessage.value = isEndpointMissing(error)
      ? '入库链路的服务端尚未实现（接口返回 404），上传无法完成。'
      : (apiError?.message ?? '上传失败，请稍后重试')
    errorTraceId.value = apiError?.traceId
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="card p-5">
    <h2 class="text-sm font-medium text-slate-800">上传法规原文</h2>
    <p class="mt-1 text-xs text-slate-600">
      支持含文本层的 PDF、Markdown 与纯文本。**扫描件不做 OCR**，会进入「待人工干预」队列并说明原因，不会被静默丢弃。
    </p>

    <div class="mt-4 space-y-4">
      <!-- 拖拽区用 label 包住真实 file input：既能拖拽，也保持键盘可达 -->
      <label
        class="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2
               border-dashed px-4 py-8 text-center transition-colors"
        :class="dragging ? 'border-brand-500 bg-brand-50' : 'border-slate-500 hover:bg-slate-50'"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <input
          type="file"
          class="hidden"
          accept=".pdf,.md,.markdown,.txt,.docx"
          @change="pickFile(($event.target as HTMLInputElement).files)"
        />
        <span class="text-sm text-slate-700">
          {{ file ? file.name : '点击选择文件，或把文件拖到这里' }}
        </span>
        <span v-if="file" class="mt-1 text-xs text-slate-600">
          {{ (file.size / 1024 / 1024).toFixed(1) }} MB
        </span>
      </label>

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="mb-1 block text-xs font-medium text-slate-600" for="up-jurisdiction">
            法域 <span class="text-risk-500">*</span>
          </label>
          <select
            id="up-jurisdiction"
            v-model="form.jurisdictionCode"
            class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                   focus-ring"
          >
            <option value="">请选择</option>
            <option v-for="item in JURISDICTIONS" :key="item.code" :value="item.code">
              {{ item.label }}（{{ item.code }}）
            </option>
          </select>
        </div>

        <div>
          <label class="mb-1 block text-xs font-medium text-slate-600" for="up-title">
            法规名称
          </label>
          <input
            id="up-title"
            v-model="form.statuteTitle"
            type="text"
            class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                   focus-ring"
            placeholder="留空时取文件名"
          />
        </div>

        <div class="col-span-2">
          <label class="mb-1 block text-xs font-medium text-slate-600" for="up-source">
            公开来源 URL <span class="text-risk-500">*</span>
          </label>
          <input
            id="up-source"
            v-model="form.sourceUrl"
            type="url"
            class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                   focus-ring"
            placeholder="https://…"
          />
          <p class="mt-1 text-xs text-slate-600">
            入库后无法补登。每条法条的可信度都建立在这个 URL 上。
          </p>
        </div>

        <div>
          <label class="mb-1 block text-xs font-medium text-slate-600" for="up-from">
            生效日期 <span class="text-risk-500">*</span>
          </label>
          <input
            id="up-from"
            v-model="form.effectiveFrom"
            type="date"
            class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                   focus-ring"
          />
        </div>

        <div>
          <label class="mb-1 block text-xs font-medium text-slate-600" for="up-to">
            失效日期
          </label>
          <input
            id="up-to"
            v-model="form.effectiveTo"
            type="date"
            class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                   focus-ring"
          />
          <p class="mt-1 text-xs text-slate-600">留空表示仍然有效</p>
        </div>

        <div class="col-span-2">
          <label class="mb-1 block text-xs font-medium text-slate-600" for="up-version">
            版本标签 <span class="text-risk-500">*</span>
          </label>
          <input
            id="up-version"
            v-model="form.versionLabel"
            type="text"
            class="w-full rounded-md border border-slate-500 px-3 py-1.5 text-sm
                   focus-ring"
            placeholder="例如：2024 修订版"
          />
        </div>
      </div>

      <p v-if="errorMessage" class="text-xs text-risk-800">
        {{ errorMessage }}
        <span v-if="errorTraceId" class="ml-1 font-mono text-slate-600">
          traceId {{ errorTraceId }}
        </span>
      </p>

      <div class="flex items-center gap-3">
        <button
          type="button"
          :disabled="!canSubmit || submitting"
          class="rounded-md bg-brand-600 px-4 py-1.5 text-sm font-medium text-white
                 transition-colors enabled:hover:bg-brand-700
                 disabled:cursor-not-allowed disabled:bg-slate-300"
          @click="submit"
        >
          {{ submitting ? '上传中' : '上传并入库' }}
        </button>
        <span v-if="!canSubmit" class="text-xs text-slate-600">
          标注 * 的字段为必填
        </span>
      </div>
    </div>
  </section>
</template>

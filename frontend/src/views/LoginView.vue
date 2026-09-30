<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

/**
 * 登录页。
 *
 * 表单里刻意**没有租户下拉框之外的任何租户输入**，也没有"记住我"——
 * 租户必须显式选择而不是从上次会话继承，因为一个用户同时为多个律所工作时，
 * 沿用上次的选择会让他在错误的租户上下文里操作而毫无察觉。
 */
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const tenantCode = ref('')
const username = ref('')
const password = ref('')
/** 展示用的字段错误。提交前不发请求，减少无意义的往返。 */
const touched = ref(false)

const tenantError = computed(() =>
  touched.value && tenantCode.value.trim() === '' ? '请填写租户标识' : '',
)
const usernameError = computed(() =>
  touched.value && username.value.trim() === '' ? '请填写用户名' : '',
)
const passwordError = computed(() =>
  touched.value && password.value === '' ? '请填写密码' : '',
)

const formValid = computed(
  () =>
    tenantCode.value.trim() !== '' &&
    username.value.trim() !== '' &&
    password.value !== '',
)

async function onSubmit(): Promise<void> {
  touched.value = true
  if (!formValid.value || auth.loading) return

  const ok = await auth.login({
    tenantCode: tenantCode.value.trim(),
    username: username.value.trim(),
    // 密码不做 trim：首尾空格可能是密码的一部分，静默去掉会导致
    // "我明明输对了却登不上"，而用户无从知道发生了什么
    password: password.value,
  })

  if (ok) {
    const redirect = route.query.redirect
    await router.replace(typeof redirect === 'string' ? redirect : { name: 'consult' })
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-slate-100 px-4">
    <div class="w-full max-w-md">
      <!-- 品牌区 -->
      <div class="mb-8 text-center">
        <h1 class="text-2xl font-semibold tracking-tight text-slate-900">
          LexBridge <span class="text-brand-600">法桥</span>
        </h1>
        <p class="mt-2 text-sm text-slate-600">
          跨境法律咨询与合规分析平台
        </p>
      </div>

      <form class="card p-6 space-y-4" novalidate @submit.prevent="onSubmit">
        <div>
          <label for="tenant" class="block text-sm font-medium text-slate-700">
            租户标识
          </label>
          <input
            id="tenant"
            v-model="tenantCode"
            type="text"
            autocomplete="organization"
            class="mt-1 w-full rounded-md border px-3 py-2 text-sm
                   focus-ring"
            :class="tenantError ? 'border-risk-500' : 'border-slate-500'"
            placeholder="例如 acme-law"
          />
          <p v-if="tenantError" class="mt-1 text-xs text-risk-800">{{ tenantError }}</p>
        </div>

        <div>
          <label for="username" class="block text-sm font-medium text-slate-700">
            用户名
          </label>
          <input
            id="username"
            v-model="username"
            type="text"
            autocomplete="username"
            class="mt-1 w-full rounded-md border px-3 py-2 text-sm
                   focus-ring"
            :class="usernameError ? 'border-risk-500' : 'border-slate-500'"
          />
          <p v-if="usernameError" class="mt-1 text-xs text-risk-800">{{ usernameError }}</p>
        </div>

        <div>
          <label for="password" class="block text-sm font-medium text-slate-700">
            密码
          </label>
          <input
            id="password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            class="mt-1 w-full rounded-md border px-3 py-2 text-sm
                   focus-ring"
            :class="passwordError ? 'border-risk-500' : 'border-slate-500'"
          />
          <p v-if="passwordError" class="mt-1 text-xs text-risk-800">{{ passwordError }}</p>
        </div>

        <!--
          错误提示统一文案，不区分"用户不存在"与"密码错误"。
          区分开来等于对外提供了一个账号枚举接口。
        -->
        <p
          v-if="auth.error"
          role="alert"
          class="rounded-md bg-risk-50 border border-risk-200 px-3 py-2 text-sm text-risk-800"
        >
          {{ auth.error }}
        </p>

        <button
          type="submit"
          :disabled="auth.loading"
          class="w-full rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white
                 hover:bg-brand-700 disabled:opacity-60 disabled:cursor-not-allowed
                 transition-colors"
        >
          {{ auth.loading ? '登录中…' : '登录' }}
        </button>
      </form>

      <p class="mt-6 text-center text-xs text-slate-600">
        本系统提供的分析仅供参考，不构成法律意见。
      </p>
    </div>
  </div>
</template>

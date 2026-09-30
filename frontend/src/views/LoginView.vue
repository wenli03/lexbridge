<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { authApi, type DemoAccount } from '@/api/auth'
import { landingRouteFor } from '@/router'
import { useAuthStore } from '@/stores/auth'

/**
 * 登录页。
 *
 * 表单里刻意**没有租户下拉框之外的任何租户输入**，也没有"记住我"——
 * 租户必须显式选择而不是从上次会话继承，因为一个用户同时为多个律所工作时，
 * 沿用上次的选择会让他在错误的租户上下文里操作而毫无察觉。
 *
 * ## 关于"一键进入演示"
 *
 * 本系统**没有自助注册**，而且这不是待补的功能：多租户企业工具的账号由管理员
 * 开设（PRD 里"管理本租户成员"是租户管理员的能力）。但这对一个刚 clone 下来的人
 * 意味着他会停在这一页——"租户标识"不是能猜出来的东西。
 *
 * 所以这里把**已经公开写在 README 里的演示凭据**渲染成按钮。点下去走的是
 * **真实的登录流程**，JWT 签发、租户解析、审计留痕一个都不少；它省掉的只是
 * "先去文档里翻租户标识"这一步。服务端在演示模式关闭时返回 `enabled=false`，
 * 那一块整个不渲染——生产部署上不会出现任何入口。
 */
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

/**
 * 演示入口。
 *
 * 取不到（网络故障、或服务端关掉了演示模式）时静默留空：
 * 登录页的主路径是表单，演示入口是锦上添花，不能因为它失败就让整页不可用。
 * 因此这里**不**用到 StateBlock 去显示错误——那会把一个可选功能的问题
 * 呈现成"登录页坏了"。
 */
const demoAccounts = ref<DemoAccount[]>([])
const demoLoading = ref(false)

onMounted(async () => {
  try {
    const result = await authApi.demoAccounts()
    demoAccounts.value = result.enabled ? result.accounts : []
  } catch {
    demoAccounts.value = []
  }
})

/** 用某个演示账号登录：填表 → 走与手填完全相同的提交路径。 */
async function loginAs(account: DemoAccount): Promise<void> {
  if (auth.loading) return
  demoLoading.value = true
  tenantCode.value = account.tenantCode
  username.value = account.username
  password.value = account.password
  try {
    await submit()
  } finally {
    demoLoading.value = false
  }
}

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

function onSubmit(): void {
  void submit()
}

/**
 * 提交登录。
 *
 * 表单提交与"一键进入演示"共用这一条路径——两处各写一份的话，
 * 演示入口会逐渐漂移成一条"看起来一样、行为不同"的捷径，
 * 而那正是最不该出现的分歧：被演示的那条路必须是真实的那条。
 */
async function submit(): Promise<void> {
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
    if (typeof redirect === 'string') {
      await router.replace(redirect)
      return
    }
    // 没有指定去处时，送去**该角色真正的第一页**，而不是写死某一页。
    //
    // 这里原本写死 `{ name: 'consult' }`，后果对演示是致命的：
    // 访客点「系统管理员」进来，落在「法律咨询」上，而那一页的服务端尚未实现——
    // 第一眼看到的是"本功能未实现"。而管理员真正的主工作面（知识库，
    // 里面有 2,562 条真实法条）就在旁边一项。
    const landing = landingRouteFor(auth)
    await router.replace(landing === false ? '/' : landing)
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

      <!--
        演示入口。仅在服务端启用了演示模式时渲染（见 script 里的说明）。
        放在表单**上方**而不是下方：访客的第一屏就是它，不必先滚动或先试错。
      -->
      <div v-if="demoAccounts.length" class="mb-4">
        <div class="card p-4">
          <p class="text-xs font-medium text-slate-700">一键进入演示</p>
          <p class="mt-1 text-xs leading-relaxed text-slate-600">
            本系统没有自助注册——账号由管理员开设。下面三个是预置的演示账号，
            点击后走的是真实的登录流程。
          </p>
          <div class="mt-3 space-y-2">
            <button
              v-for="account in demoAccounts"
              :key="account.username"
              type="button"
              :disabled="auth.loading || demoLoading"
              class="w-full rounded-md border border-brand-300 bg-brand-50 px-3 py-2 text-left
                     transition-colors hover:bg-brand-100
                     disabled:opacity-60 disabled:cursor-not-allowed focus-ring"
              @click="loginAs(account)"
            >
              <span class="flex items-baseline justify-between gap-2">
                <span class="text-sm font-medium text-brand-700">
                  {{ account.displayName }}
                </span>
                <span class="shrink-0 font-mono text-xs text-slate-600">
                  {{ account.tenantCode }} / {{ account.username }}
                </span>
              </span>
              <span class="mt-0.5 block text-xs text-slate-600">
                {{ account.scope }}
              </span>
            </button>
          </div>
        </div>

        <div class="my-4 flex items-center gap-3">
          <span class="h-px flex-1 bg-slate-200"></span>
          <span class="text-xs text-slate-600">或手动填写</span>
          <span class="h-px flex-1 bg-slate-200"></span>
        </div>
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

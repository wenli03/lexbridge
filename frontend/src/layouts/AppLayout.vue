<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { ROLE_LABEL } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

/**
 * 主应用布局。
 *
 * 菜单按角色过滤。再次强调：**这只是体验优化，不是安全措施**——
 * 隐藏入口改变不了任何人直接构造请求。真正拦截在后端的租户拦截器与 RLS。
 */
const auth = useAuthStore()
const router = useRouter()

interface MenuItem {
  key: string
  label: string
  to: string
  hint: string
}

const ALL_MENUS: MenuItem[] = [
  { key: 'consult', label: '法律咨询', to: '/consult', hint: '税务筹划与监管差异分析' },
  // 紧跟「法律咨询」：这两页是一件事的两面——咨询页干活，历史页找活。
  // 顺序还决定了各角色的落地页（守卫取第一个有权访问的），所以它不能随手放
  { key: 'history', label: '会话历史', to: '/history', hint: '历史咨询的检索与回看' },
  { key: 'knowledge', label: '知识库', to: '/knowledge', hint: '法规上传与发布' },
  { key: 'review', label: '入库复核', to: '/review', hint: '抽取结果人工确认' },
  { key: 'audit', label: '审计日志', to: '/audit', hint: '操作留痕（只读）' },
]

const menus = computed(() => ALL_MENUS.filter((m) => auth.canAccess(m.key)))

const roleLabel = computed(() => (auth.user ? ROLE_LABEL[auth.user.role] : ''))

async function onLogout(): Promise<void> {
  await auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="min-h-screen flex bg-slate-50">
    <!-- 侧边栏 -->
    <aside class="w-60 shrink-0 border-r border-slate-200 bg-white flex flex-col">
      <div class="h-16 flex items-center px-5 border-b border-slate-200">
        <span class="text-lg font-semibold tracking-tight text-slate-900">
          LexBridge <span class="text-brand-600">法桥</span>
        </span>
      </div>

      <nav class="flex-1 p-3 space-y-1">
        <RouterLink
          v-for="item in menus"
          :key="item.key"
          :to="item.to"
          class="block rounded-md px-3 py-2 text-sm transition-colors"
          active-class="bg-brand-50 text-brand-700 font-medium"
          :class="'text-slate-600 hover:bg-slate-50'"
        >
          <span class="block">{{ item.label }}</span>
          <span class="mt-0.5 block text-xs text-slate-600">{{ item.hint }}</span>
        </RouterLink>
      </nav>

      <!-- 当前租户与身份。多租户系统里这条信息必须常驻可见：
           用户在错误的租户下操作却不自知，是这个系统最危险的失效模式。 -->
      <div v-if="auth.user" class="border-t border-slate-200 p-4">
        <div class="text-xs text-slate-600">当前租户</div>
        <div class="mt-0.5 truncate text-sm font-medium text-slate-800">
          {{ auth.user.tenantName }}
        </div>
        <div class="mt-2 text-xs text-slate-600">身份</div>
        <div class="mt-0.5 text-sm text-slate-700">
          {{ auth.user.displayName }}
          <span class="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
            {{ roleLabel }}
          </span>
        </div>
        <button
          class="mt-3 w-full rounded-md border border-slate-500 px-3 py-1.5 text-xs
                 text-slate-600 hover:bg-slate-50 transition-colors"
          @click="onLogout"
        >
          退出登录
        </button>
      </div>
    </aside>

    <!-- 内容区 -->
    <div class="flex-1 min-w-0 flex flex-col">
      <header class="h-16 shrink-0 flex items-center justify-between border-b
                     border-slate-200 bg-white px-6">
        <h1 class="text-base font-medium text-slate-800">
          {{ $route.meta.title ?? '' }}
        </h1>
      </header>

      <main class="flex-1 min-h-0 overflow-auto p-6">
        <RouterView />
      </main>
    </div>
  </div>
</template>

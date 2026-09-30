import {
  createRouter,
  createWebHistory,
  type RouteLocationRaw,
  type RouteRecordRaw,
} from 'vue-router'

import { useAuthStore } from '@/stores/auth'

/**
 * 路由定义。
 *
 * `meta.requiresAuth` 与 `meta.menu` 是两条不同的约束，刻意分开：
 *   requiresAuth —— 是否需要登录
 *   menu         —— 需要哪个菜单权限
 *
 * 合成一个字段会让"登录即可访问"与"需要特定角色"混在一起，
 * 而这两者在评审权限设计时必须能分别回答。
 *
 * **前端的所有权限判断都只是体验优化。** 真正的强制在后端——
 * 前端隐藏一个入口，改变不了任何人直接构造请求。把前端守卫当安全措施，
 * 是多租户系统里最常见也最危险的误解。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        // 用函数而不是写死某一页：写死会让"访问根路径"与角色脱钩。
        // 函数在 store 尚未恢复时会返回登录页（见 landingRouteFor），
        // 而守卫的 `to.meta.public` 分支随即把已登录的人送回正确的落地页——
        // 兜了一圈，但每一步都是确定的，不会停在一个该角色进不去的页面上。
        // `redirect` 不接受 `false`（那是"中止导航"的语义，只有守卫能给），
        // 因此把 `landingRouteFor` 的兜底值在这里换成登录页：
        // 已登录但没有任何可进页面时，登录页的守卫会以 `false` 中止导航，
        // 人停在原地而不是空转——不会形成重定向环。
        redirect: () => {
          const landing = landingRouteFor(useAuthStore())
          return landing === false ? { name: 'login' } : landing
        },
      },
      {
        path: 'consult',
        name: 'consult',
        component: () => import('@/views/ConsultView.vue'),
        meta: { menu: 'consult', title: '法律咨询' },
      },
      {
        // 执业律师的第二个入口。此前 `MENU_BY_ROLE.LAWYER` 里已经写着
        // `history`，但既没有这条路由，`AppLayout.ALL_MENUS` 里也没有对应项，
        // 于是律师的侧栏实际只剩「法律咨询」一项——菜单矩阵声明了一个不存在的地方。
        path: 'history',
        name: 'history',
        component: () => import('@/views/HistoryView.vue'),
        meta: { menu: 'history', title: '会话历史' },
      },
      {
        path: 'knowledge',
        name: 'knowledge',
        component: () => import('@/views/KnowledgeView.vue'),
        meta: { menu: 'knowledge', title: '知识库' },
      },
      {
        path: 'review',
        name: 'review',
        component: () => import('@/views/ReviewView.vue'),
        meta: { menu: 'review', title: '入库复核' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/views/AuditView.vue'),
        meta: { menu: 'audit', title: '审计日志' },
      },
    ],
  },
  {
    // 兜底到首页而不是做一个 404 页面：
    // 这是个内部工具，用户输错路径时最想要的是"回到能干活的地方"
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/**
 * 某个角色真正能进的第一页。
 *
 * **这里原来硬编码 `{ name: 'consult' }`，是一个会把界面卡死的缺陷。**
 * 合规官的菜单只有 `audit`，而 `consult` 自己也带 `meta.menu`——于是他：
 * 登录后被送到 /consult，守卫判定无权，送回 /consult，再判无权……
 * Vue Router 会检测到原地重定向并以「无限重定向」中止这次导航，
 * 用户看到的是一片空白，且没有任何提示。同一段代码还守着
 * `to.meta.public` 那条分支，所以合规官连登录都走不完。
 *
 * 兜底目标必须是**该角色真能进的那一页**，而不是"默认那一页"。
 * 写成「从路由表里挑第一个他有权限的」，菜单矩阵以后怎么改，
 * 这里都自动跟着变，不会再出现两处对不上的情况。
 *
 * 返回 `false` 的分支是一个不可能状态（某个角色在矩阵里被配成空数组）：
 * 那时没有任何页面可去，回登录页又会被再弹回来，所以直接放行当前导航，
 * 让界面停在原地而不是空转。
 */
export function landingRouteFor(auth: ReturnType<typeof useAuthStore>): RouteLocationRaw | false {
  const shell = routes.find((record) => Array.isArray(record.children))
  const children = shell?.children ?? []

  // **按角色自己的菜单顺序找，而不是按路由表的顺序。**
  //
  // `MENU_BY_ROLE` 里的顺序是有意的——管理员的第一项是「知识库」，
  // 那是他的主工作面。而路由表的顺序只是代码组织方式（consult 恰好写在最前）。
  // 用路由表顺序会让管理员落在「法律咨询」上，而那一页的服务端尚未实现：
  // 访客点「一键进入演示」，第一眼看到的是"本功能未实现"——
  // 一个纯粹由排序造成的、最差的观感。
  for (const menu of auth.visibleMenus) {
    const match = children.find((child) => child.meta?.menu === menu)
    if (typeof match?.name === 'string') {
      return { name: match.name }
    }
  }

  // 走不到这里（守卫的另一条分支已排除无权限的角色）。
  // 保留兜底：宁可停在原地，也不要返回一个会引发无限重定向的目标。
  return auth.isAuthenticated ? false : { name: 'login' }
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 刷新页面后 store 为空但令牌可能仍有效。必须先尝试恢复，
  // 否则会被误判为未登录而踢到登录页——用户会看到"刚登录又被登出"。
  if (!auth.initialized) {
    await auth.restore()
  }

  if (to.meta.public) {
    // 已登录用户访问登录页，直接送回工作台
    return auth.isAuthenticated ? landingRouteFor(auth) : true
  }

  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    // 记下原本要去的地方，登录后回到那里而不是一律回首页
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  const menu = to.meta.menu as string | undefined
  if (menu && !auth.canAccess(menu)) {
    // 角色无权访问。不弹错误，直接回落到**该角色能进的**页面——
    // 手动输 URL 是常见行为，为此弹一个错误框属于过度反应。
    // 注意不能写成固定的某一页：那一页本身可能也是这个角色进不去的，
    // 于是守卫把用户送过去、再判一次无权，就成了死循环。
    return landingRouteFor(auth)
  }

  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · LexBridge 法桥` : 'LexBridge 法桥'
})

export default router

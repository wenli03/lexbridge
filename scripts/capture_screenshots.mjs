/**
 * 用真实浏览器截取各角色、各页面的截图，产出 `docs/images/*.png`。
 *
 * ## 为什么截图要由脚本生成，而不是手工另存
 *
 * 手工截图无法复现：界面改了一处间距，没人知道要不要重截。脚本至少让
 * "这一批图是怎么来的"是可读的，也让下一次更新只需重跑一条命令。
 *
 * ## 前置
 *
 *   docker compose up -d          # 站点要在 http://localhost:8088 上
 *   npm i -D playwright           # 本脚本依赖 playwright（未列入 package.json：
 *                                 # 它只在维护文档时需要，不想让每次 npm ci 都装它）
 *   npx playwright install chromium
 *   node scripts/capture_screenshots.mjs
 *
 * 端口可用 WEB_PORT 环境变量覆盖（默认 8088）。
 *
 * ## 截图里出现的都是真实数据
 *
 * 语料来自 `deploy/seed/corpus`（2,562 条真实法条，带来源 URL），
 * 账号来自 `DemoDataBootstrap`。**没有任何一处是为了好看而造的假数据**——
 * 同一个项目里，"README 说有什么"与"截图里能看到什么"必须是一回事。
 */

import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUT_DIR = resolve(ROOT, 'docs', 'images')
const BASE = `http://localhost:${process.env.WEB_PORT ?? 8088}`
const PASSWORD = 'LexBridge@2026'

/** 截图统一尺寸。1440×900 是常见笔记本视口，2 倍缩放让文字在 README 里清晰。 */
const VIEWPORT = { width: 1440, height: 900 }

const shots = []

async function login(page, tenant, username) {
  // 先清掉上一个角色的令牌再进登录页。
  //
  // **不能直接 goto /login**：路由守卫会把已登录的用户送回工作台
  // （`router/index.ts` 里 `to.meta.public` 那一条），于是登录表单根本不渲染，
  // 脚本会以"等不到 #tenant"超时——看起来像选择器写错了，实际是会话没清。
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(() => sessionStorage.clear())
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })

  await page.fill('#tenant', tenant)
  await page.fill('#username', username)
  await page.fill('#password', PASSWORD)
  await page.click('button[type="submit"]')
  // 登录成功后路由会跳到该角色的落地页；等菜单出现比等固定超时可靠
  await page.waitForSelector('nav, aside', { timeout: 15000 })
  await page.waitForLoadState('networkidle')
}

async function shot(page, name, note) {
  await page.waitForTimeout(400) // 让过渡动画结束，避免截到半透明的中间态
  const path = resolve(OUT_DIR, name)
  await page.screenshot({ path })
  shots.push({ name, note })
  console.log(`  captured ${name}  (${note})`)
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true })

  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    locale: 'zh-CN',
  })
  const page = await context.newPage()

  console.log(`capturing from ${BASE}`)

  // --- 登录页（未登录状态）-------------------------------------------------
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  await shot(page, '01-login.png', '登录页')

  // --- 管理员 --------------------------------------------------------------
  await login(page, 'demo-law', 'admin')

  // 知识库：默认落地页是 consult（未实现），所以显式导航到 knowledge
  await page.goto(`${BASE}/knowledge`, { waitUntil: 'networkidle' })
  await page.waitForSelector('table tbody tr', { timeout: 15000 })
  await shot(page, '02-knowledge-list.png', '知识库列表：真实语料 8 部法规，IE/NL 两法域')

  // 法条详情：点第一行打开右侧面板
  await page.click('table tbody tr:first-child')
  await page.waitForTimeout(500)
  await shot(page, '03-statute-detail.png', '法规详情面板（含来源 URL）')
  await page.keyboard.press('Escape')
  await page.mouse.click(20, 400)

  // 审计日志
  await page.goto(`${BASE}/audit`, { waitUntil: 'networkidle' })
  await page.waitForSelector('table tbody tr', { timeout: 15000 })
  await shot(page, '04-audit-log.png', '审计日志：登录与查询留痕')

  // 未实现的页面（如实标注，不是红色报错）
  await page.goto(`${BASE}/consult`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  await shot(page, '05-consult-not-implemented.png', '法律咨询：服务端未实现（如实说明）')

  await page.goto(`${BASE}/review`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  await shot(page, '06-review-not-implemented.png', '入库复核：服务端未实现（如实说明）')

  // --- 律师：菜单只有两项，且两项都未实现 ---------------------------------
  await login(page, 'demo-law', 'lawyer')
  await page.waitForTimeout(800)
  await shot(page, '07-lawyer-landing.png', '律师落地页：菜单仅「法律咨询 / 会话历史」')

  await page.goto(`${BASE}/history`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  await shot(page, '08-lawyer-history.png', '会话历史：依赖咨询链路，服务端未实现')

  // --- 合规官：只有审计一项 ------------------------------------------------
  await login(page, 'demo-law', 'compliance')
  await page.waitForSelector('table tbody tr', { timeout: 15000 })
  await shot(page, '09-compliance-audit.png', '合规官落地页：只有审计日志一项')

  await browser.close()

  console.log(`\n${shots.length} screenshots written to docs/images/`)
}

main().catch((error) => {
  console.error('\nscreenshot capture failed:', error.message)
  process.exit(1)
})
